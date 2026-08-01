from __future__ import annotations


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
