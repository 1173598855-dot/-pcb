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
    }
    with container.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DesignCommandBatchRow)) == 1
        assert session.scalar(select(func.count()).select_from(DesignCommandRow)) == 1
        assert session.scalar(select(func.count()).select_from(ChangeProposalRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRow)) >= 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) >= 1


def test_create_rejects_same_key_with_different_canonical_input(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    key = str(value["idempotency_key"])
    container.proposals.create(json.dumps(value).encode(), key)
    value["intent"] = "A different intent"

    with pytest.raises(IdempotencyConflictError):
        container.proposals.create(json.dumps(value).encode(), key)


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
