"""Compatibility facade for the historical pcb_candidates module.

The implementation now lives in pcb_candidate_validation, pcb_candidate_codec,
pcb_candidate_store, and pcb_candidate_execution. Import paths are preserved.
"""

from __future__ import annotations

from pcbflow.pcb_candidate_codec import (  # noqa: F401
    _boolean,
    _expect_keys,
    _ids,
    _integer,
    _list,
    _mapping,
    _point,
    _rect,
    _strings,
    deserialize_board_operations,
)
from pcbflow.pcb_candidate_execution import (  # noqa: F401
    PcbCandidateExecutionTaskHandler,
    _CandidateExecutionError,
    _existing_artifact_descriptor,
    _findings_payload,
    _is_blocking_finding,
    _load_candidate_rulepack,
    _unconnected_net_ids,
    _validate_candidate_operations,
)
from pcbflow.pcb_candidate_store import (  # noqa: F401
    PcbCandidateService,
    PcbCandidateStore,
    _candidate,
    _candidate_output_kind,
    _public_algorithm_evidence,
)
from pcbflow.pcb_candidate_validation import (  # noqa: F401
    G3_EVIDENCE_SET_KIND,
    G3_EVIDENCE_SET_MEDIA_TYPE,
    G3_REQUIRED_EVIDENCE,
    G3_REQUIRED_EVIDENCE_MEDIA_TYPES,
    PCB_EXPORT_RELEASE_TASK_KIND,
    PCB_GENERATE_CANDIDATE_TASK_KIND,
    PcbCandidate,
    PcbCandidateNotFoundError,
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
    _release_task_idempotency_key,
    _task_idempotency_key,
    pcb_candidate_review_digest,
    validate_candidate_digest,
    validate_candidate_public_inputs,
)

# Preserve the original public handler name while routing every new worker
# through the evidence-bound implementation.
PcbCandidateTaskHandler = PcbCandidateExecutionTaskHandler
