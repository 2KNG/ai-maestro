# Workspace Orchestration Harness (`ai-maestro`)
Global rules in `~/.gemini/GEMINI.md` are active. Workspace essential policies:

1. `<MAESTRO_DISPATCH_GATE>`:
   - `<thought>` 3~4줄 이내 작성 후 첫 툴로 즉시 위임:
     * 읽기 전용 탐색·조사: `invoke_subagent(TypeName='research', Model='flash')` 전용 (`flash_lite`도 허용, research 타입만 허용).
     * 모든 코딩·구현·파일 수정·리팩토링·아키텍처 위임은 `run_command`에서 `~/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec-file <path>`로 수행한다 (`Opus 5.5`, `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`).
     * `invoke_subagent`는 `TypeName='research'`와 `Model='flash'` 또는 `'flash_lite'` 조합만 허용한다. `self`와 사용자 정의 타입, `pro`/`inherit` 및 모델 생략은 4 Argon 쿼터 보호를 위해 하네스가 강제 차단한다 (`decision: block`, 종료 코드 2).
   - Claude 쿼터 적극 활용 (Quota-Surplus Dispatch): 코딩·디버깅·아키텍처·UI 위임은 `--pool auto` 또는 `--pool claude`(`Opus 5.5`)로 수행하고, 습관적으로 `--pool codex`를 하드코딩하지 않는다. Claude 7d 잔여가 리셋 전에 남아 있거나 병렬 워커를 여러 개 투입할 때 `--pool auto`가 Claude 리셋 잉여와 활성 워커 부하를 반영해 `Opus 5.5`와 `gpt-6.1-sol`/`gpt-6-astra`로 분산한다. `--pool codex`는 PC 조작/Computer-Use처럼 명시적 이유가 있을 때만 지정한다. 투입 전 확인: `maestro route --task '<task>' --difficulty <high|medium|low>`.
   - 메인 에이전트 단독 도구 연속 실행 2회 초과 금지 (Hard Solo-Tool Cap, 단 읽기 전용 `view_file`은 최대 4회까지 허용).
   - Hierarchical Same-Family Sub-Orchestration (`MAESTRO_WORKER_DEPTH <= 2` & Same-Pool Lock): Depth 1 서브 워커가 3개 이상 독립 모듈 처리 시 분할 계획 보고 후 동일 모델 패밀리(Claude→Claude, Codex→Codex) 내에서만 Depth 2 리프 워커 분기 기동. effort는 최대 `high`로 제한되며 속도 티어는 `default` 고정. Depth 2 리프 워커는 추가 위임(Depth 3)이 차단되며 직접 완수해야 함.
   - Shell Backtick Crash Guard (`--spec` 백틱 폭발 방지): `maestro worker-start` 호출 시 프롬프트 안에 백틱(`` ` ``), `$`, `()`, 따옴표가 포함될 경우 절대 큰따옴표(`--spec "..."`) 안에 그냥 넣지 않는다(Bash가 백틱 안의 `` `ssh orin` ``이나 `` `func()` ``를 쉘 명령어로 실행해 크래시·무한 행이 발생함). 반드시 작은따옴표 Heredoc (`<<'EOF' ... EOF`) 또는 `--spec '...'` / `--spec-file`을 사용한다!
   - Schedule Polling Hard Block: `schedule()`을 subagent/task 상태 확인이나 대기에 사용하지 않는다. `DurationSeconds`가 120 미만이거나 `TimerCondition`이 빈 값/`never`가 아니거나, `Prompt`에 `wait for`, `subagent`, `check on`, `status`, `서브에이전트`, `대기`, `확인`이 포함되면 하네스가 호출을 차단한다. Antigravity Reactive Wakeup을 사용하려면 도구 호출을 멈추고 즉시 턴을 끝낸다.
2. Task-Domain + Live Benchmark-Driven Routing & Speed Tiers:
   - Task-Fit Benchmark Routing with Low-Quota Guard (`LOW_QUOTA_THRESHOLD = 20.0%`): 20% 미만 진입 전까지 실시간 벤치마크 점수·태스크 적합성 우선 자동 배정 (긴 호흡 코딩: `gpt-6.1-sol`, 심층 추론: `Gemini 4 Argon` & `gpt-6-astra`, 모션/UI: `Opus 5.5`, PC 조작: `gpt-6.1-sol`, 빠른 탐색: `Gemini 3.8 Flash`, 일반 코딩: `gpt-6-astra`, 가벼운 수정/테스트: `gpt-6-luna` · 4 Argon 7d < 50% 시 보호용 1순위 투입).
   - Reset-Aware Quota Surplus & Load Balancing: 벤치마크 순위는 기준점이다. `recommend_model_for_task()`는 Claude 7d ≥ 50%이고 리셋까지 일일 소진 여력(잔여% / 리셋까지 일수)이 Codex보다 크면 `claude-opus-5-5`에 최대 +10점 잉여 보너스를 주고, Codex 실행 중 워커가 Claude보다 많으면(Claude 잔여 ≥ 30%) 워커 1개 차이당 +3점(최대 +6) 부하 분산 보너스를 준다(반대 방향도 동일). 대상은 `general_coding`·`long_horizon_coding`·`research_orchestration`·`frontend_ui_motion`이며 low 난이도·`fast_explore`·`computer_use`는 제외한다. Claude 쿼터가 리셋 전에 남아 있으면 코딩·디버깅도 `Opus 5.5`로 배정된다.
   - `Opus 5.5 Fast (claude-fast · 2x quota)`: 명시적 fast 요청 전용.
   - `Opus 5.5 (xhigh/high · claude / agy-claude)`: 아키텍처 및 심층 추론, 프론트엔드 UI/UX 및 모션그래픽.
   - `Opus 5.5 Ultra (ultra · 3x quota)`: 최고난도 추론 전용 (`--effort ultra`).
   - `gpt-6.1-sol (high/medium · standard)`: 백엔드/테스트 루프 (`priority`는 `--interactive`/`--urgent` 필요).
   - `gpt-6-astra (medium · default)`: 전반적인 일반 코딩 및 모션그래픽 구현 (`--pool auto --difficulty medium` 자동 선택).
   - `gpt-6-luna (low/medium · default)`: 초경량 단위 수정, 빠른 테스트 실행 (Argon 7d < 50% 시 최우선 배정).
   - `Gemini 3.8 Flash (low · fast)`: 초고속 코드/로그 탐색 (코딩 금지).
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
