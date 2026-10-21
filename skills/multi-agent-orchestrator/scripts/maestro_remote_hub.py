#!/usr/bin/env python3
"""Adapter for an existing fleet hub Registry and heartbeat HTTP contract.

The hub address, registry origin and SSH key come from user-local settings
(``~/.config/ai-maestro/remote.json`` or MAESTRO_* environment variables); the
repository ships none. No nginx configuration, hub code installation, or
service lifecycle changes.

Nodes scale 1..N: each enrolled node takes the lowest free slot in the hub
registry and its tunnel ports are ORCA_PORT_BASE+slot / WIDGET_PORT_BASE+slot.
Only registered nodes exist; there are no pre-allocated placeholder slots.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import platform
import socket
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

ORCA_PORT_BASE = 16701
WIDGET_PORT_BASE = 18701
# Port arithmetic bound only (orca ports must stay below the widget port range).
MAX_SLOTS = WIDGET_PORT_BASE - ORCA_PORT_BASE

REMOTE_CODE = r'''
import contextlib, itertools, fcntl, hashlib, hmac, importlib, json, os, sqlite3, sys, urllib.request
from pathlib import Path
c = json.load(sys.stdin)
project = Path(c['hub_project']).resolve()
db = Path(c['hub_db'])
if not db.is_absolute(): db = project / db
db = db.resolve()
if not (project / 'fleet.py').is_file() or not db.is_file():
    raise RuntimeError('fleet hub code/database is not deployed')
sys.path.insert(0, str(project))
module = importlib.import_module('fleet')
if not hasattr(module, 'Registry'):
    raise RuntimeError('Unsupported fleet registry contract')
# Verify the configured database matches the currently serving registry before writes.
headers = {}
if c.get('fleet_access_token'): headers['Authorization'] = 'Bearer ' + c['fleet_access_token']
request = urllib.request.Request(c['hub_probe_url'].rstrip('/') + '/api/orca/nodes', headers=headers)
with urllib.request.urlopen(request, timeout=10) as response:
    served = json.loads(response.read(131072))['nodes']
@contextlib.contextmanager
def guard():
    if c['action'] == 'register':
        with open(str(db) + '.maestro-enroll.lock', 'a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
    else: yield
with guard():
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    rows = con.execute('SELECT id,token,slot FROM nodes').fetchall()
    con.close()
    if {(x[0],x[2]) for x in rows} != {(x['node_id'],x['slot']) for x in served}:
        raise RuntimeError('Configured database differs from serving fleet registry; retry or correct hub_db')
    # The persistent node credential identifies this machine: it keeps its ID on rerun.
    # Another machine asking for a taken name (usually its hostname) gets name_2, name_3, ...
    digest = hashlib.sha256(c['node_token'].encode()).hexdigest()
    row = next((x for x in rows if hmac.compare_digest(x[1], digest)), None)
    if row:
        node_id = row[0]
        slot = row[2]
    else:
        taken = {x[0] for x in rows}
        base = c['node_id']
        node_id = next(n for n in itertools.chain([base], (base[:c.get('max_id',48)-len('_%d' % i)] + '_%d' % i
                       for i in itertools.count(2))) if n not in taken)
        used = {x[2] for x in rows if x[2] is not None}
        slot = next(i for i in itertools.count() if i not in used)
    if slot is None or not 0 <= slot < c['max_slots']:
        raise RuntimeError('Fleet has no available tunnel slot')
    if row is None:
        occupied = set()
        for table in ('/proc/net/tcp', '/proc/net/tcp6'):
            if Path(table).exists():
                for line in Path(table).read_text().splitlines()[1:]:
                    fields = line.split()
                    if fields[3] == '0A': occupied.add(int(fields[1].rsplit(':',1)[1],16))
        if occupied.intersection((c['orca_port_base']+slot,c['widget_port_base']+slot)):
            raise RuntimeError('Allocated hub tunnel port is already occupied')
    if c['action'] == 'register' and row is None:
        os.chdir(project)
        sys.path.insert(0, str(project))
        module = importlib.import_module('fleet')
        registry = module.Registry(str(db), origin=c['registry_url'])
        try:
            registry.provision(node_id, c['node_token'])
            node = registry.get(node_id)
            if node['slot'] != slot:
                # Another enrollment tool may not honor our file lock.
                raise RuntimeError('Slot changed during enrollment; retry to obtain current allocation')
        finally: registry.db.close()
    print(json.dumps({'node_id':node_id, 'slot':slot, 'port':c['orca_port_base']+slot, 'widget_port':c['widget_port_base']+slot}))
'''


NODE_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
MAX_ID = 48


def settings(config):
    c = dict(config)
    if not NODE_ID.fullmatch(c.get('node_id', '')):
        raise ValueError('Invalid node id')
    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*', c.get('hub', '')):
        raise ValueError('Invalid SSH hub')
    token = c.get('node_token', '')
    if not isinstance(token, str) or len(token) < 32 or any(x.isspace() for x in token):
        raise ValueError('A persistent node_token of at least 32 characters is required')
    if not c.get('registry_url'):
        raise ValueError('registry_url is required: set the hub HTTPS domain with `maestro remote configure --hub <domain>` '
                         'or --registry-url https://<your-server-domain>')
    url = urlsplit(c['registry_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('registry_url must be an HTTPS origin')
    c['registry_url'] = c['registry_url'].rstrip('/')
    c.setdefault('hub_project', '/home/ubuntu/dev/rtc2kng')
    c.setdefault('hub_db', 'var/fleet.sqlite3')
    # Use the public route by default; deployments can run Fleet separately from
    # the legacy application without sharing its internal listener port.
    c.setdefault('hub_probe_url', c['registry_url'])
    c.setdefault('hub_python', None)
    if not str(c['hub_project']).startswith('/'):
        raise ValueError('hub_project must be absolute')
    return c


def ssh_request(config, action):
    c = settings(config)
    payload = {k:c[k] for k in ('node_id','node_token','registry_url','hub_project','hub_db','hub_probe_url')}
    payload.update(action=action, orca_port_base=ORCA_PORT_BASE, widget_port_base=WIDGET_PORT_BASE, max_slots=MAX_SLOTS, max_id=MAX_ID)
    if c.get('fleet_access_token'):
        payload['fleet_access_token'] = c['fleet_access_token']
    argv = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10']
    if c.get('identity_file'):
        argv += ['-i', str(Path(c['identity_file']).expanduser())]
    if c['hub_python']:
        command = shlex.join([c['hub_python'], '-c', REMOTE_CODE])
    else:
        candidates = [str(Path(c['hub_project'])/'.venv/bin/python'), str(Path(c['hub_project'])/'venv/bin/python')]
        command = ''.join('if test -x '+shlex.quote(p)+'; then exec '+shlex.join([p,'-c',REMOTE_CODE])+'; fi; ' for p in candidates)
        command += 'exec '+shlex.join(['python3','-c',REMOTE_CODE])
    argv += [c['hub'], command]
    # Credentials are stdin only. Do not include remote stderr in errors.
    result = subprocess.run(argv, input=json.dumps(payload), text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Fleet hub operation failed; verify deployed fleet API, hub_db, Python dependencies and node ownership')
    try:
        allocation = json.loads(result.stdout)
        slot = allocation['slot']
        if type(slot) is not int or not 0 <= slot < MAX_SLOTS:
            raise ValueError()
        if allocation['port'] != ORCA_PORT_BASE+slot or allocation['widget_port'] != WIDGET_PORT_BASE+slot:
            raise ValueError()
        # Older hub scripts omit node_id: the requested ID was used unchanged.
        node_id = allocation.get('node_id', c['node_id'])
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Invalid fleet allocation response') from None
    for key, allocated in (('requested_port', allocation['port']), ('requested_widget_port', allocation['widget_port'])):
        if c.get(key) is not None and c[key] != allocated:
            raise RuntimeError('Requested tunnel port differs from fleet allocation')
    base = c['registry_url']
    return dict(node_id=node_id, port=allocation['port'], widget_port=allocation['widget_port'],
                registry_url=base, orca_url='wss'+base[5:]+'/orca/'+node_id+'/',
                widget_url=base+'/orca/'+node_id+'-maestro/')


def preflight(config):
    """Read-only remote discovery; refuses undeployed or conflicting registries."""
    return ssh_request(config, 'preflight')


def register(config):
    """Enroll through the actual deployed Registry; idempotent for same token."""
    return ssh_request(config, 'register')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def heartbeat(config, telemetry=None, pairing=None):
    c = settings(config)
    payload = {'node_id':c['node_id'], 'telemetry':telemetry or {}}
    if pairing is not None:
        validate_pairing(pairing)
        payload['pairing'] = pairing
    body = json.dumps(payload).encode()
    request = urllib.request.Request(c['registry_url']+'/api/orca/register', data=body,
        headers={'Authorization':'Bearer '+c['node_token'], 'Content-Type':'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            node = json.loads(response.read(65536))
    except (OSError, ValueError):
        raise RuntimeError('Fleet heartbeat failed') from None
    if node.get('orca_port') != c['port'] or node.get('maestro_port') != c['widget_port']:
        raise RuntimeError('Fleet allocation changed; rerun setup before reconnecting')
    return node


def validate_pairing(code):
    try:
        url = urlsplit(code)
        raw = parse_qs(url.query)['code'][0]
        payload = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if (url.scheme != 'orca' or url.netloc != 'pair' or payload.get('v') != 2
                or payload.get('scope') != 'runtime'
                or not all(payload.get(k) for k in ('deviceToken','publicKeyB64','pairedDeviceId'))):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Invalid Orca runtime pairing offer') from None


def private_pairing(config):
    path = Path(config['state_dir'])/'pairing.json'
    try:
        if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
            raise ValueError('Pairing state must be private')
        if path.stat().st_size > 65536:
            raise ValueError('Pairing state is too large')
        data = json.loads(path.read_text(encoding='utf-8'))
        code = data.get('pairing_code')
        validate_pairing(code)
        return code
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid private pairing state') from None


def current_telemetry():
    data = {'hostname':socket.gethostname(), 'os':platform.platform()}
    try:
        import psutil
        memory = psutil.virtual_memory()
        data.update(cpu_percent=psutil.cpu_percent(interval=0.1), memory_percent=memory.percent,
                    memory_total=memory.total, memory_used=memory.used)
        addresses = [a.address for items in psutil.net_if_addrs().values() for a in items
                     if a.family == socket.AF_INET and not a.address.startswith('127.')]
        data['lan_ip'] = ', '.join(sorted(set(addresses)))
    except (ImportError, OSError):
        pass
    return data


def run_node(config):
    """Service loop; failure exits so the platform manager can restart it."""
    while True:
        heartbeat(config, current_telemetry(), private_pairing(config))
        time.sleep(15)
