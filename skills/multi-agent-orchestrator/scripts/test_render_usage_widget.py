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
        cid = 'testparent-0001-0000-0000-000000000001'
        parent = dict(
            conversation_id=cid,
            status='IDLE',
            last_modified_time=iso_old,
            children=[
                dict(conversation_id='sub-done-001', role='Completed Subagent', last_modified_time=iso_old, status='CASCADE_RUN_STATUS_DONE'),
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

            # 3. Cron indicator: calm idle-fresh status dot, ctx-time-txt with ⏰ 15m주기
            self.assertIn('status-dot idle-fresh', active[pos_cron - 350:pos_cron])
            self.assertIn('⏰ 15m주기', active)
            self.assertIn('<span class="ctx-time-txt">⏰ 15m주기</span>', active)

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
        lines = [
            json.dumps({"step_index": 1, "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/15 * * * *"}}], "created_at": "2026-10-16T10:00:00Z"}),
            json.dumps({"step_index": 2, "content": "background task with task id: test/task-100\nTask Description: Cron: */15 * * * *", "created_at": "2026-10-16T10:00:00Z"}),
            json.dumps({"step_index": 3, "content": "All your subagents and background tasks have been stopped due to server restart", "created_at": "2026-10-19T02:00:00Z"}),
            json.dumps({"step_index": 4, "tool_calls": [{"name": "run_command", "args": {"IsDaemon": True}}], "created_at": "2026-10-19T03:00:00Z"}),
            json.dumps({"step_index": 5, "content": "background task with task id: test/task-200\nTask Description: python server.py --port 8080", "created_at": "2026-10-19T03:00:00Z"}),
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


if __name__ == '__main__':
    unittest.main()

