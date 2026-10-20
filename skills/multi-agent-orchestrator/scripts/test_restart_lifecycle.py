"""Regression fixtures for runtime restart and explicit child termination."""
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agy_transcript_state as lifecycle
import check_all_usage as usage


class RestartLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)

    def log(self, cid, steps):
        path = self.home / '.gemini/antigravity/brain' / cid / '.system_generated/logs/transcript.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(map(json.dumps, steps)))
        return path

    def spawn(self):
        return [{'tool_calls': [{'name': 'invoke_subagent'}]},
                {'content': 'Created the following subagents: {"conversationId":"child"}'}]

    def test_restart_preserves_child_and_explicit_kill_survives_restart(self):
        restart = {'type': 'SYSTEM_MESSAGE', 'content': lifecycle.RESTART}
        self.log('parent', self.spawn() + [restart])
        self.assertEqual(lifecycle.transcript_state(['parent']), ({'child': 'parent'}, {'child': False}))
        kill = {'created_at': '2026-10-20T01:00:00Z', 'tool_calls': [
            {'name': 'manage_subagents', 'args': {'Action': 'kill_all'}}]}
        self.log('parent', self.spawn() + [kill, restart])
        self.assertTrue(lifecycle.transcript_state(['parent'])[1]['child'])

    def test_new_child_activity_clears_kill_without_using_file_mtime(self):
        self.log('parent', self.spawn() + [{'created_at': '2026-10-20T01:00:00Z', 'tool_calls': [
            {'name': 'manage_subagents', 'args': {'Action': 'kill', 'Ids': ['child']}}]}])
        for timestamp, expected in [('2026-10-20T00:59:59Z', True),
                                    ('2026-10-20T01:00:00Z', True),
                                    ('2026-10-20T01:00:01Z', False)]:
            with self.subTest(timestamp=timestamp):
                self.log('child', [{'type': 'PLANNER_RESPONSE', 'created_at': timestamp}])
                self.assertEqual(lifecycle.transcript_state(['parent', 'child'])[1]['child'], expected)

    def task(self, number, minute):
        return {'created_at': f'2026-10-20T01:{minute:02d}:00Z',
                'content': f'background task with task id: parent/task-{number}\nTask Description: service --port 9876'}

    def test_delayed_restart_preserves_tasks_started_after_event(self):
        path = self.log('parent', [self.task(1, 0), self.task(2, 2), {
            'type': 'SYSTEM_MESSAGE', 'created_at': '2026-10-20T01:01:00Z',
            'content': lifecycle.RESTART}, self.task(3, 3)])
        tasks = usage._parse_background_tasks(path)
        self.assertEqual({k: v['done'] for k, v in tasks.items()},
                         {'parent/task-1': True, 'parent/task-2': False, 'parent/task-3': False})

    def test_explicit_restart_task_list_only_stops_listed_tasks(self):
        path = self.log('parent', [self.task(1, 0), self.task(2, 0), {
            'type': 'SYSTEM_MESSAGE', 'created_at': '2026-10-20T01:01:00Z',
            'content': 'Task parent/task-1 stopped due to server restart'}])
        tasks = usage._parse_background_tasks(path)
        self.assertTrue(tasks['parent/task-1']['done'])
        self.assertFalse(tasks['parent/task-2']['done'])

    def test_even_a_named_later_task_survives_an_earlier_restart(self):
        import render_usage_widget as widget
        path = self.log('parent', [self.task(2, 2), {
            'type': 'SYSTEM_MESSAGE', 'created_at': '2026-10-20T01:01:00Z',
            'content': 'Task parent/task-2 stopped due to server restart'}])
        for parser in (usage._parse_background_tasks, widget._parse_render_background_tasks):
            self.assertFalse(parser(path)['parent/task-2']['done'])

    def test_restart_quote_is_not_runtime_event(self):
        for kind in ('USER_MESSAGE', 'GENERIC'):
            path = self.log('parent', [self.task(1, 0), {'type': kind, 'content': lifecycle.RESTART}])
            self.assertFalse(usage._parse_background_tasks(path)['parent/task-1']['done'])

    def test_collector_active_background_overrides_stale_done_but_not_kill(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        summaries = {'parent': {'summary': 'Parent', 'lastModifiedTime': now},
                     'child': {'summary': 'Child', 'lastModifiedTime': now,
                               'status': 'CASCADE_RUN_STATUS_DONE'}}
        def response(request, **kwargs):
            return io.BytesIO(json.dumps({'trajectorySummaries': summaries}
                if request.full_url.endswith('GetAllCascadeTrajectories') else {}).encode())
        self.log('parent', self.spawn())
        self.log('child', [{'type': 'PLANNER_RESPONSE', 'status': 'DONE'}])
        for background, processes in (([{'id': 'task-1'}], []),
                                      ([], [{'parent_conversation_id': 'child', 'pid': 123}])):
            self.log('parent', self.spawn())
            with patch.object(usage, 'discover_ls_credentials', return_value=('localhost:1', 'test')), \
                 patch.object(usage, 'get_last_selected_agy_model', return_value=None), \
                 patch.object(usage, '_load_agy_spawn_registry', return_value={}), \
                 patch.object(usage, 'AGY_METADATA_CACHE_PATH', str(self.home / 'meta.json')), \
                 patch.object(usage.urllib.request, 'urlopen', side_effect=response), \
                 patch('live_worker_processes.scan_live_worker_processes', return_value=processes), \
                 patch.object(usage, 'extract_active_bg_tasks', return_value=background):
                nodes = usage._fetch_antigravity_agent_tree()
                child = nodes[0]['children'][0]
                self.assertTrue(child['has_active_bg'])
                self.assertTrue(child['is_running'])
                self.log('parent', self.spawn() + [{'tool_calls': [
                    {'name': 'manage_subagents', 'args': {'Action': 'kill_all'}}]}])
                child = usage._fetch_antigravity_agent_tree()[0]['children'][0]
                self.assertTrue(child['killed'])
                self.assertFalse(child['is_running'])
                self.assertFalse(child['has_active_bg'])



if __name__ == '__main__':
    unittest.main()
