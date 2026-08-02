# File Boundary and Resource Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind component and workspace reads to verified filesystem entries, eliminate unbounded revision snapshot reads, and cap KiCad design/report input allocation.

**Architecture:** `workspaces.py` will own a handle-verified regular-file helper and a one-pass copier. POSIX will traverse children relative to an open directory descriptor with `O_NOFOLLOW`; the Windows fallback will compare pre-open, opened-handle, and post-open identity/reparse metadata. Revision hashes will stream fixed chunks under existing limits. KiCad will reject configured oversized inputs before parsing.

**Tech Stack:** Python 3.12/3.13, standard-library `os`, `stat`, `hashlib`, SQLAlchemy/FastAPI wiring, pytest.

## Global Constraints

- Preserve `WorkspaceCopier.copy()` arguments, exception classes, exclusions, and destination cleanup behavior.
- Do not follow a symlink, junction, reparse point, device, FIFO, or substitution introduced during copy.
- Component assets remain direct-sibling regular non-link files; staging-write failures must remain storage errors, not client-input errors.
- POSIX must use descriptor-relative child opens plus `O_NOFOLLOW`; Windows must check reparse metadata and identity before and after opening.
- Enforce limits against bytes actually copied or hashed with fixed 1 MiB reads.
- KiCad defaults are 50,000,000 design-file bytes and 10,000,000 report bytes; both must be configurable positive settings.
- Add focused failing regression tests before production changes.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/pcbflow/workspaces.py` | Safe-entry predicates, handle-verified regular-file opening, one-pass copying. |
| `src/pcbflow/components.py` | Component error mapping around the safe opener. |
| `tests/unit/test_components.py` | Component substitution and storage-write error regressions. |
| `tests/unit/test_revisions.py` | Copy substitution, cleanup, exclusion, and actual-byte-limit regressions. |
| `src/pcbflow/revisions.py` | Streaming snapshot hashing and snapshot limits. |
| `src/pcbflow/container.py` | Revision/KiCad limit injection. |
| `tests/integration/test_managed_projects.py` | Snapshot streaming and limit regressions. |
| `src/pcbflow/config.py` | KiCad input-limit settings. |
| `src/pcbflow/kicad.py` | Typed bounded design/report reads. |
| `src/pcbflow/validation.py` | Terminal error mapping. |
| `src/pcbflow/proposals.py` | Proposal terminal error mapping. |
| `tests/unit/test_kicad.py` | Oversized report/design tests. |
| `tests/integration/test_validation.py` | Worker terminal-status contract. |

### Task 1: Bind Component Reads to Actual File Handles

**Files:**
- Modify: `src/pcbflow/workspaces.py`
- Modify: `src/pcbflow/components.py`
- Modify: `tests/unit/test_components.py`

**Interfaces:** Add `open_regular_file(path: Path) -> ContextManager[BinaryIO]` to `pcbflow.workspaces`. It raises `WorkspaceLinkError` for a link/reparse entry and `WorkspaceEntryError` for a non-regular or replaced entry. Component-facing errors retain their current text.

- [x] **Step 1: Write failing link-substitution and storage-error tests**

Create a component fixture where the declared digest matches `outside/secret.bin`. Monkeypatch the low-level file opening path to replace the checked asset with a symlink to that outside file after validation. Assert import raises `ValueError("component evidence must be a regular non-link file")`, no revision exists, and no asset is registered.

Create another test where `container.artifacts.stage_stream` raises `OSError("disk full")` on its second call. Assert `import_revision()` raises the same `OSError`, not `ValueError("component evidence file cannot be read")`, and staging is empty.

- [x] **Step 2: Verify RED**

Run `pytest tests/unit/test_components.py -q` with the repository Python command. The current implementation follows the substituted link and catches the staging `OSError` around `yield`.

- [x] **Step 3: Implement handle verification and narrow source-error mapping**

Add this shape to `workspaces.py`; use `errno.ELOOP` to map POSIX no-follow failures to `WorkspaceLinkError`:

```python
@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    before = assert_supported_entry(path)
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceEntryError(str(path))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _is_link_or_reparse_point(opened):
            raise WorkspaceLinkError(str(path))
        if not stat.S_ISREG(opened.st_mode) or not _same_entry(before, opened):
            raise WorkspaceEntryError(str(path))
        after = assert_supported_entry(path)
        if not _same_entry(opened, after):
            raise WorkspaceEntryError(str(path))
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor != -1:
            os.close(descriptor)
```

Implement `_same_entry` with `st_dev` and `st_ino`. Keep the pre/post comparisons on Windows because it lacks POSIX `O_NOFOLLOW` behavior.

Make component `_open_regular_file` translate only errors thrown while opening the source. Map `WorkspaceLinkError` to the existing non-link message, map `WorkspaceEntryError` to the existing regular-file message, and leave code inside the context body outside the translation so `stage_stream` exceptions propagate unchanged.

- [x] **Step 4: Verify GREEN**

Run `pytest tests/unit/test_components.py tests/integration/test_component_revisions.py -q` with the repository Python command.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/workspaces.py src/pcbflow/components.py tests/unit/test_components.py` followed by `git commit -m "fix: bind component assets to verified file handles"`.

### Task 2: Copy Workspaces Through Verified Descriptors

**Files:**
- Modify: `src/pcbflow/workspaces.py`
- Modify: `tests/unit/test_revisions.py`

**Interfaces:** Preserve `WorkspaceCopier.copy(source, destination, exclude_names, registered_excludes)`. It must copy verified regular bytes once, preserve executable mode bits, and remove only a destination created by a failed call.

- [x] **Step 1: Write failing copy-race and actual-byte-limit tests**

Monkeypatch `pcbflow.workspaces.os.open` so the first child-file open replaces `source / "allowed.txt"` with a symlink to `outside / "secret.txt"` and then calls real `os.open`. Assert `WorkspaceCopier.copy()` raises `WorkspaceLinkError` or `WorkspaceEntryError`; `destination` contains no secret content and is removed after failure.

Add a growing-file test that writes more than `max_bytes` via the opened stream after initial metadata is observed. Assert `WorkspaceLimitError("total_bytes")` rather than accepting stale `stat().st_size`.

- [x] **Step 2: Verify RED**

Run `pytest tests/unit/test_revisions.py -q` with the repository Python command. It must fail because current code pre-walks then calls `shutil.copytree`, which re-traverses and follows the substituted link.

- [x] **Step 3: Replace preflight plus `copytree` with one safe traversal**

Remove the standalone `os.walk` preflight and `shutil.copytree`. Validate the root before creating the destination; create the destination once, and on any later exception delete that destination with the existing readonly-aware cleanup callback.

On POSIX, open the root with `O_DIRECTORY | O_NOFOLLOW`, enumerate `os.listdir(directory_fd)`, and open each child using `os.open(name, flags, dir_fd=directory_fd)`. Use `os.fstat` to recurse only into directories and stream only regular files. A link must fail at the actual open.

Copy regular files in 1 MiB chunks. Count each file once; increase `total_bytes` from chunk length and read no more than `remaining_bytes + 1` while enforcing the total. Raise `WorkspaceLimitError("file_count")` or `WorkspaceLimitError("total_bytes")` before writing an over-limit chunk. Use `os.fchmod` to preserve source mode bits.

On Windows, retain path enumeration only as a fallback. Validate every directory before and after enumeration with `assert_supported_entry`, and read every file through `open_regular_file`, which performs pre-open, handle, and post-open checks.

Apply exclusions before opening each child:

```python
relative = PurePosixPath((relative_parent / name).as_posix())
if name in exclude_names or is_snapshot_excluded(relative, normalized_excludes):
    continue
if any(part.startswith(".pcbflow-tmp-") for part in relative.parts):
    raise WorkspaceEntryError(str(source_path))
```

- [x] **Step 4: Verify GREEN**

Run `pytest tests/unit/test_revisions.py tests/integration/test_projects.py tests/integration/test_managed_projects.py tests/integration/test_validation.py -q` with the repository Python command.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/workspaces.py tests/unit/test_revisions.py` followed by `git commit -m "fix: copy workspaces through verified descriptors"`.

### Task 3: Stream and Limit Revision Snapshot Hashing

**Files:**
- Modify: `src/pcbflow/revisions.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_managed_projects.py`

**Interfaces:** `RevisionService` receives `max_files` and `max_bytes` from current Settings values. Snapshot output remains byte-for-byte compatible for permitted projects and raises existing `WorkspaceLimitError` for excess files/bytes.

- [x] **Step 1: Write failing streaming and limit tests**

Add a multi-megabyte managed-project file and monkeypatch only that file's `Path.read_bytes` to raise `AssertionError("snapshot must stream")`. Assert `snapshot_digest()` still matches a baseline digest.

Instantiate a `RevisionService` with `max_bytes=4` against a tree containing one 5-byte file. Assert both `snapshot_digest()` and `snapshot_manifest()` raise `WorkspaceLimitError("total_bytes")`.

- [x] **Step 2: Verify RED**

Run `pytest tests/integration/test_managed_projects.py -q` with the repository Python command. It must fail because snapshot functions call `path.read_bytes()` and own no limits.

- [x] **Step 3: Implement chunked hashing and shared limits**

Add keyword-only `max_files` and `max_bytes` to `RevisionService.__init__`, passed from `build_container`. Extend `_snapshot_files` to count selected regular files and total their sizes under the same error names used by `WorkspaceCopier`.

Use this helper in both `snapshot_digest()` and `snapshot_manifest()` without changing canonical dictionaries, field order, or `snapshot_policy_version`:

```python
def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
```

- [x] **Step 4: Verify GREEN**

Run `pytest tests/unit/test_revisions.py tests/integration/test_managed_projects.py -q` with the repository Python command.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/revisions.py src/pcbflow/container.py tests/integration/test_managed_projects.py` followed by `git commit -m "fix: bound revision snapshot hashing"`.

### Task 4: Bound KiCad Design and Report Reads

**Files:**
- Modify: `src/pcbflow/config.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/kicad.py`
- Modify: `src/pcbflow/validation.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_kicad.py`
- Modify: `tests/integration/test_validation.py`

**Interfaces:** Add `KicadInputLimitError(code, path, limit)` with codes `KICAD_DESIGN_FILE_LIMIT_EXCEEDED` and `KICAD_REPORT_LIMIT_EXCEEDED`. `KicadCli` accepts keyword-only `max_design_file_bytes` and `max_report_bytes` and receives them from Settings.

- [x] **Step 1: Write failing size-limit and terminal-mapping tests**

Use `ReportRunner` to write an ERC JSON payload beyond `KicadCli(..., max_report_bytes=32)`; `validate()` must raise `KicadInputLimitError` with the report-limit code before parsing. Create a design file over `max_design_file_bytes=32` and assert `_validate_design_format()` raises the design-limit code. At the worker level, a fake KiCad that raises the new error must produce `FAILED_TERMINAL` with that exact code.

- [x] **Step 2: Verify RED**

Run `pytest tests/unit/test_kicad.py tests/integration/test_validation.py tests/unit/test_config.py -q` with the repository Python command. It must fail because neither constructor limits nor terminal mapping exists.

- [x] **Step 3: Add settings, typed errors, and bounded reads**

Append these Settings fields after existing API settings to preserve legacy positional construction:

```python
max_kicad_design_file_bytes: int = 50_000_000
max_kicad_report_bytes: int = 10_000_000
```

Validate both positive and parse `PCBFLOW_MAX_KICAD_DESIGN_FILE_BYTES` and `PCBFLOW_MAX_KICAD_REPORT_BYTES`.

Implement:

```python
class KicadInputLimitError(ValueError):
    def __init__(self, code: str, path: Path, limit: int) -> None:
        super().__init__(f"KiCad input exceeds {limit} bytes: {path.name}")
        self.code = code
```

Before parsing a report, reject `report_file.stat().st_size` above the report limit. Then read at most `limit + 1` bytes from an opened stream and reject a growing report at the same limit. Apply the same bounded helper to `_validate_design_format` with the design limit. Pass both settings in `build_container`.

Add `KicadInputLimitError` to terminal handling in `ValidationTaskHandler` and `ProposalExecutor`, passing `error.code` unchanged.

- [x] **Step 4: Verify GREEN**

Run `pytest tests/unit/test_kicad.py tests/integration/test_validation.py tests/integration/test_proposals.py tests/unit/test_config.py -q` with the repository Python command.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/config.py src/pcbflow/container.py src/pcbflow/kicad.py src/pcbflow/validation.py src/pcbflow/proposals.py tests/unit/test_config.py tests/unit/test_kicad.py tests/integration/test_validation.py` followed by `git commit -m "fix: bound KiCad design and report inputs"`.

### Task 5: Record Hardening and Run the Complete Gate

**Files:**
- Modify: `docs/OPTIMIZATION_GUIDE.md`

- [x] **Step 1: Update the guidance**

After Tasks 1-4 are green, record completed entries for verified artifact publication, component registration serialization, retry policy, workspace/component file boundaries, streaming snapshot hashes, and KiCad limits. Remove the workspace-copy deferral and state the explicit default values.

- [x] **Step 2: Run the complete gate**

Run the repository Python command for `pytest -q`, then coverage with `--cov=pcbflow --cov-report=term-missing --cov-fail-under=90`, then `python -m compileall -q src`, `git diff --check`, and `git diff --cached --check`.

- [x] **Step 3: Record exact results and commit**

Update `Latest Verification Run` with fresh exact test counts, timings, coverage, and skipped-KiCad status. Run `git add docs/OPTIMIZATION_GUIDE.md` followed by `git commit -m "docs: record concurrency and input hardening"`.
