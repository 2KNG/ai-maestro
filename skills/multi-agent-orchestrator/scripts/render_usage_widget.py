#!/usr/bin/env python3
"""
Automatically generates the AOH-style self-contained usage & multi-agent hierarchy tree widget HTML
using live data from check_all_usage.py, and writes it to the target artifact path.

Usage:
  ai-usage-widget <output_html_path>
"""
import base64
import gzip
import glob
import html as html_lib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def parse_kst_to_epoch(kst_str: str) -> int | None:
    if not kst_str or "202" not in kst_str:
        return None
    clean = kst_str.split(" KST")[0].strip()
    try:
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return None


def short_reset_label(kst_str: str) -> str:
    if not kst_str or "202" not in kst_str:
        return "대기"
    clean = kst_str.split(" KST")[0].strip()
    try:
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        if dt.date() == now.date():
            return f"오늘 {dt.strftime('%H:%M')}"
        return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"
    except Exception:
        return clean


def fmt_k(tokens: int | float) -> str:
    tokens = int(tokens or 0)
    if tokens >= 1000000:
        val = tokens / 1000000
        return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


SUB_IDLE_RETENTION_SEC = 15
# An open Claude terminal untouched this long (and not busy) is a forgotten pane, not a main agent.
CLAUDE_MAIN_STALE_SEC = 86400


def _fmt_duration(node: dict) -> str:
    """Use the same latest-turn duration as the collapsed row."""
    _, _, duration = _node_turn_timing(node)
    return f" · ⏱ {_fmt_dur_compact(duration)}" if duration is not None else ""


def normalize_agy_model(model_str: str) -> dict:
    m = model_str or "Unknown"
    is_fast = "Fast" in m or "Flash" in m
    ver_m = re.search(r"(\d+(?:\.\d+)?)", m)
    if "Argon" in m:
        ver = ver_m.group(1) if ver_m else "4"
        effort = "Ultra" if "Ultra" in m else ("Max" if "Max" in m else ("High" if "High" in m else ("Low" if "Low" in m else "Med")))
        eff_lvl = "ultra" if "Ultra" in m else ("max" if "Max" in m else ("high" if "High" in m else ("low" if "Low" in m else "med")))
        return {
            "pill": f"{ver} Argon · {effort}",
            "full": f"{ver} Argon",
            "short": f"{ver}-A",
            "effort": eff_lvl,
            "kind": "argon",
            "fast": False,
        }
    if "Flash" in m:
        ver = ver_m.group(1) if ver_m else "3.8"
        effort = " · High" if "High" in m else (" · Med" if "Medium" in m else (" · Low" if "Low" in m else ""))
        eff_lvl = "high" if "High" in m else ("med" if "Med" in m else "low")
        return {
            "pill": f"{ver} Flash{effort}",
            "full": f"{ver} Flash",
            "short": f"{ver}-F",
            "effort": eff_lvl,
            "kind": "flash",
            "fast": True,
        }
    if "Sonnet" in m:
        ver = ver_m.group(1) if ver_m else "5.5"
        eff_lvl = "ultra" if "Ultra" in m else ("max" if "Max" in m else ("high" if "High" in m else ("med" if "Med" in m else "high")))
        return {
            "pill": f"Sonnet {ver}",
            "full": f"Sonnet {ver}",
            "short": f"S-{ver}",
            "effort": eff_lvl,
            "kind": "claude",
            "fast": False,
        }
    if "Opus" in m:
        ver = ver_m.group(1) if ver_m else "5.5"
        eff_lvl = "ultra" if "Ultra" in m else ("max" if "Max" in m else ("high" if "High" in m else ("med" if "Med" in m else "high")))
        return {
            "pill": f"Opus {ver}",
            "full": f"Opus {ver}",
            "short": f"O-{ver}",
            "effort": eff_lvl,
            "kind": "claude",
            "fast": False,
        }
    if "GPT-OSS" in m:
        return {
            "pill": "GPT-OSS 120B",
            "full": "OSS 120B",
            "short": "120B",
            "effort": "med",
            "kind": "codex",
            "fast": False,
        }
    return {
        "pill": m[:16],
        "full": m[:12],
        "short": m[:6],
        "effort": "med",
        "kind": "argon",
        "fast": is_fast,
    }


def normalize_agy_status(status_str: str) -> str:
    s = (status_str or "").upper()
    if "RUNNING" in s:
        return "running"
    if "IDLE" in s:
        return "idle"
    return "done"


def convert_agy_node(node: dict) -> dict:
    cid = node.get("conversation_id", "")
    ctx = node.get("context_usage", {})
    used = int(ctx.get("used_tokens", 0) or 0)
    max_t = int(ctx.get("max_tokens", 1000000) or 1000000)
    inp = int(ctx.get("input_tokens", 0) or 0)
    cache = int(ctx.get("cache_read_tokens", 0) or 0)
    out_t = int(ctx.get("output_tokens", 0) or 0)
    pct = float(ctx.get("percent", 0.0) or 0.0)
    m_info = normalize_agy_model(node.get("model", ""))
    depth = int(node.get("nesting_depth", 0) or 0)
    role = node.get("role") or ("Main Orchestrator" if depth == 0 else "Subagent")
    title = node.get("title") or role
    display_name = f"Main Orchestrator · {title}" if depth == 0 and role == "Orchestrator" else role

    children_raw = node.get("children", [])
    # Sort children: running first, then by role
    children_sorted = sorted(
        children_raw,
        key=lambda c: (0 if "RUNNING" in (c.get("status") or "") else 1, c.get("role") or ""),
    )

    return {
        "id": cid,
        "provider": "antigravity",
        "repo": _repo_from_uris_or_cwd(node.get("workspaces"))[0],
        "agyOrigin": True,
        "name": display_name,
        "role": role,
        "title": title,
        "depth": depth,
        "status": normalize_agy_status(node.get("status", "")),
        "modelPill": m_info["pill"],
        "modelKind": m_info["kind"],
        "fast": m_info["fast"],
        "serviceTier": "fast" if m_info["fast"] else "default",
        "usedTokens": fmt_k(used),
        "maxTokens": fmt_k(max_t),
        "percent": pct,
        "detailLeft": f"{'ID' if depth == 0 else f'Subagent (Depth {depth})'}: {cid}",
        "detailRight": f"{fmt_k(used)} / {fmt_k(max_t)} (In {fmt_k(inp)} · Cache {fmt_k(cache)} · Out {fmt_k(out_t)})",
        "actionType": "link",
        "actionHref": f"conversation://{cid}",
        "actionLabel": "열기 ↗",
        "children": [convert_agy_node(c) for c in children_sorted[:5]],
    }


def convert_codex_node(node: dict, depth: int = 0) -> dict:
    tid = node.get("thread_id", "")
    tc = node.get("token_count", {})
    used = int(tc.get("total_tokens", 0) or 0)
    max_t = int(tc.get("model_context_window", 475000) or 475000)
    pct = float(tc.get("percent", 0.0) or 0.0)

    tctx = node.get("turn_context", {})
    model_raw = (tctx.get("model") or "gpt-6.1-sol").replace("gpt-", "")
    effort = tctx.get("reasoning_effort") or "ultra"
    fast = (tctx.get("service_tier") or "default") in ("priority", "fast", "ultrafast")

    status_raw = (node.get("status") or "").lower()
    if depth == 0:
        status = "running" if node.get("total_children_count", 0) == 0 else "idle"
    else:
        status = "idle" if status_raw == "open" else "done"

    children = [convert_codex_node(c, depth + 1) for c in node.get("children", [])[:5]]
    total_children = node.get("total_children_count", len(children))

    if depth == 0:
        sub_note = f" · 총 {total_children}개 서브에이전트" if total_children > 0 else " · 단일 스레드"
        detail_left = f"Codex Thread: {tid}"
        detail_right = f"{fmt_k(used)} / {fmt_k(max_t)} ({pct:.1f}%){sub_note}"
    else:
        path_note = f" ({node.get('agent_path')})" if node.get("agent_path") else ""
        detail_left = f"Subagent (Depth {depth}): {tid}{path_note}"
        detail_right = f"{fmt_k(used)} / {fmt_k(max_t)} tokens ({pct:.1f}%)"

    return {
        "id": tid,
        "provider": "codex",
        "repo": _repo_from_uris_or_cwd(None, node.get("cwd"))[0],
        "serviceTier": resolve_speed_tier(tctx.get("service_tier"), node.get("tier")),
        "agyOrigin": bool(node.get("agy_origin", False)),
        "name": node.get("display_name") or node.get("nickname") or node.get("title") or "Codex Agent",
        "role": node.get("role") or ("Parent Coordinator" if depth == 0 else "Subagent"),
        "depth": depth,
        "status": status,
        "modelPill": f"{model_raw} · {effort}",
        "modelKind": "codex",
        "fast": fast,
        "usedTokens": fmt_k(used),
        "maxTokens": fmt_k(max_t),
        "percent": pct,
        "totalChildrenCount": total_children,
        "detailLeft": detail_left,
        "detailRight": detail_right,
        "actionType": "copy",
        "actionCmd": f"codex resume {tid}",
        "actionLabel": "복사",
        "children": children,
    }


WIDGET_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    @font-face{font-family:D2CodingLigature;src:local("D2CodingLigature Nerd Font"),local("D2CodingLigature Nerd Font Mono"),local("D2Coding ligature"),local("D2Coding"),url(fonts/D2CodingLigature-Regular.ttf) format("truetype")}
    @font-face{font-family:D2CodingLigature;font-weight:700;src:local("D2CodingLigature Nerd Font Bold"),local("D2Coding ligature Bold"),local("D2Coding Bold"),url(fonts/D2CodingLigature-Bold.ttf) format("truetype")}
    :root {
      --mono: D2CodingLigature, "D2CodingLigature Nerd Font", "D2Coding ligature", D2Coding, ui-monospace, SFMono-Regular, Menlo, monospace;
      --bg: var(--background, #0d131f);
      --panel: var(--card, #111827);
      --surface: rgba(128, 145, 175, 0.06);
      --surface-2: rgba(128, 145, 175, 0.14);
      --border: var(--border, rgba(148, 163, 184, 0.22));
      --text: var(--foreground, #e6edf7);
      --muted: var(--muted-foreground, #8b9bb4);
      --blue: #3b82f6;
      --green: #10b981;
      --amber: #f59e0b;
      --red: #f43f5e;
      --purple: #a855f7;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "NanumSquareRound", "Segoe UI", sans-serif;
      background: transparent;
      color: var(--text);
      padding: 3px;
      font-size: 12px;
      line-height: 1.35;
      -webkit-font-smoothing: antialiased;
    }

    .widget-shell {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 7px 9px;
      display: flex;
      flex-direction: column;
      gap: 7px;
      min-width: 0;
      container-type: inline-size;
    }

    /* ── 1. AOH QUOTA PANEL (바깥 링: 잔여 한도 / 안쪽 시계원: 기간 진행율) ── */
    .usage-panel {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 0;
    }
    .usage-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      min-height: 21px;
    }
    .usage-toolbar-left {
      display: flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
      flex: 1;
    }
    .usage-hint {
      font-size: 9.5px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .usage-toolbar-btns {
      display: flex;
      align-items: center;
      gap: 4px;
      flex: none;
    }
    .mini-btn {
      height: 21px;
      padding: 0 7px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: 5px;
      font-size: 9.5px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 3px;
      transition: background 0.12s;
    }
    .mini-btn:hover { background: var(--surface-2); }

    /* 6-column fluid grid matching AOH's --usage-span layout */
    .usage-panel-body {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
      min-width: 0;
    }
    @media (max-width: 520px) {
      .usage-panel-body {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
    }

    fieldset.usage-account {
      min-width: 0;
      min-inline-size: 0;
      margin: 0;
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 3px 4px 5px;
      background: var(--surface);
      grid-column: span 1;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    fieldset.usage-account[data-provider="antigravity"] { --accent: #60a5fa; border-color: rgba(96, 165, 250, 0.45); }
    fieldset.usage-account[data-provider="codex"] { --accent: var(--green); border-color: rgba(16, 185, 129, 0.45); }
    fieldset.usage-account[data-provider="claude"] { --accent: #df934a; border-color: rgba(223, 147, 74, 0.45); }

    legend.usage-account-heading {
      width: auto;
      max-width: 100%;
      padding: 0 5px;
      margin: 0 auto 2px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      font-size: 10px;
      line-height: 1.2;
      white-space: nowrap;
    }
    legend.usage-account-heading > strong {
      color: var(--accent, var(--text));
      font-weight: 700;
      white-space: nowrap;
    }
    .usage-state-dot {
      width: 5px;
      height: 5px;
      border-radius: 99px;
      background: var(--green);
      display: inline-block;
      flex: none;
    }

    .usage-account-windows {
      display: grid;
      grid-template-columns: repeat(var(--cols, 1), minmax(0, 1fr));
      gap: 2px;
      min-width: 0;
    }
    .usage-window {
      min-width: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      gap: 1px;
      text-align: center;
    }
    .usage-gauge-button {
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 1px;
      border: 0;
      background: transparent;
      color: var(--text);
      border-radius: 5px;
      cursor: pointer;
    }
    .usage-gauge-button:hover { background: var(--surface-2); }

    /* AOH Dual-Ring + Inner Clock Pie SVG */
    .usage-donut {
      position: relative;
      width: 48px;
      height: 48px;
      flex: none;
      color: var(--muted);
    }
    .usage-donut.unused { color: var(--blue); }
    .usage-donut.in-use { color: var(--green); }
    .usage-donut.exhausted { color: var(--red); }
    .usage-donut > svg {
      width: 100%;
      height: 100%;
      stroke-width: 2.4;
      stroke-linecap: round;
      fill: none;
    }
    .usage-ring-track { stroke: var(--border); }
    .usage-ring-remaining { stroke: currentColor; }
    .usage-donut.exhausted .usage-ring-track { stroke: var(--red); }
    .usage-donut.exhausted .usage-ring-remaining { display: none; }
    .usage-clock-background { fill: rgba(15, 23, 42, 0.3); stroke: rgba(148, 163, 184, 0.12); stroke-width: 1; }
    .usage-clock-fill { fill: rgba(96, 165, 250, 0.26); stroke: rgba(147, 197, 253, 0.38); stroke-width: 1.1; stroke-linejoin: miter; }

    .usage-donut-center {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      pointer-events: none;
      color: var(--text);
    }
    .usage-donut-center > strong {
      font-size: 10.5px;
      line-height: 1.05;
      font-weight: 800;
      letter-spacing: -0.4px;
      font-variant-numeric: tabular-nums;
    }
    .usage-donut-center > small {
      font-size: 7.5px;
      line-height: 1.05;
      color: var(--muted);
    }
    .usage-center-period { display: none; }

    .usage-window-title {
      font-size: 9px;
      font-weight: 600;
      line-height: 1.2;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 100%;
    }
    .usage-quota-state {
      font-size: 8px;
      line-height: 1.15;
      font-weight: 500;
      white-space: nowrap;
    }
    .usage-quota-state.unused { color: var(--blue); }
    .usage-quota-state.in-use { color: var(--green); }
    .usage-quota-state.exhausted { color: var(--red); }

    .usage-reset {
      font-size: 8px;
      line-height: 1.2;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      border: 0;
      background: transparent;
      cursor: pointer;
      padding: 1px 3px;
      border-radius: 3px;
    }
    .usage-reset:hover { background: var(--surface-2); color: var(--text); }

    /* ── AOH COMPACT DENSITY MODE (1-line work bar, crisp at 340px) ── */
    .usage-provider-inline {
      display: none;
      font-size: 10px;
      font-weight: 700;
      color: var(--accent, var(--text));
      white-space: nowrap;
      align-items: center;
      gap: 3px;
    }
    .usage-panel[data-density="compact"] .usage-panel-body {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      padding: 2px 0;
      overflow-x: auto;
    }
    .usage-panel[data-density="compact"] fieldset.usage-account {
      flex-direction: row;
      align-items: center;
      gap: 3px;
      padding: 2px 5px;
      flex: none;
      border-radius: 6px;
    }
    .usage-panel[data-density="compact"] legend.usage-account-heading { display: none; }
    .usage-panel[data-density="compact"] .usage-provider-inline { display: inline-flex; }
    .usage-panel[data-density="compact"] .usage-account-windows {
      display: flex;
      gap: 2px;
    }
    .usage-panel[data-density="compact"] .usage-window-title,
    .usage-panel[data-density="compact"] .usage-quota-state,
    .usage-panel[data-density="compact"] .usage-reset,
    .usage-panel[data-density="compact"] .usage-center-caption {
      display: none;
    }
    .usage-panel[data-density="compact"] .usage-donut {
      width: 34px;
      height: 34px;
    }
    .usage-panel[data-density="compact"] .usage-donut > svg { stroke-width: 4.5; }
    .usage-panel[data-density="compact"] .usage-donut-center > strong {
      font-size: 9px;
      line-height: 9.5px;
      letter-spacing: -0.4px;
    }
    .usage-panel[data-density="compact"] .usage-center-period {
      display: block;
      font-size: 7px;
      line-height: 8px;
      color: var(--text);
      opacity: 0.85;
      letter-spacing: -0.25px;
    }
    @media (max-width: 420px) {
      .usage-panel[data-density="compact"] .usage-provider-inline span.lbl { display: none; }
      .usage-panel[data-density="compact"] fieldset.usage-account {
        padding: 2px 3px;
        border-left: 2.5px solid var(--accent, var(--border));
      }
    }

    /* ── 2. NARROW-SAFE MULTI-AGENT HIERARCHY TREE ── */
    .tree-section {
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 6px 7px;
      background: var(--surface);
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 0;
    }
    .tree-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding-bottom: 4px;
      margin-bottom: 1px;
      border-bottom: 1px solid var(--border);
      font-size: 10.5px;
      font-weight: 700;
    }
    .tree-header-title {
      display: flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .agent-node-group {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }
    .agent-children {
      display: flex;
      flex-direction: column;
      gap: 1.5px;
      min-width: 0;
    }

    .agent-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      min-height: 22px;
      padding: 2px 5px;
      border-radius: 5px;
      cursor: pointer;
      min-width: 0;
      transition: background 0.12s, box-shadow 0.15s;
    }
    .agent-row:hover { background: var(--surface-2); }
    .agent-row.parent,
    .agent-row.depth-0 {
      background: linear-gradient(90deg, rgba(59, 130, 246, 0.065) 0%, rgba(99, 102, 241, 0.035) 100%);
      margin-top: 0;
      box-shadow: inset 2px 0 0 rgba(96, 165, 250, 0.65);
      transition: background 0.12s, box-shadow 0.15s;
    }
    .agent-row.parent:hover,
    .agent-row.depth-0:hover {
      background: linear-gradient(90deg, rgba(59, 130, 246, 0.11) 0%, rgba(99, 102, 241, 0.06) 100%);
      box-shadow: inset 2px 0 0 rgba(125, 211, 252, 0.9);
    }
    .agent-row.child,
    .agent-row.depth-1,
    .agent-row.depth-2 {
      background: rgba(128, 145, 175, 0.025);
      box-shadow: none !important;
    }

    .agent-left {
      display: flex;
      align-items: center;
      gap: 4.5px;
      min-width: 0;
      flex: 1;
    }
    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      flex: none;
      transition: background-color 1.2s ease, box-shadow 1.2s ease, opacity 1.2s ease;
    }
    .status-dot.running { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .status-dot.idle { background: var(--amber); }
    .status-dot.done { background: #64748b; }

    .agent-name {
      font-size: 10.5px;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }
    .agent-row.depth-1 .agent-name,
    .agent-row.depth-2 .agent-name { font-weight: 500; }

    /* Provider-independent model, context and action columns. */
    .agent-right {
      display: flex;
      align-items: center;
      gap: 5px;
      flex: none;
    }
    .model-pill {
      font-family: var(--mono);
      font-size: 9px;
      padding: 1.5px 5.5px;
      border-radius: 4px;
      border: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.4);
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }
    .model-pill .mp-full,
    .model-pill .mp-short {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .model-pill.argon { color: #60a5fa; border-color: rgba(96, 165, 250, 0.32); }
    .model-pill.flash { color: #38bdf8; border-color: rgba(56, 189, 248, 0.32); }
    .model-pill.codex { color: #34d399; border-color: rgba(52, 211, 153, 0.32); }
    .model-pill.claude { color: var(--claude, #d97757); border-color: rgba(217, 119, 87, 0.35); }
    .tier-bolt {
      flex: none;
      margin-left: 3px;
      color: #fbbf24;
      font-size: 10px;
      line-height: 1;
      letter-spacing: -2px;
      text-shadow: 0 0 6px rgba(251, 191, 36, 0.55);
      font-family: "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif;
    }
    .tier-bolt.tier-ultrafast {
      padding-right: 2px;
      text-shadow: 0 0 8px rgba(245, 158, 11, 0.85);
    }

    .fast-badge {
      display: inline-flex;
      align-items: center;
      gap: 3.5px;
      margin-left: 1px;
      padding: 1.5px 5.5px;
      border-radius: 4px;
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 700;
      line-height: 1.2;
      color: #fbbf24;
      background: rgba(245, 158, 11, 0.14);
      border: 1px solid rgba(245, 158, 11, 0.35);
      white-space: nowrap;
      flex: none;
    }
    .fast-badge svg {
      width: 9px;
      height: 9px;
      flex: none;
    }
    @media (max-width: 390px) {
      .fast-badge span.fast-txt { display: none; }
      .fast-badge { padding: 1.5px 4px; }
    }

    .ctx-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-family: var(--mono);
      font-size: 9px;
      color: var(--muted);
      white-space: nowrap;
      min-width: 60px;
      justify-content: flex-end;
    }
    .ctx-bar {
      width: 24px;
      height: 4px;
      border-radius: 99px;
      background: rgba(148, 163, 184, 0.2);
      overflow: hidden;
      flex: none;
    }
    @media (max-width: 430px) {
      .ctx-bar { display: none; }
      .ctx-pill { min-width: 32px; }
    }
    .ctx-bar-fill { height: 100%; border-radius: 99px; }

    .open-btn {
      height: 18px;
      padding: 0 5px;
      border-radius: 4px;
      font-size: 9px;
      font-weight: 600;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(96, 165, 250, 0.35);
      background: rgba(59, 130, 246, 0.14);
      color: #60a5fa;
      cursor: pointer;
      white-space: nowrap;
      flex: none;
    }
    .open-btn:hover { background: rgba(59, 130, 246, 0.26); }
    .open-btn.codex {
      border-color: rgba(16, 185, 129, 0.35);
      background: rgba(16, 185, 129, 0.14);
      color: #34d399;
    }
    .open-btn.codex:hover { background: rgba(16, 185, 129, 0.26); }

    .agent-detail {
      --border: rgba(148, 163, 184, 0.22);
      display: block;
      max-height: 0;
      opacity: 0;
      transform: translateY(-4px) scale(0.99);
      margin: 0 0 0 20px;
      padding: 0 7px;
      border: 0 solid transparent;
      border-width: 0;
      border-radius: 5px;
      background: rgba(15, 23, 42, 0.5);
      font-family: var(--mono);
      font-size: 9px;
      color: var(--muted);
      line-height: 0;
      overflow: hidden;
      pointer-events: none;
      transition: max-height 0.28s cubic-bezier(0.22, 1, 0.36, 1),
                  opacity 0.24s ease,
                  transform 0.24s cubic-bezier(0.22, 1, 0.36, 1),
                  margin 0.24s ease,
                  padding 0.24s ease,
                  line-height 0.24s ease,
                  border-color 0.24s ease;
    }
    .agent-detail.open {
      max-height: 260px;
      opacity: 1;
      transform: translateY(0) scale(1);
      line-height: 1.4;
      margin: 3px 0 3px 20px;
      padding: 5px 7px;
      border: 1px solid var(--border);
      pointer-events: auto;
      overflow-y: auto;
    }
    .agent-detail-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 4px 8px;
    }

    /* ── 3. BOTTOM MCP & WORKER STRIP ── */
    .mcp-strip {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      padding-top: 1px;
    }
    .mcp-chips {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
    }
    .mcp-chip {
      display: inline-flex;
      align-items: center;
      gap: 3.5px;
      padding: 1.5px 5.5px;
      border-radius: 5px;
      border: 1px solid var(--border);
      background: var(--surface);
      font-size: 9.5px;
      font-weight: 500;
      color: var(--text);
      white-space: nowrap;
    }
    .mcp-chip svg { width: 10px; height: 10px; flex: none; }
    .mcp-chip .dot { width: 5px; height: 5px; border-radius: 99px; background: var(--green); }
    .stamp-label {
      font-size: 8.5px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    /* Tooltip */
    #usage-tooltip {
      position: fixed;
      z-index: 99;
      max-width: 280px;
      background: #1e293b;
      color: #f8fafc;
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 6px;
      padding: 6px 9px;
      font-size: 10px;
      line-height: 1.5;
      white-space: pre-line;
      box-shadow: 0 6px 20px rgba(0,0,0,0.45);
      pointer-events: none;
    }
    .hidden { display: none !important; }

    /* Equal quota cards and aligned agent rows at every density. */
    .usage-panel .usage-panel-body,
    .usage-panel[data-density="compact"] .usage-panel-body {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      grid-auto-rows: 1fr;
      align-items: stretch;
    }
    .usage-panel fieldset.usage-account,
    .usage-panel[data-density="compact"] fieldset.usage-account {
      grid-column: span 1;
      min-height: 108px;
      height: 100%;
      padding: 5px 6px 6px;
      flex-direction: column;
      justify-content: flex-start;
    }
    /* Top-align every donut so a taller caption under one gauge cannot shift its circle. */
    .usage-panel .usage-account-windows,
    .usage-panel[data-density="compact"] .usage-account-windows {
      display: grid;
      grid-template-columns: repeat(var(--cols, 1), minmax(0, 1fr));
      align-items: start;
      justify-items: center;
      align-content: start;
      flex: 1;
      width: 100%;
    }
    .agent-row { min-height: 22px; flex-wrap: nowrap; padding: 2px 5px; }
    @media (min-width: 640px) {
      .agent-row { padding: 2.5px 6px; }
      .tree-section { gap: 3px; }
    }
    .agent-row .agent-left, .agent-row .agent-right { flex-wrap: nowrap; }
    .agent-row .sub-badge, .agent-row .model-pill, .agent-row .ctx-pill,
    .agent-row .state-pill, .agent-row .repo-tag, .agent-row .fast-badge,
    .agent-row .bolt-badge, .agent-row .tab-badge {
      height: 18px; display: inline-flex; align-items: center; flex-shrink: 0;
    }
    .agent-row .open-btn, .agent-row .mini-btn, .agent-row .import-btn,
    .agent-row .dismiss-btn { height: 18px; }
    .agy-arch-mark {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      flex: none;
    }
    .agy-arch-mark svg { display: block; }
    .agent-row .status-dot { width: 8px; height: 8px; flex: none; }
    .agent-row .repo-tag {
      width: 84px; flex: 0 0 84px; text-align: center; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; box-sizing: border-box;
    }
    .agent-row .sub-prefix {
      width: 97px; min-width: 97px; flex: 0 0 97px; box-sizing: border-box;
      display: inline-flex; align-items: center; gap: 4.5px;
      padding-left: 4px; overflow: hidden; white-space: nowrap;
    }
    .agent-row .sub-prefix .tree-elbow {
      font-family: var(--mono);
      font-size: 11.5px; font-weight: 700; color: #818cf8;
      width: 14px; text-align: center; line-height: 1; flex: none;
    }
    .agent-row .sub-prefix .status-dot { width: 7.5px; height: 7.5px; flex: none; }
    .agent-row .sub-prefix .sub-tag {
      font-family: var(--mono);
      font-size: 9px; font-weight: 700; letter-spacing: 0.02em;
      flex: 1 1 auto; min-width: 0; height: 18px; line-height: 16px;
      padding: 0 5px; border-radius: 4px; box-sizing: border-box;
      display: inline-flex; align-items: center; justify-content: center;
      background: rgba(99, 102, 241, 0.13); color: #a5b4fc;
      border: 1px solid rgba(129, 140, 248, 0.28);
    }
    .agent-row .sub-prefix .sub-tag.bg-tag {
      background: rgba(129, 140, 248, 0.045); color: rgba(165, 180, 252, 0.72);
      border-color: rgba(129, 140, 248, 0.16); font-weight: 600;
    }
    .agent-row .agent-name { flex: 1 1 0%; min-width: 0; }
    .agent-row .model-pill {
      width: 112px; flex: 0 0 112px; display: inline-flex; align-items: center;
      justify-content: center; gap: 3px; box-sizing: border-box;
      overflow: hidden; text-overflow: ellipsis;
    }
    .agent-row .ctx-pill {
      width: 78px; flex: 0 0 78px; display: inline-flex; align-items: center;
      justify-content: flex-end; gap: 4px; font-variant-numeric: tabular-nums;
      box-sizing: border-box;
    }
    .agent-row .ctx-bar { display: inline-block; }
    .agent-row .ctx-bar-fill { display: block; }
    .ctx-vbar {
      width: 4px; height: 12px; border-radius: 2px;
      background: rgba(148, 163, 184, 0.22);
      display: inline-flex; align-items: flex-end; overflow: hidden; flex: none;
    }
    .ctx-vbar .ctx-vbar-fill { width: 100%; border-radius: 2px; }
    .ctx-vbar .ctx-vbar-fill.ok { background: #54c9a0; }
    .ctx-vbar .ctx-vbar-fill.warn { background: #e9b45b; }
    .ctx-vbar .ctx-vbar-fill.crit { background: #ef7a85; }
    .ctx-repeat-ico { width: 11px; height: 11px; flex: none; color: #54c9a0; opacity: 0.95; }
    html.light .ctx-repeat-ico { color: #059669; }
    .ctx-pill.has-repeat-ico {
      display: inline-flex !important;
      align-items: center !important;
      gap: 4px !important;
      justify-content: flex-start !important;
    }
    .agent-row .ctx-pill {
      background: transparent !important; border: none !important; padding: 0 2px !important;
    }
    .ctx-pill.busy .ctx-time-txt, .ctx-pill.running .ctx-time-txt, .ctx-pill.st-busy .ctx-time-txt { color: #60a5fa; font-weight: 700; }
    .ctx-pill.idle-fresh .ctx-time-txt, .ctx-pill.age-green .ctx-time-txt { color: #54c9a0; font-weight: 600; }
    .ctx-pill.age-yellow .ctx-time-txt { color: #e9b45b; font-weight: 600; }
    .ctx-pill.age-orange .ctx-time-txt { color: #f59e0b; font-weight: 600; }
    .ctx-pill.age-red .ctx-time-txt, .ctx-pill.done .ctx-time-txt { color: #94a3b8; font-weight: 500; }
    .ctx-time-txt { transition: color 1.2s ease; }
    @keyframes nodeEnter {
      0% {
        opacity: 0;
        transform: translateY(-4px) scale(0.985);
        max-height: 0;
        margin-top: 0;
        margin-bottom: 0;
        padding-top: 0;
        padding-bottom: 0;
        overflow: hidden;
      }
      60% {
        opacity: 0.85;
      }
      100% {
        opacity: 1;
        transform: translateY(0) scale(1);
        max-height: 800px;
        overflow: visible;
      }
    }
    .tree-node {
      max-height: 800px;
    }
    .tree-node.node-enter {
      animation: nodeEnter 0.28s cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }
    .tree-node.node-exit,
    .tree-node.sub-evaporating {
      opacity: 0 !important;
      transform: translateY(-4px) scale(0.98) !important;
      max-height: 0 !important;
      margin-top: 0 !important;
      margin-bottom: 0 !important;
      padding-top: 0 !important;
      padding-bottom: 0 !important;
      overflow: hidden !important;
      pointer-events: none !important;
      transition: opacity 0.32s cubic-bezier(0.4, 0, 0.2, 1),
                  transform 0.32s cubic-bezier(0.4, 0, 0.2, 1),
                  max-height 0.32s cubic-bezier(0.4, 0, 0.2, 1),
                  margin 0.32s cubic-bezier(0.4, 0, 0.2, 1),
                  padding 0.32s cubic-bezier(0.4, 0, 0.2, 1) !important;
    }
    .row-actions {
      width: 68px; flex: 0 0 68px; display: inline-flex;
      align-items: center; justify-content: flex-end; gap: 4px;
    }
  </style>
</head>
<body>
  <svg width="0" height="0" style="position:absolute"><defs><linearGradient id="agy-arch-grad" x1="4" y1="2" x2="20" y2="21" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#f59e0b"/>
        <stop offset="22%" stop-color="#ef4444"/>
        <stop offset="46%" stop-color="#4ade80"/>
        <stop offset="72%" stop-color="#2584fc"/>
        <stop offset="100%" stop-color="#3b82f6"/>
      </linearGradient>
      <symbol id="ico-agy-logo" viewBox="0 0 24 24">
        <path fill="url(#agy-arch-grad)" d="M12 2.2c-2.6 0-4.3 2.3-5.8 7.2-1.3 4.3-2.5 8.1-4.2 10.1-.6.7-.4 1.7.4 2 .9.3 2.1-.3 3.4-1.8 1.5-1.7 2.7-4.3 3.9-6 0.6-.9 1.4-1.4 2.3-1.4s1.7.5 2.3 1.4c1.2 1.7 2.4 4.3 3.9 6 1.3 1.5 2.5 2.1 3.4 1.8.8-.3 1-1.3.4-2-1.7-2-2.9-5.8-4.2-10.1C16.3 4.5 14.6 2.2 12 2.2z"/>
      </symbol></defs></svg>
  <div class="widget-shell">

    <!-- 1. TOP: AOH DUAL-RING + INNER CLOCK PIE QUOTA PANEL -->
    <div class="usage-panel" id="usage-panel" data-density="expanded">
      <div class="usage-toolbar">
        <div class="usage-toolbar-left">
          <span class="usage-hint">바깥 링: 잔여 한도 · 안쪽 시계원: 기간 진행율 (터치/호버 시 상세)</span>
        </div>
        <div class="usage-toolbar-btns">
          <button type="button" class="mini-btn" id="density-btn" onclick="toggleDensity()">컴팩트 바 전환</button>
        </div>
      </div>

      <div class="usage-panel-body" id="quota-grid"></div>
    </div>

    <!-- 2. MIDDLE: NARROW-SAFE MULTI-AGENT HIERARCHY TREE (Parent -> Subagent -> Sub-subagent) -->
    <div class="tree-section">
      <div class="tree-header">
        <div class="tree-header-title">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>
          </svg>
          <span>에이전트 트리</span>
          <span style="font-weight:400;font-size:9px;color:var(--muted)">· 클릭 시 토큰 상세 / ↗ 열기</span>
        </div>
        <button type="button" class="mini-btn" id="btn-toggle-all" onclick="toggleAllBranches()">전체 접기</button>
      </div>

      <div id="agent-tree-root" class="agent-node-group"></div>
    </div>

    <!-- 3. BOTTOM: COMPACT MCP & WORKER STRIP -->
    <div class="mcp-strip">
      <div class="mcp-chips">
        <span class="mcp-chip">
          <svg viewBox="0 0 24 24" fill="#10b981"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729z"/></svg>
          <span>codex-worker</span><span class="dot"></span>
        </span>
        <span class="mcp-chip">
          <svg viewBox="0 0 24 24" fill="#D97757"><path d="M13.827 3.5h3.676L24 20.5h-3.676l-1.897-4.5H9.597l-1.897 4.5H4L10.5 3.5h3.327zm-3.08 9.94h5.52l-2.76-6.56-2.76 6.56z"/></svg>
          <span>claude-worker</span><span class="dot"></span>
        </span>
        <span class="mcp-chip">
          <svg viewBox="0 0 28 28" fill="none"><rect width="28" height="28" rx="5" fill="currentColor"/><path d="M10.57 15.26C10.57 17.5 9.66 19.38 8.18 21.1h3.45v1.68H6.33v-1.59c1.62-1.83 2.25-3.33 2.25-5.93h1.99z" fill="#121a29"/><path d="M17.43 15.26C17.43 17.5 18.34 19.38 19.82 21.1h-3.45v1.68h5.3v-1.59c-1.62-1.83-2.25-3.33-2.25-5.93h-1.99z" fill="#121a29"/><path d="M10.57 12.74C10.57 10.5 9.66 8.62 8.18 6.9h3.45V5.22H6.33v1.59c1.62 1.83 2.25 3.33 2.25 5.93h1.99z" fill="#121a29"/><path d="M17.43 12.74C17.43 10.5 18.34 8.62 19.82 6.9h-3.45V5.22h5.3v1.59c-1.62 1.83-2.25 3.33-2.25 5.93h-1.99z" fill="#121a29"/></svg>
          <span>Context7</span><span class="dot"></span>
        </span>
        <span class="mcp-chip">
          <svg viewBox="0 0 139 64" fill="currentColor" style="width:13px"><path d="M84.3504 64H48.1695V47.315L32.569 63.9989L0 63.9841V29.9515L28.31 0H67.4439V15.9214L82.6881 0H116.593V26.1874H139V64H99.1163V48.208L84.3504 64Z"/></svg>
          <span>Mobbin</span><span class="dot"></span>
        </span>
        <span class="mcp-chip">
          <svg viewBox="0 0 87.3 78"><path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8H0c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/><path d="M43.65 25 29.9 1.2c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44A9.06 9.06 0 0 0 0 53h27.5z" fill="#00ac47"/><path d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5H59.8l5.85 11.2z" fill="#ea4335"/><path d="M43.65 25 57.4 1.2C56.05.4 54.5 0 52.9 0H34.4c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/><path d="M59.8 53H87.3c0-1.55-.4-3.1-1.2-4.5l-22.5-39c-.8-1.4-1.95-2.5-3.3-3.3L46.55 30z" fill="#ffba00"/><path d="M27.5 53h32.3l-13.25-23-3-5.2z" fill="#2684fc"/></svg>
          <span>Drive</span><span class="dot"></span>
        </span>
      </div>
      <span class="stamp-label" id="updated-stamp"></span>
    </div>

  </div>

  <div id="usage-tooltip" class="hidden"></div>

  <script>
    /*__LIVE_DATA_JS__*/

    function clockPath(progress) {
      if (progress === null || progress <= 0) return '';
      if (progress >= 1) return 'M32 10 A22 22 0 1 1 32 54 A22 22 0 1 1 32 10 Z';
      const angle = 2 * Math.PI * progress;
      const x = (32 + 22 * Math.sin(angle)).toFixed(2);
      const y = (32 - 22 * Math.cos(angle)).toFixed(2);
      return `M32 32 L32 10 A22 22 0 ${progress > 0.5 ? 1 : 0} 1 ${x} ${y} Z`;
    }

    function timeProgress(w) {
      if (!w.windowMinutes || !w.resetsAt) return null;
      const nowSec = Date.now() / 1000;
      return Math.max(0, Math.min(1, 1 - (w.resetsAt - nowSec) / (w.windowMinutes * 60)));
    }

    function quotaState(rem, started = false) {
      if (rem === null || rem === undefined) return { id: 'unknown', label: '미확인' };
      if (rem >= 100) return started ? { id: 'in-use', label: '사용중' } : { id: 'unused', label: '미사용' };
      if (rem <= 0) return { id: 'exhausted', label: '소진 · 대기' };
      return { id: 'in-use', label: '사용중' };
    }

    function compactRemaining(resetsAt, fallback) {
      if (!resetsAt) return fallback || '대기';
      const sec = Math.ceil(resetsAt - Date.now() / 1000);
      if (sec <= 0) return '리셋 확인 중';
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (d > 0) return `${d}일 ${h}시간 후`;
      if (h > 0) return `${h}시간 ${m}분 후`;
      return `${m}분 후`;
    }

    /*__POOL_CAPACITIES_JSON_START__*/
    const POOL_TOKEN_CAPACITIES = {
      agy_gemini_5h: 193000000,
      agy_gemini_7d: 621000000,
      agy_claude_5h: 2120000,
      agy_claude_7d: 3940000,
      claude_5h: 35700000,
      claude_7d: 97830000,
      codex_7d: 31240000
    };
    /*__POOL_CAPACITIES_JSON_END__*/

    function formatTokenKorean(tokens) {
      if (!tokens || isNaN(tokens) || tokens <= 0) return '0';
      if (tokens >= 100000000) {
        const uk = Math.floor(tokens / 100000000);
        const remMan = Math.round((tokens % 100000000) / 10000);
        return remMan > 0 ? `${uk}억 ${remMan.toLocaleString()}만` : `${uk}억`;
      } else if (tokens >= 10000) {
        const man = Math.round(tokens / 10000);
        return `${man.toLocaleString()}만`;
      }
      return tokens.toLocaleString();
    }

    function renderQuotas() {
      const grid = document.getElementById('quota-grid');
      grid.innerHTML = '';
      ACCOUNTS.forEach(acc => {
        const span = Math.max(1, acc.windows.length);
        const fs = document.createElement('fieldset');
        fs.className = 'usage-account';
        fs.dataset.provider = acc.provider;
        fs.style.setProperty('--span', '1');
        fs.style.setProperty('--cols', span);

        const legend = document.createElement('legend');
        legend.className = 'usage-account-heading';
        legend.innerHTML = `<strong>${acc.name}</strong><span class="usage-state-dot" title="정상 연결됨"></span>`;
        fs.appendChild(legend);

        const inlineHead = document.createElement('span');
        inlineHead.className = 'usage-provider-inline';
        inlineHead.innerHTML = `<span class="lbl"><strong>${acc.short}</strong></span>`;
        fs.appendChild(inlineHead);

        const winWrap = document.createElement('div');
        winWrap.className = 'usage-account-windows';

        acc.windows.forEach(w => {
          const st = quotaState(w.remaining, w.started);
          const prog = timeProgress(w);
          const pathD = clockPath(prog);
          const dispPct = w.remaining === 100 ? '100%' : w.remaining === 0 ? '0%' : `${w.remaining.toFixed(1)}%`;
          const remCountdown = compactRemaining(w.resetsAt, w.resetText);
          const totalTok = w.totalTokens || POOL_TOKEN_CAPACITIES[w.poolKey] || null;
          let tokenLine = '';
          if (totalTok && w.remaining !== null && w.remaining !== undefined) {
            const remTokens = Math.round(totalTok * (w.remaining / 100.0));
            tokenLine = `\n토큰 용량: 잔여 약 ${formatTokenKorean(remTokens)} / 총 ${formatTokenKorean(totalTok)} 토큰`;
          }
          const tip = `${acc.name} · ${w.label}\n바깥 링: 남음 ${dispPct} (${st.label})\n안쪽 시계원: ${prog === null ? '리셋 대기중 (미사용)' : `기간 ${Math.round(prog * 100)}% 경과`}\n리셋: ${remCountdown} (${w.resetText} KST)${tokenLine}`;

          const winEl = document.createElement('div');
          winEl.className = `usage-window ${st.id}`;
          winEl.innerHTML = `
            <button type="button" class="usage-gauge-button" data-tip="${tip.replace(/"/g, '&quot;')}" title="${tip.replace(/"/g, '&quot;')}">
              <div class="usage-donut ${st.id}">
                <svg viewBox="0 0 64 64" aria-hidden="true">
                  <circle cx="32" cy="32" r="22" class="usage-clock-background"></circle>
                  <path class="usage-clock-fill" d="${pathD}"></path>
                  <circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-track"></circle>
                  <circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-remaining" stroke-dasharray="${w.remaining} 100" transform="rotate(-90 32 32)"></circle>
                </svg>
                <div class="usage-donut-center">
                  <strong>${dispPct}</strong>
                  <small class="usage-center-caption">남음</small>
                  <small class="usage-center-period">${w.shortPeriod}</small>
                </div>
              </div>
            </button>
            <strong class="usage-window-title">${w.label}</strong>
            <span class="usage-quota-state ${st.id}">${st.label}</span>
            <button type="button" class="usage-reset" data-tip="${tip.replace(/"/g, '&quot;')}">리셋 ${remCountdown}</button>
          `;
          winEl.querySelectorAll('[data-tip]').forEach(btn => {
            btn.addEventListener('mouseenter', e => showTip(e.currentTarget));
            btn.addEventListener('mouseleave', hideTip);
            btn.addEventListener('click', e => { e.stopPropagation(); showTip(e.currentTarget); });
          });
          winWrap.appendChild(winEl);
        });

        fs.appendChild(winWrap);
        grid.appendChild(fs);
      });
    }

    function providerIconSvg(provider) {
      if (provider === 'antigravity') {
        return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" style="flex:none"><path d="M12 2C12 7.52 7.52 12 2 12C7.52 12 12 16.48 12 22C12 16.48 16.48 12 22 12C16.48 12 12 7.52 12 2Z" fill="#60a5fa"/></svg>`;
      }
      return `<svg width="15" height="15" viewBox="0 0 24 24" fill="#10b981" style="flex:none"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729z"/></svg>`;
    }

    function barColor(pct) {
      if (pct >= 75) return 'var(--red)';
      if (pct >= 45) return 'var(--amber)';
      return 'var(--green)';
    }

    const branchIds = [];

    function renderAgentNode(node, isLast, idxPrefix) {
      const depth = node.depth || 0;
      const hasChildren = Array.isArray(node.children) && node.children.length > 0;
      const detId = `det-${idxPrefix}`;
      const brId = `br-${idxPrefix}`;
      if (hasChildren) branchIds.push(brId);

      const wrap = document.createElement('div');
      wrap.className = 'agent-node-group';

      const row = document.createElement('div');
      row.className = `agent-row depth-${Math.min(2, depth)}`;
      row.onclick = () => toggleDetail(detId);

      const fastHtml = node.serviceTier === 'ultrafast'
        ? '<span class="tier-bolt tier-ultrafast" title="Ultrafast Tier">⚡⚡</span>'
        : (['fast', 'priority'].includes(node.serviceTier)
          ? `<span class="tier-bolt" title="Fast / Priority Tier · ${node.serviceTier}">⚡</span>` : '');

      const pctVal = Number(node.percent || 0).toFixed(1);
      const actionHtml = node.actionType === 'link'
        ? `<a href="${node.actionHref}" target="_top" onclick="event.stopPropagation()" class="open-btn" title="클릭하여 대화창 바로 열기">${node.actionLabel}</a>`
        : `<button type="button" onclick="event.stopPropagation(); copyCmd('${node.actionCmd}', this)" class="open-btn codex" title="${node.actionCmd} 복사">${node.actionLabel}</button>`;

      row.innerHTML = `
        <div class="agent-left">
          <span class="status-dot ${node.status}" title="${node.status.toUpperCase()}"></span>
          <span class="repo-tag">${(node.repo || 'dev').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]))}</span>
          ${providerIconSvg(node.provider)}
          <span class="agent-name" title="${(node.name || '').replace(/"/g, '&quot;')}">${node.name}</span>
        </div>
        <div class="agent-right">
          <span class="model-pill ${node.modelKind}">${node.modelPill}${fastHtml}</span>
          <span class="ctx-pill" title="${node.usedTokens} / ${node.maxTokens} (${pctVal}%)">
            <span class="ctx-bar"><span class="ctx-bar-fill" style="width:${Math.min(100, pctVal)}%;background:${barColor(pctVal)}"></span></span>
            ${node.usedTokens}
          </span>
          <span class="row-actions">${actionHtml}</span>
        </div>
      `;
      wrap.appendChild(row);

      const det = document.createElement('div');
      det.id = detId;
      det.className = 'agent-detail';
      det.innerHTML = `
        <div class="agent-detail-row">
          <span>${node.detailLeft}</span>
          <span>${node.detailRight}</span>
        </div>
      `;
      wrap.appendChild(det);

      if (hasChildren) {
        const chContainer = document.createElement('div');
        chContainer.id = brId;
        chContainer.className = allCollapsed ? 'tree-children hidden' : 'tree-children';
        node.children.forEach((child, i) => {
          const childLast = i === node.children.length - 1;
          chContainer.appendChild(renderAgentNode(child, childLast, `${idxPrefix}-${i}`));
        });
        wrap.appendChild(chContainer);
      }

      return wrap;
    }

    function renderTrees() {
      const root = document.getElementById('agent-tree-root');
      root.innerHTML = '';
      branchIds.length = 0;
      AGENT_TREES.forEach((tree, i) => {
        root.appendChild(renderAgentNode(tree, i === AGENT_TREES.length - 1, `r${i}`));
      });
      const stamp = document.getElementById('updated-stamp');
      if (stamp && typeof UPDATED_AT_KST === 'string') {
        stamp.textContent = UPDATED_AT_KST;
      }
    }

    const tooltip = document.getElementById('usage-tooltip');
    function showTip(anchor) {
      tooltip.textContent = anchor.dataset.tip || '';
      tooltip.classList.remove('hidden');
      const r = anchor.getBoundingClientRect();
      const left = Math.max(6, Math.min(window.innerWidth - 260, r.left + r.width / 2 - 125));
      const top = r.bottom + 6 + 90 > window.innerHeight ? Math.max(6, r.top - 84) : r.bottom + 6;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
    function hideTip() { tooltip.classList.add('hidden'); }
    document.addEventListener('click', hideTip);

    function toggleDensity() {
      const panel = document.getElementById('usage-panel');
      const btn = document.getElementById('density-btn');
      const next = panel.dataset.density === 'compact' ? 'expanded' : 'compact';
      panel.dataset.density = next;
      btn.textContent = next === 'compact' ? '확장 게이지 전환' : '컴팩트 바 전환';
    }

    let allCollapsed = false;
    try { allCollapsed = localStorage.getItem('agy_all_collapsed_v1') === 'true'; } catch (_) {}

    function toggleAllBranches() {
      allCollapsed = !allCollapsed;
      branchIds.forEach(bId => {
        const el = document.getElementById(bId);
        if (el) el.classList.toggle('hidden', allCollapsed);
      });
      try { localStorage.setItem('agy_all_collapsed_v1', String(allCollapsed)); } catch (_) {}
      document.getElementById('btn-toggle-all').textContent = allCollapsed ? '전체 펴기' : '전체 접기';
    }

    function toggleDetail(id) {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('open');
    }

    function copyCmd(text, btn) {
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      const prev = btn.textContent;
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = prev; }, 1000);
    }

    let openSessionGeneration = 0;
    async function openSession(e, nodeId, cid, label, repo) {
      if (e) { e.stopPropagation(); e.preventDefault(); }
      const button = e?.currentTarget;
      const generation = ++openSessionGeneration;

      const fleetEmbedded = window.parent !== window && /^\/(?:orca\/[^/]+-maestro|maestro\/[^/]+)\//.test(location.pathname);
      if (fleetEmbedded) {
        window.parent.postMessage({ type: 'maestro-open-antigravity', nodeId, cascadeId: cid, repo }, window.location.origin);
        return;
      }

      const text = cid ? `conversation://${cid}` : (nodeId || '');
      if (text && navigator.clipboard) navigator.clipboard.writeText(text);
      const bridge = location.protocol.startsWith('http') && location.host
        ? location.origin + location.pathname.replace(/\/[^/]*$/, '') : 'http://127.0.0.1:18765';
      try {
        const r = await fetch(bridge + '/api/open-session', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nodeId, cascadeId: cid, label, repo, view: fleetEmbedded ? 'chat' : 'terminal' })
        });
        const j = await r.json();
        if (generation !== openSessionGeneration) return;
        if (!r.ok || !j?.ok) throw new Error('session unavailable');
        if (j.target === 'antigravity') {
          if (fleetEmbedded && j.antigravityTarget) {
            window.parent.postMessage({ type: 'maestro-open-antigravity', nodeId, cascadeId: cid, repo }, window.location.origin);
            return;
          }
          if (j.conversationUri && navigator.clipboard) navigator.clipboard.writeText(j.conversationUri);
          if (button) button.title = j.message || 'Antigravity 창 열기를 요청했습니다';
          return;
        }
        if (fleetEmbedded) {
          try { window.parent.postMessage({ type: 'maestro-open-antigravity', nodeId, cascadeId: cid, repo }, window.location.origin); } catch (_) {}
        }
        return;
      } catch (_) {}
      if (generation === openSessionGeneration && button) {
        button.textContent = '!';
        button.title = '세션을 열지 못했습니다';
      }
    }

    renderQuotas();
    renderTrees();
    document.getElementById('btn-toggle-all').textContent = allCollapsed ? '전체 펴기' : '전체 접기';
  </script>
</body>
</html>
"""


def remaining_pct(rem, used=None) -> float:
    """Remaining % for gauges. In-use pools never show 100%: values < 100 floor to 0.1 and cap at 99.9."""
    try:
        r = float(rem)
    except (TypeError, ValueError):
        return 100.0
    in_use = used is not None and float(used or 0) > 0
    if r >= 100.0 and not in_use:
        return 100.0
    if r <= 0.0:
        return 0.0
    return min(math.floor(r * 10) / 10, 99.9)


POOL_TOKEN_CAPACITIES = {
    # Antigravity Gemini Pool (Gemini 4 Argon / 3.8 Flash)
    "agy_gemini_5h": 193_000_000,
    "agy_gemini_7d": 621_000_000,
    # Antigravity Claude & GPT-OSS Pool (Opus 5.5 / Sonnet 5.5)
    "agy_claude_5h": 2_120_000,
    "agy_claude_7d": 3_940_000,
    # Claude Code Team 5x (Claude Code / claude_local)
    "claude_5h": 35_700_000,
    "claude_7d": 97_830_000,
    # OpenAI Codex ProMax (Codex / gpt-6.1-sol): 5h unlimited / omitted
    "codex_7d": 31_240_000,
}


def load_calibrated_pool_token_capacities() -> dict:
    """Load dynamically calibrated pool token capacities from telemetry_calibration.json with baseline fallback."""
    cal_file = os.path.expanduser("~/.gemini/antigravity/telemetry_calibration.json")
    capacities = dict(POOL_TOKEN_CAPACITIES)
    if os.path.exists(cal_file):
        try:
            with open(cal_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            custom = data.get("pool_token_capacities") or {}
            for k, v in custom.items():
                if isinstance(v, (int, float)) and v > 0:
                    capacities[k] = int(round(v))
        except Exception:
            pass
    return capacities


def format_token_korean(tokens: int) -> str:
    """Format token count cleanly into Korean 만 / 억 units (e.g. 193_000_000 -> 1억 9,300만, 2_120_000 -> 212만)."""
    if not tokens or tokens <= 0:
        return "0"
    if tokens >= 100_000_000:
        uk = tokens // 100_000_000
        rem_man = round((tokens % 100_000_000) / 10_000)
        if rem_man > 0:
            return f"{uk}억 {rem_man:,}만"
        return f"{uk}억"
    elif tokens >= 10_000:
        man = round(tokens / 10_000)
        return f"{man:,}만"
    return f"{tokens:,}"


def build_accounts(data: dict) -> list[dict]:
    ag_pools = data.get("antigravity", {}).get("pools", {})
    gem_pool = ag_pools.get("Gemini Pool (Argon / Flash)", {})
    cl_pool = ag_pools.get("Claude & GPT-OSS Pool (Antigravity)", {})
    gem_5h = gem_pool.get("five_hour") or {}
    gem_wk = gem_pool.get("weekly")
    cl_ag_5h = cl_pool.get("five_hour") or {}
    cl_ag_wk = cl_pool.get("weekly")

    codex = data.get("codex_local", {})
    codex_wk = codex.get("weekly") or {}
    codex_5h = codex.get("five_hour")

    claude = data.get("claude_local", {})
    cl_5h = claude.get("five_hour") or {}
    cl_wk = claude.get("weekly") or {}
    cl_fable = cl_wk.get("weekly_scoped")

    cal_caps = load_calibrated_pool_token_capacities()

    def win(label, short, src, minutes, model_icons, period_kind, pool_key, default_rem=100.0):
        total_tokens = cal_caps.get(pool_key)
        item = {
            "label": label,
            "shortPeriod": short,
            "periodKind": period_kind,
            "modelIcon": model_icons[0],
            "modelIcons": model_icons,
            "poolKey": pool_key,
            "remaining": remaining_pct(src.get("remaining_percent", default_rem), src.get("used_percent")),
            "windowMinutes": minutes,
            "resetsAt": parse_kst_to_epoch(src.get("reset_time_kst", "")),
            "resetText": short_reset_label(src.get("reset_time_kst", "")),
            # Reset timer running even though a tiny ping left 100% remaining.
            "started": src.get("started") is True,
        }
        if total_tokens:
            item["totalTokens"] = total_tokens
        return item

    agy_windows = [win("Gemini Pool · 5시간 세션", "5h", gem_5h, 300, ["#ico-gemini"], "5h", "agy_gemini_5h")]
    if isinstance(gem_wk, dict):
        agy_windows.append(win("Gemini Pool · 주간 (7일)", "7d", gem_wk, 10080, ["#ico-gemini"], "7d", "agy_gemini_7d"))
    agy_windows.append(win("Claude | GPT Pool · 5시간 세션", "5h", cl_ag_5h, 300, ["#ico-claude", "#ico-codex"], "5h", "agy_claude_5h"))
    if isinstance(cl_ag_wk, dict):
        agy_windows.append(win("Claude | GPT Pool · 주간 (7일)", "7d", cl_ag_wk, 10080, ["#ico-claude", "#ico-codex"], "7d", "agy_claude_7d"))

    accounts = [{
        "provider": "antigravity",
        "agyOrigin": True,
        "accountIcon": "#ico-agy-logo",
        "name": "Antigravity",
        "short": "AGY",
        "windows": agy_windows,
    }]

    codex_windows = []
    if codex_5h and isinstance(codex_5h, dict):
        codex_windows.append(win("Codex · 5시간 세션", "5h", codex_5h, 300, ["#ico-codex"], "5h", "codex_5h"))
    if codex_wk:
        codex_windows.append(win("Codex · 주간 (7일)", "7d", codex_wk, 10080, ["#ico-codex"], "7d", "codex_7d", 0.0))
    accounts.append({
        "provider": "codex",
        "accountIcon": "#ico-codex",
        "name": "Codex",
        "short": "Codex",
        "windows": codex_windows,
    })

    claude_windows = [
        win("Claude · 5시간 세션", "5h", cl_5h, 300, ["#ico-claude"], "5h", "claude_5h"),
        win("Claude · 주간 (7일)", "7d", cl_wk, 10080, ["#ico-claude"], "7d", "claude_7d", 0.0),
    ]
    if cl_fable and isinstance(cl_fable, dict) and cl_fable.get("is_active") and float(cl_wk.get("remaining_percent", 0) or 0) > 0:
        claude_windows.append(win(f"{cl_fable.get('model', 'Fable')} · 주간 (7일)", "F·7d", cl_fable, 10080, ["#ico-claude"], "7d", "claude_fable_7d"))
    accounts.append({
        "provider": "claude",
        "accountIcon": "#ico-claude",
        "name": "Claude",
        "short": "Claude",
        "windows": claude_windows,
    })
    return accounts


def codex_archive_counts(db_path: str = "~/.codex/state_5.sqlite") -> dict | None:
    """Root threads (no parent edge, not user-archived) older than 7 days, plus all 7d+ threads incl. subagents."""
    db_full = os.path.expanduser(db_path)
    if not os.path.exists(db_full):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_full}?mode=ro", uri=True)
        cut = "CAST(strftime('%s','now','-7 days') AS INTEGER)"
        roots = conn.execute(
            f"SELECT COUNT(*) FROM threads t WHERE t.id NOT IN (SELECT child_thread_id FROM thread_spawn_edges) "
            f"AND t.archived = 0 AND t.updated_at < {cut}"
        ).fetchone()[0]
        total = conn.execute(f"SELECT COUNT(*) FROM threads WHERE archived = 0 AND updated_at < {cut}").fetchone()[0]
        conn.close()
        return {"roots": int(roots), "total": int(total)}
    except Exception:
        return None


ACCOUNTS_BLOCK_RE = re.compile(r"/\*__ACCOUNTS_JSON_START__\*/.*?/\*__ACCOUNTS_JSON_END__\*/", re.S)
SERVER_STATE_BLOCK_RE = re.compile(r"/\*__SERVER_STATE_JSON_START__\*/.*?/\*__SERVER_STATE_JSON_END__\*/", re.S)
QUOTA_GRID_BLOCK_RE = re.compile(r"<!--__QUOTA_GRID_HTML_START__-->.*?<!--__QUOTA_GRID_HTML_END__-->", re.S)
ACTIVE_TREE_BLOCK_RE = re.compile(r"<!--__ACTIVE_TREE_HTML_START__-->.*?<!--__ACTIVE_TREE_HTML_END__-->", re.S)
CLOSED_TREE_BLOCK_RE = re.compile(r"<!--__CLOSED_TREE_HTML_START__-->.*?<!--__CLOSED_TREE_HTML_END__-->", re.S)
POOL_CAPACITIES_BLOCK_RE = re.compile(
    r"/\*__POOL_CAPACITIES_JSON_START__\*/.*?/\*__POOL_CAPACITIES_JSON_END__\*/|const\s+POOL_TOKEN_CAPACITIES\s*=\s*\{.*?\};",
    re.S,
)


def _html_esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _load_persisted_widget_state() -> dict:
    st_path = os.path.expanduser("~/.gemini/antigravity/widget_state.json")
    default_st = {
        "dismissed": ["node-cdx-discord"],
        "pinned": [],
        "imported": [],
        "importedCascades": {},
        "closedDays": 7,
        "scope": "current",
        "tab": "active",
        "density": "compact",
        "allCollapsed": False,
        "collapsedBranches": [],
        "openDetails": [],
    }
    if os.path.exists(st_path):
        try:
            with open(st_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                default_st.update(loaded)
        except Exception:
            pass
    return default_st


def _age_days_from_iso(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        clean = re.sub(r"\.\d+Z$", "Z", iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        diff = datetime.now(dt.tzinfo) - dt
        return max(0, int(diff.total_seconds() // 86400))
    except Exception:
        return 0


def _age_sec_from_iso(iso_str: str) -> float:
    if not iso_str:
        return 999999.0
    try:
        clean = re.sub(r"\.\d+Z$", "Z", iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        diff = datetime.now(dt.tzinfo) - dt
        return max(0.0, float(diff.total_seconds()))
    except Exception:
        return 999999.0


def _age_label_from_days(days: int, updated_ts: int | None = None) -> str:
    if updated_ts:
        sec = max(0, int(datetime.now().timestamp() - updated_ts))
        if sec < 3600:
            return f"{max(1, sec // 60)}분 전"
        if sec < 86400:
            return f"{sec // 3600}시간 전"
    if days <= 0:
        return "오늘"
    return f"{days}일 전"


ORCA_WORKTREE_ROOTS = ("~/orca/workspaces/", "~/.config/orca/worktrees/")


def _orca_worktree(*paths: str | None) -> tuple[str, str] | None:
    """(repo, worktree) of the first path inside an Orca worktree. Accepts ORCA_WORKTREE_ID
    ("<uuid>::<path>") and cwds below the worktree root."""
    for path in paths:
        path = (path or "").rsplit("::", 1)[-1]
        for root in ORCA_WORKTREE_ROOTS:
            root = os.path.expanduser(root)
            parts = path[len(root):].split("/") if path.startswith(root) else []
            if len(parts) >= 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
    return None


def _repo_from_uris_or_cwd(workspaces: list[str] | None = None, cwd: str | None = None) -> tuple[str, str]:
    """Returns (repo_short_name, scope: 'current' | 'external')."""
    path = ""
    if workspaces:
        path = workspaces[0].replace("file://", "")
    elif cwd:
        path = cwd
    path = path.rstrip("/")
    if not path:
        return "dev", "current"
    base = os.path.basename(path) or "dev"
    if path in (os.path.expanduser("~/dev"), os.path.expanduser("~")) or "/.gemini/" in path:
        return "dev", "current"
    # Orca worktrees are the user's own repos too; the badge is the repo alone because the
    # worktree is the folder row its sessions are grouped under.
    orca = _orca_worktree(path)
    if orca:
        return orca[0], "current"
    if path.startswith(tuple(os.path.expanduser(p) for p in ("~/dev/", "~/orca/", "~/.config/orca/worktrees/"))):
        return base, "current"
    return base, "external"


def _agy_badge_html(is_agy: bool = True) -> str:
    return '<span class="agy-arch-mark" title="Google Antigravity (AGY) 생성 에이전트"><svg width="13" height="13"><use href="#ico-agy-logo"/></svg></span>' if is_agy else ""


def _effort_gauge_svg(effort_raw: str) -> str:
    """Render the v6 5-stage effort speedometer (Low 25% < Med 50% < High 75% < Max 86% < Ultra 95%)."""
    e = (effort_raw or "high").strip().lower()
    if e in ("low", "minimal"):
        lvl, lbl = "low", "Low 25% (경량 추론 · 하늘색)"
    elif e in ("med", "medium"):
        lvl, lbl = "med", "Medium 50% (표준 추론 · 에메랄드)"
    elif e == "max":
        lvl, lbl = "max", "Max 86% (최대 추론 · 레드라인)"
    elif e in ("ultra", "xhigh"):
        lvl, lbl = "ultra", "Ultra 95% (오버드라이브 · 마젠타✦✦)"
    else:
        lvl, lbl = "high", "High 75% (고난도 추론 · 앰버)"
    return (
        f'<svg class="eff-gauge eff-{lvl}" width="13.5" height="13.5" aria-label="Effort {lvl}">'
        f'<title>추론 강도 (Effort): {lbl}</title><use href="#ico-eff-{lvl}"/></svg>'
    )


FAST_SERVICE_TIERS = ("fast", "priority", "ultrafast")
_TIER_RANK = {"default": 0, "fast": 1, "priority": 1, "ultrafast": 2}
_MODEL_TIER_RE = re.compile(r"\(\s*[\w.-]+\s*,\s*(default|standard|fast|priority|ultrafast)\s*\)", re.I)


def resolve_speed_tier(*tiers, model: str = "", pool: str = "") -> str:
    """Canonical speed tier (default|fast|priority|ultrafast) from every source a row has.

    Explicit sources (registry tier, rollout service_tier, "model (effort, tier)" labels) win;
    the fastest explicit value is kept because a rollout's missing tier falls back to
    "default". Model/pool names are only consulted when nothing explicit exists.
    """
    explicit = []
    m_tier = _MODEL_TIER_RE.search(model or "")
    for raw in (*tiers, m_tier.group(1) if m_tier else None):
        t = str(raw or "").strip().lower()
        t = "default" if t == "standard" else t
        if t in _TIER_RANK:
            explicit.append(t)
    if explicit:
        return max(explicit, key=lambda t: _TIER_RANK[t])
    m, p = (model or "").lower(), (pool or "").lower()
    if "ultrafast" in m or "ultrafast" in p:
        return "ultrafast"
    if "priority" in m:
        return "priority"
    if re.search(r"(?<![a-z])fast(?![a-z])", f"{m} {p}"):
        return "fast"
    return "default"


def _tier_bolt_html(tier: str, provider: str = "") -> str:
    """Amber lightning next to the effort gauge: ⚡ fast/priority, ⚡⚡ ultrafast."""
    source = {"codex": "Codex service_tier", "claude": "Claude speed",
              "antigravity": "Antigravity Flash"}.get(provider, "tier")
    if tier == "ultrafast":
        return (f'<span class="tier-bolt tier-ultrafast" title="Ultrafast Tier · {source}: ultrafast" '
                f'aria-label="ultrafast">⚡⚡</span>')
    if tier in ("fast", "priority"):
        return (f'<span class="tier-bolt" title="Fast / Priority Tier · {source}: {tier}" '
                f'aria-label="{tier}">⚡</span>')
    return ""


def _pin_btn_html(node_id: str, label: str, is_pinned: bool) -> str:
    act = " active" if is_pinned else ""
    tip = "📌 고정 해제 (완료 정리 대상에 포함)" if is_pinned else "📌 고정 핀 (완료 정리 시에도 지워지지 않고 유지)"
    return (
        f'<button type="button" class="icon-act-btn pin-btn{act}" '
        f'onclick="event.stopPropagation(); togglePin(\'{node_id}\', \'{_html_esc(label[:28])}\', this)" '
        f'title="{tip}" aria-label="고정 핀">'
        f'<svg width="12" height="12" aria-hidden="true"><use href="#ico-pin"/></svg></button>'
    )


def _open_btn_html(node_id: str, cid: str, label: str, repo: str = "") -> str:
    # JS-escape first: the browser decodes &#39; back to a quote before the onclick runs.
    esc_label, esc_repo = (_html_esc(v.replace("\\", "\\\\").replace("'", "\\'")) for v in (label[:28], repo))
    return (
        f'<button type="button" class="icon-act-btn open" '
        f'onclick="event.stopPropagation(); openSession(event, \'{node_id}\', \'{cid}\', \'{esc_label}\', \'{esc_repo}\')" '
        f'title="Antigravity에서 열기" aria-label="Antigravity에서 열기">'
        f'<svg width="12" height="12" aria-hidden="true"><use href="#ico-open-ext"/></svg></button>'
    )


def _import_btn_html(node_id: str, cmd: str) -> str:
    return (f'<button type="button" class="icon-act-btn import-btn" onclick="event.stopPropagation(); '
            f'importOrCopySession(\'{node_id}\', \'{cmd}\', this)" title="세션 가져오기">↗</button>')


def _orca_open_or(node_id: str, sid: str, label: str, node: dict, other_btn: str) -> str:
    """The Orca open button for a session living in an Orca pane or worktree, else `other_btn`."""
    cwd = node.get("cwd") or ""
    orca = _orca_worktree(node.get("orca_worktree"), cwd)
    if not (orca or node.get("orca_terminal_handle") or node.get("orca_lead_id")):
        return other_btn
    return _open_btn_html(node_id, sid, label, "/".join(orca) if orca else cwd)


def _provider_icon_and_pill(model_str: str, provider: str = "antigravity", service_tier: str = "default", effort: str = "ultra") -> tuple[str, str, str]:
    """Returns (icon_svg_html, model_pill_html, sub_badge_html) with Speedometer Effort Gauge and 4-A / 3.8-F short codes."""
    if provider == "codex":
        m_full = _MODEL_TIER_RE.sub("", (model_str or "gpt-6.1-sol").replace("gpt-", "")).strip()
        if "luna" in m_full:
            m_short = "6-luna"
            extra_style = ' style="border-color:rgba(16,185,129,0.4);background:rgba(16,185,129,0.12);color:#10b981;"'
        elif "astra" in m_full:
            m_short = "6-astra"
            extra_style = ""
        elif "6.1" in m_full:
            m_short = "6.1-s"
            extra_style = ""
        else:
            m_short = m_full[:5]
            extra_style = ""
        eff_svg = _effort_gauge_svg(effort or ("low" if "luna" in m_full else "high"))
        service_tier = resolve_speed_tier(service_tier, model=model_str or "")
        bolt_svg = _tier_bolt_html(service_tier, "codex")
        return (
            '<svg class="brand-icon" width="15" height="15" style="color:var(--green)"><use href="#ico-codex"/></svg>',
            f'<span class="model-pill codex"{extra_style} title="Codex {_html_esc(m_full)} · effort: {_html_esc(effort)} · tier: {_html_esc(service_tier)}">'
            f'<span class="mp-full">{_html_esc(m_full)}</span>'
            f'<span class="mp-short">{_html_esc(m_short)}</span>'
            f'{eff_svg}{bolt_svg}</span>',
            "",
        )
    if provider == "claude":
        m_raw = (model_str or "claude-opus-5-5").lower()
        # Callers default to "default"; only a real tier may outrank the model name (claude-opus-5-fast).
        service_tier = resolve_speed_tier(None if service_tier == "default" else service_tier,
                                          model=model_str or "")
        is_fast = service_tier in FAST_SERVICE_TIERS
        is_ultra = (effort in ("ultra", "max")) or ("ultra" in m_raw)
        if "opus" in m_raw:
            if is_ultra and is_fast:
                m_full = "Opus 5.5 Ultra Fast"
                m_short = "O-5.5UF"
            elif is_ultra:
                m_full = "Opus 5.5 Ultra"
                m_short = "O-5.5U"
            elif is_fast:
                m_full = "Opus 5.5 Fast"
                m_short = "O-5.5F"
            else:
                m_full = "Opus 5.5"
                m_short = "O-5.5"
        elif "sonnet" in m_raw:
            m_full = "Sonnet 5.5"
            m_short = "S-5.5"
        else:
            m_full = model_str.replace("claude-", "")
            m_short = m_full[:6]
        eff_svg = _effort_gauge_svg("max" if is_ultra else (effort or "high"))
        bolt_svg = _tier_bolt_html(service_tier, "claude")
        return (
            '<svg class="brand-icon" width="15" height="15" style="color:var(--claude, #d97757)"><use href="#ico-claude"/></svg>',
            f'<span class="model-pill claude" title="Claude {_html_esc(m_full)} · effort: {_html_esc(effort)} · tier: {_html_esc(service_tier)}">'
            f'<span class="mp-full">{_html_esc(m_full)}</span>'
            f'<span class="mp-short">{_html_esc(m_short)}</span>'
            f'{eff_svg}{bolt_svg}</span>',
            "",
        )
    m = model_str or "Gemini 4 Argon (High)"
    m_info = normalize_agy_model(m)
    eff_svg = _effort_gauge_svg(m_info.get("effort", "high"))
    full_lbl = m_info.get("full", m_info["pill"])
    short_lbl = m_info.get("short", full_lbl[:5])
    kind = m_info.get("kind", "argon")
    brand_ico = "#ico-agy-claude" if kind == "claude" else "#ico-agy-gemini"
    long_cls = " is-long" if len(full_lbl) >= 14 else ""
    agy_tier = resolve_speed_tier(service_tier, "fast" if m_info.get("fast") else None, model=m)
    return (
        f'<svg class="brand-icon" width="15" height="15"><use href="{brand_ico}"/></svg>',
        f'<span class="model-pill {kind}{long_cls}" title="{_html_esc(m_info["pill"])} · tier: {agy_tier}">'
        f'<span class="mp-full">{_html_esc(full_lbl)}</span>'
        f'<span class="mp-short">{_html_esc(short_lbl)}</span>'
        f'{eff_svg}{_tier_bolt_html(agy_tier, "antigravity")}</span>',
        "",
    )


def _parse_any_ts(val) -> float | None:
    """Parse a finite numeric epoch (seconds, or milliseconds if absurdly large) or an ISO8601 string (incl. trailing 'Z') into epoch seconds."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        if not math.isfinite(f) or f <= 0:
            return None
        return f / 1000.0 if f > 1e11 else f
    if not isinstance(val, str):
        return None
    txt = val.strip()
    if not txt:
        return None
    try:
        f = float(txt)
    except ValueError:
        f = None
    if f is not None:
        if not math.isfinite(f) or f <= 0:
            return None
        return f / 1000.0 if f > 1e11 else f
    iso = txt[:-1] + "+00:00" if txt[-1] in "zZ" else txt
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    # Normalise odd fractional-second widths (e.g. 7-9 digits) that fromisoformat may reject.
    m = re.match(r"^(.*?[T ]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", iso)
    if m:
        try:
            return datetime.fromisoformat(f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}").timestamp()
        except ValueError:
            return None
    return None


_ROW_START_KEYS = ("turn_start_ts", "started_at", "start_time", "created_at", "created_time")
_ROW_END_KEYS = ("turn_end_ts", "completed_at", "end_time", "updated_at", "last_modified_time")


def _fmt_dur_compact(sec) -> str:
    sec = max(1, int(sec))
    if sec >= 3600:
        return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec}s"


def _node_turn_timing(node: dict | None, is_running: bool = False):
    """Resolve turn metadata, then bounded log tails, then guarded legacy times."""
    node = node if isinstance(node, dict) else {}
    now = datetime.now().timestamp()
    start = next((ts for key in _ROW_START_KEYS
                  if (ts := _parse_any_ts(node.get(key))) is not None), None)
    end = next((ts for key in _ROW_END_KEYS
                if (ts := _parse_any_ts(node.get(key))) is not None), None)
    value = node.get("turn_duration_sec")
    try:
        duration = float(value) if not isinstance(value, bool) else None
        if duration is not None and (not math.isfinite(duration) or duration < 0):
            duration = None
    except (TypeError, ValueError):
        duration = None
    verified = duration is not None
    if verified and _parse_any_ts(node.get("turn_start_ts")) is None and end is not None:
        start = end - duration
    if not verified or (is_running and start is not None and now - start > 7200):
        # Old cached snapshots may lack turn fields. Read the actual logs directly.
        from check_all_usage import _extract_agy_turn_timing, _extract_turn_timing, _jsonl_tail
        timing = {}
        if node.get("rollout_path"):
            timing = _extract_turn_timing(_jsonl_tail(node["rollout_path"], 250000))
        elif node.get("conversation_id"):
            timing = _extract_agy_turn_timing(node["conversation_id"], None, None)
        if timing.get("turn_duration_sec") is not None:
            start = timing["turn_start_ts"]
            end = timing["turn_end_ts"]
            duration = timing["turn_duration_sec"]
            verified = True
    if duration is None and start is not None and end is not None:
        duration = max(1, int(end - start))
        if duration > 7200:
            # No turn evidence: never count multi-day room idle time as work.
            duration = 7200
            start = end - duration
    if is_running:
        ref = start if start is not None else end if end is not None else now
        if not verified and now - ref > 7200:
            ref = max(end or now, now - 7200)
        duration = max(1, int(now - ref))
    return start, end, max(1, int(duration)) if duration is not None else None


def _fmt_row_work_time(node: dict | None, is_running: bool = False, status_cls: str = "") -> tuple[str, str, str]:
    """Return latest-turn duration; keep the finished clock time in the tooltip."""
    _, end, duration = _node_turn_timing(node, is_running)
    if is_running:
        label = "⏱ " + _fmt_dur_compact(duration or 1)
        return label, status_cls or "busy", f"작업중 경과시간: {label.removeprefix('⏱ ')}"
    label = "✓ " + _fmt_dur_compact(duration) if duration is not None else "✓ 완료"
    tip = "작업 완료"
    if end is not None:
        dt = datetime.fromtimestamp(end)
        day = "오늘" if dt.date() == datetime.now().date() else dt.strftime("%Y-%m-%d")
        tip = f"{day} {dt.strftime('%H:%M:%S')} 완료"
    if duration is not None:
        tip += f" · 소요 {_fmt_dur_compact(duration)}"
    return label, status_cls or "done", tip


def _context_pill_html(used: str, percent: float, color: str, node: dict | None = None, is_running: bool = False, status_cls: str = "") -> str:
    """Render a vertical context meter (no token numbers) + running or completed turn duration."""
    pct = float(percent or 0.0)
    if pct >= 80.0:
        lvl_cls, lvl_ko = "crit", "높음"
    elif pct >= 45.0:
        lvl_cls, lvl_ko = "warn", "보통"
    else:
        lvl_cls, lvl_ko = "ok", "여유"
    start, end, duration = _node_turn_timing(node, is_running)
    t_lbl, t_cls, t_tip = _fmt_row_work_time(node, is_running, status_cls)
    full_tip = f"{t_tip} · 컨텍스트 {lvl_ko}: {_html_esc(used)} ({pct:.1f}%)"
    fill_h = max(18, min(100, int(round(pct))))
    ctx_meter = (
        f'<span class="ctx-vbar" aria-hidden="true">'
        f'<span class="ctx-vbar-fill {lvl_cls}" style="height:{fill_h}%"></span>'
        f'</span>'
    )

    time_attrs = []
    if is_running:
        if start is not None:
            time_attrs.append(f'data-start-ts="{int(start)}"')
            dt_start = datetime.fromtimestamp(start, tz=KST)
            time_attrs.append(f'data-clock-label="{_html_esc("▶ " + dt_start.strftime("%H:%M"))}"')
        dur_label = _fmt_dur_compact(duration or 1)
        time_attrs.append(f'data-dur-label="{_html_esc(dur_label)}"')
        disp_txt = dur_label
    else:
        dur_label = _fmt_dur_compact(duration) if duration is not None else "✓ 완료"
        time_attrs.append(f'data-dur-label="{_html_esc(dur_label)}"')
        if end is not None:
            dt_end = datetime.fromtimestamp(end, tz=KST)
            now_dt = datetime.now(tz=KST)
            clock_fmt = "%H:%M" if dt_end.date() == now_dt.date() else "%m/%d %H:%M"
            time_attrs.append(f'data-clock-label="{_html_esc("✓ " + dt_end.strftime(clock_fmt))}"')
        else:
            time_attrs.append('data-clock-label="✓ 완료"')
        disp_txt = dur_label

    time_attrs_str = (" " + " ".join(time_attrs)) if time_attrs else ""

    return (
        f'<span class="ctx-pill {t_cls}" title="{_html_esc(full_tip)}">'
        f'{ctx_meter}'
        f'<span class="ctx-time-txt"{time_attrs_str}>{_html_esc(disp_txt)}</span>'
        f'</span>'
    )


RES_MARK_START, RES_MARK_END = "<!--__RES__-->", "<!--__RES_END__-->"
# Display order CPU -> GPU -> RAM -> send (tx) -> receive (rx); (on, hot) thresholds:
# CPU % of all logical CPUs, GPU % of total VRAM, RAM % of system memory, KB/s.
_RES_LEVELS = {"cpu_pct": (0.2, 25.0), "gpu_pct": (0.01, 50.0), "ram_pct": (1.0, 10.0),
               "tx_kbps": (64.0, 10240.0), "rx_kbps": (64.0, 10240.0)}
_RES_CLASSES = {"cpu_pct": "r-cpu", "gpu_pct": "r-gpu", "ram_pct": "r-ram", "tx_kbps": "r-tx", "rx_kbps": "r-rx"}
_RES_TAGS = {"cpu_pct": "CPU", "gpu_pct": "GPU", "ram_pct": "RAM", "tx_kbps": "↑", "rx_kbps": "↓"}
_RES_LABELS = {"cpu_pct": "CPU", "gpu_pct": "GPU", "ram_pct": "RAM", "tx_kbps": "↑ 보냄", "rx_kbps": "↓ 받음"}


def _fmt_res_mb(mb) -> str:
    mb = float(mb or 0)
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def _fmt_res_rate(kbps) -> str:
    kbps = float(kbps or 0)
    if kbps >= 1024:
        return f"{kbps / 1024:.1f}MB/s"
    return f"{kbps:.1f}KB/s" if kbps < 10 else f"{kbps:.0f}KB/s"


def _fmt_res_pct(pct, present: bool = False) -> str:
    """One decimal; a non-zero (or present) value never reads below 0.1%."""
    pct = float(pct or 0)
    if present or pct > 0:
        pct = max(0.1, round(pct, 1))
    return f"{pct:.1f}%"


def _res_cls(field: str, value) -> str:
    """Field color class plus r-on / r-hot activity level ("" level renders dim)."""
    on, hot = _RES_LEVELS[field]
    value = float(value or 0)
    level = " r-hot" if value >= hot else (" r-on" if value >= on else "")
    return _RES_CLASSES[field] + level


def _res_metrics(res: dict) -> list[tuple[str, float, str]]:
    """(field, level value, text) of the non-zero metrics in CPU, GPU, RAM, send, receive order."""
    out = []
    cpu = float(res.get("cpu_pct") or 0)
    if cpu >= 0.1 or float(res.get("cpu_core_pct", cpu) or 0) > 0.04:  # 2.1% of one core / 24 still shows 0.1%
        out.append(("cpu_pct", cpu, _fmt_res_pct(cpu, True)))
    for field, mb_key in (("gpu_pct", "gpu_mb"), ("ram_pct", "ram_mb")):
        mb, pct = float(res.get(mb_key) or 0), res.get(field)
        if mb > 0 or float(pct or 0) > 0:  # without a known total the exact MB is shown instead of a percentage
            out.append((field, float(pct or 0), _fmt_res_pct(pct, True) if pct is not None else _fmt_res_mb(mb)))
    for field in ("tx_kbps", "rx_kbps"):
        rate = float(res.get(field) or 0)
        if rate >= 0.05:
            out.append((field, rate, _fmt_res_rate(rate)))
    return out


def _has_res(res) -> bool:
    return isinstance(res, dict) and bool(res.get("proc_count") or res.get("runtime_shares"))


def _res_active(res) -> bool:
    """Live consumption: RAM alone (an idle shell) does not count."""
    return _has_res(res) and any(field != "ram_pct" for field, _, _ in _res_metrics(res))


def _res_cpu_ram(res) -> str:
    res = res or {}
    return f"CPU {_fmt_res_pct(res.get('cpu_pct'))} · RAM {_fmt_res_pct(res.get('ram_pct'), bool(res.get('ram_mb')))}"


def _res_split_text(total: dict, split) -> str:
    """'합산: ... (메인 본체: ... + 하위 에이전트/잡 합계: ...)' so a main row shows its own share and its children."""
    own, kids = split
    kids_text = f"하위 에이전트/잡 합계: {_res_cpu_ram(kids)}" if kids else "하위 에이전트/잡 없음"
    return f"합산: {_res_cpu_ram(total)} (메인 본체: {_res_cpu_ram(own)} + {kids_text})"


def _res_title(res: dict, rollup: bool = False, split=None) -> str:
    scope = "합산: 메인 + 하위 에이전트·잡·데몬 프로세스 트리" if rollup else "프로세스 트리 (루트 + 모든 자식)"
    cpu = f"CPU {_fmt_res_pct(res.get('cpu_pct'))}"
    if res.get("cpu_core_pct") is not None:
        cpu += f" (코어 기준 {float(res['cpu_core_pct']):.1f}% / {int(res.get('cpu_count') or 1)}스레드)"
    parts = [scope, cpu]
    for field, mb_key, unit in (("gpu_pct", "gpu_mb", "VRAM"), ("ram_pct", "ram_mb", "RSS")):
        mb = float(res.get(mb_key) or 0)
        if field == "ram_pct" or mb > 0:
            pct = res.get(field)
            exact = f"{unit} {mb:.0f}MB"
            parts.append(f"{_RES_LABELS[field]} " + (f"{_fmt_res_pct(pct, mb > 0)} ({exact})" if pct is not None else exact))
    procs = f"프로세스 {int(res.get('proc_count') or 0)}개"
    if res.get("runtime_shares"):
        procs += " + Antigravity 런타임 몫 (language_server · 창)"
    parts += [f"↑ 보냄 {_fmt_res_rate(res.get('tx_kbps'))}", f"↓ 받음 {_fmt_res_rate(res.get('rx_kbps'))}", procs]
    text = " · ".join(parts)
    return f"{_res_split_text(res, split)}\n{text}" if split else text


def _res_tag(field: str) -> str:
    return f'<b class="rl">{_RES_TAGS[field]}</b>'


def _res_pill_html(res, rollup: bool = False, force: bool = False, split=None) -> str:
    """Inline pill "CPU 2.3% GPU 4.1% RAM 0.6% ↑ 70KB/s ↓ 2.5MB/s" (zeros omitted); empty for idle trees unless forced.

    `split` = (main own, children sum) adds the breakdown line to a main row's tooltip.
    """
    if not (_res_active(res) or (force and _has_res(res))):
        return ""
    metrics = _res_metrics(res)
    classes = [_res_cls(field, value) for field, value, _ in metrics]
    cls = "res-pill" + (" rollup" if rollup else "") + ("" if any(" r-" in c for c in classes) else " idle")
    inner = "".join(f'<span class="{c}">{_res_tag(field)}{text}</span>'
                    for c, (field, _, text) in zip(classes, metrics))
    title = _html_esc(_res_title(res, rollup, split)).replace("\n", "&#10;")
    return f'<span class="{cls}" title="{title}">{inner}</span>'


def _res_slot_html(key: str, res, rollup: bool = False, force: bool = False, split=None) -> str:
    """Keyed slot the live patcher refreshes in place; omitted for unattributed trees and, unless
    forced (live main agents, running SUB / JOB rows, which always hold at least RAM), for idle ones."""
    if not (_res_active(res) or (force and _has_res(res))):
        return ""
    return (f'<span class="res-slot" data-res-key="{_html_esc(key)}">'
            f'{RES_MARK_START}{_res_pill_html(res, rollup, force, split)}{RES_MARK_END}</span>')


def _res_table_row(row_cls: str, tag: str, label: str, res) -> str:
    shown = {field: (value, text) for field, value, text in _res_metrics(res)}
    cells = "".join(f'<td class="{_res_cls(field, shown[field][0])}">{shown[field][1]}</td>' if field in shown
                    else '<td>-</td>' for field in _RES_LEVELS)
    return (f'<tr class="{row_cls}"><td title="{_html_esc(label)}"><span class="res-kind">{_html_esc(tag)}</span>'
            f'{_html_esc(label[:90])}</td>{cells}<td>{int(res.get("proc_count") or 0)}</td></tr>')


def _res_board_html(entries: list[dict], proc_table: dict, totals=None) -> str:
    """Section-level rollup line plus the "리소스 상세" breakdown drawer."""
    from check_all_usage import merge_resources
    rows = [e["res"] for e in entries if e.get("res") and _has_res(e["res"].get("rollup"))]
    total = merge_resources(proc_table, [r["rollup"] for r in rows], totals)
    if not total:
        summary, drawer = '<span class="res-meta">귀속된 프로세스 없음</span>', ""
    else:
        table_rows = []
        for row in rows:
            table_rows.append(_res_table_row("main", row["kind"], row["label"], row["rollup"]))
            table_rows.extend(_res_table_row("child", tag, label, res)
                              for tag, label, res in row["children"] if _has_res(res))
        toggle_js = ("var h=document.documentElement;h.classList.toggle('res-open');"
                     "try{localStorage.setItem('agy_res_open_v1',h.classList.contains('res-open')?1:0)}catch(e){}"
                     "if(window.notifyParentResize)notifyParentResize()")
        summary = (f'{_res_pill_html(total, rollup=True, force=True)}'
                   f'<span class="res-meta">에이전트 {len(rows)} · 프로세스 {total["proc_count"]}</span>'
                   f'<button type="button" class="res-toggle" onclick="{toggle_js}" '
                   f'title="에이전트별 CPU · GPU · RAM · ↑ 보냄 · ↓ 받음 · 프로세스 수 (자세히)">리소스 상세</button>')
        drawer = ('<div class="res-drawer"><table class="res-table"><tr><th>에이전트 (메인 합산 → 하위)</th>'
                  '<th>CPU</th><th>GPU</th><th>RAM</th><th>↑ 보냄</th><th>↓ 받음</th><th>프로세스</th></tr>'
                  f'{"".join(table_rows)}</table></div>')
    return (f'<div class="res-board" data-res-key="res-board">{RES_MARK_START}'
            f'<div class="res-head"><b>리소스</b>{summary}</div>{drawer}{RES_MARK_END}</div>')


RES_ASSETS_BLOCK_RE = re.compile(r"<!--__RES_ASSETS_(CSS|JS)_START__-->.*?<!--__RES_ASSETS_\1_END__-->", re.S)
RES_ASSETS_CSS = (
    "<!--__RES_ASSETS_CSS_START__--><style>"
    "body.standalone-web .widget-shell{max-width:clamp(980px,92vw,1380px)}"
    ".res-slot{display:inline-flex;flex:none}"
    ".res-pill{display:inline-flex;align-items:center;gap:6px;height:18px;box-sizing:border-box;padding:0 5px;"
    "border-radius:4px;border:1px solid var(--bdr);white-space:nowrap}"
    ".res-pill,.res-meta,.res-table{color:var(--muted);font:9.5px var(--mono,monospace)}"
    ".res-pill.rollup{border-color:#22d3ee55}.res-pill.idle{opacity:.55}"
    ".res-pill>span{display:inline-flex;align-items:baseline;gap:3px}"
    ".rl{font-weight:700;opacity:.85}"
    ".r-on.r-cpu{color:#22d3ee}.r-on.r-gpu{color:#a78bfa}.r-on.r-ram{color:#34d399}.r-on.r-tx{color:#f472b6}.r-on.r-rx{color:#38bdf8}"
    ".r-hot{color:#fbbf24!important;font-weight:700}html.light :is(.r-on,.r-hot){filter:brightness(.7)}"
    ".res-board{margin:0 0 4px;border:1px solid var(--bdr);border-radius:6px}"
    ".res-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:3px 6px;font-size:10.5px}"
    ".res-toggle{margin-left:auto;height:18px;padding:0 7px;border-radius:4px;border:1px solid var(--bdr);"
    "background:none;color:var(--muted);cursor:pointer;font-size:10px}"
    ".res-toggle:after{content:' ▾'}.res-open .res-toggle:after{content:' ▴'}"
    ".res-drawer{display:none;padding:0 6px 6px;overflow-x:auto}.res-open .res-drawer{display:block}"
    ".res-table{width:100%;border-collapse:collapse}"
    ".res-table :is(th,td){text-align:right;padding:1.5px 6px;white-space:nowrap}"
    ".res-table th{border-bottom:1px solid var(--bdr)}"
    ".res-table :is(th,td):first-child{text-align:left;max-width:520px;overflow:hidden;text-overflow:ellipsis;color:var(--text)}"
    ".res-table .main td{font-weight:700}.res-table .child td:first-child{padding-left:18px}"
    ".res-kind{margin-right:5px;padding:0 4px;border-radius:3px;font-size:8.5px;background:#6366f122;color:#818cf8}"
    "@media (max-width:760px){.agent-row .res-slot{display:none}}"
    "</style><!--__RES_ASSETS_CSS_END__-->"
)
# Resource numbers change every pass: keep them out of the tree signature, then
# patch keyed slots (insert / refresh / drop) after the regular reconcile.
RES_ASSETS_JS = (
    "<!--__RES_ASSETS_JS_START__-->"
    "<script>(function(){"
    "try{if(localStorage.getItem('agy_res_open_v1')==1)document.documentElement.classList.add('res-open')}catch(e){}"
    "var S=new RegExp('<!'+'--__RES__-->[\\\\s\\\\S]*?<!'+'--__RES_END__-->','g'),o=window.smartPatchTree;"
    "function q(c,s){return c.querySelector(s)}"
    "function sync(c,h){if(!c||!h)return;var t=document.createElement('div'),k={};t.innerHTML=h;"
    "t.querySelectorAll('[data-res-key]').forEach(function(n){var key=n.dataset.resKey,"
    "cur=q(c,'[data-res-key=\"'+CSS.escape(key)+'\"]');k[key]=1;"
    "if(cur){if(cur.innerHTML!==n.innerHTML)cur.innerHTML=n.innerHTML;return}"
    "if(key=='res-board')return c.prepend(n);"
    "var r=q(c,'.tree-node[data-node-id=\"'+CSS.escape(key.slice(4))+'\"]>.agent-row .agent-right');if(r)r.prepend(n)});"
    "c.querySelectorAll('[data-res-key]').forEach(function(n){if(!k[n.dataset.resKey])n.remove()})}"
    "if(typeof o=='function'&&!o.res){window.smartPatchTree=function(c,h){"
    "var r=o(c,typeof h=='string'?h.replace(S,''):h);sync(c,h);return r};window.smartPatchTree.res=1}"
    "})()</script><!--__RES_ASSETS_JS_END__-->"
)


def _inject_resource_assets(html: str) -> str:
    """Add the resource pill CSS/JS at render time (idempotent)."""
    html = RES_ASSETS_BLOCK_RE.sub("", html)
    head = html.find("</head>")
    if head >= 0:
        html = html[:head] + RES_ASSETS_CSS + html[head:]
    body = html.rfind("</body>")
    if body >= 0:
        html = html[:body] + RES_ASSETS_JS + html[body:]
    return html


def _status_dot_from_age(is_busy: bool, age_sec: float) -> tuple[str, str]:
    """6-stage status dot: BUSY (blue breathing) -> IDLE fresh (green breathing) -> Static Green -> Yellow -> Orange -> Red."""
    if is_busy:
        return "busy", "🔵 BUSY · 현재 작업 실행 중 (파란색 브리딩)"
    if age_sec <= 1800:
        return "idle-fresh", "🟢 IDLE · 즉시 작업 가능 대기 중 (초록색 브리딩)"
    if age_sec <= 3600:
        return "age-green", "🟢 대기 중 (브리딩 꺼짐 · 30분~1시간 경과)"
    if age_sec <= 10800:
        return "age-yellow", "🟡 숙성 중 (1~3시간 경과)"
    if age_sec <= 43200:
        return "age-orange", "🟠 오래됨 (3~12시간 경과)"
    return "age-red", "🔴 장기 미작업 (12시간 이상 경과 · 7일 후 닫은 세션으로 이동)"


def _sub_status_dot_from_age(is_busy: bool, age_sec: float, slot_idx: int = 0) -> tuple[str, str, int]:
    """SUB rows matching client tickSubRipening: a finished worker shows a static
    completed dot for SUB_IDLE_RETENTION_SEC, then evaporates."""
    if is_busy:
        return "busy", "🔵 SUB 실행 중 (파란색 브리딩)", 0
    eff_age = int(max(0.0, age_sec))
    rem_sec = max(0, SUB_IDLE_RETENTION_SEC - eff_age)
    return "sub-done", f"✓ SUB 완료 ({eff_age}s · {rem_sec}s 후 증발)", eff_age


def _sub_row_state(node: dict, is_busy: bool, age_sec: float, slot_idx: int = 0) -> tuple[str, str, int, str]:
    """(dot, tip, eff_age, row class) of a SUB row. An open Orca teammate pane waits at its
    prompt between turns, so it ages like a main card instead of evaporating."""
    if node.get("orca_pane_open") and not is_busy:
        dot, tip = _status_dot_from_age(False, age_sec)
        return dot, f"Orca 팀원 패인 · {tip}", 0, "tree-node sub-node"
    dot, tip, eff_age = _sub_status_dot_from_age(is_busy, age_sec, slot_idx)
    return dot, tip, eff_age, "tree-node sub-node sub-ripening"


def _sub_prefix_html(
    dot_cls_or_is_last: str | bool = "done",
    tag_lbl_or_dot_cls: str = "SUB",
    dot_tip: str = "",
) -> str:
    """Render an indented tree branch (├─ / └─) + status dot + SUB/DMN/JOB/CRON/WAIT badge matching the exact 97px width of parent status-dot + repo-tag."""
    if isinstance(dot_cls_or_is_last, bool):
        is_last = dot_cls_or_is_last
        dot_cls = tag_lbl_or_dot_cls if tag_lbl_or_dot_cls not in ("SUB", "DMN", "JOB", "CRON", "WAIT") else "done"
        tag_lbl = "SUB"
        elbow = "└─" if is_last else "├─"
    else:
        dot_cls = dot_cls_or_is_last
        tag_lbl = tag_lbl_or_dot_cls
        elbow = "├─"
    is_bg = tag_lbl not in ("SUB", "LEAD")
    tip = dot_tip or ("하위 백그라운드 태스크 (클릭 시 상세·시작 시각 표시)" if is_bg else "하위 서브에이전트 워커 (완료 후 15초 뒤 자동 증발)")
    t_cls = "sub-tag bg-tag" if is_bg else "sub-tag"
    return (
        f'<span class="sub-prefix" title="{_html_esc(tip)}">'
        f'<span class="tree-elbow">{elbow}</span>'
        f'<span class="status-dot {dot_cls}" title="{_html_esc(tip)}"></span>'
        f'<span class="{t_cls}">{_html_esc(tag_lbl)}</span>'
        f'</span>'
    )


def _fmt_cron_short(expr: str) -> tuple[str, str]:
    e = (expr or "").strip().strip('"\'\\').rstrip(",").strip().strip('"\'\\')
    if not e:
        return "반복", "정기 실행 대기"
    if e in ("@hourly", "0 * * * *"):
        return "1h", f"매시간 정각 ({e})"
    if e in ("@daily", "0 0 * * *"):
        return "1d", f"매일 ({e})"
    m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", e)
    if m:
        return f"{m.group(1)}m", f"{m.group(1)}분마다 ({e})"
    parts = e.split()
    if len(parts) == 5:
        if parts[0] == "0" and parts[1] == "0" and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            return "1d", f"매일 ({e})"
        if parts[0].isdigit() and parts[1] == "*" and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            ko_tip = f"매시간 정각 ({e})" if parts[0] == "0" else f"매시 {parts[0]}분 ({e})"
            return "1h", ko_tip
        if parts[0].isdigit() and parts[1].startswith("*/"):
            return f"{parts[1][2:]}h", f"주기: {e}"
    m_hr = re.match(r"^0\s+\*/(\d+)\s+\*\s+\*\s+\*$", e)
    if m_hr:
        return f"{m_hr.group(1)}h", f"{m_hr.group(1)}시간마다 ({e})"
    m_day = re.match(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+\*$", e)
    if m_day:
        h = int(m_day.group(2))
        m_min = int(m_day.group(1))
        if h == 0 and m_min == 0:
            return "1d", f"매일 ({e})"
        hm = f"{h:02d}:{m_min:02d}"
        return f"{hm}", f"매일 {hm} ({e})"
    return f"{e[:8]}", f"주기: {e}"

def _fmt_bg_badge_and_timing(bgt: dict, now_sec: float) -> tuple[str, str, str, str]:
    """Return (left_tag, kind_label, time_col_str, detail_timing_ko) with zero LLM token overhead."""
    kind = bgt.get("kind") or "job"
    created_iso = bgt.get("created_at") or ""
    age_s = max(0, int(_age_sec_from_iso(created_iso))) if created_iso else 0
    start_hm = ""
    if created_iso:
        try:
            dt_utc = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
            dt_kst = dt_utc.astimezone()
            start_hm = dt_kst.strftime("%H:%M")
        except Exception:
            start_hm = ""
    if age_s >= 3600:
        h, m = age_s // 3600, (age_s % 3600) // 60
        el_short = f"{h}h{m:02d}m" if h < 10 and m > 0 else f"{h}h"
        el_ko = f"{h}시간 {m}분" if m > 0 else f"{h}시간"
    elif age_s >= 60:
        el_short = f"{age_s // 60}m"
        el_ko = f"{age_s // 60}분"
    else:
        el_short = f"{age_s}s"
        el_ko = f"{age_s}초"
    start_ko = f"{start_hm} 시작 · {el_ko} 경과" if start_hm else f"{el_ko} 경과"

    if kind == "daemon":
        return "DMN", "Daemon", el_short, f"Daemon (상시 데몬) · {start_ko}"
    if kind == "cron":
        c_short, c_ko = _fmt_cron_short(bgt.get("cron_expr") or "")
        return "CRON", "Cron", c_short, f"Cron (정기 루틴 · {c_ko}) · {start_ko}"
    if kind == "timer":
        desc = bgt.get("desc") or ""
        dur_m = re.search(r"Timer:\s*(\d+)s", desc)
        dur_s = int(dur_m.group(1)) if dur_m else 0
        if dur_s >= 3600:
            dur_short = f"{dur_s // 3600}h"
        elif dur_s >= 60:
            dur_short = f"{dur_s // 60}m"
        elif dur_s > 0:
            dur_short = f"{dur_s}s"
        else:
            dur_short = ""
        w_time = f"{el_short}/{dur_short}" if dur_short else el_short
        w_ko = f"Wait (대기 타이머 · 총 {dur_short}) · {start_ko}" if dur_short else f"Wait (대기 타이머) · {start_ko}"
        return "WAIT", "Wait", w_time, w_ko
    return "JOB", "Job", el_short, f"Job (일회성 작업) · {start_ko}"


def _parse_render_background_tasks(tpath):
    from check_all_usage import BG_TASK_DESC_RE, is_worker_spawn_command, task_log_is_worker_result
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

                if ("stopped due to server restart" in c.lower()
                        and d.get("type") == "SYSTEM_MESSAGE" and d.get("source", "SYSTEM") == "SYSTEM"):
                    event_time = _parse_any_ts(created_iso)
                    mentioned = set(re.findall(r"[\w./-]*task-\d+", c))
                    for tid, tv in tasks.items():
                        started = _parse_any_ts(tv.get("created_at"))
                        if (((tid in mentioned or tv["short_id"] in mentioned) and
                             not (event_time and started and started > event_time)) or
                                (not mentioned and (not event_time or (started and started <= event_time)))):
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
                        if tv["kind"] == "timer" or "exited with code" in c or "terminated" in c:
                            tv["done"] = True
    except Exception:
        return {}

    return tasks


def _extract_active_bg_tasks(cid: str, now_sec: float) -> list[dict]:
    """Use the collector's lifecycle and cron health policy in both views."""
    from check_all_usage import extract_active_bg_tasks
    return extract_active_bg_tasks(cid, now_sec=now_sec)


def _finalize_child_rows(rows: list[str]) -> str:
    if not rows:
        return ""
    out = list(rows)
    out[-1] = out[-1].replace('<span class="tree-elbow">├─</span>', '<span class="tree-elbow">└─</span>', 1)
    return "".join(out)


def _detail_extras_html(provider: str, session_id: str, repo: str = "", is_parent: bool = False) -> str:
    """Render Mod 5 (recent repo handover memory) and Mod 6 (on-demand live log tail viewer) inside .agent-detail."""
    mem_html = ""
    if is_parent and repo:
        try:
            script_dir = os.path.dirname(os.path.realpath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            import maestro_memory
            notes = maestro_memory.get_recent_handover_notes(repo=repo, max_age_seconds=21600, limit=1)
            if notes:
                n = notes[0]
                one_line = ((n.get("summary") or "").splitlines() or [""])[0][:110]
                mem_html = (
                    f'<div class="handover-note-row">'
                    f'<span class="handover-badge">🧠 인계 메모리 ({_html_esc(n.get("time_kst",""))} · {_html_esc(n.get("pool",""))})</span>'
                    f'<span class="handover-text">{_html_esc(one_line)}</span>'
                    f'</div>'
                )
        except Exception:
            pass
    safe_sid = _html_esc(session_id)
    safe_prov = _html_esc(provider)
    log_box_id = f"logbox-{safe_prov}-{safe_sid[:12]}"
    return (
        f'{mem_html}'
        f'<div class="detail-log-toolbar">'
        f'<button type="button" class="mini-btn log-toggle-btn" onclick="event.stopPropagation(); toggleLiveLog(\'{safe_prov}\', \'{safe_sid}\', \'{log_box_id}\', this)">실시간 로그 ▾</button>'
        f'</div>'
        f'<pre class="live-log-box hidden" id="{log_box_id}">로그 불러오는 중...</pre>'
    )


def _merge_registry_workers(nodes: list[dict], provider: str) -> list[dict]:
    """Keep collected usage while joining early registry IDs to final session IDs."""
    merged = []
    id_key = "thread_id" if provider == "codex" else "session_id"
    raw_nodes = [n for n in nodes if isinstance(n, dict) and "error" not in n]
    known_ids = set()
    for n in raw_nodes:
        nid = n.get(id_key) or n.get("session_id") or n.get("thread_id") or n.get("id")
        if nid:
            known_ids.add(nid)
    registry = [{**w, "registry_id": w.get("registry_id") or key,
                 id_key: w.get(id_key) or w.get("session_id") or key,
                 "registry_only": (w.get(id_key) or w.get("session_id") or key) not in known_ids}
                for key, w in _worker_registry().items()
                if isinstance(w, dict) and w.get("provider") == provider]
    for node in registry + raw_nodes:
        node = dict(node)
        node[id_key] = node.get(id_key) or node.get("session_id") or node.get("id")
        if not node[id_key]:
            continue
        aliases = {node.get(k) for k in (id_key, "session_id", "registry_id")} - {None, ""}
        matches = [old for old in merged if aliases.intersection(
            {old.get(k) for k in (id_key, "session_id", "registry_id")} - {None, ""})]
        combined = {}
        for old in matches:
            combined.update(old)
            merged.remove(old)
        canonical = combined.get("session_id") or node.get("session_id")
        combined.update(node)
        if canonical:
            combined[id_key] = canonical
        if provider == "codex" and combined.get("registry_id"):
            combined.setdefault("source", "exec")
            ctx = dict(combined.get("turn_context") or {})
            for key, value in (("model", combined.get("model")), ("reasoning_effort", combined.get("effort")),
                               ("service_tier", combined.get("tier"))):
                if value:
                    ctx.setdefault(key, value)
            # maestro passed -c service_tier explicitly; a rollout tail without settings reports "default".
            ctx["service_tier"] = resolve_speed_tier(ctx.get("service_tier"), combined.get("tier"))
            combined["turn_context"] = ctx
        merged.append(combined)
    return merged


def _merge_live_process_workers(nodes: list[dict], provider: str, processes: list[dict], now_sec: float) -> list[dict]:
    """Join exact live process evidence, synthesizing a row before registry flush."""
    from spawn_evidence import prompt_fragments
    nodes = [dict(n) for n in nodes]
    id_key = "thread_id" if provider == "codex" else "session_id"
    for process in processes:
        # Owned by an Antigravity conversation, or by the Claude/Codex session (e.g. an
        # Orca terminal agent) whose process tree launched it.
        if process.get("provider") != provider or not (
                process.get("parent_conversation_id") or process.get("parent_session_id")):
            continue
        sid = process.get("session_id") or process.get("registry_id")
        exact = [n for n in nodes if (sid and sid in {n.get(id_key), n.get("session_id"), n.get("registry_id")})
                 or (n.get("pid") and str(n["pid"]) == str(process.get("pid")))]
        if not exact and process.get("spec"):
            spec = " ".join(process["spec"].split())
            for n in nodes:
                if sid and n.get(id_key) != sid and not n.get("registry_only"):
                    continue
                if n.get("status") in ("completed", "failed", "timeout", "done", "closed"):
                    continue
                # When a body is available, a shared generated title is insufficient.
                source = {k: n[k] for k in ("spec", "spec_preview") if n.get(k)} or n
                fragments = prompt_fragments(source)
                if fragments and all(" ".join(f.split()) in spec for f in fragments):
                    exact.append(n)
        if len(exact) > 1:
            continue  # Ambiguous prompts never acquire a guessed owner.
        if exact:
            node = exact[0]
            if sid and (node.get("registry_only") or str(node.get(id_key) or "").startswith(f"live-{provider}-")):
                node[id_key] = sid
                if "session_id" in node:
                    node["session_id"] = sid
        else:
            node = {id_key: sid or f"live-{provider}-{process['pid']}",
                    "title": (process.get("spec") or f"{provider} worker").splitlines()[0][:160],
                    "spec": process.get("spec") or "", "cwd": process.get("cwd") or "",
                    "source": "exec", "agy_origin": True, "registry_only": True}
            nodes.append(node)
        node.update(parent_conversation_id=process.get("parent_conversation_id") or "",
                    pid=process["pid"], is_running=True, live_process=True, updated_at=now_sec,
                    status="running", turn_completed=False)
        if process.get("parent_session_id"):
            node["parent_session_id"] = process["parent_session_id"]
        if process.get("tier"):
            # The live argv is ground truth for -c service_tier / --model claude-opus-5-fast.
            node["tier"] = resolve_speed_tier(node.get("tier"), process["tier"])
            if provider == "codex":
                ctx = dict(node.get("turn_context") or {})
                ctx["service_tier"] = resolve_speed_tier(ctx.get("service_tier"), process["tier"])
                node["turn_context"] = ctx
        node.pop("ok", None)
    return nodes


def _maestro_result_ids(content: str) -> set[str]:
    """Recognize actual CLI result objects, not quoted IDs in investigation text."""
    try:
        result, _ = json.JSONDecoder().raw_decode(content.lstrip())
    except ValueError:
        return set()
    if not isinstance(result, dict) or not (result.get("pool") or result.get("provider")):
        return set()
    ids = {result[k] for k in ("session_id", "thread_id") if isinstance(result.get(k), str)}
    ids.update(tid for tid in (result.get("thread_ids") or []) if isinstance(tid, str))
    return ids


def _worker_parent(worker: dict, parents: list[dict], transcripts: dict[str, str], now_sec: float,
                   maestro_activity: dict[str, float] | None = None,
                   launch_owners: dict[str, set[str]] | None = None) -> str | None:
    """Prefer explicit evidence; ties never resolve by list order or repo basename."""
    def identity(n):
        return n.get("conversation_id") or n.get("thread_id") or n.get("session_id")

    ids = {worker.get(k) for k in ("thread_id", "session_id", "registry_id")} - {None, ""}
    candidates = [p for p in parents if identity(p) not in ids]
    direct = worker.get("parent_conversation_id")
    if direct and any(identity(p) == direct for p in candidates):
        return direct
    explicit = [p for p in candidates if identity(p) in
                (worker.get("parent_id"), worker.get("parent_cid"), worker.get("parent_conversation_id"),
                 worker.get("parent_session_id"), worker.get("parent_thread_id"), worker.get("orca_lead_id"))
                or any(ids.intersection({c.get("thread_id"), c.get("session_id"), c.get("registry_id")})
                       for c in (p.get("children") or []))]
    if explicit:
        return identity(explicit[0]) if len(explicit) == 1 else None
    owners = set().union(*((launch_owners or {}).get(wid, set()) for wid in ids))
    launches = [p for p in candidates if identity(p) in owners]
    if launches:
        if len(launches) == 1:
            return identity(launches[0])
        candidates = launches
    from spawn_evidence import prompt_fragments
    fragments = prompt_fragments(worker)
    for by_id in (True, False):
        scores = {}
        for p in candidates:
            evidence = transcripts.get(identity(p), "")
            values = ids if by_id else fragments
            if not by_id:
                evidence = " ".join(evidence.split())
            scores[identity(p)] = sum(
                (value if by_id else " ".join(value.split())) in evidence for value in values)
        best = max(scores.values(), default=0)
        matches = [p for p in candidates if best and scores[identity(p)] == best]
        if matches:
            if len(matches) == 1:
                return identity(matches[0])
            candidates = matches
    return None


def _is_claude_main(node: dict) -> bool:
    """A human-driven Claude Code terminal (Orca pane or shell), not a delegated worker.

    Claude Code records entrypoint "cli" for interactive terminals; `claude -p`
    workers report "sdk-cli". Any spawn evidence keeps a session a worker.
    """
    return (node.get("entrypoint") == "cli" and not node.get("agy_origin") and not node.get("registry_id")
            and not any(node.get(key) for key in ("parent_id", "parent_cid", "parent_conversation_id",
                                                   "parent_session_id", "parent_thread_id", "orca_lead_id")))


def _claude_worker_row(node: dict, live_ids: set[str], now_sec: float) -> str:
    sid = _html_esc(str(node.get("session_id") or node.get("id") or ""))
    model = node.get("model") or "claude-opus-5-5"
    effort = node.get("effort") or node.get("reasoning_effort") or "high"
    pool = node.get("pool") or ""

    is_fallback = bool(node.get("fallback_from"))
    # A Fast→standard fallback reruns on claude-opus-5-5; its stale fast pool must not light the bolt.
    service_tier = "default" if is_fallback else resolve_speed_tier(
        node.get("tier"), node.get("service_tier"), (node.get("turn_context") or {}).get("service_tier"),
        model=model, pool=pool)
    is_fast = service_tier in FAST_SERVICE_TIERS
    is_ultra = (effort in ("ultra", "max")) or ("ultra" in model.lower()) or ("ultra" in pool)

    m_raw = model.lower()
    if "opus" in m_raw:
        if is_ultra and is_fast:
            mp_full = "Opus 5.5 Ultra Fast"
            mp_short = "Opus-UF"
        elif is_ultra:
            mp_full = "Opus 5.5 Ultra"
            mp_short = "Opus-U"
        elif is_fast:
            mp_full = "Opus 5.5 Fast"
            mp_short = "Opus-F"
        else:
            mp_full = "Opus 5.5"
            mp_short = "Opus"
    elif "sonnet" in m_raw:
        mp_full = "Sonnet 5.5"
        mp_short = "Sonnet"
    else:
        mp_full = model.replace("claude-", "")
        mp_short = mp_full[:6]

    is_terminal = node.get("status") in ("completed", "failed", "timeout", "done", "closed") or node.get("ok") is not None
    running = (node.get("session_id") in live_ids or node.get("is_running") is True) and not is_terminal
    age = max(0, now_sec - (_parse_any_ts(node.get("updated_at")) or now_sec))
    dot, tip, eff_age, row_class = _sub_row_state(node, running, age)

    tokens = node.get("token_count") or node.get("usage") or node.get("context_usage") or {}
    total = tokens.get("total_tokens") or tokens.get("used_tokens") or sum(
        tokens.get(k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    cost = node.get("cost_usd", node.get("total_cost_usd", tokens.get("cost_usd", 0))) or 0

    raw_title = node.get("display_name") or node.get("title") or node.get("spec_preview") or node.get("spec") or "Claude worker"
    raw_title = re.sub(r"^🪐\s*", "", raw_title)
    raw_title = re.sub(r"^ultrathink\s+", "", raw_title).strip()
    title_line = (raw_title.splitlines() or ["Claude worker"])[0].strip()
    if not title_line.startswith("✦"):
        title_line = f"✦ {title_line}".strip()
    title = _html_esc(title_line)

    badge = " Ultra Fast" if (is_ultra and is_fast) else (" Ultra" if is_ultra else (" Fast" if is_fast else ""))
    label = _html_esc(f"{mp_full} · {effort}{badge}")
    c_icon, c_pill, _ = _provider_icon_and_pill(model, "claude", service_tier, effort)
    c_node_id = f"node-sub-claude-{sid}"
    pct = float(tokens.get("percent") or 0.0)

    c_ts = int(_parse_any_ts(node.get("updated_at")) or 0)
    scope = _repo_from_uris_or_cwd(None, node.get("cwd") or "")[1]
    return (
        f'<div class="{row_class}" data-scope="{scope}" data-running="{int(running)}" data-node-id="{c_node_id}" data-sub-running="{int(running)}" data-sub-age="{eff_age}" data-idle-age="{int(age)}" data-done-ts="{c_ts}">'
        f'<div class="agent-row child" onclick="toggleDetail(\'det-sub-claude-{sid}\')">'
        f'<div class="agent-left">{_sub_prefix_html(dot, "SUB", tip)}'
        f'{c_icon}'
        f'<span class="agent-name" title="{title}">{title}</span></div><div class="agent-right">'
        f'{_res_slot_html(f"res-{c_node_id}", node.get("resources"), force=running)}'
        f'{c_pill}'
        f'{_context_pill_html(fmt_k(total), pct, "green", node, running, dot)}'
        f'<span class="row-actions">'
        f'{_orca_open_or(c_node_id, sid, title_line.lstrip("✦ "), node, _import_btn_html(c_node_id, f"claude --resume {sid}"))}'
        f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{c_node_id}\', \'{_html_esc(title_line[:24])}\')">✕</button>'
        f'</span>'
        f'</div></div>'
        f'<div class="agent-detail" id="det-sub-claude-{sid}"><div class="agent-detail-row">'
        f'<span>Claude Subagent: {sid} · cwd: {_html_esc(node.get("cwd") or "")} · {label}</span>'
        f'<span>{total} tokens · ${float(cost):.4f}</span></div>'
        f'<div class="agent-detail-row">{_html_esc(json.dumps(tokens, ensure_ascii=False))}</div>'
        f'{_detail_extras_html("claude", sid)}</div></div>'
    )


def _worker_registry() -> dict:
    # Dead worker pids are reaped to "completed" so they never render as running.
    from spawn_registry_reaper import load_registry
    return load_registry("~/.gemini/antigravity/agy_spawned_sessions.json")


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _running_worker_sessions(now_sec: float) -> tuple[set[str], set[str], set[str]]:
    """Resolve actual worker processes to sessions, never every session in a cwd."""
    codex_ids, agy_ids, claude_ids = set(), set(), set()
    workers = _worker_registry()
    for path in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            with open(path) as f:
                session = json.load(f)
            if _pid_alive(session.get("pid")):
                sid = session.get("sessionId") or session.get("session_id")
                if sid:
                    claude_ids.add(sid)
        except (OSError, ValueError, AttributeError):
            pass
    for key, worker in workers.items():
        if isinstance(worker, dict) and _pid_alive(worker.get("pid")):
            if worker.get("status") in ("completed", "failed", "timeout", "done", "closed") or worker.get("ok") is not None:
                continue
            provider = worker.get("provider")
            ids = claude_ids if provider == "claude" else codex_ids if provider == "codex" else agy_ids
            ids.add(worker.get("session_id") or key)
    try:
        processes = subprocess.check_output(
            ["ps", "-eo", "pid=,comm=,etimes="], text=True,
            stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return codex_ids, agy_ids, claude_ids
    registry = None
    for line in processes.splitlines():
        try:
            pid, command, elapsed = line.split()
            if command not in ("codex", "node", "python", "python3", "maestro", "agy-orca", "claude"):
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").strip("\0").split("\0")
            # Node launchers put the executable script in argv[1].
            tool_idx = 1 if os.path.basename(argv[0]) in ("node", "python", "python3") else 0
            tool = os.path.basename(argv[tool_idx])
            args = argv[tool_idx + 1:]
            if tool in ("codex", "codex.js") and args and args[0] in ("exec", "resume"):
                # An exec worker keeps its exact rollout open even during a long tool call.
                for fd in os.scandir(f"/proc/{pid}/fd"):
                    try:
                        match = re.search(r"rollout-.*-([0-9a-f-]{36})\.jsonl$", os.readlink(fd.path))
                        if match:
                            codex_ids.add(match.group(1))
                    except OSError:
                        continue
                # A resume command also identifies the thread before opening its rollout.
                for arg in args[1:]:
                    if re.fullmatch(r"[0-9a-f-]{36}", arg):
                        codex_ids.add(arg)
            elif tool in ("claude", "claude.js", "cli.js") and ("--session-id" in args or any(a.startswith("--session-id=") for a in args) or "-p" in args or "--print" in args):
                if "--session-id" in args:
                    claude_ids.add(args[args.index("--session-id") + 1])
                for arg in args:
                    if arg.startswith("--session-id="):
                        claude_ids.add(arg.split("=", 1)[1])
                for key, worker in workers.items():
                    if isinstance(worker, dict) and str(worker.get("pid")) == pid:
                        claude_ids.add(worker.get("session_id") or key)
            elif tool in ("maestro", "maestro.py", "agy-orca") and args and args[0] == "worker-start":
                pool_idx = args.index("--pool")
                pool = args[pool_idx + 1]
                if pool in ("claude", "claude-fast", "claude-ultra", "opus", "opus-ultra", "opus-5.5-ultra"):
                    cwd = args[args.index("--cwd") + 1] if "--cwd" in args else os.readlink(f"/proc/{pid}/cwd")
                    spec = args[args.index("--spec") + 1] if "--spec" in args else ""
                    matches = [w.get("session_id") or key for key, w in workers.items()
                               if isinstance(w, dict) and w.get("provider") == "claude"
                               and os.path.abspath(w.get("cwd") or ".") == os.path.abspath(cwd)
                               and (spec and (w.get("spec") == spec or spec in (w.get("title") or "")))
                               and now_sec - int(elapsed) - 2 <= float(w.get("updated_at") or 0) <= now_sec]
                    if len(matches) == 1:
                        claude_ids.add(matches[0])
                    continue
                if pool not in ("agy-claude", "agy-sonnet"):
                    continue
                if registry is None:
                    with open(os.path.expanduser("~/.gemini/antigravity/agy_spawned_sessions.json")) as f:
                        registry = json.load(f)
                cwd = args[args.index("--cwd") + 1] if "--cwd" in args else os.readlink(f"/proc/{pid}/cwd")
                started = now_sec - int(elapsed)
                if not isinstance(registry, dict):
                    continue
                for cid, info in registry.items():
                    if (isinstance(info, dict) and info.get("pool") == pool
                            and os.path.abspath(info.get("cwd") or ".") == os.path.abspath(cwd)
                            and started - 2 <= float(info.get("updated_at") or 0) <= started + 30):
                        agy_ids.add(cid)
        except (OSError, ValueError, IndexError, TypeError):
            continue
    return codex_ids, agy_ids, claude_ids


def _codex_rollout_paths(nodes: list[dict]) -> dict[str, str]:
    """Use collected rollout paths, fetching missing paths from older snapshots."""
    paths = {n.get("thread_id"): n["rollout_path"] for n in nodes if n.get("rollout_path")}
    missing = [n["thread_id"] for n in nodes if n.get("thread_id") and n["thread_id"] not in paths]
    if missing:
        try:
            db = os.path.expanduser("~/.codex/state_5.sqlite")
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)) as conn:
                placeholders = ",".join("?" for _ in missing)
                paths.update(conn.execute(
                    f"SELECT id, rollout_path FROM threads WHERE id IN ({placeholders})", missing,
                ).fetchall())
        except sqlite3.Error:
            pass
    return paths


def _codex_worker_is_active(node: dict, live_ids: set[str], paths: dict[str, str], now_sec: float) -> bool:
    if node.get("turn_completed") is True:
        return False
    tid = node.get("thread_id", "")
    if tid and tid in live_ids:
        return True
    path = node.get("rollout_path") or paths.get(tid)
    if path:
        try:
            return 0 <= now_sec - os.path.getmtime(path) <= 45
        except OSError:
            pass
    return False


def _agy_worker_is_active(node: dict, live_ids: set[str]) -> bool:
    if node.get("killed"):
        return False
    if node.get("has_active_bg") or node.get("has_active_cli"):
        return True
    cid = node.get("conversation_id", "")
    is_sub = bool(
        node.get("is_subagent")
        or node.get("parent_conversation_id")
        or (node.get("nesting_depth", 0) > 0)
        or (node.get("role") and "Subagent" in str(node.get("role", "")))
    )
    if is_sub:
        # A planner response can be a progress update while run_command --wait
        # is still blocked on a worker. Transcript age alone cannot end it.
        if any(t.get("kind") != "cron" for t in (node.get("background_tasks") or [])):
            return True
        if cid and any(t.get("kind") != "cron" for t in _extract_active_bg_tasks(cid, time.time())):
            return True
        age_sec = None
        last_step = None
        if cid:
            t_path = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
            if os.path.exists(t_path):
                try:
                    age_sec = max(0.0, time.time() - os.path.getmtime(t_path))
                    try:
                        from spawn_evidence import last_step_state
                        last_step = last_step_state(t_path)
                    except Exception:
                        pass
                except OSError:
                    pass
        if age_sec is None and node.get("last_modified_time"):
            age_sec = _age_sec_from_iso(node.get("last_modified_time") or "")

        # Check if subagent already returned final response without tools
        if last_step and isinstance(last_step, dict):
            step_type = str(last_step.get("type", "")).upper()
            step_status = str(last_step.get("status", "")).upper()
            tool_calls = last_step.get("tool_calls")
            if step_type == "PLANNER_RESPONSE" and step_status == "DONE" and not tool_calls and age_sec is not None and age_sec > 15:
                return False

        if age_sec is not None and age_sec > 300:
            return False

        stale_threshold = 1800 if str(node.get("status") or "").upper() in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING") else 180
        if age_sec is not None and age_sec > stale_threshold:
            return False

    return (str(node.get("status") or "").upper() in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING")
            or node.get("is_running") is True or cid in live_ids)


def _codex_root_activity(node: dict, live_ids: set[str], paths: dict[str, str], now_sec: float) -> tuple[bool, float]:
    """Separate a visible Desktop session from one with current activity."""
    updated_ts = float(node.get("updated_at") or 0)
    rollout_ts = 0.0
    path = node.get("rollout_path") or paths.get(node.get("thread_id", ""))
    if path:
        try:
            rollout_ts = os.path.getmtime(path)
        except OSError:
            pass
    has_running_child = any(
        (child.get("is_running") is True and not child.get("turn_completed"))
        or _codex_worker_is_active(child, live_ids, paths, now_sec)
        for child in (node.get("children") or [])
    )
    is_terminal = (node.get("status") in ("completed", "failed", "timeout", "done", "closed")
                   or node.get("turn_completed") is True
                   or node.get("ok") is not None)
    if is_terminal:
        return has_running_child, max(updated_ts, rollout_ts)
    if node.get("registry_id") and not node.get("live_process"):
        # A maestro registry row is live only while its process is: a recent
        # updated_at or rollout write from a worker that already exited is not.
        from spawn_registry_reaper import entry_process_alive
        ids = {node.get(k) for k in ("thread_id", "session_id", "registry_id")} - {None, ""}
        return (has_running_child or bool(ids & live_ids) or entry_process_alive(node),
                max(updated_ts, rollout_ts))
    is_live_running = (max(0, now_sec - updated_ts) <= 90
                       or (rollout_ts > 0 and max(0, now_sec - rollout_ts) <= 60)
                       or has_running_child
                       or node.get("thread_id", "") in live_ids)
    return is_live_running, max(updated_ts, rollout_ts)


def _main_prefix_html(dot: str, dot_tip: str, repo: str, in_folder: bool, repo_tip: str = "") -> str:
    """Status dot + repo badge of a main card; inside an Orca worktree folder, a LEAD branch prefix."""
    if in_folder:
        return _sub_prefix_html(dot, "LEAD", f"Orca 워크트리의 리드 패인 · {dot_tip}")
    tip = f' title="{_html_esc(repo_tip)}"' if repo_tip else ""
    return (f'<span class="status-dot {dot}" title="{_html_esc(dot_tip)}"></span>'
            f'<span class="repo-tag"{tip}>{_html_esc(repo)}</span>')


def _pill_label(model_pill: str) -> tuple[str, str] | None:
    """(full, short) model names shown by a model pill."""
    m = re.search(r'class="mp-full">([^<]*)</span><span class="mp-short">([^<]*)<', model_pill or "")
    return (html_lib.unescape(m.group(1)), html_lib.unescape(m.group(2))) if m else None


def _orca_folder_html(key: tuple[str, str], members: list[dict], proc_table: dict, proc_res: dict,
                      now_sec: float) -> str:
    """Folder card for one Orca worktree: [repo] 📁 worktree, then its lead cards as LEAD rows."""
    from check_all_usage import merge_resources
    repo, worktree = key
    folder_id = "orca-" + re.sub(r"[^\w-]", "-", f"{repo}-{worktree}")
    busy = any(m.get("busy") for m in members)
    newest = max((m.get("updated_ts") or 0.0) for m in members)
    dot, dot_tip = _status_dot_from_age(busy, max(0.0, now_sec - newest))
    rollup = merge_resources(proc_table, [m["res"].get("rollup") for m in members], proc_res)
    sessions = len(members) + sum(kind == "SUB" for m in members for kind, _, _ in m["res"].get("children") or ())
    models = list(dict.fromkeys(m["model"] for m in members if m.get("model")))
    short = "+".join(name for _, name in models)
    run_attr = ' data-running="1"' if any(m.get("is_live_running") for m in members) else ""
    # Spacers keep the folder's pills in the same columns as its rows (ctx pill = session count).
    right = (f'<span class="model-pill" title="리드 모델: {_html_esc(" + ".join(name for name, _ in models))}">{_html_esc(short)}</span>'
             f'<span class="ctx-pill" title="리드 {len(members)} · 전체 세션 {sessions}">세션 {sessions}</span>'
             f'<span class="row-actions">{_open_btn_html(folder_id, "", worktree, f"{repo}/{worktree}")}</span>')
    return (
        f'<div class="tree-node orca-folder" data-node-id="{folder_id}" data-scope="current"{run_attr}>'
        f'<div class="agent-row parent" onclick="toggleFolder(\'{folder_id}\')">'
        f'<div class="agent-left">'
        f'<span class="status-dot {dot}" title="{_html_esc(dot_tip)}"></span>'
        f'<span class="repo-tag" title="Orca 워크트리 · {_html_esc(repo)}/{_html_esc(worktree)}">{_html_esc(repo)}</span>'
        f'<span class="fold-chev" style="transition:transform .15s">▾</span>'
        f'<span class="agent-name">📁 {_html_esc(worktree)}</span>'
        f'</div>'
        f'<div class="agent-right">{_res_slot_html(f"res-{folder_id}", rollup, rollup=True)}{right}</div>'
        f'</div>'
        f'<div class="tree-children folder-body" id="br-{folder_id}">{_finalize_child_rows([m["html"] for m in members])}</div>'
        f'</div>'
    )


def build_active_and_closed_trees_html(data: dict, state: dict) -> tuple[str, str, int, int]:
    """Builds (active_html, closed_html, active_curr_count, closed_7d_curr_count) from live data."""
    trees_raw = data.get("agent_trees", {})
    ag_list = [n for n in trees_raw.get("antigravity", []) if isinstance(n, dict) and "error" not in n]
    # Accept both nested collector trees and flat trajectories. Parent identity wins
    # over titles, pins and imports, including for descendants of descendants.
    ag_by_cid: dict[str, dict] = {}
    ag_parent_ids: dict[str, str] = {}

    def collect_ag(nodes, enclosing_cid=None):
        for node in nodes:
            cid = node.get("conversation_id")
            if not cid:
                continue
            parent = node.get("parent_id") or node.get("parent_conversation_id") or enclosing_cid
            if parent:
                ag_parent_ids[cid] = parent
            if cid in ag_by_cid:
                continue
            ag_by_cid[cid] = dict(node)
            collect_ag(node.get("children") or [], cid)

    collect_ag(ag_list)
    from agy_transcript_state import transcript_state
    transcript_parents, transcript_killed = transcript_state(ag_by_cid)
    ag_parent_ids.update(transcript_parents)
    for cid, node in ag_by_cid.items():
        if cid in transcript_killed:
            node["killed"] = transcript_killed[cid]
        if node.get("killed"):
            node.update(killed=True, is_running=False, status="TERMINATED")
    sub_to_root_cid: dict[str, str] = {}
    for cid, node in ag_by_cid.items():
        root_cid, seen = cid, set()
        while root_cid in ag_parent_ids and root_cid not in seen:
            seen.add(root_cid)
            root_cid = ag_parent_ids[root_cid]
        if cid in ag_parent_ids or node.get("is_subagent"):
            sub_to_root_cid[cid] = root_cid
    # Rebuild root membership from authoritative edges. This also keeps an old
    # root visible when a flat descendant is running, without duplicate owners.
    ag_by_cid = {
        cid: ({**node, "children": [child for child_id, child in ag_by_cid.items()
                                   if sub_to_root_cid.get(child_id) == cid]}
              if cid not in sub_to_root_cid else node)
        for cid, node in ag_by_cid.items()
    }
    cdx_list = [n for n in trees_raw.get("codex", []) if isinstance(n, dict) and "error" not in n]
    dismissed = set(state.get("dismissed") or [])
    pinned = set(state.get("pinned") or [])
    imported = set(state.get("imported") or [])
    now_sec = datetime.now().timestamp()
    live_codex_ids, live_agy_ids, live_claude_ids = _running_worker_sessions(now_sec)
    cdx_list = _merge_registry_workers(cdx_list, "codex")
    claude_list = _merge_registry_workers(trees_raw.get("claude", []), "claude")
    from live_worker_processes import scan_live_worker_processes
    live_processes = scan_live_worker_processes()
    live_parent_ids = {p.get("parent_conversation_id") for p in live_processes}
    for cid, node in ag_by_cid.items():
        if cid in live_parent_ids:
            node["has_active_cli"] = True
    cdx_list = _merge_live_process_workers(cdx_list, "codex", live_processes, now_sec)
    claude_list = _merge_live_process_workers(claude_list, "claude", live_processes, now_sec)
    from check_all_usage import antigravity_only_mode, antigravity_only_trees
    if data.get("antigravity_only", antigravity_only_mode()):
        visible = antigravity_only_trees({"antigravity": list(ag_by_cid.values()),
                                        "codex": cdx_list, "claude": claude_list})
        cdx_list, claude_list = visible["codex"], visible["claude"]
        orca_terminals = {}
    live_codex_ids.update(n["thread_id"] for n in cdx_list if n.get("live_process"))
    live_claude_ids.update(n["session_id"] for n in claude_list if n.get("live_process"))
    live_codex_proc_ids = {p.get("session_id") for p in live_processes
                           if p.get("provider") == "codex" and p.get("session_id")}
    claude_mains = [c for c in claude_list if _is_claude_main(c)]
    for c in claude_list:
        # An open Orca teammate pane is alive between turns; only its busy flag means running.
        if c.get("orca_lead_id") and c.get("is_running") is True:
            c.update(orca_pane_open=True, is_running=c.get("activity") != "idle")
    claude_list = [c for c in claude_list if not _is_claude_main(c) and (
                   c.get("session_id") in live_claude_ids or c.get("is_running") or c.get("orca_pane_open")
                   or now_sec - (_parse_any_ts(c.get("updated_at")) or 0) <= SUB_IDLE_RETENTION_SEC)]
    rollout_paths = _codex_rollout_paths(cdx_list)
    proc_res = data.get("process_resources") if isinstance(data.get("process_resources"), dict) else {}
    proc_table = proc_res.get("procs") or {}
    from check_all_usage import merge_resources

    def _worker_res(node: dict) -> dict | None:
        if node.get("resources"):
            return node["resources"]
        workers = proc_res.get("workers") or {}
        for key in ("session_id", "thread_id", "registry_id", "id"):
            if node.get(key) and node[key] in workers:
                return workers[node[key]]
        return (proc_res.get("pids") or {}).get(str(node.get("pid") or ""))

    def _conv_res(cid_val: str) -> dict | None:
        return (proc_res.get("conversations") or {}).get(cid_val or "")

    def _claim(claimed: set, res) -> dict | None:
        """A child row's exclusive share: PIDs an earlier sibling (SUB before JOB/CRON) owns are dropped,
        so the child rows under one main agent add up instead of repeating a shared tree."""
        if not res:
            return None
        pids = [int(pid) for pid in res.get("pids") or ()]
        own = [pid for pid in pids if pid not in claimed]
        claimed.update(own)
        if len(own) == len(pids):
            return res
        return merge_resources(proc_table, [{"pids": own}], proc_res) if own else None

    def _is_worker_wrapper(cid_val: str, task: dict) -> bool:
        # The task's process tree holds a live CLI worker: its SUB row already shows that usage.
        res = (proc_res.get("tasks") or {}).get(f"{cid_val}/{task.get('short_id')}") or {}
        return task.get("kind") not in ("cron", "timer") and bool(res.get("worker_pids"))

    def _first_line(value, fallback: str) -> str:
        line = (str(value or "").splitlines() or [""])[0]
        return re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", line).strip() or fallback

    running_codex = [
        c for c in cdx_list
        if c.get("source") == "exec" and _codex_worker_is_active(c, live_codex_ids, rollout_paths, now_sec)
    ]

    def _bg_daemon_chip_and_detail(cid_val: str, br_id_val: str = "") -> tuple[list[dict], str, str]:
        bg_all = [t for t in _extract_active_bg_tasks(cid_val, now_sec) if not _is_worker_wrapper(cid_val, t)]
        if not bg_all:
            return [], "", ""
        daemons = [t for t in bg_all if t.get("kind") == "daemon"]
        timers = [t for t in bg_all if t.get("kind") == "timer"]
        if not daemons and not timers:
            return bg_all, "", ""
        tooltip_lines = [f"[{t['short_id']}] {t['desc']}" for t in (daemons + timers)]
        onclick_js = f"event.stopPropagation(); toggleNodeDaemons('{br_id_val}', this)" if br_id_val else ""
        chip_html = (
            f'<button type="button" class="bg-cnt-chip" data-branch-id="{br_id_val}" onclick="{onclick_js}" '
            f'title="데몬·타이머 {len(daemons + timers)}개 가동 중 (클릭하여 열기/닫기):&#10;{_html_esc(chr(10).join(tooltip_lines))}" '
            f'aria-label="데몬 목록 열기/닫기">'
            f'<svg width="11" height="11" aria-hidden="true"><use href="#ico-ubuntu-term"/></svg>'
            f'</button>'
        )
        head_parts = []
        if daemons:
            head_parts.append(f"⚙ 상시 데몬 {len(daemons)}개 가동 중")
        if timers:
            head_parts.append(f"⏱ 대기 타이머 {len(timers)}개 활성")
        item_rows = []
        for dmn in daemons:
            item_rows.append(
                f'<div class="bg-daemon-item" title="{_html_esc(dmn["desc"])}">'
                f'<span class="bg-daemon-tag">Daemon</span>'
                f'<span class="bg-daemon-name">{_html_esc(dmn["desc"])}</span>'
                f'<span class="bg-daemon-id">#{_html_esc(dmn["short_id"])}</span></div>'
            )
        for tmr in timers:
            item_rows.append(
                f'<div class="bg-daemon-item" title="{_html_esc(tmr["desc"])}">'
                f'<span class="bg-daemon-tag timer">Wait</span>'
                f'<span class="bg-daemon-name">{_html_esc(tmr["desc"])}</span>'
                f'<span class="bg-daemon-id">#{_html_esc(tmr["short_id"])}</span></div>'
            )
        detail_html = (
            f'<div class="bg-daemon-section">'
            f'<div class="bg-daemon-head"><span>{" · ".join(head_parts)}</span><span>우측: 태스크 번호 (#task-ID)</span></div>'
            f'{"".join(item_rows)}'
            f'</div>'
        )
        return bg_all, chip_html, detail_html

    active_ag = []
    closed_ag = []
    internal_ag_workers = []
    seen_active_repos = set()
    for n in ag_by_cid.values():
        cid = n.get("conversation_id", "")
        if cid in sub_to_root_cid:
            internal_ag_workers.append(n)
            continue
        cid8 = cid[:8]
        node_id = f"node-agy-{cid8}"
        children = n.get("children") or []
        has_running_child = any(_agy_worker_is_active(c, live_agy_ids) for c in children)
        age_sec_ag = _age_sec_from_iso(n.get("last_modified_time") or "")
        age_days_ag = _age_days_from_iso(n.get("last_modified_time") or "")
        has_active_bg = age_sec_ag <= 43200 and any(
            t.get("kind") != "cron" for t in _extract_active_bg_tasks(cid, now_sec)
        )
        repo_name, _ = _repo_from_uris_or_cwd(n.get("workspaces"))
        raw_t = (n.get("title") or "").strip()
        is_internal_worker = (
            raw_t.lstrip().startswith(("✦", "🪐", "[AGY]"))
            or "Reply with a single word" in raw_t
            or "You are Claude" in raw_t
        )
        is_running = (
            _agy_worker_is_active(n, live_agy_ids)
            or has_running_child
            or has_active_bg
            or cid == "521b781e-43da-4647-b411-585e65e5f002"
        )
        is_pinned = (node_id in pinned or cid in pinned or f"closed-agy-{cid8}" in pinned)
        is_imported = (node_id in imported or cid in imported)
        is_dismissed = not is_pinned and (node_id in dismissed or cid in dismissed or f"closed-agy-{cid8}" in dismissed)
        if is_internal_worker and not is_pinned and not is_imported:
            if "Reply with a single word" not in raw_t and (_agy_worker_is_active(n, live_agy_ids) or age_sec_ag <= SUB_IDLE_RETENTION_SEC):
                internal_ag_workers.append(n)
            closed_ag.append(n)
        elif (is_running or is_pinned or is_imported or age_days_ag <= 7) and not is_dismissed:
            seen_active_repos.add(repo_name)
            active_ag.append(n)
        else:
            closed_ag.append(n)

    active_cdx = []
    closed_cdx = []
    internal_cdx_workers = []
    active_vscode_repos = set()
    for c in cdx_list:
        tid = c.get("thread_id", "")
        tid8 = tid[:8]
        node_id = f"node-cdx-{tid}"
        is_live_running, cdx_upd_ts = _codex_root_activity(c, live_codex_ids, rollout_paths, now_sec)
        age_sec_cdx = max(0.0, now_sec - float(cdx_upd_ts or c.get("updated_at") or 0.0))
        age_days_cdx = int(c.get("age_days", 0) or 0)
        is_internal_cdx = bool(c.get("agy_origin")) or (c.get("title") or "").lstrip().startswith("✦") or c.get("source") == "exec"
        is_pinned = (node_id in pinned or tid in pinned or f"closed-cdx-{tid8}" in pinned)
        is_imported = (node_id in imported or tid in imported)
        is_dismissed = not is_pinned and (node_id in dismissed or tid in dismissed or f"closed-cdx-{tid8}" in dismissed)
        if c.get("orca_lead_id"):
            # A pane another Orca agent opened is its SUB row while the pane is open.
            c = {**c, "orca_pane_open": tid in live_codex_proc_ids}
            if c["orca_pane_open"] or is_live_running or age_sec_cdx <= SUB_IDLE_RETENTION_SEC:
                internal_cdx_workers.append(c)
        elif c.get("source") == "exec" or c.get("agy_origin") or (is_internal_cdx and not is_pinned and not is_imported):
            if is_live_running or age_sec_cdx <= SUB_IDLE_RETENTION_SEC:
                internal_cdx_workers.append(c)
            if not c.get("registry_only") and c.get("source") != "exec":
                closed_cdx.append(c)
        elif (is_live_running or is_pinned or is_imported or age_days_cdx <= 7) and not is_dismissed:
            active_cdx.append(c)
            if c.get("source") == "vscode":
                active_vscode_repos.add(_repo_from_uris_or_cwd(None, c.get("cwd") or "")[0])
        elif not c.get("registry_only"):
            closed_cdx.append(c)

    # Aggregate descendant evidence under its root, including asynchronous command
    # output that appears only in task logs rather than in the parent transcript.
    maestro_specs_by_cid: dict[str, str] = {}
    maestro_activity: dict[str, float] = {}
    launch_owners: dict[str, set[str]] = {}
    evidence_by_root: dict[str, list[str]] = {}
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    for cid in ag_by_cid:
        root_cid = sub_to_root_cid.get(cid, cid)
        system_dir = os.path.join(brain_dir, cid, ".system_generated")
        paths = [os.path.join(system_dir, "logs", "transcript.jsonl")]
        paths.extend(glob.glob(os.path.join(system_dir, "tasks", "task-*.log")))
        for path in paths:
            try:
                from spawn_evidence import task_evidence
                from agy_transcript_state import transcript_summary
                if path.endswith("transcript.jsonl"):
                    _, _, evidence, result_ids, activity = transcript_summary(path)
                else:
                    evidence, result_ids, activity = task_evidence(path)
                evidence_by_root.setdefault(root_cid, []).append(evidence)
                for wid in result_ids:
                    launch_owners.setdefault(wid, set()).add(root_cid)
                if activity:
                    maestro_activity[root_cid] = max(maestro_activity.get(root_cid, 0), os.path.getmtime(path))
            except OSError:
                pass
    maestro_specs_by_cid = {cid: "\n".join(parts) for cid, parts in evidence_by_root.items()}

    attached_ag_workers_by_parent: dict[str, list[dict]] = {}
    for w_ag in internal_ag_workers:
        w_raw = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", ((w_ag.get("title") or "").splitlines() or [""])[0]).strip()
        target_p = None
        # 1. Direct parent_conversation_id if it exists in active_ag
        p_direct = sub_to_root_cid.get(w_ag.get("conversation_id")) or w_ag.get("parent_id") or w_ag.get("parent_conversation_id")
        if p_direct and any(ag.get("conversation_id") == p_direct for ag in active_ag):
            target_p = p_direct
        if w_ag.get("conversation_id") in sub_to_root_cid and not target_p:
            # Missing/cyclic explicit parents must not attach to an unrelated repo.
            continue
        if target_p:
            attached_ag_workers_by_parent.setdefault(target_p, []).append({
                **w_ag,
                "role": w_ag.get("role") or w_raw or "maestro worker",
            })

    # Claude Code terminals (Orca panes, plain CLI) are main agents like Codex CLI threads:
    # open sessions stay in the active view, exited ones move to the closed list.
    active_claude = []
    closed_claude = []
    for c in claude_mains:
        sid = c.get("session_id") or ""
        node_id = f"node-claude-{sid}"
        is_pinned = node_id in pinned or sid in pinned
        if not is_pinned and (node_id in dismissed or sid in dismissed):
            continue
        is_live = sid in live_claude_ids or c.get("is_running") is True
        age_sec_cl = now_sec - (_parse_any_ts(c.get("updated_at")) or 0.0)
        if is_pinned or node_id in imported or (is_live and (age_sec_cl <= CLAUDE_MAIN_STALE_SEC or c.get("activity") == "busy")):
            active_claude.append(c)
        elif not is_live:
            closed_claude.append(c)

    parents = active_ag + active_cdx + active_claude
    for parent in active_cdx:
        path = parent.get("rollout_path") or rollout_paths.get(parent.get("thread_id"))
        if path:
            try:
                from spawn_evidence import rollout_evidence
                maestro_specs_by_cid[parent["thread_id"]] = rollout_evidence(path)
            except OSError:
                pass
    attached_cdx_workers_by_parent: dict[str, list[dict]] = {}
    attached_claude_workers_by_parent: dict[str, list[dict]] = {}
    for provider, workers, attached in (
        ("codex", internal_cdx_workers, attached_cdx_workers_by_parent),
        ("claude", claude_list, attached_claude_workers_by_parent),
    ):
        for worker in workers:
            if worker.get("killed"):
                continue
            worker = dict(worker)
            for key in ("parent_id", "parent_cid", "parent_conversation_id", "parent_session_id", "parent_thread_id"):
                if worker.get(key) in sub_to_root_cid:
                    worker[key] = sub_to_root_cid[worker[key]]
            target = _worker_parent(worker, parents, maestro_specs_by_cid, now_sec, maestro_activity, launch_owners)
            if target:
                attached.setdefault(target, []).append({**worker, "parent_cid": target})

    def _codex_sub_row(repo_codex: dict, ctid: str, repo: str, slot_idx: int,
                       claimed: set, res_children: list) -> tuple[int, float, str] | None:
        """One delegated Codex worker row under a main card (AGY or Claude); None once it has faded."""
        ctid_short = ctid[-8:] if len(ctid) >= 8 else ctid
        c_node_id = f"node-sub-cdx-{ctid}"
        c_live, c_upd = _codex_root_activity(repo_codex, live_codex_ids, rollout_paths, now_sec)
        c_age_s = max(0.0, now_sec - float(c_upd or repo_codex.get("updated_at") or 0.0))
        if not c_live and (c_node_id in dismissed or ctid in dismissed or (
                not repo_codex.get("orca_pane_open") and c_age_s > SUB_IDLE_RETENTION_SEC)):
            return None
        ctc = repo_codex.get("token_count") or {}
        c_used = fmt_k(ctc.get("total_tokens", 0))
        c_pct = float(ctc.get("percent", 0.0) or 0.0)
        ccwd = repo_codex.get("cwd") or os.path.join(os.path.expanduser("~/dev"), repo)
        c_disp = re.sub(
            r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?",
            "",
            (repo_codex.get("display_name") or ((repo_codex.get("title") or "").splitlines() or [""])[0] or "codex-worker"),
        ).strip()
        c_repo, _ = _repo_from_uris_or_cwd(None, ccwd)
        c_tctx = repo_codex.get("turn_context") or {}
        c_icon, c_pill, _ = _provider_icon_and_pill(
            c_tctx.get("model") or "gpt-6.1-sol", "codex",
            resolve_speed_tier(c_tctx.get("service_tier"), repo_codex.get("tier")),
            c_tctx.get("reasoning_effort") or "high",
        )
        c_dot, c_tip, c_eff_age, c_cls = _sub_row_state(repo_codex, c_live, c_age_s, slot_idx)
        c_ts = float(c_upd or repo_codex.get("updated_at") or 0.0)
        c_res = _claim(claimed, _worker_res(repo_codex))
        res_children.append(("SUB", c_disp, c_res))
        c_html = (
            f'<div class="{c_cls}" data-node-id="{c_node_id}" data-sub-running="{1 if c_live else 0}" data-sub-age="{c_eff_age}" data-idle-age="{int(c_age_s)}" data-done-ts="{int(c_ts)}">'
            f'<div class="agent-row child" onclick="toggleDetail(\'det-sub-cdx-{ctid_short}\')">'
            f'<div class="agent-left">'
            f'{_sub_prefix_html(c_dot, "SUB", c_tip)}'
            f'{c_icon}'
            f'<span class="agent-name">{_html_esc(c_disp)}</span>'
            f'</div>'
            f'<div class="agent-right">'
            f'{_res_slot_html(f"res-{c_node_id}", c_res, force=c_live)}'
            f'{c_pill}{_context_pill_html(c_used, c_pct, "green", repo_codex, c_live, c_dot)}'
            f'<span class="row-actions">'
            f'<button type="button" class="icon-act-btn codex" onclick="event.stopPropagation(); openCodexDirectBar(\'{ctid}\', \'{_html_esc(repo)} ({ctid_short})\', \'{_html_esc(ccwd)}\')" title="Codex 직접 명령 보내기 (하단 팝오버)" aria-label="Codex 직접 명령 보내기"><svg width="12" height="12" aria-hidden="true"><use href="#ico-cmd-send"/></svg></button>'
            f'{_orca_open_or(c_node_id, ctid, c_disp, repo_codex, "")}'
            f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{c_node_id}\', \'{_html_esc(c_disp[:24])}\')">✕</button>'
            f'</span>'
            f'</div></div>'
            f'<div class="agent-detail" id="det-sub-cdx-{ctid_short}"><div class="agent-detail-row">'
            f'<span>Codex Subagent: {ctid} · cwd: {_html_esc(ccwd)}{_fmt_duration(repo_codex)}</span>'
            f'<span>{c_used} / 475k ({c_pct:.1f}%)</span>'
            f'</div>{_detail_extras_html("codex", ctid, c_repo, False)}</div></div>'
        )
        return (0 if c_live else 1, -c_ts, c_html)

    def _claude_sub_items(workers: list[dict], claimed: set, res_children: list) -> list[tuple[int, float, str]]:
        """Delegated Claude worker rows under a Codex or Claude main card."""
        items = []
        for c in workers:
            sid = c.get("session_id") or c.get("id") or ""
            if not sid or sid in dismissed or f"node-sub-claude-{sid}" in dismissed:
                continue
            is_terminal = c.get("status") in ("completed", "failed", "timeout", "done", "closed") or c.get("ok") is not None
            c_live = (sid in live_claude_ids or c.get("is_running") is True) and not is_terminal
            c_ts = _parse_any_ts(c.get("updated_at")) or 0.0
            if not c_live and not c.get("orca_pane_open") and now_sec - c_ts > SUB_IDLE_RETENTION_SEC:
                continue
            c = {**c, "resources": _claim(claimed, _worker_res(c))}
            res_children.append(("SUB", _first_line(c.get("display_name") or c.get("title") or c.get("spec_preview"), "Claude worker"), c["resources"]))
            items.append((0 if c_live else 1, -c_ts, _claude_worker_row(c, live_claude_ids, now_sec)))
        return items

    active_parts = ["<!--__ACTIVE_TREE_HTML_START__-->"]
    active_entries = []
    active_curr_cnt = 0
    rendered_codex_child_ids = set()

    for ag in active_ag:
        cid = ag.get("conversation_id", "")
        cid8 = cid[:8]
        node_id = f"node-agy-{cid8}"
        is_pinned = (node_id in pinned or cid in pinned or f"closed-agy-{cid8}" in pinned)
        if not is_pinned and (node_id in dismissed or cid in dismissed):
            continue
        repo, scope = _repo_from_uris_or_cwd(ag.get("workspaces"))
        if cid == "521b781e-43da-4647-b411-585e65e5f002":
            repo, scope = "dev", "current"
        scope = "current"
        active_curr_cnt += 1

        raw_title = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", (ag.get("title") or "Orchestrator")).strip()
        disp_title = f"Main Orchestrator · {raw_title}" if cid == "521b781e-43da-4647-b411-585e65e5f002" else raw_title
        ctx = ag.get("context_usage") or {}
        used_k = fmt_k(ctx.get("used_tokens", 0))
        max_k = fmt_k(ctx.get("max_tokens", 1000000))
        pct = float(ctx.get("percent", 0.0) or 0.0)
        inp_k = fmt_k(ctx.get("input_tokens", 0))
        cache_k = fmt_k(ctx.get("cache_read_tokens", 0))
        out_k = fmt_k(ctx.get("output_tokens", 0))
        icon_svg, model_pill, sub_badge = _provider_icon_and_pill(ag.get("model", ""), "antigravity")

        extra_ag_workers = attached_ag_workers_by_parent.get(cid, [])
        extra_cdx_workers = attached_cdx_workers_by_parent.get(cid, [])
        extra_claude_workers = attached_claude_workers_by_parent.get(cid, [])
        is_busy_ag = (
            _agy_worker_is_active(ag, live_agy_ids)
            or any(_agy_worker_is_active(c, live_agy_ids) for c in (ag.get("children") or []))
            or any(c.get("session_id") in live_claude_ids or c.get("is_running") for c in extra_claude_workers)
            or any(_agy_worker_is_active(c, live_agy_ids) for c in extra_ag_workers)
            or any(_codex_root_activity(c, live_codex_ids, rollout_paths, now_sec)[0] for c in extra_cdx_workers)
        )
        age_sec_ag = _age_sec_from_iso(ag.get("last_modified_time") or "")
        root_dot, root_dot_tip = _status_dot_from_age(is_busy_ag, age_sec_ag)
        try:
            updated_ts = datetime.fromisoformat((ag.get("last_modified_time") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            updated_ts = 0.0

        br_id = f"br-act-{cid8}"
        det_id = f"det-act-{cid8}"

        subagent_items: list[tuple[int, float, str]] = []
        res_children: list[tuple[str, str, dict | None]] = []
        claimed_pids: set[int] = set()
        for c in extra_claude_workers:
            sid = c.get("session_id") or c.get("id") or ""
            is_terminal = c.get("status") in ("completed", "failed", "timeout", "done", "closed") or c.get("ok") is not None
            c_live = (sid in live_claude_ids or c.get("is_running") is True) and not is_terminal
            c_ts = _parse_any_ts(c.get("updated_at")) or 0.0
            if not c_live and (not sid or sid in dismissed or f"node-sub-claude-{sid}" in dismissed
                               or now_sec - c_ts > SUB_IDLE_RETENTION_SEC):
                continue
            c = {**c, "resources": _claim(claimed_pids, _worker_res(c))}
            res_children.append(("SUB", _first_line(c.get("display_name") or c.get("title") or c.get("spec_preview"), "Claude worker"), c["resources"]))
            subagent_items.append((0 if c_live else 1, -c_ts, _claude_worker_row(c, live_claude_ids, now_sec)))

        sub_slot_idx = 0
        seen_ctids = set()
        for repo_codex in extra_cdx_workers:
            ctid = repo_codex.get("thread_id", "")
            if not ctid or ctid in seen_ctids:
                continue
            seen_ctids.add(ctid)
            rendered_codex_child_ids.add(ctid)
            item = _codex_sub_row(repo_codex, ctid, repo, sub_slot_idx, claimed_pids, res_children)
            if item:
                sub_slot_idx += 1
                subagent_items.append(item)

        ag_children_all = [
            c for c in (list(extra_ag_workers) + (ag.get("children") or []))
            if _agy_worker_is_active(c, live_agy_ids) or _age_sec_from_iso(c.get("last_modified_time") or "") <= SUB_IDLE_RETENTION_SEC
        ]
        seen_ag_child_cids = set()
        ag_children = []
        for ch in sorted(ag_children_all, key=lambda x: _age_sec_from_iso(x.get("last_modified_time") or "")):
            ccid = ch.get("conversation_id", "")
            if not ccid or ccid in seen_ag_child_cids:
                continue
            seen_ag_child_cids.add(ccid)
            ag_children.append(ch)

        running_ag = [c for c in ag_children if _agy_worker_is_active(c, live_agy_ids)]
        completed_ag = [
            c for c in ag_children
            if not _agy_worker_is_active(c, live_agy_ids) and _age_sec_from_iso(c.get("last_modified_time") or "") <= SUB_IDLE_RETENTION_SEC
        ]
        ag_children = running_ag + completed_ag[:max(0, 6 - len(subagent_items) - len(running_ag))]
        for ch in ag_children:
            ccid = ch.get("conversation_id", "")
            ccid8 = ccid[:8]
            c_node_id = f"node-sub-{ccid8}"
            c_running = _agy_worker_is_active(ch, live_agy_ids)
            if not c_running and (c_node_id in dismissed or ccid in dismissed):
                continue
            c_role = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", (ch.get("role") or ch.get("title") or "Subagent").splitlines()[0]).strip()
            c_ctx = ch.get("context_usage") or {}
            c_used = fmt_k(c_ctx.get("used_tokens", 0))
            c_pct = float(c_ctx.get("percent", 0.0) or 0.0)
            c_age_s = _age_sec_from_iso(ch.get("last_modified_time") or "")
            c_dot, c_tip, c_eff_age = _sub_status_dot_from_age(c_running, c_age_s, sub_slot_idx)
            sub_slot_idx += 1
            c_icon, c_pill, _ = _provider_icon_and_pill(ch.get("model", ""), "antigravity")
            c_repo = _repo_from_uris_or_cwd(ch.get("workspaces"))[0] if ch.get("workspaces") else repo
            c_ts = _parse_any_ts(ch.get("last_modified_time")) or 0.0
            c_res = _claim(claimed_pids, ch.get("resources") or _conv_res(ccid))
            res_children.append(("SUB", c_role, c_res))
            c_html = (
                f'<div class="tree-node sub-node sub-ripening" data-node-id="{c_node_id}" data-sub-running="{1 if c_running else 0}" data-sub-age="{c_eff_age}" data-idle-age="{int(c_age_s)}" data-done-ts="{int(c_ts)}">'
                f'<div class="agent-row child" onclick="toggleDetail(\'det-sub-{ccid8}\')">'
                f'<div class="agent-left">'
                f'{_sub_prefix_html(c_dot, "SUB", c_tip)}'
                f'{c_icon}'
                f'<span class="agent-name">{_html_esc(c_role)}</span>'
                f'</div>'
                f'<div class="agent-right">'
                f'{_res_slot_html(f"res-{c_node_id}", c_res, force=c_running)}'
                f'{c_pill}'
                f'{_context_pill_html(c_used, c_pct, "blue", ch, c_running, c_dot)}'
                f'<span class="row-actions">'
                f'{_open_btn_html(c_node_id, ccid, c_role, c_repo)}'
                f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{c_node_id}\', \'{_html_esc(c_role[:24])}\')">✕</button>'
                f'</span>'
                f'</div></div>'
                f'<div class="agent-detail" id="det-sub-{ccid8}"><div class="agent-detail-row">'
                f'<span>Subagent ID: {ccid}{_fmt_duration(ch)}</span>'
                f'<span>{c_used} / 1M ({c_pct:.1f}%)</span>'
                f'</div>{_detail_extras_html("antigravity", ccid, c_repo, False)}</div></div>'
            )
            subagent_items.append((0 if c_running else 1, -c_ts, c_html))

        subagent_rows = [item[2] for item in sorted(subagent_items, key=lambda x: (x[0], x[1]))]

        bg_tree_jobs, bg_parent_chip, bg_detail_section = _bg_daemon_chip_and_detail(cid, br_id)
        show_bg_daemons = state.get("showBgDaemons", True) is not False

        def _render_bg_row(bgt: dict) -> str:
            bt_id = bgt["short_id"]
            bt_node_id = f"node-bg-{cid8}-{bt_id}"
            if bt_node_id in dismissed:
                return ""
            bt_desc = bgt["desc"]
            bt_kind = bgt.get("kind") or "job"
            if bt_kind == "cron":
                clean_desc = re.sub(r"^Cron:\s*[^,]+,\s*Prompt:\s*", "", bt_desc, flags=re.IGNORECASE).strip()
                if clean_desc:
                    bt_desc = clean_desc.strip('"\'')
            bt_tag, bt_kind_lbl, bt_time_str, bt_timing_ko = _fmt_bg_badge_and_timing(bgt, now_sec)
            is_dmn_or_tmr = bt_kind in ("daemon", "timer")
            node_cls = "tree-node sub-node bg-daemon-node"
            if is_dmn_or_tmr and not show_bg_daemons:
                node_cls += " hidden"
            bt_dot = "busy" if bt_kind == "job" else "idle-fresh"
            bt_res = _claim(claimed_pids, bgt.get("resources") or (proc_res.get("tasks") or {}).get(f"{cid}/{bt_id}"))
            res_children.append((bt_tag, bt_desc, bt_res))
            if bt_kind == "cron":
                bt_pill = ""
            else:
                bt_pill = (
                    f'<span class="model-pill bg-task" title="{_html_esc(bt_timing_ko)} · #{_html_esc(bt_id)}: {_html_esc(bt_desc)}">'
                    f'<span class="mp-full">{_html_esc(bt_kind_lbl)}</span>'
                    f'<span class="mp-short">{_html_esc(bt_kind_lbl)}</span>'
                    f'</span>'
                )
            if bt_kind == "cron":
                repeat_svg = (
                    '<svg class="ctx-repeat-ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
                    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                    '<path d="M11.5 2.5L14 5l-2.5 2.5"/><path d="M2 8V6a1 1 0 0 1 1-1h11"/>'
                    '<path d="M4.5 13.5L2 11l2.5-2.5"/><path d="M14 8v2a1 1 0 0 1-1 1H2"/>'
                    '</svg>'
                )
                bt_time_cls = f"ctx-pill bg-time {bt_dot} has-repeat-ico"
                bt_time_pill = (
                    f'<span class="{bt_time_cls}" title="{_html_esc(bt_timing_ko)}">'
                    f'{repeat_svg}'
                    f'<span class="ctx-time-txt">{_html_esc(bt_time_str)}</span></span>'
                )
            else:
                bt_time_cls = f"ctx-pill bg-time {bt_dot}"
                bt_time_pill = (
                    f'<span class="{bt_time_cls}" title="{_html_esc(bt_timing_ko)}">'
                    f'<span class="ctx-time-txt">{_html_esc(bt_time_str)}</span></span>'
                )
            return (
                f'<div class="{node_cls}" data-node-id="{bt_node_id}" data-bg-kind="{bt_kind}">'
                f'<div class="agent-row child bg-task-row" onclick="toggleDetail(\'det-bg-{cid8}-{bt_id}\')">'
                f'<div class="agent-left">'
                f'{_sub_prefix_html(bt_dot, bt_tag)}'
                f'<svg class="brand-icon" width="15" height="15"><use href="#ico-ubuntu-term"/></svg>'
                f'<span class="agent-name">{_html_esc(bt_desc)}</span>'
                f'</div>'
                f'<div class="agent-right">'
                f'{_res_slot_html(f"res-{bt_node_id}", bt_res, force=bt_kind == "job")}'
                f'{bt_pill}'
                f'{bt_time_pill}'
                f'<span class="row-actions bg-actions">'
                f'<span class="bg-task-num" title="태스크 번호: #{_html_esc(bt_id)}">#{_html_esc(bt_id)}</span>'
                f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{bt_node_id}\', \'{_html_esc(bt_id)}\')">✕</button>'
                f'</span>'
                f'</div></div>'
                f'<div class="agent-detail" id="det-bg-{cid8}-{bt_id}"><div class="agent-detail-row">'
                f'<span>{_html_esc(bt_timing_ko)}</span>'
                f'<span>태스크 번호: #{_html_esc(bt_id)} ({_html_esc(bgt["id"])})</span>'
                f'</div></div></div>'
            )

        cmd_tasks = [t for t in bg_tree_jobs if t.get("kind") != "cron"]
        cron_tasks = [t for t in bg_tree_jobs if t.get("kind") == "cron"]

        cmd_tasks_sorted = sorted(cmd_tasks, key=lambda x: _age_sec_from_iso(x.get("created_at") or ""))
        cron_tasks_sorted = sorted(cron_tasks, key=lambda x: _age_sec_from_iso(x.get("created_at") or ""))

        cmd_task_rows = [_render_bg_row(t) for t in cmd_tasks_sorted[:8]]
        cmd_task_rows = [r for r in cmd_task_rows if r]

        cron_task_rows = [_render_bg_row(t) for t in cron_tasks_sorted[:8]]
        cron_task_rows = [r for r in cron_task_rows if r]

        child_rows_html = subagent_rows + cmd_task_rows + cron_task_rows

        br_cls = "tree-children hidden" if state.get("allCollapsed") is True else "tree-children"
        children_block = f'<div class="{br_cls}" id="{br_id}">{_finalize_child_rows(child_rows_html)}</div>' if child_rows_html else ""
        # Main rollup: child rows are disjoint, so it is their sum plus the main's unclaimed PIDs
        # (own conversation, descendant conversations without a row).
        ag_rollup = merge_resources(proc_table, [ag.get("rollup_resources"), _conv_res(cid)]
                                    + [_conv_res(ch.get("conversation_id")) for ch in (ag.get("children") or [])]
                                    + [res for _, _, res in res_children], proc_res)
        # Own share = rollup minus the PIDs child rows claimed: the Antigravity runtime share
        # (language_server + window) plus run_command shells no row shows.
        own_pids = [pid for pid in (ag_rollup or {}).get("pids") or () if pid not in claimed_pids]
        ag_self = merge_resources(proc_table, [{"pids": own_pids}], proc_res) if own_pids else None
        ag_kids = merge_resources(proc_table, [{"pids": sorted(claimed_pids)}], proc_res) if claimed_pids else None
        pin_cls = " pinned" if is_pinned else ""
        pin_attr = ' data-pinned="1"' if is_pinned else ""
        has_running_job = any(t.get("kind") == "job" for t in bg_tree_jobs)
        is_keep_alive = is_busy_ag or root_dot == "idle-fresh" or has_running_job or cid == "521b781e-43da-4647-b411-585e65e5f002"
        run_attr = ' data-running="1"' if is_keep_alive else ""

        parent_html = (
            f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}"{pin_attr}{run_attr}>'
            f'<div class="agent-row parent" onclick="toggleDetail(\'{det_id}\')">'
            f'<div class="agent-left">'
            f'<span class="status-dot {root_dot}" title="{_html_esc(root_dot_tip)}"></span>'
            f'<span class="repo-tag">{_html_esc(repo)}</span>'
            f'{icon_svg}'
            f'<span class="agent-name">{_html_esc(disp_title)}</span>'
            f'{bg_parent_chip}'
            f'</div>'
            f'<div class="agent-right">'
            f'{_res_slot_html(f"res-{node_id}", ag_rollup, rollup=True, force=True, split=(ag_self, ag_kids))}'
            f'{model_pill}'
            f'{_context_pill_html(used_k, pct, "blue", ag, is_busy_ag, root_dot)}'
            f'<span class="row-actions">'
            f'{_pin_btn_html(node_id, disp_title, is_pinned)}'
            f'{_open_btn_html(node_id, cid, disp_title, repo)}'
            f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(disp_title)}\')">✕</button>'
            f'</span>'
            f'</div></div>'
            f'<div class="agent-detail" id="{det_id}">'
            f'<div class="agent-detail-row"><span>ID: {cid} · 경로: {_html_esc(repo)}{_fmt_duration(ag)}</span>'
            f'<span>{used_k} / {max_k} (In {inp_k} · Cache {cache_k} · Out {out_k})</span></div>'
            f'{bg_detail_section}'
            f'{_detail_extras_html("antigravity", cid, repo, True)}'
            f'</div>'
            f'{children_block}'
            f'</div>'
        )
        active_entries.append({
            "is_live_running": is_keep_alive,
            "is_main_orch": cid == "521b781e-43da-4647-b411-585e65e5f002",
            "updated_ts": updated_ts,
            "html": parent_html,
            "res": {"kind": "MAIN", "label": disp_title, "rollup": ag_rollup,
                    "children": [("SELF", "메인 본체 (Antigravity 런타임 몫 + 직접 실행)", ag_self)] + res_children},
        })

    active_cdx_sorted = sorted(active_cdx, key=lambda x: 0 if x.get("source") == "vscode" else 1)
    for cdx in active_cdx_sorted:
        if cdx.get("source") == "exec" or cdx.get("agy_origin"):
            continue
        tid = cdx.get("thread_id", "")
        if tid in rendered_codex_child_ids:
            continue
        tid8 = tid[:8]
        node_id = f"node-cdx-{tid}"
        is_pinned = (node_id in pinned or tid in pinned or f"closed-cdx-{tid8}" in pinned)
        if not is_pinned and (node_id in dismissed or tid in dismissed):
            continue
        cwd = cdx.get("cwd") or os.path.expanduser("~/dev")
        repo, scope = _repo_from_uris_or_cwd(None, cwd)
        if scope == "current" or is_pinned or node_id in imported:
            active_curr_cnt += 1
        disp_raw = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?(?:[A-Za-z0-9_.-]+\s*·\s*)?", "", (cdx.get("display_name") or cdx.get("title") or "Codex Agent")).strip()
        disp = f"✦ {disp_raw}" if (cdx.get("agy_origin") or (cdx.get("title") or "").lstrip().startswith("✦")) else disp_raw
        tc = cdx.get("token_count") or {}
        used_k = fmt_k(tc.get("total_tokens", 0))
        pct = float(tc.get("percent", 0.0) or 0.0)
        tctx = cdx.get("turn_context") or {}
        icon_svg, model_pill, _ = _provider_icon_and_pill(
            tctx.get("model") or "gpt-6.1-sol",
            "codex",
            resolve_speed_tier(tctx.get("service_tier"), cdx.get("tier")),
            tctx.get("reasoning_effort") or "high",
        )
        is_live_running, updated_ts = _codex_root_activity(cdx, live_codex_ids, rollout_paths, now_sec)
        claude_children = attached_claude_workers_by_parent.get(tid, [])
        is_live_running = is_live_running or any(c.get("session_id") in live_claude_ids or c.get("is_running") for c in claude_children)
        res_children_cdx: list[tuple[str, str, dict | None]] = []
        claimed_pids_cdx: set[int] = set()
        subagent_items_cdx = _claude_sub_items(claude_children, claimed_pids_cdx, res_children_cdx)

        if not cdx.get("agy_origin") or cdx.get("source") == "vscode":
            child_candidates = [
                ch for ch in (cdx.get("children") or [])
                if (ch.get("is_running") is True and not ch.get("turn_completed"))
                or _codex_worker_is_active(ch, live_codex_ids, rollout_paths, now_sec)
            ]
            child_candidates.extend(
                ch for ch in attached_cdx_workers_by_parent.get(tid, [])
            )
            seen_child_ids = {tid}
            active_children = []
            for ch in child_candidates:
                child_tid = ch.get("thread_id", "")
                child_node_id = f"node-sub-cdx-{tid}-{child_tid}"
                if (not child_tid or child_tid in seen_child_ids or child_tid in rendered_codex_child_ids
                        or child_tid in dismissed or child_node_id in dismissed):
                    continue
                seen_child_ids.add(child_tid)
                rendered_codex_child_ids.add(child_tid)
                active_children.append(ch)
            for ch in active_children:
                child_tid = ch["thread_id"]
                child_node_id = f"node-sub-cdx-{tid}-{child_tid}"
                child_det_id = f"det-sub-cdx-{tid}-{child_tid}"
                child_cwd = ch.get("cwd") or cwd
                child_disp = re.sub(
                    r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "",
                    ch.get("display_name") or ch.get("nickname") or ch.get("title") or "Codex Subagent",
                ).strip()
                child_ctx = ch.get("turn_context") or {}
                child_icon, child_pill, _ = _provider_icon_and_pill(
                    child_ctx.get("model") or "gpt-6.1-sol", "codex",
                    resolve_speed_tier(child_ctx.get("service_tier"), ch.get("tier")),
                    child_ctx.get("reasoning_effort") or "high",
                )
                child_tokens = ch.get("token_count") or {}
                child_used = fmt_k(child_tokens.get("total_tokens", 0))
                child_pct = float(child_tokens.get("percent", 0.0) or 0.0)
                child_repo, _ = _repo_from_uris_or_cwd(None, child_cwd)
                if ch.get("registry_id"):
                    c_live_child, child_ts = _codex_root_activity(ch, live_codex_ids, rollout_paths, now_sec)
                else:
                    c_live_child = (ch.get("is_running") is True and not ch.get("turn_completed")) or _codex_worker_is_active(ch, live_codex_ids, rollout_paths, now_sec)
                    child_ts = float(ch.get("updated_at") or 0.0)
                child_age = max(0.0, now_sec - float(child_ts or 0.0))
                if not c_live_child and not ch.get("orca_pane_open") and child_age > SUB_IDLE_RETENTION_SEC:
                    continue
                is_live_running = is_live_running or c_live_child
                child_dot, child_tip, child_eff_age, child_cls = _sub_row_state(ch, c_live_child, child_age)
                child_res = _claim(claimed_pids_cdx, _worker_res(ch))
                res_children_cdx.append(("SUB", child_disp, child_res))
                row_html = (
                    f'<div class="{child_cls}" data-node-id="{child_node_id}" data-sub-running="{int(c_live_child)}" data-sub-age="{child_eff_age}" data-idle-age="{int(child_age)}" data-done-ts="{int(child_ts or 0)}">'
                    f'<div class="agent-row child" onclick="toggleDetail(\'{child_det_id}\')">'
                    f'<div class="agent-left">{_sub_prefix_html(child_dot, "SUB", child_tip)}'
                    f'{child_icon}'
                    f'<span class="agent-name">{_html_esc(child_disp)}</span></div>'
                    f'<div class="agent-right">{_res_slot_html(f"res-{child_node_id}", child_res)}'
                    f'{child_pill}{_context_pill_html(child_used, child_pct, "green", ch, c_live_child, child_dot)}'
                    f'<span class="row-actions">'
                    f'<button type="button" class="icon-act-btn codex" onclick="event.stopPropagation(); openCodexDirectBar(\'{child_tid}\', \'{_html_esc(child_disp[:28])}\', \'{_html_esc(child_cwd)}\')" title="Codex 직접 명령 보내기 (하단 팝오버)" aria-label="Codex 직접 명령 보내기"><svg width="12" height="12" aria-hidden="true"><use href="#ico-cmd-send"/></svg></button>'
                    f'{_orca_open_or(child_node_id, child_tid, child_disp, ch, "")}'
                    f'</span>'
                    f'</div></div>'
                    f'<div class="agent-detail" id="{child_det_id}"><div class="agent-detail-row">'
                    f'<span>Codex Subagent: {child_tid} · cwd: {_html_esc(child_cwd)}{_fmt_duration(ch)}</span>'
                    f'<span>{child_used} tokens</span></div>{_detail_extras_html("codex", child_tid, child_repo, False)}</div></div>'
                )
                subagent_items_cdx.append((0 if c_live_child else 1, -child_ts, row_html))

        child_rows_html = [item[2] for item in sorted(subagent_items_cdx, key=lambda x: (x[0], x[1]))]
        br_id = f"br-cdx-{tid8}"
        age_sec_cdx = max(0.0, now_sec - float(updated_ts or 0.0))
        root_dot, root_dot_tip = _status_dot_from_age(is_live_running, age_sec_cdx)
        br_cls = "tree-children hidden" if state.get("allCollapsed") is True else "tree-children"
        children_block = f'<div class="{br_cls}" id="{br_id}">{_finalize_child_rows(child_rows_html)}</div>' if child_rows_html else ""
        pin_cls = " pinned" if is_pinned else ""
        pin_attr = ' data-pinned="1"' if is_pinned else ""
        # An open interactive Codex terminal (Orca pane, CLI) stays protected while idle at its prompt.
        is_keep_alive_cdx = is_live_running or root_dot == "idle-fresh" or tid in live_codex_proc_ids
        cdx_rollup = merge_resources(proc_table, [cdx.get("rollup_resources"), _worker_res(cdx)]
                                     + [res for _, _, res in res_children_cdx], proc_res)
        own_pids_cdx = [pid for pid in (cdx_rollup or {}).get("pids") or () if pid not in claimed_pids_cdx]
        cdx_self = merge_resources(proc_table, [{"pids": own_pids_cdx}], proc_res) if own_pids_cdx else None
        cdx_kids = merge_resources(proc_table, [{"pids": sorted(claimed_pids_cdx)}], proc_res) if claimed_pids_cdx else None
        run_attr = ' data-running="1"' if is_keep_alive_cdx else ""
        orca = _orca_worktree(cdx.get("orca_worktree"), cwd)
        cdx_open_btn = _open_btn_html(node_id, tid, disp, "/".join(orca)) if orca else (
            f'<button type="button" class="icon-act-btn import-btn" onclick="event.stopPropagation(); importOrCopySession(\'{node_id}\', \'codex resume {tid}\', this)" title="왼쪽 사이드바(dev)로 가져오기" aria-label="왼쪽 사이드바(dev)로 가져오기"><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 2h5v12H9M1 8h9M6 4l4 4-4 4"/></svg></button>')
        parent_html = (
            f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}"{pin_attr}{run_attr}>'
            f'<div class="agent-row parent" onclick="toggleDetail(\'det-cdx-{tid8}\')">'
            f'<div class="agent-left">'
            f'{_main_prefix_html(root_dot, root_dot_tip, repo, bool(orca))}'
            f'{icon_svg}'
            f'<span class="agent-name">{_html_esc(disp)}</span>'
            f'</div>'
            f'<div class="agent-right">'
            f'{_res_slot_html(f"res-{node_id}", cdx_rollup, rollup=True, split=(cdx_self, cdx_kids) if claimed_pids_cdx else None)}'
            f'{model_pill}'
            f'{_context_pill_html(used_k, pct, "green", cdx, is_live_running, root_dot)}'
            f'<span class="row-actions">'
            f'{_pin_btn_html(node_id, disp, is_pinned)}'
            f'<button type="button" class="icon-act-btn codex" onclick="event.stopPropagation(); openCodexDirectBar(\'{tid}\', \'{_html_esc(disp[:28])}\', \'{_html_esc(cwd)}\')" title="Codex 직접 명령 보내기 (하단 팝오버)" aria-label="Codex 직접 명령 보내기"><svg width="12" height="12" aria-hidden="true"><use href="#ico-cmd-send"/></svg></button>'
            f'{cdx_open_btn}'
            f'<button type="button" class="dismiss-btn" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(disp[:28])}\')">✕</button>'
            f'</span>'
            f'</div></div>'
            f'<div class="agent-detail" id="det-cdx-{tid8}"><div class="agent-detail-row">'
            f'<span>Codex Thread: {tid} · cwd: {_html_esc(cwd)}{_fmt_duration(cdx)}</span>'
            f'<span>{used_k} / 475k ({pct:.1f}%)</span>'
            f'</div>{_detail_extras_html("codex", tid, repo, True)}</div>{children_block}</div>'
        )
        active_entries.append({
            "is_live_running": is_keep_alive_cdx,
            "is_main_orch": False,
            "updated_ts": updated_ts,
            "html": parent_html,
            "orca": orca, "busy": is_live_running, "model": _pill_label(model_pill),
            "res": {"kind": "MAIN", "label": disp, "rollup": cdx_rollup,
                    "children": ([("SELF", "메인 본체 (Codex 세션)", cdx_self)] if claimed_pids_cdx else []) + res_children_cdx},
        })

    for cl in active_claude:
        sid = cl.get("session_id") or ""
        sid8 = sid[:8]
        node_id = f"node-claude-{sid}"
        is_pinned = node_id in pinned or sid in pinned
        cwd = cl.get("cwd") or os.path.expanduser("~/dev")
        repo, scope = _repo_from_uris_or_cwd(None, cwd)
        if scope == "current" or is_pinned or node_id in imported:
            active_curr_cnt += 1
        terminal = "Orca 터미널" if cl.get("terminal") == "orca" else "CLI 터미널"
        disp = _first_line(cl.get("display_name") or cl.get("title"), "Claude Session")
        model = cl.get("model") or "claude-opus-5-5"
        effort = cl.get("effort") or "high"
        icon_svg, model_pill, _ = _provider_icon_and_pill(model, "claude", "default", effort)
        is_live = sid in live_claude_ids or cl.get("is_running") is True
        res_children_cl: list[tuple[str, str, dict | None]] = []
        claimed_pids_cl: set[int] = set()
        # Delegated maestro workers, then Claude Code's own Task/teammate subagents.
        items = _claude_sub_items(attached_claude_workers_by_parent.get(sid, []) + (cl.get("subagents") or []),
                                  claimed_pids_cl, res_children_cl)
        seen_ctids = set()
        for worker in attached_cdx_workers_by_parent.get(sid, []):
            ctid = worker.get("thread_id", "")
            if not ctid or ctid in seen_ctids or ctid in rendered_codex_child_ids:
                continue
            seen_ctids.add(ctid)
            rendered_codex_child_ids.add(ctid)
            item = _codex_sub_row(worker, ctid, repo, len(items), claimed_pids_cl, res_children_cl)
            if item:
                items.append(item)
        updated_ts = _parse_any_ts(cl.get("updated_at")) or 0.0
        is_busy_cl = (is_live and cl.get("activity") == "busy") or any(item[0] == 0 for item in items)
        root_dot, root_dot_tip = _status_dot_from_age(is_busy_cl, max(0.0, now_sec - updated_ts))
        child_rows_html = [item[2] for item in sorted(items, key=lambda x: (x[0], x[1]))]
        br_cls = "tree-children hidden" if state.get("allCollapsed") is True else "tree-children"
        children_block = f'<div class="{br_cls}" id="br-cl-{sid8}">{_finalize_child_rows(child_rows_html)}</div>' if child_rows_html else ""
        cl_rollup = merge_resources(proc_table, [cl.get("rollup_resources") or cl.get("resources")]
                                    + [res for _, _, res in res_children_cl], proc_res)
        own_pids = [pid for pid in (cl_rollup or {}).get("pids") or () if pid not in claimed_pids_cl]
        cl_self = merge_resources(proc_table, [{"pids": own_pids}], proc_res) if own_pids else None
        cl_kids = merge_resources(proc_table, [{"pids": sorted(claimed_pids_cl)}], proc_res) if claimed_pids_cl else None
        tokens = cl.get("token_count") or cl.get("usage") or {}
        used_k = fmt_k(tokens.get("total_tokens", 0))
        pct = float(tokens.get("percent", 0.0) or 0.0)
        is_keep_alive_cl = is_live or root_dot == "idle-fresh"
        pin_cls = " pinned" if is_pinned else ""
        pin_attr = ' data-pinned="1"' if is_pinned else ""
        run_attr = ' data-running="1"' if is_keep_alive_cl else ""
        pid_txt = f" · PID {cl['pid']}" if is_live and cl.get("pid") else ""
        orca = _orca_worktree(cl.get("orca_worktree"), cwd)
        parent_html = (
            f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}"{pin_attr}{run_attr}>'
            f'<div class="agent-row parent" onclick="toggleDetail(\'det-cl-{sid8}\')">'
            f'<div class="agent-left">'
            f'{_main_prefix_html(root_dot, root_dot_tip, repo, bool(orca), f"{terminal} · {cwd}")}'
            f'{icon_svg}'
            f'<span class="agent-name">{_html_esc(disp)}</span>'
            f'</div>'
            f'<div class="agent-right">'
            f'{_res_slot_html(f"res-{node_id}", cl_rollup, rollup=True, force=is_live, split=(cl_self, cl_kids))}'
            f'{model_pill}'
            f'{_context_pill_html(used_k, pct, "green", cl, is_busy_cl, root_dot)}'
            f'<span class="row-actions">'
            f'{_pin_btn_html(node_id, disp, is_pinned)}'
            f'{_orca_open_or(node_id, sid, disp, cl, _import_btn_html(node_id, f"claude --resume {_html_esc(sid)}"))}'
            f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(disp[:28])}\')">✕</button>'
            f'</span>'
            f'</div></div>'
            f'<div class="agent-detail" id="det-cl-{sid8}"><div class="agent-detail-row">'
            f'<span>Claude Session: {_html_esc(sid)} · {terminal}{pid_txt} · cwd: {_html_esc(cwd)}{_fmt_duration(cl)}</span>'
            f'<span>{used_k} tokens · {_html_esc(model)} · {_html_esc(effort)}</span>'
            f'</div>{_detail_extras_html("claude", sid, repo, True)}</div>{children_block}</div>'
        )
        active_entries.append({
            "is_live_running": is_keep_alive_cl,
            "is_main_orch": False,
            "updated_ts": updated_ts,
            "html": parent_html,
            "orca": orca, "busy": is_busy_cl, "model": _pill_label(model_pill),
            "res": {"kind": "MAIN", "label": disp, "rollup": cl_rollup,
                    "children": [("SELF", f"메인 본체 ({terminal} Claude 세션)", cl_self)] + res_children_cl},
        })

    active_entries.sort(key=lambda item: (
        0 if item["is_main_orch"] else 1,
        0 if item["is_live_running"] else 1,
        -item["updated_ts"],
    ))
    if proc_res and "error" not in proc_res:
        active_parts.append(_res_board_html(active_entries, proc_table, proc_res))
    # Sessions in one Orca worktree share a folder card at the rank of its first member.
    folders: dict[tuple[str, str], list[dict]] = {}
    for item in active_entries:
        key = item.get("orca")
        if not key:
            active_parts.append(item["html"])
        elif key not in folders:
            folders[key] = [item]
            active_parts.append(key)
        else:
            folders[key].append(item)
    active_parts = [_orca_folder_html(part, folders[part], proc_table, proc_res, now_sec)
                    if isinstance(part, tuple) else part for part in active_parts]
    active_parts.append("<!--__ACTIVE_TREE_HTML_END__-->")

    # 2. Build #view-closed (recently worked on & closed/completed within 7d / 30d)
    closed_parts = [
        "<!--__CLOSED_TREE_HTML_START__-->",
        '<div style="display:flex;align-items:center;justify-content:space-between;padding:2px 4px 4px;font-size:10px;color:var(--muted)">'
        '<span id="closed-summary-label">최근 7일 이내 작업 후 완료·닫은 세션</span>'
        '<span>📌 핀 고정 시 완료 정리에서 보호됨</span>'
        '</div>',
    ]
    closed_7d_curr_cnt = 0

    closed_items = []
    for ag in closed_ag:
        cid = ag.get("conversation_id", "")
        age_d = _age_days_from_iso(ag.get("last_modified_time", ""))
        if age_d > 30:
            continue
        closed_items.append(("antigravity", age_d, ag))

    for cdx in closed_cdx:
        if cdx.get("source") == "exec" or cdx.get("agy_origin"):
            continue
        age_d = int(cdx.get("age_days", 0) or 0)
        if age_d > 30:
            continue
        closed_items.append(("codex", age_d, cdx))

    for cl in closed_claude:
        age_sec_cl = max(0.0, now_sec - (_parse_any_ts(cl.get("updated_at")) or 0.0))
        closed_items.append(("claude", int(age_sec_cl // 86400), cl))

    def closed_timestamp(entry):
        kind, age_d, item = entry
        if kind == "codex":
            return float(item.get("updated_at") or 0)
        if kind == "claude":
            return _parse_any_ts(item.get("updated_at")) or 0.0
        value = item.get("last_modified_time") or ""
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return datetime.now().timestamp() - age_d * 86400

    closed_items.sort(key=closed_timestamp, reverse=True)
    rendered_closed = 0

    for kind, age_d, item in closed_items:
        if rendered_closed >= 8:
            break
        if kind == "antigravity":
            cid = item.get("conversation_id", "")
            cid8 = cid[:8]
            node_id = f"closed-agy-{cid8}"
            is_pinned = (node_id in pinned or f"node-agy-{cid8}" in pinned or cid in pinned)
            if not is_pinned and (node_id in dismissed or f"node-agy-{cid8}" in dismissed or cid in dismissed):
                continue
            repo, scope = _repo_from_uris_or_cwd(item.get("workspaces"))
            raw_title = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", (item.get("title") or "Session")).strip()
            if "Subagent" in raw_title and len(raw_title) < 12:
                continue
            if age_d <= 7 and scope == "current":
                closed_7d_curr_cnt += 1
            ctx = item.get("context_usage") or {}
            used_k = fmt_k(ctx.get("used_tokens", 0))
            pct = float(ctx.get("percent", 0.0) or 0.0)
            icon_svg, model_pill, _ = _provider_icon_and_pill(item.get("model", ""), "antigravity")
            age_lbl = _age_label_from_days(age_d)
            cls_dot, cls_tip = _status_dot_from_age(False, _age_sec_from_iso(item.get("last_modified_time") or ""))
            pin_cls = " pinned" if is_pinned else ""
            pin_attr = ' data-pinned="1"' if is_pinned else ""
            closed_parts.append(
                f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}" data-age-days="{age_d}"{pin_attr}>'
                f'<div class="agent-row" onclick="toggleDetail(\'det-cls-agy-{cid8}\')">'
                f'<div class="agent-left">'
                f'<span class="status-dot {cls_dot}" title="{_html_esc(cls_tip)}"></span>'
                f'<span class="repo-tag">{_html_esc(repo)}</span>'
                f'{icon_svg}'
                f'<span class="agent-name">{_html_esc(raw_title)}</span>'
                f'</div>'
                f'<div class="agent-right">'
                f'{model_pill}'
                f'{_context_pill_html(used_k, pct, "blue", item, False, cls_dot)}'
                f'<span class="row-actions">'
                f'{_pin_btn_html(node_id, raw_title, is_pinned)}'
                f'{_open_btn_html(node_id, cid, raw_title, "")}'
                f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(raw_title[:28])}\')">✕</button>'
                f'</span>'
                f'</div></div>'
                f'<div class="agent-detail" id="det-cls-agy-{cid8}"><div class="agent-detail-row">'
                f'<span>Cascade ID: {cid} · 상태: IDLE (작업 완료) · {age_lbl}{_fmt_duration(item)}</span>'
                f'<span>{used_k} / 1M ({pct:.1f}%)</span>'
                f'</div></div></div>'
            )
        elif kind == "claude":
            sid = item.get("session_id") or ""
            sid8 = sid[:8]
            node_id = f"closed-claude-{sid8}"
            is_pinned = node_id in pinned or f"node-claude-{sid}" in pinned or sid in pinned
            if not is_pinned and (node_id in dismissed or f"node-claude-{sid}" in dismissed or sid in dismissed):
                continue
            cwd = item.get("cwd") or os.path.expanduser("~/dev")
            repo, scope = _repo_from_uris_or_cwd(None, cwd)
            if age_d <= 7 and scope == "current":
                closed_7d_curr_cnt += 1
            disp = _first_line(item.get("display_name") or item.get("title"), "Claude Session")
            tokens = item.get("token_count") or item.get("usage") or {}
            used_k = fmt_k(tokens.get("total_tokens", 0))
            icon_svg, model_pill, _ = _provider_icon_and_pill(item.get("model") or "claude-opus-5-5", "claude",
                                                              "default", item.get("effort") or "high")
            upd = _parse_any_ts(item.get("updated_at")) or 0.0
            age_lbl = _age_label_from_days(age_d, int(upd))
            cls_dot, cls_tip = _status_dot_from_age(False, max(0.0, now_sec - upd))
            pin_cls = " pinned" if is_pinned else ""
            pin_attr = ' data-pinned="1"' if is_pinned else ""
            closed_parts.append(
                f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}" data-age-days="{age_d}"{pin_attr}>'
                f'<div class="agent-row" onclick="toggleDetail(\'det-cls-cl-{sid8}\')">'
                f'<div class="agent-left">'
                f'<span class="status-dot {cls_dot}" title="{_html_esc(cls_tip)}"></span>'
                f'<span class="repo-tag">{_html_esc(repo)}</span>'
                f'{icon_svg}'
                f'<span class="agent-name">{_html_esc(disp)}</span>'
                f'</div>'
                f'<div class="agent-right">'
                f'{model_pill}'
                f'{_context_pill_html(used_k, 0.0, "green", item, False, cls_dot)}'
                f'<span class="row-actions">'
                f'{_pin_btn_html(node_id, disp, is_pinned)}'
                f'<button type="button" class="icon-act-btn import-btn" onclick="event.stopPropagation(); importOrCopySession(\'{node_id}\', \'claude --resume {_html_esc(sid)}\', this)" title="세션 가져오기">↗</button>'
                f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(disp[:24])}\')">✕</button>'
                f'</span>'
                f'</div></div>'
                f'<div class="agent-detail" id="det-cls-cl-{sid8}"><div class="agent-detail-row">'
                f'<span>Claude Session: {_html_esc(sid)} · 경로: {_html_esc(cwd)} · {age_lbl}{_fmt_duration(item)}</span>'
                f'<span>{used_k} tokens</span>'
                f'</div></div></div>'
            )
        else:
            tid = item.get("thread_id", "")
            tid8 = tid[:8]
            cwd = item.get("cwd") or os.path.expanduser("~/dev")
            disp_raw = item.get("display_name") or item.get("title") or "Codex Session"
            is_discord = "discord-translator" in cwd or "discord" in disp_raw.lower() or tid8 == "019d61b1"
            node_id = "node-cdx-discord" if is_discord else f"closed-cdx-{tid8}"
            is_pinned = (node_id in pinned or f"node-cdx-{tid}" in pinned or tid in pinned)
            if not is_pinned and (node_id in dismissed or f"node-cdx-{tid8}" in dismissed or tid in dismissed):
                continue
            repo, scope = _repo_from_uris_or_cwd(None, cwd)
            if age_d <= 7 and scope == "current":
                closed_7d_curr_cnt += 1
            disp = re.sub(r"^(?:✦\s*|🪐\s*)?(?:\[AGY\]\s*)?", "", disp_raw).strip()
            tc = item.get("token_count") or {}
            used_k = fmt_k(tc.get("total_tokens", 0))
            pct = float(tc.get("percent", 0.0) or 0.0)
            tctx = item.get("turn_context") or {}
            icon_svg, model_pill, _ = _provider_icon_and_pill(
                tctx.get("model", "gpt-6.1-sol"),
                "codex",
                resolve_speed_tier(tctx.get("service_tier"), item.get("tier")),
                tctx.get("reasoning_effort", "ultra"),
            )
            age_lbl = _age_label_from_days(age_d, item.get("updated_at"))
            cls_dot, cls_tip = _status_dot_from_age(False, max(0.0, now_sec - float(item.get("updated_at") or 0.0)))
            child_cnt = int(item.get("total_children_count", 0) or 0)
            sub_info = f" · 하위 {child_cnt}개" if child_cnt > 0 else ""
            pin_cls = " pinned" if is_pinned else ""
            pin_attr = ' data-pinned="1"' if is_pinned else ""
            closed_parts.append(
                f'<div class="tree-node{pin_cls}" data-node-id="{node_id}" data-scope="{scope}" data-age-days="{age_d}"{pin_attr}>'
                f'<div class="agent-row" onclick="toggleDetail(\'det-cls-cdx-{tid8}\')">'
                f'<div class="agent-left">'
                f'<span class="status-dot {cls_dot}" title="{_html_esc(cls_tip)}"></span>'
                f'<span class="repo-tag">{_html_esc(repo)}</span>'
                f'{icon_svg}'
                f'<span class="agent-name">{_html_esc(disp)}</span>'
                f'</div>'
                f'<div class="agent-right">'
                f'{model_pill}'
                f'{_context_pill_html(used_k, pct, "green", item, False, cls_dot)}'
                f'<span class="row-actions">'
                f'{_pin_btn_html(node_id, disp, is_pinned)}'
                f'<button type="button" class="icon-act-btn codex" onclick="event.stopPropagation(); openCodexDirectBar(\'{tid}\', \'{_html_esc(disp[:24])}\', \'{_html_esc(cwd)}\')" title="Codex 직접 명령 보내기 (하단 팝오버)" aria-label="Codex 직접 명령 보내기"><svg width="12" height="12" aria-hidden="true"><use href="#ico-cmd-send"/></svg></button>'
                f'<button type="button" class="icon-act-btn import-btn" onclick="event.stopPropagation(); importOrCopySession(\'{node_id}\', \'codex resume {tid}\', this)" title="왼쪽 사이드바(dev)로 가져오기" aria-label="왼쪽 사이드바(dev)로 가져오기"><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 2h5v12H9M1 8h9M6 4l4 4-4 4"/></svg></button>'
                f'<button type="button" class="dismiss-btn" title="목록에서 숨기기" onclick="event.stopPropagation(); dismissNode(\'{node_id}\', \'{_html_esc(disp[:24])}\')">✕</button>'
                f'</span>'
                f'</div></div>'
                f'<div class="agent-detail" id="det-cls-cdx-{tid8}"><div class="agent-detail-row">'
                f'<span>Codex Thread: {tid} · 경로: {_html_esc(cwd)} · {age_lbl}{_fmt_duration(item)}</span>'
                f'<span>{used_k} / 475k ({pct:.1f}%){sub_info}</span>'
                f'</div></div></div>'
            )

        rendered_closed += 1

    closed_parts.append("<!--__CLOSED_TREE_HTML_END__-->")
    return "".join(active_parts), "".join(closed_parts), active_curr_cnt, closed_7d_curr_cnt


def _static_clock_path(progress: float | None) -> str:
    if progress is None or progress <= 0:
        return ""
    if progress >= 1:
        return "M32 10 A22 22 0 1 1 32 54 A22 22 0 1 1 32 10 Z"
    # Plain elapsed pie wedge; must stay byte-compatible with clockPath() in usage_widget_template.html.
    angle = 2 * math.pi * progress
    x = f"{32 + 22 * math.sin(angle):.2f}"
    y = f"{32 - 22 * math.cos(angle):.2f}"
    large = 1 if progress > 0.5 else 0
    return f"M32 32 L32 10 A22 22 0 {large} 1 {x} {y} Z"


def _static_compact_rem(resets_at: int | None, fallback: str) -> str:
    if not resets_at:
        return fallback or "-"
    sec = int(math.ceil(resets_at - datetime.now().timestamp()))
    if sec <= 0:
        return "곧 리셋"
    d = sec // 86400
    h = (sec % 86400) // 3600
    m = (sec % 3600) // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _render_model_icons_html(icons: list[str], size: int = 12) -> str:
    if not icons:
        return ""
    if len(icons) == 1:
        ico = icons[0]
        cls = "win-ico codex-tint" if "codex" in ico else "win-ico"
        return f'<span class="win-model-icons"><svg class="{cls}" width="{size}" height="{size}"><use href="{ico}"/></svg></span>'
    parts = []
    for i, ico in enumerate(icons):
        if i > 0:
            parts.append('<span class="ico-pipe" aria-hidden="true"></span>')
        cls = "win-ico codex-tint" if "codex" in ico else "win-ico"
        parts.append(f'<svg class="{cls}" width="{size}" height="{size}"><use href="{ico}"/></svg>')
    return f'<span class="win-model-icons dual">{"".join(parts)}</span>'


def build_static_quota_grid_html(accounts: list[dict]) -> str:
    now_sec = datetime.now().timestamp()
    cal_caps = load_calibrated_pool_token_capacities()
    sorted_accs = sorted(
        accounts,
        key=lambda a: 1 if max([0.0] + [float(w.get("remaining") or 0.0) for w in a.get("windows", [])]) > 0 else 0,
        reverse=True,
    )
    parts = ["<!--__QUOTA_GRID_HTML_START__-->"]
    for acc in sorted_accs:
        wins = acc.get("windows", [])
        span = max(1, len(wins))
        prov = acc.get("provider", "")
        acc_ico = acc.get("accountIcon") or ("#ico-agy-logo" if prov == "antigravity" else f"#ico-{prov}")
        win_html = []
        for idx, w in enumerate(wins):
            rem = float(w.get("remaining") or 0.0)
            if rem >= 100.0:
                st_id, st_label, disp_pct = ("in-use", "사용중", "100%") if w.get("started") else ("unused", "미사용", "100%")
            elif rem <= 0.0:
                st_id, st_label, disp_pct = "exhausted", "소진 · 리셋 대기", "0%"
            else:
                st_id, st_label = "in-use", "사용중"
                disp_pct = f"{min(math.floor(rem * 10) / 10, 99.9):.1f}%"
            w_min = w.get("windowMinutes")
            r_at = w.get("resetsAt")
            prog = None
            if w_min and r_at:
                prog = max(0.0, min(1.0, 1.0 - (r_at - now_sec) / (w_min * 60.0)))
            path_d = _static_clock_path(prog)
            rem_cd = _static_compact_rem(r_at, w.get("resetText", ""))
            m_icons = w.get("modelIcons") or [w.get("modelIcon") or acc_ico]
            p_kind = w.get("periodKind") or ("5h" if "5h" in str(w.get("shortPeriod", "")) else "7d")
            short_p = w.get("shortPeriod", p_kind)
            token_line = ""
            total_tok = w.get("totalTokens") or cal_caps.get(w.get("poolKey"))
            if total_tok and rem is not None:
                rem_tok = int(round(total_tok * (rem / 100.0)))
                token_line = f"\n토큰 용량: 잔여 약 {format_token_korean(rem_tok)} / 총 {format_token_korean(total_tok)} 토큰"

            tip = (
                f'{acc.get("name","")} · {w.get("label","")}\n'
                f'바깥 링: 남음 {disp_pct} ({st_label})\n'
                f'안쪽 시계원: {"리셋 대기중 (미사용)" if prog is None else f"기간 {int(round(prog * 100))}% 경과"}\n'
                f'리셋: {rem_cd} ({w.get("resetText","")} KST)'
                f'{token_line}'
            )

            glow = "g-full" if rem >= 100 else "g-hi" if rem > 50 else "g-mid" if rem >= 20 else "g-low"
            win_html.append(
                f'<div class="usage-window {st_id} {glow}">'
                f'<button type="button" class="usage-gauge-button" data-tip="{_html_esc(tip)}" title="{_html_esc(tip)}">'
                f'<div class="usage-donut {st_id}">'
                f'<svg viewBox="0 0 64 64" aria-hidden="true">'
                f'<circle cx="32" cy="32" r="22" class="usage-clock-background"></circle>'
                f'<path class="usage-clock-fill" style="--e:{(prog or 0) * 360:.1f}deg" d="{path_d}"></path>'
                f'<circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-track"></circle>'
                f'<circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-remaining" stroke-dasharray="{rem} 100" transform="rotate(-90 32 32)"></circle>'
                f'</svg>'
                f'<div class="usage-donut-center">'
                f'<strong>{disp_pct}</strong>'
                f'<small class="usage-center-caption">남음</small>'
                f'<small class="usage-center-period p-{p_kind}">{short_p}</small>'
                f'</div>'
                f'</div></button>'
                f'<span class="usage-window-title" title="{_html_esc(w.get("label",""))}">'
                f'{_render_model_icons_html(m_icons, 11)}'
                f'</span>'
                f'<span class="usage-quota-state {st_id}">{st_label}</span>'
                f'<span class="usage-reset">{rem_cd}</span>'
                f'</div>'
            )
        parts.append(
            f'<fieldset class="usage-account" data-provider="{prov}" style="--cols: {span};">'
            f'<legend class="usage-account-heading" title="{_html_esc(acc.get("name",""))}">'
            f'<svg class="acc-head-ico" width="13" height="13"><use href="{acc_ico}"/></svg>'
            f'<strong>{acc.get("short","")}</strong>'
            f'</legend>'
            f'<div class="usage-account-windows">{"".join(win_html)}</div>'
            f'</fieldset>'
        )
    parts.append("<!--__QUOTA_GRID_HTML_END__-->")
    return "".join(parts)


QUOTA_STRIP_BLOCK_RE = re.compile(r"<!--__QUOTA_STRIP_START__-->.*?<!--__QUOTA_STRIP_END__-->", re.S)
# (box class, ((sub-pool, poolKeys), ...)) - mirrors QUOTA_STRIP in usage_widget_template.html.
# A box with several sub-pools (AGY) wraps each pair in an inner q-sub border.
QUOTA_STRIP_BOXES = (
    ("agy", (("Gemini", ("agy_gemini_5h", "agy_gemini_7d")), ("AGY Claude", ("agy_claude_5h", "agy_claude_7d")))),
    ("cdx", (("Codex", ("codex_7d",)),)),
    ("cld", (("Claude", ("claude_5h", "claude_7d")),)),
)


def _strip_dial_html(sub: str, w: dict, now_sec: float) -> str:
    """Mini glowing donut (same ring + elapsed sweep as the grid donut), integer % and period only."""
    rem = max(0.0, min(100.0, float(w.get("remaining") or 0.0)))
    n = int(math.floor(rem + 0.5))  # matches JS Math.round (Python round() is banker's rounding)
    st_id = "unused" if rem >= 100 and not w.get("started") else "exhausted" if rem <= 0 else "in-use"
    glow = "g-full" if rem >= 100 else "g-hi" if rem > 50 else "g-mid" if rem >= 20 else "g-low"
    w_min, r_at = w.get("windowMinutes"), w.get("resetsAt")
    prog = max(0.0, min(1.0, 1.0 - (r_at - now_sec) / (w_min * 60.0))) if w_min and r_at else None
    p = w["poolKey"][-2:]
    tip = f'{sub} {p} {n}% · {_static_compact_rem(r_at, w.get("resetText", ""))}'
    return (
        f'<span class="{glow}" title="{_html_esc(tip)}"><span class="usage-donut {st_id}">'
        f'<svg viewBox="0 0 64 64" aria-hidden="true">'
        f'<circle cx="32" cy="32" r="22" class="usage-clock-background"></circle>'
        f'<path class="usage-clock-fill" style="--e:{(prog or 0) * 360:.1f}deg" d="{_static_clock_path(prog)}"></path>'
        f'<circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-track"></circle>'
        f'<circle cx="32" cy="32" r="27" pathLength="100" class="usage-ring-remaining" stroke-dasharray="{rem} 100" transform="rotate(-90 32 32)"></circle>'
        f'</svg><span class="usage-donut-center"><strong>{n}%</strong>'
        f'<small class="usage-center-period p-{p}">{p}</small></span></span></span>'
    )


def build_top_quota_strip_html(accounts: list[dict]) -> str:
    """Toolbar quota strip: tinted box per pool holding mini glowing dials (same markup as renderQuotaStrip())."""
    ws = {w.get("poolKey"): w for acc in accounts for w in acc.get("windows", [])}
    now_sec = datetime.now().timestamp()
    boxes = []
    for cls, groups in QUOTA_STRIP_BOXES:
        subs = []
        for sub, keys in groups:
            dials = "".join(_strip_dial_html(sub, ws[k], now_sec) for k in keys if k in ws)
            if dials:
                subs.append(f'<span class="q-sub">{dials}</span>' if len(groups) > 1 else dials)
        if subs:
            boxes.append(f'<span class="q-box q-{cls}">' + "".join(subs) + "</span>")
    return "<!--__QUOTA_STRIP_START__-->" + "".join(boxes) + "<!--__QUOTA_STRIP_END__-->"


PC_TELEMETRY_BLOCK_RE = re.compile(
    r"<!--__PC_TELEMETRY_HTML_START__-->.*?<!--__PC_TELEMETRY_HTML_END__-->",
    re.S,
)


def build_optional_pc_telemetry_html(open_drawer: bool = False) -> str:
    """Return rendered PC telemetry HTML if pc_telemetry_ext.py is installed and enabled; otherwise empty markers."""
    try:
        import pc_telemetry_ext as pc_ext
        if pc_ext.is_enabled():
            return pc_ext.render_pc_telemetry_html(open_drawer=open_drawer)
    except Exception:
        pass
    return "<!--__PC_TELEMETRY_HTML_START__--><!--__PC_TELEMETRY_HTML_END__-->"


HOST_PILL_BLOCK_RE = re.compile(r"<!--__HOST_PILL_START__-->.*?<!--__HOST_PILL_END__-->", re.S)


def build_host_pill_html(agent_count: int = 0) -> str:
    """Host identity and visible agent count, independent of Orca inventory."""
    import socket
    host = socket.gethostname()
    where = "🌐 SSH 원격" if os.environ.get("SSH_CONNECTION") else "🖥 로컬"
    label = f"{where} · {host} · 에이전트 {agent_count}"
    return ("<!--__HOST_PILL_START__-->"
            f'<span class="remote-pill" title="{_html_esc(label)}">{_html_esc(label)}</span>'
            "<!--__HOST_PILL_END__-->")


def inject_live_data(template: str, data: dict) -> str:
    """Replace all live-data regions (SERVER_STATE, ACCOUNTS, QUOTA_GRID, QUOTA_STRIP, PC_TELEMETRY, ACTIVE_TREE, CLOSED_TREE) at render time for 0ms first paint."""
    accounts = build_accounts(data)
    state = _load_persisted_widget_state()

    state_js = (
        "/*__SERVER_STATE_JSON_START__*/\n"
        f"    const SERVER_STATE = {json.dumps(state, ensure_ascii=False)};\n"
        "    /*__SERVER_STATE_JSON_END__*/"
    )
    html = SERVER_STATE_BLOCK_RE.sub(lambda _m: state_js, template, count=1)
    density = "expanded" if state.get("density") == "expanded" else "compact"
    html = html.replace('id="usage-panel" data-density="compact"', f'id="usage-panel" data-density="{density}"', 1)
    html = html.replace('>확장 ▾</button>', '>컴팩트 ▴</button>' if density == "expanded" else '>확장 ▾</button>', 1)

    accounts_js = (
        "/*__ACCOUNTS_JSON_START__*/\n"
        f"    /* live: check_all_usage.py @ {data.get('timestamp_kst', '')} */\n"
        f"    const ACCOUNTS = {json.dumps(accounts, ensure_ascii=False, indent=2)};\n"
        "    /*__ACCOUNTS_JSON_END__*/"
    )
    html = ACCOUNTS_BLOCK_RE.sub(lambda _m: accounts_js, html, count=1)

    cal_caps = load_calibrated_pool_token_capacities()
    cap_js = (
        "/*__POOL_CAPACITIES_JSON_START__*/\n"
        f"    const POOL_TOKEN_CAPACITIES = {json.dumps(cal_caps, indent=2)};\n"
        "    /*__POOL_CAPACITIES_JSON_END__*/"
    )
    if POOL_CAPACITIES_BLOCK_RE.search(html):
        html = POOL_CAPACITIES_BLOCK_RE.sub(lambda _m: cap_js, html, count=1)

    if QUOTA_GRID_BLOCK_RE.search(html):
        static_grid = build_static_quota_grid_html(accounts)
        html = QUOTA_GRID_BLOCK_RE.sub(lambda _m: static_grid, html, count=1)

    if QUOTA_STRIP_BLOCK_RE.search(html):
        strip_html = build_top_quota_strip_html(accounts)
        html = QUOTA_STRIP_BLOCK_RE.sub(lambda _m: strip_html, html, count=1)

    active_html, closed_html, act_cnt, cls_cnt = build_active_and_closed_trees_html(data, state)
    if HOST_PILL_BLOCK_RE.search(html):
        pill_html = build_host_pill_html(act_cnt)
        html = HOST_PILL_BLOCK_RE.sub(lambda _m: pill_html, html, count=1)

    if PC_TELEMETRY_BLOCK_RE.search(html):
        pc_html = build_optional_pc_telemetry_html(open_drawer=bool(state.get('pcMonOpen', False)))
        html = PC_TELEMETRY_BLOCK_RE.sub(lambda _m: pc_html, html, count=1)

    if ACTIVE_TREE_BLOCK_RE.search(html):
        html = ACTIVE_TREE_BLOCK_RE.sub(lambda _m: active_html, html, count=1)
    if CLOSED_TREE_BLOCK_RE.search(html):
        html = CLOSED_TREE_BLOCK_RE.sub(lambda _m: closed_html, html, count=1)

    html = re.sub(r'(id="badge-active-cnt">)\d+(<)', rf"\g<1>{act_cnt}\g<2>", html, count=1)
    html = re.sub(r'(id="badge-closed-cnt">)\d+(<)', rf"\g<1>{cls_cnt}\g<2>", html, count=1)
    return _inject_resource_assets(html)


def compact_widget_html(html: str) -> str:
    """Keep standalone artifacts small without fetching assets from localhost.

    The readable template remains the source of truth. Chromium's native gzip
    decoder restores it, including its inline scripts, before bridge startup.
    """
    if len(html.encode("utf-8")) < 45000:
        return html
    slim = re.sub(r"/\*(?!__).*?\*/", "", html, flags=re.S)
    slim = re.sub(r"<!--(?!__).*?-->", "", slim, flags=re.S)
    slim = re.sub(r"\n[ \t]+", "\n", slim)
    if len(gzip.compress(slim.encode("utf-8"), compresslevel=9)) * 4 // 3 + 800 >= 58000:
        # Minify only oversized artifacts. Keep public function names for inline handlers.
        import shutil
        terser = shutil.which("terser")
        if terser:
            def minify_script(match):
                try:
                    result = subprocess.run([terser, "--compress", "--mangle", "--comments", "/__/"],
                        input=match.group(1), text=True, capture_output=True, timeout=20)
                    if result.returncode == 0 and result.stdout.strip():
                        return "<script>" + result.stdout.strip() + "</script>"
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return match.group(0)
            slim = re.sub(r"<script>(.*?)</script>", minify_script, slim, flags=re.S)
    compressed = gzip.compress(slim.encode("utf-8"), compresslevel=9, mtime=0)
    payload = base64.b64encode(compressed).decode("ascii")
    if len(payload) + 800 >= 58000:
        # Six bytes fit in seven base-122 ASCII digits. Exclude JS string and HTML
        # delimiters, NUL and line breaks; no external decoder or asset is needed.
        alphabet = "".join(chr(c) for c in range(1, 128) if c not in (10, 13, 39, 60, 92))
        digits = []
        for offset in range(0, len(compressed), 6):
            value = int.from_bytes(compressed[offset:offset + 6].ljust(6, b"\0"), "big")
            chunk = []
            for _ in range(7):
                value, digit = divmod(value, 122)
                chunk.append(alphabet[digit])
            digits.extend(reversed(chunk))
        packed = "".join(digits)
        return (
            '<!DOCTYPE html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="background:#0b1120;color:#e5e7eb;margin:0"><script>'
            "(async()=>{try{const p='" + packed + "',a=[];for(let c=1;c<128;c++)"
            "if(![10,13,39,60,92].includes(c))a.push(String.fromCharCode(c));"
            "const b=[];for(let i=0;i<p.length;i+=7){let n=0;for(let j=0;j<7;j++)n=n*122+a.indexOf(p[i+j]);"
            "const t=Array(6);for(let j=5;j>=0;j--){t[j]=n%256;n=Math.floor(n/256)}b.push(...t)}"
            f"const s=new Blob([Uint8Array.from(b.slice(0,{len(compressed)}))]).stream().pipeThrough(new DecompressionStream('gzip'));"
            "const h=await new Response(s).text();document.open();document.write(h);document.close();"
            "}catch(e){document.body.textContent='Widget load failed: '+e.message}})();</script>"
        )

    return (
        '<!DOCTYPE html><html lang="ko" style="background:#0b1120;color:#e5e7eb"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head><body style="background:#0b1120;color:#e5e7eb;margin:0"><script>'
        '(async()=>{try{const bytes=Uint8Array.from(atob("' + payload + '"),c=>c.charCodeAt(0));'
        'const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));'
        'const html=await new Response(stream).text();document.open();document.write(html);document.close();'
        '}catch(e){document.body.textContent="Usage widget could not load: "+e.message;}})();'
        '</script></body></html>'
    )


def build_widget_html(data: dict) -> str:
    """Legacy fallback renderer (used only when no marker-based template is available)."""
    accounts = build_accounts(data)

    trees_raw = data.get("agent_trees", {})
    agent_trees = []
    for ag_node in trees_raw.get("antigravity", []):
        if "error" not in ag_node:
            agent_trees.append(convert_agy_node(ag_node))
    for cdx_node in trees_raw.get("codex", []):
        if "error" not in cdx_node:
            agent_trees.append(convert_codex_node(cdx_node, 0))

    live_js = (
        f"const UPDATED_AT_KST = {json.dumps(data.get('timestamp_kst', ''), ensure_ascii=False)};\n"
        f"    const ACCOUNTS = {json.dumps(accounts, ensure_ascii=False, indent=2)};\n"
        f"    const AGENT_TREES = {json.dumps(agent_trees, ensure_ascii=False, indent=2)};"
    )
    cal_caps = load_calibrated_pool_token_capacities()
    cap_js = (
        "/*__POOL_CAPACITIES_JSON_START__*/\n"
        f"    const POOL_TOKEN_CAPACITIES = {json.dumps(cal_caps, indent=2)};\n"
        "    /*__POOL_CAPACITIES_JSON_END__*/"
    )
    res = WIDGET_TEMPLATE.replace("/*__LIVE_DATA_JS__*/", live_js)
    if POOL_CAPACITIES_BLOCK_RE.search(res):
        res = POOL_CAPACITIES_BLOCK_RE.sub(lambda _m: cap_js, res, count=1)
    return res


TEMPLATE_FILENAME = "usage_widget_template.html"


def pick_template(target: str, script_dir: str) -> tuple[str | None, str]:
    """Latest template priority: existing target with markers → skill template copy → legacy inline template."""
    for cand in (os.path.join(script_dir, TEMPLATE_FILENAME), target):
        try:
            with open(cand, encoding="utf-8") as f:
                txt = f.read()
            if ACCOUNTS_BLOCK_RE.search(txt):
                return txt, cand
        except OSError:
            continue
    return None, "legacy WIDGET_TEMPLATE"


def main(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(description="Generate the AOH usage & multi-agent tree widget HTML.")
    parser.add_argument("target", nargs="?", default=None, help="output HTML path (positional, legacy)")
    parser.add_argument("-o", "--output", dest="output", default=None, help="output HTML path (overrides positional target)")
    args = parser.parse_args(argv)
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import check_all_usage as cau

    data = cau.collect_usage(include_agent_trees=True)
    target = args.output or args.target or "/tmp/usage_widget.html"

    template, source = pick_template(target, script_dir)
    html_str = compact_widget_html(inject_live_data(template, data) if template else build_widget_html(data))

    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(html_str)
    meta_target = target + ".metadata.json"
    if not os.path.exists(meta_target):
        with open(meta_target, "w", encoding="utf-8") as mf:
            json.dump(
                {
                    "summary": "AOH dual-ring + clock-pie quota widget and narrow-safe multi-agent execution tree.",
                    "updatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "userFacing": True,
                },
                mf,
                ensure_ascii=False,
                indent=2,
            )
    print(f"Generated {target} ({len(html_str.encode("utf-8"))} bytes) · template: {source}")



if __name__ == "__main__":
    main()
