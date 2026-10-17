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

CACHE_PATH = os.path.expanduser("~/.gemini/antigravity/orca_benchmark_cache.json")
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
CACHE_SCHEMA_VERSION = 2
LEADERBOARD_URL = "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml"


def _profile(model, pool, effort, coding, reasoning, frontend_ui, speed, quota_multiplier):
    return dict(model=model, pool=pool, effort=effort, coding=coding, reasoning=reasoning,
                frontend_ui=frontend_ui, speed=speed, quota_multiplier=quota_multiplier,
                source="configured baseline")


DEFAULT_BENCHMARK_MATRIX = {
    "gpt-6-astra (ultra/high)": _profile("gpt-6-astra", "codex-astra", "ultra", 83.8, 94.2, 87.5, 62.0, 3.5),
    "gpt-6-astra (medium)": _profile("gpt-6-astra", "codex-astra", "medium", 80.2, 89.0, 85.0, 74.0, 1.0),
    "gpt-6.1-sol (ultra)": _profile("gpt-6.1-sol", "codex", "ultra", 82.4, 92.5, 86.5, 68.0, 4.0),
    "gpt-6.1-sol (high)": _profile("gpt-6.1-sol", "codex", "high", 79.4, 88.5, 85.0, 78.0, 1.0),
    "gpt-6.1-sol (medium)": _profile("gpt-6.1-sol", "codex", "medium", 76.8, 84.0, 82.5, 84.0, 0.5),
    "claude-opus-5-5 (xhigh)": _profile("claude-opus-5-5", "claude", "xhigh", 81.2, 91.5, 92.0, 72.0, 1.2),
    "claude-opus-5-5 (high)": _profile("claude-opus-5-5", "claude", "high", 79.5, 89.5, 91.5, 76.0, 1.0),
    "claude-opus-5-fast": _profile("claude-opus-5-fast", "claude-fast", "high", 77.5, 86.0, 90.5, 96.0, 2.0),
    "claude-sonnet-5-5 (high)": _profile("claude-sonnet-5-5", "agy-sonnet", "high", 78.0, 85.0, 89.5, 88.0, 0.6),
    "Gemini 4 Argon (High)": _profile("Gemini 4 Argon", "agy-argon", "high", 77.2, 88.0, 88.5, 82.0, 1.0),
    "Gemini 3.8 Flash (Fast)": _profile("Gemini 3.8 Flash", "agy-flash", "low", 73.5, 79.0, 83.0, 98.5, 0.1),
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
    if not force_refresh and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
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


if __name__ == "__main__":
    print(json.dumps(get_daily_benchmark_and_models(force_refresh=True), ensure_ascii=False, indent=2))
