# Workspace Orchestration Harness (`ai-maestro`)
Global rules in `~/.gemini/GEMINI.md` are active. Workspace essential policies:

1. `<MAESTRO_DISPATCH_GATE>`:
   - `<thought>` 3~4줄 이내 작성 후 첫 툴로 즉시 위임 (`invoke_subagent(Model='flash')` 또는 `maestro worker-start`).
   - 메인 에이전트 단독 도구 연속 실행 2회 초과 금지 (Hard Solo-Tool Cap).
2. Explicit Worker Role Roster & Speed Tiers:
   - `Opus 5.5 Fast (claude-fast · 2x quota)`: 명시적 fast 요청 전용.
   - `Opus 5.5 (xhigh/high · claude / agy-claude)`: 아키텍처 및 심층 추론.
   - `Opus 5.5 Ultra (ultra · 3x quota)`: 최고난도 추론 전용 (`--effort ultra`).
   - `gpt-6.1-sol (high/medium · standard)`: 백엔드/테스트 루프 (`priority`는 `--interactive`/`--urgent` 필요).
   - `gpt-6-astra (medium · default)`: 보조 모듈 병렬 구현.
   - `Gemini 3.8 Flash (low · fast)`: 초고속 코드/로그 탐색.
   - `fast`/`priority`/`ultrafast`는 대화형 인터랙티브 전용. 백그라운드/cron은 standard/default 티어 사용.
3. Single-Value ETA Standard:
   - 단일 수치 형식 예: `⏱ 2분 · 35만 토큰 (5천원)`. 범위(`~`) 표기나 금지어 배제.
4. Natural Developer Style & Zero-Chinese:
   - AI 클리셰 없는 간결한 개발자 어조. 장식용 이모지/구분선 지양.
   - 사용자 응답 및 코드·문서 내 중국어/한자([\u4e00-\u9fff]) 및 도구 호출 전 프리앰블 절대 금지.
5. Service Guard:
   - 포트/서비스 변경 전 `/home/mjserver/dev/SERVER_PORTS.md` 확인 필수 (`4173`: mjmaterial, `18765`: agy-widget-bridge 상시 가동).
