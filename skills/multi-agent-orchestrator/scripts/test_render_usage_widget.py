"""Rendering regressions; no live provider calls or session-state writes."""
import base64
from datetime import datetime, timezone, timedelta
import gzip
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

import render_usage_widget as widget


class RenderingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(widget, '_worker_registry', return_value={})
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_closed_sessions_use_exact_recency_and_stop_at_eight(self):
        # Sessions older than seven days are closed; all share age_days, in oldest-first input order.
        nodes = [dict(thread_id=f'{i:08x}-thread', title=f'Session {i}',
                      updated_at=1000+i, age_days=8, cwd='/tmp', status='closed')
                 for i in range(12)]
        _, closed, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'codex': nodes}}, {})
        self.assertEqual(re.findall(r'data-node-id="closed-cdx-([^"]+)"', closed),
                         [f'{i:08x}' for i in range(11, 3, -1)])

    def test_parent_child_limit_includes_injected_workers(self):
        # Rows are capped at six per parent: one injected Codex worker plus five recent AGY children.
        now = datetime.now(timezone.utc)
        iso = now.isoformat()
        parent = dict(conversation_id='521b781e-43da-4647-b411-585e65e5f002',
                      status='RUNNING', last_modified_time=iso, children=[
                          dict(conversation_id=f'{i:08x}-child', role=f'Child {i}', last_modified_time=iso)
                          for i in range(20)])
        worker = dict(thread_id='12345678-worker', title='worker', cwd='/home/u24c9g5090r64/dev',
                      status='closed', source='exec', updated_at=now.timestamp() - 5)
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'antigravity': [parent], 'codex': [worker]}}, {})
        self.assertEqual(len(re.findall(r'data-node-id="node-sub-', active)), 6)
        self.assertIn('data-node-id="node-sub-cdx-12345678-worker"', active)
        self.assertIn('Child 4</span>', active)
        self.assertNotIn('Child 5</span>', active)

    def test_origin_mark_is_text_prefix_only_for_agy_origin(self):
        # AGY-origin roots are internal workers unless pinned; the mark is a leading "✦" in the name.
        now = datetime.now(timezone.utc).timestamp()
        nodes = [dict(thread_id=f'{i:08x}-thread', title='Agent', updated_at=now - i,
                      status='running', source='cli', agy_origin=origin)
                 for i, origin in enumerate((True, False), 1)]
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'codex': nodes}}, {'pinned': ['node-cdx-00000001-thread']})
        self.assertEqual(active.count('<span class="agent-name">✦ Agent</span>'), 1)
        self.assertEqual(active.count('<span class="agent-name">Agent</span>'), 1)
        self.assertNotIn('agy-origin-badge', active)
        self.assertNotIn(chr(0x1fa90), active)

    def test_standalone_bundle_roundtrips_and_stays_below_55kb(self):
        template = Path(widget.__file__).with_name(widget.TEMPLATE_FILENAME).read_text()
        nodes = [dict(thread_id=f'{i:08x}-thread', title=f'Thread {i}',
                      updated_at=1000+i, status='closed', cwd='/tmp') for i in range(20)]
        with mock.patch.object(widget, '_load_persisted_widget_state', return_value={}):
            original = widget.inject_live_data(template, {'agent_trees': {'codex': nodes}})
        bundled = widget.compact_widget_html(original)
        payload = re.search(r'atob\("([^"]+)"\)', bundled).group(1)
        restored = gzip.decompress(base64.b64decode(payload)).decode()
        # Bundling intentionally drops ordinary comments and indentation, but keeps live-data markers.
        expected = re.sub(r"/\*(?!__).*?\*/", "", original, flags=re.S)
        expected = re.sub(r"<!--(?!__).*?-->", "", expected, flags=re.S)
        expected = re.sub(r"\n[ \t]+", "\n", expected)
        self.assertEqual(restored, expected)
        self.assertLess(len(restored), len(original))
        for marker in ('__SERVER_STATE_JSON_START__', '__ACCOUNTS_JSON_START__', '__ACTIVE_TREE_HTML_START__'):
            self.assertIn(marker, restored)
        self.assertLess(len(bundled.encode()), 55000)

    def test_child_nodes_ordering_and_cron_at_bottom(self):
        now = datetime.now(timezone.utc)
        iso_now = now.isoformat()
        iso_old = (now - timedelta(minutes=10)).isoformat()
        iso_done = (now - timedelta(seconds=20)).isoformat()
        cid = 'testparent-0001-0000-0000-000000000001'
        parent = dict(
            conversation_id=cid,
            status='IDLE',
            last_modified_time=iso_old,
            children=[
                dict(conversation_id='sub-done-001', role='Completed Subagent', last_modified_time=iso_done, status='CASCADE_RUN_STATUS_DONE'),
                dict(conversation_id='sub-run-001', role='Running Subagent', last_modified_time=iso_now, status='CASCADE_RUN_STATUS_RUNNING'),
            ],
        )
        fake_bg = [
            {'id': 'task-cron-1', 'short_id': 'task-cron-1', 'desc': 'Cron: */15 * * * *', 'kind': 'cron', 'cron_expr': '*/15 * * * *', 'created_at': iso_old, 'done': False},
            {'id': 'task-job-1', 'short_id': 'task-job-1', 'desc': 'pytest run', 'kind': 'job', 'created_at': iso_now, 'done': False},
        ]
        with mock.patch.object(widget, '_extract_active_bg_tasks', return_value=fake_bg):
            active, _, _, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'antigravity': [parent]}}, {}
            )
            # 1. Subagents must appear first (Running Subagent -> Completed Subagent)
            pos_run_sub = active.find('Running Subagent')
            pos_done_sub = active.find('Completed Subagent')
            pos_job = active.find('pytest run')
            pos_cron = active.find('Cron: */15 * * * *')

            self.assertNotEqual(pos_run_sub, -1)
            self.assertNotEqual(pos_done_sub, -1)
            self.assertNotEqual(pos_job, -1)
            self.assertNotEqual(pos_cron, -1)

            # Strict ordering: running subagent < completed subagent < command task < cron
            self.assertLess(pos_run_sub, pos_done_sub)
            self.assertLess(pos_done_sub, pos_job)
            self.assertLess(pos_job, pos_cron)

            # 2. Tree elbow connectors: cron is the last item so it must have └─, earlier items have ├─
            self.assertIn('└─', active[pos_cron - 350:pos_cron])
            self.assertIn('├─', active[pos_run_sub - 350:pos_run_sub])

            # 3. Cron indicator: calm idle-fresh status dot, SVG repeat icon, ctx-time-txt with 15m
            self.assertIn('status-dot idle-fresh', active[pos_cron - 350:pos_cron])
            self.assertIn('ctx-repeat-ico', active[pos_cron:pos_cron + 1000])
            self.assertIn('<span class="ctx-time-txt">15m</span>', active[pos_cron:pos_cron + 1000])

    def test_cron_alone_does_not_force_parent_running(self):
        now = datetime.now(timezone.utc)
        iso_old = (now - timedelta(hours=2)).isoformat()
        cid = 'testparent-0002-0000-0000-000000000002'
        parent = dict(
            conversation_id=cid,
            status='IDLE',
            last_modified_time=iso_old,
            children=[],
        )
        fake_bg = [
            {'id': 'task-cron-2', 'short_id': 'task-cron-2', 'desc': 'Cron: 0 * * * *', 'kind': 'cron', 'cron_expr': '0 * * * *', 'created_at': iso_old, 'done': False},
        ]
        with mock.patch.object(widget, '_extract_active_bg_tasks', return_value=fake_bg):
            active, _, _, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'antigravity': [parent]}}, {}
            )
            # data-running="1" must NOT be present on parent node
            parent_match = re.search(r'data-node-id="node-agy-testpare"[^>]*>', active)
            self.assertIsNotNone(parent_match)
            self.assertNotIn('data-running="1"', parent_match.group(0))

    def test_server_restart_cleans_stale_tasks(self):
        import tempfile
        now_dt = datetime.now(timezone.utc)
        ts_past = (now_dt - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_restart = (now_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_recent = (now_dt - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            json.dumps({"step_index": 1, "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/15 * * * *"}}], "created_at": ts_past}),
            json.dumps({"step_index": 2, "content": "background task with task id: test/task-100\nTask Description: Cron: */15 * * * *", "created_at": ts_past}),
            json.dumps({"step_index": 3, "content": "All your subagents and background tasks have been stopped due to server restart", "created_at": ts_restart}),
            json.dumps({"step_index": 4, "tool_calls": [{"name": "run_command", "args": {"IsDaemon": True}}], "created_at": ts_recent}),
            json.dumps({"step_index": 5, "content": "background task with task id: test/task-200\nTask Description: python server.py --port 8080", "created_at": ts_recent}),
        ]
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            tpath = tf.name
        try:
            with mock.patch("os.path.expanduser", return_value=tpath), mock.patch("os.path.exists", return_value=True):
                tasks = widget._extract_active_bg_tasks("dummy-cid", datetime.now(timezone.utc).timestamp())
                task_ids = [t["short_id"] for t in tasks]
                self.assertNotIn("task-100", task_ids)
                self.assertIn("task-200", task_ids)
        finally:
            if os.path.exists(tpath):
                os.remove(tpath)

    def test_completed_subagent_filtered_after_65s(self):
        now = datetime.now(timezone.utc)
        iso_fresh = (now - timedelta(seconds=30)).isoformat()
        iso_expired = (now - timedelta(seconds=70)).isoformat()
        cid = 'testparent-0003-0000-0000-000000000003'
        parent = dict(
            conversation_id=cid,
            status='IDLE',
            last_modified_time=iso_fresh,
            children=[
                dict(conversation_id='sub-fresh-001', role='Fresh Subagent', last_modified_time=iso_fresh, status='CASCADE_RUN_STATUS_DONE'),
                dict(conversation_id='sub-expired-001', role='Expired Subagent', last_modified_time=iso_expired, status='CASCADE_RUN_STATUS_DONE'),
            ],
        )
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'antigravity': [parent]}}, {}
        )
        self.assertIn('Fresh Subagent', active)
        self.assertNotIn('Expired Subagent', active)
        self.assertIn('data-idle-age="30"', active)
        self.assertIn('class="tree-node sub-node sub-ripening"', active)

    def test_filter_worker_start_jobs(self):
        import tempfile
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lines = [
            json.dumps({
                "step_index": 1,
                "tool_calls": [{
                    "name": "run_command",
                    "args": {"CommandLine": "/home/mjserver/.local/bin/maestro worker-start --pool auto --spec 'fix bug'"}
                }],
                "created_at": now_iso
            }),
            json.dumps({
                "step_index": 2,
                "content": "background task with task id: test/task-301\nTask Description: Run command: /home/mjserver/.local/bin/maestro worker-start --pool auto",
                "created_at": now_iso
            }),
            json.dumps({
                "step_index": 3,
                "tool_calls": [{
                    "name": "run_command",
                    "args": {"CommandLine": "npm run test"}
                }],
                "created_at": now_iso
            }),
            json.dumps({
                "step_index": 4,
                "content": "background task with task id: test/task-302\nTask Description: Run command: npm run test",
                "created_at": now_iso
            }),
        ]
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            tpath = tf.name
        try:
            with mock.patch("os.path.expanduser", return_value=tpath), mock.patch("os.path.exists", return_value=True):
                tasks = widget._extract_active_bg_tasks("dummy-cid", now.timestamp())
                task_ids = [t["short_id"] for t in tasks]
                self.assertNotIn("task-301", task_ids)
                self.assertIn("task-302", task_ids)
        finally:
            if os.path.exists(tpath):
                os.remove(tpath)

    def test_cron_quote_and_hourly_formatting(self):
        badge, tooltip = widget._fmt_cron_short('"7 * * * *"')
        self.assertEqual(badge, '1h')
        self.assertEqual(tooltip, '매시 7분 (7 * * * *)')

        badge, tooltip = widget._fmt_cron_short('7 * * * *')
        self.assertEqual(badge, '1h')
        self.assertEqual(tooltip, '매시 7분 (7 * * * *)')

        badge, tooltip = widget._fmt_cron_short('"0 */2 * * *"')
        self.assertEqual(badge, '2h')
        self.assertEqual(tooltip, '주기: 0 */2 * * *')

        badge, tooltip = widget._fmt_cron_short('"0 * * * *"')
        self.assertEqual(badge, '1h')
        self.assertEqual(tooltip, '매시간 정각 (0 * * * *)')

        badge, tooltip = widget._fmt_cron_short('0 0 * * *')
        self.assertEqual(badge, '1d')
        self.assertEqual(tooltip, '매일 (0 0 * * *)')

        badge, tooltip = widget._fmt_cron_short('')
        self.assertEqual(badge, '반복')
        self.assertEqual(tooltip, '정기 실행 대기')

    def test_stale_subagent_over_1800s_not_active(self):
        now = datetime.now(timezone.utc).timestamp()
        stale_sub = {
            "conversation_id": "stale-sub-123",
            "role": "Subagent",
            "is_subagent": True,
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "last_modified_time": datetime.fromtimestamp(now - 1900, timezone.utc).isoformat(),
        }
        with mock.patch("os.path.exists", return_value=False):
            is_active = widget._agy_worker_is_active(stale_sub, set())
            self.assertFalse(is_active)

        # Active running subagent at 200s (waiting on tool/build) is preserved
        active_sub = {
            "conversation_id": "active-sub-123",
            "role": "Subagent",
            "is_subagent": True,
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "last_modified_time": datetime.fromtimestamp(now - 200, timezone.utc).isoformat(),
        }
        with mock.patch("os.path.exists", return_value=False):
            self.assertTrue(widget._agy_worker_is_active(active_sub, set()))

    def test_cron_clean_desc_and_no_redundant_model_pill(self):
        now = datetime.now(timezone.utc)
        iso_now = now.isoformat()
        cid = 'testparent-cron-0001'
        parent = dict(
            conversation_id=cid,
            status='ACTIVE',
            last_modified_time=iso_now,
            title='Cockpit Project',
        )
        cron_task = {
            'id': f'{cid}/task-12',
            'short_id': 'task-12',
            'desc': 'Cron: 7 * * * *, Prompt: [Hourly Audit] 1시간 주기 콕핏 점검',
            'kind': 'cron',
            'cron_expr': '7 * * * *',
            'created_at': iso_now,
            'done': False,
        }
        with mock.patch.object(widget, '_extract_active_bg_tasks', return_value=[cron_task]):
            active, _, _, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'antigravity': [parent]}}, {}
            )
            # 1. Raw Cron/Prompt prefix must be cleanly stripped
            self.assertIn('[Hourly Audit] 1시간 주기 콕핏 점검', active)
            self.assertNotIn('Cron: 7 * * * *, Prompt:', active)
            # 2. Right-side Cron model-pill must be omitted to prevent triple-repetition
            pos_cron_row = active.find('node-bg-testpare-task-12')
            self.assertNotEqual(pos_cron_row, -1)
            row_chunk = active[pos_cron_row:pos_cron_row + 1200]
            self.assertNotIn('<span class="mp-full">Cron</span>', row_chunk)
            # 3. Repeat pill has 1h without ⏱ emoji
            self.assertIn('<span class="ctx-time-txt">1h</span>', row_chunk)

    def test_token_capacity_and_korean_formatting(self):
        self.assertEqual(widget.format_token_korean(193_000_000), "1억 9,300만")
        self.assertEqual(widget.format_token_korean(621_000_000), "6억 2,100만")
        self.assertEqual(widget.format_token_korean(2_120_000), "212만")
        self.assertEqual(widget.format_token_korean(31_240_000), "3,124만")

        with mock.patch.object(widget, "load_calibrated_pool_token_capacities", return_value=dict(widget.POOL_TOKEN_CAPACITIES)):
            accs = widget.build_accounts({
                "antigravity": {"pools": {
                    "Gemini Pool (Argon / Flash)": {"five_hour": {"remaining_percent": 90.0}},
                }},
                "codex_local": {
                    "five_hour": {"remaining_percent": 100.0},
                    "weekly": {"remaining_percent": 50.0},
                }
            })
            gem = next(a for a in accs if a["provider"] == "antigravity")
            gem_5h = gem["windows"][0]
            self.assertEqual(gem_5h.get("totalTokens"), 193_000_000)

            cdx = next(a for a in accs if a["provider"] == "codex")
            cdx_5h = next(w for w in cdx["windows"] if w["periodKind"] == "5h")
            self.assertNotIn("totalTokens", cdx_5h)
            cdx_7d = next(w for w in cdx["windows"] if w["periodKind"] == "7d")
            self.assertEqual(cdx_7d.get("totalTokens"), 31_240_000)

            html = widget.build_static_quota_grid_html(accs)
            self.assertIn("토큰 용량: 잔여 약", html)
            self.assertIn("1억 9,300만 토큰", html)
            self.assertIn("3,124만 토큰", html)

    def test_calibrated_pool_capacities_injected_into_template(self):
        template = Path(widget.__file__).with_name(widget.TEMPLATE_FILENAME).read_text()
        fake_caps = {
            "agy_gemini_5h": 200_000_000,
            "agy_gemini_7d": 700_000_000,
            "agy_claude_5h": 3_000_000,
            "agy_claude_7d": 5_000_000,
            "claude_5h": 40_000_000,
            "claude_7d": 100_000_000,
            "codex_7d": 35_000_000,
        }
        with mock.patch.object(widget, "load_calibrated_pool_token_capacities", return_value=fake_caps), \
             mock.patch.object(widget, "_load_persisted_widget_state", return_value={}):
            injected = widget.inject_live_data(template, {})
        m = re.search(r"const\s+POOL_TOKEN_CAPACITIES\s*=\s*(\{.*?\});", injected, re.S)
        self.assertIsNotNone(m)
        parsed = json.loads(m.group(1))
        self.assertEqual(parsed.get("agy_gemini_5h"), 200_000_000)
        self.assertEqual(parsed.get("codex_7d"), 35_000_000)

    def test_build_static_quota_grid_calibrated_fallback(self):
        fake_caps = {
            "test_pool_5h": 50_000_000,
        }
        accs = [{
            "name": "Test Acc",
            "provider": "codex",
            "windows": [{
                "label": "Test Window",
                "poolKey": "test_pool_5h",
                "remaining": 50.0,
            }]
        }]
        with mock.patch.object(widget, "load_calibrated_pool_token_capacities", return_value=fake_caps):
            html = widget.build_static_quota_grid_html(accs)
        self.assertIn("토큰 용량: 잔여 약 2,500만 / 총 5,000만 토큰", html)


if __name__ == '__main__':
    unittest.main()

