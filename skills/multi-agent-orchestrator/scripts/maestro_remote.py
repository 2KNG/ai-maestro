#!/usr/bin/env python3
"""Persistent remote node setup. No downloaded scripts or implicit service takeover."""
from __future__ import annotations
import argparse
import importlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

import remote_config

SCRIPT = Path(__file__).resolve()
MARKER = '# Managed by ai-maestro remote v1'


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, **kwargs)


def checked(argv):
    result = run(argv)
    if result.returncode:
        # External output can contain pairing credentials. Keep it out of reports.
        raise RuntimeError(f'Command failed ({result.returncode}): {argv[0]} {argv[1:3]}')
    return result


def validate(node_id, port, hub):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', node_id):
        raise ValueError('node-id must be 1-48 lowercase letters, digits or hyphens')
    if not 1024 <= port <= 65534:
        raise ValueError('port must be 1024-65534')
    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*', hub):
        raise ValueError('hub must be a plain SSH host or user@host')
    if port in (6768,18765):
        raise ValueError('hub ports must differ from the fixed local service ports')


def discover_orca(system):
    # Never invokes the Linux GNOME screen reader /usr/bin/orca.
    import orca_bridge
    return orca_bridge.discover(system)


def regular_snapshot(path):
    try:
        info=Path(path).lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Refusing nonregular configuration file')
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)


def read_private_json(path):
    regular_snapshot(path)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'r',encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Refusing nonregular configuration file')
        return json.load(stream)


def atomic_private(path, data):
    path=Path(path)
    original=regular_snapshot(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if regular_snapshot(path)!=original:
            raise RuntimeError('Configuration changed concurrently; retry')
        os.replace(temporary,path)
        if os.name!='nt':
            directory=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


SECRET_KEYS={'registration_token','node_token','widget_password','fleet_access_token','pairing_code','deviceToken'}


def _public(value):
    def collect(item):
        if isinstance(item,dict):
            return [str(v) for k,v in item.items() if k in SECRET_KEYS and v]+[secret for v in item.values() for secret in collect(v)]
        if isinstance(item,list):
            return [secret for v in item for secret in collect(v)]
        return []
    secrets_to_hide=collect(value)
    def redact(item):
        if isinstance(item,dict):
            return {k:redact(v) for k,v in item.items() if k not in SECRET_KEYS}
        if isinstance(item,list):
            return [redact(v) for v in item]
        if isinstance(item,str):
            for secret in secrets_to_hide:
                item=item.replace(secret,'[redacted]')
        return item
    return redact(value)


def rebuild_tunnel(config):
    argv=[shutil.which('ssh') or 'ssh','-NT','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
          '-R',f"127.0.0.1:{config['port']}:127.0.0.1:6768",
          '-R',f"127.0.0.1:{config['widget_port']}:127.0.0.1:18765",config['hub']]
    if config.get('identity_file'):
        config['identity_file']=str(Path(config['identity_file']).expanduser().resolve())
        argv[1:1]=['-i',config['identity_file']]
    config['tunnel_command']=argv


def build_plan(node_id, port=None, hub=None, system=None,
               home=None, orca=None, orca_url=None, identity_file=None, widget_port=None, registration_token=None, **hub_options):
    if not hub:
        raise ValueError('No hub configured: run `maestro setup` or pass --hub <your-server-domain-or-ip>')
    requested_port, requested_widget_port = port, widget_port
    port = 16701 if port is None else port
    validate(node_id, port, hub)
    widget_port = widget_port if widget_port is not None else 18701 + port - 16701
    if not 1024 <= widget_port <= 65535 or widget_port in (6768,18765,port):
        raise ValueError('widget-port must be a distinct unprivileged hub port')
    system = system or platform.system()
    if system not in ('Linux', 'Darwin', 'Windows'):
        raise ValueError(f'Unsupported platform: {system}')
    home = Path(home or Path.home())
    state = home / '.local' / 'state' / 'ai-maestro' / 'remote' / node_id
    orca = orca or discover_orca(system)
    config = dict(node_id=node_id, port=port, widget_port=widget_port, hub=hub,
                  requested_port=requested_port, requested_widget_port=requested_widget_port,
                  system=system, state_dir=str(state), orca=orca,
                  orca_url=orca_url, script=str(SCRIPT), python=sys.executable, identity_file=identity_file, registration_token=registration_token)
    config.update({k:v for k,v in hub_options.items() if v is not None})
    rebuild_tunnel(config)
    config['services'] = []
    for role in ('orca', 'widget', 'tunnel', 'heartbeat'):
        name = f'ai-maestro-remote-{node_id}-{role}'
        argv = [sys.executable, str(SCRIPT), '_run', '--config', str(state/'config.json'), '--role', role]
        item = dict(name=name, role=role, argv=argv)
        if system == 'Linux':
            # systemd uses its own token parser, not a shell. Escape specifiers too.
            command = ' '.join('"' + a.replace('\\','\\\\').replace('"','\\"').replace('%','%%') + '"' for a in argv)
            item.update(path=str(home/'.config/systemd/user'/f'{name}.service'), content=
                f'{MARKER}\n[Unit]\nDescription=ai-maestro remote {role}\nAfter=network-online.target\n'
                f'[Service]\nExecStart={command}\nRestart=on-failure\nRestartSec=5\nUMask=0077\n'
                '[Install]\nWantedBy=default.target\n')
        elif system == 'Darwin':
            item.update(path=str(home/'Library/LaunchAgents'/f'{name}.plist'), content=plistlib.dumps(dict(
                Label=name, ProgramArguments=argv, RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False),
                ThrottleInterval=5, StandardOutPath=str(state/f'{role}.log'),
                StandardErrorPath=str(state/f'{role}.log'), MaestroManaged=True)).decode())
        else:
            # A task runs in the logged-in user's desktop session, required for computer control.
            item.update(path=str(state/f'{role}.task.json'), content=json.dumps(dict(managed=MARKER,argv=argv)))
        config['services'].append(item)
    return config


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.3):
            return True
    except OSError:
        return False


def local_preflight(config):
    errors = []
    if not config['orca'] or not Path(config['orca']).is_file():
        errors.append('Orca CLI executable missing; install Orca or set ORCA_CLI_COMMAND to its full path')
    if not shutil.which('ssh'):
        errors.append('OpenSSH client missing')
    manager = {'Linux':'systemctl', 'Darwin':'launchctl', 'Windows':'powershell.exe'}[config['system']]
    if not shutil.which(manager):
        errors.append(f'{manager} missing')
    for item in config['services']:
        path = Path(item['path'])
        if path.exists() and path.read_text(encoding='utf-8') != item['content']:
            errors.append(f'Refusing to replace differing service configuration: {path}')
    existing = Path(config['state_dir'])/'config.json'
    if existing.exists():
        old = read_private_json(existing)
        if any(old.get(k) != config.get(k) for k in ('node_id','hub','port','widget_port','orca_url','orca','identity_file')):
            errors.append('Existing node configuration differs; migrate explicitly before rerunning')
    # Never take over another Orca profile/server or an unrelated widget service.
    for port, role in ((6768,'orca'),(18765,'widget')):
        owned = existing.exists() and any(Path(s['path']).exists() for s in config['services'] if s['role']==role)
        if port_open(port) and not owned:
            unit=reusable_unit(role,config.get('orca_service')) if config['system']=='Linux' else None
            if unit:
                for item in config['services']:
                    if item['role']==role:
                        item['reused_unit']=unit
                continue
            errors.append(f'Local port {port} already occupied; existing {role} is preserved')
    if config['system']=='Windows':
        for item in config['services']:
            name="'"+item['name']+"'"
            probe=run(['powershell.exe','-NoProfile','-Command',f'if (Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue) {{exit 0}} else {{exit 1}}'])
            if probe.returncode==0 and not Path(item['path']).exists():
                errors.append(f"Existing scheduled task is not owned: {item['name']}")
    if config['system']=='Linux':
        result = run(['systemctl','--user','show-environment'])
        if result.returncode:
            errors.append('No systemd user manager; log into a user session first')
    return errors


def reusable_unit(role, orca_service=None):
    """Active user unit that already serves the role's fixed local port, if owned by us or Orca."""
    if role=='widget':
        unit, marker = 'agy-widget-bridge.service', str(SCRIPT.parent/'widget_bridge_daemon.py')
    else:
        # Orca's own remote-control unit (linked by `maestro setup` step 2).
        unit, marker = orca_service or 'orca-remote.service', '6768'
    probe=run(['systemctl','--user','show',unit,'--property=ActiveState,ExecStart'])
    if probe.returncode==0 and 'ActiveState=active' in probe.stdout and marker in probe.stdout \
            and (role=='widget' or 'orca' in probe.stdout):
        return unit
    return None


def install_services(config):
    for item in config['services']:
        if item.get('reused_unit'):
            continue
        path = Path(item['path'])
        if not path.exists():
            atomic_private(path, item['content'])
    if config['system']=='Linux':
        checked(['systemctl','--user','daemon-reload'])
        for item in config['services']:
            if item.get('reused_unit'):
                continue
            checked(['systemctl','--user','enable','--now',item['name']+'.service'])
    elif config['system']=='Darwin':
        domain=f'gui/{os.getuid()}'
        for item in config['services']:
            if run(['launchctl','print',domain+'/'+item['name']]).returncode:
                checked(['launchctl','bootstrap',domain,item['path']])
    else:
        for item in config['services']:
            # PowerShell single quoted literals escape a quote by doubling it.
            quote=lambda s: "'"+s.replace("'","''")+"'"
            executable=quote(item['argv'][0])
            arguments=quote(subprocess.list2cmdline(item['argv'][1:]))
            name=quote(item['name'])
            script=(f'$ErrorActionPreference="Stop"; $t=Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; '
                    f'if ($t -and ($t.Actions.Count -ne 1 -or $t.Actions[0].Execute -ne {executable} -or $t.Actions[0].Arguments -ne {arguments})) {{ throw "Existing scheduled task action differs" }}; '
                    f'if (!$t) {{ $a=New-ScheduledTaskAction -Execute {executable} -Argument {arguments}; '
                    '$tr=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name); '
                    '$p=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive; '
                    '$s=New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero); '
                    f'Register-ScheduledTask -TaskName {name} -Action $a -Trigger $tr -Principal $p -Settings $s | Out-Null }}; '
                    f'Start-ScheduledTask -TaskName {name}')
            checked(['powershell.exe','-NoProfile','-NonInteractive','-Command',script])


def provision(config, dry_run=False):
    saved=Path(config['state_dir'])/'config.json'
    old=read_private_json(saved) if regular_snapshot(saved) is not None else {}
    provided=config.get('registration_token')
    if provided and old.get('node_token') and provided != old['node_token']:
        return {'ok':False,'dry_run':dry_run,'blockers':['Provided node credential differs from existing configuration'],'plan':_public(config)}
    config['node_token']=provided or old.get('node_token') or secrets.token_urlsafe(32)
    config['registration_token']=provided or old.get('registration_token')
    for key in ('identity_file','registry_url','hub_project','hub_db','hub_python','hub_probe_url','fleet_access_token'):
        if config.get(key) is None and old.get(key) is not None:
            config[key]=old[key]
    rebuild_tunnel(config)
    try:
        adapter=importlib.import_module('maestro_remote_hub')
    except ImportError:
        adapter=None
    public_config=_public(config)
    result={'ok':False,'dry_run':dry_run,'plan':public_config,'blockers':[]}
    if adapter is None:
        result['blockers'].append('Hub route adapter unavailable; no services changed')
    else:
        try:
            route=adapter.preflight(config)
            for requested, allocated in (('requested_port','port'),('requested_widget_port','widget_port')):
                if config.get(requested) is not None and config[requested] != route.get(allocated,config[allocated]):
                    raise ValueError('Requested port differs from allocated fleet slot')
            config.update(route)
            rebuild_tunnel(config)
            public_config.clear()
            public_config.update(_public(config))
        except Exception as exc:
            result['blockers'].append(f'Hub preflight failed: {type(exc).__name__}')
    result['blockers'].extend(local_preflight(config))
    address=config.get('orca_url')
    if not address or urlsplit(address).scheme != 'wss':
        result['blockers'].append('Verified TLS Orca hub route required (wss://)')
    if dry_run or result['blockers']:
        return result
    # Checkpoint before Orca can synchronize its agent configuration.
    try:
        from codex_config_safe import ensure_codex_config_safe
        ensure_codex_config_safe()
    except Exception as exc:
        result['blockers'].append(f'Codex configuration checkpoint failed: {type(exc).__name__}')
        return result
    atomic_private(saved, json.dumps(config,indent=2))
    registered=adapter.register(config)
    if any(registered.get(k) != config[k] for k in ('port','widget_port')):
        config.update(registered)
        rebuild_tunnel(config)
        atomic_private(saved,json.dumps(config,indent=2))
        result['blockers'].append('Fleet slot changed during registration; saved current allocation, rerun setup before starting services')
        return result
    install_services(config)
    reused={s['role']:s['reused_unit'] for s in config['services'] if s.get('reused_unit')}
    if reused:
        result['reused_services']=reused
        if 'orca' in reused:
            result['pairing_note']=f"Orca runs under {reused['orca']}; its pairing address is set by that unit"
    result.update(ok=True, state='services_started', pairing='available' if wait_for_pairing(config['state_dir']) else 'pending',
                  **node_urls(config['node_id'],config.get('orca_url')),
                  note='Service start is not end-to-end desktop or browser verification')
    return result


def capture_pairing(line, state):
    # Actual Orca CLI contract; never manufacture a pairing code or URL.
    path=Path(state)/'pairing.json'
    if line.startswith('Pairing URL: orca://pair?'):
        key, value='pairing_code', line[len('Pairing URL: '):].strip()
    elif line.startswith('Web client URL: https://'):
        key, value='web_url', line[len('Web client URL: '):].strip()
    else:
        return False
    data=read_private_json(path) if regular_snapshot(path) is not None else {}
    data.update({key:value,'created_at':time.time()})
    atomic_private(path, json.dumps(data))
    return True


def run_role(config, role):
    if role=='heartbeat':
        adapter=importlib.import_module('maestro_remote_hub')
        return adapter.run_node(config)
    if role=='widget':
        import widget_bridge_daemon as bridge
        server=bridge.http.server.ThreadingHTTPServer(('127.0.0.1',18765),bridge.BridgeHandler)
        threading.Thread(target=bridge.widget_sync_loop,daemon=True).start()
        server.serve_forever()
        return 0
    if role=='orca':
        (Path(config['state_dir'])/'pairing.json').unlink(missing_ok=True)
    argv=config['tunnel_command'] if role=='tunnel' else [config['orca'],'serve','--port','6768',
        '--pairing-address',config['orca_url'],'--grant-desktop-control']
    child=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    def stop_child(signum, frame):
        if child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop_child)
    try:
        for line in child.stdout:
            if role=='orca':
                capture_pairing(line,config['state_dir'])
            # Do not emit Orca stdout: QR/web-client lines may contain bearer offers.
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=20)


def node_urls(node_id, orca_url=None):
    addresses=[]
    try:
        addresses=[item[4][0] for item in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET)]
        if platform.system()=='Linux':
            probe=run(['hostname','-I'])
            if probe.returncode==0:
                addresses=probe.stdout.split()+addresses
    except (OSError,subprocess.SubprocessError):
        pass
    lan=None
    for candidate in addresses:
        try:
            ip=ipaddress.ip_address(candidate)
            if ip.version==4 and not ip.is_loopback and not ip.is_unspecified:
                lan=str(ip)
                break
        except ValueError:
            continue
    host=urlsplit(orca_url).netloc if orca_url else None
    if not host:
        try:
            origin=remote_config.resolve().get('registry_url')
        except (OSError,ValueError):
            origin=None
        host=urlsplit(origin).netloc if origin else None
    return {'local_lan_url':f'http://{lan}:6768/' if lan else None,
            'fleet_url':f'https://{host}/orca/{node_id}/' if host else None,
            'fleet_home_url':f'https://{host}/orca' if host else None}


def wait_for_pairing(state, timeout=8):
    deadline=time.monotonic()+timeout
    path=Path(state)/'pairing.json'
    while True:
        try:
            data=json.loads(path.read_text())
            if str(data.get('pairing_code','')).startswith('orca://pair?'):
                return True
        except (OSError,ValueError):
            pass
        if time.monotonic()>=deadline:
            return False
        time.sleep(.25)


def service_active(system, name):
    try:
        if system=='Linux':
            return run(['systemctl','--user','is-active',name]).returncode==0
        if system=='Darwin':
            return run(['launchctl','print',f'gui/{os.getuid()}/'+name]).returncode==0
        q="'"+name.replace("'","''")+"'"
        return run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            f'$ErrorActionPreference="Stop"; if ((Get-ScheduledTask -TaskName {q}).State -eq "Running") {{exit 0}} else {{exit 1}}']).returncode==0
    except (OSError,subprocess.SubprocessError):
        return False


def remote_status(node_id, home=None):
    validate(node_id,16701,'localhost')
    state=Path(home or Path.home())/'.local/state/ai-maestro/remote'/node_id
    path=state/'config.json'
    config=read_private_json(path) if regular_snapshot(path) is not None else {}
    system=config.get('system',platform.system())
    statuses=[]
    for item in config.get('services',[]):
        name=item.get('reused_unit',item['name']+'.service') if system=='Linux' else item['name']
        statuses.append({'role':item['role'],'name':name,'active':service_active(system,name)})
    legacy=[]
    if system=='Linux':
        for name, role, port in [('orca-remote.service','orca',6768),
                                 ('agy-widget-bridge.service','widget',18765),
                                 ('rtc2kng-node.service','node',None),
                                 ('rtc2kng-tunnel.service','tunnel',None)]:
            legacy.append({'name':name,'role':role,'local_port':port,'active':service_active(system,name)})
    # Nodes enrolled by rtc2kng itself run outside maestro's state dir: healthy when
    # Orca + widget are up and either the fleet node daemon or the SSH tunnel carries them.
    active_roles={s['role'] for s in legacy if s['active']}
    legacy_ok=not statuses and {'orca','widget'}<=active_roles and bool({'node','tunnel'}&active_roles)
    pairing=wait_for_pairing(state,timeout=0)
    try:
        import orca_bridge
        snap=orca_bridge.snapshot()
        orca={'available':snap.get('available',False),'running':snap.get('running',False),
              'summary':orca_bridge.summary_line(snap),'worktrees':snap.get('worktrees',0),
              'terminals':[{k:t.get(k) for k in ('handle','title','agent','worktree','branch','connected')}
                           for t in snap.get('terminals',[])]}
    except Exception as exc:
        orca={'available':False,'error':type(exc).__name__}
    try:
        hub=remote_config.public_view(remote_config.resolve())
    except (OSError,ValueError):
        hub={}
    return {'ok':(bool(statuses) and all(s['active'] for s in statuses)) or legacy_ok,'node_id':node_id,
            'state':'installed' if config else 'legacy' if legacy_ok else 'not_installed',
            'services':statuses,'legacy_services':legacy,
            'local_ports':{'orca':port_open(6768),'widget':port_open(18765)},
            'pairing_available':pairing,'pairing':'available' if pairing else 'pending',
            'orca_url':config.get('orca_url'),'widget_url':config.get('widget_url'),
            'hub':{k:hub[k] for k in ('hub','registry_url','identity_file') if k in hub},
            'orca':orca,
            **node_urls(node_id,config.get('orca_url')),
            'verification':'Service and TCP reachability only; URLs are candidates, not browser verification'}


def default_node_id():
    return socket.gethostname().lower().replace('_','-').split('.')[0]


def resolve_settings(args, saved=None):
    """CLI > MAESTRO_* env > ~/.config/ai-maestro/remote.json. Returns (settings, updates_to_save)."""
    saved=remote_config.resolve() if saved is None else saved
    settings={k:saved.get(k) for k in remote_config.FIELDS}
    updates={}
    if args.hub:
        updates.update(remote_config.apply_server(args.hub,args.ssh_user,saved))
        settings.update(updates)
    for key in ('registry_url','identity_file','hub_project','hub_db','hub_python','hub_probe_url'):
        value=getattr(args,key,None)
        if value:
            if key=='identity_file':
                value=str(Path(value).expanduser())
            settings[key]=updates[key]=value
    if args.node_id:
        settings['node_id']=args.node_id
    settings['node_id']=settings.get('node_id') or default_node_id()
    return settings, updates


def fleet_nodes(settings, fleet_access_token=None, timeout=8):
    """Nodes currently registered at the hub (only real entries; no empty slots)."""
    import urllib.request
    origin=settings.get('hub_probe_url') or settings.get('registry_url')
    if not origin:
        raise ValueError('No registry origin configured: run `maestro remote configure --hub <domain>`')
    headers={'Authorization':'Bearer '+fleet_access_token} if fleet_access_token else {}
    request=urllib.request.Request(origin.rstrip('/')+'/api/orca/nodes',headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            nodes=json.loads(response.read(1<<20)).get('nodes',[])
    except (OSError,ValueError) as exc:
        raise RuntimeError(f'Fleet registry unreachable: {type(exc).__name__}') from None
    keep=('node_id','slot','orca_port','maestro_port','online','last_seen','status','hostname')
    rows=[{k:n[k] for k in keep if k in n} for n in nodes if isinstance(n,dict) and n.get('node_id')]
    return sorted(rows,key=lambda n:(n.get('slot') is None,n.get('slot') or 0,n['node_id']))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','up','install-node','configure','nodes','_run'],nargs='?',default='status')
    parser.add_argument('--node-id')
    parser.add_argument('--port',type=int)
    parser.add_argument('--hub',help='Hub server: domain, IP, user@host or https:// URL (saved to ~/.config/ai-maestro/remote.json)')
    parser.add_argument('--ssh-user',help='SSH user when --hub has none (default ubuntu)')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--identity-file')
    parser.add_argument('--widget-port',type=int)
    parser.add_argument('--registration-token',default=os.environ.get('MAESTRO_REGISTRATION_TOKEN') or os.environ.get('RTC2KNG_REGISTRATION_TOKEN'))
    parser.add_argument('--show-pairing',action='store_true')
    for option in ('registry-url','hub-project','hub-db','hub-python','hub-probe-url'):
        parser.add_argument('--'+option)
    parser.add_argument('--fleet-access-token',default=os.environ.get('MAESTRO_FLEET_ACCESS_TOKEN') or os.environ.get('RTC2KNG_FLEET_ACCESS_TOKEN'))
    parser.add_argument('--config')
    parser.add_argument('--role',choices=['orca','widget','tunnel','heartbeat'])
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.action=='_run':
            if not args.config or not args.role:
                parser.error('_run requires --config and --role')
            return run_role(read_private_json(args.config),args.role)
        settings,updates=resolve_settings(args)
        if args.action=='status':
            result=remote_status(settings['node_id'])
        elif args.action=='configure':
            if not args.hub and not updates:
                parser.error('configure requires --hub <your-server-domain-or-ip>')
            if args.node_id:
                updates['node_id']=args.node_id
            path=remote_config.config_path() if args.dry_run else remote_config.save(updates)
            result={'ok':True,'dry_run':args.dry_run,'config_file':str(path),
                    'settings':remote_config.public_view({k:v for k,v in settings.items() if v}),
                    **node_urls(settings['node_id'],None if settings.get('registry_url') is None else
                                'wss'+settings['registry_url'][5:]+'/orca/'+settings['node_id']+'/')}
            if not settings.get('registry_url'):
                result['next']='IP hubs need an HTTPS domain for wss pairing: add --registry-url https://<your-domain>'
        elif args.action=='nodes':
            nodes=fleet_nodes(settings,args.fleet_access_token)
            result={'ok':True,'registry_url':settings.get('registry_url'),'count':len(nodes),'nodes':nodes}
        else:
            if updates and not args.dry_run:
                remote_config.save(updates)
            result=provision(build_plan(settings['node_id'],args.port,settings.get('hub'),
                       identity_file=settings.get('identity_file'),widget_port=args.widget_port,registration_token=args.registration_token,
                       registry_url=settings.get('registry_url'),hub_project=settings.get('hub_project'),hub_db=settings.get('hub_db'),
                       hub_python=settings.get('hub_python'),hub_probe_url=settings.get('hub_probe_url'),
                       orca_service=settings.get('orca_service'),
                       fleet_access_token=args.fleet_access_token),dry_run=args.dry_run)
        if args.show_pairing and not args.dry_run and args.action in ('status','up','install-node'):
            state=Path.home()/'.local/state/ai-maestro/remote'/settings['node_id']/'pairing.json'
            if state.exists():
                result['pairing']=read_private_json(state)
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return 0 if result.get('ok') or args.dry_run else 1
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
