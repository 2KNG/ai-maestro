"""Quota fallback never materializes whole session transcripts."""
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import check_all_usage as usage


class BoundedQuotaTests(unittest.TestCase):
    def test_huge_rollout_uses_bounded_reads_and_latest_complete_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'rollout.jsonl')
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            def event(used):
                return json.dumps({'timestamp': now, 'payload': {
                    'rate_limits': {'primary': {'used_percent': used, 'window_minutes': 10080},
                                    'secondary': {'used_percent': 17, 'window_minutes': 300}},
                    'info': {'total_token_usage': {'total_tokens': 123456}}}}).encode() + b'\n'
            with open(path, 'wb') as handle:
                handle.write(event(99))
                handle.write(b'{"large_tool_output":"')
                # Sparse 50 MiB record keeps the fixture cheap but exercises actual seeks.
                handle.seek(50 * 1024 * 1024)
                handle.write(b'"}\n' + event(32) + b'{"payload":{"rate_limits":')
            real_open = open
            reads = []
            class GuardedReader:
                def __init__(self):
                    self.handle = real_open(path, 'rb')
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.handle.close()
                def seek(self, *args):
                    return self.handle.seek(*args)
                def tell(self):
                    return self.handle.tell()
                def read(self, size=-1):
                    if not 0 <= size <= 1024 * 1024:
                        raise AssertionError('Unbounded rollout read')
                    reads.append(size)
                    return self.handle.read(size)
                def readlines(self, *args):
                    raise AssertionError('Whole rollout readlines')
            def guarded_open(filename, mode='r', **kwargs):
                if filename == path:
                    return GuardedReader()
                if 'w' in mode:
                    return io.StringIO()
                raise FileNotFoundError(filename)
            with mock.patch('builtins.open', side_effect=guarded_open), mock.patch.object(
                usage.os.path, 'exists', return_value=False
            ), mock.patch.object(usage.glob, 'glob', return_value=[path]), mock.patch.object(
                usage, '_codex_model_metadata', return_value={}
            ):
                result = usage.get_codex_usage(ttl_sec=0)
            self.assertEqual(result['weekly']['remaining_percent'], 68)
            self.assertEqual(result['five_hour']['remaining_percent'], 83)
            self.assertFalse(result['stale'])
            self.assertEqual(reads, [1024 * 1024])
