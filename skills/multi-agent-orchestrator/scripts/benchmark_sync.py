#!/usr/bin/env python3
"""Zero-Token Daily AI Coding Benchmark & New Subscription Model Detector.

Runs purely in Python (0 LLM tokens) with a 24-hour disk cache:
1. Detects newly released/enabled models across Antigravity RPC, OpenAI Codex cache,
   and Claude Code CLI by comparing live subscription model catalogs against the baseline.
2. Reads Aider Polyglot JSON/YAML coding results once every 24 hours and updates
   matching profiles while retaining the configured four-domain planning baselines.
"""

from __future__ import annotations

import copy
import json
import math
import re
import os
import time
import urllib.request
from typing import Any

CACHE_PATH = os.path.expanduser("~/.gemini/antigravity/maestro_benchmark_cache.json")
LEGACY_CACHE_PATH = os.path.expanduser("~/.gemini/antigravity/orca_benchmark_cache.json")
CACHE_TTL_SEC = 86400  # 24 hours (하루 1회 자동 갱신, 평상시 0ms 로컬 캐시 읽기)

# Known baseline models currently configured in our orchestrator
BASELINE_MODELS = {
    "antigravity": {
        "MODEL_PLACEHOLDER_M417": "Gemini 4 Argon (High)",
        "MODEL_PLACEHOLDER_M418": "Gemini 4 Argon (Medium)",
        "MODEL_PLACEHOLDER_M322": "Gemini 3.8 Flash",
        "MODEL_PLACEHOLDER_M402": "Claude Opus 5.5 (High)",
        "MODEL_PLACEHOLDER_M403": "Claude Opus 5.5 (Medium)",
        "MODEL_PLACEHOLDER_M405": "Claude Sonnet 5.5 (High)",
        "MODEL_PLACEHOLDER_M350": "GPT-OSS 120B",
    },
    "codex": {
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
    },
}

# User-supplied planning baselines, not claims of published leaderboard results.
# Aider can update coding only; reasoning/UI/speed and quota estimates stay intact.
DOMAINS = ("coding", "reasoning", "frontend_ui", "speed")
CACHE_SCHEMA_VERSION = 4
LEADERBOARD_URL = "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml"


def _profile(model, pool, effort, coding, reasoning, frontend_ui, speed, quota_multiplier, **kwargs):
    d = dict(model=model, pool=pool, effort=effort, coding=coding, reasoning=reasoning,
             frontend_ui=frontend_ui, speed=speed, quota_multiplier=quota_multiplier,
             source="configured baseline")
    d.update(kwargs)
    return d


DEFAULT_BENCHMARK_MATRIX = {
    "gpt-6-astra (ultra/high)": _profile(
        "gpt-6-astra", "codex-astra", "ultra", 83.8, 94.2, 87.5, 62.0, 3.5,
        swe_bench_verified=87.0, terminal_bench=58.2, swe_lancer=66.3, long_horizon_coding=94.0,
        webdev_elo=1776, design_elo=1344, frontend_ui_motion=95.0,
        gpqa_diamond=96.2, arc_agi=95.0, hle=54.8, research_orchestration=97.0,
        osworld=72.6, computer_use=95.0, general_coding=85.4,
        weekly_quota_burn_pct=0.38, avg_net_tokens="120k", avg_duration_sec=170.0, avg_cost_krw=3500,
        citations="Aider 83.8% · SWE-bench 87.0% · ARC-AGI-2 95.0% · OSWorld 72.6%"
    ),
    "gpt-6-astra (medium)": _profile(
        "gpt-6-astra", "codex-astra", "medium", 80.2, 89.0, 85.0, 74.0, 1.0,
        swe_bench_verified=84.0, terminal_bench=58.2, swe_lancer=66.3, long_horizon_coding=93.0,
        webdev_elo=1776, design_elo=1344, frontend_ui_motion=93.5,
        gpqa_diamond=96.2, arc_agi=95.0, hle=54.8, research_orchestration=96.0,
        osworld=72.6, computer_use=95.0, general_coding=85.0,
        weekly_quota_burn_pct=0.207, avg_net_tokens="64.7k", avg_duration_sec=182.7, avg_cost_krw=1500,
        citations="Aider 80.2% · SWE-bench 84.0% · ARC-AGI-2 95.0% · 0.207%/회"
    ),
    "gpt-6.1-sol (ultra)": _profile(
        "gpt-6.1-sol", "codex", "ultra", 82.4, 92.5, 86.5, 68.0, 4.0,
        swe_bench_verified=87.5, terminal_bench=58.2, swe_lancer=66.3, long_horizon_coding=97.0,
        webdev_elo=1753, frontend_ui_motion=91.0,
        gpqa_diamond=94.5, hle=52.9, research_orchestration=94.8,
        osworld=72.6, computer_use=95.0, general_coding=85.0,
        weekly_quota_burn_pct=0.359, avg_net_tokens="112.2k", avg_duration_sec=119.7, avg_cost_krw=4000,
        citations="Aider 82.4% · SWE-bench 87.5% · Terminal-Bench 58.2% · 0.359%/회"
    ),
    "gpt-6.1-sol (high)": _profile(
        "gpt-6.1-sol", "codex", "high", 79.4, 88.5, 85.0, 78.0, 1.0,
        swe_bench_verified=85.0, terminal_bench=58.2, swe_lancer=66.3, long_horizon_coding=98.0,
        webdev_elo=1753, frontend_ui_motion=90.5,
        gpqa_diamond=94.5, hle=52.9, research_orchestration=93.5,
        osworld=72.6, computer_use=96.0, general_coding=83.5,
        weekly_quota_burn_pct=0.400, avg_net_tokens="124.9k", avg_duration_sec=185.7, avg_cost_krw=3000,
        citations="Aider 79.4% · SWE-bench 85.0% · OSWorld 72.6% · 0.400%/회"
    ),
    "gpt-6.1-sol (medium)": _profile(
        "gpt-6.1-sol", "codex", "medium", 76.8, 84.0, 82.5, 84.0, 0.5,
        swe_bench_verified=82.0, terminal_bench=58.2, long_horizon_coding=92.0,
        frontend_ui_motion=88.0, research_orchestration=90.0,
        osworld=72.6, computer_use=93.0, general_coding=79.4,
        weekly_quota_burn_pct=0.25, avg_net_tokens="65k", avg_duration_sec=120.0, avg_cost_krw=1500,
        citations="Aider 76.8% · SWE-bench 82.0% · OSWorld 72.6%"
    ),
    "claude-opus-5-5 (ultra)": _profile(
        "claude-opus-5-5", "claude", "ultra", 83.2, 93.8, 93.2, 65.0, 3.0,
        swe_bench_verified=89.0, terminal_bench=64.8, long_horizon_coding=95.5,
        webdev_elo=1807, design_elo=1335, frontend_ui_motion=97.5,
        gpqa_diamond=93.0, hle=61.4, research_orchestration=96.5,
        osworld=65.0, computer_use=84.0, general_coding=86.1,
        weekly_quota_burn_pct=0.45, avg_net_tokens="141k", avg_duration_sec=162.8, avg_cost_krw=12000,
        citations="Aider 83.2% · SWE-bench 89.0% · HLE 61.4% (1위) · WebDev 1807 (1위)"
    ),
    "claude-opus-5-5 (xhigh)": _profile(
        "claude-opus-5-5", "claude", "xhigh", 81.2, 91.5, 92.0, 72.0, 1.2,
        swe_bench_verified=87.5, terminal_bench=64.8, long_horizon_coding=95.0,
        webdev_elo=1807, design_elo=1335, frontend_ui_motion=97.0,
        gpqa_diamond=93.0, hle=61.4, research_orchestration=95.8,
        osworld=65.0, computer_use=83.0, general_coding=84.4,
        weekly_quota_burn_pct=0.25, avg_net_tokens="150k", avg_duration_sec=170.0, avg_cost_krw=7000,
        citations="Aider 81.2% · SWE-bench 87.5% · Terminal-Bench 64.8%"
    ),
    "claude-opus-5-5 (high)": _profile(
        "claude-opus-5-5", "claude", "high", 79.5, 89.5, 91.5, 76.0, 1.0,
        swe_bench_verified=86.5, terminal_bench=64.8, long_horizon_coding=93.5,
        webdev_elo=1807, design_elo=1335, frontend_ui_motion=97.5,
        gpqa_diamond=93.0, hle=61.4, research_orchestration=94.5,
        osworld=65.0, computer_use=82.0, general_coding=83.0,
        weekly_quota_burn_pct=0.176, avg_net_tokens="171.8k", avg_duration_sec=178.4, avg_cost_krw=5000,
        citations="Aider 79.5% · SWE-bench 86.5% · WebDev 1807 Elo (1위) · 0.176%/회"
    ),
    "claude-opus-5-fast": _profile(
        "claude-opus-5-fast", "claude-fast", "high", 77.5, 86.0, 90.5, 96.0, 2.0,
        swe_bench_verified=84.0, long_horizon_coding=91.0,
        webdev_elo=1790, frontend_ui_motion=95.0,
        gpqa_diamond=91.0, research_orchestration=92.0,
        osworld=65.0, computer_use=80.0, general_coding=80.8,
        weekly_quota_burn_pct=0.35, avg_net_tokens="160k", avg_duration_sec=45.0, avg_cost_krw=4000,
        citations="Fast Interactive UI · 45s ultra response"
    ),
    "claude-sonnet-5-5 (high)": _profile(
        "claude-sonnet-5-5", "agy-sonnet", "high", 78.0, 85.0, 89.5, 88.0, 0.6,
        swe_bench_verified=83.0, long_horizon_coding=89.0,
        frontend_ui_motion=92.0, research_orchestration=91.0,
        computer_use=78.0, general_coding=80.5,
        weekly_quota_burn_pct=0.30, avg_net_tokens="120k", avg_duration_sec=60.0, avg_cost_krw=1000,
        citations="Sonnet 5.5 High · Antigravity"
    ),
    "Gemini 4 Argon (High)": _profile(
        "Gemini 4 Argon", "agy-argon", "high", 77.2, 88.0, 88.5, 82.0, 1.0,
        swe_bench_verified=79.0, terminal_bench=54.0, long_horizon_coding=88.0,
        webdev_elo=1678, frontend_ui_motion=86.0,
        gpqa_diamond=96.4, arc_agi=91.3, hle=57.1, research_orchestration=97.5,
        osworld=60.0, computer_use=75.0, general_coding=78.1,
        weekly_quota_burn_pct=0.25, avg_net_tokens="130k", avg_duration_sec=180.0, avg_cost_krw=3000,
        citations="GPQA Diamond 96.4% (1위) · ARC-AGI-3 97.5% · Deep Orchestration"
    ),
    "Gemini 3.8 Flash (Fast)": _profile(
        "Gemini 3.8 Flash", "agy-flash", "low", 73.5, 79.0, 83.0, 98.5, 0.1,
        swe_bench_verified=75.0, long_horizon_coding=80.0,
        frontend_ui_motion=83.0, research_orchestration=86.0, fast_explore=98.5,
        computer_use=70.0, general_coding=74.2,
        weekly_quota_burn_pct=0.02, avg_net_tokens="40k", avg_duration_sec=40.0, avg_cost_krw=500,
        citations="Gemini 3.8 Flash · Fast Explore & Scanning (98.5)"
    ),
    "gpt-6-luna (medium)": _profile(
        "gpt-6-luna", "codex-luna", "medium", 78.5, 82.0, 80.0, 96.0, 0.35,
        swe_bench_verified=78.0, terminal_bench=55.0, swe_lancer=62.0, long_horizon_coding=84.0,
        webdev_elo=1710, frontend_ui_motion=80.0,
        gpqa_diamond=88.5, arc_agi=86.0, research_orchestration=83.0, fast_explore=95.0,
        osworld=68.0, computer_use=85.0, general_coding=78.5,
        weekly_quota_burn_pct=0.08, avg_net_tokens="40k", avg_duration_sec=60.0, avg_cost_krw=800,
        citations="Codex Luna Medium · High Speed & Low Quota"
    ),
    "gpt-6-luna (low)": _profile(
        "gpt-6-luna", "codex-luna", "low", 75.0, 79.0, 78.0, 98.0, 0.20,
        swe_bench_verified=75.0, terminal_bench=52.0, swe_lancer=58.0, long_horizon_coding=80.0,
        webdev_elo=1680, frontend_ui_motion=78.0,
        gpqa_diamond=85.0, arc_agi=82.0, research_orchestration=80.0, fast_explore=97.0,
        osworld=65.0, computer_use=82.0, general_coding=75.0,
        weekly_quota_burn_pct=0.05, avg_net_tokens="25k", avg_duration_sec=35.0, avg_cost_krw=500,
        citations="Codex Luna Low · Fast Fixes & Lightweight Tests"
    ),
}


def _pool_scores(matrix):
    """Keep the existing quota router's pool-level API and ordering."""
    profiles = {"codex": "gpt-6.1-sol (high)",
                "antigravity_claude": "claude-opus-5-5 (high)",
                "antigravity_gemini": "Gemini 4 Argon (High)",
                "claude": "claude-opus-5-5 (xhigh)"}
    scores = {pool: {**matrix[label], "profile": label} for pool, label in profiles.items()}
    ordered = sorted(scores, key=lambda pool: scores[pool]["coding"], reverse=True)
    for rank, pool in enumerate(ordered, 1):
        scores[pool].update(composite_rank=rank, routing_preference=len(ordered) - rank)
    return scores


DEFAULT_BENCHMARK_SCORES = _pool_scores(DEFAULT_BENCHMARK_MATRIX)


def _detect_local_subscription_models(agy_usage: dict | None = None) -> dict[str, Any]:
    """Zero-token local inspection of models currently unlocked in user subscriptions."""
    discovered_agy: dict[str, str] = {}
    new_models: list[dict[str, str]] = []

    if isinstance(agy_usage, dict):
        for pool_name, pinfo in (agy_usage.get("pools") or {}).items():
            for m_label in (pinfo.get("models") or []):
                discovered_agy[m_label] = pool_name
                known_labels = set(BASELINE_MODELS["antigravity"].values())
                if m_label not in known_labels and not any(k.split(" (")[0] in m_label for k in known_labels):
                    new_models.append({
                        "provider": "antigravity",
                        "model": m_label,
                        "pool": pool_name,
                        "recommendation": f"Antigravity 구독에 신규 모델 '{m_label}'이 활성화되었습니다!",
                    })

    # Inspect ~/.codex/models_cache.json if present (0 tokens)
    codex_cache_file = os.path.expanduser("~/.codex/models_cache.json")
    discovered_codex: list[str] = []
    if os.path.exists(codex_cache_file):
        try:
            with open(codex_cache_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            models_list = cdata.get("models") if isinstance(cdata, dict) else cdata
            if isinstance(models_list, list):
                for item in models_list:
                    slug = item.get("slug") or item.get("id") if isinstance(item, dict) else str(item)
                    if slug:
                        discovered_codex.append(slug)
                        if (
                            slug not in BASELINE_MODELS["codex"]
                            and any(slug.startswith(p) for p in ("gpt-6.", "gpt-7", "o4", "o5", "codex-"))
                        ):
                            new_models.append({
                                "provider": "codex",
                                "model": slug,
                                "pool": "codex",
                                "recommendation": f"Codex ProMax에 신규 모델 '{slug}'이 출시/활성화되었습니다!",
                            })
        except Exception:
            pass

    return {
        "antigravity_models": list(discovered_agy.keys()),
        "codex_models": discovered_codex[:12],
        "new_models_detected": new_models,
    }


def _parse_leaderboard(body: str) -> list[dict]:
    """Read JSON or safe YAML. The stdlib fallback handles Aider's flat rows."""
    try:
        data = json.loads(body)
    except ValueError:
        try:
            import yaml
        except ImportError:
            # Extract only scalar fields from the official list-of-mappings schema.
            # Never evaluate YAML tags or constructors.
            data = []
            row = None
            for line in body.splitlines():
                match = re.match(r"^(?P<start>- )?(?:  )?(?P<key>[a-z_0-9]+):\s*(?P<value>.*)$", line)
                if not match:
                    continue
                if match["start"]:
                    row = {}
                    data.append(row)
                if row is not None:
                    value = match["value"].split(" #", 1)[0].strip()
                    row[match["key"]] = value.strip("\"'")
        else:
            data = yaml.safe_load(body)
    if isinstance(data, dict):
        data = next((data[key] for key in ("entries", "leaderboard", "data") if key in data), [])
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def _normalized_model(label: str) -> str:
    # Match aliases such as openai/gpt-6.1-sol and Claude Opus 5.5 exactly.
    label = re.sub(r"^(?:openrouter/)?(?:openai/|anthropic/|google/|gemini/)", "", label.lower())
    return re.sub(r"[^a-z0-9]", "", label)


def _matching_profiles(entry, matrix):
    label = entry.get("model") or entry.get("name")
    if not isinstance(label, str):
        return []
    effort = entry.get("effort") or entry.get("reasoning_effort")
    suffix = re.search(r"\s*\(([^)]+)\)\s*$", label)
    if suffix:
        effort = effort or suffix[1].lower()
        label = label[:suffix.start()]
    candidates = [key for key, profile in matrix.items()
                  if _normalized_model(profile["model"]) == _normalized_model(label)]
    if effort:
        effort = str(effort).lower()
        return [key for key in candidates if effort == matrix[key]["effort"]
                or (key == "claude-opus-5-5 (ultra)" and effort in ("ultra", "max"))
                or (key == "gpt-6-astra (ultra/high)" and effort in ("high", "ultra", "ultra/high"))
                or (matrix[key]["pool"] == "agy-flash" and effort == "fast")]
    # Do not assign an unspecified effort's score to several different profiles.
    return candidates if len(candidates) == 1 else []


def _fetch_verified_leaderboard_Update() -> dict[str, Any]:
    """Parse actual HTTP entries; retain complete baselines on offline/invalid data."""
    matrix = copy.deepcopy(DEFAULT_BENCHMARK_MATRIX)
    entries = []
    updated = set()
    try:
        req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "ai-maestro-benchmark-sync/2.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                entries = _parse_leaderboard(resp.read().decode("utf-8"))
    except Exception:
        pass
    valid_entries = 0
    live_scores = {}
    for entry in entries:
        value = entry.get("pass_rate_2", entry.get("aider_polyglot", entry.get("coding")))
        try:
            if isinstance(value, bool):
                continue
            score = float(value)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score) or not 0 <= score <= 100:
            continue
        if not isinstance(entry.get("model") or entry.get("name"), str):
            continue
        valid_entries += 1
        for key in _matching_profiles(entry, matrix):
            # Several Aider runs can exist for the same profile: use its best run.
            live_scores[key] = max(score, live_scores.get(key, score))
    for key, score in live_scores.items():
        matrix[key].update(coding=score, aider_polyglot=score, source=LEADERBOARD_URL)
        updated.add(key)
    return {"scores": _pool_scores(matrix), "matrix": matrix,
            "online_verified": bool(valid_entries), "live_entry_count": valid_entries,
            "updated_profiles": sorted(updated)}


def get_max_capability_recommendation(domain: str = "coding", *, pools=None, model=None,
                                      allow_fast: bool = False, effort: str | None = None) -> dict[str, Any]:
    """Return the highest absolute domain score, without bypassing quota checks.

    Optional pool/model filters constrain explicit CLI requests. Fast is opt-in.
    """
    if domain not in DOMAINS:
        raise ValueError(f"Unknown benchmark domain: {domain}")
    matrix = get_daily_benchmark_and_models()["benchmark_matrix"]
    candidates = [(label, profile) for label, profile in matrix.items()
                  if (pools is None or profile["pool"] in pools)
                  and (model is None or profile["model"] == model)
                  and (effort is None or profile["effort"] == effort)
                  and (allow_fast or profile["pool"] != "claude-fast")]
    if not candidates:
        raise ValueError("No benchmark profile matches the requested pool/model")
    label, profile = max(candidates, key=lambda item: item[1][domain])
    return {**profile, "profile": label, "domain": domain, "score": profile[domain]}


def get_daily_benchmark_and_models(agy_usage: dict | None = None, force_refresh: bool = False) -> dict[str, Any]:
    """Return cached daily benchmark rankings + live subscription new-model detection (0 LLM tokens)."""
    now = int(time.time())
    cached: dict[str, Any] = {}
    read_path = CACHE_PATH if os.path.exists(CACHE_PATH) else LEGACY_CACHE_PATH
    if not force_refresh and os.path.exists(read_path):
        try:
            with open(read_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}

    if not isinstance(cached, dict):
        cached = {}
    try:
        last_sync = int(cached.get("last_sync_epoch") or 0)
    except (TypeError, ValueError):
        last_sync = 0
    if cached.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(cached.get("benchmark_matrix"), dict):
        cached = {}
    if not force_refresh and cached and 0 <= (now - last_sync) < CACHE_TTL_SEC:
        # Still check local subscription models in-memory (0ms, 0 network) if agy_usage is passed
        if agy_usage:
            local_check = _detect_local_subscription_models(agy_usage)
            cached["new_models_detected"] = local_check["new_models_detected"]
        return cached

    lb = _fetch_verified_leaderboard_Update()
    local_check = _detect_local_subscription_models(agy_usage)
    result = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "last_sync_epoch": now,
        "last_sync_kst": time.strftime("%Y-%m-%d %H:%M KST", time.localtime(now)),
        "ttl_hours": 24,
        "llm_tokens_used": 0,
        "online_verified": lb["online_verified"],
        "benchmark_scores": lb["scores"],
        "benchmark_matrix": lb["matrix"],
        "live_entry_count": lb["live_entry_count"],
        "updated_profiles": lb["updated_profiles"],
        "new_models_detected": local_check["new_models_detected"],
        "discovered_models": {
            "antigravity": local_check["antigravity_models"],
            "codex": local_check["codex_models"],
        },
    }
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return result


def classify_task_domain(spec: str = "", difficulty: str = "medium") -> str:
    """Classify user task spec and difficulty into benchmark domain."""
    s = (spec or "").lower()
    if any(k in s for k in ("computer use", "computer_use", "osworld", "화면 조작", "데스크탑 조작", "마우스", "키보드", "gui 조작", "pc 조작")):
        return "computer_use"
    if any(k in s for k in ("motion", "모션", "애니메이션", "animation", "ui/ux", "frontend", "프론트엔드", "css", "svg", "canvas", "웹디자인", "위젯", "widget", "generative_ui", "html widget")):
        return "frontend_ui_motion"
    if any(k in s for k in ("길게 이어지는", "장기 호흡", "long horizon", "long-horizon", "terminal-bench", "swe-lancer", "테스트 루프", "디버깅 루프", "test loop", "e2e", "엔드투엔드", "지속적 빌드", "단위테스트 반복")):
        return "long_horizon_coding"
    if any(k in s for k in ("연구", "개선", "research", "orchestration", "오케스트레이션", "알고리즘", "algorithm", "수식", "gpqa", "arc-agi", "hle", "심층", "deep reason", "아키텍처 설계")):
        return "research_orchestration"
    if difficulty == "low" or any(k in s for k in ("탐색", "로그 확인", "log scan", "grep", "스캔", "빠른 탐색", "fast explore")):
        return "fast_explore"
    return "general_coding"


def recommend_model_for_task(spec: str = "", difficulty: str = "medium", usage_data: dict | None = None) -> dict[str, Any]:
    """Recommend the optimal model/pool using Domain Benchmark × Cost-Efficiency,
    failing over only when quota drops below the low threshold (< 15~20%).
    """
    domain = classify_task_domain(spec, difficulty)
    matrix = get_daily_benchmark_and_models(usage_data)["benchmark_matrix"]

    quotas = {
        "codex": 61.0, "codex-astra": 61.0, "codex-luna": 61.0,
        "claude": 80.0, "claude-fast": 80.0,
        "agy-argon": 46.5, "agy-sonnet": 0.0, "agy-flash": 90.0,
    }
    gem_7d_val = 50.0
    if isinstance(usage_data, dict):
        cl = usage_data.get("claude_local") or {}
        cl_5h = (cl.get("five_hour") or {}).get("remaining_percent")
        cl_7d = (cl.get("weekly") or {}).get("remaining_percent")
        if cl_5h is not None and cl_7d is not None:
            quotas["claude"] = quotas["claude-fast"] = min(float(cl_5h), float(cl_7d))
        elif cl_7d is not None:
            quotas["claude"] = quotas["claude-fast"] = float(cl_7d)
        elif cl_5h is not None:
            quotas["claude"] = quotas["claude-fast"] = float(cl_5h)

        cdx = usage_data.get("codex_local") or {}
        cdx_5h = (cdx.get("five_hour") or {}).get("remaining_percent")
        cdx_7d = (cdx.get("weekly") or {}).get("remaining_percent")
        if cdx_5h is not None and cdx_7d is not None:
            quotas["codex"] = quotas["codex-astra"] = quotas["codex-luna"] = min(float(cdx_5h), float(cdx_7d))
        elif cdx_7d is not None:
            quotas["codex"] = quotas["codex-astra"] = quotas["codex-luna"] = float(cdx_7d)
        elif cdx_5h is not None:
            quotas["codex"] = quotas["codex-astra"] = quotas["codex-luna"] = float(cdx_5h)

        pools = (usage_data.get("antigravity") or {}).get("pools") or {}
        gem = pools.get("Gemini Pool (Argon / Flash)") or {}
        gem_5h = (gem.get("five_hour") or {}).get("remaining_percent")
        gem_7d = (gem.get("weekly") or {}).get("remaining_percent")
        if gem_7d is not None:
            gem_7d_val = float(gem_7d)
        gem_rem = min(float(gem_5h), float(gem_7d)) if (gem_5h is not None and gem_7d is not None) else float(gem_7d if gem_7d is not None else (gem_5h or 50.0))
        quotas["agy-argon"] = gem_rem
        quotas["agy-flash"] = float(gem_5h) if gem_5h is not None else 90.0

        cl_agy = pools.get("Claude & GPT-OSS Pool (Antigravity)") or {}
        cl_agy_7d = (cl_agy.get("weekly") or {}).get("remaining_percent")
        quotas["agy-sonnet"] = float(cl_agy_7d) if cl_agy_7d is not None else 0.0

    scored_candidates = []
    for label, prof in matrix.items():
        pool = prof["pool"]
        effort = prof.get("effort", "medium")

        # Exclude ultra/xhigh unless requested by difficulty
        if effort == "ultra" and difficulty != "ultra":
            continue
        if effort == "xhigh" and difficulty not in ("xhigh", "ultra"):
            continue
        # Exclude claude-fast from auto routing unless explicitly requested
        if pool == "claude-fast" and not any(k in (spec or "").lower() for k in ("fast", "인터랙티브", "고속", "즉시")):
            continue

        rem = quotas.get(pool, 50.0)

        if difficulty == "low":
            diff_match = 1.0 if (effort == "low" or prof.get("model") == "gpt-6-luna") else 0.7
        elif difficulty == "medium":
            diff_match = 1.0 if effort == "medium" else (0.95 if effort == "high" else 0.8)
        elif difficulty in ("high", "xhigh"):
            diff_match = 1.0 if effort in ("high", "xhigh") else (0.9 if effort == "medium" else 0.7)
        else:
            diff_match = 1.0 if effort == "ultra" else 0.85

        if domain == "computer_use":
            bench_score = prof.get("computer_use", 75.0)
        elif domain == "frontend_ui_motion":
            bench_score = prof.get("frontend_ui_motion", 85.0)
        elif domain == "long_horizon_coding":
            bench_score = prof.get("long_horizon_coding", 85.0)
        elif domain == "research_orchestration":
            bench_score = prof.get("research_orchestration", 85.0)
        elif domain == "fast_explore":
            bench_score = prof.get("fast_explore", prof.get("speed", 80.0))
        else:
            bench_score = prof.get("general_coding", (prof.get("coding", 80.0) + prof.get("swe_bench_verified", 80.0)) / 2.0)

        if rem >= 20.0:
            quota_score = 0.95 * bench_score + 0.05 * rem
        elif rem >= 10.0:
            penalty = (20.0 - rem) * 1.5
            quota_score = max(0.0, 0.95 * bench_score + 0.05 * rem - penalty)
        elif rem > 0.0:
            penalty = 25.0 + (10.0 - rem) * 3.0
            quota_score = max(0.0, 0.95 * bench_score + 0.05 * rem - penalty)
        else:
            quota_score = 0.0

        final_score = round(quota_score * diff_match, 2)

        # Protect Antigravity Gemini (4 Argon) pool for parent IDE sessions when 7d < 50%
        if gem_7d_val < 50.0 and pool in ("agy-argon", "agy-flash"):
            final_score = round(final_score * 0.35, 2)

        if pool in ("codex", "codex-astra", "codex-luna"):
            canonical = "codex"
        elif pool in ("claude", "claude-fast"):
            canonical = "claude"
        elif pool in ("agy-argon", "agy-flash"):
            canonical = "antigravity_gemini"
        elif pool == "agy-sonnet":
            canonical = "antigravity_claude"
        else:
            canonical = pool

        scored_candidates.append({
            "profile": label,
            "model": prof["model"],
            "pool": pool,
            "canonical_pool": canonical,
            "effort": effort,
            "domain": domain,
            "benchmark_score": bench_score,
            "remaining_percent": rem,
            "score": final_score,
            "citation": prof.get("citations", ""),
            "weekly_quota_burn_pct": prof.get("weekly_quota_burn_pct"),
            "cost_krw": prof.get("avg_cost_krw"),
        })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    winner = scored_candidates[0] if scored_candidates else {}
    return {
        "domain": domain,
        "difficulty": difficulty,
        "recommended_model": winner.get("model"),
        "recommended_pool": winner.get("pool"),
        "canonical_pool": winner.get("canonical_pool"),
        "effort": winner.get("effort"),
        "benchmark_score": winner.get("benchmark_score"),
        "score": winner.get("score"),
        "remaining_percent": winner.get("remaining_percent"),
        "citation": winner.get("citation"),
        "cost_krw": winner.get("cost_krw"),
        "candidates": scored_candidates,
    }


if __name__ == "__main__":
    print(json.dumps(get_daily_benchmark_and_models(force_refresh=True), ensure_ascii=False, indent=2))
