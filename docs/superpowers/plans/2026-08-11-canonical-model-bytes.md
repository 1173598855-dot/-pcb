# Canonical Model Bytes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make model canonical bytes the single source for BoardIR and rulepack digests and candidate evidence payloads.

**Architecture:** `BoardSnapshot` and `ManufacturingRulePack` encode their already-canonical dictionaries in model-owned methods. Their digest methods hash exactly those bytes. G3 evidence publication and G4 frozen-input validation reuse the resulting bytes or digests instead of rebuilding equivalent canonical payloads.

**Tech Stack:** Python 3.13, pytest, SHA-256, JSON canonicalization.

## Global Constraints

- Existing SHA-256 digest values and persisted evidence bytes must remain identical.
- Do not change public candidate behavior, APIs, database tables, or migrations.
- Do not introduce an alternate JSON serializer or change the global `canonical_json_bytes` contract.
- Preserve the repository-wide coverage threshold of at least 90 percent.

---

### Task 1: Specify Canonical Byte Invariants

**Files:**
- Modify: `tests/unit/test_board_ir.py:90-122`
- Modify: `tests/unit/test_board_rulepack.py:52-57`

**Interfaces:**
- Produces: `BoardSnapshot.canonical_bytes() -> bytes`
- Produces: `ManufacturingRulePack.canonical_bytes() -> bytes`
- Requires: `model.canonical_digest()` equals `"sha256:" + hashlib.sha256(model.canonical_bytes()).hexdigest()`.

- [x] **Step 1: Write failing BoardIR and rulepack tests**

```python
assert snapshot.canonical_digest() == (
    "sha256:" + hashlib.sha256(snapshot.canonical_bytes()).hexdigest()
)
assert rulepack.canonical_digest() == (
    "sha256:" + hashlib.sha256(rulepack.canonical_bytes()).hexdigest()
)
```

- [x] **Step 2: Run the tests to verify the expected red state**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_board_ir.py tests/unit/test_board_rulepack.py -q`

Expected: FAIL with `AttributeError` because neither model exposes `canonical_bytes`.

### Task 2: Centralize Model Serialization

**Files:**
- Modify: `src/pcbflow/canonical.py:7-18`
- Modify: `src/pcbflow/board/ir.py:543-557`
- Modify: `src/pcbflow/board/rulepack.py:250-253`
- Modify: `src/pcbflow/pcb_candidates.py:98-106,546-556,1800-1824`

**Interfaces:**
- Consumes: `BoardSnapshot.canonical_bytes() -> bytes`
- Consumes: `ManufacturingRulePack.canonical_bytes() -> bytes`
- Produces: unchanged digest strings and evidence artifact bytes.

- [x] **Step 1: Add the model methods**

```python
def sha256_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"

def canonical_bytes(self) -> bytes:
    return canonical_json_bytes(_canonicalize_object_lists(...))

def canonical_digest(self) -> str:
    return sha256_digest(self.canonical_bytes())
```

- [x] **Step 2: Remove parallel candidate serializers and reuse validated G3/G4 bytes**

```python
input_staged = stage("pcb_input_snapshot", before.canonical_bytes(), media_type)
rulepack_staged = stage("rulepack", rulepack.canonical_bytes(), media_type)
post_snapshot = after.canonical_bytes()
```

- [x] **Step 3: Verify unit behavior and compatibility regression values**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_board_ir.py tests/unit/test_board_rulepack.py tests/unit/test_board_copper.py tests/unit/test_board_validation.py -q`

Expected: PASS, including the pre-existing fixed BoardIR digest value.

### Task 3: Verify Candidate Evidence Integration

**Files:**
- Review: `src/pcbflow/pcb_candidates.py`
- Test: `tests/integration/test_pcb_g3.py`
- Test: `tests/integration/test_pcb_candidates.py`

**Interfaces:**
- Consumes: model-owned canonical byte methods from Task 2.
- Produces: unchanged candidate evidence digest validation.

- [x] **Step 1: Inspect the focused diff**

Run: `git diff -- src/pcbflow/board/ir.py src/pcbflow/board/rulepack.py src/pcbflow/pcb_candidates.py tests/unit/test_board_ir.py tests/unit/test_board_rulepack.py`

Expected: no changed digest constants, no remaining `_BOARD_OBJECT_LISTS`, `_canonical_snapshot_bytes`, or `_canonical_rulepack_bytes` in `pcb_candidates.py`.

- [x] **Step 2: Run candidate integration tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_pcb_g3.py tests/integration/test_pcb_candidates.py -q`

Expected: PASS; candidate evidence remains bound to frozen inputs.

- [x] **Step 3: Run repository gates**

Run: `./.venv/Scripts/python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90`

Expected: exit code 0 and total coverage at least 90 percent.

Run: `./.venv/Scripts/python.exe -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check`

Expected: exit code 0.
