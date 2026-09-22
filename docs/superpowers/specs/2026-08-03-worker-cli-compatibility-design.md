# Worker CLI Compatibility Design

**Date:** 2026-08-03  
**Status:** Complete  
**Scope:** Phase 5A.1 regression repair

## Problem

`pcbflow worker` is registered both as a top-level command and as a Typer
command group. Typer resolves the group, so the documented execution options
`--once`, `--run`, and `--exit-when-idle` are rejected before `run_worker()`
can handle them. This breaks existing CLI workflows and three end-to-end
tests, while `worker health` remains available only through the group.

## Decision

Keep `worker` as one command group and move the execution entry point to that
group's callback. The callback runs only when no subcommand is selected:

```text
pcbflow worker --once --json
pcbflow worker --run
pcbflow worker --run --exit-when-idle --json
pcbflow worker health --json
```

The group keeps `no_args_is_help=True`; invoking `pcbflow worker` without an
explicit mode displays help instead of unexpectedly claiming work.

## Validation And Errors

- `--once` and `--run` remain mutually exclusive.
- Execution options cannot be combined with a management subcommand, such as
  `pcbflow worker --once health`; the CLI returns the existing stable
  `INVALID_ARGUMENT` error.
- `worker health` receives no execution side effects.

## Testing

Add a focused command-help contract proving that the same `worker` group
exposes legacy execution options and the `health` subcommand. Existing
end-to-end workflows continue to prove that `--once` actually handles queued
tasks.

## Documentation

Synchronize the README and development guide with the implemented resident
Worker. Remove stale statements that claim only one-shot Worker execution is
available or that a resident Worker remains outside scope; retain the actual
remaining limitations such as AI design, PCB automation, manufacturing output,
Web UI, and PostgreSQL.

## Completion Notes

Implemented on 2026-08-04. The worker group callback restores `--once`,
`--run`, and `--exit-when-idle` while preserving `worker health`. The final
audit also corrected resident execution to run the lease it already claimed,
wait for active tasks before disposal, publish an atomic health snapshot, and
adapt the default heartbeat for short task leases. Full verification completed
with 522 passing tests and 90.13% coverage.
