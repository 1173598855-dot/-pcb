from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pcbflow.commands import load_command_batch
from pcbflow.container import build_container
from pcbflow.design_tables import (
    ChangeProposalRow,
    DesignCommandBatchRow,
    DesignCommandRow,
    OutboxEventRow,
)
from pcbflow.domain import TaskStatus
from pcbflow.observability import bind_log_context
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import TaskRow


def _batch(project, requirement_set) -> dict[str, object]:
    actor = {"type": "human", "id": "local-user"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_set_status_value",
        "project_id": project.id,
        "base_revision": project.current_revision,
        "requirement_set_id": requirement_set.id,
        "idempotency_key": "proposal:set-status-value",
        "actor": actor,
        "intent": "Set the status indicator value",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_set_status_value",
                "batch_id": "bat_set_status_value",
                "project_id": project.id,
                "base_revision": project.current_revision,
                "idempotency_key": "proposal:set-status-value:1",
                "actor": actor,
                "intent": "Set the status indicator value",
                "risk": "low",
                "preconditions": [
                    {
                        "type": "project.revision_equals",
                        "revision": project.current_revision,
                    }
                ],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": {
                            "kind": "symbol",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000002",
                            "pin_number": None,
                        },
                        "property_name": "Value",
                        "value": "GREEN",
                        "expected_old_value": "LED",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }


def test_create_persists_batch_commands_task_proposal_and_outbox_once(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    data = json.dumps(value, separators=(",", ":")).encode()

    with bind_log_context(trace_id="trc_command_batch"):
        first = container.proposals.create(data, value["idempotency_key"])
    repeated = container.proposals.create(data, value["idempotency_key"])

    assert repeated.id == first.id
    assert container.command_batches.get(first.command_batch_id) == load_command_batch(
        data
    )
    task = container.tasks.get(first.task_id)
    assert task.status is TaskStatus.QUEUED
    assert task.payload == {
        "proposal_id": first.id,
        "command_batch_id": first.command_batch_id,
        "project_id": first.project_id,
        "trace_id": "trc_command_batch",
    }
    with container.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DesignCommandBatchRow)) == 1
        assert session.scalar(select(func.count()).select_from(DesignCommandRow)) == 1
        assert session.scalar(select(func.count()).select_from(ChangeProposalRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRow)) >= 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) >= 1
        events = session.scalars(
            select(OutboxEventRow).where(
                OutboxEventRow.aggregate_type == "change_proposal",
                OutboxEventRow.aggregate_id == first.id,
                OutboxEventRow.event_type == "proposal.queued",
            )
        ).all()

    assert len(events) == 1
    payload = events[0].payload_json
    assert payload["trace_id"] == "trc_command_batch"
    assert payload["actor"] == {"type": "human", "id": "local-user"}
    assert payload["action"] == "proposal.create"
    assert payload["object"] == {"type": "change_proposal", "id": first.id}
    assert payload["result"] == "queued"
    assert payload["command_batch_id"] == first.command_batch_id
    assert payload["task_id"] == first.task_id


@pytest.mark.parametrize("change", ["intent", "risk", "command payload"])
def test_create_rejects_same_key_with_different_canonical_input(
    container, frozen_requirement_set, change: str
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    key = str(value["idempotency_key"])
    container.proposals.create(json.dumps(value).encode(), key)
    if change == "intent":
        value["intent"] = "Set a different status indicator value"
    elif change == "risk":
        value["risk"] = "medium"
    else:
        commands = value["commands"]
        assert isinstance(commands, list)
        command = commands[0]
        assert isinstance(command, dict)
        operation = command["operation"]
        assert isinstance(operation, dict)
        payload = operation["payload"]
        assert isinstance(payload, dict)
        payload["value"] = "RED"

    with pytest.raises(IdempotencyConflictError):
        container.proposals.create(json.dumps(value).encode(), key)


def test_create_replays_existing_proposal_after_project_revision_advances(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    data = json.dumps(value).encode()

    first = container.proposals.create(data, str(value["idempotency_key"]))
    advanced = container.projects.compare_and_set_revision(
        project.id,
        expected_revision=project.current_revision,
        new_revision="git:" + "f" * 40,
        snapshot_digest="sha256:" + "0" * 64,
        expected_version=project.version,
    )

    assert advanced.current_revision != project.current_revision
    repeated = container.proposals.create(data, str(value["idempotency_key"]))

    assert repeated == first


def test_create_rejects_reusing_a_command_key_in_a_different_batch(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    first = _batch(project, frozen_requirement_set)
    container.proposals.create(
        json.dumps(first).encode(), str(first["idempotency_key"])
    )
    second = json.loads(json.dumps(first))
    second["batch_id"] = "bat_set_status_value_second"
    second["idempotency_key"] = "proposal:set-status-value:second"
    commands = second["commands"]
    assert isinstance(commands, list)
    command = commands[0]
    assert isinstance(command, dict)
    command["batch_id"] = second["batch_id"]
    command["command_id"] = "cmd_set_status_value_second"

    with pytest.raises(IdempotencyConflictError, match="proposal:set-status-value:1"):
        container.proposals.create(
            json.dumps(second).encode(), str(second["idempotency_key"])
        )


def test_command_batch_get_rejects_tampered_invalid_json_shape(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    proposal = container.proposals.create(
        json.dumps(value).encode(), str(value["idempotency_key"])
    )

    with container.sessions.begin() as session:
        row = session.get(DesignCommandBatchRow, proposal.command_batch_id)
        assert row is not None
        row.commands_json = [{}]

    with pytest.raises(RuntimeError, match="^stored command batch is invalid$"):
        container.command_batches.get(proposal.command_batch_id)


def test_command_batch_get_rejects_tampered_canonical_digest(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    proposal = container.proposals.create(
        json.dumps(value).encode(), str(value["idempotency_key"])
    )

    with container.sessions.begin() as session:
        row = session.get(DesignCommandBatchRow, proposal.command_batch_id)
        assert row is not None
        row.canonical_digest = "sha256:" + "0" * 64

    with pytest.raises(
        RuntimeError, match="^stored command batch digest mismatch$"
    ):
        container.command_batches.get(proposal.command_batch_id)


def test_create_concurrent_replay_serializes_before_inserting_batch(
    container, frozen_requirement_set, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    data = json.dumps(value).encode()
    first_begin_started = Event()
    second_begin_attempted = Event()
    release_first = Event()
    calls_lock = Lock()
    begin_calls = 0
    original_execute = Session.execute

    def pause_first_begin(session, statement, *args, **kwargs):
        nonlocal begin_calls
        if getattr(statement, "text", None) == "BEGIN IMMEDIATE":
            with calls_lock:
                begin_calls += 1
                begin_number = begin_calls
            if begin_number == 1:
                result = original_execute(session, statement, *args, **kwargs)
                first_begin_started.set()
                assert release_first.wait(timeout=5)
                return result
            elif begin_number == 2:
                second_begin_attempted.set()
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", pause_first_begin)
    second_container = build_container(container.settings)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                container.proposals.create, data, str(value["idempotency_key"])
            )
            assert first_begin_started.wait(timeout=5)
            second = executor.submit(
                second_container.proposals.create,
                data,
                str(value["idempotency_key"]),
            )

            assert second_begin_attempted.wait(timeout=5)
            release_first.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)

        assert first_result.id == second_result.id
        with container.sessions() as session:
            batches = session.scalar(
                select(func.count()).select_from(DesignCommandBatchRow)
            )
            proposals = session.scalar(
                select(func.count()).select_from(ChangeProposalRow)
            )
            assert batches == proposals == 1
    finally:
        release_first.set()
        second_container.dispose()
