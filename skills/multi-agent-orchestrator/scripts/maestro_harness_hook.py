#!/usr/bin/env python3
"""
ai-maestro Runtime Orchestration Harness Hook (maestro_harness_hook.py)
Antigravity payloads (`conversationId` / `toolCall`) get `{"decision":"allow"}` JSON. Claude Code / Orca
payloads get empty stdout on allow (exit 0), so no hook schema errors and no context tokens. Main sessions
in both environments track solo/read streaks; Claude Code gets a one-line reminder on the next prompt only
after a solo-heavy turn. Sub-workers (MAESTRO_WORKER_ACTIVE=1) skip tracking but keep the depth and
spec-substitution guards.
"""
from __future__ import annotations
import json, os, re, sys, time

STATE_DIR = "/tmp/maestro_harness_state"
SOLO_READ_TOOLS = {"view_file", "Read", "grep_search", "find_by_name", "Grep", "Glob"}
SOLO_WRITE_TOOLS = {"replace_file_content", "multi_replace_file_content", "write_to_file", "Edit", "MultiEdit", "Write"}
DELEGATION_TOOLS = {"invoke_subagent", "send_message", "Task", "Agent"}
CLAUDE_WRITE_TOOLS = {"Edit", "MultiEdit", "Write"}
SOLO_REMINDER = "[🎼 MAESTRO] 단독 작업이 연속되었습니다. 병렬 탐색은 Subagent(Agent/Task), 무거운 구현·검증은 maestro worker-start 위임을 권장합니다."

def _load_session_state(session_id: str) -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    safe_id = "".join(c for c in (session_id or "default") if c.isalnum() or c in ("-", "_"))[:64] or "default"
    path = os.path.join(STATE_DIR, f"{safe_id}.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"solo_streak": 0, "read_streak": 0, "delegated_in_turn": False, "updated_at": time.time()}

def _save_session_state(session_id: str, st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    safe_id = "".join(c for c in (session_id or "default") if c.isalnum() or c in ("-", "_"))[:64] or "default"
    path = os.path.join(STATE_DIR, f"{safe_id}.json")
    st["updated_at"] = time.time()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass

def _is_agy(payload: dict) -> bool:
    return "conversationId" in payload or "toolCall" in payload


def _allow(payload: dict) -> None:
    # Claude Code: empty stdout on exit 0 = pass-through with 0 context tokens and 0 schema errors.
    # Antigravity: expects {"decision":"allow"}.
    if _is_agy(payload):
        print('{"decision":"allow"}')


WORKER_START_MARKERS = ("maestro worker-start", "agy-orca worker-start", "maestro.py worker-start", "agy_orca.py worker-start")
QUOTED_HEREDOC = re.compile(r"<<-?\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\(\w+))[^\n]*\n.*?^\t*(?:\1|\2|\3)[ \t]*$", re.S | re.M)
SINGLE_QUOTED = re.compile(r"'[^']*'")


def _is_worker_start(cmd_str: str) -> bool:
    return any(m in cmd_str for m in WORKER_START_MARKERS)


def _has_unescaped_substitution(cmd_str: str) -> bool:
    """True when backticks or $( survive outside quoted heredocs, single quotes and backslash escapes."""
    stripped = SINGLE_QUOTED.sub("", QUOTED_HEREDOC.sub("", cmd_str)).replace("\\`", "").replace("\\$", "")
    return "`" in stripped or "$(" in stripped


def _is_delegation_command(cmd_str: str) -> bool:
    if not cmd_str:
        return False
    return any(m in cmd_str for m in WORKER_START_MARKERS + ("codex exec", "claude -p"))

FLASH_MARKERS = ("MODEL_TIER_FLASH", "MODEL_PLACEHOLDER_M322")


def _flash_session(payload: dict, session_id: str) -> bool:
    if any(marker in key or marker in value
           for key, value in os.environ.items() for marker in FLASH_MARKERS):
        return True
    path = payload.get("transcript_path") or payload.get("transcriptPath")
    if not path and session_id != "default" and os.path.basename(session_id) == session_id:
        path = os.path.expanduser(
            f"~/.gemini/antigravity/brain/{session_id}/.system_generated/logs/transcript.jsonl")
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as transcript:
                return any(any(marker in line for marker in FLASH_MARKERS) for line in transcript)
        except OSError:
            pass
    return False


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    print(reason, file=sys.stderr, flush=True)
    sys.exit(2)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    event = (sys.argv[1] if len(sys.argv) > 1 else "") or payload.get("hook_event_name") or payload.get("event") or "PreInvocation"

    session_id = str(payload.get("conversationId") or payload.get("conversation_id") or payload.get("session_id") or "default")
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tool_call.get("name") or payload.get("tool_name") or payload.get("tool") or "")
    tool_input = tool_call.get("arguments") or payload.get("tool_input") or payload.get("input") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            tool_input = {"raw": tool_input}
    if not isinstance(tool_input, dict):
        tool_input = {}
    is_agy = _is_agy(payload)
    if event in ("PreToolUse", "BeforeTool"):
        if tool_name == "schedule":
            dur = tool_input.get("DurationSeconds")
            cond = str(tool_input.get("TimerCondition") or "").strip().lower()
            prompt_txt = str(tool_input.get("Prompt") or "").strip().lower()
            if dur is not None and (
                int(dur) < 120
                or cond not in ("", "never")
                or any(k in prompt_txt for k in ("wait for", "subagent", "check on", "status", "서브에이전트", "대기", "확인"))
            ):
                _block("BLOCKED BY MAESTRO HARNESS: Never use schedule() timers to poll or wait for subagents/tasks! Antigravity has native Reactive Wakeup — stop calling tools and end your turn immediately; the system will automatically wake you when the subagent or background task finishes.")
        if tool_name == "invoke_subagent":
            type_reason = "BLOCKED BY MAESTRO HARNESS: invoke_subagent(TypeName='self') is strictly forbidden to protect 4 Argon quota! You MUST delegate all coding, implementation, and architecture tasks via run_command -> ~/.local/bin/maestro worker-start --pool auto (Opus 5.5 / gpt-6-astra / gpt-6.1-sol / gpt-6-luna)."
            entries = tool_input.get("Subagents") or tool_input.get("subagents") or []
            if isinstance(entries, str):
                try:
                    entries = json.loads(entries)
                except ValueError:
                    _block(type_reason)
            if not isinstance(entries, list):
                _block(type_reason)
            for entry in entries:
                if not isinstance(entry, dict) or str(entry.get("TypeName") or "self").strip().lower() != "research":
                    _block(type_reason)
                if str(entry.get("Model") or "inherit").strip().lower() not in ("flash", "flash_lite"):
                    _block("BLOCKED BY MAESTRO HARNESS: Never spawn 4 Argon (Model='inherit' or 'pro') via invoke_subagent! Use maestro worker-start --pool auto (Opus 5.5 / gpt-6-astra / gpt-6.1-sol / gpt-6-luna) for work, or Model='flash' with TypeName='research' for read-only lookups.")
        # Claude-native Edit/Write outside Antigravity skip the transcript scan (Opus sessions false-positive on it).
        if tool_name in SOLO_WRITE_TOOLS and (is_agy or tool_name not in CLAUDE_WRITE_TOOLS) \
                and _flash_session(payload, session_id):
            _block("BLOCKED BY MAESTRO HARNESS: Gemini Flash is strictly READ-ONLY and forbidden from modifying any file!")
        cmd_line = str(tool_input.get("CommandLine") or tool_input.get("command") or tool_input.get("cmd") or "")
        if _is_worker_start(cmd_line):
            try:
                depth = int(os.environ.get("MAESTRO_WORKER_DEPTH", "0") or "0")
            except ValueError:
                depth = 0
            if depth >= 2:
                _block("BLOCKED BY MAESTRO HARNESS: Max worker orchestration depth (2) reached. Leaf workers (MAESTRO_WORKER_DEPTH>=2) must execute subtasks directly without spawning Depth 3 workers.")
            if not is_agy and _has_unescaped_substitution(cmd_line):
                _block("BLOCKED BY MAESTRO HARNESS: maestro worker-start command contains unescaped backticks or $(...), which the shell will execute as command substitution. Pass the spec via a single-quoted heredoc (<<'EOF' ... EOF), --spec '...', or --spec-file.")

    # Sub-workers and Claude Code subagent tool calls (agent_id) are not main-session solo work.
    if os.environ.get("MAESTRO_WORKER_ACTIVE") == "1" or payload.get("agent_id"):
        _allow(payload)
        return 0

    st = _load_session_state(session_id)

    if event in ("PreInvocation", "UserPromptSubmit", "BeforeAgent", "SessionStart"):
        prev_streak = int(st.get("solo_streak", 0))
        prev_read_streak = int(st.get("read_streak", 0))
        st["solo_streak"] = 0
        st["read_streak"] = 0
        st["delegated_in_turn"] = False
        _save_session_state(session_id, st)
        if is_agy:
            _allow(payload)
        elif prev_streak >= 2 or prev_read_streak >= 4:
            # Claude Code injects UserPromptSubmit stdout as context: one line, only after a solo-heavy turn.
            print(SOLO_REMINDER)
        return 0

    if event in ("PreToolUse", "BeforeTool"):
        if tool_name in DELEGATION_TOOLS or _is_delegation_command(cmd_line):
            st["solo_streak"] = 0
            st["read_streak"] = 0
            st["delegated_in_turn"] = True
            _save_session_state(session_id, st)
            if is_agy and "worker-start" in cmd_line and ("`" in cmd_line or "$(" in cmd_line):
                sys.stderr.write("[⚠️ MAESTRO HARNESS] --spec에 백틱(`) 또는 $(...)이 포함되어 쉘 명령어 치환으로 프로세스가 행(hang)되거나 충돌할 수 있습니다. 작은따옴표 Heredoc(<<'EOF' ... EOF) 또는 --spec-file을 사용하세요.\n")
                sys.stderr.flush()
        elif tool_name in SOLO_READ_TOOLS:
            st["read_streak"] = int(st.get("read_streak", 0)) + 1
            _save_session_state(session_id, st)
        elif tool_name in SOLO_WRITE_TOOLS or (tool_name in ("run_command", "Bash") and not _is_delegation_command(cmd_line)):
            st["solo_streak"] = int(st.get("solo_streak", 0)) + 1
            _save_session_state(session_id, st)
        _allow(payload)
        return 0

    _allow(payload)
    return 0

if __name__ == "__main__":
    sys.exit(main())
