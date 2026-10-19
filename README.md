# ai-maestro — Multi-Agent Quota-Aware Orchestrator & AOH Live Telemetry Suite

[![License: MIT](https://img.shields.io/badge/License-MIT-10b981.svg)](LICENSE)
[![Pools](https://img.shields.io/badge/Pools-Antigravity%204.0%20%7C%20Codex%206.1%20%7C%20Claude%20Opus%205.5-3b82f6.svg)](#architecture)
[![Zero Token Telemetry](https://img.shields.io/badge/Telemetry-0%20LLM%20Tokens-f59e0b.svg)](#aoh-live-widget)

`ai-maestro` (CLI: `maestro`)는 Google Antigravity(Gemini 4 Argon / 3.8 Flash / Claude Opus 5.5), OpenAI Codex ProMax CLI(gpt-6.1-sol), Anthropic Claude Code CLI(Opus 5.5 / Sonnet 5.5) 풀의 5시간 단기(5h) 및 주간(7d) 쿼터를 실시간 추적하고, 잔량과 작업 난이도에 맞춰 분산 라우팅, 0% 선제 방어, Git Worktree 병렬 실행, AOH 트리 위젯 시각화를 수행하는 멀티에이전트 오케스트레이터입니다.

## 설치

### 방법 A. AI 에이전트(Antigravity / Claude Code / Codex)에게 지시
채팅창에 아래 명령을 전달하면 클론, 심볼릭 링크, 데몬 등록, 글로벌 룰 설정을 자동으로 진행합니다.

> "https://github.com/2KNG/ai-maestro 설치해줘"

### 방법 B. 터미널 스크립트 실행
```bash
curl -fsSL https://raw.githubusercontent.com/2KNG/ai-maestro/main/install.sh | bash
```

### 방법 C. Git 클론 후 심볼릭 링크 연동
```bash
git clone https://github.com/2KNG/ai-maestro.git ~/dev/ai-maestro
chmod +x ~/dev/ai-maestro/install.sh
~/dev/ai-maestro/install.sh
```

`install.sh`는 `~/.gemini/config/skills/multi-agent-orchestrator`, `~/.gemini/config/plugins/codex-claude-bridge`, `~/.local/bin/{maestro,ai-usage,ai-usage-widget,ai-usage-web}`를 `~/dev/ai-maestro` 저장소 내부로 심볼릭 링크 연결합니다. 기능 수정 후 `~/dev/ai-maestro`에서 커밋 및 푸시하여 지속 관리할 수 있습니다.

## 핵심 기능

### 1. 3단계 주간 쿼터 보호 및 0% 자동 조향
5시간 단기 쿼터(`5h`)만 감시하다 주간 한도(`7d`)가 고갈되는 현상을 방지하기 위해 유효 가용치(`eff_rem`) 공식으로 풀을 자동 선택합니다.

| 단계 | 주간 잔량 (`7d`) 조건 | 유효 가용치 (`eff_rem`) 계산식 | 동작 방식 |
| :--- | :--- | :--- | :--- |
| 1단계 (평시) | `7d > 10%` | `5h` 단기 잔량 우선 + 주간 보정 | 가장 여유로운 풀로 배정 |
| 2단계 (주간 보호) | `2% < 7d <= 10%` | `min(5h, 7d * 2.5)` | 주간 한도 고갈 방지를 위해 타 풀로 부하 분산 |
| 3단계 (비상 보존) | `0% < 7d <= 2%` | `7d * 0.3` | 해당 풀을 후순위로 강등하고 여유 풀로 우회 |
| 차단 및 자동 조향 | `5h == 0%` 또는 `7d == 0%` | 즉시 차단 (`-1.0`) | 사전(Pre-flight) 및 실행 중(Mid-flight) 0% 감지 시 대체 풀로 자동 인계 |

### 2. AOH 실시간 위젯 (0 LLM Token Telemetry)
LLM 토큰 소모 없이 로컬 프로세스, SQLite, JSONL 로그를 파싱하여 아티팩트 패널 및 `http://127.0.0.1:18765` 대시보드에 실시간 반영합니다.

- 듀얼 동심원 게이지: 바깥 링(`5h` 단기 잔량) + 안쪽 링(`7d` 주간 잔량) + 시계형 리셋 파이 차트.
- 스마트 DOM 패칭: 시그니처 비교(`lastQuotasSig` / `normalizeTreeSig`)를 통해 변경된 노드만 인플레이스 패치하여 불필요한 재렌더링 방지.
- 1분 4단계 숙성(Ripening) 및 정리: 완료된 서브에이전트(`SUB`), 데몬(`DMN`), 작업(`JOB`)이 15초 간격으로 초록(0~15s) ➔ 노랑(15~30s) ➔ 주황(30~45s) ➔ 빨강 페이드(45~60s) 단계로 상태를 표시하고 이후 활성 트리에서 정리.
- 부모 폴더 슬롯(`repo-tag`)과 정렬된 서브 뱃지(`sub-tag`): `├ SUB`, `└ DMN` 뱃지가 상위 워크스페이스 슬롯과 일치하게 배치.
- 채팅 가독성 유지: 채팅창에 HTML을 직접 삽입하지 않고 백그라운드 아티팩트 파일로만 갱신.

### 3. 모델 및 노력별 단일 수치 소요시간 기준표

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

- 보정된 계획용 기준값이며 실제 청구액과 다를 수 있습니다. 워커에 지정된 모델, 노력, 티어에 맞는 행을 선택합니다.
- `~` 범위(`1~3분`), "예상", "평균", "약", "대" 등의 표현을 지양하고 단일 기준값을 제시합니다.
- 채팅 ETA 출력 형식: `⏱ 2분 · 35만 토큰 (5천원)`.

#### 워커 배정 고지 및 속도 티어 정책

- 실전 모델 우선순위 위계: **1순위 주력** `gpt-6-astra (medium · default)` > **2순위 주력** `Opus 5.5 (high/medium · claude)` > **3순위 특수 목적** `gpt-6.1-sol (high · standard, computer-use/test loop)`.
- 워커 투입 시 각 작업에 배정된 모델, 노력 수준, 속도 티어를 사전 명시합니다.
- 사후 보고 시 `--pool auto` 배정 결과나 장애 조치(Failover) 내역을 기록합니다.
- 속도 티어 정책:
  - Claude Opus 5.5 Fast(`claude-fast`)는 표준 `claude-opus-5-5` 대비 2배 쿼터를 소모합니다. 사용자가 명시적으로 빠른 응답을 요구할 때만 투입하며, 단순 "오퍼스 시켜" 지시에는 표준 `claude-opus-5-5`를 사용합니다.
  - `fast` / `priority` / `ultrafast` 티어는 라이브 인터랙티브 응답이나 명시적 고속 요청 시에만 사용합니다.
  - cron, watchdog, 장시간 자율 작업, 백그라운드 원격 작업은 표준/기본 티어와 표준 `claude-opus-5-5`를 사용합니다.
  - `maestro worker-start`는 `--interactive` 또는 `--urgent` 옵션이 없을 경우 기본 속도로 동작합니다.
  - Opus 5.5 Ultra(`--pool claude-ultra` 또는 `--effort ultra`)는 표준 Opus 5.5 기반으로 `--effort max` 및 `ultrathink`를 적용하며 3배 쿼터를 소모합니다. 최고난도 추론 및 설계에 선별 적용합니다.

| 구체적 작업 | 모델 · 노력 수준 · 속도 티어 |
|---|---|
| 반응형 UI / 빠른 패치 (명시적 fast 요청 시) | Opus 5.5 Fast (`claude-fast`, 2x quota) |
| Cron / 감시 루프 / 장시간 자율 / 백그라운드 | Opus 5.5 (`claude-opus-5-5`, standard) |
| 모션그래픽 / 프론트엔드 UI 및 인터랙션 | Opus 5.5 (`claude`, standard) / gpt-6-astra (`default`) |
| 핵심 아키텍처 및 심층 추론 | Opus 5.5 (`high`; `xhigh` via `--max-capability`, standard) |
| 최고난도 추론 / 복합 설계 (명시적 선택) | Opus 5.5 Ultra (`claude-ultra` 또는 `--effort ultra` → `--effort max` + `ultrathink`, standard, 3x quota) |
| 전반적인 일반 코딩 및 병렬 모듈 구현 (1순위 주력) | gpt-6-astra (`medium`, `default`; `high`/`ultra` only via `--max-capability`) |
| 백엔드 / 시스템 / 테스트 검증 | gpt-6.1-sol (`high`, `standard`; `priority` interactive/urgent only, `ultrafast` urgent only) |
| PC 조작 / 데스크탑 GUI 제어 / Computer-Use | gpt-6.1-sol (`high`, `standard` · `--pool codex`) |
| 빠른 코드베이스 및 로그 탐색 | Gemini 3.8 Flash (`low`, `fast`) |

### 4. Git Worktree 격리 병렬 실행 및 세션 핸드오버 메모리

- Git Worktree 병렬 실행 (`--worktree`): 동시 작업 시 파일 충돌을 방지하기 위해 독립 Git Worktree 및 브랜치를 생성하여 작업 후 검증·병합합니다.
- 로컬 핸드오버 메모리: SQLite FTS5 전문 검색 기반 인계 노트를 기록하여 Codex, Claude, Antigravity 간 작업 맥락을 전달합니다.
- 자동 검증 게이트 (`--verify`): 작업 종료 후 `py_compile`, `pytest`, `ruff`, `tsc --noEmit` 등을 자동 실행하여 검증합니다.

## CLI 명령어 레퍼런스 (`~/.local/bin/`)

```bash
# 1. 전 풀(Antigravity / Codex / Claude) 실시간 쿼터 및 추천 라우팅 JSON 조회
ai-usage

# 2. 쿼터 기반 자동 라우팅 워커 실행 (난이도: high | medium | low)
maestro worker-start --pool auto --difficulty high --cwd "$PWD" --wait --spec "요구사항 설명"

# 3. Git Worktree 격리 브랜치 실행 및 자동 검증 게이트
maestro worker-start --pool auto --difficulty high --worktree --verify "pytest -q" --cwd "$PWD" --wait --spec "기능 구현"

# 4. 특정 풀 지정 실행
maestro worker-start --pool codex --tier standard --cwd "$PWD" --wait --spec "백엔드 로직 리팩토링"
maestro worker-start --pool codex --interactive --tier priority --cwd "$PWD" --wait --spec "실시간 인터랙티브 수정"
maestro worker-start --pool codex-astra --cwd "$PWD" --wait --spec "병렬 서브모듈 구현"
maestro worker-start --pool codex --model gpt-6-astra --cwd "$PWD" --wait --spec "병렬 서브모듈 구현"
maestro worker-start --pool claude --cwd "$PWD" --wait --spec "코어 아키텍처 설계"
maestro worker-start --pool claude-fast --interactive --cwd "$PWD" --wait --spec "반응형 UI 빠른 패치"   # 명시적 fast 요청 시 · 2x 쿼터
maestro worker-start --pool claude-ultra --cwd "$PWD" --wait --spec "최고난도 추론"   # = opus-ultra · --effort max + ultrathink · 3x 쿼터
maestro worker-start --pool claude --effort ultra --cwd "$PWD" --wait --spec "최고난도 추론"
maestro worker-start --pool auto --max-capability --domain coding --cwd "$PWD" --wait --spec "고난도 코딩"

# 5. AOH 라이브 위젯 HTML 수동 생성 및 갱신
ai-usage-widget /tmp/usage_widget.html

# 6. 실시간 대시보드(http://127.0.0.1:18765) 열기
ai-usage-web
```

## 디렉토리 구조

```text
ai-maestro/
├── install.sh                                 # 설치 및 심볼릭 링크/systemd 구성 스크립트
├── LICENSE                                    # MIT 라이선스
├── README.md                                  # 프로젝트 문서
├── bin/                                       # ~/.local/bin 실행 래퍼
│   ├── maestro                                # 메인 CLI 래퍼
│   ├── agy-orca                               # 호환 래퍼
│   ├── ai-usage
│   ├── ai-usage-widget
│   └── ai-usage-web
├── rules/                                     # 에이전트 공통 규칙 원본
│   ├── GEMINI.md                              # Antigravity 글로벌 룰 (~/.gemini/GEMINI.md)
│   ├── AGENTS.workspace.md                    # 워크스페이스 룰 (~/dev/AGENTS.md)
│   ├── AGENTS.codex.md                        # Codex 글로벌 룰 (~/.codex/AGENTS.md)
│   ├── CLAUDE.md                              # Claude Code 글로벌 룰 (~/.claude/CLAUDE.md)
│   └── hooks.agy.json                         # Antigravity 하네스 훅 설정
├── templates -> rules                         # 호환 심볼릭 링크
├── skills/
│   └── multi-agent-orchestrator/
│       ├── SKILL.md                           # 스킬 정의서
│       └── scripts/
│           ├── maestro.py                     # 워커 스폰·0% 자동 조향·Worktree·품질 게이트 CLI
│           ├── maestro_memory.py              # SQLite/FTS5 세션 핸드오버 메모리
│           ├── benchmark_sync.py              # 12 profiles × 4 domains 벤치마크 매트릭스
│           ├── check_all_usage.py             # 3대 풀 쿼터·트리·라우팅 수집 엔진
│           ├── maestro_harness_hook.py        # 하네스 훅 엔트리포인트
│           ├── pc_telemetry_ext.py            # 로컬 PC 텔레메트리 확장
│           ├── render_usage_widget.py         # AOH 위젯 SSR 렌더러 및 Gzip 패커
│           ├── render_remote_widget.py        # 원격 대시보드 위젯 렌더러
│           ├── usage_widget_template.html     # 위젯 템플릿
│           ├── widget_bridge_daemon.py        # 18765 포트 브리지 데몬
│           └── test_*.py                      # 단위 테스트
├── plugins/
│   └── codex-claude-bridge/                   # /codex, /claude 슬래시 스킬 브리지 플러그인
└── systemd/
    └── agy-widget-bridge.service              # systemd 유저 서비스 유닛
```

## 라이선스 및 보안

- 라이선스: MIT License 하에 배포됩니다.
- 보안 원칙: 저장소에 API 키, 토큰, 개인 세션 로그를 포함하지 않습니다. 모든 작업은 로컬 머신에 설정된 공식 CLI 세션을 통해 로컬에서 실행됩니다.
