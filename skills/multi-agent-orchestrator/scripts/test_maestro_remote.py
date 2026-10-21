import json
import os
from pathlib import Path
from unittest.mock import Mock
import pytest
import maestro_remote as remote


def plan(tmp_path, system='Linux'):
    return remote.build_plan('test-node', 26768, 'ubuntu@example.com', system=system,
                             home=tmp_path, widget_port=28701, orca='/opt/orca-ide', orca_url='wss://example.com/orca/test-node/')


@pytest.mark.parametrize('node,port,hub', [('bad/node',26768,'host'),('okay',65535,'host'),
    ('okay',6768,'host'),('okay',18765,'host'),('okay',26768,'-oProxyCommand=bad'),
    ('okay',26768,'user@host;evil'),('UPPER',26768,'host')])
def test_reject_command_injection_and_bad_ports(node,port,hub):
    with pytest.raises(ValueError):
        remote.validate(node,port,hub)


def test_forward_only_loopback_and_fail_closed(tmp_path):
    config=plan(tmp_path)
    args=config['tunnel_command']
    assert '127.0.0.1:26768:127.0.0.1:6768' in args
    assert '127.0.0.1:28701:127.0.0.1:18765' in args
    assert 'ExitOnForwardFailure=yes' in args
    assert 'StrictHostKeyChecking=yes' in args


@pytest.mark.parametrize('system,fragment',[('Linux','Restart=on-failure'),('Darwin','KeepAlive'),('Windows','managed')])
def test_persistent_platform_plans(tmp_path,system,fragment):
    config=plan(tmp_path,system)
    assert len(config['services'])==4
    assert fragment in config['services'][0]['content']
    assert not Path(config['state_dir']).exists()


def test_dry_run_never_writes_or_registers(tmp_path,monkeypatch):
    config=plan(tmp_path)
    adapter=Mock()
    adapter.preflight.return_value={'orca_url':config['orca_url']}
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    result=remote.provision(config,dry_run=True)
    assert result['dry_run']
    adapter.register.assert_not_called()
    assert not Path(config['state_dir']).exists()


def test_hub_failure_prevents_local_mutation(tmp_path,monkeypatch):
    adapter=Mock()
    adapter.preflight.side_effect=RuntimeError('secret material')
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    result=remote.provision(plan(tmp_path))
    assert not result['ok']
    assert 'secret material' not in json.dumps(result)
    adapter.register.assert_not_called()
    assert not (tmp_path/'.local').exists()


def test_preserve_unknown_service_file(tmp_path,monkeypatch):
    config=plan(tmp_path)
    path=Path(config['services'][0]['path'])
    path.parent.mkdir(parents=True)
    path.write_text('unrelated service')
    monkeypatch.setattr(remote,'port_open',lambda _:False)
    monkeypatch.setattr(remote,'run',lambda _:Mock(returncode=0,stdout=''))
    errors=remote.local_preflight(config)
    assert any('Refusing to replace' in x for x in errors)
    assert path.read_text()=='unrelated service'


def test_occupied_ports_never_taken_over(tmp_path,monkeypatch):
    monkeypatch.setattr(remote,'port_open',lambda _:True)
    monkeypatch.setattr(remote,'run',lambda _:Mock(returncode=0,stdout=''))
    errors=remote.local_preflight(plan(tmp_path))
    assert any('6768 already occupied' in x for x in errors)
    assert any('18765 already occupied' in x for x in errors)


def test_capture_only_real_pairing_and_keep_private(tmp_path):
    assert not remote.capture_pairing('invented orca://pair?x=secret',tmp_path)
    assert remote.capture_pairing('Pairing URL: orca://pair?code=actual\n',tmp_path)
    assert remote.capture_pairing('Web client URL: https://example.com/#actual\n',tmp_path)
    path=tmp_path/'pairing.json'
    assert json.loads(path.read_text())['pairing_code']=='orca://pair?code=actual'
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


def test_identity_file_is_argv_not_shell(tmp_path):
    config=remote.build_plan('test',hub='ubuntu@hub.example.com',identity_file=str(tmp_path/'key with spaces'))
    assert config['tunnel_command'][1:3]==['-i',str(tmp_path/'key with spaces')]


def test_install_identical_does_not_restart(tmp_path,monkeypatch):
    config=plan(tmp_path)
    calls=[]
    monkeypatch.setattr(remote,'checked',lambda argv:calls.append(argv))
    remote.install_services(config)
    remote.install_services(config)
    assert not any('restart' in call for call in calls)
    assert Path(config['services'][0]['path']).read_text()==config['services'][0]['content']


def test_known_widget_reused_without_restart(tmp_path,monkeypatch):
    config=plan(tmp_path)
    monkeypatch.setattr(remote,'port_open',lambda p:p==18765)
    monkeypatch.setattr(remote,'run',lambda _:Mock(returncode=0,stdout='ActiveState=active\nExecStart='+str(remote.SCRIPT.parent/'widget_bridge_daemon.py')))
    errors=remote.local_preflight(config)
    assert not any('18765' in x for x in errors)
    widget=next(s for s in config['services'] if s['role']=='widget')
    assert widget['reused_unit']=='agy-widget-bridge.service'
    calls=[]
    monkeypatch.setattr(remote,'checked',lambda argv:calls.append(argv))
    remote.install_services(config)
    assert not Path(widget['path']).exists()
    assert not any(widget['name']+'.service' in cmd for cmd in calls)

def test_token_never_in_dry_run(tmp_path,monkeypatch):
    config=plan(tmp_path)
    config['registration_token']='secret-registration-token'
    adapter=Mock()
    adapter.preflight.return_value={}
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    result=remote.provision(config,dry_run=True)
    assert 'secret-registration-token' not in json.dumps(result)
    assert config['node_token'] not in json.dumps(result)


def test_status_reports_legacy_services_when_uninstalled(tmp_path,monkeypatch):
    monkeypatch.setattr(remote.platform,'system',lambda:'Linux')
    monkeypatch.setattr(remote,'service_active',lambda system,name:name=='orca-remote.service')
    monkeypatch.setattr(remote,'port_open',lambda port:port==6768)
    monkeypatch.setattr(remote,'node_urls',lambda *args:{'local_lan_url':'http://192.168.1.2:6768/','fleet_url':'https://hub.example.com/orca/pc2/'})
    result=remote.remote_status('pc2',home=tmp_path)
    assert result['state']=='not_installed'
    assert len(result['legacy_services'])==3
    assert result['legacy_services'][0]['active']
    assert result['legacy_services'][0]['local_port']==6768
    assert result['pairing']=='pending'
    assert result['local_lan_url']=='http://192.168.1.2:6768/'
    assert not (tmp_path/'.local').exists()


def test_pairing_wait_is_bounded_and_validates_payload(tmp_path):
    (tmp_path/'pairing.json').write_text('{}')
    assert not remote.wait_for_pairing(tmp_path,timeout=0)
    remote.capture_pairing('Pairing URL: orca://pair?code=real',tmp_path)
    assert remote.wait_for_pairing(tmp_path,timeout=0)


def test_default_ports_are_allocated_not_requested(tmp_path):
    config=remote.build_plan('pc2',hub='ubuntu@hub.example.com',home=tmp_path)
    assert config['requested_port'] is None
    assert config['requested_widget_port'] is None
    assert config['port']==16701


def test_different_explicit_allocation_blocks_before_write(tmp_path,monkeypatch):
    config=plan(tmp_path)
    adapter=Mock()
    adapter.preflight.return_value={'port':16702,'widget_port':18702}
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    result=remote.provision(config)
    assert not result['ok']
    adapter.register.assert_not_called()
    assert not Path(config['state_dir']).exists()


def test_checkpoint_before_registration_and_allocation_race_blocks(tmp_path,monkeypatch):
    import codex_config_safe
    config=plan(tmp_path)
    adapter=Mock()
    adapter.preflight.return_value={'port':26768,'widget_port':28701}
    events=[]
    monkeypatch.setattr(codex_config_safe,'ensure_codex_config_safe',lambda:events.append('checkpoint'))
    def register(_):
        events.append('register')
        return {'port':16702,'widget_port':18702}
    adapter.register.side_effect=register
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    monkeypatch.setattr(remote,'install_services',lambda _:events.append('install'))
    result=remote.provision(config)
    assert events==['checkpoint','register']
    assert not result['ok']
    assert any('slot changed' in x for x in result['blockers'])


def test_windows_tasks_are_persistent_interactive_and_guard_actions(tmp_path,monkeypatch):
    config=plan(tmp_path,'Windows')
    calls=[]
    monkeypatch.setattr(remote,'checked',lambda args:calls.append(args))
    remote.install_services(config)
    assert len(calls)==4
    for argv in calls:
        script=argv[-1]
        assert '-AtLogOn' in script
        assert '-LogonType Interactive' in script
        assert '-ExecutionTimeLimit ([TimeSpan]::Zero)' in script
        assert 'Existing scheduled task action differs' in script
        assert '$ErrorActionPreference="Stop"' in script


def test_successful_provision_and_rerun_preserve_private_credentials(tmp_path,monkeypatch):
    import codex_config_safe
    token='node-token-'+'x'*32
    access='fleet-access-'+'y'*32
    config=remote.build_plan('pc2',hub='ubuntu@hub.example.com',home=tmp_path,orca='/opt/orca-ide',
        identity_file=str(tmp_path/'identity key'),registration_token=token,
        fleet_access_token=access,registry_url='https://hub.example.com',hub_project='/srv/fleet',
        hub_db='/srv/fleet/fleet.db',hub_python='/srv/fleet/.venv/bin/python',hub_probe_url='http://127.0.0.1:8000')
    route={'port':16702,'widget_port':18702,'orca_url':'wss://hub.example.com/orca/pc2/','widget_url':'https://hub.example.com/orca/pc2/widget/'}
    adapter=Mock()
    adapter.preflight.return_value=route
    adapter.register.return_value=route
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    monkeypatch.setattr(remote,'local_preflight',lambda _:[])
    monkeypatch.setattr(codex_config_safe,'ensure_codex_config_safe',lambda:None)
    monkeypatch.setattr(remote,'install_services',lambda _:None)
    monkeypatch.setattr(remote,'wait_for_pairing',lambda _:True)
    monkeypatch.setattr(remote,'node_urls',lambda *args:{'fleet_url':'https://hub.example.com/orca/pc2/'})
    first=remote.provision(config)
    assert first['ok'] and first['pairing']=='available'
    saved=Path(config['state_dir'])/'config.json'
    private=json.loads(saved.read_text())
    assert private['node_token']==token
    assert private['fleet_access_token']==access
    assert '127.0.0.1:16702:127.0.0.1:6768' in private['tunnel_command']
    assert '127.0.0.1:18702:127.0.0.1:18765' in private['tunnel_command']
    assert token not in json.dumps(first) and access not in json.dumps(first)
    if os.name!='nt':
        assert saved.stat().st_mode & 0o777==0o600
    again=remote.build_plan('pc2',hub='ubuntu@hub.example.com',home=tmp_path,orca='/opt/orca-ide')
    second=remote.provision(again)
    assert second['ok']
    for key in ('node_token','identity_file','registry_url','hub_project','hub_db','hub_python','hub_probe_url','fleet_access_token'):
        assert again[key]==private[key]
    assert again['tunnel_command'][1:3]==['-i',str(tmp_path/'identity key')]
    assert token not in json.dumps(second) and access not in json.dumps(second)


def test_existing_credential_mismatch_blocks_without_hub_call(tmp_path,monkeypatch):
    config=plan(tmp_path)
    saved=Path(config['state_dir'])/'config.json'
    remote.atomic_private(saved,json.dumps({'node_token':'old-secret-'+'x'*32}))
    config['registration_token']='different-secret-'+'y'*32
    adapter=Mock()
    monkeypatch.setattr(remote.importlib,'import_module',lambda _:adapter)
    result=remote.provision(config)
    assert not result['ok']
    adapter.preflight.assert_not_called()
    assert 'different-secret' not in json.dumps(result)
    assert json.loads(saved.read_text())['node_token'].startswith('old-secret')


def test_recursive_public_redaction():
    data={'node_token':'abc-secret','nested':[{'fleet_access_token':'fleet-secret','message':'abc-secret and fleet-secret'}]}
    result=remote._public(data)
    assert result=={'nested':[{'message':'[redacted] and [redacted]'}]}
    assert data['node_token']=='abc-secret'


def test_private_write_ignores_predictable_temp_and_rejects_symlink(tmp_path):
    target=tmp_path/'target'
    target.write_text('keep')
    link=tmp_path/'config.json'
    try:
        link.symlink_to(target)
    except (OSError,NotImplementedError):
        pytest.skip('Symlinks unavailable on this platform/account')
    with pytest.raises(ValueError,match='nonregular'):
        remote.atomic_private(link,'new')
    with pytest.raises(ValueError,match='nonregular'):
        remote.read_private_json(link)
    assert target.read_text()=='keep'
    link.unlink()
    (tmp_path/'config.json.tmp').symlink_to(target)
    remote.atomic_private(link,'new')
    assert link.read_text()=='new'
    assert target.read_text()=='keep'


def test_temporary_file_removed_when_replace_fails(tmp_path,monkeypatch):
    def fail(*args):
        raise OSError('simulated failure')
    monkeypatch.setattr(remote.os,'replace',fail)
    with pytest.raises(OSError):
        remote.atomic_private(tmp_path/'config.json','{}')
    assert list(tmp_path.iterdir())==[]
