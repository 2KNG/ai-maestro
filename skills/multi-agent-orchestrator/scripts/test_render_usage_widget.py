"""Rendering regressions; no live provider calls or session-state writes."""
import base64
from datetime import datetime, timezone, timedelta
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock

import render_usage_widget as widget

DEV = os.path.expanduser('~/dev')


class RenderingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(widget, '_worker_registry', return_value={})
        self.addCleanup(patcher.stop)
        patcher.start()
        processes = mock.patch("live_worker_processes.scan_live_worker_processes", return_value=[])
        self.addCleanup(processes.stop)
        processes.start()

    def test_unmatched_active_workers_never_become_roots(self):
        now = datetime.now(timezone.utc)
        parent = dict(conversation_id='parent-test', status='RUNNING',
                      last_modified_time=now.isoformat(), workspaces=['file://' + DEV])
        codex = dict(thread_id='standalone-codex', source='exec', agy_origin=True,
                     is_running=True, status='running', updated_at=now.timestamp(),
                     cwd=DEV)
        claude = dict(session_id='standalone-claude', is_running=True,
                      updated_at=now.timestamp(), cwd=DEV)
        with mock.patch.object(widget, '_worker_parent', return_value=None), \
             mock.patch.object(widget, '_running_worker_sessions', return_value=({'standalone-codex'}, set(), {'standalone-claude'})):
            active, closed, _, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'antigravity': [parent], 'codex': [codex], 'claude': [claude]}}, {})
        self.assertNotIn('node-cdx-standalone-codex', active + closed)
        self.assertNotIn('node-sub-claude-standalone-claude', active + closed)

    def test_orca_terminal_sessions_are_current_main_agents(self):
        orca = os.path.expanduser('~/orca/workspaces/tag_gcdual/로봇-테스트')
        self.assertEqual(widget._repo_from_uris_or_cwd(None, orca), ('tag_gcdual/로봇-테스트', 'current'))
        self.assertEqual(widget._repo_from_uris_or_cwd(None, orca + '/src'), ('tag_gcdual/로봇-테스트', 'current'))
        self.assertEqual(widget._repo_from_uris_or_cwd(None, '/tmp/elsewhere'), ('elsewhere', 'external'))
        now = datetime.now(timezone.utc).timestamp()
        main = dict(session_id='orca-main', entrypoint='cli', terminal='orca', activity='busy', is_running=True,
                    pid=4242, cwd=orca, title='자코비안 따라가기', model='claude-opus-5-5', effort='xhigh',
                    updated_at=now, subagents=[dict(session_id='orca-main:a1', agent_id='a1', title='Explore repo',
                                                    is_running=True, updated_at=now, cwd=orca)])
        # Launched from the Orca pane: process ancestry names the owning session.
        worker = dict(session_id='orca-worker', parent_session_id='orca-main', is_running=True,
                      updated_at=now, cwd=orca, title='delegated')
        printer = dict(session_id='print-only', entrypoint='sdk-cli', is_running=True, updated_at=now, cwd=orca)
        exited = dict(session_id='orca-exited', entrypoint='cli', is_running=False, updated_at=now - 600,
                      cwd=orca, title='finished pane')
        codex = dict(thread_id='orca-codex', source='cli', cwd=orca, title='Codex in Orca', updated_at=now - 30)
        live = (set(), set(), {'orca-main', 'orca-worker', 'print-only'})
        with mock.patch.object(widget, '_running_worker_sessions', return_value=live):
            active, closed, count, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'codex': [codex], 'claude': [main, worker, printer, exited]}}, {})
        self.assertIn('data-node-id="node-claude-orca-main" data-scope="current" data-running="1"', active)
        self.assertIn('data-node-id="node-cdx-orca-codex" data-scope="current"', active)
        self.assertIn('>tag_gcdual/로봇-테스트</span>', active)
        card = active.split('data-node-id="node-claude-orca-main"')[1].split('data-node-id="node-cdx-')[0]
        self.assertIn('status-dot busy', card)
        self.assertIn('Opus 5.5', card)
        self.assertIn('data-node-id="node-sub-claude-orca-worker"', card)
        self.assertIn('data-node-id="node-sub-claude-orca-main:a1"', card)
        self.assertNotIn('print-only', active)
        self.assertNotIn('node-claude-orca-exited', active)
        self.assertIn('data-node-id="closed-claude-orca-exi" data-scope="current"', closed)
        self.assertEqual(count, 2)

    def test_orca_teammate_panes_nest_as_sub_rows_under_their_lead(self):
        orca = os.path.expanduser('~/orca/workspaces/tag_gcdual/로봇-테스트')
        now = datetime.now(timezone.utc).timestamp()
        lead = dict(thread_id='cdx-lead', source='cli', cwd=orca, title='구현 tag_gcdual 플랜 B 테스트 콘솔',
                    updated_at=now - 5)
        cx = dict(thread_id='cdx-mate', source='cli', cwd=orca, title='Delegate Plan B DRFL code work',
                  updated_at=now - 900, turn_completed=True, orca_lead_id='cdx-lead')
        claude_lead = dict(session_id='cl-lead', entrypoint='cli', terminal='orca', activity='idle', is_running=True,
                           pid=11, cwd=orca, title='자코비안 따라가기', updated_at=now - 60)
        busy = dict(session_id='cl-busy', entrypoint='cli', terminal='orca', activity='busy', is_running=True, pid=12,
                    cwd=orca, title='You own code-only work for Plan B DRFL', updated_at=now, orca_lead_id='cdx-lead')
        idle = dict(session_id='cl-idle', entrypoint='cli', terminal='orca', activity='idle', is_running=True, pid=13,
                    cwd=orca, title='You own the field-tester coordinate display task', updated_at=now - 900,
                    orca_lead_id='cdx-lead')
        gone = dict(cx, thread_id='cdx-gone', title='closed pane')
        panes = [dict(pid=20, provider='codex', session_id='cdx-mate', parent_session_id='', parent_conversation_id='')]
        with mock.patch('live_worker_processes.scan_live_worker_processes', return_value=panes):
            active, closed, count, _ = widget.build_active_and_closed_trees_html(
                {'agent_trees': {'codex': [lead, cx, gone], 'claude': [claude_lead, busy, idle]}}, {})
        for main in ('node-cdx-cdx-mate', 'node-claude-cl-busy', 'node-claude-cl-idle', 'node-cdx-cdx-gone'):
            self.assertNotIn(f'data-node-id="{main}"', active)
        card = active.split('data-node-id="node-cdx-cdx-lead"')[1].split('data-node-id="node-claude-cl-lead"')[0]
        rows = {m: card.split(f'data-node-id="{m}"')[0].rsplit('<div class="', 1)[1] for m in (
            'node-sub-claude-cl-busy', 'node-sub-claude-cl-idle', 'node-sub-cdx-cdx-lead-cdx-mate')}
        # The busy teammate is a ripening SUB; idle open panes age like mains instead of evaporating.
        self.assertTrue(rows['node-sub-claude-cl-busy'].startswith('tree-node sub-node sub-ripening'))
        self.assertIn('data-sub-running="1"', card.split('node-sub-claude-cl-busy')[1].split('>', 1)[0])
        for row in ('node-sub-claude-cl-idle', 'node-sub-cdx-cdx-lead-cdx-mate'):
            self.assertEqual(rows[row].split('"')[0], 'tree-node sub-node')
        self.assertIn('Orca 팀원 패인', card)
        self.assertIn('status-dot busy', card.split('class="agent-row parent"')[1].split('</div>')[0])
        self.assertIn('data-node-id="node-claude-cl-lead"', active)
        self.assertNotIn('node-sub-cdx-cdx-lead-cdx-gone', active)
        self.assertNotIn('closed-cdx-cdx-mate', closed)
        self.assertEqual(count, 2)

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
                      workspaces=['file://' + DEV],
                      status='RUNNING', last_modified_time=iso, children=[
                          dict(conversation_id=f'{i:08x}-child', role=f'Child {i}', last_modified_time=iso)
                          for i in range(20)])
        worker = dict(parent_id=parent['conversation_id'], thread_id='12345678-worker', title='worker', cwd=DEV,
                      status='closed', source='exec', updated_at=now.timestamp() - 5)
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'antigravity': [parent], 'codex': [worker]}}, {})
        self.assertEqual(len(re.findall(r'data-node-id="node-sub-', active)), 6)
        self.assertIn('data-node-id="node-sub-cdx-12345678-worker"', active)
        self.assertIn('Child 4</span>', active)
        self.assertNotIn('Child 5</span>', active)

    def test_pinned_agy_origin_workers_cannot_become_roots(self):
        # Pinning does not turn an unattached AGY-origin worker into a root.
        now = datetime.now(timezone.utc).timestamp()
        nodes = [dict(thread_id=f'{i:08x}-thread', title='Agent', updated_at=now - i,
                      status='running', source='cli', agy_origin=origin)
                 for i, origin in enumerate((True, False), 1)]
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'codex': nodes}}, {'pinned': ['node-cdx-00000001-thread']})
        self.assertEqual(active.count('<span class="agent-name">✦ Agent</span>'), 0)
        self.assertEqual(active.count('<span class="agent-name">Agent</span>'), 1)
        self.assertNotIn('agy-origin-badge', active)
        self.assertNotIn(chr(0x1fa90), active)

    def test_top_quota_strip_groups_pools_with_integer_tiers(self):
        def acc(*pairs):
            return {'windows': [{'poolKey': k, 'remaining': r} for k, r in pairs]}
        accounts = [acc(('agy_gemini_5h', 88.5), ('agy_gemini_7d', 4.4), ('agy_claude_5h', 100.0), ('agy_claude_7d', 0.0)),
                    acc(('codex_5h', 12.0), ('codex_7d', 67.2)),
                    acc(('claude_5h', 35.5), ('claude_7d', 50.4), ('claude_fable_7d', 9.0))]
        strip = widget.build_top_quota_strip_html(accounts)
        boxes = [b.split('">', 1) for b in strip.split('<span class="q-box q-')[1:]]
        self.assertEqual([b[0] for b in boxes], ['agy', 'cdx', 'cld'])
        dial = r'<span class="g-(\w+)" title="([^"]+)"><span class="usage-donut ([\w-]+)">.*?<strong>(\d+)%</strong><small class="usage-center-period p-(\w+)">'
        self.assertEqual([(g, st, n, p) for g, _, st, n, p in re.findall(dial, strip)],
                         [('hi', 'in-use', '89', '5h'), ('low', 'in-use', '4', '7d'), ('full', 'unused', '100', '5h'),
                          ('low', 'exhausted', '0', '7d'), ('hi', 'in-use', '67', '7d'),
                          ('mid', 'in-use', '36', '5h'), ('hi', 'in-use', '50', '7d')])
        self.assertEqual([t.split(' · ')[0] for _, t, *_ in re.findall(dial, strip)],
                         ['Gemini 5h 89%', 'Gemini 7d 4%', 'AGY Claude 5h 100%', 'AGY Claude 7d 0%',
                          'Codex 7d 67%', 'Claude 5h 36%', 'Claude 7d 50%'])
        # Tier follows the raw value like the grid donut (50.4 stays green even though it reads 50%).
        # Only AGY splits its two sub-pools with inner borders; no marks or captions inside the strip.
        self.assertEqual(boxes[0][1].count('<span class="q-sub">'), 2)
        self.assertNotIn('q-sub', boxes[1][1] + boxes[2][1])
        self.assertEqual(re.sub(r'<[^>]+>', '', strip), '89%5h4%7d100%5h0%7d67%7d36%5h50%7d')
        self.assertNotIn('<use', strip)
        template = Path(widget.__file__).with_name(widget.TEMPLATE_FILENAME).read_text()
        with mock.patch.object(widget, '_load_persisted_widget_state', return_value={}):
            html = widget.inject_live_data(template, {})
        toolbar = html[html.index('class="usage-toolbar"'):html.index('id="quota-grid"')]
        self.assertIn('id="top-quota-strip"', toolbar)
        self.assertIn('class="q-box q-agy"', toolbar)

    def test_standalone_bundle_roundtrips_and_stays_below_58kb(self):
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
        # The template alone bundles to ~54.96 KB; the per-agent resource CSS/JS adds ~1.3 KB and the
        # dark-theme quota tint layer (pool rims, thin ring glow, elapsed wedge) ~0.3 KB (bundle ~56.9 KB).
        # The toolbar quota strip replaced the old mini-donut compact bar CSS (bundle ~57.3 KB).
        self.assertLess(len(bundled.encode()), 58000)

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
            json.dumps({"type": "SYSTEM_MESSAGE", "step_index": 3, "content": "All your subagents and background tasks have been stopped due to server restart", "created_at": ts_restart}),
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

    def test_completed_subagent_filtered_after_retention(self):
        now = datetime.now(timezone.utc)
        iso_fresh = (now - timedelta(seconds=5)).isoformat()
        iso_expired = (now - timedelta(seconds=widget.SUB_IDLE_RETENTION_SEC + 5)).isoformat()
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
        self.assertIn('data-idle-age="5"', active)
        self.assertIn('class="tree-node sub-node sub-ripening"', active)
        fresh_row = active.split('Fresh Subagent')[0].rsplit('sub-ripening', 1)[1]
        # A finished SUB shows a static completed dot, never a running/pulsing one.
        self.assertIn('status-dot sub-done', fresh_row)
        self.assertNotIn('status-dot busy', fresh_row)
        self.assertNotIn('status-dot idle-fresh', fresh_row)

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

    def test_claude_worker_row_preserves_and_prepends_diamond(self):
        node = {
            "session_id": "claude-test-sid",
            "title": "Clean codebase and add tests",
            "model": "claude-opus-5-5",
            "effort": "high",
            "updated_at": 1000,
        }
        html = widget._claude_worker_row(node, set(), 1005)
        self.assertIn("✦ Clean codebase and add tests", html)

        node2 = {
            "session_id": "claude-test-sid-2",
            "title": "✦ Already diamond",
            "model": "claude-opus-5-5",
            "effort": "high",
            "updated_at": 1000,
        }
        html2 = widget._claude_worker_row(node2, set(), 1005)
        self.assertIn("✦ Already diamond", html2)
        self.assertNotIn("✦ ✦", html2)


def usage_merge(procs, pids, totals):
    from check_all_usage import merge_resources
    return merge_resources(procs, [{'pids': pids}], totals)


class ResourceRenderingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(widget, '_worker_registry', return_value={})
        self.addCleanup(patcher.stop)
        patcher.start()
        processes = mock.patch("live_worker_processes.scan_live_worker_processes", return_value=[])
        self.addCleanup(processes.stop)
        processes.start()

    @staticmethod
    def res(pids, cpu, ram, gpu=0.0, tx=0.0, rx=0.0):
        # 16 GiB RAM and 8 GiB VRAM, matching TOTALS.
        return dict(cpu_pct=cpu, ram_mb=ram, gpu_mb=gpu, tx_kbps=tx, rx_kbps=rx, ram_pct=round(ram / 163.84, 2),
                    gpu_pct=round(gpu / 81.92, 2), proc_count=len(pids), pids=pids)

    # One logical CPU keeps per-core fixture values equal to the normalized cpu_pct.
    TOTALS = {'cpu_count': 1, 'mem_total_mb': 16384.0, 'gpu_total_mb': 8192.0}

    @staticmethod
    def tags(pill):
        return re.findall(r'<b class="rl">(.*?)</b>', pill)

    @staticmethod
    def slot(html, key):
        return re.search(rf'data-res-key="{key}"><!--__RES__-->(.*?)<!--__RES_END__-->', html).group(1)

    def render(self, process_resources):
        now = datetime.now(timezone.utc).isoformat()
        cid = 'resparent-0001-0000-0000-000000000001'
        parent = dict(conversation_id=cid, status='RUNNING', last_modified_time=now,
                      children=[dict(conversation_id='ressub-0001', role='Resource Subagent',
                                     last_modified_time=now, status='CASCADE_RUN_STATUS_RUNNING')])
        bg = [{'id': f'{cid}/task-42', 'short_id': 'task-42', 'desc': 'pytest loop', 'kind': 'job',
               'created_at': now, 'done': False}]
        data = {'agent_trees': {'antigravity': [parent]}}
        if process_resources is not None:
            data['process_resources'] = process_resources
        with mock.patch.object(widget, '_extract_active_bg_tasks', return_value=bg):
            return widget.build_active_and_closed_trees_html(data, {})[0]

    def test_rows_show_own_trees_and_main_rolls_up_by_pid_union(self):
        procs = {'1': [10.0, 100.0, 0.0, 1.0, 0.0, 'bash'], '2': [30.0, 300.0, 2048.0, 150.0, 50.0, 'pytest'],
                 '3': [0.1, 50.0, 0.0, 0.0, 0.0, 'node']}
        active = self.render({
            'procs': procs, **self.TOTALS,
            'conversations': {'resparent-0001-0000-0000-000000000001': self.res([1, 2], 40.0, 400.0, 2048.0, 151.0, 50.0),
                              'ressub-0001': self.res([3], 0.1, 50.0)},
            'tasks': {'resparent-0001-0000-0000-000000000001/task-42': self.res([2], 30.0, 300.0, 2048.0, 150.0, 50.0)},
            'workers': {}, 'pids': {},
        })
        main = self.slot(active, 'res-node-agy-resparen')
        # Subagent pid 3 joins the rollup; the job's pid 2 is not counted twice. Order: CPU, GPU, RAM, send, receive.
        self.assertEqual(self.tags(main), ['CPU', 'GPU', 'RAM', '↑', '↓'])
        self.assertIn('<b class="rl">CPU</b>40.1%', main)
        self.assertIn('<b class="rl">GPU</b>25.0%', main)  # 2048 of 8192 MiB
        self.assertIn('<b class="rl">RAM</b>2.8%', main)  # 450 of 16384 MiB = 2.75%
        self.assertIn('<b class="rl">↑</b>151KB/s', main)
        self.assertIn('<b class="rl">↓</b>50KB/s', main)
        self.assertIn('GPU 25.0% (VRAM 2048MB)', main)  # exact sizes stay in the tooltip
        self.assertIn('RAM 2.8% (RSS 450MB)', main)
        self.assertIn('res-pill rollup', main)
        self.assertNotIn('I/O', main)
        sub = self.slot(active, 'res-node-sub-ressub-0')
        self.assertEqual(self.tags(sub), ['CPU', 'RAM'])  # zero GPU / send / receive omitted
        self.assertIn('idle', sub)  # below every activity threshold
        job = self.slot(active, 'res-node-bg-resparen-task-42')
        self.assertIn('<span class="r-cpu r-hot"><b class="rl">CPU</b>30.0%</span>', job)  # 30% of the whole PC
        self.assertIn('r-gpu r-on', job)
        self.assertIn('r-tx r-on', job)
        # The board precedes the first tree node and carries the breakdown drawer.
        self.assertLess(active.index('class="res-board"'), active.index('class="tree-node'))
        self.assertIn('리소스 상세', active)
        self.assertIn('<span class="res-kind">JOB</span>pytest loop', active)
        self.assertIn('에이전트 1 · 프로세스 3', active)

    def test_child_rows_partition_pids_and_add_up_to_the_main_rollup(self):
        procs = {'1': [2.0, 10.0, 0.0, 0.0, 0.0, 'bash'], '2': [30.0, 300.0, 0.0, 40.0, 0.0, 'node'],
                 '3': [5.0, 50.0, 0.0, 0.0, 0.0, 'node'], '4': [7.0, 70.0, 0.0, 0.0, 0.0, 'pytest']}
        active = self.render({
            'procs': procs, **self.TOTALS,
            'conversations': {'resparent-0001-0000-0000-000000000001': self.res([1, 2, 3, 4], 44.0, 430.0, tx=40.0),
                              'ressub-0001': self.res([2, 3], 35.0, 350.0, tx=40.0)},
            # The job shell (4) also contains the subagent's pid 2: that PID stays with the SUB row.
            'tasks': {'resparent-0001-0000-0000-000000000001/task-42': self.res([2, 4], 37.0, 370.0, tx=40.0)},
            'workers': {}, 'pids': {},
        })
        sub = self.slot(active, 'res-node-sub-ressub-0')
        job = self.slot(active, 'res-node-bg-resparen-task-42')
        main = self.slot(active, 'res-node-agy-resparen')
        self.assertIn('<b class="rl">CPU</b>35.0%', sub)
        self.assertIn('<b class="rl">CPU</b>7.0%', job)  # only pid 4
        self.assertNotIn('<b class="rl">↑</b>', job)
        # SUB 35 + JOB 7 + the main's own shell 2 = 44.
        self.assertIn('<b class="rl">CPU</b>44.0%', main)

    def test_worker_start_wrapper_job_is_not_a_second_row(self):
        procs = {'1': [1.0, 10.0, 0.0, 0.0, 0.0, 'bash'], '2': [20.0, 200.0, 0.0, 9.0, 9.0, 'claude']}
        active = self.render({
            'procs': procs, **self.TOTALS,
            'conversations': {'resparent-0001-0000-0000-000000000001': self.res([1, 2], 21.0, 210.0, tx=9.0, rx=9.0),
                              'ressub-0001': self.res([2], 20.0, 200.0, tx=9.0, rx=9.0)},
            'tasks': {'resparent-0001-0000-0000-000000000001/task-42': {**self.res([1], 1.0, 10.0), 'worker_pids': [2]}},
            'workers': {}, 'pids': {},
        })
        self.assertNotIn('node-bg-resparen-task-42', active)
        self.assertNotIn('pytest loop', active)
        self.assertIn('<b class="rl">CPU</b>20.0%', self.slot(active, 'res-node-sub-ressub-0'))
        self.assertIn('<b class="rl">CPU</b>21.0%', self.slot(active, 'res-node-agy-resparen'))

    def test_background_parser_skips_worker_start_wrappers(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        logs, task_logs = os.path.join(tmp, 'logs'), os.path.join(tmp, 'tasks')
        os.makedirs(logs)
        os.makedirs(task_logs)

        def started(step, command):
            return json.dumps({'step_index': step, 'type': 'GENERIC', 'created_at': '2026-10-21T06:24:46Z',
                               'content': f'Tool is running as a background task with task id: c/task-{step}\n'
                                          f'Task Description: {command}\nTask logs are available at: file:///x'})
        # Planner args truncated the command, so only the tool result shows the worker-start line.
        spawn = "cat <<'EOF' > /tmp/spec.md\nfix it\nEOF\n~/.local/bin/maestro worker-start --pool claude --wait --spec-file /tmp/spec.md"
        lines = [started(10, spawn), started(20, 'python3 long_job.py'), started(30, 'SPEC_FILE=$(mktemp)')]
        with open(os.path.join(task_logs, 'task-30.log'), 'w') as f:
            f.write('{\n  "pool": "codex",\n  "ok": true\n}\n')
        tpath = os.path.join(logs, 'transcript.jsonl')
        with open(tpath, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        tasks = widget._parse_render_background_tasks(tpath)
        self.assertEqual(list(tasks), ['c/task-20'])
        self.assertEqual(tasks['c/task-20']['desc'], 'python3 long_job.py')
        import check_all_usage
        self.assertEqual(list(check_all_usage._parse_background_tasks(tpath)), ['c/task-20'])

    def test_idle_rows_get_no_pill_and_zero_metrics_are_omitted(self):
        active = self.render({
            'procs': {'1': [0.0, 5.0, 0.0, 0.0, 0.0, 'bash'], '3': [0.0, 50.0, 0.0, 0.0, 2.0, 'node']},
            **self.TOTALS,
            'conversations': {'resparent-0001-0000-0000-000000000001': self.res([1], 0.0, 5.0),
                              'ressub-0001': self.res([3], 0.0, 50.0, rx=2.0)},
            'tasks': {}, 'workers': {}, 'pids': {},
        })
        # RAM alone is not activity: the idle job/shell rows render no slot at all.
        self.assertNotIn('res-node-bg-resparen-task-42', active)
        sub = self.slot(active, 'res-node-sub-ressub-0')
        self.assertEqual(self.tags(sub), ['RAM', '↓'])
        self.assertIn('2.0KB/s', sub)
        self.assertNotIn('<b class="rl">CPU</b>', sub)
        board = self.slot(active, 'res-board')
        self.assertNotIn('<b class="rl">GPU</b>', board)

        quiet = self.render({'procs': {'1': [0.0, 5.0, 0.0, 0.0, 0.0, 'bash']}, **self.TOTALS,
                             'conversations': {'resparent-0001-0000-0000-000000000001': self.res([1], 0.0, 5.0)},
                             'tasks': {}, 'workers': {}, 'pids': {}})
        # A live main agent always shows its pill, even when it only holds RAM; unattributed rows show none.
        self.assertEqual(self.tags(self.slot(quiet, 'res-node-agy-resparen')), ['RAM'])
        self.assertNotIn('res-node-sub-ressub-0', quiet)
        self.assertNotIn('res-node-bg-resparen-task-42', quiet)
        # The summary bar still shows the attributed total (RAM only).
        self.assertEqual(self.tags(self.slot(quiet, 'res-board')), ['RAM'])

    def test_runtime_shares_give_main_its_own_usage_on_top_of_children(self):
        # -1 / -2: Antigravity runtime shares (language_server + window) of the main and its subagent.
        procs = {'-1': [3.0, 600.0, 0.0, 20.0, 40.0, 'agy-runtime'], '-2': [1.0, 200.0, 0.0, 0.0, 0.0, 'agy-runtime'],
                 '7': [0.0, 30.0, 0.0, 0.0, 0.0, 'bash']}
        active = self.render({
            'procs': procs, **self.TOTALS,
            'conversations': {'resparent-0001-0000-0000-000000000001': self.res([-1], 3.0, 600.0, tx=20.0, rx=40.0),
                              'ressub-0001': self.res([-2], 1.0, 200.0)},
            # An idle job shell (RAM only) still gets its pill: it is a running JOB row.
            'tasks': {'resparent-0001-0000-0000-000000000001/task-42': self.res([7], 0.0, 30.0)},
            'workers': {}, 'pids': {},
        })
        main = self.slot(active, 'res-node-agy-resparen')
        sub = self.slot(active, 'res-node-sub-ressub-0')
        self.assertEqual(self.tags(self.slot(active, 'res-node-bg-resparen-task-42')), ['RAM'])
        self.assertIn('<b class="rl">CPU</b>1.0%', sub)
        # Main total 4.0% = own runtime share 3.0% + SUB 1.0% (+ idle JOB 0%), so it exceeds the children.
        self.assertIn('<b class="rl">CPU</b>4.0%', main)
        self.assertIn('합산: CPU 4.0% · RAM 5.1% (메인 본체: CPU 3.0% · RAM 3.7% + '
                      '하위 에이전트/잡 합계: CPU 1.0% · RAM 1.4%)', main)
        self.assertIn('Antigravity 런타임 몫', main)
        self.assertIn('<span class="res-kind">SELF</span>', active)
        self.assertIn('에이전트 1 · 프로세스 1', active)  # runtime shares are not counted as processes

    def test_pill_uses_text_labels_and_arrows_with_a_ram_floor(self):
        pill = widget._res_pill_html(dict(cpu_pct=2.3, gpu_mb=336.0, gpu_pct=4.1, ram_mb=5.0, ram_pct=0.03,
                                          tx_kbps=70.0, rx_kbps=2560.0, proc_count=2))
        text = re.sub(r'<[^>]+>', ' ', pill.split('>', 1)[1])
        self.assertEqual(' '.join(text.split()), 'CPU 2.3% GPU 4.1% RAM 0.1% ↑ 70KB/s ↓ 2.5MB/s')
        self.assertNotIn('<svg', pill)
        self.assertNotIn('<0.1%', pill)
        # A known RAM percentage with no MB sample still shows RAM.
        self.assertEqual(self.tags(widget._res_pill_html(dict(cpu_pct=9.0, ram_pct=0.6, proc_count=1))), ['CPU', 'RAM'])

    def test_cpu_is_a_share_of_all_threads_with_the_core_sum_in_the_tooltip(self):
        procs = {'1': [164.6, 100.0, 0.0, 0.0, 0.0, 'node'], '2': [2.1, 10.0, 0.0, 0.0, 0.0, 'bash'],
                 '3': [3000.0, 10.0, 0.0, 0.0, 0.0, 'stress']}
        totals = {**self.TOTALS, 'cpu_count': 24}
        busy = usage_merge(procs, [1], totals)
        pill = widget._res_pill_html(busy)
        self.assertIn('<b class="rl">CPU</b>6.9%', pill)  # 164.6 / 24, never above 100
        self.assertIn('CPU 6.9% (코어 기준 164.6% / 24스레드)', pill)
        light = widget._res_pill_html(usage_merge(procs, [2], totals))
        self.assertEqual(self.tags(light), ['CPU', 'RAM'])
        self.assertIn('<b class="rl">CPU</b>0.1%', light)  # 2.1 / 24 = 0.09 still reads 0.1%
        self.assertIn('<b class="rl">CPU</b>100.0%', widget._res_pill_html(usage_merge(procs, [3], totals)))

    def test_without_process_data_no_resource_markup_is_rendered(self):
        active = self.render(None)
        self.assertNotIn('res-slot', active)
        self.assertNotIn('res-board', active)
        idle = self.render({'procs': {}, 'conversations': {}, 'tasks': {}, 'workers': {}, 'pids': {}})
        self.assertNotIn('res-slot', idle)
        self.assertIn('귀속된 프로세스 없음', idle)

    def test_assets_are_injected_once_and_widen_the_standalone_shell(self):
        page = '<html><head><style></style></head><body><script></script></body></html>'
        once = widget._inject_resource_assets(page)
        twice = widget._inject_resource_assets(once)
        self.assertEqual(once, twice)
        self.assertLess(once.index('RES_ASSETS_CSS_START'), once.index('</head>'))
        self.assertLess(once.index('RES_ASSETS_JS_START'), once.index('</body>'))
        self.assertIn('max-width:clamp(980px,92vw,1380px)', once)
        self.assertNotIn('<symbol id="ri-', once)  # text labels replaced the icon sprites
        self.assertIn('.rl{', once)
        # The script must not contain a literal HTML comment opener.
        script = once[once.index('<script>(function'):once.index('RES_ASSETS_JS_END')]
        self.assertNotIn('<!--__RES__', script)


if __name__ == '__main__':
    unittest.main()

