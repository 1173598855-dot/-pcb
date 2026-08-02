# Component Revision Module Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist and expose immutable bindings from verified component revisions to one verified module revision for each KiCad major.

**Architecture:** Add a SQLite-backed `ComponentModuleBinding` record that freezes a module revision ID and canonical module manifest digest without storing the file-system module catalog in the database. A service verifies the component and module catalog before delegating create/replay/conflict semantics to a dedicated store; container, REST, and CLI adapters remain thin.

**Tech Stack:** Python 3.12/3.13, SQLAlchemy 2.x, Alembic, FastAPI, Typer, pytest, SQLite WAL.

## Global Constraints

- Do not mutate `ComponentRevision`, module manifests, or existing module catalog files.
- Permit exactly one binding per `(component_revision_id, kicad_major)`.
- Freeze `module_revision_id` and `ModuleRevision.manifest_digest` at bind time.
- Resolve the live module catalog only during create; list operations must read only SQLite.
- Require the module manifest to be verified and to declare the requested KiCad major.
- Reuse `IdempotencyConflictError` for a reused key with different input; use a distinct conflict for an occupied component-and-major slot with different frozen module data.
- Preserve the existing local-source restriction for component import, but permit authenticated remote binding because the request contains no client local path.
- Use `BEGIN IMMEDIATE` before binding-row reads that decide a write.
- Test with a fresh `--basetemp` below `C:\tmp` and `-p no:cacheprovider`; the repository's legacy pytest cache directories have inaccessible Windows ACLs.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/pcbflow/component_bindings.py` | Immutable binding domain record, catalog validation service, and catalog/major errors. |
| `src/pcbflow/component_binding_store.py` | SQLite create, replay, conflict, list, and concurrent-insert recovery. |
| `src/pcbflow/tables.py` | SQLAlchemy `ComponentModuleBindingRow` declaration and named constraints/index. |
| `alembic/versions/0005_component_module_bindings.py` | Forward and reverse schema migration from task retry head. |
| `src/pcbflow/container.py` | Store/service construction and `Container` exposure. |
| `src/pcbflow/api.py` | Strict request model, routes, and stable HTTP error mapping. |
| `src/pcbflow/cli.py` | `component bind-module` and `component bindings` commands and error mapping. |
| `tests/integration/test_component_module_bindings.py` | Store, service, digest-freeze, replay, conflict, and write-reservation regressions. |
| `tests/unit/test_component_bindings.py` | Isolated catalog validation and service boundary tests. |
| `tests/integration/test_migrations.py` | Migration-head, table, column, index, and unique-constraint assertions. |
| `tests/e2e/test_component_api_cli.py` | REST/CLI happy paths, replay, strict input, remote mode, and stable errors. |
| `README.md` | Public API and CLI reference for module bindings. |

### Task 1: Persist Immutable Binding Records

**Files:**
- Create: `src/pcbflow/component_bindings.py`
- Create: `src/pcbflow/component_binding_store.py`
- Create: `alembic/versions/0005_component_module_bindings.py`
- Modify: `src/pcbflow/tables.py`
- Create: `tests/integration/test_component_module_bindings.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ComponentModuleBinding:
    id: str
    component_revision_id: str
    kicad_major: int
    module_revision_id: str
    module_manifest_digest: str
    idempotency_key: str
    created_at: datetime


class ComponentModuleBindingConflictError(RuntimeError):
    pass


class ComponentModuleBindingStore:
    def create(
        self,
        *,
        component_revision_id: str,
        kicad_major: int,
        module_revision_id: str,
        module_manifest_digest: str,
        idempotency_key: str,
    ) -> ComponentModuleBinding: ...

    def list_for_component_revision(
        self, component_revision_id: str
    ) -> tuple[ComponentModuleBinding, ...]: ...

    def find_by_idempotency_key(
        self, idempotency_key: str
    ) -> ComponentModuleBinding | None: ...
```

The model table is named `component_module_bindings`. It has a foreign key to
`component_revisions.id`, named uniqueness constraints
`uq_component_module_binding_component_major` and
`uq_component_module_binding_idempotency`, plus index
`ix_component_module_bindings_component_created` over
`component_revision_id`, `created_at`, `id`.

- [ ] **Step 1: Write failing persistence and migration tests**

Create `tests/integration/test_component_module_bindings.py`. Import a fixture
component through `container.components.import_revision()` and exercise a
`ComponentModuleBindingStore` against that revision ID. Start with these
tests, using a fixed digest only for store-level tests:

```python
def test_store_creates_replays_and_lists_bindings(container) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    created = store.create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-1",
    )
    replayed = store.create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-1",
    )

    assert replayed == created
    assert store.list_for_component_revision(component.id) == (created,)


def test_store_rejects_conflicting_slot_and_idempotency_key(container) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    store.create(
        component_revision_id=component.id,
        kicad_major=9,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-slot-1",
    )

    with pytest.raises(ComponentModuleBindingConflictError):
        store.create(
            component_revision_id=component.id,
            kicad_major=9,
            module_revision_id="modrev_other_v1",
            module_manifest_digest="sha256:" + "b" * 64,
            idempotency_key="component-module-slot-2",
        )
    with pytest.raises(IdempotencyConflictError):
        store.create(
            component_revision_id=component.id,
            kicad_major=10,
            module_revision_id="modrev_other_v1",
            module_manifest_digest="sha256:" + "b" * 64,
            idempotency_key="component-module-slot-1",
        )
```

Add a write-reservation test modeled on
`test_component_revision_store_reserves_write_path_before_catalog_reads`: wrap
`Session.execute` and `Session.scalar`, create one binding, and assert that
`BEGIN IMMEDIATE` is recorded before the first select from
`component_module_bindings`.

Extend `tests/integration/test_migrations.py` so the expected Alembic head is
`0005_component_module_bindings`, the initial table set contains
`component_module_bindings`, and reflection asserts all seven fields, the
component/major unique constraint, idempotency uniqueness, and the list index.

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_component_module_bindings.py tests\integration\test_migrations.py -q --basetemp C:\tmp\pcbflow-component-binding-task-1 -p no:cacheprovider
```

Expected: collection fails because `ComponentModuleBindingStore`,
`ComponentModuleBindingConflictError`, and migration `0005` do not exist.

- [ ] **Step 3: Add the model, migration, domain record, and store**

Append this SQLAlchemy row after `ComponentRevisionRow` in
`src/pcbflow/tables.py`:

```python
class ComponentModuleBindingRow(Base):
    __tablename__ = "component_module_bindings"
    __table_args__ = (
        UniqueConstraint(
            "component_revision_id",
            "kicad_major",
            name="uq_component_module_binding_component_major",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_component_module_binding_idempotency",
        ),
        Index(
            "ix_component_module_bindings_component_created",
            "component_revision_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    component_revision_id: Mapped[str] = mapped_column(
        ForeignKey("component_revisions.id"), nullable=False
    )
    kicad_major: Mapped[int] = mapped_column(Integer, nullable=False)
    module_revision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    module_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Create `0005_component_module_bindings.py` with
`down_revision = "0004_task_retry_schedule"`. Its `upgrade()` creates the
table with the foreign key and both named unique constraints, then creates the
list index. `downgrade()` drops the index before dropping the table.

In `component_bindings.py`, define the immutable dataclass above. In
`component_binding_store.py`:

```python
def _matches(
    row: ComponentModuleBindingRow,
    *,
    component_revision_id: str,
    kicad_major: int,
    module_revision_id: str,
    module_manifest_digest: str,
) -> bool:
    return (
        row.component_revision_id == component_revision_id
        and row.kicad_major == kicad_major
        and row.module_revision_id == module_revision_id
        and row.module_manifest_digest == module_manifest_digest
    )
```

Implement `create()` with this order inside `with self._sessions.begin()`:

```python
session.execute(text("BEGIN IMMEDIATE"))
keyed = session.scalar(
    select(ComponentModuleBindingRow).where(
        ComponentModuleBindingRow.idempotency_key == idempotency_key
    )
)
if keyed is not None:
    if not _matches(
        keyed,
        component_revision_id=component_revision_id,
        kicad_major=kicad_major,
        module_revision_id=module_revision_id,
        module_manifest_digest=module_manifest_digest,
    ):
        raise IdempotencyConflictError(idempotency_key)
    return _binding(keyed)

slot = session.scalar(
    select(ComponentModuleBindingRow).where(
        ComponentModuleBindingRow.component_revision_id == component_revision_id,
        ComponentModuleBindingRow.kicad_major == kicad_major,
    )
)
if slot is not None:
    if not _matches(
        slot,
        component_revision_id=component_revision_id,
        kicad_major=kicad_major,
        module_revision_id=module_revision_id,
        module_manifest_digest=module_manifest_digest,
    ):
        raise ComponentModuleBindingConflictError(component_revision_id, kicad_major)
    return _binding(slot)
```

Validate a nonempty idempotency key and a positive major before opening the
transaction. Insert `new_id("compmod")`, flush, and return `_binding(row)`.
On `IntegrityError`, open a new read session and perform this exact recovery:

```python
keyed = session.scalar(
    select(ComponentModuleBindingRow).where(
        ComponentModuleBindingRow.idempotency_key == idempotency_key
    )
)
if keyed is not None:
    if _matches(
        keyed,
        component_revision_id=component_revision_id,
        kicad_major=kicad_major,
        module_revision_id=module_revision_id,
        module_manifest_digest=module_manifest_digest,
    ):
        return _binding(keyed)
    raise IdempotencyConflictError(idempotency_key)
slot = session.scalar(
    select(ComponentModuleBindingRow).where(
        ComponentModuleBindingRow.component_revision_id == component_revision_id,
        ComponentModuleBindingRow.kicad_major == kicad_major,
    )
)
if slot is not None:
    if _matches(
        slot,
        component_revision_id=component_revision_id,
        kicad_major=kicad_major,
        module_revision_id=module_revision_id,
        module_manifest_digest=module_manifest_digest,
    ):
        return _binding(slot)
    raise ComponentModuleBindingConflictError(component_revision_id, kicad_major)
raise
```

Implement `find_by_idempotency_key()` with one `session.scalar()` query and
return `_binding(row)` when present. `list_for_component_revision()` orders by
`created_at`, then `id`.

- [ ] **Step 4: Run persistence tests to verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_component_module_bindings.py tests\integration\test_migrations.py -q --basetemp C:\tmp\pcbflow-component-binding-task-1 -p no:cacheprovider
```

Expected: all new persistence and migration tests pass.

- [ ] **Step 5: Commit the persistence foundation**

```powershell
git add src/pcbflow/component_bindings.py src/pcbflow/component_binding_store.py src/pcbflow/tables.py alembic/versions/0005_component_module_bindings.py tests/integration/test_component_module_bindings.py tests/integration/test_migrations.py
git commit -m "feat: persist component module bindings"
```

### Task 2: Verify Catalog Bindings and Wire the Container

**Files:**
- Modify: `src/pcbflow/component_bindings.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_component_module_bindings.py`
- Create: `tests/unit/test_component_bindings.py`

**Interfaces:**

```python
class ModuleCatalogUnavailableError(RuntimeError):
    pass


class ModuleKicadMajorUnsupportedError(ValueError):
    def __init__(self, module_revision_id: str, kicad_major: int) -> None: ...


class ComponentModuleBindingService:
    def __init__(
        self,
        component_store: ComponentRevisionStore,
        binding_store: ComponentModuleBindingStore,
        module_catalog: ModuleCatalogPort | None,
    ) -> None: ...

    def bind(
        self,
        component_revision_id: str,
        kicad_major: int,
        module_revision_id: str,
        idempotency_key: str,
    ) -> ComponentModuleBinding: ...

    def list_for_component_revision(
        self, component_revision_id: str
    ) -> tuple[ComponentModuleBinding, ...]: ...
```

`Container` exposes `component_module_binding_store` and
`component_module_bindings`. The service receives the already-configured
`module_catalog` local in `build_container`; it must not create another file
catalog or persist its path.

- [ ] **Step 1: Write failing service and container tests**

In `tests/unit/test_component_bindings.py`, use these explicit, mutable test
doubles instead of mutating a frozen `ModuleManifest`:

```python
from types import SimpleNamespace


class FakeComponentStore:
    def get(self, component_revision_id: str) -> object:
        return SimpleNamespace(id=component_revision_id, status="verified")


class RecordingBindingStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(**kwargs)


class FakeCatalog:
    def __init__(self, *, majors: tuple[int, ...], digest: str) -> None:
        self._revision = SimpleNamespace(
            manifest=SimpleNamespace(
                status="verified",
                kicad_majors=majors,
                module_revision_id="modrev_status_led_v1",
            ),
            manifest_digest=digest,
        )

    def get(self, module_revision_id: str) -> object:
        assert module_revision_id == "modrev_status_led_v1"
        return self._revision
```

Write the failure cases and the frozen-digest forwarding assertion exactly as
follows:

```python
def test_bind_rejects_missing_catalog() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(), RecordingBindingStore(), None
    )

    with pytest.raises(ModuleCatalogUnavailableError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "missing-catalog")


def test_bind_requires_the_module_to_support_the_requested_major() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(),
        RecordingBindingStore(),
        FakeCatalog(majors=(9,), digest="sha256:" + "a" * 64),
    )

    with pytest.raises(ModuleKicadMajorUnsupportedError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "bad-major")


def test_bind_forwards_the_catalog_manifest_digest() -> None:
    bindings = RecordingBindingStore()
    digest = "sha256:" + "b" * 64
    service = ComponentModuleBindingService(
        FakeComponentStore(), bindings, FakeCatalog(majors=(9, 10), digest=digest)
    )

    service.bind("comprev_fixture", 10, "modrev_status_led_v1", "freeze-digest")

    assert bindings.calls == [{
        "component_revision_id": "comprev_fixture",
        "kicad_major": 10,
        "module_revision_id": "modrev_status_led_v1",
        "module_manifest_digest": digest,
        "idempotency_key": "freeze-digest",
    }]
```

Add a catalog-integrity test whose `get()` raises `ModuleIntegrityError` and
asserts `ModuleCatalogUnavailableError`, plus a catalog-missing test whose
`get()` raises `ModuleRevisionNotFoundError` and asserts that same exception
propagates.

Add integration coverage that builds a container with
`PCBFLOW_MODULE_CATALOG_DIR` set to `tests/fixtures/modules`, imports the
fixture component, and binds `modrev_status_led_v1` for majors 9 and 10. Assert
each record contains the digest returned by an independently constructed
`FileModuleCatalog`. For digest-freeze conflict coverage, construct a service
with a catalog double that returns this sequence on two calls:

```python
SimpleNamespace(
    manifest=SimpleNamespace(
        status="verified",
        kicad_majors=(10,),
        module_revision_id="modrev_status_led_v1",
    ),
    manifest_digest="sha256:" + "a" * 64,
)
SimpleNamespace(
    manifest=SimpleNamespace(
        status="verified",
        kicad_majors=(10,),
        module_revision_id="modrev_status_led_v1",
    ),
    manifest_digest="sha256:" + "b" * 64,
)
```

Bind the first result to one component/major slot, then bind the second result
to the same slot under a new idempotency key and assert
`ComponentModuleBindingConflictError`.

- [ ] **Step 2: Run service tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_component_bindings.py tests\integration\test_component_module_bindings.py -q --basetemp C:\tmp\pcbflow-component-binding-task-2 -p no:cacheprovider
```

Expected: import fails because the service, catalog-unavailable error, and
unsupported-major error do not exist.

- [ ] **Step 3: Implement catalog verification and dependency injection**

Add the service to `component_bindings.py`. Its `bind()` must execute this
sequence without catching the existing component not-found error:

```python
self._component_store.get(component_revision_id)
if kicad_major <= 0:
    raise ValueError("KiCad major must be positive")
if self._module_catalog is None:
    raise ModuleCatalogUnavailableError("module catalog is not configured")
try:
    module = self._module_catalog.get(module_revision_id)
except ModuleIntegrityError as error:
    raise ModuleCatalogUnavailableError("module catalog is unavailable") from error
if module.manifest.status != "verified":
    raise ModuleCatalogUnavailableError("module revision is not verified")
if kicad_major not in module.manifest.kicad_majors:
    raise ModuleKicadMajorUnsupportedError(module_revision_id, kicad_major)
return self._binding_store.create(
    component_revision_id=component_revision_id,
    kicad_major=kicad_major,
    module_revision_id=module.manifest.module_revision_id,
    module_manifest_digest=module.manifest_digest,
    idempotency_key=idempotency_key,
)
```

Do not catch `ModuleRevisionNotFoundError`; adapters map it to the stable
not-found contract. `list_for_component_revision()` must first call
`component_store.get()` so a list for an absent component returns the existing
component-not-found error instead of an empty list.

In `container.py`, create the binding store immediately after
`component_store`, create the service after `module_catalog` has been selected,
add both fields to the frozen `Container`, and pass them in the final
`Container(...)` construction. Keep `FileModuleCatalog` construction exactly
where it is so proposal execution and binding use the same configured catalog.

- [ ] **Step 4: Run service and existing catalog tests to verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_component_bindings.py tests\integration\test_component_module_bindings.py tests\unit\test_schematic_modules.py tests\integration\test_component_revisions.py -q --basetemp C:\tmp\pcbflow-component-binding-task-2 -p no:cacheprovider
```

Expected: all binding tests pass without regressing module catalog or component
revision behavior.

- [ ] **Step 5: Commit service wiring**

```powershell
git add src/pcbflow/component_bindings.py src/pcbflow/container.py tests/unit/test_component_bindings.py tests/integration/test_component_module_bindings.py
git commit -m "feat: verify component module bindings"
```

### Task 3: Expose Binding Operations Through REST and CLI

**Files:**
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/e2e/test_component_api_cli.py`

**Interfaces:**

```text
POST /api/v1/component-revisions/{component_revision_id}/module-bindings
GET  /api/v1/component-revisions/{component_revision_id}/module-bindings

pcbflow component bind-module <component_revision_id> --kicad-major <n> \
  --module-revision-id <id> --idempotency-key <key> --json
pcbflow component bindings <component_revision_id> --json
```

The strict request model is:

```python
class CreateComponentModuleBindingRequest(StrictRequest):
    kicad_major: int = Field(ge=1)
    module_revision_id: str = Field(min_length=1)
```

- [ ] **Step 1: Write failing API and CLI contract tests**

Extend `_settings()` in `tests/e2e/test_component_api_cli.py` with an optional
`module_catalog_dir: Path | None` parameter that adds
`PCBFLOW_MODULE_CATALOG_DIR` to the environment. Point it to
`tests/fixtures/modules` for successful binding tests.

Add an API test that imports a component, creates a major-10 binding with an
`Idempotency-Key`, checks 201 and these response fields, replays for 200, and
lists the same frozen response:

```python
assert {
    "id",
    "component_revision_id",
    "kicad_major",
    "module_revision_id",
    "module_manifest_digest",
    "idempotency_key",
    "created_at",
} <= created.json().keys()
assert replayed.json() == created.json()
assert listed.json() == [created.json()]
```

In the same test file, cover these concrete cases:

- `POST` with an unknown component returns 404
  `COMPONENT_REVISION_NOT_FOUND`;
- unknown module returns 404 `MODULE_REVISION_NOT_FOUND`;
- major 11 returns 422 `MODULE_KICAD_MAJOR_UNSUPPORTED`;
- an unconfigured catalog returns 409 `MODULE_CATALOG_UNAVAILABLE`;
- a second different module/digest in the same slot returns 409
  `COMPONENT_MODULE_BINDING_CONFLICT`;
- an extra JSON field receives the normal strict request-schema response; and
- remote mode, with an authorization header and server-side catalog configured,
  allows a binding even though it still rejects local `manifest_path` import.

For the slot-conflict API case, add `import shutil` and build an isolated
catalog under `tmp_path`:

```python
catalog_root = tmp_path / "modules"
shutil.copytree(_module_fixture_root(), catalog_root)
alternate = catalog_root / "status-led-alt"
shutil.copytree(catalog_root / "status-led-v1", alternate)
manifest = alternate / "module.yaml"
manifest.write_text(
    manifest.read_text(encoding="utf-8").replace(
        "module_revision_id: modrev_status_led_v1",
        "module_revision_id: modrev_status_led_alt_v1",
    ),
    encoding="utf-8",
)
```

Build the app with this `catalog_root`, first bind `modrev_status_led_v1`, then
post `modrev_status_led_alt_v1` against the same component revision and major
with a different idempotency key. The changed manifest ID yields a different
canonical digest and must produce the 409 binding-conflict code.

Add CLI coverage after the existing component import assertions:

```python
bound = runner.invoke(
    app,
    [
        "component", "bind-module", revision["id"],
        "--kicad-major", "10",
        "--module-revision-id", "modrev_status_led_v1",
        "--idempotency-key", "cli-binding-1", "--json",
    ],
    env={
        **env,
        "PCBFLOW_MODULE_CATALOG_DIR": str(_module_fixture_root()),
    },
)
assert bound.exit_code == 0, bound.output
binding = json.loads(bound.stdout)
```

Then invoke `component bindings <revision-id> --json` and assert `[binding]`.
Assert the unsupported-major CLI call exits 2 and prints
`MODULE_KICAD_MAJOR_UNSUPPORTED`.

- [ ] **Step 2: Run adapter tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_component_api_cli.py -q --basetemp C:\tmp\pcbflow-component-binding-task-3 -p no:cacheprovider
```

Expected: API routes and CLI commands are not registered, so binding requests
return 404 and the CLI reports an unknown command.

- [ ] **Step 3: Implement routes, error mapping, and commands**

In `api.py` import the three new binding errors plus
`ModuleRevisionNotFoundError`. Add exception handlers or `_mapped_domain_error`
branches with these exact mappings:

```python
ModuleRevisionNotFoundError -> (404, "MODULE_REVISION_NOT_FOUND", "module revision not found")
ModuleCatalogUnavailableError -> (409, "MODULE_CATALOG_UNAVAILABLE", "module catalog is unavailable")
ModuleKicadMajorUnsupportedError -> (422, "MODULE_KICAD_MAJOR_UNSUPPORTED", "module does not support the requested KiCad major")
ComponentModuleBindingConflictError -> (409, "COMPONENT_MODULE_BINDING_CONFLICT", "component revision already has a different module binding for this KiCad major")
```

Add `CreateComponentModuleBindingRequest` beside
`CreateComponentRevisionRequest`. Implement the create route with the
existing `Header(alias="Idempotency-Key", min_length=1)` declaration. Check
`services.component_module_binding_store.find_by_idempotency_key()` before
calling the service so the response status is 200 for a replay and 201 for a
new binding. Implement the list route by calling
`services.component_module_bindings.list_for_component_revision()` and return
`jsonable_encoder(...)`. Do not apply the local-source-path remote-mode guard
to either route.

In `cli.py`, import the same error types and `ModuleRevisionNotFoundError`.
Add their four stable code/message pairs to `_cli_error()` before the generic
`_mapped_domain_error()` fallback. Add these Typer command functions under the
existing `component_*` commands:

```python
@component_app.command("bind-module")
def component_bind_module(
    component_revision_id: str,
    kicad_major: Annotated[int, typer.Option("--kicad-major")],
    module_revision_id: Annotated[str, typer.Option("--module-revision-id")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None: ...


@component_app.command("bindings")
def component_bindings(
    component_revision_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None: ...
```

Use the same `_build()`, `try/finally: container.dispose()`, `_abort(error)`,
and `_emit(...)` patterns as `component_import`, `component_show`, and
`component_list`.

- [ ] **Step 4: Run API, CLI, and relevant regression tests to verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_component_api_cli.py tests\integration\test_component_module_bindings.py tests\integration\test_component_revisions.py tests\unit\test_component_bindings.py -q --basetemp C:\tmp\pcbflow-component-binding-task-3 -p no:cacheprovider
```

Expected: all adapter and binding tests pass; remote import remains forbidden
while remote binding is accepted with the configured server catalog.

- [ ] **Step 5: Commit the public adapters**

```powershell
git add src/pcbflow/api.py src/pcbflow/cli.py tests/e2e/test_component_api_cli.py
git commit -m "feat: expose component module bindings"
```

### Task 4: Document the Public Contract and Run the Complete Gate

**Files:**
- Modify: `README.md`

**Interfaces:** The README must list both REST operations and both CLI commands
with their idempotency, configured catalog, and immutable digest semantics.

- [ ] **Step 1: Update README API and CLI documentation**

Add these items to the existing API operation list:

```text
POST /api/v1/component-revisions/{component_revision_id}/module-bindings
GET /api/v1/component-revisions/{component_revision_id}/module-bindings
```

Add a concise component catalog example adjacent to the existing component CLI
commands:

```powershell
pcbflow component bind-module $componentRevisionId `
  --kicad-major 10 `
  --module-revision-id modrev_status_led_v1 `
  --idempotency-key component-module-v1 `
  --json

pcbflow component bindings $componentRevisionId --json
```

State that `PCBFLOW_MODULE_CATALOG_DIR` supplies the verified server-side
catalog, a binding freezes the selected module manifest digest, and a component
revision/major cannot be rebound to a different module.

- [ ] **Step 2: Run focused documentation-adjacent tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_component_api_cli.py tests\integration\test_component_module_bindings.py tests\integration\test_migrations.py -q --basetemp C:\tmp\pcbflow-component-binding-task-4 -p no:cacheprovider
```

Expected: all binding and migration contracts remain green.

- [ ] **Step 3: Run the complete quality gate**

Run these commands in order with fresh temporary roots for each pytest run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp C:\tmp\pcbflow-component-binding-full -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90 --basetemp C:\tmp\pcbflow-component-binding-cov -p no:cacheprovider
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
git diff --cached --check
```

Expected: the full suite passes, coverage remains at or above 90%, compilation
has no output, and both whitespace checks are clean.

- [ ] **Step 4: Commit the documentation and verification record**

Before committing, run `git status --short` and confirm only the README and
intended binding implementation/test files are present. Then run:

```powershell
git add README.md
git commit -m "docs: document component module bindings"
```

Do not include `.pytest-tmp*`, `.pytest_cache`, SQLite databases, artifacts,
or any unrelated user changes in this commit.
