"""Bounded task reads, unchanged-file reuse, invalidation and lifecycle cache."""
import builtins
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import spawn_evidence as evidence
from agy_transcript_state import transcript_summary


class SpawnEvidenceCacheTests(unittest.TestCase):
    def setUp(self):
        evidence.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'task.log'

    def test_50mb_task_reads_only_boundaries_and_reuses_unchanged(self):
        with self.path.open('wb') as stream:
            stream.write(b'{"pool":"claude","session_id":"worker-at-head"}\n')
            stream.seek(50 * 1024 * 1024)
            stream.write(b'\nsession_id=worker-at-tail\n')
        reads = []
        real_open = builtins.open

        class Reader:
            def __init__(self, file):
                self.file = file
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def __getattr__(self, name):
                return getattr(self.file, name)
            def read(self, count=-1):
                reads.append(count)
                return self.file.read(count)

        with patch('builtins.open', side_effect=lambda *a, **kw: Reader(real_open(*a, **kw))) as opened:
            result = evidence.task_evidence(self.path)
            self.assertIn('worker-at-head', result[1])
            self.assertIn('worker-at-tail', result[0])
            self.assertLessEqual(len(result[0]), evidence.SNIPPET_LIMIT)
            self.assertEqual(reads, [65536, 65536])
            self.assertEqual(evidence.task_evidence(self.path), result)
            self.assertEqual(opened.call_count, 1)
        with self.path.open('ab') as stream:
            stream.write(b'new-session-id')
        with patch('builtins.open', wraps=real_open) as opened:
            self.assertIn('new-session-id', evidence.task_evidence(self.path)[0])
            self.assertEqual(opened.call_count, 1)

    def test_cached_transcript_lifecycle_and_command_invalidation(self):
        steps = [
            {'tool_calls': [{'name': 'invoke_subagent'}]},
            {'content': 'Created the following subagents: {"conversationId":"child"}'},
            {'tool_calls': [{'name': 'manage_subagents', 'args': {'Action': 'kill_all'}}]},
            {'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': 'maestro worker-start --spec "exact task"'}}]},
        ]
        self.path.write_text('\n'.join(map(json.dumps, steps)))
        first = transcript_summary(self.path)
        self.assertTrue(first[1]['child'])
        self.assertIn('exact task', first[2])
        with patch('builtins.open', side_effect=AssertionError('unchanged transcript reopened')):
            self.assertEqual(transcript_summary(self.path), first)
        with self.path.open('a') as stream:
            stream.write('\n' + json.dumps({'tool_calls': [{'name': 'send_message', 'args': {'Recipient': 'child'}}]}))
        self.assertFalse(transcript_summary(self.path)[1]['child'])

    def test_terminal_json_after_progress_is_launch_evidence(self):
        self.path.write_text('Progress starting\n' + json.dumps({
            'pool': 'codex', 'thread_ids': ['terminal-worker']}, indent=2))
        self.assertIn('terminal-worker', evidence.task_evidence(self.path)[1])
        self.path.write_text('Investigate quoted ' + json.dumps({
            'pool': 'codex', 'thread_ids': ['quoted-worker']}))
        self.assertEqual(evidence.task_evidence(self.path)[1], frozenset())

    def test_many_large_outputs_keep_only_small_summary(self):
        steps = [{'type': 'SYSTEM_MESSAGE', 'content': 'Output:\n' + 'x' * 20000}
                 for _ in range(20)]
        self.path.write_text('\n'.join(map(json.dumps, steps)))
        self.assertLessEqual(len(transcript_summary(self.path)[2]), evidence.SNIPPET_LIMIT)

    def test_long_spec_prefix_matches_but_ambiguous_prefix_does_not(self):
        from render_usage_widget import _worker_parent
        spec = 'Implement independent feature ' * 30
        worker = {'session_id': 'worker', 'spec': spec}
        parents = [{'conversation_id': 'one'}, {'conversation_id': 'two'}]
        self.assertEqual(_worker_parent(worker, parents, {'one': spec[:200]}, 0), 'one')
        self.assertIsNone(_worker_parent(worker, parents,
                                        {'one': spec[:200], 'two': spec[:200]}, 0))

    def test_concurrent_miss_opens_once_and_mtime_change_invalidates(self):
        from concurrent.futures import ThreadPoolExecutor
        self.path.write_text('first-spec')
        with patch('builtins.open', wraps=builtins.open) as opened:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(evidence.task_evidence, [self.path] * 16))
            self.assertEqual(opened.call_count, 1)
            self.assertTrue(all(value == results[0] for value in results))
        previous = self.path.stat()
        self.path.write_text('other-spec')
        os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
        with patch('builtins.open', wraps=builtins.open) as opened:
            self.assertIn('other-spec', evidence.task_evidence(self.path)[0])
            evidence.task_evidence(self.path)
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(evidence._cache), 1)
        changed = self.path.stat()
        with self.path.open('a') as stream:
            stream.write(' appended')
        os.utime(self.path, ns=(changed.st_atime_ns, changed.st_mtime_ns))
        with patch('builtins.open', wraps=builtins.open) as opened:
            self.assertIn('appended', evidence.task_evidence(self.path)[0])
            evidence.task_evidence(self.path)
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(evidence._cache), 1)

    def test_background_expiry_uses_current_time_without_rereading(self):
        from datetime import datetime, timezone
        import check_all_usage as usage
        cid = 'timer-parent'
        transcript = Path(self.tmp.name) / '.gemini/antigravity/brain' / cid / '.system_generated/logs/transcript.jsonl'
        transcript.parent.mkdir(parents=True)
        created = 100000.0
        transcript.write_text(json.dumps({
            'created_at': datetime.fromtimestamp(created, timezone.utc).isoformat(),
            'content': 'background task with task id: timer-parent/task-1\nTask Description: Timer: 10s',
        }))
        with patch.dict(os.environ, HOME=self.tmp.name):
            self.assertEqual(len(usage.extract_active_bg_tasks(cid, created + 1)), 1)
            with patch('builtins.open', side_effect=AssertionError('unchanged transcript reopened')):
                self.assertEqual(usage.extract_active_bg_tasks(cid, created + 30), [])

    def test_lru_is_bounded(self):
        with patch.object(evidence, 'CACHE_LIMIT', 2):
            for index in range(3):
                path = Path(self.tmp.name) / str(index)
                path.write_text(str(index))
                evidence.task_evidence(path)
            self.assertEqual(len(evidence._cache), 2)


if __name__ == '__main__':
    unittest.main()
