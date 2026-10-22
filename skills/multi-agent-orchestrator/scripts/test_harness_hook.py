import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).with_name('maestro_harness_hook.py')


def _clean_env(**env):
    clean = {k: v for k, v in os.environ.items()
             if 'MODEL_TIER_FLASH' not in k + v and 'MODEL_PLACEHOLDER_M322' not in k + v
             and k not in ('MAESTRO_WORKER_ACTIVE', 'MAESTRO_WORKER_DEPTH')}
    clean.update(env)
    return clean


class ClaudeCodePassThroughTests(unittest.TestCase):
    """Claude Code / Orca payloads (no conversationId/toolCall) must emit nothing on pass-through."""
    STATE_DIR = Path('/tmp/maestro_harness_state')

    def run_hook(self, event, payload, **env):
        return subprocess.run([sys.executable, str(HOOK), event], input=json.dumps(payload),
                              text=True, capture_output=True, env=_clean_env(**env))

    def claude_payload(self, event, session, **extra):
        return {'session_id': session, 'hook_event_name': event, 'cwd': '/tmp', **extra}

    def assert_silent(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')

    def state_path(self, session):
        return self.STATE_DIR / f'{session}.json'

    def read_state(self, session):
        return json.loads(self.state_path(session).read_text())

    def seed_state(self, session, solo, read):
        self.STATE_DIR.mkdir(exist_ok=True)
        self.state_path(session).write_text(json.dumps(
            {'solo_streak': solo, 'read_streak': read, 'delegated_in_turn': False, 'updated_at': 0}))
        self.addCleanup(self.state_path(session).unlink, missing_ok=True)

    def tool(self, session, tool, tool_input, **extra):
        return self.run_hook('PreToolUse', self.claude_payload(
            'PreToolUse', session, tool_name=tool, tool_input=tool_input, **extra))

    def test_pre_tool_use_tracks_streaks_silently(self):
        session = f'claude-track-{os.getpid()}'
        self.seed_state(session, 0, 0)
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl') as transcript:
            # Opus transcript quoting the Flash placeholder must not trip the Flash write lock.
            transcript.write('{"text":"Flash · MODEL_PLACEHOLDER_M322"}\n')
            transcript.flush()
            for tool, tool_input in (('Read', {'file_path': '/tmp/x'}), ('Grep', {'pattern': 'x'}),
                                     ('Edit', {'file_path': '/tmp/x'}), ('Write', {'file_path': '/tmp/x'}),
                                     ('Bash', {'command': 'ls -la'})):
                with self.subTest(tool=tool):
                    self.assert_silent(self.tool(session, tool, tool_input, transcript_path=transcript.name))
        state = self.read_state(session)
        self.assertEqual((state['read_streak'], state['solo_streak']), (2, 3))
        self.assertFalse(state['delegated_in_turn'])

    def test_delegation_resets_streaks(self):
        session = f'claude-deleg-{os.getpid()}'
        for tool, tool_input in (('Agent', {'prompt': 'x'}), ('Task', {'prompt': 'x'}),
                                 ('Bash', {'command': "maestro worker-start --pool auto --spec 'plain task'"}),
                                 ('Bash', {'command': '~/.local/bin/agy-orca worker-start --spec-file /tmp/s'})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.seed_state(session, 5, 7)
                self.assert_silent(self.tool(session, tool, tool_input))
                state = self.read_state(session)
                self.assertEqual((state['solo_streak'], state['read_streak']), (0, 0))
                self.assertTrue(state['delegated_in_turn'])

    def test_subagent_tool_calls_do_not_count(self):
        session = f'claude-sub-{os.getpid()}'
        self.seed_state(session, 0, 0)
        self.assert_silent(self.tool(session, 'Read', {'file_path': '/tmp/x'}, agent_id='agent-1'))
        self.assertEqual(self.read_state(session)['read_streak'], 0)

    def test_user_prompt_submit_silent_below_threshold(self):
        session = f'claude-quiet-{os.getpid()}'
        for solo, read in ((0, 0), (1, 3)):
            with self.subTest(solo=solo, read=read):
                self.seed_state(session, solo, read)
                self.assert_silent(self.run_hook('UserPromptSubmit', self.claude_payload(
                    'UserPromptSubmit', session, prompt='hello')))
                state = self.read_state(session)
                self.assertEqual((state['solo_streak'], state['read_streak']), (0, 0))

    def test_user_prompt_submit_reminds_after_solo_turn(self):
        session = f'claude-remind-{os.getpid()}'
        for solo, read in ((2, 0), (0, 4)):
            with self.subTest(solo=solo, read=read):
                self.seed_state(session, solo, read)
                result = self.run_hook('UserPromptSubmit', self.claude_payload(
                    'UserPromptSubmit', session, prompt='hello'))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                lines = result.stdout.splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith('[🎼 MAESTRO]'))
                self.assertNotIn('invoke_subagent', lines[0])
                self.assertNotIn('decision', lines[0])
                self.assertEqual(self.read_state(session)['solo_streak'], 0)
                # The reset means the next prompt is silent again.
                self.assert_silent(self.run_hook('UserPromptSubmit', self.claude_payload(
                    'UserPromptSubmit', session, prompt='again')))

    def test_worker_active_is_silent_and_untracked(self):
        session = f'claude-worker-{os.getpid()}'
        self.seed_state(session, 0, 0)
        result = self.run_hook('PreToolUse', self.claude_payload(
            'PreToolUse', session, tool_name='Bash', tool_input={'command': 'pytest'}),
            MAESTRO_WORKER_ACTIVE='1')
        self.assert_silent(result)
        self.assertEqual(self.read_state(session)['solo_streak'], 0)

    def test_unescaped_backticks_in_worker_start_are_blocked(self):
        for cmd in ('maestro worker-start --pool auto --spec "fix `ssh orin` issue"',
                    'maestro worker-start --spec "run $(whoami)"',
                    "agy-orca worker-start --spec \"see `func()`\" --wait"):
            with self.subTest(cmd=cmd):
                result = self.run_hook('PreToolUse', self.claude_payload(
                    'PreToolUse', 'claude-bt', tool_name='Bash', tool_input={'command': cmd}))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                self.assertIn('unescaped backticks', result.stderr)

    def test_safely_quoted_specs_pass_silently(self):
        for cmd in ("maestro worker-start --pool auto --wait --spec-file - <<'EOF'\nfix `ssh orin` and $(x)\nEOF",
                    'maestro worker-start --spec - <<"SPEC"\ncall `func()`\nSPEC',
                    "maestro worker-start --spec 'fix `ssh orin` and $(x)'",
                    'maestro worker-start --spec "escaped \\`tick\\` and \\$(x)"'):
            with self.subTest(cmd=cmd):
                result = self.run_hook('PreToolUse', self.claude_payload(
                    'PreToolUse', 'claude-safe', tool_name='Bash', tool_input={'command': cmd}))
                self.assert_silent(result)

    def test_depth_three_spawn_is_blocked(self):
        cmd = "maestro worker-start --pool claude --spec 'leaf task'"
        for payload in (self.claude_payload('PreToolUse', 'claude-depth', tool_name='Bash',
                                            tool_input={'command': cmd}),
                        {'conversationId': 'agy-depth', 'toolCall': {'name': 'run_command',
                                                                     'arguments': {'CommandLine': cmd}}}):
            with self.subTest(payload=payload):
                result = self.run_hook('PreToolUse', payload, MAESTRO_WORKER_ACTIVE='1', MAESTRO_WORKER_DEPTH='2')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                self.assertIn('depth (2)', result.stderr)
        self.assert_silent(self.run_hook('PreToolUse', self.claude_payload(
            'PreToolUse', 'claude-depth', tool_name='Bash', tool_input={'command': cmd}),
            MAESTRO_WORKER_ACTIVE='1', MAESTRO_WORKER_DEPTH='1'))


class AntigravityDecisionTests(unittest.TestCase):
    """Antigravity payloads (conversationId/toolCall) keep the JSON decision protocol."""

    def run_hook(self, event, payload, **env):
        return subprocess.run([sys.executable, str(HOOK), event], input=json.dumps(payload),
                              text=True, capture_output=True, env=_clean_env(**env))

    def agy(self, name, arguments, conv='agy-test-conv'):
        return {'conversationId': f'{conv}-{os.getpid()}', 'toolCall': {'name': name, 'arguments': arguments}}

    def assert_allowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"decision":"allow"}\n')

    def assert_blocked(self, result, needle):
        self.assertEqual(result.returncode, 2, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['decision'], 'block')
        self.assertIn(needle, out['reason'])

    def test_allowed_tools_print_allow(self):
        for event in ('PreToolUse', 'BeforeTool'):
            for name, args in (('view_file', {'AbsolutePath': '/tmp/x'}),
                               ('run_command', {'CommandLine': 'ls'}),
                               ('schedule', {'DurationSeconds': 600}),
                               ('invoke_subagent', {'Subagents': [{'TypeName': 'research', 'Model': 'flash'}]})):
                with self.subTest(event=event, name=name):
                    self.assert_allowed(self.run_hook(event, self.agy(name, args)))

    def test_pre_invocation_prints_only_allow_even_after_streak(self):
        payload = self.agy('replace_file_content', {}, conv='agy-streak')
        for _ in range(5):
            self.run_hook('PreToolUse', payload)
        payload.pop('toolCall')
        self.assert_allowed(self.run_hook('PreInvocation', payload))

    def test_blocked_calls(self):
        self.assert_blocked(self.run_hook('PreToolUse', self.agy(
            'schedule', {'DurationSeconds': 30, 'Prompt': 'wait for subagent'})), 'schedule() timers')
        self.assert_blocked(self.run_hook('BeforeTool', self.agy(
            'invoke_subagent', {'Subagents': [{'TypeName': 'self', 'Model': 'flash'}]})), "TypeName='self'")
        self.assert_blocked(self.run_hook('PreToolUse', self.agy(
            'write_to_file', {'TargetFile': '/tmp/x'}), MODEL='MODEL_TIER_FLASH'), 'READ-ONLY')


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
