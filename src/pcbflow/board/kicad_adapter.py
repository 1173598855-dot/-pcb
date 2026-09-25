from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn, cast

from pcbflow.eda import EdaCapability
from pcbflow.kicad import KicadPort, parse_kicad_report
from pcbflow.schematic.cst import CstAtom, CstList, CstNode, parse_cst

from .adapter import CandidateWorkspace, ReleaseArtifacts, UnsupportedEdaOperationError
from .ir import (
    BoardObjectId,
    BoardSnapshot,
    CopperZone,
    Footprint,
    Keepout,
    Net,
    NetClass,
    OpaqueNode,
    Pad,
    PointUm,
    RectUm,
    RouteSegment,
    Via,
)
from .operations import BoardOperation
from .rulepack import ManufacturingRulePack


class KicadBoardFormatError(ValueError):
    pass


def _head(node: CstNode) -> str | None:
    if isinstance(node, CstList) and node.items and isinstance(node.items[0], CstAtom):
        return node.items[0].value
    return None


def _child(node: CstList, name: str) -> CstList | None:
    return next((item for item in node.items[1:] if isinstance(item, CstList) and _head(item) == name), None)


def _children(node: CstList, name: str) -> Iterable[CstList]:
    return (item for item in node.items[1:] if isinstance(item, CstList) and _head(item) == name)


def _atoms(node: CstList | None) -> list[str]:
    if node is None:
        return []
    return [item.value for item in node.items[1:] if isinstance(item, CstAtom)]


def _locked(node: CstList) -> bool:
    """Read KiCad's explicit ``(locked yes|no)`` child value."""
    values = _atoms(_child(node, "locked"))
    return bool(values) and values[0].casefold() in {"yes", "true", "1"}


def _um(value: str) -> int:
    return round(float(value) * 1000)


def _point(node: CstList | None) -> PointUm:
    values = _atoms(node)
    if len(values) < 2:
        raise KicadBoardFormatError("KICAD_BOARD_INVALID_POINT")
    return PointUm(_um(values[0]), _um(values[1]))


def _id(node: CstList) -> BoardObjectId | None:
    for name in ("uuid", "tstamp"):
        values = _atoms(_child(node, name))
        if values:
            return BoardObjectId(values[0])
    return None


def _payload(node: CstNode) -> object:
    if isinstance(node, CstAtom):
        return node.value
    return [_payload(item) for item in node.items]


def _rect(points: Iterable[PointUm]) -> RectUm:
    values = tuple(points)
    if not values:
        raise KicadBoardFormatError("KICAD_BOARD_INVALID_BOUNDS")
    xs, ys = [p.x for p in values], [p.y for p in values]
    return RectUm(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class KicadBoardAdapter:
    """Read-only KiCad 10 board adapter. KiCad is never a second write target."""

    profile_id = "kicad-10-v1"

    def __init__(self, kicad_cli: KicadPort) -> None:
        self._kicad = kicad_cli

    def probe(self) -> EdaCapability:
        # Identity passthrough by contract (see test_adapter_probe_delegates_to_the_kicad_port);
        # the KicadPort protocol declares KicadCapability while the board adapter interface
        # widens it to EdaCapability.
        return cast(EdaCapability, self._kicad.probe())

    def load_snapshot(self, project_dir: Path) -> BoardSnapshot:
        paths = sorted(project_dir.glob("*.kicad_pcb"))
        if len(paths) != 1:
            raise KicadBoardFormatError("KICAD_BOARD_PROJECT_REQUIRES_ONE_BOARD")
        root = parse_cst(paths[0].read_bytes()).root
        if _head(root) != "kicad_pcb":
            raise KicadBoardFormatError("KICAD_BOARD_INVALID_ROOT")

        layer_node = _child(root, "layers")
        layers = tuple(
            atoms[0]
            for item in (layer_node.items[1:] if layer_node else ())
            if isinstance(item, CstList) and (atoms := _atoms(item)) and atoms[0].endswith(".Cu")
        )
        if not layers:
            raise KicadBoardFormatError("KICAD_BOARD_NO_COPPER_LAYERS")

        net_names = {values[0]: values[1] for node in _children(root, "net") if len(values := _atoms(node)) >= 2 and values[0] != "0"}
        setup = _child(root, "setup")
        rules = _child(setup, "rules") if setup else None
        def rule(name: str, default: float) -> int:
            if rules:
                return _um((_atoms(_child(rules, name)) or [str(default)])[0])
            return _um(str(default))
        min_via = rule("min_via_size", 0.6)
        min_hole = rule("min_through_hole", 0.3)
        if min_hole >= min_via:
            min_hole = max(1, min_via - 1)
        net_class_id = BoardObjectId("Default")
        net_classes = (NetClass(net_class_id, rule("min_track_width", 0.25), rule("min_clearance", 0.2), min_via, min_hole),)
        nets = tuple(Net(BoardObjectId(name), net_class_id) for name in net_names.values())

        footprints: list[Footprint] = []
        pads: list[Pad] = []
        routes: list[RouteSegment] = []
        vias: list[Via] = []
        zones: list[CopperZone] = []
        keepouts: list[Keepout] = []
        opaque: list[OpaqueNode] = []
        outline: tuple[PointUm, ...] = ()

        for node in (item for item in root.items[1:] if isinstance(item, CstList)):
            kind = _head(node) or ""
            native_id = _id(node)
            if kind == "footprint":
                if native_id is None:
                    self._unpreservable(kind)
                at = _atoms(_child(node, "at"))
                origin = _point(_child(node, "at"))
                angle = float(at[2]) if len(at) > 2 else 0.0
                layer = (_atoms(_child(node, "layer")) or ["F.Cu"])[0]
                pad_ids: list[BoardObjectId] = []
                bounds_points: list[PointUm] = []
                for pad_node in _children(node, "pad"):
                    pad_id = _id(pad_node)
                    if pad_id is None:
                        self._unpreservable("pad")
                    local = _point(_child(pad_node, "at"))
                    radians = math.radians(angle)
                    position = PointUm(
                        origin.x + round(math.cos(radians) * local.x - math.sin(radians) * local.y),
                        origin.y + round(math.sin(radians) * local.x + math.cos(radians) * local.y),
                    )
                    size = _atoms(_child(pad_node, "size"))
                    drill = _atoms(_child(pad_node, "drill"))
                    pad_layers = tuple(value for value in _atoms(_child(pad_node, "layers")) if value.endswith(".Cu"))
                    if "*.Cu" in pad_layers:
                        pad_layers = layers
                    net_values = _atoms(_child(pad_node, "net"))
                    pads.append(
                        Pad(
                            pad_id,
                            native_id,
                            BoardObjectId(net_names[net_values[0]])
                            if net_values and net_values[0] in net_names
                            else None,
                            position,
                            _um(size[0]),
                            _um(size[1]),
                            _um(drill[0]) if drill else 0,
                            pad_layers,
                        )
                    )
                    pad_ids.append(pad_id)
                    bounds_points.extend((PointUm(position.x - _um(size[0]) // 2, position.y - _um(size[1]) // 2), PointUm(position.x + _um(size[0]) // 2, position.y + _um(size[1]) // 2)))
                fp_rect = _child(node, "fp_rect")
                if fp_rect:
                    start, end = _point(_child(fp_rect, "start")), _point(_child(fp_rect, "end"))
                    width, height = abs(end.x - start.x), abs(end.y - start.y)
                else:
                    bounds = _rect(bounds_points or [origin, PointUm(origin.x + 1, origin.y + 1)])
                    width, height = max(1, bounds.width), max(1, bounds.height)
                footprints.append(Footprint(native_id, origin, width, height, layer, tuple(pad_ids), (), _locked(node)))
            elif kind == "segment":
                if native_id is None:
                    self._unpreservable(kind)
                net = (_atoms(_child(node, "net")) or [""])[0]
                routes.append(
                    RouteSegment(
                        native_id,
                        BoardObjectId(net_names[net]),
                        _point(_child(node, "start")),
                        _point(_child(node, "end")),
                        _um(_atoms(_child(node, "width"))[0]),
                        _atoms(_child(node, "layer"))[0],
                        _locked(node),
                    )
                )
            elif kind == "via":
                if native_id is None:
                    self._unpreservable(kind)
                net = (_atoms(_child(node, "net")) or [""])[0]
                vias.append(
                    Via(
                        native_id,
                        BoardObjectId(net_names[net]),
                        _point(_child(node, "at")),
                        _um(_atoms(_child(node, "size"))[0]),
                        _um(_atoms(_child(node, "drill"))[0]),
                        tuple(_atoms(_child(node, "layers"))),
                        _locked(node),
                    )
                )
            elif kind == "zone":
                if native_id is None:
                    self._unpreservable(kind)
                polygon = _child(node, "polygon")
                pts = tuple(_point(point) for point in _children(_child(polygon, "pts") or polygon, "xy")) if polygon else ()
                bounds = _rect(pts)
                keepout = _child(node, "keepout")
                zone_layers = tuple(_atoms(_child(node, "layers"))) or tuple(_atoms(_child(node, "layer")))
                if keepout:
                    prohibited = tuple(name for name in ("tracks", "vias", "pads", "copperpour", "footprints") if _child(keepout, name) is not None)
                    keepouts.append(Keepout(native_id, "zone", bounds, zone_layers, prohibited))
                else:
                    net = (_atoms(_child(node, "net")) or [""])[0]
                    connect_pads = _child(node, "connect_pads")
                    clearance = _atoms(_child(connect_pads, "clearance")) if connect_pads else []
                    zones.append(CopperZone(native_id, BoardObjectId(net_names[net]), zone_layers[0], bounds, _um(clearance[0]) if clearance else rule("min_clearance", 0.2), _locked(node)))
            elif kind == "gr_rect" and (_atoms(_child(node, "layer")) or [""])[0] == "Edge.Cuts":
                start, end = _point(_child(node, "start")), _point(_child(node, "end"))
                outline = (start, PointUm(end.x, start.y), end, PointUm(start.x, end.y))
            elif kind not in {"version", "generator", "generator_version", "general", "paper", "layers", "setup", "net"}:
                if native_id is None:
                    self._unpreservable(kind)
                opaque.append(OpaqueNode(native_id, kind, {"ast": _payload(node)}))

        if len(outline) < 4:
            raise KicadBoardFormatError("KICAD_BOARD_OUTLINE_NOT_SUPPORTED")
        return BoardSnapshot("1.0", self.profile_id, 1, outline, layers, net_classes, nets, tuple(keepouts), tuple(footprints), tuple(pads), tuple(routes), tuple(vias), tuple(zones), tuple(opaque))

    @staticmethod
    def _unpreservable(kind: str) -> NoReturn:
        raise KicadBoardFormatError(f"KICAD_BOARD_UNPRESERVABLE_NODE: {kind}")

    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace:
        raise UnsupportedEdaOperationError("KICAD_BOARD_WRITE_NOT_IMPLEMENTED")

    def apply_operations(self, candidate: CandidateWorkspace, operations: tuple[BoardOperation, ...], expected_snapshot: BoardSnapshot) -> BoardSnapshot:
        raise UnsupportedEdaOperationError("KICAD_BOARD_WRITE_NOT_IMPLEMENTED")

    def run_drc(self, candidate: CandidateWorkspace):
        return tuple(parse_kicad_report(report.kind, report.data) for report in self._kicad.validate(candidate.path, candidate.output_dir) if report.kind == "drc")

    def export_release(self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack) -> ReleaseArtifacts:
        raise UnsupportedEdaOperationError("KICAD_BOARD_WRITE_NOT_IMPLEMENTED")


__all__ = ["KicadBoardAdapter", "KicadBoardFormatError"]
