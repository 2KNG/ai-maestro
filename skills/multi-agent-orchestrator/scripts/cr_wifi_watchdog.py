#!/usr/bin/env python3
"""
cr_wifi_watchdog — Monitors CR_WIFI / internet connectivity every 30s.
If CR_WIFI is connected without internet (or internet is unreachable) for >= 5 minutes (300s):
1. Switches Wi-Fi from CR_WIFI back to STARPORT (~/.local/bin/cr-wifi off).
2. Resumes the 3D Camera / Robot Antigravity agent (4500bbbc-e155-49f9-b324-88364ca9b84b)
   via LanguageServer SendUserCascadeMessage so its work continues without stalling.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

STATE_FILE = "/tmp/cr_wifi_watchdog_state.json"
LOG_FILE = os.path.expanduser("~/.gemini/antigravity/cr_wifi_watchdog.log")
CR_WIFI_BIN = os.path.expanduser("~/.local/bin/cr-wifi")
AI_USAGE_REMOTE_BIN = os.path.expanduser("~/.local/bin/ai-usage-remote")
TARGET_CASCADE_ID = "4500bbbc-e155-49f9-b324-88364ca9b84b"
OFFLINE_LIMIT_SEC = 300  # 5 minutes


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def has_internet(targets=("1.1.1.1", "8.8.8.8"), timeout: int = 4) -> bool:
    for target in targets:
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "2", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def active_wifi_name(timeout: int = 5) -> str:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "connection", "show", "--active"],
            text=True,
            timeout=timeout,
        )
        for ln in out.splitlines():
            parts = ln.split(":")
            if len(parts) >= 3 and "wireless" in parts[2]:
                return parts[0]
    except Exception:
        pass
    return ""


def resume_3d_agent(reason: str = "인터넷 복구 알림") -> bool:
    """
    Discovers Antigravity LanguageServer credentials and sends SendUserCascadeMessage
    to the 3D Agent (4500bbbc-e155-49f9-b324-88364ca9b84b) to resume stalled work.
    Falls back to widget_bridge_daemon /api/agent-control if direct LS call fails.
    """
    try:
        import check_all_usage as cau
        import maestro

        addr, token = cau.discover_ls_credentials()
        if not addr or not token:
            log("LanguageServer credentials not found; checking bridge daemon fallback.")
            raise RuntimeError("discover_ls_credentials returned no credentials")

        # Check agent status
        resp = maestro.ls_post(addr, token, "GetAllCascadeTrajectories", {}, timeout=5)
        summaries = resp.get("trajectorySummaries", {})
        info = summaries.get(TARGET_CASCADE_ID, {})
        status = info.get("status", "")
        if "RUNNING" in status:
            log(f"3D agent {TARGET_CASCADE_ID[:8]} is already RUNNING ({status}); no need to resume.")
            return True

        # Extract latest planModel if possible
        plan_model = "MODEL_PLACEHOLDER_M417"
        try:
            gm = maestro.ls_post(
                addr,
                token,
                "GetCascadeTrajectoryGeneratorMetadata",
                {"cascadeId": TARGET_CASCADE_ID},
                timeout=4,
            )
            items = gm.get("generatorMetadata", [])
            if items and items[-1].get("planModel"):
                plan_model = items[-1]["planModel"]
        except Exception:
            pass

        prompt = (
            f"[자동 복구 알림: {reason}] CR_WIFI 전환 후 인터넷 단절이 5분 이상 지속되어 "
            f"자동으로 STARPORT(외부 인터넷)로 복구 완료했습니다. "
            f"진행 중이던 3D 카메라/로봇 작업을 이어서 계속 수행해 주세요."
        )
        log(f"Resuming 3D agent {TARGET_CASCADE_ID[:8]} ({status}, model={plan_model})...")
        payload = {
            "cascadeId": TARGET_CASCADE_ID,
            "items": [{"text": prompt}],
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
        res = maestro.ls_post(addr, token, "SendUserCascadeMessage", payload, timeout=20)
        log(f"Successfully sent resume command to 3D agent {TARGET_CASCADE_ID[:8]}: {res.get('status', 'OK')}")
        return True
    except Exception as e:
        log(f"Direct LanguageServer resume attempt failed: {e}")

    # Fallback: try widget_bridge_daemon endpoint /api/agent-control (port 18765)
    try:
        import urllib.request
        log("Trying fallback to widget_bridge_daemon http://127.0.0.1:18765/api/agent-control...")
        body = json.dumps({
            "action": "resume",
            "cascadeId": TARGET_CASCADE_ID,
            "reason": reason,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:18765/api/agent-control",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ret = json.loads(r.read().decode("utf-8"))
            if ret.get("ok"):
                log(f"Successfully resumed 3D agent via /api/agent-control: {ret}")
                return True
            else:
                log(f"Bridge fallback returned non-ok: {ret}")
    except Exception as e2:
        log(f"Bridge fallback also failed: {e2}")

    return False


def check_once(dry_run: bool = False) -> None:
    now = time.time()
    wifi = active_wifi_name()
    online = has_internet()
    state = {"offline_since": None, "cr_wifi_since": None, "last_recover_ts": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
        except Exception:
            pass

    if wifi == "CR_WIFI":
        if not state.get("cr_wifi_since"):
            state["cr_wifi_since"] = now
            log("Detected Wi-Fi switched to CR_WIFI.")
    else:
        state["cr_wifi_since"] = None

    if not online:
        if not state.get("offline_since"):
            state["offline_since"] = now
            log(f"Internet unreachable (wifi={wifi or 'none'}). Starting 5-min timer...")
        offline_dur = now - float(state["offline_since"])
        if offline_dur >= OFFLINE_LIMIT_SEC:
            log(f"Internet offline for {int(offline_dur)}s (>= 300s) on wifi={wifi}. Switching to STARPORT!")
            if not dry_run:
                subprocess.run([CR_WIFI_BIN, "off"], check=False)
                time.sleep(4)
                if has_internet():
                    log("STARPORT reconnected and internet restored!")
                    state["offline_since"] = None
                    state["cr_wifi_since"] = None
                    state["last_recover_ts"] = now
                    resume_3d_agent(f"CR_WIFI 오프라인 {int(offline_dur)}초 경과 후 STARPORT 복구")
                else:
                    log("Failed to restore internet after switching to STARPORT.")
            else:
                log("[DRY-RUN] Would have switched to STARPORT and resumed 3D agent.")
    else:
        state["offline_since"] = None

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        subprocess.run(
            [AI_USAGE_REMOTE_BIN],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="CR_WIFI Watchdog & 3D Agent Auto-Resume")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--resume", action="store_true", help="Manually trigger 3D agent resume")
    parser.add_argument("--dry-run", action="store_true", help="Do not execute connection switches")
    args = parser.parse_args()

    if args.resume:
        ok = resume_3d_agent("수동 테스트/재개 명령")
        sys.exit(0 if ok else 1)

    if args.once:
        check_once(dry_run=args.dry_run)
        return

    log("Started cr-wifi-watchdog daemon (5-min offline -> STARPORT + 3D agent auto-resume).")
    while True:
        try:
            check_once(dry_run=args.dry_run)
        except Exception as e:
            log(f"Watchdog loop error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
