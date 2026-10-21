"""Reap maestro spawn registry entries whose worker process is gone.

maestro writes "running" before launching a worker and relies on a monitor
(an in-process daemon thread for background launches) to write the terminal
status. When that process exits first, the entry stays "running" forever. A
dead entry is moved to "completed" and stamped with its last real activity
time, never the reap time, so an old worker does not look freshly finished.
"""
import fcntl
import glob
import json
import os
import tempfile
import time

REGISTRY_PATH = "~/.gemini/antigravity/agy_spawned_sessions.json"
ACTIVE_STATUSES = ("running", "starting", "active")
# maestro registers "running" before Popen returns; a pid-less entry younger
# than this is still being launched.
PIDLESS_GRACE_SEC = 30
# A worker starts within seconds of its registration. A process with the same
# pid that started much later is an unrelated process that reused the pid.
PID_REUSE_SLACK_SEC = 120


def _proc_start_epoch(pid: int, proc_root: str) -> float | None:
    try:
        with open(os.path.join(proc_root, "stat"), encoding="ascii", errors="replace") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            stat = f.read()
        ticks = int(stat[stat.rfind(")") + 1:].split()[19])
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def pid_alive(pid, proc_root: str = "/proc", started_at=None) -> bool:
    """True when /proc/<pid> exists, is not a zombie, and was not reused."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return False
    fields = stat[stat.rfind(")") + 1:].split()
    if not fields or fields[0] in ("Z", "X"):
        return False
    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
        begun = _proc_start_epoch(pid, proc_root)
        if begun is not None and begun > started_at + PID_REUSE_SLACK_SEC:
            return False
    return True


def entry_process_alive(entry: dict, proc_root: str = "/proc") -> bool:
    """The worker pid (background launch) or the waiting maestro supervisor is alive."""
    started = _num(entry.get("started_at"))
    return (pid_alive(entry.get("pid"), proc_root, started)
            or pid_alive(entry.get("supervisor_pid"), proc_root))


def live_session_ids(proc_root: str = "/proc", home: str | None = None) -> set[str]:
    """Session ids held by a running claude / codex process right now."""
    ids = set()
    try:
        from live_worker_processes import scan_live_worker_processes
        for process in scan_live_worker_processes(proc_root):
            ids.update(v for v in (process.get("session_id"), process.get("registry_id")) if v)
    except Exception:
        pass
    home = home or os.path.expanduser("~")
    for path in glob.glob(os.path.join(home, ".claude", "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                session = json.load(f)
            sid = session.get("sessionId") or session.get("session_id")
            if sid and pid_alive(session.get("pid"), proc_root):
                ids.add(sid)
        except (OSError, ValueError, AttributeError):
            continue
    return ids


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _transcript_mtime(entry: dict, key: str, home: str) -> float | None:
    """Last write to the worker's own transcript is when it actually stopped."""
    ids = {v for v in (entry.get("session_id"), entry.get("thread_id"), key) if isinstance(v, str) and v}
    patterns = []
    for sid in ids:
        if glob.has_magic(sid):
            continue
        if entry.get("provider") == "claude":
            patterns.append(os.path.join(home, ".claude", "projects", "*", f"{sid}.jsonl"))
        else:
            patterns.append(os.path.join(home, ".codex", "sessions", "*", "*", "*", f"rollout-*-{sid}.jsonl"))
    mtimes = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                mtimes.append(os.path.getmtime(path))
            except OSError:
                pass
    return max(mtimes) if mtimes else None


def finished_at(entry: dict, key: str, now: float, home: str) -> int:
    started = _num(entry.get("started_at"))
    elapsed = _num(entry.get("elapsed_sec"))
    end = None
    if started is not None and elapsed is not None and elapsed >= 0:
        end = started + elapsed
    if end is None:
        end = _transcript_mtime(entry, key, home)
    if end is None:
        end = max(v for v in (_num(entry.get("updated_at")), started, 0.0) if v is not None)
    if started is not None:
        end = max(end, started)
    return int(min(end, now))


def reap_registry(registry: dict, *, now: float | None = None, proc_root: str = "/proc",
                  home: str | None = None, live_ids=None) -> list[str]:
    """Mark dead active entries completed in place; returns the reaped keys."""
    if not isinstance(registry, dict):
        return []
    now = time.time() if now is None else now
    home = home or os.path.expanduser("~")
    suspects = []
    for key, entry in registry.items():
        if not isinstance(entry, dict) or entry.get("status") not in ACTIVE_STATUSES:
            continue
        if entry_process_alive(entry, proc_root):
            continue
        has_pid = isinstance(entry.get("pid"), int) or isinstance(entry.get("supervisor_pid"), int)
        started = _num(entry.get("started_at")) or _num(entry.get("updated_at")) or 0.0
        if not has_pid and now - started <= PIDLESS_GRACE_SEC:
            continue
        suspects.append(key)
    if not suspects:
        return []
    if live_ids is None:
        live_ids = live_session_ids(proc_root, home)
    reaped = []
    for key in suspects:
        entry = registry[key]
        if {key, entry.get("session_id"), entry.get("thread_id")} & set(live_ids):
            continue
        end = finished_at(entry, key, now, home)
        entry.update(status="completed", completed_at=end, updated_at=end, reaped_at=int(now),
                     reap_reason="pid_dead" if entry.get("pid") or entry.get("supervisor_pid") else "pid_missing")
        reaped.append(key)
    return reaped


def _persist(path: str, reaped: dict) -> bool:
    """Apply reaped entries to the latest file under maestro's registry lock."""
    temporary = None
    try:
        with open(path + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(path, encoding="utf-8") as f:
                current = json.load(f)
            if not isinstance(current, dict):
                return False
            changed = False
            for key, entry in reaped.items():
                old = current.get(key)
                # Skip entries a writer has since finished, relaunched or given a pid.
                if (not isinstance(old, dict) or old.get("status") not in ACTIVE_STATUSES
                        or old.get("started_at") != entry.get("started_at")
                        or old.get("pid") != entry.get("pid")
                        or old.get("supervisor_pid") != entry.get("supervisor_pid")):
                    continue
                current[key] = {**old, **{k: entry[k] for k in
                                          ("status", "completed_at", "updated_at", "reaped_at", "reap_reason")}}
                changed = True
            if not changed:
                return False
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                             prefix=".agy_spawned_", delete=False) as f:
                temporary = f.name
                json.dump(current, f, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
            temporary = None
            return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def load_registry(path: str | None = None, *, persist: bool = True, now: float | None = None,
                  proc_root: str = "/proc", home: str | None = None, live_ids=None) -> dict:
    """Read the spawn registry with dead workers already reaped (and persisted)."""
    path = os.path.expanduser(path or REGISTRY_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            registry = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(registry, dict):
        return {}
    reaped = reap_registry(registry, now=now, proc_root=proc_root, home=home, live_ids=live_ids)
    if reaped and persist:
        _persist(path, {key: registry[key] for key in reaped})
    return registry


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    before = {}
    try:
        with open(os.path.expanduser(target or REGISTRY_PATH), encoding="utf-8") as f:
            before = json.load(f)
    except (OSError, ValueError):
        pass
    after = load_registry(target)
    reaped = [k for k, v in after.items() if v.get("reaped_at") and before.get(k, {}).get("status") in ACTIVE_STATUSES]
    still = [k for k, v in after.items() if isinstance(v, dict) and v.get("status") in ACTIVE_STATUSES]
    print(json.dumps({"reaped": reaped, "still_running": still}, indent=2))
