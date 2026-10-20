#!/usr/bin/env python3
"""
ai-maestro Runtime Orchestration Harness Hook (maestro_harness_hook.py)
Outputs valid JSON `{}` for Antigravity protojson compatibility while tracking solo tool streaks.
"""
from __future__ import annotations
import json, os, sys, time

STATE_DIR = "/tmp/maestro_harness_state"
SOLO_READ_TOOLS = {"view_file", "Read", "grep_search", "find_by_name", "Grep", "Glob"}
SOLO_WRITE_TOOLS = {"replace_file_content", "multi_replace_file_content", "write_to_file", "Edit", "MultiEdit", "Write"}
DELEGATION_TOOLS = {"invoke_subagent", "send_message", "Task", "Agent"}

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

def _is_delegation_command(cmd_str: str) -> bool:
    if not cmd_str:
        return False
    return any(m in cmd_str for m in ("maestro worker-start", "agy-orca worker-start", "maestro.py worker-start", "agy_orca.py worker-start", "codex exec", "claude -p"))

def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    event = (sys.argv[1] if len(sys.argv) > 1 else "") or payload.get("hook_event_name") or payload.get("event") or "PreInvocation"

    # Immediately bypass if running inside an active sub-worker (prevents blocking worker execution)
    if os.environ.get("MAESTRO_WORKER_ACTIVE") == "1":
        if event in ("PreToolUse", "BeforeTool"):
            print('{"decision":"allow"}')
        return 0

    is_agy = "conversationId" in payload or "toolCall" in payload
    session_id = str(payload.get("conversationId") or payload.get("session_id") or "default")
    st = _load_session_state(session_id)

    if event in ("PreInvocation", "UserPromptSubmit", "BeforeAgent", "SessionStart"):
        prev_streak = int(st.get("solo_streak", 0))
        prev_read_streak = int(st.get("read_streak", 0))
        st["solo_streak"] = 0
        st["read_streak"] = 0
        st["delegated_in_turn"] = False
        _save_session_state(session_id, st)
        if is_agy:
            print('{"decision":"allow"}')
        else:
            if prev_streak >= 2 or prev_read_streak >= 4:
                print("[🎼 MAESTRO HARNESS] 단독 작업이 연속되었습니다. 읽기/탐색은 invoke_subagent(TypeName='research', Model='flash'), 코드 수정/구현은 maestro worker-start (Astra/Opus/Sol, 코딩에 Flash 금지)로 위임하세요.")
        return 0

    if event in ("PreToolUse", "BeforeTool"):
        tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        tool_name = str(tool_call.get("name") or payload.get("tool_name") or payload.get("tool") or "")
        tool_input = tool_call.get("arguments") or payload.get("tool_input") or payload.get("input") or {}
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except Exception:
                tool_input = {"raw": tool_input}
        cmd_line = str(tool_input.get("CommandLine") or tool_input.get("command") or tool_input.get("cmd") or "")
        if tool_name in DELEGATION_TOOLS or _is_delegation_command(cmd_line):
            st["solo_streak"] = 0
            st["read_streak"] = 0
            st["delegated_in_turn"] = True
            _save_session_state(session_id, st)
        elif tool_name in SOLO_READ_TOOLS:
            st["read_streak"] = int(st.get("read_streak", 0)) + 1
            _save_session_state(session_id, st)
        elif tool_name in SOLO_WRITE_TOOLS or (tool_name in ("run_command", "Bash") and not _is_delegation_command(cmd_line)):
            st["solo_streak"] = int(st.get("solo_streak", 0)) + 1
            _save_session_state(session_id, st)
        print('{"decision":"allow"}')
        return 0

    print('{"decision":"allow"}')
    return 0

if __name__ == "__main__":
    sys.exit(main())
