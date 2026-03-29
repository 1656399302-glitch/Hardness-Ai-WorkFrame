"""
Local dashboard for editing `.env`, launching the harness, and tailing logs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from runtime_state import LOG_FILE, append_event, read_log_slice, read_state, reset_state


REPO_ROOT = Path(__file__).parent.resolve()
DASHBOARD_HTML = REPO_ROOT / "dashboard.html"


class HarnessProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._command: list[str] = []

    def start(
        self,
        prompt: str,
        resume_dir: str,
        skip_planner: bool,
        verbose: bool,
        max_rounds: str,
    ) -> dict:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": "Harness is already running."}

            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE.write_text("", encoding="utf-8")
            reset_state()

            command = [sys.executable, "-u", str(REPO_ROOT / "harness.py"), "run"]
            if verbose:
                command.append("--verbose")
            if skip_planner:
                command.append("--skip-planner")
            if resume_dir.strip():
                command.extend(["--resume-dir", resume_dir.strip()])
            if prompt.strip():
                command.append(prompt.strip())

            env = os.environ.copy()
            if max_rounds.strip():
                env["MAX_HARNESS_ROUNDS"] = max_rounds.strip()

            log_handle = LOG_FILE.open("a", encoding="utf-8")
            self._process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            self._command = command
            append_event(
                "dashboard_start",
                "Dashboard started harness process",
                pid=self._process.pid,
                command=command,
            )
            return {"ok": True, "pid": self._process.pid, "command": command}

    def stop(self) -> dict:
        with self._lock:
            if not self._process or self._process.poll() is not None:
                self._process = None
                self._command = []
                return {"ok": True, "message": "No running harness process."}
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            append_event("dashboard_stop", "Dashboard stopped harness process", command=self._command)
            self._process = None
            self._command = []
            return {"ok": True, "message": "Harness stopped."}

    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def status(self) -> dict:
        state = read_state()
        with self._lock:
            running = self.is_running()
            pid = self._process.pid if self._process else None
            returncode = self._process.poll() if self._process else None
            command = self._command
        return {
            "running": running,
            "pid": pid,
            "returncode": returncode,
            "command": command,
            "state": state,
        }


PROCESS_MANAGER = HarnessProcessManager()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HarnessDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_dashboard()
            return
        if parsed.path == "/api/config":
            self._json_response(
                {
                    "env": config.read_env_file_values(),
                    "specs": [spec.__dict__ for spec in config.ENV_SPECS],
                }
            )
            return
        if parsed.path == "/api/status":
            self._json_response(PROCESS_MANAGER.status())
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            offset = int(query.get("offset", ["0"])[0] or 0)
            chunk, new_offset = read_log_slice(offset)
            self._json_response({"chunk": chunk, "offset": new_offset})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._read_json_body()

        if parsed.path == "/api/config":
            env_values = {
                spec.name: str(payload.get(spec.name, spec.default))
                for spec in config.ENV_SPECS
            }
            config.write_env_file_values(env_values)
            self._json_response({"ok": True})
            return

        if parsed.path == "/api/run":
            result = PROCESS_MANAGER.start(
                prompt=str(payload.get("prompt", "")),
                resume_dir=str(payload.get("resume_dir", "")),
                skip_planner=bool(payload.get("skip_planner", False)),
                verbose=bool(payload.get("verbose", True)),
                max_rounds=str(payload.get("max_rounds", "")),
            )
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            self._json_response(result, status=status)
            return

        if parsed.path == "/api/stop":
            self._json_response(PROCESS_MANAGER.stop())
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _serve_dashboard(self) -> None:
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _json_response(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve_dashboard() -> None:
    server = ThreadingHTTPServer((config.DASHBOARD_HOST, config.DASHBOARD_PORT), DashboardHandler)
    print(f"Harness dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
