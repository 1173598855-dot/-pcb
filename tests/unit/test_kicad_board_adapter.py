from __future__ import annotations

from pathlib import Path

import pytest

from pcbflow.board.adapter import (
    CandidateWorkspace,
    UnsupportedEdaOperationError,
    semantic_diff,
)
from pcbflow.board.kicad_adapter import (
    KicadBoardAdapter,
    KicadBoardFormatError,
    _head,
    _point,
    _rect,
)
from pcbflow.kicad import RawValidationReport
from pcbflow.schematic.cst import CstAtom

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad" / "board-v10"


class FakeKicadCli:
    def validate(self, project_dir: Path, output_dir: Path) -> tuple[RawValidationReport, ...]:
        assert project_dir == FIXTURE
        assert output_dir.name == "reports"
        return (
            RawValidationReport(
                kind="drc",
                data=b'{"source":"minimal.kicad_pcb","violations":[]}',
                argv=("kicad-cli", "pcb", "drc"),
                returncode=0,
                tool_version="10.0.0",
                executable_digest="a" * 64,
                profile_id="kicad-10-v1",
                profile_revision=1,
            ),
        )


def _write_board(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fixture.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_snapshot_projects_geometry_nets_and_unknown_nodes() -> None:
    snapshot = KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(FIXTURE)

    assert snapshot.profile_id == "kicad-10-v1"
    assert [(point.x, point.y) for point in snapshot.outline] == [
        (0, 0), (20_000, 0), (20_000, 30_000), (0, 30_000)
    ]
    assert snapshot.layers == ("F.Cu", "B.Cu")
    assert [str(net.id) for net in snapshot.nets] == ["GND", "SIG"]
    assert snapshot.pads[0].net_id == "GND"
    assert (snapshot.pads[0].position.x, snapshot.pads[0].position.y) == (8_000, 21_000)
    assert snapshot.routes[0].net_id == "GND"
    assert snapshot.vias[0].net_id == "GND"
    assert snapshot.copper_zones[0].net_id == "GND"
    assert snapshot.footprints[0].placement_lock is True
    assert snapshot.routes[0].route_lock is True
    assert snapshot.vias[0].route_lock is False
    assert snapshot.copper_zones[0].route_lock is False
    assert snapshot.opaque_nodes[0].native_type == "gr_text"
    assert semantic_diff(snapshot, snapshot).is_empty


def test_unlocatable_unknown_native_node_is_rejected(tmp_path: Path) -> None:
    board = (FIXTURE / "minimal.kicad_pcb").read_text(encoding="utf-8")
    (tmp_path / "minimal.kicad_pcb").write_text(
        board.rstrip()[:-1] + "\n  (future_native_node (value 1))\n)\n",
        encoding="utf-8",
    )

    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_UNPRESERVABLE_NODE"):
        KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)


def test_adapter_is_read_only_and_runs_native_drc(tmp_path: Path) -> None:
    adapter = KicadBoardAdapter(kicad_cli=FakeKicadCli())
    snapshot = adapter.load_snapshot(FIXTURE)

    with pytest.raises(UnsupportedEdaOperationError, match="KICAD_BOARD_WRITE_NOT_IMPLEMENTED"):
        adapter.apply_operations(
            CandidateWorkspace(FIXTURE, tmp_path / "reports"), (), snapshot
        )

    reports = adapter.run_drc(CandidateWorkspace(FIXTURE, tmp_path / "reports"))
    assert len(reports) == 1
    assert reports[0].kind == "drc"
    assert reports[0].findings == ()
    assert not hasattr(adapter, "apply_operation")


def test_adapter_probe_delegates_to_the_kicad_port() -> None:
    capability = object()
    port = FakeKicadCli()
    port.probe = lambda: capability  # type: ignore[attr-defined]

    assert KicadBoardAdapter(kicad_cli=port).probe() is capability


def test_parser_head_returns_none_for_an_atom() -> None:
    assert _head(CstAtom("atom", False)) is None


def test_locked_parser_requires_an_explicit_true_value(tmp_path: Path) -> None:
    board = (FIXTURE / "minimal.kicad_pcb").read_text(encoding="utf-8")
    board = board.replace("(locked yes)", "(locked no)", 1)
    _write_board(tmp_path, board)

    snapshot = KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)

    assert snapshot.footprints[0].placement_lock is False


def test_parser_rejects_a_missing_point() -> None:
    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_INVALID_POINT"):
        _point(None)


def test_parser_rejects_empty_bounds() -> None:
    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_INVALID_BOUNDS"):
        _rect(())


def test_project_requires_exactly_one_board_file(tmp_path: Path) -> None:
    adapter = KicadBoardAdapter(kicad_cli=FakeKicadCli())

    with pytest.raises(
        KicadBoardFormatError, match="KICAD_BOARD_PROJECT_REQUIRES_ONE_BOARD"
    ):
        adapter.load_snapshot(tmp_path)


def test_project_rejects_an_invalid_board_root(tmp_path: Path) -> None:
    _write_board(tmp_path, "(not_a_kicad_board)\n")

    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_INVALID_ROOT"):
        KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)


def test_project_requires_at_least_one_copper_layer(tmp_path: Path) -> None:
    _write_board(
        tmp_path,
        """(kicad_pcb
  (version 20250101)
  (layers (36 \"B.SilkS\" user \"b.silkscreen\"))
)
""",
    )

    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_NO_COPPER_LAYERS"):
        KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)


def test_project_requires_a_supported_board_outline(tmp_path: Path) -> None:
    _write_board(
        tmp_path,
        """(kicad_pcb
  (version 20250101)
  (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal))
)
""",
    )

    with pytest.raises(KicadBoardFormatError, match="KICAD_BOARD_OUTLINE_NOT_SUPPORTED"):
        KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)


def test_footprint_without_graphic_bounds_uses_safe_minimum_and_via_rules(
    tmp_path: Path,
) -> None:
    _write_board(
        tmp_path,
        """(kicad_pcb
  (version 20250101)
  (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal))
  (setup
    (rules
      (min_clearance 0.2)
      (min_track_width 0.25)
      (min_via_size 0.3)
      (min_through_hole 0.6)
    )
  )
  (footprint \"NoRect\"
    (layer \"F.Cu\")
    (at 10 10)
    (uuid \"fp-no-rect\")
  )
  (gr_rect
    (start 0 0)
    (end 20 20)
    (layer \"Edge.Cuts\")
    (uuid \"edge\")
  )
)
""",
    )

    snapshot = KicadBoardAdapter(kicad_cli=FakeKicadCli()).load_snapshot(tmp_path)

    assert snapshot.footprints[0].width_um == 1
    assert snapshot.footprints[0].height_um == 1
    assert snapshot.net_classes[0].min_via_diameter_um == 300
    assert snapshot.net_classes[0].min_via_hole_um == 299


def test_read_only_adapter_rejects_candidate_creation_and_release(
    tmp_path: Path,
) -> None:
    adapter = KicadBoardAdapter(kicad_cli=FakeKicadCli())

    with pytest.raises(UnsupportedEdaOperationError, match="KICAD_BOARD_WRITE_NOT_IMPLEMENTED"):
        adapter.create_candidate(FIXTURE, tmp_path / "candidate")
    with pytest.raises(UnsupportedEdaOperationError, match="KICAD_BOARD_WRITE_NOT_IMPLEMENTED"):
        adapter.export_release(
            CandidateWorkspace(FIXTURE, tmp_path / "reports"),
            object(),  # type: ignore[arg-type]
        )
