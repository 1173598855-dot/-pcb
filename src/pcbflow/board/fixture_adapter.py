from __future__ import annotations

import json
from pathlib import Path

from pcbflow.domain import EdaOperation, NormalizedFinding, ValidationReport
from pcbflow.workspaces import WorkspaceCopier

from .adapter import (
    BoardSemanticMismatchError,
    CandidateWorkspace,
    ReleaseArtifacts,
    UnsupportedEdaOperationError,
    all_opaque,
    object_locks,
    semantic_diff,
)
from .ir import BoardSnapshot
from .operations import (
    AddGroundStitching,
    BoardOperation,
    CreateCopperZones,
    PlaceFootprints,
    RouteNets,
    ThermalPolicy,
)
from .rulepack import ManufacturingRulePack
from .validation import BoardRuleChecker


class FixtureBoardAdapter:
    """Deterministic BoardIR adapter used for algorithm and contract tests."""

    def __init__(
        self,
        *,
        extra_native_change: bool = False,
        drc_findings: tuple[NormalizedFinding, ...] = (),
    ) -> None:
        self._extra_native_change = extra_native_change
        self._drc_findings = tuple(drc_findings)
        self._copier = WorkspaceCopier(max_files=100_000, max_bytes=512 * 1024 * 1024)

    def probe(self):
        from pcbflow.eda import EdaCapability

        return EdaCapability(
            available=True,
            executable=None,
            version="fixture",
            executable_digest=None,
            profile_id="fixture-boardir-v1",
            profile_revision=1,
            operations=frozenset(EdaOperation),
            write_verified=True,
            reason=None,
        )

    def load_snapshot(self, project_dir: Path) -> BoardSnapshot:
        path = _snapshot_path(project_dir)
        return BoardSnapshot.load_json(path.read_bytes())

    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace:
        source = source_dir.resolve(strict=True)
        destination = destination_dir.resolve(strict=False)
        if destination == source or source in destination.parents:
            raise ValueError("candidate destination must be isolated from source")
        self._copier.copy(source, destination)
        output_dir = destination / "output"
        output_dir.mkdir()
        return CandidateWorkspace(path=destination, output_dir=output_dir)

    def apply_operations(
        self,
        candidate: CandidateWorkspace,
        operations: tuple[BoardOperation, ...],
        expected_snapshot: BoardSnapshot,
    ) -> BoardSnapshot:
        current = self.load_snapshot(candidate.path)
        if current != expected_snapshot:
            diff = semantic_diff(expected_snapshot, current)
            raise BoardSemanticMismatchError("candidate changed before operations", diff)
        proposed = expected_snapshot
        allowed_ids: set[str] = set()
        for operation in operations:
            if not isinstance(operation, RouteNets):
                allowed_ids.update(str(item) for item in operation.target_object_ids)
            if isinstance(operation, PlaceFootprints):
                placements = {str(item.footprint_id): item for item in operation.placements}
                allowed_ids.update(placements)
                data = proposed.to_canonical_dict()
                deltas: dict[str, tuple[int, int]] = {}
                for item in data["footprints"]:  # type: ignore[index]
                    placement = placements.get(item["id"])  # type: ignore[index]
                    if placement is not None:
                        previous = item["position"]  # type: ignore[index]
                        deltas[item["id"]] = (  # type: ignore[index]
                            placement.position.x - previous["x"],  # type: ignore[index]
                            placement.position.y - previous["y"],  # type: ignore[index]
                        )
                        item["position"] = {"x": placement.position.x, "y": placement.position.y}  # type: ignore[index]
                        item["layer"] = placement.layer  # type: ignore[index]
                for pad in data["pads"]:  # type: ignore[index]
                    delta = deltas.get(pad["footprint_id"])  # type: ignore[index]
                    if delta is not None:
                        allowed_ids.add(pad["id"])  # type: ignore[index]
                        position = pad["position"]  # type: ignore[index]
                        pad["position"] = {  # type: ignore[index]
                            "x": position["x"] + delta[0],  # type: ignore[index]
                            "y": position["y"] + delta[1],  # type: ignore[index]
                        }
                proposed = BoardSnapshot.load_json(json.dumps(data, separators=(",", ":")).encode())
            elif isinstance(operation, RouteNets):
                data = proposed.to_canonical_dict()
                routes = {str(item["id"]): item for item in data["routes"]}  # type: ignore[index]
                removed = {str(item) for item in operation.removed_route_ids}
                for identifier in removed:
                    route = routes.get(identifier)
                    if route is None:
                        raise ValueError(f"route removal target is missing: {identifier}")
                    if route["route_lock"]:  # type: ignore[index]
                        raise ValueError(f"route removal target is locked: {identifier}")
                existing_ids = {
                    str(item["id"])
                    for collection in ("net_classes", "nets", "keepouts", "footprints", "pads", "routes", "vias", "copper_zones", "opaque_nodes")
                    for item in data[collection]  # type: ignore[index]
                } - removed
                additions = operation.segments + operation.vias
                for item in additions:
                    if str(item.id) in existing_ids:
                        raise ValueError(f"route addition identifier collides: {item.id}")
                data["routes"] = [item for item in data["routes"] if str(item["id"]) not in removed] + [  # type: ignore[index]
                    {
                        "id": str(item.id), "net_id": str(item.net_id),
                        "start": {"x": item.start.x, "y": item.start.y}, "end": {"x": item.end.x, "y": item.end.y},
                        "width_um": item.width_um, "layer": item.layer, "route_lock": item.route_lock,
                    } for item in operation.segments
                ]
                data["vias"] = data["vias"] + [  # type: ignore[index]
                    {
                        "id": str(item.id), "net_id": str(item.net_id),
                        "position": {"x": item.position.x, "y": item.position.y},
                        "diameter_um": item.diameter_um, "hole_diameter_um": item.hole_diameter_um,
                        "layers": list(item.layers), "route_lock": item.route_lock,
                    } for item in operation.vias
                ]
                allowed_ids.update(str(item) for item in operation.removed_route_ids)
                allowed_ids.update(str(item.id) for item in additions)
                proposed = BoardSnapshot.load_json(json.dumps(data, separators=(",", ":")).encode())
            elif isinstance(operation, CreateCopperZones):
                data = proposed.to_canonical_dict()
                existing_ids = _existing_ids(data)
                for zone in operation.zones:
                    if str(zone.id) in existing_ids:
                        raise ValueError(f"copper zone identifier collides: {zone.id}")
                pads = {str(item["id"]): item for item in data["pads"]}  # type: ignore[index]
                for policy in operation.thermal_policies:
                    pad = pads.get(str(policy.pad_id))
                    if pad is None:
                        raise ValueError(f"thermal policy pad is missing: {policy.pad_id}")
                    if pad["net_id"] != str(policy.net_id):
                        raise ValueError(
                            f"thermal policy net does not match pad: {policy.pad_id}"
                        )
                    payload = _thermal_policy_payload(policy)
                    if (
                        pad.get("thermal_policy") is not None
                        and pad["thermal_policy"] != payload
                    ):
                        raise ValueError(
                            f"thermal policy is locked: {policy.pad_id}"
                        )
                    pad["thermal_policy"] = payload
                data["copper_zones"] = data["copper_zones"] + [  # type: ignore[index]
                    {
                        "id": str(zone.id),
                        "net_id": str(zone.net_id),
                        "layer": zone.layer,
                        "bounds": {
                            "x": zone.bounds.x,
                            "y": zone.bounds.y,
                            "width": zone.bounds.width,
                            "height": zone.bounds.height,
                        },
                        "clearance_um": zone.clearance_um,
                        "route_lock": zone.route_lock,
                    }
                    for zone in operation.zones
                ]
                allowed_ids.update(str(item.id) for item in operation.zones)
                allowed_ids.update(
                    str(item.pad_id) for item in operation.thermal_policies
                )
                proposed = BoardSnapshot.load_json(json.dumps(data, separators=(",", ":")).encode())
            elif isinstance(operation, AddGroundStitching):
                data = proposed.to_canonical_dict()
                existing_ids = _existing_ids(data)
                for via in operation.vias:
                    if str(via.id) in existing_ids:
                        raise ValueError(f"ground stitching via identifier collides: {via.id}")
                data["vias"] = data["vias"] + [  # type: ignore[index]
                    {
                        "id": str(via.id),
                        "net_id": str(via.net_id),
                        "position": {"x": via.position.x, "y": via.position.y},
                        "diameter_um": via.diameter_um,
                        "hole_diameter_um": via.hole_diameter_um,
                        "layers": list(via.layers),
                        "route_lock": via.route_lock,
                    }
                    for via in operation.vias
                ]
                allowed_ids.update(str(item.id) for item in operation.vias)
                proposed = BoardSnapshot.load_json(json.dumps(data, separators=(",", ":")).encode())
            else:
                raise UnsupportedEdaOperationError(f"fixture adapter does not implement {operation.operation_type}")
        _snapshot_path(candidate.path).write_bytes(json.dumps(proposed.to_canonical_dict(), separators=(",", ":")).encode())
        if self._extra_native_change:
            self._inject_native_change(candidate.path)
        actual = self.load_snapshot(candidate.path)
        diff = semantic_diff(expected_snapshot, actual)
        locks_before = object_locks(expected_snapshot)
        locks_after = object_locks(actual)
        lock_changed = tuple(sorted(key for key in locks_before.keys() & locks_after.keys() if locks_before[key] != locks_after[key]))
        opaque_changed = all_opaque(expected_snapshot) != all_opaque(actual)
        unexpected = (
            set(diff.unexpected_object_ids) | set(diff.changed_object_ids)
        ) - allowed_ids
        unexpected.update(lock_changed)
        if opaque_changed:
            unexpected.add("<opaque>")
        if unexpected:
            mismatch = type(diff)(diff.changed_object_ids, tuple(sorted(unexpected)))
            raise BoardSemanticMismatchError("candidate contains unannounced native changes", mismatch)
        return actual

    def run_drc(self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack | None = None) -> tuple[ValidationReport, ...]:
        findings = self._drc_findings
        if rulepack is not None:
            findings = findings + BoardRuleChecker().check(self.load_snapshot(candidate.path), rulepack)
        return (ValidationReport(kind="drc", findings=findings),)

    def export_release(self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack) -> ReleaseArtifacts:
        findings = self.run_drc(candidate, rulepack)[0]
        if findings.findings:
            raise BoardSemanticMismatchError("cannot export a board with DRC findings")
        output = candidate.output_dir / "boardir.json"
        output.write_bytes(json.dumps(self.load_snapshot(candidate.path).to_canonical_dict(), indent=2).encode())
        return ReleaseArtifacts(files=(("boardir", output),))

    @staticmethod
    def _inject_native_change(project_dir: Path) -> None:
        path = _snapshot_path(project_dir)
        data = json.loads(path.read_bytes())
        if data["opaque_nodes"]:
            data["opaque_nodes"][0]["payload"]["unexpected_native_change"] = True
        else:
            data["profile_id"] = "fixture-boardir-mutated"
        path.write_bytes(json.dumps(data, separators=(",", ":")).encode())


def _snapshot_path(project_dir: Path) -> Path:
    for name in ("expected-boardir.json", "boardir.json"):
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"BoardIR fixture not found in {project_dir}")


def _existing_ids(data: dict[str, object]) -> set[str]:
    collections = (
        "net_classes",
        "nets",
        "keepouts",
        "footprints",
        "pads",
        "routes",
        "vias",
        "copper_zones",
        "opaque_nodes",
    )
    return {
        str(item["id"])
        for collection in collections
        for item in data[collection]  # type: ignore[index]
    }


def _thermal_policy_payload(policy: ThermalPolicy) -> dict[str, object]:
    return {
        "pad_id": str(policy.pad_id),
        "net_id": str(policy.net_id),
        "layers": list(policy.layers),
        "style": policy.style,
        "spoke_count": policy.spoke_count,
        "spoke_width_um": policy.spoke_width_um,
        "gap_um": policy.gap_um,
        "locked": policy.locked,
    }


__all__ = ["FixtureBoardAdapter"]
