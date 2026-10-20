"""Synthetic procfs coverage: attribution does not depend on log contents."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import live_worker_processes as live


class LiveWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        live.clear_cache()

    def process(self, pid, argv, ppid=1, env=None, log=None):
        path = self.root / str(pid)
        (path / 'fd').mkdir(parents=True)
        (path / 'stat').write_text(f'{pid} (name with ) parentheses) S {ppid} 0 0')
        (path / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
        (path / 'environ').write_bytes(('\0'.join(f'{k}={v}' for k, v in (env or {}).items()) + '\0').encode())
        (path / 'cwd').symlink_to(self.root)
        if log:
            (path / 'fd' / '1').symlink_to(log)
        return path

    def scan(self):
        return {r['pid']: r for r in live.scan_live_worker_processes(self.root)}

    def test_zero_byte_log_fd_and_ancestry(self):
        log = self.root / 'brain/cid-one/.system_generated/tasks/task-abc.log'
        log.parent.mkdir(parents=True)
        log.touch()
        self.process(10, ['bash'], log=log)
        self.process(11, ['python3', '/x/maestro.py', 'worker-start', '--pool', 'agy-claude', '--spec', 'actual work'], ppid=10)
        self.process(12, ['/bin/claude', '-p', '--session-id', 'child-id'], ppid=11)
        records = self.scan()
        self.assertEqual(set(records), {11, 12})
        self.assertEqual(records[11]['pool'], 'agy-claude')
        self.assertEqual(records[12]['parent_conversation_id'], 'cid-one')
        self.assertEqual(records[12]['task_id'], 'task-abc')
        self.assertEqual(records[12]['spec'], 'actual work')
        self.assertEqual(records[12]['session_id'], 'child-id')
        self.assertEqual(log.stat().st_size, 0)
        self.assertEqual(live.detect_parent_conversation_id(self.root, 10), 'cid-one')

    def test_environ_precedence_and_stderr(self):
        proc = self.process(21, ['codex', 'resume', 'session-one'], env={'ANTIGRAVITY_CONVERSATION_ID': 'env-cid'})
        (proc / 'fd/2').symlink_to('/brain/fd-cid/.system_generated/tasks/task-two.log')
        record = self.scan()[21]
        self.assertEqual(record['parent_conversation_id'], 'env-cid')
        self.assertEqual(record['session_id'], 'session-one')
        self.assertEqual(live.detect_parent_conversation_id(self.root, 21), 'env-cid')
        self.assertEqual(live.detect_parent_conversation_id(self.root, 21, environ={}), 'fd-cid')

    def test_unrelated_prompt_is_not_an_executable(self):
        self.process(30, ['bash', '-c', 'maestro worker-start --spec fake'])
        self.process(31, ['python3', '-c', 'codex exec fake'])
        self.process(32, ['echo', 'claude', '--session-id', 'fake'])
        self.process(33, ['maestro', 'status', 'worker-start'])
        self.process(34, ['codex', 'app-server'], env={'ANTIGRAVITY_CONVERSATION_ID': 'root'})
        self.assertEqual(self.scan(), {})

    def test_cache_expiry_removes_departed_process_and_returns_copies(self):
        self.process(40, ['claude', '-p', 'hello'])
        with patch.object(live.time, 'monotonic', return_value=10.0):
            first = self.scan()
            first[40]['spec'] = 'mutated'
            shutil.rmtree(self.root / '40')
            self.assertEqual(self.scan()[40]['spec'], 'hello')
        with patch.object(live.time, 'monotonic', return_value=11.01):
            self.assertEqual(self.scan(), {})

    def test_spec_file_bounds_and_relative_cwd(self):
        (self.root / 'spec.txt').write_text('original task body')
        self.process(50, ['maestro', 'worker-start', '--spec-file', 'spec.txt'])
        self.assertEqual(self.scan()[50]['spec'], 'original task body')
        (self.root / 'spec.txt').write_bytes(b'x' * (256 * 1024))
        self.assertEqual(live.scan_live_worker_processes(self.root, force=True)[0]['spec'], '')

    def test_codex_rollout_fd_and_cli_prompts(self):
        sid = '11111111-2222-3333-4444-555555555555'
        proc = self.process(60, ['node', '/bin/codex.js', 'exec', '-c', 'model=x', '--json', 'do real task'], env={'CODEX_THREAD_ID': 'inherited-parent'})
        (proc / 'fd/9').symlink_to('/x/rollout-2026-10-20-' + sid + '.jsonl')
        self.process(61, ['claude', '-p', '--model', 'opus', '--session-id', 'cli-sid', 'claude task'])
        records = self.scan()
        self.assertEqual(records[60]['session_id'], sid)
        self.assertEqual(records[60]['spec'], 'do real task')
        self.assertEqual(records[61]['spec'], 'claude task')

    def test_ancestry_cycle_is_bounded(self):
        self.process(70, ['claude'], ppid=71)
        self.process(71, ['bash'], ppid=70)
        self.assertEqual(set(self.scan()), {70})


if __name__ == '__main__':
    unittest.main()
