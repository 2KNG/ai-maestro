#!/usr/bin/env python3
"""
maestro_memory.py — Mod 5 (Shared Cross-Agent Handover Memory in WAL SQLite)
               & Mod 6 (On-Demand Live Log Tail Extractor + Quiet Long-Task Notifications)

Design guarantees (from Pre-Mortem Audit):
1. Uses ~/.gemini/antigravity/maestro_memory.sqlite with WAL mode and busy_timeout=5000.
   Migrates the legacy orca_memory.sqlite with SQLite backup, including WAL records.
   Never touches ~/.codex/state_5.sqlite for writes.
2. Prevents stale context pollution: handover lookup filters strictly by (repo, branch) and max_age_seconds (default 2h),
   capped to 5 concise lines / 600 chars.
3. On-demand log tail reads only the last N KB of rollout .jsonl or Antigravity trajectory steps when requested by the UI.
4. Desktop notifications fire at most once per (session_id, completed_ts) and only for tasks >= min_elapsed_sec (default 60s).
"""
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.request
from datetime import timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
MEMORY_DB_PATH = os.path.expanduser("~/.gemini/antigravity/maestro_memory.sqlite")
LEGACY_MEMORY_DB_PATH = os.path.expanduser("~/.gemini/antigravity/orca_memory.sqlite")


def _migrate_legacy_db() -> None:
    """Publish a complete WAL-aware snapshot once; preserve both existing databases."""
    if os.path.exists(MEMORY_DB_PATH) or not os.path.exists(LEGACY_MEMORY_DB_PATH):
        return
    fd, snapshot = tempfile.mkstemp(prefix=".maestro-memory-", dir=os.path.dirname(MEMORY_DB_PATH))
    os.close(fd)
    try:
        source = sqlite3.connect(Path(LEGACY_MEMORY_DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        try:
            os.link(snapshot, MEMORY_DB_PATH)
        except FileExistsError:
            pass  # Another process published its snapshot first.
    finally:
        os.unlink(snapshot)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(MEMORY_DB_PATH), exist_ok=True)
    _migrate_legacy_db()
    conn = sqlite3.connect(MEMORY_DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handover_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            branch TEXT NOT NULL,
            cwd TEXT NOT NULL,
            pool TEXT NOT NULL,
            model TEXT NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_handover_repo_branch_ts ON handover_notes(repo, branch, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notified_completions (
            session_id TEXT NOT NULL,
            completed_at INTEGER NOT NULL,
            notified_at INTEGER NOT NULL,
            PRIMARY KEY (session_id, completed_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            pool TEXT NOT NULL,
            model TEXT NOT NULL,
            effort TEXT NOT NULL,
            tier TEXT NOT NULL,
            est_sec REAL,
            actual_sec REAL,
            est_tokens INTEGER,
            actual_net_tokens INTEGER,
            actual_gross_tokens INTEGER,
            ok INTEGER NOT NULL DEFAULT 1,
            is_valid INTEGER NOT NULL DEFAULT 1,
            excluded_reason TEXT DEFAULT '',
            title TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_model_effort ON worker_telemetry(model, effort, tier, is_valid)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_session ON worker_telemetry(session_id)"
    )
    return conn


def detect_repo_and_branch(cwd: str) -> tuple[str, str]:
    abs_cwd = os.path.abspath(cwd or os.path.expanduser("~/dev"))
    repo = os.path.basename(abs_cwd.rstrip("/")) or "dev"
    branch = "default"
    try:
        out = subprocess.check_output(
            ["git", "-C", abs_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).strip()
        if out:
            branch = out
    except Exception:
        pass
    return repo, branch


_git_repo_and_branch = detect_repo_and_branch


def save_handover_note(
    cwd: str,
    pool: str,
    model: str,
    title: str,
    summary: str,
    session_id: str = "manual",
    modified_files: list[str] | None = None,
    next_steps: str = "",
) -> dict:
    def _strip_chinese_preamble(text: str) -> str:
        if not text:
            return ""
        return re.sub(r"用到的工具调用如下[^\n]*", "", text)

    clean_summary = "\n".join(
        line.strip()
        for line in _strip_chinese_preamble(summary).strip().splitlines()
        if line.strip()
    )
    title = _strip_chinese_preamble(title)
    if modified_files:
        clean_summary += f"\nFiles: {', '.join(modified_files[:6])}"
    if next_steps:
        clean_summary += f"\nNext: {_strip_chinese_preamble(next_steps).strip()[:120]}"
    if len(clean_summary) > 600:
        clean_summary = clean_summary[:597] + "..."
    if not clean_summary:
        return {"ok": False, "error": "empty summary"}
    repo, branch = detect_repo_and_branch(cwd)
    now_ts = int(time.time())
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO handover_notes (repo, branch, cwd, pool, model, session_id, title, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    branch,
                    os.path.abspath(cwd or os.path.expanduser("~/dev")),
                    pool or "unknown",
                    model or "unknown",
                    session_id or "manual",
                    (title or "Task Handover")[:120],
                    clean_summary,
                    now_ts,
                ),
            )
            # Prune old notes beyond last 100 entries to keep DB tiny
            conn.execute(
                """
                DELETE FROM handover_notes
                WHERE id NOT IN (SELECT id FROM handover_notes ORDER BY created_at DESC LIMIT 100)
                """
            )
    finally:
        conn.close()
    return {
        "ok": True,
        "repo": repo,
        "branch": branch,
        "pool": pool,
        "model": model,
        "session_id": session_id,
        "title": title,
        "summary": clean_summary,
        "created_at": now_ts,
    }


def get_recent_handover_notes(
    cwd: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    max_age_seconds: int = 7200,
    limit: int = 3,
) -> list[dict]:
    if cwd and not repo:
        repo, det_branch = detect_repo_and_branch(cwd)
        if not branch:
            branch = det_branch
    cutoff = int(time.time()) - max(60, int(max_age_seconds))
    conn = _connect()
    try:
        if repo and branch:
            rows = conn.execute(
                """
                SELECT id, repo, branch, cwd, pool, model, session_id, title, summary, created_at
                FROM handover_notes
                WHERE repo = ? AND branch = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (repo, branch, cutoff, limit),
            ).fetchall()
        elif repo:
            rows = conn.execute(
                """
                SELECT id, repo, branch, cwd, pool, model, session_id, title, summary, created_at
                FROM handover_notes
                WHERE repo = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (repo, cutoff, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, repo, branch, cwd, pool, model, session_id, title, summary, created_at
                FROM handover_notes
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
    finally:
        conn.close()

    notes = []
    for r_id, r_repo, r_branch, r_cwd, r_pool, r_model, r_sid, r_title, r_sum, r_ts in rows:
        dt_kst = datetime.datetime.fromtimestamp(r_ts, tz=KST).strftime("%H:%M:%S")
        notes.append(
            {
                "id": r_id,
                "repo": r_repo,
                "branch": r_branch,
                "cwd": r_cwd,
                "pool": r_pool,
                "model": r_model,
                "session_id": r_sid,
                "title": r_title,
                "summary": r_sum,
                "created_at": r_ts,
                "time_kst": dt_kst,
            }
        )
    return notes


def format_handover_context_for_prompt(cwd: str, max_age_seconds: int = 7200) -> str:
    notes = get_recent_handover_notes(cwd=cwd, max_age_seconds=max_age_seconds, limit=2)
    if not notes:
        return ""
    lines = ["[Recent Handover Memory (same repo/branch, <2h)]"]
    for n in notes:
        first_line = n["summary"].splitlines()[0][:180]
        lines.append(f"- [{n['time_kst']} · {n['pool']}] {n['title']}: {first_line}")
    return "\n".join(lines)


def notify_long_task_completion(
    session_id: str,
    title: str,
    elapsed_sec: float = 0.0,
    repo: str = "",
    model: str = "",
    pool: str = "",
    cwd: str = "",
    ok: bool = True,
    completed_at: int | None = None,
    min_elapsed_sec: float = 60.0,
) -> bool:
    """Fires a single Ubuntu desktop notification (and optional Telegram webhook) if elapsed_sec >= min_elapsed_sec."""
    if elapsed_sec < min_elapsed_sec or not session_id:
        return False
    if not repo and cwd:
        repo, _ = detect_repo_and_branch(cwd)
    repo = repo or "dev"
    model = model or pool or "worker"
    comp_ts = int(completed_at or time.time())
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM notified_completions WHERE session_id = ? AND ABS(completed_at - ?) <= 120",
            (session_id, comp_ts),
        ).fetchone()
        if existing:
            return False
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO notified_completions (session_id, completed_at, notified_at) VALUES (?, ?, ?)",
                (session_id, comp_ts, int(time.time())),
            )
    finally:
        conn.close()

    mins = int(elapsed_sec // 60)
    secs = int(elapsed_sec % 60)
    dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
    clean_title = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", title or "작업").strip()[:60]
    icon_prefix = "✅" if ok else "⚠️"
    summary_title = f"{icon_prefix} 작업 완료 ({repo} · {model})"
    body_text = f"{clean_title}\n소요 시간: {dur_str}"

    # 1. Ubuntu Desktop Notification (notify-send)
    notify_bin = shutil.which("notify-send")
    if notify_bin:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        uid = os.getuid()
        if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(f"/run/user/{uid}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        try:
            subprocess.Popen(
                [notify_bin, "-a", "AI Orchestrator", "-i", "utilities-terminal", summary_title, body_text],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # 2. Optional Telegram Bot notification if configured in environment
    tg_token = os.environ.get("MAESTRO_TELEGRAM_BOT_TOKEN") or os.environ.get("ORCA_TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("MAESTRO_TELEGRAM_CHAT_ID") or os.environ.get("ORCA_TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            payload = json.dumps({"chat_id": tg_chat, "text": f"{summary_title}\n{body_text}"}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2.5).read()
        except Exception:
            pass
    return True


def extract_session_log_tail(provider: str, session_id: str, max_lines: int = 25) -> dict:
    """On-demand log tail for Claude/Codex transcripts or Antigravity trajectories."""
    provider = (provider or "").lower()
    session_id = (session_id or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required", "lines": []}

    if provider == "claude":
        # Match a filename, never interpolate a request parameter into a path/glob.
        projects = Path(os.path.expanduser("~/.claude/projects"))
        transcripts = [p for p in projects.glob("*/*.jsonl") if p.stem == session_id]
        if not transcripts:
            return {"ok": False, "error": "Claude transcript not found", "lines": []}
        lines_out = []
        try:
            path = max(transcripts, key=lambda p: p.stat().st_mtime)
            with path.open("rb") as transcript:
                transcript.seek(0, os.SEEK_END)
                transcript.seek(max(0, transcript.tell() - 131072))
                raw_lines = transcript.read().decode("utf-8", errors="replace").splitlines()
            for raw in raw_lines:
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        continue
                    message = obj.get("message") or {}
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content") or []
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    if not isinstance(content, list):
                        continue
                    timestamp = str(obj.get("timestamp") or "")[11:19]
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            lines_out.append(f"[{timestamp}] 🔧 도구 호출: {str(block.get('name') or 'tool')[:100]}")
                        elif block.get("type") == "text":
                            text = str(block.get("text") or "").strip().replace("\n", " ")
                            if text:
                                lines_out.append(f"[{timestamp}] {message.get('role') or obj.get('type') or 'message'}: {text[:120]}")
                except (ValueError, TypeError):
                    continue
        except OSError as exc:
            return {"ok": False, "error": str(exc), "lines": []}
        return {"ok": True, "provider": provider, "session_id": session_id,
                "lines": lines_out[-max(1, max_lines):] or ["(아직 기록된 도구/출력 이벤트가 없습니다)"]}

    if provider == "codex":
        db_path = os.path.expanduser("~/.codex/state_5.sqlite")
        rollout_path = None
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                row = conn.execute(
                    "SELECT rollout_path, cwd, title FROM threads WHERE id LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"{session_id}%",),
                ).fetchone()
                conn.close()
                if row:
                    rollout_path = row[0]
            except Exception:
                pass
        if not rollout_path or not os.path.exists(rollout_path):
            return {"ok": False, "error": "rollout file not found", "lines": []}

        lines_out = []
        try:
            with open(rollout_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 131072), os.SEEK_SET)
                raw_lines = f.read().decode("utf-8", errors="ignore").splitlines()
            for raw in raw_lines:
                if not raw.startswith("{"):
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = (obj.get("timestamp") or "")[11:19]
                otype = obj.get("type")
                pl = obj.get("payload") or {}
                if otype == "event_msg":
                    ev = pl.get("type")
                    if ev == "task_started":
                        lines_out.append(f"[{ts}] ▶ 작업 시작 (turn_id={str(pl.get('turn_id',''))[:8]})")
                    elif ev == "exec_command_begin":
                        cmd = " ".join(pl.get("command") or []) if isinstance(pl.get("command"), list) else str(pl.get("command") or "")
                        lines_out.append(f"[{ts}] $ {cmd[:110]}")
                    elif ev == "agent_message":
                        msg = (pl.get("message") or "").strip().replace("\n", " ")
                        if msg:
                            lines_out.append(f"[{ts}] 💬 {msg[:120]}")
                    elif ev == "task_complete":
                        lines_out.append(f"[{ts}] ✔ 작업 완료 (task_complete)")
                    elif ev == "turn_aborted":
                        lines_out.append(f"[{ts}] ⚠ 작업 중단 ({pl.get('reason', 'aborted')})")
                elif otype == "response_item":
                    if pl.get("type") == "function_call":
                        fname = pl.get("name", "tool")
                        lines_out.append(f"[{ts}] 🔧 도구 호출: {fname}")
        except Exception as e:
            return {"ok": False, "error": str(e), "lines": []}

        return {
            "ok": True,
            "provider": "codex",
            "session_id": session_id,
            "lines": lines_out[-max_lines:] if lines_out else ["(아직 기록된 도구/출력 이벤트가 없습니다)"],
        }

    # Antigravity trajectory log tail
    script_dir = os.path.dirname(os.path.realpath(__file__))
    import sys
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import check_all_usage as cau

    addr, token = cau.discover_ls_credentials()
    if not addr:
        return {"ok": False, "error": "LanguageServer not found", "lines": []}

    # Resolve full cascadeId if 8-char prefix was passed
    full_cid = session_id
    if len(session_id) < 36:
        try:
            req = urllib.request.Request(
                f"http://{addr}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories",
                data=b"{}",
                headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                trajs = json.loads(resp.read().decode()).get("trajectorySummaries", {})
            for cid in trajs:
                if cid.startswith(session_id):
                    full_cid = cid
                    break
        except Exception:
            pass

    lines_out = []
    try:
        req = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
            data=json.dumps({"cascadeId": full_cid}).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
        steps = (data.get("trajectory") or {}).get("steps") or []
        for idx, st in enumerate(steps[-40:], max(1, len(steps) - 39)):
            stype = (st.get("type") or "").replace("CORTEX_STEP_TYPE_", "")
            sstat = (st.get("status") or "").replace("CORTEX_STEP_STATUS_", "")
            if stype == "RUN_COMMAND":
                rc = st.get("runCommand") or {}
                cmd = (rc.get("commandLine") or rc.get("command") or "").strip().splitlines()[0] if (rc.get("commandLine") or rc.get("command")) else "shell"
                lines_out.append(f"[#{idx} · {sstat}] $ {cmd[:105]}")
            elif stype == "PLANNER_RESPONSE":
                pr = st.get("plannerResponse") or {}
                tcalls = pr.get("toolCalls") or []
                if tcalls:
                    tnames = ", ".join(tc.get("name", "tool") for tc in tcalls[:3])
                    lines_out.append(f"[#{idx} · {sstat}] 🔧 도구 호출: {tnames}")
                txt = (pr.get("response") or pr.get("modifiedResponse") or "").strip().replace("\n", " ")
                if txt:
                    lines_out.append(f"[#{idx} · {sstat}] 💬 {txt[:115]}")
            elif stype not in ("", "USER_INPUT"):
                lines_out.append(f"[#{idx} · {sstat}] • {stype}")
    except Exception as e:
        return {"ok": False, "error": str(e), "lines": []}

    return {
        "ok": True,
        "provider": "antigravity",
        "session_id": full_cid,
        "lines": lines_out[-max_lines:] if lines_out else ["(실행 단계 로그 대기 중)"],
    }


# =====================================================================
# Mod 7: Worker ETA vs Actual Telemetry Logger (worker_telemetry)
# =====================================================================

BASELINE_ETA_TABLE: dict[tuple[str, str, str], tuple[float, int, int]] = {
    ("gemini-3.8-flash", "low", "default"): (40.0, 20000, 500),
    ("claude-sonnet-5-5", "high", "default"): (60.0, 40000, 1000),
    ("claude-opus-5-fast", "high", "fast"): (45.0, 160000, 4000),
    ("claude-opus-5-5", "high", "default"): (120.0, 150000, 5000),
    ("claude-opus-5-5", "xhigh", "default"): (150.0, 220000, 6000),
    ("claude-opus-5-5", "ultra", "default"): (180.0, 1200000, 15000),
    ("claude-ultra", "ultra", "default"): (180.0, 1200000, 15000),
    ("gpt-6-astra", "medium", "default"): (120.0, 200000, 3000),
    ("gpt-6.1-sol", "medium", "default"): (60.0, 150000, 2000),
    ("gpt-6.1-sol", "medium", "priority"): (50.0, 150000, 2000),
    ("gpt-6.1-sol", "high", "default"): (90.0, 350000, 5000),
    ("gpt-6.1-sol", "high", "priority"): (75.0, 350000, 5000),
    ("gpt-6.1-sol", "high", "ultrafast"): (60.0, 350000, 5000),
    ("gpt-6.1-sol", "ultra", "default"): (150.0, 1500000, 20000),
}

NETWORK_ERROR_KEYWORDS = (
    "timed out",
    "timeout after",
    "connection reset",
    "econnreset",
    "enetunreach",
    "network unreachable",
    "disconnected",
    "server restart",
    "api error",
    "502",
    "503",
    "504",
)


def normalize_telemetry_model(model: str, tier: str = "default") -> str:
    m = (model or "").lower().strip()
    if "flash" in m or "m401" in m:
        return "gemini-3.8-flash"
    if "argon" in m or "m402" in m:
        return "gemini-4-argon"
    if "sonnet" in m or "sonnet-4-6" in m:
        return "claude-sonnet-5-5"
    if "ultra" in m:
        return "claude-ultra"
    if "opus" in m or "claude" in m or "opus-4-6" in m:
        if tier == "fast" or "fast" in m:
            return "claude-opus-5-fast"
        return "claude-opus-5-5"
    if "mini" in m or "spark" in m or "astra" in m or "gpt-5.4-mini" in m or "gpt-5.3" in m:
        return "gpt-6-astra"
    if "sol" in m or "gpt-6.1" in m or "gpt-5.4" in m:
        return "gpt-6.1-sol"
    return m


_normalize_telemetry_model = normalize_telemetry_model


def lookup_baseline_eta(model: str, effort: str = "high", tier: str = "default") -> tuple[float, int, int]:
    m_norm = normalize_telemetry_model(model, tier)
    e_norm = (effort or "high").lower().strip()
    if e_norm in ("max", "ultra"):
        e_norm = "ultra"
    elif e_norm == "xhigh" and "sol" in m_norm:
        e_norm = "ultra"
    t_norm = (tier or "default").lower().strip()
    if t_norm in ("standard", ""):
        t_norm = "default"

    key = (m_norm, e_norm, t_norm)
    if key in BASELINE_ETA_TABLE:
        return BASELINE_ETA_TABLE[key]

    if (m_norm, e_norm, "default") in BASELINE_ETA_TABLE:
        return BASELINE_ETA_TABLE[(m_norm, e_norm, "default")]

    # Generic fallbacks
    if "astra" in m_norm:
        return (120.0, 200000, 3000)
    if "sol" in m_norm:
        return (90.0, 350000, 5000)
    if "flash" in m_norm:
        return (40.0, 20000, 500)
    return (120.0, 150000, 5000)


def _parse_codex_rollout_tokens(rollout_path: str) -> tuple[int, int, str]:
    """Extract (net_tokens, gross_tokens, detected_tier) from a Codex rollout jsonl."""
    if not rollout_path or not os.path.exists(rollout_path):
        return 0, 0, ""
    net_tokens = 0
    gross_tokens = 0
    detected_tier = ""
    last_total = None
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                p = d.get("payload")
                if not isinstance(p, dict):
                    continue
                if t == "turn_context":
                    detected_tier = p.get("service_tier") or detected_tier
                elif t == "token_usage_record":
                    u = p.get("usage", {})
                    if isinstance(u, dict):
                        in_tok = u.get("input_tokens", 0) or 0
                        cached = u.get("cached_input_tokens", 0) or 0
                        out_tok = u.get("output_tokens", 0) or 0
                        reasoning = u.get("reasoning_output_tokens", 0) or 0
                        net_tokens += max(0, in_tok - cached) + out_tok + reasoning
                        gross_tokens += u.get("total_tokens", 0) or (in_tok + out_tok)
                elif t == "event_msg":
                    info = p.get("info", {})
                    if isinstance(info, dict) and "total_token_usage" in info:
                        last_total = info["total_token_usage"]
        if last_total and gross_tokens == 0:
            in_tok = last_total.get("input_tokens", 0) or 0
            cached = last_total.get("cached_input_tokens", 0) or 0
            out_tok = last_total.get("output_tokens", 0) or 0
            reasoning = last_total.get("reasoning_output_tokens", 0) or 0
            net_tokens = max(0, in_tok - cached) + out_tok + reasoning
            gross_tokens = last_total.get("total_tokens", 0) or (in_tok + out_tok)
    except Exception:
        pass
    return net_tokens, gross_tokens, detected_tier


def _parse_claude_session_tokens(session_id: str) -> tuple[int, int, float, list[str]]:
    """Extract (net_tokens, gross_tokens, elapsed_sec, models) from Claude jsonl."""
    if not session_id:
        return 0, 0, 0.0, []
    pattern = os.path.expanduser(f"~/.claude/projects/**/{session_id}.jsonl")
    matched = glob.glob(pattern, recursive=True)
    if not matched:
        return 0, 0, 0.0, []
    target_file = matched[0]
    records = []
    timestamps = []
    models = []
    try:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if ts:
                    try:
                        timestamps.append(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        pass
                msg = d.get("message")
                if isinstance(msg, dict):
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        records.append(u)
                    m = msg.get("model")
                    if m and m not in models:
                        models.append(m)
    except Exception:
        pass

    net_tokens = sum(
        (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0) + (r.get("cache_creation_input_tokens", 0) or 0)
        for r in records
    )
    gross_tokens = net_tokens + sum((r.get("cache_read_input_tokens", 0) or 0) for r in records)
    timestamps.sort()
    elapsed_sec = (timestamps[-1] - timestamps[0]) if len(timestamps) >= 2 else 0.0
    return net_tokens, gross_tokens, elapsed_sec, models


def record_worker_telemetry(
    pool: str,
    model: str,
    effort: str,
    tier: str,
    actual_sec: float,
    session_id: str = "",
    title: str = "",
    ok: bool = True,
    output: str = "",
    stderr: str = "",
    error: str = "",
    actual_net_tokens: int | None = None,
    actual_gross_tokens: int | None = None,
    timestamp: str | None = None,
) -> dict:
    """Record worker execution telemetry with automated error & anomaly sample exclusion."""
    norm_tier = (tier or "default").lower().strip()
    if norm_tier in ("standard", ""):
        norm_tier = "default"
    norm_model = normalize_telemetry_model(model, norm_tier)
    norm_effort = (effort or "high").lower().strip()
    if norm_effort in ("max", "ultra"):
        norm_effort = "ultra"
    elif norm_effort == "xhigh" and "sol" in norm_model:
        norm_effort = "ultra"

    # Query tokens from Codex or Claude databases if not supplied
    if (actual_net_tokens is None or actual_gross_tokens is None) and session_id:
        codex_db = os.path.expanduser("~/.codex/state_5.sqlite")
        if os.path.exists(codex_db) and ("codex" in pool.lower() or "gpt" in norm_model.lower()):
            try:
                conn_cdx = sqlite3.connect(f"file:{codex_db}?mode=ro", uri=True, timeout=2.0)
                try:
                    row = conn_cdx.execute(
                        "SELECT tokens_used, rollout_path FROM threads WHERE id = ?", (session_id,)
                    ).fetchone()
                    if row:
                        gross = row[0] or 0
                        rp = row[1]
                        if rp and os.path.exists(rp):
                            net_p, gross_p, tier_p = _parse_codex_rollout_tokens(rp)
                            if net_p > 0 and actual_net_tokens is None:
                                actual_net_tokens = net_p
                            if gross_p > 0 and actual_gross_tokens is None:
                                actual_gross_tokens = gross_p
                            if tier_p and norm_tier == "default":
                                norm_tier = tier_p
                        if actual_gross_tokens is None or actual_gross_tokens == 0:
                            actual_gross_tokens = gross
                        if actual_net_tokens is None or actual_net_tokens == 0:
                            actual_net_tokens = int(round(gross * 0.15)) if gross > 0 else 0
                finally:
                    conn_cdx.close()
            except Exception:
                pass
        elif "claude" in pool.lower() or "claude" in norm_model.lower() or "opus" in norm_model.lower():
            net_c, gross_c, dur_c, _ = _parse_claude_session_tokens(session_id)
            if net_c > 0 and actual_net_tokens is None:
                actual_net_tokens = net_c
            if gross_c > 0 and actual_gross_tokens is None:
                actual_gross_tokens = gross_c
            if actual_sec <= 0 and dur_c > 0:
                actual_sec = dur_c

    actual_net_tokens = int(actual_net_tokens or 0)
    actual_gross_tokens = int(actual_gross_tokens or 0)
    actual_sec = float(actual_sec or 0.0)

    est_sec, est_tokens, _ = lookup_baseline_eta(norm_model, norm_effort, norm_tier)

    # Automated Anomaly & Error Detection
    is_valid = 1
    excluded_reasons = []

    if not ok or error:
        is_valid = 0
        excluded_reasons.append(f"error: {error or 'task failed'}")

    full_err_text = f"{output} {stderr} {error}".lower()
    for kw in NETWORK_ERROR_KEYWORDS:
        if kw in full_err_text:
            is_valid = 0
            excluded_reasons.append(f"network/server error: '{kw}'")
            break

    if actual_sec <= 1.0:
        is_valid = 0
        excluded_reasons.append(f"duration too short ({actual_sec:.1f}s <= 1.0s)")
    elif actual_sec > 1800.0:
        is_valid = 0
        excluded_reasons.append(f"duration too long ({actual_sec:.1f}s > 1800s)")

    if actual_net_tokens <= 0 and actual_gross_tokens <= 0:
        is_valid = 0
        excluded_reasons.append("zero tokens recorded")

    excluded_reason = "; ".join(excluded_reasons)
    ts_str = timestamp or datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO worker_telemetry (
                    timestamp, session_id, pool, model, effort, tier,
                    est_sec, actual_sec, est_tokens, actual_net_tokens, actual_gross_tokens,
                    ok, is_valid, excluded_reason, title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_str,
                    session_id or "",
                    pool or "unknown",
                    norm_model,
                    norm_effort,
                    norm_tier,
                    est_sec,
                    actual_sec,
                    est_tokens,
                    actual_net_tokens,
                    actual_gross_tokens,
                    1 if ok else 0,
                    is_valid,
                    excluded_reason,
                    title[:120] if title else "",
                ),
            )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "model": norm_model,
        "effort": norm_effort,
        "tier": norm_tier,
        "est_sec": est_sec,
        "actual_sec": actual_sec,
        "est_tokens": est_tokens,
        "actual_net_tokens": actual_net_tokens,
        "actual_gross_tokens": actual_gross_tokens,
        "is_valid": is_valid,
        "excluded_reason": excluded_reason,
    }


def backfill_historical_telemetry(max_codex: int = 300, max_claude: int = 300, reset: bool = False) -> dict:
    """Scan past valid completed Codex threads and Claude sessions to populate initial baseline."""
    conn = _connect()
    if reset:
        with conn:
            conn.execute("DELETE FROM worker_telemetry")
        existing = set()
    else:
        existing = set(
            x[0] for x in conn.execute(
                "SELECT session_id FROM worker_telemetry WHERE session_id IS NOT NULL AND session_id != ''"
            ).fetchall()
        )

    # Preload spawned session metadata (tier, effort, model) to join with state_5.sqlite and claude sessions
    spawned_map = {}
    spawn_reg = os.path.expanduser("~/.gemini/antigravity/agy_spawned_sessions.json")
    if os.path.exists(spawn_reg):
        try:
            with open(spawn_reg, "r", encoding="utf-8") as sf:
                s_data = json.load(sf)
                for sid, sinfo in s_data.items():
                    if isinstance(sinfo, dict):
                        spawned_map[sid] = sinfo
                        for tid in sinfo.get("thread_ids") or []:
                            spawned_map[tid] = sinfo
        except Exception:
            pass

    codex_scanned = 0
    claude_scanned = 0
    inserted = 0
    valid_count = 0
    excluded_count = 0

    # 1. Backfill Codex threads
    codex_db = os.path.expanduser("~/.codex/state_5.sqlite")
    if os.path.exists(codex_db):
        try:
            conn_cdx = sqlite3.connect(f"file:{codex_db}?mode=ro", uri=True, timeout=5.0)
            conn_cdx.row_factory = sqlite3.Row
            rows = conn_cdx.execute(
                """
                SELECT id, model, reasoning_effort, source, created_at, updated_at,
                       created_at_ms, updated_at_ms, tokens_used, rollout_path, title
                FROM threads
                WHERE tokens_used > 0
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max_codex,),
            ).fetchall()
            conn_cdx.close()

            for r in rows:
                tid = r["id"]
                if tid in existing:
                    continue
                codex_scanned += 1
                c_ms = r["created_at_ms"]
                u_ms = r["updated_at_ms"]
                dur = (u_ms - c_ms) / 1000.0 if (c_ms and u_ms) else float((r["updated_at"] or 0) - (r["created_at"] or 0))

                net_p, gross_p, tier_p = _parse_codex_rollout_tokens(r["rollout_path"])
                gross_tokens = gross_p or r["tokens_used"] or 0
                net_tokens = net_p or int(round(gross_tokens * 0.15))

                sp_info = spawned_map.get(tid, {})
                m = sp_info.get("model") or r["model"] or ("gpt-6-astra" if "astra" in (r["title"] or "").lower() else "gpt-6.1-sol")
                eff = sp_info.get("effort") or r["reasoning_effort"] or "high"
                t = sp_info.get("tier") or tier_p or "default"
                pool = sp_info.get("pool") or ("codex-astra" if "astra" in str(m).lower() else "codex")

                ts_dt = datetime.datetime.fromtimestamp(r["created_at"] or time.time(), tz=KST)
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")

                res = record_worker_telemetry(
                    pool=pool,
                    model=m,
                    effort=eff,
                    tier=t,
                    actual_sec=dur,
                    session_id=tid,
                    title=r["title"] or sp_info.get("title", ""),
                    ok=True,
                    actual_net_tokens=net_tokens,
                    actual_gross_tokens=gross_tokens,
                    timestamp=ts_str,
                )
                if res.get("ok"):
                    existing.add(tid)
                    inserted += 1
                    if res.get("is_valid"):
                        valid_count += 1
                    else:
                        excluded_count += 1
        except Exception:
            pass

    # 2. Backfill Claude sessions
    claude_files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    claude_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    for cf in claude_files[:max_claude]:
        sid = Path(cf).stem
        if sid in existing:
            continue
        claude_scanned += 1
        records = []
        timestamps = []
        models = []
        try:
            with open(cf, "r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp")
                    if ts:
                        try:
                            timestamps.append(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            pass
                    msg = d.get("message")
                    if isinstance(msg, dict):
                        u = msg.get("usage")
                        if isinstance(u, dict):
                            records.append(u)
                        m = msg.get("model")
                        if m and m not in models:
                            models.append(m)
        except Exception:
            continue

        if not records or not timestamps:
            continue

        timestamps.sort()
        dur = max(timestamps) - min(timestamps)
        max_gap = max((timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)), default=0.0)

        net = sum(
            (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0) + (r.get("cache_creation_input_tokens", 0) or 0)
            for r in records
        )
        gross = net + sum((r.get("cache_read_input_tokens", 0) or 0) for r in records)

        sp_info = spawned_map.get(sid, {})
        m_chosen = sp_info.get("model") or (models[0] if models else "claude-opus-5-5")
        eff = sp_info.get("effort") or "high"
        tier = sp_info.get("tier") or "default"
        pool = sp_info.get("pool") or "claude"

        # Multi-turn interactive session gap detection
        extra_err = ""
        if max_gap > 1800.0:
            extra_err = f"gap > 30m ({max_gap:.0f}s)"

        ts_dt = datetime.datetime.fromtimestamp(timestamps[0], tz=KST)
        ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")

        res = record_worker_telemetry(
            pool=pool,
            model=m_chosen,
            effort=eff,
            tier=tier,
            actual_sec=dur,
            session_id=sid,
            title="Claude Session",
            ok=(extra_err == ""),
            error=extra_err,
            actual_net_tokens=net,
            actual_gross_tokens=gross,
            timestamp=ts_str,
        )
        if res.get("ok"):
            existing.add(sid)
            inserted += 1
            if res.get("is_valid"):
                valid_count += 1
            else:
                excluded_count += 1

    return {
        "ok": True,
        "codex_scanned": codex_scanned,
        "claude_scanned": claude_scanned,
        "inserted": inserted,
        "valid": valid_count,
        "excluded": excluded_count,
    }


def get_telemetry_calibration_summary() -> dict:
    """Aggregate worker telemetry comparison grouped by (model, effort, tier)."""
    conn = _connect()
    try:
        tot_row = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_valid = 0 THEN 1 ELSE 0 END)
            FROM worker_telemetry
            """
        ).fetchone()
        tot_records = tot_row[0] or 0
        val_records = tot_row[1] or 0
        excl_records = tot_row[2] or 0

        rows = conn.execute(
            """
            SELECT model, effort, tier,
                   COUNT(*) as total_samples,
                   SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) as valid_samples,
                   SUM(CASE WHEN is_valid = 0 THEN 1 ELSE 0 END) as excluded_samples,
                   AVG(CASE WHEN is_valid = 1 THEN actual_sec ELSE NULL END) as avg_actual_sec,
                   AVG(CASE WHEN is_valid = 1 THEN actual_net_tokens ELSE NULL END) as avg_net_tokens,
                   AVG(CASE WHEN is_valid = 1 THEN actual_gross_tokens ELSE NULL END) as avg_gross_tokens,
                   MAX(est_sec) as est_sec,
                   MAX(est_tokens) as est_tokens
            FROM worker_telemetry
            GROUP BY model, effort, tier
            ORDER BY valid_samples DESC, model, effort
            """
        ).fetchall()

        calibrations = []
        for r in rows:
            m, e, t = r[0], r[1], r[2]
            valid_count = r[4] or 0
            excl_count = r[5] or 0

            med_sec = None
            med_net_tok = None
            if valid_count > 0:
                secs = [
                    x[0] for x in conn.execute(
                        "SELECT actual_sec FROM worker_telemetry WHERE model=? AND effort=? AND tier=? AND is_valid=1",
                        (m, e, t),
                    ).fetchall() if x[0] is not None
                ]
                if secs:
                    med_sec = round(statistics.median(secs), 1)

                toks = [
                    x[0] for x in conn.execute(
                        "SELECT actual_net_tokens FROM worker_telemetry WHERE model=? AND effort=? AND tier=? AND is_valid=1",
                        (m, e, t),
                    ).fetchall() if x[0] is not None
                ]
                if toks:
                    med_net_tok = int(round(statistics.median(toks)))

            est_s, est_tok, _ = lookup_baseline_eta(m, e, t)
            e_sec = r[9] if r[9] is not None else est_s
            e_tok = r[10] if r[10] is not None else est_tok

            ratio_s = round(med_sec / e_sec, 2) if (med_sec is not None and e_sec and e_sec > 0) else None
            ratio_t = round(med_net_tok / e_tok, 2) if (med_net_tok is not None and e_tok and e_tok > 0) else None

            calibrations.append({
                "model": m,
                "effort": e,
                "tier": t,
                "valid_samples": valid_count,
                "excluded_samples": excl_count,
                "est_sec": e_sec,
                "avg_actual_sec": round(r[6], 1) if r[6] is not None else None,
                "median_actual_sec": med_sec,
                "ratio_sec": ratio_s,
                "est_net_tokens": e_tok,
                "avg_actual_net_tokens": int(round(r[7])) if r[7] is not None else None,
                "median_actual_net_tokens": med_net_tok,
                "ratio_net_tokens": ratio_t,
                "avg_gross_tokens": int(round(r[8])) if r[8] is not None else None,
            })

        return {
            "ok": True,
            "total_records": tot_records,
            "valid_records": val_records,
            "excluded_records": excl_records,
            "calibrations": calibrations,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "total_records": 0,
            "valid_records": 0,
            "excluded_records": 0,
            "calibrations": [],
        }

