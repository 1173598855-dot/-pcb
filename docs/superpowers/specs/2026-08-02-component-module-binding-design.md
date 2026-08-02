# Component Revision Module Binding Design

**Date:** 2026-08-02

**Status:** Approved design

## Goal

Add the next Phase 2B vertical slice: an immutable, auditable binding from a
verified `ComponentRevision` to one verified schematic module revision for a
specific KiCad major version.

The binding freezes the module revision ID and the canonical module manifest
digest observed at creation time. It does not change either source catalog or
infer symbol or footprint semantic equivalence from a KiCad template.

## Scope

This increment will:

- persist immutable component-module binding records in SQLite;
- permit at most one binding for each `(component_revision_id, kicad_major)`;
- verify the component revision, module catalog entry, requested KiCad major,
  and canonical module manifest digest before persistence;
- expose create and list operations through the existing local CLI and REST
  API conventions;
- preserve idempotent replay and deterministic conflict behavior under
  concurrent requests; and
- add migration, store, service, API, CLI, and regression coverage.

## Non-Goals

This increment does not:

- modify an existing `ComponentRevision`;
- import module files or module assets into the artifact store;
- change `InstantiateModule`, proposal execution, schematic editing, or PCB
  editing;
- prove semantic agreement between a component's symbol or footprint evidence
  and the contents of a module template;
- add a database foreign key to a file-system module catalog; or
- implement multiple alternative modules for one component revision and KiCad
  major.

## Data Model

Add an immutable `ComponentModuleBinding` domain record and a
`component_module_bindings` table with these fields:

| Field | Meaning |
| --- | --- |
| `id` | Public immutable `compmod_...` binding ID. |
| `component_revision_id` | Foreign key to `component_revisions.id`. |
| `kicad_major` | Positive KiCad major selected for this binding. |
| `module_revision_id` | Stable ID from the verified module manifest. |
| `module_manifest_digest` | Canonical digest returned by `FileModuleCatalog`. |
| `idempotency_key` | Unique create-operation key. |
| `created_at` | UTC creation timestamp. |

The table must enforce both of these constraints:

- `UNIQUE(component_revision_id, kicad_major)`;
- `UNIQUE(idempotency_key)`.

It will also have an index ordered by `component_revision_id`, `created_at`,
and `id` for stable binding-list queries. The module catalog remains a
runtime file-system source, so `module_revision_id` deliberately has no
database foreign key.

## Services And Persistence

`ComponentModuleBindingService.bind()` accepts a component revision ID, KiCad
major, module revision ID, and idempotency key. It:

1. Reads the component revision through `ComponentRevisionStore`; the existing
   not-found contract remains authoritative.
2. Requires a configured `FileModuleCatalog` and resolves the requested
   module revision from it. The catalog's existing integrity checks establish
   that the module manifest is verified.
3. Rejects a KiCad major that is not in `module.manifest.kicad_majors`.
4. Passes the component ID, requested major, module ID, and observed
   `module.manifest_digest` to `ComponentModuleBindingStore`.

The store owns transaction, insert, replay, and conflict behavior. It reserves
SQLite's write path with `BEGIN IMMEDIATE` before reading binding rows. Its
matching rule includes every immutable persisted field except generated ID and
creation time:

- an existing idempotency key with the same inputs returns that binding;
- an existing `(component_revision_id, kicad_major)` with the same module ID
  and digest returns that binding;
- a reused idempotency key with different inputs raises
  `IdempotencyConflictError`; and
- an occupied component-and-major slot with a different module ID or digest
  raises `ComponentModuleBindingConflictError`.

An `IntegrityError` race follows the same retrieve-and-compare logic, so a
concurrent identical request safely returns the immutable record rather than
creating a duplicate.

If a catalog later changes the bytes for the same module revision ID, a new
attempt to bind that component revision and major conflicts because the frozen
digest differs. Existing bindings remain immutable. A later consumer that
uses a binding must reload its module and compare the live digest with the
frozen digest; that consumer integration is intentionally outside this
increment.

## Public Interfaces

Add these REST operations:

```text
POST /api/v1/component-revisions/{component_revision_id}/module-bindings
GET  /api/v1/component-revisions/{component_revision_id}/module-bindings
```

The create request body is strict and contains only:

```json
{
  "kicad_major": 10,
  "module_revision_id": "modrev_example_v1"
}
```

It requires the existing `Idempotency-Key` header. A newly created binding
returns HTTP 201; a replay returns HTTP 200. The list operation returns only
the persisted, frozen binding records in stable creation order and does not
read the live catalog.

Add matching CLI commands:

```text
pcbflow component bind-module <component_revision_id> --kicad-major <n> \
  --module-revision-id <id> --idempotency-key <key> --json
pcbflow component bindings <component_revision_id> --json
```

The binding request contains no client local file path, so authenticated remote
mode permits this API operation. The server's configured module catalog is the
only catalog read.

## Error Contract

| Condition | HTTP status | Stable code |
| --- | --- | --- |
| Component revision absent | 404 | `COMPONENT_REVISION_NOT_FOUND` |
| Module catalog unavailable or fails integrity validation | 409 | `MODULE_CATALOG_UNAVAILABLE` |
| Module revision absent | 404 | `MODULE_REVISION_NOT_FOUND` |
| Requested major unsupported by the module | 422 | `MODULE_KICAD_MAJOR_UNSUPPORTED` |
| Slot already has a different module or digest | 409 | `COMPONENT_MODULE_BINDING_CONFLICT` |
| Idempotency key has different inputs | 409 | `IDEMPOTENCY_CONFLICT` |

CLI error output must use the same stable codes. Existing component and generic
idempotency error mappings remain unchanged.

## Migration

Add an Alembic migration after `0004_task_retry_schedule` to create
`component_module_bindings`, its foreign key, uniqueness constraints, and its
list-query index. The downgrade removes only this new table and its dependent
indexes and constraints.

## Verification

Focused tests will cover:

- migration upgrade and table, column, index, and constraint shape;
- successful creation, stable listing, and identical replay;
- conflicts for reused keys and occupied component-and-major slots;
- concurrent insert/replay recovery;
- missing component, unavailable or invalid catalog, missing module, and
  unsupported KiCad major;
- a changed manifest digest for the same module revision ID;
- REST status codes, strict request bodies, remote-mode behavior, and
  idempotency headers; and
- CLI JSON responses and stable error codes.

The complete quality gate uses a fresh `--basetemp` directory under `C:\tmp`
and disables pytest's cache provider because the checkout's legacy pytest
temporary/cache directories have inaccessible Windows ACLs.

## Acceptance Criteria

1. A verified component revision can bind exactly one verified module for an
   allowed KiCad major.
2. The returned binding contains the observed canonical module manifest digest.
3. Identical requests are safely replayed; conflicting requests never mutate
   an existing record.
4. Catalog changes cannot silently alter a stored binding.
5. API and CLI expose identical stable identifiers, error codes, and
   idempotency semantics.
6. Migration, focused tests, full regression tests, coverage, compilation,
   and whitespace checks pass.
