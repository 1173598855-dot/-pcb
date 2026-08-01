# Component Revision Catalog Design

**Status:** Approved design

## Goal

Implement the first Phase 2B vertical slice: a local, immutable catalog of
verified component revisions. Each revision records its canonical component
manifest and immutable evidence for the datasheet, pin mapping, symbol,
footprint, and optional 3D model.

The catalog gives a later schematic or PCB command a stable component revision
identifier and verifiable input digests. It does not perform component search,
vendor synchronization, pricing, availability checks, or automatic selection.

## Scope

The slice adds these capabilities:

- Import one local component manifest and its declared local assets.
- Store every accepted asset in the existing content-addressed artifact store.
- Persist an immutable `ComponentRevision` record in SQLite.
- Retrieve a revision by ID and list revisions for a manufacturer part number.
- Expose the same behavior through thin CLI and REST endpoints.
- Enforce input, digest, idempotency, and artifact-metadata contracts.

The existing `FileModuleCatalog` remains the runtime source for verified
schematic module fragments in this slice. Component revisions do not change
the current command schema or alter a KiCad project directly. A later Phase
2B increment can bind a verified module manifest to a component revision after
both catalogs have stable persistence contracts.

## Component Manifest

`component.yaml` is the import boundary. Its schema is strict, versioned, and
has no unknown fields. It contains:

- `schema_version`: initially `1.0`.
- `component_key`: stable manufacturer and part-number identity, formatted as
  `<manufacturer>:<part-number>`.
- `revision`: supplier or internal immutable revision label.
- `status`: only `verified` is importable.
- `name`, `manufacturer`, and `part_number`.
- `datasheet`, `pinout`, `symbol`, and `footprint` asset declarations.
- an optional `model_3d` declaration.

An asset declaration has a relative filename, media type, and declared
SHA-256 digest. Asset paths must be regular, non-link files directly beside
the manifest in its component directory. The importer independently hashes
the bytes and rejects mismatches before creating a database record.

The canonical JSON form of the parsed manifest becomes the revision's manifest
artifact. This makes the semantic declaration independently auditable even
when an input YAML file uses different formatting.

## Data Model And Storage

Add migration `0003_component_revision_catalog` and the `component_revisions`
table. A row contains:

- `id` (`comprev_...`) as the immutable public revision ID.
- `component_key`, `manufacturer`, `part_number`, `name`, `revision`, and
  `status`.
- `canonical_digest` and `manifest_artifact_digest`.
- foreign keys to the datasheet, pinout, symbol, footprint, and optional 3D
  artifact digests in `artifacts`.
- `idempotency_key` and `created_at`.

`(component_key, revision)` and `idempotency_key` are unique. Reusing either
key with identical canonical input replays the stored revision; any difference
raises the existing idempotency-conflict contract. Records have no update or
delete operation in the service API.

All bytes enter `ContentAddressedStore` before the SQL transaction. During the
transaction, the repository registers the artifact descriptors with the same
metadata-conflict checks used by requirements and approvals, then inserts the
component revision. An insert race reads and validates the winning row before
returning it. Orphaned content-addressed bytes are harmless and remain outside
the authoritative database record.

## Services And Interfaces

`ComponentRevisionStore` owns database reads, immutable insert/replay, and
artifact registration. `ComponentRevisionService` owns safe directory reading,
manifest parsing, asset digest verification, and artifact creation.

The interfaces are:

- `POST /api/v1/component-revisions` imports a local manifest path supplied by
  the local client and an idempotency key.
- `GET /api/v1/component-revisions/{component_revision_id}` returns one
  revision and artifact digests.
- `GET /api/v1/component-revisions?component_key=...` lists revisions in
  creation order.
- `pcbflow component import <component.yaml> --idempotency-key ...`.
- `pcbflow component show <component_revision_id>`.
- `pcbflow component list --component-key ...`.

API and CLI responses return identifiers, metadata, and digests only. They do
not return local paths or asset contents. The input path is accepted only in
local mode and follows the existing regular-file, byte-limit, and reparse-point
policy.

## Failure Contract

The service fails before persistence for invalid YAML, unsupported media type,
unsafe path, missing asset, digest mismatch, or non-verified status. It fails
transactionally for duplicate revision or idempotency conflicts. Missing rows
produce a stable `COMPONENT_REVISION_NOT_FOUND` code. Artifact metadata
conflicts are terminal integrity failures and never overwrite existing records.

No external process, network request, package manager, or KiCad mutation is
part of importing a component revision.

## Testing And Acceptance

Tests cover strict manifest parsing, traversal/link rejection, digest and media
type validation, immutable replay, conflicting-key rejection, concurrent
inserts, migration upgrade, artifact registration conflicts, API responses,
and CLI JSON output.

The completion gate is:

1. A local fixture component imports with four required evidence artifacts and
   an optional 3D artifact when declared.
2. Repeating the same request returns the same revision ID and creates no new
   row.
3. Altering any manifest or asset under the same key fails deterministically.
4. API and CLI expose the same digests without leaking local paths.
5. The complete suite passes with the repository coverage gate preserved.

## Non-Goals

This design deliberately excludes library symbol placement, footprint placement,
3D rendering, replacement-part rules, supplier synchronization, BOM generation,
PCB placement, and AI component selection. Those depend on this catalog's
immutable evidence contract and are separate Phase 2B or later increments.
