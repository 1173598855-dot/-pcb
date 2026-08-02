# Bound Module Instantiation Design

**Date:** 2026-08-02

**Status:** Approved design

## Goal

Make a verified `ComponentModuleBinding` usable by the controlled schematic
workflow. A proposal command that names an immutable `compmod_...` binding must
resolve its module only at execution time, verify the active KiCad major and
the frozen manifest digest, and then render the verified in-memory module into
the isolated candidate worktree.

## Scope

This increment will:

- add a strict `schematic.instantiate_bound_module` command operation;
- use `component_module_binding_id` as its sole module-selection input;
- resolve the binding and its live catalog module immediately before the
  adapter writes a candidate;
- reject a missing binding, KiCad-major mismatch, unavailable catalog, missing
  module, unsupported live module, or manifest digest drift;
- record the resolved binding and module identity in proposal evidence;
- preserve `schematic.instantiate_module` and every existing command schema
  behavior; and
- cover the new flow through unit, integration, and REST/CLI proposal tests.

## Non-Goals

This increment does not:

- change a stored binding, component revision, module manifest, or catalog
  directory;
- import modules into SQLite or the artifact store;
- add alternate component rules, supplier data, BOM generation, or PCB edits;
- require a component binding for existing direct module instantiation; or
- infer semantic agreement between a component's evidence and a module
  template beyond the already-created immutable binding.

## Command Contract

Add these strict command models beside the existing direct module operation:

```python
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
```

`DesignOperation` includes the new operation. The payload deliberately omits a
module revision ID and manifest digest, so a caller cannot select a module that
differs from the immutable binding. The original
`schematic.instantiate_module` payload and operation remain unchanged for
generic verified modules.

The binding ID is part of the canonical command batch artifact. Successful
adapter evidence additionally records the binding ID, component revision ID,
KiCad major, module revision ID, frozen manifest digest, and verified live
manifest digest. No evidence contains a local catalog path.

## Binding Resolution

`ComponentModuleBindingStore` gains a read-only `get(binding_id)` operation.
It raises `ComponentModuleBindingNotFoundError` when no row exists.

`ComponentModuleBindingService` gains:

```python
def resolve_for_instantiation(
    self, binding_id: str, kicad_major: int
) -> BoundModuleResolution: ...
```

`BoundModuleResolution` contains the immutable binding and the verified
`ModuleRevision` returned by the existing configured catalog. The resolution
algorithm is ordered as follows:

1. Read the persisted binding by ID.
2. Require the active `kicad_major` to equal `binding.kicad_major`.
3. Require the configured catalog and resolve `binding.module_revision_id`.
4. Require the returned manifest to be verified, to retain the bound revision
   ID, and to support the active KiCad major.
5. Require the live `manifest_digest` to exactly equal
   `binding.module_manifest_digest`.
6. Return the in-memory resolution to the adapter.

The service performs no writes. It reuses existing catalog-unavailable,
module-not-found, and unsupported-major contracts where applicable. It adds
these stable resolution errors:

| Condition | Error code |
| --- | --- |
| Binding ID is absent | `COMPONENT_MODULE_BINDING_NOT_FOUND` |
| Binding major differs from active KiCad major | `COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH` |
| Live manifest ID or digest differs from the binding | `COMPONENT_MODULE_BINDING_DIGEST_MISMATCH` |

Digest mismatch errors carry only the public binding and digest values needed
for failed evidence. They never include catalog paths or template bytes.

## Adapter And Container Boundaries

Define a narrow `BoundModuleResolverPort` protocol in the component-binding
domain module. It accepts a binding ID and KiCad major and returns a
`BoundModuleResolution`. `ComponentModuleBindingService` implements it.

`CstSchematicAdapter` accepts an optional resolver in addition to the existing
read-only `ModuleCatalogPort`. For
`schematic.instantiate_bound_module`, it calls the resolver immediately before
the existing hierarchical instantiation routine. That routine receives the
already-verified `ModuleRevision`; it must not reopen the catalog or use a
path after resolution.

The adapter treats one bound instantiation like the existing one-command
direct-instantiation path: it enforces the same parameter, port, placement,
KiCad profile, CST, deterministic UUID, semantic-diff, and rollback rules.
The container injects the current binding service and the same configured
catalog already used for ordinary modules. No new database schema or migration
is required.

## Proposal Failure And Evidence Contract

The proposal executor converts the three new binding-resolution errors into
terminal task failures with their exact stable codes. It persists the standard
failed evidence set and a canonical command-execution record containing the
stage, binding ID, module revision ID when known, frozen digest when known,
and observed digest when known. A failure occurs before any schematic write,
candidate commit, or candidate-ref publication.

On success, `adapter_capability_report` includes a stable ordered
`bound_module_resolutions` collection. Every entry has:

```json
{
  "binding_id": "compmod_example",
  "component_revision_id": "comprev_example",
  "kicad_major": 10,
  "module_revision_id": "modrev_example_v1",
  "frozen_manifest_digest": "sha256:<digest>",
  "live_manifest_digest": "sha256:<digest>"
}
```

The existing `module_digests` field continues to report the live verified
digest. Direct module instantiation produces an empty
`bound_module_resolutions` collection, preserving its behavior and allowing
consumers to distinguish the two paths.

## Verification

Focused coverage must establish:

- strict schema acceptance for a valid bound operation and rejection of a
  payload that also supplies a module revision ID;
- store/service behavior for missing bindings, active-major mismatch, missing
  catalog, missing module, unsupported module, and digest drift;
- adapter rendering from a resolver-returned module and unchanged direct
  instantiation behavior;
- successful managed proposal execution after importing a component, creating
  a binding, and submitting a bound command;
- a catalog mutation that remains internally valid but changes its manifest
  digest fails with `COMPONENT_MODULE_BINDING_DIGEST_MISMATCH`, creates no
  candidate revision, and leaves the managed source unchanged; and
- REST and CLI proposal submission retain strict command-schema handling and
  expose the persisted terminal code through existing proposal status output.

The complete gate runs the full suite with a fresh `C:\tmp` base temporary
directory and disabled pytest cache provider, coverage at or above 90%,
`python -m compileall -q src`, and both Git whitespace checks.

## Acceptance Criteria

1. A `compmod_...` binding can instantiate only its frozen module for its
   selected KiCad major.
2. A live catalog change cannot silently alter a bound module candidate.
3. Failure prevents schematic writes, candidate commits, and candidate-ref
   publication while retaining auditable failed evidence.
4. Existing direct module-instantiation commands remain compatible.
5. The new command is strict, deterministic, and exposed through the existing
   proposal REST and CLI command-batch paths.
6. Unit, integration, E2E, full regression, coverage, compilation, and
   whitespace gates pass.
