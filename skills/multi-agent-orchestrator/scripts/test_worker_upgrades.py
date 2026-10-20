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
import maestro_harness_hook


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
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "MAESTRO_WORKER_DEPTH": "0", "MAESTRO_WORKER_POOL": "",
        }))
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
        for pool, effort in (*( (p, None) for p in workers.CLAUDE_ULTRA_POOLS ), ("claude", "ultra"), ("claude-fast", "ultra")):
            for kwargs in ({"interactive": True}, {"urgent": True}):
                with self.subTest(pool=pool, effort=effort, kwargs=kwargs):
                    result = workers.start_claude_worker(
                        pool, "patch ultrathink files", "/tmp", effort, True, 30, **kwargs)
                    command = self.run.call_args.args[0]
                    self.assertEqual((result["model"], result["effort"], result["tier"]),
                                     ("claude-opus-5-5", "ultra", "default"))
                    self.assertEqual(command[command.index("--model") + 1], "claude-opus-5-5")
                    self.assertTrue(command[-1].startswith("✦ patch ultrathink files\n\nultrathink\n\n"))
        result = workers.start_claude_worker("claude", "max effort task", "/tmp", "max", True, 30)
        command = self.run.call_args.args[0]
        self.assertEqual(result["effort"], "max")
        self.assertEqual(command[command.index("--effort") + 1], "max")
        self.assertNotIn("\n\nultrathink\n\n", command[-1])

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

    def test_is_computer_use_task(self):
        positive = [
            "pc 조작 필요", "간단한 pc조작 작업", "컴퓨터 조작해줘", "using Computer Use API",
            "computer-use automated click", "데스크탑 조작 테스트", "마우스 클릭 자동화",
            "화면을 조작하여 버튼 누르기", "xdotool key enter", "GUI 조작 수행",
        ]
        for spec in positive:
            with self.subTest(spec=spec):
                self.assertTrue(workers.is_computer_use_task(spec))

        negative = ["일반 버그 수정", "단위 테스트 실행", "리팩토링", "", None]
        for spec in negative:
            with self.subTest(spec=spec):
                self.assertFalse(workers.is_computer_use_task(spec))

    def test_auto_routing_computer_use_to_codex(self):
        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "화면을 조작해서 브라우저 열어줘", "--pool", "auto", "--wait"]), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             mock.patch.object(workers, "start_claude_worker", return_value={"ok": True}) as claude, \
             mock.patch.object(workers, "start_agy_worker") as agy, \
             redirect_stdout(io.StringIO()):
            workers.main()
            agy.assert_not_called()
            claude.assert_not_called()
            codex.assert_called_once()
            self.assertEqual(codex.call_args.kwargs.get("pool"), "codex")


    def test_is_motion_or_ui_task(self):
        positive = [
            "모션그래픽 효과 추가", "부드러운 애니메이션 구현", "frontend ui 스타일링",
            "three.js 3D 렌더링", "canvas 인터랙션 최적화", "motion physics 개선",
            "tailwind css 레이아웃 수정", "svg 아이콘 애니메이션", "웹 프론트엔드 작업",
        ]
        for spec in positive:
            with self.subTest(spec=spec):
                self.assertTrue(workers.is_motion_or_ui_task(spec))

        negative = ["백엔드 DB 쿼리 튜닝", "단위 테스트 실행", "리팩토링", "", None]
        for spec in negative:
            with self.subTest(spec=spec):
                self.assertFalse(workers.is_motion_or_ui_task(spec))

    def test_auto_routing_motion_ui_to_claude(self):
        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "모션그래픽 인터랙션 추가", "--pool", "auto", "--wait"]), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             mock.patch.object(workers, "start_claude_worker", return_value={"ok": True}) as claude, \
             mock.patch.object(workers, "start_agy_worker") as agy, \
             redirect_stdout(io.StringIO()):
            workers.main()
            agy.assert_not_called()
            codex.assert_not_called()
            claude.assert_called_once()
            self.assertEqual(claude.call_args.args[0], "claude")

    def test_medium_difficulty_hierarchy_prefers_astra(self):
        routing = {
            "pool": "codex",
            "candidates": [
                {"pool": "codex", "status": "available", "remaining_percent": 80.0, "weekly_remaining": 60.0},
                {"pool": "claude", "status": "available", "remaining_percent": 80.0, "weekly_remaining": 65.0},
            ],
        }
        with mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(usage, "select_best_pool", return_value=routing):
            pool, _ = workers.choose_auto_pool("medium")
            self.assertEqual(pool, "codex-astra")

    def test_medium_difficulty_hierarchy_prefers_claude_when_headroom_large(self):
        routing = {
            "pool": "codex",
            "candidates": [
                {"pool": "codex", "status": "available", "remaining_percent": 50.0, "weekly_remaining": 30.0},
                {"pool": "claude", "status": "available", "remaining_percent": 80.0, "weekly_remaining": 50.0},  # 50 > 30 + 12
            ],
        }
        with mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(usage, "select_best_pool", return_value=routing):
            pool, _ = workers.choose_auto_pool("medium")
            self.assertEqual(pool, "claude")


    def test_depth_2_blocks_further_delegation(self):
        with mock.patch.dict(os.environ, {"MAESTRO_WORKER_DEPTH": "2", "MAESTRO_WORKER_POOL": "claude"}):
            with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "do leaf work", "--pool", "claude"]), \
                 redirect_stdout(io.StringIO()) as out:
                workers.main()
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertIn("Max worker orchestration depth (2) reached", res["error"])

    def test_depth_1_claude_locks_to_claude_and_clamps_effort_tier(self):
        with mock.patch.dict(os.environ, {"MAESTRO_WORKER_DEPTH": "1", "MAESTRO_WORKER_POOL": "claude"}):
            with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "split work", "--pool", "codex", "--effort", "ultra", "--tier", "fast", "--wait"]), \
                 mock.patch.object(usage, "collect_usage", return_value={}), \
                 mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
                 mock.patch.object(workers, "start_claude_worker", return_value={"ok": True}) as claude, \
                 mock.patch.object(workers, "start_codex_worker") as codex, \
                 redirect_stdout(io.StringIO()):
                workers.main()
                codex.assert_not_called()
                claude.assert_called_once()
                # target_pool is locked to claude, effort clamped to high, tier clamped to default
                self.assertEqual(claude.call_args.args[0], "claude")
                self.assertEqual(claude.call_args.args[3], "high")
                self.assertEqual(claude.call_args.kwargs.get("tier"), "default")

    def test_depth_1_codex_locks_to_codex_and_clamps_effort_tier(self):
        with mock.patch.dict(os.environ, {"MAESTRO_WORKER_DEPTH": "1", "MAESTRO_WORKER_POOL": "codex"}):
            with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--spec", "split codex work", "--pool", "claude", "--effort", "ultra", "--tier", "priority", "--wait"]), \
                 mock.patch.object(usage, "collect_usage", return_value={}), \
                 mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
                 mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
                 mock.patch.object(workers, "start_claude_worker") as claude, \
                 redirect_stdout(io.StringIO()):
                workers.main()
                claude.assert_not_called()
                codex.assert_called_once()
                # target_pool is locked to codex, effort clamped to high, tier clamped to default
                self.assertEqual(codex.call_args.kwargs.get("pool"), "codex")
                self.assertEqual(codex.call_args.args[2], "high")
                self.assertEqual(codex.call_args.args[3], "default")

    def test_depth_env_forwarding_in_claude_and_codex(self):
        with mock.patch.dict(os.environ, {"MAESTRO_WORKER_DEPTH": "0"}, clear=False):
            with mock.patch("subprocess.run") as sub_run, \
                 mock.patch("subprocess.Popen") as sub_popen:
                sub_run.return_value = mock.Mock(returncode=0, stdout="done", stderr="")
                res_claude = workers.start_claude_worker("claude", "test spec", "/tmp", effort="high", wait=True, timeout=1800)
                self.assertEqual(res_claude["depth"], 1)
                env_passed_claude = sub_run.call_args.kwargs["env"]
                self.assertEqual(env_passed_claude["MAESTRO_WORKER_DEPTH"], "1")
                self.assertEqual(env_passed_claude["MAESTRO_WORKER_POOL"], "claude")

                res_codex = workers.start_codex_worker("test spec", "/tmp", effort="high", tier="default", timeout=1800)
                env_passed_codex = sub_run.call_args.kwargs["env"]
                self.assertEqual(env_passed_codex["MAESTRO_WORKER_DEPTH"], "1")
                self.assertEqual(env_passed_codex["MAESTRO_WORKER_POOL"], "codex")

    def test_codex_luna_pool_dispatch(self):
        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--pool", "codex-luna", "--spec", "fix small typo", "--wait"]), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             redirect_stdout(io.StringIO()):
            workers.main()
            codex.assert_called_once()
            self.assertEqual(codex.call_args.kwargs.get("pool"), "codex-luna")
            self.assertEqual(codex.call_args.kwargs.get("model"), "gpt-6-luna")
            self.assertEqual(codex.call_args.args[2], "low")
            self.assertEqual(codex.call_args.args[3], "default")

    def test_codex_luna_effort_and_tier_clamping(self):
        model, effort, tier = workers.resolve_codex_settings("codex-luna", None, "ultra", "ultrafast", urgent=True)
        self.assertEqual(model, "gpt-6-luna")
        self.assertEqual(effort, "high")
        self.assertEqual(tier, "priority")

    def test_argon_protection_under_50_percent(self):
        usage_data = {
            "antigravity_gemini": {
                "remaining_percent": 60.0,
                "weekly": {"remaining_percent": 45.0, "status": "active"},
                "status": "active"
            },
            "claude_local": {
                "remaining_percent": 80.0,
                "weekly": {"remaining_percent": 70.0, "status": "active"},
                "status": "available"
            },
            "codex": {
                "remaining_percent": 80.0,
                "weekly": {"remaining_percent": 65.0, "status": "active"},
                "status": "available"
            }
        }
        res = usage.select_best_pool(usage_data, difficulty="high")
        # Claude should win because gemini 45% gets 0.35 multiplier -> 15.75%
        self.assertEqual(res["pool"], "claude")

    def test_diamond_title_prefix_and_ultrathink_placement(self):
        with mock.patch("subprocess.run") as sub_run:
            sub_run.return_value = mock.Mock(returncode=0, stdout="done", stderr="")
            workers.start_codex_worker("implement feature", "/tmp", "ultra", "default", 1800, max_capability=True)
            cmd = sub_run.call_args.args[0]
            prompt = cmd[-1]
            lines = prompt.splitlines()
            self.assertTrue(lines[0].startswith("✦ implement feature"))
            self.assertEqual(lines[2], "ultrathink")

    def test_wait_defaults_to_true_and_supports_no_wait(self):
        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--pool", "codex", "--spec", "default wait test"]), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             redirect_stdout(io.StringIO()):
            workers.main()
            self.assertTrue(codex.call_args.kwargs.get("wait"))

        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--pool", "codex", "--spec", "no wait test", "--no-wait"]), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             redirect_stdout(io.StringIO()):
            workers.main()
            self.assertFalse(codex.call_args.kwargs.get("wait"))

    def test_spec_from_file_and_stdin(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write("Spec from temporary file")
            tf_name = tf.name

        try:
            with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--pool", "codex", "--spec-file", tf_name]), \
                 mock.patch.object(usage, "collect_usage", return_value={}), \
                 mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
                 mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
                 redirect_stdout(io.StringIO()):
                workers.main()
                self.assertEqual(codex.call_args.args[0], "Spec from temporary file")
        finally:
            os.unlink(tf_name)

        with mock.patch.object(workers.sys, "argv", ["maestro", "worker-start", "--pool", "codex"]), \
             mock.patch.object(workers.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(workers.sys.stdin, "read", return_value="Spec from heredoc stdin"), \
             mock.patch.object(usage, "collect_usage", return_value={}), \
             mock.patch.object(workers, "is_pool_exhausted", return_value=(False, "")), \
             mock.patch.object(workers, "start_codex_worker", return_value={"ok": True}) as codex, \
             redirect_stdout(io.StringIO()):
            workers.main()
            self.assertEqual(codex.call_args.args[0], "Spec from heredoc stdin")

    def test_hook_backtick_warning(self):
        hook_payload = json.dumps({
            "event": "PreToolUse",
            "conversationId": "test-conv",
            "toolCall": {
                "name": "run_command",
                "arguments": {
                    "CommandLine": 'maestro worker-start --pool codex --spec "`ssh orin` echo test"'
                }
            }
        })
        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch.dict(os.environ, {"MAESTRO_WORKER_ACTIVE": ""}), \
             mock.patch.object(maestro_harness_hook.sys, "argv", ["maestro_harness_hook.py"]), \
             mock.patch.object(maestro_harness_hook.sys.stdin, "read", return_value=hook_payload), \
             mock.patch.object(maestro_harness_hook.sys, "stderr", stderr_buf), \
             mock.patch.object(maestro_harness_hook.sys, "stdout", stdout_buf):
            ret = maestro_harness_hook.main()
            self.assertEqual(ret, 0)
            self.assertIn("백틱(`)", stderr_buf.getvalue())
            self.assertIn('"decision":"allow"', stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()

