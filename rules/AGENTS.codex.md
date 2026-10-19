# Codex Orchestration Rules (`ai-maestro`)
- Default tier: `service_tier = "default"` (standard) for background / cron / long autonomous work. Use `"priority"` (`Fast` ⚡) only for live interactive ping-pong, and `"ultrafast"` (`⚡⚡⚡`) only when urgent real-time turnaround is explicitly requested (see Speed Tier Policy below).
- For parallel sub-tasks, spawn child workers or use `/home/mjserver/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"` (`--pool codex|codex-astra|claude|claude-ultra|claude-fast|agy-argon|agy-claude|agy-flash`; `claude-ultra` / `opus-ultra` / `opus-5.5-ultra` = Opus 5.5 `--effort max` + `ultrathink`, 3x quota; `--model gpt-6-astra` for parallel submodules; `--effort low|medium|high|xhigh|max|ultra`, `--tier default|standard|priority|ultrafast`, `--max-capability`, `--domain coding|reasoning|frontend_ui|speed`).

## Model & Effort Single-Value ETA Table (모델·노력별 단일 수치 소요시간 · 한글 토큰 · 원화)

| Model & Effort (모델 · 노력 · 속도 티어) | ETA (단일 값) |
|---|---|
| Gemini 3.8 Flash (low · fast) / Sonnet 5.5 (high) | `⏱ 40초 · 5만 토큰 (1천원)` |
| Opus 5.5 Fast (claude-fast · interactive only · 2x quota) | `⏱ 45초 · 16만 토큰 (4천원)` |
| Opus 5.5 (high/xhigh) / Gemini 4 Argon (high) | `⏱ 2분 · 22만 토큰 (6천원)` |
| Opus 5.5 Ultra (ultra · `--effort max` + `ultrathink` · standard · 3x quota) | `⏱ 3분 · 120만 토큰 (1만5천원)` |
| gpt-6-astra (medium · default) / gpt-6.1-sol (low) | `⏱ 1분 · 35만 토큰 (3천원)` |
| gpt-6.1-sol (medium · priority · interactive/urgent) | `⏱ 1분 20초 · 80만 토큰 (7천원)` |
| gpt-6.1-sol (high · priority · interactive/urgent) | `⏱ 2분 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (high · ultrafast · urgent only) | `⏱ 1분 15초 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (ultra · priority/ultrafast · interactive/urgent) | `⏱ 2분 30초 · 180만 토큰 (1만8천원)` |
| gpt-6-astra (ultra · default · `--max-capability`) / 멀티풀 병렬 합산 | `⏱ 4분 · 350만 토큰 (3만원)` |

- 보정된 계획용 기준값이며 실제 청구액과 다를 수 있습니다. 워커에 지정된 모델, 노력, 티어에 맞는 행을 선택합니다.
- `~` 범위(`1~3분`), "예상", "평균", "약", "대" 등의 표현을 지양하고 단일 기준값을 제시합니다.
- 채팅 ETA 출력 형식: `⏱ 2분 · 35만 토큰 (5천원)`.

## Explicit Worker Assignment Roster Rule (워커 배정 명시 · 투입 전 고지)

- 워커 투입 시 모델, 노력 수준, 속도 티어를 사전 명시합니다.
- 사후 보고 시 `--pool auto` 배정 결과나 장애 조치(Failover) 내역을 기록합니다.
- 속도 티어 정책:
  - Claude Opus 5.5 Fast (`claude-fast`)는 표준 `claude-opus-5-5` 대비 2배 쿼터를 소모합니다 (`⏱ 45초 · 16만 토큰 (4천원)`). 사용자가 명시적으로 빠른 응답을 요구할 때만 투입하며, 단순 "오퍼스 시켜" 지시에는 표준 `claude-opus-5-5`를 사용합니다.
  - `fast` / `priority` / `ultrafast` 티어는 라이브 인터랙티브 핑퐁이나 사용자의 명시적 고속 요청 시에만 적용합니다.
  - cron, watchdog, 장시간 자율 실행, 백그라운드 작업은 standard/default 티어와 표준 `claude-opus-5-5`를 사용합니다.
  - CLI `maestro worker-start`는 `--interactive` / `--urgent`가 없으면 background 속도를 기본 적용합니다. `--background`는 interactive/urgent 속도를 오버라이드합니다.

| Concrete subtask | Model · Effort · Speed tier |
|---|---|
| Responsive UI / quick patch (명시적 fast 요청 시) | Opus 5.5 Fast (`claude-fast`, 2x quota) |
| Cron / watchdog / long autonomous / remote background | Opus 5.5 (`claude-opus-5-5`, standard) |
| Core architecture reasoning | Opus 5.5 (`high`; `xhigh` via `--max-capability`, standard) |
| Hardest reasoning / architecture (explicit opt-in) | Opus 5.5 Ultra (`claude-ultra` or `--effort ultra` → `--effort max` + `ultrathink`, standard, 3x quota) |
| Backend / system / test verification | gpt-6.1-sol (`high`, `standard`; `priority` interactive/urgent only, `ultrafast` urgent only) |
| Parallel submodule implementation | gpt-6-astra (`medium`, `default`; `high`/`ultra` only via `--max-capability`) |
| Fast code / log search | Gemini 3.8 Flash (`low`, `fast`) |

## Natural Developer Communication Style
- 장황한 서두나 AI 클리셰 없이 핵심 내용 위주로 간결하게 보고한다.
- 헤더에 장식용 이모지를 달지 않고, 과도한 볼드(`**`)와 구분선(`---`) 남발을 피한다.
- 중국어/한자 및 도구 호출 프리앰블 절대 금지: 모든 응답과 사고 과정은 한국어와 표준 영문 기술 용어로만 작성하며, 중국어/한자([\u4e00-\u9fff], 예: '用到的工具调用如下', '我将调用' 등)나 도구 호출 직전 안내 사족을 절대 출력하지 않는다.

## `<MJSERVER_PORT_AND_SERVICE_GUARD>`

- mjserver (`192.168.10.10`)의 포트 기준 문서는 `/home/mjserver/dev/SERVER_PORTS.md`다. 포트를 바인딩하거나 프로세스를 종료하기 전에 반드시 이 문서를 읽고 `ss -tulpn`으로 실제 점유 상태를 확인한다.
- `4173/tcp` → `mjmaterial-web.service`: 무진소재 홈페이지 본 서버, 상시 가동. 절대 강제 종료하거나 다른 용도로 점유하지 않는다.
- `18765/tcp` → `agy-widget-bridge.service`: ai-maestro AOH 텔레메트리/위젯 데몬, 상시 가동. 점유하거나 종료하지 않는다.
- 무진소재 임시 검증·프리뷰는 `4174–4179/tcp` 중 비어 있는 포트를 골라 `MUJIN_PORT`로 지정한다(예: `MUJIN_PORT=4174`).
- `8080–8089/tcp`, `8554/tcp`, `1984/tcp`는 향후 내부 CCTV/RTSP/웹 서비스용 예약 포트다. `3000–3099/tcp`, `5000–5099/tcp`는 내부 자동화·웹앱용으로 예약한다.
- 상시 서비스를 추가할 때는 사용 포트, 바인딩 주소, 용도와 systemd unit을 `/home/mjserver/dev/SERVER_PORTS.md`에 등록한다.
