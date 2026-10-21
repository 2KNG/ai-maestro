---
name: multi-agent-orchestrator
description: >-
  Orchestrate and coordinate tasks across Google Antigravity (Gemini 4 Argon / Flash),
  local OpenAI Codex CLI (codex 0.162.1), and local Anthropic Claude Code CLI (claude 2.1.296),
  and inspect real-time token/quota usage across all pools. Activate when the user asks to
  delegate tasks to Claude or Codex, run multi-agent workflows, use Mobbin/Context7 MCPs,
  check quota/token usage, drive Orca terminals/worktrees, or install/set up ai-maestro
  (CLIs, optional local Orca, optional remote hub).
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

## 2. Launching Local Codex & Claude Workers

Delegate coding, implementation, and architecture through `run_command` using
`~/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec-file <path>`.
The worker pool selects Opus 5.5, gpt-6-astra, gpt-6.1-sol, or gpt-6-luna.
Use `--spec-file` for prompts containing shell-sensitive text.

Do not define or invoke `codex-worker` / `claude-worker` wrappers through
`invoke_subagent`. Every `invoke_subagent` entry must explicitly use
`TypeName='research'` with `Model='flash'` or `Model='flash_lite'` for read-only
lookups. The harness blocks every other type (including missing type, which
means `self`) and every other model (including missing model, which means
`inherit`) to protect 4 Argon quota.

## 2.5. Maestro Multi-Pool Dispatch (`maestro`)

> [!IMPORTANT]
> - `invoke_subagent`는 읽기 전용 탐색·로그/파일 조사에만 사용하며, 모든 항목에 `TypeName='research'`와 `Model='flash'` 또는 `'flash_lite'`를 지정한다.
> - `TypeName='self'`, 사용자 정의 타입, `Model='pro'`/`'inherit'`, 타입 또는 모델 생략은 4 Argon 쿼터 보호를 위해 하네스가 강제 차단한다 (`decision: block`, 종료 코드 2).
> - 모든 코딩·구현·리팩토링·UI/모션·알고리즘·아키텍처 위임은 `run_command`에서 `~/.local/bin/maestro worker-start --pool auto ...` (`Opus 5.5`, `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`)로 수행한다.
> - Claude 쿼터 적극 활용 (Quota-Surplus Dispatch): 코딩·디버깅·아키텍처·UI 위임은 `--pool auto` 또는 `--pool claude`(`Opus 5.5`)를 사용하고 `--pool codex`를 습관적으로 하드코딩하지 않는다. `--pool auto`는 `benchmark_sync.recommend_model_for_task()`의 Reset-Aware Quota Surplus(Claude 7d ≥ 50% · 리셋까지 일일 소진 여력이 Codex보다 크면 최대 +10점)와 활성 워커 부하 분산(실행 중 워커 수 차이 1개당 +3점, 최대 +6, `agy_spawned_sessions.json` + `/proc` 기준)을 반영해 병렬 워커를 `Opus 5.5`와 `gpt-6.1-sol`/`gpt-6-astra`로 나눈다. `--pool codex`는 PC 조작/Computer-Use처럼 명시적 이유가 있을 때만 지정한다.
> - 투입 전 라우팅 확인(드라이런, 워커 미기동): `~/.local/bin/maestro route --task '<task>' --difficulty <high|medium|low>`.

| Difficulty | Dispatch command |
|---|---|
| High / Medium → Antigravity Claude Pool (`planModel` `MODEL_PLACEHOLDER_M402` Opus 5.5 High / `MODEL_PLACEHOLDER_M405` Sonnet 5.5 High) | `~/.local/bin/maestro worker-start --pool agy-claude --cwd <dir> --wait --spec "..."` |
| High / Medium → OpenAI Codex Pool (`gpt-6.1-sol`, default/`standard` tier; `priority` interactive/urgent only, `ultrafast` urgent only) | `~/.local/bin/maestro worker-start --pool codex --cwd <dir> --spec "..."` |
| Responsive UI / quick patch → Local Claude Code Fast (Opus 5.5 Fast, live interactive only · 2x quota) | `~/.local/bin/maestro worker-start --pool claude-fast --cwd <dir> --wait --spec "..."` |
| Core architecture → Local Claude Code (Opus 5.5 `high`; `xhigh` via `--max-capability`) | `~/.local/bin/maestro worker-start --pool claude --cwd <dir> --wait --spec "..."` |
| Hardest reasoning / architecture → Local Claude Code Ultra (Opus 5.5 `ultra` → `--effort max` + `ultrathink`, standard, 3x quota) | `~/.local/bin/maestro worker-start --pool claude-ultra --cwd <dir> --wait --spec "..."` (aliases `opus-ultra` / `opus-5.5-ultra`, or `--pool claude --effort ultra`; no `--max-capability` needed) |
| Max-capability by domain (12-profile benchmark) | `~/.local/bin/maestro worker-start --pool auto --max-capability --domain coding\|reasoning\|frontend_ui\|speed --cwd <dir> --wait --spec "..."` |
| Parallel submodules → OpenAI Codex Astra Pool (`gpt-6-astra`, `medium`, default tier) | `~/.local/bin/maestro worker-start --pool codex-astra --cwd <dir> --wait --spec "..."` (or `--pool codex --model gpt-6-astra`) |
| Lightweight fast coding / small fixes → OpenAI Codex Luna Pool (`gpt-6-luna`, `low`/`medium`, default tier) | `~/.local/bin/maestro worker-start --pool codex-luna --cwd <dir> --wait --spec "..."` (or `--pool codex --model gpt-6-luna`) |
| PC 조작 / 데스크탑 GUI 제어 / Computer-Use → OpenAI Codex (`gpt-6.1-sol` / `gpt-6-astra`) | `~/.local/bin/maestro worker-start --pool codex --cwd <dir> --spec "..."` |
| Low / read-only lookup (읽기 전용 탐색·조사) | `invoke_subagent(TypeName='research', Model='flash')` (`flash_lite` also allowed; 코딩/수정 금지) |

- **Hierarchical Same-Family Sub-Orchestration (`MAESTRO_WORKER_DEPTH <= 2` & Same-Pool Lock)**:
  - When a Depth 1 sub-worker receives an oversized task (3+ independent modules or large implementation + verification), it reports its split plan (`[SUB-ORCHESTRATION REPORT]`) and spawns Depth 2 leaf workers (`MAESTRO_WORKER_DEPTH=2`) within its own model family only.
  - Claude sub-worker (`parent_pool == 'claude'`) is strictly locked to Claude workers (`--pool claude`, `Opus 5.5`).
  - Codex sub-worker (`parent_pool == 'codex'`) is strictly locked to Codex workers (`--pool codex`, `gpt-6-astra` or `gpt-6.1-sol`).
  - Sub-workers can choose `--effort` (`low`, `medium`, `high`) freely, but `ultra` is clamped to `high`, and speed tiers (`fast`, `ultrafast`, `priority`) are clamped to `default` (preventing 2x/3x quota burn without user opt-in).
  - Max depth guard: `MAESTRO_WORKER_DEPTH >= 2` blocks Depth 3 recursion and instructs the leaf worker to execute directly.
- **Shell Backtick Crash Guard (`--spec` 백틱 폭발 방지)**: `maestro worker-start` 호출 시 프롬프트 안에 백틱(`` ` ``), `$`, `()`, 따옴표가 포함될 경우 절대 큰따옴표(`--spec "..."`) 안에 그냥 넣지 않는다(Bash가 백틱 안의 `` `ssh orin` ``이나 `` `func()` ``를 쉘 명령어로 실행해 크래시·무한 행이 발생함). 반드시 **작은따옴표 Heredoc (`<<'EOF' ... EOF`)** 또는 `--spec '...'` / `--spec-file`을 사용한다!
- **PC 조작 / 데스크탑 GUI 제어 / Computer-Use 작업**: OpenAI Codex (`gpt-6.1-sol` / `gpt-6-astra`)의 Computer Use 스킬이 가장 안정적이므로, PC 화면 조작·마우스/키보드 제어·데스크탑 앱 자동화 작업은 GPT(`--pool codex`)로 우선 라우팅한다 (`--pool auto` 사용 시에도 키워드 감지 시 자동 `codex`로 라우팅).
- Run Claude-pool and Codex-pool workers in parallel when useful; balance by `ai-usage` remaining %.
- **Every response** must silently run `~/.local/bin/ai-usage-widget <appDataDir>/brain/<conversation-id>/usage_widget.html` in the background. Do NOT embed `<agent-embed>` in chat unless the user explicitly says `"위젯 띄워줘"` / `"위젯 인라인으로 보여줘"`.

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
  - Claude Opus 5.5 Ultra (`--pool claude-ultra` / `opus-ultra` / `opus-5.5-ultra`, or `--pool claude --effort ultra`) runs standard `claude-opus-5-5` with `--effort max` and prepends `ultrathink` to the prompt; it consumes **3x quota** vs. standard `claude-opus-5-5`, never uses the Fast tier, and is therefore allowed in background runs; clean ETA: `⏱ 2분 45초 · 14만 토큰 (1만2천원)`.
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
| Lightweight fast coding / small fixes | gpt-6-luna (`low`/`medium`, `default` · `--pool codex-luna`) |

## 2.7. Model & Effort Single-Value ETA Table (모델·노력별 단일 수치 소요시간 · 한글 토큰 · 원화)

| Model & Effort (모델 · 노력 · 속도 티어) | ETA (단일 값) |
|---|---|
| Gemini 3.8 Flash (low · fast) | `⏱ 40초 · 2만 토큰 (5백원)` |
| gpt-6-luna (low · default) | `⏱ 35초 · 2만5천 토큰 (5백원)` |
| gpt-6-luna (medium · default) | `⏱ 1분 · 4만 토큰 (8백원)` |
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
- Chat ETA output contains only a single wall-clock value, Korean tokens, and KRW: `⏱ 3분 · 13만 토큰 (3천원)`. Never append subscription percentages or cache-included parenthetical notes.
- NEVER run or print `5h/7d` quota lookups (`"다시 조회"`, `"미제공"`) just to produce an ETA. Quota checks remain mandatory for **routing** (`--pool auto`, 3-Stage Weekly Protection, 0% Auto-Steering).

## 2.8. Natural Developer Communication Style

- 간결한 현업 개발자 어조: 장황한 서두나 AI 클리셰("결론부터 말씀드리면", "완벽하게 차단", "100% 완료")를 배제하고 핵심 작업 내역과 결과만 명확히 전달한다.
- 마크다운 서식 절제: 헤더와 목록에 장식용 이모지를 사용하지 않고, 불필요한 볼드(`**`)와 구분선(`---`) 남발을 지양한다.
- 사용자 응답 및 코드 내 중국어/한자·프리앰블 절대 금지: 에이전트 간 내부 통신·추론 언어는 무관하나, 사용자에게 출력하는 모든 메시지와 코드·주석·문서는 한국어와 표준 영문만 사용하며 중국어/한자([\u4e00-\u9fff], 예: '用到的工具调用如下', '我将调用' 등)나 도구 호출 직전 안내 사족을 절대 포함하지 않는다.

## 3. Workspace Discipline Rules

When coordinating across multiple agents in `~/dev`:
- Always check `STATE.md`, `CLAUDE.md`, and `AGENTS.md` in the target repository before dispatching subagents.
- Assign strict, disjoint file ownership boundaries to each agent (Maestro Codex/Claude workers or read-only Flash research subagents).
- Never revert or overwrite dirty files owned by other active sessions.

## 4. Orca-Based Operation

`maestro` runs inside and alongside Orca worktrees/terminals. Inside an Orca terminal `ORCA_TERMINAL_HANDLE` and `ORCA_WORKTREE_PATH` are set; `maestro orca-status` reports them.

```bash
maestro orca-status                          # runtime summary + current Orca terminal
maestro orca-list [--worktree path:<dir>]    # live terminals: handle, title, agent, worktree
maestro orca-worktrees                       # Orca-managed worktrees
maestro orca-send --terminal <handle> --text "<input>" --enter
maestro orca-read --terminal <handle> [--screen]
maestro remote status                        # node services + active Orca terminals
maestro remote nodes                         # registered fleet nodes (1..N, no placeholder slots)
```

`ai-usage` (key `orca`), the widget host pill, and the bridge endpoint `GET /api/orca` show the same Orca summary.

Remote hub settings are user-local: CLI flags > `MAESTRO_REMOTE_HUB` / `MAESTRO_SSH_KEY` / `MAESTRO_REGISTRY_URL` / `MAESTRO_NODE_ID` > `~/.config/ai-maestro/remote.json`. Never write a server address, IP or key path into repository files.

<AI_SETUP_ONBOARDING>

## 5. AI-Assisted Setup Onboarding

Use this when the user asks to install or set up `ai-maestro`, or says "설치할 때 서버 주소나 IP 물어보고 매칭해줘". Step 3 is optional: without a server address/IP, `ai-maestro` completes in Local Orchestrator Mode with Antigravity + Codex + Claude orchestration and the local widget at `127.0.0.1:18765`; if Orca is installed, local Orca is available at `127.0.0.1:6768`. If the user provides a server address/IP during setup or later, additionally connect the remote fleet hub. A request such as "서버 안 붙이고 로컬에서만 쓸게" means skip remote setup without prompting again. `maestro setup` implements this flow for terminals (`install.sh` / `install.ps1` run it last).

1. Core agent CLIs — ask exactly:
   "오르카(Orca) 및 AI Maestro 구동에 필요한 기본 CLI [Antigravity / Codex / Claude] (기본 전체 선택)를 먼저 확인하고 설치할까요?"
   Defaults: `[x] Antigravity` `[x] OpenAI Codex CLI` `[x] Claude Code CLI`. On yes run `maestro setup --yes --skip-orca --skip-remote` (or `--cli codex,claude` for a subset). It installs only missing CLIs and restores `~/.codex/config.toml` and `~/.claude/settings.json` byte-for-byte if an installer touched them; do not edit those files yourself.
2. Orca — ask exactly:
   "이어서 오르카(Orca)를 설치하고 AI Maestro와 연동할까요?"
   On yes run `maestro setup --yes --skip-cli --skip-remote`. If Orca is missing, give the user https://github.com/stablyai/orca/releases/latest, wait for the install, and rerun. Confirm with `maestro orca-status`.
3. Optional server address — ask:
   "[선택] 원격 관제 허브 서버 주소(도메인)나 IP를 입력해 주세요. Enter를 누르면 로컬 오케스트레이터 모드로 완료합니다."
   If the user presses Enter/skips or requests local-only operation, finish successfully in Local Orchestrator Mode; do not invoke `maestro remote up` or create tunnel services. When the user provides an address now or later (for example "내 서버 주소는 hub.example.com이야, 매칭해줘"), additionally run:
   ```bash
   maestro remote up --hub <address>
   ```
   For a bare IP also ask for the hub's HTTPS domain and add `--registry-url https://<domain>` (wss pairing needs a TLS certificate). Add `--identity-file <path>` if the user names an SSH key. Report `blockers` verbatim when present; on success report `fleet_url` and `maestro remote status`.

The address is saved only to `~/.config/ai-maestro/remote.json`.

</AI_SETUP_ONBOARDING>
