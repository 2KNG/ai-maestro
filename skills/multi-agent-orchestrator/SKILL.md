---
name: multi-agent-orchestrator
description: >-
  Orchestrate and coordinate tasks across Google Antigravity (Gemini 4 Argon / Flash),
  local OpenAI Codex CLI (codex 0.162.1), and local Anthropic Claude Code CLI (claude 2.1.296),
  and inspect real-time token/quota usage across all pools. Activate when the user asks to
  delegate tasks to Claude or Codex, run multi-agent workflows, use Mobbin/Context7 MCPs,
  or check quota/token usage.
---

# Multi-Agent Orchestrator (Antigravity + Codex + Claude Code)

Use this skill to coordinate work across Antigravity's internal models and the user's local **Codex CLI** and **Claude Code CLI**, or to inspect real-time quota usage across all pools.

## 1. Checking Real-Time Usage & Quotas Across All Pools

Run the bundled usage inspector script to check:
- **Antigravity Gemini Pool** (`Gemini 4 Argon`, `Gemini 3.8 Flash`)
- **Antigravity Third-Party Pool** (`Claude Opus 5.5`, `Claude Sonnet 5.5`, `GPT-OSS 120B`)
- **Local OpenAI Codex Pool** (`ProMax` weekly/rolling usage & reset timestamp)
- **Local Anthropic Claude Code Status** & connected MCPs

```bash
python3 ~/.gemini/config/skills/multi-agent-orchestrator/scripts/check_all_usage.py
```

To trigger/start the 5-hour rolling reset timer on any untouched Antigravity model pool, send a lightweight 1-turn `StartCascade` + `SendUserCascadeMessage` ("Reply OK") and `DeleteCascadeTrajectory` via `$ANTIGRAVITY_LS_ADDRESS`.

## 2. Launching Local Codex & Claude in the Subagents Panel

To ensure delegated tasks appear live as cards in the right-hand **Subagents** panel:

1. Define (if not yet defined in the current conversation) `codex-worker` and/or `claude-worker` using `define_subagent` with `enable_write_tools: true`.
2. Call `invoke_subagent` with `Subagents`:
   - **`codex-worker`**:
     - Executes `/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin/codex exec -C <dir> "<prompt>"`
     - Or runs code review: `/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin/codex review -C <dir>`
     - Or queues follow-up into an active desktop/CLI thread: `codex queue --thread <uuid> --message "<prompt>"`
   - **`claude-worker`**:
     - Executes `/home/u24c9g5090r64/.local/bin/claude -p "<prompt>"`
     - Has direct access to `claude.ai` cloud MCPs including **Mobbin** (`https://api.mobbin.com/mcp`), **Context7**, **Mermaid Chart**, **Google Drive**, **Gmail**, and **Google Calendar**.

## 2.5. Maestro Multi-Pool Dispatch (`maestro`)

> [!IMPORTANT]
> `invoke_subagent` only supports **Gemini Pool** models (`inherit` / `pro` / `flash`). **NEVER** call `invoke_subagent(Model='inherit' | 'pro')` when the parent runs on Argon.

| Difficulty | Dispatch command |
|---|---|
| High / Medium → Antigravity Claude Pool (`planModel` `MODEL_PLACEHOLDER_M402` Opus 5.5 High / `MODEL_PLACEHOLDER_M405` Sonnet 5.5 High) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool agy-claude --cwd <dir> --wait --spec "..."` |
| High / Medium → OpenAI Codex Pool (`gpt-6.1-sol`, `ultrafast` / `priority`) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex --cwd <dir> --spec "..."` |
| Responsive UI / quick patch → Local Claude Code Fast (Opus 5.5 Fast) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool claude-fast --cwd <dir> --wait --spec "..."` |
| Core architecture → Local Claude Code (Opus 5.5 `xhigh`/`ultra`) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool claude --cwd <dir> --wait --spec "..."` |
| Parallel submodules → OpenAI Codex Astra Pool (`gpt-6-astra`, `medium`, `priority`) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex-astra --cwd <dir> --wait --spec "..."` (or `--pool codex --model gpt-6-astra`) |
| Low / read-only lookup | `invoke_subagent(Model='flash')` only |

- Run Claude-pool and Codex-pool workers in parallel when useful; balance by `ai-usage` remaining %.
- **Every response** must run `/home/u24c9g5090r64/.local/bin/ai-usage-widget /home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html` and embed `<agent-embed src="file:///home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html"></agent-embed>`.

## 2.6. Explicit Worker Assignment Roster Rule (워커 배정 명시 · 투입 전 고지)

- Whenever delegating one or many workers (`maestro worker-start`, `invoke_subagent`, child workers), ALWAYS state the exact **model + effort + speed tier** assigned to each concrete subtask **BEFORE launch**, with its ETA row.
- After launch / completion, report every actual `--pool auto` assignment or fallback / failover change (`requested → actual` model · effort · tier + reason, e.g. 0% quota steering).
- Speed tier: Codex default is `priority`; `ultrafast` ONLY with `--urgent` (urgent real-time turnaround).

| Concrete subtask | Model · Effort · Speed tier |
|---|---|
| Responsive UI / quick patch | Opus 5.5 Fast (`claude-fast`) |
| Core architecture reasoning | Opus 5.5 (`xhigh`/`ultra`) |
| Backend / system / test verification | gpt-6.1-sol (`high`, `ultrafast`/`priority`) |
| Parallel submodule implementation | gpt-6-astra (`medium`, `priority`) |
| Fast code / log search | Gemini 3.8 Flash (`low`, `fast`) |

## 2.7. Model & Effort Single-Value ETA Table (모델·노력별 단일 수치 소요시간 · 한글 토큰 · 원화)

| Model & Effort (모델 · 노력 · 속도 티어) | ETA (단일 값) |
|---|---|
| Gemini 3.8 Flash (low · fast) / Sonnet 5.5 (high) | `⏱ 40초 · 5만 토큰 (1천원)` |
| Opus 5.5 Fast (claude-fast) | `⏱ 45초 · 15만 토큰 (3천원)` |
| Opus 5.5 (high/xhigh) / Gemini 4 Argon (high) | `⏱ 2분 · 22만 토큰 (6천원)` |
| gpt-6-astra (medium · priority) / gpt-6.1-sol (low) | `⏱ 1분 · 35만 토큰 (3천원)` |
| gpt-6.1-sol (medium · priority) | `⏱ 1분 20초 · 80만 토큰 (7천원)` |
| gpt-6.1-sol (high · priority) | `⏱ 2분 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (high · ultrafast) | `⏱ 1분 15초 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (ultra · priority/ultrafast) | `⏱ 2분 30초 · 180만 토큰 (1만8천원)` |
| gpt-6-astra (ultra) / 멀티풀 병렬 합산 | `⏱ 4분 · 350만 토큰 (3만원)` |

- These are **calibrated planning values (보정된 계획용 기준값), NOT actual measured billing (실측 청구액 아님)**. Pick the row matching each worker's assigned model · effort · tier.
- NEVER write `~` ranges (`1~3분`, `3천~6천`), `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, human calendar estimates (`"며칠/1주일"`), or raw `k/M` units.
- NEVER run or print `5h/7d` quota lookups (`"다시 조회"`, `"미제공"`) just to produce an ETA. Quota checks remain mandatory for **routing** (`--pool auto`, 3-Stage Weekly Protection, 0% Auto-Steering).

## 3. Workspace Discipline Rules

When coordinating across multiple agents in `/home/u24c9g5090r64/dev`:
- Always check `STATE.md`, `CLAUDE.md`, and `AGENTS.md` in the target repository before dispatching subagents.
- Assign strict, disjoint file ownership boundaries to each agent (`codex-worker`, `claude-worker`, or internal Gemini subagents).
- Never revert or overwrite dirty files owned by other active sessions.
