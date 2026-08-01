# Versioned KiCad CLI Fixtures

These fixtures are repository-owned copies used by the optional real-tool
contracts. Parser goldens under tests/fixtures/kicad/golden/ are separate and
are never treated as native KiCad CLI input. Each directory is selected only
after the KiCad version probe resolves to its explicit compatibility profile.

The local fixture copies are deterministic. KiCad 9 uses the 20240108 PCB
format; KiCad 10 uses the 20241229 PCB format. The schematic format is
20250114 for both verified profiles.
