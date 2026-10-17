#!/usr/bin/env python3
"""
render_remote_widget.py — Remote-Viewable Artifact Generator (Does NOT touch local usage_widget.html)
Generates:
1. remote_usage_widget.html: Uncompressed, self-contained HTML artifact (no document.write/gzip sandbox issues,
   auto-tries LAN 172.20.1.252:18765 -> 127.0.0.1:18765 -> Static Remote Mode, with PC MON pre-rendered).
2. remote_dashboard.md: Pure Markdown live dashboard artifact for instant remote viewing in any IDE/mobile client.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_all_usage as cau
import pc_telemetry_ext as pct
import render_usage_widget as ruw

DEFAULT_BRAIN_DIR = "/home/u24c9g5090r64/.gemini/antigravity/brain/521b781e-43da-4647-b411-585e65e5f002"
WATCHDOG_LOG = "/home/u24c9g5090r64/.gemini/antigravity/cr_wifi_watchdog.log"


def _get_lan_ip() -> str:
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"], text=True, timeout=3)
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 4 and parts[1] != "lo":
                ip = parts[3].split("/")[0]
                if ip.startswith("172.20.") or ip.startswith("192.168.") or ip.startswith("10."):
                    return ip
    except Exception:
        pass
    return "172.20.1.252"


def build_remote_html(data: dict, lan_ip: str, target_path: str) -> str:
    template, _ = ruw.pick_template(target_path, SCRIPT_DIR)
    orig_load_state = ruw._load_persisted_widget_state

    def _remote_state():
        st = dict(orig_load_state() or {})
        st["pcMonOpen"] = True
        return st

    ruw._load_persisted_widget_state = _remote_state
    try:
        raw_html = ruw.inject_live_data(template, data) if template else ruw.build_widget_html(data)
    finally:
        ruw._load_persisted_widget_state = orig_load_state

    # HTTP origins (incl. localhost/127.0.0.1 served by the bridge) talk to their own origin;
    # file:// / vscode-webview fall back to the LAN bridge (fetch failures are swallowed silently).
    bridge_re = re.compile(r"const BRIDGE_URL = \(location\.protocol\.startsWith\('http'\) && location\.host\)\s*\? location\.origin : '[^']*';")
    new_bridge = (
        "const BRIDGE_URL = (location.protocol.startsWith('http') && location.host)\n"
        f"      ? location.origin : 'http://{lan_ip}:18765';"
    )
    raw_html = bridge_re.sub(lambda _m: new_bridge, raw_html, count=1)

    # Mark the bridge live only after a successful /api/status response.
    raw_html = raw_html.replace(
        "        if (j && j.ok) {\n",
        "        if (j && j.ok) {\n          window.__BRIDGE_LIVE_OK = true;\n",
        1,
    )

    # Offline/static viewers keep completed subagent nodes instead of evaporating them.
    start = raw_html.find("function tickSubRipening()")
    if start != -1:
        end = raw_html.find("\n    }\n", start)
        end = len(raw_html) if end == -1 else end
        body = raw_html[start:end].replace("node.remove();", "if (window.__BRIDGE_LIVE_OK) node.remove();", 1)
        body = body.replace("if (age >= 60) {", "if (age >= 60 && window.__BRIDGE_LIVE_OK) {", 1)
        raw_html = raw_html[:start] + body + raw_html[end:]
    return raw_html


def build_remote_markdown(data: dict, lan_ip: str) -> str:
    now_kst = datetime.now().strftime("%Y-%m-%d %H:%M:%S KST")
    accounts = ruw.build_accounts(data)
    pc = pct.collect_pc_telemetry() or {}

    q_rows = []
    for acc in accounts:
        name = acc.get("name") or acc.get("short") or "Pool"
        wins = acc.get("windows") or []
        w_parts = []
        for w in wins:
            lbl = (w.get("period") or w.get("label") or "").replace("|", "·")
            rem = w.get("remaining")
            rst = w.get("resetText") or w.get("reset") or "-"
            w_parts.append(f"**{lbl}: {rem}%** ({rst})")
        q_rows.append(f"| **{name}** | {' · '.join(w_parts) if w_parts else '-'} |")

    cpu = pc.get("cpu") or {}
    gpu = (pc.get("gpus") or [{}])[0] if pc.get("gpus") else (pc.get("gpu") or {})
    ram = pc.get("ram") or {}
    disk = pc.get("storage") or {}
    net = pc.get("network") or {}
    nd = (pc.get("network") or {}).get("diagnosis") or pc.get("network_diagnostics") or {}

    cpu_s = f"**{cpu.get('percent', 0):.0f}%** ({cpu.get('temp_c', 0):.0f}°C)"
    vram_used = gpu.get("vram_used_gb", gpu.get("mem_used_gb", 0)) or 0
    vram_total = gpu.get("vram_total_gb", gpu.get("mem_total_gb", 32)) or 0
    free_gb = disk.get("disk_free_gb", disk.get("free_gb", 0)) or 0
    total_gb = disk.get("disk_total_gb", disk.get("total_gb", 0)) or 0
    ws_lbl = disk.get("workspace_label") or f"{disk.get('dev_used_gb', 0):.1f} GB"

    gpu_s = f"**{gpu.get('util_pct', gpu.get('percent', 0)):.0f}%** · VRAM `{vram_used:.1f}/{vram_total:.0f} GB` ({gpu.get('temp_c', 0):.0f}°C · {gpu.get('power_w', 0):.0f}W)"
    ram_s = f"**{ram.get('used_gb', 0):.1f} / {ram.get('total_gb', 0):.1f} GB** ({ram.get('percent', 0):.0f}%)"
    disk_s = f"여유 **{free_gb:.0f} GB** / {total_gb:.0f} GB (`~/dev` {ws_lbl})"
    rx_kb = float(net.get("rx_bytes_per_sec", 0) or 0) / 1024.0
    tx_kb = float(net.get("tx_bytes_per_sec", 0) or 0) / 1024.0
    net_s = f"`{net.get('iface', 'wlp134s0f0')}` (`{lan_ip}`) · ↓ `{rx_kb:.1f} KB/s` · ↑ `{tx_kb:.1f} KB/s` · 상태: **{nd.get('summary', '정상')}**"

    wd_active = "inactive"
    try:
        wd_active = subprocess.check_output(
            ["systemctl", "--user", "is-active", "cr-wifi-watchdog.service"], text=True, timeout=3
        ).strip()
    except Exception:
        pass

    wd_lines = []
    if os.path.exists(WATCHDOG_LOG):
        try:
            with open(WATCHDOG_LOG, "r", encoding="utf-8") as f:
                wd_lines = [ln.strip() for ln in f.readlines()[-5:] if ln.strip()]
        except Exception:
            pass

    ag_sessions = ((data.get("agent_trees") or {}).get("antigravity") or [])
    cdx_sessions = ((data.get("agent_trees") or {}).get("codex") or [])

    claude_sessions = ((data.get("claude_local") or {}).get("sessions") or [])

    # (is_running, row) tuples so RUNNING sessions sort first across all pools.
    sess_items = []
    for ag in ag_sessions[:12]:
        cid = (ag.get("conversation_id") or "")[:8]
        title = (ag.get("title") or "Session").replace("|", "·")[:42]
        model = ag.get("model") or "-"
        is_run = "RUNNING" in (ag.get("status") or "")
        st = "🟢 RUNNING" if is_run else "⚪ IDLE"
        if cid == "4500bbbc":
            title = f"🤖 **[3D/로봇]** {title}"
        dur_lbl, _, _ = ruw._fmt_row_work_time(ag, is_run)
        ctx_pct = ((ag.get("context_usage") or {}).get("percent") or 0.0)
        sess_items.append((is_run, f"| `{cid}` | {title} | {model} | {st} | `{dur_lbl}` | {ctx_pct:.1f}% |"))

    for cdx in cdx_sessions[:8]:
        tid = (cdx.get("thread_id") or "")[:8]
        title = (cdx.get("display_name") or cdx.get("title") or "Codex").replace("|", "·")[:42]
        tctx = cdx.get("turn_context") or {}
        model = f"{tctx.get('model', 'gpt-6.1-sol')} ({tctx.get('service_tier', 'priority')})"
        is_run = bool(cdx.get("is_running") and not cdx.get("turn_completed"))
        st = "🟢 RUNNING" if is_run else "⚪ IDLE"
        dur_lbl, _, _ = ruw._fmt_row_work_time(cdx, is_run)
        ctx_pct = ((cdx.get("token_count") or {}).get("percent") or 0.0)
        sess_items.append((is_run, f"| `{tid}` | {title} | {model} | {st} | `{dur_lbl}` | {ctx_pct:.1f}% |"))

    for cl in claude_sessions[:8]:
        sid = (cl.get("session_id") or cl.get("id") or "")[:8]
        title = (cl.get("title") or cl.get("summary") or cl.get("display_name") or "Claude Code").replace("|", "·")[:42]
        model = cl.get("model") or "Claude Code"
        is_run = bool(cl.get("is_running")) or "RUNNING" in str(cl.get("status") or "").upper()
        st = "🟢 RUNNING" if is_run else "⚪ IDLE"
        dur_lbl, _, _ = ruw._fmt_row_work_time(cl, is_run)
        ctx_pct = ((cl.get("context_usage") or cl.get("token_count") or {}).get("percent") or 0.0)
        sess_items.append((is_run, f"| `{sid}` | {title} | {model} | {st} | `{dur_lbl}` | {ctx_pct:.1f}% |"))

    sess_items.sort(key=lambda it: not it[0])
    sess_rows = [row for _, row in sess_items]

    md = [
        "# 🌐 AI Maestro 원격 실시간 대시보드 (Remote Status Artifact)",
        f"> **최종 갱신**: `{now_kst}` · **워치독 데몬**: `{wd_active.upper()}` · **사내망 웹 접속**: `http://{lan_ip}:18765`",
        "",
        "## 🛡️ 1. `CR_WIFI` ↔ `STARPORT` 자동 복구 & 3D 에이전트(`4500bbbc`) 감시 상태",
        "- **감시 규칙**: `CR_WIFI` 전환 후 **5분(300초) 이상 인터넷 단절 시** 자동으로 `STARPORT`(`172.20.1.252`) 복구 및 3D 에이전트(`4500bbbc-e155-49f9-b324-88364ca9b84b`) 자동 재개",
        "- **최근 워치독 로그**:",
        "```text",
        *(wd_lines if wd_lines else ["[대기 중] 현재 STARPORT 인터넷 연결 정상 유지 중 (오프라인 이벤트 없음)"]),
        "```",
        "",
        "## 💻 2. PC 하드웨어 & 네트워크 텔레메트리 (`u24c9g5090r64`)",
        "| 항목 | 실시간 상태 |",
        "| :--- | :--- |",
        f"| **CPU** | {cpu_s} |",
        f"| **GPU (RTX 5090)** | {gpu_s} |",
        f"| **RAM** | {ram_s} |",
        f"| **Storage (`~/dev`)** | {disk_s} |",
        f"| **Network** | {net_s} |",
        "",
        "## ⚡ 3. 활성 에이전트 세션 & 작업 소요 시간",
        "| ID | 세션 / 작업명 | 모델 | 상태 | 작업 시간 | 컨텍스트 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        *(sess_rows if sess_rows else ["| - | 활성 세션 없음 | - | - | - | - |"]),
        "",
        "## 📊 4. 멀티 풀 5h / 7d 잔여 쿼터",
        "| 에이전트 풀 | 5h / 7d 잔여 및 리셋 |",
        "| :--- | :--- |",
        *(q_rows if q_rows else ["| - | - |"]),
        "",
    ]
    return "\n".join(md)


def _write_artifact_meta(path: str, summary: str) -> None:
    meta_path = path + ".metadata.json"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(
            {
                "summary": summary,
                "updatedAt": now_iso,
                "userFacing": True,
            },
            mf,
            ensure_ascii=False,
            indent=2,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate remote-viewable HTML & Markdown artifacts without touching local usage_widget.html")
    ap.add_argument("--brain-dir", default=DEFAULT_BRAIN_DIR)
    args = ap.parse_args()

    os.makedirs(args.brain_dir, exist_ok=True)
    data = cau.collect_usage(include_agent_trees=True)
    lan_ip = _get_lan_ip()

    remote_html_path = os.path.join(args.brain_dir, "remote_usage_widget.html")
    remote_md_path = os.path.join(args.brain_dir, "remote_dashboard.md")

    html_content = build_remote_html(data, lan_ip, remote_html_path)
    with open(remote_html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    _write_artifact_meta(
        remote_html_path,
        "원격 열람 전용 독립형 HTML 위젯 (로컬 127.0.0.1 의존 없이 SSR 데이터 및 PC MON 기본 펼침 내장)",
    )

    md_content = build_remote_markdown(data, lan_ip)
    with open(remote_md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    _write_artifact_meta(
        remote_md_path,
        "원격/모바일 아티팩트 뷰어용 실시간 마크다운 대시보드 (CR_WIFI 워치독, 3D 에이전트, PC 하드웨어, 5h/7d 쿼터)",
    )

    print(f"Generated remote artifacts:\n  - {remote_html_path} ({len(html_content)} bytes)\n  - {remote_md_path} ({len(md_content)} bytes)")


if __name__ == "__main__":
    main()
