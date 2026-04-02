"""
Runtime state and process telemetry shared by the CLI and the dashboard.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import config


RUNTIME_ROOT = (Path(__file__).parent / config.ARTIFACT_ROOT_NAME / "runtime").resolve()
STATE_FILE = RUNTIME_ROOT / "state.json"
LOG_FILE = RUNTIME_ROOT / "harness.log"
EVENTS_FILE = RUNTIME_ROOT / "events.jsonl"


def ensure_runtime_root() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)


def _default_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "phase": "",
        "round": 0,
        "pid": None,
        "workspace": "",
        "prompt": "",
        "active_agent": "",
        "start_time": None,
        "last_update": None,
        "message": "",
        "compactions": 0,
        "resets": 0,
        "current_run_command": "",
        "current_run_argv": [],
    }


def read_state() -> dict[str, Any]:
    ensure_runtime_root()
    if not STATE_FILE.exists():
        return _default_state()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()


def write_state(**fields: Any) -> dict[str, Any]:
    ensure_runtime_root()
    current = read_state()
    current.update(fields)
    current["last_update"] = time.time()
    STATE_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return current


def reset_state() -> dict[str, Any]:
    ensure_runtime_root()
    state = _default_state()
    state["last_update"] = time.time()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return state


def increment_counter(name: str, amount: int = 1) -> dict[str, Any]:
    current = read_state()
    current[name] = int(current.get(name, 0) or 0) + amount
    return write_state(**current)


def append_event(kind: str, message: str, **data: Any) -> None:
    ensure_runtime_root()
    payload = {
        "ts": time.time(),
        "kind": kind,
        "message": message,
        "data": data,
    }
    with EVENTS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def read_log_slice(offset: int = 0) -> tuple[str, int]:
    ensure_runtime_root()
    if not LOG_FILE.exists():
        return "", 0
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    if offset < 0:
        offset = 0
    return text[offset:], len(text)
