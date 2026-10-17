# 🎼 Codex Orchestration Rules (`ai-maestro`)
- Default tier: `service_tier = "priority"` (`Fast` ⚡). Use `"ultrafast"` (`⚡⚡⚡`) only when urgent real-time turnaround is explicitly requested.
- For parallel sub-tasks, spawn child workers or use `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"` (`--pool codex|codex-astra|claude|claude-fast|agy-argon|agy-claude|agy-flash`; `--model gpt-6-astra` for parallel submodules).

## Model & Effort Single-Value ETA Table (모델·노력별 단일 수치 소요시간 · 한글 토큰 · 원화)

| Model & Effort (모델 · 노력 · 속도 티어) | ETA (단일 값) |
|---|---|
| Gemini 3.8 Flash (low · fast) / Sonnet 5.5 (high) | `⏱ 40초 · 5만 토큰 (1천원)` |
| Opus 5.5 Fast (claude-fast) | `⏱ 45초 · 15만 토큰 (3천원)` |
| Opus 5.5 (high/xhigh) / Gemini 4 Argon (high) | `⏱ 2분 · 22만 토큰 (6천원)` |
| gpt-6-astra (medium · priority) / gpt-6.1-sol (low) | `⏱ 1분 · 35만 토큰 (3천원)` |
| gpt-6.1-sol (medium · priority) | `⏱ 1분 20초 · 80만 토큰 (7천원)` |
| gpt-6.1-sol (high · priority) | `⏱ 2분 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (high · ultrafast) | `⏱ 1분 15초 · 120만 토큰 (1만2천원)` |
| gpt-6.1-sol (ultra · priority/ultrafast) | `⏱ 2분 30초 · 180만 토큰 (1만8천원)` |
| gpt-6-astra (ultra) / 멀티풀 병렬 합산 | `⏱ 4분 · 350만 토큰 (3만원)` |

- These are **calibrated planning values (보정된 계획용 기준값), NOT actual measured billing (실측 청구액 아님)**. Pick the row matching each worker's assigned model · effort · tier.
- NEVER write `~` ranges (`1~3분`, `3천~6천`), `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, human calendar estimates (`"며칠/1주일"`), or raw `k/M` units.
- NEVER run or print `5h/7d` quota lookups (`"다시 조회"`, `"미제공"`) just to produce an ETA. Quota checks remain mandatory for **routing** (`--pool auto`, 3-Stage Weekly Protection, 0% Auto-Steering).

## Explicit Worker Assignment Roster Rule (워커 배정 명시 · 투입 전 고지)

- Whenever delegating one or many workers (`maestro worker-start`, `invoke_subagent`, child workers), ALWAYS state the exact **model + effort + speed tier** assigned to each concrete subtask **BEFORE launch**, with its ETA row.
- After launch / completion, report every actual `--pool auto` assignment or fallback / failover change (`requested → actual` model · effort · tier + reason, e.g. 0% quota steering).
- Speed tier: Codex default is `priority`; `ultrafast` ONLY with `--urgent` (urgent real-time turnaround).

| Concrete subtask | Model · Effort · Speed tier |
|---|---|
| Responsive UI / quick patch | Opus 5.5 Fast (`claude-fast`) |
| Core architecture reasoning | Opus 5.5 (`xhigh`/`ultra`) |
| Backend / system / test verification | gpt-6.1-sol (`high`, `ultrafast`/`priority`) |
| Parallel submodule implementation | gpt-6-astra (`medium`, `priority`) |
| Fast code / log search | Gemini 3.8 Flash (`low`, `fast`) |
