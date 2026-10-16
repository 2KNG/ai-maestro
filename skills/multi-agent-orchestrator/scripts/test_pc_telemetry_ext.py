"""Offline regressions for the optional PC monitor and network diagnostics."""
import json
import os
from pathlib import Path
import subprocess
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


def _iface(kind="ethernet", ip="192.168.0.10", subnet="192.168.0", primary=True,
           rx_drop=0, speed_label="1Gbps"):
    return {"iface": "eno1" if kind == "ethernet" else "wlp2s0", "kind": kind,
            "kind_label": "이더넷" if kind == "ethernet" else "Wi-Fi", "ip": ip,
            "subnet": subnet, "is_primary": primary, "rx_drop": rx_drop,
            "speed_label": speed_label}


class NetworkVerdictTests(unittest.TestCase):
    def verdict(self, ifaces=None, gw="192.168.0.1", gw_ms=2.0, ext_ms=10.0,
                dns_ms=20.0, rate=0.0):
        return pc._synthesize_network_verdict(
            ifaces if ifaces is not None else [_iface()], gw, gw_ms, ext_ms, dns_ms, rate)

    def test_high_cumulative_drops_with_low_live_drop_is_ok(self):
        v = self.verdict([_iface(rx_drop=5_000_000)], rate=0.4)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["reasons"], [])
        self.assertTrue(v["summary"].startswith("정상"))
        self.assertNotIn("병목", v["summary"])
        self.assertTrue(any("누적" in n for n in v["notes"]))

    def test_live_drop_threshold_is_inclusive_at_2_0(self):
        below = self.verdict(rate=1.99)
        exact = self.verdict(rate=2.0)
        self.assertEqual(below["status"], "ok")
        self.assertEqual(exact["status"], "warn")
        self.assertIn("병목", exact["summary"])
        self.assertTrue(exact["reasons"])

    def test_gateway_80ms_ok_and_above_80_warns(self):
        self.assertEqual(self.verdict(gw_ms=80.0)["status"], "ok")
        v = self.verdict(gw_ms=80.1)
        self.assertEqual(v["status"], "warn")
        self.assertIn("공유기", v["summary"])
        self.assertNotIn("병목", v["summary"])

    def test_gateway_failed_with_external_reachable_warns_not_crit(self):
        v = self.verdict(gw_ms=None, ext_ms=15.0)
        self.assertEqual(v["status"], "warn")
        self.assertIn("공유기", v["summary"])

    def test_gateway_and_external_down_stays_critical(self):
        v = self.verdict(gw_ms=None, ext_ms=None)
        self.assertEqual(v["status"], "crit")
        self.assertTrue(v["summary"].startswith("오류"))

    def test_external_down_with_gateway_up_stays_critical(self):
        v = self.verdict(gw_ms=3.0, ext_ms=None)
        self.assertEqual(v["status"], "crit")
        self.assertTrue(v["summary"].startswith("오류"))

    def test_subnet_overlap_wifi_24ghz_dns_external_latency_are_informational(self):
        ifaces = [_iface(), _iface("wifi", "192.168.0.20", primary=False,
                                   speed_label="home (2.4GHz · 60%)")]
        v = self.verdict(ifaces, gw_ms=5.0, ext_ms=300.0, dns_ms=900.0, rate=0.0)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["reasons"], [])
        self.assertTrue(v["summary"].startswith("정상"))
        self.assertGreaterEqual(len(v["notes"]), 4)

    def test_primary_24ghz_wifi_alone_is_not_warn(self):
        v = self.verdict([_iface("wifi", primary=True, speed_label="home (2.4GHz · 60%)")])
        self.assertEqual(v["status"], "ok")

    def test_outage_not_downgraded_by_informational_signals(self):
        v = self.verdict([_iface(rx_drop=9999)], gw_ms=None, ext_ms=None, rate=0.0)
        self.assertEqual(v["status"], "crit")

    def test_collect_diagnosis_uses_live_rate_not_cumulative(self):
        proc = "h1\nh2\n  eno1: 100 10 0 9999 0 0 0 0 100 10 0 0 0 0 0 0\n"
        ip_out = "2: eno1    inet 192.168.0.10/24 brd 192.168.0.255 scope global eno1\n"
        route = "default via 192.168.0.1 dev eno1 metric 100\n"

        def fake_check_output(cmd, *a, **k):
            return ip_out if "addr" in cmd else route

        real_open = open

        def fake_open(path, *a, **k):
            if path == "/proc/net/dev":
                return mock.mock_open(read_data=proc)()
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", fake_open), \
                mock.patch.object(pc.subprocess, "check_output", fake_check_output), \
                mock.patch.object(pc.shutil, "which", return_value=None), \
                mock.patch.object(pc, "_ping_host_ms", return_value=5.0), \
                mock.patch.object(pc, "_probe_dns_ms", return_value=20.0), \
                mock.patch.object(pc, "_collect_recent_ipv4_conflicts", return_value=[]):
            calm = pc._collect_interfaces_and_diagnosis(0.0)
            busy = pc._collect_interfaces_and_diagnosis(2.0)
        self.assertEqual(calm["status"], "ok")
        self.assertEqual(calm["interfaces"][0]["rx_drop"], 9999)
        self.assertEqual(busy["status"], "warn")


NOW = 1_800_000_000.0
LOCAL = {"192.168.0.10"}


def _rec(msg, age=10.0, transport="kernel", ident=None):
    r = {"__REALTIME_TIMESTAMP": str(int((NOW - age) * 1_000_000)), "MESSAGE": msg}
    if transport:
        r["_TRANSPORT"] = transport
    if ident:
        r["SYSLOG_IDENTIFIER"] = ident
    return json.dumps(r)


class Ipv4ConflictTests(unittest.TestCase):
    def conflicts(self, lines=(), local=LOCAL, returncode=0, side_effect=None):
        proc = mock.Mock(returncode=returncode, stdout="\n".join(lines))
        with mock.patch.object(pc.time, "time", return_value=NOW), \
                mock.patch.object(pc.subprocess, "run", return_value=proc,
                                  side_effect=side_effect) as run:
            res = pc._collect_recent_ipv4_conflicts(set(local))
        return res, run

    def test_current_conflict_warns_and_uses_bounded_no_shell_call(self):
        res, run = self.conflicts([_rec("IPv4: Duplicate address 192.168.0.10 used by aa:bb:cc:dd:ee:ff detected!")])
        self.assertEqual(res, ["192.168.0.10"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "journalctl")
        self.assertLessEqual(kwargs["timeout"], 1.0)
        self.assertFalse(kwargs.get("shell", False))
        v = pc._synthesize_network_verdict([_iface()], "192.168.0.1", 2.0, 10.0, 20.0, 0.0, res)
        self.assertEqual(v["status"], "warn")
        self.assertTrue(any("192.168.0.10" in r for r in v["reasons"]))
        self.assertIn("충돌", v["summary"])

    def test_networkmanager_address_conflict_counts(self):
        res, _ = self.conflicts([_rec("ipv4 (eno1): address conflict for 192.168.0.10 detected",
                                      transport="journal", ident="NetworkManager")])
        self.assertEqual(res, ["192.168.0.10"])

    def test_stale_event_over_300s_ignored(self):
        res, _ = self.conflicts([_rec("IPv4: Duplicate address 192.168.0.10 detected!", age=300.5)])
        self.assertEqual(res, [])
        res, _ = self.conflicts([_rec("IPv4: Duplicate address 192.168.0.10 detected!", age=300.0)])
        self.assertEqual(res, ["192.168.0.10"])

    def test_future_timestamp_ignored(self):
        res, _ = self.conflicts([_rec("IPv4: Duplicate address 192.168.0.10 detected!", age=-5.0)])
        self.assertEqual(res, [])

    def test_different_ip_ignored(self):
        res, _ = self.conflicts([_rec("IPv4: Duplicate address 192.168.0.99 detected!")])
        self.assertEqual(res, [])

    def test_benign_messages_ignored(self):
        res, _ = self.conflicts([
            _rec("IPv6: duplicate address fe80::1 detected!"),
            _rec("ipv6 (eno1): address conflict detected, ignoring 192.168.0.10"),
            _rec("some generic conflict duplicate warning on 192.168.0.10"),
            _rec("192.168.0.10 on same subnet 192.168.0.0/24 as wlp2s0", ident="NetworkManager"),
            _rec("IPv4: Duplicate address 192.168.0.10 detected!", transport="stdout", ident="other"),
            "not json",
        ])
        self.assertEqual(res, [])
        v = pc._synthesize_network_verdict(
            [_iface(), _iface("wifi", "192.168.0.20", primary=False)], "192.168.0.1", 2.0, 10.0, 20.0, 0.0, res)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["reasons"], [])

    def test_missing_journal_permission_timeout_bad_json_means_no_conflict(self):
        for kw in ({"side_effect": FileNotFoundError()},
                   {"side_effect": subprocess.TimeoutExpired("journalctl", 1.0)},
                   {"returncode": 1, "lines": [_rec("IPv4: Duplicate address 192.168.0.10 detected!")]},
                   {"lines": ["{bad json", "[]", "null"]}):
            res, _ = self.conflicts(**kw)
            self.assertEqual(res, [])

    def test_conflict_does_not_downgrade_crit(self):
        v = pc._synthesize_network_verdict([_iface()], "192.168.0.1", None, None, None, 0.0, ["192.168.0.10"])
        self.assertEqual(v["status"], "crit")
        self.assertTrue(v["summary"].startswith("오류"))
        self.assertTrue(any("192.168.0.10" in r for r in v["reasons"]))


if __name__ == "__main__":
    unittest.main()
