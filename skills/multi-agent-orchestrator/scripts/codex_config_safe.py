#!/usr/bin/env python3
"""Byte-preserving Codex config checkpoints and conservative recovery.

All helper writers serialize on a sibling lock; external writers are checked just
before replacement. External editors do not honor that lock, so a tiny OS-level
check/replace race remains. No portable filesystem compare-and-swap exists.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

try:
    import tomllib
except ImportError:  # Python 3.10
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


class ConfigSafetyError(RuntimeError):
    """Config could not be changed without losing user data."""


def _parse(raw):
    if tomllib is None:
        raise ConfigSafetyError('TOML validation requires Python 3.11+ or tomli')
    encoding = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = 'cp949'
        text = raw.decode(encoding)
    return tomllib.loads(text), text, encoding


def _snapshot(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigSafetyError(f'Refusing non-regular config or backup: {path}')
    raw = path.read_bytes()
    after = path.lstat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if signature(info) != signature(after):
        raise ConfigSafetyError('Config changed while reading; retry')
    return raw, signature(after), stat.S_IMODE(after.st_mode)


def _same(path, original):
    if _snapshot(path) != original:
        raise ConfigSafetyError('Config changed concurrently; refusing replacement')


def _exclusive_write(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _backup(path, raw, kind='bak'):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    dest = path.with_name(f'{path.name}.{kind}.{stamp}.{uuid.uuid4().hex[:8]}')
    _exclusive_write(dest, raw)
    _sync_directory(path.parent)
    return dest


def _candidates(path):
    candidates = list(path.parent.glob(path.name + '.bak.*'))
    candidates.append(path.with_name(path.name + '.bak'))
    candidates = [p for p in candidates if p.exists() and not p.is_symlink()]
    return sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def _valid_backups(path):
    for candidate in _candidates(path):
        try:
            snapshot = _snapshot(candidate)
            raw = snapshot[0]
            _, decoded, _ = _parse(raw)
            if decoded.strip():
                yield candidate, raw
        except (ValueError, UnicodeError, OSError, ConfigSafetyError):
            continue


def _checkpoint(path, raw, *, force_new=False):
    _, decoded, _ = _parse(raw)
    if not decoded.strip():
        return None
    existing = [] if force_new else list(_valid_backups(path))
    backup = next((p for p, data in existing if data == raw and p.name != path.name + '.bak'), None)
    if backup is None:
        backup = _backup(path, raw)
    legacy = path.with_name(path.name + '.bak')
    try:
        _exclusive_write(legacy, raw)
    except FileExistsError:
        pass  # Never overwrite a pre-existing backup.
    return str(backup)


def _sync_directory(directory):
    """Best-effort POSIX directory durability; unsupported filesystems may skip."""
    if os.name == 'nt':
        return
    fd = None
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        os.fsync(fd)
    except OSError:
        # Some network filesystems and platforms reject directory fsync.
        pass
    finally:
        if fd is not None:
            os.close(fd)


def _atomic_replace(path, raw, original):
    fd, name = tempfile.mkstemp(prefix=path.name + '.tmp.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, original[2] if original else 0o600)
        _same(path, original)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(_value(v) for v in value) + ']'
    raise ConfigSafetyError(f'Unsupported default type: {type(value).__name__}')


def _merge(raw, defaults):
    parsed, text, encoding = _parse(raw)
    expected = copy.deepcopy(parsed)
    additions = []

    def collect(current, wanted, prefix=()):
        for key, value in wanted.items():
            if not isinstance(key, str):
                raise ConfigSafetyError('Default keys must be strings')
            if key in current:
                if isinstance(current[key], dict) and isinstance(value, dict):
                    collect(current[key], value, prefix + (key,))
                continue
            if isinstance(value, dict):
                current[key] = {}
                collect(current[key], value, prefix + (key,))
            else:
                current[key] = value
                additions.append((prefix + (key,), value))

    collect(expected, defaults)
    newline = '\r\n' if '\r\n' in text else '\n'
    # Try insertion boundaries, accepting only a parsed tree equal to the precise
    # additive change. This avoids hand-parsing TOML multiline strings/tables.
    for keys, value in additions:
        next_expected = copy.deepcopy(parsed)
        target = next_expected
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
        lines = text.splitlines(keepends=True)
        found = None
        for size in range(len(keys), 0, -1):
            assignment = '.'.join(json.dumps(k) for k in keys[-size:]) + ' = ' + _value(value) + newline
            for pos in range(len(lines) + 1):
                before = ''.join(lines[:pos])
                candidate = before + (newline if before and not before.endswith(('\r', '\n')) else '') + assignment + ''.join(lines[pos:])
                try:
                    if tomllib.loads(candidate) == next_expected:
                        found = candidate
                        break
                except ValueError:
                    pass
            if found is not None:
                break
        if found is None:
            raise ConfigSafetyError('Cannot add default without rewriting existing TOML')
        text, parsed = found, next_expected
    if parsed != expected:
        raise ConfigSafetyError('Default merge failed validation')
    return text.encode(encoding)


@contextmanager
def _config_lock(path, timeout=2.0):
    """OS advisory lock survives crashes without a stale ownership sentinel."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigSafetyError('Refusing non-regular lock file')
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ConfigSafetyError('Config guard lock busy; retry') from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        # Do not unlink: waiting processes must keep locking the same inode.


def inspect_codex_config_safe(config_path=None):
    """Read-only health report: no directory creation, lock, or checkpoint."""
    path = Path(config_path) if config_path is not None else Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    path = path.expanduser().absolute()
    result = dict(ok=False, changed=False, status='missing', path=str(path), backup=None)
    try:
        snapshot = _snapshot(path)
        if snapshot is not None:
            raw = snapshot[0]
            try:
                _, decoded, _ = _parse(raw)
                if decoded.strip():
                    result.update(ok=True, status='valid')
                else:
                    result['status'] = 'empty'
            except (ValueError, UnicodeError):
                result['status'] = 'invalid'
        source = next(_valid_backups(path), None)
        if source:
            result['backup'] = str(source[0])
    except (ConfigSafetyError, OSError) as exc:
        result.update(status='error', error=str(exc))
    return result


def ensure_codex_config_safe(config_path=None, defaults=None, *, recover=True):
    """Checkpoint valid config; recover broken config; optionally add missing keys.

    Returns a status dict (ok, changed, status, path, backup, recovered_from).
    Raises ConfigSafetyError/OSError on unsafe state; invalid files without a valid
    backup remain untouched. A new absent config is left absent unless defaults
    are explicitly supplied. Syntactically valid partial configs are never rolled
    back: omitted user settings cannot safely be inferred to be corruption.
    """
    path = Path(config_path) if config_path is not None else Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.maestro.lock')
    with _config_lock(lock):
        original = _snapshot(path)
        raw = original[0] if original else b''
        result = dict(ok=True, changed=False, status='unchanged', path=str(path), backup=None, recovered_from=None)
        valid = False
        try:
            _, decoded, _ = _parse(raw)
            valid = bool(decoded.strip())
        except (ValueError, UnicodeError):
            pass
        if tomllib is None:
            raise ConfigSafetyError('TOML validation requires Python 3.11+ or tomli')
        if not valid:
            source = next(_valid_backups(path), None) if recover else None
            if source:
                result['recovered_from'], raw = str(source[0]), source[1]
                result['status'] = 'recovered'
            elif original is not None:
                raise ConfigSafetyError('Empty or invalid config has no usable backup; left untouched')
            elif not defaults:
                result['status'] = 'missing'
                return result
        merged = _merge(raw, defaults or {})
        if original is None or merged != original[0]:
            if original:
                result['backup'] = _checkpoint(path, original[0], force_new=True) if valid else str(_backup(path, original[0], 'damaged'))
            _same(path, original)
            _atomic_replace(path, merged, original)
            result['changed'] = True
            if result['status'] != 'recovered':
                result['status'] = 'updated' if original else 'created'
        checkpoint = _checkpoint(path, merged)
        result['backup'] = result['backup'] or checkpoint
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--no-recover', action='store_true')
    args = parser.parse_args()
    try:
        result = ensure_codex_config_safe(args.config, recover=not args.no_recover)
    except (ConfigSafetyError, OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
