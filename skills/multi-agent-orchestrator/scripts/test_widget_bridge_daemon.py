"""Isolated bridge checks; never open the user's database or provider sessions."""
import contextlib
import http.server
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import widget_bridge_daemon as bridge


@contextlib.contextmanager
def running_bridge():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def get_json(base, path):
    try:
        response = urllib.request.urlopen(base + path, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


class UnarchiveTests(unittest.TestCase):
    def test_preserves_every_original_cwd_and_other_threads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite"
            original_rows = [
                ("normal", 1, "/home/user/original project", 12),
                ("empty", 1, "", 13),
                ("null", 1, None, 14),
                ("untouched", 1, "/other", 15),
            ]
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER, cwd TEXT, updated_at INTEGER)")
                connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", original_rows)

            with mock.patch.object(bridge.os.path, "expanduser", return_value=str(db_path)), mock.patch.object(
                bridge.time, "time", return_value=1234567890
            ):
                for thread_id in ("normal", "empty", "null", "missing"):
                    bridge.unarchive_codex_thread(thread_id)

            with sqlite3.connect(db_path) as connection:
                actual = connection.execute("SELECT id, archived, cwd, updated_at FROM threads ORDER BY id").fetchall()
            expected = [
                (thread_id, 1 if thread_id == "untouched" else 0, cwd, updated_at if thread_id == "untouched" else 1234567890)
                for thread_id, _, cwd, updated_at in sorted(original_rows)
            ]
            self.assertEqual(actual, expected)


class RouteTests(unittest.TestCase):
    def test_fresh_quota_recommendations_default_and_each_difficulty_without_writes(self):
        snapshots = [{"timestamp_kst": f"snapshot-{number}"} for number in range(4)]
        with mock.patch.object(bridge.cau, "collect_usage", side_effect=snapshots, create=True) as collect, mock.patch.object(
            bridge.cau, "select_best_pool", side_effect=lambda data, difficulty: {"difficulty": difficulty, "snapshot": data["timestamp_kst"]}, create=True
        ) as select, mock.patch.object(bridge, "save_state") as save, mock.patch.object(
            bridge, "unarchive_codex_thread"
        ) as unarchive, mock.patch.object(bridge, "create_antigravity_left_sidebar_cascade") as create_cascade, mock.patch.object(
            bridge, "load_state"
        ) as load, mock.patch.object(bridge.subprocess, "check_output") as subprocess_call, running_bridge() as base:
            paths = ["/api/route", "/api/route?difficulty=high", "/api/route?difficulty=medium", "/api/route?difficulty=low"]
            for index, (path, difficulty) in enumerate(zip(paths, ("high", "high", "medium", "low"))):
                status, body = get_json(base, path)
                self.assertEqual(status, 200)
                self.assertEqual(body, {
                    "ok": True,
                    "timestamp_kst": f"snapshot-{index}",
                    "recommended_routing": {"difficulty": difficulty, "snapshot": f"snapshot-{index}"},
                })
            self.assertEqual(collect.call_args_list, [mock.call(include_agent_trees=False)] * 4)
            self.assertEqual(select.call_args_list, [
                mock.call(snapshot, difficulty=difficulty)
                for snapshot, difficulty in zip(snapshots, ("high", "high", "medium", "low"))
            ])
            for operation in (save, unarchive, create_cascade, load, subprocess_call):
                operation.assert_not_called()

    def test_invalid_difficulty_does_not_collect(self):
        with mock.patch.object(bridge.cau, "collect_usage", create=True) as collect, running_bridge() as base:
            for value in ("invalid", "", "HIGH"):
                with self.subTest(value=value):
                    status, body = get_json(base, "/api/route?difficulty=" + value)
                    self.assertEqual(status, 400)
                    self.assertFalse(body["ok"])
                    self.assertIn("difficulty", body["error"])
            collect.assert_not_called()

    def test_collection_failure_returns_json_error(self):
        with mock.patch.object(bridge.cau, "collect_usage", side_effect=RuntimeError("quota unavailable"), create=True), running_bridge() as base:
            status, body = get_json(base, "/api/route")
            self.assertEqual(status, 500)
            self.assertEqual(body, {"ok": False, "error": "quota unavailable"})

    def test_refresh_and_status_include_collected_routing(self):
        data = {"timestamp_kst": "now", "recommended_routing": {"primary": "test-pool"}}
        with mock.patch.object(bridge, "fetch_usage_data", return_value=data), mock.patch.object(
            bridge, "write_widget_to_all_conversations"
        ) as write_widgets, mock.patch.object(
            bridge.ruw, "build_accounts", return_value=[]
        ), mock.patch.object(bridge.ruw, "codex_archive_counts", return_value={}), mock.patch.object(
            bridge, "load_state", return_value={}
        ), running_bridge() as base:
            for path in ("/api/status", "/api/refresh"):
                status, body = get_json(base, path)
                self.assertEqual(status, 200)
                self.assertEqual(body["recommended_routing"], data["recommended_routing"])
            write_widgets.assert_called_once_with(data)


if __name__ == "__main__":
    unittest.main()
