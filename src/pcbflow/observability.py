from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from pcbflow.domain import new_id

_LOG_FIELDS = frozenset(
    {
        "project_id",
        "requirement_set_id",
        "command_batch_id",
        "proposal_id",
        "base_revision",
        "candidate_revision",
        "task_id",
        "trace_id",
        "error_code",
        "adapter_contract",
        "result",
    }
)
_METRIC_LABELS = frozenset({"result", "code", "contract"})
_context: ContextVar[dict[str, str]] = ContextVar(
    "pcbflow_log_context", default={}
)


def _fields(values: Mapping[str, object]) -> dict[str, str]:
    unknown = set(values) - _LOG_FIELDS
    if unknown:
        raise ValueError(f"unsupported log field: {sorted(unknown)[0]}")
    return {key: str(value) for key, value in values.items() if value is not None}


@contextmanager
def bind_log_context(**values: object) -> Iterator[None]:
    token = _context.set({**_context.get(), **_fields(values)})
    try:
        yield
    finally:
        _context.reset(token)


def ensure_trace_id() -> str:
    current = _context.get()
    if trace_id := current.get("trace_id"):
        return trace_id
    trace_id = new_id("trc")
    _context.set({**current, "trace_id": trace_id})
    return trace_id


def log_event(
    logger: logging.Logger, level: int, event: str, **values: object
) -> None:
    context = {**_context.get(), **_fields(values)}
    context.setdefault("trace_id", ensure_trace_id())
    logger.log(level, event, extra={"event": event, **context})


def audit_payload(
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    before_digest: str | None,
    after_digest: str | None,
    result: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "trace_id": ensure_trace_id(),
        "actor": {"type": actor_type, "id": actor_id},
        "action": action,
        "object": {"type": object_type, "id": object_id},
        "before_digest": before_digest,
        "after_digest": after_digest,
        "result": result,
    }


class MetricName(StrEnum):
    PROPOSAL_EXECUTION_SECONDS = "proposal_execution_duration_seconds"
    SCHEMATIC_PARSE_SECONDS = "schematic_parse_duration_seconds"
    KICAD_ERC_SECONDS = "kicad_erc_duration_seconds"
    PROPOSAL_VALIDATION_TOTAL = "proposal_validation_total"
    PROJECT_REVISION_CONFLICT_TOTAL = "project_revision_conflict_total"
    PROPOSAL_REVIEW_WAIT_SECONDS = "proposal_review_wait_seconds"
    GIT_REF_RECONCILIATION_RETRY_TOTAL = "git_ref_reconciliation_retry_total"
    ADAPTER_EXECUTION_TOTAL = "adapter_contract_execution_total"


class MetricKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: MetricName
    kind: MetricKind
    labels: tuple[tuple[str, str], ...]
    count: int
    total: float
    maximum: float


@dataclass(slots=True)
class _Aggregate:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0


class Metrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[
            tuple[MetricName, MetricKind, tuple[tuple[str, str], ...]], _Aggregate
        ] = {}

    @staticmethod
    def _labels(
        labels: Mapping[str, str] | None,
    ) -> tuple[tuple[str, str], ...]:
        values = labels or {}
        unknown = set(values) - _METRIC_LABELS
        if unknown:
            raise ValueError(f"unsupported metric label: {sorted(unknown)[0]}")
        return tuple(sorted((str(key), str(value)) for key, value in values.items()))

    def _record(
        self,
        name: MetricName,
        kind: MetricKind,
        value: float,
        labels: Mapping[str, str] | None,
    ) -> None:
        if value < 0:
            raise ValueError("metric value must be non-negative")
        key = (name, kind, self._labels(labels))
        with self._lock:
            aggregate = self._values.setdefault(key, _Aggregate())
            aggregate.count += 1
            aggregate.total += value
            aggregate.maximum = max(aggregate.maximum, value)

    def increment(
        self,
        name: MetricName,
        value: float = 1.0,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, MetricKind.COUNTER, value, labels)

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, MetricKind.HISTOGRAM, value, labels)

    def snapshot(self) -> tuple[MetricPoint, ...]:
        with self._lock:
            points = [
                MetricPoint(
                    name=name,
                    kind=kind,
                    labels=labels,
                    count=value.count,
                    total=value.total,
                    maximum=value.maximum,
                )
                for (name, kind, labels), value in self._values.items()
            ]
        return tuple(
            sorted(
                points,
                key=lambda point: (point.name.value, point.kind.value, point.labels),
            )
        )
