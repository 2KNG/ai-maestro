# 🎼 Workspace Orchestration Harness (`ai-maestro`)
Follow `~/.gemini/GEMINI.md` strictly on EVERY turn:
1. **`<MAESTRO_DISPATCH_GATE>` & Explicit Per-Worker Role Roster**:
   - Keep `<thought>` ≤ 4 lines before delegating.
   - Always state the exact worker (`모델명 + Effort + 속도 티어`) and what sub-task it handles:
     * `Opus 5.5 Fast (claude-fast)` → 반응성·즉각적인 UI/프론트엔드 및 빠른 코드 패치
     * `Opus 5.5 (xhigh/high)` → 복합 아키텍처 설계 및 심층 추론
     * `gpt-6.1-sol (high/medium · priority)` → 백엔드·시스템·테스트 루프 (`ultrafast`는 오케스트레이터가 느려 의미 없으므로 백그라운드 워커에 사용 금지)
     * `gpt-6-astra (medium · standard)` → 보조 모듈 병렬 구현 (**`gpt-6-astra`에서 `ultrafast`/`priority(패스트)` 및 `ultra` effort 절대 사용 금지**)
     * `Gemini 3.8 Flash (low · fast)` → 초고속 코드/로그 탐색
2. **ETA Standard (단일 수치 · 범위 `~` 및 금지어 제외 · 쿼터 재조회 생략)**:
   - `Gemini 3.8 Flash` / `Sonnet 5.5`: `⏱ 40초 · 5만 토큰 (1천원)`
   - `Opus 5.5 Fast`: `⏱ 45초 · 15만 토큰 (3천원)`
   - `Opus 5.5 (high/xhigh)` / `Argon 4`: `⏱ 2분 · 22만 토큰 (6천원)`
   - `gpt-6.1-sol (medium · priority)`: `⏱ 1분 20초 · 110만 토큰 (4천원)`
   - `gpt-6.1-sol (high · priority)`: `⏱ 2분 · 800만 토큰 (1만2천원)`
3. **Silent Widget Update**: Update `usage_widget.html` silently in the background WITHOUT inline `<agent-embed>` in chat.
