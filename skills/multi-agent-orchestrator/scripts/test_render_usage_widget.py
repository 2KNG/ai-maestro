"""Rendering regressions; no live provider calls or session-state writes."""
import base64
import gzip
from pathlib import Path
import re
import unittest
from unittest import mock

import render_usage_widget as widget


class RenderingTests(unittest.TestCase):
    def test_closed_sessions_use_exact_recency_and_stop_at_eight(self):
        # All sessions have the same age_days, in oldest-first input order.
        nodes = [dict(thread_id=f'{i:08x}-thread', title=f'Session {i}',
                      updated_at=1000+i, age_days=0, cwd='/tmp', status='closed')
                 for i in range(12)]
        _, closed, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'codex': nodes}}, {})
        self.assertEqual(re.findall(r'data-node-id="closed-cdx-([^"]+)"', closed),
                         [f'{i:08x}' for i in range(11, 3, -1)])

    def test_parent_child_limit_includes_injected_workers(self):
        parent = dict(conversation_id='521b781e-43da-4647-b411-585e65e5f002',
                      status='RUNNING', children=[
                          dict(conversation_id=f'{i:08x}-child', role=f'Child {i}')
                          for i in range(20)])
        worker = dict(thread_id='12345678-worker', cwd='/tmp', status='closed')
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'antigravity': [parent], 'codex': [worker]}}, {})
        self.assertEqual(len(re.findall(r'data-node-id="node-sub-', active)), 5)
        self.assertNotIn('Child 3</span>', active)

    def test_origin_mark_is_icon_only_and_first_in_row(self):
        nodes = [dict(thread_id=f'{i:08x}-thread', title='Agent', updated_at=i,
                      status='running', agy_origin=origin)
                 for i, origin in enumerate((True, False), 1)]
        active, _, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'codex': nodes}}, {})
        self.assertEqual(active.count('class="agy-arch-mark"'), 1)
        self.assertIn('<div class="agent-left">'+widget._agy_badge_html(), active)
        self.assertNotIn('agy-origin-badge', active)
        self.assertNotIn(chr(0x1fa90), active)

    def test_standalone_bundle_roundtrips_and_stays_below_45kb(self):
        template = Path(widget.__file__).with_name(widget.TEMPLATE_FILENAME).read_text()
        nodes = [dict(thread_id=f'{i:08x}-thread', title=f'Thread {i}',
                      updated_at=1000+i, status='closed', cwd='/tmp') for i in range(20)]
        with mock.patch.object(widget, '_load_persisted_widget_state', return_value={}):
            original = widget.inject_live_data(template, {'agent_trees': {'codex': nodes}})
        bundled = widget.compact_widget_html(original)
        payload = re.search(r'atob\("([^"]+)"\)', bundled).group(1)
        self.assertEqual(gzip.decompress(base64.b64decode(payload)).decode(), original)
        self.assertLess(len(bundled.encode()), 45000)


if __name__ == '__main__':
    unittest.main()
