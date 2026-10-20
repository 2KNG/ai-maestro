"""Empty-log attribution and background wait retention through the real renderer."""
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import maestro
import render_usage_widget as widget
import spawn_evidence
from agy_transcript_state import transcript_summary
import test_worker_attribution as fixtures


class LiveAttributionTests(unittest.TestCase):
    setUp = fixtures.WorkerAttributionTests.setUp
    node = fixtures.WorkerAttributionTests.node
    log = fixtures.WorkerAttributionTests.log
    render = fixtures.WorkerAttributionTests.render

    def setUp(self):
        fixtures.WorkerAttributionTests.setUp(self)
        self.stack.enter_context(patch('live_worker_processes.scan_live_worker_processes', return_value=[]))

    def test_empty_task_log_with_synthesized_title_heredoc_and_spec_file(self):
        body = 'Implement exact parent attribution while waiting for independent backend work.\nPreserve running children.'
        worker = dict(session_id='live-worker', spec='✦ A synthesized unrelated title\n\n' + body,
                      updated_at=self.now, is_running=True)
        spec_file = self.home / 'worker spec.txt'
        spec_file.write_text(body)
        commands = ["cat <<'EOF' > /tmp/worker.txt\n" + body + "\nEOF\nmaestro worker-start --spec-file /tmp/worker.txt",
                    f'maestro worker-start --spec-file "{spec_file}" --wait']
        for command in commands:
            with self.subTest(command=command[:60]):
                self.log('child000', json.dumps({'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': command}}]}))
                self.log('child000', '', 'tasks/task-44.log')
                tree, _, _ = self.render([self.node('root0000'), self.node('child000', parent_id='root0000'), self.node('other000')], claude=[worker])
                self.assertEqual(tree.nodes['node-sub-claude-live-worker'][1], ['node-agy-root0000'])

    def test_spec_file_preview_invalidates_without_parent_transcript_change(self):
        spec_file = self.home / 'spec.txt'
        spec_file.write_text('first unique body ' * 30)
        self.log('root0000', json.dumps({'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': f'maestro worker-start --spec-file {spec_file}'}}]}))
        path = self.home / '.gemini/antigravity/brain/root0000/.system_generated/logs/transcript.jsonl'
        self.assertIn('first unique body', transcript_summary(path)[2])
        spec_file.write_text('second unique body ' * 30)
        result = transcript_summary(path)[2]
        self.assertIn('second unique body', result)
        self.assertNotIn('first unique body', result)

    def test_unique_body_breaks_a_shared_synthesized_title_tie(self):
        worker = {'spec': '✦ Shared generated title\n\nImplement this distinctive independent backend feature for the current parent'}
        parents = [{'conversation_id': 'one'}, {'conversation_id': 'two'}]
        evidence = {'one': 'Shared generated title', 'two': 'Shared generated title\n' + worker['spec'].split('\n\n')[1]}
        self.assertEqual(widget._worker_parent(worker, parents, evidence, self.now), 'two')

    def test_direct_codex_and_claude_commands_are_spawn_evidence(self):
        for command in ('codex exec "Implement distinctive direct CLI worker feature"',
                        'claude -p "Implement distinctive direct CLI worker feature"'):
            self.log('root0000', json.dumps({'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': command}}]}))
            path = self.home / '.gemini/antigravity/brain/root0000/.system_generated/logs/transcript.jsonl'
            self.assertIn('Implement distinctive direct CLI worker feature', transcript_summary(path)[2])

    def test_long_command_extracts_heredoc_before_compacting(self):
        body = 'Implement distinctive backend attribution ' * 10
        result = spawn_evidence.command_evidence('export UNUSED=' + 'x' * 12000 + "\ncat <<'EOF' > /tmp/task\n" + body + '\nEOF\nmaestro worker-start --spec-file /tmp/task')
        self.assertIn(body[:120], result)
        self.assertLessEqual(len(result), spawn_evidence.SNIPPET_LIMIT)

    def test_parent_conversation_id_resolves_descendant_and_overrides_other_evidence(self):
        worker = dict(session_id='explicit', parent_conversation_id='child000', parent_id='other000', updated_at=self.now)
        tree, _, _ = self.render([self.node('root0000'), self.node('child000', parent_id='root0000'), self.node('other000')], claude=[worker])
        self.assertEqual(tree.nodes['node-sub-claude-explicit'][1], ['node-agy-root0000'])

    def test_live_process_attribution_without_log_output_or_matching_prompt(self):
        process = dict(pid=12345, provider='codex', session_id='process-worker', parent_conversation_id='child000', spec='Actual live command', cwd='/different/repo')
        self.log('child000', '', 'tasks/task-44.log')
        with patch('live_worker_processes.scan_live_worker_processes', return_value=[process]):
            tree, html, _ = self.render([self.node('root0000'), self.node('child000', parent_id='root0000'), self.node('other000')])
        self.assertEqual(tree.nodes['node-sub-cdx-process-worker'][1], ['node-agy-root0000'])
        self.assertIn('data-sub-running="1"', html.split('node-sub-cdx-process-worker', 1)[1].split('>', 1)[0])

    def test_stale_planner_response_stays_running_with_background_job(self):
        self.log('child000', json.dumps({'type': 'PLANNER_RESPONSE', 'status': 'DONE'}), age=180)
        child = self.node('child000', parent_id='root0000')
        child.update(last_modified_time='2020-01-01T00:00:00Z', status='DONE', has_active_bg=True)
        self.assertTrue(widget._agy_worker_is_active(child, set()))
        tree, html, _ = self.render([self.node('root0000'), child])
        self.assertEqual(tree.nodes['node-sub-child000'][1], ['node-agy-root0000'])
        self.assertIn('data-sub-running="1"', html.split('node-sub-child000', 1)[1].split('>', 1)[0])
        child['killed'] = True
        self.assertFalse(widget._agy_worker_is_active(child, set()))
        tree, _, _ = self.render([self.node('root0000'), child])
        self.assertNotIn('node-sub-child000', tree.nodes)

    def test_live_launcher_and_native_binary_merge_into_one_session(self):
        processes = [dict(pid=111, provider='codex', parent_conversation_id='root0000', spec='Implement unique live worker feature'),
                     dict(pid=112, provider='codex', parent_conversation_id='root0000', session_id='canonical', spec='Implement unique live worker feature')]
        merged = widget._merge_live_process_workers([], 'codex', processes, self.now)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['thread_id'], 'canonical')

    def test_live_worker_does_not_claim_another_known_session_with_same_title(self):
        old = dict(thread_id='other-session', title='Shared generated title',
                   spec_preview='Implement unrelated feature in a different workspace', status='running')
        process = dict(pid=123, provider='codex', session_id='actual-session',
                       parent_conversation_id='root0000', spec='✦ Shared generated title\n\nImplement the actual independent feature')
        nodes = widget._merge_live_process_workers([old], 'codex', [process], self.now)
        self.assertEqual(len(nodes), 2)
        self.assertNotIn('parent_conversation_id', nodes[0])
        self.assertEqual(nodes[1]['thread_id'], 'actual-session')

    def test_registration_captures_parent_env_and_fd_without_overwriting_explicit(self):
        registry = self.home / 'registry.json'
        with patch.object(maestro, 'SPAWN_REGISTRY_PATH', str(registry)):
            with patch.dict(os.environ, ANTIGRAVITY_CONVERSATION_ID='env-parent'):
                maestro.record_spawned_session('env-worker', {'provider': 'codex'})
                maestro.record_spawned_session('explicit-worker', {'parent_conversation_id': 'explicit'})
            with patch.dict(os.environ, ANTIGRAVITY_CONVERSATION_ID=''), patch('live_worker_processes.os.readlink', return_value='/brain/fd-parent/.system_generated/tasks/task-44.log'):
                maestro.record_spawned_session('fd-worker', {'provider': 'claude'})
        records = json.loads(registry.read_text())
        self.assertEqual(records['env-worker']['parent_conversation_id'], 'env-parent')
        self.assertEqual(records['fd-worker']['parent_conversation_id'], 'fd-parent')
        self.assertEqual(records['explicit-worker']['parent_conversation_id'], 'explicit')


if __name__ == '__main__':
    unittest.main()
