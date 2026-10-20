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

    TYPE_REASON = "BLOCKED BY MAESTRO HARNESS: invoke_subagent(TypeName='self') is strictly forbidden to protect 4 Argon quota! You MUST delegate all coding, implementation, and architecture tasks via run_command -> ~/.local/bin/maestro worker-start --pool auto (Opus 5.5 / gpt-6-astra / gpt-6.1-sol / gpt-6-luna)."
    MODEL_REASON = "BLOCKED BY MAESTRO HARNESS: Never spawn 4 Argon (Model='inherit' or 'pro') via invoke_subagent! Use maestro worker-start --pool auto (Opus 5.5 / gpt-6-astra / gpt-6.1-sol / gpt-6-luna) for work, or Model='flash' with TypeName='research' for read-only lookups."

    def assert_blocked(self, result, reason):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'decision': 'block', 'reason': reason})
        self.assertEqual(result.stderr.strip(), reason)

    def test_non_research_is_blocked_even_inside_worker(self):
        for model in ('flash', 'flash_lite', 'pro', 'inherit', None):
            for kind in ('self', ' SELF ', 'codex-worker', '', None):
                for key in ('Subagents', 'subagents'):
                    with self.subTest(model=model, kind=kind, key=key):
                        entry = {'Model': model}
                        if kind is not None:
                            entry['TypeName'] = kind
                        result = self.run_hook({'tool_name': 'invoke_subagent', 'tool_input': {
                            key: [{'Model': 'flash', 'TypeName': 'research'}, entry]}},
                            MAESTRO_WORKER_ACTIVE='1')
                        self.assert_blocked(result, self.TYPE_REASON)

    def test_research_blocks_non_flash_and_default_model(self):
        for model in ('pro', 'inherit', ' PRO ', 'unknown', '', None):
            with self.subTest(model=model):
                entry = {'TypeName': 'research'}
                if model is not None:
                    entry['Model'] = model
                result = self.run_hook({'toolCall': {'name': 'invoke_subagent', 'arguments':
                    json.dumps({'Subagents': [entry]})}}, MAESTRO_WORKER_ACTIVE='1')
                self.assert_blocked(result, self.MODEL_REASON)

    def test_only_flash_research_allowed(self):
        for model in ('flash', 'flash_lite', ' FLASH ', ' FLASH_LITE '):
            result = self.run_hook({'toolCall': {'name': 'invoke_subagent', 'arguments': json.dumps({
                'Subagents': [{'Model': model, 'TypeName': ' Research '}]})}})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['decision'], 'allow')

    def test_empty_uppercase_list_falls_back_to_lowercase(self):
        for empty in (None, []):
            result = self.run_hook({'tool_name': 'invoke_subagent', 'tool_input': {
                'Subagents': empty, 'subagents': [{'TypeName': 'research', 'Model': 'pro'}]}})
            self.assert_blocked(result, self.MODEL_REASON)

    def test_before_tool_event_also_blocks(self):
        result = subprocess.run([sys.executable, str(HOOK), 'BeforeTool'],
                                input=json.dumps({'tool_name': 'invoke_subagent',
                                                  'tool_input': {'Subagents': [{}]}}),
                                text=True, capture_output=True)
        self.assert_blocked(result, self.TYPE_REASON)

    def test_json_encoded_subagents_are_checked(self):
        for key in ('Subagents', 'subagents'):
            for entry, reason in (
                    ({'Model': 'inherit', 'Prompt': 'implement'}, self.TYPE_REASON),
                    ({'Model': 'pro', 'TypeName': 'research'}, self.MODEL_REASON),
                    ({'Model': 'flash', 'TypeName': 'research'}, None)):
                with self.subTest(key=key, entry=entry):
                    result = self.run_hook({'toolCall': {'name': 'invoke_subagent',
                        'arguments': {key: json.dumps([entry])}}}, MAESTRO_WORKER_ACTIVE='1')
                    if reason:
                        self.assert_blocked(result, reason)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)['decision'], 'allow')

    def test_malformed_subagents_fail_closed(self):
        for entries in ('[invalid', 'null', '{}', '42', '"text"',
                        {'Model': 'flash', 'TypeName': 'research'}, 42,
                        [None], ['research'], [False],
                        [{'Model': 'flash', 'TypeName': 'research'}, None]):
            for encoded in (False, True):
                with self.subTest(entries=entries, encoded=encoded):
                    result = self.run_hook({'tool_name': 'invoke_subagent', 'tool_input': {
                        'Subagents': json.dumps(entries) if encoded else entries}},
                        MAESTRO_WORKER_ACTIVE='1')
                    self.assert_blocked(result, self.TYPE_REASON)

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
