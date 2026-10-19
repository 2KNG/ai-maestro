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
        # Normal zone (7d > 10%): the 5h window routes, so a low 7d value alone does not throttle.
        data = snapshot(60, 40, 20, 15)
        data["codex_local"]["five_hour"] = window(95)
        data["claude_local"]["five_hour"] = window(99)
        result = usage.select_best_pool(data)
        self.assertEqual(result["pool"], "claude")
        self.assertEqual(candidates(result)["codex"]["remaining_percent"], 95)
        self.assertEqual(candidates(result)["claude"]["remaining_percent"], 99)
        # Weekly Conserve zone (2% < 7d <= 10%): score is min(5h, 7d * 3.5), so antigravity wins.
        data = snapshot(60, 40, 8, 5)
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
                    "builtins.open", mock.mock_open(read_data=json.dumps(event) + "\n")
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
                # Weekly headroom (80%) is above the 10% guardrail, so routing follows the 5h window (90%).
                self.assertEqual(usage._routing_remaining(fresh), 90)


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


if __name__ == "__main__":
    unittest.main()
