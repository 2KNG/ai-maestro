"""Bounded, short-lived Linux process evidence for workers with empty task logs.

Only executable argv positions identify workers. Prompt text is never searched.
The cache may retain a departed process for CACHE_TTL seconds.
"""
import os
from pathlib import Path
import re
import threading
import time

import spawn_evidence

CACHE_TTL = 1.0
MAX_PROCESSES = 32768
READ_LIMIT = 128 * 1024
_TASK_LOG = re.compile(r'/brain/([^/]+)/\.system_generated/tasks/(task-[^/]+)\.log(?: \(deleted\))?$')
_cache = {}
_lock = threading.RLock()


def clear_cache():
    with _lock:
        _cache.clear()


def _read(path):
    try:
        with open(path, 'rb') as stream:
            return stream.read(READ_LIMIT).decode('utf-8', errors='replace')
    except OSError:
        return ''


def _env(path):
    return dict(item.split('=', 1) for item in _read(path).split('\0') if '=' in item)


def _link(path):
    try:
        return os.readlink(path)
    except OSError:
        return ''


def _task(path):
    for fd in ('1', '2'):
        target = _link(path / 'fd' / fd)
        match = _TASK_LOG.search(target)
        if match:
            return match[1], match[2], target.removesuffix(' (deleted)')
    return '', '', ''


def detect_parent_conversation_id(proc_root='/proc', pid=None, environ=None):
    """Prefer explicit Antigravity attribution, then stdout/stderr task log path."""
    if environ is None:
        environ = os.environ if pid is None else _env(Path(proc_root) / str(pid) / 'environ')
    cid = environ.get('ANTIGRAVITY_CONVERSATION_ID', '').strip()
    return cid or _task(Path(proc_root) / str(os.getpid() if pid is None else pid))[0]


def _executable(argv):
    if not argv:
        return '', []
    name = Path(argv[0]).name
    # Script launchers are accepted only at their script argument, never -c/-e.
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?|node(?:js)?', name):
        index = 1
        while index < len(argv) and argv[index] in ('-u', '-B', '--no-warnings'):
            index += 1
        if index >= len(argv) or argv[index].startswith('-'):
            return '', []
        name, argv = Path(argv[index]).name, argv[index:]
    if name in ('maestro', 'maestro.py', 'agy-orca') and len(argv) > 1 and argv[1] == 'worker-start':
        return 'maestro', argv[2:]
    if name in ('codex', 'codex.js', 'claude', 'claude.js'):
        if len(argv) > 1 and argv[1] in ('app-server', 'mcp-server', 'login', 'logout', 'update', 'doctor'):
            return '', []
        return name.split('.')[0], argv[1:]
    # Claude's npm launcher has a generic filename; require its package path.
    if name == 'cli.js' and '/@anthropic-ai/claude-code/' in argv[0]:
        return 'claude', argv[1:]
    return '', []


def _option(args, flag):
    for index, arg in enumerate(args):
        if arg == '--':
            break
        if arg.startswith(flag + '='):
            return arg[len(flag) + 1:]
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
    return ''


def _session(provider, args, env):
    sid = _option(args, '--session-id') or _option(args, '--resume')
    if not sid and provider == 'codex':
        # Only command positions, so a prompt containing 'resume' cannot match.
        index = 1 if args[:1] == ['exec'] else 0
        if len(args) > index + 1 and args[index] == 'resume' and not args[index + 1].startswith('-'):
            sid = args[index + 1]
    return sid or env.get('CODEX_THREAD_ID', '') or env.get('CLAUDE_SESSION_ID', '') or env.get('MAESTRO_SESSION_ID', '')


def _prompt(provider, args):
    if provider not in ('codex', 'claude'):
        return ''
    value_options = {'-m', '--model', '-c', '--config', '-C', '--cd', '--cwd',
                     '--session-id', '--resume', '-r', '--effort', '--output-format',
                     '--input-format', '--permission-mode', '--allowedTools',
                     '--disallowedTools', '--system-prompt', '--append-system-prompt',
                     '--max-turns', '--max-budget-usd', '--add-dir', '--sandbox',
                     '-s', '--profile', '--color', '--output-last-message', '-o',
                     '--output-schema', '--image', '-i'}
    positionals = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            positionals.extend(args[index + 1:])
            break
        if arg in value_options:
            index += 2
            continue
        if not arg.startswith('-'):
            positionals.append(arg)
        index += 1
    if provider == 'codex':
        if positionals[:1] == ['exec']:
            positionals.pop(0)
        if positionals[:1] == ['resume']:
            positionals = positionals[2:] if '--last' not in args else positionals[1:]
    return positionals[-1] if positionals else ''


_SERVICE_TIER = re.compile(r'^\s*service_tier\s*=\s*["\']?([a-z]+)')


def _speed_tier(provider, args, env):
    """Speed tier the worker process was launched with ('' when unknown)."""
    if provider == 'codex':
        tier = ''
        for index, arg in enumerate(args):
            if arg == '--':
                break
            value = args[index + 1] if arg in ('-c', '--config') and index + 1 < len(args) else (
                arg.split('=', 1)[1] if arg.startswith('--config=') else '')
            match = _SERVICE_TIER.match(value)
            if match:
                tier = match[1]  # last -c wins, as in codex
        return tier
    if provider == 'claude':
        model = _option(args, '--model').lower()
        if env.get('CLAUDE_CODE_FAST_MODE') == '1' or re.search(r'(?<![a-z])fast(?![a-z])', model):
            return 'fast'
        return 'default' if model else ''
    return ''


def _rollout_session(path):
    try:
        with os.scandir(path / 'fd') as entries:
            for index, entry in enumerate(entries):
                if index >= 512:
                    break
                target = _link(entry.path)
                match = re.search(r'/rollout-[^/]*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl(?: \(deleted\))?$', target)
                if match:
                    return match[1]
    except OSError:
        pass
    return ''


def _scan(proc_root):
    root = Path(proc_root)
    processes = {}
    try:
        with os.scandir(root) as entries:
            pids = []
            for entry in entries:
                if entry.name.isdigit():
                    pids.append(entry.name)
                    if len(pids) >= MAX_PROCESSES:
                        break
    except OSError:
        return []
    for pid in pids:
        path = root / pid
        stat = _read(path / 'stat')
        fields = stat[stat.rfind(')') + 1:].split()
        if len(fields) < 2 or fields[0] == 'Z':
            continue
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        argv = _read(path / 'cmdline').rstrip('\0').split('\0')
        provider, args = _executable(argv)
        env = _env(path / 'environ')
        cid, task_id, task_log = _task(path)
        cwd = _link(path / 'cwd')
        record = dict(pid=int(pid), ppid=ppid,
                      parent_conversation_id=env.get('ANTIGRAVITY_CONVERSATION_ID', '').strip() or cid,
                      provider=provider, pool='', session_id='', registry_id='', spec='', cwd=cwd,
                      task_id=task_id, task_log=task_log, tier='')
        if provider:
            record['pool'] = _option(args, '--pool') or env.get('MAESTRO_WORKER_POOL', '') or provider
            record['tier'] = _speed_tier(provider, args, env)
            # CODEX_THREAD_ID may be inherited from the spawning Codex session.
            # An open rollout or explicit CLI ID identifies this process itself.
            record['session_id'] = _session(provider, args, {})
            if provider == 'codex' and not record['session_id']:
                record['session_id'] = _rollout_session(path)
            if not record['session_id']:
                record['session_id'] = _session(provider, args, env)
            record['registry_id'] = env.get('MAESTRO_REGISTRY_ID', '') or _option(args, '--registry-id')
            record['spec'] = _option(args, '--spec')
            spec_file = _option(args, '--spec-file')
            if not record['spec'] and spec_file:
                try:
                    candidate = Path(spec_file)
                    if not candidate.is_absolute():
                        candidate = Path(cwd) / candidate if cwd else path / 'cwd' / candidate
                    record['spec'] = spawn_evidence.read_spec_file(candidate, limit=READ_LIMIT)
                except (OSError, ValueError):
                    pass
            if not record['spec']:
                record['spec'] = _prompt(provider, args)
        processes[int(pid)] = record
    result = []
    for pid, record in processes.items():
        if not record['provider']:
            continue
        record = dict(record)
        visited = {pid}
        parent = record['ppid']
        while parent in processes and parent not in visited:
            visited.add(parent)
            ancestor = processes[parent]
            for key in ('parent_conversation_id', 'task_id', 'task_log'):
                if not record[key]:
                    record[key] = ancestor[key]
            if ancestor['provider'] == 'maestro':
                for key in ('spec', 'registry_id'):
                    if not record[key]:
                        record[key] = ancestor[key]
            parent = ancestor['ppid']
        result.append(record)
    return result


def scan_live_worker_processes(proc_root='/proc', *, force=False):
    """Return independent record dictionaries; scans are cached for one second."""
    key = os.fspath(proc_root)
    with _lock:
        now = time.monotonic()
        entry = _cache.get(key)
        if force or entry is None or now - entry[0] >= CACHE_TTL:
            records = _scan(proc_root)
            # Bound cache even when callers use many synthetic proc roots.
            if len(_cache) >= 8:
                _cache.clear()
            _cache[key] = (time.monotonic(), records)
        return [dict(record) for record in _cache[key][1]]
