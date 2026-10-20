# Workspace Orchestration Harness (`ai-maestro`)
Global rules in `~/.gemini/GEMINI.md` are active. Workspace essential policies:

1. `<MAESTRO_DISPATCH_GATE>`:
   - `<thought>` 3~4줄 이내 작성 후 첫 툴로 즉시 위임:
     * 읽기 전용 탐색·조사: `invoke_subagent(TypeName='research', Model='flash')` 전용 (research 타입만 허용).
     * 코딩·구현·수정·설계: Flash 사용 절대 금지! `maestro worker-start --pool auto --difficulty <high|medium> --cwd <dir> --wait --spec "<task>"` (`gpt-6-astra`, `Opus 5.5`, `gpt-6.1-sol`) 또는 `Model='pro'`/`'inherit'`(`Gemini 4 Argon`)만 허용.
   - 메인 에이전트 단독 도구 연속 실행 2회 초과 금지 (Hard Solo-Tool Cap, 단 읽기 전용 `view_file`은 최대 4회까지 허용).
   - Hierarchical Same-Family Sub-Orchestration (`MAESTRO_WORKER_DEPTH <= 2` & Same-Pool Lock): Depth 1 서브 워커가 3개 이상 독립 모듈 처리 시 분할 계획 보고 후 동일 모델 패밀리(Claude→Claude, Codex→Codex) 내에서만 Depth 2 리프 워커 분기 기동. effort는 최대 `high`로 제한되며 속도 티어는 `default` 고정. Depth 2 리프 워커는 추가 위임(Depth 3)이 차단되며 직접 완수해야 함.
2. Task-Domain + Live Benchmark-Driven Routing & Speed Tiers:
   - Task-Fit Benchmark Routing with Low-Quota Guard (`LOW_QUOTA_THRESHOLD = 20.0%`): 20% 미만 진입 전까지 실시간 벤치마크 점수·태스크 적합성 우선 자동 배정 (긴 호흡 코딩: `gpt-6.1-sol`, 심층 추론: `Gemini 4 Argon` & `gpt-6-astra`, 모션/UI: `Opus 5.5`, PC 조작: `gpt-6.1-sol`, 빠른 탐색: `Gemini 3.8 Flash`, 일반 코딩: `gpt-6-astra`).
   - `Opus 5.5 Fast (claude-fast · 2x quota)`: 명시적 fast 요청 전용.
   - `Opus 5.5 (xhigh/high · claude / agy-claude)`: 아키텍처 및 심층 추론, 프론트엔드 UI/UX 및 모션그래픽.
   - `Opus 5.5 Ultra (ultra · 3x quota)`: 최고난도 추론 전용 (`--effort ultra`).
   - `gpt-6.1-sol (high/medium · standard)`: 백엔드/테스트 루프 (`priority`는 `--interactive`/`--urgent` 필요).
   - `gpt-6-astra (medium · default)`: 전반적인 일반 코딩 및 모션그래픽 구현 (`--pool auto --difficulty medium` 자동 선택).
   - `Gemini 3.8 Flash (low · fast)`: 초고속 코드/로그 탐색.
   - 모션그래픽 / UI·UX: Claude Opus 5.5 (`--pool claude`) 우선 라우팅 (`is_motion_or_ui_task()` 자동 감지).
   - PC 조작 / 데스크탑 GUI 제어 / Computer-Use: OpenAI Codex(`gpt-6.1-sol` / `--pool codex`) 우선 라우팅.
   - `fast`/`priority`/`ultrafast`는 대화형 인터랙티브 전용. 백그라운드/cron은 standard/default 티어 사용.
3. Single-Value ETA Standard:
   - 단일 수치 형식 예: `⏱ 3분 · 13만 토큰 (3천원)`. 범위(`~`) 표기나 금지어 배제.
4. Natural Developer Style & Zero-Chinese:
   - AI 클리셰 없는 간결한 개발자 어조. 장식용 이모지/구분선 지양.
   - 사용자 응답 및 코드·문서 내 중국어/한자([\u4e00-\u9fff]) 및 도구 호출 전 프리앰블 절대 금지.
5. Service Guard:
   - 포트/서비스 변경 전 `/home/mjserver/dev/SERVER_PORTS.md` 확인 필수 (`4173`: mjmaterial, `18765`: agy-widget-bridge 상시 가동).
