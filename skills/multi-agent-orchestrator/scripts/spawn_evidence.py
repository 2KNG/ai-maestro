"""Small, stat-invalidated spawn evidence. Never cache source logs or rollouts."""
from collections import OrderedDict
import json
import os
import re
import threading

WINDOW = 64 * 1024
SNIPPET_LIMIT = 8192
CACHE_LIMIT = 4096
_cache = OrderedDict()
_versions = {}
_lock = threading.RLock()


def cached(path, kind, loader):
    """Serialize misses so concurrent renderers open an unchanged file only once."""
    stat = os.stat(path)
    key = (os.fspath(path), stat.st_mtime_ns, stat.st_size, kind, stat.st_ctime_ns)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        value = loader(path)
        # Drop obsolete versions without scanning the entire LRU on every miss.
        slot = (key[0], kind)
        old = _versions.get(slot)
        if old is not None:
            _cache.pop(old, None)
        _versions[slot] = key
        _cache[key] = value
        while len(_cache) > CACHE_LIMIT:
            evicted, _ = _cache.popitem(last=False)
            _versions.pop((evicted[0], evicted[3]), None)
        return value


def clear_cache():
    with _lock:
        _cache.clear()
        _versions.clear()


def result_ids(content):
    try:
        result, _ = json.JSONDecoder().raw_decode(content.lstrip())
    except ValueError:
        return set()
    if not isinstance(result, dict) or not (result.get('pool') or result.get('provider')):
        return set()
    ids = {result[k] for k in ('session_id', 'thread_id') if isinstance(result.get(k), str)}
    ids.update(tid for tid in (result.get('thread_ids') or []) if isinstance(tid, str))
    return ids


def compact(text):
    """Keep ID and command contexts plus short boundary text for legacy spec output."""
    if len(text) <= SNIPPET_LIMIT:
        return text
    text = text[:WINDOW] + '\n' + text[-WINDOW:] if len(text) > 2 * WINDOW else text
    pieces = [text[:1024], text[-1024:]]
    for match in re.finditer(r'[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|(?:session_id|thread_ids?|--spec)\b', text):
        pieces.append(text[max(0, match.start() - 80):match.end() + 512])
        if sum(map(len, pieces)) >= SNIPPET_LIMIT:
            break
    return '\n'.join(pieces)[:SNIPPET_LIMIT]


class Snippets:
    """Bound aggregate retained text per file, including files with many outputs."""
    def __init__(self):
        self.text = ''

    def append(self, text):
        joined = self.text + '\n' + compact(text)
        self.text = joined if len(joined) <= SNIPPET_LIMIT else joined[:1024] + joined[-(SNIPPET_LIMIT - 1024):]


def bounded_text(path):
    with open(path, 'rb') as stream:
        head = stream.read(WINDOW)
        size = os.fstat(stream.fileno()).st_size
        tail = b''
        if size > WINDOW:
            stream.seek(max(WINDOW, size - WINDOW))
            tail = stream.read(WINDOW)
    return (head + b'\n' + tail).decode('utf-8', errors='replace')


def task_evidence(path):
    def load(path):
        text = bounded_text(path)
        ids = result_ids(text)
        # CLI JSON may follow progress messages; only decode objects beginning a
        # line, never an ID quoted inside a prose investigation.
        for match in re.finditer(r"(?m)^\s*(?=\{)", text):
            ids.update(result_ids(text[match.end():]))
        return compact(text), frozenset(ids), 'maestro' in text
    return cached(path, 'task-evidence', load)


def rollout_evidence(path):
    def load(path):
        snippets = Snippets()
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if 'maestro' not in line and 'session_id' not in line and 'thread_id' not in line:
                    continue
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                payload = step.get('payload') or {}
                if not isinstance(payload, dict):
                    continue
                if payload.get('type') == 'function_call':
                    args = payload.get('arguments') or ''
                    if 'maestro' in args and 'worker-start' in args:
                        try:
                            args = json.loads(args)
                        except (ValueError, TypeError):
                            pass
                        snippets.append(compact(str(args)))
                elif payload.get('type') == 'function_call_output':
                    snippets.append(compact(str(payload.get('output') or '')))
        return snippets.text
    return cached(path, 'rollout-evidence', load)


def last_step_state(path):
    def load(path):
        with open(path, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - 8192))
            lines = stream.read(8192).decode('utf-8', errors='ignore').strip().splitlines()
        try:
            step = json.loads(lines[-1]) if lines else {}
        except ValueError:
            return {}
        if not isinstance(step, dict):
            return {}
        # Only retain fields used for terminal-state classification.
        return {'type': step.get('type'), 'status': step.get('status'),
                'tool_calls': bool(step.get('tool_calls'))}
    return cached(path, 'last-step-state', load)
