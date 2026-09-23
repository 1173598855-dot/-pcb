__version__ = "0.1.0"

# Suppress warnings for clean output
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Main PCB workflow exports
from pcbflow.artifacts import ContentAddressedStore

# Board adapter exports
from pcbflow.board.adapter import (
    BoardSemanticDiff,
    BoardSemanticMismatchError,
    CandidateWorkspace,
    PcbEdaAdapter,
    ReleaseArtifacts,
    UnsupportedEdaOperationError,
    all_opaque,
    object_locks,
    semantic_diff,
)

# Board IR exports
from pcbflow.board.ir import (
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
from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    RequestInvalidError,
    Task,
    TaskLease,
    TaskStatus,
    utc_now,
)
from pcbflow.eda import EdaCapability, validate_idempotency_key
from pcbflow.eda_authority_store import ProjectEdaAuthorityStore
from pcbflow.kicad_export import (
    KicadExportError,
    KicadExportRequest,
    KicadExportResult,
    KicadExportUnavailableError,
    KicadManufacturingExporter,
)
from pcbflow.lceda_pro import LcedaProAdapter, LcedaProCapabilityError
from pcbflow.pcb_workflow import CapabilityGateService, CapabilityProbeTaskHandler
from pcbflow.release_packaging import (
    PackagedRelease,
    ReleasePackageError,
    package_kicad_release,
)
from pcbflow.repositories import EvidenceRepository, ProjectRepository, TaskRepository

__all__ = [
    # Board IR
    "BoardObjectId",
    # Board adapter
    "BoardSemanticDiff",
    "BoardSemanticMismatchError",
    "BoardSnapshot",
    "CandidateWorkspace",
    # Core workflow
    "CapabilityGateService",
    "CapabilityProbeTaskHandler",
    # Artifacts
    "ContentAddressedStore",
    "CopperZone",
    # EDA base
    "EdaCapability",
    # Domain
    "EdaKind",
    "EdaOperation",
    # Repositories
    "EvidenceRepository",
    "Footprint",
    "Keepout",
    # KiCad export
    "KicadExportError",
    "KicadExportRequest",
    "KicadExportResult",
    "KicadExportUnavailableError",
    "KicadManufacturingExporter",
    # LCEDA Pro
    "LcedaProAdapter",
    "LcedaProCapabilityError",
    "Net",
    "NetClass",
    "OpaqueNode",
    # Release packaging
    "PackagedRelease",
    "Pad",
    "PcbEdaAdapter",
    "PointUm",
    # Authority store
    "ProjectEdaAuthorityStore",
    "ProjectRepository",
    "RectUm",
    "ReleaseArtifacts",
    "ReleasePackageError",
    "RequestInvalidError",
    "RouteSegment",
    "Task",
    "TaskLease",
    "TaskRepository",
    "TaskStatus",
    "UnsupportedEdaOperationError",
    "Via",
    # Version
    "__version__",
    "all_opaque",
    # Canonical
    "canonical_json_bytes",
    "object_locks",
    "package_kicad_release",
    "semantic_diff",
    "utc_now",
    "validate_idempotency_key",
]
