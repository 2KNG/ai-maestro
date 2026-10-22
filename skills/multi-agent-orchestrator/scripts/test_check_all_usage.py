"""Quota routing checks with synthetic snapshots; no accounts or sessions used."""
import copy
from contextlib import ExitStack
import datetime
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import check_all_usage as usage


def window(remaining):
    return {"remaining_percent": remaining, "status": "활성"}


def snapshot(ag_claude=40, gemini=40, codex=40, claude=40):
    return {
        "antigravity": {"pools": {
            "Claude & GPT-OSS Pool (Antigravity)": {
                "five_hour": window(ag_claude),
                "models": ["Claude Sonnet 5.5 (Low)", "Claude Opus 5.5 (High)"],
            },
            "Gemini Pool (Argon / Flash)": {
                "five_hour": window(gemini),
                "models": ["Gemini 4 Argon (High)", "Gemini 3.8 Flash (Low)"],
            },
        }},
        "codex_local": {"plan": "promax", "weekly": window(codex),
                        "service_tiers": ["priority", "ultrafast"], "stale": False},
        "claude_local": {"subscription": "team", "authenticated": True,
                         "quota_available": True, "weekly": window(claude)},
    }


def candidates(result):
    return {item["pool"]: item for item in result["candidates"]}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        # Keep tie-breaking independent of live/cached benchmark data: pin the router's
        # default pool preference (codex > antigravity_claude > antigravity_gemini > claude).
        patcher = mock.patch.dict(sys.modules, {"benchmark_sync": mock.Mock(
            get_daily_benchmark_and_models=mock.Mock(return_value={}))})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_pool_can_win_when_it_has_the_most_capacity(self):
        for index, pool in enumerate(("antigravity_claude", "antigravity_gemini", "codex", "claude")):
            with self.subTest(pool=pool):
                capacities = [10, 10, 10, 10]
                capacities[index] = 90
                result = usage.select_best_pool(snapshot(*capacities))
                self.assertEqual(result["pool"], pool)
                self.assertEqual(len(result["candidates"]), 4)

    def test_codex_and_antigravity_rebalance_when_capacity_changes(self):
        self.assertEqual(usage.select_best_pool(snapshot(85, 50, 60, 0))["pool"], "antigravity_claude")
        self.assertEqual(usage.select_best_pool(snapshot(60, 50, 85, 0))["pool"], "codex")
        self.assertEqual(usage.select_best_pool(snapshot(60, 40, 60, 0))["pool"], "codex")

    def test_weekly_guardrail_limits_codex_and_claude_only_near_exhaustion(self):
        # Normal zone (7d > 10%): blend 5h and 7d remaining quotas (Codex 0.25*5h + 0.75*7d, Claude 0.45*5h + 0.55*7d).
        data = snapshot(50, 40, 20, 15)
        data["codex_local"]["five_hour"] = window(95)
        data["claude_local"]["five_hour"] = window(99)
        result = usage.select_best_pool(data)
        self.assertEqual(result["pool"], "claude")
        self.assertEqual(candidates(result)["codex"]["remaining_percent"], 38.75)
        self.assertEqual(candidates(result)["claude"]["remaining_percent"], 52.8)
        # Weekly Conserve zone (2% < 7d <= 10%): score is min(5h, 7d * 3.5), so antigravity wins.
        data = snapshot(50, 40, 8, 5)
        data["codex_local"]["five_hour"] = window(95)
        data["claude_local"]["five_hour"] = window(99)
        result = usage.select_best_pool(data)
        self.assertEqual(result["pool"], "antigravity_claude")
        self.assertEqual(candidates(result)["codex"]["remaining_percent"], 28)
        self.assertEqual(candidates(result)["claude"]["remaining_percent"], 17.5)
        self.assertEqual(candidates(result)["codex"]["routing_mode"], "weekly_conserve")
        data["codex_local"]["five_hour"] = window(0)
        self.assertEqual(candidates(usage.select_best_pool(data))["codex"]["status"], "exhausted")

    def test_stale_error_unavailable_or_missing_capacity_never_wins(self):
        changes = [
            {"stale": True}, {"error": "offline"}, {"quota_available": False},
            {"weekly": None}, {"weekly": {}},
            {"weekly": {"remaining_percent": 100, "error": "offline"}},
            {"weekly": {"remaining_percent": 100, "status": "정보 없음"}},
            {"model_available": False}, {"service_tiers": []},
        ]
        for update in changes:
            with self.subTest(update=update):
                data = snapshot(50, 10, 100, 0)
                data["codex_local"].update(update)
                result = usage.select_best_pool(data)
                self.assertEqual(result["pool"], "antigravity_claude")
                self.assertNotEqual(candidates(result)["codex"]["status"], "available")
        data = snapshot(0, 0, 0, 100)
        data["claude_local"]["authenticated"] = False
        self.assertIsNone(usage.select_best_pool(data)["pool"])

    def test_parent_antigravity_error_and_stale_hide_nested_quotas(self):
        for update in ({"error": "offline"}, {"stale": True}):
            with self.subTest(update=update):
                data = snapshot(100, 100, 40, 0)
                data["antigravity"].update(update)
                result = usage.select_best_pool(data)
                self.assertEqual(result["pool"], "codex")
                self.assertEqual(candidates(result)["antigravity_gemini"]["status"], "unknown")

    def test_malformed_percentages_are_unknown(self):
        for invalid in (-1, 101, "bad", "NaN", float("nan"), float("inf"), [], {}, True):
            with self.subTest(invalid=invalid):
                data = snapshot(50, 10, invalid, 0)
                result = usage.select_best_pool(data)
                self.assertEqual(result["pool"], "antigravity_claude")
                self.assertEqual(candidates(result)["codex"]["status"], "unknown")

    def test_used_percent_fallback_and_numeric_strings(self):
        data = snapshot(60, 10, 0, 0)
        data["codex_local"]["weekly"] = {"used_percent": "20"}
        self.assertEqual(usage.select_best_pool(data)["remaining_percent"], 80)
        data["codex_local"]["weekly"] = {"remaining_percent": "75"}
        self.assertEqual(usage.select_best_pool(data)["pool"], "codex")

    def test_task_fit_and_models_change_with_difficulty(self):
        data = snapshot(80, 80, 80, 70)
        high = usage.select_best_pool(data, "high")
        low = usage.select_best_pool(data, "low")
        self.assertEqual(high["pool"], "codex")
        self.assertEqual(low["pool"], "antigravity_gemini")
        self.assertIn("Flash", low["model"])
        self.assertIn("Opus", candidates(high)["antigravity_claude"]["model"])
        self.assertIn("Sonnet", candidates(low)["antigravity_claude"]["model"])

    def test_codex_tiers_share_one_candidate_and_follow_threshold(self):
        for difficulty, capacity, tiers, urgent, expected in (
            ("high", 50, ["priority", "ultrafast"], True, "ultrafast"),
            ("high", 50, ["priority", "ultrafast"], False, "default"),
            ("high", 49.99, ["priority", "ultrafast"], True, "priority"),
            ("medium", 90, ["priority", "ultrafast"], True, "priority"),
            ("low", 90, ["priority", "ultrafast"], True, "priority"),
            ("high", 90, ["priority"], True, "priority"),
        ):
            with self.subTest(difficulty=difficulty, capacity=capacity, tiers=tiers, urgent=urgent):
                data = snapshot(0, 0, capacity, 0)
                data["codex_local"]["service_tiers"] = tiers
                result = usage.select_best_pool(data, difficulty, urgent=urgent)
                self.assertEqual(result["service_tier"], expected)
                codex_entries = [entry for entry in result["candidates"] if entry["pool"] == "codex"]
                self.assertEqual(len(codex_entries), 1)
                self.assertTrue(codex_entries[0]["shared_quota"])

    def test_empty_and_exhausted_inputs_have_no_recommendation(self):
        for data in ({}, snapshot(0, 0, 0, 0)):
            result = usage.select_best_pool(data)
            for key in ("pool", "provider", "model", "service_tier", "remaining_percent"):
                self.assertIsNone(result[key])

    def test_does_not_mutate_input_and_rejects_unknown_difficulty(self):
        data = snapshot()
        original = copy.deepcopy(data)
        usage.select_best_pool(data)
        self.assertEqual(data, original)
        with self.assertRaises(ValueError):
            usage.select_best_pool(data, "extreme")


class CollectorTests(unittest.TestCase):
    def test_codex_windows_classify_both_orderings_and_units(self):
        for key, five_duration, week_duration in (("limit_window_seconds", 18000, 604800), ("window_minutes", 300, 10080)):
            five, week = {key: five_duration, "used_percent": 10}, {key: week_duration, "used_percent": 20}
            with self.subTest(key=key):
                self.assertEqual(usage._codex_windows(five, week), (week, five))
                self.assertEqual(usage._codex_windows(week, five), (week, five))
                self.assertEqual(usage._codex_windows(week, None), (week, None))
                self.assertEqual(usage._codex_windows(five, None), (None, five))
        self.assertEqual(usage._codex_windows(None, None), (None, None))
        with self.assertRaises(ValueError):
            usage._codex_windows({"limit_window_seconds": 60, "used_percent": 10}, None)

    def test_collect_places_recommendation_near_timestamp_and_skips_trees(self):
        data = snapshot(50, 10, 80, 0)
        with mock.patch.object(usage, "get_antigravity_usage", return_value=data["antigravity"]), mock.patch.object(
            usage, "get_codex_usage", return_value=data["codex_local"]
        ), mock.patch.object(usage, "get_claude_status", return_value=data["claude_local"]), mock.patch.object(
            usage, "get_antigravity_agent_tree"
        ) as agy_tree, mock.patch.object(usage, "get_codex_agent_tree") as codex_tree:
            report = usage.collect_usage(include_agent_trees=False)
        self.assertEqual(list(report)[:2], ["timestamp_kst", "recommended_routing"])
        self.assertEqual(report["recommended_routing"]["pool"], "codex")
        self.assertNotIn("agent_trees", report)
        agy_tree.assert_not_called()
        codex_tree.assert_not_called()

    def test_live_codex_collector_uses_real_window_durations(self):
        payload = {"plan_type": "promax", "rate_limit": {
            "primary_window": {"used_percent": 25, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 80, "limit_window_seconds": 604800},
        }}
        response = io.BytesIO(json.dumps(payload).encode())
        with mock.patch.object(usage.os.path, "exists", return_value=True), mock.patch(
            "builtins.open", mock.mock_open(read_data='{"access_token": "test-only"}')
        ), mock.patch.object(usage.urllib.request, "urlopen", return_value=response), mock.patch.object(
            usage, "_codex_model_metadata", return_value={"service_tiers": ["priority"]}
        ), mock.patch.object(usage.glob, "glob") as fallback:
            result = usage.get_codex_usage()
        self.assertEqual(result["five_hour"]["remaining_percent"], 75)
        self.assertEqual(result["weekly"]["remaining_percent"], 20)
        self.assertFalse(result["stale"])
        fallback.assert_not_called()

    def test_codex_fallback_marks_old_and_future_snapshots_stale(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        for delta, stale in ((-60, False), (-3600, True), (3600, True)):
            with self.subTest(delta=delta):
                event = {"timestamp": (now + datetime.timedelta(seconds=delta)).isoformat(), "payload": {
                    "rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080}}}}
                with mock.patch.object(usage.os.path, "exists", return_value=False), mock.patch.object(
                    usage.glob, "glob", return_value=["/fake/rollout.jsonl"]
                ), mock.patch.object(usage.os.path, "getmtime", return_value=0), mock.patch(
                    "builtins.open", side_effect=lambda path, mode="r", **kw: io.BytesIO((json.dumps(event) + "\n").encode()) if "b" in mode else io.StringIO()
                ), mock.patch.object(usage, "_codex_model_metadata", return_value={"service_tiers": ["priority"]}):
                    result = usage.get_codex_usage()
                self.assertEqual(result["stale"], stale)
                self.assertEqual(result["weekly"]["remaining_percent"], 70)

    def test_missing_codex_usage_percentage_does_not_create_capacity(self):
        payload = {"rate_limit": {"primary_window": {"limit_window_seconds": 604800}}}
        with mock.patch.object(usage.os.path, "exists", return_value=True), mock.patch(
            "builtins.open", mock.mock_open(read_data='{"access_token": "test-only"}')
        ), mock.patch.object(usage.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), mock.patch.object(
            usage.glob, "glob", return_value=[]
        ), mock.patch.object(usage, "_codex_model_metadata", return_value={"service_tiers": ["priority"]}):
            result = usage.get_codex_usage()
        self.assertIsNone(usage._routing_remaining(result))

    def test_claude_missing_usage_is_unknown_and_stale_cache_is_not_capacity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {
                "~/.claude/.credentials.json": Path(temp_dir) / "credentials.json",
                "~/.claude/history.jsonl": Path(temp_dir) / "history.jsonl",
                "~/.gemini/antigravity/claude_usage_cache.json": Path(temp_dir) / "cache.json",
            }
            paths["~/.claude/.credentials.json"].write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "test-only", "subscriptionType": "team"},
            }))
            with mock.patch.object(usage.os.path, "expanduser", side_effect=lambda path: str(paths[path])), mock.patch.object(
                usage.urllib.request, "urlopen", side_effect=OSError("offline")
            ):
                missing = usage.get_claude_status()
                self.assertTrue(missing["authenticated"])
                self.assertFalse(missing["quota_available"])
                self.assertIsNone(usage._routing_remaining(missing))

                cache = paths["~/.gemini/antigravity/claude_usage_cache.json"]
                cache.write_text(json.dumps({"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}}))
                timestamp = datetime.datetime.now().timestamp() - 3600
                os.utime(cache, (timestamp, timestamp))
                stale = usage.get_claude_status()
                self.assertTrue(stale["quota_available"])
                self.assertTrue(stale["stale"])
                self.assertEqual(stale["source"], "cache")
                self.assertIsNone(usage._routing_remaining(stale))

                cache.touch()
                fresh = usage.get_claude_status()
                self.assertFalse(fresh["stale"])
                # Weekly headroom (80%) is blended with 5h window (90%): 0.45 * 90 + 0.55 * 80 = 84.5%.
                self.assertEqual(usage._routing_remaining(fresh), 84.5)


class AntigravityCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.tree_path = directory / "tree.json"
        self.meta_path = directory / "metadata.json"
        self.stack.enter_context(mock.patch.object(usage, "AGY_TREE_CACHE_PATH", str(self.tree_path)))
        self.stack.enter_context(mock.patch.object(usage, "AGY_METADATA_CACHE_PATH", str(self.meta_path)))
        self.stack.enter_context(mock.patch.object(usage, "_agy_tree_cache", None))
        self.clock = self.stack.enter_context(mock.patch.object(usage.time, "time", return_value=1000.0))

    def test_tree_caches_entire_result_in_memory_and_on_disk(self):
        tree = [{"conversation_id": "root", "children": [{"conversation_id": "child"}]}]
        with mock.patch.object(usage, "_fetch_antigravity_agent_tree", return_value=tree) as fetch:
            self.assertEqual(usage.get_antigravity_agent_tree(), tree)
            self.assertEqual(json.loads(self.tree_path.read_text()), {"cached_at": 1000.0, "tree": tree})
            self.clock.return_value = 1014.999
            with mock.patch("builtins.open", side_effect=AssertionError("warm hit read disk")):
                self.assertEqual(usage.get_antigravity_agent_tree(), tree)
            # Simulate a new process reading the shared disk cache.
            usage._agy_tree_cache = None
            self.assertEqual(usage.get_antigravity_agent_tree(), tree)
            fetch.assert_called_once_with()

    def test_tree_expires_at_fifteen_seconds(self):
        with mock.patch.object(usage, "_fetch_antigravity_agent_tree", side_effect=[[{"version": 1}], [{"version": 2}]]) as fetch:
            self.assertEqual(usage.get_antigravity_agent_tree(), [{"version": 1}])
            self.clock.return_value = 1015.0
            self.assertEqual(usage.get_antigravity_agent_tree(), [{"version": 2}])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads(self.tree_path.read_text())["cached_at"], 1015.0)

    def test_corrupt_stale_and_future_tree_cache_are_rebuilt(self):
        for content in (
            "{broken", "null", "[]", '{"cached_at": "now", "tree": []}',
            '{"cached_at": 1000, "tree": {}}', '{"cached_at": 1000, "tree": [null]}',
            '{"cached_at": 985, "tree": []}', '{"cached_at": 1001, "tree": []}',
            '{"cached_at": NaN, "tree": []}',
        ):
            with self.subTest(content=content):
                usage._agy_tree_cache = None
                self.tree_path.write_text(content)
                with mock.patch.object(usage, "_fetch_antigravity_agent_tree", return_value=[{"fresh": True}]) as fetch:
                    self.assertEqual(usage.get_antigravity_agent_tree(), [{"fresh": True}])
                    fetch.assert_called_once_with()

    def test_empty_tree_is_cached_and_cache_write_failure_is_safe(self):
        with mock.patch.object(usage, "_fetch_antigravity_agent_tree", return_value=[]) as fetch, mock.patch.object(
            usage.os, "replace", side_effect=OSError("read-only")
        ):
            self.assertEqual(usage.get_antigravity_agent_tree(), [])
            self.assertEqual(usage.get_antigravity_agent_tree(), [])
            fetch.assert_called_once_with()
        self.assertEqual(list(self.tree_path.parent.iterdir()), [])

    def test_running_metadata_uses_ttl_across_modified_times(self):
        summary = {"status": "CASCADE_RUN_STATUS_RUNNING", "lastModifiedTime": "first"}
        metadata_calls = []

        def response(request, **kwargs):
            if request.full_url.endswith("GetAllCascadeTrajectories"):
                data = {"trajectorySummaries": {"test-id": summary}}
            else:
                self.assertTrue(request.full_url.endswith("GetCascadeTrajectoryGeneratorMetadata"))
                metadata_calls.append(self.clock.return_value)
                data = {"generatorMetadata": [{"chatModel": {"usage": {
                    "inputTokens": len(metadata_calls) * 100, "model": "test-model",
                }}}]}
            return io.BytesIO(json.dumps(data).encode())

        with mock.patch.object(usage, "discover_ls_credentials", return_value=("localhost:123", "test-only")), mock.patch.object(
            usage, "_load_agy_spawn_registry", return_value={}
        ), mock.patch.object(usage, "get_last_selected_agy_model", return_value=None), mock.patch.object(
            usage.urllib.request, "urlopen", side_effect=response
        ):
            self.assertEqual(usage._fetch_antigravity_agent_tree()[0]["context_usage"]["used_tokens"], 100)
            cached = json.loads(self.meta_path.read_text())["test-id"]
            self.assertEqual(cached["cached_at"], 1000.0)
            self.assertEqual(cached["last_mod"], "first")
            summary["lastModifiedTime"] = "second"
            self.clock.return_value = 1044.999
            self.assertEqual(usage._fetch_antigravity_agent_tree()[0]["context_usage"]["used_tokens"], 100)
            self.clock.return_value = 1045.0
            self.assertEqual(usage._fetch_antigravity_agent_tree()[0]["context_usage"]["used_tokens"], 200)
            # An unchanged trajectory remains reusable even after the TTL.
            self.clock.return_value = 2000.0
            self.assertEqual(usage._fetch_antigravity_agent_tree()[0]["context_usage"]["used_tokens"], 200)
            self.assertEqual(metadata_calls, [1000.0, 1045.0])

    def test_empty_metadata_is_cached_for_running_and_idle_trajectories(self):
        summaries = {key: {"status": status, "lastModifiedTime": "first"} for key, status in (
            ("running", "CASCADE_RUN_STATUS_RUNNING"), ("idle", "CASCADE_RUN_STATUS_IDLE"),
        )}
        metadata_calls = []

        def response(request, **kwargs):
            if request.full_url.endswith("GetAllCascadeTrajectories"):
                data = {"trajectorySummaries": summaries}
            else:
                metadata_calls.append(request.data)
                data = {"generatorMetadata": []}
            return io.BytesIO(json.dumps(data).encode())

        with mock.patch.object(usage, "discover_ls_credentials", return_value=("localhost:123", "test-only")), mock.patch.object(
            usage, "_load_agy_spawn_registry", return_value={}
        ), mock.patch.object(usage, "get_last_selected_agy_model", return_value=None), mock.patch.object(
            usage.urllib.request, "urlopen", side_effect=response
        ):
            self.meta_path.write_text("[]")
            self.assertEqual(len(usage._fetch_antigravity_agent_tree()), 2)
            for summary in summaries.values():
                summary["lastModifiedTime"] = "second"
            self.clock.return_value = 1044.0
            self.assertEqual(len(usage._fetch_antigravity_agent_tree()), 2)
            self.assertEqual(len(metadata_calls), 2)
            self.assertEqual(set(json.loads(self.meta_path.read_text())), {"running", "idle"})


class AntigravityOriginTests(unittest.TestCase):
    def test_prefix_accepts_plain_and_historical_decorated_labels(self):
        for title in ("[AGY] worker", "[AGY]", "\U0001fa90 [AGY] worker"):
            with self.subTest(title=title):
                self.assertTrue(usage._has_agy_prefix(title))
        for title in (None, "worker", "worker [AGY]", "[AGY]worker"):
            with self.subTest(title=title):
                self.assertFalse(usage._has_agy_prefix(title))

    def test_codex_origin_uses_name_title_or_registry(self):
        parents = [
            ("name", "[AGY] worker", "title", "exec"),
            ("title", "worker", "[AGY] task", "exec"),
            ("registered", "worker", "task", "exec"),
            ("ordinary", "worker", "task", "cli"),
            ("desktop", "[AGY] worker", "[AGY] task", "vscode"),
        ]
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value
        cursor.execute.return_value.fetchall.side_effect = [
            [(cid, name, title, "/fake", None, "test-model", "low", 1000, 1000, source, 0)
             for cid, name, title, source in parents],
            [], [], [], [], [],
        ]
        with mock.patch.object(usage.os.path, "exists", return_value=True), mock.patch.object(
            usage.sqlite3, "connect", return_value=connection
        ), mock.patch.object(usage, "_running_codex_cwds_and_snippets", return_value=(set(), [])), mock.patch.object(
            usage, "_load_agy_spawn_registry", return_value={
                "registered": {"provider": "codex"}, "desktop": {"provider": "codex"},
            }
        ), mock.patch.object(usage, "parse_codex_rollout", return_value={"token_count": {}, "turn_context": {}}):
            result = usage.get_codex_agent_tree("/fake/db")
        self.assertEqual({node["thread_id"]: node["agy_origin"] for node in result}, {
            "name": True, "title": True, "registered": True, "ordinary": False, "desktop": False,
        })
        self.assertEqual({node["thread_id"]: node["source"] for node in result},
                         {cid: source for cid, _, _, source in parents})


class ProcessResourceTests(unittest.TestCase):
    """Synthetic /proc trees; nvidia-smi is replaced by a fixed PID map."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        for name, value in (("_gpu_process_memory", {101: 512.0}), ("_gpu_total_memory", 8192.0), ("_cpu_count", 4)):
            patcher = mock.patch.object(usage, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        Path(self.root, "uptime").write_text("1000.00 0.00\n")
        Path(self.root, "meminfo").write_text("MemTotal:       16777216 kB\nMemFree:  1 kB\n")  # 16 GiB

    def proc(self, pid, ppid, *, cpu_sec=0, rss_pages=256, cid="", step=None, io=(0, 0), comm="python3"):
        d = Path(self.root, str(pid))
        d.mkdir(exist_ok=True)
        tck = usage._CLK_TCK
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(int(cpu_sec * tck)), "0"] + ["0"] * 6 + [str(500 * tck), "0", str(rss_pages)]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + " 0 0\n")
        env = b"PATH=/bin\0"
        if cid:
            env += f"ANTIGRAVITY_CONVERSATION_ID={cid}\0".encode()
        if step is not None:
            env += ('ANTIGRAVITY_SOURCE_METADATA={"tool":{"stepIndex":%d}}\0' % step).encode()
        (d / "environ").write_bytes(env)
        (d / "io").write_text(f"rchar: {io[0]}\nwchar: {io[1]}\n")

    def build(self, trees, workers=()):
        return usage.build_process_resources(trees, proc_root=self.root, live_workers=list(workers))

    def test_trees_are_attributed_by_environment_and_rolled_up_once(self):
        self.proc(100, 1, cpu_sec=50, cid="conv-a", step=42, comm="bash")
        self.proc(101, 100, cid="conv-a", step=42)
        self.proc(200, 100, cid="conv-a", step=42, comm="claude")
        self.proc(201, 200, rss_pages=512, comm="pytest")  # child without the variable still belongs to its tree
        self.proc(300, 1, rss_pages=9999)  # unrelated
        self.proc(400, 1, cid="conv-b", step=7)
        trees = {"antigravity": [{"conversation_id": "conv-a",
                                  "background_tasks": [{"short_id": "task-42"}],
                                  "children": [{"conversation_id": "conv-b", "children": []}]}],
                 "claude": [{"session_id": "sess-1"}], "codex": []}
        pr = self.build(trees, [{"pid": 200, "provider": "claude", "session_id": "sess-1"}])

        root = trees["antigravity"][0]
        self.assertEqual(pr["conversations"]["conv-a"]["pids"], [100, 101, 200, 201])
        # The claude worker tree (200, 201) belongs to its SUB row, not to the job that launched it.
        self.assertEqual(pr["tasks"]["conv-a/task-42"]["pids"], [100, 101])
        self.assertEqual(pr["tasks"]["conv-a/task-42"]["worker_pids"], [200, 201])
        self.assertEqual(root["background_tasks"][0]["resources"]["pids"], [100, 101])
        self.assertEqual(root["children"][0]["resources"]["pids"], [400])
        self.assertEqual(root["rollup_resources"]["pids"], [100, 101, 200, 201, 400])
        self.assertEqual(root["rollup_resources"]["active_procs"], 5)
        self.assertEqual(trees["claude"][0]["resources"]["pids"], [200, 201])
        self.assertEqual(pr["workers"]["sess-1"]["proc_count"], 2)
        self.assertNotIn("300", pr["procs"])
        page_mb = usage._PAGE_KB / 1024
        self.assertAlmostEqual(root["rollup_resources"]["ram_mb"], round((256 * 4 + 512) * page_mb, 1))
        self.assertEqual(root["rollup_resources"]["gpu_mb"], 512.0)
        self.assertEqual(root["rollup_resources"]["gpu_pct"], 6.25)  # 512 of 8192 MiB VRAM
        self.assertAlmostEqual(root["rollup_resources"]["ram_pct"],
                               round((256 * 4 + 512) * page_mb / 16384 * 100, 2))
        self.assertEqual((pr["cpu_count"], pr["mem_total_mb"], pr["gpu_total_mb"]), (4, 16384.0, 8192.0))
        # First pass: lifetime average, 50 CPU seconds over a 500 s old process = 10% of one core, 2.5% of 4.
        self.assertAlmostEqual(pr["conversations"]["conv-a"]["cpu_core_pct"], 10.0)
        self.assertAlmostEqual(pr["conversations"]["conv-a"]["cpu_pct"], 2.5)

    def test_second_pass_uses_cpu_and_io_deltas(self):
        self.proc(100, 1, cpu_sec=50, cid="conv-a", step=1, io=(10 * 1024, 0))
        with mock.patch.object(usage.time, "monotonic", return_value=100.0):
            self.build({})
        self.proc(100, 1, cpu_sec=51, cid="conv-a", step=1, io=(30 * 1024, 10 * 1024))
        with mock.patch.object(usage.time, "monotonic", return_value=101.0):
            self.assertIs(usage.process_snapshot(self.root), usage.process_snapshot(self.root))  # cached
        with mock.patch.object(usage.time, "monotonic", return_value=104.0):
            pr = self.build({})
        res = pr["conversations"]["conv-a"]
        self.assertAlmostEqual(res["cpu_core_pct"], 25.0)  # 1 CPU second in 4 s
        self.assertAlmostEqual(res["cpu_pct"], 6.25)  # of 4 logical CPUs
        self.assertAlmostEqual(res["rx_kbps"], 5.0)  # 20 KiB read (rchar) in 4 s
        self.assertAlmostEqual(res["tx_kbps"], 2.5)  # 10 KiB written (wchar) in 4 s
        self.assertNotIn("io_kbps", res)

    def test_job_and_worker_partition_the_conversation_tree(self):
        # task-9 shell -> maestro worker-start -> claude -> node; task-10 is an unrelated job.
        self.proc(100, 1, cpu_sec=5, cid="conv-a", step=9, comm="bash")
        self.proc(101, 100, cpu_sec=10, cid="conv-a", step=9, comm="maestro")
        self.proc(102, 101, cpu_sec=20, cid="conv-a", step=9, comm="claude")
        self.proc(103, 102, cpu_sec=40, cid="conv-a", step=9, comm="node")
        self.proc(110, 1, cpu_sec=15, cid="conv-a", step=10, comm="pytest")
        trees = {"antigravity": [{"conversation_id": "conv-a", "children": [],
                                  "background_tasks": [{"short_id": "task-9"}, {"short_id": "task-10"}]}]}
        pr = self.build(trees, [{"pid": 101, "provider": "maestro", "registry_id": "reg-1"},
                                {"pid": 102, "provider": "claude", "session_id": "sess-1"}])
        worker, wrapper, job = pr["workers"]["sess-1"], pr["tasks"]["conv-a/task-9"], pr["tasks"]["conv-a/task-10"]
        self.assertEqual(worker["pids"], [101, 102, 103])
        self.assertEqual(wrapper["pids"], [100])
        self.assertEqual(wrapper["worker_pids"], [101, 102, 103])
        self.assertNotIn("worker_pids", job)
        self.assertFalse(set(worker["pids"]) & set(wrapper["pids"]))
        rollup = trees["antigravity"][0]["rollup_resources"]
        for field in ("cpu_pct", "cpu_core_pct", "ram_mb", "rx_kbps", "tx_kbps", "proc_count"):
            self.assertAlmostEqual(rollup[field], worker[field] + wrapper[field] + job[field], places=1)

    def test_worker_spawn_detection_ignores_heredoc_bodies(self):
        spawn = "cat <<'EOF' > /tmp/spec.md\nRun pytest\nEOF\n~/.local/bin/maestro worker-start --pool claude --spec-file /tmp/spec.md"
        self.assertTrue(usage.is_worker_spawn_command(spawn))
        self.assertTrue(usage.is_worker_spawn_command(spawn.replace("\n", "\\n")))  # escaped transcript args
        mention = "cat <<'EOF' > /tmp/notes.md\nuse maestro worker-start later\nEOF\npytest -q"
        self.assertFalse(usage.is_worker_spawn_command(mention))
        self.assertFalse(usage.is_worker_spawn_command("pytest -q"))

    def test_maestro_wrapper_and_cli_are_one_worker(self):
        self.proc(10, 1, comm="python3")
        self.proc(11, 10, comm="claude")
        self.proc(12, 11, comm="node")
        pr = self.build({}, [{"pid": 10, "provider": "maestro", "registry_id": "reg-1"},
                             {"pid": 11, "provider": "claude", "session_id": "sess-9"}])
        self.assertEqual(pr["workers"]["sess-9"]["pids"], [10, 11, 12])
        self.assertEqual(pr["workers"]["reg-1"]["pids"], [10, 11, 12])

    def test_antigravity_runtime_is_split_across_live_sessions(self):
        now = usage.datetime.datetime.now(usage.datetime.timezone.utc)
        recent, old = now.isoformat(), (now - usage.datetime.timedelta(days=30)).isoformat()
        self.proc(50, 1, cpu_sec=20, rss_pages=1000, comm="antigravity")  # Electron main window
        self.proc(51, 50, cpu_sec=60, rss_pages=2000, comm="antigravity")  # renderer
        self.proc(60, 50, cpu_sec=40, rss_pages=3000, io=(4000 * 1024, 0), comm="language_server")
        self.proc(70, 60, cpu_sec=5, cid="conv-a", step=3, comm="bash")  # run_command: the conversation's own tree
        trees = {"antigravity": [
            {"conversation_id": "conv-a", "is_running": True, "last_modified_time": recent,
             "context_usage": {"used_tokens": 100_000}, "background_tasks": [{"short_id": "task-3"}],
             "children": [{"conversation_id": "sub-a", "is_running": True, "is_subagent": True, "children": []},
                          {"conversation_id": "sub-done", "is_running": False, "is_subagent": True, "children": []}]},
            {"conversation_id": "conv-idle", "is_running": False, "last_modified_time": recent, "children": []},
            {"conversation_id": "conv-old", "is_running": False, "last_modified_time": old, "children": []},
            {"conversation_id": "conv-killed", "is_running": True, "killed": True, "children": []},
        ]}
        pr = self.build(trees)
        main, idle_main, old_main = trees["antigravity"][:3]
        runtime = pr["agy_runtime"]
        self.assertEqual(runtime["pids"], [50, 51, 60])  # the run_command shell 70 stays with conv-a
        self.assertEqual(set(runtime["shares"]), {"conv-a", "sub-a", "conv-idle"})
        self.assertNotIn("self_resources", old_main)
        self.assertNotIn("resources", main["children"][1])
        shares = [main["self_resources"], main["children"][0]["self_resources"], idle_main["self_resources"]]
        # Every share is non-zero and the shares add up to the runtime exactly.
        for field in ("cpu_core_pct", "ram_mb", "rx_kbps"):
            self.assertTrue(all(s[field] > 0 for s in shares), field)
            self.assertAlmostEqual(sum(s[field] for s in shares), runtime[field], delta=0.2)
        # CPU follows running state (1 : 1 : 0.1); RAM follows context size (2 : 1 : 1).
        self.assertAlmostEqual(main["self_resources"]["cpu_core_pct"], runtime["cpu_core_pct"] / 2.1, delta=0.1)
        self.assertAlmostEqual(idle_main["self_resources"]["cpu_core_pct"], runtime["cpu_core_pct"] * 0.1 / 2.1, delta=0.1)
        self.assertAlmostEqual(main["self_resources"]["ram_mb"], runtime["ram_mb"] / 2, delta=0.2)
        self.assertEqual((main["self_resources"]["proc_count"], main["self_resources"]["runtime_shares"]), (0, 1))
        # Main total = own runtime share + its run_command tree + the subagent's share.
        bash = pr["tasks"]["conv-a/task-3"]
        rollup = main["rollup_resources"]
        self.assertAlmostEqual(rollup["cpu_core_pct"], main["self_resources"]["cpu_core_pct"] + bash["cpu_core_pct"]
                               + main["children"][0]["self_resources"]["cpu_core_pct"], delta=0.2)
        self.assertEqual(rollup["proc_count"], 1)
        self.assertEqual(pr["procs"][str(runtime["shares"]["conv-a"])][-1], "agy-runtime")
        merged = usage.merge_resources(pr["procs"], [rollup], pr)
        self.assertAlmostEqual(merged["cpu_core_pct"], rollup["cpu_core_pct"], delta=0.1)
        self.assertNotIn("50", pr["procs"])

    def test_workspace_language_server_goes_to_its_own_workspace(self):
        now = usage.datetime.datetime.now(usage.datetime.timezone.utc).isoformat()
        self.proc(50, 1, rss_pages=1000, comm="antigravity")
        self.proc(60, 50, rss_pages=4000, comm="language_server")
        Path(self.root, "60", "cmdline").write_bytes(b"language_server_linux_x64\0--workspace_id\0"
                                                     b"file_home_u_dev_robot\0")
        trees = {"antigravity": [
            {"conversation_id": "robot", "is_running": True, "last_modified_time": now,
             "workspaces": ["file:///home/u/dev/robot"], "children": []},
            {"conversation_id": "web", "is_running": True, "last_modified_time": now,
             "workspaces": ["file:///home/u/dev/web"], "children": []},
        ]}
        self.build(trees)
        robot, web = (node["self_resources"]["ram_mb"] for node in trees["antigravity"])
        page_mb = usage._PAGE_KB / 1024
        self.assertAlmostEqual(robot, round((4000 + 500) * page_mb, 1), delta=0.2)  # own server + half the window
        self.assertAlmostEqual(web, round(500 * page_mb, 1), delta=0.2)

    def test_nodes_without_processes_get_no_resource_keys(self):
        trees = {"antigravity": [{"conversation_id": "idle", "resources": {"stale": 1}, "children": []}],
                 "claude": [{"session_id": "gone", "pid": 999999}], "codex": [{"thread_id": "t", "children": []}]}
        self.build(trees)
        self.assertNotIn("resources", trees["antigravity"][0])
        self.assertNotIn("rollup_resources", trees["antigravity"][0])
        self.assertNotIn("resources", trees["claude"][0])
        self.assertNotIn("rollup_resources", trees["codex"][0])

    def test_merge_resources_unions_pids(self):
        table = {"1": [1.0, 10.0, 0.0, 1.0, 4.0, "a"], "2": [2.0, 20.0, 5.0, 2.0, 0.0, "b"]}
        merged = usage.merge_resources(table, [{"pids": [1, 2]}, {"pids": [2]}, None],
                                       {"cpu_count": 2, "mem_total_mb": 1000.0, "gpu_total_mb": 0.0})
        self.assertEqual((merged["cpu_pct"], merged["ram_mb"], merged["gpu_mb"], merged["tx_kbps"], merged["rx_kbps"]),
                         (1.5, 30.0, 5.0, 3.0, 4.0))
        self.assertEqual((merged["cpu_core_pct"], merged["cpu_count"]), (3.0, 2))
        # Per-core rows above the thread count cap at 100% of the machine.
        busy = usage.merge_resources({"1": [350.0, 1.0, 0.0, 0.0, 0.0, "x"]}, [{"pids": [1]}], {"cpu_count": 2})
        self.assertEqual((busy["cpu_pct"], busy["cpu_core_pct"]), (100.0, 350.0))
        self.assertEqual(merged["ram_pct"], 3.0)
        self.assertNotIn("gpu_pct", merged)  # unknown VRAM total: no percentage
        self.assertEqual((merged["proc_count"], merged["active_procs"], merged["pids"]), (2, 2, [1, 2]))
        self.assertIsNone(usage.merge_resources(table, [{"pids": [7]}, None]))

    def test_orca_lead_rollup_adds_teammate_panes(self):
        # Orca opens each pane from its own shell: the teammate is outside the lead's process tree.
        self.proc(100, 1, comm="codex")
        self.proc(101, 100, comm="bash")
        self.proc(200, 1, comm="claude")
        trees = {"codex": [{"thread_id": "lead", "source": "cli", "children": []}],
                 "claude": [{"session_id": "mate", "pid": 200, "is_running": True, "orca_lead_id": "lead"}]}
        self.build(trees, [{"pid": 100, "provider": "codex", "session_id": "lead"},
                           {"pid": 200, "provider": "claude", "session_id": "mate"}])
        lead = trees["codex"][0]
        self.assertEqual(lead["self_resources"]["pids"], [100, 101])
        self.assertEqual(lead["rollup_resources"]["pids"], [100, 101, 200])
        self.assertEqual(trees["claude"][0]["rollup_resources"]["pids"], [200])

    def test_collect_usage_survives_resource_failures(self):
        with ExitStack() as stack:
            for name in ("get_antigravity_usage", "get_codex_usage", "get_claude_status",
                         "get_antigravity_agent_tree", "get_codex_agent_tree", "get_claude_agent_tree"):
                stack.enter_context(mock.patch.object(usage, name, return_value={} if "usage" in name or "status" in name else []))
            stack.enter_context(mock.patch.object(usage, "get_orca_summary", return_value={}))
            stack.enter_context(mock.patch.object(usage, "build_process_resources", side_effect=RuntimeError))
            report = usage.collect_usage()
        self.assertEqual(report["process_resources"], {"error": "RuntimeError"})



class OrcaTeammateTests(unittest.TestCase):
    """Orca panes a lead opened nest under it by terminal handle, never by shared cwd alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        usage._ORCA_HANDLE_SCAN.clear()
        self.ticks = 0

    @staticmethod
    def handle(name):
        return "term_%08x-0000-4000-8000-000000000000" % (sum(map(ord, name)) * 7919)

    def transcript(self, name, *mentions):
        path = Path(self.tmp.name, name + ".jsonl")
        path.write_text("".join(json.dumps({"output": f'{{"terminal": {{"handle": "{self.handle(m)}"}}}}'}) + "\n"
                                for m in mentions))
        return str(path)

    def pane(self, sid, provider, worktree="wt-a"):
        self.ticks += 10
        return {"pid": self.ticks, "provider": provider, "session_id": sid, "orca_terminal": self.handle(sid),
                "orca_worktree": worktree, "start_ticks": self.ticks, "parent_session_id": "",
                "parent_conversation_id": ""}

    def test_lead_transcript_handle_nests_panes_and_siblings_do_not_steal(self):
        trees = {
            "codex": [{"thread_id": "lead", "source": "cli", "title": "구현 플랜 B 테스트 콘솔",
                       "rollout_path": self.transcript("lead", "w1", "w2", "cx")},
                      {"thread_id": "cx", "source": "cli", "title": "You own code-only work for Plan B DRFL.",
                       "rollout_path": self.transcript("cx")}],
            "claude": [{"session_id": "claude-lead", "entrypoint": "cli", "spec_preview": "자코비안 따라가기",
                        "jsonl_path": self.transcript("claude-lead")},
                       # A sibling briefed on the other worker's pane must not become its owner.
                       {"session_id": "w1", "entrypoint": "cli", "jsonl_path": self.transcript("w1", "w2"),
                        "spec_preview": '\n\n<pasted_content id="1">\nYou own the field-tester coordinate display task.'},
                       {"session_id": "w2", "entrypoint": "cli", "jsonl_path": self.transcript("w2"),
                        "spec_preview": "You are the Opus 5.5 high-effort STANDARD-speed worker auditing DRFL."},
                       {"session_id": "human", "entrypoint": "cli", "spec_preview": "hello",
                        "jsonl_path": self.transcript("human")},
                       {"session_id": "printer", "entrypoint": "sdk-cli", "orca_lead_id": "stale"}],
        }
        panes = [self.pane("lead", "codex"), self.pane("claude-lead", "claude"), self.pane("w1", "claude"),
                 self.pane("cx", "codex"), self.pane("w2", "claude"), self.pane("human", "claude")]
        links = usage.link_orca_teammates(trees, panes)
        self.assertEqual(links, {"w1": "lead", "cx": "lead", "w2": "lead"})
        self.assertEqual(trees["codex"][1]["orca_lead_id"], "lead")
        self.assertEqual([n.get("orca_lead_id") for n in trees["claude"]], [None, "lead", "lead", None, None])
        # A pane opened before the lead, or one whose process is gone, is never nested.
        early = [dict(panes[2], start_ticks=1)] + panes[:2]
        self.assertEqual(usage.link_orca_teammates(trees, early), {})
        self.assertNotIn("orca_lead_id", trees["claude"][1])

    def test_delegated_prompt_falls_back_to_the_single_earlier_lead_of_its_worktree(self):
        def trees(*claude):
            return {"codex": [], "claude": [{"session_id": sid, "entrypoint": "cli", "spec_preview": prompt}
                                            for sid, prompt in claude]}
        delegated = "You are the code-only worker for Plan B. Do not spawn workers."
        t = trees(("lead", "자코비안 따라가기"), ("mate", delegated), ("other", delegated), ("second", "질문 하나"))
        panes = [self.pane("lead", "claude"), self.pane("mate", "claude"), self.pane("other", "claude", "wt-b"),
                 self.pane("second", "claude")]
        self.assertEqual(usage.link_orca_teammates(t, panes), {"mate": "lead"})
        # Two human-driven leads in one worktree and no handle evidence: no guessed owner.
        t = trees(("lead", "자코비안 따라가기"), ("lead2", "플랜 B 콘솔"), ("mate", delegated))
        panes = [self.pane("lead", "claude"), self.pane("lead2", "claude"), self.pane("mate", "claude")]
        self.assertEqual(usage.link_orca_teammates(t, panes), {})

    def test_handle_scan_is_incremental(self):
        path = Path(self.transcript("lead", "w1"))
        self.assertEqual(usage._transcript_orca_handles(str(path)), {self.handle("w1")})
        with path.open("a") as stream:
            stream.write(self.handle("w2") + "\n")
        self.assertEqual(usage._transcript_orca_handles(str(path)), {self.handle("w1"), self.handle("w2")})
        self.assertEqual(usage._ORCA_HANDLE_SCAN[str(path)][1], path.stat().st_size)
        self.assertEqual(usage._transcript_orca_handles(""), frozenset())


class ClaudeTerminalSessionTests(unittest.TestCase):
    def test_orca_terminal_session_with_subagent(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, HOME=home):
            usage._CLAUDE_JSONL_CACHE.clear()
            self.addCleanup(usage._CLAUDE_JSONL_CACHE.clear)
            cwd = os.path.join(home, "orca/workspaces/tag_gcdual/wt")
            project = Path(home, ".claude/projects/p")
            (project / "sid-1/subagents").mkdir(parents=True)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            def assistant(sid, mid, model, tokens):
                return json.dumps({"type": "assistant", "sessionId": sid, "timestamp": now, "cwd": cwd,
                                   "message": {"id": mid, "model": model, "usage": {"input_tokens": tokens}}})
            (project / "sid-1.jsonl").write_text("\n".join([
                json.dumps({"type": "user", "sessionId": "sid-1", "timestamp": now, "cwd": cwd,
                            "message": {"content": '<pasted_content id="1">\nPlan B field test\nmore'}}),
                assistant("sid-1", "m1", "claude-opus-5-5", 10),
                assistant("sid-1", "m2", "<synthetic>", 0)]) + "\n")
            (project / "sid-1/subagents/agent-a1.jsonl").write_text(assistant("sid-1", "s1", "claude-sonnet-5-5", 7) + "\n")
            (project / "sid-1/subagents/agent-a1.meta.json").write_text(json.dumps({"description": "Explore repo"}))
            sessions = Path(home, ".claude/sessions")
            sessions.mkdir(parents=True)
            (sessions / f"{os.getpid()}.json").write_text(json.dumps({
                "pid": os.getpid(), "sessionId": "sid-1", "cwd": cwd, "kind": "interactive",
                "entrypoint": "cli", "status": "busy", "tmux": "orca:@0.%1"}))
            Path(home, ".claude/settings.json").write_text(json.dumps(
                {"effortLevel": "high", "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}}}))
            with mock.patch.object(usage, "_load_agy_spawn_registry", return_value={}):
                node, = usage.get_claude_agent_tree()
        self.assertEqual((node["entrypoint"], node["terminal"], node["activity"]), ("cli", "orca", "busy"))
        self.assertEqual((node["model"], node["effort"], node["running"]), ("claude-opus-5-5", "xhigh", True))
        self.assertEqual(node["title"], "Plan B field test")
        sub, = node["subagents"]
        self.assertEqual((sub["title"], sub["model"], sub["token_count"]["total_tokens"], sub["is_running"]),
                         ("Explore repo", "claude-sonnet-5-5", 7, True))

if __name__ == "__main__":
    unittest.main()
