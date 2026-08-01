# Component Revision Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, immutable SQLite-backed catalog for verified component
revisions and their content-addressed technical evidence.

**Architecture:** A strict `component.yaml` parser creates a canonical manifest
and validates local asset declarations. A dedicated store persists immutable
`ComponentRevision` records and artifact references through a new Alembic
migration. A service owns safe local file reading and CAS insertion, while
thin FastAPI and Typer adapters expose import and query operations without
returning local paths.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLAlchemy 2, Alembic, FastAPI,
Typer, PyYAML, pytest, pytest-cov, SQLite, existing `ContentAddressedStore`.

## Global Constraints

- SQLite remains the only supported database.
- All imported files are local, regular non-link files; symlinks and Windows
  reparse points are rejected.
- No network access, supplier synchronization, KiCad mutation, BOM generation,
  or automatic component selection is introduced.
- Only a manifest with `status: verified` is accepted.
- A component revision is immutable: the service exposes no update or delete.
- Every manifest and asset byte sequence is stored through
  `ContentAddressedStore` and registered before the row references it.
- API and CLI outputs contain metadata and digests, never local paths or asset
  bytes.
- New behavior follows test-first red-green cycles and preserves the existing
  coverage gate.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `src/pcbflow/components.py` | Strict manifest models, domain record, canonical identity and local import-value types. |
| `src/pcbflow/component_store.py` | Immutable persistence, artifact registration, idempotent replay, and lookup errors. |
| `src/pcbflow/tables.py` | SQLAlchemy `ComponentRevisionRow` definition. |
| `alembic/versions/0003_component_revision_catalog.py` | SQLite schema evolution from Phase 2A to the component catalog. |
| `src/pcbflow/container.py` | Constructs and exposes component store and service. |
| `src/pcbflow/api.py` | Local import and read-only component-revision routes and error mapping. |
| `src/pcbflow/cli.py` | `pcbflow component import/show/list` commands and stable CLI errors. |
| `tests/component_fixtures.py` | Deterministic local component directory builder shared by unit, integration, and end-to-end tests. |
| `tests/unit/test_components.py` | Strict manifest and pure domain tests. |
| `tests/integration/test_component_revisions.py` | Migration, CAS, persistence, idempotency, and input-boundary tests. |
| `tests/e2e/test_component_api_cli.py` | API/CLI response and local-mode contract tests. |
| `README.md` | Local import and query examples plus the catalog's non-goals. |

## Shared Interfaces

Task 1 defines the types consumed by later tasks:

| Symbol | Contract |
| --- | --- |
| `ComponentRevision` | Frozen domain record with public ID, component identity, status, canonical manifest digest, six artifact-digest fields (3D nullable), idempotency key, and creation time. |
| `ComponentManifest` | Strict Pydantic model with `schema_version`, component identity metadata, `status`, four required `ComponentAsset` fields, and nullable `model_3d`. |
| `load_component_manifest(data)` | Parses YAML bytes and returns a `ComponentManifest`; invalid shape or field values raise `ValueError`. |
| `component_manifest_digest(manifest)` | Returns the SHA-256 canonical digest of `manifest.model_dump(mode="json")`. |

Task 2 defines the persistence and service interfaces consumed by adapters:

| Symbol | Contract |
| --- | --- |
| `ComponentRevisionNotFoundError` | `LookupError` raised by `ComponentRevisionStore.get` for an unknown public ID. |
| `ComponentRevisionStore.get(component_revision_id)` | Returns the stored immutable `ComponentRevision`. |
| `ComponentRevisionStore.list_for_component(component_key)` | Returns component revisions in ascending creation order. |
| `ComponentRevisionStore.find_by_idempotency_key(idempotency_key)` | Returns the matching revision or `None`. |
| `ComponentRevisionStore.find_by_identity(component_key, revision)` | Returns the matching immutable identity or `None`. |
| `ComponentRevisionStore.create(manifest, canonical_digest, idempotency_key, artifacts)` | Registers descriptors and returns a newly inserted or verified replayed revision; conflicting input raises `IdempotencyConflictError`. |
| `ComponentRevisionService.import_revision(manifest_path, idempotency_key)` | Safely reads one component directory, verifies declared asset digests, stores evidence, and delegates the immutable insert. |

The shared test fixture builder writes these deterministic bytes: `datasheet.pdf`
contains `b"%PDF-1.4\ncomponent fixture\n"`; `pinout.json` contains
`b'{"pins":[{"number":"1","name":"A"}]}'`; `symbol.kicad_sym` contains
`b"(kicad_symbol_lib (version 20231120) (generator pcbflow))\n"`;
`footprint.kicad_mod` contains `b"(footprint \"LED_0603\")\n"`; and optional
`model.step` contains `b"ISO-10303-21;\nEND-ISO-10303-21;\n"`. It calculates
every declared `sha256:` digest from those bytes, writes `component.yaml`, and
returns the manifest path.

`ComponentArtifacts` is a frozen value object containing an
`ArtifactDescriptor` for the canonical manifest, four required evidence assets,
and an optional 3D descriptor.

### Task 1: Strict Component Manifest And Domain Types

**Files:**
- Create: `src/pcbflow/components.py`
- Create: `tests/component_fixtures.py`
- Create: `tests/unit/test_components.py`

**Interfaces:**
- Produces `ComponentAsset`, `ComponentManifest`, `ComponentRevision`,
  `load_component_manifest`, and `component_manifest_digest`.
- Consumed by `ComponentRevisionStore` and `ComponentRevisionService` in Tasks
  2 and 3.

- [ ] **Step 1: Write the failing manifest tests**

```python
def test_load_component_manifest_accepts_verified_canonical_input() -> None:
    manifest = load_component_manifest(component_yaml())
    assert manifest.component_key == "Acme:LED-0603-RED"
    assert manifest.status == "verified"
    assert component_manifest_digest(manifest).startswith("sha256:")

@pytest.mark.parametrize("replacement", [
    b"status: draft",
    b"path: ../datasheet.pdf",
    b"media_type: text/plain",
])
def test_load_component_manifest_rejects_unverified_or_unsafe_assets(
    replacement: bytes,
) -> None:
    with pytest.raises(ValueError):
        if replacement == b"status: draft":
            data = component_yaml().replace(b"status: verified", replacement)
        elif replacement == b"path: ../datasheet.pdf":
            data = component_yaml().replace(b"path: datasheet.pdf", replacement)
        else:
            data = component_yaml().replace(b"media_type: application/pdf", replacement)
        load_component_manifest(data)
```

Implement `component_yaml()` in `tests/component_fixtures.py`; it serializes a
verified manifest for the deterministic byte literals above. Add assertions
that changing a declared asset digest changes the canonical digest.

- [ ] **Step 2: Run the new unit test file to verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_components.py -v`

Expected: FAIL during collection because `pcbflow.components` does not exist.

- [ ] **Step 3: Implement strict manifest parsing and the immutable domain record**

```python
class ComponentAsset(StrictModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    media_type: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

def load_component_manifest(data: bytes) -> ComponentManifest:
    value = yaml.safe_load(data)
    if not isinstance(value, dict):
        raise ValueError("component manifest must be a mapping")
    return ComponentManifest.model_validate(value, strict=True)

def component_manifest_digest(manifest: ComponentManifest) -> str:
    return canonical_digest(manifest.model_dump(mode="json"))
```

Use `model_validator` to require exact media types by field:
`application/pdf`, `application/json`, `application/vnd.kicad.symbol`,
`application/vnd.kicad.footprint`, and, when present, `model/step`. Require
`component_key == f"{manufacturer}:{part_number}"` and reject duplicate asset
paths. The public `ComponentRevision` is a frozen `dataclass` and contains no
local file path.

- [ ] **Step 4: Run unit tests to verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_components.py -v`

Expected: PASS with accepted canonical manifests and all invalid inputs rejected.

- [ ] **Step 5: Commit the domain contract**

```powershell
git add src/pcbflow/components.py tests/component_fixtures.py tests/unit/test_components.py
git commit -m "feat: define verified component manifests"
```

### Task 2: Persist Immutable Component Revisions

**Files:**
- Modify: `src/pcbflow/tables.py`
- Create: `alembic/versions/0003_component_revision_catalog.py`
- Create: `src/pcbflow/component_store.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_component_revisions.py`

**Interfaces:**
- Consumes `ComponentManifest`, `ComponentRevision`, and
  `component_manifest_digest` from Task 1.
- Produces `ComponentRevisionStore`, `ComponentRevisionNotFoundError`, and
  migration revision `0003_component_revision_catalog`.
- Consumed by `ComponentRevisionService` in Task 3 and the composition root in
  Task 4.

- [ ] **Step 1: Write failing migration and store tests**

```python
def test_migrations_upgrade_to_component_catalog_head(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003_component_revision_catalog"
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("component_revisions")}
    assert {"id", "component_key", "revision", "manifest_artifact_digest"} <= columns

def test_component_revision_store_replays_identical_import(
    session_factory, artifact_store, component_manifest
) -> None:
    store = ComponentRevisionStore(session_factory)
    artifacts = component_artifacts(artifact_store, component_manifest)
    first = store.create(manifest=component_manifest, canonical_digest=component_manifest_digest(component_manifest), idempotency_key="component-led-1", artifacts=artifacts)
    second = store.create(manifest=component_manifest, canonical_digest=component_manifest_digest(component_manifest), idempotency_key="component-led-1", artifacts=artifacts)
    assert second == first

def test_component_revision_store_rejects_key_or_identity_conflicts(
    session_factory, artifact_store, component_manifest
) -> None:
    store = ComponentRevisionStore(session_factory)
    artifacts = component_artifacts(artifact_store, component_manifest)
    store.create(
        manifest=component_manifest,
        canonical_digest=component_manifest_digest(component_manifest),
        idempotency_key="component-led-1",
        artifacts=artifacts,
    )
    changed = component_manifest.model_copy(update={"name": "Different LED"})
    with pytest.raises(IdempotencyConflictError):
        store.create(
            manifest=changed,
            canonical_digest=component_manifest_digest(changed),
            idempotency_key="component-led-1",
            artifacts=artifacts,
        )
    with pytest.raises(IdempotencyConflictError):
        store.create(
            manifest=changed,
            canonical_digest=component_manifest_digest(changed),
            idempotency_key="component-led-2",
            artifacts=artifacts,
        )
```

In this file, define `component_manifest` as
`load_component_manifest(component_yaml())`, `component_artifacts` by storing
the canonical manifest bytes plus deterministic fixture asset bytes in the CAS,
and `component_artifact_digests` as a tuple of the six digest fields after
filtering out `None`. Assert every resulting digest resolves to an
`ArtifactRow`.

- [ ] **Step 2: Run the focused integration tests to verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_component_revisions.py -v`

Expected: FAIL because the table, migration, and store do not yet exist.

- [ ] **Step 3: Add the table, migration, and store**

```python
# src/pcbflow/tables.py
class ComponentRevisionRow(Base):
    __tablename__ = "component_revisions"
    __table_args__ = (
        UniqueConstraint("component_key", "revision", name="uq_component_revision_identity"),
        Index("ix_component_revisions_component_key", "component_key", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    component_key: Mapped[str] = mapped_column(String(255), nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=False)
    part_number: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_artifact_digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    datasheet_artifact_digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    pinout_artifact_digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    symbol_artifact_digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    footprint_artifact_digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    model_3d_artifact_digest: Mapped[str | None] = mapped_column(ForeignKey("artifacts.digest"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

The migration creates this table and index, with all required columns
non-nullable and each artifact digest a foreign key to `artifacts.digest`.
`ComponentRevisionStore.create` first checks the idempotency key and identity,
validates canonical digest plus every artifact descriptor digest, then registers
descriptors and inserts `new_id("comprev")` in one session transaction. On an
`IntegrityError`, re-read the winning row and run the same replay validation;
never overwrite a row.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_component_revisions.py -v`

Expected: PASS, including deterministic replay and both conflict cases.

- [ ] **Step 5: Commit catalog persistence**

```powershell
git add src/pcbflow/tables.py src/pcbflow/component_store.py alembic/versions/0003_component_revision_catalog.py tests/integration/test_migrations.py tests/integration/test_component_revisions.py
git commit -m "feat: persist immutable component revisions"
```

### Task 3: Import Local Evidence Into The Catalog

**Files:**
- Modify: `src/pcbflow/components.py`
- Modify: `src/pcbflow/component_store.py`
- Modify: `tests/integration/test_component_revisions.py`

**Interfaces:**
- Consumes `ComponentRevisionStore.create` from Task 2 and
  `ContentAddressedStore.put_bytes`.
- Produces `ComponentRevisionService.import_revision(manifest_path,
  idempotency_key)`.
- Consumed by the `Container`, API, and CLI in Task 4.

- [ ] **Step 1: Write failing safe-import integration tests**

```python
def test_component_import_stores_canonical_manifest_and_all_asset_evidence(
    container, component_directory: Path
) -> None:
    created = container.components.import_revision(component_directory / "component.yaml", "component-import-1")
    assert created.model_3d_artifact_digest is not None
    assert all(container.artifacts.verify(digest) for digest in component_artifact_digests(created))

def test_component_import_rejects_linked_missing_or_digest_mismatched_asset(
    container, component_directory: Path
) -> None:
    (component_directory / "symbol.kicad_sym").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        container.components.import_revision(component_directory / "component.yaml", "component-import-2")
```

Add separate cases for a `../` asset path, a reparse-point/symlink asset where
supported by the platform, a missing asset, and an import key replay. Assert
the failed operations leave no `component_revisions` row.

Define `component_directory` as a fixture that calls
`build_component_directory(tmp_path / "component", include_model=True)` from
`tests.component_fixtures`. Define `component_artifact_digests(revision)` in
the same test file as:

```python
def component_artifact_digests(revision: ComponentRevision) -> tuple[str, ...]:
    return tuple(
        digest
        for digest in (
            revision.manifest_artifact_digest,
            revision.datasheet_artifact_digest,
            revision.pinout_artifact_digest,
            revision.symbol_artifact_digest,
            revision.footprint_artifact_digest,
            revision.model_3d_artifact_digest,
        )
        if digest is not None
    )
```

- [ ] **Step 2: Run safe-import tests to verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_component_revisions.py -v`

Expected: FAIL because `Container` has no `components` service.

- [ ] **Step 3: Implement bounded local asset import**

```python
def import_revision(self, manifest_path: Path, idempotency_key: str) -> ComponentRevision:
    manifest_bytes = _read_regular_file(manifest_path, self._max_bytes)
    manifest = load_component_manifest(manifest_bytes)
    root = _checked_component_directory(manifest_path.parent)
    assets = _load_declared_assets(root, manifest, self._max_bytes)
    descriptors = ComponentArtifacts(
        manifest=self._artifacts.put_bytes(canonical_json_bytes(manifest.model_dump(mode="json")), _MANIFEST_MEDIA_TYPE),
        datasheet=self._artifacts.put_bytes(assets.datasheet, manifest.datasheet.media_type),
        pinout=self._artifacts.put_bytes(assets.pinout, manifest.pinout.media_type),
        symbol=self._artifacts.put_bytes(assets.symbol, manifest.symbol.media_type),
        footprint=self._artifacts.put_bytes(assets.footprint, manifest.footprint.media_type),
        model_3d=(self._artifacts.put_bytes(assets.model_3d, manifest.model_3d.media_type) if assets.model_3d is not None else None),
    )
    return self._store.create(manifest=manifest, canonical_digest=component_manifest_digest(manifest), idempotency_key=idempotency_key, artifacts=descriptors)
```

`_read_regular_file` checks `lstat`, rejects reparse points and non-regular
files, reads no more than the configured byte limit plus one byte, and maps
filesystem errors to stable `ValueError` messages. `_load_declared_assets`
uses `root / asset.path` only after requiring `candidate.parent == root`, then
independently verifies its computed SHA-256 digest equals `asset.digest`. It applies the
byte limit cumulatively across manifest and all evidence assets.

Update `Container` with fields `component_store` and `components`, construct
both after `ContentAddressedStore`, and pass `settings.max_project_bytes` as
the import byte budget. Do not change worker handlers or module catalog wiring.

- [ ] **Step 4: Run import integration tests to verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_component_revisions.py -v`

Expected: PASS with artifact verification, safe-path rejection, and immutable
replay coverage.

- [ ] **Step 5: Commit local import service**

```powershell
git add src/pcbflow/components.py src/pcbflow/component_store.py src/pcbflow/container.py tests/integration/test_component_revisions.py
git commit -m "feat: import verified component evidence"
```

### Task 4: Expose Read-Only Catalog Operations Through API And CLI

**Files:**
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/unit/test_phase2a_edges.py`
- Create: `tests/e2e/test_component_api_cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes `Container.components.import_revision`,
  `Container.component_store.get`, and
  `Container.component_store.list_for_component` from Task 3.
- Produces `POST /api/v1/component-revisions`,
  `GET /api/v1/component-revisions/{id}`, and
  `GET /api/v1/component-revisions?component_key=Acme:LED-0603-RED`; and the equivalent
  `pcbflow component import/show/list` commands.

- [ ] **Step 1: Write failing API and CLI contract tests**

```python
def test_component_revision_api_import_show_and_list(tmp_path: Path) -> None:
    client = _client_for(tmp_path)
    manifest_path = build_component_directory(tmp_path / "api-component")
    response = client.post(
        "/api/v1/component-revisions",
        json={"manifest_path": str(manifest_path)},
        headers={"Idempotency-Key": "api-component-1"},
    )
    assert response.status_code == 201
    revision = response.json()
    assert "manifest_path" not in revision
    assert client.get(f"/api/v1/component-revisions/{revision['id']}").json() == revision
    assert client.get("/api/v1/component-revisions", params={"component_key": "Acme:LED-0603-RED"}).json() == [revision]

def test_component_commands_return_stable_json_and_missing_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PCBFLOW_DATA_DIR", str(tmp_path / "component-cli-data"))
    manifest_path = build_component_directory(tmp_path / "cli-component")
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["component", "import", str(manifest_path), "--idempotency-key", "cli-component-1", "--json"],
    )
    assert created.exit_code == 0, created.output
    replayed = runner.invoke(
        app,
        ["component", "import", str(manifest_path), "--idempotency-key", "cli-component-1", "--json"],
    )
    assert json.loads(replayed.output)["id"] == json.loads(created.output)["id"]
    result = runner.invoke(app, ["component", "show", "missing", "--json"])
    assert result.exit_code == 2
    assert "COMPONENT_REVISION_NOT_FOUND" in result.output
```

The API test must assert remote mode rejects import with
`LOCAL_SOURCE_PATHS_DISABLED`, while both GET routes remain available. The CLI
test imports the same local fixture twice and asserts the same revision ID is
returned.

- [ ] **Step 2: Run adapter tests to verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/e2e/test_component_api_cli.py tests/unit/test_phase2a_edges.py -v`

Expected: FAIL because the component routes, Typer subcommand, and error mapper
do not exist.

- [ ] **Step 3: Implement thin API, CLI, and documentation adapters**

```python
# src/pcbflow/api.py
class CreateComponentRevisionRequest(StrictRequest):
    manifest_path: str = Field(min_length=1)

@app.post("/api/v1/component-revisions", status_code=201)
def import_component_revision(payload: CreateComponentRevisionRequest, response: Response,
                              idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)]):
    if services.settings.remote_mode:
        raise ApiError(403, "LOCAL_SOURCE_PATHS_DISABLED", "local source paths are disabled in remote mode")
    existing = services.component_store.find_by_idempotency_key(idempotency_key)
    result = services.components.import_revision(Path(payload.manifest_path), idempotency_key)
    response.status_code = 200 if existing is not None else 201
    return _component_revision_response(result)
```

Add `component_app = typer.Typer(no_args_is_help=True)` and register it on the
root application. The import command accepts `manifest_path` as a positional
argument and `--idempotency-key`; show accepts an ID; list requires
`--component-key`. Each command builds and disposes the container in `try` /
`finally`, mirrors existing JSON output behavior, and maps
`ComponentRevisionNotFoundError` to `COMPONENT_REVISION_NOT_FOUND`.

Document a local command sequence using a component directory and explain that
imports copy evidence into PCBFlow's data directory while outputs expose only
digests.

- [ ] **Step 4: Run API and CLI tests to verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/e2e/test_component_api_cli.py tests/unit/test_phase2a_edges.py -v`

Expected: PASS with 201/200 idempotency behavior, hidden paths, stable missing
code, and remote-mode import rejection.

- [ ] **Step 5: Commit public adapters and docs**

```powershell
git add src/pcbflow/api.py src/pcbflow/cli.py tests/e2e/test_component_api_cli.py tests/unit/test_phase2a_edges.py README.md
git commit -m "feat: expose component revision catalog"
```

### Task 5: Run The Full Acceptance Gate

**Files:**
- Modify only if verification identifies a defect in the files listed above.

**Interfaces:**
- Verifies all Task 1-4 public and persistence interfaces together.

- [ ] **Step 1: Run migration, unit, integration, and adapter coverage tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  tests\unit\test_components.py `
  tests\integration\test_component_revisions.py `
  tests\integration\test_migrations.py `
  tests\e2e\test_component_api_cli.py `
  --cov=pcbflow --cov-report=term-missing
```

Expected: PASS; the report includes `components.py` and `component_store.py`
with normal, idempotent, unsafe-input, and conflict paths executed.

- [ ] **Step 2: Run the complete regression and coverage gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  --cov=pcbflow --cov-report=term-missing:skip-covered --cov-fail-under=91
```

Expected: all tests pass, optional real-KiCad contracts may skip when the CLI
is unavailable to the test process, and total coverage remains at or above
`91%`.

- [ ] **Step 3: Verify migration upgrade from an empty database**

Run:

```powershell
Remove-Item -LiteralPath .pytest-tmp\component-migration.db -Force -ErrorAction SilentlyContinue
$env:PCBFLOW_DATABASE_URL = "sqlite+pysqlite:///$((Resolve-Path .pytest-tmp).Path.Replace('\', '/'))/component-migration.db"
.\.venv\Scripts\pcbflow.exe doctor --json
```

Expected: command exits `0` and Alembic upgrades the empty database to
`0003_component_revision_catalog` before the diagnostic result is returned.

- [ ] **Step 4: Check the final diff**

Run: `git diff --check HEAD~4..HEAD; git status --short`

Expected: no whitespace errors; only intentionally uncommitted user files, if
any, remain outside the Phase 2B commits.
