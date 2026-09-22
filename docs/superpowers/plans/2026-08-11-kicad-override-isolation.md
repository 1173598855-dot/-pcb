# KiCad Override Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure an injected `KicadPort` never triggers host KiCad executable discovery during container construction.

**Architecture:** Keep `build_container()` as the composition root. Branch only at the existing `kicad_override` seam: use the injected port directly, otherwise construct the existing `KicadCli` with unchanged settings.

**Tech Stack:** Python 3.13, pytest, unittest mock, SQLAlchemy container setup.

## Global Constraints

- Preserve all existing user changes and do not modify public API contracts.
- Do not alter KiCad discovery behavior when no override is supplied.
- Keep the change limited to container composition and its regression coverage.
- Validate with the repository's coverage threshold of at least 90 percent.

---

### Task 1: Guard the Override Composition Path

**Files:**
- Modify: `src/pcbflow/container.py:199-206`
- Test: `tests/unit/test_container.py`

**Interfaces:**
- Consumes: `build_container(settings: Settings, kicad_override: KicadPort | None = None, ...) -> Container`
- Produces: `Container.kicad` set to the injected `KicadPort` without calling `KicadCli.locate`.

- [x] **Step 1: Write the failing test**

```python
def test_build_container_does_not_locate_kicad_when_an_override_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_located(*args: object, **kwargs: object) -> Path:
        raise AssertionError("KicadCli.locate must not run for an injected port")

    monkeypatch.setattr("pcbflow.container.KicadCli.locate", fail_if_located)
    override = FakeKicad()

    container = build_container(_settings(tmp_path), kicad_override=override)

    assert container.kicad is override
```

- [x] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_container.py::test_build_container_does_not_locate_kicad_when_an_override_is_supplied -q`

Expected: FAIL because `build_container()` eagerly invokes `KicadCli.locate()`.

- [x] **Step 3: Write minimal implementation**

```python
if kicad_override is None:
    selected_kicad: KicadPort = KicadCli(
        runner,
        KicadCli.locate(settings.kicad_cli),
        settings.process_timeout_seconds,
        max_design_file_bytes=settings.max_kicad_design_file_bytes,
        max_report_bytes=settings.max_kicad_report_bytes,
    )
else:
    selected_kicad = kicad_override
```

- [x] **Step 4: Run focused tests to verify the behavior**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_container.py tests/unit/test_kicad.py -q`

Expected: PASS.

- [x] **Step 5: Verify the ordinary construction path**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_config.py tests/unit/test_kicad.py -q`

Expected: PASS; configured and automatic discovery behavior remains covered by existing KiCad tests.

### Task 2: Review and Validate the Change

**Files:**
- Review: `src/pcbflow/container.py`
- Review: `tests/unit/test_container.py`

**Interfaces:**
- Consumes: the completed override isolation change from Task 1.
- Produces: evidence that the smallest applicable change satisfies the design and does not introduce style or runtime regressions.

- [x] **Step 1: Inspect the focused diff**

Run: `git diff -- src/pcbflow/container.py tests/unit/test_container.py`

Expected: only the lazy construction branch and its focused regression test are present.

- [x] **Step 2: Run the full verification gate**

Run: `./.venv/Scripts/python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90`

Expected: exit code 0, all non-environment-gated tests pass, total coverage is at least 90 percent.

- [x] **Step 3: Compile and check whitespace**

Run: `./.venv/Scripts/python.exe -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check`

Expected: exit code 0.
