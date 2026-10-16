#!/usr/bin/env python3
"""
Optional Plug-in Extension: PC Hardware & Workspace Telemetry Monitor (pc_telemetry_ext.py)
-------------------------------------------------------------------------------------------
Designed as a drop-in optional module ("있어도 되고 없어도 되는 확장판"):
- If this file is present alongside render_usage_widget.py and widget_bridge_daemon.py,
  the AOH widget automatically displays a sleek, low-glare Mobbin/Beszel/Raycast-inspired
  Hardware Telemetry HUD Bar + Expandable 5-Card Bento Drawer (CPU, GPU/VRAM, RAM,
  Workspace Storage, Network I/O), and serves a standalone dashboard at http://127.0.0.1:18765/pc.
- If this file is removed or disabled via AGY_DISABLE_PC_MON=1, the main orchestrator and
  widget continue operating normally with zero errors.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any

_lock = threading.Lock()
_prev_cpu_times: tuple[int, int] | None = None
_prev_net_bytes: tuple[float, int, int] | None = None
_cached_telemetry: dict[str, Any] | None = None
_cached_telemetry_ts: float = 0.0
_cached_workspace_info: dict[str, Any] | None = None
_cached_workspace_ts: float = 0.0
_cached_cpu_model: str | None = None

TELEMETRY_TTL_SEC = 2.5
WORKSPACE_TTL_SEC = 60.0


def is_enabled() -> bool:
    return os.environ.get("AGY_DISABLE_PC_MON", "0").strip() not in ("1", "true", "yes")


def _get_cpu_model() -> str:
    global _cached_cpu_model
    if _cached_cpu_model:
        return _cached_cpu_model
    model = "CPU"
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    raw = line.split(":", 1)[1].strip()
                    raw = re.sub(r"\(R\)|\(TM\)|Processor|CPU|@\s*[\d.]+GHz", "", raw, flags=re.I)
                    model = re.sub(r"\s+", " ", raw).strip()
                    break
    except Exception:
        pass
    _cached_cpu_model = model
    return model


def _read_cpu_jiffies() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline()
        parts = [int(x) for x in first.split()[1:]]
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total
    except Exception:
        return None


def _get_cpu_temp_c() -> float | None:
    best: float | None = None
    try:
        # Prefer x86_pkg_temp or k10temp / coretemp
        for root in ("/sys/class/thermal",):
            if not os.path.isdir(root):
                continue
            for name in os.listdir(root):
                if not name.startswith("thermal_zone"):
                    continue
                t_path = os.path.join(root, name, "temp")
                type_path = os.path.join(root, name, "type")
                try:
                    with open(t_path, "r", encoding="utf-8") as tf:
                        val = int(tf.read().strip()) / 1000.0
                    ztype = ""
                    if os.path.exists(type_path):
                        with open(type_path, "r", encoding="utf-8") as yf:
                            ztype = yf.read().strip().lower()
                    if 15.0 <= val <= 115.0:
                        if "pkg" in ztype or "core" in ztype or "tctl" in ztype:
                            return round(val, 1)
                        if best is None or val > best:
                            best = round(val, 1)
                except Exception:
                    continue
    except Exception:
        pass
    return best


def _collect_cpu() -> dict[str, Any]:
    global _prev_cpu_times
    cores = os.cpu_count() or 1
    load1, load5, load15 = 0.0, 0.0, 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        pass

    j1 = _read_cpu_jiffies()
    pct = None
    if j1 is not None:
        if _prev_cpu_times is not None:
            idle_d = j1[0] - _prev_cpu_times[0]
            total_d = j1[1] - _prev_cpu_times[1]
            if total_d > 0:
                pct = max(0.0, min(100.0, (1.0 - (idle_d / total_d)) * 100.0))
        _prev_cpu_times = j1

    if pct is None:
        # Quick 60ms sample on first run
        time.sleep(0.06)
        j2 = _read_cpu_jiffies()
        if j1 and j2 and (j2[1] - j1[1]) > 0:
            pct = max(0.0, min(100.0, (1.0 - ((j2[0] - j1[0]) / (j2[1] - j1[1]))) * 100.0))
            _prev_cpu_times = j2
        else:
            pct = min(100.0, (load1 / max(1, cores)) * 100.0)

    return {
        "model": _get_cpu_model(),
        "cores": cores,
        "percent": round(pct, 1),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "temp_c": _get_cpu_temp_c(),
    }


def _collect_gpu() -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {"available": False}
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ],
            timeout=1.5,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").strip()
        if not out:
            return {"available": False}
        first_line = out.splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        if len(parts) < 8:
            return {"available": False}
        name = parts[0].replace("NVIDIA GeForce ", "").replace("NVIDIA ", "").strip()
        gpu_util = float(parts[1] or 0)
        mem_ctrl_util = float(parts[2] or 0)
        vram_used_mib = float(parts[3] or 0)
        vram_total_mib = max(1.0, float(parts[4] or 1))
        temp_c = float(parts[5] or 0)
        power_w = float(parts[6] or 0) if parts[6] not in ("[N/A]", "N/A", "") else 0.0
        power_limit_w = float(parts[7] or 0) if parts[7] not in ("[N/A]", "N/A", "") else 0.0
        vram_used_gb = vram_used_mib / 1024.0
        vram_total_gb = vram_total_mib / 1024.0
        vram_pct = (vram_used_mib / vram_total_mib) * 100.0
        return {
            "available": True,
            "name": name,
            "percent": round(gpu_util, 1),
            "mem_ctrl_percent": round(mem_ctrl_util, 1),
            "vram_used_gb": round(vram_used_gb, 1),
            "vram_total_gb": round(vram_total_gb, 1),
            "vram_percent": round(vram_pct, 1),
            "temp_c": round(temp_c, 1),
            "power_w": round(power_w, 0),
            "power_limit_w": round(power_limit_w, 0),
        }
    except Exception:
        return {"available": False}


def _collect_ram() -> dict[str, Any]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                num = re.search(r"\d+", v)
                if num:
                    info[k.strip()] = int(num.group(0)) * 1024  # bytes
    except Exception:
        pass
    total = max(1, info.get("MemTotal", 1))
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    cached = info.get("Cached", 0) + info.get("Buffers", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "used_gb": round(used / (1024 ** 3), 1),
        "total_gb": round(total / (1024 ** 3), 1),
        "avail_gb": round(avail / (1024 ** 3), 1),
        "cached_gb": round(cached / (1024 ** 3), 1),
        "percent": round((used / total) * 100.0, 1),
        "swap_used_gb": round(swap_used / (1024 ** 3), 1),
        "swap_total_gb": round(swap_total / (1024 ** 3), 1),
    }


_ws_scan_running = False


def _bg_scan_workspace(dev_dir: str) -> None:
    global _cached_workspace_info, _cached_workspace_ts, _ws_scan_running
    try:
        proc = subprocess.run(
            ["du", "-s", "--apparent-size", "-B1", "--max-depth=1", dev_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
            text=True,
        )
        out = proc.stdout or ""
        ws_total_bytes = 0
        items: list[tuple[int, str]] = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                sz = int(parts[0])
            except ValueError:
                continue
            p = parts[1].rstrip("/")
            if os.path.abspath(p) == os.path.abspath(dev_dir):
                ws_total_bytes = sz
            else:
                bname = os.path.basename(p)
                if not bname.startswith("."):
                    items.append((sz, bname))
        items.sort(reverse=True)
        if ws_total_bytes == 0 and items:
            ws_total_bytes = sum(sz for sz, _ in items)
        top_dirs = [
            {
                "name": bname,
                "size_gb": round(sz / (1024 ** 3), 2),
                "size_label": _fmt_bytes_short(sz),
            }
            for sz, bname in items[:4]
        ]
        if ws_total_bytes > 0 or top_dirs:
            _cached_workspace_info = {
                "workspace_path": "~/dev",
                "workspace_bytes": ws_total_bytes,
                "workspace_label": _fmt_bytes_short(ws_total_bytes),
                "top_projects": top_dirs,
            }
            _cached_workspace_ts = time.time()
    except Exception:
        pass
    finally:
        _ws_scan_running = False


def _collect_workspace_storage() -> dict[str, Any]:
    global _cached_workspace_info, _cached_workspace_ts, _ws_scan_running
    now = time.time()
    dev_dir = os.path.expanduser("~/dev")
    disk = shutil.disk_usage(dev_dir if os.path.isdir(dev_dir) else "/")
    disk_used_gb = disk.used / (1024 ** 3)
    disk_total_gb = max(0.1, disk.total / (1024 ** 3))
    disk_free_gb = disk.free / (1024 ** 3)
    disk_pct = (disk.used / max(1, disk.total)) * 100.0

    if _cached_workspace_info is None:
        # First call: wait up to 1.2s for background scan so SSR has real numbers if fast, otherwise return immediately
        if not _ws_scan_running and os.path.isdir(dev_dir):
            _ws_scan_running = True
            t = threading.Thread(target=_bg_scan_workspace, args=(dev_dir,), daemon=True)
            t.start()
            t.join(timeout=1.2)
    elif (now - _cached_workspace_ts) >= WORKSPACE_TTL_SEC and not _ws_scan_running and os.path.isdir(dev_dir):
        _ws_scan_running = True
        threading.Thread(target=_bg_scan_workspace, args=(dev_dir,), daemon=True).start()

    ws = dict(_cached_workspace_info) if _cached_workspace_info else {
        "workspace_path": "~/dev",
        "workspace_bytes": disk.used,
        "workspace_label": f"{disk_used_gb:.0f}G",
        "top_projects": [],
    }

    return {
        "disk_used_gb": round(disk_used_gb, 1),
        "disk_total_gb": round(disk_total_gb, 1),
        "disk_free_gb": round(disk_free_gb, 1),
        "disk_percent": round(disk_pct, 1),
        **ws,
    }


def _read_net_counters() -> tuple[int, int, str]:
    rx_total = 0
    tx_total = 0
    active_ifaces: list[tuple[int, str]] = []
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            lines = f.readlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo" or iface.startswith(("docker", "veth", "br-", "virbr")):
                continue
            fields = rest.split()
            if len(fields) < 9:
                continue
            rx = int(fields[0])
            tx = int(fields[8])
            rx_total += rx
            tx_total += tx
            if rx + tx > 0:
                active_ifaces.append((rx + tx, iface))
    except Exception:
        pass
    active_ifaces.sort(reverse=True)
    primary_iface = active_ifaces[0][1] if active_ifaces else "eth0"
    return rx_total, tx_total, primary_iface


def _fmt_bytes_short(num_bytes: float) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.1f}G"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.0f}M"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}K"
    return f"{int(num_bytes)}B"


def _fmt_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / (1024 ** 2):.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{int(bytes_per_sec)} B/s"


def _collect_network() -> dict[str, Any]:
    global _prev_net_bytes
    now = time.time()
    rx1, tx1, iface = _read_net_counters()
    rx_rate = 0.0
    tx_rate = 0.0
    if _prev_net_bytes is not None:
        dt = now - _prev_net_bytes[0]
        if dt > 0.05:
            rx_rate = max(0.0, (rx1 - _prev_net_bytes[1]) / dt)
            tx_rate = max(0.0, (tx1 - _prev_net_bytes[2]) / dt)
    _prev_net_bytes = (now, rx1, tx1)
    return {
        "iface": iface,
        "rx_bytes_per_sec": round(rx_rate, 1),
        "tx_bytes_per_sec": round(tx_rate, 1),
        "rx_rate_label": _fmt_rate(rx_rate),
        "tx_rate_label": _fmt_rate(tx_rate),
        "rx_total_gb": round(rx1 / (1024 ** 3), 2),
        "tx_total_gb": round(tx1 / (1024 ** 3), 2),
    }


def collect_pc_telemetry(force: bool = False) -> dict[str, Any]:
    """Return cached or freshly sampled PC hardware & workspace telemetry (<8ms typical)."""
    global _cached_telemetry, _cached_telemetry_ts
    if not is_enabled():
        return {"enabled": False}
    now = time.time()
    with _lock:
        if not force and _cached_telemetry is not None and (now - _cached_telemetry_ts) <= TELEMETRY_TTL_SEC:
            return _cached_telemetry
        data = {
            "enabled": True,
            "hostname": socket.gethostname(),
            "timestamp": int(now),
            "cpu": _collect_cpu(),
            "gpu": _collect_gpu(),
            "ram": _collect_ram(),
            "storage": _collect_workspace_storage(),
            "network": _collect_network(),
        }
        _cached_telemetry = data
        _cached_telemetry_ts = time.time()
        return data


def _tone_class(pct: float, warn: float = 70.0, crit: float = 88.0) -> str:
    if pct >= crit:
        return "crit"
    if pct >= warn:
        return "warn"
    return "ok"


def render_pc_telemetry_html(pc: dict[str, Any] | None = None, open_drawer: bool = False) -> str:
    """Render the Mobbin/Beszel/Raycast-inspired collapsible Hardware HUD Strip + 5-Card Bento Drawer."""
    if not is_enabled():
        return ""
    if pc is None:
        pc = collect_pc_telemetry(force=False)
    if not pc or not pc.get("enabled"):
        return ""

    cpu = pc.get("cpu", {})
    gpu = pc.get("gpu", {})
    ram = pc.get("ram", {})
    st = pc.get("storage", {})
    net = pc.get("network", {})

    cpu_pct = float(cpu.get("percent", 0.0))
    cpu_temp = cpu.get("temp_c")
    cpu_temp_str = f"{int(cpu_temp)}°C" if cpu_temp is not None else f"{cpu.get('cores', 0)}C"
    cpu_tone = _tone_class(cpu_pct)

    gpu_avail = bool(gpu.get("available"))
    gpu_pct = float(gpu.get("percent", 0.0)) if gpu_avail else 0.0
    vram_pct = float(gpu.get("vram_percent", 0.0)) if gpu_avail else 0.0
    gpu_tone = _tone_class(max(gpu_pct, vram_pct))
    gpu_name = html.escape(str(gpu.get("name", "GPU")))

    ram_pct = float(ram.get("percent", 0.0))
    ram_tone = _tone_class(ram_pct)

    disk_pct = float(st.get("disk_percent", 0.0))
    disk_tone = _tone_class(disk_pct, warn=78.0, crit=92.0)
    ws_label = html.escape(str(st.get("workspace_label", "0B")))

    rx_label = html.escape(str(net.get("rx_rate_label", "0 B/s")))
    tx_label = html.escape(str(net.get("tx_rate_label", "0 B/s")))
    net_busy = (float(net.get("rx_bytes_per_sec", 0)) + float(net.get("tx_bytes_per_sec", 0))) > 50 * 1024

    top_proj_pills = "".join(
        f'<span class="pc-proj-pill"><b>{html.escape(p["name"])}</b> {html.escape(p["size_label"])}</span>'
        for p in st.get("top_projects", [])[:4]
    ) or '<span class="pc-proj-pill">~/dev 스캔 완료</span>'

    open_attr = " open" if open_drawer else ""

    gpu_pill_html = (
        f'<div class="pc-hud-item" title="{gpu_name} · 코어 {gpu_pct:.0f}% · VRAM {gpu.get("vram_used_gb",0)}/{gpu.get("vram_total_gb",0)}GB · {gpu.get("temp_c",0):.0f}°C · {gpu.get("power_w",0):.0f}W">'
        f'  <span class="pc-hud-k">GPU</span>'
        f'  <span class="pc-hud-bar"><i class="{gpu_tone}" style="width:{max(4.0, min(100.0, gpu_pct)):.0f}%"></i></span>'
        f'  <span class="pc-hud-v">{gpu_pct:.0f}% <small>VRAM {gpu.get("vram_used_gb",0):.1f}/{gpu.get("vram_total_gb",0):.0f}G · {gpu.get("temp_c",0):.0f}°C · {gpu.get("power_w",0):.0f}W</small></span>'
        f'</div>'
        if gpu_avail
        else ""
    )

    gpu_card_html = (
        f'<div class="pc-bento-card">'
        f'  <div class="pc-bento-head"><span class="pc-bento-title">GPU · {gpu_name}</span><span class="pc-bento-badge {gpu_tone}">{gpu_pct:.0f}% · {gpu.get("temp_c",0):.0f}°C</span></div>'
        f'  <div class="pc-bento-meter"><span class="pc-bento-fill {gpu_tone}" style="width:{min(100.0, gpu_pct):.1f}%"></span></div>'
        f'  <div class="pc-bento-sub"><span>VRAM <b>{gpu.get("vram_used_gb",0):.1f} / {gpu.get("vram_total_gb",0):.1f} GB</b> ({vram_pct:.0f}%)</span><span>전력 <b>{gpu.get("power_w",0):.0f}W</b> / {gpu.get("power_limit_w",0):.0f}W</span></div>'
        f'</div>'
        if gpu_avail
        else ""
    )

    return (
        f'<!--__PC_TELEMETRY_HTML_START__-->'
        f'<details class="pc-telemetry-ext" id="pc-telemetry-ext"{open_attr}>'
        f'  <summary class="pc-hud-strip" title="클릭하여 PC 하드웨어 & 작업용량 상세 모니터 펼치기/접기 (확장 모듈: pc_telemetry_ext.py)">'
        f'    <span class="pc-hud-tag">PC MON</span>'
        f'    <div class="pc-hud-grid">'
        f'      <div class="pc-hud-item" title="{html.escape(str(cpu.get("model","CPU")))} ({cpu.get("cores",0)} Threads) · Load {cpu.get("load1",0)}">'
        f'        <span class="pc-hud-k">CPU</span>'
        f'        <span class="pc-hud-bar"><i class="{cpu_tone}" style="width:{max(4.0, min(100.0, cpu_pct)):.0f}%"></i></span>'
        f'        <span class="pc-hud-v">{cpu_pct:.0f}% <small>{cpu_temp_str}</small></span>'
        f'      </div>'
        f'      {gpu_pill_html}'
        f'      <div class="pc-hud-item" title="메모리 {ram.get("used_gb",0)} / {ram.get("total_gb",0)} GB (캐시 {ram.get("cached_gb",0)} GB)">'
        f'        <span class="pc-hud-k">RAM</span>'
        f'        <span class="pc-hud-bar"><i class="{ram_tone}" style="width:{max(4.0, min(100.0, ram_pct)):.0f}%"></i></span>'
        f'        <span class="pc-hud-v">{ram.get("used_gb",0):.1f}<small>/{ram.get("total_gb",0):.0f}G ({ram_pct:.0f}%)</small></span>'
        f'      </div>'
        f'      <div class="pc-hud-item" title="NVMe 디스크 {st.get("disk_used_gb",0)}/{st.get("disk_total_gb",0)}GB · 워크스페이스(~/dev) {ws_label}">'
        f'        <span class="pc-hud-k">작업용량</span>'
        f'        <span class="pc-hud-bar"><i class="{disk_tone}" style="width:{max(4.0, min(100.0, disk_pct)):.0f}%"></i></span>'
        f'        <span class="pc-hud-v">~/dev <b>{ws_label}</b> <small>· 디스크 {disk_pct:.0f}% ({st.get("disk_free_gb",0):.0f}G 여유)</small></span>'
        f'      </div>'
        f'      <div class="pc-hud-item net {"busy" if net_busy else ""}" title="네트워크 인터페이스({html.escape(str(net.get("iface","eth0")))}) 누적 ↓{net.get("rx_total_gb",0)}GB / ↑{net.get("tx_total_gb",0)}GB">'
        f'        <span class="pc-hud-k">NET</span>'
        f'        <span class="pc-hud-v"><b class="rx">↓ {rx_label}</b> <small class="tx">↑ {tx_label}</small></span>'
        f'      </div>'
        f'    </div>'
        f'    <span class="pc-hud-caret">▾</span>'
        f'  </summary>'
        f'  <div class="pc-bento-drawer">'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">CPU · {html.escape(str(cpu.get("model","CPU")))}</span><span class="pc-bento-badge {cpu_tone}">{cpu_pct:.1f}% · {cpu_temp_str}</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {cpu_tone}" style="width:{min(100.0, cpu_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>코어 <b>{cpu.get("cores",0)} Threads</b></span><span>Load <b>{cpu.get("load1",0)} / {cpu.get("load5",0)} / {cpu.get("load15",0)}</b></span></div>'
        f'    </div>'
        f'    {gpu_card_html}'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">RAM & Swap</span><span class="pc-bento-badge {ram_tone}">{ram.get("used_gb",0):.1f} / {ram.get("total_gb",0):.1f} GB ({ram_pct:.0f}%)</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {ram_tone}" style="width:{min(100.0, ram_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>가용 <b>{ram.get("avail_gb",0):.1f} GB</b> · 캐시 {ram.get("cached_gb",0):.1f}G</span><span>Swap <b>{ram.get("swap_used_gb",0):.1f}/{ram.get("swap_total_gb",0):.1f}G</b></span></div>'
        f'    </div>'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">작업용량 (~/dev) & NVMe</span><span class="pc-bento-badge {disk_tone}">~/dev {ws_label} · 디스크 {disk_pct:.0f}%</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {disk_tone}" style="width:{min(100.0, disk_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>사용 <b>{st.get("disk_used_gb",0):.0f} / {st.get("disk_total_gb",0):.0f} GB</b> (여유 {st.get("disk_free_gb",0):.0f}G)</span><div class="pc-proj-list">{top_proj_pills}</div></div>'
        f'    </div>'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">네트워크 ({html.escape(str(net.get("iface","eth0")))})</span><span class="pc-bento-badge ok">↓ {rx_label} · ↑ {tx_label}</span></div>'
        f'      <div class="pc-bento-sub"><span>누적 다운로드 <b>{net.get("rx_total_gb",0):.2f} GB</b></span><span>누적 업로드 <b>{net.get("tx_total_gb",0):.2f} GB</b></span></div>'
        f'    </div>'
        f'  </div>'
        f'</details>'
        f'<!--__PC_TELEMETRY_HTML_END__-->'
    )


if __name__ == "__main__":
    print(json.dumps(collect_pc_telemetry(force=True), ensure_ascii=False, indent=2))
