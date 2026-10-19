# Workspace Orchestration Harness (`ai-maestro`)
Follow `~/.gemini/GEMINI.md` on every turn:
1. `<MAESTRO_DISPATCH_GATE>` & Explicit Per-Worker Role Roster:
   - `<thought>`를 4줄 이내로 유지하고 즉시 위임한다.
   - 워커 투입 전 모델명, Effort, 속도 티어, 구체적 담당 업무를 명시한다:
     * `Opus 5.5 Fast (claude-fast · 2x quota)`: 명시적 fast/속도 요청 시에만 투입 (인터랙티브 UI/빠른 패치). 단순 "오퍼스 시켜"에는 표준 `claude-opus-5-5` 사용.
     * `Opus 5.5 (xhigh/high)`: 복합 아키텍처 설계 및 심층 추론
     * `Opus 5.5 Ultra (ultra → --effort max + ultrathink · 3x quota)`: 최고난도 추론·아키텍처 전용 (`claude-ultra` / `opus-ultra` / `opus-5.5-ultra` 또는 `--effort ultra`, 명시적 opt-in)
     * `gpt-6.1-sol (high/medium · standard)`: 백엔드·시스템·테스트 루프 (`priority`는 `--interactive`/`--urgent`, `ultrafast`는 `--urgent` 전용)
     * `gpt-6-astra (medium · default)`: 보조 모듈 병렬 구현 (Astra는 항상 default tier, medium 상한 — `--max-capability`로만 high/ultra 해제)
     * `Gemini 3.8 Flash (low · fast)`: 초고속 코드/로그 탐색
2. ETA Standard (단일 수치 · 범위 `~` 및 금지어 제외 · 쿼터 재조회 생략):
   - `Gemini 3.8 Flash` / `Sonnet 5.5`: `⏱ 40초 · 5만 토큰 (1천원)`
   - `Opus 5.5 Fast` (2x quota · interactive only): `⏱ 45초 · 16만 토큰 (4천원)`
   - `Opus 5.5 (high/xhigh)` / `Argon 4`: `⏱ 2분 · 22만 토큰 (6천원)`
   - `Opus 5.5 Ultra` (ultra · `--effort max` + `ultrathink` · 3x quota): `⏱ 3분 · 120만 토큰 (1만5천원)`
   - `gpt-6.1-sol (medium · priority · interactive/urgent)`: `⏱ 1분 20초 · 80만 토큰 (7천원)`
   - `gpt-6.1-sol (high · priority · interactive/urgent)`: `⏱ 2분 · 120만 토큰 (1만2천원)`
   - 채팅 ETA 출력 형식: `⏱ 2분 · 35만 토큰 (5천원)`.
3. Speed Tier Policy: `fast`/`priority`/`ultrafast`는 라이브 인터랙티브 핑퐁이나 사용자의 명시적 고속 요청 시에만 사용. cron, watchdog, 장시간 자율 작업은 standard/default tier + 표준 `claude-opus-5-5` 사용. `worker-start`는 `--interactive`/`--urgent` 없으면 background 속도 적용.
4. Natural Developer Communication Style:
   - 장황한 서두나 AI 클리셰("결론부터 말씀드리면", "완벽하게 완료") 없이 핵심 내용 위주로 간결하게 보고한다.
   - 헤더에 장식용 이모지를 달지 않고, 과도한 볼드(`**`)와 구분선(`---`) 남발을 피한다.
   - 사용자 응답 및 코드 내 중국어/한자·프리앰블 절대 금지: 에이전트 간 내부 통신·추론 언어는 무관하나, 사용자에게 출력하는 모든 메시지와 코드·주석·문서는 한국어와 표준 영문만 사용하며 중국어/한자([\u4e00-\u9fff], 예: '用到的工具调用如下', '我将调用' 등)나 도구 호출 직전 안내 사족을 절대 포함하지 않는다.
5. Silent Widget Update: `usage_widget.html`을 백그라운드에서 조용히 갱신하고 채팅창에 `<agent-embed>`를 출력하지 않는다.

## `<MJSERVER_PORT_AND_SERVICE_GUARD>`

- mjserver (`192.168.10.10`)의 포트 기준 문서는 `/home/mjserver/dev/SERVER_PORTS.md`다. 포트를 바인딩하거나 프로세스를 종료하기 전에 반드시 이 문서를 읽고 `ss -tulpn`으로 실제 점유 상태를 확인한다.
- `4173/tcp` → `mjmaterial-web.service`: 무진소재 홈페이지 본 서버, 상시 가동. 절대 강제 종료하거나 다른 용도로 점유하지 않는다.
- `18765/tcp` → `agy-widget-bridge.service`: ai-maestro AOH 텔레메트리/위젯 데몬, 상시 가동. 점유하거나 종료하지 않는다.
- 무진소재 임시 검증·프리뷰는 `4174–4179/tcp` 중 비어 있는 포트를 골라 `MUJIN_PORT`로 지정한다(예: `MUJIN_PORT=4174`).
- `8080–8089/tcp`, `8554/tcp`, `1984/tcp`는 향후 내부 CCTV/RTSP/웹 서비스용 예약 포트다. `3000–3099/tcp`, `5000–5099/tcp`는 내부 자동화·웹앱용으로 예약한다.
- 상시 서비스를 추가할 때는 사용 포트, 바인딩 주소, 용도와 systemd unit을 `/home/mjserver/dev/SERVER_PORTS.md`에 등록한다.
