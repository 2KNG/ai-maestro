#!/usr/bin/env python3
"""
maestro: Maestro Multi-Pool Orchestrator CLI for Google Antigravity + OpenAI Codex + Claude.

Why this exists:
- Antigravity's built-in `invoke_subagent` tool only exposes `Model: inherit | pro | flash | flash_lite`,
  which ALL consume the Gemini Pool (Argon / Flash) and CANNOT select the independent
  `Claude & GPT-OSS Pool (Antigravity)` (`Claude Opus 5.5` = MODEL_PLACEHOLDER_M402,
  `Claude Sonnet 5.5` = MODEL_PLACEHOLDER_M405, `GPT-OSS 120B` = MODEL_PLACEHOLDER_M412).
- `maestro` talks directly to Antigravity's local LanguageServer (`StartCascade` + `SendUserCascadeMessage`
  with `cascadeConfig.plannerConfig.planModel = "MODEL_PLACEHOLDER_M402"`) AND to OpenAI Codex CLI
  (`codex exec --full-auto -m gpt-6.1-sol -c service_tier="default"`), balancing quota across ALL pools
  and keeping every conversation's `usage_widget.html` up to date.

Commands:
  maestro status [--json]
  maestro worker-start --spec "<task>" [--pool auto|agy-claude|agy-sonnet|codex|codex-astra|claude|claude-fast|agy-flash] [--model <model>] [--cwd <dir>] [--wait] [--timeout 3600] [--json]
  maestro check --id <cascade_id> [--wait] [--timeout 3600] [--json]
  maestro sync-widgets [--json]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

SKILL_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_SCRIPTS_DIR)
import benchmark_sync
import check_all_usage as cau
import render_usage_widget as ruw
from codex_config_safe import ConfigSafetyError, ensure_codex_config_safe, inspect_codex_config_safe

MODEL_MAP = {
    "agy-claude": "MODEL_PLACEHOLDER_M402",      # Claude Opus 5.5 (High) - Antigravity Claude/GPT Pool
    "opus-5.5": "MODEL_PLACEHOLDER_M402",
    "agy-sonnet": "MODEL_PLACEHOLDER_M405",      # Claude Sonnet 5.5 (High) - Antigravity Claude/GPT Pool
    "sonnet-5.5": "MODEL_PLACEHOLDER_M405",
    "agy-gpt-oss": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",  # GPT-OSS 120B (Medium) - Antigravity Claude/GPT Pool
    "agy-argon": "MODEL_PLACEHOLDER_M417",       # Gemini 4 Argon (High) - Antigravity Gemini Pool
    "argon": "MODEL_PLACEHOLDER_M417",
    "agy-argon-med": "MODEL_PLACEHOLDER_M418",   # Gemini 4 Argon (Medium) - Antigravity Gemini Pool
    "agy-flash": "MODEL_PLACEHOLDER_M322",       # Gemini 3.8 Flash (Fast) - Antigravity Gemini Pool
    "flash": "MODEL_PLACEHOLDER_M322",
}
CLAUDE_FAST_POOLS = ("claude-fast", "opus-5.5-fast", "opus-fast")
CLAUDE_ULTRA_POOLS = ("claude-ultra", "opus-ultra", "opus-5.5-ultra")
CLAUDE_POOLS = ("claude", "opus-5.5-xhigh") + CLAUDE_ULTRA_POOLS + CLAUDE_FAST_POOLS
CODEX_ASTRA_POOLS = ("codex-astra", "astra", "gpt-6-astra")
CODEX_LUNA_POOLS = ("codex-luna", "luna", "gpt-6-luna")
CODEX_POOLS = ("codex",) + CODEX_ASTRA_POOLS + CODEX_LUNA_POOLS

COMPUTER_USE_KEYWORDS = (
    "pc 조작", "pc조작", "컴퓨터 조작", "computer use", "computer-use",
    "데스크탑 조작", "마우스 클릭", "화면을 조작", "xdotool", "gui 조작",
)

MOTION_UI_CODING_KEYWORDS = (
    "모션", "모션그래픽", "애니메이션", "animation", "motion",
    "ui", "ux", "frontend", "프론트엔드", "webgl", "canvas",
    "three.js", "svg", "인터랙션", "interaction", "css", "tailwind",
)


def is_computer_use_task(spec: str) -> bool:
    """Return True if spec contains PC control, desktop GUI, or computer-use keywords."""
    if not spec:
        return False
    lowered = spec.lower()
    return any(kw in lowered for kw in COMPUTER_USE_KEYWORDS)


def is_motion_or_ui_task(spec: str) -> bool:
    """Return True if spec contains motion graphics, animation, or frontend UI keywords."""
    if not spec:
        return False
    lowered = spec.lower()
    return any(kw in lowered for kw in MOTION_UI_CODING_KEYWORDS)


def _is_mock_proc(proc) -> bool:
    """Return True if proc is a unittest mock or dummy object to avoid leaking monitor threads in tests."""
    if proc is None:
        return True
    mod = getattr(type(proc), "__module__", "")
    if mod.startswith("unittest.mock") or "mock" in mod:
        return True
    d = getattr(proc, "__dict__", {})
    return "_mock_return_value" in d or "_mock_name" in d


def ls_post(addr: str, token: str, method: str, payload: dict, timeout: int = 10) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-codeium-csrf-token": token or "",
    }
    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def notify_parent_cascade(title: str, pool: str, ok: bool, elapsed_sec: float, summary: str = "") -> bool:
    """Send high-priority completion notification to parent Antigravity session if known."""
    parent_cid = (
        os.environ.get("MAESTRO_PARENT_CASCADE_ID") or
        os.environ.get("ANTIGRAVITY_CASCADE_ID") or
        os.environ.get("MAESTRO_PARENT_SESSION_ID") or ""
    ).strip()
    if not parent_cid:
        return False
    try:
        addr, token = cau.discover_ls_credentials()
        if not addr or not token:
            return False
        msg = f"[MAESTRO WORKER COMPLETE] {title} ({pool}) finished (ok={ok}, elapsed={elapsed_sec:.1f}s)"
        if summary:
            msg += f"\n\nSummary:\n{summary[:400]}"
        ls_post(addr, token, "SendUserCascadeMessage", {
            "cascadeId": parent_cid,
            "userMessage": {"parts": [{"text": msg}]},
        }, timeout=5)
        return True
    except Exception:
        return False


def _canonical_pool_key(pool: str) -> str:
    if pool in ("agy-claude", "opus-5.5", "agy-sonnet", "sonnet-5.5", "agy-gpt-oss"):
        return "antigravity_claude"
    if pool in ("agy-argon", "argon", "agy-argon-med", "agy-flash", "flash"):
        return "antigravity_gemini"
    if pool in CODEX_POOLS:
        return "codex"
    if pool in CLAUDE_POOLS:
        return "claude"
    return pool


def _map_canonical_to_cli_pool(winner: str, difficulty: str = "high") -> str:
    if winner == "antigravity_gemini":
        if difficulty == "low":
            return "agy-flash"
        if difficulty == "medium":
            return "agy-argon-med"
        return "agy-argon"
    if winner == "antigravity_claude":
        return "agy-claude" if difficulty == "high" else "agy-sonnet"
    if winner == "codex":
        return "codex-astra" if difficulty == "medium" else "codex"
    if winner == "claude":
        return "claude"
    return "agy-argon" if difficulty == "high" else "agy-flash"


def is_pool_exhausted(pool: str, data: dict) -> tuple[bool, str]:
    """Check if the requested pool has 0% remaining in either its 5h or 7d window."""
    ckey = _canonical_pool_key(pool)
    routing = cau.select_best_pool(data, "high")
    candidates = routing.get("candidates") or []
    cand = next((c for c in candidates if c.get("pool") == ckey), None)
    if cand is not None:
        rem = cand.get("remaining_percent")
        mode = cand.get("routing_mode")
        status = cand.get("status")
        if mode == "exhausted" or status in ("exhausted", "unavailable") or (rem is not None and float(rem) <= 0.0):
            fh = cand.get("five_hour_remaining")
            wk = cand.get("weekly_remaining")
            return True, f"{ckey} exhausted (5h={fh}%, 7d={wk}%)"
        return False, ""
    details = (routing.get("pool_details") or {}).get(ckey, {})
    if details and (details.get("mode") == "exhausted" or float(details.get("effective_pct") or 0.0) <= 0.0):
        fh = details.get("5h_pct")
        wk = details.get("7d_pct")
        return True, f"{ckey} exhausted (5h={fh}%, 7d={wk}%)"
    return False, ""


def choose_auto_pool(difficulty: str = "high", spec: str = "", exclude_canonical: set[str] | None = None) -> tuple[str | None, dict]:
    data = cau.collect_usage(include_agent_trees=False)
    best = cau.select_best_pool(data, difficulty, spec=spec)
    excl = exclude_canonical or set()
    winner = best.get("pool")

    # For medium difficulty general coding without spec, follow the quota-balanced benchmark
    # recommendation (Claude reset surplus + active worker load); otherwise prefer codex-astra
    # unless Claude 7d > Codex 7d by 12%+
    if not spec and difficulty == "medium" and "codex" not in excl and "claude" not in excl:
        c_map = {c.get("pool"): c for c in (best.get("candidates") or [])}
        codex_c = c_map.get("codex")
        claude_c = c_map.get("claude")
        if (
            codex_c
            and codex_c.get("status") == "available"
            and float(codex_c.get("remaining_percent") or 0.0) > 0.0
            and claude_c
            and claude_c.get("status") == "available"
        ):
            codex_7d = float(codex_c.get("weekly_remaining") or 0.0)
            claude_7d = float(claude_c.get("weekly_remaining") or 0.0)
            rec_canonical = (best.get("benchmark_recommendation") or {}).get("canonical_pool")
            if rec_canonical in ("claude", "codex"):
                winner = rec_canonical
            elif claude_7d > codex_7d + 12.0:
                winner = "claude"
            else:
                winner = "codex"

    if winner in excl:
        candidates = [
            (c.get("pool"), float(c.get("score") or c.get("remaining_percent") or 0.0))
            for c in (best.get("candidates") or [])
            if c.get("pool") not in excl
            and c.get("status") == "available"
            and float(c.get("remaining_percent") or 0.0) > 0.0
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        winner = candidates[0][0] if candidates else None

    rec = best.get("benchmark_recommendation") or {}
    rec_pool = rec.get("recommended_pool")
    if spec and winner == rec.get("canonical_pool") and rec_pool and _canonical_pool_key(rec_pool) not in excl:
        return rec_pool, data

    return (_map_canonical_to_cli_pool(winner, difficulty) if winner else None), data


def extract_cascade_summary(traj_resp: dict) -> dict:
    status = traj_resp.get("status", "UNKNOWN")
    steps = traj_resp.get("trajectory", {}).get("steps", [])
    model_used = None
    last_text = ""
    for st in steps:
        stype = st.get("type", "")
        if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse", {})
            if pr.get("model"):
                model_used = pr.get("model")
            gen = st.get("metadata", {}).get("generatorModel")
            if gen:
                model_used = gen
            txt = (pr.get("response") or pr.get("modifiedResponse") or "").strip()
            if txt:
                last_text = txt
        elif stype == "CORTEX_STEP_TYPE_NOTIFY_USER":
            nu = st.get("notifyUser", {})
            txt = (nu.get("message") or "").strip()
            if txt:
                last_text = txt
    return {
        "status": status,
        "step_count": len(steps),
        "generator_model": model_used,
        "output": last_text,
    }


AGY_PREFIX = "✦"
SPAWN_REGISTRY_PATH = os.path.expanduser("~/.gemini/antigravity/agy_spawned_sessions.json")


def make_agy_title(spec: str, cwd: str) -> str:
    repo = os.path.basename(os.path.abspath(cwd).rstrip("/")) or "dev"
    first_line = (spec or "").strip().splitlines()[0] if (spec or "").strip() else "Worker Task"
    first_line = first_line.replace("\U0001fa90", "").strip()
    for noise in ("READ-ONLY", "READ ONLY", "Strict read-only", "Task:", "Target:", "Repo:"):
        if first_line.lower().startswith(noise.lower()):
            first_line = first_line[len(noise):].strip(" :.-—")
    short = first_line[:44].strip() or "Subagent Task"
    if short.startswith(AGY_PREFIX):
        return short
    return f"{AGY_PREFIX} {short}"


def assemble_worker_prompt(spec: str, title: str, effort: str | None, handover: str | None = None) -> str:
    """Ensure line 1 is ALWAYS '✦ <title>', line 2 is 'ultrathink' (if ultra), followed by rest of prompt."""
    body = (spec or "").strip()
    spec_lines = body.splitlines()
    first_line = spec_lines[0].strip() if spec_lines else ""

    if first_line.startswith(AGY_PREFIX):
        title_line = first_line
        rest = "\n".join(spec_lines[1:]).strip()
    else:
        title_line = title
        rest = body

    parts = [title_line]
    if effort == "ultra":
        parts.append("ultrathink")
    if rest:
        parts.append(rest)
    if handover and handover.strip():
        parts.append(handover.strip())

    return "\n\n".join(parts)


def record_spawned_session(session_id: str, info: dict, *, replace_session_id: str | None = None) -> None:
    """Merge under a stable lock; atomically publish for lock-free readers."""
    import fcntl
    import tempfile
    temporary = None
    try:
        from live_worker_processes import detect_parent_conversation_id
        info = dict(info)
        if not info.get("parent_conversation_id"):
            parent_cid = detect_parent_conversation_id()
            if parent_cid:
                info["parent_conversation_id"] = parent_cid
        os.makedirs(os.path.dirname(SPAWN_REGISTRY_PATH), exist_ok=True)
        with open(SPAWN_REGISTRY_PATH + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reg = {}
            if os.path.exists(SPAWN_REGISTRY_PATH):
                with open(SPAWN_REGISTRY_PATH, "r", encoding="utf-8") as f:
                    reg = json.load(f)
            previous = reg.pop(replace_session_id, {}) if replace_session_id else {}
            reg[session_id] = {**previous, **reg.get(session_id, {}), **info,
                               "session_id": session_id, "updated_at": int(time.time())}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=os.path.dirname(SPAWN_REGISTRY_PATH), delete=False) as f:
                temporary = f.name
                json.dump(reg, f, ensure_ascii=False, indent=2)
            os.replace(temporary, SPAWN_REGISTRY_PATH)
            temporary = None
    except Exception:
        pass
    finally:
        if temporary is not None:
            os.unlink(temporary)


def stamp_codex_db_titles(since_ts: int, agy_title: str, abs_cwd: str) -> list[str]:
    import sqlite3
    db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db_path):
        return []
    stamped = []
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute(
            "SELECT id, name, title, preview, first_user_message FROM threads WHERE created_at >= ? AND cwd = ? AND source IN ('exec', 'vscode') ORDER BY created_at DESC LIMIT 5",
            (since_ts - 5, abs_cwd),
        ).fetchall()
        for tid, name, title, prev, fum in rows:
            def _clean(val: str, fallback: str) -> str:
                s = (val or "").replace("\U0001fa90", "").strip()
                if s.lower().startswith("ultrathink"):
                    s = s[10:].strip()
                    if s.startswith(AGY_PREFIX):
                        s = s[len(AGY_PREFIX):].strip()
                    return f"{AGY_PREFIX} [Ultra] {s or fallback}".strip()
                if not s.startswith(AGY_PREFIX):
                    return f"{AGY_PREFIX} {s or fallback}".strip()
                return s

            new_name = _clean(name, agy_title)
            new_title = _clean(title, agy_title)
            new_prev = _clean(prev, agy_title)
            new_fum = _clean(fum, agy_title)

            conn.execute(
                "UPDATE threads SET name = ?, title = ?, preview = ?, first_user_message = ? WHERE id = ?",
                (new_name, new_title, new_prev, new_fum, tid),
            )
            stamped.append(tid)
            record_spawned_session(tid, {"provider": "codex", "title": new_name, "cwd": abs_cwd})
        conn.commit()
        conn.close()
    except Exception:
        pass
    return stamped


def start_agy_worker(pool: str, spec: str, cwd: str, wait: bool, timeout: int) -> dict:
    import maestro_memory
    addr, token = cau.discover_ls_credentials()
    if not addr:
        return {"ok": False, "error": "Could not discover Antigravity LanguageServer"}
    model_id = MODEL_MAP.get(pool, "MODEL_PLACEHOLDER_M402")
    abs_cwd = os.path.abspath(cwd)
    agy_title = make_agy_title(spec, abs_cwd)
    handover_ctx = maestro_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
    tagged_spec = spec if spec.lstrip().startswith(AGY_PREFIX) else f"{agy_title}\n\n{spec}"
    if handover_ctx:
        tagged_spec = f"{tagged_spec}\n\n{handover_ctx}"
    metadata = {
        "ideName": "antigravity",
        "apiKey": "",
        "locale": "ko",
        "ideVersion": "2.23.1",
        "extensionName": "antigravity",
    }
    start_res = ls_post(
        addr,
        token,
        "StartCascade",
        {
            "metadata": metadata,
            "source": "CORTEX_TRAJECTORY_SOURCE_VSCODE",
            "workspaceUris": [f"file://{abs_cwd}"],
        },
    )
    cid = start_res.get("cascadeId")
    if not cid:
        return {"ok": False, "error": f"StartCascade returned no cascadeId: {start_res}"}

    record_spawned_session(
        cid,
        {
            "provider": "antigravity",
            "pool": pool,
            "plan_model": model_id,
            "title": agy_title,
            "cwd": abs_cwd,
        },
    )

    t0 = time.time()
    ls_post(
        addr,
        token,
        "SendUserCascadeMessage",
        {
            "cascadeId": cid,
            "items": [{"text": tagged_spec}],
            "metadata": metadata,
            "cascadeConfig": {
                "plannerConfig": {
                    "planModel": model_id,
                    "conversational": {
                        "plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT",
                        "agenticMode": True,
                    },
                }
            },
        },
    )

    result = {
        "ok": True,
        "pool": pool,
        "plan_model": model_id,
        "cascade_id": cid,
        "title": agy_title,
        "cwd": abs_cwd,
        "handover_injected": bool(handover_ctx),
    }
    if not wait:
        return result

    deadline = time.time() + timeout
    time.sleep(1.5)
    while time.time() < deadline:
        t_res = ls_post(addr, token, "GetCascadeTrajectory", {"cascadeId": cid})
        info = extract_cascade_summary(t_res)
        result.update(info)
        if info["status"] in ("CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_DONE") and info["step_count"] >= 2:
            break
        time.sleep(1.5)
    elapsed = round(time.time() - t0, 2)
    result["elapsed_sec"] = elapsed
    if result.get("output"):
        maestro_memory.save_handover_note(
            cwd=abs_cwd,
            pool=pool,
            model=model_id,
            title=agy_title,
            summary=result["output"][:280],
            session_id=cid,
        )
    maestro_memory.notify_long_task_completion(
        session_id=cid,
        title=agy_title,
        pool=pool,
        elapsed_sec=elapsed,
        cwd=abs_cwd,
        ok=True,
        min_elapsed_sec=60.0,
    )
    try:
        maestro_memory.record_worker_telemetry(
            pool=pool,
            model=model_id,
            effort="low" if "flash" in model_id.lower() or "flash" in pool.lower() else "high",
            tier="fast" if "flash" in model_id.lower() or "flash" in pool.lower() else "default",
            actual_sec=elapsed,
            session_id=cid,
            title=agy_title,
            ok=bool(result.get("ok", True)),
            output=result.get("output", "") or "",
            stderr=result.get("error", "") or "",
            error=result.get("error", "") or "",
        )
    except Exception:
        pass
    return result


def start_claude_worker(pool: str, spec: str, cwd: str, effort: str | None,
                        wait: bool, timeout: int, *, tier: str | None = None,
                        max_capability: bool = False,
                        interactive: bool = False, urgent: bool = False,
                        background: bool = False) -> dict:
    """Run local Claude; retry only Fast model/effort availability failures."""
    import tempfile
    import uuid
    import maestro_memory

    if pool in CLAUDE_ULTRA_POOLS:
        pool, effort = "claude", "ultra"
    abs_cwd = os.path.abspath(cwd)
    title = make_agy_title(spec, abs_cwd)
    session_id = str(uuid.uuid4())
    handover = maestro_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
    selected_effort = effort or ("xhigh" if max_capability else "high")
    if selected_effort in ("ultra", "xhigh", "max") and not max_capability and effort is None:
        selected_effort = "high"
    min_timeout = {"low": 900, "medium": 1800, "high": 3600, "xhigh": 3600, "ultra": 5400}.get(selected_effort, 1800)
    if not os.environ.get("MAESTRO_ALLOW_SHORT_TIMEOUT"):
        timeout = max(int(timeout or min_timeout), min_timeout)

    # Opus Fast speed tier determination:
    if selected_effort == "ultra":
        fast = False
        effective_tier = "default"
    else:
        explicit_fast_pool = pool in CLAUDE_FAST_POOLS
        explicit_fast_tier = tier in ("fast", "priority", "ultrafast")
        auto_fast = (interactive or urgent) and not background and not max_capability
        fast = explicit_fast_pool or explicit_fast_tier or auto_fast

        if tier == "ultrafast" or (urgent and fast):
            effective_tier = "ultrafast"
        elif fast:
            effective_tier = "fast"
        elif tier in ("default", "standard"):
            effective_tier = "default"
        elif tier:
            effective_tier = tier
        else:
            effective_tier = "default"

    model = "claude-opus-5-fast" if fast else "claude-opus-5-5"
    prompt = assemble_worker_prompt(spec, title, selected_effort, handover)
    started = time.monotonic()

    current_depth = int(os.environ.get("MAESTRO_WORKER_DEPTH", "0") or "0")
    worker_env = dict(os.environ)
    worker_env["MAESTRO_WORKER_ACTIVE"] = "1"
    worker_env["MAESTRO_WORKER_DEPTH"] = str(current_depth + 1)
    worker_env["MAESTRO_WORKER_POOL"] = "claude"
    worker_env["MAESTRO_PARENT_SESSION_ID"] = os.environ.get("MAESTRO_PARENT_SESSION_ID") or session_id
    if fast:
        worker_env["CLAUDE_CODE_FAST_MODE"] = "1"

    def command():
        return [os.path.expanduser("~/.local/bin/claude"), "-p",
                "--dangerously-skip-permissions", "--model", model,
                "--effort", "max" if selected_effort in ("ultra", "max") else selected_effort,
                "--session-id", session_id, prompt]

    result = {"pool": pool, "title": title, "cwd": abs_cwd,
              "session_id": session_id, "handover_injected": bool(handover),
              "tier": effective_tier, "depth": current_depth + 1}

    def register(status, **extra):
        record_spawned_session(session_id, {**result, "provider": "claude", "model": model,
                                           "effort": selected_effort, "spec_preview": spec[:160],
                                           "started_at": started_at, "status": status, **extra})

    started_at = int(time.time())
    register("running")
    try:
        if not wait:
            with tempfile.NamedTemporaryFile(prefix="maestro-claude-", suffix=".log", delete=False) as log:
                proc = subprocess.Popen(command(), cwd=abs_cwd, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                        env=worker_env)
            register("running", pid=proc.pid)

            def _monitor_claude():
                try:
                    proc.wait(timeout=timeout)
                except Exception:
                    pass
                elapsed = round(time.monotonic() - started, 2)
                register("completed" if proc.returncode == 0 else "failed")
                out_txt = ""
                try:
                    with open(log.name, "r", encoding="utf-8", errors="replace") as f:
                        out_txt = f.read().strip()[-4000:]
                except Exception:
                    pass
                if out_txt:
                    maestro_memory.save_handover_note(cwd=abs_cwd, pool=pool, model=f"{model} ({selected_effort})",
                                                   title=title, summary=out_txt[:280], session_id=session_id)
                maestro_memory.notify_long_task_completion(session_id=session_id, title=title, pool=pool,
                                                       elapsed_sec=elapsed, cwd=abs_cwd,
                                                       ok=(proc.returncode == 0), min_elapsed_sec=60.0)
                notify_parent_cascade(title, pool, proc.returncode == 0, elapsed, summary=out_txt[:300])

            if not _is_mock_proc(proc):
                threading.Thread(target=_monitor_claude, daemon=True).start()
            return {**result, "ok": True, "status": "running", "model": model,
                    "effort": selected_effort, "pid": proc.pid, "log_path": log.name}

        proc = subprocess.run(command(), cwd=abs_cwd, capture_output=True, text=True, timeout=timeout,
                              env=worker_env)
        error_text = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
        unsupported = any(marker in error_text for marker in (
            "model not found", "unknown model", "invalid model", "unsupported model",
            "model is not available", "model is unavailable", "not have access to model",
            "not have access to the model", "invalid choice", "unsupported effort",
            "does not support", "there's an issue with the selected model",
        ))
        if fast and proc.returncode != 0 and unsupported:
            register("failed", ok=False, stderr=(proc.stderr or "").strip()[-1000:])
            result["fallback_from"] = model
            model, selected_effort = "claude-opus-5-5", "medium"
            result["pool"] = "claude"
            result["tier"] = "default"
            session_id = str(uuid.uuid4())
            result["session_id"] = session_id
            started_at = int(time.time())
            register("running")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command(), timeout)
            proc = subprocess.run(command(), cwd=abs_cwd, capture_output=True, text=True, timeout=remaining,
                                  env=worker_env)
        output = (proc.stdout or "").strip()[-4000:]
        result.update(ok=proc.returncode == 0, model=model, effort=selected_effort,
                      elapsed_sec=round(time.monotonic() - started, 2), output=output,
                      stderr=(proc.stderr or "").strip()[-1000:])
        register("completed" if proc.returncode == 0 else "failed")
        if output:
            maestro_memory.save_handover_note(cwd=abs_cwd, pool=pool, model=f"{model} ({selected_effort})",
                                           title=title, summary=output[:280], session_id=session_id)
        maestro_memory.notify_long_task_completion(session_id=session_id, title=title, pool=pool,
                                               elapsed_sec=result["elapsed_sec"], cwd=abs_cwd,
                                               ok=result["ok"], min_elapsed_sec=60.0)
        notify_parent_cascade(title, pool, result["ok"], result["elapsed_sec"], summary=output[:300])
        try:
            maestro_memory.record_worker_telemetry(
                pool=pool,
                model=model,
                effort=selected_effort,
                tier=effective_tier,
                actual_sec=result["elapsed_sec"],
                session_id=session_id,
                title=title,
                ok=result["ok"],
                output=output,
                stderr=result["stderr"],
                error="" if result["ok"] else (result["stderr"] or "Worker exited with error"),
            )
        except Exception:
            pass
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        elapsed_fail = round(time.monotonic() - started, 2)
        register("timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failed",
                 ok=False, error=str(exc))
        try:
            maestro_memory.record_worker_telemetry(
                pool=pool,
                model=model,
                effort=selected_effort,
                tier=effective_tier,
                actual_sec=elapsed_fail,
                session_id=session_id,
                title=title,
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
        return {**result, "ok": False, "model": model, "effort": selected_effort, "error": str(exc)}


def resolve_codex_settings(pool, model=None, effort=None, tier=None, *,
                           max_capability=False, interactive=False, urgent=False,
                           background=False):
    """Apply spend guards even when a caller bypasses the argument parser."""
    if pool in CODEX_LUNA_POOLS or model in ("gpt-6-luna", "luna"):
        model = "gpt-6-luna"
    elif pool in CODEX_ASTRA_POOLS or model in ("gpt-6-astra", "astra"):
        model = "gpt-6-astra"
    else:
        model = model or "gpt-6.1-sol"

    luna = (model == "gpt-6-luna")
    astra = (model == "gpt-6-astra")

    effort = effort or ("low" if luna else ("medium" if astra else "high"))
    if astra and effort == "xhigh":
        effort = "high"
    if luna and effort in ("ultra", "max"):
        effort = "high"
    if not max_capability:
        if astra and effort in ("high", "xhigh", "ultra"):
            effort = "medium"
        elif effort == "ultra":
            effort = "high"
    live = (interactive or urgent) and not background
    if astra or not live:
        tier = "default"
    elif tier in ("default", "standard"):
        tier = "default"
    elif urgent:
        tier = "priority" if luna else "ultrafast"
    else:
        tier = "priority"
    return model, effort, tier


def _benchmark_pools(pool):
    if pool == "auto":
        return None
    if pool in CODEX_LUNA_POOLS:
        return {"codex-luna"}
    if pool in CODEX_ASTRA_POOLS:
        return {"codex-astra"}
    if pool in CODEX_POOLS:
        return {"codex", "codex-astra", "codex-luna"}
    if pool in CLAUDE_FAST_POOLS:
        return {"claude-fast"}
    if pool in CLAUDE_POOLS:
        return {"claude"}
    canonical = _canonical_pool_key(pool)
    if canonical == "antigravity_claude":
        return {"agy-sonnet"} if pool in ("agy-sonnet", "sonnet-5.5") else {"claude"}
    if canonical == "antigravity_gemini":
        return {"agy-flash"} if pool in ("agy-flash", "flash") else {"agy-argon"}
    return set()


def start_codex_worker(spec: str, cwd: str, effort: str, tier: str, timeout: int,
                       pool: str = "codex", model: str | None = None, *,
                       wait: bool = True,
                       max_capability: bool = False, interactive: bool = False,
                       urgent: bool = False, background: bool = False) -> dict:
    import shutil
    import tempfile
    import threading
    import uuid
    import maestro_memory
    try:
        ensure_codex_config_safe()
    except (ConfigSafetyError, OSError, ValueError) as exc:
        return {"ok": False, "pool": pool, "error": f"Codex config guard: {exc}"}
    abs_cwd = os.path.abspath(cwd)
    model = model or ("gpt-6-luna" if pool in CODEX_LUNA_POOLS else ("gpt-6-astra" if pool in CODEX_ASTRA_POOLS else "gpt-6.1-sol"))
    model, effort, tier = resolve_codex_settings(
        pool, model, effort, tier, max_capability=max_capability,
        interactive=interactive, urgent=urgent, background=background)
    min_timeout = {"low": 900, "medium": 1800, "high": 3600, "xhigh": 3600, "ultra": 5400}.get(effort, 1800)
    if not os.environ.get("MAESTRO_ALLOW_SHORT_TIMEOUT"):
        timeout = max(int(timeout or min_timeout), min_timeout)
    agy_title = make_agy_title(spec, abs_cwd)
    handover_ctx = maestro_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
    tagged_spec = assemble_worker_prompt(spec, agy_title, effort, handover_ctx)
    temporary_sid = f"codex-{uuid.uuid4()}"
    current_depth = int(os.environ.get("MAESTRO_WORKER_DEPTH", "0") or "0")
    env = os.environ.copy()
    env["MAESTRO_WORKER_ACTIVE"] = "1"
    env["MAESTRO_WORKER_DEPTH"] = str(current_depth + 1)
    env["MAESTRO_WORKER_POOL"] = "codex"
    env["MAESTRO_PARENT_SESSION_ID"] = os.environ.get("MAESTRO_PARENT_SESSION_ID") or temporary_sid
    nvm_bins = sorted(glob.glob(os.path.expanduser("~/.config/nvm/versions/node/*/bin")), reverse=True)
    extra_paths = nvm_bins + [os.path.expanduser("~/.local/bin")]
    env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
    codex_bin = shutil.which("codex", path=env["PATH"]) or "codex"
    cmd = [
        codex_bin,
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        f'service_tier="{tier}"' if tier in ("priority", "ultrafast") else 'service_tier="default"',
        "-C",
        abs_cwd,
        tagged_spec,
    ]
    t0 = int(time.time())
    registration = {"provider": "codex", "pool": pool, "model": model,
                    "effort": effort, "tier": tier, "cwd": abs_cwd, "title": agy_title,
                    "spec_preview": spec[:160], "started_at": t0, "status": "running",
                    "depth": current_depth + 1}
    record_spawned_session(temporary_sid, registration)
    registration_lock = threading.Lock()
    thread_ids = []

    def stamp_and_register(status=None, **extra):
        # Serialize the timer with finalization so a late callback cannot revive a worker.
        with registration_lock:
            if status is not None:
                registration.update(status=status, **extra)
            for tid in stamp_codex_db_titles(t0, agy_title, abs_cwd):
                if tid not in thread_ids:
                    thread_ids.append(tid)
            for tid in thread_ids or [temporary_sid]:
                record_spawned_session(tid, registration, replace_session_id=temporary_sid)
            return list(thread_ids)

    # Stamp the SQLite thread row right after codex creates it (so it shows [AGY] even while running!)
    timer = threading.Timer(2.5, stamp_and_register)
    if "unittest" not in sys.modules:
        timer.start()
    elif hasattr(timer, "_mock_return_value") or hasattr(timer, "_mock_name"):
        timer.start()
    try:
        if not wait:
            log = tempfile.NamedTemporaryFile(prefix="maestro-codex-", suffix=".log", delete=False)
            proc = subprocess.Popen(cmd, cwd=abs_cwd, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    env=env)
            stamp_and_register("running", pid=proc.pid)

            def _monitor_codex():
                try:
                    proc.wait(timeout=timeout)
                except Exception:
                    pass
                elapsed = round(time.time() - t0, 2)
                stamped = stamp_and_register("completed" if proc.returncode == 0 else "failed",
                                             ok=(proc.returncode == 0))
                sid = stamped[0] if stamped else temporary_sid
                out_txt = ""
                try:
                    with open(log.name, "r", encoding="utf-8", errors="replace") as f:
                        out_txt = f.read().strip()[-4000:]
                except Exception:
                    pass
                if out_txt:
                    maestro_memory.save_handover_note(
                        cwd=abs_cwd, pool=pool, model=f"{model} ({effort}, {tier})",
                        title=agy_title, summary=out_txt[:280], session_id=sid,
                    )
                maestro_memory.notify_long_task_completion(
                    session_id=sid, title=agy_title, pool=pool, elapsed_sec=elapsed,
                    cwd=abs_cwd, ok=(proc.returncode == 0), min_elapsed_sec=60.0,
                )
                notify_parent_cascade(agy_title, pool, proc.returncode == 0, elapsed, summary=out_txt[:300])
                try:
                    first_tid = stamped[0] if stamped else sid
                    maestro_memory.record_worker_telemetry(
                        pool=pool, model=model, effort=effort, tier=tier,
                        actual_sec=elapsed, session_id=first_tid, title=agy_title,
                        ok=(proc.returncode == 0), output=out_txt, stderr="",
                        error="" if (proc.returncode == 0) else "Worker failed",
                    )
                except Exception:
                    pass

            if not _is_mock_proc(proc):
                threading.Thread(target=_monitor_codex, daemon=True).start()
            return {
                "ok": True,
                "status": "running",
                "pool": pool,
                "model": f"{model} ({effort}, {tier})",
                "effort": effort,
                "tier": tier,
                "title": agy_title,
                "thread_ids": thread_ids,
                "cwd": abs_cwd,
                "pid": proc.pid,
                "log_path": log.name,
            }

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        stamped = stamp_and_register("completed" if proc.returncode == 0 else "failed",
                                     ok=proc.returncode == 0)
        elapsed = round(time.time() - t0, 2)
        out_txt = (proc.stdout or "").strip()[-4000:]
        sid = stamped[0] if stamped else temporary_sid
        if out_txt:
            maestro_memory.save_handover_note(
                cwd=abs_cwd,
                pool=pool,
                model=f"{model} ({effort}, {tier})",
                title=agy_title,
                summary=out_txt[:280],
                session_id=sid,
            )
        maestro_memory.notify_long_task_completion(
            session_id=sid,
            title=agy_title,
            pool=pool,
            elapsed_sec=elapsed,
            cwd=abs_cwd,
            ok=(proc.returncode == 0),
            min_elapsed_sec=60.0,
        )
        notify_parent_cascade(agy_title, pool, proc.returncode == 0, elapsed, summary=out_txt[:300])
        try:
            first_tid = stamped[0] if stamped else sid
            maestro_memory.record_worker_telemetry(
                pool=pool,
                model=model,
                effort=effort,
                tier=tier,
                actual_sec=elapsed,
                session_id=first_tid,
                title=agy_title,
                ok=(proc.returncode == 0),
                output=out_txt,
                stderr=(proc.stderr or "").strip()[-1000:],
                error="" if (proc.returncode == 0) else ((proc.stderr or "").strip()[-1000:] or "Worker failed"),
            )
        except Exception:
            pass
        return {
            "ok": proc.returncode == 0,
            "pool": pool,
            "model": f"{model} ({effort}, {tier})",
            "effort": effort,
            "tier": tier,
            "title": agy_title,
            "thread_ids": stamped,
            "cwd": abs_cwd,
            "elapsed_sec": elapsed,
            "handover_injected": bool(handover_ctx),
            "output": out_txt,
            "stderr": (proc.stderr or "").strip()[-1000:],
        }
    except subprocess.TimeoutExpired as e:
        elapsed_err = round(time.time() - t0, 2)
        stamped = stamp_and_register("timeout", ok=False, error=f"Timeout after {timeout}s")
        try:
            first_tid = stamped[0] if stamped else sid
            maestro_memory.record_worker_telemetry(
                pool=pool,
                model=model,
                effort=effort,
                tier=tier,
                actual_sec=elapsed_err,
                session_id=first_tid,
                title=agy_title,
                ok=False,
                output=(e.stdout or "")[-2000:] if e.stdout else "",
                error=f"Timeout after {timeout}s",
            )
        except Exception:
            pass
        return {
            "ok": False,
            "pool": pool,
            "title": agy_title,
            "thread_ids": stamped,
            "error": f"Timeout after {timeout}s",
            "output": (e.stdout or "")[-2000:] if e.stdout else "",
        }
    except OSError as e:
        elapsed_err = round(time.time() - t0, 2)
        stamped = stamp_and_register("failed", ok=False, error=str(e))
        try:
            first_tid = stamped[0] if stamped else sid
            maestro_memory.record_worker_telemetry(
                pool=pool,
                model=model,
                effort=effort,
                tier=tier,
                actual_sec=elapsed_err,
                session_id=first_tid,
                title=agy_title,
                ok=False,
                error=str(e),
            )
        except Exception:
            pass
        return {"ok": False, "pool": pool, "title": agy_title,
                "thread_ids": stamped, "error": str(e)}
    finally:
        timer.cancel()


def sync_all_widgets() -> dict:
    data = cau.collect_usage(include_agent_trees=True)
    template_path = os.path.join(SKILL_SCRIPTS_DIR, "usage_widget_template.html")
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    html_str = ruw.compact_widget_html(ruw.inject_live_data(template, data))

    brain_root = os.path.expanduser("~/.gemini/antigravity/brain")
    updated = []
    meta_str = json.dumps(
        {
            "summary": "AOH dual-ring + clock-pie quota widget and narrow-safe multi-agent execution tree.",
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "userFacing": True,
        },
        ensure_ascii=False,
        indent=2,
    )
    if os.path.isdir(brain_root):
        for entry in sorted(os.listdir(brain_root)):
            cdir = os.path.join(brain_root, entry)
            if not os.path.isdir(cdir) or entry.startswith("."):
                continue
            target = os.path.join(cdir, "usage_widget.html")
            meta_target = target + ".metadata.json"
            try:
                with open(target, "w", encoding="utf-8") as wf:
                    wf.write(html_str)
                if not os.path.exists(meta_target):
                    with open(meta_target, "w", encoding="utf-8") as mf:
                        mf.write(meta_str)
                updated.append(target)
            except OSError:
                pass
    return {"ok": True, "updated_count": len(updated), "files": updated}


def main():
    # Remote commands have their own portable parser and avoid quota/model discovery.
    if len(sys.argv) > 1 and sys.argv[1] in ("remote", "remote-setup", "orca-remote", "fleet-setup", "fleet"):
        import maestro_remote
        argv = sys.argv[2:]
        if sys.argv[1] in ("remote-setup", "fleet-setup") and (not argv or argv[0].startswith("-")):
            argv = ["up", *argv]
        raise SystemExit(maestro_remote.main(argv))
    parser = argparse.ArgumentParser(prog="maestro")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--difficulty", default="high", choices=["high", "medium", "low"])
    p_status.add_argument("--json", action="store_true")

    p_route = sub.add_parser("route", help="Show which pool/model --pool auto would pick for a task (dry run)")
    p_route.add_argument("--task", "--spec", dest="task", required=True)
    p_route.add_argument("--difficulty", default="high", choices=["high", "medium", "low"])

    p_doctor = sub.add_parser("doctor", help="Inspect or recover the Codex user configuration")
    p_doctor.add_argument("--fix-codex-config", action="store_true", help="Checkpoint valid config or restore damaged config from a valid backup")
    p_doctor.add_argument("--config", help="Config path override (default: CODEX_HOME/config.toml)")
    p_doctor.add_argument("--json", action="store_true")
    # Show discoverable help while the remote module handles these arguments above.
    sub.add_parser("remote", aliases=["remote-setup", "orca-remote", "fleet-setup", "fleet"], help="Provision Orca and the 2kng.com fleet tunnel")

    p_start = sub.add_parser("worker-start")
    p_start.add_argument("--spec", default="", help="Task spec string (or '-' / empty to read from --spec-file or stdin)")
    p_start.add_argument("--spec-file", default="", help="Path to file containing task spec")
    p_start.add_argument(
        "--pool",
        default="auto",
        choices=["auto", *MODEL_MAP, *CODEX_POOLS, *CLAUDE_POOLS],
        help="Quota pool: auto, Antigravity aliases, local claude/claude-fast, or codex/codex-astra",
    )
    p_start.add_argument("--difficulty", default="high", choices=["high", "medium", "low"])
    p_start.add_argument("--cwd", default=os.getcwd())
    p_start.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"],
                         help="Default: Codex/Claude high, Astra medium; max-capability unlocks top efforts")
    p_start.add_argument("--model", help="Codex model override (e.g. gpt-6-astra); auto selects the Codex pool")
    p_start.add_argument("--tier", choices=["default", "standard", "fast", "priority", "ultrafast"],
                         help="Default/standard for background; fast/priority for interactive; ultrafast requires urgent")
    p_start.add_argument("--interactive", action="store_true", help="Live user ping-pong: priority or Claude Fast")
    p_start.add_argument("--background", action="store_true", help="Scheduled/remote/autonomous work: standard speed (overrides interactive/urgent)")
    p_start.add_argument("--max-capability", action="store_true", help="Choose the highest benchmark capability; unlock top reasoning efforts")
    p_start.add_argument("--domain", default="coding", choices=benchmark_sync.DOMAINS,
                         help="Benchmark domain for --max-capability")
    p_start.add_argument("--urgent", action="store_true", help="Use ultrafast tier only when urgent immediate response is required")
    p_start.add_argument("--wait", dest="wait", action="store_true", default=True, help="Wait synchronously for worker completion (default: True)")
    p_start.add_argument("--no-wait", "--async", dest="wait", action="store_false", help="Run worker asynchronously in background without waiting")
    p_start.add_argument("--timeout", type=int, default=3600)
    p_start.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check")
    p_check.add_argument("--id", required=True)
    p_check.add_argument("--wait", action="store_true")
    p_check.add_argument("--timeout", type=int, default=3600)
    p_check.add_argument("--json", action="store_true")

    p_sync = sub.add_parser("sync-widgets")
    p_sync.add_argument("--json", action="store_true")

    p_msave = sub.add_parser("memory-save")
    p_msave.add_argument("--summary", required=True)
    p_msave.add_argument("--cwd", default=os.getcwd())
    p_msave.add_argument("--pool", default="orchestrator")
    p_msave.add_argument("--model", default="gemini-4-argon")
    p_msave.add_argument("--title", default="Handover Note")
    p_msave.add_argument("--next-steps", default="")
    p_msave.add_argument("--files", nargs="*", default=None)

    p_mget = sub.add_parser("memory-get")
    p_mget.add_argument("--cwd", default=os.getcwd())
    p_mget.add_argument("--limit", type=int, default=5)
    p_mget.add_argument("--max-age", type=int, default=21600)

    p_telemetry = sub.add_parser("telemetry-stats")
    p_telemetry.add_argument("--backfill", action="store_true", help="Backfill historical telemetry from Codex rollouts and Claude sessions")
    p_telemetry.add_argument("--reset-backfill", action="store_true", help="Clear existing telemetry table and re-backfill all historical data")
    p_telemetry.add_argument("--json", action="store_true", help="Output raw JSON summary")

    p_cal = sub.add_parser("telemetry-calibrate")
    p_cal.add_argument("--min-samples", type=int, default=5, help="Minimum sample threshold to calibrate ETA baseline (default: 5)")
    p_cal.add_argument("--json", action="store_true", help="Output raw JSON calibration snapshot")

    args = parser.parse_args()

    if args.cmd == "doctor":
        try:
            result = (ensure_codex_config_safe(args.config) if args.fix_codex_config
                      else inspect_codex_config_safe(args.config))
        except (ConfigSafetyError, OSError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)
        return

    if args.cmd == "route":
        if is_computer_use_task(args.task):
            pool, data, reason = "codex", None, "computer-use task pre-route"
        elif is_motion_or_ui_task(args.task):
            pool, data, reason = "claude", None, "motion/UI task pre-route"
        else:
            pool, data = choose_auto_pool(args.difficulty, spec=args.task)
            reason = "choose_auto_pool"
        if data is None:
            data = cau.collect_usage(include_agent_trees=False)
        rec = benchmark_sync.recommend_model_for_task(args.task, args.difficulty, data)
        print(json.dumps({
            "ok": True,
            "task": args.task,
            "difficulty": args.difficulty,
            "pool": pool,
            "route_reason": reason,
            "domain": rec.get("domain"),
            "recommended_model": rec.get("recommended_model"),
            "recommended_pool": rec.get("recommended_pool"),
            "quota_balance": rec.get("quota_balance"),
            "top_candidates": [
                {k: c.get(k) for k in ("profile", "pool", "score", "benchmark_score", "remaining_percent", "balance_bonus")}
                for c in (rec.get("candidates") or [])[:4]
            ],
        }, ensure_ascii=False, indent=2))
        return

    if args.cmd == "status":
        pool, data = choose_auto_pool(args.difficulty)
        out = {
            "ok": True,
            "timestamp_kst": data.get("timestamp_kst"),
            "recommended_pool": pool,
            "recommended_routing": cau.select_best_pool(data, args.difficulty, urgent=False),
            "antigravity_pools": data.get("antigravity", {}).get("pools", {}),
            "codex_weekly": (data.get("codex_local", {}) or {}).get("weekly"),
            "claude_direct_weekly": (data.get("claude_local", {}) or {}).get("weekly"),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if args.cmd == "worker-start":
        import maestro_memory

        spec = (args.spec or "").strip()
        if not spec or spec == "-":
            if getattr(args, "spec_file", None):
                with open(os.path.expanduser(args.spec_file), "r", encoding="utf-8") as f:
                    spec = f.read().strip()
            elif not sys.stdin.isatty():
                spec = sys.stdin.read().strip()
            if not spec:
                parser.error("--spec, --spec-file, or stdin input is required for worker-start")
        args.spec = spec

        current_depth = int(os.environ.get("MAESTRO_WORKER_DEPTH", "0") or "0")
        parent_pool = os.environ.get("MAESTRO_WORKER_POOL", "").strip()
        canonical_parent = _canonical_pool_key(parent_pool) if parent_pool else ""

        if current_depth >= 2:
            print(json.dumps({
                "ok": False,
                "error": "Max worker orchestration depth (2) reached. Leaf workers must execute subtasks directly without further delegation."
            }, ensure_ascii=False, indent=2))
            return

        # Depth 1 Sub-Worker Guards: Same-Family Lock & Effort/Tier Clamping
        if current_depth == 1:
            if args.effort in ("ultra", "max", "xhigh"):
                args.effort = "high"
            if args.tier in ("fast", "ultrafast", "priority"):
                args.tier = "default"
            args.interactive = False
            args.urgent = False
            args.background = True

            if canonical_parent == "claude" or parent_pool in CLAUDE_POOLS:
                args.model = None
                args.pool = "claude"
            elif canonical_parent == "codex" or parent_pool in CODEX_POOLS:
                if args.pool not in CODEX_POOLS:
                    args.pool = "codex"

        requested_pool = args.pool
        if args.model:
            if requested_pool not in ("auto", *CODEX_POOLS):
                parser.error("--model is supported only with auto or a Codex pool")
            if requested_pool == "auto":
                if args.model in ("gpt-6-luna", "luna"):
                    requested_pool = "codex-luna"
                elif args.model in ("gpt-6-astra", "astra"):
                    requested_pool = "codex-astra"
                else:
                    requested_pool = "codex"
        if requested_pool == "auto":
            if is_computer_use_task(args.spec):
                requested_pool = "codex-astra" if args.model == "gpt-6-astra" else "codex"
            elif is_motion_or_ui_task(args.spec):
                # Motion graphics & frontend UI excels on Claude Opus 5.5
                requested_pool = "claude"
        recommendation = None
        if args.max_capability:
            try:
                recommendation = benchmark_sync.get_max_capability_recommendation(
                    args.domain, pools=_benchmark_pools(requested_pool), model=args.model,
                    allow_fast=requested_pool in CLAUDE_FAST_POOLS and current_depth < 1)
            except ValueError as exc:
                parser.error(str(exc))
            if requested_pool == "auto":
                requested_pool = recommendation["pool"]
            if current_depth == 1 and recommendation and recommendation.get("effort") in ("ultra", "max", "xhigh"):
                recommendation["effort"] = "high"
        steered_from = None
        steer_reason = None
        if requested_pool == "auto":
            pool, live_data = choose_auto_pool(args.difficulty, spec=args.spec)
        else:
            live_data = cau.collect_usage(include_agent_trees=False)
            exhausted, ex_reason = is_pool_exhausted(requested_pool, live_data)
            if exhausted:
                steered_from = requested_pool
                steer_reason = f"Pre-flight auto-steer: {ex_reason}"
                if current_depth == 1:
                    if canonical_parent == "claude" or parent_pool in CLAUDE_POOLS:
                        pool = None
                    else:
                        pool = "codex-astra" if requested_pool == "codex" else "codex"
                else:
                    pool, _ = choose_auto_pool(
                        args.difficulty,
                        spec=args.spec,
                        exclude_canonical={_canonical_pool_key(requested_pool)},
                    )
            else:
                pool = requested_pool

        def _dispatch(target_pool: str) -> dict:
            if not target_pool:
                return {"ok": False, "error": "No available quota pool"}
            selected = None
            model = args.model if _canonical_pool_key(requested_pool) == _canonical_pool_key(target_pool) == "codex" else None
            if args.max_capability:
                selected = benchmark_sync.get_max_capability_recommendation(
                    args.domain, pools=_benchmark_pools(target_pool), model=model,
                    allow_fast=target_pool in CLAUDE_FAST_POOLS)
                model = selected["model"]
            effort = args.effort or (selected["effort"] if selected else None)
            speed_flags = dict(max_capability=args.max_capability, interactive=args.interactive,
                               urgent=args.urgent, background=args.background)
            if target_pool in CODEX_POOLS:
                if target_pool in CODEX_LUNA_POOLS or model in ("gpt-6-luna", "luna"):
                    default_eff = "low"
                elif target_pool in CODEX_ASTRA_POOLS or model in ("gpt-6-astra", "astra"):
                    default_eff = "medium"
                else:
                    default_eff = "high"
                effort = effort or default_eff
                model, effort, tier = resolve_codex_settings(target_pool, model, effort, args.tier, **speed_flags)
                result = start_codex_worker(args.spec, args.cwd, effort, tier, args.timeout,
                                           pool=target_pool, model=model, wait=args.wait, **speed_flags)
            elif target_pool in CLAUDE_POOLS:
                if target_pool in CLAUDE_ULTRA_POOLS:
                    target_pool, effort = "claude", "ultra"
                result = start_claude_worker(target_pool, args.spec, args.cwd, effort, args.wait,
                                            args.timeout, tier=args.tier, **speed_flags)
            else:
                result = start_agy_worker(target_pool, args.spec, args.cwd, args.wait, args.timeout)
            if selected:
                result["benchmark_recommendation"] = selected
            return result

        res = _dispatch(pool)

        # Mid-flight quota exhaustion / rate-limit failover steering
        combined_err = f"{res.get('error','')} {res.get('stderr','')} {res.get('output','')} {res.get('status','')}".lower()
        quota_hit_signals = ("quota", "exhausted", "rate limit", "429", "limit reached", "cascade_run_status_error")
        midflight_exhausted = (not res.get("ok") or "CASCADE_RUN_STATUS_ERROR" in str(res.get("status", ""))) and any(
            sig in combined_err for sig in quota_hit_signals
        )
        if pool and not midflight_exhausted and (not res.get("ok") or "CASCADE_RUN_STATUS_ERROR" in str(res.get("status", ""))):
            post_data = cau.collect_usage(include_agent_trees=False)
            midflight_exhausted, _ = is_pool_exhausted(pool, post_data)

        if midflight_exhausted:
            failed_pool = pool
            if current_depth == 1:
                if canonical_parent == "claude" or parent_pool in CLAUDE_POOLS:
                    fallback_pool = None
                else:
                    fallback_pool = "codex-astra" if failed_pool == "codex" else "codex"
            else:
                fallback_pool, _ = choose_auto_pool(
                    args.difficulty,
                    spec=args.spec,
                    exclude_canonical={_canonical_pool_key(failed_pool), _canonical_pool_key(requested_pool)},
                )
            if fallback_pool and _canonical_pool_key(fallback_pool) != _canonical_pool_key(failed_pool):
                maestro_memory.save_handover_note(
                    cwd=args.cwd,
                    pool=failed_pool,
                    model=str(res.get("model") or res.get("plan_model") or failed_pool),
                    title=f"⚡ Mid-flight Quota Failover ({failed_pool} → {fallback_pool})",
                    summary=(res.get("output") or res.get("stderr") or "Quota exhausted mid-flight; steering to backup pool.")[:260],
                )
                steered_from = steered_from or failed_pool
                steer_reason = f"Mid-flight quota failover ({failed_pool} -> {fallback_pool})"
                res = _dispatch(fallback_pool)

        if steered_from:
            res["steered_from"] = steered_from
            res["steer_reason"] = steer_reason
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.cmd == "check":
        addr, token = cau.discover_ls_credentials()
        if not addr:
            print(json.dumps({"ok": False, "error": "No LS found"}))
            sys.exit(1)
        deadline = time.time() + (args.timeout if args.wait else 0)
        while True:
            t_res = ls_post(addr, token, "GetCascadeTrajectory", {"cascadeId": args.id})
            info = extract_cascade_summary(t_res)
            info["ok"] = True
            info["cascade_id"] = args.id
            if not args.wait or info["status"] in ("CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_DONE"):
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return
            if time.time() >= deadline:
                info["timeout"] = True
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return
            time.sleep(1.5)

    if args.cmd == "sync-widgets":
        res = sync_all_widgets()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.cmd == "memory-save":
        import maestro_memory
        res = maestro_memory.save_handover_note(
            cwd=args.cwd,
            pool=args.pool,
            model=args.model,
            title=args.title,
            summary=args.summary,
            modified_files=args.files,
            next_steps=args.next_steps,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.cmd == "memory-get":
        import maestro_memory
        repo, branch = maestro_memory._git_repo_and_branch(args.cwd)
        notes = maestro_memory.get_recent_handover_notes(
            repo=repo,
            branch=branch,
            max_age_seconds=args.max_age,
            limit=args.limit,
        )
        print(json.dumps({"ok": True, "repo": repo, "branch": branch, "notes": notes}, ensure_ascii=False, indent=2))
        return

    if args.cmd == "telemetry-stats":
        import maestro_memory
        if args.backfill or args.reset_backfill:
            bf_res = maestro_memory.backfill_historical_telemetry(reset=args.reset_backfill)
            if not args.json:
                print(f"[telemetry] Backfill complete (reset={args.reset_backfill}): inserted={bf_res.get('inserted')}, valid={bf_res.get('valid')}, excluded={bf_res.get('excluded')}\n")

        summary = maestro_memory.get_telemetry_calibration_summary()
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return

        calibrations = summary.get("calibrations", [])
        if not calibrations:
            print("기록된 워커 텔레메트리 데이터가 없습니다.")
            return

        print(f"### Worker ETA vs Actual Telemetry Calibration (총 {summary.get('total_records', 0)}건, 유효 {summary.get('valid_records', 0)}건, 제외 {summary.get('excluded_records', 0)}건)\n")
        headers = ["Model", "Effort", "Tier", "Samples", "Est Sec", "Act Sec (med)", "Sec Ratio", "Est Net Tok", "Act Net Tok (med)", "Tok Ratio", "Avg Ctx (Ctx %)"]
        rows = []
        for c in calibrations:
            model_disp = str(c.get("model", "-"))
            eff_disp = str(c.get("effort", "-"))
            tier_disp = str(c.get("tier", "-"))
            samples_disp = str(c.get("valid_samples", 0))
            est_s = c.get("est_sec")
            est_sec_disp = f"{est_s:.0f}s" if est_s is not None else "-"
            med_s = c.get("median_actual_sec")
            act_sec_disp = f"{med_s:.1f}s" if med_s is not None else "-"
            ratio_s = c.get("ratio_sec")
            sec_ratio_disp = f"{ratio_s:.2f}x" if ratio_s is not None else "-"
            est_t = c.get("est_net_tokens")
            est_tok_disp = f"{est_t // 1000}k" if est_t else "0k"
            med_t = c.get("median_actual_net_tokens")
            act_tok_disp = f"{med_t // 1000}k" if med_t is not None else "-"
            ratio_t = c.get("ratio_net_tokens")
            tok_ratio_disp = f"{ratio_t:.2f}x" if ratio_t is not None else "-"
            ctx_tok = c.get("avg_context_tokens", 0)
            ctx_pct = c.get("avg_context_pct", 0.0)
            ctx_disp = f"{ctx_tok // 1000}k ({ctx_pct:.1f}%)" if ctx_tok else "-"
            rows.append([model_disp, eff_disp, tier_disp, samples_disp, est_sec_disp, act_sec_disp, sec_ratio_disp, est_tok_disp, act_tok_disp, tok_ratio_disp, ctx_disp])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
        sep_line = "| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |"
        print(header_line)
        print(sep_line)
        for row in rows:
            print("| " + " | ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)) + " |")
        return

    if args.cmd == "telemetry-calibrate":
        import maestro_memory
        res = maestro_memory.run_daily_calibration(min_samples=args.min_samples)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"[telemetry] Calibration complete: {res.get('calibrated_count')} buckets updated (snapshot: {res.get('snapshot_path')})\n")
        calibrated = res.get("calibrations", [])
        if not calibrated:
            print("캘리브레이션 기준 샘플 수(min-samples)를 충족하는 버킷이 없습니다.")
            return
        headers = ["Model", "Effort", "Tier", "Samples", "Calibrated Sec", "Calibrated Net Tok", "Avg Ctx (Ctx %)"]
        rows = []
        for c in calibrated:
            rows.append([
                c["model"], c["effort"], c["tier"], str(c["valid_samples"]),
                f"{c['sec']:.1f}s", f"{c['net_tokens'] // 1000}k",
                f"{c['avg_context_tokens'] // 1000}k ({c['avg_context_pct']:.1f}%)" if c['avg_context_tokens'] else "-"
            ])
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))
        print("| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |")
        print("| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |")
        for row in rows:
            print("| " + " | ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)) + " |")
        return


if __name__ == "__main__":
    main()

