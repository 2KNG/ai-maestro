# Codex & Claude ProMax Bridge (`codex-claude-bridge`)

Antigravity 하단 기본 모델 선택 탭(Google Cloud Code 서버 고정 목록)의 한계를 넘어, 이 PC에 로그인된 **OpenAI Codex ProMax (`gpt-6.1-sol`, `ultra`/`high`, `ultrafast`/`priority`)** 및 **Anthropic Claude Code (`fable` / `claude-opus-4-6`)** 를 Antigravity 안에서 직접 선택·실행할 수 있게 해주는 플러그인 & 사이드카 확장입니다.

## 제공 기능
1. **우측 보조 패널 전용 UI (`Codex & Claude Studio` Sidecar)**
   - 채팅창의 `[Codex & Claude Studio](sidecar://codex-claude-studio/)` 알약(Pill) 버튼을 클릭하면 우측 보조 패널에 전용 스튜디오가 열립니다.
   - 엔진(`Codex CLI`, `Codex Review`, `Claude Code CLI`, `AGY Claude Opus 5.5`), 추론 강도(`ultra`/`xhigh`/`high`), 속도 티어(`⚡⚡⚡ Ultrafast`/`⚡ Priority`), 작업 폴더(`CWD`)를 드롭다운으로 직접 골라 즉시 실행하고 실시간 스트리밍 로그를 볼 수 있습니다.
2. **채팅 입력창 슬래시 커맨드 (`/codex`, `/claude`)**
   - `/codex <지시사항>` : 로컬 Codex `gpt-6.1-sol` (`ultra` · `ultrafast`) 직접 실행
   - `/claude <지시사항>` : 로컬 Claude Code (`fable` / `opus-4-6`) 또는 Antigravity Claude Opus 5.5 직접 실행
