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
  (`codex exec --full-auto -m gpt-6.1-sol -c service_tier="ultrafast"`), balancing quota across ALL pools
  and keeping every conversation's `usage_widget.html` up to date.

Commands:
  maestro status [--json]
  maestro worker-start --spec "<task>" [--pool auto|agy-claude|agy-sonnet|codex|agy-flash] [--cwd <dir>] [--wait] [--timeout 180] [--json]
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

SKILL_SCRIPTS_DIR = "/home/u24c9g5090r64/.gemini/config/skills/multi-agent-orchestrator/scripts"
sys.path.insert(0, SKILL_SCRIPTS_DIR)
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
    if pool == "codex":
        return "codex"
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
            and c.get("pool") != "claude"
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        winner = candidates[0][0] if candidates else winner
    return _map_canonical_to_cli_pool(winner, difficulty), data


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
    import orca_memory
    addr, token = cau.discover_ls_credentials()
    if not addr:
        return {"ok": False, "error": "Could not discover Antigravity LanguageServer"}
    model_id = MODEL_MAP.get(pool, "MODEL_PLACEHOLDER_M402")
    abs_cwd = os.path.abspath(cwd)
    agy_title = make_agy_title(spec, abs_cwd)
    handover_ctx = orca_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
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
        orca_memory.save_handover_note(
            cwd=abs_cwd,
            pool=pool,
            model=model_id,
            title=agy_title,
            summary=result["output"][:280],
            session_id=cid,
        )
    orca_memory.notify_long_task_completion(
        session_id=cid,
        title=agy_title,
        pool=pool,
        elapsed_sec=elapsed,
        cwd=abs_cwd,
        ok=True,
        min_elapsed_sec=60.0,
    )
    return result


def start_codex_worker(spec: str, cwd: str, effort: str, tier: str, timeout: int) -> dict:
    import shutil
    import threading
    import orca_memory
    abs_cwd = os.path.abspath(cwd)
    agy_title = make_agy_title(spec, abs_cwd)
    handover_ctx = orca_memory.format_handover_context_for_prompt(abs_cwd, max_age_seconds=7200)
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
        "gpt-6.1-sol",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        f'service_tier="{tier}"',
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
            orca_memory.save_handover_note(
                cwd=abs_cwd,
                pool="codex",
                model=f"gpt-6.1-sol ({effort}, {tier})",
                title=agy_title,
                summary=out_txt[:280],
                session_id=sid,
            )
        orca_memory.notify_long_task_completion(
            session_id=sid,
            title=agy_title,
            pool="codex",
            elapsed_sec=elapsed,
            cwd=abs_cwd,
            ok=(proc.returncode == 0),
            min_elapsed_sec=60.0,
        )
        return {
            "ok": proc.returncode == 0,
            "pool": "codex",
            "model": f"gpt-6.1-sol ({effort}, {tier})",
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
            "pool": "codex",
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
        choices=["auto", "agy-argon", "argon", "agy-argon-med", "agy-claude", "opus-5.5", "agy-sonnet", "sonnet-5.5", "agy-gpt-oss", "codex", "agy-flash", "flash"],
    )
    p_start.add_argument("--difficulty", default="high", choices=["high", "medium", "low"])
    p_start.add_argument("--cwd", default=os.getcwd())
    p_start.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "ultra"])
    p_start.add_argument("--tier", default="priority", choices=["priority", "ultrafast"])
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
        import orca_memory
        requested_pool = args.pool
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
            if target_pool == "codex":
                eff_tier = "ultrafast" if args.urgent else args.tier
                return start_codex_worker(args.spec, args.cwd, args.effort, eff_tier, args.timeout)
            return start_agy_worker(target_pool, args.spec, args.cwd, args.wait, args.timeout)

        res = _dispatch(pool)

        # Mid-flight quota exhaustion / rate-limit failover steering
        combined_err = f"{res.get('error','')} {res.get('stderr','')} {res.get('output','')} {res.get('status','')}".lower()
        quota_hit_signals = ("quota", "exhausted", "rate limit", "429", "limit reached", "cascade_run_status_error")
        midflight_exhausted = (not res.get("ok") or "CASCADE_RUN_STATUS_ERROR" in str(res.get("status", ""))) and any(
            sig in combined_err for sig in quota_hit_signals
        )
        if not midflight_exhausted and (not res.get("ok") or "CASCADE_RUN_STATUS_ERROR" in str(res.get("status", ""))):
            post_data = cau.collect_usage(include_agent_trees=False)
            midflight_exhausted, _ = is_pool_exhausted(pool, post_data)

        if midflight_exhausted:
            failed_pool = pool
            fallback_pool, _ = choose_auto_pool(
                args.difficulty,
                exclude_canonical={_canonical_pool_key(failed_pool), _canonical_pool_key(requested_pool)},
            )
            if fallback_pool and _canonical_pool_key(fallback_pool) != _canonical_pool_key(failed_pool):
                orca_memory.save_handover_note(
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
        import orca_memory
        res = orca_memory.save_handover_note(
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
        import orca_memory
        repo, branch = orca_memory._git_repo_and_branch(args.cwd)
        notes = orca_memory.get_recent_handover_notes(
            repo=repo,
            branch=branch,
            max_age_seconds=args.max_age,
            limit=args.limit,
        )
        print(json.dumps({"ok": True, "repo": repo, "branch": branch, "notes": notes}, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()

