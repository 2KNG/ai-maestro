"""3-step setup wizard: prompts, defaults, config preservation, hub matching."""
from pathlib import Path

import pytest

import maestro_remote
import maestro_setup as setup
import remote_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / '.claude'))
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Darwin')
    return tmp_path


def run(argv, answers=(), monkeypatch=None):
    lines, asked = [], []
    answers = list(answers)
    def ask(prompt):
        asked.append(prompt)
        return answers.pop(0) if answers else ''
    code = setup.main(argv, ask=ask if answers or argv.count('--yes') == 0 else None, out=lines.append)
    return code, '\n'.join(lines), asked


def test_step_prompts_are_exact(home, monkeypatch):
    monkeypatch.setattr(setup, 'find_cli', lambda cli: '/bin/' + cli['key'])
    monkeypatch.setattr(setup.orca_bridge, 'discover', lambda system=None: None)
    code, out, asked = run([], ['y', 'y', ''])
    assert code == 0
    assert asked[0].startswith(setup.STEP1_PROMPT)
    assert asked[1].startswith(setup.STEP2_PROMPT)
    assert asked[2].startswith(setup.STEP3_PROMPT)
    assert '[x] Antigravity' in out and '[x] OpenAI Codex CLI' in out and '[x] Claude Code CLI' in out


def test_cli_install_restores_touched_configs(home, monkeypatch):
    codex = home / '.codex' / 'config.toml'
    claude = home / '.claude' / 'settings.json'
    codex.parent.mkdir(); claude.parent.mkdir()
    codex.write_text('model = "keep"\n'); claude.write_text('{"keep": true}')
    installed = set()
    monkeypatch.setattr(setup, 'find_cli', lambda cli: '/bin/' + cli['key'] if cli['key'] in installed or cli['key'] == 'agy' else None)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/usr/bin/npm' if name == 'npm' else None)
    commands = []
    def fake_install(argv, timeout):
        commands.append(argv)
        installed.add('codex' if 'codex' in argv[-1] else 'claude')
        codex.write_text('clobbered'); claude.unlink()
        return type('R', (), {'returncode': 0})()
    monkeypatch.setattr(setup.subprocess, 'run', fake_install)
    code, out, _ = run(['--yes', '--skip-orca', '--skip-remote'])
    assert code == 0
    assert commands == [['/usr/bin/npm', 'install', '-g', '@openai/codex'],
                        ['/usr/bin/npm', 'install', '-g', '@anthropic-ai/claude-code']]
    assert codex.read_text() == 'model = "keep"\n' and claude.read_text() == '{"keep": true}'
    assert '복원' in out


def test_selected_subset_and_check_only_installs_nothing(home, monkeypatch):
    monkeypatch.setattr(setup, 'find_cli', lambda cli: None)
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **k: pytest.fail('must not install'))
    code, out, _ = run(['--check-only', '--skip-orca'])
    assert code == 0 and '건너뜀' in out
    wizard = setup.Wizard(setup.build_parser().parse_args(['--cli', 'codex', '--check-only', '--skip-orca', '--skip-remote']),
                          out=lambda *_: None)
    wizard.step_clis()
    assert wizard.report['clis'] == {'codex': 'missing'}


def test_hub_answer_is_saved_and_remote_up_runs(home, monkeypatch):
    monkeypatch.setattr(setup, 'find_cli', lambda cli: '/bin/x')
    calls = []
    monkeypatch.setattr(maestro_remote, 'main', lambda argv: calls.append(argv) or 0)
    code, out, _ = run(['--skip-cli', '--skip-orca'], ['hub.example.com', '~/.ssh/hub_key', 'y'])
    assert code == 0
    saved = remote_config.load()
    assert saved['hub'] == 'ubuntu@hub.example.com' and saved['registry_url'] == 'https://hub.example.com'
    assert saved['identity_file'] == str(Path('~/.ssh/hub_key').expanduser())
    assert calls and calls[0][:1] == ['up'] and '--hub' in calls[0] and 'ubuntu@hub.example.com' in calls[0]
    assert 'wss://hub.example.com/orca/' in out


def test_noninteractive_hub_flag(home, monkeypatch):
    calls = []
    monkeypatch.setattr(maestro_remote, 'main', lambda argv: calls.append(argv) or 0)
    code, _, _ = run(['--yes', '--skip-cli', '--skip-orca', '--hub', '203.0.113.7',
                      '--registry-url', 'https://hub.example.com', '--node-id', 'lab'])
    assert code == 0
    assert remote_config.load() == {'hub': 'ubuntu@203.0.113.7', 'registry_url': 'https://hub.example.com', 'node_id': 'lab'}
    assert calls[0][:3] == ['up', '--node-id', 'lab']


def test_invalid_hub_is_rejected_without_saving(home):
    code, out, _ = run(['--yes', '--skip-cli', '--skip-orca', '--hub', 'host;evil'])
    assert '주소 형식 오류' in out and remote_config.load() == {}


def test_orca_missing_points_to_releases(home, monkeypatch):
    monkeypatch.setattr(setup.orca_bridge, 'discover', lambda system=None: None)
    code, out, _ = run(['--yes', '--skip-cli', '--skip-remote'])
    assert setup.ORCA_RELEASES in out
