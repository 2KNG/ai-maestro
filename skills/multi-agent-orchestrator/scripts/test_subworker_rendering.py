"""Offline delegated-worker rendering regressions, isolated from the real HOME."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import render_usage_widget as widget


class SubworkerRenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, HOME=self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = time.time()
        for name, value in [('_extract_active_bg_tasks', []), ('_detail_extras_html', ''),
                            ('_codex_rollout_paths', {})]:
            patch = mock.patch.object(widget, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def registry(self, workers):
        path = Path(self.temp.name) / '.gemini/antigravity/agy_spawned_sessions.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workers))

    def parent(self, sid='parent', cwd='/home/mjserver/dev'):
        return dict(thread_id=sid, cwd=cwd, source='vscode', title=sid,
                    created_at=self.now - 100, updated_at=self.now)

    def render(self, trees, live=(set(), set(), set())):
        with mock.patch.object(widget, '_running_worker_sessions', return_value=live):
            return widget.build_active_and_closed_trees_html({'agent_trees': trees}, {})[0]

    def test_registry_claude_usage_badges_and_no_truncation(self):
        workers = {f'claude-{i}': dict(provider='claude', parent_id='parent', cwd='/home/mjserver/dev/ai-maestro',
                   title=f'Worker {i}', model='claude-opus-5-5', effort='ultra' if i == 0 else 'high',
                   tier='fast' if i == 1 else 'default', updated_at=self.now) for i in range(8)}
        self.registry(workers)
        html = self.render({'codex': [self.parent()], 'claude': [dict(session_id='claude-0',
                           usage=dict(input_tokens=123, output_tokens=45), total_cost_usd=1.25)]},
                           (set(), set(), set(workers)))
        self.assertEqual(html.count('data-node-id="node-sub-claude-'), 8)
        for text in ('#ico-claude', 'Ultra', 'Fast', '168 tokens', '$1.2500',
                     'data-sub-running="1"', 'id="det-sub-claude-claude-0"'):
            self.assertIn(text, html)

    def test_codex_placeholder_merges_final_session(self):
        self.registry({'early': dict(provider='codex', parent_id='parent', session_id='final', cwd='/home/mjserver/dev/ai-maestro',
                                    title='delegated', updated_at=self.now)})
        nodes = widget._merge_registry_workers([
            dict(thread_id='early', registry_id='early'),
            dict(thread_id='final', token_count={'total_tokens': 987})], 'codex')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['thread_id'], 'final')
        self.assertEqual(nodes[0]['token_count']['total_tokens'], 987)
        html = self.render({'codex': [self.parent(), *nodes]}, ({'final'}, set(), set()))
        self.assertEqual(html.count('data-node-id="node-sub-cdx-parent-final"'), 1)

    def test_parent_evidence_path_boundary_and_ambiguity(self):
        broad = self.parent()
        narrow = self.parent('narrow', '/home/mjserver/dev/ai-maestro')
        worker = dict(session_id='worker', cwd='/home/mjserver/dev/ai-maestro/sub',
                      spec='Implement the distinct task', updated_at=self.now)
        self.assertIsNone(widget._worker_parent(worker, [broad, narrow], {}, self.now))
        self.assertEqual(widget._worker_parent(worker, [broad, narrow],
                         {'parent': 'worker'}, self.now), 'parent')
        self.assertEqual(widget._worker_parent(worker, [broad, narrow],
                         {'parent': worker['spec']}, self.now), 'parent')
        twin = self.parent('twin', narrow['cwd'])
        self.assertIsNone(widget._worker_parent(worker, [narrow, twin], {}, self.now))
        twin['created_at'] = self.now + 10
        self.assertIsNone(widget._worker_parent(worker, [narrow, twin], {}, self.now))
        worker['cwd'] = '/home/mjserver/dev/ai-maestro-other'
        self.assertIsNone(widget._worker_parent(worker, [narrow], {}, self.now))
        worker['cwd'] = '/home/mjserver/dev/mjmaterial'
        self.assertIsNone(widget._worker_parent(worker, [broad], {}, self.now))

    def test_multiple_parents_do_not_duplicate_worker(self):
        worker = dict(thread_id='worker', source='exec', cwd='/home/mjserver/dev/repo',
                      updated_at=self.now, title='worker')
        html = self.render({'codex': [self.parent('one'), self.parent('two'), worker]},
                           ({'worker'}, set(), set()))
        self.assertNotIn('node-sub-cdx-one-worker', html)
        self.assertNotIn('node-sub-cdx-two-worker', html)
        self.assertNotIn('data-node-id="node-cdx-worker"', html)

    def test_live_claude_sessions_and_dead_pid(self):
        directory = Path(self.temp.name) / '.claude/sessions'
        directory.mkdir(parents=True)
        for sid, pid in [('live', 100), ('dead', 101)]:
            (directory / f'{sid}.json').write_text(json.dumps(dict(sessionId=sid, pid=pid)))
        with mock.patch.object(widget, '_pid_alive', side_effect=lambda p: p == 100), \
             mock.patch.object(widget.subprocess, 'check_output', return_value=''):
            self.assertEqual(widget._running_worker_sessions(self.now)[2], {'live'})

    def test_cli_and_maestro_alias_processes(self):
        self.registry({'alias': dict(provider='claude', cwd='/home/mjserver/dev/repo',
                                    title='distinct spec', updated_at=self.now - 1)})
        real_open = open
        commands = {
            '/proc/1/cmdline': b'claude\0-p\0--session-id\0direct\0prompt\0',
            '/proc/2/cmdline': b'maestro\0worker-start\0--pool\0opus-5.5-ultra\0--cwd\0/home/mjserver/dev/repo\0--spec\0distinct spec\0',
        }
        def fake_open(path, *args, **kwargs):
            if str(path) in commands:
                import io
                return io.BytesIO(commands[str(path)])
            return real_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=fake_open), \
             mock.patch.object(widget, '_pid_alive', return_value=False), \
             mock.patch.object(widget.subprocess, 'check_output', return_value='1 claude 5\n2 maestro 5'):
            self.assertEqual(widget._running_worker_sessions(self.now)[2], {'direct', 'alias'})


if __name__ == '__main__':
    unittest.main()
