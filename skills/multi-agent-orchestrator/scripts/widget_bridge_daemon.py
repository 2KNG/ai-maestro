#!/usr/bin/env python3
"""
HTTP Bridge Daemon (0.0.0.0:18765) for standalone dashboards and <agent-embed> widgets.
Supports:
1. GET /api/refresh?scope=current|all
   - Calls check_all_usage.py + render_usage_widget.py live
   - Returns fresh ACCOUNTS JSON (live quotas for Antigravity, Codex, Claude) + persisted state
   - Updates usage_widget.html on disk
2. POST /api/import
   - Imports an external/archived Codex or Antigravity session into the current workspace (~/dev):
     (a) Creates/bridges a real Antigravity Cascade trajectory in file://$HOME/dev via Language Server StartCascade + SendUserCascadeMessage so it immediately appears in the LEFT sidebar of Antigravity IDE!
     (b) Updates ~/.codex/state_5.sqlite (archived=0, updated_at=now) so archived Codex sessions are restored.
     (c) Saves imported state to ~/.gemini/antigravity/widget_state.json.
3. POST /api/state
   - Persists dismissedSet, importedSet,importedCascades, and currentScope on disk so sandboxed <agent-embed> iframes never lose state across reloads.
4. GET /api/route?difficulty=high|medium|low
   - Returns a routing recommendation from fresh quota data without changing session or widget state.
"""
import gzip
import hashlib
import http.server
import json
import os
import re
import shutil
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
import subscription_wake

HOME_DIR = os.path.expanduser("~")
DEFAULT_DEV_DIR = os.path.join(HOME_DIR, "dev")
STATE_FILE = os.path.expanduser("~/.gemini/antigravity/widget_state.json")
WIDGET_HTML = os.path.join(HOME_DIR, ".gemini/antigravity/brain/521b781e-43da-4647-b411-585e65e5f002/usage_widget.html")
PORT = 18765
# Widget monospace font, served so remote (proxied) browsers get D2Coding Ligature too.
FONT_DIR = os.path.join(HOME_DIR, ".local/share/fonts/D2CodingNerdFont")
FONT_ROUTES = {
    "/fonts/D2CodingLigature-Regular.ttf": "D2CodingLigatureNerdFont-Regular.ttf",
    "/fonts/D2CodingLigature-Bold.ttf": "D2CodingLigatureNerdFont-Bold.ttf",
}

# Auto-provisioning of usage_widget.html into every conversation directory
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")
WIDGET_FILENAME = "usage_widget.html"
SCAN_INTERVAL_SEC = 6
# Only conversations touched this recently get a widget provisioned on disk.
WIDGET_SYNC_RECENT_SEC = 86400
GZIP_MIN_BYTES = 1024
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

_refresh_lock = threading.RLock()
_collect_lock = threading.Lock()
_refresh_requested = threading.Event()
_http_snapshot = None
_bootstrap_html = b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>AOH</title><p>Dashboard warming up...</p>'
_bootstrap_status = b'{"ok":true,"warmingUp":true,"accounts":[],"activeTreeHtml":"","closedTreeHtml":"","activeCount":0,"closedCount":0,"state":{}}'
# Large HTML fields the client may already hold: (hash key, body field, unchanged flag).
HASHED_HTML_FIELDS = (("active", "activeTreeHtml", "activeTreeUnchanged"),
                      ("closed", "closedTreeHtml", "closedTreeUnchanged"),
                      ("pc", "pcTelemetryHtml", "pcTelemetryUnchanged"))
_written_widget_hashes: dict[str, str] = {}
HARNESS_STATE_DIR = "/tmp/maestro_harness_state"
HARNESS_STATE_MAX_AGE_SEC = 7 * 86400
PRUNE_INTERVAL_SEC = 3600
_last_prune = 0.0


def prune_stale_files(directory: str = HARNESS_STATE_DIR, max_age: float = HARNESS_STATE_MAX_AGE_SEC,
                      now: float | None = None) -> int:
    """Delete regular files older than max_age so per-session hook state cannot pile up."""
    cutoff = (time.time() if now is None else now) - max_age
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def refresh_http_snapshot(force=False):
    """Singleflight build with atomic publication and lock-free stale readers."""
    global _http_snapshot
    observed = _http_snapshot
    with _refresh_lock:
        current = _http_snapshot
        if current is not observed:
            return current
        if not force and current and time.monotonic() - current["built_at"] < HOT_CACHE_TTL_SEC:
            return current
        data = fetch_usage_data(force=force)
        st = load_state()
        template, _ = ruw.pick_template(WIDGET_HTML, SCRIPT_DIR)
        html = ruw.inject_live_data(template, data) if template else ruw.build_widget_html(data)
        active, closed, active_count, closed_count = ruw.build_active_and_closed_trees_html(data, st)
        body = {
            "ok": True, "timestamp_kst": data.get("timestamp_kst"),
            "recommended_routing": data.get("recommended_routing"),
            "accounts": ruw.build_accounts(data),
            "pcTelemetryHtml": ruw.build_optional_pc_telemetry_html(open_drawer=bool(st.get("pcMonOpen", False))) if hasattr(ruw, "build_optional_pc_telemetry_html") else "",
            "activeTreeHtml": active, "closedTreeHtml": closed,
            "activeCount": active_count, "closedCount": closed_count,
        }
        body["hashes"] = {key: _content_hash(body[field]) for key, field, _ in HASHED_HTML_FIELDS}
        # Scope/age filtering is client-side; trees contain all scope/age attributes.
        # Payloads are encoded lazily per (scope, days, fields the client already holds).
        snapshot = {"html": html.encode("utf-8"), "body": body, "state": st, "status": {},
                    "scope": st.get("scope", "current"),
                    "closedDays": st.get("closedDays", DEFAULT_CLOSED_DAYS),
                    "data": data, "built_at": time.monotonic()}
        _http_snapshot = snapshot
        return snapshot


def status_snapshot_payload(snapshot, query, gzip_ok=False):
    """JSON bytes for /api/status; HTML fields matching the client's h_* hashes are sent as null.

    Returns (payload, gzipped). Encodings are memoized on the immutable snapshot.
    """
    if snapshot is None:
        return _bootstrap_status, False
    scope = query.get("scope", [snapshot["scope"]])[0]
    if scope not in ("current", "all"):
        scope = snapshot["scope"]
    days = _normalize_closed_days(query.get("closedDays", [snapshot["closedDays"]])[0])
    hashes = snapshot["body"]["hashes"]
    unchanged = tuple(key for key, _, _ in HASHED_HTML_FIELDS if query.get(f"h_{key}", [""])[0] == hashes[key])
    gzipped = False
    key = (scope, days, unchanged)
    payload = snapshot["status"].get(key)
    if payload is None:
        body = dict(snapshot["body"], state=dict(snapshot["state"], scope=scope, closedDays=days))
        for hkey, field, flag in HASHED_HTML_FIELDS:
            if hkey in unchanged:
                body[field] = None
                body[flag] = True
        payload = snapshot["status"][key] = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if gzip_ok and len(payload) > GZIP_MIN_BYTES:
        gz_key = key + ("gzip",)
        if gz_key not in snapshot["status"]:
            snapshot["status"][gz_key] = gzip.compress(payload, compresslevel=6)
        payload, gzipped = snapshot["status"][gz_key], True
    return payload, gzipped


def _recently_active(conv_dir: str, now: float) -> bool:
    """A conversation counts as recent if its dir or transcript changed within WIDGET_SYNC_RECENT_SEC."""
    for path in (conv_dir, os.path.join(conv_dir, ".system_generated", "logs", "transcript.jsonl")):
        try:
            if now - os.stat(path).st_mtime <= WIDGET_SYNC_RECENT_SEC:
                return True
        except OSError:
            pass
    return False


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
    """Collect once per concurrent refresh wave and cache data for background work."""
    global _cached_usage_data, _cached_usage_ts
    observed_ts = _cached_usage_ts
    with _collect_lock:
        now = time.time()
        if _cached_usage_data is not None and (
            _cached_usage_ts != observed_ts or (not force and now - _cached_usage_ts <= HOT_CACHE_TTL_SEC)
        ):
            return _cached_usage_data
        data = cau.collect_usage(include_agent_trees=True)
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
    with _refresh_lock:
        rendered_cache: dict[str, str] = {}
        written = 0
        dirs_to_write = target_dirs if target_dirs is not None else list_conversation_dirs()
        for conv_dir in dirs_to_write:
            target = os.path.join(conv_dir, WIDGET_FILENAME)
            meta_target = target + ".metadata.json"
            try:
                template, source = ruw.pick_template(target, SCRIPT_DIR)
                if source not in rendered_cache:
                    rendered = ruw.compact_widget_html(
                        ruw.inject_live_data(template, data) if template else ruw.build_widget_html(data)
                    )
                    rendered_cache[source] = (rendered, _content_hash(rendered))
                rendered, digest = rendered_cache[source]
                if _written_widget_hashes.get(target) == digest and os.path.exists(target):
                    continue
                _atomic_write(target, rendered)
                _written_widget_hashes[target] = digest
                if not os.path.exists(meta_target):
                    _atomic_write(meta_target, meta_json)
                written += 1
            except Exception as e:
                sys.stderr.write(f"widget write failed for {target}: {e}\n")
        _last_full_sync = time.time()
        return written


def widget_sync_loop() -> None:
    """Refresh immutable HTTP snapshots and provision widgets for new conversations."""
    global _last_failure, _last_prune
    while True:
        try:
            requested = _refresh_requested.is_set()
            _refresh_requested.clear()
            now = time.time()
            try:
                snapshot = refresh_http_snapshot(force=requested)
                # Self-throttled to one background pass per 5 minutes.
                subscription_wake.maybe_wake_async(snapshot["data"])
            except Exception as e:
                sys.stderr.write(f"[widget-sync] snapshot refresh failed: {e}\n")
            if now - _last_prune >= PRUNE_INTERVAL_SEC:
                _last_prune = now
                prune_stale_files()
            dirs = list_conversation_dirs()
            missing = [d for d in dirs if not os.path.exists(os.path.join(d, WIDGET_FILENAME))
                       and _recently_active(d, now)]
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
        _refresh_requested.wait(SCAN_INTERVAL_SEC)


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
    st["statusFilter"] = str(st.get("statusFilter") or "all")
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
        _refresh_requested.set()


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


DEV_DIR = os.path.expanduser("~/dev")
ORCA_IDE = os.path.join(HOME_DIR, ".local/opt/orca/orca-ide")
ORCA_WORKSPACES = os.path.join(HOME_DIR, "orca/workspaces")
ORCA_CLI_TIMEOUT_SEC = 8.0
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _orca_cli(*args: str):
    """Runs orca-ide; parsed JSON when --json was passed, True on success otherwise, None on failure."""
    try:
        proc = subprocess.run([ORCA_IDE, *args], capture_output=True, text=True, timeout=ORCA_CLI_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    if "--json" not in args:
        return True
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _orca_terminals(worktree: str = "") -> list[dict]:
    args = ["terminal", "list", "--json"] + (["--worktree", f"path:{worktree}"] if worktree else [])
    data = _orca_cli(*args)
    terminals = ((data or {}).get("result") or {}).get("terminals") if isinstance(data, dict) else None
    return [t for t in terminals or () if isinstance(t, dict) and t.get("handle")]


def _session_orca_hint(ids: list[str]) -> tuple[str, str]:
    """(terminal handle, worktree path) of the Claude session / Codex thread an id names."""
    uuids = {m for i in ids if i for m in _UUID_RE.findall(i)}
    if not uuids:
        return "", ""
    handle = worktree = ""
    try:
        from live_worker_processes import scan_live_worker_processes
        for rec in scan_live_worker_processes():
            if isinstance(rec, dict) and rec.get("session_id") in uuids and rec.get("orca_terminal"):
                handle = rec["orca_terminal"]
                worktree = (rec.get("orca_worktree") or "").rsplit("::", 1)[-1] or rec.get("cwd") or ""
                break
    except Exception:
        pass
    trees = (_cached_usage_data or {}).get("agent_trees") or {}
    for kind in ("claude", "codex"):
        for node in trees.get(kind) or ():
            if isinstance(node, dict) and (node.get("session_id") or node.get("thread_id")) in uuids:
                handle = handle or node.get("orca_terminal_handle") or ""
                worktree = worktree or (node.get("orca_worktree") or "").rsplit("::", 1)[-1] or node.get("cwd") or ""
    return handle, worktree


def _orca_worktree_path(repo: str, hint: str = "") -> str:
    """Existing directory for a session cwd, an absolute path, '<repo>/<worktree>' or a ~/dev repo."""
    for cand in (hint, repo if os.path.isabs(repo or "") else "",
                 os.path.join(ORCA_WORKSPACES, repo) if repo else "",
                 os.path.join(DEV_DIR, repo) if repo else ""):
        if cand and os.path.isdir(cand):
            return os.path.realpath(cand)
    return ""


def _focus_orca_window() -> None:
    """Raises the local Orca desktop window; best effort, never blocks the request."""
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY") or ":0"}
    cmd = "wmctrl -xa orca || xdotool search --class orca windowactivate"
    try:
        subprocess.Popen(["sh", "-c", cmd], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def open_in_orca(node_id: str, cascade_id: str, repo: str, label: str) -> dict:
    """Switches Orca to the pane of a session, or else to its worktree (first pane, then the folder)."""
    handle = cascade_id if (cascade_id or "").startswith("term_") else ""
    hint_handle, hint_path = _session_orca_hint([cascade_id, node_id])
    handle = handle or hint_handle
    worktree = _orca_worktree_path(repo, hint_path)
    terminals = _orca_terminals()
    known = {t["handle"] for t in terminals}
    if handle not in known:
        handle = ""
    if not handle:
        clean = re.sub(r"^\W+", "", label or "").strip()
        in_tree = [t for t in terminals if worktree and os.path.realpath(t.get("worktreePath") or "") == worktree]
        titled = [t for t in in_tree or terminals if clean and len(clean) >= 4 and clean in (t.get("title") or "")]
        # Panes are listed oldest first; a worktree's first pane is the one the user opened.
        pick = (titled or in_tree or _orca_terminals(worktree)) if worktree else titled
        handle = pick[0]["handle"] if pick else ""
    switched = bool(handle) and _orca_cli("terminal", "switch", "--terminal", handle) is True
    if not switched and worktree:
        switched = _orca_cli("file", "open", worktree, "--worktree", f"path:{worktree}", "--focus") is True
    _focus_orca_window()
    return {"ok": switched, "target": "orca", "handle": handle, "worktree": worktree}


def open_in_antigravity(cascade_id: str, *, web=False, repo="") -> dict:
    """Return an exact web route, or request focus from the native host app."""
    result = {"ok": False, "target": "antigravity", "cascadeId": cascade_id,
              "sessionFocused": False}
    if not isinstance(cascade_id, str) or not _UUID_RE.fullmatch(cascade_id):
        return {**result, "error": "invalid_antigravity_session"}
    trees = (_cached_usage_data or {}).get("agent_trees") or {}
    def known(nodes):
        return any(isinstance(node, dict) and (node.get("conversation_id") == cascade_id
            or known(node.get("children") or [])) for node in nodes)
    if not known(trees.get("antigravity") or []) and not os.path.isdir(os.path.join(BRAIN_DIR, cascade_id)):
        return {**result, "error": "antigravity_session_unavailable"}
    if web:
        # Fleet validates its Dashboard frame and builds the node-scoped route.
        # This acknowledges a navigation target, not a loaded conversation.
        return {**result, "ok": True, "antigravityTarget": {"conversationId": cascade_id}}
    def workspace(nodes):
        for node in nodes:
            if node.get("conversation_id") == cascade_id:
                return node.get("cwd") or next(iter(node.get("workspaces") or []), "")
            child = workspace(node.get("children") or [])
            if child:
                return child
        return ""
    candidate = workspace(trees.get("antigravity") or []) or repo
    if isinstance(candidate, str):
        candidate = candidate.removeprefix("file://")
        if not os.path.isabs(candidate):
            candidate = os.path.join(HOME_DIR, "dev", candidate)
        repo = candidate if os.path.isdir(candidate) else ""
    else:
        repo = ""
    executable = os.path.join(HOME_DIR, ".local/bin/antigravity")
    if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        return {**result, "error": "antigravity_app_unavailable"}
    try:
        subprocess.Popen([executable] + ([repo] if repo else []), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return {**result, "error": "antigravity_app_unavailable"}
    try:
        subprocess.run(["wmctrl", "-xa", "antigravity"], timeout=3,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {**result, "ok": True, "accepted": True, "navigation": "host-app-requested",
            "conversationUri": "conversation://" + cascade_id,
            "message": "Antigravity 창 열기를 요청했습니다. 해당 창에서 대화를 선택하세요."}


def open_session_target(node_id, cascade_id, repo, label, *, chat=False, state=None):
    """Keep the stopped Orca runtime out of Antigravity-only navigation."""
    from check_all_usage import antigravity_only_mode
    if antigravity_only_mode():
        target = ((state or {}).get("importedCascades") or {}).get(node_id) or cascade_id
        return open_in_antigravity(target, web=chat, repo=repo)
    return open_in_orca(node_id, cascade_id, repo, label)


def _nvm_bin_dirs() -> list[str]:
    """Node bin directories installed by nvm, newest version first."""
    import glob
    def version(path):
        name = os.path.basename(os.path.dirname(path)).lstrip("v")
        return tuple(int(x) for x in name.split(".") if x.isdigit())
    roots = [os.environ.get("NVM_DIR") or os.path.expanduser("~/.nvm"), os.path.expanduser("~/.config/nvm")]
    dirs = {d for root in roots for d in glob.glob(os.path.join(root, "versions", "node", "*", "bin"))}
    return sorted(dirs, key=version, reverse=True)


def create_antigravity_left_sidebar_cascade(thread_id: str, label: str, cmd_text: str) -> str | None:
    """
    Creates a real Antigravity Cascade conversation in file://$HOME/dev
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
            f"- 원본 경로: `{t_info.get('cwd', DEV_DIR)}`\n"
            f"- 재개 명령어: `{cmd_text}`\n\n"
            f"이 대화창은 위젯에서 [가져오기]를 눌러 현재 워크스페이스(`{DEV_DIR}`) 왼쪽 사이드바 목록으로 가져온 세션입니다. "
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
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _set_cors(self, status=200, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_cors(204)

    def _serve_font(self, path, head_only=False):
        font_path = os.path.join(FONT_DIR, FONT_ROUTES[path])
        try:
            with open(font_path, "rb") as fh:
                body = b"" if head_only else fh.read()
            size = os.path.getsize(font_path)
        except OSError:
            self._set_cors(404, "text/plain; charset=utf-8")
            if not head_only:
                self.wfile.write(b"font not installed")
            return
        self._set_cors(200, "font/ttf", {"Content-Length": str(size),
                                          "Cache-Control": "public, max-age=31536000, immutable"})
        if body:
            self.wfile.write(body)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path in FONT_ROUTES:
            self._serve_font(path, head_only=True)
        else:
            self._set_cors(405, "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in FONT_ROUTES:
            self._serve_font(parsed.path)
            return
        if parsed.path in ("/", "/index.html", "/widget", "/usage_widget.html"):
            snapshot = _http_snapshot
            self._set_cors(200, "text/html; charset=utf-8")
            self.wfile.write(snapshot["html"] if snapshot else _bootstrap_html)
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
                with _collect_lock:
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
            qs = urllib.parse.parse_qs(parsed.query)
            snapshot = _http_snapshot
            if parsed.path == "/api/refresh":
                try:
                    snapshot = refresh_http_snapshot(force=True)
                    dirs = list_conversation_dirs()
                    targets = dirs if qs.get("write_disk", ["0"])[0] == "1" else [
                        d for d in dirs if not os.path.exists(os.path.join(d, WIDGET_FILENAME))]
                    if targets:
                        write_widget_to_all_conversations(snapshot["data"], target_dirs=targets)
                except Exception as e:
                    sys.stderr.write(f"[widget-sync] refresh failed: {e}\n")
                    if snapshot is None:
                        self._set_cors(503)
                        self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
                        return
            gzip_ok = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            payload, gzipped = status_snapshot_payload(snapshot, qs, gzip_ok=gzip_ok)
            headers = {"Content-Length": str(len(payload)), "Vary": "Accept-Encoding", "Cache-Control": "no-store"}
            if gzipped:
                headers["Content-Encoding"] = "gzip"
            self._set_cors(200, headers=headers)
            self.wfile.write(payload)
            return

        if parsed.path == "/api/orca":
            try:
                import orca_bridge
                snap = orca_bridge.snapshot()
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": True, "summary": orca_bridge.summary_line(snap), "orca": snap},
                                            ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
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

        if parsed.path in ("/api/state", "/api/widget-state"):
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

        if parsed.path in ("/api/state", "/api/widget-state"):
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
                if "statusFilter" in body:
                    st["statusFilter"] = str(body["statusFilter"] or "all")
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

            # Create or reuse Antigravity Cascade in left sidebar (~/dev)
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

        if parsed.path == "/api/open-session":
            node_id = body.get("nodeId", "")
            cascade_id = body.get("cascadeId", "")
            thread_id = body.get("threadId", "")
            repo = body.get("repo", "")
            label = body.get("label", node_id)

            st = load_state()
            if not cascade_id and node_id:
                cascade_id = st.get("importedCascades", {}).get(node_id, "")

            # Unarchive codex thread if threadId or thread uuid in nodeId
            if thread_id:
                unarchive_codex_thread(thread_id)
            elif node_id and ("cdx" in node_id or "thread" in node_id):
                m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", node_id)
                if m:
                    unarchive_codex_thread(m.group(1))

            result = open_session_target(node_id, cascade_id, repo, label, chat=body.get("view") == "chat", state=st)
            self._set_cors(200)
            self.wfile.write(
                json.dumps(
                    {
                        **result,
                        "nodeId": node_id,
                        "cascadeId": cascade_id,
                        "message": result.get("message") or (f"'{label}' Antigravity로 전환 중..." if result["ok"] else f"'{label}' 세션을 열지 못했습니다"),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            return

        if parsed.path == "/api/agent-control":
            cid = body.get("cascadeId") or "4500bbbc-e155-49f9-b324-88364ca9b84b"
            reason = body.get("reason", "manual request")
            text = body.get("text") or (
                f"[자동 복구 알림: {reason}] 네트워크 복구 완료. 진행 중이던 작업을 계속 수행해 주세요."
            )
            plan_model = body.get("planModel", "MODEL_PLACEHOLDER_M417")
            addr, token = cau.discover_ls_credentials()
            if not addr:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": "LanguageServer credentials not found"}).encode("utf-8"))
                return
            try:
                msg_payload = {
                    "cascadeId": cid,
                    "items": [{"text": text}],
                    "metadata": {
                        "ideName": "antigravity",
                        "ideVersion": "1.107.0",
                        "extensionName": "antigravity",
                        "extensionVersion": "1.107.0",
                    },
                    "cascadeConfig": {
                        "plannerConfig": {
                            "planModel": plan_model,
                            "conversational": {
                                "plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT",
                                "agenticMode": True,
                            },
                        }
                    },
                }
                req = urllib.request.Request(
                    f"http://{addr}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage",
                    data=json.dumps(msg_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Connect-Protocol-Version": "1",
                        "x-codeium-csrf-token": token or "",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    res_raw = r.read().decode("utf-8")
                    res = json.loads(res_raw) if res_raw else {}
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": True, "res": res}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return
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
            extra_paths = [*_nvm_bin_dirs(), os.path.expanduser("~/.local/bin"), "/usr/local/bin"]
            env["PATH"] = ":".join(extra_paths + [env.get("PATH", "/usr/bin:/bin")])
            codex_bin = shutil.which("codex", path=env["PATH"]) or "codex"
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
            # A thread already open in an Orca pane is switched to; otherwise Orca opens a new pane
            # in the thread's worktree and terminal emulators are only the fallback.
            handle, _ = _session_orca_hint([thread_id])
            if handle and handle in {t["handle"] for t in _orca_terminals()} and \
                    _orca_cli("terminal", "switch", "--terminal", handle) is True:
                _focus_orca_window()
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": True, "target": "orca", "handle": handle, "cmd": run_cmd, "cwd": cwd},
                                            ensure_ascii=False).encode("utf-8"))
                return
            created = _orca_cli("terminal", "create", "--worktree", f"path:{cwd}", "--title", run_cmd[:40],
                                "--command", run_cmd, "--focus", "--json")
            if isinstance(created, dict) and created.get("ok"):
                _focus_orca_window()
                handle = ((created.get("result") or {}).get("terminal") or {}).get("handle") or ""
                self._set_cors(200)
                self.wfile.write(json.dumps({"ok": True, "target": "orca", "handle": handle, "cmd": run_cmd, "cwd": cwd},
                                            ensure_ascii=False).encode("utf-8"))
                return
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
