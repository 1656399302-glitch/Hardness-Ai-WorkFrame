import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import config
from artifacts import claim_operator_instructions, ensure_workspace_layout, read_operator_inbox
from dashboard_server import DashboardHandler, PROCESS_MANAGER
from runtime_state import reset_state, write_state


class DashboardServerOperatorInboxTest(unittest.TestCase):
    def setUp(self):
        self.original_workspace = config.WORKSPACE
        self.temp_dir = tempfile.TemporaryDirectory()
        config.WORKSPACE = self.temp_dir.name
        ensure_workspace_layout(config.WORKSPACE)

        PROCESS_MANAGER.stop()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        PROCESS_MANAGER.stop()
        reset_state()
        config.WORKSPACE = self.original_workspace
        self.temp_dir.cleanup()

    def _get_json(self, path: str) -> tuple[int, dict]:
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _spawn_external_harness_process(self, workspace: Path) -> subprocess.Popen[str]:
        script_path = Path(self.temp_dir.name) / "harness.py"
        script_path.write_text(
            "import signal\n"
            "import sys\n"
            "import time\n"
            "\n"
            "def _stop(*_args):\n"
            "    sys.exit(0)\n"
            "\n"
            "signal.signal(signal.SIGTERM, _stop)\n"
            "while True:\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--resume-dir",
                str(workspace),
                "--skip-planner",
                "Continue from terminal",
            ],
            cwd=self.temp_dir.name,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(self._cleanup_process, process)
        return process

    def _cleanup_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def test_operator_inbox_api_returns_empty_queue_by_default(self):
        status, payload = self._get_json("/api/operator-inbox")

        self.assertEqual(status, 200)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["summary"]["pending"], 0)

    def test_operator_inbox_api_accepts_new_instruction(self):
        status, payload = self._post_json(
            "/api/operator-inbox",
            {
                "content": "Fix the latest blocking bug in the next build.",
                "scope": "next_build",
                "mode": "must_fix",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["item"]["scope"], "next_build")
        self.assertEqual(payload["item"]["mode"], "must_fix")
        self.assertEqual(payload["item"]["status"], "pending")

        _, inbox = self._get_json("/api/operator-inbox")
        self.assertEqual(len(inbox["items"]), 1)
        self.assertEqual(inbox["summary"]["pending"], 1)

    def test_operator_inbox_api_reflects_processed_status_after_claim(self):
        status, payload = self._post_json(
            "/api/operator-inbox",
            {
                "content": "Verify the regression path during evaluation.",
                "scope": "next_evaluate",
                "mode": "advisory",
            },
        )
        self.assertEqual(status, 200)

        claim_operator_instructions("evaluate", 6, config.WORKSPACE)

        _, inbox = self._get_json("/api/operator-inbox")
        self.assertEqual(inbox["summary"]["pending"], 0)
        self.assertEqual(inbox["summary"]["processed"], 1)
        self.assertEqual(inbox["items"][0]["status"], "processed")
        self.assertEqual(inbox["items"][0]["processed_phase"], "evaluate")
        self.assertEqual(inbox["items"][0]["processed_round"], 6)

    def test_operator_inbox_api_rejects_empty_content(self):
        status, payload = self._post_json(
            "/api/operator-inbox",
            {"content": "   ", "scope": "next_round", "mode": "advisory"},
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("required", payload["error"].lower())

    def test_operator_inbox_api_uses_runtime_workspace_when_present(self):
        runtime_workspace = Path(self.temp_dir.name) / "active-workspace"
        runtime_workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(runtime_workspace)
        write_state(workspace=str(runtime_workspace), status="running", phase="build", round=3)

        status, payload = self._post_json(
            "/api/operator-inbox",
            {
                "content": "Route this instruction to the active run workspace.",
                "scope": "next_build",
                "mode": "must_fix",
            },
        )

        self.assertEqual(status, 200)
        inbox = read_operator_inbox(runtime_workspace)
        self.assertEqual(len(inbox["items"]), 1)
        self.assertEqual(inbox["items"][0]["content"], "Route this instruction to the active run workspace.")
        self.assertEqual(
            Path(payload["inbox"]["workspace"]).resolve(),
            runtime_workspace.resolve(),
        )

    def test_status_api_exposes_cached_workspace_after_run_finishes(self):
        finished_workspace = Path(self.temp_dir.name) / "finished-workspace"
        finished_workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(finished_workspace)
        write_state(workspace=str(finished_workspace), status="passed", phase="complete", round=4)

        status_code, payload = self._get_json("/api/status")

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["running"])
        self.assertEqual(payload["current_workspace"], "")
        self.assertEqual(Path(payload["cached_workspace"]).resolve(), finished_workspace.resolve())
        self.assertEqual(payload["cached_project_name"], finished_workspace.name)

    def test_clear_history_endpoint_removes_cached_workspace(self):
        finished_workspace = Path(self.temp_dir.name) / "finished-workspace"
        finished_workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(finished_workspace)
        write_state(workspace=str(finished_workspace), status="passed", phase="complete", round=4)
        self._get_json("/api/status")

        status_code, payload = self._post_json("/api/clear-history", {})
        _, refreshed = self._get_json("/api/status")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["history"]["last_workspace"], "")
        self.assertEqual(refreshed["cached_workspace"], "")

    def test_status_api_detects_external_terminal_harness_process(self):
        workspace = Path(self.temp_dir.name) / "terminal-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(workspace)
        process = self._spawn_external_harness_process(workspace)
        write_state(
            workspace=str(workspace),
            status="running",
            phase="build",
            round=2,
            pid=process.pid,
            current_run_command=(
                f"{sys.executable} harness.py --resume-dir {workspace} --skip-planner Continue from terminal"
            ),
            current_run_argv=[
                sys.executable,
                "harness.py",
                "--resume-dir",
                str(workspace),
                "--skip-planner",
                "Continue from terminal",
            ],
        )

        status_code, payload = self._get_json("/api/status")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["pid"], process.pid)
        self.assertEqual(Path(payload["current_workspace"]).resolve(), workspace.resolve())
        self.assertEqual(payload["current_project_name"], workspace.name)

    def test_status_api_detects_external_terminal_harness_process_without_pid_in_state(self):
        workspace = Path(self.temp_dir.name) / "terminal-workspace-no-pid"
        workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(workspace)
        process = self._spawn_external_harness_process(workspace)
        write_state(
            workspace=str(workspace),
            status="running",
            phase="build",
            round=2,
            current_run_command=(
                f"{sys.executable} harness.py --resume-dir {workspace} --skip-planner Continue from terminal"
            ),
        )

        status_code, payload = self._get_json("/api/status")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["pid"], process.pid)
        self.assertEqual(Path(payload["current_workspace"]).resolve(), workspace.resolve())

    def test_stop_endpoint_stops_external_terminal_harness_process(self):
        workspace = Path(self.temp_dir.name) / "terminal-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        ensure_workspace_layout(workspace)
        process = self._spawn_external_harness_process(workspace)
        write_state(
            workspace=str(workspace),
            status="running",
            phase="evaluate",
            round=5,
            pid=process.pid,
            current_run_command=(
                f"{sys.executable} harness.py --resume-dir {workspace} --skip-planner Continue from terminal"
            ),
            current_run_argv=[
                sys.executable,
                "harness.py",
                "--resume-dir",
                str(workspace),
                "--skip-planner",
                "Continue from terminal",
            ],
        )

        status_code, payload = self._post_json("/api/stop", {})
        process.wait(timeout=5)
        _, refreshed = self._get_json("/api/status")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(refreshed["running"])
        self.assertEqual(Path(refreshed["cached_workspace"]).resolve(), workspace.resolve())

    def test_dashboard_html_contains_operator_inbox_panel(self):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        self.assertIn("instructionContent", html)
        self.assertIn("operatorInboxList", html)
        self.assertIn("/api/operator-inbox", html)
        self.assertIn("continueBtn", html)
        self.assertIn("clearHistoryBtn", html)


if __name__ == "__main__":
    unittest.main()
