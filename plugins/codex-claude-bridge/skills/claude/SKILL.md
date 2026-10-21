---
name: claude
description: >-
  Run tasks directly on the local Anthropic Claude Code Team CLI (fable / claude-opus-4-6) or
  Antigravity Claude Opus 5.5 (High · M402). Triggered when the user types /claude or asks to
  run a task directly with Claude.
---

# Direct Claude Runner (`/claude`)

When the user invokes `/claude` (or asks to run their prompt directly via Claude):

1. Check `~/.local/bin/ai-usage` or run the user's task directly via either:
   - **Local Anthropic Claude Code CLI (`fable` / `claude-opus-4-6`, with cloud MCPs like Mobbin/Context7/Drive)**:
     ```bash
     ~/.local/bin/claude -p "<USER_PROMPT>" --model fable --effort high --permission-mode acceptEdits
     ```
   - **Antigravity Claude Opus 5.5 Pool (`MODEL_PLACEHOLDER_M402`) via `maestro`**:
     ```bash
     ~/.local/bin/maestro worker-start --pool agy-claude --cwd ~/dev --wait --spec "<USER_PROMPT>"
     ```
2. Offer the clickable Aux Pane Studio pill if they want an interactive model/effort selector panel:
   `[Codex & Claude Studio](sidecar://codex-claude-studio/)`
3. Always refresh and embed `usage_widget.html` at the top of the response.
