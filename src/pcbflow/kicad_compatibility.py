from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KicadCompatibilityProfile:
    profile_id: str
    revision: int
    major: int
    minimum: tuple[int, int, int]
    maximum_exclusive: tuple[int, int, int]
    schematic_format_versions: frozenset[int]
    pcb_format_versions: frozenset[int]
    validation_operations: frozenset[str]


_FORMAT_SCHEMATIC = frozenset({20231120, 20250114})
_FORMAT_PCB = frozenset({20240108, 20241229})
_VALIDATION_OPERATIONS = frozenset({"erc", "drc"})
_PROFILES = (
    KicadCompatibilityProfile(
        profile_id="kicad-9-v1",
        revision=1,
        major=9,
        minimum=(9, 0, 0),
        maximum_exclusive=(10, 0, 0),
        schematic_format_versions=_FORMAT_SCHEMATIC,
        pcb_format_versions=_FORMAT_PCB,
        validation_operations=_VALIDATION_OPERATIONS,
    ),
    KicadCompatibilityProfile(
        profile_id="kicad-10-v1",
        revision=1,
        major=10,
        minimum=(10, 0, 0),
        maximum_exclusive=(11, 0, 0),
        schematic_format_versions=_FORMAT_SCHEMATIC,
        pcb_format_versions=_FORMAT_PCB,
        validation_operations=_VALIDATION_OPERATIONS,
    ),
)
_PROFILES_BY_ID = {profile.profile_id: profile for profile in _PROFILES}
_PROFILES_BY_MAJOR = {profile.major: profile for profile in _PROFILES}
_VERSION = re.compile(r"\d+(?:\.\d+){1,2}\Z")


def _parse_version(version: str) -> tuple[int, int, int] | None:
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        return None
    components = tuple(int(component) for component in version.split("."))
    if len(components) == 2:
        return components[0], components[1], 0
    return components  # type: ignore[return-value]


def select_kicad_profile(version: str) -> KicadCompatibilityProfile | None:
    parsed = _parse_version(version)
    if parsed is None:
        return None
    for profile in _PROFILES:
        if profile.minimum <= parsed < profile.maximum_exclusive:
            return profile
    return None


def profile_by_id(profile_id: str) -> KicadCompatibilityProfile | None:
    return _PROFILES_BY_ID.get(profile_id)


def profile_for_major(major: int) -> KicadCompatibilityProfile | None:
    return _PROFILES_BY_MAJOR.get(major)
