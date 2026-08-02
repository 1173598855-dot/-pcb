from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ComponentModuleBinding:
    id: str
    component_revision_id: str
    kicad_major: int
    module_revision_id: str
    module_manifest_digest: str
    idempotency_key: str
    created_at: datetime
