"""Auto-wake guards with synthetic quota snapshots; nothing is ever pinged."""
import copy
import datetime
import os
import tempfile
import unittest
from unittest import mock

import check_all_usage as usage
import render_usage_widget as widget
import subscription_wake as wake

NOW = 1_800_000_000.0
AGY_GEMINI, AGY_3P = wake.AGY_GEMINI_POOL, wake.AGY_3P_POOL


def agy_window(reset_in, rem=100.0):
    return {"remaining_percent": rem, "used_percent": round(100.0 - rem, 2), "reset_time_kst": "x",
            "reset_epoch": NOW + reset_in if reset_in is not None else None,
            "status": "활성" if rem < 100 else "가용 (대기)"}


def snapshot(gemini=18000, agy_claude=18000, codex=604800, claude_reset=None,
             agy_source="RetrieveUserQuotaSummary", claude_stale=False, codex_stale=False):
    claude_5h = {"remaining_percent": 100.0, "used_percent": 0.0,
                 "reset_time_kst": "대기 (미사용)" if claude_reset is None else "y",
                 "reset_epoch": None if claude_reset is None else NOW + claude_reset,
                 "status": "가용 (대기)" if claude_reset is None else "활성"}
    return {
        "antigravity": {"pools": {
            AGY_GEMINI: {"five_hour": agy_window(gemini), "weekly": agy_window(500000, 90.0),
                         "quota_source": agy_source},
            AGY_3P: {"five_hour": agy_window(agy_claude), "weekly": agy_window(500000, 60.0),
                     "quota_source": agy_source},
        }},
        "codex_local": {"weekly": {**agy_window(codex), "status": "활성"}, "quota_available": True,
                        "stale": codex_stale, "source": "https://chatgpt.com/backend-api/wham/usage"},
        "claude_local": {"authenticated": True, "quota_available": True, "stale": claude_stale,
                         "five_hour": claude_5h, "weekly": {"remaining_percent": 80.0}},
    }


class WakeHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "pool_wake_state.json")
        self.calls = []

    def actions(self, ok=True):
        def make(key):
            def run():
                self.calls.append(key)
                return ok, "OK" if ok else "boom"
            return run
        return {key: make(key) for key in wake.POOLS}

    def observe(self, data, now):
        return wake.annotate_window_activity(data, now, self.path)

    def run_wake(self, data, now, ok=True, fresh=None):
        revalidate = (lambda key: copy.deepcopy(fresh if fresh is not None else data))
        return wake.ensure_subscription_windows_awake(
            copy.deepcopy(data), now=now, path=self.path, actions=self.actions(ok), revalidate=revalidate)



def shifted(data, seconds):
    """Same sliding windows seen `seconds` later (reset = now + span keeps moving)."""
    out = copy.deepcopy(data)
    for pool in out["antigravity"]["pools"].values():
        pool["five_hour"]["reset_epoch"] += seconds
    out["codex_local"]["weekly"]["reset_epoch"] += seconds
    return out


class SlidingDetectionTests(WakeHarness):
    def test_single_observation_never_wakes(self):
        result = self.run_wake(snapshot(), NOW)
        self.assertEqual(self.calls, ["claude_team_5h"])
        self.assertEqual(result["agy_gemini_5h"], "skip: sliding reset not confirmed")
        self.assertEqual(result["codex_promax_7d"], "skip: sliding reset not confirmed")

    def test_confirmed_sliding_wakes_each_pool_once(self):
        self.observe(shifted(snapshot(), -60), NOW - 60)
        result = self.run_wake(snapshot(), NOW)
        self.assertEqual(sorted(self.calls), sorted(wake.POOLS))
        self.assertTrue(all(v == "woken" for v in result.values()), result)
        state = wake.load_state(self.path)
        self.assertEqual(state["agy_claude_5h"]["active_until_epoch"], NOW + 18000)
        self.assertEqual(state["claude_team_5h"]["active_until_epoch"], NOW + 18000)
        # Codex records the observed weekly reset as the end of the woken cycle.
        self.assertEqual(state["codex_promax_7d"]["active_until_epoch"], NOW + 604800)

        # The next daemon pass (5 minutes later) must not ping again even if
        # upstream still shows a sliding 100% window.
        self.calls.clear()
        result = self.run_wake(shifted(snapshot(), 300), NOW + 300)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(v.startswith("skip: already woken") for v in result.values()), result)

    def test_static_reset_is_pinned_not_sliding(self):
        # Same reset seen 90s apart: started window that still reports 100%.
        # Started 10s before the first poll, so the reset stays above the 4h58m line.
        self.observe(snapshot(gemini=17990), NOW - 90)
        data = snapshot(gemini=17990)
        result = self.run_wake(data, NOW)
        self.assertNotIn("agy_gemini_5h", self.calls)
        self.assertEqual(result["agy_gemini_5h"], "skip: window started")


class AntiFalsePositiveGuards(WakeHarness):
    def test_stale_or_fallback_data_is_ignored(self):
        self.observe(shifted(snapshot(), -60), NOW - 60)
        data = snapshot(agy_source="GetUserStatus", claude_stale=True, codex_stale=True)
        result = self.run_wake(data, NOW)
        self.assertEqual(self.calls, [])
        self.assertEqual(result["agy_claude_5h"], "skip: fallback quota source")
        self.assertIn("stale", result["claude_team_5h"])
        self.assertIn("stale", result["codex_promax_7d"])

    def test_missing_or_error_data_is_ignored(self):
        result = self.run_wake({"antigravity": {"error": "down"}, "codex_local": {"error": "x"},
                                "claude_local": {}}, NOW)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(v.startswith("skip:") for v in result.values()))

    def test_cooldown_blocks_repeat_attempts(self):
        wake.update_state(lambda s: s.update({k: {"last_wake_attempt_ts": NOW - 1799} for k in wake.POOLS}), self.path)
        self.observe(shifted(snapshot(), -60), NOW - 60)
        result = self.run_wake(snapshot(), NOW)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(v == "skip: cooldown" for v in result.values()), result)

    def test_active_until_blocks_wake(self):
        wake.update_state(lambda s: s.update({k: {"active_until_epoch": NOW + 10} for k in wake.POOLS}), self.path)
        self.observe(shifted(snapshot(), -60), NOW - 60)
        result = self.run_wake(snapshot(), NOW)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(v == "skip: already woken this window" for v in result.values()), result)

    def test_live_recheck_vetoes_wake(self):
        self.observe(shifted(snapshot(), -60), NOW - 60)
        started = snapshot(gemini=17000, agy_claude=17000, codex=600000, claude_reset=17000)
        result = self.run_wake(snapshot(), NOW, fresh=started)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(v == "skip: window started" for v in result.values()), result)

    def test_used_window_and_exhausted_weekly_never_wake(self):
        self.observe(shifted(snapshot(), -60), NOW - 60)
        data = snapshot()
        data["antigravity"]["pools"][AGY_GEMINI]["five_hour"].update(remaining_percent=99.5, used_percent=0.5)
        data["antigravity"]["pools"][AGY_3P]["weekly"]["remaining_percent"] = 0.0
        result = self.run_wake(data, NOW, fresh=data)
        self.assertNotIn("agy_gemini_5h", self.calls)
        self.assertNotIn("agy_claude_5h", self.calls)
        self.assertEqual(result["agy_gemini_5h"], "skip: window in use")
        self.assertEqual(result["agy_claude_5h"], "skip: weekly quota exhausted")

    def test_failures_back_off_exponentially(self):
        data = snapshot()
        self.run_wake(data, NOW, ok=False)  # claude pool fires (no sliding proof needed)
        state = wake.load_state(self.path)["claude_team_5h"]
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertNotIn("active_until_epoch", state)
        wake.update_state(lambda s: s["claude_team_5h"].update(consecutive_failures=3), self.path)
        self.calls.clear()
        self.run_wake(data, NOW + 1800 * 3, ok=False)  # 3 failures -> 2h cooldown
        self.assertNotIn("claude_team_5h", self.calls)
        self.run_wake(data, NOW + 7201, ok=False)
        self.assertIn("claude_team_5h", self.calls)

    def test_concurrent_run_is_refused(self):
        self.assertTrue(wake._run_lock.acquire(blocking=False))
        try:
            self.assertEqual(self.run_wake(snapshot(), NOW), {"_": "already running"})
        finally:
            wake._run_lock.release()
        self.assertEqual(self.calls, [])


class StartedWindowDisplayTests(WakeHarness):
    def test_pinned_100_percent_window_is_marked_active(self):
        data = snapshot(gemini=17000, agy_claude=18000, codex=604000, claude_reset=17000)
        kinds = self.observe(data, NOW)
        gem = data["antigravity"]["pools"][AGY_GEMINI]["five_hour"]
        self.assertEqual((kinds["agy_gemini_5h"], gem["status"], gem["started"]), ("pinned", "활성", True))
        self.assertEqual(kinds["agy_claude_5h"], "sliding")
        self.assertFalse(data["antigravity"]["pools"][AGY_3P]["five_hour"]["started"])
        self.assertTrue(data["codex_local"]["weekly"]["started"])
        self.assertTrue(data["claude_local"]["five_hour"]["started"])

    def test_repeat_observation_and_woken_cycle_pin_the_window(self):
        data = snapshot(agy_claude=17990)
        self.observe(data, NOW)
        later = snapshot(agy_claude=17990)
        self.assertEqual(self.observe(later, NOW + 90)["agy_claude_5h"], "pinned")
        wake.update_state(lambda s: s.setdefault("agy_gemini_5h", {}).update(active_until_epoch=NOW + 18000), self.path)
        self.assertEqual(self.observe(snapshot(), NOW + 1)["agy_gemini_5h"], "pinned")

    def test_agy_window_pins_on_early_reset_without_state(self):
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4, minutes=30)).isoformat()
        slide = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
        self.assertEqual(usage._agy_quota_window(1, soon, 18000)["status"], "활성")
        self.assertEqual(usage._agy_quota_window(1, slide, 18000)["status"], "가용 (대기)")
        self.assertEqual(usage._agy_quota_window(0.5, slide, 18000)["status"], "활성")
        self.assertIsNotNone(usage._agy_quota_window(1, soon, 18000)["reset_epoch"])

    def test_widget_shows_in_use_for_started_full_window(self):
        data = snapshot(gemini=17000)
        self.observe(data, NOW)
        accounts = widget.build_accounts(data)
        gem = next(w for a in accounts for w in a["windows"] if w["poolKey"] == "agy_gemini_5h")
        cl3p = next(w for a in accounts for w in a["windows"] if w["poolKey"] == "agy_claude_5h")
        self.assertEqual((gem["remaining"], gem["started"], cl3p["started"]), (100.0, True, False))
        with mock.patch.object(widget, "load_calibrated_pool_token_capacities", return_value={}):
            html = widget.build_static_quota_grid_html([{"provider": "antigravity", "name": "AGY", "windows": [gem]}])
            idle = widget.build_static_quota_grid_html([{"provider": "antigravity", "name": "AGY", "windows": [cl3p]}])
        self.assertIn("usage-window in-use", html)
        self.assertIn("사용중", html)
        self.assertIn("usage-window unused", idle)


class DaemonSchedulingTests(unittest.TestCase):
    def test_prune_removes_only_week_old_files(self):
        import widget_bridge_daemon as daemon
        with tempfile.TemporaryDirectory() as tmp:
            old, new = os.path.join(tmp, "old.json"), os.path.join(tmp, "new.json")
            os.mkdir(os.path.join(tmp, "subdir"))
            for path in (old, new):
                open(path, "w").close()
            os.utime(old, (NOW - 8 * 86400, NOW - 8 * 86400))
            os.utime(new, (NOW - 6 * 86400, NOW - 6 * 86400))
            self.assertEqual(daemon.prune_stale_files(tmp, now=NOW), 1)
            self.assertEqual(sorted(os.listdir(tmp)), ["new.json", "subdir"])


    def test_disabled_and_throttled(self):
        with mock.patch.object(wake.threading, "Thread") as thread, \
                mock.patch.dict(os.environ, {"MAESTRO_AUTO_WAKE": "0"}):
            self.assertFalse(wake.maybe_wake_async({}))
        thread.assert_not_called()
        with mock.patch.object(wake.threading, "Thread") as thread, \
                mock.patch.dict(os.environ, {"MAESTRO_AUTO_WAKE": "1"}), \
                mock.patch.object(wake, "_last_scheduled", 0.0):
            self.assertTrue(wake.maybe_wake_async({}))
            self.assertFalse(wake.maybe_wake_async({}))
        self.assertEqual(thread.call_count, 1)


if __name__ == "__main__":
    unittest.main()
