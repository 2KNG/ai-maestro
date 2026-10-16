"""Offline regressions for the optional PC monitor and network diagnostics."""
import os
from pathlib import Path
import unittest
from unittest import mock

import pc_telemetry_ext as pc


class TelemetryTests(unittest.TestCase):
    def test_disabled_module_does_not_sample_or_render(self):
        with mock.patch.dict(os.environ, {"AGY_DISABLE_PC_MON": "1"}), \
                mock.patch.object(pc.subprocess, "run") as run, \
                mock.patch.object(pc.subprocess, "check_output") as check:
            self.assertEqual(pc.collect_pc_telemetry(force=True), {"enabled": False})
            self.assertEqual(pc.render_pc_telemetry_html({"enabled": True}), "")
            run.assert_not_called()
            check.assert_not_called()

    def test_legacy_snapshot_still_renders_and_escapes_device_names(self):
        snapshot = {"enabled": True, "cpu": {"model": '<CPU & "test">'},
                    "network": {"iface": '<wifi & "test">',
                                "rx_rate_label": "4 KB/s", "tx_rate_label": "2 KB/s"}}
        with mock.patch.dict(os.environ, {"AGY_DISABLE_PC_MON": "0"}):
            rendered = pc.render_pc_telemetry_html(snapshot)
        self.assertIn("4 KB/s", rendered)
        self.assertIn("2 KB/s", rendered)
        self.assertIn("&lt;CPU &amp; &quot;test&quot;&gt;", rendered)
        self.assertNotIn('<wifi & "test">', rendered)
        self.assertIn('id="pc-telemetry-ext"', rendered)


if __name__ == "__main__":
    unittest.main()
