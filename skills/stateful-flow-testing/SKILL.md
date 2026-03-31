---
name: stateful-flow-testing
description: Enforce lifecycle-complete QA for modals, panels, save/load, publish, retry, and other stateful workflows. Use when writing contracts, evaluating rounds, or tightening browser test plans that currently stop at "it opens".
---

Use this skill when the task touches stateful UI or workflows:
- modals, dialogs, drawers, panels, overlays
- onboarding/tutorial/wizard flows
- save/load/restore/import/export/publish flows
- retry/resume/reopen/close behavior

Minimum contract coverage:
1. Entry path: the feature can be opened or started.
2. Completion or dismissal path: the user can finish, close, cancel, or confirm.
3. Final state: the system returns to a usable state after the action.
4. Repeat path: reopen, retry, resume, or run the same flow again.
5. Negative path: at least one "should not happen" check such as hidden/absent/not stuck/not crashing.

Browser QA reminders:
- Prefer explicit assertions over visual guesses.
- Use `browser_test` assertion actions such as `assert_visible`, `assert_hidden`, `assert_text`, `assert_not_text`, `assert_url_contains`, and `press`.
- For modal-like flows, do not stop after the first open. Verify close or completion, then reopen or retry.
- Capture at least one negative end-state check, such as "dialog is hidden" or "error text is absent".

Red flags:
- The contract only says "opens" or "renders".
- Test methods say "browser smoke test" without concrete checks.
- The browser plan has clicks but no assertions.
- A modal action changes app state but nothing verifies that the modal actually disappears.

If you are updating prompts or release gates, push these requirements into both:
- contract quality rules
- evaluator browser evidence rules
