from pathlib import Path

import pytest

from pcbflow.board.adapter import CandidateWorkspace
from pcbflow.board.kicad_adapter import KicadBoardAdapter
from pcbflow.kicad import KicadCli
from pcbflow.process import ProcessRunner


def test_kicad_locator_is_optional_and_returns_a_file_when_present() -> None:
    executable = KicadCli.locate()
    if executable is not None:
        assert executable.is_file()


@pytest.mark.kicad
def test_real_kicad_runs_drc_for_v10_board_fixture(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")

    adapter = KicadBoardAdapter(KicadCli(ProcessRunner(2_000_000), executable, 120))
    fixture = Path(__file__).parents[1] / "fixtures" / "kicad" / "real" / "10" / "validation"
    reports = adapter.run_drc(CandidateWorkspace(fixture, tmp_path / "reports"))

    assert len(reports) == 1
    assert reports[0].kind == "drc"
