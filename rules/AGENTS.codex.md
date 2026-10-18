# 🎼 Codex Orchestration Rules (`ai-maestro`)
- Default tier: `service_tier = "default"` (standard) for background / cron / long autonomous work. Use `"priority"` (`Fast` ⚡) only for live interactive ping-pong, and `"ultrafast"` (`⚡⚡⚡`) only when urgent real-time turnaround is explicitly requested (see Speed Tier Policy below).
- For parallel sub-tasks, spawn child workers or use `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"` (`--pool codex|codex-astra|claude|claude-ultra|claude-fast|agy-argon|agy-claude|agy-flash`; `claude-ultra` / `opus-ultra` / `opus-5.5-ultra` = Opus 5.5 `--effort max` + `ultrathink`, 3x quota; `--model gpt-6-astra` for parallel submodules; `--effort low|medium|high|xhigh|max|ultra`, `--tier default|standard|priority|ultrafast`, `--max-capability`, `--domain coding|reasoning|frontend_ui|speed`).

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

- These are **calibrated planning values (보정된 계획용 기준값), NOT actual measured billing (실측 청구액 아님)**. Pick the row matching each worker's assigned model · effort · tier. Default/standard tiers have no separate row — use the same-effort row; `priority` / `ultrafast` rows apply only under `--interactive` / `--urgent`.
- NEVER write `~` ranges (`1~3분`, `3천~6천`), `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, human calendar estimates (`"며칠/1주일"`), or raw `k/M` units.
- Chat ETA output contains only a single wall-clock value, Korean tokens, and KRW: `⏱ 2분 · 35만 토큰 (5천원)`. Never append subscription percentages or cache-included parenthetical notes.
- NEVER run or print `5h/7d` quota lookups (`"다시 조회"`, `"미제공"`) just to produce an ETA. Quota checks remain mandatory for **routing** (`--pool auto`, 3-Stage Weekly Protection, 0% Auto-Steering).

## Explicit Worker Assignment Roster Rule (워커 배정 명시 · 투입 전 고지)

- Whenever delegating one or many workers (`maestro worker-start`, `invoke_subagent`, child workers), ALWAYS state the exact **model + effort + speed tier** assigned to each concrete subtask **BEFORE launch**, with its ETA row.
- After launch / completion, report every actual `--pool auto` assignment or fallback / failover change (`requested → actual` model · effort · tier + reason, e.g. 0% quota steering).
- **Speed Tier Policy (속도 티어 정책)**:
  - Claude Opus 5.5 Fast (`claude-fast`) consumes **2x quota** vs. standard `claude-opus-5-5`; clean ETA: `⏱ 45초 · 16만 토큰 (4천원)`.
  - `fast` / `priority` / `ultrafast` tiers are ONLY for live interactive ping-pong, immediate responsiveness, when the user says `"느리다"`, or when the user explicitly asks for fast.
  - Scheduled cron / watchdog, long autonomous runs, and remote background work use standard/default tiers and standard `claude-opus-5-5`; multi-agent parallel orchestration stays active.
  - CLI `maestro worker-start` defaults to background speed unless `--interactive` / `--urgent`; `--background` overrides interactive/urgent speed; explicit `--pool claude-fast` remains opt-in even in background.
  - `--tier default|standard|priority|ultrafast` is supported: `priority` requires `--interactive` / `--urgent`; `ultrafast` requires `--urgent`.
  - Astra (`gpt-6-astra`) is ALWAYS default tier, with a `medium` effort cap unless `--max-capability` unlocks `high` / `ultra`.
  - `--max-capability` + `--domain coding|reasoning|frontend_ui|speed` uses the `benchmark_sync` recommendation and retains quota-exhaustion steering; an explicit `--pool` constrains the recommendation.
  - Claude Opus 5.5 Ultra (`--pool claude-ultra` / `opus-ultra` / `opus-5.5-ultra`, or `--pool claude --effort ultra`) runs standard `claude-opus-5-5` with `--effort max` and prepends `ultrathink` to the prompt; it consumes **3x quota** vs. standard `claude-opus-5-5`, never uses the Fast tier, and is therefore allowed in background runs; clean ETA: `⏱ 3분 · 120만 토큰 (1만5천원)`.
  - Explicit `--effort ultra` / `--effort max` (or an ultra alias pool) is allowed WITHOUT `--max-capability`; only implicit escalation is gated — with no `--effort`, Claude runs `high`, or `xhigh` under `--max-capability`.
  - **Max-Capability Policy**: `--max-capability` also excludes the Fast profile unless `--pool claude-fast` is explicit, disables auto-Fast under `--interactive` / `--urgent`, and unlocks Astra `high` / `ultra`; reserve Opus 5.5 Ultra (3x quota) for the hardest reasoning / architecture subtasks.
  - The `benchmark_sync` matrix has **12 profiles** scored across **4 domains** (`coding`, `reasoning`, `frontend_ui`, `speed`), each with a quota multiplier (Opus 5.5 Ultra 3x, Opus 5.5 Fast 2x); JSON/YAML coding scores update only on a matching model + effort.

| Concrete subtask | Model · Effort · Speed tier |
|---|---|
| Responsive UI / quick patch (live interactive only) | Opus 5.5 Fast (`claude-fast`, 2x quota) |
| Cron / watchdog / long autonomous / remote background | Opus 5.5 (`claude-opus-5-5`, standard) |
| Core architecture reasoning | Opus 5.5 (`high`; `xhigh` via `--max-capability`, standard) |
| Hardest reasoning / architecture (explicit opt-in) | Opus 5.5 Ultra (`claude-ultra` or `--effort ultra` → `--effort max` + `ultrathink`, standard, 3x quota) |
| Backend / system / test verification | gpt-6.1-sol (`high`, `standard`; `priority` interactive/urgent only, `ultrafast` urgent only) |
| Parallel submodule implementation | gpt-6-astra (`medium`, `default`; `high`/`ultra` only via `--max-capability`) |
| Fast code / log search | Gemini 3.8 Flash (`low`, `fast`) |

## `<MJSERVER_PORT_AND_SERVICE_GUARD>`

- mjserver (`192.168.10.10`)의 포트 기준 문서는 `/home/mjserver/dev/SERVER_PORTS.md`다. **포트를 바인딩하거나 프로세스를 종료하기 전에 반드시 이 문서를 읽고 `ss -tulpn`으로 실제 점유 상태를 확인한다.**
- `4173/tcp` → `mjmaterial-web.service`: 무진소재 홈페이지 본 서버, 상시 가동. 절대 강제 종료하거나 다른 용도로 점유하지 않는다.
- `18765/tcp` → `agy-widget-bridge.service`: ai-maestro AOH 텔레메트리/위젯 데몬, 상시 가동. 점유하거나 종료하지 않는다.
- 무진소재 임시 검증·프리뷰는 `4174–4179/tcp` 중 비어 있는 포트를 골라 `MUJIN_PORT`로 지정한다(예: `MUJIN_PORT=4174`).
- `8080–8089/tcp`, `8554/tcp`, `1984/tcp`는 향후 내부 CCTV/RTSP/웹 서비스용 예약 포트다. `3000–3099/tcp`, `5000–5099/tcp`는 내부 자동화·웹앱용으로 예약한다.
- 상시 서비스를 추가할 때는 사용 포트, 바인딩 주소, 용도와 systemd unit을 `/home/mjserver/dev/SERVER_PORTS.md`에 등록한다.
