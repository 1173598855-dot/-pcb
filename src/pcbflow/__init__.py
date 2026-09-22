__version__ = "0.1.0"

# Suppress warnings for clean output
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Main PCB workflow exports
from pcbflow.pcb_workflow import CapabilityGateService, CapabilityProbeTaskHandler
from pcbflow.domain import EdaKind, EdaOperation, RequestInvalidError, Task, TaskLease, TaskStatus, utc_now
from pcbflow.eda import EdaCapability, validate_idempotency_key
from pcbflow.eda_authority_store import ProjectEdaAuthorityStore
from pcbflow.lceda_pro import LcedaProAdapter, LcedaProCapabilityError
from pcbflow.repositories import EvidenceRepository, ProjectRepository, TaskRepository
from pcbflow.artifacts import ContentAddressedStore
from pcbflow.canonical import canonical_json_bytes

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

__all__ = [
    # Version
    "__version__",
    # Core workflow
    "CapabilityGateService",
    "CapabilityProbeTaskHandler",
    # Domain
    "EdaKind",
    "EdaOperation",
    "RequestInvalidError",
    "Task",
    "TaskLease",
    "TaskStatus",
    "utc_now",
    # EDA base
    "EdaCapability",
    "validate_idempotency_key",
    # Authority store
    "ProjectEdaAuthorityStore",
    # LCEDA Pro
    "LcedaProAdapter",
    "LcedaProCapabilityError",
    # Repositories
    "EvidenceRepository",
    "ProjectRepository",
    "TaskRepository",
    # Artifacts
    "ContentAddressedStore",
    # Canonical
    "canonical_json_bytes",
    # Board adapter
    "BoardSemanticDiff",
    "BoardSemanticMismatchError",
    "CandidateWorkspace",
    "PcbEdaAdapter",
    "ReleaseArtifacts",
    "UnsupportedEdaOperationError",
    "all_opaque",
    "object_locks",
    "semantic_diff",
    # Board IR
    "BoardObjectId",
    "BoardSnapshot",
    "CopperZone",
    "Footprint",
    "Keepout",
    "Net",
    "NetClass",
    "OpaqueNode",
    "Pad",
    "PointUm",
    "RectUm",
    "RouteSegment",
    "Via",
]
