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
import json
import os
import re
import shutil
import sqlite3
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
    clean_summary = "\n".join(line.strip() for line in (summary or "").strip().splitlines() if line.strip())
    if modified_files:
        clean_summary += f"\nFiles: {', '.join(modified_files[:6])}"
    if next_steps:
        clean_summary += f"\nNext: {next_steps.strip()[:120]}"
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
