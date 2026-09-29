# PCBFlow Optimization Guide

Updated: 2026-09-29

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

## 2026-09-29 Increment: Candidate Store Edges and Documentation Reorganization

| Status | Priority | Area | Change | Acceptance condition |
| --- | --- | --- | --- | --- |
| Complete | P1 | Candidate store edge paths | 15 tests added (`test_pcb_candidate_store_edges.py`): public-input resolution (missing base revision, missing LCEDA authority, unavailable BoardIR digest, capability digest resolved from evidence, blocked without evidence), creation guards (non-tuple operations, unverified output_kind, seed validation inside algorithm evidence, stale base revision enforced both pre-transaction and inside `BEGIN IMMEDIATE`), and IntegrityError race recovery (in-transaction replay after a hidden pre-lookup, replay and conflict mapping after a forced candidate-row flush failure). | Focused file green; `pcb_candidate_store` coverage rose from 82% to 87% (the remaining misses are release-state transition and mirror branches). |
| Complete | P2 | Documentation reorganization | README restructured: positioning moved to the top, journal sections moved to `PROJECT_STATUS.md`, environment variables merged into one table that matches `config.py` (including five previously undocumented `PCBFLOW_MAX_KICAD_*`/`PCBFLOW_TASK_RETRY_*` variables), English sections translated, MCP server section added. `QUICK_REFERENCE.md` slimmed to troubleshooting/deployment/tuning. The stale 2026-08-03 auto-generated quality report removed from `PROJECT_STATUS.md`. | Docs describe the shipped surface only; numbers match measured values. |
| Complete | P2 | Known quirk recorded | pytest-cov submodule coverage targets (e.g. `--cov=pcbflow.approvals`) fail during collection under coverage 7.15.2 with numpy's "cannot load module more than once per process"; the full-package target `--cov=pcbflow` is unaffected, so CI and the documented commands are safe. | README documents the quirk and the workaround. |
| Rejected | P2 | mypy `disallow_untyped_defs` | Measured: enabling it surfaces 80 missing-annotation errors across 20 files. Fixing those is mechanical churn with no behavioral gain while `check_untyped_defs` already checks those bodies. | Revisit only if the package adopts full strict mode deliberately. |

Deferred from the 2026-09-28 increment (state corrected 2026-09-29):

- The POSIX workspace-copy branch needs a POSIX test runner (the advisory
  `test-posix` CI job now accumulates that evidence) before its coverage can
  rise on Windows-only runs.
- The earlier note named `proposal_store.py` (83%) and `approvals.py` (84%) as
  the next gaps; the 2026-09-28 edge tests already landed before that note was
  written, and the measured full-suite numbers are 99% and 91%. The actual
  next-largest Windows-reachable gaps are `schematic/adapter.py` (84%),
  `lceda_pro.py` (81%), and `kicad_export.py` (83%).

## 2026-09-28 Increment: MCP Contract and Coverage Hardening

Plan: `docs/superpowers/plans/2026-09-28-mcp-contract-and-coverage-hardening.md`.

| Status | Priority | Area | Change | Acceptance condition |
| --- | --- | --- | --- | --- |
| Complete | P0 | MCP tool contract | Every registered MCP tool raised `TypeError` on invocation (`json.dumps` on a `CallToolResult`). Handlers now return payload dicts and tools serialize them. | All seven tools return parseable JSON payloads through the registered tool functions. |
| Complete | P0 | MCP argument defaults | Tool wrappers pass omitted arguments as explicit `None`, so `dict.get(key, default)` never fired; `pcbflow_query_eda_capabilities()` returned `{"null": {}}` and `pcbflow_run_workflow` without `collaboration_chain` always failed. Defaults now use truthiness. | Omitted optional arguments resolve their documented defaults. |
| Complete | P1 | Evidence-critical coverage | `pcb_candidate_codec` 58% → 100%, `mcp_server` 66% → 96%, `workspaces` 64% → 71% (the remainder is the POSIX descriptor-relative branch, unreachable on Windows), `worker_service` 75% → 99%. 68 tests added across four files. | Full suite green with the coverage gate. |
| Complete | P0 | Warning-free suite | `filterwarnings = ["error"]` is enforced. An intermittent `ResourceWarning: unclosed database` was hunted with a deterministic per-test `gc.collect()` probe in `tests/conftest.py`; under deterministic collection the whole suite is leak-free, so the original warning was a one-off GC-timing artifact. The probe stays as a permanent regression guard. | The full suite reports zero warnings and treats any warning as a failure. |
| Complete | P2 | Coverage gate | CI and README gate raised from 88 to 90 (measured 91.13%). | `--cov-fail-under=90` passes locally and in CI. |
| Complete | P1 | Proposal store edge paths | 27 tests added (`test_proposal_store_edges.py`): not-found lookups, batch-without-proposal, IntegrityError recovery and command-key conflict mapping, stale-fence guards, wrong proposal/task binding with an active lease, evidence/artifact registration conflicts, `mark_ready` evidence validation branches, and accept/reject replay idempotency including stale-decision replay. | Focused file green; store edge branches execute. |
| Complete | P1 | Approvals edge paths | 19 tests added (`test_approvals_edges.py`): G1 replay, missing/mismatched requirement set, digest and revision mismatch, stale-version and version-race conflicts, missing/unfrozen active requirement set, plus direct coverage of the G3 payload validators (`_valid_native_drc_payload`, `_valid_boardir_validation_payload`, `_valid_candidate_summary_payload`), `_expected_candidate_digest`, and `_verify_evidence_set` malformed-shape branches. | Focused file green. |
| Complete | P2 | Flake hardening | The hypothesis property in `test_artifacts.py` gets `deadline=None` (parallel scheduling can exceed the default 200 ms per example). The real-KiCad contract tests remain load-sensitive: one flaky occurrence under an unusually loaded full run; they passed on re-run and in three isolated `-n auto` runs. | Full suite green across repeated runs. |
| Complete | P0 | Test-fixture connection race | An intermittent `ResourceWarning: unclosed database` under parallel runs was traced to a teardown race: `engine.dispose()` while a worker heartbeat thread was mid-renew stranded the returned connection in the orphaned pool object. Fixture teardowns now drain checked-out connections (`pool.checkedout()==0`) before disposing, and the per-test GC probe (kept as a leak guard) drains all registered engines before forcing collection. | 8/8 stress runs of the previously failing combination pass; full suite clean under `filterwarnings = ["error"]`. |
| Complete | P2 | POSIX CI evidence | Advisory `test-posix` job (ubuntu-latest, `continue-on-error`) runs the full suite on Linux for the first time, exercising the `workspaces.py` POSIX copy branch. Promote to a required gate once green across several runs. | Job present in CI; first runs recorded. |
| Complete | P1 | mypy strictness | `check_untyped_defs` enabled; one fix in `schematic/adapter.py` (`children` inferred as `list[object]` because `CstNode` is a union alias). | `mypy src/pcbflow` stays clean. |
| Complete | P2 | CI matrix | The test job now runs the full suite on Python 3.12 and 3.13; `requires-python` claims 3.12 but CI never executed it. | Both supported majors run the full gate per push. |
| Complete | P2 | Test infrastructure | The legacy `.pytest_cache` directory has unrecoverable Windows ACLs; pytest cache relocated to `.pytest-cache`. Nine stale `.pytest-tmp-*` directories removed; four ACL-locked directories and `.pytest_cache` require an elevated shell to delete. | No `PytestCacheWarning` during runs. |

Deferred from this increment:

- The POSIX workspace-copy branch needs a POSIX test runner (or contract tests
  on a Linux CI job) before its coverage can rise on Windows-only runs.
- `proposal_store.py` (83%) and `approvals.py` (84%) are the next-largest
  Windows-reachable coverage gaps after this increment.

## Execution Status (2026-08-02 baseline)

| Status | Priority | Area | Change | Acceptance condition |
| --- | --- | --- | --- | --- |
| Complete | P0 | KiCad execution identity | Proposal validation is bound to the capability observed during preflight. It reuses that version/profile and verifies the executable digest immediately before every spawned validation command. | A replacement after preflight is rejected before a new command is run. |
| Complete | P0 | Remote API configuration | The configured service actor now has the same 255-character storage/domain bound, configured tokens must be ASCII, and request actor limits align with that boundary. | Invalid startup configuration fails deterministically instead of producing a request-time 500. |
| Complete | P1 | Command batch persistence | Per-command conflict lookups inside `BEGIN IMMEDIATE` are replaced with bounded set queries and in-memory command-order conflict selection. | A normal multi-command batch uses one command-conflict query while preserving the exact conflicting key. |
| Complete | P1 | Content-addressed component imports | Canonical manifests and declared assets stream through store-owned staging files, are SHA-256 checked before publication, and retain the cumulative import limit. | Large assets use bounded reads; a late asset failure leaves no component revision or staging residue. |
| Complete | P0 | Artifact publication | Staged content-addressed objects publish with atomic no-clobber links and verify an object created concurrently. | A competing publisher cannot be overwritten; mismatched existing content is an artifact conflict. |
| Complete | P0 | Component registration serialization | Component revision creation reserves SQLite's write path before catalog reads that lead to registration. | Concurrent matching imports remain idempotent and conflicting imports retain their conflict contract. |
| Complete | P1 | Retry scheduling | Retryable tasks persist eligibility timestamps and use a bounded exponential delay with an attempt ceiling. | Fresh queued work is not blocked by ineligible retries; defaults are 5 attempts, 5 seconds, and 300 seconds. |
| Complete | P0 | Component and workspace file boundaries | Component reads and workspace copies use verified regular-file handles, descriptor-relative traversal on POSIX, and actual-byte limits. | Links, reparse points, replacement races, and over-limit copies are rejected without leaking destination data. |
| Complete | P1 | Revision snapshot hashing | Snapshot digest and manifest generation share fixed 1 MiB streaming reads and enforce file/byte limits from actual input. | Permitted snapshot canonical output remains stable; oversized or growing files raise `WorkspaceLimitError`. |
| Complete | P0 | KiCad input limits | Design and report reads use positive configurable limits with distinct terminal error codes. | Defaults are 50,000,000 design bytes and 10,000,000 report bytes; growing inputs cannot exceed either bound. |
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

## Boundary and Resource Hardening

The completed hardening work uses one-pass, bounded operations at every file
boundary. Component assets are opened as verified regular-file handles before
they are staged. Workspace copies traverse verified descriptors on POSIX and
perform pre-open, handle, and post-open identity checks on Windows. Both paths
reject links, reparse points, replacement races, non-regular entries, and
actual bytes beyond the configured project limits.

Revision snapshots hash selected files through 1 MiB reads and share the same
file-count and byte-limit contract for digest and manifest evidence. KiCad
design and report inputs have independent positive limits, defaulting to
50,000,000 and 10,000,000 bytes respectively. A report or design file that is
already too large, or grows past its limit after the initial metadata check,
raises a typed terminal error with code
`KICAD_REPORT_LIMIT_EXCEEDED` or `KICAD_DESIGN_FILE_LIMIT_EXCEEDED`.

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
- Test startup: a pre-migrated SQLite template or worker parallelism is useful
  only after profiling confirms Alembic setup dominates the suite.

## Verification Matrix

| Change | Focused verification |
| --- | --- |
| KiCad identity | Unit test for replacement after expected probe; integration test that proposal execution passes its preflight capability. |
| Remote configuration | Unit tests for oversized actor IDs and non-ASCII tokens; remote API contract tests remain green. |
| Batch conflict query | Integration test counts command-row selects for a multi-command batch and retains conflict tests. |
| Component import streaming | `python -m pytest tests/unit/test_artifacts.py tests/unit/test_components.py tests/integration/test_component_revisions.py tests/e2e/test_component_api_cli.py -q`. |
| Artifact publication and component registration | Artifact race and component catalog ordering regressions, plus the focused component/API suite. |
| Retry scheduling | Task timing, fairness, exhaustion, migration, and configuration tests. |
| File boundaries and snapshots | Component substitution, workspace replacement/limit, and managed snapshot streaming regressions. |
| KiCad input limits | Design/report overage tests and validation/proposal terminal-code regressions. |
| Whole change | `python -m pytest -q`, coverage threshold, `python -m compileall src`, and `git diff --check`. |

## Latest Verification Run

Run on 2026-09-29 against the working tree containing this guide (Python 3.13.9,
Windows):

- `python -m pytest -q -n auto --cov=pcbflow --cov-report=term-missing --cov-fail-under=90`:
  1024 passed, 1 skipped (LCEDA Pro bridge contract, machine lacks a verified
  bridge) in 234.78 s; total coverage 91.86%; zero warnings under
  `filterwarnings = ["error"]`; exit 0.
- `ruff check src tests` and `mypy src/pcbflow` (with `check_untyped_defs`):
  clean, 70 source files.
- `git diff --check`: clean.

Run on 2026-09-28 (Python 3.13.9, pytest 8.4.2, Windows):

- `python -m pytest -q -n auto --cov=pcbflow --cov-report=term --cov-fail-under=90`:
  1009 passed, 1 skipped (LCEDA Pro bridge contract, machine lacks a verified
  bridge) in 206 s; total coverage 91.72%; zero warnings under
  `filterwarnings = ["error"]`; exit 0.
- `ruff check src tests` and `mypy src/pcbflow` (with `check_untyped_defs`):
  clean, 70 source files.
- `git diff --check`: clean.

Historical baseline (2026-08-02, 453 tests, 90.26% serial coverage) is
superseded by the numbers above; the module split and subsequent increments
grew the suite to 1009 tests.

## Latest Verification Run (2026-08-02 baseline)

Run on 2026-08-02 against the working tree containing this guide:

- `python -m pytest -q`: 453 passed in 410.51 seconds.
- `python -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90`:
  453 passed in 820.18 seconds; total coverage 90.26%.
- `python -m compileall -q src`, `git diff --check`, and
  `git diff --cached --check`: passed with no errors.

The test commands used a fresh `--basetemp` under `C:\tmp` and disabled pytest's
cache provider because this checkout's legacy `.pytest-tmp` and `.pytest_cache`
directories had inaccessible Windows ACLs.
