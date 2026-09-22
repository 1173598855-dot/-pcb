# KiCad Override Isolation Design

## Scope

`build_container()` accepts `kicad_override` so tests and embedding callers can
supply a deterministic `KicadPort`. The current construction path still invokes
`KicadCli.locate()` before selecting that override. This makes an injected path
depend on host executable discovery even though the discovered client is unused.

This change covers only dependency assembly. It does not change KiCad discovery,
the `KicadPort` protocol, runtime capability semantics, public API contracts, or
LCEDA capability behavior.

## Design

When `kicad_override` is present, assign it directly to `selected_kicad` and do
not construct `KicadCli`. When it is absent, preserve the existing `KicadCli`
construction, including configured executable resolution and size limits.

## Acceptance Criteria

1. A regression test proves `build_container(..., kicad_override=...)` does not
   call `KicadCli.locate()`.
2. The container exposes the injected port and the ordinary no-override path is
   unchanged.
3. Focused tests, the full coverage gate, `compileall`, and `git diff --check`
   succeed.
