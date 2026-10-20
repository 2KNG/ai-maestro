"""Offline HTTP, cache and dispatch regressions: never launches a paid worker."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import benchmark_sync as bench
import maestro as workers
import maestro_memory


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = root / 'benchmark.json'
        self.stack.enter_context(mock.patch.object(bench, 'CACHE_PATH', str(self.cache)))
        self.legacy_cache = root / 'legacy.json'
        self.stack.enter_context(mock.patch.object(bench, 'LEGACY_CACHE_PATH', str(self.legacy_cache)))
        self.stack.enter_context(mock.patch.object(bench, '_detect_local_subscription_models', return_value={
            'new_models_detected': [], 'antigravity_models': [], 'codex_models': []}))
        self.http = self.stack.enter_context(mock.patch.object(bench.urllib.request, 'urlopen'))
        self.http.side_effect = OSError('offline')

    def serve(self, body, status=200):
        self.http.side_effect = None
        response = self.http.return_value.__enter__.return_value
        response.status = status
        response.read.return_value = body.encode()

    def test_exact_baselines_and_offline_recommendations(self):
        result = bench.get_daily_benchmark_and_models()
        self.assertFalse(result['online_verified'])
        matrix = result['benchmark_matrix']
        self.assertEqual(len(matrix), 12)
        expected = [
            (83.8,94.2,87.5,62,3.5), (80.2,89,85,74,1), (82.4,92.5,86.5,68,4),
            (79.4,88.5,85,78,1), (76.8,84,82.5,84,.5), (83.2,93.8,93.2,65,3),
            (81.2,91.5,92,72,1.2),
            (79.5,89.5,91.5,76,1), (77.5,86,90.5,96,2), (78,85,89.5,88, .6),
            (77.2,88,88.5,82,1), (73.5,79,83,98.5,.1)]
        for profile, values in zip(matrix.values(), expected):
            self.assertEqual(tuple(profile[k] for k in (*bench.DOMAINS,'quota_multiplier')), values)
        self.assertEqual(bench.get_max_capability_recommendation()['model'], 'gpt-6-astra')
        self.assertEqual(bench.get_max_capability_recommendation('reasoning')['effort'], 'ultra')
        self.assertEqual(bench.get_max_capability_recommendation('frontend_ui')['effort'], 'ultra')
        self.assertEqual(bench.get_max_capability_recommendation('speed')['pool'], 'agy-flash')
        self.assertEqual(bench.get_max_capability_recommendation(pools={'codex'})['model'], 'gpt-6.1-sol')
        with self.assertRaises(ValueError):
            bench.get_max_capability_recommendation('missing')

    def test_json_updates_matching_effort_and_ranking_only(self):
        self.serve(json.dumps({'entries': [
            {'model':'openai/gpt-6.1-sol (high)', 'pass_rate_2':95.1},
            {'model':'gpt-6.1-sol (high)', 'pass_rate_2':90},
            {'model':'claude-opus-5-5', 'reasoning_effort':'xhigh', 'pass_rate_2':96},
            {'model':'gpt-6-astra', 'pass_rate_2':99},  # ambiguous effort
            {'model':'gpt-6.1-sol (ultra)', 'pass_rate_2':'NaN'},
            {'model':'gpt-6.1-sol (medium)', 'pass_rate_2':101}]}))
        result = bench.get_daily_benchmark_and_models()
        matrix = result['benchmark_matrix']
        self.assertTrue(result['online_verified'])
        self.assertEqual(matrix['gpt-6.1-sol (high)']['coding'], 95.1)
        self.assertEqual(matrix['gpt-6.1-sol (high)']['reasoning'], 88.5)
        self.assertEqual(matrix['gpt-6.1-sol (high)']['quota_multiplier'], 1)
        self.assertEqual(matrix['gpt-6.1-sol (ultra)']['coding'], 82.4)
        self.assertEqual(matrix['gpt-6-astra (medium)']['coding'], 80.2)
        self.assertEqual(result['benchmark_scores']['claude']['routing_preference'], 3)
        self.assertEqual(bench.get_max_capability_recommendation()['model'], 'claude-opus-5-5')
        self.assertEqual(bench.DEFAULT_BENCHMARK_MATRIX['gpt-6.1-sol (high)']['coding'], 79.4)

    def test_yaml_official_schema_and_stdlib_fallback(self):
        body = '''- dirname: run-1
  model: "GPT-6-Astra (high)"
  pass_rate_2: 97.4
  command: aider --model openai/gpt-6-astra
- dirname: run-2
  model: Gemini 3.8 Flash (Fast)
  pass_rate_2: 89.5
'''
        for without_yaml in (False, True):
            with self.subTest(without_yaml=without_yaml), ExitStack() as stack:
                if without_yaml:
                    stack.enter_context(mock.patch.dict('sys.modules', {'yaml': None}))
                self.serve(body)
                result = bench.get_daily_benchmark_and_models(force_refresh=True)
                self.assertEqual(result['benchmark_matrix']['gpt-6-astra (ultra/high)']['coding'], 97.4)
                self.assertEqual(result['benchmark_matrix']['Gemini 3.8 Flash (Fast)']['coding'], 89.5)

    def test_invalid_http_and_data_never_claim_verification(self):
        for body, status in [('<html>oops</html>',200), ('[]',200), ('[{"pass_rate_2":99}]',200),
                             ('[{"model":"x","pass_rate_2":true}]',200),
                             ('[{"model":"x","pass_rate_2":99}]',503), ('model: [broken',200)]:
            with self.subTest(body=body, status=status):
                self.serve(body,status)
                result = bench.get_daily_benchmark_and_models(force_refresh=True)
                self.assertFalse(result['online_verified'])
                self.assertEqual(result['benchmark_matrix'], bench.DEFAULT_BENCHMARK_MATRIX)

    def test_cache_ttl_schema_upgrade_and_corruption(self):
        first = bench.get_daily_benchmark_and_models()
        self.http.reset_mock()
        self.assertEqual(bench.get_daily_benchmark_and_models(), first)
        self.http.assert_not_called()
        for data in ([], {'last_sync_epoch':'invalid'}, {'last_sync_epoch':first['last_sync_epoch']},
                     {**first,'last_sync_epoch':first['last_sync_epoch']-bench.CACHE_TTL_SEC},
                     {**first,'last_sync_epoch':first['last_sync_epoch']+100}):
            with self.subTest(data=data):
                self.http.reset_mock()
                self.cache.write_text(json.dumps(data))
                self.assertEqual(len(bench.get_daily_benchmark_and_models()['benchmark_matrix']),12)
                self.http.assert_called_once()

    def test_legacy_cache_fallback_and_new_cache_precedence(self):
        cached = bench.get_daily_benchmark_and_models()
        self.cache.rename(self.legacy_cache)
        self.http.reset_mock()
        self.assertEqual(bench.get_daily_benchmark_and_models(), cached)
        self.http.assert_not_called()
        newer = {**cached, 'last_sync_kst': 'new cache wins'}
        self.cache.write_text(json.dumps(newer))
        self.assertEqual(bench.get_daily_benchmark_and_models(), newer)
        self.http.assert_not_called()

    def test_ultra_and_max_scores_update_only_the_claude_ultra_profile(self):
        for effort in ('ultra', 'max'):
            with self.subTest(effort=effort):
                self.serve(json.dumps([{'model': 'claude-opus-5-5', 'effort': effort, 'coding': 97}]))
                matrix = bench.get_daily_benchmark_and_models(force_refresh=True)['benchmark_matrix']
                self.assertEqual(matrix['claude-opus-5-5 (ultra)']['coding'], 97)
                self.assertEqual(matrix['claude-opus-5-5 (ultra)']['quota_multiplier'], 3)
                self.assertEqual(matrix['claude-opus-5-5 (xhigh)']['coding'], 81.2)
                self.assertEqual(matrix['claude-opus-5-5 (high)']['coding'], 79.5)


class SpeedAndDispatchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('format_handover_context_for_prompt',''),('save_handover_note',None),
                            ('notify_long_task_completion',None)]:
            self.stack.enter_context(mock.patch.object(maestro_memory,name,return_value=value))
        self.stack.enter_context(mock.patch.object(workers,'record_spawned_session'))
        self.stack.enter_context(mock.patch.object(workers,'stamp_codex_db_titles',return_value=[]))
        self.stack.enter_context(mock.patch('threading.Timer'))
        self.run = self.stack.enter_context(mock.patch.object(workers.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'done','')))
        self.stack.enter_context(mock.patch.object(bench,'get_daily_benchmark_and_models',return_value={'benchmark_matrix':copy.deepcopy(bench.DEFAULT_BENCHMARK_MATRIX)}))
        self.stack.enter_context(mock.patch.object(workers.cau,'collect_usage',return_value={}))
        self.exhausted = self.stack.enter_context(mock.patch.object(workers,'is_pool_exhausted',return_value=(False,'')))

    def cli(self,*flags):
        with mock.patch.object(workers.sys,'argv',['maestro','worker-start','--spec','task','--cwd','/tmp','--wait',*flags]), redirect_stdout(io.StringIO()) as out:
            workers.main()
        return json.loads(out.getvalue())

    def test_codex_default_interactive_urgent_and_background_precedence(self):
        for flags,tier in [([], 'default'),(['--tier','priority'],'default'),
                           (['--interactive'],'priority'),(['--interactive','--tier','ultrafast'],'priority'),
                           (['--urgent'],'ultrafast'),(['--interactive','--tier','standard'],'default'),
                           (['--background','--urgent'],'default'),(['--background','--interactive'],'default')]:
            with self.subTest(flags=flags):
                result = self.cli('--pool','codex',*flags)
                self.assertEqual(result['tier'],tier)
                self.assertIn(f'service_tier="{tier}"',self.run.call_args.args[0])

    def test_astra_guard_survives_every_speed_and_entry_point(self):
        for unlocked in (False,True):
            for effort in ('high','ultra'):
                for flags in ({},{'interactive':True},{'urgent':True},{'background':True,'urgent':True}):
                    with self.subTest(unlocked=unlocked,effort=effort,flags=flags):
                        result = workers.start_codex_worker('task','/tmp',effort,'ultrafast',10,
                            pool='codex-astra',max_capability=unlocked,**flags)
                        self.assertEqual(result['tier'],'default')
                        self.assertEqual(result['effort'],effort if unlocked else 'medium')
        result = self.cli('--pool','codex-astra','--effort','ultra','--urgent')
        self.assertEqual((result['effort'],result['tier']),('medium','default'))

    def test_claude_standard_live_and_explicit_fast(self):
        for flags,model in [([], 'claude-opus-5-5'),(['--interactive'],'claude-opus-5-fast'),
                            (['--urgent'],'claude-opus-5-fast'),(['--background','--urgent'],'claude-opus-5-5')]:
            with self.subTest(flags=flags):
                self.assertEqual(self.cli('--pool','claude',*flags)['model'],model)
        self.assertEqual(self.cli('--pool','claude-fast','--background')['model'],'claude-opus-5-fast')

    def test_claude_ultra_aliases_and_explicit_efforts(self):
        for pool in workers.CLAUDE_ULTRA_POOLS:
            for flags in ([], ['--interactive'], ['--urgent'], ['--effort', 'high', '--interactive']):
                with self.subTest(pool=pool, flags=flags):
                    result = self.cli('--pool', pool, *flags)
                    command = self.run.call_args.args[0]
                    self.assertEqual((result['pool'], result['model'], result['effort'], result['tier']),
                                     ('claude', 'claude-opus-5-5', 'ultra', 'default'))
                    self.assertEqual(command[command.index('--model') + 1], 'claude-opus-5-5')
                    self.assertEqual(command[command.index('--effort') + 1], 'max')
                    self.assertTrue(command[-1].startswith('ultrathink\n\n'))
        for flags in ([], ['--interactive'], ['--urgent']):
            with self.subTest(explicit_ultra_flags=flags):
                result = self.cli('--pool', 'claude', '--effort', 'ultra', *flags)
                command = self.run.call_args.args[0]
                self.assertEqual((result['model'], result['effort'], result['tier']),
                                 ('claude-opus-5-5', 'ultra', 'default'))
                self.assertEqual(command[command.index('--model') + 1], 'claude-opus-5-5')
                self.assertEqual(command[command.index('--effort') + 1], 'max')
                self.assertTrue(command[-1].startswith('ultrathink\n\n'))
        for effort in ('ultra', 'max', 'xhigh', 'high'):
            with self.subTest(effort=effort):
                result = self.cli('--pool', 'claude', '--effort', effort)
                command = self.run.call_args.args[0]
                self.assertEqual(result['effort'], effort)
                self.assertEqual(command[command.index('--effort') + 1],
                                 'max' if effort in ('ultra', 'max') else effort)
                self.assertEqual(command[-1].startswith('ultrathink\n\n'), effort == 'ultra')
                if effort == 'max':
                    self.assertNotIn('ultrathink', command[-1].lower())

    def test_ultrathink_prefix_prepended_even_when_word_appears_later(self):
        for pool, effort in (('claude-ultra', None), ('claude', 'ultra')):
            for kwargs in ({}, {'interactive': True}, {'urgent': True}):
                with self.subTest(pool=pool, effort=effort, kwargs=kwargs):
                    result = workers.start_claude_worker(
                        pool, '✦ Explain how ultrathink and ULTRATHINK work', '/tmp', effort, True, 30, **kwargs)
                    command = self.run.call_args.args[0]
                    self.assertEqual((result['model'], result['effort'], result['tier']),
                                     ('claude-opus-5-5', 'ultra', 'default'))
                    self.assertEqual(command[command.index('--model') + 1], 'claude-opus-5-5')
                    self.assertEqual(command[command.index('--effort') + 1], 'max')
                    self.assertTrue(command[-1].startswith('ultrathink\n\n✦ Explain how ultrathink and ULTRATHINK work'))
                    self.assertEqual(command[-1].lower().count('ultrathink'), 3)

    def test_max_capability_domain_and_model_override(self):
        result = self.cli('--max-capability','--urgent')
        self.assertEqual(result['model'],'gpt-6-astra (ultra, default)')
        result = self.cli('--max-capability','--model','gpt-6.1-sol')
        self.assertEqual(result['model'],'gpt-6.1-sol (ultra, default)')
        result = self.cli('--max-capability','--domain','frontend_ui','--interactive')
        self.assertEqual((result['model'],result['effort']),('claude-opus-5-5','ultra'))
        with mock.patch.object(workers,'start_agy_worker',return_value={'ok':True}) as agy:
            self.cli('--max-capability','--domain','speed')
            self.assertEqual(agy.call_args.args[0],'agy-flash')

    def test_max_capability_exhausted_pool_steers_and_recommends_again(self):
        self.exhausted.return_value = (True,'codex 0%')
        with mock.patch.object(workers,'choose_auto_pool',return_value=('claude',{})):
            result = self.cli('--max-capability')
        self.assertEqual(result['steered_from'],'codex-astra')
        self.assertEqual((result['model'],result['effort']),('claude-opus-5-5','ultra'))

    def test_max_capability_midflight_quota_failover(self):
        self.run.side_effect = [subprocess.CompletedProcess([],1,'','429 quota'), subprocess.CompletedProcess([],0,'done','')]
        with mock.patch.object(workers,'choose_auto_pool',return_value=('claude',{})):
            result = self.cli('--max-capability','--background')
        self.assertEqual(result['model'],'claude-opus-5-5')
        self.assertIn('Mid-flight',result['steer_reason'])
        self.assertEqual(result['tier'],'default')

    def test_no_exhausted_pool_reuse_when_all_excluded(self):
        routing = {'pool':'codex','candidates':[{'pool':'codex','status':'available','remaining_percent':80}]}
        with mock.patch.object(workers.cau,'select_best_pool',return_value=routing):
            self.assertIsNone(workers.choose_auto_pool(exclude_canonical={'codex'})[0])

    def test_task_domain_benchmark_routing_and_low_quota_failover(self):
        # 1. Domain classifications and benchmark winning models
        domains_and_models = [
            ("긴 호흡의 버그 수정 및 단위테스트 루프", "high", "gpt-6.1-sol"),
            ("모션 그래픽 CSS SVG 애니메이션 위젯 제작", "high", "claude-opus-5-5"),
            ("복합 아키텍처 연구 및 심층 알고리즘 분석", "high", "Gemini 4 Argon"),
            ("빠른 로그 스캔 및 파일 탐색", "low", "Gemini 3.8 Flash"),
            ("OSWorld 데스크탑 화면 마우스 조작", "high", "gpt-6.1-sol"),
        ]
        for spec, diff, expected_model in domains_and_models:
            with self.subTest(spec=spec, diff=diff):
                rec = bench.recommend_model_for_task(spec=spec, difficulty=diff)
                self.assertEqual(rec["recommended_model"], expected_model)

        # 2. Low quota threshold (< 20%) failover:
        # When Codex quota drops to 15% (<20%), long-horizon coding fails over to Claude
        low_codex_usage = {
            "codex_local": {"weekly": {"remaining_percent": 15.0}},
            "claude_local": {"weekly": {"remaining_percent": 80.0}},
            "antigravity": {"pools": {}},
        }
        failover_rec = bench.recommend_model_for_task(
            spec="긴 호흡의 버그 수정 및 단위테스트 루프", difficulty="high", usage_data=low_codex_usage
        )
        self.assertEqual(failover_rec["recommended_model"], "claude-opus-5-5")

    def test_worker_timeout_safety_floor(self):
        # Default safety floor for high effort is 3600s
        workers.start_codex_worker("task", "/tmp", "high", "default", 180)
        self.assertEqual(self.run.call_args.kwargs.get("timeout"), 3600)

        workers.start_claude_worker("claude", "task", "/tmp", "high", True, 180)
        self.assertEqual(self.run.call_args.kwargs.get("timeout"), 3600)

        # Environment override MAESTRO_ALLOW_SHORT_TIMEOUT allows short timeouts
        with mock.patch.dict("os.environ", {"MAESTRO_ALLOW_SHORT_TIMEOUT": "1"}):
            workers.start_codex_worker("task", "/tmp", "high", "default", 45)
            self.assertEqual(self.run.call_args.kwargs.get("timeout"), 45)


if __name__ == '__main__':
    unittest.main()
