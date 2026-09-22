from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pcbflow.canonical import canonical_json_bytes, sha256_digest

from .ir import (
    BoardObjectId,
    JsonObject,
    NetClass,
    _decode_json,
    _find_by_id,
    _canonicalize_object_lists,
    _net_class_dict,
    _parse_net_class,
    _require_bool,
    _require_exact_fields,
    _require_int,
    _require_list,
    _require_string,
)


_V1_NET_CLASSES = frozenset(
    {"signal", "quiet_signal", "logic_power", "load_power", "ground"}
)


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    grid_step_um: int
    edge_clearance_um: int
    max_vias_per_net: int
    max_retry_rounds: int

    def __post_init__(self) -> None:
        _require_int(self.grid_step_um, "routing grid step", minimum=1, micrometres=True)
        _require_int(self.edge_clearance_um, "routing edge clearance", minimum=0, micrometres=True)
        _require_int(self.max_vias_per_net, "routing maximum vias", minimum=0)
        _require_int(self.max_retry_rounds, "routing retry rounds", minimum=1)


@dataclass(frozen=True, slots=True)
class CopperPolicy:
    edge_clearance_um: int
    stitching_pitch_um: int
    max_stitching_vias: int
    thermal_spoke_count: int
    thermal_spoke_width_um: int
    thermal_gap_um: int
    min_island_area_um2: int

    def __post_init__(self) -> None:
        _require_int(self.edge_clearance_um, "copper edge clearance", minimum=0, micrometres=True)
        _require_int(self.stitching_pitch_um, "stitching pitch", minimum=1, micrometres=True)
        _require_int(self.max_stitching_vias, "maximum stitching vias", minimum=0)
        _require_int(self.thermal_spoke_count, "thermal spoke count", minimum=1)
        _require_int(self.thermal_spoke_width_um, "thermal spoke width", minimum=1, micrometres=True)
        _require_int(self.thermal_gap_um, "thermal gap", minimum=0, micrometres=True)
        _require_int(self.min_island_area_um2, "minimum island area", minimum=1)


@dataclass(frozen=True, slots=True)
class ManufacturingRulePack:
    schema_version: str
    profile_id: str
    layer_count: int
    copper_oz: int
    max_board_width_um: int
    max_board_height_um: int
    max_voltage_mv: int
    net_classes: tuple[NetClass, ...]
    routing: RoutingPolicy
    copper: CopperPolicy
    high_voltage: bool
    rf_impedance_controlled: bool
    high_speed_differential: bool
    blind_buried_vias: bool
    unrestricted_full_board_routing: bool

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("unsupported rule pack schema version")
        _require_string(self.profile_id, "rule pack profile id")
        if self.layer_count != 2:
            raise ValueError("V1 rule pack requires exactly two layers")
        if self.copper_oz != 1:
            raise ValueError("V1 rule pack requires 1 oz copper")
        _require_int(
            self.max_board_width_um,
            "maximum board width",
            minimum=1,
            micrometres=True,
        )
        _require_int(
            self.max_board_height_um,
            "maximum board height",
            minimum=1,
            micrometres=True,
        )
        _require_int(self.max_voltage_mv, "maximum voltage", minimum=1)
        if self.max_voltage_mv > 24_000:
            raise ValueError("unsupported V1 scope: high voltage")
        flags = {
            "high_voltage": self.high_voltage,
            "rf_impedance_controlled": self.rf_impedance_controlled,
            "high_speed_differential": self.high_speed_differential,
            "blind_buried_vias": self.blind_buried_vias,
            "unrestricted_full_board_routing": self.unrestricted_full_board_routing,
        }
        for name, enabled in flags.items():
            _require_bool(enabled, f"scope {name}")
            if enabled:
                raise ValueError(f"unsupported V1 scope: {name}")
        if type(self.net_classes) is not tuple or any(
            not isinstance(item, NetClass) for item in self.net_classes
        ):
            raise TypeError("rule pack net classes must be a tuple of NetClass")
        ids = [str(item.id) for item in self.net_classes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate net class")
        if set(ids) != _V1_NET_CLASSES:
            raise ValueError("V1 rule pack requires the five fixed net classes")
        if not isinstance(self.routing, RoutingPolicy):
            raise TypeError("rule pack routing must be RoutingPolicy")
        if not isinstance(self.copper, CopperPolicy):
            raise TypeError("rule pack copper policy must be CopperPolicy")

    @classmethod
    def load_json(cls, data: bytes) -> Self:
        value = _require_exact_fields(
            _decode_json(data, "manufacturing rule pack"),
            frozenset(
                {
                    "schema_version",
                    "profile_id",
                    "layer_count",
                    "copper_oz",
                    "max_board_size_um",
                    "max_voltage_mv",
                    "net_classes",
                    "routing",
                    "copper",
                    "scope",
                }
            ),
            "manufacturing rule pack",
        )
        size = _require_exact_fields(
            value["max_board_size_um"],
            frozenset({"width", "height"}),
            "maximum board size",
        )
        scope = _require_exact_fields(
            value["scope"],
            frozenset(
                {
                    "high_voltage",
                    "rf_impedance_controlled",
                    "high_speed_differential",
                    "blind_buried_vias",
                    "unrestricted_full_board_routing",
                }
            ),
            "rule pack scope",
        )
        routing = _require_exact_fields(
            value["routing"],
            frozenset({"grid_step_um", "edge_clearance_um", "max_vias_per_net", "max_retry_rounds"}),
            "routing policy",
        )
        copper = _require_exact_fields(
            value["copper"],
            frozenset(
                {
                    "edge_clearance_um",
                    "stitching_pitch_um",
                    "max_stitching_vias",
                    "thermal_spoke_count",
                    "thermal_spoke_width_um",
                    "thermal_gap_um",
                    "min_island_area_um2",
                }
            ),
            "copper policy",
        )
        return cls(
            schema_version=_require_string(value["schema_version"], "schema version"),
            profile_id=_require_string(value["profile_id"], "profile id"),
            layer_count=_require_int(value["layer_count"], "layer count", minimum=1),
            copper_oz=_require_int(value["copper_oz"], "copper weight", minimum=1),
            max_board_width_um=_require_int(
                size["width"],
                "maximum board width",
                minimum=1,
                micrometres=True,
            ),
            max_board_height_um=_require_int(
                size["height"],
                "maximum board height",
                minimum=1,
                micrometres=True,
            ),
            max_voltage_mv=_require_int(
                value["max_voltage_mv"], "maximum voltage", minimum=1
            ),
            net_classes=tuple(
                _parse_net_class(item)
                for item in _require_list(value["net_classes"], "net classes")
            ),
            routing=RoutingPolicy(
                grid_step_um=_require_int(routing["grid_step_um"], "routing grid step", minimum=1, micrometres=True),
                edge_clearance_um=_require_int(routing["edge_clearance_um"], "routing edge clearance", minimum=0, micrometres=True),
                max_vias_per_net=_require_int(routing["max_vias_per_net"], "routing maximum vias", minimum=0),
                max_retry_rounds=_require_int(routing["max_retry_rounds"], "routing retry rounds", minimum=1),
            ),
            copper=CopperPolicy(
                edge_clearance_um=_require_int(copper["edge_clearance_um"], "copper edge clearance", minimum=0, micrometres=True),
                stitching_pitch_um=_require_int(copper["stitching_pitch_um"], "stitching pitch", minimum=1, micrometres=True),
                max_stitching_vias=_require_int(copper["max_stitching_vias"], "maximum stitching vias", minimum=0),
                thermal_spoke_count=_require_int(copper["thermal_spoke_count"], "thermal spoke count", minimum=1),
                thermal_spoke_width_um=_require_int(copper["thermal_spoke_width_um"], "thermal spoke width", minimum=1, micrometres=True),
                thermal_gap_um=_require_int(copper["thermal_gap_um"], "thermal gap", minimum=0, micrometres=True),
                min_island_area_um2=_require_int(copper["min_island_area_um2"], "minimum island area", minimum=1),
            ),
            high_voltage=_require_bool(scope["high_voltage"], "scope high voltage"),
            rf_impedance_controlled=_require_bool(
                scope["rf_impedance_controlled"], "scope RF impedance control"
            ),
            high_speed_differential=_require_bool(
                scope["high_speed_differential"], "scope high-speed differential"
            ),
            blind_buried_vias=_require_bool(
                scope["blind_buried_vias"], "scope blind or buried vias"
            ),
            unrestricted_full_board_routing=_require_bool(
                scope["unrestricted_full_board_routing"],
                "scope unrestricted full-board routing",
            ),
        )

    @property
    def max_board_size_um(self) -> tuple[int, int]:
        return self.max_board_width_um, self.max_board_height_um

    def net_class(self, object_id: str | BoardObjectId) -> NetClass:
        return _find_by_id(self.net_classes, object_id, "net class")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            _canonicalize_object_lists(self.to_canonical_dict(), ("net_classes",))
        )

    def canonical_digest(self) -> str:
        return sha256_digest(self.canonical_bytes())

    def to_canonical_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "layer_count": self.layer_count,
            "copper_oz": self.copper_oz,
            "max_board_size_um": {
                "width": self.max_board_width_um,
                "height": self.max_board_height_um,
            },
            "max_voltage_mv": self.max_voltage_mv,
            "net_classes": [_net_class_dict(item) for item in self.net_classes],
            "routing": {
                "grid_step_um": self.routing.grid_step_um,
                "edge_clearance_um": self.routing.edge_clearance_um,
                "max_vias_per_net": self.routing.max_vias_per_net,
                "max_retry_rounds": self.routing.max_retry_rounds,
            },
            "copper": {
                "edge_clearance_um": self.copper.edge_clearance_um,
                "stitching_pitch_um": self.copper.stitching_pitch_um,
                "max_stitching_vias": self.copper.max_stitching_vias,
                "thermal_spoke_count": self.copper.thermal_spoke_count,
                "thermal_spoke_width_um": self.copper.thermal_spoke_width_um,
                "thermal_gap_um": self.copper.thermal_gap_um,
                "min_island_area_um2": self.copper.min_island_area_um2,
            },
            "scope": {
                "high_voltage": self.high_voltage,
                "rf_impedance_controlled": self.rf_impedance_controlled,
                "high_speed_differential": self.high_speed_differential,
                "blind_buried_vias": self.blind_buried_vias,
                "unrestricted_full_board_routing": self.unrestricted_full_board_routing,
            },
        }


__all__ = ["CopperPolicy", "ManufacturingRulePack", "RoutingPolicy"]
