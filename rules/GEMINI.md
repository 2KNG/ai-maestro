# Global Multi-Agent Orchestrator Rules

1. **Auto-Balanced Multi-Pool Routing (`agy-orca`)**:
   - Split multi-file/audit/coding tasks across subagents; never run 10+ sequential reads/commands alone.
   - Use `/home/u24c9g5090r64/.local/bin/agy-orca worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"` (or `--pool codex|agy-argon|agy-claude|agy-flash`).
   - **3-Stage Weekly Protection & 0% Auto-Steering**:
     - `7d > 10%`: Normal (`5h` priority). `2% < 7d <= 10%`: Weekly Conserve (`min(5h, 7d * 2.5)`). `0% < 7d <= 2%`: Emergency Reserve (`7d * 0.3`). `5h == 0%` or `7d == 0%`: Hard-blocked + automatic pre-flight/mid-flight failover steering (`agy-orca`).
   - **Codex Tier**: Default `--tier priority` (`Fast` ⚡); use `--urgent` (`ultrafast` ⚡⚡⚡) ONLY when urgent real-time response is required.
   - **Fast Lookups**: Use `invoke_subagent(Model='flash')` (`3.8 Flash`) for read-only lookups.

2. **AI Wall-Clock ETA Rule (직관적 실제 소요시간 · 한글 토큰 규모 + 원화 + 구독 소모율 % 안내)**:
   - When asked how long a task will take, NEVER give human calendar estimates ("며칠/1주일") or raw `k/M` units. Always report **AI parallel wall-clock time + Korean token scale with KRW & subscription quota % (`⏱ 30초~1분 · 8천~1만5천 토큰 규모 (5백원대 · 구독 5h 0.3%)` / `⏱ 1~3분 · 10만~15만 토큰 규모 (5천원대 · 구독 5h 2~4% / 7d 0.5%)` / `⏱ 3~6분 · 100만~150만 토큰 규모 (5만원대 · 구독 5h 20~30% / 7d 3~5%)` / `⏱ 7~12분 · 1천만 토큰 규모 (53만원대 · 구독 7d 15~25% 분산)`) + target pools** (omit "약").

3. **Silent Background Widget Sync (`usage_widget.html`) — NO Inline `<agent-embed>` in Chat**:
   - Update `/home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html` silently in the background via `/home/u24c9g5090r64/.local/bin/ai-usage-widget <path>` (so the right-hand Artifact panel & `http://127.0.0.1:18765` stay live).
   - **DO NOT output `<agent-embed src=".../usage_widget.html"></agent-embed>` in chat messages** (it takes up too much vertical space and hurts chat readability), UNLESS the user explicitly says `"위젯 띄워줘"` / `"위젯 인라인으로 보여줘"`.
