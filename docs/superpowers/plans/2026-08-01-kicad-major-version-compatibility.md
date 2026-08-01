# KiCad Major-Version Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support explicitly verified KiCad 9.x and 10.x profiles, carry their identity through validation and proposal evidence, and make later major-version support an additive profile-and-contract change.

**Architecture:** Add a small immutable compatibility registry between version probing and `KicadCli`, then pass the selected major/profile into the CST adapter and evidence pipeline. Keep parser goldens separate from native real-tool fixtures; unknown majors and unsupported file formats fail with stable codes before KiCad output can be mistaken for an ordinary ERC/DRC result.

**Tech Stack:** Python 3.12/3.13, dataclasses, Pydantic v2, KiCad S-expression CST, KiCad 9/10 CLI, pytest, Typer/FastAPI JSON encoding.

## Global Constraints

- Preserve the current Python requirement `>=3.12,<3.14` and SQLite-only Phase 2A scope.
- Support KiCad `>=9.0.0,<10.0.0` as profile `kicad-9-v1` and KiCad `>=10.0.0,<11.0.0` as profile `kicad-10-v1`.
- Never accept an unknown KiCad major version merely because it is newer.
- Never modify the registered external `source_path`; real validation and writes remain isolated.
- Never use regex or line-oriented replacement to modify `.kicad_sch` files.
- Keep `KicadPort`, REST routes, task states, SQLite tables, and review-digest construction backward compatible.
- Preserve strict Pydantic validation and `extra="forbid"` for module manifests.
- Keep parser/CST goldens separate from real CLI fixtures.
- Keep full-suite coverage at or above 90 percent.
- Before implementation, verify and commit the existing Phase 2A hardening diff separately; several plan files overlap that uncommitted work.

## Execution Preflight

The current branch contains a verified but uncommitted Phase 2A hardening diff.
Before Task 1, rerun its gate and checkpoint only tracked files:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
git add -u
git diff --cached --check
git commit -m "fix: harden controlled design workflows"
```

Expected: the baseline suite reports at least the previously observed `311
passed, 1 skipped`; the skip is the old KiCad-9-only real contract. Inspect
`git diff --cached --name-only` before the commit and leave this implementation
plan out of that baseline commit if it has not already been committed.

## Planned File Map

Create:

```text
src/pcbflow/kicad_compatibility.py
tests/unit/test_kicad_compatibility.py
tests/fixtures/kicad/real/README.md
tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_sch
tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_pcb
tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_pro
tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_sch
tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_pcb
tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_pro
tests/fixtures/kicad/real/10/compatible-legacy/up-down-c.kicad_sch
tests/fixtures/kicad/real/10/compatible-legacy/up-down-c.kicad_pro
tests/fixtures/kicad/real/10/controlled-write/up-down-c.kicad_sch
tests/fixtures/kicad/real/10/controlled-write/up-down-c.kicad_pro
```

Modify:

```text
src/pcbflow/kicad.py
src/pcbflow/validation.py
src/pcbflow/proposals.py
src/pcbflow/schematic/semantic.py
src/pcbflow/schematic/adapter.py
src/pcbflow/schematic/modules.py
tests/unit/test_kicad.py
tests/unit/test_schematic_semantic.py
tests/unit/test_schematic_adapter.py
tests/unit/test_schematic_modules.py
tests/integration/test_validation.py
tests/integration/test_proposals.py
tests/e2e/test_api_cli.py
tests/e2e/test_controlled_design_change.py
tests/contract/test_kicad_cli.py
tests/contract/test_kicad_schematic_write.py
tests/fixtures/modules/status-led-v1/module.yaml
README.md
docs/DEVELOPMENT_GUIDE.md
```

---

### Task 1: Immutable Compatibility Profiles And Version Probe

**Files:**
- Create: `src/pcbflow/kicad_compatibility.py`
- Create: `tests/unit/test_kicad_compatibility.py`
- Modify: `src/pcbflow/kicad.py:14-27,151-210`
- Modify: `tests/unit/test_kicad.py:1-90`

**Interfaces:**
- Produces: `KicadCompatibilityProfile`
- Produces: `select_kicad_profile(version: str) -> KicadCompatibilityProfile | None`
- Produces: `profile_by_id(profile_id: str) -> KicadCompatibilityProfile | None`
- Produces: `profile_for_major(major: int) -> KicadCompatibilityProfile | None`
- Extends: `KicadCapability.major`, `profile_id`, and `profile_revision`

- [ ] **Step 1: Write profile selection and KiCad 10 probe tests**

```python
# tests/unit/test_kicad_compatibility.py
import pytest

from pcbflow.kicad_compatibility import select_kicad_profile


@pytest.mark.parametrize(
    ("version", "profile_id", "major"),
    (("9.0.0", "kicad-9-v1", 9), ("9.0.7", "kicad-9-v1", 9),
     ("10.0", "kicad-10-v1", 10), ("10.0.4", "kicad-10-v1", 10)),
)
def test_selects_an_explicit_supported_profile(
    version: str, profile_id: str, major: int
) -> None:
    profile = select_kicad_profile(version)
    assert profile is not None
    assert profile.profile_id == profile_id
    assert profile.major == major
    assert profile.revision == 1


@pytest.mark.parametrize("version", ("8.0.7", "11.0.0", "10.0.0-rc1", "bad"))
def test_rejects_versions_without_an_exact_verified_profile(version: str) -> None:
    assert select_kicad_profile(version) is None
```

Append to `tests/unit/test_kicad.py`:

```python
def test_probe_accepts_kicad_10_and_reports_profile_identity(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    runner = VersionRunner(
        ProcessResult((str(executable), "--version"), 0, "10.0.4\n", "", False)
    )

    report = KicadCli(runner, executable, 5).probe()

    assert report.available
    assert report.version == "10.0.4"
    assert report.major == 10
    assert report.profile_id == "kicad-10-v1"
    assert report.profile_revision == 1
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py::test_probe_accepts_kicad_10_and_reports_profile_identity -q
```

Expected: FAIL because `pcbflow.kicad_compatibility` and the capability profile fields do not exist.

- [ ] **Step 3: Implement the immutable profile registry**

```python
# src/pcbflow/kicad_compatibility.py
from __future__ import annotations

from dataclasses import dataclass


Release = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class KicadCompatibilityProfile:
    profile_id: str
    revision: int
    major: int
    minimum: Release
    maximum_exclusive: Release
    schematic_format_versions: frozenset[int]
    pcb_format_versions: frozenset[int]
    validation_operations: frozenset[str]


_PROFILES = (
    KicadCompatibilityProfile(
        profile_id="kicad-9-v1",
        revision=1,
        major=9,
        minimum=(9, 0, 0),
        maximum_exclusive=(10, 0, 0),
        schematic_format_versions=frozenset((20231120, 20250114)),
        pcb_format_versions=frozenset((20240108, 20241229)),
        validation_operations=frozenset(("erc", "drc")),
    ),
    KicadCompatibilityProfile(
        profile_id="kicad-10-v1",
        revision=1,
        major=10,
        minimum=(10, 0, 0),
        maximum_exclusive=(11, 0, 0),
        schematic_format_versions=frozenset((20231120, 20250114)),
        pcb_format_versions=frozenset((20240108, 20241229)),
        validation_operations=frozenset(("erc", "drc")),
    ),
)


def _release(version: str) -> Release | None:
    parts = version.split(".")
    if len(parts) not in (2, 3) or any(not part.isdecimal() for part in parts):
        return None
    values = tuple(int(part) for part in parts)
    return (values[0], values[1], values[2] if len(values) == 3 else 0)


def select_kicad_profile(version: str) -> KicadCompatibilityProfile | None:
    release = _release(version)
    if release is None:
        return None
    return next(
        (
            profile
            for profile in _PROFILES
            if profile.minimum <= release < profile.maximum_exclusive
        ),
        None,
    )


def profile_by_id(profile_id: str) -> KicadCompatibilityProfile | None:
    return next((item for item in _PROFILES if item.profile_id == profile_id), None)


def profile_for_major(major: int) -> KicadCompatibilityProfile | None:
    return next((item for item in _PROFILES if item.major == major), None)
```

Extend `KicadCapability` without changing its existing first five positional fields:

```python
@dataclass(frozen=True, slots=True)
class KicadCapability:
    available: bool
    path: Path | None
    version: str | None
    executable_digest: str | None
    reason: str | None
    major: int | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
```

In `KicadCli.probe()`, replace the hard-coded major-9 branch with
`select_kicad_profile(version)`. Populate all three profile fields only on a
successful profile selection; unavailable results leave them `None`.

- [ ] **Step 4: Run profile and existing probe tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the profile registry**

```powershell
git add src/pcbflow/kicad_compatibility.py src/pcbflow/kicad.py tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py
git commit -m "feat: add explicit KiCad compatibility profiles"
```

---

### Task 2: Profile-Driven CLI Commands, Format Checks, And Stable Errors

**Files:**
- Modify: `src/pcbflow/kicad_compatibility.py`
- Modify: `src/pcbflow/kicad.py:29-52,212-298`
- Modify: `src/pcbflow/validation.py:146-191`
- Modify: `tests/unit/test_kicad_compatibility.py`
- Modify: `tests/unit/test_kicad.py:92-213`
- Modify: `tests/integration/test_validation.py`

**Interfaces:**
- Produces: `KicadCompatibilityProfile.validation_argv(*, executable: Path, kind: Literal["erc", "drc"], design_file: Path, report_file: Path) -> tuple[str, ...]`
- Produces: `KicadDesignFormatError.code == "KICAD_FILE_FORMAT_UNSUPPORTED"`
- Produces: `KicadOperationUnsupportedError.code == "KICAD_OPERATION_UNSUPPORTED"`
- Extends: `RawValidationReport` with executable/profile identity

- [ ] **Step 1: Write failing command, format, and validation-result tests**

Add tests which assert:

```python
def test_kicad_10_profile_builds_the_verified_erc_command(tmp_path: Path) -> None:
    profile = select_kicad_profile("10.0.4")
    assert profile is not None
    argv = profile.validation_argv(
        executable=tmp_path / "kicad-cli.exe",
        kind="erc",
        design_file=tmp_path / "board.kicad_sch",
        report_file=tmp_path / "erc.json",
    )
    assert argv[1:] == (
        "sch", "erc", "--format", "json", "--output",
        str(tmp_path / "erc.json"), str(tmp_path / "board.kicad_sch"),
    )


def test_validate_rejects_a_format_outside_the_selected_profile(tmp_path: Path) -> None:
    # Create an otherwise parseable root with an unregistered format date.
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_sch").write_text(
        '(kicad_sch (version 20990101) (uuid "00000000-0000-0000-0000-000000000001"))',
        encoding="utf-8",
    )
    with pytest.raises(KicadDesignFormatError) as caught:
        KicadCli(VersionRunner(_version_result("10.0.4")), _executable(tmp_path), 5).validate(
            project, tmp_path / "output"
        )
    assert caught.value.code == "KICAD_FILE_FORMAT_UNSUPPORTED"
```

Add an integration test in `tests/integration/test_validation.py` which queues
a validation with the same unsupported format and asserts the terminal task
error is `KICAD_FILE_FORMAT_UNSUPPORTED`, not `KICAD_TOOL_FAILED`.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py tests/integration/test_validation.py -q
```

Expected: FAIL because profiles do not build commands, file formats are not
checked, and the stable compatibility errors do not exist.

- [ ] **Step 3: Implement command construction and structured format checks**

Add `validation_argv()` to `KicadCompatibilityProfile`. It must reject a kind
not in `validation_operations`, map `erc` to `sch` and `drc` to `pcb`, and
return the exact tuple already used by `KicadCli`.

```python
def validation_argv(
    self,
    *,
    executable: Path,
    kind: Literal["erc", "drc"],
    design_file: Path,
    report_file: Path,
) -> tuple[str, ...]:
    if kind not in self.validation_operations:
        raise KicadOperationUnsupportedError(kind, self.profile_id)
    command_group = "sch" if kind == "erc" else "pcb"
    return (
        str(executable),
        command_group,
        kind,
        "--format",
        "json",
        "--output",
        str(report_file),
        str(design_file),
    )
```

Define `KicadOperationUnsupportedError` in
`pcbflow.kicad_compatibility` so the profile has no reverse dependency on
`kicad.py`; import and re-export it from `kicad.py` with the other public tool
errors.

In `kicad.py`, parse the top-level S-expression with `parse_cst()` and read the
single `(version <integer>)` child. Do not use regex. Validate `.kicad_sch` against
`profile.schematic_format_versions` and `.kicad_pcb` against
`profile.pcb_format_versions` before launching the process.

```python
class KicadDesignFormatError(ValueError):
    code = "KICAD_FILE_FORMAT_UNSUPPORTED"


class KicadOperationUnsupportedError(ValueError):
    code = "KICAD_OPERATION_UNSUPPORTED"
```

Extend `RawValidationReport` with:

```python
executable_digest: str
profile_id: str
profile_revision: int
```

Populate those fields from the one capability selected for the validation
attempt. `ValidationTaskHandler` catches both compatibility exceptions as
`TerminalTaskError(error.code, str(error))` and includes a stable `tool` object
in its returned task result:

```python
"tool": {
    "version": reports[0].tool_version,
    "executable_digest": reports[0].executable_digest,
    "profile_id": reports[0].profile_id,
    "profile_revision": reports[0].profile_revision,
}
```

Require all reports in one attempt to carry the same identity.

- [ ] **Step 4: Update test fakes to provide complete report identity**

Update every `RawValidationReport` constructor under `tests/` with the
same deterministic values used by its fake capability:

```python
RawValidationReport(
    "erc", report, ("kicad-cli", "sch", "erc"), 0, "10.0.4",
    "sha256:" + "a" * 64, "kicad-10-v1", 1,
)
```

Do not make production identity fields optional to preserve a false sense of
evidence completeness.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py tests/integration/test_validation.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit CLI compatibility behavior**

```powershell
git add src/pcbflow/kicad_compatibility.py src/pcbflow/kicad.py src/pcbflow/validation.py tests/unit/test_kicad_compatibility.py tests/unit/test_kicad.py tests/integration/test_validation.py
git commit -m "feat: enforce KiCad profile contracts"
```

---

### Task 3: Native Schematic Semantics Across Verified Formats

**Files:**
- Modify: `src/pcbflow/schematic/semantic.py:188-312,808-883`
- Modify: `src/pcbflow/schematic/adapter.py:105-214`
- Modify: `tests/unit/test_schematic_semantic.py`
- Modify: `tests/unit/test_schematic_adapter.py`
- Create: `tests/fixtures/kicad/real/10/compatible-legacy/up-down-c.kicad_sch`
- Create: `tests/fixtures/kicad/real/10/compatible-legacy/up-down-c.kicad_pro`
- Create: `tests/fixtures/kicad/real/10/controlled-write/up-down-c.kicad_sch`
- Create: `tests/fixtures/kicad/real/10/controlled-write/up-down-c.kicad_pro`

**Interfaces:**
- Extends: `inspect_schematic(project, *, accepted_versions: frozenset[int])`
- Extends: `parse_schematic(project, *, accepted_versions: frozenset[int])`
- Extends: `CstSchematicAdapter.inspect(project: Path, *, kicad_major: int) -> SchematicDocument`
- Extends: `CstSchematicAdapter.apply(project: Path, commands: tuple[DesignCommand, ...], *, kicad_major: int) -> ApplyResult`

- [ ] **Step 1: Add the frozen native fixture with provenance**

Copy the KiCad 10.0.4-distributed `simulation/up-down-counter` project unchanged
into `tests/fixtures/kicad/real/10/compatible-legacy/`. Copy the same two files
to `tests/fixtures/kicad/real/10/controlled-write/`, open that second copy with
KiCad 10.0.4 Eeschema, save it, and close it. Assert the saved copy identifies
the KiCad 10 generator before committing it. Commit only the `.kicad_sch` and
`.kicad_pro` inputs required by ERC. Tests must never read fixtures from the
installation directory at runtime.

The unchanged distributed schematic has format `20231120`; this is intentional
coverage for a real backward-compatible format accepted by the KiCad 10
profile. The saved copy is the native KiCad 10 controlled-write contract.

- [ ] **Step 2: Write failing semantic and controlled-edit tests**

```python
def test_inspects_native_kicad_10_distributed_schematic() -> None:
    project = (
        Path(__file__).resolve().parents[1]
        / "fixtures" / "kicad" / "real" / "10" / "compatible-legacy"
    )
    document = inspect_schematic(
        project, accepted_versions=frozenset((20231120, 20250114))
    )
    assert document.root_file == "up-down-c.kicad_sch"
    assert document.symbols
    assert all(symbol.pins for symbol in document.symbols)
```

Add an adapter test that inspects the native `controlled-write` fixture, chooses its first symbol,
executes a `schematic.set_property` command with `kicad_major=10`, reparses the
result, and asserts only the selected `Value` changed.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_semantic.py tests/unit/test_schematic_adapter.py -q
```

Expected: FAIL first on unsupported version `20231120`, then on missing native
library pin definitions after version plumbing is present.

- [ ] **Step 4: Implement accepted-version plumbing and native unit names**

Replace the exact `version != "20250114"` check with integer membership in the
caller-supplied frozen set. Keep the existing default `frozenset((20250114,))`
only on public helpers used by parser-only tests; proposal execution must pass
the selected profile's set explicitly.

Update `_library_symbol_unit()` so a nested native symbol can use either the
fully qualified `Library:Name_1_1` prefix used by existing goldens or KiCad's
native local `Name_1_1` prefix:

```python
prefixes = (lib_id, lib_id.rsplit(":", 1)[-1])
parts = name.rsplit("_", 2)
if len(parts) != 3 or parts[0] not in prefixes:
    return None
```

`CstSchematicAdapter.inspect()` and `.apply()` select the registered profile
from the required `kicad_major`, pass its schematic formats into semantic
parsing, and report that same major. Update existing adapter tests to pass
`kicad_major=9` explicitly.

- [ ] **Step 5: Run semantic, CST, adapter, and golden tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_cst.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_adapter.py tests/unit/test_schematic_goldens.py -q
```

Expected: PASS, including byte-preserving existing goldens and the native
KiCad-distributed fixture.

- [ ] **Step 6: Commit native semantic compatibility**

```powershell
git add src/pcbflow/schematic/semantic.py src/pcbflow/schematic/adapter.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_adapter.py tests/fixtures/kicad/real/10/compatible-legacy tests/fixtures/kicad/real/10/controlled-write
git commit -m "feat: parse verified KiCad schematic formats"
```

---

### Task 4: Module Compatibility And Profile-Bound Proposal Evidence

**Files:**
- Modify: `src/pcbflow/schematic/modules.py:39-91,180-216`
- Modify: `src/pcbflow/schematic/adapter.py:153-240`
- Modify: `src/pcbflow/proposals.py:37,438-525`
- Modify: `tests/fixtures/modules/status-led-v1/module.yaml`
- Modify: `tests/unit/test_schematic_modules.py`
- Modify: `tests/integration/test_proposals.py`
- Modify: `tests/e2e/test_controlled_design_change.py`

**Interfaces:**
- Preserves: wire manifest v1.0 `kicad_major: int`
- Produces: wire manifest v1.1 `kicad_majors: list[int]`
- Produces: normalized `ModuleManifest.kicad_majors: tuple[int, ...]`
- Consumes: `KicadCapability.major/profile_id/profile_revision`
- Produces: profile-bound `adapter_capability_report` evidence

- [ ] **Step 1: Write failing manifest and KiCad 10 proposal tests**

Add a module test that loads the fixture and asserts:

```python
assert revision.manifest.kicad_majors == (9, 10)
```

Add a copied legacy v1.0 manifest with `kicad_major: 9` and assert it normalizes
to `(9,)`. Mutate a v1.1 manifest to an unsorted or duplicated list and assert
`ModuleIntegrityError("invalid module manifest")`.

Add an integration proposal test using a KiCad 10 fake and assert the candidate
becomes `READY` and its `adapter_capability_report` contains:

```python
assert report["kicad_major"] == 10
assert report["kicad"] == {
    "available": True,
    "version": "10.0.4",
    "executable_digest": "sha256:" + "a" * 64,
    "reason": None,
    "profile_id": "kicad-10-v1",
    "profile_revision": 1,
}
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_modules.py tests/integration/test_proposals.py -q
```

Expected: FAIL because v1.1 multi-major manifests are unsupported and proposal
execution still requires major 9.

- [ ] **Step 3: Implement immutable multi-major module declarations**

Retain the strict v1.0 wire model and add a strict v1.1 wire model. Both
normalize into one immutable domain tuple:

```python
class _ManifestBase(_StrictManifest):
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    status: str = Field(pattern=r"^verified$")
    name: str = Field(min_length=1)
    adapter_contract: str = Field(pattern=r"^pcbflow\.schematic\.cst\.v1$")
    template: TemplateEntry
    uuid_bindings: dict[str, str]
    parameters: dict[str, ParameterEntry]
    ports: dict[str, str]
    footprints: dict[str, FootprintEntry]


class _ManifestModelV1(_ManifestBase):
    schema_version: Literal["1.0"]
    kicad_major: int = Field(ge=1)


class _ManifestModelV1_1(_ManifestBase):
    schema_version: Literal["1.1"]
    kicad_majors: list[int] = Field(min_length=1)

    @field_validator("kicad_majors")
    @classmethod
    def canonical_kicad_majors(cls, values: list[int]) -> list[int]:
        if values != sorted(set(values)) or any(value < 1 for value in values):
            raise ValueError("kicad_majors must be sorted unique positive integers")
        return values


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    schema_version: str
    module_revision_id: str
    status: str
    name: str
    kicad_majors: tuple[int, ...]
    adapter_contract: str
    template: TemplateEntry
    uuid_bindings: Mapping[str, str]
    parameters: Mapping[str, ParameterEntry]
    ports: Mapping[str, str]
    footprints: Mapping[str, FootprintEntry]
```

In `_read_manifest`, parse YAML to a mapping, dispatch only `"1.0"` to
`_ManifestModelV1` and `"1.1"` to `_ManifestModelV1_1`, and reject every other
value. In `_load_revision`, normalize the legacy integer to a one-item tuple
and the v1.1 list to a tuple. Keep the manifest digest bound to the original
versioned wire representation.

Update the fixture `module.yaml` to `schema_version: "1.1"` and
`kicad_majors: [9, 10]`. The manifest digest changes canonically; the template
byte digest does not.

Require `kicad_major in manifest.kicad_majors` before module instantiation and
emit the caller-selected major in `AdapterCapabilityReport`.

- [ ] **Step 4: Bind proposal execution and evidence to one profile**

Replace the hard-coded major-9 preflight with a requirement that the capability
is available and all profile fields are non-null. Pass `capability.major` to
both adapter inspection and application.

Add `profile_id` and `profile_revision` to `kicad_identity`. Assert every
`RawValidationReport` matches the probed version, executable digest, profile ID,
and profile revision before constructing evidence. Map
`KicadDesignFormatError` and `KicadOperationUnsupportedError` to their stable
terminal codes instead of the generic `CANDIDATE_VALIDATION_FAILED` path.

- [ ] **Step 5: Update all fakes and run focused workflows**

Update `FakeProposalKicad`, `FakeKicad9`, and other fake capabilities with
explicit profile fields. Keep both one KiCad 9 E2E workflow and the new KiCad
10 integration workflow.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_modules.py tests/integration/test_proposals.py tests/e2e/test_controlled_design_change.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit module and proposal compatibility**

```powershell
git add src/pcbflow/schematic/modules.py src/pcbflow/schematic/adapter.py src/pcbflow/proposals.py tests/fixtures/modules/status-led-v1/module.yaml tests/unit/test_schematic_modules.py tests/integration/test_proposals.py tests/e2e/test_controlled_design_change.py
git commit -m "feat: bind proposals to KiCad profiles"
```

---

### Task 5: Versioned Real-Tool Fixtures And Contracts

**Files:**
- Create: `tests/fixtures/kicad/real/README.md`
- Create: `tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_sch`
- Create: `tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_pcb`
- Create: `tests/fixtures/kicad/real/9/validation/EuroCard160mmX100mm.kicad_pro`
- Create: `tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_sch`
- Create: `tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_pcb`
- Create: `tests/fixtures/kicad/real/10/validation/EuroCard160mmX100mm.kicad_pro`
- Modify: `tests/contract/test_kicad_schematic_write.py`
- Modify: `tests/contract/test_kicad_cli.py`

**Interfaces:**
- Produces: one version-aware real ERC/DRC contract for the active supported CLI
- Produces: one KiCad 10 native controlled property-write/ERC contract

- [ ] **Step 1: Freeze official versioned validation fixtures**

Use the small official `EuroCard160mmX100mm` template whose file metadata records
KiCad 9 as the generator for the KiCad 9 directory. Run that contract when a
KiCad 9 executable is available; otherwise preserve the explicit contract skip.
For the KiCad 10 directory, open copies of all three project files with KiCad
10.0.4, save the schematic and board with their respective editors, then verify
both with the KiCad 10 CLI before committing. Record source release, original
relative path, resulting file SHA-256 values, generator metadata, and copy date
in `tests/fixtures/kicad/real/README.md`.

The repository copy is immutable test input; tests never depend on `Program
Files` or `AppData` fixture paths.

- [ ] **Step 2: Rewrite the real validation contract to select by profile**

Replace the KiCad-9-only skip with:

```python
executable = KicadCli.locate()
if executable is None:
    pytest.skip("kicad-cli is not installed")
kicad = KicadCli(ProcessRunner(2_000_000), executable, 120)
capability = kicad.probe()
if not capability.available or capability.major not in (9, 10):
    pytest.skip("a verified KiCad 9 or 10 CLI is not available")
project = tmp_path / f"KiCad {capability.major} validation"
shutil.copytree(
    fixtures / "kicad" / "real" / str(capability.major) / "validation",
    project,
)
reports = kicad.validate(project, tmp_path / "validation-output")
assert {report.kind for report in reports} == {"erc", "drc"}
assert all(report.profile_id == capability.profile_id for report in reports)
```

Parser goldens must no longer be looped through the real CLI contract.

- [ ] **Step 3: Add the KiCad 10 controlled-write contract**

Use the frozen `controlled-write` fixture from Task 3. Inspect it with
`kicad_major=10`, choose the first symbol, build a real
`schematic.set_property` command using that symbol's complete object reference,
apply it in the temporary copy, and run KiCad 10 ERC. Assert:

- semantic inspection observes the requested value change;
- the modified schematic still parses through the CST adapter;
- KiCad creates valid JSON and `parse_kicad_report("erc", data)` succeeds; and
- the command/result profile identity is `kicad-10-v1` revision 1.

When the discovered executable is KiCad 9, this KiCad-10-specific test skips;
on the current machine with KiCad 10.0.4 it must run and pass.

- [ ] **Step 4: Run real KiCad 10 contracts and verify GREEN**

Run:

```powershell
$env:PCBFLOW_KICAD_CLI = "C:\Users\1\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe"
.\.venv\Scripts\python.exe -m pytest -m kicad -v
```

Expected on the current machine: locator contract PASS, version-aware ERC/DRC
contract PASS, and KiCad 10 controlled-write/ERC contract PASS. No KiCad 10
test may skip because it is misidentified as an unsupported KiCad 9 contract.

- [ ] **Step 5: Commit frozen fixtures and contracts**

```powershell
git add tests/fixtures/kicad/real tests/contract/test_kicad_cli.py tests/contract/test_kicad_schematic_write.py
git commit -m "test: verify KiCad 9 and 10 contracts"
```

---

### Task 6: Locator, CLI Contract, Documentation, And Full Gate

**Files:**
- Modify: `src/pcbflow/kicad.py:153-173`
- Modify: `tests/unit/test_kicad.py`
- Modify: `tests/e2e/test_api_cli.py:220-245,309-320`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`

**Interfaces:**
- Extends: Windows locator to supported numeric KiCad install directories
- Extends: `pcbflow doctor --json` with profile identity
- Documents: compatibility matrix and future-major enablement checklist

- [ ] **Step 1: Write failing locator and doctor JSON tests**

Add a unit test that creates fake `9.0`, `10.0`, and `11.0` install roots,
monkeypatches discovery inputs, and asserts automatic discovery chooses the
highest registered profile (`10.0`) rather than lexicographic `9.0` or unknown
`11.0`. Keep an explicitly configured executable authoritative even when its
version later probes as unsupported.

Update the doctor E2E assertion to require exactly:

```python
{
    "available", "path", "version", "executable_digest", "reason",
    "major", "profile_id", "profile_revision",
}
```

- [ ] **Step 2: Run locator and doctor tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad.py tests/e2e/test_api_cli.py -q
```

Expected: FAIL because Windows directory ordering is string-based and doctor
does not yet assert the profile fields.

- [ ] **Step 3: Implement supported numeric Windows discovery**

Search both `%ProgramFiles%\KiCad\*\bin\kicad-cli.exe` and
`%LocalAppData%\Programs\KiCad\*\bin\kicad-cli.exe`. Parse the install
directory's numeric major/minor tuple, discard directory majors without a
registered profile, sort numerically descending, and return the first file.
Retain configured-path precedence and `PATH` behavior; if a `PATH` candidate's
directory exposes a numeric unknown major, continue to known install roots.

- [ ] **Step 4: Update compatibility documentation**

README compatibility matrix:

```text
KiCad major | Profile      | CLI validation | Controlled schematic writes
9.x         | kicad-9-v1   | Supported      | Supported
10.x        | kicad-10-v1  | Supported      | Supported
Other       | none          | Rejected       | Rejected
```

Replace all statements that KiCad 9 is the only supported release. Document
that `PCBFLOW_KICAD_CLI` selects one active executable and that doctor reports
its exact profile.

In `docs/DEVELOPMENT_GUIDE.md`, add the five-step future-major gate from the
approved design: explicit profile, native fixtures, real ERC/DRC and write
contracts, selection/boundary tests, then matrix update. Document the stable
format and operation error codes and the separation between parser goldens and
real CLI fixtures.

- [ ] **Step 5: Run the complete verification gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90 -q
$env:PCBFLOW_KICAD_CLI = "C:\Users\1\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe"
.\.venv\Scripts\python.exe -m pytest -m kicad -v
.\.venv\Scripts\python.exe -m pcbflow doctor --json
.\.venv\Scripts\python.exe -m pcbflow --help
git diff --check
```

Expected: all non-tool tests PASS; coverage is at least 90 percent; all KiCad
10 real contracts PASS on the current machine; doctor reports `10.0.4`, major
`10`, profile `kicad-10-v1`, revision `1`; CLI commands exit 0; and
`git diff --check` prints nothing.

- [ ] **Step 6: Commit locator and documentation**

```powershell
git add src/pcbflow/kicad.py tests/unit/test_kicad.py tests/e2e/test_api_cli.py README.md docs/DEVELOPMENT_GUIDE.md
git commit -m "docs: publish KiCad compatibility matrix"
```

---

## Final Review

- Confirm no production branch contains `major == 9` as the availability rule.
- Confirm every successful capability and raw report carries exact profile identity.
- Confirm unknown major versions remain unavailable.
- Confirm the real CLI contract never iterates parser-only handcrafted goldens.
- Confirm no test reads fixtures from the local KiCad installation at runtime.
- Confirm the pre-existing Phase 2A hardening diff was committed separately and not folded into these task commits.
