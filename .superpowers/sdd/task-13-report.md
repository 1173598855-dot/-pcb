# Task 13 Report

## RED

- `tests/unit/test_schematic_modules.py` first failed during collection because
  `pcbflow.schematic.adapter` did not exist.
- The port-binding test first failed because the parent-page label was placed
  inside the sheet subtree rather than as a KiCad root sibling.
- The late semantic-validation test first failed because a post-write
  `build_semantic_diff` error left the root schematic and generated child in
  the project.
- The no-catalog footprint test first failed with `DESIGN_COMMAND_UNSUPPORTED`
  instead of the stable catalog-not-found error.

## Implementation

- Added a read-only, security-checked `FileModuleCatalog` with strict frozen
  Pydantic manifest models, raw template digest verification, link/reparse,
  path, type, file-count, and byte-limit checks.
- Added deterministic UUID v5 derivation and a CST-only KiCad 9 module
  instantiator. It replaces template UUID and declared parameter CST atoms,
  writes the child with a sibling temporary file plus `os.replace`, and adds a
  deterministic hierarchical sheet using a relative generated path.
- The complete post-write parse, selector construction, and semantic-diff
  attribution stay inside the write transaction. A failure restores the
  original parent bytes and removes the new child.
- Only `schematic.instantiate_module` is implemented. Future operations return
  `DESIGN_COMMAND_UNSUPPORTED`; module and footprint paths without a catalog
  return `MODULE_CATALOG_NOT_CONFIGURED` without filesystem probing.

## Fixture SHA

- `status-led.kicad_sch` raw bytes:
  `sha256:af9e40b8da5a63158a95cff1d222eb8c7ce57e05e88759df50dacfc775ccfe4e`
- The module test independently recomputes this raw SHA-256 and compares the
  literal manifest value. The fixture uses LF line endings.

## Verification

- RED: `python -m pytest tests/unit/test_schematic_modules.py -q` failed as
  expected before the adapter existed.
- GREEN: `python -m pytest tests/unit/test_schematic_modules.py -q` ->
  `8 passed`.
- Required focused suite: `python -m pytest tests/unit/test_schematic_modules.py
  tests/unit/test_schematic_semantic.py tests/unit/test_schematic_cst.py -q` ->
  `48 passed`.
- Full unit suite: `python -m pytest tests/unit -q` -> `111 passed`.
- `git diff --check` produced no whitespace errors.

## Tool Limitation

`kicad-cli` is not installed on this machine. The module fixture was not
generated or saved by a live KiCad 9 process, and no real KiCad 9 save or CLI
validation was performed. It is hand-authored to the KiCad 9 S-expression
contract and validated here only through the repository CST and semantic
parsers.
