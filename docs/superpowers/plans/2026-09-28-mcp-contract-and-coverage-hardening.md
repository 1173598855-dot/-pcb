# MCP Tool Contract and Coverage Hardening Plan

**Goal:** Close the coverage gap on the evidence-critical modules (`pcb_candidate_codec`, `mcp_server`, `workspaces`), fix the MCP tool contract defects the new tests exposed, and tighten the mypy/CI gates.

**Architecture:** No production behavior change outside `pcbflow/mcp_server.py`. The MCP handlers return payload dicts and the registered tools serialize them; omitted tool arguments now resolve their documented defaults. Tests target the exact production paths (JSON round-trip through the codec, the Windows copy guards, and the MCP tool surface with a stubbed transport).

**Tech Stack:** Python 3.12/3.13, pytest, asyncio, monkeypatched `urllib` transport.

## Global Constraints

- No changes to database tables, REST/CLI envelopes, digests, or candidate state machines.
- MCP tool outputs may change only because the previous behavior was a crash (`TypeError`) at every tool invocation.
- Tests must stay hermetic: no real network, no model SDKs (`openai`/`anthropic` remain uninstalled and guarded by `skipif`).
- Keep the repository coverage threshold and improve the weakest modules without excluding lines.

---

### Task 1: Codec round-trip and rejection coverage

**Files:**
- Add: `tests/unit/test_pcb_candidate_codec.py`

**Interfaces:**
- Consumes: `_operation_payload` (the store's persist path, `pcb_candidate_store.py:287`) and `deserialize_board_operations` (the read path used by `pcb_candidate_execution.py` and `pcb_release.py`).
- Contract: every supported `BoardOperation` type survives a real JSON round-trip unchanged; every malformed payload is rejected with `ValueError`/`TypeError`.

- [x] **Step 1: Round-trip all five operation types** (`board.place_footprints`, `board.route_nets`, `board.create_copper_zones`, `board.add_ground_stitching`, `board.lock_board_objects`) through `json.loads(json.dumps(_operation_payload(op)))`. The JSON hop is mandatory: persisted payloads are JSON, and `BoardObjectId` must collapse to plain strings exactly as production sees them.
- [x] **Step 2: Reject non-sequences, unknown operation types, non-object payloads, extra/missing fields, blank common strings, non-list collections, non-object points, non-integer widths, and non-boolean locks.**

### Task 2: MCP tool contract tests and defect fixes

**Files:**
- Modify: `src/pcbflow/mcp_server.py`
- Modify: `tests/unit/test_mcp_server.py`

**Defects found and fixed:**

1. **Every registered MCP tool crashed on invocation.** The tool functions wrapped the `_handle_*` results (already `CallToolResult` objects) in `json.dumps`, raising `TypeError: Object of type CallToolResult is not JSON serializable`. Fix: handlers return the payload `dict`; the tool functions serialize it. `_text_result` and the `mcp.types` import are removed.
2. **Omitted tool arguments defeated their own defaults.** The wrappers put `None` into `args` for omitted parameters, so `args.get(key, default)` never fired: `pcbflow_query_eda_capabilities()` returned `{"eda_capabilities": {"null": {}}}`, and `pcbflow_run_workflow` without `collaboration_chain` always failed chain lookup. Fix: handler-side defaults use truthiness (`args.get(key) or default`).

- [x] **Step 1: Transport-layer tests for `_post_json_sync`** (success decode, HTTP error detail, timeout) and the async `_post_json` dispatch.
- [x] **Step 2: Endpoint dispatch through `_call_model`** for LOCAL and AZURE with a stubbed `_post_json`; ImportError guards for the optional SDKs; unhealthy-model rejection and the 300 s health recheck; half-open circuit breaker closing on success.
- [x] **Step 3: Every registered tool callable end-to-end** (`register_project`, `run_workflow` falling back until the LOCAL model answers, `query_chains` for all/single/unknown, `list_history`, `list_projects`, `read_artifacts`, `query_eda_capabilities` filtering) plus the `main()` entry point.

### Task 3: Workspace guard coverage on the Windows path

**Files:**
- Add: `tests/unit/test_workspaces.py`

**Scope note:** the POSIX descriptor-relative copy branch (`copy_posix_directory`, `open_at`) cannot execute on Windows, where all verification runs; those lines remain covered by design review only. Every guard reachable on the Windows path is tested.

- [x] **Step 1: `open_regular_file` guards** — directory rejection, `ELOOP` mapping to `WorkspaceLinkError`, unrelated `OSError` passthrough, reparse point reported by `os.fstat`, and the post-open entry-replacement TOCTOU check (via a flaky `assert_supported_entry` stub).
- [x] **Step 2: Copy guards** — non-directory source rejection, subdirectory recursion with content equality, mode preservation on the non-`fchmod` fallback (Python 3.13 exposes `fchmod` on Windows; the fallback is forced with `monkeypatch.delattr`), and `_remove_readonly_entry` re-raise plus chmod-and-retry semantics.

### Task 4: Toolchain and CI gates

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `src/pcbflow/schematic/adapter.py`

- [x] **Step 1: Enable `check_untyped_defs`** in `[tool.mypy]`. The strict sweep surfaced exactly one error: `schematic/adapter.py` inferred `children` as `list[object]` because `CstNode` is a union alias, not a common base class; fixed with an explicit `list[CstNode]` annotation.
- [x] **Step 2: Add Python 3.12 to the CI test matrix** alongside 3.13. `requires-python` declares `>=3.12,<3.14`; until now 3.12 was never executed.
- [x] **Step 3: Relocate the pytest cache** to `.pytest-cache` (matched by the existing `.pytest-*` gitignore pattern). The legacy `.pytest_cache` directory has unrecoverable Windows ACLs that made every run emit a `PytestCacheWarning`; removal requires an elevated shell.

## Verification

- Focused: `pytest tests/unit/test_pcb_candidate_codec.py tests/unit/test_workspaces.py tests/unit/test_mcp_server.py -q` — 63 passed.
- Full: `pytest -q -n auto --cov=pcbflow --cov-fail-under=88` with ruff and mypy green — see `docs/OPTIMIZATION_GUIDE.md` "Latest Verification Run" for the recorded numbers of this increment.
