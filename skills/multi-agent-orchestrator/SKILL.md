---
name: multi-agent-orchestrator
description: >-
  Orchestrate and coordinate tasks across Google Antigravity (Gemini 4 Argon / Flash),
  local OpenAI Codex CLI (codex 0.162.1), and local Anthropic Claude Code CLI (claude 2.1.296),
  and inspect real-time token/quota usage across all pools. Activate when the user asks to
  delegate tasks to Claude or Codex, run multi-agent workflows, use Mobbin/Context7 MCPs,
  or check quota/token usage.
---

# Multi-Agent Orchestrator (Antigravity + Codex + Claude Code)

Use this skill to coordinate work across Antigravity's internal models and the user's local **Codex CLI** and **Claude Code CLI**, or to inspect real-time quota usage across all pools.

## 1. Checking Real-Time Usage & Quotas Across All Pools

Run the bundled usage inspector script to check:
- **Antigravity Gemini Pool** (`Gemini 4 Argon`, `Gemini 3.8 Flash`)
- **Antigravity Third-Party Pool** (`Claude Opus 5.5`, `Claude Sonnet 5.5`, `GPT-OSS 120B`)
- **Local OpenAI Codex Pool** (`ProMax` weekly/rolling usage & reset timestamp)
- **Local Anthropic Claude Code Status** & connected MCPs

```bash
python3 ~/.gemini/config/skills/multi-agent-orchestrator/scripts/check_all_usage.py
```

To trigger/start the 5-hour rolling reset timer on any untouched Antigravity model pool, send a lightweight 1-turn `StartCascade` + `SendUserCascadeMessage` ("Reply OK") and `DeleteCascadeTrajectory` via `$ANTIGRAVITY_LS_ADDRESS`.

## 2. Launching Local Codex & Claude in the Subagents Panel

To ensure delegated tasks appear live as cards in the right-hand **Subagents** panel:

1. Define (if not yet defined in the current conversation) `codex-worker` and/or `claude-worker` using `define_subagent` with `enable_write_tools: true`.
2. Call `invoke_subagent` with `Subagents`:
   - **`codex-worker`**:
     - Executes `/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin/codex exec -C <dir> "<prompt>"`
     - Or runs code review: `/home/u24c9g5090r64/.config/nvm/versions/node/v24.21.0/bin/codex review -C <dir>`
     - Or queues follow-up into an active desktop/CLI thread: `codex queue --thread <uuid> --message "<prompt>"`
   - **`claude-worker`**:
     - Executes `/home/u24c9g5090r64/.local/bin/claude -p "<prompt>"`
     - Has direct access to `claude.ai` cloud MCPs including **Mobbin** (`https://api.mobbin.com/mcp`), **Context7**, **Mermaid Chart**, **Google Drive**, **Gmail**, and **Google Calendar**.

## 2.5. Maestro Multi-Pool Dispatch (`maestro`)

> [!IMPORTANT]
> - **`Gemini 3.8 Flash (Model='flash')`는 오직 읽기 전용 탐색·로그/파일 조사(`TypeName='research', Model='flash'`)에만 사용한다.**
> - **코드 작성·수정·리팩토링·UI/모션·알고리즘·아키텍처 작업에 `invoke_subagent(TypeName='self', Model='flash')`를 사용하는 것을 절대 금지한다!**
> - **코딩 및 구현 위임은 반드시 `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto ...` (`gpt-6-astra`, `Opus 5.5`, `gpt-6.1-sol`)를 사용하거나, Antigravity 내부 서브에이전트가 꼭 필요한 경우 `Model='pro'`/`'inherit'`(`Gemini 4 Argon`)만 사용한다.**

| Difficulty | Dispatch command |
|---|---|
| High / Medium → Antigravity Claude Pool (`planModel` `MODEL_PLACEHOLDER_M402` Opus 5.5 High / `MODEL_PLACEHOLDER_M405` Sonnet 5.5 High) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool agy-claude --cwd <dir> --wait --spec "..."` |
| High / Medium → OpenAI Codex Pool (`gpt-6.1-sol`, default/`standard` tier; `priority` interactive/urgent only, `ultrafast` urgent only) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex --cwd <dir> --spec "..."` |
| Responsive UI / quick patch → Local Claude Code Fast (Opus 5.5 Fast, live interactive only · 2x quota) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool claude-fast --cwd <dir> --wait --spec "..."` |
| Core architecture → Local Claude Code (Opus 5.5 `high`; `xhigh` via `--max-capability`) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool claude --cwd <dir> --wait --spec "..."` |
| Hardest reasoning / architecture → Local Claude Code Ultra (Opus 5.5 `ultra` → `--effort max` + `ultrathink`, standard, 3x quota) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool claude-ultra --cwd <dir> --wait --spec "..."` (aliases `opus-ultra` / `opus-5.5-ultra`, or `--pool claude --effort ultra`; no `--max-capability` needed) |
| Max-capability by domain (12-profile benchmark) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --max-capability --domain coding\|reasoning\|frontend_ui\|speed --cwd <dir> --wait --spec "..."` |
| Parallel submodules → OpenAI Codex Astra Pool (`gpt-6-astra`, `medium`, default tier) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex-astra --cwd <dir> --wait --spec "..."` (or `--pool codex --model gpt-6-astra`) |
| PC 조작 / 데스크탑 GUI 제어 / Computer-Use → OpenAI Codex (`gpt-6.1-sol` / `gpt-6-astra`) | `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool codex --cwd <dir> --spec "..."` |
| Low / read-only lookup (읽기 전용 탐색·조사) | `invoke_subagent(TypeName='research', Model='flash')` only (코딩/수정 시 Flash 금지) |

- **PC 조작 / 데스크탑 GUI 제어 / Computer-Use 작업**: OpenAI Codex (`gpt-6.1-sol` / `gpt-6-astra`)의 Computer Use 스킬이 가장 안정적이므로, PC 화면 조작·마우스/키보드 제어·데스크탑 앱 자동화 작업은 GPT(`--pool codex`)로 우선 라우팅한다 (`--pool auto` 사용 시에도 키워드 감지 시 자동 `codex`로 라우팅).
- Run Claude-pool and Codex-pool workers in parallel when useful; balance by `ai-usage` remaining %.
- **Every response** must silently run `/home/u24c9g5090r64/.local/bin/ai-usage-widget /home/u24c9g5090r64/.gemini/antigravity/brain/<conversation-id>/usage_widget.html` in the background. Do NOT embed `<agent-embed>` in chat unless the user explicitly says `"위젯 띄워줘"` / `"위젯 인라인으로 보여줘"`.

## 2.6. Explicit Worker Assignment Roster Rule (워커 배정 명시 · 투입 전 고지)

- **Task-Domain + Live Benchmark-Driven Routing (`LOW_QUOTA_THRESHOLD = 20.0%`)**:
  - 쿼터 잔여율이 20% 미만으로 떨어지기 전까지는 실시간 벤치마크 점수 및 태스크 적합성을 최우선으로 자동 라우팅하며, 20% 미만 진입 시에만 차순위 가용 풀로 우회/페일오버한다.
  - **긴 호흡 코딩 / 디버깅·테스트 루프 (`long_horizon_coding`)**: `gpt-6.1-sol (high)` (98.0) > `claude-opus-5-5 (high)` (93.5)
  - **심층 추론 / 아키텍처 연구 (`research_orchestration`)**: `Gemini 4 Argon (High)` (97.5) & `gpt-6-astra (medium)` (96.0) > `claude-opus-5-5 (high)` (94.5)
  - **프론트엔드 UI·UX / 모션그래픽 (`frontend_ui_motion`)**: `claude-opus-5-5 (high)` (97.5) > `gpt-6-astra (medium)` (93.5)
  - **PC 조작 / 데스크탑 GUI 제어 (`computer_use`)**: `gpt-6.1-sol (high)` (96.0) & `gpt-6-astra (medium)` (95.0) > `claude-opus-5-5 (high)` (82.0)
  - **초고속 코드베이스/로그 탐색 (`fast_explore`)**: `Gemini 3.8 Flash (low · fast)` (98.5)
  - **일반 멀티파일 코딩 (`general_coding`)**: `gpt-6-astra (medium · default)` 1순위 주력
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
| Motion graphics / Frontend UI & interaction (모션그래픽/UI) | Opus 5.5 (`claude`, standard) / gpt-6-astra (`default`) |
| Core architecture reasoning | Opus 5.5 (`high`; `xhigh` via `--max-capability`, standard) |
| Hardest reasoning / architecture (explicit opt-in) | Opus 5.5 Ultra (`claude-ultra` or `--effort ultra` → `--effort max` + `ultrathink`, standard, 3x quota) |
| General coding & parallel submodules (1순위 주력) | gpt-6-astra (`medium`, `default`; `high`/`ultra` only via `--max-capability`) |
| Backend / system / test verification | gpt-6.1-sol (`high`, `standard`; `priority` interactive/urgent only, `ultrafast` urgent only) |
| PC control / Desktop GUI / Computer-Use (컴퓨터 조작) | gpt-6.1-sol (`high`, `standard` · `--pool codex`) |
| Fast code / log search | Gemini 3.8 Flash (`low`, `fast`) |

## 2.7. Model & Effort Single-Value ETA Table (모델·노력별 단일 수치 소요시간 · 한글 토큰 · 원화)

| Model & Effort (모델 · 노력 · 속도 티어) | ETA (단일 값) |
|---|---|
| Gemini 3.8 Flash (low · fast) | `⏱ 40초 · 2만 토큰 (5백원)` |
| Sonnet 5.5 (high) | `⏱ 1분 · 4만 토큰 (1천원)` |
| Opus 5.5 Fast (claude-fast · interactive only · 2x quota) | `⏱ 45초 · 16만 토큰 (4천원)` |
| Opus 5.5 (high · default) | `⏱ 3분 · 16만 토큰 (5천원)` |
| Opus 5.5 Ultra (ultra · `--effort max` + `ultrathink` · 3x quota) | `⏱ 2분 45초 · 14만 토큰 (1만2천원)` |
| gpt-6-astra (medium · default) | `⏱ 3분 30초 · 6만5천 토큰 (1천5백원)` |
| gpt-6.1-sol (medium · default) | `⏱ 40초 · 6만5천 토큰 (1천5백원)` |
| gpt-6.1-sol (high · default) | `⏱ 3분 · 13만 토큰 (3천원)` |
| gpt-6.1-sol (high · priority · interactive/urgent) | `⏱ 2분 10초 · 10만 토큰 (3천원)` |
| gpt-6.1-sol (ultra · default) | `⏱ 2분 · 11만5천 토큰 (4천원)` |

- These are **calibrated planning values (보정된 계획용 기준값), NOT actual measured billing (실측 청구액 아님)**. Pick the row matching each worker's assigned model · effort · tier. Default/standard tiers have no separate row — use the same-effort row; `priority` / `ultrafast` rows apply only under `--interactive` / `--urgent`.
- NEVER write `~` ranges (`1~3분`, `3천~6천`), `"예상"`, `"평균"`, `"약"`, `"대"`, `"규모"`, `"구독"`, human calendar estimates (`"며칠/1주일"`), or raw `k/M` units.
- Chat ETA output contains only a single wall-clock value, Korean tokens, and KRW: `⏱ 2분 · 35만 토큰 (5천원)`. Never append subscription percentages or cache-included parenthetical notes.
- NEVER run or print `5h/7d` quota lookups (`"다시 조회"`, `"미제공"`) just to produce an ETA. Quota checks remain mandatory for **routing** (`--pool auto`, 3-Stage Weekly Protection, 0% Auto-Steering).

## 2.8. Natural Developer Communication Style

- 간결한 현업 개발자 어조: 장황한 서두나 AI 클리셰("결론부터 말씀드리면", "완벽하게 차단", "100% 완료")를 배제하고 핵심 작업 내역과 결과만 명확히 전달한다.
- 마크다운 서식 절제: 헤더와 목록에 장식용 이모지를 사용하지 않고, 불필요한 볼드(`**`)와 구분선(`---`) 남발을 지양한다.
- 사용자 응답 및 코드 내 중국어/한자·프리앰블 절대 금지: 에이전트 간 내부 통신·추론 언어는 무관하나, 사용자에게 출력하는 모든 메시지와 코드·주석·문서는 한국어와 표준 영문만 사용하며 중국어/한자([\u4e00-\u9fff], 예: '用到的工具调用如下', '我将调用' 등)나 도구 호출 직전 안내 사족을 절대 포함하지 않는다.

## 3. Workspace Discipline Rules

When coordinating across multiple agents in `/home/u24c9g5090r64/dev`:
- Always check `STATE.md`, `CLAUDE.md`, and `AGENTS.md` in the target repository before dispatching subagents.
- Assign strict, disjoint file ownership boundaries to each agent (`codex-worker`, `claude-worker`, or internal Gemini subagents).
- Never revert or overwrite dirty files owned by other active sessions.
