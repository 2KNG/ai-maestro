"""Speed tier propagation: rollout/registry/argv -> resolve_speed_tier -> ⚡ / ⚡⚡ on every row type."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import check_all_usage as cau
import live_worker_processes as lwp
import render_usage_widget as widget

BOLT = 'class="tier-bolt"'
BOLT2 = 'class="tier-bolt tier-ultrafast"'


class ResolveSpeedTierTests(unittest.TestCase):
    def test_explicit_sources_beat_names_and_fastest_explicit_wins(self):
        cases = [
            ((), {}, 'default'),
            (('default',), {'model': 'gpt-6.1-sol (high, priority)'}, 'priority'),
            (('default', 'ultrafast'), {}, 'ultrafast'),
            (('standard',), {'pool': 'claude-fast'}, 'default'),
            ((None,), {'model': 'claude-opus-5-fast'}, 'fast'),
            ((None,), {'model': 'claude-opus-5-5', 'pool': 'claude'}, 'default'),
            ((None,), {'model': 'Gemini 3.8 Flash (Fast)'}, 'fast'),
            ((None,), {'model': 'breakfast-model'}, 'default'),
            (('PRIORITY',), {}, 'priority'),
        ]
        for tiers, kwargs, expected in cases:
            with self.subTest(tiers=tiers, kwargs=kwargs):
                self.assertEqual(widget.resolve_speed_tier(*tiers, **kwargs), expected)


class PillBoltTests(unittest.TestCase):
    def pill(self, *args):
        return widget._provider_icon_and_pill(*args)[1]

    def test_codex_claude_and_agy_pills(self):
        self.assertIn(BOLT2, self.pill('gpt-6.1-sol', 'codex', 'ultrafast', 'high'))
        self.assertIn('>⚡⚡<', self.pill('gpt-6.1-sol', 'codex', 'ultrafast', 'high'))
        priority = self.pill('gpt-6.1-sol', 'codex', 'priority', 'high')
        self.assertIn(BOLT, priority)
        self.assertIn('>⚡<', priority)
        self.assertNotIn('tier-bolt', self.pill('gpt-6.1-sol', 'codex', 'default', 'high'))
        # "(effort, tier)" labels from maestro results keep their tier and lose the suffix in the name.
        labelled = self.pill('gpt-6.1-sol (high, priority)', 'codex', 'default', 'high')
        self.assertIn(BOLT, labelled)
        self.assertNotIn('(high, priority)', labelled)
        self.assertIn(BOLT, self.pill('claude-opus-5-fast', 'claude', 'default', 'high'))
        self.assertIn(BOLT2, self.pill('claude-opus-5-5', 'claude', 'ultrafast', 'high'))
        self.assertNotIn('tier-bolt', self.pill('claude-opus-5-5', 'claude', 'default', 'high'))
        self.assertIn(BOLT, self.pill('Gemini 3.8 Flash (Fast)', 'antigravity'))
        self.assertNotIn('tier-bolt', self.pill('Gemini 4 Argon (High)', 'antigravity'))


class RowBoltTests(unittest.TestCase):
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
        # Real /proc may hold this very Claude session; keep live-process attribution out.
        patch = mock.patch.object(lwp, 'scan_live_worker_processes', return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def registry(self, workers):
        path = Path(self.temp.name) / '.gemini/antigravity/agy_spawned_sessions.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workers))

    def render(self, trees, live=(set(), set(), set())):
        with mock.patch.object(widget, '_running_worker_sessions', return_value=live):
            return widget.build_active_and_closed_trees_html({'agent_trees': trees}, {})[0]

    def test_claude_worker_rows(self):
        base = dict(provider='claude', parent_id='parent', cwd='/home/mjserver/dev/ai-maestro',
                    model='claude-opus-5-5', effort='high', updated_at=self.now)
        workers = {
            'std': dict(base, title='Standard', tier='default'),
            'fast': dict(base, title='Fast', tier='fast', model='claude-opus-5-fast'),
            'ultra': dict(base, title='Ultrafast', tier='ultrafast', model='claude-opus-5-fast'),
            'fallback': dict(base, title='Fallback', tier='fast', pool='claude-fast',
                             fallback_from='claude-opus-5-fast'),
        }
        self.registry(workers)
        parent = dict(thread_id='parent', cwd='/home/mjserver/dev', source='vscode', title='parent',
                      created_at=self.now - 100, updated_at=self.now)
        html = self.render({'codex': [parent]}, (set(), set(), set(workers)))
        rows = {sid: html.split(f'data-node-id="node-sub-claude-{sid}"')[1].split('data-node-id=')[0]
                for sid in workers}
        self.assertNotIn('tier-bolt', rows['std'])
        self.assertIn(BOLT, rows['fast'])
        self.assertIn(BOLT2, rows['ultra'])
        self.assertNotIn('tier-bolt', rows['fallback'])

    def test_codex_registry_tier_beats_rollout_fallback_default(self):
        self.registry({'w': dict(provider='codex', parent_id='parent', session_id='w', tier='ultrafast',
                                 cwd='/home/mjserver/dev/ai-maestro', title='urgent', updated_at=self.now)})
        nodes = widget._merge_registry_workers(
            [dict(thread_id='w', turn_context={'model': 'gpt-6.1-sol', 'service_tier': 'default'})], 'codex')
        self.assertEqual(nodes[0]['turn_context']['service_tier'], 'ultrafast')
        parent = dict(thread_id='parent', cwd='/home/mjserver/dev', source='vscode', title='parent',
                      created_at=self.now - 100, updated_at=self.now)
        html = self.render({'codex': [parent, *nodes]}, ({'w'}, set(), set()))
        row = html.split('data-node-id="node-sub-cdx-parent-w"')[1]
        self.assertIn(BOLT2, row.split('data-node-id=')[0])

    def test_live_process_tier_reaches_node(self):
        nodes = widget._merge_live_process_workers(
            [], 'codex', [dict(provider='codex', pid=7, session_id='live', parent_conversation_id='p',
                               spec='task', cwd='/tmp', tier='priority')], self.now)
        self.assertEqual(nodes[0]['tier'], 'priority')
        self.assertEqual(nodes[0]['turn_context']['service_tier'], 'priority')


class RolloutAndArgvTierTests(unittest.TestCase):
    def write_rollout(self, lines, pad=0):
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, 'w') as f:
            for line in lines[:1]:
                f.write(json.dumps(line) + '\n')
            for _ in range(pad):
                f.write(json.dumps({'type': 'response_item', 'payload': {'text': 'x' * 1000}}) + '\n')
            for line in lines[1:]:
                f.write(json.dumps(line) + '\n')
        return path

    def test_thread_settings_tier_survives_turn_context_without_tier(self):
        settings = {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_settings':
                    {'model': 'gpt-6.1-sol', 'service_tier': 'ultrafast'}}}
        turn = {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'effort': 'high'}}
        with mock.patch.object(cau, '_default_codex_service_tier', return_value='default'):
            for pad in (0, 400):  # 400KB pushes thread_settings out of the 250KB tail window
                with self.subTest(pad=pad):
                    ctx = cau.parse_codex_rollout(self.write_rollout([settings, turn], pad))['turn_context']
                    self.assertEqual((ctx['service_tier'], ctx['reasoning_effort']), ('ultrafast', 'high'))
            ctx = cau.parse_codex_rollout(self.write_rollout([turn]))['turn_context']
            self.assertEqual(ctx['service_tier'], 'default')

    def test_argv_tier(self):
        self.assertEqual(lwp._speed_tier('codex', ['exec', '-c', 'service_tier="default"',
                                                   '-c', 'service_tier="ultrafast"', 'task'], {}), 'ultrafast')
        self.assertEqual(lwp._speed_tier('codex', ['exec', '--config=service_tier=priority', 'x'], {}), 'priority')
        self.assertEqual(lwp._speed_tier('codex', ['exec', 'service_tier="ultrafast" in prompt'], {}), '')
        self.assertEqual(lwp._speed_tier('claude', ['-p', '--model', 'claude-opus-5-fast', 'x'], {}), 'fast')
        self.assertEqual(lwp._speed_tier('claude', ['-p', 'x'], {'CLAUDE_CODE_FAST_MODE': '1'}), 'fast')
        self.assertEqual(lwp._speed_tier('claude', ['-p', '--model', 'claude-opus-5-5', 'x'], {}), 'default')


if __name__ == '__main__':
    unittest.main()
