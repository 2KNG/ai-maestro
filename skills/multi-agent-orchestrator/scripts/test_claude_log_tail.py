"""Claude detail-card logs use local transcripts without contacting the LanguageServer."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import maestro_memory as memory


class ClaudeLogTailTests(unittest.TestCase):
    def test_tail_reads_text_and_tools_and_ignores_partial_events(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            path = Path(home) / ".claude/projects/repo/session.jsonl"
            path.parent.mkdir(parents=True)
            events = [
                {"timestamp": "2026-10-18T10:00:00Z", "message": {"role": "user", "content": "Fix workers"}},
                {"timestamp": "2026-10-18T10:00:01Z", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Read"}, {"type": "text", "text": "Workers fixed"}]}},
            ]
            path.write_text("\n".join(json.dumps(e) for e in events) + '\nnull\n{"partial":')
            with mock.patch("check_all_usage.discover_ls_credentials") as discover:
                result = memory.extract_session_log_tail("claude", "session", max_lines=2)
            discover.assert_not_called()
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["lines"]), 2)
            self.assertIn("Read", result["lines"][0])
            self.assertIn("Workers fixed", result["lines"][1])

    def test_missing_or_path_like_session_does_not_use_other_provider(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            for session in ("missing", "../../secret", "*"):
                with self.subTest(session=session):
                    self.assertFalse(memory.extract_session_log_tail("claude", session)["ok"])


if __name__ == "__main__":
    unittest.main()
