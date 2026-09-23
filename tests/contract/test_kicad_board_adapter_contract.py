from __future__ import annotations

from pathlib import Path

from pcbflow.board.adapter import PcbEdaAdapter
from pcbflow.board.kicad_adapter import KicadBoardAdapter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad" / "board-v10"


class NoopKicadCli:
    def validate(self, project_dir: Path, output_dir: Path):
        return ()


def test_kicad_adapter_satisfies_the_board_adapter_contract_read_only() -> None:
    adapter: PcbEdaAdapter = KicadBoardAdapter(kicad_cli=NoopKicadCli())

    assert adapter.load_snapshot(FIXTURE).profile_id == "kicad-10-v1"
