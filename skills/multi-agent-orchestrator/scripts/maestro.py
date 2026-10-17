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
  maestro worker-start --spec "<task>" [--pool auto|agy-claude|agy-sonnet|codex|codex-astra|claude|claude-fast|agy-flash] [--model <model>] [--cwd <dir>] [--wait] [--timeout 180] [--json]
  maestro check --id <cascade_id> [--wait] [--timeout 180] [--json]
  maestro sync-widgets [--json]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

SKILL_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_SCRIPTS_DIR)
import benchmark_sync
import check_all_usage as cau
import render_usage_widget as ruw

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
CODEX_POOLS = ("codex",) + CODEX_ASTRA_POOLS


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
        return "codex"
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


def choose_auto_pool(difficulty: str = "high", exclude_canonical: set[str] | None = None) -> tuple[str, dict]:
    data = cau.collect_usage(include_agent_trees=False)
    best = cau.select_best_pool(data, difficulty)
    excl = exclude_canonical or set()
    winner = best.get("pool")
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


def record_spawned_session(session_id: str, info: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SPAWN_REGISTRY_PATH), exist_ok=True)
        reg = {}
        if os.path.exists(SPAWN_REGISTRY_PATH):
            with open(SPAWN_REGISTRY_PATH, "r", encoding="utf-8") as f:
                reg = json.load(f)
        reg[session_id] = {**reg.get(session_id, {}), **info, "updated_at": int(time.time())}
        with open(SPAWN_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def stamp_codex_db_titles(since_ts: int, agy_title: str, abs_cwd: str) -> list[str]:
    import sqlite3
    db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db_path):
        return []
    stamped = []
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute(
            "SELECT id, name, title FROM threads WHERE created_at >= ? AND cwd = ? AND source = 'exec' ORDER BY created_at DESC LIMIT 3",
            (since_ts - 5, abs_cwd),
        ).fetchall()
        for tid, name, title in rows:
            name = name.replace("\U0001fa90", "").strip() if name else name
            title = title.replace("\U0001fa90", "").strip() if title else title
            new_name = name if (name and name.startswith(AGY_PREFIX)) else agy_title
            new_title = title if (title and title.startswith(AGY_PREFIX)) else f"{agy_title} — {title or ''}".strip(" —")
            conn.execute("UPDATE threads SET name = ?, title = ? WHERE id = ?", (new_name, new_title, tid))
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
    return result


def start_claude_worker(pool: str, spec: str, cwd: str, effort: str | None,
                        wait: bool, timeout: int, *, max_capability: bool = False,
                        interactive: bool = False, urgent: bool = False,
                        background: bool = False) -> dict:
    """Run local Claude; retry only Fast model/effort availability failures."""
    import tempfile
    import uuid
    import maestro_memory

    if pool in CLAUDE_ULTRA_POOLS:
        pool, effort = "claude", effort or "ultra"
    abs_cwd = os.path.abspath(cwd)
    title = make_agy_title(spec, abs_cwd)
    session_id = str(uuid.uuid4())
    handover = maestro_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
    prompt = spec if spec.lstrip().startswith(AGY_PREFIX) else f"{title}\n\n{spec}"
    if handover:
        prompt += f"\n\n{handover}"
    # An explicit Fast pool remains an opt-in, including background invocations.
    fast = pool in CLAUDE_FAST_POOLS or ((interactive or urgent) and not background and not max_capability)
    model = "claude-opus-5-fast" if fast else "claude-opus-5-5"
    selected_effort = effort or ("xhigh" if max_capability else "high")
    if selected_effort in ("ultra", "xhigh", "max") and not max_capability and effort is None:
        selected_effort = "high"
    if selected_effort == "ultra" and "ultrathink" not in prompt.lower():
        prompt = "ultrathink\n\n" + prompt
    started = time.monotonic()

    def command():
        return ["/home/u24c9g5090r64/.local/bin/claude", "-p",
                "--dangerously-skip-permissions", "--model", model,
                "--effort", "max" if selected_effort in ("ultra", "max") else selected_effort,
                "--session-id", session_id, prompt]

    result = {"pool": pool, "title": title, "cwd": abs_cwd,
              "session_id": session_id, "handover_injected": bool(handover),
              "tier": "fast" if fast else "default"}
    try:
        if not wait:
            with tempfile.NamedTemporaryFile(prefix="maestro-claude-", suffix=".log", delete=False) as log:
                proc = subprocess.Popen(command(), cwd=abs_cwd, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            record_spawned_session(session_id, {**result, "provider": "claude", "model": model,
                                               "effort": selected_effort, "pid": proc.pid})
            return {**result, "ok": True, "status": "running", "model": model,
                    "effort": selected_effort, "pid": proc.pid, "log_path": log.name}

        proc = subprocess.run(command(), cwd=abs_cwd, capture_output=True, text=True, timeout=timeout)
        error_text = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
        unsupported = any(marker in error_text for marker in (
            "model not found", "unknown model", "invalid model", "unsupported model",
            "model is not available", "model is unavailable", "not have access to model",
            "not have access to the model", "invalid choice", "unsupported effort",
            "does not support", "there's an issue with the selected model",
        ))
        if fast and proc.returncode != 0 and unsupported:
            result["fallback_from"] = model
            model, selected_effort = "claude-opus-5-5", "medium"
            result["tier"] = "default"
            session_id = str(uuid.uuid4())
            result["session_id"] = session_id
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command(), timeout)
            proc = subprocess.run(command(), cwd=abs_cwd, capture_output=True, text=True, timeout=remaining)
        output = (proc.stdout or "").strip()[-4000:]
        result.update(ok=proc.returncode == 0, model=model, effort=selected_effort,
                      elapsed_sec=round(time.monotonic() - started, 2), output=output,
                      stderr=(proc.stderr or "").strip()[-1000:])
        record_spawned_session(session_id, {**result, "provider": "claude"})
        if output:
            maestro_memory.save_handover_note(cwd=abs_cwd, pool=pool, model=f"{model} ({selected_effort})",
                                           title=title, summary=output[:280], session_id=session_id)
        maestro_memory.notify_long_task_completion(session_id=session_id, title=title, pool=pool,
                                               elapsed_sec=result["elapsed_sec"], cwd=abs_cwd,
                                               ok=result["ok"], min_elapsed_sec=60.0)
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, "ok": False, "model": model, "effort": selected_effort, "error": str(exc)}


def resolve_codex_settings(pool, model=None, effort=None, tier=None, *,
                           max_capability=False, interactive=False, urgent=False,
                           background=False):
    """Apply spend guards even when a caller bypasses the argument parser."""
    model = model or ("gpt-6-astra" if pool in CODEX_ASTRA_POOLS else "gpt-6.1-sol")
    astra = model == "gpt-6-astra"
    effort = effort or ("medium" if astra else "high")
    if astra and effort == "xhigh":
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
        tier = "ultrafast"
    else:
        tier = "priority"
    return model, effort, tier


def _benchmark_pools(pool):
    if pool == "auto":
        return None
    if pool in CODEX_ASTRA_POOLS:
        return {"codex-astra"}
    if pool in CODEX_POOLS:
        return {"codex", "codex-astra"}
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
                       max_capability: bool = False, interactive: bool = False,
                       urgent: bool = False, background: bool = False) -> dict:
    import shutil
    import threading
    import maestro_memory
    abs_cwd = os.path.abspath(cwd)
    model = model or ("gpt-6-astra" if pool in CODEX_ASTRA_POOLS else "gpt-6.1-sol")
    model, effort, tier = resolve_codex_settings(
        pool, model, effort, tier, max_capability=max_capability,
        interactive=interactive, urgent=urgent, background=background)
    agy_title = make_agy_title(spec, abs_cwd)
    handover_ctx = maestro_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
    tagged_spec = spec if spec.lstrip().startswith(AGY_PREFIX) else f"{agy_title}\n\n{spec}"
    if handover_ctx:
        tagged_spec = f"{tagged_spec}\n\n{handover_ctx}"
    env = os.environ.copy()
    nvm_bins = sorted(glob.glob(os.path.expanduser("~/.config/nvm/versions/node/*/bin")), reverse=True)
    extra_paths = nvm_bins + [os.path.expanduser("~/.local/bin")]
    env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])
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
    # Stamp the SQLite thread row right after codex creates it (so it shows [AGY] even while running!)
    threading.Timer(2.5, stamp_codex_db_titles, args=(t0, agy_title, abs_cwd)).start()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        stamped = stamp_codex_db_titles(t0, agy_title, abs_cwd)
        elapsed = round(time.time() - t0, 2)
        out_txt = (proc.stdout or "").strip()[-4000:]
        sid = stamped[0] if stamped else f"codex-{t0}"
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
        stamped = stamp_codex_db_titles(t0, agy_title, abs_cwd)
        return {
            "ok": False,
            "pool": pool,
            "title": agy_title,
            "thread_ids": stamped,
            "error": f"Timeout after {timeout}s",
            "output": (e.stdout or "")[-2000:] if e.stdout else "",
        }


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
    parser = argparse.ArgumentParser(prog="maestro")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--difficulty", default="high", choices=["high", "medium", "low"])
    p_status.add_argument("--json", action="store_true")

    p_start = sub.add_parser("worker-start")
    p_start.add_argument("--spec", required=True)
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
    p_start.add_argument("--tier", choices=["default", "standard", "priority", "ultrafast"],
                         help="Default/standard for background; priority for interactive; ultrafast requires urgent")
    p_start.add_argument("--interactive", action="store_true", help="Live user ping-pong: priority or Claude Fast")
    p_start.add_argument("--background", action="store_true", help="Scheduled/remote/autonomous work: standard speed (overrides interactive/urgent)")
    p_start.add_argument("--max-capability", action="store_true", help="Choose the highest benchmark capability; unlock top reasoning efforts")
    p_start.add_argument("--domain", default="coding", choices=benchmark_sync.DOMAINS,
                         help="Benchmark domain for --max-capability")
    p_start.add_argument("--urgent", action="store_true", help="Use ultrafast tier only when urgent immediate response is required")
    p_start.add_argument("--wait", action="store_true")
    p_start.add_argument("--timeout", type=int, default=180)
    p_start.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check")
    p_check.add_argument("--id", required=True)
    p_check.add_argument("--wait", action="store_true")
    p_check.add_argument("--timeout", type=int, default=180)
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

    args = parser.parse_args()

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
        requested_pool = args.pool
        if args.model:
            if requested_pool not in ("auto", *CODEX_POOLS):
                parser.error("--model is supported only with auto or a Codex pool")
            if requested_pool == "auto":
                requested_pool = "codex-astra" if args.model == "gpt-6-astra" else "codex"
        recommendation = None
        if args.max_capability:
            try:
                recommendation = benchmark_sync.get_max_capability_recommendation(
                    args.domain, pools=_benchmark_pools(requested_pool), model=args.model,
                    allow_fast=requested_pool in CLAUDE_FAST_POOLS)
            except ValueError as exc:
                parser.error(str(exc))
            if requested_pool == "auto":
                requested_pool = recommendation["pool"]
        steered_from = None
        steer_reason = None
        if requested_pool == "auto":
            pool, live_data = choose_auto_pool(args.difficulty)
        else:
            live_data = cau.collect_usage(include_agent_trees=False)
            exhausted, ex_reason = is_pool_exhausted(requested_pool, live_data)
            if exhausted:
                steered_from = requested_pool
                steer_reason = f"Pre-flight auto-steer: {ex_reason}"
                pool, _ = choose_auto_pool(
                    args.difficulty,
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
                effort = effort or ("medium" if target_pool in CODEX_ASTRA_POOLS or model == "gpt-6-astra" else "high")
                model, effort, tier = resolve_codex_settings(target_pool, model, effort, args.tier, **speed_flags)
                result = start_codex_worker(args.spec, args.cwd, effort, tier, args.timeout,
                                           pool=target_pool, model=model, **speed_flags)
            elif target_pool in CLAUDE_POOLS:
                if target_pool in CLAUDE_ULTRA_POOLS:
                    target_pool, effort = "claude", args.effort or "ultra"
                result = start_claude_worker(target_pool, args.spec, args.cwd, effort, args.wait,
                                            args.timeout, **speed_flags)
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
            fallback_pool, _ = choose_auto_pool(
                args.difficulty,
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


if __name__ == "__main__":
    main()

