# Canonical Model Bytes Design

## Problem

`BoardSnapshot.canonical_digest()` and `ManufacturingRulePack.canonical_digest()`
each own a canonical ordering rule, while `pcb_candidates.py` repeats those rules
to create evidence artifacts. A future model field can therefore update a digest
without updating its evidence bytes, or vice versa.

## Scope

Add model-level `canonical_bytes()` methods for BoardIR snapshots and manufacturing
rule packs. Each existing `canonical_digest()` must derive its value from the same
bytes. Candidate evidence publication must call these methods rather than duplicate
the ordering rules.

No schema, persisted payload, API, candidate workflow, or digest regression value
may change.

## Acceptance Criteria

1. `sha256(canonical_bytes())` exactly equals each model's existing
   `canonical_digest()` output.
2. Reordering canonical object collections still produces identical bytes and
   digests.
3. Candidate evidence calls the model methods and no longer owns parallel object
   ordering helpers.
4. Existing fixed digest regression values and all project verification gates pass.
