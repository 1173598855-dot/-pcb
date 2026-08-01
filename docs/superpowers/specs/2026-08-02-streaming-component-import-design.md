# Streaming Component Import Design

**Status:** Approved design

## Goal

Reduce peak memory during local component-revision imports without changing the
component manifest, REST API, CLI output, SQLite schema, idempotency behavior,
or artifact metadata contract.

The importer must process declared component assets in bounded chunks while
preserving the declared SHA-256 verification, atomic content-addressed
publication, and the existing no-revision-on-failure behavior.

## Scope

This increment changes only the ingestion path used by
`ComponentRevisionService.import_revision()` and the content-addressed artifact
store it calls.

It includes:

- a staged, chunked artifact-write path;
- bounded streaming of declared component assets;
- cleanup of uncommitted temporary artifacts on input failures;
- regression coverage for chunking, limits, digest mismatches, atomic publish,
  and unchanged component import behavior.

It excludes request-body streaming, component schema changes, SQLite migration
changes, API or CLI shape changes, artifact garbage collection, supplier
integration, and any KiCad project mutation.

## Existing Contract

`component.yaml` is read and parsed before its declared sibling assets. Each
asset must be a regular, non-link direct sibling of the manifest; its media type
and SHA-256 digest must equal the strict declaration. The sum of the manifest
and all asset bytes must not exceed the configured import limit.

On a successful import, the existing `ComponentRevisionStore` receives artifact
descriptors in this exact order: canonical manifest, datasheet, pinout, symbol,
footprint, then optional 3D model. It registers their metadata and atomically
creates or replays the immutable component revision. API and CLI responses
expose only revision metadata and digest values.

## Design

### Artifact Staging

`ContentAddressedStore` gains a bounded streaming write primitive. It writes a
source stream into a temporary file below the artifact root, computes SHA-256
and size incrementally, and validates an optional expected digest before the
temporary file becomes visible at a content-addressed object path.

The temporary file is never held in memory as a complete asset. Its final
publication uses `os.replace()` from the store-owned staging directory to the
digest-derived target on the same filesystem. If that target already exists,
the store rehashes it and verifies both digest and size before returning the
descriptor. A mismatch remains an `ArtifactConflictError`.

Staged writes expose descriptors only after all input validation has completed.
Each staged file has an explicit cleanup path. Publishing a staged file is
per-artifact atomic; files successfully published before a later storage error
remain harmless unregistered content-addressed objects, matching the existing
artifact-store failure model.

`put_bytes()` delegates to the same publication path so byte-oriented callers
retain their current contract and there is one implementation of collision and
atomic-write behavior.

### Component Import Flow

The manifest remains an in-memory byte value because YAML parsing requires the
complete document. Its current configured size bound is unchanged.

For each declared asset, `ComponentRevisionService` verifies the existing path
and file-kind policy, opens the regular file once, and sends that handle to the
staged artifact writer with:

- the asset media type;
- the manifest's expected digest; and
- the remaining cumulative import-byte allowance.

The service subtracts each validated descriptor size from the remaining budget.
It collects staged descriptors in the existing manifest/asset order. If any
asset is unreadable, exceeds the total limit, or has a digest mismatch, all
unpublished staged files are removed, no component revision is inserted, and
the existing `ValueError` messages remain stable.

After every input has been validated, the service publishes the canonical
manifest and all staged assets, then passes the resulting descriptors unchanged
to `ComponentRevisionStore.create()`. The store remains the sole owner of
database registration, replay, conflict, and artifact metadata behavior.

## Failure Handling

| Condition | Result |
| --- | --- |
| Source read failure, link/reparse point, or non-regular file | Existing component input error; all uncommitted stages are removed. |
| Cumulative size limit exceeded | Existing `component import exceeds size limit` error; no revision row is created. |
| Declared digest mismatch | Existing asset-specific digest-mismatch error; no revision row is created. |
| Existing target content is corrupted | `ArtifactConflictError`; no revision row is created. |
| Database registration or uniqueness race fails | Existing component-store replay/conflict behavior; already-published orphan objects remain harmless. |

No error path may overwrite a verified artifact object, return a descriptor for
bytes that failed digest validation, or leave a component revision that refers
to an unpublished object.

## Testing And Acceptance

Add focused tests that prove:

1. A stream is read in fixed-size bounded chunks and produces the expected
   descriptor, digest, and bytes.
2. Digest mismatch and byte-limit failure remove staged files and do not publish
   an object for the failed source.
3. A component asset larger than one stream chunk imports successfully without
   using `put_bytes()` for that asset.
4. A failure in a later declared asset leaves no component revision and no
   unpublished staging residue.
5. Existing replay, collision, API, and CLI integration tests continue to pass
   without response-shape changes.

Completion requires focused artifact/component tests, the full pytest suite,
the 90 percent coverage gate, source compilation, and `git diff --check`.
