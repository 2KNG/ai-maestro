"""Offline regression checks for reset handling and local worker dispatch."""
from contextlib import ExitStack, redirect_stdout
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import check_all_usage as usage
import maestro as workers
import maestro_memory
import benchmark_sync


class ClaudeResetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = root / "cache.json"
        credentials = root / "credentials.json"
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-token"}}))
        paths = {"~/.claude/.credentials.json": credentials,
                 "~/.claude/history.jsonl": root / "history.jsonl",
                 "~/.gemini/antigravity/claude_usage_cache.json": self.cache}
        self.stack.enter_context(mock.patch.object(usage.os.path, "expanduser", side_effect=lambda p: str(paths[p])))
        self.http = self.stack.enter_context(mock.patch.object(usage.urllib.request, "urlopen"))

    def payload(self, fh_reset="2000-01-01T00:00:00Z", weekly_reset="2000-01-01T00:00:00Z"):
        return {"five_hour": {"utilization": 100, "resets_at": fh_reset},
                "seven_day": {"utilization": 100, "resets_at": weekly_reset}}

    def test_iso_boundaries_offsets_and_invalid_values(self):
        now = datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc)
        with mock.patch.object(usage.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.now.return_value = now
            for timestamp in ("2026-10-17T00:00:00Z", "2026-10-17T09:00:00+09:00", "2026-10-17T00:00:00"):
                self.assertTrue(usage._is_iso_past(timestamp))
            for timestamp in ("2026-10-17T00:00:01Z", None, "", "invalid", 42):
                self.assertFalse(usage._is_iso_past(timestamp))

    def test_cache_survives_five_minutes_and_resets_both_windows(self):
        self.cache.write_text(json.dumps(self.payload()))
        timestamp = datetime.datetime.now().timestamp() - 300
        os.utime(self.cache, (timestamp, timestamp))
        result = usage.get_claude_status()
        self.http.assert_not_called()
        self.assertFalse(result["stale"])
        self.assertEqual(result["five_hour"]["status"], "가용 (리셋 완료)")
        self.assertEqual(result["weekly"]["status"], "활성 (리셋 완료)")
        for window in ("five_hour", "weekly"):
            self.assertEqual(result[window]["remaining_percent"], 100.0)
            self.assertEqual(result[window]["used_percent"], 0.0)
        self.assertEqual(usage._routing_remaining(result), 100.0)
        with mock.patch("benchmark_sync.get_daily_benchmark_and_models", return_value={}):
            self.assertEqual(usage.select_best_pool({"claude_local": result})["pool"], "claude")

    def test_api_reset_and_independent_windows(self):
        for fh_reset, weekly_reset, fh_remaining, weekly_remaining in (
            ("2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z", 0, 0),
            ("2999-01-01T00:00:00Z", "2000-01-01T00:00:00Z", 0, 100),
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", 100, 100),
        ):
            with self.subTest(fh_reset=fh_reset, weekly_reset=weekly_reset):
                self.cache.unlink(missing_ok=True)
                self.http.return_value = io.BytesIO(json.dumps(self.payload(fh_reset, weekly_reset)).encode())
                result = usage.get_claude_status()
                self.assertEqual(result["five_hour"]["remaining_percent"], fh_remaining)
                self.assertEqual(result["weekly"]["remaining_percent"], weekly_remaining)

    def test_cache_expires_after_ten_minutes_and_429_falls_back(self):
        self.cache.write_text(json.dumps(self.payload()))
        timestamp = datetime.datetime.now().timestamp() - 601
        os.utime(self.cache, (timestamp, timestamp))
        self.http.side_effect = OSError("HTTP 429")
        result = usage.get_claude_status()
        self.http.assert_called_once()
        self.assertTrue(result["stale"])
        self.assertEqual(result["weekly"]["remaining_percent"], 100.0)
        self.assertEqual(result["five_hour"]["remaining_percent"], 100.0)
        self.assertTrue(result["reset_reconciled"])
        self.assertEqual(usage._routing_remaining(result), 100.0)

    def test_stale_weekly_reset_does_not_refresh_an_unexpired_five_hour_window(self):
        self.cache.write_text(json.dumps(self.payload("2999-01-01T00:00:00Z")))
        timestamp = datetime.datetime.now().timestamp() - 601
        os.utime(self.cache, (timestamp, timestamp))
        self.http.side_effect = OSError("HTTP 429")
        result = usage.get_claude_status()
        self.assertFalse(result["reset_reconciled"])
        self.assertIsNone(usage._routing_remaining(result))


class WorkerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("format_handover_context_for_prompt", ""),
                            ("save_handover_note", None), ("notify_long_task_completion", None)):
            self.stack.enter_context(mock.patch.object(maestro_memory, name, return_value=value))
        self.stack.enter_context(mock.patch.object(workers, "record_spawned_session"))
        self.stack.enter_context(mock.patch.object(workers, "stamp_codex_db_titles", return_value=[]))
        self.stack.enter_context(mock.patch("threading.Timer"))
        self.run = self.stack.enter_context(mock.patch.object(workers.subprocess, "run"))
        self.run.return_value = subprocess.CompletedProcess([], 0, "done", "")

    def test_claude_aliases_models_and_efforts(self):
        for pool in workers.CLAUDE_POOLS:
            with self.subTest(pool=pool):
                result = workers.start_claude_worker(pool, "patch files", "/tmp", None, True, 30)
                command = self.run.call_args.args[0]
                expected = "claude-opus-5-fast" if pool in workers.CLAUDE_FAST_POOLS else "claude-opus-5-5"
                self.assertEqual(command[command.index("--model") + 1], expected)
                self.assertIn("--dangerously-skip-permissions", command)
                self.assertEqual(self.run.call_args.kwargs["cwd"], "/tmp")
                self.assertEqual(result["effort"], "ultra" if pool in workers.CLAUDE_ULTRA_POOLS else "high")
                self.assertEqual(workers._canonical_pool_key(pool), "claude")

    def test_fast_fallback_only_for_model_errors(self):
        for failure, count in (("unknown model claude-opus-5-fast", 2), ("HTTP 429 rate limit", 1), ("task failed", 1)):
            with self.subTest(failure=failure):
                self.run.reset_mock()
                self.run.side_effect = [subprocess.CompletedProcess([], 1, "", failure),
                                        subprocess.CompletedProcess([], 0, "done", "")]
                result = workers.start_claude_worker("claude-fast", "task", "/tmp", "high", True, 30)
                self.assertEqual(self.run.call_count, count)
                if count == 2:
                    command = self.run.call_args.args[0]
                    self.assertEqual(command[command.index("--model") + 1], "claude-opus-5-5")
                    self.assertEqual(command[command.index("--effort") + 1], "medium")
                    self.assertTrue(result["ok"])
                else:
                    self.assertFalse(result["ok"])

    def test_claude_timeout_or_missing_cli_returns_failure(self):
        for failure in (FileNotFoundError("missing CLI"), subprocess.TimeoutExpired("claude", 1)):
            self.run.side_effect = failure
            self.assertFalse(workers.start_claude_worker("claude", "task", "/tmp", None, True, 1)["ok"])

    def test_claude_background_launch_returns_pid_and_log(self):
        with mock.patch.object(workers.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            result = workers.start_claude_worker("claude", "task", "/tmp", "high", False, 30)
            self.assertEqual(result["pid"], 123)
            self.assertEqual(result["status"], "running")
            self.run.assert_not_called()
            Path(result["log_path"]).unlink()

    def test_codex_default_alias_and_explicit_models(self):
        for pool, model, expected in (("codex", None, "gpt-6.1-sol"),
                                     *((p, None, "gpt-6-astra") for p in workers.CODEX_ASTRA_POOLS),
                                     ("codex", "gpt-6-astra", "gpt-6-astra")):
            result = workers.start_codex_worker("task", "/tmp", "medium", "priority", 30, pool=pool, model=model)
            command = self.run.call_args.args[0]
            self.assertEqual(command[command.index("-m") + 1], expected)
            self.assertIn('service_tier="default"', command)
            self.assertEqual(result["pool"], pool)
            self.assertIn(expected, result["model"])
            self.assertEqual(workers._canonical_pool_key(pool), "codex")

    def test_auto_selection_and_exclusion_allow_local_claude(self):
        routing = {"pool": "codex", "candidates": [
            {"pool": "codex", "status": "available", "remaining_percent": 80},
            {"pool": "claude", "status": "available", "remaining_percent": 70}]}
        with mock.patch.object(usage, "collect_usage", return_value={}), mock.patch.object(usage, "select_best_pool", return_value=routing):
            self.assertEqual(workers.choose_auto_pool(exclude_canonical={"codex"})[0], "claude")
        self.assertEqual(workers._map_canonical_to_cli_pool("claude"), "claude")

    def test_main_dispatches_new_pools_and_model_flag(self):
        for flags, expected, effort in ((["--pool", "claude-fast"], "claude", None),
                                        (["--pool", "codex-astra"], "codex", "medium"),
                                        (["--model", "gpt-6-astra"], "codex", "medium")):
            with self.subTest(flags=flags), mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "task", "--wait", *flags]), mock.patch.object(usage, "collect_usage", return_value={}), mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, mock.patch.object(workers, "start_claude_worker", return_value={"ok": True}) as claude, mock.patch.object(workers, "start_agy_worker") as agy, redirect_stdout(io.StringIO()):
                workers.main()
                agy.assert_not_called()
                if expected == "claude":
                    self.assertEqual(claude.call_args.args[3], effort)
                    codex.assert_not_called()
                else:
                    self.assertEqual(codex.call_args.args[2:4], (effort, "default"))
                    claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
