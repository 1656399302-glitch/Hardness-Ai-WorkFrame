"""
Local dashboard for editing `.env`, launching the harness, and tailing logs.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from artifacts import enqueue_operator_instruction, read_operator_inbox
from runtime_state import LOG_FILE, append_event, read_log_slice, read_state, reset_state


REPO_ROOT = Path(__file__).parent.resolve()
DASHBOARD_HTML = REPO_ROOT / "dashboard.html"
DASHBOARD_HISTORY_FILE = LOG_FILE.parent / "dashboard-history.json"


def _default_dashboard_history() -> dict:
    return {
        "last_workspace": "",
        "ignored_workspace": "",
        "updated_at": None,
    }


def _read_dashboard_history() -> dict:
    if not DASHBOARD_HISTORY_FILE.exists():
        return _default_dashboard_history()
    try:
        payload = json.loads(DASHBOARD_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _default_dashboard_history()
    if not isinstance(payload, dict):
        return _default_dashboard_history()
    return {
        "last_workspace": str(payload.get("last_workspace", "") or ""),
        "ignored_workspace": str(payload.get("ignored_workspace", "") or ""),
        "updated_at": payload.get("updated_at"),
    }


def _write_dashboard_history(last_workspace: str, ignored_workspace: str = "") -> dict:
    payload = {
        "last_workspace": str(last_workspace or "").strip(),
        "ignored_workspace": str(ignored_workspace or "").strip(),
        "updated_at": time.time(),
    }
    DASHBOARD_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_HISTORY_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return payload


def _clear_dashboard_history() -> dict:
    current = _read_dashboard_history()
    ignored_workspace = str(current.get("last_workspace") or read_state().get("workspace") or "").strip()
    return _write_dashboard_history("", ignored_workspace=ignored_workspace)


def _project_name_from_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    return Path(raw).name or raw


def _normalize_workspace(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    return str(Path(raw).resolve())


def _workspace_candidates(path: str) -> list[str]:
    raw = str(path or "").strip()
    if not raw:
        return []
    candidates: list[str] = []
    for value in (raw, os.path.abspath(raw), os.path.realpath(raw), str(Path(raw).resolve())):
        normalized = str(value or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _cache_last_workspace(workspace: str) -> dict:
    resolved = _normalize_workspace(workspace)
    current = _read_dashboard_history()
    if current.get("ignored_workspace") == resolved:
        return current
    if current.get("last_workspace") == resolved:
        return current
    return _write_dashboard_history(resolved, ignored_workspace="")


def _clear_ignored_workspace_if_matches(workspace: str) -> dict:
    resolved = _normalize_workspace(workspace)
    current = _read_dashboard_history()
    if not resolved or current.get("ignored_workspace") != resolved:
        return current
    return _write_dashboard_history(current.get("last_workspace", ""), ignored_workspace="")


def _runtime_command_from_state(state: dict) -> list[str]:
    argv = state.get("current_run_argv")
    if isinstance(argv, list):
        return [str(token) for token in argv if str(token)]
    raw = str(state.get("current_run_command") or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return [raw]


def _command_looks_like_harness(command: str) -> bool:
    normalized = str(command or "").replace("\\", "/").lower()
    return "harness.py" in normalized and "dashboard_server.py" not in normalized


def _read_process_command(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    line = result.stdout.strip()
    if not line:
        return ""
    parts = line.split(None, 1)
    proc_state = parts[0]
    command = parts[1].strip() if len(parts) > 1 else ""
    if proc_state.upper().startswith("Z"):
        return ""
    return command


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _read_process_command(pid):
            return True
        time.sleep(0.1)
    return not _read_process_command(pid)


def _find_harness_process_by_workspace(workspace: str, excluded_pids: set[int] | None = None) -> tuple[int, str] | None:
    workspace_candidates = _workspace_candidates(workspace)
    if not workspace_candidates:
        return None
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=", "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    skip = excluded_pids or set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in skip:
            continue
        command = parts[1].strip()
        if not _command_looks_like_harness(command):
            continue
        if not any(candidate in command for candidate in workspace_candidates):
            continue
        if not _read_process_command(pid):
            continue
        return pid, command
    return None


class HarnessProcessManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
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
            if self._external_process_snapshot(read_state()):
                return {"ok": False, "error": "A harness process is already running outside the dashboard."}

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
            if resume_dir.strip():
                _clear_ignored_workspace_if_matches(resume_dir.strip())

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
            else:
                managed_process = self._process
                managed_command = list(self._command)
                workspace = str(read_state().get("workspace") or "").strip()
                managed_process.terminate()
                try:
                    managed_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    managed_process.kill()
                    managed_process.wait(timeout=5)
                append_event("dashboard_stop", "Dashboard stopped harness process", command=managed_command)
                if workspace:
                    _cache_last_workspace(workspace)
                self._process = None
                self._command = []
                return {"ok": True, "message": "Harness stopped."}

        state = read_state()
        external = self._external_process_snapshot(state)
        if external:
            pid = int(external["pid"])
            workspace = str(external.get("workspace") or state.get("workspace") or "").strip()
            try:
                os.kill(pid, signal.SIGTERM)
                if not _wait_for_pid_exit(pid, timeout_seconds=5):
                    os.kill(pid, signal.SIGKILL)
                    _wait_for_pid_exit(pid, timeout_seconds=5)
            except ProcessLookupError:
                pass
            append_event(
                "dashboard_stop",
                "Dashboard stopped external harness process",
                pid=pid,
                workspace=workspace,
                command=external.get("command", []),
            )
            if workspace:
                _cache_last_workspace(workspace)
            return {"ok": True, "message": "Harness stopped.", "pid": pid}

        return {"ok": True, "message": "No running harness process."}

    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def status(self) -> dict:
        state = read_state()
        with self._lock:
            managed_running = self.is_running()
            managed_pid = self._process.pid if self._process else None
            managed_returncode = self._process.poll() if self._process else None
            managed_command = list(self._command)
        external = None if managed_running else self._external_process_snapshot(state)
        running = managed_running or bool(external)
        pid = managed_pid if managed_running else (external["pid"] if external else None)
        returncode = managed_returncode if managed_running else None
        command = managed_command if managed_running else list(external.get("command", [])) if external else []
        runtime_workspace = _normalize_workspace(str(state.get("workspace") or ""))
        if not running and runtime_workspace:
            _cache_last_workspace(runtime_workspace)
        cached = _read_dashboard_history()
        current_workspace = runtime_workspace if running else ""
        if running and not current_workspace:
            current_workspace = _normalize_workspace(_extract_resume_dir_from_command(command))
        if running and current_workspace:
            _clear_ignored_workspace_if_matches(current_workspace)
        return {
            "running": running,
            "pid": pid,
            "returncode": returncode,
            "command": command,
            "state": state,
            "current_workspace": current_workspace,
            "current_project_name": _project_name_from_path(current_workspace),
            "cached_workspace": str(cached.get("last_workspace") or ""),
            "cached_project_name": _project_name_from_path(str(cached.get("last_workspace") or "")),
        }

    def _external_process_snapshot(self, state: dict) -> dict | None:
        if str(state.get("status") or "").strip().lower() != "running":
            return None
        raw_workspace = str(state.get("workspace") or "").strip()
        workspace = _normalize_workspace(raw_workspace)
        pid = int(state.get("pid") or 0)
        with self._lock:
            managed_pid = self._process.pid if self._process and self._process.poll() is None else 0
            if managed_pid and managed_pid == pid:
                return None
        process_command = _read_process_command(pid) if pid > 0 else ""
        if not process_command:
            search_workspace = raw_workspace or workspace
            located = _find_harness_process_by_workspace(
                search_workspace,
                excluded_pids={managed_pid} if managed_pid else set(),
            )
            if located:
                pid, process_command = located
        if not process_command or not _command_looks_like_harness(process_command):
            return None
        command = _runtime_command_from_state(state)
        if not command:
            command = [process_command]
        if not workspace:
            workspace = _normalize_workspace(_extract_resume_dir_from_command(command))
        return {
            "pid": pid,
            "command": command,
            "workspace": workspace,
            "process_command": process_command,
        }


PROCESS_MANAGER = HarnessProcessManager()


def _extract_resume_dir_from_command(command: list[str]) -> str:
    if not isinstance(command, list):
        return ""
    for index, token in enumerate(command):
        if token == "--resume-dir" and index + 1 < len(command):
            return str(command[index + 1] or "").strip()
    return ""


def _resolve_dashboard_workspace(explicit_workspace: str = "") -> str:
    candidate = _normalize_workspace(explicit_workspace)
    if candidate:
        return candidate
    runtime_workspace = _normalize_workspace(str(read_state().get("workspace") or ""))
    if runtime_workspace:
        return runtime_workspace
    cached_workspace = str(_read_dashboard_history().get("last_workspace") or "").strip()
    if cached_workspace:
        return cached_workspace
    return config.WORKSPACE


def _operator_inbox_payload() -> dict:
    inbox = read_operator_inbox(_resolve_dashboard_workspace())
    items = inbox.get("items", [])
    summary = {"pending": 0, "processed": 0, "invalid": 0}
    for item in items:
        status = str(item.get("status") or "pending").lower()
        summary[status] = summary.get(status, 0) + 1
    return {
        "schema_version": inbox.get("schema_version", 1),
        "items": items,
        "summary": summary,
    }


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
        if parsed.path == "/api/operator-inbox":
            query = parse_qs(parsed.query)
            workspace = str(query.get("workspace", [""])[0] or "")
            self._json_response(_operator_inbox_payload_for_workspace(workspace))
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

        if parsed.path == "/api/clear-history":
            history = _clear_dashboard_history()
            self._json_response({"ok": True, "history": history})
            return

        if parsed.path == "/api/operator-inbox":
            content = str(payload.get("content", "")).strip()
            if not content:
                self._json_response(
                    {"ok": False, "error": "Instruction content is required."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            workspace = _resolve_dashboard_workspace(str(payload.get("workspace", "")))
            item = enqueue_operator_instruction(
                content,
                scope=str(payload.get("scope", "next_round")),
                mode=str(payload.get("mode", "advisory")),
                workspace=workspace,
            )
            append_event(
                "dashboard_operator_inbox_enqueue",
                "Dashboard queued operator instruction",
                workspace=workspace,
                item_id=item.get("id"),
                scope=item.get("scope"),
                mode=item.get("mode"),
            )
            self._json_response({"ok": True, "item": item, "inbox": _operator_inbox_payload_for_workspace(workspace)})
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


def _operator_inbox_payload_for_workspace(workspace: str = "") -> dict:
    resolved_workspace = _resolve_dashboard_workspace(workspace)
    payload = _operator_inbox_payload() if not workspace else {
        **read_operator_inbox(resolved_workspace),
        "summary": {"pending": 0, "processed": 0, "invalid": 0},
    }
    if workspace:
        for item in payload.get("items", []):
            status = str(item.get("status") or "pending").lower()
            payload["summary"][status] = payload["summary"].get(status, 0) + 1
    payload["workspace"] = resolved_workspace
    return payload


def serve_dashboard() -> None:
    server = ThreadingHTTPServer((config.DASHBOARD_HOST, config.DASHBOARD_PORT), DashboardHandler)
    print(f"Harness dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
