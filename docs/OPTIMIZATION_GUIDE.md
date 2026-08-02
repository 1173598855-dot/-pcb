# PCBFlow Optimization Guide

Updated: 2026-08-02

This guide records the optimization order for PCBFlow. The order is deliberate:
preserve evidence and boundary contracts first, then reduce work on measured hot
paths. An optimization must not weaken deterministic output, idempotency, or the
external-tool trust boundary.

## Operating Rules

1. Add a focused regression test before changing production behavior.
2. Preserve the existing API error envelope and idempotency semantics.
3. Bound every database and memory optimization by an explicit limit.
4. Measure before changing an I/O strategy whose behavior is security-sensitive.
5. Run focused tests first, then the complete suite and coverage gate.

## Execution Status

| Status | Priority | Area | Change | Acceptance condition |
| --- | --- | --- | --- | --- |
| Complete | P0 | KiCad execution identity | Proposal validation is bound to the capability observed during preflight. It reuses that version/profile and verifies the executable digest immediately before every spawned validation command. | A replacement after preflight is rejected before a new command is run. |
| Complete | P0 | Remote API configuration | The configured service actor now has the same 255-character storage/domain bound, configured tokens must be ASCII, and request actor limits align with that boundary. | Invalid startup configuration fails deterministically instead of producing a request-time 500. |
| Complete | P1 | Command batch persistence | Per-command conflict lookups inside `BEGIN IMMEDIATE` are replaced with bounded set queries and in-memory command-order conflict selection. | A normal multi-command batch uses one command-conflict query while preserving the exact conflicting key. |
| Complete | P1 | Content-addressed component imports | Canonical manifests and declared assets stream through store-owned staging files, are SHA-256 checked before publication, and retain the cumulative import limit. | Large assets use bounded reads; a late asset failure leaves no component revision or staging residue. |
| Deferred | P1 | Error-contract reuse | Keep schema and request errors on the established API envelope; only deduplicate construction when a contract test proves the exact response remains stable. | All callers retain their existing status, code, details, and correlation-id behavior. |

## KiCad Identity Binding

`ProposalExecutor` performs the initial capability probe because the capability
is recorded as evidence. A capability-aware KiCad port exposes
`validate_with_capability()` so `KicadCli` can accept that identity rather than
probing a second executable. Ports that only implement the original
two-argument `validate()` contract remain compatible but do not opt into this
identity-binding extension. The expected identity is accepted only when its path
belongs to the configured executable and includes an available version, digest,
and profile metadata consistent with its version.

The validator must hash the expected executable immediately before each ERC or
DRC subprocess. A digest mismatch raises `KicadUnavailableError` before the
subprocess is started. It must also retain the existing post-process digest check
so a replacement during execution is detected and invalidates the run.

This prevents the known preflight-to-validation replacement gap. It does not
claim to make arbitrary filesystem paths immutable across process creation; that
would require an operating-system-specific trusted executable handle or a
verified immutable installation snapshot and is outside this iteration.

## Remote Configuration Boundary

The remote service actor is injected with `model_copy(update=...)`, which does
not re-run Pydantic field validation. Therefore `Settings` is the authoritative
startup boundary for `api_actor_id`: it must be nonempty and no longer than 255
characters. A configured API token must be ASCII so constant-time string
comparison cannot raise on its representation. Request actor models use the
same actor-id length bound.

The API authentication check rejects non-ASCII presented bearer values before
constant-time comparison, so malformed credentials return the normal 401 error
envelope instead of a comparison exception.

## Bounded Command-Conflict Queries

Command IDs are globally unique while command idempotency keys are unique per
project. A batch query therefore uses the following predicate:

```text
(project_id = batch.project_id AND idempotency_key IN keys)
OR id IN command_ids
```

Rows are indexed in memory by both fields. The original command order selects
the reported key, preserving the existing conflict contract. Inputs are chunked
to fewer than 999 SQLite bind values per query, so the optimization is valid for
large batches as well as ordinary requests.

## Deferred Optimizations

The following candidates are intentionally deferred until their contracts and
benchmarks exist:

- Error-envelope refactoring: existing non-finite JSON API regression coverage
  already returns the intended 422 response. Reuse-only changes are deferred
  because they do not remove a measured hot path or a current contract failure.
- Request-body buffering: a valid `Content-Length` cannot be trusted as a reason
  to bypass the byte limiter. Any streaming replacement needs a bounded ASGI
  receive wrapper with tests for chunked bodies, disconnects, and oversized
  chunks.
- Workspace copying: combining preflight and copy traversal needs profiling on
  realistic KiCad repositories and must retain link/reparse-point protections.
- Test startup: a pre-migrated SQLite template or worker parallelism is useful
  only after profiling confirms Alembic setup dominates the suite.

## Verification Matrix

| Change | Focused verification |
| --- | --- |
| KiCad identity | Unit test for replacement after expected probe; integration test that proposal execution passes its preflight capability. |
| Remote configuration | Unit tests for oversized actor IDs and non-ASCII tokens; remote API contract tests remain green. |
| Batch conflict query | Integration test counts command-row selects for a multi-command batch and retains conflict tests. |
| Component import streaming | `python -m pytest tests/unit/test_artifacts.py tests/unit/test_components.py tests/integration/test_component_revisions.py tests/e2e/test_component_api_cli.py -q`. |
| Whole change | `python -m pytest -q`, coverage threshold, `python -m compileall src`, and `git diff --check`. |

## Latest Verification Run

Run on 2026-08-02 against the working tree containing this guide:

- `python -m pytest -q`: 428 passed, 2 skipped in 340.60 seconds.
- `python -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90`:
  428 passed, 2 skipped in 363.49 seconds; total coverage 91.25%.
- `python -m compileall -q src`, `git diff --check`, and
  `git diff --cached --check`: passed with no errors.

Both skipped tests require a locally installed `kicad-cli`; the rest of the
suite executed normally.
