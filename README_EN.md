# Harness — High-Standard Engineering AI Workflow

[中文](README.md) | English

Harness is an artifact-driven workflow for long-running engineering tasks. Its core design goals are:

- contract first, then implementation
- independent evaluation with real interaction
- fail on any core dimension below threshold
- mandatory handoff artifacts for long tasks
- context strategy treated as an engineering concern

Reference:
- Anthropic: [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## Overview

### Structured artifacts

Each workspace now keeps both root-level convenience files and a structured artifact tree:

```text
<workspace>/
  spec.md
  contract.md
  feedback.md
  progress.md
  .ai-harness/
    product-spec/
    sprint-contracts/
    qa-reports/
    handoffs/
    decision-log/
    runbooks/
    runtime/
```

This gives you:
- auditability
- resumability
- per-round history
- durable handoff data after resets

### Stricter role boundaries

- Planner owns the product spec
- Contract proposer/reviewer gate the sprint before coding
- Builder implements but does not approve release
- Evaluator verifies with runtime evidence and browser interaction

### Six-dimension QA

The evaluator now scores:

1. Feature Completeness
2. Functional Correctness
3. Product Depth
4. UX / Visual Quality
5. Code Quality
6. Operability

Default release gates:

- average score `< 9.0` => FAIL
- functional correctness `< 9.0` => FAIL
- operability `< 9.0` => FAIL
- any other core dimension `< 8.5` => FAIL
- any untested acceptance criterion => FAIL
- missing browser evidence => FAIL
- placeholder / fake completion => FAIL

### Structured reset + handoff

Context reset now writes structured handoff JSON instead of vague prose.
Runtime state tracks:
- current phase
- current round
- active agent
- compaction count
- reset count

### Operator inbox for queued human instructions

If the harness is already running, avoid mutating the live agent context directly.
Instead, queue human instructions in:

```text
<workspace>/.ai-harness/runtime/operator-inbox.json
```

The harness reads this file only at phase boundaries (`contract`, `build`, `evaluate`).
That means:
- the current phase is not disturbed
- the next matching phase can receive the new instruction
- processed items are marked so they are not injected repeatedly

Supported scopes:
- `next_contract`
- `next_build`
- `next_evaluate`
- `next_round` (applies at the next contract phase)

Example:

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "operator-item-1",
      "scope": "next_build",
      "mode": "must_fix",
      "content": "Fix the latest modal close bug before adding new scope.",
      "status": "pending"
    }
  ]
}
```

### Local HTML dashboard

The dashboard supports:
- editing `.env`
- starting / stopping the harness
- viewing current phase / round / agent / PID
- tailing logs live
- monitoring compactions and resets

## Quick Start

```bash
pip install -r requirements.txt
python -m playwright install chromium

cp .env.template .env
# fill in your API settings
```

Run the harness:

```bash
python harness.py "Build a release-ready browser app with real QA evidence."
```

Resume an existing workspace:

```bash
python harness.py \
  --resume-dir workspace/20260328-114410_ai-ai-ui-ai \
  --skip-planner \
  "Remediation only. Close every blocker in feedback.md and progress.md."
```

Start the dashboard:

```bash
./start-dashboard.sh
```

You can still call it directly with Python:

```bash
python harness.py dashboard
```

Default URL:

```text
http://127.0.0.1:8765
```

## Important Files

- [harness.py](./harness.py): orchestrator, gates, CLI entry
- [prompts.py](./prompts.py): Planner / Builder / Evaluator / Contract prompts
- [artifacts.py](./artifacts.py): artifact layout, handoffs, decision log
- [context.py](./context.py): compaction / reset / structured checkpoint
- [runtime_state.py](./runtime_state.py): runtime telemetry and log slicing
- [dashboard_server.py](./dashboard_server.py): dashboard API server
- [dashboard.html](./dashboard.html): local control panel
- [config.py](./config.py): `.env` schema + runtime config

## Ground Rules

1. No Sprint Contract, no development.
2. Builder self-check is not acceptance.
3. Evaluator must interact with the running system.
4. Handoff artifacts are mandatory for long tasks.
5. If documentation claims “fixed” but the browser path fails, the round fails.

## Validation

At minimum, this repository should pass:

```bash
python3 -m py_compile harness.py config.py prompts.py agents.py context.py logger.py tools.py artifacts.py runtime_state.py dashboard_server.py
python3 -m unittest tests.test_harness_guards -v
```
