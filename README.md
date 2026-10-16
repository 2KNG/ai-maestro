# 🐋 agy-orca — Multi-Agent Quota-Aware Orchestrator & AOH Live Telemetry Suite

[![License: MIT](https://img.shields.io/badge/License-MIT-10b981.svg)](LICENSE)
[![Pools](https://img.shields.io/badge/Pools-Antigravity%204.0%20%7C%20Codex%206.1%20%7C%20Claude%20Opus%204.6-3b82f6.svg)](#architecture)
[![Zero Token Telemetry](https://img.shields.io/badge/Telemetry-0%20LLM%20Tokens-f59e0b.svg)](#aoh-live-widget)

**`agy-orca`**는 **Google Antigravity (`Gemini 4 Argon / 3.8 Flash / Claude Opus 5.5`)**, **OpenAI Codex ProMax CLI (`gpt-6.1-sol`)**, **Anthropic Claude Code CLI (`claude-opus-4-6 / sonnet-4-6`)** 3대 에이전트 풀의 **5시간 단기(5h) · 주간(7d) 쿼터를 실시간 추적**하고, 잔량과 작업 난이도에 맞춰 **자동 분산 라우팅 · 0% 선제 방어 · Git Worktree 병렬 실행 · 실시간 AOH 트리 위젯 시각화**를 수행하는 통합 멀티에이전트 오케스트레이터입니다.

---

## ⚡ 1초 한방 설치 (One-Shot Installation)

### 방법 A. 다른 PC의 AI 에이전트(Antigravity / Claude Code / Codex)에게 말로 시키기
다른 PC에서 에이전트 채팅창에 아래 한 줄만 입력하면 에이전트가 알아서 클론·심볼릭 링크·데몬 등록·글로벌 룰 세팅까지 한 번에 끝냅니다:

> **"야 https://github.com/2KNG/agy-orca 설치해줘"**

### 방법 B. 터미널 한 줄 설치 (`curl | bash`)
```bash
curl -fsSL https://raw.githubusercontent.com/2KNG/agy-orca/main/install.sh | bash
```

### 방법 C. Git 클론 후 지속 관리 모드 설치 (`~/dev/agy-orca` 심볼릭 링크 연동)
```bash
git clone https://github.com/2KNG/agy-orca.git ~/dev/agy-orca
chmod +x ~/dev/agy-orca/install.sh
~/dev/agy-orca/install.sh
```
> **💡 지속 관리 아키텍처**: `install.sh`는 복사본을 방치하지 않고 `~/.gemini/config/skills/multi-agent-orchestrator`, `~/.gemini/config/plugins/codex-claude-bridge`, `~/.local/bin/{agy-orca,ai-usage,ai-usage-widget,ai-usage-web}`를 모두 `~/dev/agy-orca` 레포지토리 내부로 **심볼릭 링크(Symlink)** 연결합니다. 따라서 기능 수정 후 `~/dev/agy-orca`에서 바로 `git commit && git push`로 지속 관리할 수 있습니다.

---

## 🌟 핵심 기능 (Key Features)

### 1. 🛡️ 3단계 주간 쿼터 보호 & 0% 자동 조향 (`3-Stage Weekly Protection & 0% Failover`)
단순히 5시간 단기 쿼터(`5h`)만 보고 작업을 던지다가 주간 한도(`7d`)가 먼저 바닥나는 문제를 막기 위해 **유효 가용치(`eff_rem`)** 공식으로 풀을 자동 선택합니다:

| 단계 | 주간 잔량 (`7d`) 조건 | 유효 가용치 (`eff_rem`) 계산식 | 동작 방식 |
| :--- | :--- | :--- | :--- |
| **1단계 (평시)** | `7d > 10%` | `5h` 단기 잔량 우선 + 주간 보정 | 가장 여유로운 풀로 최고 속도 배정 |
| **2단계 (주간 보호)** | `2% < 7d <= 10%` | `min(5h, 7d * 2.5)` | 주간 한도 고갈 방지를 위해 타 풀로 부하 자동 분산 |
| **3단계 (비상 보존)** | `0% < 7d <= 2%` | `7d * 0.3` | 해당 풀을 후순위로 강등하고 여유 풀로 우회 |
| **Hard Block & Auto-Steering** | `5h == 0%` 또는 `7d == 0%` | **즉시 차단 (`-1.0`)** | **투입 전(Pre-flight)** 및 **실행 중(Mid-flight)** 0% 감지 시 작업 중단 없이 대체 풀로 자동 인계 |

---

### 2. 📊 AOH 듀얼 링 & 서브에이전트 숙성(Ripening) 실시간 위젯 (`0 LLM Token Telemetry`)
LLM 토큰을 단 1토큰도 소모하지 않고 로컬 프로세스·SQLite·JSONL 로그만 파싱하여 **우측 아티팩트 패널** 및 **`http://127.0.0.1:18765`** 대시보드에서 4초 주기로 라이브 업데이트합니다:

- **듀얼 동심원 게이지**: 바깥 링(`5h` 단기 잔량) + 안쪽 링(`7d` 주간 잔량) + 시계형 리셋 파이 차트.
- **무깜빡임(Zero-Flicker) 스마트 DOM 패칭**: 시그니처 비교(`lastQuotasSig` / `normalizeTreeSig`)를 통해 실제로 변경된 노드만 인플레이스 패치하여 화면 번쩍임을 원천 차단.
- **1분 4단계 숙성(Ripening) & 자동 증발**: 완료된 서브에이전트(`SUB`)·데몬(`DMN`)·작업(`JOB`)이 **15초 간격으로 `초록(0~15s) ➔ 노랑(15~30s) ➔ 주황(30~45s) ➔ 빨강+페이드(45~60s)`** 순으로 익어가며 60초 후 활성 트리에서 깔끔하게 증발.
- **부모 폴더 슬롯(`repo-tag`)과 정렬된 서브 뱃지(`sub-tag`)**: `├ SUB`, `└ DMN` 뱃지가 상위 워크스페이스 폴더 슬롯(`84px × 18px`)과 1px 오차 없이 수직 정렬.
- **채팅 가독성 보호 (Zero Chat Spam)**: 매 턴 채팅창에 거대한 인라인 HTML을 도배하지 않고 백그라운드에서만 아티팩트를 조용히 갱신.

---

### 3. ⏱ 직관적 실제 소요시간 · 한글 토큰 규모 · 원화(₩) · 구독 소모율(%) 안내 표준
인간 기준 달력 시간("며칠/1주일")이나 영문 약어(`12k/1.5M`) 대신, **AI 병렬 실측 시간 + 한글 토큰 규모 + API 원화 환산 체감가 + 현재 구독 쿼터 소모율(%)**을 한눈에 보고합니다:

- **단순 수정 / 단일 파일**: `⏱ 30초~1분` · **`8천~1만5천 토큰 규모 (5백원대 · 구독 5h 0.3%)`** · 단일 워커
- **중간 기능 / 멀티 파일**: `⏱ 1~3분` · **`10만~15만 토큰 규모 (5천원대 · 구독 5h 2~4% / 7d 0.5%)`** · 2~3개 서브에이전트 병렬
- **대규모 리팩토링 / 풀스택**: `⏱ 3~6분` · **`100만~150만 토큰 규모 (5만원대 · 구독 5h 20~30% / 7d 3~5%)`** · Git Worktree 멀티풀 풀가동
- **초대형 아키텍처 / 전체 감사**: `⏱ 7~12분` · **`1천만 토큰 규모 (53만원대 · 구독 7d 15~25% 분산)`** · 전 풀 병렬 루프

---

### 4. 🌿 Git Worktree 격리 병렬 실행 & SQLite/FTS5 세션 핸드오버 메모리
- **Git Worktree 병렬 실행 (`--worktree`)**: 여러 에이전트가 동시에 코딩할 때 파일 충돌이 나지 않도록 `.orca-worktrees/` 아래에 독립 브랜치를 생성해 작업 후 검증·병합합니다.
- **로컬 핸드오버 메모리 (`orca_memory.py`)**: `~/.gemini/antigravity/orca_memory.sqlite` (FTS5 전문 검색 지원)에 에이전트 간 인계 노트(수정한 파일, 요약, 다음 단계)를 자동 기록하여 `Codex ➔ Claude ➔ Antigravity` 간 무손실 컨텍스트 인계를 보장합니다.
- **자동 품질 게이트 (`--verify`)**: 워커 종료 직후 `py_compile`, `pytest`, `ruff`, `tsc --noEmit` 등을 자동 실행해 통과 여부를 즉시 검증합니다.

---

## 🛠️ CLI 명령어 레퍼런스 (`~/.local/bin/`)

```bash
# 1. 전 풀(Antigravity / Codex / Claude) 실시간 쿼터 & 추천 라우팅 JSON 조회
ai-usage

# 2. 쿼터 기반 자동 라우팅 워커 실행 (난이도: high | medium | low)
agy-orca worker-start --pool auto --difficulty high --cwd "$PWD" --wait --spec "요구사항 설명"

# 3. Git Worktree 격리 브랜치에서 안전하게 코딩 워커 실행 + 자동 검증 게이트
agy-orca worker-start --pool auto --difficulty high --worktree --verify "pytest -q" --cwd "$PWD" --wait --spec "기능 구현"

# 4. 특정 풀 지정 실행 (codex | agy-argon | agy-claude | agy-flash | claude)
agy-orca worker-start --pool codex --tier priority --cwd "$PWD" --wait --spec "백엔드 로직 리팩토링"

# 5. AOH 라이브 위젯 HTML 수동 생성 / 갱신
ai-usage-widget /tmp/usage_widget.html

# 6. 브라우저에서 독립 실시간 대시보드(http://127.0.0.1:18765) 열기
ai-usage-web
```

---

## 📂 디렉토리 구조 (Repository Layout)

```text
agy-orca/
├── install.sh                                 # 원클릭 설치 & 심볼릭 링크/systemd 자동 구성 스크립트
├── LICENSE                                    # MIT 라이선스 및 서드파티 고지사항
├── README.md                                  # 프로젝트 문서
├── bin/                                       # ~/.local/bin 심볼릭 링크용 실행 래퍼
│   ├── agy-orca
│   ├── ai-usage
│   ├── ai-usage-widget
│   └── ai-usage-web
├── rules/                                     # 전 에이전트 공통 글로벌 룰 템플릿
│   ├── GEMINI.md                              # Antigravity 글로벌 룰 (~/.gemini/GEMINI.md)
│   ├── AGENTS.workspace.md                    # 워크스페이스 룰 (~/dev/AGENTS.md)
│   ├── AGENTS.codex.md                        # Codex 글로벌 룰 (~/.codex/AGENTS.md)
│   └── CLAUDE.md                              # Claude Code 글로벌 룰 (~/.claude/CLAUDE.md)
├── skills/
│   └── multi-agent-orchestrator/
│       ├── SKILL.md                           # 멀티에이전트 오케스트레이터 스킬 정의서
│       └── scripts/
│           ├── check_all_usage.py             # 3대 풀 쿼터·트리·라우팅 수집 엔진
│           ├── orca.py                        # 워커 스폰·0% 자동 조향·Worktree·품질 게이트 CLI
│           ├── orca_memory.py                 # SQLite + FTS5 크로스 에이전트 핸드오버 메모리
│           ├── render_usage_widget.py         # AOH 위젯 SSR 렌더러 & Gzip 컴팩트 패커
│           ├── usage_widget_template.html     # AOH 듀얼 링 & 숙성 애니메이션 위젯 템플릿
│           └── widget_bridge_daemon.py        # 18765 포트 실시간 브리지 & 세션 복원 데몬
├── plugins/
│   └── codex-claude-bridge/                   # /codex, /claude 슬래시 스킬 브리지 플러그인
└── systemd/
    └── agy-widget-bridge.service              # 유저 레벨 systemd 상주 데몬 유닛
```

---

## 📜 라이선스 및 보안 고지 (License & Security)

- **라이선스**: 본 저장소의 모든 코드는 **[MIT License](LICENSE)** 하에 배포되어 누구나 자유롭게 사용·수정·배포할 수 있습니다.
- **무결성 & 무인증정보(Zero-Credential) 원칙**: 본 레포지토리에는 어떠한 API 키, OAuth 토큰, 개인 세션 로그도 포함되지 않습니다. 모든 쿼터 조회 및 에이전트 실행은 사용자의 로컬 머신에 이미 로그인된 공식 CLI(`agy`, `codex`, `claude`) 세션을 통해서만 100% 로컬로 동작합니다.
