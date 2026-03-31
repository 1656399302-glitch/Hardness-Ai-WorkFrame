import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import agents
import context
import tools
from artifacts import ensure_workspace_layout, read_resume_state, write_resume_state
from harness import EvaluationReport, Harness
from skills import SkillRegistry


class HarnessGuardsTest(unittest.TestCase):
    def setUp(self):
        self.original_workspace = config.WORKSPACE
        self.temp_dir = tempfile.TemporaryDirectory()
        config.WORKSPACE = self.temp_dir.name

    def tearDown(self):
        config.WORKSPACE = self.original_workspace
        self.temp_dir.cleanup()

    def _passing_report(self, *, round_num: int = 1, criteria_total: int = 1) -> EvaluationReport:
        return EvaluationReport(
            average_score=9.4,
            functionality_score=9.4,
            verdict="PASS",
            feedback_round=round_num,
            contract_round=round_num,
            contract_criteria_total=criteria_total,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=criteria_total,
            criteria_total=criteria_total,
            untested_criteria=0,
            feature_completeness=9.3,
            functional_correctness=9.4,
            product_depth=9.2,
            ux_quality=9.1,
            code_quality=9.3,
            operability=9.4,
        )

    def test_compaction_keeps_tool_call_and_result_together(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]

        compacted = context.compact_messages(messages, lambda _messages: "summary", role="builder")
        recent = compacted[2:]

        for idx, msg in enumerate(recent):
            if msg.get("role") != "tool":
                continue
            tool_id = msg.get("tool_call_id")
            matched = any(
                earlier.get("role") == "assistant"
                and any(tc.get("id") == tool_id for tc in earlier.get("tool_calls", []))
                for earlier in recent[:idx]
            )
            self.assertTrue(matched, f"tool result {tool_id} lost its matching tool call")

    def test_release_gates_fail_when_placeholder_ui_exists(self):
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text(
            'export function App() { return <div>AI features coming in Round 2</div>; }\n',
            encoding="utf-8",
        )

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=10,
            criteria_total=10,
            untested_criteria=0,
        )

        passed, blockers = Harness()._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("placeholder markers found" in blocker for blocker in blockers))

    def test_release_gates_fail_when_evaluator_skips_browser_test(self):
        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=3,
            contract_round=3,
            contract_criteria_total=10,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=10,
            criteria_total=10,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = []
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("did not call browser_test" in blocker for blocker in blockers))

    def test_release_gates_fail_when_browser_test_reports_errors(self):
        screenshot = Path(config.WORKSPACE) / "_screenshot.png"
        screenshot.write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=3,
            contract_round=3,
            contract_criteria_total=10,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=10,
            criteria_total=10,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {"url": "http://localhost:5173", "actions": [{"type": "click", "selector": "#btn"}]},
                "result": "Navigated to http://localhost:5173 — title: App\nConsole errors (1):\n  - boom\nScreenshot saved to _screenshot.png",
            }
        ]
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("console errors" in blocker for blocker in blockers))

    def test_release_gates_fail_when_feedback_round_is_stale(self):
        contract = """# Sprint Contract — Round 5

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. App renders
2. Export works
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=4,
            contract_round=5,
            contract_criteria_total=2,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=2,
            criteria_total=2,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        {"type": "click", "selector": "#a"},
                        {"type": "fill", "selector": "#b", "value": "ok"},
                        {"type": "scroll", "value": 400},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("stale relative to contract round" in blocker for blocker in blockers))

    def test_release_gates_fail_when_criteria_total_mismatches_contract(self):
        contract = """# Sprint Contract — Round 2

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. One
2. Two
3. Three
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=2,
            contract_round=2,
            contract_criteria_total=3,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=2,
            criteria_total=2,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        {"type": "click", "selector": "#a"},
                        {"type": "fill", "selector": "#b", "value": "ok"},
                        {"type": "scroll", "value": 400},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("does not match contract criteria total" in blocker for blocker in blockers))

    def test_release_gates_fail_when_contract_deliverable_file_is_missing(self):
        contract = """# Sprint Contract — Round 6

## Deliverables
1. `src/components/TextEffects.tsx`
2. `src/App.tsx`

## Acceptance Criteria
1. One
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=6,
            contract_round=6,
            contract_criteria_total=1,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=1,
            criteria_total=1,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        {"type": "click", "selector": "#a"},
                        {"type": "fill", "selector": "#b", "value": "ok"},
                        {"type": "scroll", "value": 400},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("contract deliverables missing from workspace" in blocker for blocker in blockers))

    def test_release_gates_fail_when_browser_test_is_too_shallow(self):
        contract = """# Sprint Contract — Round 7

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. One
2. Two
3. Three
4. Four
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=7,
            contract_round=7,
            contract_criteria_total=4,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=4,
            criteria_total=4,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [{"type": "wait", "delay": 1000}],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]
        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("only performed 1 actions" in blocker for blocker in blockers))

    def test_release_gates_fail_gracefully_when_browser_actions_are_malformed(self):
        contract = """# Sprint Contract — Round 8

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. One
2. Two
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        report = EvaluationReport(
            average_score=9.0,
            functionality_score=9.0,
            verdict="PASS",
            feedback_round=8,
            contract_round=8,
            contract_criteria_total=2,
            spec_coverage="FULL",
            contract_coverage="PASS",
            build_verification="PASS",
            browser_verification="PASS",
            placeholder_ui="NONE",
            critical_bugs=0,
            major_bugs=0,
            minor_bugs=0,
            criteria_passed=2,
            criteria_total=2,
            untested_criteria=0,
        )

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        "click the undo button",
                        {"type": "wait", "delay": 1000},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]

        passed, blockers = harness._passes_release_gates(report)
        self.assertFalse(passed)
        self.assertTrue(any("malformed action entries" in blocker for blocker in blockers))

    def test_structured_feedback_is_parsed(self):
        feedback = """## QA Evaluation — Round 3

### Release Decision
- Verdict: PASS
- Summary: release ready
- Spec Coverage: FULL
- Contract Coverage: PASS
- Build Verification: PASS
- Browser Verification: PASS
- Placeholder UI: NONE
- Critical Bugs: 0
- Major Bugs: 0
- Minor Bugs: 1
- Acceptance Criteria Passed: 12/12
- Untested Criteria: 0

### Scores
- Design Quality: 8.5/10 — ok
- Originality: 8.0/10 — ok
- Craft: 8.5/10 — ok
- Functionality: 9.0/10 — ok
- **Average: 8.5/10**
"""
        Path(config.WORKSPACE, config.FEEDBACK_FILE).write_text(feedback, encoding="utf-8")

        report = Harness()._extract_evaluation_report()
        self.assertEqual(report.verdict, "PASS")
        self.assertEqual(report.spec_coverage, "FULL")
        self.assertEqual(report.browser_verification, "PASS")
        self.assertEqual(report.criteria_passed, 12)
        self.assertEqual(report.criteria_total, 12)
        self.assertEqual(report.untested_criteria, 0)
        self.assertAlmostEqual(report.average_score, 8.5)
        self.assertAlmostEqual(report.functionality_score, 9.0)

    def test_six_dimension_feedback_is_parsed(self):
        feedback = """## QA Evaluation — Round 4

### Release Decision
- Verdict: FAIL
- Summary: not release-ready
- Spec Coverage: PARTIAL
- Contract Coverage: FAIL
- Build Verification: PASS
- Browser Verification: FAIL
- Placeholder UI: NONE
- Critical Bugs: 1
- Major Bugs: 1
- Minor Bugs: 0
- Acceptance Criteria Passed: 3/5
- Untested Criteria: 1

### Scores
- Feature Completeness: 8.7/10 — close
- Functional Correctness: 8.9/10 — still broken
- Product Depth: 9.1/10 — solid
- UX / Visual Quality: 8.8/10 — good
- Code Quality: 8.6/10 — acceptable
- Operability: 9.2/10 — runnable
- **Average: 8.9/10**
"""
        Path(config.WORKSPACE, config.FEEDBACK_FILE).write_text(feedback, encoding="utf-8")
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(
            "# Sprint Contract — Round 4\n\n## Acceptance Criteria\n1. One\n2. Two\n3. Three\n4. Four\n5. Five\n",
            encoding="utf-8",
        )

        report = Harness()._extract_evaluation_report()
        self.assertAlmostEqual(report.feature_completeness, 8.7)
        self.assertAlmostEqual(report.functional_correctness, 8.9)
        self.assertAlmostEqual(report.product_depth, 9.1)
        self.assertAlmostEqual(report.ux_quality, 8.8)
        self.assertAlmostEqual(report.code_quality, 8.6)
        self.assertAlmostEqual(report.operability, 9.2)

    def test_workspace_layout_creates_structured_artifacts(self):
        paths = ensure_workspace_layout(config.WORKSPACE)
        self.assertTrue(paths.product_spec_dir.exists())
        self.assertTrue(paths.sprint_contracts_dir.exists())
        self.assertTrue(paths.qa_reports_dir.exists())
        self.assertTrue(paths.handoffs_dir.exists())
        self.assertTrue(paths.runbooks_dir.exists())
        self.assertTrue((paths.runbooks_dir / "setup.md").exists())
        self.assertTrue((paths.runbooks_dir / "test.md").exists())
        self.assertTrue((paths.runbooks_dir / "release.md").exists())

    def test_structured_feedback_extracts_blocking_reasons(self):
        feedback = """## QA Evaluation — Round 4

### Release Decision
- Verdict: FAIL
- Summary: not ready
- Spec Coverage: PARTIAL
- Contract Coverage: FAIL
- Build Verification: PASS
- Browser Verification: FAIL
- Placeholder UI: FOUND
- Critical Bugs: 1
- Major Bugs: 2
- Minor Bugs: 0
- Acceptance Criteria Passed: 7/10
- Untested Criteria: 2

### Blocking Reasons
1. Browser test failed during upload flow
2. Placeholder UI still visible in right panel

### Scores
- Functionality: 5.0/10 — broken
- **Average: 6.0/10**
"""
        Path(config.WORKSPACE, config.FEEDBACK_FILE).write_text(feedback, encoding="utf-8")
        report = Harness()._extract_evaluation_report()
        self.assertEqual(
            report.blocking_reasons,
            [
                "Browser test failed during upload flow",
                "Placeholder UI still visible in right panel",
            ],
        )

    def test_resume_mode_reuses_existing_workspace_and_skips_planner(self):
        project_dir = Path(config.WORKSPACE) / "existing-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / config.SPEC_FILE).write_text("existing spec", encoding="utf-8")
        (project_dir / config.FEEDBACK_FILE).write_text(
            "## QA Evaluation — Round 2\n\n### Scores\n- Functionality: 7.0/10\n- **Average: 7.2/10**\n",
            encoding="utf-8",
        )

        harness = Harness()
        planner_calls = []
        round_numbers = []

        harness.planner.run = lambda task: planner_calls.append(task)
        harness._negotiate_contract = lambda round_num: round_numbers.append(round_num)

        def fail_build(_task):
            harness.builder.last_run_success = False
            harness.builder.last_stop_reason = "api_error"
            return ""

        harness.builder.run = fail_build
        harness.evaluator.run = lambda _task: self.fail("evaluator should not run")

        harness.run(
            "Continue from current workspace",
            resume_dir=str(project_dir),
            skip_planning=True,
        )

        self.assertEqual(planner_calls, [])
        self.assertEqual(round_numbers, [3])
        self.assertEqual(Path(config.WORKSPACE).resolve(), project_dir.resolve())

    def test_resume_mode_prefers_workspace_checkpoint_for_build_phase(self):
        project_dir = Path(config.WORKSPACE) / "checkpoint-build-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / config.SPEC_FILE).write_text("existing spec", encoding="utf-8")
        (project_dir / config.FEEDBACK_FILE).write_text(
            "## QA Evaluation — Round 2\n\n### Scores\n- Functionality: 7.0/10\n- **Average: 7.2/10**\n",
            encoding="utf-8",
        )
        write_resume_state(project_dir, status="interrupted", next_phase="build", next_round=5, message="killed")

        harness = Harness()
        build_tasks = []

        harness.planner.run = lambda _task: self.fail("planner should not run in skip-planner mode")
        harness._negotiate_contract = lambda _round_num: self.fail("contract should not rerun from build checkpoint")

        def fail_build(task):
            build_tasks.append(task)
            harness.builder.last_run_success = False
            harness.builder.last_stop_reason = "api_error"
            return ""

        harness.builder.run = fail_build
        harness.evaluator.run = lambda _task: self.fail("evaluator should not run when build fails")

        harness.run(
            "Continue from current workspace",
            resume_dir=str(project_dir),
            skip_planning=True,
        )

        self.assertEqual(len(build_tasks), 1)
        self.assertIn("Builder round 5", build_tasks[0])
        checkpoint = read_resume_state(project_dir)
        self.assertEqual(checkpoint["next_phase"], "build")
        self.assertEqual(checkpoint["next_round"], 5)

    def test_resume_mode_prefers_workspace_checkpoint_for_evaluate_phase(self):
        project_dir = Path(config.WORKSPACE) / "checkpoint-eval-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / config.SPEC_FILE).write_text("existing spec", encoding="utf-8")
        write_resume_state(project_dir, status="interrupted", next_phase="evaluate", next_round=4, message="killed")

        harness = Harness()
        evaluator_calls = []

        harness.planner.run = lambda _task: self.fail("planner should not run in skip-planner mode")
        harness._negotiate_contract = lambda _round_num: self.fail("contract should not rerun from evaluate checkpoint")
        harness.builder.run = lambda _task: self.fail("builder should not rerun from evaluate checkpoint")

        def fail_eval(task):
            evaluator_calls.append(task)
            harness.evaluator.last_run_success = False
            harness.evaluator.last_stop_reason = "api_error"
            return ""

        harness.evaluator.run = fail_eval

        harness.run(
            "Continue from current workspace",
            resume_dir=str(project_dir),
            skip_planning=True,
        )

        self.assertEqual(len(evaluator_calls), 1)
        self.assertIn("QA round 4", evaluator_calls[0])
        checkpoint = read_resume_state(project_dir)
        self.assertEqual(checkpoint["next_phase"], "evaluate")
        self.assertEqual(checkpoint["next_round"], 4)

    def test_build_task_prioritizes_feedback_remediation_when_feedback_exists(self):
        project_dir = Path(config.WORKSPACE) / "remediation-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / config.SPEC_FILE).write_text("existing spec", encoding="utf-8")
        (project_dir / config.FEEDBACK_FILE).write_text(
            "## QA Evaluation — Round 2\n\n### Blocking Reasons\n1. Fix bug\n",
            encoding="utf-8",
        )

        harness = Harness()
        captured_tasks = []
        round_numbers = []

        harness.planner.run = lambda task: self.fail("planner should not run in skip-planner mode")
        harness._negotiate_contract = lambda round_num: round_numbers.append(round_num)

        def fail_build(task):
            captured_tasks.append(task)
            harness.builder.last_run_success = False
            harness.builder.last_stop_reason = "api_error"
            return ""

        harness.builder.run = fail_build
        harness.evaluator.run = lambda _task: self.fail("evaluator should not run")

        harness.run(
            "Continue from current workspace",
            resume_dir=str(project_dir),
            skip_planning=True,
        )

        self.assertEqual(round_numbers, [3])
        self.assertEqual(len(captured_tasks), 1)
        task = captured_tasks[0]
        self.assertIn("This round is a remediation round", task)
        self.assertIn("Do not spend this round on speculative enhancements", task)

    def test_extract_primary_choice_rejects_missing_choices(self):
        with self.assertRaises(ValueError):
            agents.extract_primary_choice(SimpleNamespace(choices=None))

    def test_agent_run_retries_invalid_response_instead_of_crashing(self):
        original_get_client = agents.get_client
        original_max_tool_errors = config.MAX_TOOL_ERRORS

        invalid_response = SimpleNamespace(choices=None)
        valid_message = SimpleNamespace(content="done", tool_calls=None)
        valid_choice = SimpleNamespace(message=valid_message, finish_reason="stop")
        valid_response = SimpleNamespace(choices=[valid_choice])

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                return invalid_response if self.calls == 1 else valid_response

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        try:
            agents.get_client = lambda: fake_client
            config.MAX_TOOL_ERRORS = 2
            agent = agents.Agent("tester", "system", use_tools=False)
            result = agent.run("task")
        finally:
            agents.get_client = original_get_client
            config.MAX_TOOL_ERRORS = original_max_tool_errors

        self.assertEqual(result, "done")
        self.assertTrue(agent.last_run_success)
        self.assertEqual(agent.last_stop_reason, "completed")

    def test_agent_retries_same_iteration_after_timeout(self):
        attempts = {"count": 0}

        class FakeCompletions:
            def create(self, **_kwargs):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise RuntimeError("Request timed out.")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=[]),
                            finish_reason="stop",
                        )
                    ]
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        agent = agents.Agent("builder", "system", use_tools=False)

        with patch("agents.get_client", return_value=fake_client), patch(
            "agents._wait_for_api_recovery",
            return_value=True,
        ) as recovery_mock:
            result = agent.run("ship it")

        self.assertEqual(result, "done")
        self.assertTrue(agent.last_run_success)
        self.assertEqual(agent.last_stop_reason, "completed")
        self.assertEqual(agent.last_iterations, 1)
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(recovery_mock.call_count, 2)

    def test_agent_aborts_cleanly_when_api_recovery_timeout_expires(self):
        class FakeCompletions:
            def create(self, **_kwargs):
                raise RuntimeError("Request timed out.")

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        agent = agents.Agent("builder", "system", use_tools=False)

        with patch("agents.get_client", return_value=fake_client), patch(
            "agents._wait_for_api_recovery",
            return_value=False,
        ):
            result = agent.run("ship it")

        self.assertEqual(result, "")
        self.assertFalse(agent.last_run_success)
        self.assertEqual(agent.last_stop_reason, "api_recovery_timeout")
        self.assertEqual(agent.last_iterations, 1)

    def test_invalid_integer_env_reports_field_name(self):
        with patch.dict("os.environ", {"MAX_AGENT_ITERATIONS": "100s"}, clear=False):
            with self.assertRaisesRegex(ValueError, "MAX_AGENT_ITERATIONS"):
                config._get_int_env("MAX_AGENT_ITERATIONS")

    def test_contract_alignment_flags_too_few_test_methods(self):
        contract = """# Sprint Contract — Round 9

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. Dashboard renders
2. Export works
3. Help dialog opens

## Test Methods
1. Run the app once and smoke test the main page
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")

        blockers = Harness()._audit_contract_alignment(self._passing_report(round_num=9, criteria_total=3))
        self.assertTrue(any("only define 1 checks for 3 acceptance criteria" in blocker for blocker in blockers))

    def test_contract_alignment_flags_stateful_contract_without_lifecycle_coverage(self):
        contract = """# Sprint Contract — Round 10

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. Settings modal opens from the toolbar
2. Saving settings updates the selected preference

## Test Methods
1. Click the toolbar settings button and confirm the modal opens
2. Save settings from the modal

## Done Definition
1. Feature works in the browser
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        src_dir = Path(config.WORKSPACE) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")

        blockers = Harness()._audit_contract_alignment(self._passing_report(round_num=10, criteria_total=2))
        self.assertTrue(any("final-state verification" in blocker for blocker in blockers))
        self.assertTrue(any("negative/non-occurrence verification" in blocker for blocker in blockers))
        self.assertTrue(any("reopen/retry/repeat verification" in blocker for blocker in blockers))

    def test_evaluator_execution_requires_assertions_for_stateful_flows(self):
        contract = """# Sprint Contract — Round 11

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. Settings modal opens from the toolbar
2. Pressing Escape closes the modal and returns the page to a usable state
3. The modal can be reopened after closing
4. The modal should not remain visible after close

## Test Methods
1. Click the toolbar button and assert the modal is visible
2. Press Escape, assert the modal is hidden, and confirm the page remains usable
3. Reopen the modal and assert it renders again
4. Assert the modal text is absent after closing
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        {"type": "click", "selector": "[aria-label='open settings']"},
                        {"type": "press", "value": "Escape"},
                        {"type": "click", "selector": "[aria-label='open settings']"},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]

        blockers = harness._audit_evaluator_execution(self._passing_report(round_num=11, criteria_total=4))
        self.assertTrue(any("lacked explicit assertion actions" in blocker for blocker in blockers))
        self.assertTrue(any("lacked negative assertions" in blocker for blocker in blockers))

    def test_evaluator_execution_accepts_stateful_flow_with_repeat_and_negative_checks(self):
        contract = """# Sprint Contract — Round 12

## Deliverables
1. `src/App.tsx`

## Acceptance Criteria
1. Settings modal opens from the toolbar
2. Pressing Escape closes the modal and returns the page to a usable state
3. The modal can be reopened after closing
4. The modal should not remain visible after close

## Test Methods
1. Click the toolbar button and assert the modal is visible
2. Press Escape, assert the modal is hidden, and confirm the page remains usable
3. Reopen the modal and assert it renders again
4. Assert the modal text is absent after closing
"""
        Path(config.WORKSPACE, config.CONTRACT_FILE).write_text(contract, encoding="utf-8")
        (Path(config.WORKSPACE) / "_screenshot.png").write_bytes(b"fake image")

        harness = Harness()
        harness.evaluator.last_tool_uses = [
            {
                "name": "browser_test",
                "arguments": {
                    "url": "http://localhost:5173",
                    "actions": [
                        {"type": "click", "selector": "[aria-label='open settings']"},
                        {"type": "assert_visible", "selector": "#settings-modal"},
                        {"type": "press", "value": "Escape"},
                        {"type": "assert_hidden", "selector": "#settings-modal"},
                        {"type": "click", "selector": "[aria-label='open settings']"},
                        {"type": "assert_visible", "selector": "#settings-modal"},
                        {"type": "assert_not_text", "value": "Saving..."},
                    ],
                },
                "result": "Navigated to http://localhost:5173 — title: App\nScreenshot saved to _screenshot.png",
            }
        ]

        blockers = harness._audit_evaluator_execution(self._passing_report(round_num=12, criteria_total=4))
        self.assertEqual(blockers, [])

    def test_browser_test_supports_press_and_assertion_actions(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            def press(self, key_name, timeout=5000):
                self.page._handle_press(key_name)

            def inner_text(self):
                if self.selector == "#modal" and "#modal" in self.page.visible_selectors:
                    return "Modal Ready"
                return self.page.selector_text.get(self.selector, "")

            def wait_for(self, state="visible", timeout=5000):
                is_visible = self.selector in self.page.visible_selectors
                if state == "visible" and not is_visible:
                    raise AssertionError(f"{self.selector} was not visible")
                if state == "hidden" and is_visible:
                    raise AssertionError(f"{self.selector} was still visible")

        class FakeKeyboard:
            def __init__(self, page):
                self.page = page

            def press(self, key_name):
                self.page._handle_press(key_name)

        class FakePage:
            def __init__(self):
                self.url = "http://localhost:5173"
                self.visible_selectors = set()
                self.selector_text = {}
                self.keyboard = FakeKeyboard(self)

            def goto(self, url, timeout=15000):
                self.url = url

            def wait_for_load_state(self, state, timeout=5000):
                return None

            def title(self):
                return "Harness Demo"

            def on(self, _event, _handler):
                return None

            def click(self, selector, timeout=5000):
                if selector == "[aria-label='open modal']":
                    self.visible_selectors.add("#modal")
                if selector == "[aria-label='close modal']":
                    self.visible_selectors.discard("#modal")

            def fill(self, selector, value, timeout=5000):
                self.selector_text[selector] = value

            def wait_for_timeout(self, delay):
                return None

            def evaluate(self, script):
                if script.startswith("window.scrollBy"):
                    return None
                return True

            def locator(self, selector):
                return FakeLocator(self, selector)

            def inner_text(self, selector):
                if selector == "body":
                    return "Harness Demo Modal Ready" if "#modal" in self.visible_selectors else "Harness Demo"
                return self.selector_text.get(selector, "")

            def screenshot(self, path, full_page=True):
                Path(path).write_bytes(b"fake screenshot")

            def _handle_press(self, key_name):
                if key_name.lower() in {"escape", "esc"}:
                    self.visible_selectors.discard("#modal")

        class FakeBrowser:
            def __init__(self, page):
                self.page = page

            def new_page(self, viewport=None):
                return self.page

            def close(self):
                return None

        class FakePlaywright:
            def __init__(self, page):
                self.chromium = self
                self.page = page

            def launch(self, headless=True):
                return FakeBrowser(self.page)

        class FakePlaywrightContext:
            def __init__(self, page):
                self.page = page

            def __enter__(self):
                return FakePlaywright(self.page)

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_page = FakePage()

        with patch.object(tools, "HAS_PLAYWRIGHT", True), patch.object(
            tools,
            "sync_playwright",
            return_value=FakePlaywrightContext(fake_page),
        ):
            result = tools.browser_test(
                "http://localhost:5173",
                actions=[
                    {"type": "click", "selector": "[aria-label='open modal']"},
                    {"type": "assert_visible", "selector": "#modal"},
                    {"type": "assert_text", "value": "Modal Ready"},
                    {"type": "press", "value": "Escape"},
                    {"type": "assert_hidden", "selector": "#modal"},
                    {"type": "assert_not_text", "value": "Modal Ready"},
                ],
            )

        self.assertIn("Asserted visible: #modal", result)
        self.assertIn("Pressed key 'Escape'", result)
        self.assertIn("Asserted hidden: #modal", result)
        self.assertIn("Asserted text absent: 'Modal Ready'", result)
        self.assertNotIn("[error]", result)
        self.assertTrue((Path(config.WORKSPACE) / "_screenshot.png").exists())

    def test_skill_registry_discovers_stateful_flow_testing_skill(self):
        registry = SkillRegistry()
        self.assertTrue(any(skill["name"] == "stateful-flow-testing" for skill in registry.catalog))


if __name__ == "__main__":
    unittest.main()
