from __future__ import annotations

import logging

import pytest

from pcbflow.observability import (
    MetricKind,
    MetricName,
    Metrics,
    audit_payload,
    bind_log_context,
    log_event,
)


def test_metrics_aggregate_counters_and_durations_with_sorted_labels() -> None:
    metrics = Metrics()
    metrics.increment(
        MetricName.PROPOSAL_VALIDATION_TOTAL,
        labels={"result": "failed", "code": "CANDIDATE_VALIDATION_FAILED"},
    )
    metrics.observe(
        MetricName.PROPOSAL_EXECUTION_SECONDS,
        1.25,
        labels={"result": "failed"},
    )

    points = metrics.snapshot()
    validation = next(
        point
        for point in points
        if point.name is MetricName.PROPOSAL_VALIDATION_TOTAL
    )
    duration = next(
        point
        for point in points
        if point.name is MetricName.PROPOSAL_EXECUTION_SECONDS
    )
    assert validation.kind is MetricKind.COUNTER
    assert validation.labels == (
        ("code", "CANDIDATE_VALIDATION_FAILED"),
        ("result", "failed"),
    )
    assert validation.count == 1
    assert validation.total == 1.0
    assert duration.kind is MetricKind.HISTOGRAM
    assert duration.count == 1
    assert duration.total == 1.25
    assert duration.maximum == 1.25
    with pytest.raises(ValueError, match="unsupported metric label"):
        metrics.increment(
            MetricName.PROJECT_REVISION_CONFLICT_TOTAL,
            labels={"project_id": "prj_controller"},
        )


def test_structured_log_context_is_allowlisted_and_trace_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("pcbflow.observability.test")
    with caplog.at_level(logging.INFO), bind_log_context(
        project_id="prj_controller",
        requirement_set_id="reqset_controller",
        command_batch_id="bat_status_led",
        proposal_id="prop_status_led",
        base_revision="git:" + "1" * 40,
        candidate_revision="git:" + "2" * 40,
        task_id="tsk_status_led",
        trace_id="trc_test",
    ):
        log_event(logger, logging.INFO, "proposal.ready", result="pass")

    record = caplog.records[-1]
    assert record.event == "proposal.ready"
    assert record.project_id == "prj_controller"
    assert record.trace_id == "trc_test"
    assert record.result == "pass"
    assert "C:\\Users" not in record.getMessage()

    with pytest.raises(ValueError, match="unsupported log field"), bind_log_context(source_path="C:/secret/project"):
        pass


def test_audit_payload_contains_complete_reviewable_context() -> None:
    with bind_log_context(trace_id="trc_audit"):
        payload = audit_payload(
            actor_type="human",
            actor_id="local-user",
            action="proposal.accept",
            object_type="change_proposal",
            object_id="prop_1",
            before_digest="sha256:" + "a" * 64,
            after_digest="sha256:" + "b" * 64,
            result="accepted",
        )
    assert payload == {
        "schema_version": "1.0",
        "trace_id": "trc_audit",
        "actor": {"type": "human", "id": "local-user"},
        "action": "proposal.accept",
        "object": {"type": "change_proposal", "id": "prop_1"},
        "before_digest": "sha256:" + "a" * 64,
        "after_digest": "sha256:" + "b" * 64,
        "result": "accepted",
    }
