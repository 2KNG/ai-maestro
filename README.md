# ai-maestro — Multi-Agent Quota-Aware Orchestrator & AOH Live Telemetry Suite

[![License: MIT](https://img.shields.io/badge/License-MIT-10b981.svg)](LICENSE)
[![Pools](https://img.shields.io/badge/Pools-Antigravity%204.0%20%7C%20Codex%206.1%20%7C%20Claude%20Opus%205.5-3b82f6.svg)](#architecture)
[![Zero Token Telemetry](https://img.shields.io/badge/Telemetry-0%20LLM%20Tokens-f59e0b.svg)](#aoh-live-widget)

`ai-maestro` (CLI: `maestro`)는 Google Antigravity(Gemini 4 Argon / 3.8 Flash / Claude Opus 5.5), OpenAI Codex ProMax CLI(gpt-6.1-sol), Anthropic Claude Code CLI(Opus 5.5 / Sonnet 5.5) 풀의 5시간 단기(5h) 및 주간(7d) 쿼터를 실시간 추적하고, 잔량과 작업 난이도에 맞춰 분산 라우팅, 0% 선제 방어, Git Worktree 병렬 실행, AOH 트리 위젯 시각화를 수행하는 멀티에이전트 오케스트레이터입니다.

## 설치

### 방법 A. AI 에이전트(Antigravity / Claude Code / Codex)에게 지시
채팅창에 아래 명령을 전달하면 클론, 심볼릭 링크, 데몬 등록, 글로벌 룰 설정 후 온보딩(CLI → 선택적 Orca → 선택적 원격 허브 연결)까지 대신 진행합니다.

> "https://github.com/2KNG/ai-maestro 설치해줘. 설치할 때 서버 주소나 IP 물어보고 매칭해줘"

### 방법 B. 터미널 스크립트 실행
```bash
curl -fsSL https://raw.githubusercontent.com/2KNG/ai-maestro/main/install.sh | bash
```

Windows(PowerShell):
```powershell
git clone https://github.com/2KNG/ai-maestro.git $HOME\dev\ai-maestro
powershell -ExecutionPolicy Bypass -File $HOME\dev\ai-maestro\install.ps1
```

### 방법 C. Git 클론 후 심볼릭 링크 연동
```bash
git clone https://github.com/2KNG/ai-maestro.git ~/dev/ai-maestro
chmod +x ~/dev/ai-maestro/install.sh
~/dev/ai-maestro/install.sh
```

`install.sh`는 `~/.gemini/config/skills/multi-agent-orchestrator`, `~/.gemini/config/plugins/codex-claude-bridge`, `~/.local/bin/{maestro,ai-usage,ai-usage-widget,ai-usage-web}`를 `~/dev/ai-maestro` 저장소 내부로 심볼릭 링크 연결한 뒤 `maestro setup`을 실행합니다. 기능 수정 후 `~/dev/ai-maestro`에서 커밋 및 푸시하여 지속 관리할 수 있습니다.

### 3단계 온보딩 (`maestro setup`)

`install.sh`, `install.ps1`의 마지막 단계이며 언제든 `maestro setup`으로 다시 실행할 수 있습니다.

| 단계 | 질문 | 동작 |
| :--- | :--- | :--- |
| 1. 기본 CLI | "오르카(Orca) 및 AI Maestro 구동에 필요한 기본 CLI [Antigravity / Codex / Claude] (기본 전체 선택)를 먼저 확인하고 설치할까요?" | `[x] Antigravity` `[x] OpenAI Codex CLI` `[x] Claude Code CLI`(기본 전체 선택). 설치된 `agy`·`codex`·`claude`를 확인하고 없는 것만 설치합니다. 기존 `~/.codex/config.toml`, `~/.claude/settings.json`은 설치 전 스냅샷을 떠 두고 변경되면 바이트 그대로 복원합니다. |
| 2. Orca | "이어서 오르카(Orca)를 설치하고 AI Maestro와 연동할까요?" | `orca`/`orca-ide`를 찾고(없으면 [Orca 릴리스](https://github.com/stablyai/orca/releases/latest) 안내), Linux에서는 화면 읽기 프로그램 `/usr/bin/orca`와 겹치지 않는 `~/.local/bin/orca` shim을 만들고, 기존 `orca-remote.service`를 연결합니다. macOS LaunchAgent와 Windows 예약 작업은 3단계 `maestro remote up`이 등록합니다. |
| 3. 선택적 원격 허브 | "[선택] 원격 관제 허브 서버 주소(도메인)나 IP를 입력해 주세요" | 주소가 없으면 Enter로 건너뛰고 로컬 오케스트레이터 모드로 완료합니다. Antigravity + Codex + Claude 오케스트레이션과 로컬 위젯(`127.0.0.1:18765`)을 사용하며, Orca CLI가 설치된 경우 로컬 Orca 서비스를 별도로 시작해 `127.0.0.1:6768`에서 사용할 수 있습니다. 주소를 제공하면 `~/.config/ai-maestro/remote.json`에 저장하고 `maestro remote up --hub <주소>`로 원격 fleet hub를 추가 연결합니다. |

비대화형 실행 예:

```bash
maestro setup --yes --hub hub.example.com --identity-file ~/.ssh/hub_key   # 전체 자동
maestro setup --check-only                                                  # 확인만, 변경 없음
maestro setup --cli codex,claude --skip-orca --no-remote-up                 # 일부만
./install.sh --yes --hub <your-server-domain-or-ip>                         # 설치 + 온보딩 인자 전달
```

<AI_SETUP_ONBOARDING>

### AI 에이전트용 설치 안내

`maestro setup`은 로컬 Multi-Agent Orchestrator로 동작하며 Step 3은 선택 사항입니다. 사용자가 서버 주소/IP를 제공하지 않거나 "서버 안 붙이고 로컬에서만 쓸게"라고 하면 원격 연결을 시도하지 말고 로컬 모드로 완료합니다. 이후 주소를 제공하면 원격 fleet hub를 추가 연결할 수 있습니다. 설치 중 "설치할 때 서버 주소나 IP 물어보고 매칭해줘"라고 하면 아래 순서를 따릅니다.

1. 기본 CLI: "오르카(Orca) 및 AI Maestro 구동에 필요한 기본 CLI [Antigravity / Codex / Claude] (기본 전체 선택)를 먼저 확인하고 설치할까요?"라고 묻습니다. `[x] Antigravity` `[x] OpenAI Codex CLI` `[x] Claude Code CLI`가 기본 선택입니다. 동의하면 `maestro setup --yes --skip-orca --skip-remote` 또는 `--cli <선택>`을 실행합니다. 기존 `~/.codex/config.toml`과 `~/.claude/settings.json`은 직접 수정하지 않습니다.
2. Orca: "이어서 오르카(Orca)를 설치하고 AI Maestro와 연동할까요?"라고 묻고 `maestro setup --yes --skip-cli --skip-remote`를 실행합니다. Orca가 없으면 릴리스 페이지를 안내하고 설치 후 같은 명령을 다시 실행합니다. `maestro orca-status`로 연동을 확인합니다.
3. 선택적 서버 주소: "[선택] 원격 관제 허브 서버 주소(도메인)나 IP를 입력해 주세요. Enter를 누르면 로컬 오케스트레이터 모드로 완료합니다."라고 묻습니다. Enter/건너뛰기 또는 로컬 전용 요청이면 성공적으로 마무리하고 터널 서비스를 만들지 않습니다. 사용자가 지금 또는 나중에 "내 서버 주소는 xxx야, 매칭해줘"라고 답하면 다음을 실행합니다.
   ```bash
   maestro remote up --hub <address>
   ```
   IP만 받은 경우 wss 페어링용 HTTPS 도메인을 한 번 더 묻고 `--registry-url https://<domain>`을 붙입니다. SSH 키가 따로 있으면 `--identity-file <path>`를 붙입니다. 결과 JSON의 `blockers`가 있으면 그대로 사용자에게 전달하고, 성공하면 `fleet_url`과 `maestro remote status`를 보고합니다.

주소, IP, SSH 키 경로는 `~/.config/ai-maestro/remote.json`에만 저장합니다. 저장소 파일이나 커밋에 넣지 않습니다.

</AI_SETUP_ONBOARDING>

## Codex 설정 보호와 복구

Python 3.11 이상을 권장합니다. Python 3.10에서는 `tomli`가 필요합니다. 설치와 Codex 워커 시작 시 `CODEX_HOME/config.toml` 또는 `~/.codex/config.toml`을 검증하고 복구용 체크포인트를 만듭니다. 정상 설정은 바이트 그대로 유지합니다. 모델, 공급자, MCP 서버, 프로젝트, 프로필과 주석을 기본값으로 재작성하지 않습니다.

설정을 수정하는 helper는 누락 키만 추가하며 원본 인코딩과 CRLF를 보존합니다. 변경 전 타임스탬프 백업을 만들고 기존 `config.toml.bak`도 보존합니다. UTF-8, UTF-8 BOM, CP949를 읽을 수 있습니다. 빈 파일·잘린 TOML·삭제된 파일은 유효 백업이 있으면 복구하며, 손상 원본은 `.damaged.*`로 보관합니다. 백업 없는 손상 파일은 그대로 두고 오류로 중단합니다.

```bash
maestro doctor
maestro doctor --fix-codex-config
```

`doctor` 기본 동작은 읽기 전용입니다. 문법상 정상인 축소 설정은 사용자의 편집일 수 있으므로 자동으로 되돌리지 않습니다. 외부 프로그램의 지속적인 쓰기를 차단하는 기능은 아닙니다. [설정 쓰기 감사](docs/codex-config-audit.md)에 설치된 Orca의 시스템·관리 홈 동기화, 훅 설정, WSL 직접 쓰기 경로와 확인 범위를 기록했습니다. Codex의 사용자 설정 위치와 설정 계층은 [공식 설정 문서](https://learn.chatgpt.com/docs/config-file/config-basic)를 기준으로 합니다.

## Orca 기반 운영과 원격 허브(fleet) 연결

### Orca 워크트리·터미널 연동

`maestro`는 Orca 터미널 안팎에서 그대로 동작하며 Orca CLI(`orca terminal ...`, `orca worktree ...`)를 감쌉니다.

```bash
maestro orca-status                                # Orca 런타임 요약 + 현재 터미널(ORCA_TERMINAL_HANDLE)
maestro orca-list [--worktree path:<dir>]          # 살아 있는 Orca 터미널 (handle · 제목 · 에이전트 · 워크트리)
maestro orca-worktrees [--repo name:<repo>]        # Orca 관리 워크트리
maestro orca-send --terminal <handle> --text "pytest -q" --enter
maestro orca-read --terminal <handle> [--screen] [--cursor N]
```

`ai-usage` JSON의 `orca` 항목, 위젯 상단 호스트 표시, 브리지 `/api/orca`, `maestro remote status`의 `orca` 항목에 Orca 터미널·워크트리 수가 함께 표시됩니다. Linux에서는 GNOME 화면 읽기 프로그램과 혼동하지 않도록 `orca-ide`(또는 사용자 shim)를 사용합니다. 별도 실행 파일은 `ORCA_CLI_COMMAND`에 전체 경로를 지정합니다.

### 원격 허브 설정 위치

허브 주소, 레지스트리 도메인, SSH 키 경로는 저장소에 없으며 다음 순서로 결정됩니다.

1. 명령행 인자 (`--hub`, `--registry-url`, `--identity-file`, `--node-id`)
2. 환경 변수 `MAESTRO_REMOTE_HUB`, `MAESTRO_SSH_KEY`, `MAESTRO_REGISTRY_URL`, `MAESTRO_NODE_ID`
3. `~/.config/ai-maestro/remote.json` (권한 0600, `MAESTRO_REMOTE_CONFIG`로 경로 변경 가능)

```json
{
  "hub": "ubuntu@hub.example.com",
  "registry_url": "https://hub.example.com",
  "identity_file": "~/.ssh/hub_key",
  "node_id": "office-pc"
}
```

`--hub`에는 도메인(`hub.example.com`), IP(`203.0.113.7`), `user@host`, `https://hub.example.com`을 넣을 수 있습니다. 도메인은 SSH 대상과 레지스트리 주소를 함께 맞추고, IP는 공인 TLS 인증서가 없으므로 `--registry-url https://<도메인>`이 따로 필요합니다. SSH 사용자를 생략하면 `ubuntu`(`--ssh-user`로 변경)를 사용합니다.

```bash
maestro remote configure --hub <your-server-domain-or-ip>          # 저장만 (서비스 변경 없음)
maestro remote status                                              # 서비스·페어링·Orca 터미널 상태
maestro remote up --dry-run                                        # 저장된 허브로 사전 점검
maestro remote up --hub hub.example.com --identity-file ~/.ssh/hub_key
maestro remote install-node --node-id office-pc
maestro remote nodes                                               # 허브 레지스트리에 등록된 노드만 표시
```

`remote-setup`, `orca-remote`, `fleet-setup`, `fleet`도 사용할 수 있습니다. `remote-setup`과 `fleet-setup`에서 동작을 생략하면 `up`을 실행합니다. `install-node`는 현재 체크아웃에서 서비스를 등록하는 크로스 플랫폼 진입점입니다. Python, Orca IDE, OpenSSH와 사용자 서비스 관리자가 먼저 설치되어 있어야 합니다.

Windows에서는 `install.ps1` 실행 후 다음처럼 호출합니다.

```powershell
maestro remote install-node --node-id office-pc --hub hub.example.com --identity-file "$HOME\.ssh\hub_key"
```

### 노드 1~N 동적 확장

노드 수에 고정 상한이나 미리 만들어 둔 빈 슬롯(`pc2..pc10`)이 없습니다. 새 노드는 허브 레지스트리에서 비어 있는 가장 낮은 슬롯을 받고 허브 포트는 Orca `16701+slot`, 위젯 `18701+slot`으로 계산됩니다(포트 대역이 겹치지 않는 범위에서 최대 2000대). `maestro remote nodes`는 실제 등록된 노드만 보여 줍니다. `--port`와 `--widget-port`는 배정된 값과 일치할 때만 허용합니다. 터널은 허브의 loopback에만 바인딩합니다.

Linux는 systemd 사용자 서비스, macOS는 LaunchAgent, Windows는 로그인한 사용자의 Scheduled Task로 Orca, 위젯, SSH 터널과 heartbeat를 유지합니다. Orca는 로컬 6768, 위젯은 18765를 사용합니다. 이미 실행 중인 `agy-widget-bridge.service`와 `orca-remote.service`는 재사용합니다.

허브에는 fleet 기능이 배포된 허브 서버(기본 체크아웃 `/home/ubuntu/dev/rtc2kng`, DB `var/fleet.sqlite3`), 실행 중인 `/api/orca/nodes` 및 `/api/orca/register`, 해당 Registry에 접근 가능한 SSH 계정이 필요합니다. 별도 배포 폴더는 `--hub-project`, DB는 `--hub-db`, Python은 `--hub-python`, 내부 API 주소는 `--hub-probe-url`로 지정하거나 `remote.json`에 같은 이름으로 저장합니다. SSH host key를 먼저 신뢰할 수 있는 경로로 등록해야 하며 자동으로 검증을 끄지 않습니다. 신규 노드 자격은 허브의 실제 `Registry.provision`으로 등록하고 로컬 비공개 파일에 저장합니다. 다른 자격이 소유한 노드 ID나 기존 서비스는 덮어쓰지 않습니다. 접근 제한이 있는 허브는 `MAESTRO_FLEET_ACCESS_TOKEN`, 이미 발급된 노드 자격은 `MAESTRO_REGISTRATION_TOKEN` 환경 변수로 전달할 수 있습니다. SSH 키와 노드 토큰을 명령 예시나 저장소에 포함하지 마세요.

접속 주소는 `https://<허브 도메인>/orca/<node_id>/`와 로컬 LAN 주소입니다. 실제 Orca pairing이 준비되면 heartbeat가 허브에 전달해 자동 연결에 사용합니다. `--show-pairing`은 로컬에 저장된 실제 pairing을 표시하므로 출력 공유에 주의하세요. `--dry-run`, `configure`, `status`, `nodes`는 서비스를 변경하지 않습니다. 서비스 시작 성공과 실제 브라우저·데스크톱 제어 검증은 별도로 표시합니다.

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
- 채팅 ETA 출력 형식: `⏱ 3분 · 13만 토큰 (3천원)`.

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
maestro worker-start --pool codex --interactive --tier priority --user-requested-fast "빠르게 고쳐줘" --cwd "$PWD" --wait --spec "실시간 인터랙티브 수정"
maestro worker-start --pool codex-astra --cwd "$PWD" --wait --spec "병렬 서브모듈 구현"
maestro worker-start --pool codex --model gpt-6-astra --cwd "$PWD" --wait --spec "병렬 서브모듈 구현"
maestro worker-start --pool claude --cwd "$PWD" --wait --spec "코어 아키텍처 설계"
maestro worker-start --pool claude-fast --interactive --user-requested-fast "빨리 해줘" --cwd "$PWD" --wait --spec "반응형 UI 빠른 패치"   # 명시적 fast 요청 시 · 2x 쿼터
# fast/priority/ultrafast/claude-fast는 --user-requested-fast "<사용자 원문>"(또는 MAESTRO_ALLOW_FAST_TIER=1)이 없으면 default로 강등
maestro tier-audit --since-hours 48   # 고속 티어 워커별 인가/강등/미검증 내역 + Codex 롤아웃 service_tier 집계
maestro worker-start --pool claude-ultra --cwd "$PWD" --wait --spec "최고난도 추론"   # = opus-ultra · --effort max + ultrathink · 3x 쿼터
maestro worker-start --pool claude --effort ultra --cwd "$PWD" --wait --spec "최고난도 추론"
maestro worker-start --pool auto --max-capability --domain coding --cwd "$PWD" --wait --spec "고난도 코딩"

# 5. AOH 라이브 위젯 HTML 수동 생성 및 갱신
ai-usage-widget /tmp/usage_widget.html

# 6. 실시간 대시보드(http://127.0.0.1:18765) 열기
ai-usage-web

# 7. 온보딩 · Orca · 원격 허브
maestro setup
maestro orca-list
maestro remote status
```

## 디렉토리 구조

```text
ai-maestro/
├── install.sh                                 # 설치 및 심볼릭 링크/systemd 구성 + maestro setup
├── install.ps1                                # Windows 설치 + maestro setup
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
│           ├── maestro_setup.py               # 3단계 온보딩 (CLI → Orca → 서버 주소)
│           ├── maestro_remote.py              # 원격 노드 서비스·터널·페어링
│           ├── maestro_remote_hub.py          # 허브 Registry 어댑터 (1~N 동적 슬롯)
│           ├── remote_config.py               # ~/.config/ai-maestro/remote.json 로더
│           ├── orca_bridge.py                 # Orca 터미널·워크트리 래퍼
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
- 보안 원칙: 저장소에 API 키, 토큰, 개인 세션 로그, 서버 주소·IP·SSH 키 경로를 포함하지 않습니다. 개인 설정은 `~/.config/ai-maestro/remote.json`에만 둡니다. 모든 작업은 로컬 머신에 설정된 공식 CLI 세션을 통해 로컬에서 실행됩니다.
