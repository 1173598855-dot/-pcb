from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pcbflow.tables import Base


class RequirementSetRow(Base):
    __tablename__ = "requirement_sets"
    __table_args__ = (
        Index("ix_requirement_sets_project_status", "project_id", "status"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_requirement_set_key"
        ),
        UniqueConstraint(
            "project_id",
            "submission_idempotency_key",
            name="uq_requirement_submission_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    submission_idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    base_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    canonical_artifact_digest: Mapped[str] = mapped_column(
        ForeignKey("artifacts.digest"), nullable=False
    )
    candidate_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    candidate_snapshot_digest: Mapped[str | None] = mapped_column(
        String(71), nullable=True
    )
    frozen_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProjectRevisionRow(Base):
    __tablename__ = "project_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "revision", name="uq_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snapshot_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    requirement_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("requirement_sets.id"), nullable=True
    )
    command_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectEdaAuthorityRow(Base):
    __tablename__ = "project_eda_authorities"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_project_eda_authority_replay",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    eda_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    eda_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    board_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rulepack_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GateDecisionRow(Base):
    __tablename__ = "gate_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_gate_decision_key"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    gate: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    base_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DesignCommandBatchRow(Base):
    __tablename__ = "design_command_batches"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_command_batch_key"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    requirement_set_id: Mapped[str] = mapped_column(
        ForeignKey("requirement_sets.id"), nullable=False
    )
    base_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    commands_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    canonical_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DesignCommandRow(Base):
    __tablename__ = "design_commands"
    __table_args__ = (
        UniqueConstraint("batch_id", "ordinal", name="uq_command_ordinal"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_design_command_key"
        ),
        UniqueConstraint("batch_id", "id", name="uq_batch_command_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("design_command_batches.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_digest: Mapped[str] = mapped_column(String(71), nullable=False)


class ChangeProposalRow(Base):
    __tablename__ = "change_proposals"
    __table_args__ = (
        Index("ix_change_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    command_batch_id: Mapped[str] = mapped_column(
        ForeignKey("design_command_batches.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    candidate_snapshot_digest: Mapped[str | None] = mapped_column(
        String(71), nullable=True
    )
    review_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    semantic_diff_digest: Mapped[str | None] = mapped_column(
        String(71), nullable=True
    )
    evidence_set_digest: Mapped[str | None] = mapped_column(
        String(71), nullable=True
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PcbCandidateRow(Base):
    __tablename__ = "pcb_candidates"
    __table_args__ = (
        Index("ix_pcb_candidates_project_status", "project_id", "status"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_pcb_candidate_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    base_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    base_snapshot_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    board_snapshot_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    rulepack_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    capability_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    authority_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    operations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    operations_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    algorithm_evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    accepted_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_unprocessed", "processed_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
