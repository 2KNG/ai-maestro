# 🎼 Workspace Orchestration Harness (`ai-maestro`)
Follow `~/.gemini/GEMINI.md` strictly on EVERY turn:
1. **`<MAESTRO_DISPATCH_GATE>` (혼자 골똘히 생각 금지 · 첫 툴 즉시 위임)**:
   - Keep `<thought>` ≤ 4 lines before delegating.
   - Delegate file lookups/research immediately via `invoke_subagent(Model='flash')` and multi-file coding/debugging via `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"`.
   - **Max 2 consecutive solo tool calls** (`view_file`/`replace_file_content`/`run_command`) allowed in the main agent.
2. **ETA Standard (단일 값 · 범위 `~` 및 금지어 제외 · 쿼터 조회 생략)**:
   - Format: `⏱ 45초 · 1만 토큰 (5백원)` / `⏱ 2분 · 12만 토큰 (5천원)` / `⏱ 5분 · 120만 토큰 (5만원)` / `⏱ 10분 · 1천만 토큰 (53만원)`.
   - NEVER use `~` ranges, `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, or `5h/7d` quota lookups/notices (`"다시 조회"`, `"미제공"`).
3. **Silent Widget Update**: Update `usage_widget.html` silently in the background WITHOUT inline `<agent-embed>` in chat.
