"""Offline filename, WAL migration and notification configuration regressions."""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import maestro_memory as memory


class CompatibilityTests(unittest.TestCase):
    def test_relative_legacy_symlinks_and_imports(self):
        root = Path(__file__).parent
        for old, new in [('agy_orca.py', 'maestro.py'), ('orca.py', 'maestro.py'),
                         ('orca_memory.py', 'maestro_memory.py')]:
            with self.subTest(old=old):
                self.assertEqual(os.readlink(root / old), new)
                self.assertFalse((root / new).is_symlink())
                module = importlib.import_module(Path(old).stem)
                self.assertTrue(callable(getattr(module, '_connect' if 'memory' in old else 'main')))


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.target = root / 'maestro_memory.sqlite'
        self.legacy = root / 'orca_memory.sqlite'
        self.stack.enter_context(mock.patch.object(memory, 'MEMORY_DB_PATH', str(self.target)))
        self.stack.enter_context(mock.patch.object(memory, 'LEGACY_MEMORY_DB_PATH', str(self.legacy)))

    def test_migration_copies_committed_wal_rows_and_preserves_legacy(self):
        source = sqlite3.connect(self.legacy)
        self.addCleanup(source.close)
        source.execute('PRAGMA journal_mode=WAL')
        source.execute('PRAGMA wal_autocheckpoint=0')
        source.execute('CREATE TABLE migration_probe (value TEXT)')
        source.execute("INSERT INTO migration_probe VALUES ('committed in WAL')")
        source.commit()
        self.assertTrue(Path(str(self.legacy) + '-wal').exists())
        connection = memory._connect()
        try:
            self.assertEqual(connection.execute('SELECT value FROM migration_probe').fetchone(),
                             ('committed in WAL',))
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        finally:
            connection.close()
        self.assertEqual(source.execute('SELECT count(*) FROM migration_probe').fetchone(), (1,))
        self.assertTrue(self.legacy.exists())
        self.assertFalse(list(self.target.parent.glob('.maestro-memory-*')))

    def test_existing_maestro_database_takes_precedence(self):
        for path, value in ((self.legacy, 'legacy'), (self.target, 'maestro')):
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE migration_probe (value TEXT)')
                connection.execute('INSERT INTO migration_probe VALUES (?)', (value,))
        connection = memory._connect()
        try:
            self.assertEqual(connection.execute('SELECT value FROM migration_probe').fetchone(), ('maestro',))
        finally:
            connection.close()

    def test_new_database_when_no_legacy_exists(self):
        connection = memory._connect()
        try:
            self.assertEqual(connection.execute('SELECT count(*) FROM handover_notes').fetchone(), (0,))
        finally:
            connection.close()
        self.assertFalse(self.legacy.exists())

    def test_telegram_prefers_maestro_names_and_falls_back_to_orca(self):
        for use_maestro in (False, True):
            env = {'ORCA_TELEGRAM_BOT_TOKEN': 'old-token', 'ORCA_TELEGRAM_CHAT_ID': 'old-chat'}
            if use_maestro:
                env.update(MAESTRO_TELEGRAM_BOT_TOKEN='new-token', MAESTRO_TELEGRAM_CHAT_ID='new-chat')
            with self.subTest(use_maestro=use_maestro), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(memory.shutil, 'which', return_value=None), \
                    mock.patch.object(memory.urllib.request, 'urlopen') as http:
                self.assertTrue(memory.notify_long_task_completion(
                    session_id=f'notification-{use_maestro}', title='done', elapsed_sec=61, repo='test'))
                request = http.call_args.args[0]
                prefix = 'new' if use_maestro else 'old'
                self.assertEqual(request.full_url, f'https://api.telegram.org/bot{prefix}-token/sendMessage')
                self.assertEqual(json.loads(request.data)['chat_id'], f'{prefix}-chat')


if __name__ == '__main__':
    unittest.main()
