"""Isolated bridge checks; never open the user's database or provider sessions."""
import concurrent.futures
import contextlib
import http.server
import json
import os
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


def post_json(base, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        response = urllib.request.urlopen(req, timeout=5)
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
        with tempfile.TemporaryDirectory() as temp_dir:
            has_widget, missing_widget = (os.path.join(temp_dir, name) for name in ("has", "missing"))
            for directory in (has_widget, missing_widget):
                os.makedirs(directory)
            Path(has_widget, bridge.WIDGET_FILENAME).write_text("existing")
            with mock.patch.object(bridge, "fetch_usage_data", return_value=data), mock.patch.object(
                bridge, "write_widget_to_all_conversations"
            ) as write_widgets, mock.patch.object(
                bridge, "list_conversation_dirs", return_value=[has_widget, missing_widget]
            ), mock.patch.object(
                bridge.ruw, "build_accounts", return_value=[]
            ), mock.patch.object(bridge.ruw, "codex_archive_counts", return_value={}), mock.patch.object(
                bridge, "load_state", return_value={}
            ), mock.patch.object(bridge.ruw, "pick_template", return_value=("template", "test")), mock.patch.object(
                bridge.ruw, "inject_live_data", return_value="html"
            ), mock.patch.object(bridge.ruw, "build_active_and_closed_trees_html", return_value=("active", "closed", 1, 2)), mock.patch.object(
                bridge.ruw, "build_optional_pc_telemetry_html", return_value="pc"
            ), mock.patch.object(bridge, "_http_snapshot", None), running_bridge() as base:
                bridge.refresh_http_snapshot(force=True)
                for path in ("/api/status", "/api/refresh"):
                    status, body = get_json(base, path)
                    self.assertEqual(status, 200)
                    self.assertEqual(body["recommended_routing"], data["recommended_routing"])
                # Status never writes; refresh only fills conversation dirs that lack the widget file.
                write_widgets.assert_called_once_with(data, target_dirs=[missing_widget])
                write_widgets.reset_mock()
                status, _ = get_json(base, "/api/refresh?write_disk=1")
                self.assertEqual(status, 200)
                write_widgets.assert_called_once_with(data, target_dirs=[has_widget, missing_widget])

    def test_open_session_switches_orca_to_the_session_pane(self):
        wt = os.path.join(bridge.ORCA_WORKSPACES, "tag_gcdual", "로봇-테스트")
        terminals = [{"handle": "term_dev", "worktreePath": "/home/u/dev/gcdual", "title": "shell"},
                     {"handle": "term_lead", "worktreePath": wt, "title": "⠼ 구현 tag_gcdual 플랜 B 테스트 콘솔"},
                     {"handle": "term_mate", "worktreePath": wt, "title": "◑ DRL to DRFL replacement matrix"}]
        calls = []

        def orca_cli(*args):
            calls.append(args)
            if args[:2] == ("terminal", "list"):
                return {"ok": True, "result": {"terminals": terminals}}
            return True

        lead_id = "01a1629e-a557-7a50-b0c8-f9fa61424a9d"
        live = [{"session_id": lead_id, "orca_terminal": "term_lead", "orca_worktree": "id::" + wt}]
        with mock.patch.object(bridge, "load_state", return_value={}), \
             mock.patch.object(bridge, "unarchive_codex_thread"), \
             mock.patch.object(bridge, "_orca_cli", side_effect=orca_cli), \
             mock.patch.object(bridge, "_focus_orca_window") as focus, \
             mock.patch.object(bridge.os.path, "isdir", side_effect=lambda p: p == wt), \
             mock.patch("live_worker_processes.scan_live_worker_processes", return_value=live), \
             mock.patch.object(bridge.subprocess, "Popen") as popen, \
             running_bridge() as base:
            status, body = post_json(base, "/api/open-session", {
                "nodeId": "node-cdx-" + lead_id, "cascadeId": lead_id, "label": "구현 tag_gcdual", "repo": "tag_gcdual/로봇-테스트"})
            self.assertEqual(status, 200)
            self.assertEqual((body["ok"], body["target"], body["handle"]), (True, "orca", "term_lead"))
            self.assertIn(("terminal", "switch", "--terminal", "term_lead"), calls)
            # A folder row names only its worktree: the pane whose title matches, else its first pane.
            calls.clear()
            live.clear()
            status, body = post_json(base, "/api/open-session", {
                "nodeId": "orca-tag_gcdual-로봇-테스트", "cascadeId": "", "label": "로봇-테스트", "repo": "tag_gcdual/로봇-테스트"})
            self.assertEqual((body["handle"], body["worktree"]), ("term_lead", os.path.realpath(wt)))
            status, body = post_json(base, "/api/open-session", {
                "nodeId": "node-sub-x", "cascadeId": "", "label": "DRL to DRFL replacement", "repo": wt})
            self.assertEqual(body["handle"], "term_mate")
            # No live pane anywhere: the worktree folder is focused instead; Antigravity is never launched.
            terminals.clear()
            status, body = post_json(base, "/api/open-session", {"nodeId": "n", "label": "x", "repo": "tag_gcdual/로봇-테스트"})
            self.assertTrue(body["ok"])
            self.assertEqual(calls[-1][:2], ("file", "open"))
            self.assertGreaterEqual(focus.call_count, 4)
            popen.assert_not_called()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("_http_snapshot", None), ("_cached_usage_data", None), ("_cached_usage_ts", 0.0)):
            self.stack.enter_context(mock.patch.object(bridge, name, value))
        self.collect = self.stack.enter_context(mock.patch.object(bridge.cau, "collect_usage", return_value={"timestamp_kst": "first"}))
        self.load = self.stack.enter_context(mock.patch.object(bridge, "load_state", return_value={"scope": "all", "closedDays": 30, "pinned": ["x"]}))
        self.template = self.stack.enter_context(mock.patch.object(bridge.ruw, "pick_template", return_value=("template", "test")))
        self.render = self.stack.enter_context(mock.patch.object(bridge.ruw, "inject_live_data", return_value="cached html"))
        self.trees = self.stack.enter_context(mock.patch.object(bridge.ruw, "build_active_and_closed_trees_html", return_value=("active", "closed", 1, 2)))
        self.stack.enter_context(mock.patch.object(bridge.ruw, "build_accounts", return_value=[]))
        self.stack.enter_context(mock.patch.object(bridge.ruw, "build_optional_pc_telemetry_html", return_value="pc"))

    def test_cached_routes_and_filters_do_no_collection_render_or_disk_reads(self):
        bridge.refresh_http_snapshot(force=True)
        with running_bridge() as base:
            for _ in range(3):
                for path in ("/", "/widget"):
                    with urllib.request.urlopen(base + path) as response:
                        self.assertEqual(response.read(), b"cached html")
                status, body = get_json(base, "/api/status?scope=current&closedDays=7")
                self.assertEqual(status, 200)
                self.assertEqual(body["state"], {"scope": "current", "closedDays": 7, "pinned": ["x"]})
                _, body = get_json(base, "/api/status")
                self.assertEqual(body["state"]["closedDays"], 30)
                self.assertEqual(body["state"]["scope"], "all")
        for operation in (self.collect, self.load, self.template, self.render, self.trees):
            self.assertEqual(operation.call_count, 1)

    def test_concurrent_refresh_singleflight_and_stale_http_during_rebuild(self):
        old = bridge.refresh_http_snapshot(force=True)
        entered, release = threading.Event(), threading.Event()
        def slow_collect(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"timestamp_kst": "second"}
        self.collect.side_effect = slow_collect
        with running_bridge() as base, concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            first = pool.submit(bridge.refresh_http_snapshot, True)
            self.assertTrue(entered.wait(2))
            # Hold every follower behind the same in-progress render lock.
            barrier = threading.Barrier(5)
            original_lock = bridge._refresh_lock
            class ObservedLock:
                def __enter__(self):
                    barrier.wait(timeout=3)
                    return original_lock.__enter__()
                def __exit__(self, *args):
                    return original_lock.__exit__(*args)
            with mock.patch.object(bridge, "_refresh_lock", ObservedLock()):
                followers = [pool.submit(bridge.refresh_http_snapshot, True) for _ in range(4)]
                barrier.wait(timeout=3)
                _, body = get_json(base, "/api/status")
                self.assertEqual(body["timestamp_kst"], "first")
                self.assertIs(bridge._http_snapshot, old)
                release.set()
                results = [first.result(3)] + [f.result(3) for f in followers]
            self.assertTrue(all(item is results[0] for item in results))
        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(self.render.call_count, 2)

    def test_failed_refresh_retains_snapshot_and_bootstrap_is_available(self):
        with running_bridge() as base:
            status, body = get_json(base, "/api/status")
            self.assertEqual(status, 200)
            self.assertTrue(body["warmingUp"])
            with urllib.request.urlopen(base + "/widget") as response:
                self.assertIn(b"warming up", response.read())
            old = bridge.refresh_http_snapshot(force=True)
            self.render.side_effect = RuntimeError("render failed")
            with self.assertRaises(RuntimeError):
                bridge.refresh_http_snapshot(force=True)
            self.assertIs(bridge._http_snapshot, old)
            status, body = get_json(base, "/api/refresh")
            self.assertEqual(status, 200)
            self.assertEqual(body["timestamp_kst"], "first")

    def test_status_omits_html_the_client_already_holds_and_gzips(self):
        self.trees.return_value = ("A" * 4000, "closed", 1, 2)
        bridge.refresh_http_snapshot(force=True)
        with running_bridge() as base:
            _, full = get_json(base, "/api/status")
            self.assertEqual(full["activeTreeHtml"], "A" * 4000)
            h = full["hashes"]
            _, body = get_json(base, f"/api/status?h_active={h['active']}&h_closed={h['closed']}&h_pc=stale")
            self.assertIsNone(body["activeTreeHtml"])
            self.assertIsNone(body["closedTreeHtml"])
            self.assertTrue(body["activeTreeUnchanged"] and body["closedTreeUnchanged"])
            self.assertEqual(body["pcTelemetryHtml"], "pc")
            self.assertNotIn("pcTelemetryUnchanged", body)
            self.assertEqual(body["hashes"], h)
            req = urllib.request.Request(base + "/api/status", headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=5) as response:
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                raw = response.read()
            self.assertEqual(json.loads(bridge.gzip.decompress(raw))["activeTreeHtml"], "A" * 4000)
            self.assertLess(len(raw), 1024)
        self.assertEqual(self.collect.call_count, 1)

    def test_disconnected_clients_are_silent_at_handle_and_finish(self):
        handler = object.__new__(bridge.BridgeHandler)
        for method in ("handle", "finish"):
            for error in (BrokenPipeError, ConnectionResetError):
                with mock.patch.object(http.server.BaseHTTPRequestHandler, method, side_effect=error):
                    getattr(handler, method)()


class WidgetSyncTests(unittest.TestCase):
    def test_only_recent_conversations_and_changed_content_are_written(self):
        with tempfile.TemporaryDirectory() as brain, mock.patch.object(bridge, "BRAIN_DIR", brain), \
                mock.patch.object(bridge, "_written_widget_hashes", {}), \
                mock.patch.object(bridge.ruw, "pick_template", return_value=("t", "src")), \
                mock.patch.object(bridge.ruw, "inject_live_data", return_value="html"), \
                mock.patch.object(bridge.ruw, "compact_widget_html", side_effect=lambda h: h), \
                mock.patch.object(bridge, "_atomic_write") as write:
            fresh, stale = Path(brain, "fresh"), Path(brain, "stale")
            fresh.mkdir()
            stale.mkdir()
            old = bridge.time.time() - bridge.WIDGET_SYNC_RECENT_SEC - 60
            os.utime(stale, (old, old))
            now = bridge.time.time()
            self.assertEqual([d for d in bridge.list_conversation_dirs() if bridge._recently_active(d, now)],
                             [str(fresh)])
            target = str(fresh / bridge.WIDGET_FILENAME)
            bridge.write_widget_to_all_conversations({}, target_dirs=[str(fresh)])
            Path(target).write_text("html")
            bridge.write_widget_to_all_conversations({}, target_dirs=[str(fresh)])
            self.assertEqual([c.args[0] for c in write.call_args_list].count(target), 1)


if __name__ == "__main__":
    unittest.main()
