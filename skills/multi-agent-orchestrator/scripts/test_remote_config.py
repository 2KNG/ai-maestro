"""User-local hub settings: parsing, precedence, persistence."""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import maestro_remote as remote
import remote_config


@pytest.mark.parametrize('raw,hub,origin', [
    ('hub.example.com', 'ubuntu@hub.example.com', 'https://hub.example.com'),
    ('https://hub.example.com/', 'ubuntu@hub.example.com', 'https://hub.example.com'),
    ('admin@hub.example.com', 'admin@hub.example.com', 'https://hub.example.com'),
    ('hub.example.com:443', 'ubuntu@hub.example.com', 'https://hub.example.com'),
    ('203.0.113.7', 'ubuntu@203.0.113.7', None),
    ('ssh://ops@203.0.113.7', 'ops@203.0.113.7', None),
])
def test_normalize_server(raw, hub, origin):
    result = remote_config.normalize_server(raw)
    assert result['hub'] == hub
    assert result.get('registry_url') == origin


@pytest.mark.parametrize('raw', ['', 'host;rm -rf /', '-oProxyCommand=x', 'ftp://hub.example.com', 'bad user@host'])
def test_normalize_server_rejects_unsafe(raw):
    with pytest.raises(ValueError):
        remote_config.normalize_server(raw)


def test_ip_keeps_origin_of_same_hub_and_drops_other():
    saved = {'hub': 'ubuntu@203.0.113.7', 'registry_url': 'https://hub.example.com'}
    assert remote_config.apply_server('203.0.113.7', saved=saved) == {'hub': 'ubuntu@203.0.113.7'}
    assert remote_config.apply_server('198.51.100.9', saved=saved) == {'hub': 'ubuntu@198.51.100.9', 'registry_url': None}


def test_save_is_private_merges_and_env_overrides(monkeypatch):
    path = remote_config.save({'hub': 'ubuntu@hub.example.com', 'registry_url': 'https://hub.example.com', 'junk': 1})
    remote_config.save({'identity_file': '/keys/hub'})
    data = json.loads(path.read_text())
    assert data == {'hub': 'ubuntu@hub.example.com', 'registry_url': 'https://hub.example.com', 'identity_file': '/keys/hub'}
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('MAESTRO_REMOTE_HUB', 'ops@other.example.com')
    monkeypatch.setenv('MAESTRO_SSH_KEY', '/keys/other')
    resolved = remote_config.resolve()
    assert resolved['hub'] == 'ops@other.example.com' and resolved['identity_file'] == '/keys/other'
    remote_config.save({'identity_file': None})
    assert 'identity_file' not in remote_config.load()


def _args(**kw):
    base = dict(hub=None, ssh_user=None, node_id=None, registry_url=None, identity_file=None,
                hub_project=None, hub_db=None, hub_python=None, hub_probe_url=None)
    return SimpleNamespace(**{**base, **kw})


def test_cli_beats_saved_settings():
    saved = {'hub': 'ubuntu@203.0.113.7', 'registry_url': 'https://hub.example.com', 'node_id': 'office'}
    settings, updates = remote.resolve_settings(_args(), saved)
    assert settings['hub'] == 'ubuntu@203.0.113.7' and settings['node_id'] == 'office' and updates == {}
    settings, updates = remote.resolve_settings(_args(hub='new.example.com', node_id='lab'), saved)
    assert settings['hub'] == 'ubuntu@new.example.com'
    assert settings['registry_url'] == 'https://new.example.com'
    assert settings['node_id'] == 'lab'
    assert updates == {'hub': 'ubuntu@new.example.com', 'registry_url': 'https://new.example.com'}


def test_up_without_any_hub_explains_setup(capsys):
    assert remote.main(['up', '--dry-run']) == 1
    assert 'maestro setup' in capsys.readouterr().out


def test_configure_saves_and_up_uses_saved_hub(monkeypatch, capsys):
    assert remote.main(['configure', '--hub', 'hub.example.com', '--identity-file', '/keys/hub', '--node-id', 'lab']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['fleet_url'] == 'https://hub.example.com/orca/lab/'
    assert remote_config.load() == {'hub': 'ubuntu@hub.example.com', 'registry_url': 'https://hub.example.com',
                                    'identity_file': '/keys/hub', 'node_id': 'lab'}
    seen = {}
    monkeypatch.setattr(remote, 'provision', lambda config, dry_run=False: seen.update(config) or {'ok': True})
    assert remote.main(['up', '--dry-run']) == 0
    assert seen['hub'] == 'ubuntu@hub.example.com' and seen['node_id'] == 'lab'
    assert seen['registry_url'] == 'https://hub.example.com' and seen['identity_file'] == '/keys/hub'


def test_up_with_new_hub_persists_before_provision(monkeypatch, capsys):
    monkeypatch.setattr(remote, 'provision', lambda config, dry_run=False: {'ok': True})
    assert remote.main(['up', '--hub', 'ops@203.0.113.7', '--registry-url', 'https://hub.example.com']) == 0
    assert remote_config.load() == {'hub': 'ops@203.0.113.7', 'registry_url': 'https://hub.example.com'}


def test_ip_configure_points_to_missing_domain(capsys):
    assert remote.main(['configure', '--hub', '203.0.113.7']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['fleet_url'] is None and 'registry-url' in out['next']


def test_fleet_nodes_lists_only_registered(monkeypatch):
    body = json.dumps({'nodes': [{'node_id': 'b', 'slot': 11, 'online': True, 'token': 'x'},
                                 {'node_id': 'a', 'slot': 0}, {'slot': 3}]}).encode()
    response = Mock()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    response.read = lambda n: body
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda request, timeout: response)
    nodes = remote.fleet_nodes({'registry_url': 'https://hub.example.com'})
    assert [n['node_id'] for n in nodes] == ['a', 'b']
    assert 'token' not in nodes[1]


def test_existing_orca_remote_unit_is_reused(tmp_path, monkeypatch):
    config = remote.build_plan('test-node', 26768, 'ubuntu@hub.example.com', system='Linux', home=tmp_path,
                               widget_port=28701, orca='/opt/orca-ide', orca_url='wss://hub.example.com/orca/test-node/',
                               orca_service='orca-remote.service')
    monkeypatch.setattr(remote, 'port_open', lambda port: port == 6768)
    monkeypatch.setattr(remote, 'run', lambda argv: Mock(returncode=0, stdout=(
        'ActiveState=active\nExecStart={ path=/opt/orca-ide ; argv[]=/opt/orca-ide --serve --serve-port=6768 }'
        if 'orca-remote.service' in argv else '')))
    errors = remote.local_preflight(config)
    assert not any('6768' in e for e in errors)
    orca = next(s for s in config['services'] if s['role'] == 'orca')
    assert orca['reused_unit'] == 'orca-remote.service'


def test_node_urls_without_hub_has_no_fleet_url():
    urls = remote.node_urls('lab')
    assert urls['fleet_url'] is None and urls['fleet_home_url'] is None
