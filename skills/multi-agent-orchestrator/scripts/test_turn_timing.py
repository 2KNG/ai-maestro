"""Latest-turn regressions using temporary logs; no live provider calls."""
import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import check_all_usage as usage
import render_usage_widget as widget


BASE = 1792108800


def iso(offset):
    return datetime.datetime.fromtimestamp(BASE + offset, datetime.timezone.utc).isoformat()


def agy(offset, kind='GENERIC'):
    return json.dumps({'created_at': iso(offset), 'type': kind})


def codex(offset, kind='agent_message', **payload):
    return json.dumps({'timestamp': iso(offset), 'type': 'event_msg',
                       'payload': {'type': kind, **payload}})


class TurnTimingTests(unittest.TestCase):
    def test_agy_latest_user_input_overrides_room_and_earlier_turns(self):
        lines = [agy(-172800, 'USER_INPUT'), agy(0, 'USER_INPUT'),
                 agy(60, 'USER_INPUT'), agy(194)]
        data = usage._extract_turn_timing(lines, antigravity=True)
        self.assertEqual(data, dict(turn_start_ts=BASE+60, turn_end_ts=BASE+194,
                                   turn_duration_sec=134))

    def test_agy_gap_then_first_tail_step_fallback(self):
        for offsets, duration in (([0, 20, 1000, 1042], 42), ([0, 42], 42)):
            with self.subTest(offsets=offsets):
                data = usage._extract_turn_timing([agy(t) for t in offsets], antigravity=True)
                self.assertEqual(data['turn_duration_sec'], duration)

    def test_agy_user_input_has_priority_over_gap(self):
        data = usage._extract_turn_timing([agy(0, 'USER_INPUT'), agy(1000), agy(1042)], antigravity=True)
        self.assertEqual(data['turn_duration_sec'], 1042)

    def test_agy_bounded_tail_and_partial_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'transcript.jsonl'
            p.write_text('x' * 100000 + '\n' + agy(0, 'USER_INPUT') + '\n' + agy(18) + '\n{"broken":')
            with mock.patch.object(usage.os.path, 'expanduser', return_value=str(p)):
                result = usage._extract_agy_turn_timing('fake', iso(-172800), iso(18))
            self.assertEqual(result['turn_duration_sec'], 18)
            self.assertLessEqual(sum(len(s) for s in usage._jsonl_tail(str(p))), 65536)

    def test_agy_missing_logs_never_use_multi_day_room_lifetime(self):
        with mock.patch.object(usage, '_jsonl_tail', return_value=[]):
            data = usage._extract_agy_turn_timing('fake', iso(-172800), iso(18))
        self.assertEqual(data['turn_duration_sec'], 1)
        self.assertEqual(data['turn_end_ts'], BASE+18)

    def test_agy_missing_final_timestamp_uses_last_modified(self):
        data = usage._extract_turn_timing(
            [agy(0, 'USER_INPUT'), agy(18), '{"type": "GENERIC"}'],
            antigravity=True, fallback_end=iso(42))
        self.assertEqual(data['turn_duration_sec'], 42)

    def test_codex_latest_start_and_last_event(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            p.write_text('\n'.join([codex(-172800, 'user_message'), codex(0, 'task_started'),
                                    codex(1, 'user_message'), codex(135, 'task_complete')]))
            data = usage.parse_codex_rollout(str(p))
        self.assertEqual(data['turn_start_ts'], BASE+1)
        self.assertEqual(data['turn_end_ts'], BASE+135)
        self.assertEqual(data['turn_duration_sec'], 134)
        self.assertTrue(data['turn_completed'])

    def test_codex_gap_after_old_start_resets_turn(self):
        data = usage._extract_turn_timing([codex(0, 'task_started'), codex(20), codex(1000), codex(1042)])
        self.assertEqual(data['turn_duration_sec'], 42)

    def test_codex_full_tail_scanned_despite_metadata_early_break(self):
        lines = [codex(0, 'user_message'), codex(42, 'task_complete'),
                 json.dumps({'timestamp': iso(42), 'type': 'turn_context', 'payload': {'model': 'test'}}),
                 codex(42, 'token_count', info={'last_token_usage': {'total_tokens': 9}})]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            p.write_text('x' * 300000 + '\n' + '\n'.join(lines))
            data = usage.parse_codex_rollout(str(p))
        self.assertEqual(data['turn_duration_sec'], 42)
        self.assertEqual(data['token_count']['total_tokens'], 9)

    def test_missing_codex_rollout_returns_timing_keys(self):
        data = usage.parse_codex_rollout(None)
        self.assertTrue(all(k in data for k in usage.TURN_TIMING_KEYS))

    def test_codex_timing_and_paths_propagate_to_all_depths(self):
        connection = mock.MagicMock()
        connection.cursor.return_value.execute.return_value.fetchall.side_effect = [
            [('parent', '', 'Parent', '/tmp', '/p', 'test', 'low', BASE-172800, BASE+42, 'exec', 1)],
            [('child', 'Child', '', '', '', '', 'completed', '/c', 'test', 'low', BASE-172800, BASE+42)],
            [('grandchild', 'Grandchild', '', '', '', '', 'completed', '/g', 'test', 'low', BASE-172800, BASE+42)],
        ]
        timing = dict(turn_start_ts=BASE, turn_end_ts=BASE+42, turn_duration_sec=42)
        parsed = dict(token_count={}, turn_context={}, turn_completed=True, **timing)
        with mock.patch.object(usage.os.path, 'exists', return_value=True), mock.patch.object(
            usage.os.path, 'getmtime', return_value=BASE+42
        ), mock.patch.object(usage.sqlite3, 'connect', return_value=connection
        ), mock.patch.object(usage, '_running_codex_cwds_and_snippets', return_value=(set(), [])), mock.patch.object(
            usage, '_load_agy_spawn_registry', return_value={}
        ), mock.patch.object(usage, 'parse_codex_rollout', return_value=parsed):
            result = usage.get_codex_agent_tree('/fake/db')
        node = result[0]
        for path in ('/p', '/c', '/g'):
            self.assertEqual(node['rollout_path'], path)
            self.assertEqual({k: node[k] for k in usage.TURN_TIMING_KEYS}, timing)
            if node['children']:
                node = node['children'][0]


class DurationRenderingTests(unittest.TestCase):
    def test_compact_formats_and_minimum(self):
        for sec, expected in ((0, '1s'), (18, '18s'), (134, '2m 14s'), (7200, '2h 00m')):
            self.assertEqual(widget._fmt_dur_compact(sec), expected)

    def test_completed_duration_and_tooltip(self):
        node = dict(created_at=BASE-172800, updated_at=BASE+134,
                    turn_start_ts=BASE, turn_end_ts=BASE+134, turn_duration_sec=134)
        label, css, tip = widget._fmt_row_work_time(node)
        self.assertEqual(label, '✓ 2m 14s')
        self.assertEqual(css, 'done')
        self.assertIn('완료 · 소요 2m 14s', tip)
        self.assertEqual(widget._fmt_duration(node), ' · ⏱ 2m 14s')

    def test_running_uses_current_turn(self):
        now = datetime.datetime.fromtimestamp(BASE+134)
        with mock.patch.object(widget, 'datetime') as clock:
            clock.now.return_value = now
            label, _, _ = widget._fmt_row_work_time(dict(created_at=BASE-172800,
                                                        turn_start_ts=BASE, turn_end_ts=BASE+42,
                                                        turn_duration_sec=42), True)
        self.assertEqual(label, '⏱ 2m 14s')

    def test_running_duration_alone_does_not_use_room_creation(self):
        with mock.patch.object(widget, 'datetime') as clock:
            clock.now.return_value = datetime.datetime.fromtimestamp(BASE+134)
            label, _, _ = widget._fmt_row_work_time(dict(created_at=BASE-172800,
                                                        updated_at=BASE+134, turn_duration_sec=42), True)
        self.assertEqual(label, '⏱ 42s')

    def test_legacy_codex_node_reads_rollout_and_detail_matches(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            p.write_text(codex(0, 'task_started')+'\n'+codex(18, 'task_complete'))
            node = dict(created_at=BASE-172800, updated_at=BASE+18, rollout_path=str(p))
            self.assertEqual(widget._fmt_row_work_time(node)[0], '✓ 18s')
            self.assertEqual(widget._fmt_duration(node), ' · ⏱ 18s')

    def test_legacy_agy_node_reads_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'transcript.jsonl'
            p.write_text(agy(0, 'USER_INPUT')+'\n'+agy(42))
            with mock.patch.object(usage.os.path, 'expanduser', return_value=str(p)):
                node = dict(conversation_id='fake', created_time=iso(-172800), last_modified_time=iso(42))
                self.assertEqual(widget._fmt_row_work_time(node)[0], '✓ 42s')

    def test_running_old_metadata_refreshes_from_latest_turn(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            p.write_text(codex(0, 'task_started')+'\n'+codex(18))
            node = dict(turn_start_ts=BASE-172800, turn_end_ts=BASE-172700,
                        turn_duration_sec=100, rollout_path=str(p))
            with mock.patch.object(widget, 'datetime') as clock:
                clock.now.return_value = datetime.datetime.fromtimestamp(BASE+42)
                self.assertEqual(widget._fmt_row_work_time(node, True)[0], '⏱ 42s')

    def test_legacy_multi_day_times_guarded_in_both_views(self):
        node = dict(created_at=BASE-172800, updated_at=BASE+42)
        self.assertEqual(widget._fmt_row_work_time(node)[0], '✓ 2h 00m')
        self.assertEqual(widget._fmt_duration(node), ' · ⏱ 2h 00m')
        with mock.patch.object(widget, 'datetime') as clock:
            clock.now.return_value = datetime.datetime.fromtimestamp(BASE+43)
            self.assertEqual(widget._fmt_row_work_time(node, True)[0], '⏱ 1s')

    def test_unknown_and_invalid_duration_are_safe(self):
        self.assertEqual(widget._fmt_row_work_time(None)[0], '✓ 완료')
        for value in (float('nan'), float('inf'), True, 'bad'):
            self.assertEqual(widget._fmt_row_work_time(dict(turn_duration_sec=value))[0], '✓ 완료')


if __name__ == '__main__':
    unittest.main()
