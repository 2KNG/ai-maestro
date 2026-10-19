#!/usr/bin/env python3
"""Unit tests for Worker ETA vs Actual Telemetry Logger in maestro_memory and maestro CLI."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import maestro_memory


class TelemetryLoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_maestro_memory.sqlite")
        self.snap_path = os.path.join(self.tmp_dir.name, "test_calibration.json")
        self.patcher1 = mock.patch.object(maestro_memory, "MEMORY_DB_PATH", self.db_path)
        self.patcher2 = mock.patch.object(maestro_memory, "CALIBRATION_SNAPSHOT_PATH", self.snap_path)
        self.patcher1.start()
        self.patcher2.start()

    def tearDown(self):
        self.patcher2.stop()
        self.patcher1.stop()
        self.tmp_dir.cleanup()

    def test_baseline_eta_table_integrity(self):
        """All empirical baseline ETA configurations must exist with positive values."""
        self.assertGreaterEqual(len(maestro_memory.BASELINE_ETA_TABLE), 13)
        for (m, eff, tier), (sec, net_tok, cost_krw) in maestro_memory.BASELINE_ETA_TABLE.items():
            self.assertGreater(sec, 0, f"sec must be positive for {m, eff, tier}")
            self.assertGreater(net_tok, 0, f"net_tok must be positive for {m, eff, tier}")
            self.assertGreater(cost_krw, 0, f"cost_krw must be positive for {m, eff, tier}")

    def test_model_normalization(self):
        """Model names and speed tiers should normalize accurately."""
        self.assertEqual(maestro_memory.normalize_telemetry_model("claude-opus-5-5", "default"), "claude-opus-5-5")
        self.assertEqual(maestro_memory.normalize_telemetry_model("claude", "fast"), "claude-opus-5-fast")
        self.assertEqual(maestro_memory.normalize_telemetry_model("opus-ultra", "default"), "claude-ultra")
        self.assertEqual(maestro_memory.normalize_telemetry_model("gpt-6.1-sol", "priority"), "gpt-6.1-sol")
        self.assertEqual(maestro_memory.normalize_telemetry_model("gpt-6-astra", "default"), "gpt-6-astra")
        self.assertEqual(maestro_memory.normalize_telemetry_model("MODEL_PLACEHOLDER_M402", "default"), "gemini-4-argon")
        self.assertEqual(maestro_memory.normalize_telemetry_model("MODEL_PLACEHOLDER_M401", "fast"), "gemini-3.8-flash")
        # Legacy CLI aliases
        self.assertEqual(maestro_memory.normalize_telemetry_model("gpt-5.4", "default"), "gpt-6.1-sol")
        self.assertEqual(maestro_memory.normalize_telemetry_model("gpt-5.4-mini", "default"), "gpt-6-astra")
        self.assertEqual(maestro_memory.normalize_telemetry_model("gpt-5.3-codex-spark", "default"), "gpt-6-astra")
        self.assertEqual(maestro_memory.normalize_telemetry_model("claude-opus-4-6", "default"), "claude-opus-5-5")
        self.assertEqual(maestro_memory.normalize_telemetry_model("claude-sonnet-4-6", "default"), "claude-sonnet-5-5")

    def test_lookup_baseline_eta(self):
        """lookup_baseline_eta should return correct estimates matching empirical values."""
        # Exact match (empirical calibration)
        sec, net_tok, cost_krw = maestro_memory.lookup_baseline_eta("gpt-6.1-sol", "high", "priority")
        self.assertEqual(sec, 130.0)
        self.assertEqual(net_tok, 100000)
        self.assertEqual(cost_krw, 3000)

        # xhigh on sol maps to ultra
        sec_xhigh, net_tok_xhigh, cost_xhigh = maestro_memory.lookup_baseline_eta("gpt-6.1-sol", "xhigh", "default")
        self.assertEqual(sec_xhigh, 120.0)
        self.assertEqual(net_tok_xhigh, 115000)
        self.assertEqual(cost_xhigh, 4000)

        # Fallback match for unknown effort
        sec_fb, _, _ = maestro_memory.lookup_baseline_eta("claude-opus-5-5", "unknown_effort", "default")
        self.assertEqual(sec_fb, 180.0)

    def test_record_valid_telemetry_with_context(self):
        """Valid execution records should compute context_tokens and context_pct."""
        res = maestro_memory.record_worker_telemetry(
            pool="codex",
            model="gpt-6.1-sol",
            effort="high",
            tier="priority",
            actual_sec=115.5,
            session_id="session-001",
            title="Refactor auth module",
            ok=True,
            actual_net_tokens=100000,
            actual_gross_tokens=1100000,
            context_tokens=25840,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["is_valid"], 1)
        self.assertEqual(res["excluded_reason"], "")
        self.assertEqual(res["context_tokens"], 25840)
        self.assertEqual(res["context_pct"], 10.0)  # 25840 / 258400 * 100

        # Check DB persistence
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT model, effort, tier, est_sec, actual_sec, context_tokens, context_pct, is_valid FROM worker_telemetry WHERE session_id = 'session-001'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "gpt-6.1-sol")
            self.assertEqual(row[1], "high")
            self.assertEqual(row[2], "priority")
            self.assertEqual(row[3], 130.0)
            self.assertEqual(row[4], 115.5)
            self.assertEqual(row[5], 25840)
            self.assertEqual(row[6], 10.0)
            self.assertEqual(row[7], 1)

    def test_exclusion_rule_error_or_not_ok(self):
        """Execution with ok=False or error should be excluded (is_valid=0)."""
        res = maestro_memory.record_worker_telemetry(
            pool="claude",
            model="claude-opus-5-5",
            effort="high",
            tier="default",
            actual_sec=80.0,
            session_id="session-err-1",
            ok=False,
            error="Process terminated unexpectedly",
            actual_net_tokens=50000,
            actual_gross_tokens=100000,
        )
        self.assertEqual(res["is_valid"], 0)
        self.assertIn("error", res["excluded_reason"].lower())

    def test_exclusion_rule_network_failure(self):
        """Execution containing network error keywords should be excluded."""
        res = maestro_memory.record_worker_telemetry(
            pool="codex",
            model="gpt-6.1-sol",
            effort="medium",
            tier="default",
            actual_sec=45.0,
            session_id="session-net-1",
            ok=True,
            output="Connecting to API... 502 Bad Gateway while communicating with upstream",
            actual_net_tokens=20000,
            actual_gross_tokens=50000,
        )
        self.assertEqual(res["is_valid"], 0)
        self.assertIn("network", res["excluded_reason"].lower())

    def test_exclusion_rule_duration_anomaly(self):
        """Duration <= 1s or > 1800s should be excluded."""
        res_short = maestro_memory.record_worker_telemetry(
            pool="agy",
            model="gemini-4-argon",
            effort="high",
            tier="default",
            actual_sec=0.8,
            session_id="session-short-1",
            ok=True,
            actual_net_tokens=10000,
            actual_gross_tokens=20000,
        )
        self.assertEqual(res_short["is_valid"], 0)
        self.assertIn("duration", res_short["excluded_reason"].lower())

        res_long = maestro_memory.record_worker_telemetry(
            pool="agy",
            model="gemini-4-argon",
            effort="high",
            tier="default",
            actual_sec=1950.0,
            session_id="session-long-1",
            ok=True,
            actual_net_tokens=10000,
            actual_gross_tokens=20000,
        )
        self.assertEqual(res_long["is_valid"], 0)
        self.assertIn("duration", res_long["excluded_reason"].lower())

    def test_exclusion_rule_zero_tokens(self):
        """Executions with zero recorded tokens should be excluded."""
        res = maestro_memory.record_worker_telemetry(
            pool="claude",
            model="claude-opus-5-5",
            effort="high",
            tier="default",
            actual_sec=60.0,
            session_id="session-zero-tok",
            ok=True,
            actual_net_tokens=0,
            actual_gross_tokens=0,
        )
        self.assertEqual(res["is_valid"], 0)
        self.assertIn("zero tokens", res["excluded_reason"].lower())

    def test_calibration_summary_aggregation(self):
        """get_telemetry_calibration_summary should aggregate statistics and context metrics."""
        for i, sec in enumerate([100.0, 150.0, 200.0]):
            maestro_memory.record_worker_telemetry(
                pool="claude", model="claude-opus-5-5", effort="high", tier="default",
                actual_sec=sec, actual_net_tokens=160000, actual_gross_tokens=300000,
                context_tokens=20000 * (i + 1), ok=True
            )
        # 1 excluded sample
        maestro_memory.record_worker_telemetry(
            pool="claude", model="claude-opus-5-5", effort="high", tier="default",
            actual_sec=0.5, actual_net_tokens=0, actual_gross_tokens=0, ok=False
        )

        summary = maestro_memory.get_telemetry_calibration_summary()
        self.assertEqual(summary["total_records"], 4)
        self.assertEqual(summary["valid_records"], 3)
        self.assertEqual(summary["excluded_records"], 1)

        calibs = summary["calibrations"]
        self.assertEqual(len(calibs), 1)
        c = calibs[0]
        self.assertEqual(c["model"], "claude-opus-5-5")
        self.assertEqual(c["valid_samples"], 3)
        self.assertEqual(c["est_sec"], 180.0)
        self.assertEqual(c["median_actual_sec"], 150.0)
        self.assertEqual(c["avg_actual_sec"], 150.0)
        self.assertEqual(c["median_actual_net_tokens"], 160000)
        self.assertEqual(c["avg_context_tokens"], 40000)  # (20k + 40k + 60k) / 3
        self.assertEqual(c["avg_context_pct"], 20.0)     # (10% + 20% + 30%) / 3

    def test_run_daily_calibration(self):
        """run_daily_calibration should output calibration snapshot and override lookup_baseline_eta."""
        # Insert 5 valid samples to satisfy min_samples=5
        for s in [100.0, 110.0, 120.0, 130.0, 140.0]:
            maestro_memory.record_worker_telemetry(
                pool="claude", model="claude-opus-5-5", effort="high", tier="default",
                actual_sec=s, actual_net_tokens=165000, actual_gross_tokens=300000,
                context_tokens=30000, ok=True
            )

        cal_res = maestro_memory.run_daily_calibration(min_samples=5, run_backfill=False)
        self.assertTrue(cal_res["ok"])
        self.assertEqual(cal_res["calibrated_count"], 1)
        self.assertTrue(os.path.exists(self.snap_path))

        # Check lookup_baseline_eta uses snapshot
        sec, net_tok, _ = maestro_memory.lookup_baseline_eta("claude-opus-5-5", "high", "default")
        self.assertEqual(sec, 120.0)  # median of [100, 110, 120, 130, 140]
        self.assertEqual(net_tok, 165000)


if __name__ == "__main__":
    unittest.main()
