#!/usr/bin/env python3
"""Detect unstarted subscription quota windows and wake them with one tiny ping.

A 5h/7d quota window only starts counting down after the first request. A
1-word ping uses so few tokens that upstream still reports 100% remaining, so
"100% left" alone cannot tell an unstarted window from a freshly started one.
The reset timestamp can:

* Claude Code Team: ``five_hour.resets_at`` is null until the window starts.
* Antigravity / Codex: an unstarted window reports a *sliding* reset
  (``now + window``); once started the reset is *pinned* and stops moving.

Waking is deliberately conservative. A ping fires only from fresh, first-party
quota data, after the reset was observed sliding, outside the current woken
cycle and the per-pool cooldown, and after a second live re-check. Every doubt
resolves to "do not ping".

State lives in ``~/.gemini/antigravity/pool_wake_state.json``
(``MAESTRO_POOL_WAKE_STATE`` overrides it).
"""
import contextlib
import copy
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

DEFAULT_STATE_PATH = "~/.gemini/antigravity/pool_wake_state.json"
CLAUDE_USAGE_CACHE = "~/.gemini/antigravity/claude_usage_cache.json"
CODEX_USAGE_CACHE = "/tmp/agy_codex_usage_cache.json"

WAKE_COOLDOWN_SEC = 1800          # never ping one pool twice within 30 minutes
MAX_COOLDOWN_SEC = 6 * 3600       # failure backoff ceiling
WAKE_CHECK_INTERVAL_SEC = 300     # daemon runs the routine at most every 5 minutes
REPEAT_TOLERANCE_SEC = 5          # same reset seen again within this -> pinned
REPEAT_MIN_GAP_SEC = 90           # ...this far apart (longer than any quota cache TTL)
SLIDE_FRESH_SEC = 900             # sliding evidence must be this recent to wake
WAKE_PROMPT = "Reply with OK"

AGY_GEMINI_POOL = "Gemini Pool (Argon / Flash)"
AGY_3P_POOL = "Claude & GPT-OSS Pool (Antigravity)"

# span: window length; slack: how far below span a reset may sit and still
# count as sliding (unstarted). Anything earlier is pinned (started).
POOLS = {
    "agy_gemini_5h": {"span": 18000, "slack": 120, "model": "MODEL_PLACEHOLDER_M322", "settle": 5},
    "agy_claude_5h": {"span": 18000, "slack": 120, "model": "MODEL_PLACEHOLDER_M402", "settle": 6},
    "claude_team_5h": {"span": 18000, "slack": 120},
    "codex_promax_7d": {"span": 604800, "slack": 300},
}
SLIDING_POOLS = ("agy_gemini_5h", "agy_claude_5h", "codex_promax_7d")

_run_lock = threading.Lock()
_schedule_lock = threading.Lock()
_last_scheduled = 0.0


def state_path() -> str:
    return os.path.expanduser(os.environ.get("MAESTRO_POOL_WAKE_STATE") or DEFAULT_STATE_PATH)


def load_state(path: str | None = None) -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: str, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".pool_wake_state.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def update_state(mutate, path: str | None = None):
    """Locked read-modify-write; writes only when the state actually changed."""
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path)
        before = json.dumps(state, sort_keys=True)
        result = mutate(state)
        if json.dumps(state, sort_keys=True) != before:
            _write_state(path, state)
        return result


def _num(value) -> float | None:
    return float(value) if type(value) in (int, float) else None


def pool_view(usage: dict, key: str) -> dict:
    """The pool's quota window plus whether its source is fresh first-party data."""
    usage = usage if isinstance(usage, dict) else {}
    if key.startswith("agy_"):
        ag = usage.get("antigravity") or {}
        pool = (ag.get("pools") or {}).get(AGY_GEMINI_POOL if key == "agy_gemini_5h" else AGY_3P_POOL) or {}
        window, weekly = pool.get("five_hour"), pool.get("weekly")
        if ag.get("error") or not isinstance(window, dict):
            return {"window": None, "valid": False, "reason": "antigravity quota unavailable"}
        if pool.get("quota_source") != "RetrieveUserQuotaSummary":
            return {"window": window, "valid": False, "reason": "fallback quota source"}
    elif key == "claude_team_5h":
        cl = usage.get("claude_local") or {}
        window, weekly = cl.get("five_hour"), cl.get("weekly")
        if not isinstance(window, dict) or window.get("error"):
            return {"window": None, "valid": False, "reason": "claude quota unavailable"}
        if not cl.get("authenticated") or not cl.get("quota_available") or cl.get("stale") is not False:
            return {"window": window, "valid": False, "reason": "claude quota stale or unauthenticated"}
    elif key == "codex_promax_7d":
        cx = usage.get("codex_local") or {}
        window = weekly = cx.get("weekly")
        if cx.get("error") or not isinstance(window, dict):
            return {"window": None, "valid": False, "reason": "codex quota unavailable"}
        if not cx.get("quota_available") or cx.get("stale") is not False or cx.get("rate_limited"):
            return {"window": window, "valid": False, "reason": "codex quota stale or rate limited"}
    else:
        return {"window": None, "valid": False, "reason": "unknown pool"}
    weekly_rem = _num((weekly or {}).get("remaining_percent")) if isinstance(weekly, dict) else None
    if weekly_rem is not None and weekly_rem <= 0.0:
        return {"window": window, "valid": False, "reason": "weekly quota exhausted"}
    return {"window": window, "valid": True, "reason": ""}


def classify(key: str, window: dict | None, entry: dict, now: float) -> str:
    """'used' | 'pinned' (started, still 100%) | 'sliding' (unstarted) | 'unknown'."""
    if not isinstance(window, dict):
        return "unknown"
    rem = _num(window.get("remaining_percent"))
    if rem is None:
        return "unknown"
    if rem < 100.0:
        return "used"
    reset = _num(window.get("reset_epoch"))
    if key == "claude_team_5h":
        # Claude reports no reset at all until the first request of a window.
        if reset is None:
            return "sliding" if window.get("reset_time_kst") == "대기 (미사용)" else "unknown"
        return "pinned" if reset > now else "unknown"
    if reset is None or reset <= now:
        return "unknown"
    cfg = POOLS[key]
    if reset - now < cfg["span"] - cfg["slack"]:
        return "pinned"
    obs_reset, obs_ts = _num(entry.get("obs_reset_epoch")), _num(entry.get("obs_ts"))
    if obs_reset is not None and obs_ts is not None and abs(reset - obs_reset) <= REPEAT_TOLERANCE_SEC \
            and now - obs_ts >= REPEAT_MIN_GAP_SEC:
        return "pinned"
    active_until = _num(entry.get("active_until_epoch")) or 0.0
    if now < active_until and reset <= active_until + cfg["slack"]:
        return "pinned"  # we woke this cycle and the reset has not moved past it
    return "sliding"


def _observe(entry: dict, reset: float | None, now: float) -> None:
    """Remember the reset; flag 'sliding' when it advanced in step with the clock."""
    if reset is None:
        return
    obs_reset, obs_ts = _num(entry.get("obs_reset_epoch")), _num(entry.get("obs_ts"))
    if obs_reset is not None and obs_ts is not None and abs(reset - obs_reset) <= REPEAT_TOLERANCE_SEC:
        return
    if obs_reset is not None and obs_ts is not None:
        elapsed, drift = now - obs_ts, reset - obs_reset
        if elapsed >= REPEAT_TOLERANCE_SEC and abs(drift - elapsed) <= max(10.0, 0.1 * elapsed):
            entry["slide_confirmed_ts"] = round(now, 3)
    entry["obs_reset_epoch"] = round(reset, 3)
    entry["obs_ts"] = round(now, 3)


def annotate_window_activity(usage: dict, now: float | None = None, path: str | None = None) -> dict:
    """Mark started-but-100% windows as active and record reset observations.

    Sets ``started`` on each tracked window and upgrades ``status`` to "활성"
    for pinned windows, so the widget shows 사용중 + countdown instead of 미사용.
    Returns {pool_key: classification}.
    """
    now = time.time() if now is None else now
    views = {key: pool_view(usage, key) for key in POOLS}

    def mutate(state):
        result = {}
        for key, view in views.items():
            window = view["window"]
            if not isinstance(window, dict):
                continue
            entry = state.setdefault(key, {})
            kind = classify(key, window, entry, now)
            result[key] = kind
            if key in SLIDING_POOLS and view["valid"]:
                _observe(entry, _num(window.get("reset_epoch")), now)
            if not entry:
                state.pop(key, None)
        return result

    try:
        kinds = update_state(mutate, path)
    except OSError:
        kinds = {key: classify(key, v["window"], {}, now) for key, v in views.items() if isinstance(v["window"], dict)}
    for key, kind in kinds.items():
        window = views[key]["window"]
        window["started"] = kind in ("used", "pinned")
        if kind == "pinned" and window.get("status") == "가용 (대기)":
            window["status"] = "활성"
    return kinds


def _cooldown(entry: dict) -> float:
    failures = int(_num(entry.get("consecutive_failures")) or 0)
    return min(MAX_COOLDOWN_SEC, WAKE_COOLDOWN_SEC * 2 ** max(0, failures - 1))


def wake_reason(key: str, view: dict, entry: dict, now: float) -> str | None:
    """None when the pool must be woken now, else why it must not be."""
    if not view.get("valid"):
        return view.get("reason") or "invalid quota data"
    window = view["window"]
    active_until = _num(entry.get("active_until_epoch")) or 0.0
    if now < active_until:
        return "already woken this window"
    last_attempt = _num(entry.get("last_wake_attempt_ts")) or 0.0
    if now - last_attempt < _cooldown(entry):
        return "cooldown"
    if _num(window.get("remaining_percent")) != 100.0 or (_num(window.get("used_percent")) or 0.0) != 0.0:
        return "window in use"
    if key == "claude_team_5h":
        if window.get("status") != "가용 (대기)" or window.get("reset_time_kst") != "대기 (미사용)" \
                or window.get("reset_epoch") is not None:
            return "window started"
        return None
    kind = classify(key, window, entry, now)
    if kind != "sliding":
        return "reset time unknown" if kind == "unknown" else "window started"
    cfg = POOLS[key]
    reset = _num(window.get("reset_epoch"))
    if reset is None or reset - now < cfg["span"] - cfg["slack"]:
        return "window started"
    if now - (_num(entry.get("slide_confirmed_ts")) or 0.0) > SLIDE_FRESH_SEC:
        return "sliding reset not confirmed"
    return None


# --- wake actions -----------------------------------------------------------

def _find_exe(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidates = [os.path.expanduser(f"~/.local/bin/{name}")]
    candidates += sorted(glob.glob(os.path.expanduser(f"~/.config/nvm/versions/node/*/bin/{name}")), reverse=True)
    candidates += sorted(glob.glob(os.path.expanduser(f"~/.nvm/versions/node/*/bin/{name}")), reverse=True)
    return next((c for c in candidates if os.access(c, os.X_OK)), None)


def _run_cli(argv: list[str], timeout: float) -> tuple[bool, str]:
    exe = _find_exe(argv[0])
    if not exe:
        return False, f"{argv[0]} not found"
    env = dict(os.environ, MAESTRO_WORKER_ACTIVE="1")
    # nvm-installed CLIs need their node next to them on PATH under systemd.
    env["PATH"] = os.pathsep.join([os.path.dirname(exe), env.get("PATH", "")])
    with tempfile.TemporaryDirectory(prefix="maestro-wake-") as cwd:
        try:
            proc = subprocess.run([exe, *argv[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except OSError as exc:
            return False, type(exc).__name__
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {(proc.stderr or out).strip()[-200:]}"
    return "OK" in out.upper(), out[-80:]


def _expire_cache(path: str) -> None:
    """Force the next quota read to refetch while keeping the 429 fallback copy."""
    path = os.path.expanduser(path)
    with contextlib.suppress(OSError):
        os.utime(path, (0, 0))


def wake_claude_team() -> tuple[bool, str]:
    ok, detail = _run_cli(["claude", "-p", "--model", "sonnet", "--effort", "low",
                           "--no-session-persistence", "--strict-mcp-config", WAKE_PROMPT], timeout=25)
    _expire_cache(CLAUDE_USAGE_CACHE)
    return ok, detail


def wake_codex_promax() -> tuple[bool, str]:
    ok, detail = _run_cli(["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                           "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
                           "-c", 'service_tier="default"', WAKE_PROMPT], timeout=45)
    with contextlib.suppress(OSError):
        os.remove(CODEX_USAGE_CACHE)
    return ok, detail


def _ls_call(addr: str, token: str, method: str, payload: dict, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                 "x-codeium-csrf-token": token or ""},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _cascade_answered(trajectory: dict) -> bool:
    steps = (trajectory.get("trajectory") or {}).get("steps") or []
    return any("PLANNER_RESPONSE" in str(s.get("type")) and "DONE" in str(s.get("status"))
               for s in steps if isinstance(s, dict))


def wake_antigravity(model: str, settle: float, max_wait: float = 20.0) -> tuple[bool, str]:
    """Temporary cascade: start -> one message -> wait for the reply -> delete."""
    import check_all_usage as cau
    addr, token = cau.discover_ls_credentials()
    if not addr:
        return False, "language server not found"
    meta = {"ideName": "antigravity", "apiKey": "", "locale": "ko", "ideVersion": "2.23.1",
            "extensionName": "antigravity"}
    cid = _ls_call(addr, token, "StartCascade", {
        "metadata": meta, "source": "CORTEX_TRAJECTORY_SOURCE_VSCODE",
        "workspaceUris": [f"file://{os.path.expanduser('~/dev')}"],
    }).get("cascadeId")
    if not cid:
        return False, "StartCascade returned no cascadeId"
    answered = False
    try:
        _ls_call(addr, token, "SendUserCascadeMessage", {
            "cascadeId": cid, "items": [{"text": WAKE_PROMPT}], "metadata": meta,
            "cascadeConfig": {"plannerConfig": {"planModel": model, "conversational": {
                "plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT", "agenticMode": True}}},
        })
        time.sleep(settle)
        deadline = time.monotonic() + max_wait - settle
        while True:
            with contextlib.suppress(Exception):
                answered = _cascade_answered(_ls_call(addr, token, "GetCascadeTrajectory", {"cascadeId": cid}))
            if answered or time.monotonic() >= deadline:
                break
            time.sleep(2)
    finally:
        with contextlib.suppress(Exception):
            _ls_call(addr, token, "DeleteCascadeTrajectory", {"cascadeId": cid})
    return answered, "replied" if answered else "no reply"


def _default_actions() -> dict:
    return {
        "agy_gemini_5h": lambda: wake_antigravity(POOLS["agy_gemini_5h"]["model"], POOLS["agy_gemini_5h"]["settle"]),
        "agy_claude_5h": lambda: wake_antigravity(POOLS["agy_claude_5h"]["model"], POOLS["agy_claude_5h"]["settle"]),
        "claude_team_5h": wake_claude_team,
        "codex_promax_7d": wake_codex_promax,
    }


def _fresh_usage(key: str) -> dict:
    """Bypass every cache for the one pool about to be pinged."""
    import check_all_usage as cau
    if key.startswith("agy_"):
        return {"antigravity": cau.get_antigravity_usage()}
    if key == "claude_team_5h":
        status = cau.get_claude_status(max_cache_age=0)
        if not str(status.get("source") or "").startswith("https://"):
            status["stale"] = True  # live fetch failed; cached data never triggers a ping
        return {"claude_local": status}
    usage = cau.get_codex_usage(ttl_sec=0)
    if not str(usage.get("source") or "").startswith("https://"):
        usage["stale"] = True
    return {"codex_local": usage}


def ensure_subscription_windows_awake(usage_data: dict, *, now=None, path: str | None = None,
                                      actions: dict | None = None, revalidate=None) -> dict:
    """Wake each unstarted pool at most once per window; returns {pool: outcome}."""
    if not _run_lock.acquire(blocking=False):
        return {"_": "already running"}
    path = path or state_path()
    clock = (lambda: now) if now is not None else time.time
    actions = _default_actions() if actions is None else actions
    revalidate = _fresh_usage if revalidate is None else revalidate
    outcomes: dict = {}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".run.lock", "a") as run_lock:
            try:
                fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"_": "already running"}
            annotate_window_activity(usage_data, clock(), path)
            for key in POOLS:
                if key not in actions:
                    continue
                entry = load_state(path).get(key, {})
                reason = wake_reason(key, pool_view(usage_data, key), entry, clock())
                if reason is None:
                    try:
                        fresh = revalidate(key)
                        annotate_window_activity(fresh, clock(), path)
                        entry = load_state(path).get(key, {})
                        reason = wake_reason(key, pool_view(fresh, key), entry, clock())
                        window = pool_view(fresh, key)["window"] or {}
                    except Exception as exc:
                        reason = f"revalidation failed: {type(exc).__name__}"
                if reason is not None:
                    outcomes[key] = f"skip: {reason}"
                    continue
                outcomes[key] = _fire(key, actions[key], window, clock, path)
    finally:
        _run_lock.release()
    return outcomes


def _fire(key: str, action, window: dict, clock, path: str) -> str:
    started = clock()

    def attempt(state):
        state.setdefault(key, {})["last_wake_attempt_ts"] = round(started, 3)
    update_state(attempt, path)  # recorded first: a crash mid-ping still counts
    try:
        ok, detail = action()
    except Exception as exc:
        ok, detail = False, type(exc).__name__
    finished = clock()

    def result(state):
        entry = state.setdefault(key, {})
        if ok:
            reset = _num(window.get("reset_epoch"))
            until = reset if key == "codex_promax_7d" and reset else started + POOLS[key]["span"]
            entry.update(last_wake_ok_ts=round(finished, 3), active_until_epoch=round(until, 3),
                         consecutive_failures=0)
            entry.pop("last_error", None)
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
            entry["last_error"] = str(detail)[:200]
    update_state(result, path)
    print(f"[pool-wake] {key}: {'woken' if ok else 'failed'} ({detail})", file=sys.stderr, flush=True)
    return "woken" if ok else f"failed: {detail}"


def maybe_wake_async(usage_data: dict, interval: float = WAKE_CHECK_INTERVAL_SEC) -> bool:
    """Daemon hook: run the routine in a background thread at most every `interval` seconds."""
    global _last_scheduled
    if os.environ.get("MAESTRO_AUTO_WAKE", "1") == "0" or not isinstance(usage_data, dict):
        return False
    with _schedule_lock:
        now = time.monotonic()
        if _last_scheduled and now - _last_scheduled < interval:
            return False
        _last_scheduled = now

    snapshot = copy.deepcopy({k: usage_data.get(k) for k in ("antigravity", "codex_local", "claude_local")})

    def run():
        try:
            ensure_subscription_windows_awake(snapshot)
        except Exception as exc:  # waking is best-effort and must never break the daemon
            print(f"[pool-wake] error: {exc}", file=sys.stderr, flush=True)
    threading.Thread(target=run, name="pool-wake", daemon=True).start()
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Report decisions without pinging")
    args = parser.parse_args()
    import check_all_usage as cau
    data = cau.collect_usage(include_agent_trees=False)
    if args.dry_run:
        state = load_state()
        print(json.dumps({key: wake_reason(key, pool_view(data, key), state.get(key, {}), time.time()) or "would wake"
                          for key in POOLS}, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(ensure_subscription_windows_awake(data), ensure_ascii=False, indent=1))
