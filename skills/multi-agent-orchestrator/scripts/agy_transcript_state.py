"""Exact Antigravity spawn edges and ordered child lifecycle from transcripts."""
import datetime
import math
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


def _timestamp(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            value = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return value if math.isfinite(value) and value > 0 else None


def _latest_activity(path):
    """Read recent event times, never file mtime (copying logs is not activity)."""
    from spawn_evidence import cached
    def load(path):
        latest = None
        with open(path, 'rb') as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 65536))
            for line in stream.read().splitlines():
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(step, dict):
                    continue
                stamp = _timestamp(step.get('created_at') or step.get('timestamp'))
                if stamp is not None:
                    latest = max(latest or stamp, stamp)
        return latest
    return cached(path, 'agy-latest-activity', load)


def _parse_transcript(path):
    from spawn_evidence import compact, command_evidence, is_worker_command, result_ids, Snippets
    parents, killed, kill_times = {}, {}, {}
    children = set()
    pending_spawn = False
    cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    snippets, launches, activity = Snippets(), set(), False
    stream = open(path, encoding='utf-8', errors='replace')
    with stream:
        for line in stream:
            if not any(marker in line for marker in (
                'invoke_subagent', 'manage_subagents', 'send_message', 'run_command',
                'Created the following subagents:', 'stopped due to server restart',
                'Output:', '\\u',
            )):
                continue
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if not isinstance(step, dict):
                continue
            content = str(step.get('content') or '')
            if step.get('type') in ('GENERIC', 'SYSTEM_MESSAGE'):
                output = content.partition('Output:\n')[2]
                if output:
                    snippets.append(compact(output))
                    launches.update(result_ids(output))
            activity = activity or 'maestro' in line
            # Restart notices describe background tasks, not child lifecycles.
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
                if name == 'run_command':
                    command = str(decode(args.get('CommandLine') or ''))
                    if is_worker_command(command):
                        snippets.append(command_evidence(command, include_files=False))
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
                            kill_times[child] = _timestamp(step.get('created_at') or step.get('timestamp'))
                        elif name == 'send_message' and child in targets:
                            killed[child] = False
    return parents, killed, snippets.text, frozenset(launches), activity, kill_times


def transcript_summary(path):
    from spawn_evidence import cached, command_evidence
    summary = cached(path, 'agy-transcript', _parse_transcript)[:5]
    # Spec files have an independent lifecycle from the transcript that names them.
    return (*summary[:2], command_evidence(summary[2]), *summary[3:])


def transcript_state(cids):
    from spawn_evidence import cached
    parents, killed, kill_times = {}, {}, {}
    for cid in cids:
        path = os.path.expanduser(f'~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl')
        try:
            p, k, _, _, _, times = cached(path, 'agy-transcript', _parse_transcript)
        except OSError:
            continue
        parents.update(p)
        killed.update(k)
        kill_times.update(times)
    for child, stopped in killed.items():
        if not stopped or kill_times.get(child) is None:
            continue
        path = os.path.expanduser(f'~/.gemini/antigravity/brain/{child}/.system_generated/logs/transcript.jsonl')
        try:
            latest = _latest_activity(path)
        except OSError:
            continue
        if latest is not None and latest > kill_times[child]:
            killed[child] = False
    return parents, killed
