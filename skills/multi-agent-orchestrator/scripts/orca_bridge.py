#!/usr/bin/env python3
"""Thin wrapper over the Orca CLI (`orca terminal ...`, `orca worktree ...`).

Used by `maestro orca-*`, `maestro remote status` and the usage widget. Every
call is bounded and failure-tolerant: when Orca is absent or not running the
callers get ``{'available': False, ...}`` instead of an exception.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

TIMEOUT = 15
CACHE_TTL = 20
_PREVIEW = 160


class OrcaError(RuntimeError):
    pass


def discover(system=None):
    """Full path of the Orca CLI, or None.

    On Linux a bare `orca` may be the GNOME screen reader (/usr/bin/orca), so only
    `orca-ide` or a user shim outside /usr is accepted.
    """
    explicit = os.environ.get('ORCA_CLI_COMMAND')
    if explicit:
        return explicit
    system = system or platform.system()
    if system == 'Linux':
        found = shutil.which('orca-ide')
        if found:
            return found
        found = shutil.which('orca')
        if found and not found.startswith('/usr/'):
            return found
        return None
    for name in ('orca', 'orca-ide'):
        found = shutil.which(name)
        if found:
            return found
    return None


def call(args, orca=None, timeout=TIMEOUT):
    """Run `orca <args> --json` and return its `result` object."""
    orca = orca or discover()
    if not orca:
        raise OrcaError('Orca CLI not found; install Orca (https://github.com/stablyai/orca/releases/latest) '
                        'or set ORCA_CLI_COMMAND')
    try:
        proc = subprocess.run([orca, *args, '--json'], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrcaError(f'Orca CLI failed: {type(exc).__name__}') from None
    try:
        payload = json.loads(proc.stdout or '{}')
    except ValueError:
        payload = {}
    if proc.returncode or not payload.get('ok', False):
        error = payload.get('error') if isinstance(payload, dict) else None
        message = error.get('message') if isinstance(error, dict) else error
        raise OrcaError(str(message or (proc.stderr or '').strip()[:200] or f'orca {args[0]} failed'))
    return payload.get('result') or {}


def _short_branch(ref):
    return (ref or '').removeprefix('refs/heads/')


def terminal_row(item):
    return {
        'handle': item.get('handle'),
        'title': item.get('title') or '',
        'worktree': item.get('worktreePath') or '',
        'branch': _short_branch(item.get('branch')),
        'agent': item.get('agentIdentity') or '',
        'connected': bool(item.get('connected')),
        'writable': bool(item.get('writable')),
        'host': item.get('executionHostId') or 'local',
        'last_output_at': (item.get('lastOutputAt') or 0) / 1000 or None,
        'preview': (item.get('preview') or '')[-_PREVIEW:],
    }


def list_terminals(worktree=None, limit=None, environment=None, orca=None):
    args = ['terminal', 'list']
    if worktree:
        args += ['--worktree', worktree]
    if limit:
        args += ['--limit', str(limit)]
    if environment:
        args += ['--environment', environment]
    result = call(args, orca)
    return [terminal_row(t) for t in result.get('terminals', []) if not t.get('orphaned')]


def list_worktrees(repo=None, limit=None, environment=None, orca=None):
    args = ['worktree', 'list']
    if repo:
        args += ['--repo', repo]
    if limit:
        args += ['--limit', str(limit)]
    if environment:
        args += ['--environment', environment]
    result = call(args, orca)
    return [{'id': w.get('id'), 'path': w.get('path'), 'branch': _short_branch(w.get('branch')),
             'host': w.get('hostId') or 'local', 'main': bool(w.get('isMainWorktree'))}
            for w in result.get('worktrees', [])]


def send(handle, text=None, enter=False, interrupt=False, environment=None, orca=None):
    if not handle:
        raise OrcaError('--terminal handle is required (see `maestro orca-list`)')
    args = ['terminal', 'send', '--terminal', handle]
    if text is not None:
        args += ['--text', text]
    if enter:
        args.append('--enter')
    if interrupt:
        args.append('--interrupt')
    if environment:
        args += ['--environment', environment]
    return call(args, orca)


def read(handle=None, cursor=None, limit=None, screen=False, environment=None, orca=None):
    args = ['terminal', 'read']
    if handle:
        args += ['--terminal', handle]
    if cursor is not None:
        args += ['--cursor', str(cursor)]
    if limit:
        args += ['--limit', str(limit)]
    if screen:
        args.append('--screen')
    if environment:
        args += ['--environment', environment]
    return call(args, orca)


def current_context():
    """Orca terminal/worktree this process runs in (env set by Orca's shell wrapper)."""
    return {k: v for k, v in (('terminal', os.environ.get('ORCA_TERMINAL_HANDLE')),
                              ('worktree', os.environ.get('ORCA_WORKTREE_PATH'))) if v}


def _cache_path():
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'ai-maestro' / 'orca_snapshot.json'


def snapshot(max_age=CACHE_TTL, orca=None):
    """Compact Orca summary for status views and the widget (cached briefly)."""
    path = _cache_path()
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        if time.time() - cached.get('collected_at', 0) < max_age:
            return cached
    except (OSError, ValueError):
        pass
    orca = orca or discover()
    data = {'available': bool(orca), 'collected_at': time.time(), 'terminals': [], 'worktrees': 0}
    if orca:
        try:
            data['terminals'] = list_terminals(orca=orca)
            data['worktrees'] = len(list_worktrees(orca=orca))
            data['running'] = True
        except OrcaError as exc:
            data['running'] = False
            data['error'] = str(exc)[:200]
    data['agents'] = sum(1 for t in data['terminals'] if t.get('agent'))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.orca.', suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def summary_line(snap):
    if not snap.get('available'):
        return 'Orca 미설치'
    if not snap.get('running'):
        return 'Orca 꺼짐'
    return f"Orca 터미널 {len(snap['terminals'])} · 에이전트 {snap.get('agents', 0)} · 워크트리 {snap.get('worktrees', 0)}"


def format_terminals(rows):
    if not rows:
        return '(Orca 터미널 없음)'
    home = str(Path.home())
    lines = []
    for t in rows:
        where = t['worktree'].replace(home, '~', 1) if t['worktree'].startswith(home) else t['worktree']
        flag = '' if t['connected'] else ' (disconnected)'
        agent = f" [{t['agent']}]" if t['agent'] else ''
        lines.append(f"{t['handle']}  {t['title'] or '-'}{agent}  {where}{(' @' + t['branch']) if t['branch'] else ''}{flag}")
    return '\n'.join(lines)


COMMANDS = ('orca-list', 'orca-send', 'orca-read', 'orca-worktrees', 'orca-status')


def main(command, argv=None):
    """`maestro orca-list|orca-send|orca-read|orca-worktrees|orca-status`."""
    import argparse
    parser = argparse.ArgumentParser(prog=f'maestro {command}')
    parser.add_argument('--environment', help='Saved paired Orca server (orca environment list)')
    parser.add_argument('--json', action='store_true')
    if command == 'orca-list':
        parser.add_argument('--worktree', help='Orca worktree selector, e.g. path:<dir>, branch:<name>, current')
        parser.add_argument('--limit', type=int)
    elif command == 'orca-worktrees':
        parser.add_argument('--repo')
        parser.add_argument('--limit', type=int)
    elif command == 'orca-send':
        parser.add_argument('--terminal', required=True, help='Handle from `maestro orca-list`')
        parser.add_argument('--text')
        parser.add_argument('--enter', action='store_true', help='Press Enter after the text')
        parser.add_argument('--interrupt', action='store_true')
    elif command == 'orca-read':
        parser.add_argument('--terminal', help='Handle (default: active terminal of the current worktree)')
        parser.add_argument('--cursor', type=int)
        parser.add_argument('--limit', type=int)
        parser.add_argument('--screen', action='store_true', help='Rendered screen instead of the output stream')
    args = parser.parse_args(argv)
    try:
        if command == 'orca-list':
            result = list_terminals(args.worktree, args.limit, args.environment)
            text = format_terminals(result)
        elif command == 'orca-worktrees':
            result = list_worktrees(args.repo, args.limit, args.environment)
            home = str(Path.home())
            text = '\n'.join(f"{w['path'].replace(home, '~', 1)}  {w['branch'] or '-'}  host={w['host']}"
                             for w in result) or '(Orca 워크트리 없음)'
        elif command == 'orca-send':
            result = send(args.terminal, args.text, args.enter, args.interrupt, args.environment)
            text = json.dumps(result, ensure_ascii=False)
        elif command == 'orca-read':
            result = read(args.terminal, args.cursor, args.limit, args.screen, args.environment)
            term = result.get('terminal') or {}
            tail = term.get('tail') or []
            text = '\n'.join(tail) if isinstance(tail, list) else str(tail)
            if term.get('nextCursor') is not None:
                text += f"\n[next --cursor {term['nextCursor']}]"
        else:
            result = {**snapshot(max_age=0), 'context': current_context()}
            text = summary_line(result) + (f"\n현재 Orca 터미널: {result['context']}" if result['context'] else '')
    except OrcaError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False) if args.json else f'error: {exc}',
              file=sys.stdout if args.json else sys.stderr)
        return 1
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2) if args.json else text)
    return 0
