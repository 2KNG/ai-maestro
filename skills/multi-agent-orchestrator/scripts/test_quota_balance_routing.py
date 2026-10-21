"""Reset-aware Claude/Codex quota balancing: Opus 5.5 must not starve while Codex has quota."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import benchmark_sync as bench
import maestro as workers

DAY = 86400.0
CODING_TASKS = [
    ("long horizon debugging", "high"),
    ("긴 호흡의 버그 수정 및 단위테스트 루프", "high"),
    ("refactor the parser module and add tests", "medium"),
    ("refactor the parser module and add tests", "high"),
    ("아키텍처 설계 및 심층 알고리즘 연구", "high"),
    ("complex backend and frontend feature implementation", "high"),
]


def usage(claude_7d, codex_7d, claude_days=None, codex_days=None, claude_5h=96.0, codex_5h=None):
    now = time.time()

    def weekly(rem, days):
        win = {"remaining_percent": rem}
        if days is not None:
            win["resets_at"] = now + days * DAY
        return win

    data = {
        "claude_local": {"weekly": weekly(claude_7d, claude_days)},
        "codex_local": {"weekly": weekly(codex_7d, codex_days)},
        # Gemini 7d < 50% applies the Argon protection penalty, as in production.
        "antigravity": {"pools": {"Gemini Pool (Argon / Flash)": {"weekly": {"remaining_percent": 40.0}}}},
    }
    if claude_5h is not None:
        data["claude_local"]["five_hour"] = {"remaining_percent": claude_5h}
    if codex_5h is not None:
        data["codex_local"]["five_hour"] = {"remaining_percent": codex_5h}
    return data


class QuotaBalanceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = root
        self.stack.enter_context(mock.patch.object(bench, 'CACHE_PATH', str(root / 'benchmark.json')))
        self.stack.enter_context(mock.patch.object(bench, 'LEGACY_CACHE_PATH', str(root / 'legacy.json')))
        self.stack.enter_context(mock.patch.object(bench, '_detect_local_subscription_models', return_value={
            'new_models_detected': [], 'antigravity_models': [], 'codex_models': []}))
        self.stack.enter_context(mock.patch.object(bench.urllib.request, 'urlopen', side_effect=OSError('offline')))
        self.stack.enter_context(mock.patch.object(bench, 'SPAWN_REGISTRY_PATH', str(root / 'missing.json')))

    def recommend(self, spec, difficulty, data, active=None):
        return bench.recommend_model_for_task(spec, difficulty, data, active_workers=active or {"claude": 0, "codex": 0})

    def test_claude_70_percent_before_reset_wins_coding_tasks(self):
        # Codex just reset (7 days left); Claude has 70% left with 2.5 days to its weekly reset.
        data = usage(70.0, 100.0, claude_days=2.5, codex_days=7.0)
        for spec, difficulty in CODING_TASKS:
            with self.subTest(spec=spec, difficulty=difficulty):
                rec = self.recommend(spec, difficulty, data)
                self.assertEqual(rec["canonical_pool"], "claude")
                self.assertEqual(rec["recommended_model"], "claude-opus-5-5")
                self.assertGreater(rec["quota_balance"]["surplus"], 6.0)

    def test_claude_70_percent_without_reset_info_beats_lower_codex(self):
        data = usage(70.0, 55.0)
        rec = self.recommend("long horizon debugging", "high", data)
        self.assertEqual(rec["canonical_pool"], "claude")

    def test_running_codex_workers_shift_parallel_jobs_to_claude(self):
        # No reset surplus at all: Claude resets in 6.5 days, Codex in 2 days.
        data = usage(60.0, 89.0, claude_days=6.5, codex_days=2.0)
        idle = self.recommend("long horizon debugging", "high", data)
        self.assertEqual(idle["canonical_pool"], "codex")
        busy = self.recommend("long horizon debugging", "high", data, {"claude": 0, "codex": 3})
        self.assertEqual(busy["canonical_pool"], "claude")
        self.assertEqual(busy["quota_balance"]["claude_load"], bench.LOAD_BONUS_CAP)

    def test_running_claude_workers_shift_back_to_codex(self):
        data = usage(60.0, 89.0, claude_days=6.5, codex_days=2.0)
        rec = self.recommend("refactor the parser module", "medium", data, {"claude": 3, "codex": 0})
        self.assertEqual(rec["canonical_pool"], "codex")
        self.assertGreater(rec["quota_balance"]["codex"], 0.0)

    def test_codex_keeps_long_horizon_when_claude_has_no_surplus(self):
        data = usage(35.0, 89.0, claude_days=6.0, codex_days=3.0)
        rec = self.recommend("long horizon debugging", "high", data)
        self.assertEqual(rec["recommended_model"], "gpt-6.1-sol")
        self.assertEqual(rec["quota_balance"]["claude"], 0.0)

    def test_low_claude_quota_never_gets_balance_bonus(self):
        data = usage(70.0, 89.0, claude_days=1.0, codex_days=6.0, claude_5h=10.0)
        rec = self.recommend("long horizon debugging", "high", data, {"claude": 0, "codex": 5})
        self.assertEqual(rec["canonical_pool"], "codex")
        claude = next(c for c in rec["candidates"] if c["pool"] == "claude")
        self.assertEqual(claude["balance_bonus"], 0.0)

    def test_fast_explore_and_computer_use_are_not_rebalanced(self):
        data = usage(70.0, 100.0, claude_days=2.5, codex_days=7.0)
        busy = {"claude": 0, "codex": 5}
        self.assertEqual(self.recommend("빠른 로그 스캔 및 파일 탐색", "low", data, busy)["recommended_model"], "gpt-6-luna")
        self.assertEqual(self.recommend("OSWorld 데스크탑 화면 마우스 조작", "high", data, busy)["canonical_pool"], "codex")

    def test_surplus_bonus_is_capped(self):
        data = usage(100.0, 20.0, claude_days=0.5, codex_days=7.0)
        bal = bench.quota_balance_bonus(data, {"claude": 0, "codex": 10})
        self.assertEqual(bal["surplus"], bench.SURPLUS_BONUS_CAP)
        self.assertEqual(bal["claude"], bench.BALANCE_BONUS_CAP)

    def test_days_until_reset_parses_kst_display_string(self):
        now = 1_800_000_000.0
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + 2 * DAY + 9 * 3600))
        days = bench._days_until_reset({"reset_time_kst": f"{kst} KST (2일 후)"}, now=now)
        self.assertAlmostEqual(days, 2.0, places=3)
        self.assertAlmostEqual(bench._days_until_reset({"resets_at": now + DAY}, now=now), 1.0, places=3)
        self.assertIsNone(bench._days_until_reset({}, now=now))

    def test_count_active_workers_ignores_stale_running_records(self):
        now = 1_800_000_000.0
        registry = {
            "live-codex": {"provider": "codex", "status": "running", "started_at": now - 5 * 3600},
            "fresh-codex": {"provider": "codex", "status": "running", "started_at": now - 60},
            "stale-codex": {"provider": "codex", "status": "running", "started_at": now - 20 * 3600},
            "done-codex": {"provider": "codex", "status": "completed", "started_at": now - 60},
            "live-claude": {"provider": "claude", "pool": "claude", "status": "running", "started_at": now - 3 * 3600},
        }
        path = self.root / 'registry.json'
        path.write_text(json.dumps(registry), encoding='utf-8')
        counts = bench.count_active_workers(str(path), live_ids={"live-codex", "live-claude"}, now=now)
        self.assertEqual(counts, {"claude": 1, "codex": 2})
        self.assertEqual(bench.count_active_workers(str(self.root / 'nope.json')), {"claude": 0, "codex": 0})


class AutoPoolTests(unittest.TestCase):
    def routing(self, rec_pool):
        cand = {"status": "available", "remaining_percent": 70.0}
        return {
            "pool": "codex",
            "candidates": [{**cand, "pool": "codex", "weekly_remaining": 89.0},
                           {**cand, "pool": "claude", "weekly_remaining": 70.0}],
            "benchmark_recommendation": {"canonical_pool": rec_pool, "recommended_pool": rec_pool},
        }

    def test_medium_auto_without_spec_follows_balanced_recommendation(self):
        with mock.patch.object(workers.cau, 'collect_usage', return_value={}), \
             mock.patch.object(workers.cau, 'select_best_pool', return_value=self.routing("claude")):
            self.assertEqual(workers.choose_auto_pool("medium")[0], "claude")
        with mock.patch.object(workers.cau, 'collect_usage', return_value={}), \
             mock.patch.object(workers.cau, 'select_best_pool', return_value=self.routing("codex")):
            self.assertEqual(workers.choose_auto_pool("medium")[0], "codex-astra")

    def test_auto_with_spec_uses_claude_recommendation(self):
        routing = self.routing("claude")
        routing["pool"] = "claude"
        with mock.patch.object(workers.cau, 'collect_usage', return_value={}), \
             mock.patch.object(workers.cau, 'select_best_pool', return_value=routing):
            self.assertEqual(workers.choose_auto_pool("high", spec="long horizon debugging")[0], "claude")


if __name__ == '__main__':
    unittest.main()
