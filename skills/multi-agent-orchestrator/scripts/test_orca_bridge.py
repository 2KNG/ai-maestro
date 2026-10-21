"""Orca CLI wrapper: argv contract, parsing, failure tolerance."""
import json
from types import SimpleNamespace

import pytest

import maestro
import orca_bridge


def fake_run(payloads, calls):
    def run(argv, **kwargs):
        calls.append(argv)
        key = ' '.join(argv[1:3])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'result': payloads.get(key, {})}), stderr='')
    return run


TERMINALS = {'terminals': [
    {'handle': 'term_1', 'title': 'Claude Code', 'worktreePath': '/w/repo', 'branch': 'refs/heads/main',
     'agentIdentity': 'claude', 'connected': True, 'writable': True, 'lastOutputAt': 1700000000000, 'preview': 'x' * 400},
    {'handle': 'term_2', 'title': '', 'worktreePath': '/w/repo', 'branch': '', 'connected': True, 'orphaned': True},
    {'handle': 'term_3', 'title': 'bash', 'worktreePath': '/w/other', 'branch': '', 'connected': False},
]}


def test_list_terminals_drops_orphans_and_bounds_preview(monkeypatch):
    calls = []
    monkeypatch.setattr(orca_bridge.subprocess, 'run', fake_run({'terminal list': TERMINALS}, calls))
    rows = orca_bridge.list_terminals(worktree='path:/w/repo', orca='/opt/orca-ide')
    assert [r['handle'] for r in rows] == ['term_1', 'term_3']
    assert rows[0]['branch'] == 'main' and len(rows[0]['preview']) == orca_bridge._PREVIEW
    assert calls[0] == ['/opt/orca-ide', 'terminal', 'list', '--worktree', 'path:/w/repo', '--json']


def test_send_passes_text_as_single_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(orca_bridge.subprocess, 'run', fake_run({}, calls))
    orca_bridge.send('term_1', 'echo "$(rm -rf /)"; `x`', enter=True, orca='/opt/orca-ide')
    assert calls[0] == ['/opt/orca-ide', 'terminal', 'send', '--terminal', 'term_1',
                        '--text', 'echo "$(rm -rf /)"; `x`', '--enter', '--json']
    with pytest.raises(orca_bridge.OrcaError):
        orca_bridge.send('', 'x', orca='/opt/orca-ide')


def test_error_payload_raises(monkeypatch):
    monkeypatch.setattr(orca_bridge.subprocess, 'run', lambda argv, **k: SimpleNamespace(
        returncode=1, stdout=json.dumps({'ok': False, 'error': {'message': 'runtime unreachable'}}), stderr=''))
    with pytest.raises(orca_bridge.OrcaError, match='runtime unreachable'):
        orca_bridge.call(['status'], orca='/opt/orca-ide')


def test_snapshot_tolerates_missing_runtime_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(orca_bridge.subprocess, 'run', fake_run({'terminal list': TERMINALS,
                                                                 'worktree list': {'worktrees': [{'path': '/w/repo'}]}}, calls))
    snap = orca_bridge.snapshot(max_age=60, orca='/opt/orca-ide')
    assert snap['running'] and len(snap['terminals']) == 2 and snap['worktrees'] == 1 and snap['agents'] == 1
    assert orca_bridge.summary_line(snap) == 'Orca 터미널 2 · 에이전트 1 · 워크트리 1'
    orca_bridge.snapshot(max_age=60, orca='/opt/orca-ide')
    assert len(calls) == 2  # second call served from cache
    def boom(*a, **k):
        raise OSError('no runtime')
    monkeypatch.setattr(orca_bridge.subprocess, 'run', boom)
    down = orca_bridge.snapshot(max_age=0, orca='/opt/orca-ide')
    assert down['available'] and not down['running']
    assert orca_bridge.summary_line(down) == 'Orca 꺼짐'


def test_linux_never_uses_screen_reader(monkeypatch):
    monkeypatch.delenv('ORCA_CLI_COMMAND')
    monkeypatch.setattr(orca_bridge.shutil, 'which', lambda name: '/usr/bin/orca' if name == 'orca' else None)
    assert orca_bridge.discover('Linux') is None
    assert orca_bridge.discover('Darwin') == '/usr/bin/orca'


def test_maestro_dispatches_orca_commands(monkeypatch):
    seen = []
    monkeypatch.setattr(orca_bridge, 'main', lambda command, argv: seen.append((command, argv)) or 0)
    monkeypatch.setattr(maestro.sys, 'argv', ['maestro', 'orca-list', '--limit', '3'])
    with pytest.raises(SystemExit) as stopped:
        maestro.main()
    assert stopped.value.code == 0 and seen == [('orca-list', ['--limit', '3'])]
    monkeypatch.setattr(maestro.sys, 'argv', ['maestro', 'orca-bogus'])
    with pytest.raises(SystemExit) as stopped:
        maestro.main()
    assert 'unknown Orca command' in str(stopped.value.code)
