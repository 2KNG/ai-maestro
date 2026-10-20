"""Parent ownership and child styling regressions with an isolated brain."""
from contextlib import ExitStack
from datetime import datetime, timezone
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import check_all_usage as usage
import render_usage_widget as widget


class TreeParser(HTMLParser):
    """Record actual tree ancestry, rather than relying on HTML string order."""
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.nodes = {}
        self.bad_rows = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'div':
            return
        classes = set(attrs.get('class', '').split())
        ancestors = [a for a in self.stack if 'tree-node' in a.get('class', '').split()]
        if 'tree-node' in classes:
            self.nodes[attrs.get('data-node-id')] = (classes, [a.get('data-node-id') for a in ancestors])
        if 'agent-row' in classes and 'child' in classes:
            if not ancestors or 'sub-node' not in ancestors[-1].get('class', '').split() or len(ancestors) < 2:
                self.bad_rows.append(attrs)
        self.stack.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'div' and self.stack:
            self.stack.pop()


class WorkerAttributionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.dict(os.environ, HOME=str(self.home)))
        for name, value in (('_worker_registry', {}), ('_running_worker_sessions', (set(), set(), set())),
                            ('_codex_rollout_paths', {}), ('_detail_extras_html', ''),
                            ('_extract_active_bg_tasks', [])):
            self.stack.enter_context(mock.patch.object(widget, name, return_value=value))
        self.now = time.time()
        self.iso = datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    def node(self, cid, **extra):
        return dict(conversation_id=cid, title='Ordinary title ' + cid,
                    last_modified_time=self.iso, status='RUNNING',
                    workspaces=['file:///workspace/shared'], **extra)

    def log(self, cid, text, name='logs/transcript.jsonl', age=0):
        path = self.home / '.gemini/antigravity/brain' / cid / '.system_generated' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (self.now - age, self.now - age))

    def render(self, ag, codex=(), claude=(), state=None):
        active, closed, _, _ = widget.build_active_and_closed_trees_html(
            {'agent_trees': {'antigravity': ag, 'codex': list(codex), 'claude': list(claude)}}, state or {})
        tree = TreeParser(active)
        self.assertEqual(tree.bad_rows, [])
        return tree, active, closed

    def test_descendant_logs_attach_cross_repo_cli_workers_to_exact_root(self):
        for name in ('logs/transcript.jsonl', 'tasks/task-42.log'):
            with self.subTest(log=name):
                self.log('subtwo00', json.dumps({'type': 'SYSTEM_MESSAGE', 'content': 'Output:\nsession_id=79dfac9b thread_ids=codex-worker'}) if name.endswith('jsonl') else 'session_id=79dfac9b thread_ids=codex-worker', name)
                root = self.node('e6219556', children=[self.node('subone00', children=[self.node('subtwo00')])])
                other = self.node('other000')
                cli = dict(thread_id='codex-worker', source='exec', agy_origin=True,
                           cwd='/different/repo', updated_at=self.now)
                claude = dict(session_id='79dfac9b', cwd='/different/repo', updated_at=self.now)
                tree, active, closed = self.render([other, root], [cli], [claude])
                for child in ('node-sub-claude-79dfac9b', 'node-sub-cdx-codex-worker',
                              'node-sub-subone00', 'node-sub-subtwo00'):
                    self.assertEqual(tree.nodes[child][1], ['node-agy-e6219556'])
                self.assertNotIn('node-cdx-codex-worker', active + closed)
                self.assertNotIn('node-agy-subone00', active + closed)
                self.assertNotIn('node-agy-subtwo00', active + closed)
                # Ensure the second iteration proves task-only attribution.
                self.log('subtwo00', '')

    def test_flat_parent_chains_override_titles_pins_and_workspace_fallback(self):
        nodes = [self.node('e6219556'), self.node('subone00', parent_id='e6219556'),
                 self.node('subtwo00', parent_id='subone00'), self.node('other000')]
        worker = dict(session_id='explicit-worker', parent_id='subtwo00', updated_at=self.now)
        tree, active, closed = self.render(nodes, claude=[worker], state={
            'pinned': ['node-agy-subone00'], 'imported': ['subtwo00']})
        self.assertEqual(tree.nodes['node-sub-claude-explicit-worker'][1], ['node-agy-e6219556'])
        self.assertNotIn('node-agy-subone00', active + closed)
        self.assertNotIn('node-agy-subtwo00', active + closed)

    def test_launch_results_beat_incidental_id_mentions_in_another_root(self):
        worker = dict(session_id='79dfac9b', cwd='/workspace/shared', updated_at=self.now)
        sub = self.node('subtwo00', parent_id='e6219556')
        root = self.node('e6219556')
        root['workspaces'] = ['file:///different/repo']
        other = self.node('other000')
        result = json.dumps({'pool': 'claude', 'session_id': '79dfac9b'})
        for name, text in (
            ('tasks/task-1.log', result),
            ('logs/transcript.jsonl', json.dumps({'type': 'SYSTEM_MESSAGE', 'content': 'Output:\n' + result})),
        ):
            with self.subTest(log=name):
                self.log('subtwo00', text, name)
                self.log('other000', 'Please investigate worker 79dfac9b', 'tasks/task-2.log')
                tree, _, _ = self.render([other, root, sub], claude=[worker])
                self.assertEqual(tree.nodes['node-sub-claude-79dfac9b'][1], ['node-agy-e6219556'])
                self.log('subtwo00', '', name)

    def test_spec_match_including_old_task_logs(self):
        roots = [self.node('e6219556'), self.node('other000')]
        sub = self.node('subtwo00', parent_id='e6219556')
        worker = dict(session_id='spec-worker', spec='Implement the exact independent feature',
                      cwd='/different/repo', updated_at=self.now)
        self.log('subtwo00', worker['spec'], 'tasks/task-1.log', age=6 * 3600 + 1)
        tree, _, _ = self.render([*roots, sub], claude=[worker])
        self.assertEqual(tree.nodes['node-sub-claude-spec-worker'][1], ['node-agy-e6219556'])
        self.log('subtwo00', worker['spec'], 'tasks/task-1.log')
        tree, _, _ = self.render([*roots, sub], claude=[worker])
        self.assertEqual(tree.nodes['node-sub-claude-spec-worker'][1], ['node-agy-e6219556'])

    def test_explicit_parent_overrides_conflicting_nested_membership(self):
        child = self.node('subtwo00', parent_id='e6219556')
        tree, _, _ = self.render([self.node('other000', children=[child]), self.node('e6219556')])
        self.assertEqual(tree.nodes['node-sub-subtwo00'][1], ['node-agy-e6219556'])

    def test_running_flat_descendant_keeps_old_parent_visible(self):
        root = self.node('e6219556')
        root.update(status='IDLE', last_modified_time='2020-01-01T00:00:00Z')
        tree, _, _ = self.render([root, self.node('subtwo00', parent_id='e6219556')])
        self.assertEqual(tree.nodes['node-sub-subtwo00'][1], ['node-agy-e6219556'])

    def test_workspace_and_activity_never_establish_parenthood(self):
        one, two = self.node('e6219556'), self.node('other000')
        worker = dict(session_id='repo-worker', cwd='/workspace/shared', updated_at=self.now)
        one['status'] = 'IDLE'
        tree, _, _ = self.render([one, two], claude=[worker])
        self.assertNotIn('node-sub-claude-repo-worker', tree.nodes)
        one['status'] = 'RUNNING'
        self.log('e6219556', 'maestro worker-start', age=10)
        self.log('other000', 'maestro worker-start', age=30)
        tree, _, _ = self.render([two, one], claude=[worker])
        self.assertNotIn('node-sub-claude-repo-worker', tree.nodes)

    def test_orphan_and_pinned_exec_workers_never_become_roots(self):
        cli = dict(thread_id='orphan-worker', source='exec', agy_origin=True, updated_at=self.now)
        tree, active, closed = self.render([self.node('orphan00', parent_id='missing')], [cli],
            [dict(session_id='orphan-claude', updated_at=self.now)],
            {'pinned': ['node-cdx-orphan-worker', 'node-agy-orphan00']})
        self.assertEqual(tree.nodes, {})
        self.assertNotIn('orphan', active + closed)

    def test_background_and_native_codex_children_have_child_classes(self):
        tasks = [dict(id=f'root/task-{i}', short_id=f'task-{i}', kind=kind,
                      desc='Background task', created_at=self.iso) for i, kind in
                 enumerate(('cron', 'job', 'timer', 'daemon'))]
        with mock.patch.object(widget, '_extract_active_bg_tasks', return_value=tasks):
            tree, _, _ = self.render([self.node('e6219556')], codex=[dict(
                thread_id='desktop', source='vscode', updated_at=self.now,
                children=[dict(thread_id='native', is_running=True, updated_at=self.now)])])
        for i in range(4):
            self.assertIn('sub-node', tree.nodes[f'node-bg-e6219556-task-{i}'][0])
        self.assertIn('sub-node', tree.nodes['node-sub-cdx-desktop-native'][0])

    def test_ordered_kill_restart_and_revival(self):
        from agy_transcript_state import transcript_state, RESTART
        spawn = [{'tool_calls': [{'name': 'invoke_subagent', 'args': {}}]},
                 {'type': 'GENERIC', 'content': 'Created the following subagents: {"conversationId":"child000"}'}]
        kill = {'tool_calls': [{'name': 'manage_subagents', 'args': {'Action': '\"kill\"', 'ConversationIds': '["child000"]'}}]}
        kill_all = {'tool_calls': [{'name': 'manage_subagents', 'args': {'Action': 'kill_all'}}]}
        restart = {'type': 'SYSTEM_MESSAGE', 'content': RESTART}
        revive = {'tool_calls': [{'name': 'send_message', 'args': {'Recipient': 'child000', 'Message': 'Continue'}}]}
        for stop in (kill, kill_all, restart):
            for revived in (False, True):
                with self.subTest(stop=stop, revived=revived):
                    self.log('e6219556', '\n'.join(map(json.dumps, spawn + [stop] + ([revive] if revived else []))))
                    parents, killed = transcript_state(['e6219556'])
                    self.assertEqual(parents['child000'], 'e6219556')
                    self.assertEqual(killed['child000'], stop is not restart and not revived)
                    tree, active, _ = self.render([self.node('e6219556'), self.node('child000')])
                    self.assertIn('node-sub-child000', tree.nodes)
        self.log('e6219556', '\n'.join(map(json.dumps, [restart] + spawn)))
        self.assertFalse(transcript_state(['e6219556'])[1]['child000'])

    def test_kill_targets_and_message_text_do_not_revive_others(self):
        from agy_transcript_state import transcript_state
        steps = [{'tool_calls': [{'name': 'invoke_subagent'}]},
                 {'content': 'Created the following subagents: {"conversationId":"child000"} {"conversationId":"child111"}'},
                 {'tool_calls': [{'name': 'manage_subagents', 'args': {'Action': 'kill', 'Ids': ['child000']}}]},
                 {'tool_calls': [{'name': 'send_message', 'args': {'Recipient': 'child111', 'Message': 'child000'}}]}]
        self.log('e6219556', '\n'.join(map(json.dumps, steps)))
        self.assertEqual(transcript_state(['e6219556'])[1], {'child000': True, 'child111': False})

    def test_quoted_ids_and_restart_in_investigation_are_not_evidence(self):
        from agy_transcript_state import transcript_state, RESTART
        steps = [{'tool_calls': [{'name': 'invoke_subagent'}]},
                 {'type': 'GENERIC', 'content': 'Created the following subagents: {"conversationId":"child000"}'},
                 {'type': 'USER_MESSAGE', 'content': 'Investigate cli-worker ' + RESTART},
                 {'type': 'GENERIC', 'content': 'Output:\nSource contains: ' + RESTART}]
        self.log('e6219556', '\n'.join(map(json.dumps, steps)))
        self.assertFalse(transcript_state(['e6219556'])[1]['child000'])
        tree, _, _ = self.render([self.node('e6219556')], claude=[dict(session_id='cli-worker', updated_at=self.now)])
        self.assertNotIn('node-sub-claude-cli-worker', tree.nodes)

    def test_collector_recovers_invoke_subagent_edges_without_metadata(self):
        self.log('e6219556', '\n'.join(json.dumps(s) for s in [
            {'tool_calls': [{'name': 'invoke_subagent'}]},
            {'content': 'Created the following subagents:\n{"conversationId":"subone00"}'},
            {'content': 'Unrelated {"conversationId":"other000"}'}]))
        self.log('subone00', '\n'.join(json.dumps(s) for s in [
            {'tool_calls': [{'name': 'invoke_subagent'}]},
            {'content': 'Created the following subagents:\n{"conversationId":"subtwo00"}'},
            {'tool_calls': [{'name': 'manage_subagents', 'args': {'Action': 'kill_all'}}]}]))
        summaries = {cid: {'summary': 'No prefix', 'lastModifiedTime': self.iso}
                     for cid in ('e6219556', 'subone00', 'subtwo00', 'other000')}
        def response(request, **kwargs):
            data = {'trajectorySummaries': summaries} if request.full_url.endswith('GetAllCascadeTrajectories') else {}
            return io.BytesIO(json.dumps(data).encode())
        with mock.patch.object(usage, 'discover_ls_credentials', return_value=('localhost:1', 'test')), \
             mock.patch.object(usage, 'get_last_selected_agy_model', return_value=None), \
             mock.patch.object(usage, '_load_agy_spawn_registry', return_value={}), \
             mock.patch.object(usage, 'AGY_METADATA_CACHE_PATH', str(self.home / 'meta.json')), \
             mock.patch.object(usage.urllib.request, 'urlopen', side_effect=response):
            roots = usage._fetch_antigravity_agent_tree()
        self.assertEqual({n['conversation_id'] for n in roots}, {'e6219556', 'other000'})
        root = next(n for n in roots if n['conversation_id'] == 'e6219556')
        child = root['children'][0]
        self.assertTrue(child['is_subagent'])
        self.assertEqual(child['parent_id'], 'e6219556')
        self.assertEqual(child['children'][0]['parent_id'], 'subone00')
        self.assertTrue(child['children'][0]['killed'])
        self.assertFalse(child['children'][0]['is_running'])
        self.assertEqual(child['children'][0]['status'], 'TERMINATED')


if __name__ == '__main__':
    unittest.main()
