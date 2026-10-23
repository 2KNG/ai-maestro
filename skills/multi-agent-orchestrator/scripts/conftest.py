"""Keep tests away from the developer's real hub settings and live Orca runtime."""
import sys

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp('user-settings')
    monkeypatch.setenv('MAESTRO_REMOTE_CONFIG', str(base / 'remote.json'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(base / 'cache'))
    monkeypatch.setenv('ORCA_CLI_COMMAND', str(base / 'no-orca-cli'))
    monkeypatch.setenv('MAESTRO_POOL_WAKE_STATE', str(base / 'pool_wake_state.json'))
    monkeypatch.setenv('MAESTRO_AUTO_WAKE', '0')
    usage = sys.modules.get('check_all_usage')
    if usage is not None:
        usage._PS_CACHE.clear()  # per-scan ps memo must not leak mocked output
    for name in ('MAESTRO_REMOTE_HUB', 'MAESTRO_SSH_KEY', 'MAESTRO_REGISTRY_URL', 'MAESTRO_NODE_ID',
                 'ORCA_TERMINAL_HANDLE', 'ORCA_WORKTREE_PATH'):
        monkeypatch.delenv(name, raising=False)
