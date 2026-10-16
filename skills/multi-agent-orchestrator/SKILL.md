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

## 2.5. Orca-Style Multi-Pool Dispatch (`agy-orca`)

> [!IMPORTANT]
> `invoke_subagent` only supports **Gemini Pool** models (`inherit` / `pro` / `flash`). **NEVER** call `invoke_subagent(Model='inherit' | 'pro')` when the parent runs on Argon.

| Difficulty | Dispatch command |
|---|---|
| High / Medium → Antigravity Claude Pool (`planModel` `MODEL_PLACEHOLDER_M402` Opus 5.5 High / `MODEL_PLACEHOLDER_M405` Sonnet 5.5 High) | `/home/u24c9g5090r64/.local/bin/agy-orca worker-start --pool agy-claude --cwd <dir> --wait --spec "..."` |
| High / Medium → OpenAI Codex Pool (`gpt-6.1-sol`, `ultrafast` / `priority`) | `/home/u24c9g5090r64/.local/bin/agy-orca worker-start --pool codex --cwd <dir> --spec "..."` |
| Low / read-only lookup | `invoke_subagent(Model='flash')` only |

- Run Claude-pool and Codex-pool workers in parallel when useful; balance by `ai-usage` remaining %.
- **Every response** must run `/home/u24c9g5090r64/.local/bin/ai-usage-widget /home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html` and embed `<agent-embed src="file:///home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html"></agent-embed>`.

## 3. Workspace Discipline Rules

When coordinating across multiple agents in `/home/u24c9g5090r64/dev`:
- Always check `STATE.md`, `CLAUDE.md`, and `AGENTS.md` in the target repository before dispatching subagents.
- Assign strict, disjoint file ownership boundaries to each agent (`codex-worker`, `claude-worker`, or internal Gemini subagents).
- Never revert or overwrite dirty files owned by other active sessions.
