"""
Structured artifact layout for long-running harness work.

The harness keeps human-friendly root files (`spec.md`, `contract.md`, `feedback.md`,
`progress.md`) for agent compatibility, but also mirrors them into a structured
`.ai-harness/` tree so runs are auditable and resumable.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config


RUNBOOK_TEMPLATES = {
    "setup.md": """# Setup Runbook

## Purpose
Document the exact commands needed to install dependencies and boot the project.

## Checklist
1. Install Python requirements
2. Install browser/runtime dependencies
3. Install app dependencies (if any)
4. Start the local app
""",
    "test.md": """# Test Runbook

## Purpose
Document the exact commands needed to validate the project.

## Required Coverage
1. Build / compile
2. Automated tests
3. Browser QA path
4. Regression checks for the latest failed criteria
""",
    "release.md": """# Release Runbook

## Purpose
Document the release gate for this project.

## Release Conditions
1. All blocker counts are zero
2. All critical paths are browser-verified
3. QA report verdict is PASS
4. Handoff artifact is current
5. Decision log explains any scope tradeoffs
""",
}


@dataclass(frozen=True)
class ArtifactPaths:
    workspace: Path
    root: Path
    product_spec_dir: Path
    sprint_contracts_dir: Path
    qa_reports_dir: Path
    handoffs_dir: Path
    decision_log_dir: Path
    runbooks_dir: Path
    runtime_dir: Path
    spec_current: Path
    contract_current: Path
    feedback_current: Path
    progress_current: Path
    decision_log: Path

    @classmethod
    def for_workspace(cls, workspace: str | Path | None = None) -> "ArtifactPaths":
        ws = Path(workspace or config.WORKSPACE).resolve()
        root = ws / config.ARTIFACT_ROOT_NAME
        return cls(
            workspace=ws,
            root=root,
            product_spec_dir=root / "product-spec",
            sprint_contracts_dir=root / "sprint-contracts",
            qa_reports_dir=root / "qa-reports",
            handoffs_dir=root / "handoffs",
            decision_log_dir=root / "decision-log",
            runbooks_dir=root / "runbooks",
            runtime_dir=root / "runtime",
            spec_current=ws / config.SPEC_FILE,
            contract_current=ws / config.CONTRACT_FILE,
            feedback_current=ws / config.FEEDBACK_FILE,
            progress_current=ws / config.PROGRESS_FILE,
            decision_log=root / "decision-log" / "decisions.md",
        )


def ensure_workspace_layout(workspace: str | Path | None = None) -> ArtifactPaths:
    paths = ArtifactPaths.for_workspace(workspace)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    for directory in (
        paths.root,
        paths.product_spec_dir,
        paths.sprint_contracts_dir,
        paths.qa_reports_dir,
        paths.handoffs_dir,
        paths.decision_log_dir,
        paths.runbooks_dir,
        paths.runtime_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.decision_log.exists():
        paths.decision_log.write_text("# Decision Log\n\n", encoding="utf-8")

    for filename, template in RUNBOOK_TEMPLATES.items():
        target = paths.runbooks_dir / filename
        if not target.exists():
            target.write_text(template, encoding="utf-8")

    return paths


def append_decision(title: str, rationale: str, workspace: str | Path | None = None) -> Path:
    paths = ensure_workspace_layout(workspace)
    with paths.decision_log.open("a", encoding="utf-8") as handle:
        handle.write(f"## {title}\n")
        handle.write(f"- Rationale: {rationale}\n\n")
    return paths.decision_log


def sync_product_spec(workspace: str | Path | None = None) -> Path | None:
    paths = ensure_workspace_layout(workspace)
    if not paths.spec_current.exists():
        return None
    target = paths.product_spec_dir / "spec-v1.md"
    shutil.copyfile(paths.spec_current, target)
    return target


def sync_contract(round_num: int, workspace: str | Path | None = None) -> Path | None:
    paths = ensure_workspace_layout(workspace)
    if not paths.contract_current.exists():
        return None
    target = paths.sprint_contracts_dir / f"sprint-{round_num:02d}.md"
    shutil.copyfile(paths.contract_current, target)
    return target


def sync_qa_report(round_num: int, workspace: str | Path | None = None) -> Path | None:
    paths = ensure_workspace_layout(workspace)
    if not paths.feedback_current.exists():
        return None
    target = paths.qa_reports_dir / f"sprint-{round_num:02d}-qa.md"
    shutil.copyfile(paths.feedback_current, target)
    return target


def write_round_handoff(
    round_num: int,
    payload: dict,
    workspace: str | Path | None = None,
) -> Path:
    paths = ensure_workspace_layout(workspace)
    target = paths.handoffs_dir / f"round-{round_num:02d}.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return target


def latest_handoff_path(workspace: str | Path | None = None) -> Path:
    paths = ensure_workspace_layout(workspace)
    return paths.handoffs_dir / "latest.json"


def write_latest_handoff(payload: dict, workspace: str | Path | None = None) -> Path:
    target = latest_handoff_path(workspace)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return target


def resume_state_path(workspace: str | Path | None = None) -> Path:
    paths = ensure_workspace_layout(workspace)
    return paths.runtime_dir / "resume-state.json"


def read_resume_state(workspace: str | Path | None = None) -> dict[str, Any]:
    target = resume_state_path(workspace)
    if not target.exists():
        return {
            "status": "idle",
            "next_phase": "planning",
            "next_round": 1,
            "message": "",
            "updated_at": None,
            "prompt": "",
        }
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {
            "status": "unknown",
            "next_phase": "planning",
            "next_round": 1,
            "message": "resume-state.json could not be parsed",
            "updated_at": None,
            "prompt": "",
        }


def write_resume_state(
    workspace: str | Path | None = None,
    **fields: Any,
) -> dict[str, Any]:
    target = resume_state_path(workspace)
    current = read_resume_state(workspace)
    current.update(fields)
    current["updated_at"] = time.time()
    target.write_text(json.dumps(current, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return current
