# Bound Module Instantiation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Allow a controlled schematic proposal to instantiate only the verified module frozen by a compmod_ binding, rejecting runtime KiCad-major or catalog-manifest drift before a candidate is written.

**Architecture:** A narrow resolver port sits between the schematic adapter and the component-binding service. The resolver reloads the immutable binding and live catalog module immediately before instantiation, then returns an in-memory ModuleRevision; the adapter renders that object without reopening the catalog. Proposal execution maps the three binding-specific resolution failures to terminal codes and persists safe, deterministic evidence.

**Tech Stack:** Python 3.12/3.13, Pydantic v2, SQLAlchemy/SQLite, pytest, KiCad CST adapter.

## Global Constraints

- Preserve schematic.instantiate_module payloads and behavior exactly; the new command is schematic.instantiate_bound_module only.
- The new payload accepts component_module_binding_id: compmod_... and never a module revision ID or digest.
- Resolve the binding against the active KiCad major immediately before write; compare the live manifest digest with the frozen digest and never expose catalog paths or template bytes in evidence.
- A resolver failure must occur before candidate commit and proposal-ref publication while preserving the standard failed evidence set.
- Run each pytest command with a new C:\tmp --basetemp directory and -p no:cacheprovider.
- Maintain deterministic output, canonical JSON, strict Pydantic models, and existing source-file line-ending conventions.

---

### Task 1: Define the Strict Bound Command Contract

**Files:**
- Modify: src/pcbflow/commands.py
- Test: tests/unit/test_commands.py

**Interfaces:**

~~~python
class InstantiateBoundModulePayload(StrictCommandModel):
    component_module_binding_id: str = Field(
        pattern=r"^compmod_[A-Za-z0-9_-]+$"
    )
    instance_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    target_sheet_ref: SchematicObjectRef
    parameter_bindings: dict[str, str]
    port_bindings: dict[str, SchematicObjectRef]
    placement_slot: str = Field(min_length=1)


class InstantiateBoundModuleOperation(StrictCommandModel):
    type: Literal["schematic.instantiate_bound_module"]
    payload: InstantiateBoundModulePayload
~~~

DesignOperation includes the new operation discriminator. Existing direct operation models remain unmodified.

- [ ] **Step 1: Write the failing schema tests**

Add this helper beside _batch() in tests/unit/test_commands.py:

~~~python
def _bound_operation() -> dict[str, object]:
    return {
        "type": "schematic.instantiate_bound_module",
        "payload": {
            "component_module_binding_id": "compmod_status_led_v1",
            "instance_name": "STATUS_LED",
            "target_sheet_ref": {
                "kind": "sheet",
                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                "object_uuid": "00000000-0000-0000-0000-000000000001",
                "pin_number": None,
            },
            "parameter_bindings": {"LED_VALUE": "GREEN"},
            "port_bindings": {},
            "placement_slot": "auto",
        },
    }
~~~

Add these tests:

~~~python
def test_batch_accepts_a_bound_module_operation() -> None:
    value = _batch()
    value["commands"][0]["operation"] = _bound_operation()  # type: ignore[index]
    operation = _load(value).commands[0].operation
    assert operation.type == "schematic.instantiate_bound_module"
    assert operation.payload.component_module_binding_id == "compmod_status_led_v1"


def test_bound_module_operation_rejects_direct_module_selection() -> None:
    value = _batch()
    operation = _bound_operation()
    operation["payload"]["module_revision_id"] = "modrev_status_led_v1"  # type: ignore[index]
    value["commands"][0]["operation"] = operation  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError):
        _load(value)
~~~

- [ ] **Step 2: Run the test to verify RED**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_commands.py -q --basetemp C:\tmp\pcbflow-bound-module-task-1-red -p no:cacheprovider
~~~

Expected: valid bound input fails because its operation discriminator is unknown.

- [ ] **Step 3: Write the minimal implementation**

Add the two models after InstantiateModuleOperation and extend the union exactly as follows:

~~~python
DesignOperation = Annotated[
    InstantiateModuleOperation
    | InstantiateBoundModuleOperation
    | SetPropertyOperation
    | AssignFootprintOperation
    | AddLabelOperation,
    Field(discriminator="type"),
]
~~~

Do not add module_revision_id or module_manifest_digest to the new payload.

- [ ] **Step 4: Run the test to verify GREEN**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_commands.py -q --basetemp C:\tmp\pcbflow-bound-module-task-1-green -p no:cacheprovider
~~~

Expected: all command schema tests pass, including direct module instantiation.

- [ ] **Step 5: Commit**

~~~powershell
git add src/pcbflow/commands.py tests/unit/test_commands.py
git commit -m "feat: add bound module command"
~~~

### Task 2: Resolve Immutable Bindings Against the Live Catalog

**Files:**
- Modify: src/pcbflow/component_binding_store.py
- Modify: src/pcbflow/component_bindings.py
- Test: tests/unit/test_component_bindings.py
- Test: tests/integration/test_component_module_bindings.py

**Interfaces:**

~~~python
@dataclass(frozen=True, slots=True)
class BoundModuleResolution:
    binding: ComponentModuleBinding
    module: ModuleRevision


class BoundModuleResolverPort(Protocol):
    def resolve_for_instantiation(
        self, binding_id: str, kicad_major: int
    ) -> BoundModuleResolution: ...


def ComponentModuleBindingStore.get(
    self, binding_id: str
) -> ComponentModuleBinding: ...
~~~

The three resolution errors expose .code and only public IDs, majors, and digests needed for evidence.

- [ ] **Step 1: Write the failing resolver and store tests**

Extend RecordingBindingStore with get() and an optional binding value. Add a helper returning a SimpleNamespace binding with id compmod_status_led_v1, component_revision_id comprev_fixture, kicad_major 10, module_revision_id modrev_status_led_v1, and frozen digest sha256: plus sixty-four a characters.

Add these tests:

~~~python
def test_resolve_for_instantiation_returns_the_live_verified_module() -> None:
    digest = "sha256:" + "a" * 64
    binding = _binding(module_manifest_digest=digest)
    service = ComponentModuleBindingService(
        FakeComponentStore(),
        RecordingBindingStore(binding),
        FakeCatalog(majors=(10,), digest=digest),
    )
    resolution = service.resolve_for_instantiation(binding.id, 10)
    assert resolution.binding == binding
    assert resolution.module.manifest_digest == digest


def test_resolve_for_instantiation_rejects_a_different_active_major() -> None:
    service = _resolver_service(_binding(kicad_major=10))
    with pytest.raises(ComponentModuleBindingKicadMajorMismatchError) as raised:
        service.resolve_for_instantiation("compmod_status_led_v1", 9)
    assert raised.value.code == "COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH"


def test_resolve_for_instantiation_rejects_live_digest_drift() -> None:
    service = _resolver_service(
        _binding(module_manifest_digest="sha256:" + "a" * 64),
        digest="sha256:" + "b" * 64,
    )
    with pytest.raises(ComponentModuleBindingDigestMismatchError) as raised:
        service.resolve_for_instantiation("compmod_status_led_v1", 10)
    assert raised.value.code == "COMPONENT_MODULE_BINDING_DIGEST_MISMATCH"
    assert raised.value.observed_manifest_digest == "sha256:" + "b" * 64
~~~

Add a store test proving store.get(created.id) equals created and store.get("compmod_missing") raises ComponentModuleBindingNotFoundError with code COMPONENT_MODULE_BINDING_NOT_FOUND.

- [ ] **Step 2: Run the tests to verify RED**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_component_bindings.py tests\integration\test_component_module_bindings.py -q --basetemp C:\tmp\pcbflow-bound-module-task-2-red -p no:cacheprovider
~~~

Expected: missing get() and resolve_for_instantiation() cause the new tests to fail.

- [ ] **Step 3: Write the minimal resolver**

Add ComponentModuleBindingStore.get() as a non-writing select(ComponentModuleBindingRow).where(ComponentModuleBindingRow.id == binding_id) query. Raise ComponentModuleBindingNotFoundError(binding_id) when there is no row.

In component_bindings.py define the frozen BoundModuleResolution, the resolver port, and typed errors with these codes:

~~~python
COMPONENT_MODULE_BINDING_NOT_FOUND
COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH
COMPONENT_MODULE_BINDING_DIGEST_MISMATCH
~~~

Add the ordered service method:

~~~python
def resolve_for_instantiation(
    self, binding_id: str, kicad_major: int
) -> BoundModuleResolution:
    binding = self._binding_store.get(binding_id)
    if binding.kicad_major != kicad_major:
        raise ComponentModuleBindingKicadMajorMismatchError(
            binding.id, binding.kicad_major, kicad_major
        )
    if self._module_catalog is None:
        raise ModuleCatalogUnavailableError("module catalog is not configured")
    try:
        module = self._module_catalog.get(binding.module_revision_id)
    except ModuleIntegrityError as error:
        raise ModuleCatalogUnavailableError("module catalog is unavailable") from error
    if module.manifest.status != "verified":
        raise ModuleCatalogUnavailableError("module revision is not verified")
    if (
        module.manifest.module_revision_id != binding.module_revision_id
        or module.manifest_digest != binding.module_manifest_digest
    ):
        raise ComponentModuleBindingDigestMismatchError(
            binding.id,
            binding.module_revision_id,
            binding.module_manifest_digest,
            module.manifest_digest,
        )
    if kicad_major not in module.manifest.kicad_majors:
        raise ModuleKicadMajorUnsupportedError(binding.module_revision_id, kicad_major)
    return BoundModuleResolution(binding=binding, module=module)
~~~

The mismatch error message must not include a filesystem path or template bytes.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_component_bindings.py tests\integration\test_component_module_bindings.py tests\unit\test_schematic_modules.py -q --basetemp C:\tmp\pcbflow-bound-module-task-2-green -p no:cacheprovider
~~~

Expected: binding lookup, major mismatch, catalog integrity, missing module, unsupported major, and digest drift contracts pass.

- [ ] **Step 5: Commit**

~~~powershell
git add src/pcbflow/component_binding_store.py src/pcbflow/component_bindings.py tests/unit/test_component_bindings.py tests/integration/test_component_module_bindings.py
git commit -m "feat: resolve bound modules"
~~~

### Task 3: Render the Resolver-Verified In-Memory Module

**Files:**
- Modify: src/pcbflow/schematic/adapter.py
- Test: tests/unit/test_schematic_adapter.py

**Interfaces:**

~~~python
@dataclass(frozen=True, slots=True)
class BoundModuleResolutionReport:
    binding_id: str
    component_revision_id: str
    kicad_major: int
    module_revision_id: str
    frozen_manifest_digest: str
    live_manifest_digest: str


class AdapterCapabilityReport:
    adapter_contract: str
    kicad_major: int
    supported_operations: tuple[str, ...]
    module_digests: tuple[str, ...]
    bound_module_resolutions: tuple[BoundModuleResolutionReport, ...]
~~~

CstSchematicAdapter receives an optional BoundModuleResolverPort. A bound operation resolves once and passes resolution.module to _instantiate() without a later catalog call.

- [ ] **Step 1: Write failing adapter tests**

In tests/unit/test_schematic_adapter.py, add a resolver double recording its input and returning a fixture ModuleRevision plus ComponentModuleBinding. Add:

~~~python
def test_adapter_instantiates_a_bound_module_from_the_resolver(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    resolver = RecordingBoundModuleResolver(_fixture_resolution())
    command = _command(_bound_operation(), "cmd_bound_status_led")
    result = CstSchematicAdapter(
        None, bound_module_resolver=resolver
    ).apply(project, (command,), kicad_major=9)
    assert resolver.calls == [("compmod_status_led_v1", 9)]
    assert result.command_results[0].operation_type == "schematic.instantiate_bound_module"
    assert result.capability_report.bound_module_resolutions[0].binding_id == "compmod_status_led_v1"
~~~

Add a direct module regression asserting direct result.capability_report.bound_module_resolutions equals ().

- [ ] **Step 2: Run the test to verify RED**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_schematic_adapter.py -q --basetemp C:\tmp\pcbflow-bound-module-task-3-red -p no:cacheprovider
~~~

Expected: adapter construction or bound operation dispatch fails because the resolver route is absent.

- [ ] **Step 3: Write the minimal adapter integration**

Import InstantiateBoundModuleOperation and the resolver port. Add the keyword-only constructor parameter:

~~~python
def __init__(
    self,
    module_catalog: ModuleCatalogPort | None,
    *,
    bound_module_resolver: BoundModuleResolverPort | None = None,
    metrics: Metrics | None = None,
    monotonic=time.monotonic,
) -> None:
    self._module_catalog = module_catalog
    self._bound_module_resolver = bound_module_resolver
~~~

Treat a single direct or bound instantiate operation as the existing isolated-instantiation path. Direct behavior continues to use self._module_catalog.get(payload.module_revision_id). Bound behavior calls:

~~~python
resolution = self._bound_module_resolver.resolve_for_instantiation(
    payload.component_module_binding_id, kicad_major
)
revision = resolution.module
~~~

Make _instantiate() accept the union of direct and bound instantiate operations. Populate one ordered BoundModuleResolutionReport only on the bound route; use () for direct and controlled non-bound reports.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\unit\test_schematic_adapter.py tests\unit\test_schematic_modules.py -q --basetemp C:\tmp\pcbflow-bound-module-task-3-green -p no:cacheprovider
~~~

Expected: resolver is called once before write, bound evidence has no local path, and existing direct module rendering remains unchanged.

- [ ] **Step 5: Commit**

~~~powershell
git add src/pcbflow/schematic/adapter.py tests/unit/test_schematic_adapter.py
git commit -m "feat: instantiate resolved bound modules"
~~~

### Task 4: Wire Proposal Evidence and Terminal Resolution Codes

**Files:**
- Modify: src/pcbflow/container.py
- Modify: src/pcbflow/proposals.py
- Test: tests/integration/test_proposals.py
- Test: tests/e2e/test_api_cli.py
- Test: tests/e2e/test_controlled_design_change.py

**Interfaces:** The proposal terminal codes are COMPONENT_MODULE_BINDING_NOT_FOUND, COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH, and COMPONENT_MODULE_BINDING_DIGEST_MISMATCH. Failed command_execution_log data uses stage: bound_module_resolution plus only public binding, module, and digest fields.

- [ ] **Step 1: Write failing proposal tests**

Add _bound_instantiate_batch(project, requirement_set, binding_id) beside _instantiate_batch() in tests/integration/test_proposals.py. It retains the normal envelope but uses the bound operation.

Add a successful scenario that imports a component, binds modrev_status_led_v1 at major 9, runs the proposal, reads adapter_capability_report, and asserts:

~~~python
assert report["bound_module_resolutions"] == [{
    "binding_id": binding.id,
    "component_revision_id": binding.component_revision_id,
    "kicad_major": 9,
    "module_revision_id": binding.module_revision_id,
    "frozen_manifest_digest": binding.module_manifest_digest,
    "live_manifest_digest": binding.module_manifest_digest,
}]
~~~

Add a drift scenario that copies tests/fixtures/modules to tmp_path, creates the binding, changes only name: Status LED to name: Drifted Status LED in module.yaml, and then asserts:

~~~python
assert failed.last_error_code == "COMPONENT_MODULE_BINDING_DIGEST_MISMATCH"
assert failed.candidate_revision is None
assert _snapshot(project.source_path) == source_before
assert container.revisions.resolve_proposal_ref(project.id, proposal.id) is None
assert command_log["stage"] == "bound_module_resolution"
assert command_log["binding_id"] == binding.id
assert str(catalog_root) not in json.dumps(command_log)
~~~

In tests/e2e/test_api_cli.py, use the existing proposal API and CLI helpers to submit a valid bound batch after creating a component binding. Assert API and CLI creation both return proposal JSON. Submit a bound payload that also contains module_revision_id and assert the established DESIGN_COMMAND_SCHEMA_INVALID response.

In tests/e2e/test_controlled_design_change.py, add a managed workflow that imports a component, binds the fixture module for FakeKicad9, submits a bound proposal through create_app(container), runs one worker task, and asserts ready_for_review, candidate_revision, and adapter report bound_module_resolutions[0]["binding_id"] equals the stored ID.

- [ ] **Step 2: Run tests to verify RED**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\integration\test_proposals.py tests\e2e\test_api_cli.py tests\e2e\test_controlled_design_change.py -q --basetemp C:\tmp\pcbflow-bound-module-task-4-red -p no:cacheprovider
~~~

Expected: proposal currently rejects the operation or has no resolver report and terminal drift code.

- [ ] **Step 3: Write the minimal proposal integration**

In build_container(), inject the existing binding service into the existing adapter:

~~~python
adapter = CstSchematicAdapter(
    module_catalog,
    bound_module_resolver=component_module_bindings,
    metrics=metric_sink,
    monotonic=monotonic,
)
~~~

Initialize the preliminary capability_report with bound_module_resolutions: []. After adapter.apply(), serialize the new report field:

~~~python
"bound_module_resolutions": [
    {
        "binding_id": resolution.binding_id,
        "component_revision_id": resolution.component_revision_id,
        "kicad_major": resolution.kicad_major,
        "module_revision_id": resolution.module_revision_id,
        "frozen_manifest_digest": resolution.frozen_manifest_digest,
        "live_manifest_digest": resolution.live_manifest_digest,
    }
    for resolution in applied.capability_report.bound_module_resolutions
],
~~~

Catch the three typed binding errors before the generic exception clause. Replace command_execution_data with canonical JSON containing stage, binding_id, and any error attributes module_revision_id, frozen_manifest_digest, and observed_manifest_digest. Call add_failed_evidence_set(), mark_validation_failed() with error.code, then raise TerminalTaskError(error.code, str(error)). Do not add catalog paths to this record.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\integration\test_proposals.py tests\integration\test_component_module_bindings.py tests\e2e\test_api_cli.py tests\e2e\test_controlled_design_change.py -q --basetemp C:\tmp\pcbflow-bound-module-task-4-green -p no:cacheprovider
~~~

Expected: success stores the bound-resolution report; drift fails before candidate publication with safe failed evidence.

- [ ] **Step 5: Commit**

~~~powershell
git add src/pcbflow/container.py src/pcbflow/proposals.py tests/integration/test_proposals.py tests/e2e/test_api_cli.py tests/e2e/test_controlled_design_change.py
git commit -m "feat: evidence bound module resolution"
~~~

### Task 5: Document the Public Contract and Complete the Quality Gate

**Files:**
- Modify: README.md

**Interfaces:** Existing proposal REST and CLI surfaces accept the strict command batch. Normal proposal status exposes the terminal code produced by the worker.

- [ ] **Step 1: Write the required public documentation**

Update the Phase 2A supported-operation list in README.md to include schematic.instantiate_bound_module. Add a short batch example using component_module_binding_id. State that the worker reloads the binding and configured catalog at execution time, rejects KiCad-major or manifest-digest drift, and creates no candidate on verification failure. Do not document a new endpoint or CLI command because proposal submission uses existing interfaces.

- [ ] **Step 2: Run public-contract regressions**

Run:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest tests\e2e\test_api_cli.py tests\e2e\test_controlled_design_change.py tests\unit\test_commands.py -q --basetemp C:\tmp\pcbflow-bound-module-task-5-green -p no:cacheprovider
~~~

Expected: REST, CLI, schema, controlled workflow, and direct-instantiation regressions pass.

- [ ] **Step 3: Commit**

~~~powershell
git add README.md
git commit -m "docs: document bound module proposals"
~~~

### Task 6: Perform Complete Verification and Self-Review

**Files:**
- Review: docs/superpowers/specs/2026-08-02-bound-module-instantiation-design.md
- Review: docs/superpowers/plans/2026-08-02-bound-module-instantiation.md
- Review: git diff main...HEAD

- [ ] **Step 1: Run the complete regression suite**

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest -q --basetemp C:\tmp\pcbflow-bound-module-full -p no:cacheprovider
~~~

Expected: all tests pass with zero failures.

- [ ] **Step 2: Run the coverage gate**

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90 --basetemp C:\tmp\pcbflow-bound-module-coverage -p no:cacheprovider
~~~

Expected: all tests pass and total coverage is at least 90%.

- [ ] **Step 3: Compile and check Git whitespace**

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'C:\GitHub\codex自动化开发pcb\.venv\Scripts\python.exe' -m compileall -q src
git diff --check main...HEAD
git diff --cached --check
~~~

Expected: compilation prints no errors and both whitespace checks are clean.

- [ ] **Step 4: Self-review the completed diff**

Verify each design acceptance criterion explicitly: strict command shape, frozen-module-only behavior, major/digest failure semantics, no candidate/ref on drift, direct compatibility, safe evidence, unit/integration/E2E coverage, and all quality gates. Correct every discrepancy with a new RED/GREEN test cycle before claiming completion.

- [ ] **Step 5: Commit the reviewed plan status**

~~~powershell
git add docs/superpowers/plans/2026-08-02-bound-module-instantiation.md
git commit -m "docs: record bound module implementation plan"
~~~
