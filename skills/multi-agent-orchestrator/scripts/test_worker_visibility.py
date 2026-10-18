"""Registration-only tests; no live workers or user registry writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import maestro
import maestro_memory


class WorkerVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.registry = self.root / "sessions.json"
        self.stack.enter_context(mock.patch.object(maestro, "SPAWN_REGISTRY_PATH", str(self.registry)))
        for name in ("format_handover_context_for_prompt", "save_handover_note", "notify_long_task_completion"):
            self.stack.enter_context(mock.patch.object(maestro_memory, name, return_value=""))
        self.timer = self.stack.enter_context(mock.patch("threading.Timer"))
        self.stamp = self.stack.enter_context(mock.patch.object(maestro, "stamp_codex_db_titles", return_value=[]))
        self.run = self.stack.enter_context(mock.patch.object(maestro.subprocess, "run"))
        self.spec = "Visibility task\n" + "x" * 200

    def rows(self):
        return json.loads(self.registry.read_text())

    def check_row(self, sid, row, provider, status):
        self.assertEqual(row["provider"], provider)
        self.assertEqual(row["session_id"], sid)
        self.assertEqual(row["status"], status)
        self.assertEqual(row["spec_preview"], self.spec[:160])
        self.assertEqual(row["cwd"], str(self.root))
        self.assertIsInstance(row["started_at"], int)
        for field in ("pool", "model", "effort"):
            self.assertTrue(row[field])

    def start(self, provider, **kwargs):
        if provider == "claude":
            return maestro.start_claude_worker(kwargs.pop("pool", "claude"), self.spec,
                                               str(self.root), "high", kwargs.pop("wait", True), 30, **kwargs)
        return maestro.start_codex_worker(self.spec, str(self.root), "medium", "default", 30, **kwargs)

    def test_waiting_workers_are_visible_then_terminal(self):
        for provider in ("claude", "codex"):
            for outcome, status in ((0, "completed"), (1, "failed"),
                                    (subprocess.TimeoutExpired("worker", 30), "timeout"),
                                    (FileNotFoundError("worker"), "failed")):
                with self.subTest(provider=provider, status=status, outcome=outcome):
                    self.registry.unlink(missing_ok=True)
                    def run(*args, **kwargs):
                        rows = self.rows()
                        self.assertEqual(len(rows), 1)
                        sid, row = next(iter(rows.items()))
                        self.check_row(sid, row, provider, "running")
                        if isinstance(outcome, Exception):
                            raise outcome
                        return subprocess.CompletedProcess([], outcome, "done", "error" if outcome else "")
                    self.run.side_effect = run
                    self.start(provider)
                    sid, row = next(iter(self.rows().items()))
                    self.check_row(sid, row, provider, status)

    def test_claude_background_registers_pid(self):
        with mock.patch.object(maestro.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            result = self.start("claude", wait=False)
        self.addCleanup(Path(result["log_path"]).unlink)
        sid, row = next(iter(self.rows().items()))
        self.check_row(sid, row, "claude", "running")
        self.assertEqual(row["pid"], 123)
        self.run.assert_not_called()

    def test_fast_fallback_registers_each_attempt_before_waiting(self):
        seen = []
        def run(command, **kwargs):
            sid = command[command.index("--session-id") + 1]
            rows = self.rows()
            self.check_row(sid, rows[sid], "claude", "running")
            seen.append(sid)
            if len(seen) == 1:
                self.assertEqual(rows[sid]["model"], "claude-opus-5-fast")
                return subprocess.CompletedProcess([], 1, "", "unknown model")
            self.assertEqual(rows[seen[0]]["status"], "failed")
            self.assertEqual(rows[sid]["model"], "claude-opus-5-5")
            self.assertEqual(rows[sid]["effort"], "medium")
            return subprocess.CompletedProcess([], 0, "done", "")
        self.run.side_effect = run
        self.start("claude", pool="claude-fast")
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(self.rows()[seen[1]]["status"], "completed")

    def test_codex_stamping_merges_placeholder_and_keeps_metadata(self):
        def stamp(*args):
            # Match the real stamper's partial registration before returning IDs.
            maestro.record_spawned_session("actual-thread", {"provider": "codex", "title": "stamped"})
            return ["actual-thread"]
        self.stamp.side_effect = stamp
        def run(*args, **kwargs):
            placeholder = next(iter(self.rows()))
            self.assertTrue(placeholder.startswith("codex-"))
            self.timer.call_args.args[1]()
            self.assertEqual(set(self.rows()), {"actual-thread"})
            self.check_row("actual-thread", self.rows()["actual-thread"], "codex", "running")
            # A later lookup can miss the row; retain the ID found by the timer.
            self.stamp.side_effect = None
            self.stamp.return_value = []
            return subprocess.CompletedProcess([], 0, "done", "")
        self.run.side_effect = run
        result = self.start("codex", pool="codex-astra")
        self.assertEqual(result["thread_ids"], ["actual-thread"])
        self.timer.call_args.args[1]()  # A late timer must not restore running status.
        self.assertEqual(set(self.rows()), {"actual-thread"})
        row = self.rows()["actual-thread"]
        self.check_row("actual-thread", row, "codex", "completed")
        self.assertEqual((row["pool"], row["model"], row["effort"]),
                         ("codex-astra", "gpt-6-astra", "medium"))

    def test_codex_final_stamping_on_failure_and_timeout(self):
        for outcome, status in ((1, "failed"), (subprocess.TimeoutExpired("codex", 30), "timeout")):
            self.registry.unlink(missing_ok=True)
            self.stamp.return_value = ["thread-id"]
            self.run.side_effect = outcome if isinstance(outcome, Exception) else None
            self.run.return_value = subprocess.CompletedProcess([], 1, "", "failure")
            self.start("codex")
            self.assertEqual(set(self.rows()), {"thread-id"})
            self.check_row("thread-id", self.rows()["thread-id"], "codex", status)

    def test_codex_temporary_ids_do_not_collide_in_same_second(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(maestro.time, "time", return_value=123456):
            self.start("codex")
            self.start("codex")
        self.assertEqual(len(self.rows()), 2)

    def test_concurrent_registration_preserves_entries_and_fields(self):
        def register(index):
            maestro.record_spawned_session(str(index), {"provider": "codex", "model": "test"})
            maestro.record_spawned_session(str(index), {"status": "completed"})
        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(register, range(60)))
        rows = self.rows()
        self.assertEqual(len(rows), 60)
        for sid, row in rows.items():
            self.assertEqual(row["session_id"], sid)
            self.assertEqual(row["model"], "test")
            self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()
