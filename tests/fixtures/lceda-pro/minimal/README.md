# Frozen Minimal LCEDA Pro Fixture

The official bridge contract must create, save, reopen, and read back one
native LCEDA Pro project with exactly the following semantic data:

- one board outline
- two copper layers
- one footprint
- one net
- one keepout
- one GND zone
- one mounting hole

The bridge may publish only explicitly verified `snapshot`,
`create_candidate`, and `apply_operations` capability after its documented
official CLI, API, or plugin identifies the tested version and preserves all
seven items after the complete create/save/reopen/snapshot readback cycle.
`run_drc` and `export_release` require their own official verification
evidence; this fixture does not prove either operation. This fixture
intentionally contains no guessed proprietary project archive.
