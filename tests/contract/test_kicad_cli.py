from pcbflow.kicad import KicadCli


def test_kicad_locator_is_optional_and_returns_a_file_when_present() -> None:
    executable = KicadCli.locate()
    if executable is not None:
        assert executable.is_file()
