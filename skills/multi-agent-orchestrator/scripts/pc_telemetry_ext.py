#!/usr/bin/env python3
"""
Optional Plug-in Extension: PC Hardware, Multi-NIC Network Diagnostics & Active Port Monitor (pc_telemetry_ext.py)
------------------------------------------------------------------------------------------------------------------
Designed as a drop-in optional module ("있어도 되고 없어도 되는 확장판"):
- If present alongside render_usage_widget.py and widget_bridge_daemon.py, renders a low-glare
  Beszel/Railway/iStat-inspired Hardware & Network Telemetry HUD Bar + Expandable 6-Card Bento Drawer:
  1) CPU (Model, Threads, Load, Temp)
  2) GPU (RTX 5090 Core %, VRAM Used/Total GB, Temp, Power W)
  3) RAM & Swap (Used/Total GB, Available, Cached, Swap)
  4) Workspace (`~/dev`) & NVMe Storage (Top projects + free disk space)
  5) Multi-Interface Network Diagnostics (Ethernet vs Wi-Fi comparison, dual-subnet ARP conflict detection,
     2.4GHz vs 5GHz band detection, RX packet drop counter, Gateway/Internet/DNS latency root-cause diagnosis)
  6) Active Listening Ports Visualizer (`ss -tulpn` grouped into AI/Agent, Dev/Project, Remote/Security, System)
- Persists open/closed drawer state (`pcMonOpen`) across refreshes and updates inner DOM in-place so it never closes!
"""
from __future__ import annotations

import html
import ipaddress
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
_prev_net_bytes: tuple[float, int, int, int] | None = None
_cached_telemetry: dict[str, Any] | None = None
_cached_telemetry_ts: float = 0.0
_cached_workspace_info: dict[str, Any] | None = None
_cached_workspace_ts: float = 0.0
_cached_net_diag: dict[str, Any] | None = None
_cached_net_diag_ts: float = 0.0
_cached_ports: list[dict[str, Any]] | None = None
_cached_ports_ts: float = 0.0
_cached_cpu_model: str | None = None
_ws_scan_running = False
_net_probe_running = False

TELEMETRY_TTL_SEC = 2.5
WORKSPACE_TTL_SEC = 60.0
NET_DIAG_TTL_SEC = 10.0
PORTS_TTL_SEC = 8.0


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
        root = "/sys/class/thermal"
        if os.path.isdir(root):
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
        time.sleep(0.05)
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
                    info[k.strip()] = int(num.group(0)) * 1024
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
        if not _ws_scan_running and os.path.isdir(dev_dir):
            _ws_scan_running = True
            t = threading.Thread(target=_bg_scan_workspace, args=(dev_dir,), daemon=True)
            t.start()
            t.join(timeout=0.8)
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


def _fmt_bytes_short(num_bytes: float) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.1f}G"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.0f}M"
    if num_bytes >= 1024:
        return f"{num_bytes / (1024 ** 1):.0f}K"
    return f"{int(num_bytes)}B"


def _fmt_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / (1024 ** 2):.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{int(bytes_per_sec)} B/s"


def _ping_host_ms(host: str) -> float | None:
    if not host:
        return None
    try:
        out = subprocess.check_output(
            ["ping", "-c", "1", "-W", "1", host],
            stderr=subprocess.DEVNULL,
            timeout=1.4,
            text=True,
        )
        m = re.search(r"time=([\d.]+)\s*ms", out)
        if m:
            return round(float(m.group(1)), 1)
    except Exception:
        pass
    return None


def _probe_dns_ms() -> float | None:
    t0 = time.time()
    try:
        socket.getaddrinfo("google.com", 443)
        return round((time.time() - t0) * 1000.0, 1)
    except Exception:
        return None


# Severity thresholds. Cumulative boot-time rx_dropped is NEVER a trigger on its own.
NET_LIVE_DROP_WARN_PER_SEC = 2.0
NET_GATEWAY_WARN_MS = 80.0

# Active IPv4 conflict source: bounded journalctl window (kernel + NetworkManager only).
NET_CONFLICT_WINDOW_SEC = 300.0
NET_CONFLICT_JOURNAL_TIMEOUT_SEC = 1.0
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_CONFLICT_MSG_RE = re.compile(r"duplicate address|address conflict", re.I)


def _collect_recent_ipv4_conflicts(local_ips: set[str]) -> list[str]:
    """Return local IPv4s with an explicit duplicate-address/address-conflict journal event in the last 5 min.

    Only kernel and NetworkManager records count. Stale (>300s) or future timestamps, IPv6 DAD,
    generic words and IPs not currently assigned locally are ignored. Any failure => no conflict.
    """
    if not local_ips:
        return []
    try:
        proc = subprocess.run(
            [
                "journalctl", "-o", "json", "--no-pager", "-q",
                "--since", "5 minutes ago", "-n", "500",
                "_TRANSPORT=kernel", "+", "_SYSTEMD_UNIT=NetworkManager.service",
                "+", "SYSLOG_IDENTIFIER=NetworkManager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=NET_CONFLICT_JOURNAL_TIMEOUT_SEC,
            text=True,
        )
        if proc.returncode != 0:
            return []
        out = proc.stdout or ""
    except Exception:
        return []

    now = time.time()
    found: list[str] = []
    for line in out.splitlines():
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                continue
            age = now - int(rec["__REALTIME_TIMESTAMP"]) / 1_000_000.0
            if not (0.0 <= age <= NET_CONFLICT_WINDOW_SEC):
                continue
            is_kernel = rec.get("_TRANSPORT") == "kernel"
            is_nm = (
                rec.get("_SYSTEMD_UNIT") == "NetworkManager.service"
                or rec.get("SYSLOG_IDENTIFIER") == "NetworkManager"
            )
            if not (is_kernel or is_nm):
                continue
            msg = rec.get("MESSAGE")
            if isinstance(msg, list):
                msg = bytes(int(b) & 0xFF for b in msg).decode("utf-8", errors="ignore")
            if not isinstance(msg, str) or not _CONFLICT_MSG_RE.search(msg):
                continue
            if "ipv6" in msg.lower():
                continue
            for cand in _IPV4_RE.findall(msg):
                try:
                    ip = str(ipaddress.IPv4Address(cand))
                except ValueError:
                    continue
                if ip in local_ips and ip not in found:
                    found.append(ip)
        except Exception:
            continue
    return found


def _synthesize_network_verdict(
    interfaces: list[dict[str, Any]],
    gateway_ip: str,
    gw_ms: float | None,
    ext_ms: float | None,
    dns_ms: float | None,
    rx_drop_rate: float,
    ip_conflicts: list[str] | None = None,
) -> dict[str, Any]:
    """Pure severity decision. `reasons` hold only warn/crit causes; `notes` are informational.

    warn: live rx drop >= 2.0/s, gateway ping failed (external reachable) or > 80ms,
          confirmed IPv4 conflict (kernel/NetworkManager journal, last 5 min, local IP) in `ip_conflicts`.
    crit: gateway+external unreachable, or external unreachable while gateway answers.
    Informational only: cumulative drops, subnet overlap, 2.4GHz Wi-Fi, DNS/external latency.
    Active IP conflicts are never inferred from subnet overlap; only confirmed journal events count.
    """
    reasons: list[str] = []
    notes: list[str] = []
    status = "ok"
    causes: set[str] = set()

    active_with_ip = [i for i in interfaces if i.get("ip")]
    eth_subnets = {i["subnet"] for i in active_with_ip if i["kind"] == "ethernet" and i["subnet"]}
    wifi_subnets = {i["subnet"] for i in active_with_ip if i["kind"] == "wifi" and i["subnet"]}
    if eth_subnets & wifi_subnets:
        notes.append("유선(이더넷)+무선(Wi-Fi) 동일 대역 이중 연결 (정보: 실제 IP 충돌 아님)")

    for i in active_with_ip:
        if i["kind"] == "wifi" and "2.4GHz" in i.get("speed_label", ""):
            notes.append(f"무선({i['iface']})이 2.4GHz 대역에 연결됨 ({i['speed_label']})")
        if i.get("rx_drop", 0) > 500:
            notes.append(f"{i['iface']} 부팅 이후 누적 수신 드랍 {i['rx_drop']:,}건 (과거 누적, 정보)")

    if rx_drop_rate >= NET_LIVE_DROP_WARN_PER_SEC:
        status = "warn"
        causes.add("drop")
        reasons.append(f"실시간 수신 패킷 드랍 {rx_drop_rate:.1f}건/초")

    if ip_conflicts:
        status = "warn" if status == "ok" else status
        causes.add("conflict")
        reasons.append(f"최근 5분 내 IPv4 주소 충돌 감지 ({', '.join(ip_conflicts)})")

    if ext_ms is None and gw_ms is None and gateway_ip:
        status = "crit"
        causes.add("outage")
        reasons.insert(0, f"공유기({gateway_ip}) 및 외부 인터넷 응답 없음 (랜선/와이파이 단절)")
    elif ext_ms is None and gw_ms is not None:
        status = "crit"
        causes.add("outage")
        reasons.insert(0, f"공유기({gw_ms:.0f}ms)는 정상이나 외부 인터넷(ISP/WAN) 단절됨")
    else:
        if gw_ms is None and gateway_ip:
            status = "warn" if status == "ok" else status
            causes.add("gateway")
            reasons.insert(0, f"공유기({gateway_ip}) 핑 응답 실패 (외부 인터넷은 응답)")
        elif gw_ms is not None and gw_ms > NET_GATEWAY_WARN_MS:
            status = "warn" if status == "ok" else status
            causes.add("gateway")
            reasons.insert(0, f"공유기 내부망 지연 감지 (게이트웨이 핑 {gw_ms:.0f}ms)")
        if ext_ms is not None and ext_ms > 90.0:
            notes.append(f"외부 인터넷 지연 (1.1.1.1 핑 {ext_ms:.0f}ms, 정보)")
        if dns_ms is not None and dns_ms > 120.0:
            notes.append(f"DNS 도메인 질의 지연 ({dns_ms:.0f}ms, 정보)")

    if status == "ok":
        prim = next((i for i in interfaces if i["is_primary"]), interfaces[0] if interfaces else None)
        p_desc = f"{prim['kind_label']} {prim['speed_label']}" if prim else "연결됨"
        ping_str = f" · 핑 {ext_ms:.0f}ms" if ext_ms is not None else ""
        summary = f"정상 ({p_desc}{ping_str})"
    elif status == "crit":
        summary = "오류: 인터넷 응답 지연/단절"
    elif "conflict" in causes and not (causes & {"gateway", "drop"}):
        summary = "주의: IPv4 주소 충돌 감지"
    elif "gateway" in causes and "drop" not in causes:
        summary = "주의: 공유기 응답 지연/실패"
    else:
        summary = "주의: 네트워크 병목 감지"

    return {"status": status, "summary": summary, "reasons": reasons, "notes": notes}


def _collect_interfaces_and_diagnosis(rx_drop_rate: float) -> dict[str, Any]:
    # 1. Read /proc/net/dev per-interface counters
    dev_stats: dict[str, dict[str, int]] = {}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                fields = [int(x) for x in rest.split()]
                if len(fields) >= 12:
                    dev_stats[iface] = {
                        "rx_bytes": fields[0],
                        "rx_packets": fields[1],
                        "rx_errs": fields[2],
                        "rx_drop": fields[3],
                        "tx_bytes": fields[8],
                        "tx_packets": fields[9],
                        "tx_errs": fields[10],
                        "tx_drop": fields[11],
                    }
    except Exception:
        pass

    # 2. IPv4 addresses & subnets
    ip_map: dict[str, tuple[str, str]] = {}
    local_ips: set[str] = set()
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], timeout=1.0, text=True)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                iface = parts[1]
                cidr = parts[3]
                ip = cidr.split("/")[0]
                subnet = ".".join(ip.split(".")[:3]) if "." in ip else cidr
                ip_map[iface] = (ip, subnet)
                if "." in ip:
                    local_ips.add(ip)
    except Exception:
        pass

    # 3. Default routes & metrics
    default_routes: dict[str, tuple[str, int]] = {}
    primary_iface = ""
    best_metric = 999999
    gateway_ip = ""
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], timeout=1.0, text=True)
        for line in out.splitlines():
            m_dev = re.search(r"\bdev\s+(\S+)", line)
            m_via = re.search(r"\bvia\s+(\S+)", line)
            m_met = re.search(r"\bmetric\s+(\d+)", line)
            if m_dev:
                ifc = m_dev.group(1)
                gw = m_via.group(1) if m_via else ""
                met = int(m_met.group(1)) if m_met else 100
                default_routes[ifc] = (gw, met)
                if met < best_metric:
                    best_metric = met
                    primary_iface = ifc
                    gateway_ip = gw
    except Exception:
        pass

    # 4. Wi-Fi band / SSID detection via nmcli or iw
    wifi_info: dict[str, str] = {}
    try:
        if shutil.which("nmcli"):
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,CHAN,FREQ,RATE,SIGNAL,DEVICE", "dev", "wifi"],
                timeout=1.5,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in out.splitlines():
                if line.startswith("yes:"):
                    p = line.split(":")
                    if len(p) >= 7:
                        ssid, _chan, freq, _rate, sig, dev = p[1], p[2], p[3], p[4], p[5], p[6]
                        band = "2.4GHz" if "24" in freq else ("5GHz" if "5" in freq else freq)
                        wifi_info[dev] = f"{ssid} ({band} · {sig}%)"
    except Exception:
        pass

    interfaces: list[dict[str, Any]] = []
    for iface, st in sorted(dev_stats.items()):
        if iface == "lo" or iface.startswith(("docker", "veth", "br-", "virbr")):
            continue
        is_wifi = iface.startswith(("wl", "wlan")) or os.path.isdir(f"/sys/class/net/{iface}/wireless")
        kind = "wifi" if is_wifi else "ethernet"
        oper = "down"
        try:
            with open(f"/sys/class/net/{iface}/operstate", "r", encoding="utf-8") as f:
                oper = f.read().strip()
        except Exception:
            pass
        ip_addr, subnet = ip_map.get(iface, ("", ""))
        if oper != "up" and not ip_addr and (st["rx_bytes"] + st["tx_bytes"] == 0):
            continue
        speed_label = ""
        if is_wifi:
            speed_label = wifi_info.get(iface, "Wi-Fi")
        else:
            try:
                with open(f"/sys/class/net/{iface}/speed", "r", encoding="utf-8") as f:
                    spd = int(f.read().strip())
                    if spd >= 1000:
                        speed_label = f"{spd // 1000}Gbps"
                    elif spd > 0:
                        speed_label = f"{spd}Mbps"
            except Exception:
                speed_label = "유선랜"
        gw, metric = default_routes.get(iface, ("", 999))
        interfaces.append({
            "iface": iface,
            "kind": kind,
            "kind_label": "Wi-Fi" if is_wifi else "이더넷",
            "operstate": oper,
            "ip": ip_addr,
            "subnet": subnet,
            "speed_label": speed_label,
            "gateway": gw,
            "metric": metric,
            "is_primary": iface == primary_iface,
            "rx_drop": st["rx_drop"],
            "rx_errs": st["rx_errs"],
            "tx_drop": st["tx_drop"],
        })

    interfaces.sort(key=lambda x: (0 if x["is_primary"] else 1, 0 if x["ip"] else 1, x["metric"]))

    # 5. Latency & DNS probes
    gw_ms = _ping_host_ms(gateway_ip) if gateway_ip else None
    ext_ms = _ping_host_ms("1.1.1.1")
    dns_ms = _probe_dns_ms()

    # 6. Root-cause diagnosis synthesizer
    ip_conflicts = _collect_recent_ipv4_conflicts(local_ips)
    verdict = _synthesize_network_verdict(interfaces, gateway_ip, gw_ms, ext_ms, dns_ms, rx_drop_rate, ip_conflicts)
    status = verdict["status"]
    summary = verdict["summary"]
    reasons = verdict["reasons"]
    notes = verdict["notes"]

    return {
        "interfaces": interfaces,
        "primary_iface": primary_iface,
        "gateway_ip": gateway_ip,
        "gw_ping_ms": gw_ms,
        "ext_ping_ms": ext_ms,
        "dns_ms": dns_ms,
        "status": status,
        "summary": summary,
        "reasons": reasons,
        "notes": notes,
    }


def _collect_listening_ports() -> list[dict[str, Any]]:
    global _cached_ports, _cached_ports_ts
    now = time.time()
    if _cached_ports is not None and (now - _cached_ports_ts) < PORTS_TTL_SEC:
        return _cached_ports

    ports_by_num: dict[int, dict[str, Any]] = {}
    try:
        out = subprocess.check_output(
            ["ss", "-tulpn"],
            timeout=1.5,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5:
                continue
            proto = parts[0].lower()
            state = parts[1].upper()
            if proto.startswith("tcp") and state != "LISTEN":
                continue
            local_addr = parts[4]
            if ":" not in local_addr:
                continue
            host_part, port_str = local_addr.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                continue
            # Skip ephemeral high UDP ports (>32768) unless known
            if proto.startswith("udp") and port > 32768 and port not in (53366,):
                continue

            proc_match = re.search(r'users:\(\("([^"]+)"', line)
            proc_name = proc_match.group(1) if proc_match else ""
            if not proc_name:
                if port == 22:
                    proc_name = "sshd"
                elif port == 53:
                    proc_name = "systemd-resolve"
                elif port == 631:
                    proc_name = "cupsd"
                elif port in (3389, 3350):
                    proc_name = "xrdp"
                else:
                    proc_name = proto.upper()

            is_public = host_part in ("0.0.0.0", "*", "[::]", "::") or host_part.startswith("0.0.0.0%")
            scope = "public" if is_public else "local"

            p_low = proc_name.lower()
            if port == 18765 or "language_server" in p_low or "antigravity" in p_low or "codex" in p_low or "claude" in p_low or "ollama" in p_low or "vllm" in p_low:
                cat = "ai"
                cat_label = "🤖 AI·에이전트"
            elif any(k in p_low for k in ("node", "vite", "next", "uvicorn", "gunicorn", "flask", "django", "docker", "postgres", "redis", "jetbrains")) or (3000 <= port <= 9999) or (p_low.startswith("python") and port != 18765):
                cat = "dev"
                cat_label = "💻 개발·서버"
            elif any(k in p_low for k in ("xrdp", "rustdesk", "veraport", "lws", "sshd", "vnc")) or port in (22, 3389, 3350, 21119):
                cat = "remote"
                cat_label = "🛡️ 원격·보안"
            else:
                cat = "sys"
                cat_label = "🖥️ 시스템"

            # Friendly display name
            disp_proc = proc_name
            if port == 18765:
                disp_proc = "ai-maestro"
            elif "language_server" in p_low:
                disp_proc = "agy-langserver"

            existing = ports_by_num.get(port)
            if not existing or (is_public and existing["scope"] != "public"):
                ports_by_num[port] = {
                    "port": port,
                    "proto": "UDP" if proto.startswith("udp") else "TCP",
                    "process": disp_proc,
                    "scope": scope,
                    "category": cat,
                    "category_label": cat_label,
                }
    except Exception:
        pass

    cat_order = {"ai": 0, "dev": 1, "remote": 2, "sys": 3}
    res = sorted(ports_by_num.values(), key=lambda x: (cat_order.get(x["category"], 9), x["port"]))
    _cached_ports = res
    _cached_ports_ts = now
    return res


def _read_net_counters() -> tuple[int, int, int, str]:
    rx_total = 0
    tx_total = 0
    rx_drop_total = 0
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
            if len(fields) < 12:
                continue
            rx = int(fields[0])
            rx_drop = int(fields[3])
            tx = int(fields[8])
            rx_total += rx
            tx_total += tx
            rx_drop_total += rx_drop
            if rx + tx > 0:
                active_ifaces.append((rx + tx, iface))
    except Exception:
        pass
    active_ifaces.sort(reverse=True)
    primary_iface = active_ifaces[0][1] if active_ifaces else "eno1"
    return rx_total, tx_total, rx_drop_total, primary_iface


def _bg_update_net_diag(rx_drop_rate: float) -> None:
    global _cached_net_diag, _cached_net_diag_ts, _net_probe_running
    try:
        _cached_net_diag = _collect_interfaces_and_diagnosis(rx_drop_rate)
        _cached_net_diag_ts = time.time()
    except Exception:
        pass
    finally:
        _net_probe_running = False


def _collect_network() -> dict[str, Any]:
    global _prev_net_bytes, _cached_net_diag, _cached_net_diag_ts, _net_probe_running
    now = time.time()
    rx1, tx1, drop1, iface = _read_net_counters()
    rx_rate = 0.0
    tx_rate = 0.0
    drop_rate = 0.0
    if _prev_net_bytes is not None:
        dt = now - _prev_net_bytes[0]
        if dt > 0.05:
            rx_rate = max(0.0, (rx1 - _prev_net_bytes[1]) / dt)
            tx_rate = max(0.0, (tx1 - _prev_net_bytes[2]) / dt)
            drop_rate = max(0.0, (drop1 - _prev_net_bytes[3]) / dt)
    _prev_net_bytes = (now, rx1, tx1, drop1)

    if _cached_net_diag is None:
        if not _net_probe_running:
            _net_probe_running = True
            t = threading.Thread(target=_bg_update_net_diag, args=(drop_rate,), daemon=True)
            t.start()
            t.join(timeout=1.2)
    elif (now - _cached_net_diag_ts) >= NET_DIAG_TTL_SEC and not _net_probe_running:
        _net_probe_running = True
        threading.Thread(target=_bg_update_net_diag, args=(drop_rate,), daemon=True).start()

    diag = _cached_net_diag or {
        "interfaces": [],
        "primary_iface": iface,
        "gateway_ip": "",
        "gw_ping_ms": None,
        "ext_ping_ms": None,
        "dns_ms": None,
        "status": "ok",
        "summary": "정상",
        "reasons": [],
    }

    return {
        "iface": diag.get("primary_iface") or iface,
        "rx_bytes_per_sec": round(rx_rate, 1),
        "tx_bytes_per_sec": round(tx_rate, 1),
        "rx_rate_label": _fmt_rate(rx_rate),
        "tx_rate_label": _fmt_rate(tx_rate),
        "rx_total_gb": round(rx1 / (1024 ** 3), 2),
        "tx_total_gb": round(tx1 / (1024 ** 3), 2),
        "diagnosis": diag,
    }


def collect_pc_telemetry(force: bool = False) -> dict[str, Any]:
    """Return cached or freshly sampled PC hardware, multi-NIC network diagnostics & listening ports."""
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
            "ports": _collect_listening_ports(),
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
    """Render the Beszel/Railway/iStat-inspired collapsible Hardware HUD Strip + 6-Card Bento Drawer."""
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
    ports = pc.get("ports", [])

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
    diag = net.get("diagnosis", {}) or {}
    diag_status = diag.get("status", "ok")
    diag_summary = html.escape(str(diag.get("summary", "정상")))
    diag_reasons = diag.get("reasons", []) or []
    ifaces = diag.get("interfaces", []) or []

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

    # Interface rows inside Network Bento Card
    iface_rows_html = []
    for ifc in ifaces:
        ico = "📶" if ifc.get("kind") == "wifi" else "🔌"
        role_badge = "<b>[주경로]</b>" if ifc.get("is_primary") else "<small>[보조]</small>"
        drop_cnt = int(ifc.get("rx_drop", 0))
        drop_txt = f' · <span style="color:#e9b45b">드랍 {drop_cnt:,}</span>' if drop_cnt > 500 else ""
        iface_rows_html.append(
            f'<div class="pc-bento-sub" style="margin-top:1px">'
            f'<span>{ico} <b>{html.escape(ifc.get("iface",""))}</b> ({html.escape(ifc.get("speed_label",""))}) {role_badge}</span>'
            f'<span>{html.escape(ifc.get("ip","미연결"))}{drop_txt}</span>'
            f'</div>'
        )

    gw_ms = diag.get("gw_ping_ms")
    ext_ms = diag.get("ext_ping_ms")
    dns_ms = diag.get("dns_ms")
    ping_summary = (
        f'공유기 <b>{f"{gw_ms:.0f}ms" if gw_ms is not None else "시간초과"}</b> · '
        f'외부망 <b>{f"{ext_ms:.0f}ms" if ext_ms is not None else "시간초과"}</b> · '
        f'DNS <b>{f"{dns_ms:.0f}ms" if dns_ms is not None else "-"}</b>'
    )

    diag_alert_pills = "".join(
        f'<span class="pc-proj-pill" style="background:rgba(233,180,91,0.13);border-color:rgba(233,180,91,0.35);color:#fbbf24">⚠️ {html.escape(r)}</span>'
        for r in diag_reasons[:3]
    )
    if not diag_alert_pills:
        diag_alert_pills = '<span class="pc-proj-pill" style="background:rgba(84,201,160,0.12);border-color:rgba(84,201,160,0.30);color:#54c9a0">✓ 이더넷·라우팅·DNS 상태 정상</span>'

    # Active Listening Ports pills grouped cleanly
    port_pills = []
    for p in ports[:14]:
        cat = p.get("category", "sys")
        scope_ico = "🌐" if p.get("scope") == "public" else "🔒"
        scope_desc = "외부열림(0.0.0.0)" if p.get("scope") == "public" else "로컬전용(127.0.0.1)"
        if cat == "ai":
            style = "background:rgba(56,189,248,0.12);border-color:rgba(56,189,248,0.30);color:#7dd3fc"
        elif cat == "dev":
            style = "background:rgba(84,201,160,0.12);border-color:rgba(84,201,160,0.30);color:#6ee7b7"
        elif cat == "remote":
            style = "background:rgba(233,180,91,0.12);border-color:rgba(233,180,91,0.30);color:#fcd34d"
        else:
            style = "background:rgba(148,163,184,0.10);border-color:rgba(148,163,184,0.24);color:#cbd5e1"
        port_pills.append(
            f'<span class="pc-proj-pill" style="{style}" title="{html.escape(p.get("category_label",""))} · {p["proto"]} :{p["port"]} ({html.escape(p["process"])}) · {scope_desc}">'
            f'{scope_ico} <b>:{p["port"]}</b> {html.escape(p["process"][:14])}'
            f'</span>'
        )
    ai_cnt = sum(1 for p in ports if p.get("category") == "ai")
    dev_cnt = sum(1 for p in ports if p.get("category") == "dev")
    rem_cnt = sum(1 for p in ports if p.get("category") in ("remote", "sys"))

    net_tip = " / ".join(diag_reasons) if diag_reasons else f"네트워크 정상 ({diag_summary})"

    return (
        f'<!--__PC_TELEMETRY_HTML_START__-->'
        f'<details class="pc-telemetry-ext" id="pc-telemetry-ext" ontoggle="if(typeof onPcTelemetryToggle===\'function\')onPcTelemetryToggle(this.open)"{open_attr}>'
        f'  <summary class="pc-hud-strip" title="클릭하여 PC 하드웨어 · 네트워크 원인 진단 · 사용중인 포트 모니터 펼치기/접기">'
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
        f'        <span class="pc-hud-v">~/dev <b>{ws_label}</b> <small>· SSD {disk_pct:.0f}%</small></span>'
        f'      </div>'
        f'      <div class="pc-hud-item net" title="{html.escape(net_tip)}">'
        f'        <span class="pc-hud-k">NET</span>'
        f'        <span class="pc-hud-v"><b class="rx">↓{rx_label}</b> <small class="tx">↑{tx_label}</small></span>'
        f'        <span class="pc-bento-badge {diag_status}" style="font-size:8px;padding:0 4px">{diag_summary}</span>'
        f'      </div>'
        f'      <div class="pc-hud-item" title="현재 열려있는 리스닝 포트 총 {len(ports)}개 (AI {ai_cnt} · 개발 {dev_cnt} · 원격/시스템 {rem_cnt})">'
        f'        <span class="pc-hud-k">PORT</span>'
        f'        <span class="pc-hud-v">{len(ports)}개 <small>(AI {ai_cnt}·개발 {dev_cnt})</small></span>'
        f'      </div>'
        f'    </div>'
        f'    <span class="pc-hud-caret">▾</span>'
        f'  </summary>'
        f'  <div class="pc-bento-drawer">'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">CPU · {html.escape(str(cpu.get("model","CPU")))}</span><span class="pc-bento-badge {cpu_tone}">{cpu_pct:.1f}% · {cpu_temp_str}</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {cpu_tone}" style="width:{min(100.0, cpu_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>코어 <b>{cpu.get("cores",0)} Threads</b></span><span>Load <b>{cpu.get("load1",0)} / {cpu.get("load5",0)}</b></span></div>'
        f'    </div>'
        f'    {gpu_card_html}'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">RAM & Swap</span><span class="pc-bento-badge {ram_tone}">{ram.get("used_gb",0):.1f} / {ram.get("total_gb",0):.1f} GB ({ram_pct:.0f}%)</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {ram_tone}" style="width:{min(100.0, ram_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>가용 <b>{ram.get("avail_gb",0):.1f} GB</b> · 캐시 {ram.get("cached_gb",0):.1f}G</span><span>Swap <b>{ram.get("swap_used_gb",0):.1f}/{ram.get("swap_total_gb",0):.1f}G</b></span></div>'
        f'    </div>'
        f'    <div class="pc-bento-card">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">작업용량 (~/dev) & NVMe</span><span class="pc-bento-badge {disk_tone}">~/dev {ws_label} · SSD {disk_pct:.0f}%</span></div>'
        f'      <div class="pc-bento-meter"><span class="pc-bento-fill {disk_tone}" style="width:{min(100.0, disk_pct):.1f}%"></span></div>'
        f'      <div class="pc-bento-sub"><span>디스크 <b>{st.get("disk_used_gb",0):.0f} / {st.get("disk_total_gb",0):.0f} GB</b> (여유 {st.get("disk_free_gb",0):.0f}G)</span><div class="pc-proj-list">{top_proj_pills}</div></div>'
        f'    </div>'
        f'    <div class="pc-bento-card" style="grid-column: span 2">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">네트워크 인터페이스 비교 (이더넷 vs Wi-Fi) & 지연 원인 진단</span><span class="pc-bento-badge {diag_status}">↓ {rx_label} · ↑ {tx_label} · {diag_summary}</span></div>'
        f'      {"".join(iface_rows_html)}'
        f'      <div class="pc-bento-sub" style="margin-top:2px"><span>지연 측정: {ping_summary}</span><span>누적 ↓<b>{net.get("rx_total_gb",0):.2f}G</b> / ↑<b>{net.get("tx_total_gb",0):.2f}G</b></span></div>'
        f'      <div class="pc-proj-list">{diag_alert_pills}</div>'
        f'    </div>'
        f'    <div class="pc-bento-card" style="grid-column: span 2">'
        f'      <div class="pc-bento-head"><span class="pc-bento-title">활성 포트 모니터 (총 {len(ports)}개 · 🌐 외부열림 / 🔒 로컬)</span><span class="pc-bento-badge ok">🤖 AI {ai_cnt} · 💻 개발 {dev_cnt} · 🛡️ 원격/시스템 {rem_cnt}</span></div>'
        f'      <div class="pc-proj-list">{"".join(port_pills)}</div>'
        f'    </div>'
        f'  </div>'
        f'</details>'
        f'<!--__PC_TELEMETRY_HTML_END__-->'
    )


if __name__ == "__main__":
    print(json.dumps(collect_pc_telemetry(force=True), ensure_ascii=False, indent=2))
