"""Dead-PID reaping of the maestro spawn registry and SUB retention in the active tree."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import render_usage_widget as widget
import spawn_registry_reaper as reaper

DEAD_PID = 2 ** 22 + 12345  # above the default pid_max, never alive
LIVE_PID = 1  # init: alive and older than any registry start time


class ReapRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.now = time.time()

    def reap(self, registry, live_ids=(), proc_root='/proc'):
        return reaper.reap_registry(registry, now=self.now, home=self.home,
                                    live_ids=set(live_ids), proc_root=proc_root)

    def test_dead_pid_is_completed_at_its_own_end_time(self):
        registry = {'w': dict(provider='codex', status='running', pid=DEAD_PID,
                              started_at=self.now - 3600, updated_at=self.now - 3598)}
        self.assertEqual(self.reap(registry), ['w'])
        entry = registry['w']
        self.assertEqual(entry['status'], 'completed')
        self.assertEqual(entry['reap_reason'], 'pid_dead')
        # Last real activity, not the reap time: an hour-old worker never looks fresh.
        self.assertEqual(entry['completed_at'], int(self.now - 3598))
        self.assertEqual(entry['updated_at'], entry['completed_at'])

    def test_elapsed_and_transcript_mtime_set_the_end_time(self):
        registry = {'a': dict(provider='claude', status='running', pid=DEAD_PID, started_at=self.now - 900,
                              elapsed_sec=120, updated_at=self.now - 900)}
        self.reap(registry)
        self.assertEqual(registry['a']['completed_at'], int(self.now - 780))
        transcript = Path(self.home, '.claude/projects/-x/b-sid.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{}')
        os.utime(transcript, (self.now - 500, self.now - 500))
        registry = {'b-sid': dict(provider='claude', status='running', session_id='b-sid',
                                  started_at=self.now - 900, updated_at=self.now - 899)}
        self.reap(registry)
        self.assertEqual(registry['b-sid']['completed_at'], int(self.now - 500))
        self.assertEqual(registry['b-sid']['reap_reason'], 'pid_missing')

    def test_live_pid_supervisor_and_live_session_stay_running(self):
        registry = {
            'live': dict(status='running', pid=LIVE_PID, started_at=self.now - 10),
            'waiting': dict(status='running', supervisor_pid=os.getpid(), started_at=self.now - 3600),
            'scanned': dict(status='running', session_id='sid-live', started_at=self.now - 3600),
            'done': dict(status='completed', pid=DEAD_PID, started_at=self.now - 3600, updated_at=1),
        }
        self.assertEqual(self.reap(registry, live_ids={'sid-live'}), [])
        self.assertEqual([k for k, v in registry.items() if v['status'] == 'running'],
                         ['live', 'waiting', 'scanned'])
        self.assertEqual(registry['done']['updated_at'], 1)

    def test_pidless_entry_gets_a_launch_grace_window(self):
        registry = {'young': dict(status='starting', started_at=self.now - 5),
                    'old': dict(status='active', started_at=self.now - reaper.PIDLESS_GRACE_SEC - 1)}
        self.assertEqual(self.reap(registry), ['old'])
        self.assertEqual(registry['young']['status'], 'starting')

    def test_zombie_and_reused_pid_count_as_dead(self):
        proc = Path(self.home, 'proc')
        (proc / '700').mkdir(parents=True)
        (proc / '800').mkdir()
        (proc / 'stat').write_text('cpu 1 2 3\nbtime 1000\n')
        ticks = os.sysconf('SC_CLK_TCK')
        rest = ' '.join(['0'] * 17)
        (proc / '700/stat').write_text(f'700 (codex) Z 1 {rest} {10 * ticks} 0')
        (proc / '800/stat').write_text(f'800 (bash) S 1 {rest} {5000 * ticks} 0')
        self.assertFalse(reaper.pid_alive(700, str(proc)))
        self.assertTrue(reaper.pid_alive(800, str(proc)))
        # pid 800 began at epoch 6000, long after a worker registered at 1005.
        self.assertFalse(reaper.pid_alive(800, str(proc), started_at=1005))
        self.assertTrue(reaper.pid_alive(800, str(proc), started_at=5990))
        self.assertFalse(reaper.pid_alive(None, str(proc)))
        self.assertFalse(reaper.pid_alive(True, str(proc)))

    def test_load_registry_persists_without_clobbering_concurrent_writers(self):
        path = Path(self.home, 'agy_spawned_sessions.json')
        path.write_text(json.dumps({
            'dead': dict(status='running', pid=DEAD_PID, started_at=self.now - 600, updated_at=self.now - 590),
            'relaunched': dict(status='running', pid=DEAD_PID, started_at=self.now - 600),
            'live': dict(status='running', pid=LIVE_PID, started_at=self.now - 600),
        }))
        real_persist = reaper._persist

        def racing_persist(target, reaped):
            # Another maestro relaunches one entry between our read and our write.
            data = json.loads(path.read_text())
            data['relaunched'] = dict(status='running', pid=os.getpid(), started_at=self.now)
            data['new'] = dict(status='running', pid=os.getpid(), started_at=self.now)
            path.write_text(json.dumps(data))
            return real_persist(target, reaped)

        with mock.patch.object(reaper, '_persist', side_effect=racing_persist):
            loaded = reaper.load_registry(str(path), now=self.now, home=self.home, live_ids=set())
        self.assertEqual(loaded['dead']['status'], 'completed')
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk['dead']['status'], 'completed')
        self.assertEqual(on_disk['dead']['completed_at'], int(self.now - 590))
        self.assertEqual(on_disk['relaunched']['status'], 'running')
        self.assertEqual(on_disk['live']['status'], 'running')
        self.assertIn('new', on_disk)

    def test_missing_or_corrupt_registry_is_empty(self):
        self.assertEqual(reaper.load_registry(str(Path(self.home, 'none.json'))), {})
        bad = Path(self.home, 'bad.json')
        bad.write_text('{')
        self.assertEqual(reaper.load_registry(str(bad)), {})


class SubRetentionRenderTests(unittest.TestCase):
    CID = 'agyparent-0000-0000-0000-000000000001'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, HOME=self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = time.time()
        for name, value in [('_extract_active_bg_tasks', []), ('_detail_extras_html', ''),
                            ('_codex_rollout_paths', {})]:
            patch = mock.patch.object(widget, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def worker(self, title, **fields):
        return dict(provider='codex', pool='codex', model='gpt-6.1-sol', title=f'✦ {title}',
                    parent_conversation_id=self.CID, cwd=self.temp.name, **fields)

    def render(self, workers, live_codex=()):
        path = Path(self.temp.name, '.gemini/antigravity/agy_spawned_sessions.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workers))
        parent = dict(conversation_id=self.CID, title='Pixel To Go Kinematics Handover', status='IDLE',
                      last_modified_time=datetime.now(timezone.utc).isoformat())
        with mock.patch.object(widget, '_running_worker_sessions', return_value=(set(live_codex), set(), set())):
            html = widget.build_active_and_closed_trees_html({'agent_trees': {'antigravity': [parent]}}, {})[0]
        return html, json.loads(path.read_text())

    def row(self, html, tid):
        return html.split(f'data-node-id="node-sub-cdx-{tid}"', 1)[1].split('</div></div>', 1)[0]

    def test_dead_and_finished_codex_subs_leave_the_active_tree(self):
        workers = {
            'w-dead': self.worker('Dead sol', status='running', pid=DEAD_PID,
                                  started_at=self.now - 3000, updated_at=self.now - 2990),
            'w-fresh-dead': self.worker('Just died', status='running', pid=DEAD_PID,
                                        started_at=self.now - 300, updated_at=self.now - 2),
            'w-live': self.worker('Live sol', status='running', pid=LIVE_PID,
                                  started_at=self.now - 300, updated_at=self.now - 300),
            'w-done-old': self.worker('Old done', status='completed', ok=True,
                                      started_at=self.now - 3000, updated_at=self.now - 60),
            'w-done-new': self.worker('New done', status='completed', ok=True,
                                      started_at=self.now - 100, updated_at=self.now - 5),
        }
        html, on_disk = self.render(workers)
        self.assertNotIn('Dead sol', html)
        self.assertNotIn('Old done', html)
        self.assertIn('Live sol', html)
        self.assertIn('data-sub-running="1"', html.split('node-sub-cdx-w-live', 1)[1].split('>', 1)[0])
        # Inside the completion grace window: static completed dot and a frozen timer.
        for tid in ('w-done-new', 'w-fresh-dead'):
            head = html.split(f'node-sub-cdx-{tid}', 1)[1].split('>', 1)[0]
            self.assertIn('data-sub-running="0"', head)
            row = self.row(html, tid)
            self.assertIn('status-dot sub-done', row)
            self.assertNotIn('data-start-ts', row)
        self.assertEqual(on_disk['w-dead']['status'], 'completed')
        self.assertEqual(on_disk['w-dead']['completed_at'], int(self.now - 2990))
        self.assertEqual(on_disk['w-live']['status'], 'running')

    def test_registry_row_is_not_live_on_a_recent_timestamp_alone(self):
        node = dict(registry_id='r', thread_id='r', status='running', pid=DEAD_PID,
                    started_at=self.now - 30, updated_at=self.now)
        self.assertFalse(widget._codex_root_activity(node, set(), {}, self.now)[0])
        self.assertTrue(widget._codex_root_activity(node, {'r'}, {}, self.now)[0])
        self.assertTrue(widget._codex_root_activity({**node, 'pid': LIVE_PID}, set(), {}, self.now)[0])
        self.assertTrue(widget._codex_root_activity(
            {**node, 'pid': None, 'supervisor_pid': os.getpid()}, set(), {}, self.now)[0])

    def test_finished_claude_sub_is_dropped_after_retention(self):
        claude = lambda title, age: dict(provider='claude', model='claude-opus-5-5', title=f'✦ {title}',
                                         parent_conversation_id=self.CID, status='completed', ok=True,
                                         started_at=self.now - 3000, updated_at=self.now - age)
        html, _ = self.render({'c-old': claude('Adding English finished', widget.SUB_IDLE_RETENTION_SEC + 1),
                               'c-new': claude('Just finished opus', 3)})
        self.assertNotIn('Adding English finished', html)
        self.assertIn('Just finished opus', html)


if __name__ == '__main__':
    unittest.main()
