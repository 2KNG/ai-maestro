"""Exercise the real setup and CLI boundaries with an isolated user home."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import maestro
import codex_config_safe as guard


class ConfigIntegrationTests(unittest.TestCase):
    def test_custom_toml_preserves_all_user_tables_and_comments(self):
        raw = ('# 사용자 설정\r\nmodel = "custom-model"\r\nmodel_provider = "private"\r\n'
               '[mcp_servers.custom]\r\ncommand = "my-mcp" # keep\r\n'
               "[projects.'C:\\Users\\사용자\\작업']\r\ntrust_level = \"trusted\"\r\n"
               '[profiles.research]\r\nmodel = "another-model"\r\n'
               '[model_providers.private]\r\nbase_url = "https://example.invalid"\r\n').encode('cp949')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_bytes(raw)
            before = guard._parse(raw)[0]
            guard.ensure_codex_config_safe(config, defaults={'service_tier': 'default', 'model': 'ignored'})
            result = config.read_bytes()
            expected = dict(before, service_tier='default')
            self.assertEqual(guard._parse(result)[0], expected)
            # The one inserted root key leaves every original byte in order.
            self.assertEqual(result.replace(b'"service_tier" = "default"\r\n', b''), raw)

    def test_doctor_inspection_and_recovery_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_bytes(b'broken = ')
            with patch.object(sys, 'argv', ['maestro', 'doctor', '--config', str(path)]), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                maestro.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(path.read_bytes(), b'broken = ')
            path.with_name('config.toml.bak').write_bytes(b'model="my-model"\n')
            output = io.StringIO()
            with patch.object(sys, 'argv', ['maestro', 'doctor', '--fix-codex-config', '--config', str(path)]), redirect_stdout(output):
                maestro.main()
            self.assertEqual(json.loads(output.getvalue())['status'], 'recovered')
            self.assertEqual(path.read_bytes(), b'model="my-model"\n')

    def test_worker_config_failure_prevents_child_launch(self):
        with patch.object(maestro, 'ensure_codex_config_safe', side_effect=guard.ConfigSafetyError('invalid')), patch.object(maestro.subprocess, 'run') as run:
            result = maestro.start_codex_worker('test', '.', 'medium', 'default', 30)
        self.assertFalse(result['ok'])
        self.assertIn('Codex config guard', result['error'])
        run.assert_not_called()

    @unittest.skipUnless(shutil.which('bash') and os.name != 'nt', 'POSIX installer integration')
    def test_real_installer_twice_preserves_windows_config(self):
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / 'install'
            install.mkdir()
            shutil.copy(repo / 'install.sh', install)
            shutil.copytree(repo / 'rules', install / 'rules')
            scripts = install / 'skills/multi-agent-orchestrator/scripts'
            scripts.mkdir(parents=True)
            shutil.copy(Path(guard.__file__), scripts)
            home = root / 'home'
            config = home / '.codex/config.toml'
            config.parent.mkdir(parents=True)
            raw = '# 사용자 설정\r\nmodel="custom"\r\n[mcp_servers.mine]\r\ncommand="my-mcp"\r\n'.encode('cp949')
            config.write_bytes(raw)
            fake = root / 'fake'
            fake.mkdir()
            systemctl = fake / 'systemctl'
            systemctl.write_text('#!/bin/sh\nexit 0\n')
            systemctl.chmod(0o755)
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(config.parent), PATH=str(fake) + os.pathsep + os.environ['PATH'])
            for _ in range(2):
                subprocess.run(['bash', str(install / 'install.sh')], env=env, check=True, capture_output=True)
                self.assertEqual(config.read_bytes(), raw)
            self.assertEqual(config.with_name('config.toml.bak').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
