#!/usr/bin/env python3
"""
High-standard engineering harness.

This version is artifact-driven and long-run oriented:
- explicit Product Spec
- contract-first sprinting
- independent evaluator
- structured handoff artifacts
- runtime telemetry for the dashboard
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
import prompts
import tools
from agents import Agent, extract_primary_choice
from artifacts import (
    append_decision,
    ensure_workspace_layout,
    read_resume_state,
    sync_contract,
    sync_product_spec,
    sync_qa_report,
    write_resume_state,
    write_latest_handoff,
    write_round_handoff,
)
from runtime_state import append_event, reset_state, write_state
from skills import SkillRegistry

log = logging.getLogger("harness")


@dataclass
class EvaluationReport:
    average_score: float = 0.0
    functionality_score: float = 0.0
    verdict: str = "FAIL"
    feedback_round: int = 0
    contract_round: int = 0
    contract_criteria_total: int = 0
    spec_coverage: str = "UNKNOWN"
    contract_coverage: str = "UNKNOWN"
    build_verification: str = "UNKNOWN"
    browser_verification: str = "UNKNOWN"
    placeholder_ui: str = "UNKNOWN"
    critical_bugs: int = 999
    major_bugs: int = 999
    minor_bugs: int = 999
    criteria_passed: int = 0
    criteria_total: int = 0
    untested_criteria: int = 999
    blockers: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    feature_completeness: float = 0.0
    functional_correctness: float = 0.0
    product_depth: float = 0.0
    ux_quality: float = 0.0
    code_quality: float = 0.0
    operability: float = 0.0


@dataclass
class ResumePlan:
    round_num: int = 1
    phase: str = "contract"
    source: str = "feedback"


class Harness:
    """Planner -> contract -> builder -> evaluator loop with hard release gates."""

    def __init__(self):
        self.skill_registry = SkillRegistry()
        skill_catalog = self.skill_registry.build_catalog_prompt()
        self._active_round = 0
        self._active_phase = "bootstrap"
        self._run_prompt = ""
        self._signal_handlers: dict[int, object] = {}

        self.planner = Agent("planner", prompts.PLANNER_SYSTEM + skill_catalog, use_tools=True)
        self.builder = Agent("builder", prompts.BUILDER_SYSTEM + skill_catalog, use_tools=True)
        self.evaluator = Agent(
            "evaluator",
            prompts.EVALUATOR_SYSTEM,
            use_tools=True,
            extra_tool_schemas=tools.BROWSER_TOOL_SCHEMAS,
        )
        self.contract_proposer = Agent(
            "contract_proposer",
            prompts.CONTRACT_BUILDER_SYSTEM,
            use_tools=True,
        )
        self.contract_reviewer = Agent(
            "contract_reviewer",
            prompts.CONTRACT_REVIEWER_SYSTEM,
            use_tools=True,
        )

    def _persist_resume_point(
        self,
        next_phase: str,
        next_round: int,
        *,
        status: str = "running",
        message: str = "",
    ) -> None:
        self._active_phase = next_phase
        self._active_round = max(int(next_round or 1), 1)
        write_resume_state(
            config.WORKSPACE,
            status=status,
            next_phase=next_phase,
            next_round=self._active_round,
            message=message,
            prompt=self._run_prompt,
        )

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._signal_handlers.items():
            signal.signal(signum, previous)
        self._signal_handlers.clear()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            signal_name = signal.Signals(signum).name
            phase = self._active_phase or "contract"
            round_num = max(self._active_round, 1)
            reason = f"Interrupted by {signal_name} during {phase} phase of round {round_num}."
            try:
                self._persist_resume_point(
                    phase,
                    round_num,
                    status="interrupted",
                    message=reason,
                )
            except Exception:
                pass
            append_event(
                "run_interrupted",
                "Harness interrupted by signal",
                signal=signal_name,
                phase=phase,
                round=round_num,
                workspace=config.WORKSPACE,
            )
            tools.stop_dev_server()
            raise KeyboardInterrupt(reason)

        for signum in (signal.SIGINT, signal.SIGTERM):
            self._signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)

    def run(
        self,
        user_prompt: str,
        resume_dir: str | None = None,
        skip_planning: bool | None = None,
    ) -> None:
        project_dir = self._resolve_project_dir(user_prompt, resume_dir)
        config.WORKSPACE = os.path.abspath(project_dir)
        ensure_workspace_layout(config.WORKSPACE)
        resume_plan = self._determine_resume_plan(resume_dir)
        self._run_prompt = user_prompt
        self._active_phase = "bootstrap"
        self._active_round = max(resume_plan.round_num, 1)

        total_start = time.time()
        reset_state()
        write_state(
            status="running",
            phase="bootstrap",
            workspace=config.WORKSPACE,
            prompt=user_prompt,
            start_time=total_start,
            current_run_command=" ".join(sys.argv),
            message="Harness bootstrapping",
        )
        append_event("run_started", "Harness run started", workspace=config.WORKSPACE)
        append_decision(
            "Harness Mode",
            "Full multi-agent harness enabled because the task is treated as a long-running engineering workflow.",
            config.WORKSPACE,
        )

        self._ensure_git_repo()

        spec_path = Path(config.WORKSPACE) / config.SPEC_FILE
        if skip_planning is None:
            skip_planning = not config.ENABLE_PLANNER or (bool(resume_dir) and spec_path.exists())

        log.info(f"Project directory: {config.WORKSPACE}{' (resume mode)' if resume_dir else ''}")
        if resume_dir:
            log.info(
                f"Resume plan: start from {resume_plan.phase} phase of round {resume_plan.round_num} "
                f"(source={resume_plan.source})."
            )

        aborted_reason = ""
        last_report: EvaluationReport | None = None
        was_interrupted = False
        self._install_signal_handlers()

        try:
            # Phase 1: Planning
            log.info("=" * 60)
            log.info("PHASE 1: PLANNING")
            log.info("=" * 60)
            write_state(phase="planning", message="Planning phase")
            if resume_plan.phase == "planning":
                self._persist_resume_point("planning", 1, message="Planning phase")
            plan_start = time.time()
            if skip_planning:
                append_decision(
                    "Planning",
                    "Planner skipped because an existing spec or explicit skip-planner request was provided.",
                    config.WORKSPACE,
                )
                if spec_path.exists():
                    log.info(f"Skipping planning; using existing {config.SPEC_FILE}.")
                elif user_prompt.strip():
                    spec_path.write_text(user_prompt, encoding="utf-8")
                    log.info(f"Skipping planning; wrote provided prompt directly to {config.SPEC_FILE}.")
                else:
                    raise RuntimeError("Cannot skip planning without an existing spec or fallback prompt.")
            else:
                append_decision(
                    "Planning",
                    "Planner enabled to turn the user prompt into a durable product specification before coding begins.",
                    config.WORKSPACE,
                )
                self.planner.run(
                    "Create the product specification for this request and save it to spec.md.\n\n"
                    f"{user_prompt}"
                )
            sync_product_spec(config.WORKSPACE)
            log.info(f"Planning completed in {time.time() - plan_start:.0f}s")

            if resume_plan.phase == "planning":
                resume_plan = ResumePlan(round_num=1, phase="contract", source="resume_checkpoint")

            score_history = self._load_existing_score_history()
            start_round = resume_plan.round_num

            for round_offset in range(config.MAX_HARNESS_ROUNDS):
                round_num = start_round + round_offset
                round_start_phase = resume_plan.phase if round_offset == 0 else "contract"

                if round_start_phase == "contract":
                    log.info("=" * 60)
                    log.info(f"ROUND {round_num}/{config.MAX_HARNESS_ROUNDS}: CONTRACT NEGOTIATION")
                    log.info("=" * 60)
                    write_state(phase="contract", round=round_num, message=f"Negotiating contract for round {round_num}")
                    self._persist_resume_point(
                        "contract",
                        round_num,
                        message=f"Contract negotiation for round {round_num}",
                    )
                    contract_start = time.time()
                    self._negotiate_contract(round_num)
                    sync_contract(round_num, config.WORKSPACE)
                    append_decision(
                        f"Round {round_num} Contract",
                        "Sprint contract negotiated before implementation. Coding is gated behind a reviewed contract.",
                        config.WORKSPACE,
                    )
                    log.info(f"Contract negotiation completed in {time.time() - contract_start:.0f}s")
                    round_start_phase = "build"
                else:
                    log.info(f"Resuming round {round_num} from {round_start_phase}; skipping earlier round phases.")

                if round_start_phase == "build":
                    log.info("=" * 60)
                    log.info(f"ROUND {round_num}/{config.MAX_HARNESS_ROUNDS}: BUILD")
                    log.info("=" * 60)
                    write_state(phase="build", round=round_num, message=f"Builder working on round {round_num}")
                    self._persist_resume_point(
                        "build",
                        round_num,
                        message=f"Build phase for round {round_num}",
                    )
                    build_start = time.time()
                    build_task = self._build_task(round_num, score_history)
                    self.builder.run(build_task)
                    log.info(f"Build round {round_num} completed in {time.time() - build_start:.0f}s")

                    if not self.builder.last_run_success:
                        aborted_reason = f"Build round {round_num} failed ({self.builder.last_stop_reason})."
                        append_decision(
                            f"Round {round_num} Abort",
                            aborted_reason + " Evaluation skipped because the builder did not finish cleanly.",
                            config.WORKSPACE,
                        )
                        self._persist_resume_point(
                            "build",
                            round_num,
                            status="aborted",
                            message=aborted_reason,
                        )
                        log.error(aborted_reason)
                        tools.stop_dev_server()
                        self._write_round_handoff(round_num, None, aborted_reason)
                        break

                    round_start_phase = "evaluate"

                if round_start_phase == "evaluate":
                    log.info("=" * 60)
                    log.info(f"ROUND {round_num}/{config.MAX_HARNESS_ROUNDS}: EVALUATE")
                    log.info("=" * 60)
                    write_state(phase="evaluate", round=round_num, message=f"Evaluator running for round {round_num}")
                    self._persist_resume_point(
                        "evaluate",
                        round_num,
                        message=f"Evaluation phase for round {round_num}",
                    )
                    eval_start = time.time()
                    self.evaluator.run(self._evaluation_task(round_num))
                    tools.stop_dev_server()
                    log.info(f"Evaluation round {round_num} completed in {time.time() - eval_start:.0f}s")

                    if not self.evaluator.last_run_success:
                        aborted_reason = f"Evaluation round {round_num} failed ({self.evaluator.last_stop_reason})."
                        append_decision(
                            f"Round {round_num} Abort",
                            aborted_reason + " The run can resume from evaluator on the same round.",
                            config.WORKSPACE,
                        )
                        self._persist_resume_point(
                            "evaluate",
                            round_num,
                            status="aborted",
                            message=aborted_reason,
                        )
                        log.error(aborted_reason)
                        self._write_round_handoff(round_num, None, aborted_reason)
                        break

                    report = self._extract_evaluation_report()
                    last_report = report
                    score_history.append(report.average_score)
                    sync_qa_report(round_num, config.WORKSPACE)
                    self._write_round_handoff(round_num, report, "")

                    log.info(
                        f"Round {round_num} average score: {report.average_score:.1f} / 10  "
                        f"(release threshold: {config.RELEASE_READY_SCORE:.1f})"
                    )
                    log.info(f"Score history: {score_history}")

                    passed, blockers = self._passes_release_gates(report)
                    if passed:
                        append_decision(
                            f"Round {round_num} Release Gate",
                            "All configured release gates passed.",
                            config.WORKSPACE,
                        )
                        self._persist_resume_point(
                            "complete",
                            round_num,
                            status="passed",
                            message=f"Passed release gates at round {round_num}",
                        )
                        write_state(
                            status="passed",
                            phase="complete",
                            round=round_num,
                            message=f"Passed release gates at round {round_num}",
                        )
                        log.info(f"PASSED QA at round {round_num}.")
                        break

                    self._persist_resume_point(
                        "contract",
                        round_num + 1,
                        status="running",
                        message=f"Continuing to remediation round {round_num + 1}",
                    )
                    append_decision(
                        f"Round {round_num} Release Gate",
                        "Round failed release gates and must continue as a remediation round.",
                        config.WORKSPACE,
                    )
                    for blocker in blockers:
                        log.warning(f"Release gate failed: {blocker}")
                    continue

                raise RuntimeError(f"Unsupported resume phase: {round_start_phase}")
            else:
                aborted_reason = f"Did not pass QA after {config.MAX_HARNESS_ROUNDS} rounds."
                self._persist_resume_point(
                    "contract",
                    start_round + config.MAX_HARNESS_ROUNDS,
                    status="failed",
                    message=aborted_reason,
                )
                write_state(status="failed", phase="complete", message=aborted_reason)
                log.warning(aborted_reason)

        except KeyboardInterrupt as exc:
            was_interrupted = True
            aborted_reason = str(exc) or "Harness interrupted."
            self._persist_resume_point(
                self._active_phase or "contract",
                max(self._active_round, 1),
                status="interrupted",
                message=aborted_reason,
            )
            if self._active_round:
                self._write_round_handoff(self._active_round, None, aborted_reason)
            write_state(status="interrupted", phase="complete", round=max(self._active_round, 1), message=aborted_reason)
            log.warning(aborted_reason)
        finally:
            self._restore_signal_handlers()
            tools.stop_dev_server()

        total_duration = time.time() - total_start
        if aborted_reason and last_report is None and not was_interrupted:
            write_state(status="aborted", phase="complete", message=aborted_reason)
        if last_report and not aborted_reason and write_state:
            write_state(message="Harness finished", phase="complete")
        append_event("run_finished", "Harness run finished", duration_seconds=total_duration)
        log.info("=" * 60)
        log.info(f"HARNESS COMPLETE — total time: {total_duration / 60:.1f} minutes")
        if aborted_reason:
            log.warning(f"Harness stopped early: {aborted_reason}")
        log.info(f"Output in: {config.WORKSPACE}")
        log.info("=" * 60)

    def _resolve_project_dir(self, user_prompt: str, resume_dir: str | None) -> str:
        if resume_dir:
            return os.path.abspath(resume_dir)
        from datetime import datetime

        slug = re.sub(r"[^a-z0-9]+", "-", user_prompt.lower().strip())[:40].strip("-")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        project_name = f"{timestamp}_{slug}" if slug else timestamp
        return os.path.join(config.WORKSPACE, project_name)

    def _ensure_git_repo(self) -> None:
        git_dir = Path(config.WORKSPACE) / ".git"
        if git_dir.exists():
            return
        os.system(
            f"cd {config.WORKSPACE} && "
            "git init >/dev/null 2>&1 && "
            "git add -A >/dev/null 2>&1 && "
            "git commit -m 'init' --allow-empty >/dev/null 2>&1"
        )

    def _build_task(self, round_num: int, score_history: list[float]) -> str:
        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        prev_feedback = feedback_path.read_text(encoding="utf-8") if feedback_path.exists() else ""

        trend_info = ""
        if len(score_history) >= 2:
            delta = score_history[-1] - score_history[-2]
            direction = "IMPROVING" if delta > 0 else "DECLINING" if delta < 0 else "STAGNANT"
            trend_info = f"Score trend: {direction} ({delta:+.1f}). History: {score_history}"
        elif len(score_history) == 1:
            trend_info = f"Last score: {score_history[0]:.1f}/10"

        task = [
            f"This is Builder round {round_num}.",
            "Read spec.md, contract.md, progress.md, and feedback.md if present.",
            "Work inside the current workspace.",
            "The framework is contract-first and evidence-driven.",
            "You are not allowed to treat self-reported completion as release completion.",
            "Update progress.md before stopping.",
        ]

        if prev_feedback:
            task.extend(
                [
                    "This round is a remediation round.",
                    "This is a remediation-first round.",
                    "Fix the latest Blocking Reasons, failed contract criteria, and listed bugs before adding new scope.",
                    "Do not spend this round on speculative enhancements until the latest feedback blockers are resolved.",
                    trend_info,
                ]
            )
        else:
            task.append("This is the first implementation round for this workspace.")

        task.extend(
            [
                "When you finish coding, run real build/test commands.",
                "Do not hide unfinished work behind placeholders.",
                "Write real code files, not just summaries.",
                "Commit your work with git when the round is complete.",
            ]
        )
        return "\n".join(task)

    def _evaluation_task(self, round_num: int) -> str:
        return "\n".join(
            [
                f"This is QA round {round_num}.",
                "Read spec.md, contract.md, progress.md, and feedback.md if it exists.",
                "Verify every contract criterion against the running system.",
                "Use browser_test with meaningful interactions.",
                "Do not infer correctness from code alone.",
                "Write the QA report to feedback.md and stop the dev server when done.",
            ]
        )

    def _negotiate_contract(self, round_num: int, max_iterations: int = 3) -> None:
        self.contract_proposer.run(
            f"This is round {round_num}. Read spec.md and feedback.md if it exists. "
            "Write the sprint contract to contract.md."
        )
        for iteration in range(max_iterations):
            log.info(f"[contract] Review iteration {iteration + 1}/{max_iterations}")
            self.contract_reviewer.run(
                f"Review the contract in contract.md for round {round_num}. "
                "Approve only if it is specific, honest, and testable."
            )
            contract_path = Path(config.WORKSPACE) / config.CONTRACT_FILE
            if contract_path.exists():
                contract_text = contract_path.read_text(encoding="utf-8")
                if "APPROVED" in contract_text.upper()[:200]:
                    log.info("[contract] Contract approved.")
                    return
            if iteration < max_iterations - 1:
                log.info("[contract] Contract needs revision, builder revising...")
                self.contract_proposer.run(
                    "The contract reviewer requested changes. Read contract.md, revise it, and save contract.md again."
                )
        log.warning("[contract] Max iterations reached; proceeding with current contract.")

    def _write_round_handoff(
        self,
        round_num: int,
        report: EvaluationReport | None,
        aborted_reason: str,
    ) -> None:
        progress_path = Path(config.WORKSPACE) / config.PROGRESS_FILE
        progress_text = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
        payload = {
            "round": round_num,
            "workspace": config.WORKSPACE,
            "branch": self._git_output("git rev-parse --abbrev-ref HEAD"),
            "commit": self._git_output("git rev-parse HEAD"),
            "completed_items": self._extract_checklist_items(progress_text, done=True),
            "incomplete_items": self._extract_checklist_items(progress_text, done=False),
            "failure_reasons": report.blocking_reasons if report else ([aborted_reason] if aborted_reason else []),
            "key_files": self._git_output("git diff --name-only HEAD~1 2>/dev/null || git status --short").splitlines(),
            "run_instructions": [
                "python harness.py --resume-dir <workspace> --skip-planner \"Close remaining blockers\"",
                "python harness.py dashboard",
            ],
            "known_issues": report.blocking_reasons if report else ([] if not aborted_reason else [aborted_reason]),
            "next_priorities": (
                report.blocking_reasons[:5]
                if report and report.blocking_reasons
                else self._extract_priority_lines(progress_text)
            ),
            "evaluator_failures": report.blocking_reasons if report else [],
            "restore_instructions": [
                "Read spec.md, contract.md, feedback.md, and progress.md before continuing.",
                "Use .ai-harness/handoffs/latest.json as the first recovery artifact.",
            ],
        }
        write_round_handoff(round_num, payload, config.WORKSPACE)
        write_latest_handoff(payload, config.WORKSPACE)

    @staticmethod
    def _git_output(command: str) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=config.WORKSPACE,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() or result.stderr.strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_checklist_items(progress_text: str, done: bool) -> list[str]:
        if not progress_text:
            return []
        items: list[str] = []
        for line in progress_text.splitlines():
            stripped = line.strip()
            if done and any(marker in stripped for marker in ("VERIFIED", "DONE")):
                items.append(stripped)
            if not done and any(marker in stripped for marker in ("NOT DONE", "SELF-CHECKED")):
                items.append(stripped)
        return items[:20]

    @staticmethod
    def _extract_priority_lines(progress_text: str) -> list[str]:
        items = []
        for line in progress_text.splitlines():
            stripped = line.strip()
            if re.match(r"^\d+\.", stripped):
                items.append(stripped)
        return items[:10]

    def _load_existing_score_history(self) -> list[float]:
        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        if not feedback_path.exists():
            return []
        report = self._extract_evaluation_report()
        return [report.average_score] if report.average_score > 0 else []

    def _determine_resume_plan(self, resume_dir: str | None) -> ResumePlan:
        if not resume_dir:
            return ResumePlan(round_num=1, phase="contract", source="new_run")

        checkpoint = read_resume_state(config.WORKSPACE)
        phase = str(checkpoint.get("next_phase") or "").strip().lower()
        status = str(checkpoint.get("status") or "").strip().lower()
        try:
            round_num = int(checkpoint.get("next_round") or 0)
        except (TypeError, ValueError):
            round_num = 0

        if status in {"running", "interrupted", "aborted"} and phase in {"planning", "contract", "build", "evaluate"} and round_num >= 1:
            return ResumePlan(round_num=round_num, phase=phase, source="resume_checkpoint")

        return ResumePlan(
            round_num=self._detect_start_round(),
            phase="contract",
            source="feedback_history",
        )

    def _detect_start_round(self) -> int:
        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        if not feedback_path.exists():
            return 1
        text = feedback_path.read_text(encoding="utf-8")
        match = re.search(r"(?i)##\s+QA Evaluation\s+[—-]\s+Round\s+(\d+)", text)
        return int(match.group(1)) + 1 if match else 1

    def _extract_evaluation_report(self) -> EvaluationReport:
        feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
        if not feedback_path.exists():
            return EvaluationReport(blockers=["feedback.md was not created"])

        text = feedback_path.read_text(encoding="utf-8")
        contract_text = self._read_contract_text()
        feature_completeness = self._extract_named_score(text, "Feature Completeness")
        functional_correctness = (
            self._extract_named_score(text, "Functional Correctness")
            or self._extract_named_score(text, "Functionality")
        )
        product_depth = self._extract_named_score(text, "Product Depth") or self._extract_named_score(text, "Originality")
        ux_quality = (
            self._extract_named_score(text, "UX / Visual Quality")
            or self._extract_named_score(text, "Design Quality")
        )
        code_quality = self._extract_named_score(text, "Code Quality") or self._extract_named_score(text, "Craft")
        operability = self._extract_named_score(text, "Operability")

        report = EvaluationReport(
            average_score=self._extract_score(text),
            functionality_score=functional_correctness,
            functional_correctness=functional_correctness,
            feature_completeness=feature_completeness,
            product_depth=product_depth,
            ux_quality=ux_quality,
            code_quality=code_quality,
            operability=operability,
            verdict=self._extract_line_value(text, "Verdict").upper() or "FAIL",
            feedback_round=self._extract_round_number(text, "QA Evaluation"),
            contract_round=self._extract_round_number(contract_text, "Sprint Contract"),
            contract_criteria_total=self._count_contract_acceptance_criteria(contract_text),
            spec_coverage=self._extract_line_value(text, "Spec Coverage").upper() or "UNKNOWN",
            contract_coverage=self._extract_line_value(text, "Contract Coverage").upper() or "UNKNOWN",
            build_verification=self._extract_line_value(text, "Build Verification").upper() or "UNKNOWN",
            browser_verification=self._extract_line_value(text, "Browser Verification").upper() or "UNKNOWN",
            placeholder_ui=self._extract_line_value(text, "Placeholder UI").upper() or "UNKNOWN",
            critical_bugs=self._extract_int_value(text, "Critical Bugs", default=999),
            major_bugs=self._extract_int_value(text, "Major Bugs", default=999),
            minor_bugs=self._extract_int_value(text, "Minor Bugs", default=999),
            untested_criteria=self._extract_int_value(text, "Untested Criteria", default=999),
        )

        criteria_value = self._extract_line_value(text, "Acceptance Criteria Passed")
        criteria_match = re.search(r"(\d+)\s*/\s*(\d+)", criteria_value)
        if criteria_match:
            report.criteria_passed = int(criteria_match.group(1))
            report.criteria_total = int(criteria_match.group(2))

        if report.verdict not in {"PASS", "FAIL"}:
            report.blockers.append("QA report is missing a valid Verdict line")
        if report.feedback_round == 0:
            report.blockers.append("QA report is missing a parsable round number")
        if report.criteria_total == 0:
            report.blockers.append("QA report is missing Acceptance Criteria Passed totals")
        if report.browser_verification == "UNKNOWN":
            report.blockers.append("QA report is missing Browser Verification status")
        if report.spec_coverage == "UNKNOWN":
            report.blockers.append("QA report is missing Spec Coverage status")
        if report.contract_round == 0:
            report.blockers.append("contract.md is missing a parsable round number")
        if report.contract_criteria_total == 0:
            report.blockers.append("contract.md is missing numbered Acceptance Criteria")
        report.blocking_reasons = self._extract_blocking_reasons(text)
        return report

    def _passes_release_gates(self, report: EvaluationReport) -> tuple[bool, list[str]]:
        blockers = list(report.blockers)
        blockers.extend(self._audit_contract_alignment(report))
        blockers.extend(self._audit_evaluator_execution(report))

        if report.average_score < config.RELEASE_READY_SCORE:
            blockers.append(
                f"average score {report.average_score:.1f} is below release threshold {config.RELEASE_READY_SCORE:.1f}"
            )
        if report.verdict != "PASS":
            blockers.append(f"QA verdict is {report.verdict}, not PASS")
        if report.contract_coverage != "PASS":
            blockers.append(f"contract coverage is {report.contract_coverage}, not PASS")
        if report.build_verification != "PASS":
            blockers.append(f"build verification is {report.build_verification}, not PASS")
        if config.REQUIRE_BROWSER_VERIFICATION and report.browser_verification != "PASS":
            blockers.append(f"browser verification is {report.browser_verification}, not PASS")
        if config.REQUIRE_FULL_SPEC_COVERAGE and report.spec_coverage != "FULL":
            blockers.append(f"spec coverage is {report.spec_coverage}, not FULL")
        if config.BLOCK_PLACEHOLDER_UI and report.placeholder_ui != "NONE":
            blockers.append(f"placeholder UI status is {report.placeholder_ui}, not NONE")
        if report.critical_bugs > config.MAX_CRITICAL_BUGS:
            blockers.append(
                f"critical bugs {report.critical_bugs} exceed limit {config.MAX_CRITICAL_BUGS}"
            )
        if report.major_bugs > config.MAX_MAJOR_BUGS:
            blockers.append(f"major bugs {report.major_bugs} exceed limit {config.MAX_MAJOR_BUGS}")
        if report.criteria_total == 0 or report.criteria_passed < report.criteria_total:
            blockers.append(f"acceptance criteria passed {report.criteria_passed}/{report.criteria_total}")
        if report.untested_criteria > 0:
            blockers.append(f"{report.untested_criteria} acceptance criteria remain untested")
        if report.verdict == "PASS" and report.blocking_reasons:
            blockers.append("QA marked PASS while still listing blocking reasons")

        for label, score in {
            "Feature Completeness": report.feature_completeness,
            "Functional Correctness": report.functional_correctness or report.functionality_score,
            "Product Depth": report.product_depth,
            "UX / Visual Quality": report.ux_quality,
            "Code Quality": report.code_quality,
            "Operability": report.operability,
        }.items():
            if score and score < config.CORE_SCORE_FLOOR:
                blockers.append(f"{label} score {score:.1f} is below core floor {config.CORE_SCORE_FLOOR:.1f}")

        correctness = report.functional_correctness or report.functionality_score
        if correctness and correctness < config.STRICT_CORRECTNESS_FLOOR:
            blockers.append(
                f"functional correctness score {correctness:.1f} is below strict floor "
                f"{config.STRICT_CORRECTNESS_FLOOR:.1f}"
            )
        if report.operability and report.operability < config.STRICT_OPERABILITY_FLOOR:
            blockers.append(
                f"operability score {report.operability:.1f} is below strict floor "
                f"{config.STRICT_OPERABILITY_FLOOR:.1f}"
            )

        placeholder_matches = self._scan_placeholder_markers()
        if config.BLOCK_PLACEHOLDER_UI and placeholder_matches:
            blockers.append("placeholder markers found in app source: " + "; ".join(placeholder_matches[:5]))

        return not blockers, blockers

    def _audit_contract_alignment(self, report: EvaluationReport) -> list[str]:
        blockers: list[str] = []
        if report.contract_round and report.feedback_round < report.contract_round:
            blockers.append(
                f"feedback round {report.feedback_round} is stale relative to contract round {report.contract_round}"
            )
        if (
            report.contract_criteria_total > 0
            and report.criteria_total > 0
            and report.criteria_total != report.contract_criteria_total
        ):
            blockers.append(
                "QA acceptance criteria total "
                f"{report.criteria_total} does not match contract criteria total {report.contract_criteria_total}"
            )
        missing_deliverables = self._find_missing_contract_deliverables()
        if missing_deliverables:
            blockers.append("contract deliverables missing from workspace: " + "; ".join(missing_deliverables[:5]))
        return blockers

    def _audit_evaluator_execution(self, report: EvaluationReport) -> list[str]:
        blockers: list[str] = []
        tool_uses = getattr(self.evaluator, "last_tool_uses", [])
        browser_calls: list[dict] = []
        malformed_tool_use_seen = False

        for tool in tool_uses if isinstance(tool_uses, list) else []:
            if not isinstance(tool, dict):
                malformed_tool_use_seen = True
                continue
            if tool.get("name") == "browser_test":
                browser_calls.append(tool)

        if malformed_tool_use_seen:
            blockers.append("evaluator recorded malformed tool use entries")
        if config.REQUIRE_BROWSER_VERIFICATION and not browser_calls:
            blockers.append("evaluator did not call browser_test")
            return blockers
        if not browser_calls:
            return blockers

        successful_calls = 0
        interactive_calls = 0
        total_actions = 0
        user_actions = 0
        screenshot_seen = False

        for call in browser_calls:
            args = call.get("arguments", {})
            if not isinstance(args, dict):
                blockers.append("browser_test arguments were malformed")
                args = {}
            result = call.get("result", "")
            if not isinstance(result, str):
                result = str(result)
            raw_actions = args.get("actions") or []
            if raw_actions and not isinstance(raw_actions, list):
                blockers.append("browser_test actions were malformed")
                raw_actions = []

            valid_actions: list[dict] = []
            malformed_action_seen = False
            for action in raw_actions:
                if not isinstance(action, dict):
                    malformed_action_seen = True
                    continue
                valid_actions.append(action)
            if malformed_action_seen:
                blockers.append("browser_test included malformed action entries")

            action_count = len(valid_actions)
            total_actions += action_count
            if action_count > 0:
                interactive_calls += 1
            for action in valid_actions:
                if action.get("type") in {"click", "fill", "scroll"}:
                    user_actions += 1
            if "Navigated to" in result and "[error]" not in result:
                successful_calls += 1
            if "Screenshot saved to _screenshot.png" in result:
                screenshot_seen = True
            if "[error]" in result:
                blockers.append("browser_test reported an error")
            if "Console errors (" in result:
                blockers.append("browser_test detected console errors")
            if "Page errors (" in result:
                blockers.append("browser_test detected uncaught page errors")
            if "Request failures (" in result:
                blockers.append("browser_test detected failed network requests")

        if successful_calls == 0:
            blockers.append("no successful browser_test navigation was recorded")
        if interactive_calls == 0:
            blockers.append("browser_test never performed interactive actions")
        if user_actions == 0:
            blockers.append("browser_test never performed any click/fill/scroll user interactions")
        required_actions = 1 if report.contract_criteria_total <= 1 else 3
        if total_actions < required_actions:
            blockers.append(
                f"browser_test only performed {total_actions} actions; expected at least {required_actions} for meaningful QA"
            )
        if not screenshot_seen:
            blockers.append("browser_test did not produce a screenshot")
        if not (Path(config.WORKSPACE) / "_screenshot.png").exists():
            blockers.append("_screenshot.png was not created")
        return blockers

    @staticmethod
    def _read_contract_text() -> str:
        contract_path = Path(config.WORKSPACE) / config.CONTRACT_FILE
        return contract_path.read_text(encoding="utf-8") if contract_path.exists() else ""

    @staticmethod
    def _extract_round_number(text: str, heading: str) -> int:
        if not text:
            return 0
        match = re.search(rf"(?im)^\s*#{{1,6}}\s+{re.escape(heading)}\s+[—-]\s+Round\s+(\d+)", text)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _extract_markdown_section(text: str, heading: str) -> str:
        if not text:
            return ""
        match = re.search(rf"(?ims)^\s*##\s+{re.escape(heading)}\s*(.*?)(?=^\s*##\s+|\Z)", text)
        return match.group(1) if match else ""

    def _count_contract_acceptance_criteria(self, contract_text: str) -> int:
        section = self._extract_markdown_section(contract_text, "Acceptance Criteria")
        return len(re.findall(r"(?m)^\s*\d+\.\s+", section))

    def _find_missing_contract_deliverables(self) -> list[str]:
        section = self._extract_markdown_section(self._read_contract_text(), "Deliverables")
        deliverables = {match for match in re.findall(r"`([^`\n]+\.[A-Za-z0-9]+)`", section)}
        missing: list[str] = []
        for rel_path in sorted(deliverables):
            if not (Path(config.WORKSPACE) / rel_path).exists():
                missing.append(rel_path)
        return missing

    def _scan_placeholder_markers(self) -> list[str]:
        patterns = [
            re.compile(r"(?i)\bcoming soon\b"),
            re.compile(r"(?i)\bcoming in round \d+\b"),
            re.compile(r"(?i)\bphase \d+\b"),
            re.compile(r"(?i)\bnot yet available\b"),
            re.compile(r"(?i)\bnot implemented\b"),
            re.compile(r"(?i)\bstub\b"),
            re.compile(r"(?i)\bplaceholder\b"),
        ]
        include_exts = {".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".py", ".md"}
        skip_dirs = {".git", "node_modules", "dist", "__pycache__"}
        matches: list[str] = []
        workspace = Path(config.WORKSPACE)
        for path in workspace.rglob("*"):
            if not path.is_file() or path.suffix not in include_exts:
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            rel = path.relative_to(workspace)
            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith(("//", "/*", "*", "*/")):
                    continue
                for pattern in patterns:
                    if pattern.search(line):
                        matches.append(f"{rel}:{lineno}: {stripped[:120]}")
                        break
                if len(matches) >= 10:
                    return matches
        return matches

    @staticmethod
    def _extract_score(text: str) -> float:
        match = re.search(r"[Aa]verage[:\s]*(\d+\.?\d*)\s*/\s*10", text)
        if match:
            return float(match.group(1))
        scores = re.findall(r"(\d+\.?\d*)\s*/\s*10", text)
        if scores:
            vals = [float(score) for score in scores]
            return sum(vals) / len(vals)
        return 0.0

    @staticmethod
    def _extract_named_score(text: str, label: str) -> float:
        match = re.search(
            rf"(?im)^\s*-\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*(\d+\.?\d*)\s*/\s*10",
            text,
        )
        return float(match.group(1)) if match else 0.0

    @staticmethod
    def _extract_line_value(text: str, label: str) -> str:
        match = re.search(
            rf"(?im)^\s*-\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*([^\n]+)",
            text,
        )
        return match.group(1).replace("**", "").strip() if match else ""

    @classmethod
    def _extract_int_value(cls, text: str, label: str, default: int = 0) -> int:
        value = cls._extract_line_value(text, label)
        match = re.search(r"-?\d+", value)
        return int(match.group(0)) if match else default

    @staticmethod
    def _extract_blocking_reasons(text: str) -> list[str]:
        match = re.search(r"(?is)^###\s+Blocking Reasons\s*(.*?)(?:^###\s+|\Z)", text, re.MULTILINE)
        if not match:
            return []
        reasons: list[str] = []
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if re.match(r"^\d+\.\s+", stripped):
                reason = re.sub(r"^\d+\.\s+", "", stripped).strip()
                if reason and reason != "...":
                    reasons.append(reason)
        return reasons


def _run_harness_mode(args: argparse.Namespace) -> int:
    from logger import setup_logging
    setup_logging(verbose=args.verbose)

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        return 1

    user_prompt = " ".join(args.prompt).strip()
    if not args.resume_dir and not user_prompt:
        print("Usage: python harness.py \"<your product idea>\" [--verbose]")
        print("   or: python harness.py run --resume-dir <project_dir> [--skip-planner] [resume instructions]")
        print("   or: python harness.py dashboard")
        return 1

    if args.resume_dir and not user_prompt:
        user_prompt = "Continue improving the existing project to release-ready completion."

    log.info(f"Prompt: {user_prompt}")
    log.info(f"Model: {config.MODEL}")
    log.info(f"Base URL: {config.BASE_URL}")
    log.info(f"Workspace: {config.WORKSPACE}")
    if args.resume_dir:
        log.info(f"Resume directory: {os.path.abspath(args.resume_dir)}")
        log.info(f"Skip planner: {args.skip_planner}")

    log.info("Verifying API connection...")
    try:
        from agents import get_client
        resp = get_client().chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=5,
        )
        choice = extract_primary_choice(resp)
        log.info(f"API OK — model responded: {choice.message.content}")
    except Exception as exc:
        log.error(f"API preflight failed: {exc}")
        print(
            "\nCannot connect to API. Check your .env:\n"
            "  OPENAI_API_KEY  — is it valid?\n"
            f"  OPENAI_BASE_URL — is {config.BASE_URL} correct?\n"
            f"  HARNESS_MODEL   — does {config.MODEL} exist on this provider?"
        )
        return 1

    harness = Harness()
    harness.run(
        user_prompt,
        resume_dir=args.resume_dir,
        skip_planning=args.skip_planner if args.skip_planner else None,
    )
    return 0


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the engineering harness on a new idea or resume an existing workspace.",
    )
    parser.add_argument("prompt", nargs="*", help="Product idea or resume instructions")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--resume-dir", help="Resume work in an existing project directory")
    parser.add_argument(
        "--skip-planner",
        action="store_true",
        help="Skip planner and use existing spec.md (or raw prompt if spec.md is missing)",
    )
    return parser


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "dashboard":
        from dashboard_server import serve_dashboard

        serve_dashboard()
        return

    if argv and argv[0] == "run":
        argv = argv[1:]

    parser = _build_run_parser()
    args = parser.parse_args(argv)
    sys.exit(_run_harness_mode(args))


if __name__ == "__main__":
    main()
