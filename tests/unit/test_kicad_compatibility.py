import pytest

from pcbflow.kicad_compatibility import (
    KicadOperationUnsupportedError,
    profile_by_id,
    profile_for_major,
    select_kicad_profile,
)


@pytest.mark.parametrize(
    ("version", "profile_id"),
    [
        ("9.0.0", "kicad-9-v1"),
        ("9.0.7", "kicad-9-v1"),
        ("10.0", "kicad-10-v1"),
        ("10.0.4", "kicad-10-v1"),
    ],
)
def test_selects_explicit_kicad_profile(version: str, profile_id: str) -> None:
    profile = select_kicad_profile(version)

    assert profile is not None
    assert profile.profile_id == profile_id


@pytest.mark.parametrize("version", ["8.0.7", "11.0.0", "10.0.0-rc1", "bad"])
def test_rejects_unverified_kicad_version(version: str) -> None:
    assert select_kicad_profile(version) is None


def test_profile_lookup_returns_immutable_profile() -> None:
    profile = profile_by_id("kicad-9-v1")

    assert profile is not None
    assert profile_for_major(9) == profile
    with pytest.raises(AttributeError):
        profile.major = 10


def test_profile_rejects_unknown_validation_operation(tmp_path) -> None:
    profile = select_kicad_profile("10.0.4")
    assert profile is not None

    with pytest.raises(KicadOperationUnsupportedError) as caught:
        profile.validation_argv(
            executable=tmp_path / "kicad-cli.exe",
            kind="sim",  # type: ignore[arg-type]
            design_file=tmp_path / "board.kicad_sch",
            report_file=tmp_path / "report.json",
        )

    assert caught.value.code == "KICAD_OPERATION_UNSUPPORTED"
