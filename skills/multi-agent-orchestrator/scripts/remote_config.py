#!/usr/bin/env python3
"""User-local remote hub settings (never stored in the repository).

Resolution order for every field: explicit CLI value > environment variable >
``~/.config/ai-maestro/remote.json``. The public repository ships no hub
address, SSH key path or registry origin.
"""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

DEFAULT_SSH_USER = 'ubuntu'
# Fields persisted to remote.json. Secrets (node tokens) stay in per-node state.
FIELDS = ('hub', 'registry_url', 'identity_file', 'hub_project', 'hub_db', 'hub_python',
          'hub_probe_url', 'node_id', 'orca_service')
ENV = {'hub': 'MAESTRO_REMOTE_HUB', 'identity_file': 'MAESTRO_SSH_KEY',
       'registry_url': 'MAESTRO_REGISTRY_URL', 'node_id': 'MAESTRO_NODE_ID'}
_HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*')
_USER = re.compile(r'[A-Za-z0-9_.-]+')


def config_path() -> Path:
    override = os.environ.get('MAESTRO_REMOTE_CONFIG')
    if override:
        return Path(override).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'ai-maestro' / 'remote.json'


def load(path=None) -> dict:
    """Saved settings only (no environment overlay). Missing file -> {}."""
    path = Path(path) if path else config_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Refusing nonregular remote configuration: {path}')
    data = json.loads(path.read_text(encoding='utf-8') or '{}')
    if not isinstance(data, dict):
        raise ValueError(f'Remote configuration must be a JSON object: {path}')
    return {k: v for k, v in data.items() if k in FIELDS and v not in (None, '')}


def resolve(path=None, environ=None) -> dict:
    """Saved settings overlaid with MAESTRO_* environment variables."""
    environ = os.environ if environ is None else environ
    data = load(path)
    for key, name in ENV.items():
        if environ.get(name):
            data[key] = environ[name]
    return data


def save(updates: dict, path=None) -> Path:
    """Merge ``updates`` into remote.json atomically with 0600 permissions.

    A ``None`` or empty value removes the key.
    """
    path = Path(path) if path else config_path()
    data = load(path)
    for key, value in updates.items():
        if key not in FIELDS:
            continue
        if value in (None, ''):
            data.pop(key, None)
        else:
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_server(address: str, ssh_user: str | None = None) -> dict:
    """Turn what a user types ("hub.example.com", "203.0.113.7", "me@host",
    "https://hub.example.com/") into an SSH target plus registry origin.

    ``registry_url`` is derived only for domain names; an IP address cannot carry
    a publicly trusted TLS certificate, so the caller keeps any saved origin.
    """
    raw = (address or '').strip()
    if not raw:
        raise ValueError('Server address is empty')
    if '://' in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in ('https', 'ssh') or not parsed.hostname:
            raise ValueError('Server URL must be https:// or ssh://')
        user, host = parsed.username, parsed.hostname
    else:
        user, _, host = raw.rpartition('@')
        host = host.strip().strip('/').split('/')[0]
        user = user or None
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if ':' in host and not _is_ip(host):
        host = host.split(':', 1)[0]  # drop an accidental :port
    if not _is_ip(host) and not _HOST.fullmatch(host):
        raise ValueError(f'Invalid server host: {host!r}')
    user = user or ssh_user or DEFAULT_SSH_USER
    if not _USER.fullmatch(user):
        raise ValueError(f'Invalid SSH user: {user!r}')
    result = {'hub': f'{user}@{host}', 'host': host, 'is_ip': _is_ip(host)}
    if not result['is_ip']:
        result['registry_url'] = f'https://{host}'
    return result


def hub_host(hub: str | None) -> str | None:
    return hub.rpartition('@')[2] if hub else None


def apply_server(address: str, ssh_user: str | None = None, saved: dict | None = None) -> dict:
    """Settings updates that make the node tunnel and pairing endpoint match ``address``.

    A domain sets both the SSH hub and the HTTPS registry origin. An IP sets only
    the SSH hub (an IP has no trusted certificate) and keeps the saved origin when
    it is the same hub; for a different hub the stale origin is dropped and the
    caller must ask for the HTTPS domain.
    """
    saved = dict(saved or {})
    server = normalize_server(address, ssh_user)
    updates = {'hub': server['hub']}
    if server.get('registry_url'):
        updates['registry_url'] = server['registry_url']
    elif saved.get('registry_url') and hub_host(saved.get('hub')) != server['host']:
        updates['registry_url'] = None
    return updates


def public_view(data: dict) -> dict:
    """Display copy with home paths shortened to ~."""
    home = str(Path.home())
    out = {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith(home + os.sep):
            value = '~' + value[len(home):]
        out[key] = value
    return out
