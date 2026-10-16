# 🎼 Codex Orchestration Rules (`ai-maestro`)
- Default tier: `service_tier = "priority"` (`Fast` ⚡). Use `"ultrafast"` (`⚡⚡⚡`) only when urgent real-time turnaround is explicitly requested.
- For parallel sub-tasks, spawn child workers or use `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"`.
- **ETA Format (단일 값 · `~` 범위 및 금지어 절대 금지 · 쿼터 조회 생략)**:
  - Report task ETAs as a single clean value: `⏱ 45초 · 1만 토큰 (5백원)` / `⏱ 2분 · 12만 토큰 (5천원)` / `⏱ 5분 · 120만 토큰 (5만원)` / `⏱ 10분 · 1천만 토큰 (53만원)`.
  - NEVER write `~` ranges (`1~3분`, `3천~6천`), `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, or `5h/7d` quota notices (`"다시 조회"`, `"미제공"`). Do NOT run quota commands just to print an ETA.
