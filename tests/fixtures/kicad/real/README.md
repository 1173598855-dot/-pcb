# Versioned KiCad CLI Fixtures

These fixtures are repository-owned copies used by the optional real-tool
contracts. Parser goldens under tests/fixtures/kicad/golden/ are separate and
are never treated as native KiCad CLI input. Each directory is selected only
after the KiCad version probe resolves to its explicit compatibility profile.

The local fixture copies are deterministic. The versioned validation projects
use the 20250114 schematic format; KiCad 9 uses the 20240108 PCB format and
KiCad 10 uses the 20241229 PCB format.

The KiCad 10 `compatible-legacy` and `controlled-write` projects are frozen
copies of the `simulation/up-down-counter` example distributed with KiCad
10.0.4 at `share/kicad/demos/simulation/up-down-counter`. They intentionally
retain schematic format 20231120 and generator metadata `eeschema` 7.99 to
verify KiCad 10 backward compatibility. The controlled-write contract mutates
only a temporary copy before running the real KiCad 10 ERC command.

Copied on 2026-08-01. The two repository copies have identical source bytes:

- `up-down-c.kicad_sch`: `sha256:0741a19a6367b8ac5be788ad4b801b2c3ee04214fae8e6652c8f2a28d4662063`
- `up-down-c.kicad_pro`: `sha256:58c8491dfc050df8a1a12635ee5dfb979bd63beadbec51ca2c06732c38e5ac46`
