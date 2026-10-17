"""Rendering regressions; no live provider calls or session-state writes."""
import base64
from datetime import datetime, timezone
import gzip
from pathlib import Path
import re
import unittest
from unittest import mock

import render_usage_widget as widget


class RenderingTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
