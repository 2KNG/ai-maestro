# 🎼 Workspace Orchestration Harness (`ai-maestro`)
Follow `~/.gemini/GEMINI.md` strictly on EVERY turn:
1. **`<MAESTRO_DISPATCH_GATE>` & Explicit Per-Worker Role Roster**:
   - Keep `<thought>` ≤ 4 lines before delegating.
   - Always state the exact worker (`모델명 + Effort + 속도 티어`) and what sub-task it handles:
     * `Opus 5.5 Fast (claude-fast · 2x quota)` → 라이브 인터랙티브 전용: 반응성·즉각적인 UI/프론트엔드 및 빠른 코드 패치 (cron/watchdog/장시간 자율/원격 백그라운드는 standard `claude-opus-5-5`)
     * `Opus 5.5 (xhigh/high)` → 복합 아키텍처 설계 및 심층 추론
     * `Opus 5.5 Ultra (ultra → --effort max + ultrathink · 3x quota)` → 최고난도 추론·아키텍처 전용 (`claude-ultra` / `opus-ultra` / `opus-5.5-ultra` 또는 `--effort ultra`, 명시적 opt-in)
     * `gpt-6.1-sol (high/medium · standard)` → 백엔드·시스템·테스트 루프 (`priority`는 `--interactive`/`--urgent`, `ultrafast`는 `--urgent` 전용)
     * `gpt-6-astra (medium · default)` → 보조 모듈 병렬 구현 (Astra는 항상 default tier, medium 상한 — `--max-capability`로만 high/ultra 해제)
     * `Gemini 3.8 Flash (low · fast)` → 초고속 코드/로그 탐색
2. **ETA Standard (단일 수치 · 범위 `~` 및 금지어 제외 · 쿼터 재조회 생략)**:
   - `Gemini 3.8 Flash` / `Sonnet 5.5`: `⏱ 40초 · 5만 토큰 (1천원)`
   - `Opus 5.5 Fast` (2x quota · interactive only): `⏱ 45초 · 16만 토큰 (4천원)`
   - `Opus 5.5 (high/xhigh)` / `Argon 4`: `⏱ 2분 · 22만 토큰 (6천원)`
   - `Opus 5.5 Ultra` (ultra · `--effort max` + `ultrathink` · 3x quota): `⏱ 3분 · 120만 토큰 (1만5천원)`
   - `gpt-6.1-sol (medium · priority · interactive/urgent)`: `⏱ 1분 20초 · 80만 토큰 (7천원)`
   - `gpt-6.1-sol (high · priority · interactive/urgent)`: `⏱ 2분 · 120만 토큰 (1만2천원)`
   - Chat ETA output contains only a single wall-clock value, Korean tokens, and KRW: `⏱ 2분 · 35만 토큰 (5천원)`. Never append subscription percentages or cache-included parenthetical notes.
3. **Speed Tier Policy**: `fast`/`priority`/`ultrafast`는 라이브 인터랙티브 핑퐁·즉각 응답·사용자가 `느리다`/명시적 fast 요청 시에만. cron/watchdog/장시간 자율/원격 백그라운드는 standard/default tier + standard `claude-opus-5-5`, 멀티에이전트 병렬 오케스트레이션 유지. `worker-start`는 `--interactive`/`--urgent` 없으면 background speed, `--background`가 interactive/urgent speed를 override, 명시적 `--pool claude-fast`는 background에서도 opt-in. `--tier default|standard|priority|ultrafast` (`priority`=interactive/urgent 필요, `ultrafast`=urgent 필요). `--max-capability` + `--domain coding|reasoning|frontend_ui|speed`는 `benchmark_sync` 추천 사용(쿼터 소진 조향 유지, 명시적 pool은 추천을 제약). Opus 5.5 Ultra(`claude-ultra`/`opus-ultra`/`opus-5.5-ultra` 또는 `--effort ultra`)는 standard `claude-opus-5-5` + `--effort max` + 프롬프트 앞 `ultrathink`, 3x 쿼터, Fast 미사용. 명시적 `--effort ultra`/`max`는 `--max-capability` 없이 허용(미지정 시 `high`, `--max-capability`면 `xhigh`). `--max-capability`는 명시적 `--pool claude-fast`가 아니면 Fast 프로필 제외·interactive/urgent auto-Fast 비활성. `benchmark_sync` 매트릭스 12 profiles × 4 domains(`coding`/`reasoning`/`frontend_ui`/`speed`, Ultra 3x·Fast 2x 쿼터 배수), JSON/YAML coding은 model+effort 일치 시에만 갱신.
4. **Silent Widget Update**: Update `usage_widget.html` silently in the background WITHOUT inline `<agent-embed>` in chat.

## `<MJSERVER_PORT_AND_SERVICE_GUARD>`

- mjserver (`192.168.10.10`)의 포트 기준 문서는 `/home/mjserver/dev/SERVER_PORTS.md`다. **포트를 바인딩하거나 프로세스를 종료하기 전에 반드시 이 문서를 읽고 `ss -tulpn`으로 실제 점유 상태를 확인한다.**
- `4173/tcp` → `mjmaterial-web.service`: 무진소재 홈페이지 본 서버, 상시 가동. 절대 강제 종료하거나 다른 용도로 점유하지 않는다.
- `18765/tcp` → `agy-widget-bridge.service`: ai-maestro AOH 텔레메트리/위젯 데몬, 상시 가동. 점유하거나 종료하지 않는다.
- 무진소재 임시 검증·프리뷰는 `4174–4179/tcp` 중 비어 있는 포트를 골라 `MUJIN_PORT`로 지정한다(예: `MUJIN_PORT=4174`).
- `8080–8089/tcp`, `8554/tcp`, `1984/tcp`는 향후 내부 CCTV/RTSP/웹 서비스용 예약 포트다. `3000–3099/tcp`, `5000–5099/tcp`는 내부 자동화·웹앱용으로 예약한다.
- 상시 서비스를 추가할 때는 사용 포트, 바인딩 주소, 용도와 systemd unit을 `/home/mjserver/dev/SERVER_PORTS.md`에 등록한다.
