# KiCad Major-Version Compatibility Design

## Goal

Support KiCad 9.x and 10.x through explicit, test-backed compatibility
profiles, while making later KiCad major-version upgrades additive and
reviewable. Unknown major versions remain unsupported until their CLI and file
format contracts have passed the repository's real-tool verification gate.

## Scope

This change introduces the compatibility boundary, enables verified KiCad 9
and KiCad 10 profiles, and updates capability evidence, fixtures, tests, and
documentation. It does not download or install KiCad, launch GUI conversion,
modify registered source projects, or automatically trust future versions.

The existing SQLite schema, task model, REST API, CLI surface, artifact store,
and normalized validation report contracts remain unchanged unless a profile
identity must be added to an existing JSON evidence document.

## Compatibility Model

Add an immutable `KicadCompatibilityProfile` selected by parsed KiCad major
version. A profile defines:

- a stable profile identifier and profile revision, such as `kicad-10-v1`;
- the supported semantic version interval for that major release;
- supported schematic and PCB validation capabilities;
- the CLI argument contract for each supported validation operation;
- accepted native file-format versions for real-tool contract fixtures; and
- any version-specific output handling needed before producing the existing
  `RawValidationReport` representation.

The initial registry contains explicit profiles for KiCad 9.x and KiCad 10.x.
Profile lookup is exact by major version. A new major version is unsupported
until a profile and its contract evidence are committed; it is never accepted
because its version is merely greater than the latest known release.

The registry is internal to the KiCad adapter. Business services continue to
depend on the current `KicadPort` behavior and do not branch on KiCad versions.

## Probe And Execution Flow

`KicadCli.probe()` continues to locate the configured executable, hash it, and
execute `kicad-cli --version`. After parsing the complete version, it selects a
profile and validates the profile's supported version interval. The returned
capability retains the executable path, exact version, executable digest, and
availability reason, and also exposes the selected profile identity where
capability evidence is assembled.

Validation follows this sequence:

1. Require a successful probe and selected compatibility profile.
2. Discover the single supported schematic and PCB roots using existing path
   and ambiguity rules.
3. Validate that real-tool contract inputs use a native format accepted by the
   selected profile.
4. Ask the profile for the ERC or DRC command arguments.
5. Execute the command through the existing controlled process runner.
6. Parse the version-specific raw output into the existing normalized report
   contract.

No validation path converts files in place. The registered external
`source_path` remains immutable, and proposal validation continues to operate
only in isolated managed workspaces.

## Evidence And Reproducibility

Capability evidence records:

- the exact KiCad version;
- the executable digest;
- the stable compatibility profile identifier;
- the profile revision; and
- the validation command and execution result already captured by PCBFlow.

The profile revision changes whenever an accepted command, input-format, or
output-normalization rule changes materially. This keeps historical proposal
decisions attributable even when a later PCBFlow release revises support for
the same KiCad major version.

Existing review-digest construction remains deterministic. If profile fields
are added to an evidence document, they are included through the existing
canonical artifact digest rather than through a second, parallel digest path.

## Error Handling

The compatibility boundary preserves stable, machine-readable failures:

- missing executable: `kicad_cli_not_found`;
- unparseable version output: `version_unparseable`;
- known major outside its supported interval: `unsupported_version`;
- unknown major without a registered profile: `unsupported_version`;
- native file format incompatible with the active profile: a dedicated stable
  format-compatibility error rather than a generic ERC or DRC failure; and
- unsupported operation in a profile: a dedicated stable capability error.

The adapter does not silently fall back to another profile, retry through a
GUI application, rewrite the input, or treat a command failure as a successful
validation with findings.

## Fixture Strategy

Parser and CST golden fixtures remain independent of installed KiCad versions;
they verify PCBFlow's loss-preserving parse and controlled-edit behavior.

Real CLI contract fixtures are grouped by KiCad major version and are generated
or saved by that native KiCad release. A KiCad 10 contract never uses a partial
hand-authored KiCad 9 document as proof of compatibility. Shared semantic test
intent may be represented in both fixture families, but the native files remain
separate so each tool validates its own format.

The module catalog fixture used by a real proposal must likewise be loadable by
the active profile. Unit tests may continue to use minimal CST fixtures when no
real KiCad process is involved.

## Verification

Unit tests cover profile selection for KiCad 9 and 10, supported-version
boundaries, unknown major versions, unparseable versions, command generation,
and stable profile identity.

Integration tests verify that validation tasks and proposal capability evidence
carry the exact tool version, executable digest, profile ID, and profile
revision without changing the existing workflow state machine.

Real-tool contract tests are version-aware:

- an installed KiCad 10 must execute the KiCad 10 ERC/DRC and controlled-write
  contracts successfully;
- KiCad 9 contracts run when a KiCad 9 executable is available and otherwise
  skip explicitly; and
- a discovered unsupported major version must not be reported as supported.

The completion gate is the full non-tool test suite, at least 90 percent
coverage, successful KiCad 10 real-tool contracts on the current machine,
version-appropriate KiCad 9 results when available, CLI smoke tests, and a clean
`git diff --check`.

## Adding A Future KiCad Major Version

Supporting KiCad 11 or later requires all of the following in one reviewed
change:

1. Add a new explicit compatibility profile and supported version interval.
2. Add native real-tool fixtures produced by that KiCad major version.
3. Verify ERC, DRC, controlled schematic writes, and evidence output using the
   real CLI.
4. Add profile selection, boundary, and error tests.
5. Update the README compatibility matrix and development guide.

Until those steps pass, the new major version remains unavailable with the
stable `unsupported_version` reason. Minor and patch releases are accepted only
inside the interval declared by their existing profile and remain subject to
the same real-tool regression suite.

## Documentation

The README gains a concise compatibility matrix for supported KiCad majors,
fixture expectations, and local CLI configuration. The development guide gains
the profile contract, evidence fields, error behavior, fixture authoring rules,
and the exact checklist for enabling a new KiCad major release.
