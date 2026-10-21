"""Offline Claude session discovery tests; never use real HOME or processes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import check_all_usage as usage


class ClaudeAgentTreeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.now = 1_800_000_000
        self.stack.enter_context(mock.patch.object(usage.time, "time", return_value=self.now))
        self.kill = self.stack.enter_context(mock.patch.object(
            usage.os, "kill", side_effect=ProcessLookupError))
        self.ps = self.stack.enter_context(mock.patch.object(
            usage.subprocess, "check_output", return_value=""))
        self.stack.enter_context(mock.patch.object(
            usage.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(mock.patch.dict(usage._CLAUDE_JSONL_CACHE, clear=True))

    def write(self, name, value, raw=False):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if raw else json.dumps(value))
        os.utime(path, (self.now, self.now))
        return str(path)

    def registry(self, value):
        return self.write(".gemini/antigravity/agy_spawned_sessions.json", value)

    def transcript(self, sid, events, project="work"):
        return self.write(f".claude/projects/{project}/{sid}.jsonl",
                          "\n".join(json.dumps(e) for e in events), raw=True)

    def assistant(self, mid, **tokens):
        return {"type": "assistant", "timestamp": self.now - 10,
                "message": {"id": mid, "model": "claude-test", "usage": tokens}}

    def test_empty_home(self):
        self.assertEqual(usage.get_claude_agent_tree(), [])

    def test_merge_sources_and_deduplicate_streaming_usage(self):
        events = [
            {"type": "user", "sessionId": "session", "timestamp": self.now - 100,
             "cwd": "/work", "message": {"content": [{"type": "text", "text": "Fix issue"}]}},
            self.assistant("m1", input_tokens=10, output_tokens=2,
                           cache_creation_input_tokens=4, cache_read_input_tokens=20),
            self.assistant("m1", input_tokens=10, output_tokens=8),
            self.assistant("m1", output_tokens=3),
            self.assistant("m2", input_tokens=5, output_tokens=1),
        ]
        path = self.transcript("session", events)
        # Duplicate transcript copies must not double-count the same messages.
        self.transcript("session", events, project="copy")
        self.write(".claude/sessions/123.json", {
            "sessionId": "session", "pid": 123, "startedAt": self.now - 100})
        self.registry({"session": {"provider": "claude", "title": "Worker",
                                  "effort": "high", "updated_at": self.now - 5}})
        self.kill.side_effect = None
        tree = usage.get_claude_agent_tree()
        self.assertEqual(len(tree), 1)
        row = tree[0]
        self.assertEqual(row["id"], row["session_id"])
        self.assertEqual(row["title"], "Worker")
        self.assertEqual(row["model"], "claude-test")
        self.assertEqual(row["effort"], "high")
        self.assertEqual(row["cwd"], "/work")
        self.assertEqual(row["spec_preview"], "Fix issue")
        self.assertEqual(row["tokens"], 48)
        self.assertEqual(row["usage"]["output_tokens"], 9)
        self.assertEqual(row["created_at"], self.now - 100)
        self.assertEqual(row["updated_at"], self.now - 5)
        self.assertEqual(row["rollout_path"], path)
        self.assertEqual(row["jsonl_path"], path)
        self.assertTrue(row["running"] and row["is_running"] and row["agy_origin"])
        self.assertEqual(row["status"], "running")
        self.kill.assert_called_once_with(123, 0)
        json.dumps(tree)

    def test_recent_or_live_not_registry_running_claim(self):
        self.registry({
            "old_live": {"provider": "claude", "updated_at": self.now - 90000, "pid": 7},
            "old_dead": {"provider": "claude", "updated_at": self.now - 90000,
                         "pid": 8, "status": "running"},
            "recent": {"provider": "claude", "updated_at": self.now - 1, "pid": 8},
            "boundary": {"provider": "claude", "updated_at": self.now - 86400},
            "other": {"provider": "codex", "updated_at": self.now, "pid": 7},
        })
        def kill(pid, signal):
            if pid != 7:
                raise ProcessLookupError
        self.kill.side_effect = kill
        rows = usage.get_claude_agent_tree()
        self.assertEqual([r["id"] for r in rows], ["recent", "boundary", "old_live"])
        self.assertFalse(rows[0]["running"])
        self.assertTrue(rows[-1]["running"])

    def test_ps_fallback_exact_options_and_pid_verification(self):
        self.registry({sid: {"provider": "claude", "updated_at": self.now}
                       for sid in ("found", "resumed", "dead", "prompt", "shell")})
        self.ps.return_value = '\n'.join([
            '101 /usr/bin/claude --session-id=found -p task',
            '102 node /opt/claude/cli.js --resume resumed',
            '103 claude --session-id dead',
            '104 claude -p "discuss --session-id prompt"',
            '105 bash -c "claude --session-id shell"',
            'broken',
        ])
        def kill(pid, signal):
            if pid == 103:
                raise ProcessLookupError
        self.kill.side_effect = kill
        rows = {r["id"]: r for r in usage.get_claude_agent_tree()}
        self.assertEqual({sid for sid, row in rows.items() if row["running"]}, {"found", "resumed"})
        self.assertEqual(self.kill.call_count, 3)

    def test_permission_denied_pid_is_live_and_invalid_pids_ignored(self):
        self.registry({sid: {"provider": "claude", "updated_at": self.now, "pid": pid}
                       for sid, pid in (("allowed", "123"), ("zero", 0), ("negative", -1),
                                        ("bool", True), ("bad", "oops"))})
        self.kill.side_effect = PermissionError
        rows = usage.get_claude_agent_tree()
        self.assertEqual([r["id"] for r in rows if r["running"]], ["allowed"])
        self.kill.assert_called_once_with(123, 0)

    def test_malformed_files_partial_jsonl_and_bad_usage(self):
        self.write(".claude/sessions/bad.json", '{', raw=True)
        self.write(".claude/sessions/list.json", [])
        self.write(".gemini/antigravity/agy_spawned_sessions.json", '{', raw=True)
        event = self.assistant("m", input_tokens=-1, output_tokens=3,
                               cache_creation_input_tokens=True, cache_read_input_tokens="bad")
        self.write(".claude/projects/work/a.jsonl",
                   'bad\n[]\nnull\n' + json.dumps(event) + '\n{"message":', raw=True)
        self.ps.side_effect = subprocess.TimeoutExpired("ps", 2)
        rows = usage.get_claude_agent_tree()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 3)
        self.assertFalse(rows[0]["running"])

    def test_session_metadata_milliseconds_and_iso_timestamps(self):
        self.write(".claude/sessions/42.json", {"sessionId": "meta", "pid": 42,
            "startTime": (self.now - 50) * 1000, "cwd": "/meta"})
        self.registry({"meta": {"provider": "claude", "updated_at": "2027-01-15T08:00:00Z"}})
        row = usage.get_claude_agent_tree()[0]
        self.assertEqual(row["created_at"], self.now - 50)
        self.assertEqual(row["updated_at"], self.now)
        self.assertEqual(row["cwd"], "/meta")

    def test_old_transcript_not_revived_by_copy_mtime(self):
        self.transcript("old", [{"timestamp": self.now - 90000, "type": "user"}])
        self.assertEqual(usage.get_claude_agent_tree(), [])

    def test_unchanged_transcript_is_not_reread_and_appends_parse_incrementally(self):
        path = self.transcript("inc", [self.assistant("m1", input_tokens=10)])
        self.assertEqual(usage.get_claude_agent_tree()[0]["tokens"], 10)
        real_open = open
        with mock.patch("builtins.open", side_effect=lambda f, *a, **k: (
                self.fail("unchanged transcript reopened") if f == path else real_open(f, *a, **k))):
            self.assertEqual(usage.get_claude_agent_tree()[0]["tokens"], 10)
        offset = usage._CLAUDE_JSONL_CACHE[path][2]["offset"]
        with open(path, "a") as handle:
            handle.write("\n" + json.dumps(self.assistant("m1", input_tokens=10, output_tokens=5))
                         + "\n" + json.dumps(self.assistant("m2", output_tokens=7)) + "\n{\"partial")
        os.utime(path, (self.now, self.now + 1))
        self.assertEqual(usage.get_claude_agent_tree()[0]["tokens"], 22)
        self.assertGreater(usage._CLAUDE_JSONL_CACHE[path][2]["offset"], offset)

    def test_transcripts_older_than_lookback_are_skipped(self):
        path = self.transcript("stale", [self.assistant("m", output_tokens=1)])
        old = self.now - usage.CLAUDE_JSONL_LOOKBACK_SEC - 1
        os.utime(path, (old, old))
        self.assertEqual(usage.get_claude_agent_tree(), [])
        self.assertNotIn(path, usage._CLAUDE_JSONL_CACHE)

    def test_limit_and_order(self):
        self.registry({str(i): {"provider": "claude", "updated_at": self.now - i}
                       for i in range(40)})
        rows = usage.get_claude_agent_tree()
        self.assertEqual(len(rows), 30)
        self.assertEqual([r["id"] for r in rows], [str(i) for i in range(30)])

    def test_collect_usage_integration_and_opt_out(self):
        with ExitStack() as stack:
            for name in ("get_antigravity_usage", "get_codex_usage", "get_claude_status", "select_best_pool"):
                stack.enter_context(mock.patch.object(usage, name, return_value={}))
            for name in ("get_antigravity_agent_tree", "get_codex_agent_tree"):
                stack.enter_context(mock.patch.object(usage, name, return_value=[]))
            tree = stack.enter_context(mock.patch.object(usage, "get_claude_agent_tree", return_value=[{"id": "c"}]))
            self.assertEqual(usage.collect_usage()["agent_trees"]["claude"], [{"id": "c"}])
            self.assertNotIn("agent_trees", usage.collect_usage(include_agent_trees=False))
            tree.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
