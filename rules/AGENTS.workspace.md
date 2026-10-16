# 🎼 Workspace Orchestration Harness (`ai-maestro`)
Follow `~/.gemini/GEMINI.md` strictly on EVERY turn:
1. **`<MAESTRO_DISPATCH_GATE>` (혼자 골똘히 생각 금지 · 첫 툴 즉시 위임)**:
   - Keep `<thought>` ≤ 4 lines before delegating.
   - Delegate file lookups/research immediately via `invoke_subagent(Model='flash')` and multi-file coding/debugging via `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"`.
   - **Max 2 consecutive solo tool calls** (`view_file`/`replace_file_content`/`run_command`) allowed in the main agent.
2. **ETA Standard**: Report AI wall-clock time + Korean token count with KRW & 5h/7d % (`8천~1만5천 토큰 (5백원대 · 5h 0.3%)` / `10만~15만 토큰 (5천원대 · 5h 2~4% / 7d 0.5%)` / `100만~150만 토큰 (5만원대 · 5h 20~30% / 7d 3~5%)`).
3. **Silent Widget Update**: Update `usage_widget.html` silently in the background WITHOUT inline `<agent-embed>` in chat.
