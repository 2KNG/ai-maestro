"""Registry -> collector -> widget integration, using only a temporary home."""
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import check_all_usage as usage
import maestro
import render_usage_widget as widget


class VisibilityPipelineTests(unittest.TestCase):
    def test_registry_only_workers_survive_running_to_completed_transition(self):
        with ExitStack() as stack:
            home = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": home}))
            registry = str(Path(home) / ".gemini/antigravity/agy_spawned_sessions.json")
            stack.enter_context(mock.patch.object(maestro, "SPAWN_REGISTRY_PATH", registry))
            stack.enter_context(mock.patch.object(widget.subprocess, "check_output", return_value=""))
            stack.enter_context(mock.patch.object(widget, "_codex_rollout_paths", return_value={}))
            stack.enter_context(mock.patch.object(widget, "_detail_extras_html", return_value=""))
            dev = str(Path(home) / "dev")
            parent = {"conversation_id": "orchestrator", "title": "Workspace orchestration",
                      "workspaces": ["file://" + dev], "status": "RUNNING",
                      "last_modified_time": datetime.now(timezone.utc).isoformat(), "children": []}
            sessions = {"claude": "claude-worker", "codex": "codex-worker"}
            for provider, sid in sessions.items():
                maestro.record_spawned_session(sid, {
                    "provider": provider, "pool": provider, "model": "claude-opus-5-5" if provider == "claude" else "gpt-6-astra",
                    "effort": "medium", "title": f"✦ {provider} delegated task", "spec_preview": "Fix worker visibility",
                    "cwd": dev + "/ai-maestro", "started_at": int(time.time()),
                    "status": "running", "pid": os.getpid(),
                })
            for terminal in (False, True):
                if terminal:
                    for sid in sessions.values():
                        maestro.record_spawned_session(sid, {"status": "completed"})
                tree = {"antigravity": [parent], "claude": usage.get_claude_agent_tree(), "codex": []}
                active, _, _, _ = widget.build_active_and_closed_trees_html({"agent_trees": tree}, {})
                for provider, sid in sessions.items():
                    prefix = "claude" if provider == "claude" else "cdx"
                    marker = f'data-node-id="node-sub-{prefix}-{sid}"'
                    with self.subTest(provider=provider, terminal=terminal):
                        self.assertEqual(active.count(marker), 1)
                        row = active.split(marker, 1)[1].split(">", 1)[0]
                        self.assertIn(f'data-sub-running="{0 if terminal else 1}"', row)


if __name__ == "__main__":
    unittest.main()
