from pathlib import Path

import pytest

from pcbflow.kicad import KicadCli
from pcbflow.process import ProcessRunner


@pytest.mark.kicad
def test_installed_kicad_reports_supported_version(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")

    capability = KicadCli(ProcessRunner(2_000_000), executable, 10).probe()

    assert capability.available
    assert capability.version is not None
    assert capability.version.startswith("9.")
