from __future__ import annotations

import hashlib
from pathlib import Path


def component_yaml() -> bytes:
    return b"""schema_version: '1.0'
component_key: Acme:LED-0603-RED
revision: Rev-A
status: verified
name: 0603 red LED
manufacturer: Acme
part_number: LED-0603-RED
datasheet:
  path: datasheet.pdf
  media_type: application/pdf
  digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
pinout:
  path: pinout.json
  media_type: application/json
  digest: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
symbol:
  path: symbol.kicad_sym
  media_type: application/vnd.kicad.symbol
  digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
footprint:
  path: footprint.kicad_mod
  media_type: application/vnd.kicad.footprint
  digest: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
model_3d:
  path: model.step
  media_type: model/step
  digest: sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
"""


def build_component_directory(path: Path, *, include_model: bool) -> Path:
    path.mkdir(parents=True)
    assets = {
        "datasheet": (
            "datasheet.pdf",
            "application/pdf",
            b"%PDF-1.7\ncomponent datasheet\n",
        ),
        "pinout": (
            "pinout.json",
            "application/json",
            b'{"pins":[{"number":"1"}]}\n',
        ),
        "symbol": (
            "symbol.kicad_sym",
            "application/vnd.kicad.symbol",
            b"(kicad_symbol_lib (version 20231120) (generator pcbflow))\n",
        ),
        "footprint": (
            "footprint.kicad_mod",
            "application/vnd.kicad.footprint",
            b"(footprint \"LED_0603\")\n",
        ),
    }
    if include_model:
        assets["model_3d"] = (
            "model.step",
            "model/step",
            b"ISO-10303-21;\nEND-ISO-10303-21;\n",
        )

    for filename, _media_type, data in assets.values():
        (path / filename).write_bytes(data)

    lines = [
        "schema_version: '1.0'",
        "component_key: Acme:LED-0603-RED",
        "revision: Rev-A",
        "status: verified",
        "name: 0603 red LED",
        "manufacturer: Acme",
        "part_number: LED-0603-RED",
    ]
    for field_name, (filename, media_type, data) in assets.items():
        lines.extend(
            (
                f"{field_name}:",
                f"  path: {filename}",
                f"  media_type: {media_type}",
                f"  digest: sha256:{hashlib.sha256(data).hexdigest()}",
            )
        )
    (path / "component.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return path
