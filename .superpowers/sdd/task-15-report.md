# Task 15 Evidence Report

## Scope

Implemented fenced design proposal execution in the `phase-2a-controlled-change`
worktree. The executor keeps source projects unchanged, materializes the exact
managed revision, applies the CST adapter, requires KiCad 9 ERC, creates a
deterministic candidate commit/ref, and publishes proposal database state only
through active lease-fenced transactions.

## Test-Driven Evidence

The new proposal and artifact tests were first run before implementation and
failed during collection because `ArtifactConflictError` and proposal execution
interfaces did not exist. After implementation:

```text
PYTHONPATH=src python -m pytest tests/unit/test_artifacts.py tests/integration/test_proposals.py tests/integration/test_tasks.py -q
19 passed
```

Task-required focused regression suite:

```text
PYTHONPATH=src python -m pytest tests/unit/test_artifacts.py tests/integration/test_proposals.py tests/integration/test_tasks.py tests/integration/test_validation.py tests/unit/test_schematic_adapter.py tests/unit/test_schematic_modules.py -q
58 passed
```

Full repository suite:

```text
PYTHONPATH=src python -m pytest -q
201 passed, 1 skipped
```

The one skipped test is the existing KiCad CLI contract test because local
`kicad-cli` is not installed. This is an environment limitation; proposal
candidate validation still requires a probed KiCad 9 capability and exactly one
ERC report.

`git diff --check` completed without whitespace errors.

## Behavioral Checks

- Passing proposal produces one `ready_for_review` candidate and all nine
  required evidence kinds plus the immutable evidence-set artifact.
- ERC findings produce `validation_failed`, preserve evidence, and do not create
  a candidate revision or advance the project revision.
- Empty semantic diff produces `DESIGN_COMMAND_NO_EFFECT` and no candidate/ref.
- Expired task lease cannot transition a queued proposal to executing.
- CAS writes stream-verify an existing object and reject corrupted bytes with
  `ArtifactConflictError` without overwriting it.
- Unhandled candidate-stage failures are normalized to fenced terminal
  `CANDIDATE_VALIDATION_FAILED` state; stale lease errors remain retryable.

