#!/usr/bin/env python3
"""
Unified AI Quota & Usage Inspector for:
1. Google Antigravity (Gemini Pool + Claude/GPT-OSS Pool via local Language Server)
2. OpenAI Codex CLI / Desktop (ProMax Plan via Wham backend API + session fallback)
3. Anthropic Claude Code CLI (OAuth Usage API with 5h + 7d quotas + session/MCP status)
4. Multi-Agent Hierarchy Trees (Parent -> Subagent -> Sub-subagent for Antigravity & Codex)
"""
import argparse
import copy
import datetime
import glob
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from datetime import timezone, timedelta
from typing import Any

KST = timezone(timedelta(hours=9))
ROUTING_MAX_AGE_SECONDS = 300
CLAUDE_CACHE_MAX_AGE_SECONDS = 600
AGY_TREE_CACHE_PATH = "/tmp/agy_tree_cache.json"
AGY_METADATA_CACHE_PATH = "/tmp/agy_traj_meta_cache.json"
_agy_tree_cache = None

ANTIGRAVITY_MODEL_MAP = {
    "MODEL_PLACEHOLDER_M417": "Gemini 4 Argon (High)",
    "MODEL_PLACEHOLDER_M418": "Gemini 4 Argon (Medium)",
    "MODEL_PLACEHOLDER_M419": "Gemini 4 Argon (Low)",
    "MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High · Fast)",
    "MODEL_PLACEHOLDER_M319": "Gemini 3.8 Flash (Medium · Fast)",
    "MODEL_PLACEHOLDER_M320": "Gemini 3.8 Flash (Low · Fast)",
    "MODEL_PLACEHOLDER_M322": "Gemini 3.8 Flash (Fast)",
    "MODEL_PLACEHOLDER_M402": "Claude Opus 5.5 (High)",
    "MODEL_PLACEHOLDER_M401": "Claude Opus 5.5 (Medium)",
    "MODEL_PLACEHOLDER_M400": "Claude Opus 5.5 (Low)",
    "MODEL_PLACEHOLDER_M405": "Claude Sonnet 5.5 (High)",
    "MODEL_PLACEHOLDER_M404": "Claude Sonnet 5.5 (Medium)",
    "MODEL_PLACEHOLDER_M403": "Claude Sonnet 5.5 (Low)",
    "MODEL_OPENAI_GPT_OSS_120B_MEDIUM": "GPT-OSS 120B (Medium)",
}


def get_last_selected_agy_model() -> str | None:
    state_path = os.path.expanduser("~/.gemini/antigravity/antigravity_state.pbtxt")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "last_selected_agent_model:" in line:
                        return line.split(":", 1)[1].strip().strip('"')
        except Exception:
            pass
    return None



def fmt_iso_to_kst(iso_str: str | None, is_reset: bool = True) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(KST)
        now = datetime.datetime.now(KST)
        diff = dt - now
        mins = int(diff.total_seconds() // 60)
        if mins >= 1440:
            rel = f"{mins // 1440}일 {(mins % 1440) // 60}시간 후"
        elif mins > 0:
            rel = f"{mins // 60}시간 {mins % 60}분 후"
        elif mins == 0:
            rel = "곧 리셋"
        else:
            if is_reset:
                rel = "리셋 완료"
            else:
                past_mins = abs(mins)
                if past_mins >= 1440:
                    rel = f"{past_mins // 1440}일 전"
                elif past_mins >= 60:
                    rel = f"{past_mins // 60}시간 {past_mins % 60}분 전"
                else:
                    rel = f"{past_mins}분 전"
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S KST')} ({rel})"
    except Exception:
        return str(iso_str)


def fmt_ts_to_kst(ts: int | float | None, is_reset: bool = True) -> str:
    if not ts:
        return "N/A"
    try:
        dt = datetime.datetime.fromtimestamp(float(ts), tz=KST)
        now = datetime.datetime.now(KST)
        diff = dt - now
        mins = int(diff.total_seconds() // 60)
        if mins >= 1440:
            rel = f"{mins // 1440}일 {(mins % 1440) // 60}시간 후"
        elif mins > 0:
            rel = f"{mins // 60}시간 {mins % 60}분 후"
        elif mins == 0:
            rel = "곧 리셋"
        else:
            if is_reset:
                rel = "리셋 완료"
            else:
                past_mins = abs(mins)
                if past_mins >= 1440:
                    rel = f"{past_mins // 1440}일 전"
                elif past_mins >= 60:
                    rel = f"{past_mins // 60}시간 {past_mins % 60}분 전"
                else:
                    rel = f"{past_mins}분 전"
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S KST')} ({rel})"
    except Exception:
        return str(ts)


def discover_ls_credentials():
    """Find and validate the local LS, including outside the IDE environment."""
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    cache_path = "/tmp/agy_ls_creds_cache.json"
    attempted = set()

    def valid(candidate_addr, candidate_token, timeout):
        if not isinstance(candidate_addr, str) or not candidate_addr:
            return False
        if not isinstance(candidate_token, str) or not candidate_token:
            return False
        candidate = (candidate_addr, candidate_token)
        if candidate in attempted:
            return False
        attempted.add(candidate)
        try:
            req = urllib.request.Request(
                f"http://{candidate_addr}/exa.language_server_pb.LanguageServerService/GetUserStatus",
                data=b"{}",
                headers={"Content-Type": "application/json", "x-codeium-csrf-token": candidate_token},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            return isinstance(data, dict) and isinstance(data.get("userStatus"), dict)
        except Exception:
            return False

    def remember(candidate_addr, candidate_token):
        temp_path = None
        try:
            # Atomic replacement keeps concurrent widget requests from reading
            # a partial file; NamedTemporaryFile restricts the token to its owner.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir="/tmp", prefix="agy_ls_creds_", delete=False
            ) as f:
                temp_path = f.name
                json.dump({"addr": candidate_addr, "token": candidate_token}, f)
            os.replace(temp_path, cache_path)
        except OSError:
            pass  # A read-only/full cache directory must not break discovery.
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        return candidate_addr, candidate_token

    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, dict) and valid(cached.get("addr"), cached.get("token"), 1):
            return cached["addr"], cached["token"]
    except (OSError, ValueError):
        pass

    if valid(addr, token, 1):
        return remember(addr, token)

    try:
        pids = subprocess.check_output(
            ["pgrep", "-f", "language_server"], text=True, stderr=subprocess.DEVNULL, timeout=2
        ).split()
    except (OSError, subprocess.SubprocessError):
        pids = [os.path.basename(path) for path in glob.glob("/proc/[0-9]*")]

    servers = {}
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [os.fsdecode(arg) for arg in f.read().split(b"\0") if arg]
            # pgrep also matches shells/workers mentioning LS in their task text.
            if not args or os.path.basename(args[0]) != "language_server":
                continue
            for i, arg in enumerate(args):
                if arg == "--csrf_token" and i + 1 < len(args):
                    servers[pid] = args[i + 1]
                    break
                if arg.startswith("--csrf_token="):
                    servers[pid] = arg.split("=", 1)[1]
                    break
        except OSError:
            continue

    if not servers:
        return None, None

    ports = {pid: set() for pid in servers}
    try:
        listeners = subprocess.check_output(
            ["ss", "-H", "-tlnp"], text=True, stderr=subprocess.DEVNULL, timeout=2
        )
        for line in listeners.splitlines():
            fields = line.split()
            if len(fields) < 5 or not fields[3].startswith("127.0.0.1:"):
                continue
            port = int(fields[3].rsplit(":", 1)[1])
            for pid in servers:
                if f"pid={pid}," in line:
                    ports[pid].add(port)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    # Also works when ss is unavailable or omits process ownership information.
    try:
        with open("/proc/net/tcp", encoding="ascii") as f:
            tcp_ports = {}
            for line in f:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "0A":
                    host, port = fields[1].split(":")
                    if host == "0100007F":  # 127.0.0.1 in /proc's byte order
                        tcp_ports[f"socket:[{fields[9]}]"] = int(port, 16)
        for pid in servers:
            for fd in glob.glob(f"/proc/{pid}/fd/*"):
                try:
                    port = tcp_ports.get(os.readlink(fd))
                    if port is not None:
                        ports[pid].add(port)
                except OSError:
                    continue
    except (OSError, ValueError):
        pass

    for pid, candidate_token in servers.items():
        for port in sorted(ports[pid]):
            candidate_addr = f"127.0.0.1:{port}"
            if valid(candidate_addr, candidate_token, 1.5):
                return remember(candidate_addr, candidate_token)
    return None, None


AGY_GEMINI_POOL = "Gemini Pool (Argon / Flash)"
AGY_3P_POOL = "Claude & GPT-OSS Pool (Antigravity)"


def _agy_quota_window(rem, rst) -> dict:
    """Build a five_hour/weekly window dict from a remainingFraction + ISO resetTime."""
    try:
        rem = float(rem)
    except (TypeError, ValueError):
        rem = 0.0
    rem_pct = round(rem * 100, 2)
    if rem < 1.0 and rem_pct >= 100.0:
        rem_pct = 99.99  # in-use pool must never round up to 100% (unused)
    return {
        "remaining_percent": rem_pct,
        "used_percent": round(100.0 - rem_pct, 2),
        "reset_time_kst": fmt_iso_to_kst(rst),
        "status": "한도 도달" if rem <= 0.0 else ("활성" if rem < 1.0 else "가용 (대기)"),
    }


def _agy_retrieve_quota_summary(addr: str, token: str | None) -> dict:
    """Return {pool_key: {"five_hour": {...}|None, "weekly": {...}|None}} from RetrieveUserQuotaSummary."""
    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode())
    groups = (data.get("response") or {}).get("groups") or []
    out: dict = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = g.get("displayName") or ""
        buckets = g.get("buckets") or []
        bucket_ids = [str(b.get("bucketId") or "") for b in buckets if isinstance(b, dict)]
        if gname == "Gemini Models" or any(b.startswith("gemini-") for b in bucket_ids):
            pool_key, prefix = AGY_GEMINI_POOL, "gemini"
        elif gname == "Claude and GPT models" or any(b.startswith("3p-") for b in bucket_ids):
            pool_key, prefix = AGY_3P_POOL, "3p"
        else:
            continue
        entry = out.setdefault(pool_key, {"five_hour": None, "weekly": None})
        for b in buckets:
            if not isinstance(b, dict):
                continue
            bid = b.get("bucketId") or ""
            win = b.get("window") or ""
            window = _agy_quota_window(b.get("remainingFraction", 0.0), b.get("resetTime", ""))
            if bid == f"{prefix}-5h" or win == "5h":
                entry["five_hour"] = window
            elif bid == f"{prefix}-weekly" or win == "weekly":
                entry["weekly"] = window
    if not out:
        raise ValueError("RetrieveUserQuotaSummary returned no recognizable quota groups")
    return out


def get_antigravity_usage() -> dict:
    addr, token = discover_ls_credentials()
    if not addr:
        return {"error": "ANTIGRAVITY_LS_ADDRESS not set in environment"}
    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/GetUserStatus",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        us = data.get("userStatus", {})
        models = us.get("cascadeModelConfigData", {}).get("clientModelConfigs", [])

        try:
            summary = _agy_retrieve_quota_summary(addr, token)
            quota_source = "RetrieveUserQuotaSummary"
        except Exception:
            summary = {}
            quota_source = "GetUserStatus"

        pools = {}
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for m in models:
            label = m.get("label", "")
            pool_key = AGY_GEMINI_POOL if "Gemini" in label else AGY_3P_POOL
            if pool_key not in pools:
                windows = summary.get(pool_key)
                if not windows:
                    # Fallback: GetUserStatus exposes a single quotaInfo per model;
                    # a reset > 6h away can only belong to the weekly window.
                    q = m.get("quotaInfo", {}) or {}
                    rst = q.get("resetTime", "")
                    window = _agy_quota_window(q.get("remainingFraction", 0.0), rst)
                    is_weekly = False
                    try:
                        reset_dt = datetime.datetime.fromisoformat(rst.replace("Z", "+00:00"))
                        is_weekly = (reset_dt - now_utc).total_seconds() > 6 * 3600
                    except Exception:
                        pass
                    if is_weekly:
                        windows = {"five_hour": _agy_quota_window(1.0, ""), "weekly": window}
                    else:
                        windows = {"five_hour": window, "weekly": None}
                pools[pool_key] = {
                    "five_hour": windows.get("five_hour"),
                    "weekly": windows.get("weekly"),
                    "models": [],
                    "quota_source": quota_source if summary.get(pool_key) else "GetUserStatus",
                }
            if label not in pools[pool_key]["models"]:
                pools[pool_key]["models"].append(label)

        # Pools present in the summary but without a model entry still deserve a row.
        for pool_key, windows in summary.items():
            if pool_key not in pools:
                pools[pool_key] = {
                    "five_hour": windows.get("five_hour"),
                    "weekly": windows.get("weekly"),
                    "models": [],
                    "quota_source": quota_source,
                }

        # Keep 5-hour (short-term burst) and 7-day (weekly budget) strictly separate.
        for pool in pools.values():
            fh = pool.get("five_hour") if isinstance(pool.get("five_hour"), dict) else None
            wk = pool.get("weekly") if isinstance(pool.get("weekly"), dict) else None
            fh_rem = float(fh["remaining_percent"]) if fh and fh.get("remaining_percent") is not None else None
            wk_rem = float(wk["remaining_percent"]) if wk and wk.get("remaining_percent") is not None else None
            pool["five_hour_remaining_percent"] = fh_rem
            pool["weekly_remaining_percent"] = wk_rem
            if (fh_rem is not None and fh_rem <= 0.0) or (wk_rem is not None and wk_rem <= 0.0):
                pool["remaining_percent"] = 0.0
                pool["used_percent"] = 100.0
                pool["reset_time_kst"] = (wk if (wk_rem is not None and wk_rem <= 0.0) else fh or {}).get("reset_time_kst", "N/A")
            elif fh is not None:
                pool["remaining_percent"] = fh_rem
                pool["used_percent"] = round(100.0 - fh_rem, 2)
                pool["reset_time_kst"] = fh.get("reset_time_kst", "N/A")
            elif wk is not None:
                pool["remaining_percent"] = wk_rem
                pool["used_percent"] = round(100.0 - wk_rem, 2)
                pool["reset_time_kst"] = wk.get("reset_time_kst", "N/A")
            else:
                pool["remaining_percent"] = 0.0
                pool["used_percent"] = 100.0
                pool["reset_time_kst"] = "N/A"

        return {
            "user": us.get("name"),
            "email": us.get("email"),
            "plan": us.get("planStatus", {}).get("planInfo", {}).get("planName"),
            "pools": pools,
        }
    except Exception as e:
        return {"error": str(e)}


def get_codex_usage(ttl_sec: float = 30.0) -> dict:
    cache_path = "/tmp/agy_codex_usage_cache.json"
    if ttl_sec > 0 and os.path.exists(cache_path):
        try:
            if (time.time() - os.path.getmtime(cache_path)) <= ttl_sec:
                with open(cache_path, "r", encoding="utf-8") as cf:
                    cached = json.load(cf)
                if isinstance(cached, dict) and cached.get("quota_available"):
                    return cached
        except Exception:
            pass

    codex_auth_path = os.path.expanduser("~/.codex/auth.json")
    token = None
    if os.path.exists(codex_auth_path):
        try:
            with open(codex_auth_path) as f:
                c_auth = json.load(f)
            token = (
                c_auth.get("tokens", {}).get("access_token")
                or c_auth.get("access_token")
                or c_auth.get("auth_token")
            )
        except Exception:
            pass

    # 1. Try Wham backend API first
    if token:
        try:
            req = urllib.request.Request(
                "https://chatgpt.com/backend-api/wham/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=1.2) as resp:
                data = json.loads(resp.read().decode())
            rl = data.get("rate_limit", {})
            primary, secondary = _codex_windows(rl.get("primary_window"), rl.get("secondary_window"))
            plan = data.get("plan_type", "promax")

            if secondary:
                sec_used = float(secondary.get("used_percent", 0.0))
                sec_rst = secondary.get("reset_at") or secondary.get("resets_at")
                five_hour = {
                    "used_percent": round(sec_used, 2),
                    "remaining_percent": round(100.0 - sec_used, 2),
                    "reset_time_kst": fmt_ts_to_kst(sec_rst),
                    "status": "활성",
                }
            else:
                five_hour = None

            if primary:
                prim_used = float(primary.get("used_percent", 0.0))
                prim_rst = primary.get("reset_at") or primary.get("resets_at")
                weekly = {
                    "used_percent": round(prim_used, 2),
                    "remaining_percent": round(100.0 - prim_used, 2),
                    "reset_time_kst": fmt_ts_to_kst(prim_rst),
                    "window_seconds": primary.get("limit_window_seconds", 604800),
                    "status": "한도 도달" if prim_used >= 100.0 else "활성",
                }
            else:
                weekly = None

            res_dict = {
                "plan": plan,
                "five_hour": five_hour,
                "weekly": weekly,
                "used_percent": (weekly or five_hour or {}).get("used_percent"),
                "remaining_percent": (weekly or five_hour or {}).get("remaining_percent"),
                "reset_time_kst": (weekly or five_hour or {}).get("reset_time_kst", "N/A"),
                "source": "https://chatgpt.com/backend-api/wham/usage",
                "quota_available": bool(weekly or five_hour),
                "rate_limited": rl.get("allowed") is False or rl.get("limit_reached") is True,
                "stale": False,
                **_codex_model_metadata(),
            }
            try:
                with open(cache_path, "w", encoding="utf-8") as cf:
                    json.dump(res_dict, cf)
            except Exception:
                pass
            return res_dict
        except Exception:
            pass

    # 2. Fallback to ~/.codex/sessions rollouts
    rollouts = sorted(
        glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")),
        key=os.path.getmtime,
        reverse=True,
    )
    for r in rollouts[:10]:
        try:
            # Quota events are small; bound even a rollout containing giant tool-output lines.
            with open(r, "rb") as f:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - 1024 * 1024)
                f.seek(start)
                tail = f.read(1024 * 1024)
                if start:
                    tail = tail.partition(b"\n")[2]
                for line in reversed(tail.splitlines()):
                    if b'"rate_limits"' in line:
                        try:
                            d = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue  # A writer may still be appending the last event.
                        rl = d.get("payload", {}).get("rate_limits")
                        if rl and rl.get("primary"):
                            primary, secondary = _codex_windows(rl.get("primary"), rl.get("secondary"))
                            plan = rl.get("plan_type", "promax")

                            if secondary:
                                sec_used = float(secondary.get("used_percent", 0.0))
                                sec_rst = secondary.get("resets_at") or secondary.get("reset_at")
                                five_hour = {
                                    "used_percent": round(sec_used, 2),
                                    "remaining_percent": round(100.0 - sec_used, 2),
                                    "reset_time_kst": fmt_ts_to_kst(sec_rst),
                                    "status": "활성",
                                }
                            else:
                                five_hour = None

                            prim_used = float(primary.get("used_percent", 0.0)) if primary else 0.0
                            prim_rst = (primary.get("resets_at") or primary.get("reset_at")) if primary else None
                            weekly = {
                                "used_percent": round(prim_used, 2),
                                "remaining_percent": round(100.0 - prim_used, 2),
                                "reset_time_kst": fmt_ts_to_kst(prim_rst),
                                "window_minutes": primary.get("window_minutes", 10080),
                                "status": "한도 도달" if prim_used >= 100.0 else "활성",
                            } if primary else None
                            observed_at = d.get("timestamp")
                            try:
                                age = (datetime.datetime.now(timezone.utc) - datetime.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))).total_seconds()
                            except (AttributeError, TypeError, ValueError):
                                age = float("inf")
                            res_dict = {
                                "plan": plan,
                                "five_hour": five_hour,
                                "weekly": weekly,
                                "used_percent": weekly["used_percent"] if weekly else five_hour["used_percent"],
                                "remaining_percent": weekly["remaining_percent"] if weekly else five_hour["remaining_percent"],
                                "reset_time_kst": weekly["reset_time_kst"] if weekly else five_hour["reset_time_kst"],
                                "source": f"session_log:{os.path.basename(r)}",
                                "quota_available": bool(weekly or five_hour),
                                "observed_at": observed_at,
                                "stale": age < 0 or age > ROUTING_MAX_AGE_SECONDS,
                                **_codex_model_metadata(),
                            }
                            try:
                                with open(cache_path, "w", encoding="utf-8") as cf:
                                    json.dump(res_dict, cf)
                            except Exception:
                                pass
                            return res_dict
        except Exception:
            continue
    return {"error": "No recent Codex rollout with rate_limits found"}


def _codex_windows(primary, secondary):
    """Return (weekly, five_hour); ProMax may expose weekly as primary."""
    weekly, five_hour = None, None
    for window, fallback_seconds in ((primary, 604800), (secondary, 18000)):
        if not window:
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or used is None or not math.isfinite(float(used)) or not 0 <= float(used) <= 100:
            raise ValueError("Invalid or missing Codex used_percent")
        seconds = window.get("limit_window_seconds")
        if seconds is None:
            seconds = window.get("window_minutes", fallback_seconds / 60) * 60
        if seconds == 604800:
            weekly = window
        elif seconds == 18000:
            five_hour = window
        else:
            raise ValueError(f"Unsupported Codex quota window: {seconds}")
    return weekly, five_hour


def _codex_model_metadata() -> dict:
    """Use the local catalog for tier eligibility, never infer it from quota."""
    try:
        with open(os.path.expanduser("~/.codex/models_cache.json"), encoding="utf-8") as f:
            catalog = json.load(f)
        model = next((m for m in catalog.get("models", []) if m.get("slug") == "gpt-6.1-sol"), None)
        tiers = [t["id"] for t in model.get("service_tiers", []) if t.get("id") in ("priority", "ultrafast")] if model else []
        return {"model_available": model is not None, "service_tiers": tiers,
                "catalog_fetched_at": catalog.get("fetched_at")}
    except (OSError, ValueError, TypeError, KeyError):
        return {"service_tiers": ["priority"], "catalog_fetched_at": None}


def _is_iso_past(iso_str) -> bool:
    """Whether an ISO reset timestamp is at or before the current UTC time."""
    if not isinstance(iso_str, str) or not iso_str:
        return False
    try:
        reset_at = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        return reset_at <= datetime.datetime.now(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return False


def get_claude_status() -> dict:
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    has_oauth = False
    sub_type = "Max / Pro (OAuth)"
    token = None
    if os.path.exists(cred_path):
        try:
            with open(cred_path, encoding="utf-8") as credentials:
                d = json.load(credentials)
            oauth = d.get("claudeAiOauth", {})
            token = oauth.get("accessToken")
            has_oauth = bool(token)
            sub_type = oauth.get("subscriptionType") or oauth.get("rateLimitTier") or sub_type
        except Exception:
            pass

    hist_path = os.path.expanduser("~/.claude/history.jsonl")
    last_active = "N/A"
    if os.path.exists(hist_path):
        last_active = fmt_ts_to_kst(os.path.getmtime(hist_path), is_reset=False)

    five_hour_info = {
        "used_percent": 0.0,
        "remaining_percent": 100.0,
        "reset_time_kst": "대기 (미사용)",
        "status": "정보 없음",
    }
    weekly_info = {
        "used_percent": 0.0,
        "remaining_percent": 100.0,
        "reset_time_kst": "N/A",
        "status": "정보 없음",
        "weekly_scoped": None,
    }

    cache_path = os.path.expanduser("~/.gemini/antigravity/claude_usage_cache.json")
    data = None
    source = None
    stale = False
    reset_reconciled = False
    if os.path.exists(cache_path) and (datetime.datetime.now().timestamp() - os.path.getmtime(cache_path) < CLAUDE_CACHE_MAX_AGE_SECONDS):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                data = json.load(cf)
            source = "cache"
        except Exception:
            data = None
    if token and data is None:
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "Claude-Code/2.1.296",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
            source = "https://api.anthropic.com/api/oauth/usage"
            if isinstance(data, dict) and ("seven_day" in data or "five_hour" in data):
                try:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as cf:
                        json.dump(data, cf)
                except Exception:
                    pass
        except Exception as e:
            err_str = str(e)
            is_429 = "429" in err_str or "Too Many Requests" in err_str
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as cf:
                        data = json.load(cf)
                    source = "cache"
                    stale = datetime.datetime.now().timestamp() - os.path.getmtime(cache_path) >= CLAUDE_CACHE_MAX_AGE_SECONDS
                except Exception:
                    data = None
            if not data:
                five_hour_info["error"] = err_str
                weekly_info["error"] = err_str
            else:
                data["api_error"] = err_str
                data["is_429"] = is_429

    if isinstance(data, dict):
        fh = data.get("five_hour") or {}
        if fh and fh.get("utilization") is not None:
            fh_util = float(fh.get("utilization", 0.0))
            fh_rst = fh.get("resets_at")
            fh_reset = _is_iso_past(fh_rst)
            if fh_reset:
                fh_util = 0.0
            five_hour_info = {
                "used_percent": round(fh_util, 2),
                "remaining_percent": round(100.0 - fh_util, 2),
                "reset_time_kst": fmt_iso_to_kst(fh_rst) if fh_rst else "대기 (미사용)",
                "status": "가용 (리셋 완료)" if fh_reset else ("가용 (대기)" if not fh_rst else ("한도 도달" if fh_util >= 100.0 else "활성")),
            }

        sd = data.get("seven_day") or {}
        fable_allowance = None
        for lim in data.get("limits", []):
            if lim.get("kind") == "weekly_scoped":
                model_name = lim.get("scope", {}).get("model", {}).get("display_name", "Fable")
                fable_used = float(lim.get("percent", 0))
                fable_allowance = {
                    "model": model_name,
                    "used_percent": round(fable_used, 2),
                    "remaining_percent": round(100.0 - fable_used, 2),
                    "reset_time_kst": fmt_iso_to_kst(lim.get("resets_at")),
                    "is_active": lim.get("is_active", False),
                }
                for share in data.get("weekly_scoped_shares", []):
                    if "allowance_percent_of_weekly" in share:
                        fable_allowance["allowance_percent_of_weekly"] = share.get("allowance_percent_of_weekly")
                        fable_allowance["used_percent_of_weekly"] = share.get("used_percent_of_weekly")
                        break

        if sd and sd.get("utilization") is not None:
            sd_util = float(sd.get("utilization", 0.0))
            sd_rst = sd.get("resets_at")
            sd_reset = _is_iso_past(sd_rst)
            if sd_reset:
                sd_util = 0.0
            weekly_info = {
                "used_percent": round(sd_util, 2),
                "remaining_percent": round(100.0 - sd_util, 2),
                "reset_time_kst": fmt_iso_to_kst(sd_rst),
                "status": "활성 (리셋 완료)" if sd_reset else ("한도 도달" if sd_util >= 100.0 else "활성"),
                "weekly_scoped": fable_allowance,
            }
            if sd_util >= 100.0:
                five_hour_info = {
                    "used_percent": 100.0,
                    "remaining_percent": 0.0,
                    "reset_time_kst": fmt_iso_to_kst(sd_rst),
                    "status": "한도 도달 (주간 소진)",
                }
        # Allow reset inference after API rate limit (429) or cache expiry:
        # If 5h window passed resets_at, 5h is refreshed to 100%. Reconcile if 5h reset passed
        # and weekly quota is not exhausted, or if cache is not stale.
        fh_rst = fh.get("resets_at")
        fh_reset_past = _is_iso_past(fh_rst)
        if fh_reset_past or (fh.get("utilization") == 0 and not fh_rst):
            reset_reconciled = (sd_util < 100.0 or _is_iso_past(sd.get("resets_at")))
        elif not stale:
            reset_reconciled = True
        else:
            reset_reconciled = False

    return {
        "authenticated": has_oauth,
        "subscription": sub_type,
        "quota_available": isinstance(data, dict) and any(
            isinstance(data.get(key), dict) and data[key].get("utilization") is not None
            for key in ("five_hour", "seven_day")
        ),
        "source": source,
        "stale": stale,
        "reset_reconciled": reset_reconciled,
        "five_hour": five_hour_info,
        "weekly": weekly_info,
        "last_activity": last_active,
        "connected_mcps": [
            "Context7 (✔ Connected)",
            "Mobbin (✔ Connected via claude.ai)",
            "Mermaid Chart (✔ Connected)",
            "Google Drive / Gmail / Calendar (✔ Connected)",
        ],
    }


get_claude_code_usage = get_claude_status


def _load_agy_spawn_registry() -> dict:
    # Dead worker pids are reaped to "completed" so they never render as running.
    from spawn_registry_reaper import load_registry
    return load_registry("~/.gemini/antigravity/agy_spawned_sessions.json")


def _has_agy_prefix(value) -> bool:
    # Accept single Google Gemini 4-point sparkle ✦ or legacy [AGY] prefix.
    s = str(value or "").strip()
    return s.startswith("✦") or bool(re.match(r"^[^\w\[]*\[AGY\](?:\s|$)", s))


def _valid_agy_tree_cache(cached, now) -> bool:
    if not isinstance(cached, dict):
        return False
    cached_at = cached.get("cached_at")
    return (
        isinstance(cached_at, (int, float)) and not isinstance(cached_at, bool)
        and 0 <= now - cached_at < 15
        and isinstance(cached.get("tree"), list)
        and all(isinstance(node, dict) for node in cached["tree"])
    )


def get_antigravity_agent_tree() -> list[dict]:
    global _agy_tree_cache
    now = time.time()
    if _valid_agy_tree_cache(_agy_tree_cache, now):
        return _agy_tree_cache["tree"]
    try:
        with open(AGY_TREE_CACHE_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if _valid_agy_tree_cache(cached, now):
            _agy_tree_cache = cached
            return cached["tree"]
    except (OSError, ValueError):
        pass

    tree = _fetch_antigravity_agent_tree()
    _agy_tree_cache = {"cached_at": time.time(), "tree": tree}
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(AGY_TREE_CACHE_PATH),
            prefix="agy_tree_", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(_agy_tree_cache, f)
        os.replace(temp_path, AGY_TREE_CACHE_PATH)
    except OSError:
        pass
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return tree


TURN_TIMING_KEYS = ("turn_start_ts", "turn_end_ts", "turn_duration_sec")


def _timing_timestamp(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            ts = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return ts if math.isfinite(ts) and ts > 0 else None


def _jsonl_tail(path: str, limit: int = 65536) -> list[str]:
    """Read bounded bytes; a partial first/last JSON record is ignored by callers."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read(limit).decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return []


def _extract_turn_timing(lines: list[str], *, antigravity: bool = False,
                         fallback_start=None, fallback_end=None) -> dict:
    first_ts = last_ts = last_step_ts = explicit_start = gap_start = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        ts = _timing_timestamp(event.get("created_at" if antigravity else "timestamp"))
        last_step_ts = ts
        if ts is None:
            continue
        if first_ts is None:
            first_ts = ts
        if last_ts is not None and ts - last_ts > 600:
            gap_start = ts
        payload = event.get("payload") or {}
        kind = event.get("type") if antigravity else (
            payload.get("type") if isinstance(payload, dict) else None)
        if kind in (("USER_INPUT", "SYSTEM_MESSAGE") if antigravity else ("user_message", "task_started")):
            explicit_start = ts
        last_ts = ts
    end = (last_step_ts or _timing_timestamp(fallback_end) or last_ts) if antigravity else last_ts
    if antigravity:
        start = explicit_start or gap_start or first_ts
    else:
        start = max(explicit_start or 0, gap_start or 0) or first_ts
    if start is None:
        start = _timing_timestamp(fallback_start)
        if end is not None and (start is None or end - start > 7200):
            start = end
    if end is None:
        end = _timing_timestamp(fallback_end) or start
    if start is None:
        start = end
    dur = max(1, int(round(end - start))) if (start is not None and end is not None) else None
    return {"turn_start_ts": start, "turn_end_ts": end, "turn_duration_sec": dur}


def _extract_agy_turn_timing(cid, created_time_iso, last_mod_iso) -> dict:
    path = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
    lines = _jsonl_tail(path, limit=250000)
    return _extract_turn_timing(lines, antigravity=True,
                                fallback_start=created_time_iso, fallback_end=last_mod_iso)


def extract_active_bg_tasks(cid: str, now_sec: float | None = None) -> list[dict]:
    """Extract and classify active background tasks into daemon, timer, cron, or job.

    Filters out any background tasks / crons created before a server restart
    ('All your subagents and background tasks have been stopped due to server restart').
    """
    if not cid:
        return []
    if now_sec is None:
        now_sec = time.time()
    tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
    if not os.path.exists(tpath):
        return []
    from spawn_evidence import cached
    try:
        tasks = cached(tpath, "background-tasks", _parse_background_tasks)
    except OSError:
        return []

    out = []
    for tv in tasks.values():
        if tv["done"]:
            continue
        desc = tv["desc"]
        age_s = max(0, int(now_sec - (_timing_timestamp(tv.get("created_at") or 0) or now_sec))) if tv.get("created_at") else 0
        if tv["kind"] == "timer":
            dur_m = re.search(r"Timer:\s*(\d+)s", desc)
            dur_s = int(dur_m.group(1)) if dur_m else 90
            if age_s > dur_s + 15:
                continue
        elif tv["kind"] == "job":
            if age_s > 900:
                continue
        elif tv["kind"] == "daemon":
            if age_s > 43200:
                continue
        out.append(tv)
    return out


WORKER_SPAWN_MARKERS = ("maestro worker-start", "orca worker-start", "agy-orca worker-start", "maestro.py worker-start")
# The tool result carries the whole multi-line command; only its first line is the display description.
BG_TASK_DESC_RE = re.compile(
    r"background task with task id:\s*(\S+)\s*\nTask Description:\s*(.*?)(?:\nTask logs are available at:|\nYOU MUST TAKE|\Z)",
    re.S)
_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_WORKER_RESULT_RE = re.compile(rb'\A\s*\{\s*"pool"\s*:')


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies so a spec that merely mentions worker-start is not taken as the command."""
    out, delim = [], None
    for line in command.replace("\\n", "\n").splitlines():
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        out.append(line)
        match = _HEREDOC_START_RE.search(line)
        if match:
            delim = match.group(1)
    return "\n".join(out)


def is_worker_spawn_command(command: str) -> bool:
    """True for a run_command that launches a CLI worker (rendered as its own SUB row)."""
    text = _strip_heredocs(command or "")
    return any(marker in text for marker in WORKER_SPAWN_MARKERS)


def task_log_is_worker_result(transcript_path: str, short_id: str) -> bool:
    """A finished `maestro worker-start --wait` leaves its JSON result (`{"pool": ...`) as the task log."""
    if not transcript_path or not short_id:
        return False
    log = os.path.join(os.path.dirname(os.path.dirname(transcript_path)), "tasks", f"{short_id}.log")
    try:
        with open(log, "rb") as f:
            return bool(_WORKER_RESULT_RE.match(f.read(256)))
    except OSError:
        return False


def _parse_background_tasks(tpath):
    tasks: dict[str, dict] = {}
    step_daemon_hints: dict[int, bool] = {}
    step_cron_hints: dict[int, str] = {}
    step_worker_spawn_hints: dict[int, bool] = {}
    try:
        with open(tpath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if (
                    "task-" not in line
                    and "IsDaemon" not in line
                    and "CronExpression" not in line
                    and "server restart" not in line
                    and "stopped due to" not in line
                ):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sidx = int(d.get("step_index") or 0)
                for tc in (d.get("tool_calls") or []):
                    tname = tc.get("name")
                    raw_args = tc.get("arguments") or tc.get("args") or {}
                    targs = str(raw_args)
                    if tname == "run_command":
                        if ('"IsDaemon": "true"' in targs or '"IsDaemon": true' in targs or "'IsDaemon': True" in targs):
                            step_daemon_hints[sidx] = True
                            step_daemon_hints[sidx + 1] = True
                            step_daemon_hints[sidx + 2] = True
                        cmd_line = ""
                        if isinstance(raw_args, dict):
                            cmd_line = str(raw_args.get("CommandLine") or "")
                        else:
                            cm_cmd = re.search(r'"CommandLine"\s*:\s*"([^"]+)"', targs)
                            if cm_cmd:
                                cmd_line = cm_cmd.group(1)
                        if is_worker_spawn_command(cmd_line):
                            step_worker_spawn_hints[sidx] = True
                            step_worker_spawn_hints[sidx + 1] = True
                            step_worker_spawn_hints[sidx + 2] = True
                    elif tname == "schedule" and "CronExpression" in targs:
                        cexpr = ""
                        if isinstance(raw_args, dict):
                            cexpr = str(raw_args.get("CronExpression") or "")
                        else:
                            cm = re.search(r'"CronExpression"\s*:\s*"([^"]+)"', targs)
                            if cm:
                                cexpr = cm.group(1)
                        cexpr = cexpr.strip().strip('"\'\\').rstrip(",").strip().strip('"\'\\')
                        step_cron_hints[sidx] = cexpr or "cron"
                        step_cron_hints[sidx + 1] = cexpr or "cron"

                c = str(d.get("content") or "")
                created_iso = d.get("created_at") or ""

                # Restart notices can be appended after newer task records.
                # Compare event times rather than flushing every known task.
                runtime_restart = (
                    "stopped due to server restart" in c.lower()
                    and d.get("type") == "SYSTEM_MESSAGE"
                    and d.get("source", "SYSTEM") == "SYSTEM"
                )
                if runtime_restart:
                    event_time = _timing_timestamp(created_iso)
                    mentioned = set(re.findall(r"[\w./-]*task-\d+", c))
                    for tid, tv in tasks.items():
                        started = _timing_timestamp(tv.get("created_at"))
                        explicitly_named = tid in mentioned or tv["short_id"] in mentioned
                        before_restart = not mentioned and (
                            event_time is None or (started is not None and started <= event_time)
                        )
                        after_restart = event_time is not None and started is not None and started > event_time
                        if (explicitly_named and not after_restart) or before_restart:
                            tv["done"] = True

                m = BG_TASK_DESC_RE.search(c)
                if m:
                    tid, command = m.group(1).strip(), m.group(2).strip()
                    desc = (command.splitlines() or [""])[0].strip()
                    short_id = tid.split("/")[-1]
                    num_m = re.search(r"task-(\d+)", short_id)
                    tnum = int(num_m.group(1)) if num_m else sidx
                    # A worker-start wrapper is already its worker's SUB row; listing it again as a JOB
                    # would show the same process tree twice.
                    if (
                        step_worker_spawn_hints.get(tnum, False)
                        or step_worker_spawn_hints.get(sidx, False)
                        or is_worker_spawn_command(command)
                        or task_log_is_worker_result(tpath, short_id)
                    ):
                        continue
                    is_timer = desc.startswith("Timer:")
                    cron_hint = step_cron_hints.get(tnum) or step_cron_hints.get(sidx) or ""
                    if not cron_hint and desc.startswith("Cron:"):
                        cm2 = re.match(r"^Cron:\s*([^,]+)(?:,|$)", desc)
                        cron_hint = cm2.group(1).strip() if cm2 else "cron"
                    cron_hint = cron_hint.strip().strip('"\'\\').rstrip(",").strip().strip('"\'\\')
                    is_cron = bool(cron_hint) or desc.startswith("Cron:")
                    is_daemon = (
                        step_daemon_hints.get(tnum, False)
                        or step_daemon_hints.get(sidx, False)
                        or any(k in desc for k in ("http.server", "--port", "nexus", "synapse_signaling", "environment_demo", "./run.sh"))
                    )
                    kind = "timer" if is_timer else ("cron" if is_cron else ("daemon" if is_daemon else "job"))
                    tasks[tid] = {
                        "id": tid,
                        "short_id": short_id,
                        "desc": desc,
                        "kind": kind,
                        "cron_expr": "" if cron_hint == "cron" else cron_hint,
                        "created_at": created_iso,
                        "done": False,
                    }
                for m2 in re.finditer(r'Task "([^"]+/task-\d+)" (?:cancelled|completed|finished)', c):
                    if m2.group(1) in tasks:
                        tasks[m2.group(1)]["done"] = True
                if "cancelled early" in c.lower():
                    for tv in tasks.values():
                        if tv["kind"] == "timer":
                            tv["done"] = True
                if "<SYSTEM_MESSAGE>" in c and "sender=" in c:
                    ms = re.search(r"sender=([^\s\]]+/task-\d+)", c)
                    if ms and ms.group(1) in tasks:
                        tv = tasks[ms.group(1)]
                        if tv["kind"] == "timer" or "exited with code" in c or "terminated" in c or "completed" in c or "finished" in c:
                            tv["done"] = True
    except Exception:
        return {}

    return tasks


def _agy_transcript_parent_ids(cids) -> dict[str, str]:
    from agy_transcript_state import transcript_state
    return transcript_state(cids)[0]


def _fetch_antigravity_agent_tree() -> list[dict]:
    addr, token = discover_ls_credentials()
    if not addr:
        return []

    meta_cache_path = AGY_METADATA_CACHE_PATH
    meta_cache = {}
    meta_dirty = False
    try:
        if os.path.exists(meta_cache_path):
            with open(meta_cache_path, "r", encoding="utf-8") as mf:
                meta_cache = json.load(mf)
                if not isinstance(meta_cache, dict):
                    meta_cache = {}
    except Exception:
        meta_cache = {}

    def fetch_generator_metadata(cid: str, last_mod: str, status_str: str) -> dict:
        nonlocal meta_dirty
        cached = meta_cache.get(cid)
        if isinstance(cached, dict) and isinstance(cached.get("metadata"), dict):
            cached_at = cached.get("cached_at")
            fresh = (
                isinstance(cached_at, (int, float)) and not isinstance(cached_at, bool)
                and 0 <= time.time() - cached_at < 45
            )
            if cached.get("last_mod") == last_mod or fresh:
                return cached["metadata"]
        req = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectoryGeneratorMetadata",
            data=json.dumps({"cascadeId": cid}).encode(),
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
            gm = data.get("generatorMetadata", [])
            res = {}
            if gm:
                last = gm[-1]
                cm = last.get("chatModel", {})
                retry_infos = cm.get("retryInfos", [])
                usage = retry_infos[-1].get("usage", {}) if retry_infos else cm.get("usage", {})
                inp = int(usage.get("inputTokens", 0) or 0)
                cache = int(usage.get("cacheReadTokens", 0) or 0)
                out_tok = int(usage.get("outputTokens", 0) or 0)
                used = inp + cache
                model_id = usage.get("model") or cm.get("model") or last.get("planModel")
                res = {
                    "model_id": model_id,
                    "model_name": ANTIGRAVITY_MODEL_MAP.get(model_id, model_id or "Unknown"),
                    "context_usage": {
                        "used_tokens": used,
                        "input_tokens": inp,
                        "cache_read_tokens": cache,
                        "output_tokens": out_tok,
                        "max_tokens": 1000000,
                        "percent": round((used / 1000000) * 100, 2),
                    },
                }
            meta_cache[cid] = {"last_mod": last_mod, "cached_at": time.time(), "metadata": res}
            meta_dirty = True
            return res
        except Exception:
            pass
        return {}

    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": token or ""},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        trajs = data.get("trajectorySummaries", {})
    except Exception:
        return []

    selected_root_model = get_last_selected_agy_model()
    spawn_reg = _load_agy_spawn_registry()
    from agy_transcript_state import transcript_state
    transcript_parents, transcript_killed = transcript_state(trajs)
    from live_worker_processes import scan_live_worker_processes
    active_cli_parents = {process.get("parent_conversation_id")
                          for process in scan_live_worker_processes()}

    nodes = {}
    for cid, summary in trajs.items():
        meta = summary.get("trajectoryMetadata", {})
        spec = meta.get("subagentSpec", {})
        parent_id = transcript_parents.get(cid) or meta.get("parentConversationId")
        depth = meta.get("nestingDepth", 0)
        reg_info = spawn_reg.get(cid, {})
        if not isinstance(reg_info, dict):
            reg_info = {}
        role = spec.get("role") or ("Orchestrator" if not parent_id else "Subagent")
        raw_title = summary.get("summary") or summary.get("annotations", {}).get("title") or reg_info.get("title") or role
        title = reg_info.get("title") if (reg_info.get("title") and not _has_agy_prefix(raw_title)) else raw_title
        status = summary.get("status", "CASCADE_RUN_STATUS_UNKNOWN")
        created_time = summary.get("createdTime", "")
        last_mod = summary.get("lastModifiedTime", "")
        t_path = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
        t_age_sec = None
        last_step = None
        if os.path.exists(t_path):
            try:
                t_mtime = os.path.getmtime(t_path)
                t_age_sec = max(0.0, time.time() - t_mtime)
                t_iso = datetime.datetime.fromtimestamp(t_mtime, timezone.utc).isoformat()
                if not last_mod or t_iso > last_mod:
                    last_mod = t_iso
                try:
                    from spawn_evidence import last_step_state
                    last_step = last_step_state(t_path)
                except Exception:
                    pass
            except OSError:
                pass
        workspaces = [w.get("workspaceFolderAbsoluteUri", "") for w in summary.get("workspaces", [])]

        is_subagent = bool(parent_id or depth > 0 or spec)
        is_running_agy = (str(status).upper() in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING"))

        if is_subagent and is_running_agy:
            if last_step and isinstance(last_step, dict):
                step_type = str(last_step.get("type", "")).upper()
                step_status = str(last_step.get("status", "")).upper()
                tool_calls = last_step.get("tool_calls")
                if step_type == "PLANNER_RESPONSE" and step_status == "DONE" and not tool_calls and t_age_sec is not None and t_age_sec > 15:
                    is_running_agy = False
                    status = "CASCADE_RUN_STATUS_DONE"
            if is_running_agy and t_age_sec is not None and t_age_sec > 300:
                is_running_agy = False
                status = "CASCADE_RUN_STATUS_DONE"

        stale_threshold = 1800 if is_running_agy else 180
        if is_subagent and t_age_sec is not None and t_age_sec > stale_threshold:
            is_running_agy = False
            if str(status).upper() in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING"):
                status = "CASCADE_RUN_STATUS_DONE"

        background_tasks = extract_active_bg_tasks(cid, time.time())
        has_active_bg = any(task.get("kind") != "cron" for task in background_tasks) or cid in active_cli_parents
        if has_active_bg:
            is_running_agy = True
            status = "CASCADE_RUN_STATUS_RUNNING"
        killed = transcript_killed.get(cid, False)
        if killed:
            is_running_agy = False
            status = "TERMINATED"
        gm_info = fetch_generator_metadata(cid, last_mod, status)
        model_id = gm_info.get("model_id") or reg_info.get("plan_model") or spec.get("model")
        if not model_id and depth == 0 and selected_root_model and selected_root_model in ANTIGRAVITY_MODEL_MAP:
            model_id = selected_root_model
            model = ANTIGRAVITY_MODEL_MAP[selected_root_model]
        else:
            model = gm_info.get("model_name") or ANTIGRAVITY_MODEL_MAP.get(model_id, model_id or "Unknown")

        nodes[cid] = {
            "conversation_id": cid,
            "title": title,
            "role": role,
            "model": model,
            "model_id": model_id,
            "status": status,
            "is_running": is_running_agy,
            "killed": killed,
            "is_subagent": is_subagent,
            "created_time": created_time,
            "last_modified_time": last_mod,
            **_extract_agy_turn_timing(cid, created_time, last_mod),
            "background_tasks": background_tasks,
            "has_active_bg": has_active_bg and not killed,
            "workspaces": workspaces,
            "agy_origin": True,
            "nesting_depth": depth,
            "parent_conversation_id": parent_id,
            "parent_id": parent_id,
            "context_usage": gm_info.get("context_usage", {
                "used_tokens": 0, "max_tokens": 1000000, "percent": 0.0
            }),
            "children": [],
        }

    roots = []
    for cid, node in nodes.items():
        pid = node.get("parent_conversation_id")
        if pid and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)

    if meta_dirty:
        try:
            with open(meta_cache_path, "w", encoding="utf-8") as mf:
                json.dump(meta_cache, mf)
        except Exception:
            pass

    roots.sort(
        key=lambda x: (
            0 if "RUNNING" in (x.get("status") or "") else 1,
            x.get("last_modified_time") or "",
        ),
        reverse=False,
    )
    # Sort RUNNING roots by most recent first, and IDLE roots by most recent first
    running_roots = sorted([r for r in roots if "RUNNING" in (r.get("status") or "")], key=lambda x: x.get("last_modified_time") or "", reverse=True)
    idle_roots = sorted([r for r in roots if "RUNNING" not in (r.get("status") or "")], key=lambda x: x.get("last_modified_time") or "", reverse=True)
    return running_roots + idle_roots


_CODEX_TIER_CACHE: dict[tuple, str] = {}


def _default_codex_service_tier() -> str:
    cfg = os.path.expanduser("~/.codex/config.toml")
    try:
        st = os.stat(cfg)
    except OSError:
        return "default"
    key = (cfg, st.st_mtime_ns, st.st_size)
    if key in _CODEX_TIER_CACHE:
        return _CODEX_TIER_CACHE[key]
    tier = "default"
    try:
        with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.match(r'^\s*service_tier\s*=\s*"([^"]+)"', line)
                if m:
                    val = m.group(1).strip()
                    if val in ("default", "priority", "ultrafast"):
                        tier = val
                        break
    except Exception:
        pass
    _CODEX_TIER_CACHE.clear()
    _CODEX_TIER_CACHE[key] = tier
    return tier



def _codex_settings_from_line(line: str, fallback_model: str, fallback_effort: str) -> tuple[dict | None, str | None]:
    """(turn_context, service_tier) from one rollout line; either may be None."""
    d = json.loads(line)
    pl = d.get("payload") or {}
    if d.get("type") == "turn_context":
        src = pl
    elif isinstance(pl.get("thread_settings"), dict):
        src = pl["thread_settings"]
    else:
        return None, None
    ctx = {
        "model": src.get("model") or fallback_model,
        "reasoning_effort": src.get("effort") or src.get("reasoning_effort") or fallback_effort,
        "service_tier": src.get("service_tier"),
    }
    return ctx, src.get("service_tier") or None


# (path, mtime_ns, size, fallbacks, default tier) -> parsed rollout; rollouts are re-read only when they change.
_CODEX_ROLLOUT_CACHE: dict[tuple, dict] = {}


def parse_codex_rollout(path: str, fallback_model: str = "gpt-6.1-sol", fallback_effort: str = "ultra") -> dict:
    default_tier = _default_codex_service_tier()
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    if st is None:
        return _parse_codex_rollout(path, fallback_model, fallback_effort, default_tier)
    key = (path, st.st_mtime_ns, st.st_size, fallback_model, fallback_effort, default_tier)
    if key not in _CODEX_ROLLOUT_CACHE:
        if len(_CODEX_ROLLOUT_CACHE) > 256:
            _CODEX_ROLLOUT_CACHE.clear()
        _CODEX_ROLLOUT_CACHE[key] = _parse_codex_rollout(path, fallback_model, fallback_effort, default_tier)
    return copy.deepcopy(_CODEX_ROLLOUT_CACHE[key])


def _parse_codex_rollout(path: str, fallback_model: str, fallback_effort: str, default_tier: str) -> dict:
    token_count = None
    turn_context = None
    service_tier = None  # turn_context omits it; thread_settings_applied carries the real tier
    turn_completed = False
    seen_lifecycle = False
    timing = dict.fromkeys(TURN_TIMING_KEYS)
    if not path or not os.path.exists(path):
        return {
            "token_count": {"total_tokens": 0, "model_context_window": 475000, "percent": 0.0},
            "turn_context": {"model": fallback_model, "reasoning_effort": fallback_effort, "service_tier": default_tier},
            "turn_completed": False,
            **dict.fromkeys(TURN_TIMING_KEYS),
        }
    try:
        size = os.path.getsize(path)
        chunk_size = min(size, 250000)
        with open(path, "rb") as f:
            if size > chunk_size:
                f.seek(size - chunk_size)
            chunk = f.read().decode("utf-8", errors="ignore")
        lines = chunk.splitlines()
        timing = _extract_turn_timing(lines)
        for line in reversed(lines):
            if not seen_lifecycle and '"event_msg"' in line:
                try:
                    d = json.loads(line)
                    if d.get("type") == "event_msg":
                        pt = (d.get("payload") or {}).get("type")
                        if pt in ("task_complete", "turn_aborted"):
                            turn_completed = True
                            seen_lifecycle = True
                        elif pt in ("task_started", "exec_command_begin", "exec_command_end", "agent_message", "user_message"):
                            turn_completed = False
                            seen_lifecycle = True
                except Exception:
                    pass
            if not token_count and '"last_token_usage"' in line:
                try:
                    d = json.loads(line)
                    info = d.get("payload", {}).get("info", {})
                    if "last_token_usage" in info:
                        ltu = info["last_token_usage"]
                        cw = info.get("model_context_window", 475000)
                        tot = ltu.get("total_tokens", 0)
                        token_count = {
                            "total_tokens": tot,
                            "model_context_window": cw,
                            "percent": round((tot / cw) * 100, 2) if cw else 0.0,
                        }
                except Exception:
                    pass
            if (not turn_context or not service_tier) and ('"turn_context"' in line or '"thread_settings"' in line):
                try:
                    found, tier = _codex_settings_from_line(line, fallback_model, fallback_effort)
                    if found and not turn_context:
                        turn_context = found
                    service_tier = service_tier or tier
                except Exception:
                    pass
            if token_count and turn_context and service_tier and seen_lifecycle:
                break
        if (not turn_context or not service_tier) and size > chunk_size:
            # Long rollouts push thread_settings (with service_tier) out of the tail window.
            with open(path, "rb") as f:
                head_lines = f.read(262144).decode("utf-8", errors="ignore").splitlines()
            for line in head_lines:
                if '"turn_context"' not in line and '"thread_settings"' not in line:
                    continue
                try:
                    found, tier = _codex_settings_from_line(line, fallback_model, fallback_effort)
                except Exception:
                    continue
                turn_context = turn_context or found
                service_tier = service_tier or tier
                if turn_context and service_tier:
                    break
    except Exception:
        pass
    if turn_context:
        turn_context["service_tier"] = service_tier or default_tier

    if not token_count:
        token_count = {"total_tokens": 0, "model_context_window": 475000, "percent": 0.0}
    if not turn_context:
        turn_context = {"model": fallback_model, "reasoning_effort": fallback_effort, "service_tier": default_tier}
    return {"token_count": token_count, "turn_context": turn_context, "turn_completed": turn_completed,
            **timing}


def _clean_codex_label(nick: str | None, role: str | None, agent_path: str | None, title: str | None, first_msg: str | None) -> str:
    base = nick or role or "Subagent"
    if agent_path:
        leaf = agent_path.rstrip("/").split("/")[-1]
        if leaf and leaf != "root":
            return f"{base} · {leaf}"
    if title and len(title.strip()) <= 42:
        return f"{base} · {title.strip()}"
    if first_msg:
        msg = first_msg.strip().replace("\n", " ")
        if "gateway.py" in msg:
            return f"{base} · gateway.py & tests"
        if "SYNAPSE Desk" in msg:
            return f"{base} · SYNAPSE Desk & GPL"
        if "English visible" in msg or "brand" in msg:
            return f"{base} · brand strings & UI"
        if "separate terminal window" in msg or "terminal.mjs" in msg:
            return f"{base} · terminal.mjs window"
    return base


def _clean_parent_title(name: str | None, title: str | None, cwd: str | None) -> str:
    if name and name.strip():
        return name.strip()
    if title:
        t = title.strip().splitlines()[0]
        if "케티시냅스" in t and "러스트데스크" in t:
            return "SYNAPSE Desk · 케티시냅스 원격 제어"
        return t[:42] + ("…" if len(t) > 42 else "")
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return "Codex Session"


def _running_codex_cwds_and_snippets() -> tuple[set[str], list[str]]:
    cwds: set[str] = set()
    snippets: list[str] = []
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, stderr=subprocess.DEVNULL, timeout=2)
        for line in out.splitlines():
            if "codex" not in line or "grep" in line:
                continue
            if "codex exec" in line or "codex resume" in line:
                snippets.append(line)
                m = re.search(r"-C\s+(\S+)", line)
                if m:
                    cwds.add(os.path.abspath(m.group(1)))
    except Exception:
        pass
    return cwds, snippets



# Transcripts untouched for this long cannot produce a row (24h window) unless a
# live PID revives them, and those are rebuilt from ~/.claude/sessions instead.
CLAUDE_JSONL_LOOKBACK_SEC = 7 * 86400
# path -> (mtime_ns, size, {"offset", "lines", "nodes"}). Transcripts are append-only,
# so a grown file only parses the bytes after "offset"; unchanged files are never read.
_CLAUDE_JSONL_CACHE: dict[str, tuple[int, int, Any]] = {}


def get_claude_agent_tree() -> list[dict]:
    """Return up to 30 sessions updated in 24h or with a verified live PID.

    Times are Unix seconds; tokens include input, output and cache tokens.
    Streaming usage is merged by message ID before summing. Local reads only.
    """
    now = time.time()
    nodes = {}
    counters = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")

    def timestamp(value):
        ts = _timing_timestamp(value)
        return ts / 1000 if ts and ts > 100_000_000_000 else ts

    def string(value):
        return value.strip() if isinstance(value, str) else ""

    def node_for(sid, into=None):
        sid = string(sid)
        if not sid:
            return None
        return (nodes if into is None else into).setdefault(sid, {"id": sid, "session_id": sid,
            "thread_id": sid, "provider": "claude", "title": "", "model": "",
            "effort": "", "cwd": "", "created_at": None, "updated_at": None,
            "rollout_path": "", "jsonl_path": "", "spec_preview": "",
            "agy_origin": False, "children": [], "_pids": set(), "_usage": {},
            "_fallback_time": 0})

    def merge(node, data):
        for key, aliases in {
            "title": ("title", "customTitle", "summary"),
            "model": ("model",), "effort": ("effort", "reasoning_effort", "effortLevel"),
            "cwd": ("cwd", "workingDirectory"),
            "spec_preview": ("spec_preview", "spec", "prompt"),
            "status": ("status",),
        }.items():
            for alias in aliases:
                value = string(data.get(alias))
                if value and not (key == "model" and value.startswith("<")):  # "<synthetic>" API-error turns
                    node[key] = value[:500] if key == "spec_preview" else value
                    break
        if data.get("ok") is not None:
            node["ok"] = data.get("ok")
        for key, aliases in {
            "created_at": ("created_at", "createdAt", "startTime", "timestamp"),
            "updated_at": ("updated_at", "updatedAt", "lastActivity", "timestamp"),
        }.items():
            values = [timestamp(data.get(a)) for a in aliases]
            values = [v for v in values if v is not None]
            if node[key] is not None:
                values.append(node[key])
            if values:
                node[key] = min(values) if key == "created_at" else max(values)
        pid = data.get("pid")
        if not isinstance(pid, bool) and str(pid).isdigit() and int(pid) > 0:
            node["_pids"].add(int(pid))

    def remember_mtime(node, path):
        try:
            node["_fallback_time"] = max(node["_fallback_time"], os.path.getmtime(path))
        except OSError:
            pass

    def parse_lines(raw_lines, into, path, index):
        default_sid = os.path.basename(path)[:-6]
        for raw in raw_lines:
            index += 1
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            node = node_for(event.get("sessionId") or event.get("session_id") or default_sid, into)
            if node is None:
                continue
            merge(node, event)
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            if event.get("type") == "user" and not node["spec_preview"]:
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(string(b.get("text")) for b in content
                                       if isinstance(b, dict) and b.get("type") == "text")
                node["spec_preview"] = string(content)[:500]
            if event.get("type") != "assistant":
                continue
            merge(node, message)
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = string(message.get("id")) or string(event.get("uuid")) or f"{path}:{index - 1}"
            totals = node["_usage"].setdefault(mid, {})
            for key in counters:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    totals[key] = max(totals.get(key, 0), int(value))
        return index

    def parse_jsonl(path, st):
        entry = _CLAUDE_JSONL_CACHE.get(path)
        if entry and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
            return entry[2]
        if entry and st.st_size >= entry[1]:
            state = entry[2]
        else:
            state = {"offset": 0, "lines": 0, "nodes": {}}
        with open(path, "rb") as handle:
            handle.seek(state["offset"])
            data = handle.read()
        end = data.rfind(b"\n") + 1
        tail = data[end:]
        try:
            json.loads(tail.decode("utf-8", errors="replace"))  # final record without a newline
            end = len(data)
        except ValueError:
            pass  # A writer may still be appending the last record; reread it next pass.
        raw_lines = data[:end].split(b"\n")
        if raw_lines and not raw_lines[-1]:
            raw_lines.pop()
        state["lines"] = parse_lines(raw_lines, state["nodes"], path, state["lines"])
        state["offset"] += end
        _CLAUDE_JSONL_CACHE[path] = (st.st_mtime_ns, st.st_size, state)
        return state

    seen_paths = set()
    for path in sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))):
        try:
            st = os.stat(path)
            if now - st.st_mtime > CLAUDE_JSONL_LOOKBACK_SEC:
                continue
            state = parse_jsonl(path, st)
        except OSError:
            continue
        seen_paths.add(path)
        for partial in state["nodes"].values():
            node = node_for(partial["id"])
            merge(node, partial)
            node["_pids"].update(partial["_pids"])
            for mid, totals in partial["_usage"].items():
                merged = node["_usage"].setdefault(mid, {})
                for key, value in totals.items():
                    merged[key] = max(merged.get(key, 0), value)
            node["_fallback_time"] = max(node["_fallback_time"], st.st_mtime)
            node["rollout_path"] = node["jsonl_path"] = path
    for stale in _CLAUDE_JSONL_CACHE.keys() - seen_paths:
        # Subagent transcripts are parsed later, only while they are under 24h old.
        if "/subagents/" in stale and now - _CLAUDE_JSONL_CACHE[stale][0] / 1e9 <= 86400:
            continue
        del _CLAUDE_JSONL_CACHE[stale]

    for path in sorted(glob.glob(os.path.expanduser("~/.claude/sessions/*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        node = node_for(data.get("sessionId") or data.get("session_id") or data.get("id"))
        if node is not None:
            merge(node, data)
            remember_mtime(node, path)
            # entrypoint "cli" is a human-driven terminal (Orca pane or shell); `claude -p`
            # workers report "sdk-cli". status is the live busy/idle flag Claude Code keeps.
            for key, alias in (("entrypoint", "entrypoint"), ("activity", "status"), ("tmux", "tmux")):
                if string(data.get(alias)):
                    node[key] = string(data.get(alias))

    for sid, data in _load_agy_spawn_registry().items():
        if not isinstance(data, dict) or data.get("provider") != "claude":
            continue
        node = node_for(data.get("session_id") or data.get("sessionId") or sid)
        if node is not None:
            merge(node, data)
            node["agy_origin"] = True

    # Exact CLI options avoid matching session IDs merely mentioned in prompts.
    argv_options: dict[int, dict] = {}
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,args="], text=True, stderr=subprocess.DEVNULL, timeout=2)
        for line in output.splitlines():
            try:
                pid_text, command = line.strip().split(None, 1)
                args = shlex.split(command)
            except ValueError:
                continue
            if not pid_text.isdigit() or not args:
                continue
            executable = os.path.basename(args[0])
            is_claude = executable in ("claude", "claude.exe") or (
                executable in ("node", "nodejs", "bun") and len(args) > 1 and
                ("claude" in args[1] and args[1].endswith((".js", "/claude"))))
            if not is_claude:
                continue
            options = {}
            for i, arg in enumerate(args):
                option, _, inline = arg.partition("=")
                if option in ("--model", "--effort"):
                    options[option] = inline or (args[i + 1] if i + 1 < len(args) else "")
                if option not in ("--session-id", "--resume", "-r"):
                    continue
                sid = inline or (args[i + 1] if i + 1 < len(args) else "")
                if sid in nodes:
                    nodes[sid]["_pids"].add(int(pid_text))
            argv_options[int(pid_text)] = options
    except (OSError, subprocess.SubprocessError):
        pass

    live_pids = {}

    def alive(pid):
        if pid not in live_pids:
            try:
                os.kill(pid, 0)
                live_pids[pid] = True
            except PermissionError:
                live_pids[pid] = True
            except (OSError, OverflowError):
                live_pids[pid] = False
        return live_pids[pid]

    result = []
    for node in nodes.values():
        pids = sorted(node.pop("_pids"))
        live = [pid for pid in pids if alive(pid)]
        fallback = node.pop("_fallback_time")
        node["updated_at"] = node["updated_at"] or node["created_at"] or fallback
        node["created_at"] = node["created_at"] or node["updated_at"]
        if not live and node["updated_at"] < now - 86400:
            continue
        node["pid"] = live[0] if live else (pids[0] if pids else None)
        options = argv_options.get(node["pid"]) or {}
        node["model"] = node["model"] or options.get("--model", "")
        node["effort"] = options.get("--effort") or node["effort"] or _claude_default_effort(node["model"])
        node["terminal"] = _claude_terminal(node, live[0] if live else None)
        is_term = node.get("status") in ("completed", "failed", "timeout", "done", "closed") or node.get("ok") is not None
        if is_term:
            node["running"] = node["is_running"] = False
            node["status"] = node.get("status") or "closed"
        else:
            node["running"] = node["is_running"] = bool(live)
            node["status"] = "running" if live else "closed"
        messages = node.pop("_usage")
        node["usage"] = {key: sum(m.get(key, 0) for m in messages.values()) for key in counters}
        node["tokens"] = sum(node["usage"].values())
        node["usage"]["total_tokens"] = node["tokens"]
        node["token_count"] = {"total_tokens": node["tokens"],
                               "input_tokens": node["usage"]["input_tokens"],
                               "output_tokens": node["usage"]["output_tokens"]}
        # Pasted prompts arrive wrapped in <pasted_content ...> tags; title by their first line.
        preview = re.sub(r"^\s*(?:<[^>\n]*>\s*)+", "", node["spec_preview"])
        node["title"] = node["title"] or preview.split("\n", 1)[0][:120] or "Claude Session"
        node["display_name"] = node["title"]
        node["turn_context"] = {"model": node["model"], "reasoning_effort": node["effort"]}
        node["subagents"] = _claude_subagents(node, now, parse_jsonl)
        result.append(node)
    # Open terminal sessions with recent work outrank finished workers for the 30 slots.
    return sorted(result, key=lambda n: (not (n["running"] and n["updated_at"] >= now - 86400),
                                         -n["updated_at"], n["id"]))[:30]


def _claude_default_effort(model: str) -> str:
    """Effort an interactive session starts with when its argv has no --effort."""
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(settings, dict):
        return ""
    per_model = (settings.get("modelSettings") or {}).get(model) if model else None
    value = (per_model or {}).get("effortLevel") if isinstance(per_model, dict) else None
    return str(value or settings.get("effortLevel") or "")


ORCA_PATH_PREFIXES = ("~/orca/", "~/.config/orca/")
ORCA_HANDLE_RE = re.compile(rb"term_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# First prompt of a pane another agent opened for delegated work (pasted-content tags stripped).
DELEGATED_PROMPT_RE = re.compile(
    r"^(?:<teammate-message\b|You own\b|You are (?:the |a |an )?[^\n.]{0,80}?\b(?:worker|teammate|sub-?agent)\b)", re.I)
_ORCA_HANDLE_SCAN: dict[str, tuple[int, int, frozenset]] = {}


def _transcript_orca_handles(path: str) -> frozenset:
    """Orca terminal handles a transcript mentions (`orca terminal create/send` I/O), read incrementally."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return frozenset()
    ino, offset, handles = _ORCA_HANDLE_SCAN.get(path) or (st.st_ino, 0, frozenset())
    if ino != st.st_ino or offset > st.st_size:
        offset, handles = 0, frozenset()
    if st.st_size > offset:
        start = max(0, offset - 64)  # a handle split across two reads still matches
        try:
            with open(path, "rb") as stream:
                stream.seek(start)
                chunk = stream.read(st.st_size - start)
        except OSError:
            return handles
        handles = handles | {match.decode() for match in ORCA_HANDLE_RE.findall(chunk)}
    if len(_ORCA_HANDLE_SCAN) >= 256:
        _ORCA_HANDLE_SCAN.clear()
    _ORCA_HANDLE_SCAN[path] = (st.st_ino, st.st_size, handles)
    return handles


ORCA_LEAD_MAP_FILE = "/tmp/maestro_orca_lead_map.json"
ORCA_LEAD_MAP_TTL_SEC = 7 * 86400


def _load_orca_lead_map(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {wid: rec for wid, rec in (data if isinstance(data, dict) else {}).items()
            if isinstance(rec, dict) and rec.get("lead") and now - float(rec.get("ts") or 0) <= ORCA_LEAD_MAP_TTL_SEC}


def _save_orca_lead_map(path: str, cache: dict[str, dict]) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".orca_lead_map.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def link_orca_teammates(trees: dict, live_workers=None, cache_path: str | None = None) -> dict[str, str]:
    """Nest Orca panes a lead pane opened (teammates, sub-workers) under that lead as SUB rows.

    Orca starts every pane from its own shell, so no process or env link exists. A lead
    that ran `orca terminal create/send` holds the pane's ORCA_TERMINAL_HANDLE in its
    transcript; failing that, a delegated first prompt with exactly one earlier
    non-delegated pane in the same Orca worktree names the owner. Sets `orca_lead_id`
    on each worker node and returns {worker id: lead id}. With `cache_path`, links found
    while both panes were live are remembered there, so a teammate whose process exited
    stays nested under its lead instead of surfacing as a lead of its own.
    """
    if live_workers is None:
        try:
            from live_worker_processes import scan_live_worker_processes
            live_workers = scan_live_worker_processes()
        except Exception:
            live_workers = []
    nodes: dict[str, dict] = {}
    for node in trees.get("claude") or []:
        if isinstance(node, dict):
            node.pop("orca_lead_id", None)
            if node.get("entrypoint") == "cli":
                nodes[node.get("session_id") or node.get("id") or ""] = node
    for node in trees.get("codex") or []:
        if isinstance(node, dict) and "error" not in node:
            node.pop("orca_lead_id", None)
            if node.get("source") != "exec" and not node.get("agy_origin"):
                nodes[node.get("thread_id") or ""] = node
    panes: dict[str, dict] = {}
    for record in live_workers or ():
        sid = record.get("session_id") if isinstance(record, dict) else None
        if (sid in nodes and sid and record.get("orca_terminal") and record.get("provider") in ("claude", "codex")
                and not record.get("parent_session_id") and not record.get("parent_conversation_id")):
            panes.setdefault(sid, record)

    def delegated(sid):
        node = nodes[sid]
        prompt = node.get("spec_preview") or node.get("first_user_message") or node.get("title") or ""
        return bool(DELEGATED_PROMPT_RE.match(re.sub(r"^\s*(?:<[^>\n]*>\s*)+", "", prompt)))

    def earlier(lead, worker):
        a, b = panes[lead].get("start_ticks") or 0, panes[worker].get("start_ticks") or 0
        return not (a and b) or a < b

    def worktree(sid):
        return panes[sid].get("orca_worktree") or panes[sid].get("cwd") or nodes[sid].get("cwd") or ""

    links: dict[str, str] = {}
    for wid, worker in panes.items():
        others = [lid for lid in panes if lid != wid and earlier(lid, wid)
                  and panes[lid]["orca_terminal"] != worker["orca_terminal"]]
        leads = [lid for lid in others if worker["orca_terminal"] in _transcript_orca_handles(
            nodes[lid].get("rollout_path") or nodes[lid].get("jsonl_path") or "")]
        if len(leads) > 1:
            # Sibling teammates are told about each other's panes; the lead itself was not delegated.
            leads = [lid for lid in leads if not delegated(lid)]
        elif not leads and delegated(wid):
            leads = [lid for lid in others if worktree(lid) == worktree(wid) and not delegated(lid)]
        if len(leads) == 1:
            links[wid] = leads[0]

    def top(wid):
        seen, lead = {wid}, links[wid]
        while lead in links and links[lead] not in seen:
            seen.add(lead)
            lead = links[lead]
        return lead

    # A pane a teammate opened nests under the top lead; cycles never get an owner.
    resolved = {wid: top(wid) for wid in links}
    resolved = {wid: lead for wid, lead in resolved.items() if lead not in links and lead != wid}
    if cache_path:
        cache = _load_orca_lead_map(cache_path)
        now = time.time()
        cache.update({wid: {"lead": lead, "ts": now} for wid, lead in resolved.items()})
        for wid, rec in cache.items():
            if wid in resolved or wid not in nodes:
                continue
            lead, seen = rec["lead"], {wid}
            while lead in cache and lead not in seen:  # a remembered lead may itself be nested
                seen.add(lead)
                lead = cache[lead]["lead"]
            if lead in nodes and lead not in seen:
                resolved[wid] = lead
        _save_orca_lead_map(cache_path, cache)
    for sid, pane in panes.items():
        nodes[sid]["orca_terminal_handle"] = pane["orca_terminal"]
    for wid, lead in resolved.items():
        nodes[wid]["orca_lead_id"] = lead
    return resolved



def _claude_terminal(node: dict, live_pid: int | None) -> str:
    """'orca' for sessions inside an Orca pane/worktree, 'cli' for other terminals."""
    if node.get("entrypoint") != "cli":
        return ""
    cwd = node.get("cwd") or ""
    if (node.get("tmux") or "").startswith("orca:") or any(
            cwd.startswith(os.path.expanduser(prefix)) for prefix in ORCA_PATH_PREFIXES):
        return "orca"
    if live_pid:
        try:
            with open(f"/proc/{live_pid}/environ", "rb") as handle:
                if b"\0ORCA_PANE_KEY=" in b"\0" + handle.read(131072):
                    return "orca"
        except OSError:
            pass
    return "cli"


CLAUDE_SUBAGENT_ACTIVE_SEC = 90


def _claude_subagents(node: dict, now: float, parse_jsonl, limit: int = 8) -> list[dict]:
    """Claude Code Task/teammate subagents of one session (<project>/<sid>/subagents/agent-*.jsonl).

    Only files touched in the last 24h are read; usage parsing reuses the
    incremental transcript cache.
    """
    path = node.get("jsonl_path") or ""
    if not path.endswith(".jsonl"):
        return []
    entries = []
    for sub_path in glob.glob(os.path.join(path[:-6], "subagents", "agent-*.jsonl")):
        try:
            st = os.stat(sub_path)
        except OSError:
            continue
        if now - st.st_mtime <= 86400:
            entries.append((st.st_mtime, sub_path, st))
    subagents = []
    for mtime, sub_path, st in sorted(entries, reverse=True)[:limit]:
        agent_id = os.path.basename(sub_path)[len("agent-"):-len(".jsonl")]
        meta = {}
        try:
            with open(sub_path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            pass
        meta = meta if isinstance(meta, dict) else {}
        usage, model, prompt = {}, "", ""
        try:
            state = parse_jsonl(sub_path, st)
        except OSError:
            state = {"nodes": {}}
        for partial in state["nodes"].values():
            model = partial.get("model") or model
            prompt = prompt or partial.get("spec_preview") or ""
            for totals in partial["_usage"].values():
                for key, value in totals.items():
                    usage[key] = usage.get(key, 0) + value
        total = sum(usage.values())
        title = string_or(meta.get("description")) or prompt.split("\n", 1)[0][:120] or "Claude subagent"
        subagents.append({
            "session_id": f"{node['id']}:{agent_id}", "agent_id": agent_id,
            "title": title, "display_name": title, "spec_preview": prompt[:500],
            "agent_type": string_or(meta.get("agentType") or meta.get("subagent_type")),
            "model": model or node.get("model") or "", "effort": node.get("effort") or "",
            "cwd": node.get("cwd") or "", "created_at": None, "updated_at": mtime,
            "is_running": bool(node.get("running")) and now - mtime <= CLAUDE_SUBAGENT_ACTIVE_SEC,
            "usage": usage, "token_count": {"total_tokens": total}, "jsonl_path": sub_path,
        })
    return subagents


def string_or(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def get_codex_agent_tree(db_path: str = "~/.codex/state_5.sqlite", max_parents: int = 12, max_children: int = 4) -> list[dict]:
    db_full = os.path.expanduser(db_path)
    if not os.path.exists(db_full):
        return []
    try:
        now_ts = int(time.time())
        run_cwds, run_snips = _running_codex_cwds_and_snippets()
        conn = sqlite3.connect(db_full)
        cur = conn.cursor()

        # Fetch recent root threads within 30 days (no hardcoded Discord translation bot!)
        cutoff_30d = now_ts - (30 * 86400)
        parent_rows = cur.execute("""
            SELECT t.id, t.name, t.title, t.cwd, t.rollout_path, t.model, t.reasoning_effort, t.created_at, t.updated_at, t.source,
                   (SELECT COUNT(*) FROM thread_spawn_edges e WHERE e.parent_thread_id = t.id) as child_cnt
            FROM threads t
            WHERE t.id NOT IN (SELECT child_thread_id FROM thread_spawn_edges)
              AND t.archived = 0
              AND t.updated_at >= ?
            ORDER BY t.updated_at DESC
            LIMIT ?
        """, (cutoff_30d, max_parents)).fetchall()

        spawn_reg = _load_agy_spawn_registry()
        trees = []
        for p_id, p_name, p_title, p_cwd, p_rollout, p_model, p_effort, p_created, p_updated, p_source, child_cnt in parent_rows:
            p_data = parse_codex_rollout(p_rollout, fallback_model=p_model or "gpt-6.1-sol", fallback_effort=p_effort or "ultra")
            abs_cwd = os.path.abspath(p_cwd) if p_cwd else ""
            recency = max(0, now_ts - int(p_updated or now_ts))
            p_mtime_age = now_ts - int(os.path.getmtime(p_rollout)) if p_rollout and os.path.exists(p_rollout) else 999999
            turn_done = bool(p_data.get("turn_completed", False))
            is_running = False
            if not turn_done and ((recency <= 90 or p_mtime_age <= 60)
                    or (recency <= 180 and any(p_id in s for s in run_snips))):
                is_running = True
            age_days = max(0, int((now_ts - int(p_updated or now_ts)) // 86400))
            disp = _clean_parent_title(p_name, p_title, p_cwd)
            agy_origin = False if p_source == 'vscode' else (_has_agy_prefix(p_name) or _has_agy_prefix(p_title) or p_id in spawn_reg)

            # Order children so those with sub-subagents (depth 2) appear first, then most recent
            children_rows = cur.execute("""
                SELECT t.id, t.agent_nickname, t.agent_role, t.agent_path, t.title, t.first_user_message,
                       e.status, t.rollout_path, t.model, t.reasoning_effort, t.created_at, t.updated_at
                FROM thread_spawn_edges e
                JOIN threads t ON e.child_thread_id = t.id
                WHERE e.parent_thread_id = ?
                ORDER BY
                  (SELECT COUNT(*) FROM thread_spawn_edges e2 WHERE e2.parent_thread_id = t.id) DESC,
                  t.updated_at DESC
                LIMIT ?
            """, (p_id, max_children)).fetchall()

            children = []
            for c_id, c_nick, c_role, c_path, c_title, c_fmsg, c_status, c_rollout, c_model, c_effort, c_created, c_updated in children_rows:
                c_data = parse_codex_rollout(c_rollout, fallback_model=c_model or "gpt-6.1-sol", fallback_effort=c_effort or "ultra")
                c_recency = max(0, now_ts - int(c_updated or 0))

                # Sub-subagent traversal (Depth 2)
                sub_children_rows = cur.execute("""
                    SELECT t.id, t.agent_nickname, t.agent_role, t.agent_path, t.title, t.first_user_message,
                           e.status, t.rollout_path, t.model, t.reasoning_effort, t.created_at, t.updated_at
                    FROM thread_spawn_edges e
                    JOIN threads t ON e.child_thread_id = t.id
                    WHERE e.parent_thread_id = ?
                    ORDER BY t.updated_at DESC
                """, (c_id,)).fetchall()

                sub_children = []
                for sc_id, sc_nick, sc_role, sc_path, sc_title, sc_fmsg, sc_status, sc_rollout, sc_model, sc_effort, sc_created, sc_updated in sub_children_rows:
                    sc_data = parse_codex_rollout(sc_rollout, fallback_model=sc_model or "gpt-6.1-sol", fallback_effort=sc_effort or "ultra")
                    sc_recency = max(0, now_ts - int(sc_updated or 0))
                    sub_children.append({
                        "thread_id": sc_id,
                        "nickname": sc_nick or sc_role or "Subagent",
                        "display_name": _clean_codex_label(sc_nick, sc_role, sc_path, sc_title, sc_fmsg),
                        "role": sc_role,
                        "agent_path": sc_path,
                        "title": sc_title,
                        "status": sc_status,
                        "created_at": sc_created,
                        "updated_at": sc_updated,
                        "rollout_path": sc_rollout,
                        **{key: sc_data.get(key) for key in TURN_TIMING_KEYS},
                        "is_running": (not sc_data.get("turn_completed") and sc_recency <= 60),
                        "token_count": sc_data["token_count"],
                        "turn_context": sc_data["turn_context"],
                        "children": [],
                    })

                children.append({
                    "thread_id": c_id,
                    "nickname": c_nick or c_role or "Subagent",
                    "display_name": _clean_codex_label(c_nick, c_role, c_path, c_title, c_fmsg),
                    "role": c_role,
                    "agent_path": c_path,
                    "title": c_title,
                    "status": c_status,
                    "created_at": c_created,
                    "updated_at": c_updated,
                    "rollout_path": c_rollout,
                    **{key: c_data.get(key) for key in TURN_TIMING_KEYS},
                    "is_running": (not c_data.get("turn_completed") and c_recency <= 60),
                    "token_count": c_data["token_count"],
                    "turn_context": c_data["turn_context"],
                    "children": sub_children,
                })

            has_running_child = any(ch.get("is_running") for ch in children)
            trees.append({
                "thread_id": p_id,
                "display_name": disp,
                "title": p_title,
                "cwd": p_cwd,
                "source": p_source,
                "rollout_path": p_rollout,
                "status": "running" if (is_running or has_running_child) else "closed",
                "turn_completed": turn_done,
                "created_at": p_created,
                "updated_at": p_updated,
                **{key: p_data.get(key) for key in TURN_TIMING_KEYS},
                "age_days": age_days,
                "agy_origin": agy_origin,
                "role": "Parent Coordinator" if child_cnt > 0 else "Standalone Session",
                "total_children_count": child_cnt,
                "token_count": p_data["token_count"],
                "turn_context": p_data["turn_context"],
                "children": children,
            })
        conn.close()
        return trees
    except Exception as e:
        return [{"error": str(e)}]



def _extract_window_pct(window: dict | None) -> tuple[float | None, bool]:
    """Return (remaining_percent, has_error) for a single quota window."""
    if not isinstance(window, dict):
        return None, False
    if window.get("error"):
        return None, True
    if window.get("status") == "정보 없음":
        return None, False
    value = window.get("remaining_percent")
    if value is None and window.get("used_percent") is not None:
        if isinstance(window["used_percent"], bool):
            return None, True
        try:
            value = 100.0 - float(window["used_percent"])
        except (TypeError, ValueError):
            return None, True
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, True
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        return None, True
    return value, False


WEEKLY_CONSERVE_THRESHOLD = 10.0  # 7d <= 10%: 주간 보호 감속·우회 구간 (여유 있는 다른 풀 우선 배분)
WEEKLY_RESERVE_FLOOR = 2.0        # 7d <= 2%: 비상 예비 동결 구간 (타 풀 가용 시 자동배분 제외)


def _routing_windows(usage: dict, provider: str | None = None) -> tuple[float | None, float | None, float | None, str]:
    """Return (effective_routing_pct, five_hour_remaining, weekly_remaining, routing_mode).
    - Normal (7d > 10%): blend 5h and 7d remaining quotas (Codex 0.25*5h + 0.75*7d, Claude 0.45*5h + 0.55*7d).
    - Weekly Conserve (2% < 7d <= 10%): throttle score so pools with higher 7d headroom take primary load.
    - Reserve Hold (0% < 7d <= 2%): hold last 2% for manual use unless all other pools are exhausted.
    - Exhausted (5h <= 0% or 7d <= 0%): hard block (0.0).
    """
    if usage.get("error") or (usage.get("stale") and not usage.get("reset_reconciled")) or usage.get("quota_available") is False:
        return None, None, None, "unavailable"
    if usage.get("rate_limited"):
        return 0.0, 0.0, 0.0, "exhausted"
    fh_rem, fh_err = _extract_window_pct(usage.get("five_hour"))
    wk_rem, wk_err = _extract_window_pct(usage.get("weekly"))
    if fh_err or wk_err:
        return None, fh_rem, wk_rem, "unavailable"
    if fh_rem is None and wk_rem is None:
        top_rem, top_err = _extract_window_pct(usage)
        if top_err:
            return None, None, None, "unavailable"
        if top_rem is not None and top_rem <= 0.0:
            return 0.0, None, 0.0, "exhausted"
        return top_rem, None, top_rem, "normal"
    # Hard exhaustion on EITHER window blocks the pool immediately (e.g. Opus weekly 0% -> blocked!)
    if (fh_rem is not None and fh_rem <= 0.0) or (wk_rem is not None and wk_rem <= 0.0):
        return 0.0, fh_rem, wk_rem, "exhausted"
    # Zone 3: Critical weekly reserve floor (0% < 7d <= 2%) -> freeze auto-routing if other pools exist
    if wk_rem is not None and wk_rem <= WEEKLY_RESERVE_FLOOR:
        base = fh_rem if fh_rem is not None else wk_rem
        return round(min(base, wk_rem * 0.3), 2), fh_rem, wk_rem, "reserve_hold"
    # Zone 2: Weekly conservation zone (2% < 7d <= 10%) -> throttle score to rotate load to abundant pools
    if wk_rem is not None and wk_rem <= WEEKLY_CONSERVE_THRESHOLD:
        if fh_rem is not None:
            return round(min(fh_rem, wk_rem * 3.5), 2), fh_rem, wk_rem, "weekly_conserve"
        return wk_rem, None, wk_rem, "weekly_conserve"
    # Zone 1: Normal zone (7d > 10%) -> blend 5h and 7d rolling/weekly quotas
    if fh_rem is not None and wk_rem is not None:
        if provider == "antigravity":
            blended = 0.25 * fh_rem + 0.75 * min(fh_rem, wk_rem)
        elif (provider == "codex") or bool(usage.get("plan")) or ("service_tiers" in usage):
            blended = 0.25 * fh_rem + 0.75 * wk_rem
        else:
            blended = 0.45 * fh_rem + 0.55 * wk_rem
        return round(blended, 2), fh_rem, wk_rem, "normal"
    if fh_rem is not None:
        return fh_rem, fh_rem, wk_rem, "normal"
    return wk_rem, None, wk_rem, "normal"


def _routing_remaining(usage: dict) -> float | None:
    eff, _, _, _ = _routing_windows(usage)
    return eff


def select_best_pool(data, difficulty='high', urgent: bool = False, interactive: bool = False, spec: str = "") -> dict:
    """Rank shared quota pools with 3-Zone Weekly Guardrail (10% conserve / 2% reserve / 0% block)
    and task-domain live benchmark recommendations.
    """
    if difficulty not in ("high", "medium", "low"):
        raise ValueError("difficulty must be high, medium, or low")

    agy = data.get("antigravity") or {}
    agy_pools = agy.get("pools") or {}
    codex = data.get("codex_local") or {}
    claude = data.get("claude_local") or {}
    weights = {
        "high": (1.0, 1.0, 1.0, 1.08),
        "medium": (1.0, 1.0, 1.0, 1.05),
        "low": (0.8, 1.0, 0.9, 0.8),
    }[difficulty]

    def agy_model(pool, family):
        models = pool.get("models") or []
        preferred = ("Opus" if difficulty == "high" else "Sonnet") if family == "claude" else ("Flash" if difficulty == "low" else "Argon")
        effort = f"({difficulty.title()}"
        ranked = sorted(models, key=lambda model: (preferred in model, effort in model), reverse=True)
        if ranked:
            return ranked[0]
        name = "Claude Opus 5.5" if family == "claude" else "Gemini 4 Argon"
        return f"{name} ({difficulty.title()})"

    cl_pool = agy_pools.get("Claude & GPT-OSS Pool (Antigravity)") or {}
    gem_pool = agy_pools.get("Gemini Pool (Argon / Flash)") or {}
    if agy.get("error") or agy.get("stale"):
        cl_pool = gem_pool = agy
    if difficulty == "low":
        codex_model = "gpt-6-luna"
    elif difficulty == "medium":
        codex_model = "gpt-6-astra"
    else:
        codex_model = "gpt-6.1-sol"
    specs = [
        ("antigravity_claude", "antigravity", "Claude & GPT-OSS Pool (Antigravity)", cl_pool, agy_model(cl_pool, "claude")),
        ("antigravity_gemini", "antigravity", "Gemini Pool (Argon / Flash)", gem_pool, agy_model(gem_pool, "gemini")),
        ("codex", "codex", "Codex " + str(codex.get("plan") or "ProMax"), codex, codex_model),
        ("claude", "claude", "Claude " + str(claude.get("subscription") or "Team"), claude, "claude-opus-5-5"),
    ]
    bench_info = {}
    rec_info = {}
    try:
        import benchmark_sync
        bench_info = benchmark_sync.get_daily_benchmark_and_models(agy)
        rec_info = benchmark_sync.recommend_model_for_task(spec=spec, difficulty=difficulty, usage_data=data)
    except Exception:
        bench_info = {}
        rec_info = {}
    bench_scores = bench_info.get("benchmark_scores") or {}
    preference = {
        "codex": int((bench_scores.get("codex") or {}).get("routing_preference", 3)),
        "antigravity_claude": int((bench_scores.get("antigravity_claude") or {}).get("routing_preference", 2)),
        "antigravity_gemini": int((bench_scores.get("antigravity_gemini") or {}).get("routing_preference", 1)),
        "claude": int((bench_scores.get("claude") or {}).get("routing_preference", 0)),
    }

    candidates = []
    for (pool_id, provider, label, usage, model), weight in zip(specs, weights):
        remaining, fh_rem, wk_rem, routing_mode = _routing_windows(usage, provider=provider)
        unavailable = (provider == "claude" and usage.get("authenticated") is False) or (provider == "codex" and usage.get("model_available") is False)
        status = "unavailable" if unavailable else "unknown" if remaining is None else "exhausted" if remaining <= 0 else "available"
        b_meta = bench_scores.get(pool_id) or {}
        cand_score = round(remaining * weight, 2) if status == "available" else 0.0
        # Protect Antigravity Gemini (4 Argon) pool for parent IDE sessions when 7d < 50%
        if pool_id == "antigravity_gemini" and wk_rem is not None and wk_rem < 50.0:
            cand_score = round(cand_score * 0.35, 2)
        candidate = {
            "pool": pool_id,
            "provider": provider,
            "label": label,
            "model": model,
            "remaining_percent": remaining,
            "five_hour_remaining": fh_rem,
            "weekly_remaining": wk_rem,
            "routing_mode": routing_mode,
            "window_basis": "5h+7d_guard" if (fh_rem is not None and wk_rem is not None and wk_rem <= WEEKLY_CONSERVE_THRESHOLD) else ("5h+7d_blend" if (fh_rem is not None and wk_rem is not None) else ("5h" if fh_rem is not None else "7d")),
            "status": status,
            "score": cand_score,
            "swe_bench_verified": b_meta.get("swe_bench_verified"),
        }
        if provider == "codex":
            tiers = usage.get("service_tiers", ["priority"])
            tiers = [tier for tier in ("priority", "ultrafast") if tier in tiers]
            if not tiers:
                tier = None
            elif urgent and difficulty == "high" and remaining is not None and remaining >= 50 and "ultrafast" in tiers:
                tier = "ultrafast"
            elif (interactive or urgent) and "priority" in tiers:
                tier = "priority"
            else:
                tier = "default"
            candidate.update(service_tier=tier, available_service_tiers=tiers, shared_quota=True)
            if not tier:
                candidate.update(status="unavailable", score=0.0)
        candidates.append(candidate)

    if spec:
        rec_canonical = rec_info.get("canonical_pool")
        rec_cand = next((c for c in candidates if c["pool"] == rec_canonical and c["status"] == "available"), None)
        if rec_cand and (rec_cand.get("remaining_percent") or 0.0) >= 20.0:
            candidates.sort(key=lambda item: (item["status"] == "available", item["pool"] == rec_canonical, item["score"]), reverse=True)
        else:
            candidates.sort(key=lambda item: (item["status"] == "available", item["score"], preference.get(item["pool"], 0)), reverse=True)
    else:
        candidates.sort(key=lambda item: (item["status"] == "available", item["score"], preference.get(item["pool"], 0)), reverse=True)

    best = next((item for item in candidates if item["status"] == "available"), None)
    best_model = (rec_info.get("recommended_model") if (spec and best and best["pool"] == rec_info.get("canonical_pool")) else (best["model"] if best else None))
    return {
        "difficulty": difficulty,
        "spec": spec,
        "task_domain": rec_info.get("domain", "general_coding"),
        "pool": best["pool"] if best else None,
        "provider": best["provider"] if best else None,
        "model": best_model,
        "service_tier": best.get("service_tier") if best else None,
        "remaining_percent": best["remaining_percent"] if best else None,
        "five_hour_remaining": best.get("five_hour_remaining") if best else None,
        "weekly_remaining": best.get("weekly_remaining") if best else None,
        "routing_mode": best.get("routing_mode") if best else None,
        "citation": rec_info.get("citation"),
        "cost_krw": rec_info.get("cost_krw"),
        "benchmark_recommendation": rec_info,
        "new_models_detected": bench_info.get("new_models_detected") or [],
        "benchmark_last_sync_kst": bench_info.get("last_sync_kst"),
        "reason": (
            f"{best['label']}: 유효점수 {best['score']:g} "
            f"(5h={best.get('five_hour_remaining') if best.get('five_hour_remaining') is not None else 'N/A'}%, "
            f"7d={best.get('weekly_remaining') if best.get('weekly_remaining') is not None else 'N/A'}%, "
            f"모드={best.get('routing_mode','normal')})"
            if best else "가용 한도가 확인된 풀이 없습니다."
        ),
        "policy": "Task-Domain Benchmark Routing with 3-Zone Weekly Guardrail (LOW_QUOTA_THRESHOLD = 20.0%)",
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Per-agent process resources (CPU / RAM / GPU VRAM / I/O)
#
# One /proc pass builds the ppid tree; CPU% and I/O are deltas against the
# previous pass (lifetime average on the first). Attribution uses exact
# evidence only: ANTIGRAVITY_CONVERSATION_ID + the run_command stepIndex
# (task-<stepIndex>.log) in the inherited environment, a task log on fd 1/2,
# and live CLI worker PIDs. Every summary carries its PID list so rollups
# take a union instead of double counting nested trees.
PROC_SNAPSHOT_TTL_SEC = 2.5
GPU_SNAPSHOT_TTL_SEC = 3.0
GPU_TOTAL_TTL_SEC = 300.0
# tx = bytes sent/written (wchar), rx = bytes received/read (rchar).
# Per-PID rows carry Linux per-core CPU% (one logical core = 100, so 24 threads
# max out at 2400); summaries rescale it to cpu_pct on the 0-100 total-system
# scale shared with ram_pct / gpu_pct and keep the raw sum as cpu_core_pct.
RESOURCE_FIELDS = ("cpu_pct", "ram_mb", "gpu_mb", "tx_kbps", "rx_kbps")
_ENV_STEP_RE = re.compile(rb'"stepIndex"\s*:\s*(\d+)')
_TASK_LOG_FD_RE = re.compile(r"/brain/([^/]+)/\.system_generated/tasks/(task-\d+)\.log(?: \(deleted\))?$")
# Antigravity runtime (language_server + the Electron window tree) hosting every
# AGY conversation. What no run_command / worker tree claims is split across the
# live AGY sessions as negative pseudo-PIDs, so a main agent carries its own
# CPU / RAM / I/O and rollups stay additive under the PID-union rules.
AGY_RUNTIME_COMMS = ("language_server", "antigravity")
AGY_RUNTIME_RECENT_SEC = 7 * 86400  # idle roots the widget still lists as active
AGY_RUNTIME_IDLE_WEIGHT = 0.1  # CPU / I/O weight of an idle session against a running one
AGY_RUNTIME_RAM_TOKENS = 100_000  # context tokens worth one extra RAM weight unit
_INTERNAL_AGY_TITLE_PREFIXES = ("✦", "🪐", "[AGY]")
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
    _PAGE_KB = max(1, os.sysconf("SC_PAGE_SIZE") // 1024)
except (AttributeError, ValueError, OSError):
    _CLK_TCK, _PAGE_KB = 100, 4
_proc_cache: dict[str, tuple[float, dict]] = {}
_proc_prev: dict[str, dict] = {}
_gpu_cache: tuple[float, dict] = (-1e9, {})
_gpu_total_cache: tuple[float, float] = (-1e9, 0.0)


def _read_proc_bytes(path: str, limit: int = 131072) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _env_value(env: bytes, key: bytes) -> bytes:
    prefix = key + b"="
    if env.startswith(prefix):
        start = len(prefix)
    else:
        found = env.find(b"\0" + prefix)
        if found < 0:
            return b""
        start = found + 1 + len(prefix)
    end = env.find(b"\0", start)
    return env[start:] if end < 0 else env[start:end]


def _scan_process_table(proc_root: str = "/proc") -> dict[int, dict]:
    """pid -> ppid, CPU ticks, RSS and the conversation/task evidence of one /proc pass."""
    table = {}
    try:
        names = [e.name for e in os.scandir(proc_root) if e.name.isdigit()]
    except OSError:
        return table
    for name in names:
        base = f"{proc_root}/{name}"
        text = _read_proc_bytes(base + "/stat", 4096).decode("utf-8", "replace")
        close = text.rfind(")")
        fields = text[close + 2:].split()
        if close < 0 or len(fields) < 22 or fields[0] in ("Z", "X"):
            continue
        try:
            ppid, ticks = int(fields[1]), int(fields[11]) + int(fields[12])
            start, rss_pages = int(fields[19]), int(fields[21])
        except ValueError:
            continue
        cid = task = ""
        env = _read_proc_bytes(base + "/environ")  # empty for other users' processes
        if env:
            cid = _env_value(env, b"ANTIGRAVITY_CONVERSATION_ID").decode("utf-8", "replace").strip()
            match = _ENV_STEP_RE.search(_env_value(env, b"ANTIGRAVITY_SOURCE_METADATA")) if cid else None
            task = f"task-{int(match.group(1))}" if match else ""
            if not task:
                for fd in ("1", "2"):
                    try:
                        match = _TASK_LOG_FD_RE.search(os.readlink(f"{base}/fd/{fd}"))
                    except OSError:
                        match = None
                    if match and (not cid or match.group(1) == cid):
                        cid, task = match.group(1), match.group(2)
                        break
        table[int(name)] = {"ppid": ppid, "comm": text[text.find("(") + 1:close], "ticks": ticks,
                            "start": start, "rss_kb": rss_pages * _PAGE_KB, "cid": cid, "task": task}
    return table


def _gpu_process_memory(force: bool = False) -> dict[int, float]:
    """pid -> VRAM MiB from nvidia-smi compute apps; cached, {} without a GPU."""
    global _gpu_cache
    now = time.monotonic()
    if not force and now - _gpu_cache[0] < GPU_SNAPSHOT_TTL_SEC:
        return _gpu_cache[1]
    usage = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5).stdout
        for line in out.splitlines():
            pid_text, _, mem_text = line.partition(",")
            try:
                usage[int(pid_text)] = usage.get(int(pid_text), 0.0) + float(mem_text)
            except ValueError:
                continue  # "[N/A]" on some drivers
    except (OSError, subprocess.SubprocessError):
        pass
    _gpu_cache = (now, usage)
    return usage


def _gpu_total_memory() -> float:
    """Total VRAM MiB over all GPUs (rarely changes, cached); 0.0 without a GPU."""
    global _gpu_total_cache
    now = time.monotonic()
    if now - _gpu_total_cache[0] < GPU_TOTAL_TTL_SEC:
        return _gpu_total_cache[1]
    total = 0.0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=1.5).stdout
        for line in out.splitlines():
            try:
                total += float(line)
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    _gpu_total_cache = (now, total)
    return total


def _mem_total_mb(proc_root: str = "/proc") -> float:
    for line in _read_proc_bytes(f"{proc_root}/meminfo", 4096).splitlines():
        if line.startswith(b"MemTotal:"):
            try:
                return int(line.split()[1]) / 1024
            except (IndexError, ValueError):
                break
    return 0.0


def _cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def process_snapshot(proc_root: str = "/proc", *, force: bool = False) -> dict:
    """Cached system process snapshot with per-PID CPU% (delta against the previous pass)."""
    now = time.monotonic()
    cached = _proc_cache.get(proc_root)
    if cached and not force and now - cached[0] < PROC_SNAPSHOT_TTL_SEC:
        return cached[1]
    procs = _scan_process_table(proc_root)
    try:
        uptime = float(_read_proc_bytes(f"{proc_root}/uptime", 64).split()[0])
    except (IndexError, ValueError):
        uptime = 0.0
    prev = _proc_prev.get(proc_root, {})
    state = {}
    for pid, p in procs.items():
        key = (pid, p["start"])
        last = prev.get(key)
        p["age_sec"] = max(0.0, uptime - p["start"] / _CLK_TCK)
        if last and now > last["mono"]:
            cpu = (p["ticks"] - last["ticks"]) / _CLK_TCK / (now - last["mono"]) * 100
        else:
            cpu = (p["ticks"] / _CLK_TCK) / p["age_sec"] * 100 if p["age_sec"] > 1 else 0.0
        p["cpu_pct"] = max(0.0, cpu)
        # I/O counters are carried forward until a later pass reads them again.
        state[key] = {"mono": now, "ticks": p["ticks"], "io": (last or {}).get("io"),
                      "io_mono": (last or {}).get("io_mono")}
    children: dict[int, list[int]] = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)
    gpu = _gpu_process_memory()
    totals = {"cpu_count": _cpu_count(), "mem_total_mb": round(_mem_total_mb(proc_root), 1),
              "gpu_total_mb": round(_gpu_total_memory(), 1) if gpu or shutil.which("nvidia-smi") else 0.0}
    snap = {"proc_root": proc_root, "mono": now, "sampled_at": time.time(), "procs": procs,
            "children": children, "gpu": gpu, "totals": totals, "state": state, "stats": {}}
    _proc_prev[proc_root] = state
    if len(_proc_cache) >= 8:
        _proc_cache.clear()
    _proc_cache[proc_root] = (now, snap)
    return snap


def _descendants(snap: dict, roots) -> set[int]:
    procs, children = snap["procs"], snap["children"]
    seen: set[int] = set()
    stack = [pid for pid in roots if pid in procs]
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack.extend(children.get(pid, ()))
    return seen


def _pid_stats(snap: dict, pid: int) -> tuple[float, float, float, float, float]:
    """(per-core cpu %, ram_mb, gpu_mb, tx_kbps, rx_kbps); /proc/<pid>/io is read only for attributed PIDs."""
    stats = snap["stats"].get(pid)
    if stats is not None:
        return stats
    p = snap["procs"][pid]
    state = snap["state"].get((pid, p["start"])) or {}
    rates = (0.0, 0.0)
    raw = _read_proc_bytes(f"{snap['proc_root']}/{pid}/io", 1024)
    counters = dict(line.split(b":", 1) for line in raw.splitlines() if b":" in line)
    try:
        io = (int(counters[b"wchar"]), int(counters[b"rchar"]))  # (sent/written, received/read)
    except (KeyError, ValueError):
        io = None
    if io is not None:
        last = state.get("io")
        if isinstance(last, tuple) and snap["mono"] > (state.get("io_mono") or snap["mono"]):
            span = snap["mono"] - state["io_mono"]
            rates = tuple(max(0, cur - prev) / 1024 / span for cur, prev in zip(io, last))
        elif p.get("age_sec", 0) > 1:
            rates = tuple(value / 1024 / p["age_sec"] for value in io)
        state.update(io=io, io_mono=snap["mono"])
    stats = (p["cpu_pct"], p["rss_kb"] / 1024, float(snap["gpu"].get(pid, 0.0)), *rates)
    snap["stats"][pid] = stats
    return stats


def _resource_summary(stats, pids, totals=None) -> dict:
    """Field sums plus ram_pct / gpu_pct of system RAM and total VRAM when those totals are known.

    cpu_pct is the share of the whole machine (per-core sum / logical CPUs, capped at 100);
    cpu_core_pct keeps the per-core sum (164.6 = 1.65 cores busy).
    """
    sums = [sum(values) for values in zip(*stats)] if stats else [0.0] * len(RESOURCE_FIELDS)
    summary = {key: round(value, 1) for key, value in zip(RESOURCE_FIELDS, sums)}
    ncpu = max(1, int((totals or {}).get("cpu_count") or 0) or _cpu_count())
    summary.update(cpu_pct=round(min(100.0, sums[0] / ncpu), 2), cpu_core_pct=round(sums[0], 1), cpu_count=ncpu)
    for pct_key, field, total_key in (("ram_pct", 1, "mem_total_mb"), ("gpu_pct", 2, "gpu_total_mb")):
        total = float((totals or {}).get(total_key) or 0)
        if total > 0:
            summary[pct_key] = round(sums[field] / total * 100, 2)
    shares = sum(1 for pid in pids if int(pid) < 0)
    if shares:
        summary["runtime_shares"] = shares  # AGY runtime pseudo-PIDs, not real processes
    return {**summary, "proc_count": len(pids) - shares, "pids": sorted(pids)}


def summarize_pids(snap: dict, pids) -> dict:
    """Sum one PID set; the sorted PID list lets callers union overlapping trees."""
    pids = [pid for pid in set(pids) if pid in snap["procs"]]
    return _resource_summary([_pid_stats(snap, pid) for pid in pids], pids, snap.get("totals"))


def merge_resources(proc_table: dict, resources, totals=None) -> dict | None:
    """Roll up resource summaries by PID union using process_resources['procs'].

    Nested trees (a worker under its conversation's shell) are counted once.
    `totals` (process_resources itself works) supplies cpu_count / mem_total_mb / gpu_total_mb.
    """
    pids = set()
    for res in resources:
        if isinstance(res, dict):
            pids.update(int(pid) for pid in res.get("pids") or ())
    pids = [pid for pid in pids if str(pid) in (proc_table or {})]
    if not pids:
        return None
    rows = [[v if isinstance(v, (int, float)) else 0.0 for v in proc_table[str(pid)][:len(RESOURCE_FIELDS)]]
            for pid in pids]
    summary = _resource_summary(rows, pids, totals)
    summary["active_procs"] = summary["proc_count"]
    return summary


def _set_resources(node: dict, key: str, value) -> None:
    """Attach only real data; stale values on cached nodes are dropped."""
    if value:
        node[key] = value
    else:
        node.pop(key, None)


def _is_agy_runtime(proc) -> bool:
    comm = (proc or {}).get("comm") or ""
    return any(comm.startswith(name) for name in AGY_RUNTIME_COMMS)


def _norm_workspace(value) -> str:
    """file:///home/u/dev and the language_server's file_home_u_dev both become file_home_u_dev."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _workspace_id_arg(proc_root: str, pid: int) -> str:
    args = _read_proc_bytes(f"{proc_root}/{pid}/cmdline", 16384).split(b"\0")
    for i, arg in enumerate(args):
        if arg == b"--workspace_id" and i + 1 < len(args):
            return args[i + 1].decode("utf-8", "replace")
        if arg.startswith(b"--workspace_id="):
            return arg.split(b"=", 1)[1].decode("utf-8", "replace")
    return ""


def _agy_runtime_sessions(trees: dict, now: float | None = None) -> list[dict]:
    """AGY sessions sharing the runtime: running conversations plus the idle main agents still listed as active.

    CPU / I/O weight favours running sessions; RAM weight grows with the context
    the language server keeps in memory for that conversation.
    """
    now = time.time() if now is None else now
    sessions, seen = [], set()

    def visit(node, depth):
        cid = node.get("conversation_id")
        if not cid or cid in seen or "error" in node:
            return
        seen.add(cid)
        running = bool(node.get("is_running")) and not node.get("killed")
        if not running:
            ts = _timing_timestamp(node.get("last_modified_time"))
            title = str(node.get("title") or "").lstrip()
            listed = (depth == 0 and not node.get("is_subagent") and not node.get("killed")
                      and not title.startswith(_INTERNAL_AGY_TITLE_PREFIXES)
                      and ts is not None and now - ts <= AGY_RUNTIME_RECENT_SEC)
        if running or listed:
            tokens = (node.get("context_usage") or {}).get("used_tokens") or 0
            tokens = float(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else 0.0
            sessions.append({"cid": cid, "node": node, "running": running,
                             "cpu_w": 1.0 if running else AGY_RUNTIME_IDLE_WEIGHT,
                             "ram_w": 1.0 + max(0.0, tokens) / AGY_RUNTIME_RAM_TOKENS,
                             "workspaces": {_norm_workspace(w) for w in node.get("workspaces") or () if w}})
        for child in node.get("children") or []:
            if isinstance(child, dict):
                visit(child, depth + 1)

    for root in (trees or {}).get("antigravity") or []:
        if isinstance(root, dict):
            visit(root, 0)
    return sessions


def _agy_runtime_pools(snap: dict, claimed: set) -> dict[str, set[int]]:
    """Workspace key ("" = shared by every session) -> runtime PIDs no conversation / worker tree claims.

    A language_server started with --workspace_id keeps its own subtree for that
    workspace; the hub server and the Electron window tree are shared.
    """
    procs = snap["procs"]
    roots = [pid for pid, p in procs.items() if _is_agy_runtime(p) and not _is_agy_runtime(procs.get(p["ppid"]))]
    pools = {"": _descendants(snap, roots) - claimed}
    for pid in sorted(pools[""]):
        if pid in pools[""] and procs[pid]["comm"].startswith("language_server"):
            workspace = _norm_workspace(_workspace_id_arg(snap["proc_root"], pid))
            if workspace:
                own = _descendants(snap, [pid]) & pools[""]
                pools.setdefault(workspace, set()).update(own)
                pools[""] -= own
    return {key: pids for key, pids in pools.items() if pids}


def _split_agy_runtime(snap: dict, pools: dict, sessions: list[dict]) -> dict[str, tuple]:
    """cid -> (per-core cpu, ram_mb, gpu_mb, tx, rx) share; each pool is split in full, so shares add up to it.

    A workspace pool goes to the sessions of that workspace; one without sessions joins the shared pool.
    """
    shared = set(pools.get("", ()))
    groups = []
    for key, pids in pools.items():
        members = [s for s in sessions if key and key in s["workspaces"]]
        if members:
            groups.append((pids, members))
        elif key:
            shared |= pids
    if shared and sessions:
        groups.append((shared, sessions))
    shares: dict[str, tuple] = {}
    for pids, members in groups:
        sums = [sum(values) for values in zip(*(_pid_stats(snap, pid) for pid in pids))]
        cpu_w = sum(s["cpu_w"] for s in members)
        ram_w = sum(s["ram_w"] for s in members)
        for s in members:
            cpu, ram = s["cpu_w"] / cpu_w, s["ram_w"] / ram_w
            part = (sums[0] * cpu, sums[1] * ram, sums[2] * ram, sums[3] * cpu, sums[4] * cpu)
            prev = shares.get(s["cid"])
            shares[s["cid"]] = tuple(a + b for a, b in zip(prev, part)) if prev else part
    return shares


def build_process_resources(trees: dict | None = None, proc_root: str = "/proc", live_workers=None) -> dict:
    """Attach `resources` / `rollup_resources` to agent-tree nodes and return lookup tables.

    A conversation group is every descendant of a process carrying that
    conversation ID, so CLI workers and background jobs a main agent launched
    are inside its rollup without title or cwd guessing. The Antigravity runtime
    hosting the conversations (language_server + window tree) is split across
    live AGY sessions as negative pseudo-PIDs (`self_resources` on each node).
    """
    snap = process_snapshot(proc_root)
    procs = snap["procs"]
    conv_roots: dict[str, list[int]] = {}
    task_roots: dict[str, list[int]] = {}
    for pid, p in procs.items():
        if not p["cid"]:
            continue
        parent = procs.get(p["ppid"]) or {}
        if parent.get("cid") != p["cid"]:
            conv_roots.setdefault(p["cid"], []).append(pid)
        if p["task"] and (parent.get("cid"), parent.get("task")) != (p["cid"], p["task"]):
            task_roots.setdefault(f"{p['cid']}/{p['task']}", []).append(pid)
    conv_pids = {cid: _descendants(snap, roots) for cid, roots in conv_roots.items()}

    if live_workers is None:
        try:
            from live_worker_processes import scan_live_worker_processes
            live_workers = scan_live_worker_processes(proc_root)
        except Exception:
            live_workers = []
    records = {r.get("pid"): r for r in live_workers if isinstance(r, dict) and r.get("pid") in procs}
    workers: dict[str, dict] = {}
    by_pid: dict[str, dict] = {}
    for pid, record in records.items():
        # A maestro wrapper and the CLI it launched are one worker.
        parent = records.get(procs[pid]["ppid"])
        wrapped = parent and parent.get("provider") == "maestro" and record.get("provider") != "maestro"
        res = summarize_pids(snap, _descendants(snap, [parent["pid"] if wrapped else pid]))
        by_pid[str(pid)] = res
        for key in ("session_id", "registry_id"):
            if record.get(key):
                workers[record[key]] = res

    # Worker trees belong to their SUB rows only: a background task keeps just the
    # PIDs no worker owns, and records the worker PIDs it wraps so the widget can
    # drop a pure `maestro worker-start` wrapper instead of showing it twice.
    worker_pids = set().union(*(set(res["pids"]) for res in by_pid.values()))
    tasks = {}
    for key, roots in task_roots.items():
        pids = _descendants(snap, roots)
        tasks[key] = summarize_pids(snap, pids - worker_pids)
        if pids & worker_pids:
            tasks[key]["worker_pids"] = sorted(pids & worker_pids)

    trees = trees or {}
    sessions = _agy_runtime_sessions(trees)
    pools = _agy_runtime_pools(snap, worker_pids.union(*conv_pids.values())) if sessions else {}
    share_stats: dict[int, tuple] = {}
    share_pid: dict[str, int] = {}
    for index, (cid, stats) in enumerate(sorted(_split_agy_runtime(snap, pools, sessions).items()), 1):
        share_pid[cid] = -index
        share_stats[-index] = stats
        conv_pids.setdefault(cid, set()).add(-index)

    def summarize(pids):
        pids = [pid for pid in set(pids) if pid in procs or pid in share_stats]
        rows = [share_stats[pid] if pid < 0 else _pid_stats(snap, pid) for pid in pids]
        return _resource_summary(rows, pids, snap["totals"])

    conversations = {cid: summarize(pids) for cid, pids in conv_pids.items()}

    def agy_cids(node, seen):
        cid = node.get("conversation_id")
        if cid and cid not in seen:
            seen.add(cid)
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    agy_cids(child, seen)
        return seen

    def annotate_agy(node):
        cid = node.get("conversation_id") or ""
        _set_resources(node, "resources", conversations.get(cid))
        _set_resources(node, "self_resources", summarize([share_pid[cid]]) if cid in share_pid else None)
        for task in node.get("background_tasks") or []:
            if isinstance(task, dict):
                _set_resources(task, "resources", tasks.get(f"{cid}/{task.get('short_id')}"))
        for child in node.get("children") or []:
            if isinstance(child, dict):
                annotate_agy(child)

    def rollup(pids):
        summary = summarize(pids)
        summary["active_procs"] = summary["proc_count"]
        return summary

    for node in trees.get("antigravity") or []:
        if isinstance(node, dict) and "error" not in node:
            annotate_agy(node)
            pids = set().union(*(conv_pids.get(cid, set()) for cid in agy_cids(node, set())))
            _set_resources(node, "rollup_resources", rollup(pids) if pids else None)
    def own_share(res, own_id):
        """A session tree minus the trees of worker CLIs running inside it (their SUB rows)."""
        if not res:
            return None
        pids = set(res["pids"])
        nested = set()
        for pid in sorted(pids):
            record = records.get(pid)
            if (record and record.get("session_id") not in ("", None, own_id) and str(pid) in by_pid
                    and pid not in nested
                    and (record.get("parent_session_id") or record.get("parent_conversation_id"))):
                nested |= set(by_pid[str(pid)]["pids"]) & pids
        return summarize_pids(snap, pids - nested) if nested else res

    for node in trees.get("claude") or []:
        if not isinstance(node, dict):
            continue
        pid = node.get("pid")
        res = None
        if isinstance(pid, int) and not isinstance(pid, bool) and pid in procs and node.get("is_running"):
            res = by_pid.get(str(pid)) or summarize_pids(snap, _descendants(snap, [pid]))
        res = res or workers.get(node.get("session_id") or "")
        _set_resources(node, "resources", res)
        _set_resources(node, "self_resources", own_share(res, node.get("session_id")))
        # A Claude session's process tree already contains every worker it spawned.
        _set_resources(node, "rollup_resources", rollup(res["pids"]) if res else None)

    def annotate_codex(node):
        res = workers.get(node.get("thread_id") or "")
        _set_resources(node, "resources", res)
        _set_resources(node, "self_resources", own_share(res, node.get("thread_id")))
        pids = set((res or {}).get("pids") or ())
        for child in node.get("children") or []:
            if isinstance(child, dict):
                pids |= annotate_codex(child)
        return pids

    for node in trees.get("codex") or []:
        if isinstance(node, dict) and "error" not in node:
            pids = annotate_codex(node)
            _set_resources(node, "rollup_resources", rollup(pids) if pids else None)

    # Orca teammates run in their own panes, outside the lead's process tree.
    by_id = {node.get("session_id") or node.get("thread_id"): node
             for kind in ("claude", "codex") for node in trees.get(kind) or []
             if isinstance(node, dict) and "error" not in node}
    for node in list(by_id.values()):
        lead = by_id.get(node.get("orca_lead_id") or "")
        own = node.get("rollup_resources") or node.get("resources")
        if lead is not None and lead is not node and own:
            pids = set((lead.get("rollup_resources") or lead.get("resources") or {}).get("pids") or ())
            _set_resources(lead, "rollup_resources", rollup(pids | set(own["pids"])))

    attributed = set()
    for group in (conversations, tasks, workers, by_pid):
        for res in group.values():
            attributed.update(pid for pid in res["pids"] if pid > 0)
    runtime_pids = set().union(*pools.values()) if share_pid else set()
    return {
        "sampled_at": snap["sampled_at"],
        "ttl_sec": PROC_SNAPSHOT_TTL_SEC,
        "gpu_available": bool(snap["gpu"]) or bool(shutil.which("nvidia-smi")),
        **snap["totals"],
        # pid -> [per-core cpu %, ram_mb, gpu_mb, tx_kbps, rx_kbps, comm]; only attributed PIDs.
        # Raw per-core values keep rows additive; merge_resources normalizes the sum.
        # Negative keys are AGY runtime shares (language_server + window tree split per session).
        "procs": {**{str(pid): [round(v, 2) for v in _pid_stats(snap, pid)] + [procs[pid]["comm"]]
                     for pid in sorted(attributed)},
                  **{str(pid): [round(v, 2) for v in stats] + ["agy-runtime"]
                     for pid, stats in sorted(share_stats.items(), reverse=True)}},
        "agy_runtime": {**summarize_pids(snap, runtime_pids), "sessions": len(share_pid),
                        "shares": share_pid} if share_pid else None,
        "conversations": conversations,
        "tasks": tasks,
        "workers": workers,
        "pids": by_pid,
    }


def collect_usage(include_agent_trees=True) -> dict:
    """Collect one quota snapshot, optionally with the more expensive agent trees."""
    report = {
        "timestamp_kst": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "antigravity": get_antigravity_usage(),
        "codex_local": get_codex_usage(),
        "claude_local": get_claude_status(),
    }
    report["recommended_routing"] = select_best_pool(report)
    # Keep the recommendation near the top so --pretty | head shows the result.
    report = {"timestamp_kst": report.pop("timestamp_kst"),
              "recommended_routing": report.pop("recommended_routing"), **report}
    if include_agent_trees:
        report["agent_trees"] = {
            "antigravity": get_antigravity_agent_tree(),
            "codex": get_codex_agent_tree(),
            "claude": get_claude_agent_tree(),
        }
        try:
            link_orca_teammates(report["agent_trees"], cache_path=ORCA_LEAD_MAP_FILE)
        except Exception:
            pass
        try:
            report["process_resources"] = build_process_resources(report["agent_trees"])
        except Exception as exc:  # telemetry must never break quota reporting
            report["process_resources"] = {"error": type(exc).__name__}
        report["orca"] = get_orca_summary()
    return report


ORCA_SUMMARY_TTL_SEC = 10.0
_ORCA_SUMMARY_CACHE: dict = {}


def get_orca_summary() -> dict:
    """Live Orca terminals/worktrees (memoized 10s, snapshot file ~20s); never raises."""
    now = time.monotonic()
    if _ORCA_SUMMARY_CACHE and now - _ORCA_SUMMARY_CACHE["at"] < ORCA_SUMMARY_TTL_SEC:
        return copy.deepcopy(_ORCA_SUMMARY_CACHE["value"])
    value = _build_orca_summary()
    _ORCA_SUMMARY_CACHE.update(at=now, value=value)
    return copy.deepcopy(value)


def _build_orca_summary() -> dict:
    try:
        import orca_bridge
        snap = orca_bridge.snapshot()
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": snap.get("available", False),
        "running": snap.get("running", False),
        "summary": orca_bridge.summary_line(snap),
        "worktrees": snap.get("worktrees", 0),
        "terminals": [{k: t.get(k) for k in ("handle", "title", "agent", "worktree", "branch", "connected")}
                      for t in snap.get("terminals", [])],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="Print indented JSON (also the default for compatibility)")
    parser.parse_args()
    report = collect_usage()
    try:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    except BrokenPipeError:
        # Consumers such as head may close stdout before the full tree is printed.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)


if __name__ == "__main__":
    main()
