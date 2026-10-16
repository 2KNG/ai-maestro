---
name: codex
description: >-
  Run tasks directly on the local OpenAI Codex ProMax CLI (gpt-6.1-sol, ultra/high effort,
  ultrafast/priority speed tier) via maestro or codex exec. Triggered when the user types /codex
  or asks to run a task directly with local Codex.
---

# Direct Local OpenAI Codex Runner (`/codex`)

When the user invokes `/codex` (or asks to run their prompt directly via local Codex):

1. Execute the user's task directly via `maestro` on the `codex` pool (`gpt-6.1-sol`, `ultra` effort, `ultrafast` service tier unless the user specifies `priority` or `high`/`medium` effort):
   ```bash
   /home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex --effort ultra --tier ultrafast --cwd /home/u24c9g5090r64/dev --wait --spec "<USER_PROMPT>"
   ```
2. If the user asked for a code review (`/codex review`), run:
   ```bash
   /home/u24c9g5090r64/.config/nvm/versions/node/v24.22.0/bin/codex review -c 'model_reasoning_effort="ultra"' -c 'service_tier="ultrafast"' -C <TARGET_DIR>
   ```
3. Also offer the clickable Aux Pane Studio pill if they want an interactive model/effort/tier selector panel:
   `[Codex & Claude Studio](sidecar://codex-claude-studio/)`
4. Always refresh and embed `usage_widget.html` at the top of the response.
