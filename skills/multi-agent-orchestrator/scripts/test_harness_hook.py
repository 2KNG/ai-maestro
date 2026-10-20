import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).with_name('maestro_harness_hook.py')


class FlashLockTests(unittest.TestCase):
    def run_hook(self, payload, **env):
        clean = {k: v for k, v in os.environ.items()
                 if 'MODEL_TIER_FLASH' not in k + v and 'MODEL_PLACEHOLDER_M322' not in k + v}
        clean.update(env)
        return subprocess.run([sys.executable, str(HOOK), 'PreToolUse'],
                              input=json.dumps(payload), text=True, capture_output=True, env=clean)

    def test_flash_requires_research_even_inside_worker(self):
        for model in ('flash', 'flash_lite', 'FLASH'):
            for kind in ('self', None):
                for key in ('Subagents', 'subagents'):
                    result = self.run_hook({'tool_name': 'invoke_subagent', 'tool_input': {
                        key: [{'Model': 'pro'}, {'Model': model, 'TypeName': kind}]}},
                        MAESTRO_WORKER_ACTIVE='1')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                    self.assertIn('READ-ONLY', result.stderr)

    def test_research_and_pro_allowed(self):
        for model, kind in (('flash', 'research'), ('flash_lite', 'research'), ('pro', 'self')):
            result = self.run_hook({'toolCall': {'name': 'invoke_subagent', 'arguments': json.dumps({
                'Subagents': [{'Model': model, 'TypeName': kind}]})}})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['decision'], 'allow')

    def test_flash_write_tools_blocked_by_environment(self):
        for tool in ('replace_file_content', 'multi_replace_file_content', 'write_to_file'):
            result = self.run_hook({'tool_name': tool}, MODEL='MODEL_TIER_FLASH', MAESTRO_WORKER_ACTIVE='1')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['decision'], 'block')

    def test_transcript_path_and_session_id_detection(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / '.gemini/antigravity/brain/test-session/.system_generated/logs/transcript.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text('{"model":"MODEL_PLACEHOLDER_M322"}\n')
            for identity in ({'transcript_path': str(path)}, {'session_id': 'test-session'},
                             {'conversation_id': 'test-session'}, {'conversationId': 'test-session'}):
                result = self.run_hook({'tool_name': 'write_to_file', **identity}, HOME=home)
                self.assertEqual(result.returncode, 2, result.stderr)
            path.write_text('{"model":"pro"}\n')
            self.assertEqual(self.run_hook({'tool_name': 'write_to_file', 'transcript_path': str(path)}).returncode, 0)

    def test_schedule_polling_for_subagent_is_blocked(self):
        result = self.run_hook({'tool_name': 'schedule', 'tool_input': {
            'DurationSeconds': 15, 'Prompt': 'Wait for subagent'}})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['decision'], 'block')
        self.assertIn('Never use schedule() timers', result.stderr)
