"""
Harness configuration and environment metadata.

This module keeps two things in one place:
1. Runtime settings loaded from `.env`
2. A typed schema for editing those settings from the dashboard
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).parent.resolve()
ENV_FILE = REPO_ROOT / ".env"


@dataclass(frozen=True)
class EnvSpec:
    name: str
    label: str
    default: str
    description: str
    group: str
    secret: bool = False
    advanced: bool = False


ENV_SPECS = [
    EnvSpec(
        name="OPENAI_API_KEY",
        label="API Key",
        default="",
        description="OpenAI-compatible API key used by all agents.",
        group="API",
        secret=True,
    ),
    EnvSpec(
        name="OPENAI_BASE_URL",
        label="Base URL",
        default="https://api.openai.com/v1",
        description="Base URL for the OpenAI-compatible API provider.",
        group="API",
    ),
    EnvSpec(
        name="HARNESS_MODEL",
        label="Model",
        default="gpt-4o",
        description="Default model used by planner, builder, evaluator, and contract agents.",
        group="API",
    ),
    EnvSpec(
        name="API_REQUEST_TIMEOUT_SECONDS",
        label="API Request Timeout",
        default="600",
        description="Timeout in seconds for a single LLM API request before the harness treats it as transient failure.",
        group="API",
        advanced=True,
    ),
    EnvSpec(
        name="API_RECOVERY_POLL_SECONDS",
        label="API Recovery Poll",
        default="15",
        description="Seconds between connectivity checks while waiting for network recovery.",
        group="API",
        advanced=True,
    ),
    EnvSpec(
        name="API_MAX_RECOVERY_WAIT_SECONDS",
        label="API Recovery Max Wait",
        default="0",
        description="Maximum seconds to wait for API recovery before aborting. Use 0 to wait indefinitely.",
        group="API",
        advanced=True,
    ),
    EnvSpec(
        name="API_RETRY_BACKOFF_SECONDS",
        label="API Retry Backoff",
        default="5",
        description="Base delay in seconds between retries for transient API failures such as rate limits or 5xx errors.",
        group="API",
        advanced=True,
    ),
    EnvSpec(
        name="API_RETRY_MAX_BACKOFF_SECONDS",
        label="API Retry Max Backoff",
        default="60",
        description="Maximum delay in seconds between retries for transient API failures.",
        group="API",
        advanced=True,
    ),
    EnvSpec(
        name="HARNESS_WORKSPACE",
        label="Workspace Root",
        default="./workspace",
        description="Parent directory used for project workspaces.",
        group="Runtime",
    ),
    EnvSpec(
        name="HARNESS_ARTIFACT_ROOT",
        label="Artifact Root",
        default=".ai-harness",
        description="Structured artifact directory name created inside each workspace.",
        group="Runtime",
    ),
    EnvSpec(
        name="HARNESS_DASHBOARD_HOST",
        label="Dashboard Host",
        default="127.0.0.1",
        description="Host used by the local dashboard server.",
        group="Runtime",
    ),
    EnvSpec(
        name="HARNESS_DASHBOARD_PORT",
        label="Dashboard Port",
        default="8765",
        description="Port used by the local dashboard server.",
        group="Runtime",
    ),
    EnvSpec(
        name="ENABLE_PLANNER",
        label="Enable Planner",
        default="true",
        description="Whether new runs start with planner-generated product specs.",
        group="Orchestration",
    ),
    EnvSpec(
        name="MAX_HARNESS_ROUNDS",
        label="Max Harness Rounds",
        default="8",
        description="Maximum build/evaluate rounds before the orchestrator stops.",
        group="Orchestration",
    ),
    EnvSpec(
        name="RELEASE_READY_SCORE",
        label="Release Score Threshold",
        default="9.0",
        description="Average score required before a run may be marked release-ready.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="CORE_SCORE_FLOOR",
        label="Core Criterion Floor",
        default="8.5",
        description="Any core QA criterion below this fails the run.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="STRICT_CORRECTNESS_FLOOR",
        label="Correctness Floor",
        default="9.0",
        description="Minimum functional correctness score for release.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="STRICT_OPERABILITY_FLOOR",
        label="Operability Floor",
        default="9.0",
        description="Minimum operability score for release.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="MAX_CRITICAL_BUGS",
        label="Max Critical Bugs",
        default="0",
        description="Maximum allowed critical bugs before automatic fail.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="MAX_MAJOR_BUGS",
        label="Max Major Bugs",
        default="0",
        description="Maximum allowed major bugs before automatic fail.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="REQUIRE_FULL_SPEC_COVERAGE",
        label="Require Full Spec Coverage",
        default="true",
        description="If true, partial spec coverage fails the run.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="REQUIRE_BROWSER_VERIFICATION",
        label="Require Browser Verification",
        default="true",
        description="If true, evaluator must provide browser evidence for acceptance.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="BLOCK_PLACEHOLDER_UI",
        label="Block Placeholder UI",
        default="true",
        description="If true, placeholder or deferred UI language fails the run.",
        group="Quality Gates",
    ),
    EnvSpec(
        name="COMPRESS_THRESHOLD",
        label="Compaction Threshold",
        default="80000",
        description="Approximate token count at which a live agent compacts context.",
        group="Context",
        advanced=True,
    ),
    EnvSpec(
        name="RESET_THRESHOLD",
        label="Reset Threshold",
        default="150000",
        description="Approximate token count at which a live agent forces reset + handoff.",
        group="Context",
        advanced=True,
    ),
    EnvSpec(
        name="MAX_AGENT_ITERATIONS",
        label="Max Agent Iterations",
        default="60",
        description="Maximum inner-loop iterations before aborting an agent.",
        group="Context",
        advanced=True,
    ),
]


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int_env(name: str) -> int:
    raw = os.environ.get(name, _spec_default(name))
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid integer value for {name}: {raw!r}. "
            f"Update {ENV_FILE} and use a plain integer such as {_spec_default(name)!r}."
        ) from exc


def _get_float_env(name: str) -> float:
    raw = os.environ.get(name, _spec_default(name))
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid numeric value for {name}: {raw!r}. "
            f"Update {ENV_FILE} and use a plain number such as {_spec_default(name)!r}."
        ) from exc


def _spec_default(name: str) -> str:
    for spec in ENV_SPECS:
        if spec.name == name:
            return spec.default
    return ""


def _load_dotenv() -> None:
    """Load `.env` with file values taking priority over inherited shell values."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            os.environ[key] = value


def read_env_file_values() -> dict[str, str]:
    values = {spec.name: spec.default for spec in ENV_SPECS}
    if not ENV_FILE.exists():
        return values

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in values:
            values[key] = value.strip()
    return values


def write_env_file_values(values: dict[str, str]) -> None:
    lines = [
        "# Managed by the AI Harness dashboard and CLI.",
        "# Values here override inherited shell variables for local runs.",
        "",
    ]

    groups: dict[str, list[EnvSpec]] = {}
    for spec in ENV_SPECS:
        groups.setdefault(spec.group, []).append(spec)

    for group, specs in groups.items():
        lines.append(f"# --- {group} ---")
        for spec in specs:
            value = values.get(spec.name, _spec_default(spec.name))
            lines.append(f"{spec.name}={value}")
        lines.append("")

    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    _load_dotenv()


_load_dotenv()

# --- API ---
API_KEY = os.environ.get("OPENAI_API_KEY", _spec_default("OPENAI_API_KEY"))
BASE_URL = os.environ.get("OPENAI_BASE_URL", _spec_default("OPENAI_BASE_URL"))
MODEL = os.environ.get("HARNESS_MODEL", _spec_default("HARNESS_MODEL"))
API_REQUEST_TIMEOUT_SECONDS = _get_int_env("API_REQUEST_TIMEOUT_SECONDS")
API_RECOVERY_POLL_SECONDS = _get_int_env("API_RECOVERY_POLL_SECONDS")
API_MAX_RECOVERY_WAIT_SECONDS = _get_int_env("API_MAX_RECOVERY_WAIT_SECONDS")
API_RETRY_BACKOFF_SECONDS = _get_int_env("API_RETRY_BACKOFF_SECONDS")
API_RETRY_MAX_BACKOFF_SECONDS = _get_int_env("API_RETRY_MAX_BACKOFF_SECONDS")

# --- Paths / layout ---
WORKSPACE = os.path.abspath(os.environ.get("HARNESS_WORKSPACE", _spec_default("HARNESS_WORKSPACE")))
ARTIFACT_ROOT_NAME = os.environ.get("HARNESS_ARTIFACT_ROOT", _spec_default("HARNESS_ARTIFACT_ROOT"))
DASHBOARD_HOST = os.environ.get("HARNESS_DASHBOARD_HOST", _spec_default("HARNESS_DASHBOARD_HOST"))
DASHBOARD_PORT = _get_int_env("HARNESS_DASHBOARD_PORT")

# --- Context lifecycle ---
COMPRESS_THRESHOLD = _get_int_env("COMPRESS_THRESHOLD")
RESET_THRESHOLD = _get_int_env("RESET_THRESHOLD")
MAX_AGENT_ITERATIONS = _get_int_env("MAX_AGENT_ITERATIONS")
MAX_TOOL_ERRORS = 5

# --- Orchestration ---
ENABLE_PLANNER = _get_bool_env("ENABLE_PLANNER", True)
MAX_HARNESS_ROUNDS = _get_int_env("MAX_HARNESS_ROUNDS")

# --- Quality gates ---
RELEASE_READY_SCORE = _get_float_env("RELEASE_READY_SCORE")
CORE_SCORE_FLOOR = _get_float_env("CORE_SCORE_FLOOR")
STRICT_CORRECTNESS_FLOOR = _get_float_env("STRICT_CORRECTNESS_FLOOR")
STRICT_OPERABILITY_FLOOR = _get_float_env("STRICT_OPERABILITY_FLOOR")
MAX_CRITICAL_BUGS = _get_int_env("MAX_CRITICAL_BUGS")
MAX_MAJOR_BUGS = _get_int_env("MAX_MAJOR_BUGS")
REQUIRE_FULL_SPEC_COVERAGE = _get_bool_env("REQUIRE_FULL_SPEC_COVERAGE", True)
REQUIRE_BROWSER_VERIFICATION = _get_bool_env("REQUIRE_BROWSER_VERIFICATION", True)
BLOCK_PLACEHOLDER_UI = _get_bool_env("BLOCK_PLACEHOLDER_UI", True)

# Backward-compatible aliases used by existing code/tests
PASS_THRESHOLD = RELEASE_READY_SCORE
MIN_FUNCTIONALITY_SCORE = STRICT_CORRECTNESS_FLOOR

# --- Workspace root-level convenience files ---
SPEC_FILE = "spec.md"
FEEDBACK_FILE = "feedback.md"
CONTRACT_FILE = "contract.md"
PROGRESS_FILE = "progress.md"
