#!/usr/bin/env python3
"""
HTTP Bridge Daemon (0.0.0.0:18765) for standalone dashboards and <agent-embed> widgets.
Supports:
1. GET /api/refresh?scope=current|all
   - Calls check_all_usage.py + render_usage_widget.py live
   - Returns fresh ACCOUNTS JSON (live quotas for Antigravity, Codex, Claude) + persisted state
   - Updates usage_widget.html on disk
2. POST /api/import
   - Imports an external/archived Codex or Antigravity session into the current workspace (/home/u24c9g5090r64/dev):
     (a) Creates/bridges a real Antigravity Cascade trajectory in file:///home/u24c9g5090r64/dev via Language Server StartCascade + SendUserCascadeMessage so it immediately appears in the LEFT sidebar of Antigravity IDE!
     (b) Updates ~/.codex/state_5.sqlite (archived=0, updated_at=now) so archived Codex sessions are restored.
     (c) Saves imported state to ~/.gemini/antigravity/widget_state.json.
3. POST /api/state
   - Persists dismissedSet, importedSet,importedCascades, and currentScope on disk so sandboxed <agent-embed> iframes never lose state across reloads.
4. GET /api/route?difficulty=high|medium|low
   - Returns a routing recommendation from fresh quota data without changing session or widget state.
"""
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import check_all_usage as cau
import render_usage_widget as ruw

HOME_DIR = os.path.expanduser("~")
DEFAULT_DEV_DIR = os.path.join(HOME_DIR, "dev")
STATE_FILE = os.path.expanduser("~/.gemini/antigravity/widget_state.json")
WIDGET_HTML = os.path.join(HOME_DIR, ".gemini/antigravity/brain/521b781e-43da-4647-b411-585e65e5f002/usage_widget.html")
PORT = 18765

# Auto-provisioning of usage_widget.html into every conversation directory
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")
WIDGET_FILENAME = "usage_widget.html"
SCAN_INTERVAL_SEC = 3
FAILURE_BACKOFF_SEC = 15
ALLOWED_CLOSED_DAYS = (7, 30)
DEFAULT_CLOSED_DAYS = 7

# Re-entrant so POST /api/state can hold it across load -> modify -> save_state().
_sync_lock = threading.RLock()
_last_full_sync = 0.0
_last_failure = 0.0
_cached_usage_data: dict | None = None
_cached_usage_ts = 0.0
HOT_CACHE_TTL_SEC = 8.0


def list_conversation_dirs() -> list[str]:
    """All non-hidden directories directly under BRAIN_DIR (conversation IDs are UUID-like)."""
    try:
        entries = os.listdir(BRAIN_DIR)
    except OSError:
        return []
    out = []
    for name in entries:
        if name.startswith("."):
            continue
        path = os.path.join(BRAIN_DIR, name)
        if os.path.isdir(path):
            out.append(path)
    return sorted(out)


def fetch_usage_data(force: bool = False) -> dict:
    """Collect usage in-process and maintain an in-memory hot cache for <1ms /api/status responses."""
    global _cached_usage_data, _cached_usage_ts
    now = time.time()
    if not force and _cached_usage_data is not None and (now - _cached_usage_ts) <= HOT_CACHE_TTL_SEC:
        return _cached_usage_data
    data = cau.collect_usage(include_agent_trees=True)
    with _sync_lock:
        _cached_usage_data = data
        _cached_usage_ts = time.time()
    return data


def _atomic_write(target: str, content: str) -> None:
    d = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(prefix=".usage_widget.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_widget_to_all_conversations(data: dict, target_dirs: list[str] | None = None) -> int:
    """Render usage_widget.html from live data and atomically write it into target conversation dirs."""
    global _last_full_sync
    meta_json = json.dumps(
        {
            "summary": "AOH dual-ring + clock-pie quota widget and narrow-safe multi-agent execution tree.",
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "userFacing": True,
        },
        ensure_ascii=False,
        indent=2,
    )
    with _sync_lock:
        rendered_cache: dict[str, str] = {}
        written = 0
        dirs_to_write = target_dirs if target_dirs is not None else list_conversation_dirs()
        for conv_dir in dirs_to_write:
            target = os.path.join(conv_dir, WIDGET_FILENAME)
            meta_target = target + ".metadata.json"
            try:
                template, source = ruw.pick_template(target, SCRIPT_DIR)
                if source not in rendered_cache:
                    rendered_cache[source] = ruw.compact_widget_html(
                        ruw.inject_live_data(template, data) if template else ruw.build_widget_html(data)
                    )
                _atomic_write(target, rendered_cache[source])
                if not os.path.exists(meta_target):
                    _atomic_write(meta_target, meta_json)
                written += 1
            except Exception as e:
                sys.stderr.write(f"widget write failed for {target}: {e}\n")
        _last_full_sync = time.time()
        return written


def widget_sync_loop() -> None:
    """Background loop: keep in-memory hot cache warm and provision widgets ONLY for new conversations lacking usage_widget.html."""
    global _last_failure
    while True:
        try:
            now = time.time()
            if _cached_usage_data is None or (now - _cached_usage_ts) >= 6.0:
                try:
                    fetch_usage_data(force=True)
                except Exception:
                    pass
            dirs = list_conversation_dirs()
            missing = [d for d in dirs if not os.path.exists(os.path.join(d, WIDGET_FILENAME))]
            backing_off = (now - _last_failure) < FAILURE_BACKOFF_SEC
            if missing and not backing_off:
                try:
                    data = fetch_usage_data(force=False)
                    ruw.build_accounts(data)  # validate data shape before writing
                    n = write_widget_to_all_conversations(data, target_dirs=missing)
                    print(f"[widget-sync] provisioned {len(missing)} new conversation(s); wrote {n} widget(s)",
                          flush=True)
                except Exception as e:
                    _last_failure = time.time()
                    sys.stderr.write(f"[widget-sync] sync failed: {e}\n")
        except Exception as e:
            sys.stderr.write(f"[widget-sync] loop error: {e}\n")
        time.sleep(SCAN_INTERVAL_SEC)


def _unique_str_list(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(v) for v in value if isinstance(v, (str, int)) and str(v)))


def _normalize_closed_days(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CLOSED_DAYS
    return v if v in ALLOWED_CLOSED_DAYS else DEFAULT_CLOSED_DAYS


def _normalize_state(st) -> dict:
    if not isinstance(st, dict):
        st = {}
    cascades = st.get("importedCascades")
    st["dismissed"] = _unique_str_list(st.get("dismissed"))
    st["pinned"] = _unique_str_list(st.get("pinned"))
    st["imported"] = _unique_str_list(st.get("imported"))
    st["importedCascades"] = cascades if isinstance(cascades, dict) else {}
    st["closedDays"] = _normalize_closed_days(st.get("closedDays", DEFAULT_CLOSED_DAYS))
    st["scope"] = st.get("scope") if st.get("scope") in ("current", "all") else "current"
    st["tab"] = st.get("tab") if isinstance(st.get("tab"), str) and st.get("tab") else "active"
    # UI cache: persisted so re-rendered widgets keep the user's expand/collapse choices.
    st["density"] = st.get("density") if st.get("density") in ("compact", "expanded") else "compact"
    st["allCollapsed"] = bool(st.get("allCollapsed", False))
    st["showBgDaemons"] = bool(st.get("showBgDaemons", True))
    st["pcMonOpen"] = bool(st.get("pcMonOpen", False))
    st["collapsedBranches"] = _unique_str_list(st.get("collapsedBranches"))
    st["openDetails"] = _unique_str_list(st.get("openDetails"))
    return st


def load_state() -> dict:
    with _sync_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return _normalize_state(json.load(f))
            except Exception:
                pass
        return _normalize_state({})


def save_state(st: dict) -> None:
    with _sync_lock:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        _atomic_write(STATE_FILE, json.dumps(_normalize_state(st), ensure_ascii=False, indent=2) + "\n")


def get_codex_thread_summary(thread_id: str) -> dict:
    db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    info = {"thread_id": thread_id, "title": thread_id, "cwd": os.path.expanduser("~/dev"), "model": "gpt-6.1-sol"}
    if not os.path.exists(db_path):
        return info
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        row = conn.execute(
            "SELECT title, cwd, model_provider, rollout_path FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row:
            info["title"] = (row[0] or thread_id).strip()
            info["cwd"] = row[1] or info["cwd"]
            info["rollout_path"] = row[3]
        conn.close()
    except Exception:
        pass
    return info


def unarchive_codex_thread(thread_id: str) -> None:
    db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        now_ts = int(time.time())
        conn.execute(
            "UPDATE threads SET archived = 0, updated_at = ? WHERE id = ?",
            (now_ts, thread_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def create_antigravity_left_sidebar_cascade(thread_id: str, label: str, cmd_text: str) -> str | None:
    """
    Creates a real Antigravity Cascade conversation in file:///home/u24c9g5090r64/dev
    so that the imported session immediately appears in the Antigravity IDE LEFT sidebar!
    """
    addr, token = cau.discover_ls_credentials()
    if not addr:
        return None

    t_info = get_codex_thread_summary(thread_id) if thread_id else {"title": label, "cwd": os.path.expanduser("~/dev")}
    short_title = label or t_info.get("title", "Imported Session")
    if len(short_title) > 60:
        short_title = short_title[:57] + "..."

    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-codeium-csrf-token": token or "",
    }

    start_payload = {
        "metadata": {
            "ideName": "antigravity",
            "apiKey": "",
            "locale": "ko",
            "ideVersion": "2.23.1",
            "extensionName": "antigravity",
        },
        "source": "CORTEX_TRAJECTORY_SOURCE_VSCODE",
        "workspaceUris": [f"file://{os.path.expanduser('~/dev')}"],
    }
    try:
        req = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/StartCascade",
            data=json.dumps(start_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
        cid = res.get("cascadeId")
        if not cid:
            return None

        prompt_text = (
            f"[가져온 세션 · {short_title}]\n"
            f"- 원본 스레드 ID: `{thread_id or 'N/A'}`\n"
            f"- 원본 경로: `{t_info.get('cwd', '/home/u24c9g5090r64/dev')}`\n"
            f"- 재개 명령어: `{cmd_text}`\n\n"
            f"이 대화창은 위젯에서 [가져오기]를 눌러 현재 워크스페이스(`/home/u24c9g5090r64/dev`) 왼쪽 사이드바 목록으로 가져온 세션입니다. "
            f"원본 스레드의 요약만 2줄로 출력하고 대기하세요."
        )
        msg_payload = {
            "cascadeId": cid,
            "items": [{"text": prompt_text}],
            "metadata": start_payload["metadata"],
            "cascadeConfig": {
                "plannerConfig": {
                    "planModel": "MODEL_PLACEHOLDER_M402",
                    "conversational": {
                        "plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT",
                        "agenticMode": True,
                    },
                }
            },
        }
        req2 = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage",
            data=json.dumps(msg_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as r2:
            r2.read()
        return cid
    except Exception as e:
        sys.stderr.write(f"StartCascade error: {e}\n")
        return None


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def _set_cors(self, status=200, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_cors(204)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/widget", "/usage_widget.html"):
            try:
                data = fetch_usage_data(force=False)
                template, _ = ruw.pick_template(WIDGET_HTML, SCRIPT_DIR)
                html = ruw.inject_live_data(template, data) if template else ruw.build_widget_html(data)
                payload = html.encode("utf-8")
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
                return
            self._set_cors(200, "text/html; charset=utf-8")
            self.wfile.write(payload)
            return

        if parsed.path == "/api/route":
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            difficulty = query.get("difficulty", ["high"])[0]
            if difficulty not in ("high", "medium", "low"):
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"ok": False, "error": "difficulty must be high, medium, or low"}).encode("utf-8")
                )
                return
            try:
                data = cau.collect_usage(include_agent_trees=False)
                routing = cau.select_best_pool(data, difficulty=difficulty)
                self._set_cors(200)
                self.wfile.write(
                    json.dumps(
                        {
                            "ok": True,
                            "timestamp_kst": data.get("timestamp_kst"),
                            "recommended_routing": routing,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if parsed.path in ("/api/refresh", "/api/status"):
            try:
                is_refresh = parsed.path == "/api/refresh"
                qs = urllib.parse.parse_qs(parsed.query)
                write_disk = qs.get("write_disk", ["0"])[0] == "1"
                data = fetch_usage_data(force=is_refresh)
                accounts = ruw.build_accounts(data)
                st = load_state()
                active_html, closed_html, act_cnt, cls_cnt = ruw.build_active_and_closed_trees_html(data, st)
                pc_html = ruw.build_optional_pc_telemetry_html(open_drawer=bool(st.get("pcMonOpen", False))) if hasattr(ruw, "build_optional_pc_telemetry_html") else ""
                # Only write missing directories on /api/refresh unless write_disk=1 is explicitly requested,
                # so open artifact iframes are updated in-place via JSON without triggering IDE file-watcher reloads.
                if is_refresh:
                    try:
                        dirs = list_conversation_dirs()
                        targets = dirs if write_disk else [d for d in dirs if not os.path.exists(os.path.join(d, WIDGET_FILENAME))]
                        if targets:
                            write_widget_to_all_conversations(data, target_dirs=targets)
                    except Exception as e:
                        sys.stderr.write(f"[widget-sync] refresh write failed: {e}\n")
                self._set_cors(200)
                self.wfile.write(
                    json.dumps(
                        {
                            "ok": True,
                            "timestamp_kst": data.get("timestamp_kst"),
                            "recommended_routing": data.get("recommended_routing"),
                            "accounts": accounts,
                            "pcTelemetryHtml": pc_html,
                            "activeTreeHtml": active_html,
                            "closedTreeHtml": closed_html,
                            "activeCount": act_cnt,
                            "closedCount": cls_cnt,
                            "state": st,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/pc":
            try:
                import pc_telemetry_ext as pc_ext
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": True, "pc": pc_ext.collect_pc_telemetry(force=True)}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": False, "enabled": False, "error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/state":
            self._set_cors(200)
            self.wfile.write(json.dumps({"ok": True, "state": load_state()}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/logs/tail":
            import maestro_memory
            qs = urllib.parse.parse_qs(parsed.query)
            provider = (qs.get("provider", ["codex"])[0] or "codex").strip()
            sid = (qs.get("id", [""])[0] or "").strip()
            max_lines = min(60, max(5, int((qs.get("lines", ["20"])[0] or "20"))))
            res = maestro_memory.extract_session_log_tail(provider, sid, max_lines=max_lines)
            self._set_cors(200)
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/memory":
            import maestro_memory
            qs = urllib.parse.parse_qs(parsed.query)
            repo = (qs.get("repo", [""])[0] or "").strip() or None
            branch = (qs.get("branch", [""])[0] or "").strip() or None
            limit = min(20, max(1, int((qs.get("limit", ["5"])[0] or "5"))))
            notes = maestro_memory.get_recent_handover_notes(repo=repo, branch=branch, max_age_seconds=21600, limit=limit)
            self._set_cors(200)
            self.wfile.write(json.dumps({"ok": True, "notes": notes}, ensure_ascii=False).encode("utf-8"))
            return

        self._set_cors(404)
        self.wfile.write(b'{"ok":false,"error":"not found"}')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            body = json.loads(body_raw)
        except Exception:
            body = {}

        if parsed.path == "/api/memory":
            import maestro_memory
            res = maestro_memory.save_handover_note(
                cwd=body.get("cwd") or os.path.expanduser("~/dev"),
                pool=body.get("pool") or "orchestrator",
                model=body.get("model") or "gemini-4-argon",
                title=body.get("title") or "Handover",
                summary=body.get("summary") or "",
                modified_files=body.get("modified_files") if isinstance(body.get("modified_files"), list) else None,
                next_steps=body.get("next_steps") or "",
                session_id=body.get("session_id") or "",
            )
            self._set_cors(200)
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/state":
            if not isinstance(body, dict):
                body = {}
            with _sync_lock:
                st = load_state()
                if "dismissed" in body:
                    st["dismissed"] = _unique_str_list(body["dismissed"])
                # Incremental ops are applied after any full list so stale iframes can't wipe entries.
                if "addDismissed" in body:
                    add = body["addDismissed"]
                    add = [add] if isinstance(add, str) else add
                    st["dismissed"] = _unique_str_list(st["dismissed"] + _unique_str_list(add))
                if "removeDismissed" in body:
                    rem = body["removeDismissed"]
                    rem = set(_unique_str_list([rem] if isinstance(rem, str) else rem))
                    st["dismissed"] = [d for d in st["dismissed"] if d not in rem]
                if "imported" in body:
                    st["imported"] = _unique_str_list(body["imported"])
                if "importedCascades" in body and isinstance(body["importedCascades"], dict):
                    st["importedCascades"].update(body["importedCascades"])
                if "pinned" in body:
                    st["pinned"] = _unique_str_list(body["pinned"])
                if "closedDays" in body:
                    st["closedDays"] = _normalize_closed_days(body["closedDays"])
                if "scope" in body:
                    st["scope"] = body["scope"]
                if "tab" in body:
                    st["tab"] = body["tab"]
                if "density" in body:
                    st["density"] = body["density"]
                if "allCollapsed" in body:
                    st["allCollapsed"] = bool(body["allCollapsed"])
                if "showBgDaemons" in body:
                    st["showBgDaemons"] = bool(body["showBgDaemons"])
                if "pcMonOpen" in body:
                    st["pcMonOpen"] = bool(body["pcMonOpen"])
                if "collapsedBranches" in body:
                    st["collapsedBranches"] = _unique_str_list(body["collapsedBranches"])
                if "openDetails" in body:
                    st["openDetails"] = _unique_str_list(body["openDetails"])
                save_state(st)
                st = _normalize_state(st)
            self._set_cors(200)
            self.wfile.write(json.dumps({"ok": True, "state": st}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/import":
            node_id = body.get("nodeId", "")
            cmd_text = body.get("cmdText", "")
            label = body.get("label", node_id)
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", cmd_text)
            thread_id = m.group(1) if m else ""

            st = load_state()
            if node_id and node_id not in st["imported"]:
                st["imported"].append(node_id)
            st["scope"] = "current"

            if thread_id:
                unarchive_codex_thread(thread_id)

            # Create or reuse Antigravity Cascade in left sidebar (/home/u24c9g5090r64/dev)
            cid = st.get("importedCascades", {}).get(node_id)
            if not cid:
                cid = create_antigravity_left_sidebar_cascade(thread_id, label, cmd_text)
                if cid:
                    st.setdefault("importedCascades", {})[node_id] = cid

            save_state(st)
            self._set_cors(200)
            self.wfile.write(
                json.dumps(
                    {
                        "ok": True,
                        "nodeId": node_id,
                        "threadId": thread_id,
                        "cascadeId": cid,
                        "conversationUri": f"conversation://{cid}" if cid else None,
                        "state": st,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            return

        if parsed.path == "/api/codex-exec":
            prompt = (body.get("prompt") or "").strip()
            thread_id = (body.get("threadId") or "").strip()
            effort = (body.get("effort") or "high").strip()
            tier = (body.get("tier") or "priority").strip()
            if not prompt:
                self._set_cors(400)
                self.wfile.write(json.dumps({"ok": False, "error": "prompt is required"}).encode("utf-8"))
                return
            t_info = get_codex_thread_summary(thread_id) if thread_id else {"cwd": body.get("cwd") or os.path.expanduser("~/dev")}
            cwd = body.get("cwd") or t_info.get("cwd") or os.path.expanduser("~/dev")
            if not os.path.isdir(cwd):
                cwd = os.path.expanduser("~/dev")
            repo = os.path.basename(cwd.rstrip("/")) or "dev"
            clean_first = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", prompt.splitlines()[0]).strip()[:42]
            short_title = f"✦ {clean_first}"
            tagged_prompt = prompt if prompt.lstrip().startswith("✦") else f"{short_title}\n\n{prompt}"
            env = os.environ.copy()
            extra_paths = [
                "/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin",
                os.path.expanduser("~/.local/bin"),
                "/usr/local/bin",
            ]
            env["PATH"] = ":".join(extra_paths + [env.get("PATH", "/usr/bin:/bin")])
            codex_bin = "/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin/codex"
            if not os.path.exists(codex_bin):
                codex_bin = "codex"
            try:
                if thread_id:
                    unarchive_codex_thread(thread_id)
                    cmd = [
                        codex_bin,
                        "exec",
                        "resume",
                        thread_id,
                        tagged_prompt,
                        "--dangerously-bypass-approvals-and-sandbox",
                        "-m",
                        "gpt-6.1-sol",
                        "-c",
                        f'model_reasoning_effort="{effort}"',
                        "-c",
                        f'service_tier="{tier}"',
                    ]
                    proc = subprocess.Popen(
                        cmd,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    mode = "resume"
                else:
                    cmd = [
                        os.path.expanduser("~/.local/bin/maestro"),
                        "worker-start",
                        "--pool",
                        "codex",
                        "--effort",
                        effort,
                        "--tier",
                        tier,
                        "--cwd",
                        cwd,
                        "--spec",
                        tagged_prompt,
                    ]
                    proc = subprocess.Popen(
                        cmd,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    mode = "new"
                self._set_cors(200)
                self.wfile.write(
                    json.dumps(
                        {
                            "ok": True,
                            "mode": mode,
                            "threadId": thread_id or None,
                            "title": short_title,
                            "cwd": cwd,
                            "pid": proc.pid,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/open-terminal":
            cmd_text = (body.get("cmdText") or "").strip()
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", cmd_text)
            thread_id = m.group(1) if m else (body.get("threadId") or "").strip()
            t_info = get_codex_thread_summary(thread_id) if thread_id else {"cwd": os.path.expanduser("~/dev")}
            cwd = body.get("cwd") or t_info.get("cwd") or os.path.expanduser("~/dev")
            if not os.path.isdir(cwd):
                cwd = os.path.expanduser("~/dev")
            run_cmd = cmd_text or (f"codex resume {thread_id}" if thread_id else "codex")
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")
            launched = False
            for term_bin, term_args in [
                ("gnome-terminal", ["gnome-terminal", f"--working-directory={cwd}", "--", "bash", "-ic", f"{run_cmd}; exec bash"]),
                ("x-terminal-emulator", ["x-terminal-emulator", "-e", f"bash -ic 'cd {cwd} && {run_cmd}; exec bash'"]),
            ]:
                if subprocess.call(["which", term_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                    try:
                        subprocess.Popen(term_args, cwd=cwd, env=env, start_new_session=True)
                        launched = True
                        break
                    except Exception:
                        pass
            self._set_cors(200)
            self.wfile.write(
                json.dumps({"ok": True, "launched": launched, "cmd": run_cmd, "cwd": cwd}, ensure_ascii=False).encode("utf-8")
            )
            return

        self._set_cors(404)
        self.wfile.write(b'{"ok":false,"error":"not found"}')

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), BridgeHandler)
    print(f"Widget bridge daemon listening on 0.0.0.0:{PORT}; dashboard: http://127.0.0.1:{PORT}", flush=True)
    threading.Thread(target=widget_sync_loop, name="widget-sync", daemon=True).start()
    server.serve_forever()
