import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import codex_config_safe as guard


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.toml'

    def ensure(self, **kwargs):
        return guard.ensure_codex_config_safe(self.path, **kwargs)

    def test_noop_preserves_bytes_mode_mtime_and_deduplicates(self):
        raw = b'# custom\r\nmodel="user-model"\r\n[features]\r\nfoo=true\r\n'
        self.path.write_bytes(raw)
        self.path.chmod(0o640)
        before = self.path.stat()
        result = self.ensure()
        self.assertFalse(result['changed'])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), stat.S_IMODE(before.st_mode))
        backups = list(self.path.parent.glob('*.bak*'))
        self.ensure()
        self.assertEqual(list(self.path.parent.glob('*.bak*')), backups)
        self.assertEqual(Path(result['backup']).read_bytes(), raw)

    def test_missing_with_no_backup_stays_missing(self):
        self.assertEqual(self.ensure()['status'], 'missing')
        self.assertFalse(self.path.exists())

    def test_new_with_defaults(self):
        self.assertEqual(self.ensure(defaults={'service_tier': 'default'})['status'], 'created')
        self.assertEqual(guard._parse(self.path.read_bytes())[0], {'service_tier': 'default'})

    def test_existing_settings_never_overwritten(self):
        raw = b'model="custom"\nservice_tier="priority"\n'
        self.path.write_bytes(raw)
        self.ensure(defaults={'model': 'different', 'service_tier': 'default'})
        self.assertEqual(self.path.read_bytes(), raw)

    def test_encodings_and_crlf(self):
        for encoding in ('utf-8', 'utf-8-sig', 'cp949'):
            with self.subTest(encoding=encoding):
                raw = '# 설정 보존\r\nmodel="custom"\r\n[features]\r\nfoo=true\r\n'.encode(encoding)
                self.path.write_bytes(raw)
                self.ensure(defaults={'service_tier': 'default'})
                result = self.path.read_bytes()
                decoded = result.decode(encoding)
                self.assertIn('# 설정 보존\r\nmodel="custom"\r\n', decoded)
                self.assertNotIn('\n', decoded.replace('\r\n', ''))
                self.assertEqual(result.startswith(b'\xef\xbb\xbf'), encoding == 'utf-8-sig')
                self.assertEqual(guard._parse(result)[0]['model'], 'custom')

    def test_nested_merge_and_multiline_preservation(self):
        raw = b'model="custom"\ntext="""\n[features]\n"""\n[features]\nold=true\n'
        self.path.write_bytes(raw)
        self.ensure(defaults={'features': {'old': False, 'new': True}, 'fresh': {'value': 3}})
        parsed = guard._parse(self.path.read_bytes())[0]
        self.assertEqual(parsed['features'], {'old': True, 'new': True})
        self.assertEqual(parsed['fresh'], {'value': 3})
        self.assertEqual(parsed['text'], '[features]\n')
        self.assertIn(b'text="""\n[features]\n"""', self.path.read_bytes())

    def test_inline_table_refuses_unsafe_extension(self):
        raw = b'features={old=true}\n'
        self.path.write_bytes(raw)
        with self.assertRaises(guard.ConfigSafetyError):
            self.ensure(defaults={'features': {'new': True}})
        self.assertEqual(self.path.read_bytes(), raw)

    def test_recovery_missing_empty_invalid(self):
        good = b'# user\nmodel="custom"\n'
        self.path.write_bytes(good)
        self.ensure()
        for damaged in (None, b'', b'model="trunc', b'\xff\xff'):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(damaged)
                result = self.ensure()
                self.assertEqual(result['status'], 'recovered')
                self.assertEqual(self.path.read_bytes(), good)
                if damaged is not None:
                    self.assertEqual(Path(result['backup']).read_bytes(), damaged)

    def test_bad_backups_skipped_and_bak_retained(self):
        legacy = self.path.with_name('config.toml.bak')
        legacy.write_bytes(b'model="kept"\n')
        (self.path.parent / 'config.toml.bak.newer').write_bytes(b'model="broken')
        self.path.write_bytes(b'model = ')
        self.ensure()
        self.assertEqual(self.path.read_bytes(), legacy.read_bytes())
        self.ensure(defaults={'service_tier': 'default'})
        self.assertEqual(legacy.read_bytes(), b'model="kept"\n')

    def test_invalid_without_backup_not_touched(self):
        for raw in (b'', b'model="bad'):
            self.path.write_bytes(raw)
            with self.assertRaises(guard.ConfigSafetyError):
                self.ensure(defaults={'model': 'default'})
            self.assertEqual(self.path.read_bytes(), raw)

    def test_valid_partial_not_inferred_to_be_corrupt(self):
        self.path.write_bytes(b'model="custom"\nservice_tier="default"\n')
        self.ensure()
        partial = b'model="custom"\n'
        self.path.write_bytes(partial)
        self.assertFalse(self.ensure()['changed'])
        self.assertEqual(self.path.read_bytes(), partial)

    def test_concurrent_change_refused(self):
        self.path.write_bytes(b'model="custom"\n')
        real = guard._atomic_replace
        other = b'model="external-edit"\n'
        def race(path, raw, original):
            path.write_bytes(other)
            return real(path, raw, original)
        with patch.object(guard, '_atomic_replace', side_effect=race):
            with self.assertRaises(guard.ConfigSafetyError):
                self.ensure(defaults={'service_tier': 'default'})
        self.assertEqual(self.path.read_bytes(), other)
        self.assertFalse(list(self.path.parent.glob('*.tmp.*')))

    def test_failed_replace_preserves_original_and_cleans_up(self):
        raw = b'model="custom"\n'
        self.path.write_bytes(raw)
        with patch.object(guard.os, 'replace', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.ensure(defaults={'service_tier': 'default'})
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.with_name('config.toml.bak').read_bytes(), raw)
        self.assertFalse(list(self.path.parent.glob('*.tmp.*')))
        with guard._config_lock(self.path.with_name('config.toml.maestro.lock'), timeout=0):
            pass

    def test_backup_failure_prevents_write(self):
        raw = b'model="custom"\n'
        self.path.write_bytes(raw)
        with patch.object(guard, '_backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.ensure(defaults={'service_tier': 'default'})
        self.assertEqual(self.path.read_bytes(), raw)

    @unittest.skipIf(os.name == 'nt', 'same-process Windows locks vary')
    def test_lock_conflict(self):
        lock = self.path.with_name('config.toml.maestro.lock')
        with guard._config_lock(lock):
            with self.assertRaises(guard.ConfigSafetyError):
                with guard._config_lock(lock, timeout=0):
                    pass
        with guard._config_lock(lock, timeout=0):
            pass

    def test_stale_lock_file_is_harmless(self):
        self.path.with_name('config.toml.maestro.lock').write_bytes(b'old owner')
        self.assertEqual(self.ensure()['status'], 'missing')

    @unittest.skipIf(os.name == 'nt', 'symlink privileges platform dependent')
    def test_symlink_refused(self):
        target = self.path.parent / 'target'
        target.write_bytes(b'model="custom"\n')
        self.path.symlink_to(target)
        with self.assertRaises(guard.ConfigSafetyError):
            self.ensure(defaults={'foo': True})
        self.assertEqual(target.read_bytes(), b'model="custom"\n')

    def test_inspection_has_no_side_effects(self):
        missing = self.path.parent / 'absent' / 'config.toml'
        self.assertEqual(guard.inspect_codex_config_safe(missing)['status'], 'missing')
        self.assertFalse(missing.parent.exists())
        self.path.write_bytes(b'model="custom"')
        self.assertEqual(guard.inspect_codex_config_safe(self.path)['status'], 'valid')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.path.write_bytes(b'broken = ')
        self.assertEqual(guard.inspect_codex_config_safe(self.path)['status'], 'invalid')

    def test_windows_lock_api(self):
        calls = []
        fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2,
                                     locking=lambda fd, operation, count: calls.append((operation, count)))
        lock = self.path.with_name('config.toml.maestro.lock')
        with patch.dict('sys.modules', {'msvcrt': fake}), patch.object(guard.os, 'name', 'nt'):
            with guard._config_lock(lock):
                pass
        self.assertEqual(calls, [(1, 1), (2, 1)])

    @unittest.skipIf(os.name == 'nt', 'POSIX mode assertion')
    def test_updated_permissions_and_private_backups(self):
        self.path.write_bytes(b'model="custom"')
        self.path.chmod(0o640)
        self.ensure(defaults={'service_tier': 'default'})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        for backup in self.path.parent.glob('*.bak*'):
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode) & 0o077, 0)

    def test_every_modification_has_fresh_original_backup(self):
        raw = b'model="custom"\n'
        self.path.write_bytes(raw)
        checkpoint = self.ensure()['backup']
        existing = set(self.path.parent.glob('*.bak.*'))
        first = self.ensure(defaults={'service_tier': 'default'})
        self.assertNotIn(Path(first['backup']), existing)
        self.assertNotEqual(first['backup'], checkpoint)
        self.assertEqual(Path(first['backup']).read_bytes(), raw)
        # Simulate an external user reverting to bytes already checkpointed.
        self.path.write_bytes(raw)
        existing = set(self.path.parent.glob('*.bak.*'))
        second = self.ensure(defaults={'service_tier': 'default'})
        self.assertNotIn(Path(second['backup']), existing)
        self.assertEqual(Path(second['backup']).read_bytes(), raw)
        current = set(self.path.parent.glob('*.bak.*'))
        self.ensure()
        self.assertEqual(set(self.path.parent.glob('*.bak.*')), current)

    def test_subprocess_lock_excludes_other_writer_and_releases(self):
        lock = self.path.with_name('config.toml.maestro.lock')
        code = ("import sys; from pathlib import Path; "
                "import codex_config_safe as guard; "
                "context=guard._config_lock(Path(sys.argv[1])); "
                "context.__enter__(); print('locked', flush=True); "
                "sys.stdin.readline(); context.__exit__(None,None,None)")
        child = subprocess.Popen([sys.executable, '-c', code, str(lock)],
                                 cwd=Path(guard.__file__).parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(guard.ConfigSafetyError):
                with guard._config_lock(lock, timeout=0.1):
                    self.fail('Concurrent process acquired an exclusive lock')
            _, error = child.communicate('release\n', timeout=5)
            self.assertEqual(child.returncode, 0, error)
            with guard._config_lock(lock, timeout=0):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_directory_sync_is_best_effort(self):
        with patch.object(guard.os, 'fsync', side_effect=OSError('unsupported')):
            guard._sync_directory(self.path.parent)

    def test_bom_only_wipe_restores_good_backup(self):
        good = b'\xef\xbb\xbf# user\r\nmodel="custom"\r\n'
        self.path.write_bytes(good)
        self.ensure()
        for damaged in (b'\xef\xbb\xbf', b'\xef\xbb\xbf\r\n \t\r\n'):
            with self.subTest(damaged=damaged):
                self.path.write_bytes(damaged)
                self.assertEqual(guard.inspect_codex_config_safe(self.path)['status'], 'empty')
                result = self.ensure()
                self.assertEqual(result['status'], 'recovered')
                self.assertEqual(self.path.read_bytes(), good)
                self.assertEqual(Path(result['backup']).read_bytes(), damaged)
        self.assertFalse(self.ensure()['changed'])
        self.assertEqual(self.path.read_bytes(), good)

    def test_bom_only_backup_is_skipped(self):
        self.path.with_name('config.toml.bak').write_bytes(b'\xef\xbb\xbf\r\n')
        self.path.write_bytes(b'broken = ')
        self.assertIsNone(guard.inspect_codex_config_safe(self.path)['backup'])
        with self.assertRaises(guard.ConfigSafetyError):
            self.ensure()
        self.assertEqual(self.path.read_bytes(), b'broken = ')

    def test_bom_comments_only_config_is_valid_and_preserved(self):
        raw = b'\xef\xbb\xbf# deliberate empty config\r\n'
        self.path.write_bytes(raw)
        self.assertEqual(guard.inspect_codex_config_safe(self.path)['status'], 'valid')
        self.assertFalse(self.ensure()['changed'])
        self.assertEqual(self.path.read_bytes(), raw)

    def test_codex_home(self):
        with patch.dict(os.environ, {'CODEX_HOME': self.tmp.name}):
            result = guard.ensure_codex_config_safe()
        self.assertEqual(result['path'], str(self.path))

    def test_missing_parser_fails_closed(self):
        self.path.write_bytes(b'model="custom"\n')
        with patch.object(guard, 'tomllib', None):
            with self.assertRaises(guard.ConfigSafetyError):
                self.ensure()
        self.assertFalse(self.path.with_name('config.toml.bak').exists())

    def test_no_recovery_does_not_touch_invalid_file(self):
        self.path.with_name('config.toml.bak').write_bytes(b'model="custom"\n')
        self.path.write_bytes(b'broken = ')
        with self.assertRaises(guard.ConfigSafetyError):
            self.ensure(recover=False)
        self.assertEqual(self.path.read_bytes(), b'broken = ')


if __name__ == '__main__':
    unittest.main()
