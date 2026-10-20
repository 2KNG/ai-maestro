"""Exact Antigravity spawn edges and ordered child lifecycle from transcripts."""
import json
import os
import re

RESTART = 'All your subagents and background tasks have been stopped due to server restart'


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def transcript_state(cids):
    parents, killed = {}, {}
    for cid in cids:
        children = set()
        pending_spawn = False
        path = os.path.expanduser(f'~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl')
        try:
            stream = open(path, encoding='utf-8', errors='replace')
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(step, dict):
                    continue
                content = str(step.get('content') or '')
                # Only runtime messages count; a user quoting the marker is not a stop.
                if RESTART in content and (
                    (step.get('type') == 'SYSTEM_MESSAGE' and step.get('source', 'SYSTEM') == 'SYSTEM'
                     and ('sender=system' in content or content.strip() == RESTART))
                    or content.strip() == RESTART
                ):
                    for child in children:
                        killed[child] = True
                if pending_spawn and 'Created the following subagents:' in content:
                    for child in re.findall(r'"conversationId"\s*:\s*"([^"\s]+)"', content):
                        if child != cid:
                            parents[child] = cid
                            children.add(child)
                            killed[child] = False
                    pending_spawn = False
                for call in step.get('tool_calls') or []:
                    if not isinstance(call, dict):
                        continue
                    name = call.get('name')
                    args = decode(call.get('args') or call.get('arguments') or {})
                    if not isinstance(args, dict):
                        continue
                    if name == 'invoke_subagent':
                        pending_spawn = True
                    elif name in ('manage_subagents', 'send_message'):
                        action = str(decode(args.get('Action') or args.get('action') or '')).lower()
                        # Match only recipient fields, never IDs quoted inside the message.
                        targets = json.dumps({k: decode(v) for k, v in args.items()
                                              if k.lower() not in ('message', 'toolaction', 'toolsummary', 'action')})
                        for child in children:
                            if name == 'manage_subagents' and (action == 'kill_all' or (action == 'kill' and child in targets)):
                                killed[child] = True
                            elif name == 'send_message' and child in targets:
                                killed[child] = False
    return parents, killed
