import json
import os
import tempfile
import unittest
from unittest import mock

import cr_wifi_watchdog as watchdog


class CrWifiWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_state = os.path.join(self.tmp_dir.name, "state.json")
        self.tmp_log = os.path.join(self.tmp_dir.name, "watchdog.log")
        self.orig_state = watchdog.STATE_FILE
        self.orig_log = watchdog.LOG_FILE
        watchdog.STATE_FILE = self.tmp_state
        watchdog.LOG_FILE = self.tmp_log

    def tearDown(self):
        watchdog.STATE_FILE = self.orig_state
        watchdog.LOG_FILE = self.orig_log
        self.tmp_dir.cleanup()

    @mock.patch("check_all_usage.discover_ls_credentials")
    @mock.patch("maestro.ls_post")
    def test_resume_skips_when_agent_is_running(self, mock_ls_post, mock_creds):
        mock_creds.return_value = ("127.0.0.1:41513", "csrf-token-abc")
        mock_ls_post.return_value = {
            "trajectorySummaries": {
                watchdog.TARGET_CASCADE_ID: {"status": "CASCADE_RUN_STATUS_RUNNING"}
            }
        }
        res = watchdog.resume_3d_agent("test reason")
        self.assertTrue(res)
        # Should call GetAllCascadeTrajectories but NOT SendUserCascadeMessage
        mock_ls_post.assert_called_once_with(
            "127.0.0.1:41513", "csrf-token-abc", "GetAllCascadeTrajectories", {}, timeout=5
        )

    @mock.patch("check_all_usage.discover_ls_credentials")
    @mock.patch("maestro.ls_post")
    def test_resume_sends_message_when_idle(self, mock_ls_post, mock_creds):
        mock_creds.return_value = ("127.0.0.1:41513", "csrf-token-abc")

        def side_effect(addr, token, method, payload, timeout=10):
            if method == "GetAllCascadeTrajectories":
                return {
                    "trajectorySummaries": {
                        watchdog.TARGET_CASCADE_ID: {"status": "CASCADE_RUN_STATUS_IDLE"}
                    }
                }
            if method == "GetCascadeTrajectoryGeneratorMetadata":
                return {
                    "generatorMetadata": [{"planModel": "MODEL_PLACEHOLDER_M417"}]
                }
            if method == "SendUserCascadeMessage":
                return {"status": "OK"}
            return {}

        mock_ls_post.side_effect = side_effect
        res = watchdog.resume_3d_agent("network restored")
        self.assertTrue(res)

        # Check SendUserCascadeMessage call
        calls = [c for c in mock_ls_post.call_args_list if c[0][2] == "SendUserCascadeMessage"]
        self.assertEqual(len(calls), 1)
        send_payload = calls[0][0][3]
        self.assertEqual(send_payload["cascadeId"], watchdog.TARGET_CASCADE_ID)
        self.assertIn("network restored", send_payload["items"][0]["text"])
        self.assertEqual(
            send_payload["cascadeConfig"]["plannerConfig"]["planModel"],
            "MODEL_PLACEHOLDER_M417",
        )

    @mock.patch("check_all_usage.discover_ls_credentials")
    @mock.patch("urllib.request.urlopen")
    def test_resume_bridge_fallback(self, mock_urlopen, mock_creds):
        mock_creds.return_value = (None, None)

        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = watchdog.resume_3d_agent("bridge fallback test")
        self.assertTrue(res)
        self.assertTrue(mock_urlopen.called)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("/api/agent-control", req.full_url)

    @mock.patch("subprocess.run")
    def test_has_internet(self, mock_run):
        mock_run.return_value = mock.MagicMock(returncode=0)
        self.assertTrue(watchdog.has_internet())

        mock_run.return_value = mock.MagicMock(returncode=1)
        self.assertFalse(watchdog.has_internet())

    @mock.patch("time.sleep")
    @mock.patch("cr_wifi_watchdog.resume_3d_agent")
    @mock.patch("cr_wifi_watchdog.has_internet")
    @mock.patch("cr_wifi_watchdog.active_wifi_name")
    @mock.patch("subprocess.run")
    def test_check_once_triggers_failover_after_5_mins(self, mock_run, mock_wifi, mock_internet, mock_resume, mock_sleep):
        mock_wifi.return_value = "CR_WIFI"
        mock_internet.side_effect = [False, True]  # first offline, then online after switch

        # Pre-seed state with 301 seconds offline
        now = 1000.0
        with open(self.tmp_state, "w", encoding="utf-8") as f:
            json.dump({"offline_since": now - 301, "cr_wifi_since": now - 301}, f)

        with mock.patch("time.time", return_value=now):
            watchdog.check_once(dry_run=False)

        # cr-wifi off should be called
        mock_run.assert_any_call(["/home/u24c9g5090r64/.local/bin/cr-wifi", "off"], check=False)
        # resume_3d_agent should be called
        self.assertTrue(mock_resume.called)


if __name__ == "__main__":
    unittest.main()
