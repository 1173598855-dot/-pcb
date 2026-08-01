from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.commands import DesignCommandSchemaError, load_command_batch
from pcbflow.component_store import ComponentRevisionNotFoundError
from pcbflow.config import Settings
from pcbflow.container import Container, build_container
from pcbflow.domain import new_id
from pcbflow.observability import bind_log_context
from pcbflow.proposal_store import ProposalNotFoundError
from pcbflow.proposals import (
    CandidateNotReviewableError,
    ProjectNotManagedError as ProposalProjectNotManagedError,
    RevisionReconciliationRequiredError,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    RevisionConflictError,
    TaskNotFoundError,
)
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.requirements import (
    RequirementSet,
    RequirementSetPayload,
    RequirementsBlockedError,
)
from pcbflow.revisions import (
    GitOperationError,
    ProjectNotManagedError as RevisionProjectNotManagedError,
    ProjectWorktreeDirtyError,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProjectRequest(StrictRequest):
    name: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


class CreateComponentRevisionRequest(StrictRequest):
    manifest_path: str = Field(min_length=1)


class ActorRequest(StrictRequest):
    type: Literal["human", "service"]
    id: str = Field(min_length=1)


class ApprovalRequest(StrictRequest):
    subject_type: Literal["requirement_set"]
    subject_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: ActorRequest
    comment: str = Field(min_length=1)


class AcceptProposalRequest(StrictRequest):
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actor: ActorRequest
    comment: str = Field(min_length=1)


class RejectProposalRequest(StrictRequest):
    actor: ActorRequest
    reason: str = Field(min_length=1)


class ApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
        actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.actions = actions or []


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, object] | None = None,
    actions: list[str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "correlation_id": request.state.correlation_id,
                "details": details or {},
                "actions": actions or [],
            }
        },
    )


def _requirement_response(requirement_set: RequirementSet) -> dict[str, object]:
    value = jsonable_encoder(requirement_set)
    value["subject_digest"] = (
        requirement_set.subject_digest()
        if requirement_set.candidate_revision is not None
        else None
    )
    return value


def _validation_code(request: Request) -> str:
    path = request.url.path
    if "/requirement-sets" in path:
        return "REQUIREMENTS_SCHEMA_INVALID"
    if "/proposals" in path:
        return "DESIGN_COMMAND_SCHEMA_INVALID"
    return "REQUEST_SCHEMA_INVALID"


def _validation_details(error: RequestValidationError) -> dict[str, object]:
    fields: list[str] = []
    for item in error.errors():
        location = item.get("loc", ())
        fields.append(".".join(str(part) for part in location))
    return {"fields": fields[:32]}


def _mapped_domain_error(
    error: BaseException,
) -> tuple[int, str, str, bool, dict[str, object], list[str]]:
    if isinstance(error, RequirementsBlockedError):
        return (
            422,
            "REQUIREMENTS_BLOCKED",
            "blocking requirement assumptions remain open",
            False,
            {"blocking_ids": list(error.blocking_ids)},
            ["resolve blocking assumptions before submission"],
        )
    if isinstance(error, ApprovalDigestMismatchError):
        return (
            409,
            "APPROVAL_DIGEST_MISMATCH",
            "the approval digest does not match the current candidate",
            False,
            {"expected_digest": error.expected, "provided_digest": error.actual},
            ["refresh the requirement or proposal and sign its current digest"],
        )
    if isinstance(error, RevisionConflictError):
        return (
            409,
            "PROJECT_REVISION_CONFLICT",
            "the project revision changed before this operation completed",
            False,
            {"expected_revision": error.expected, "actual_revision": error.actual},
            ["refresh the project and retry against its current revision"],
        )
    if isinstance(
        error, (RevisionProjectNotManagedError, ProposalProjectNotManagedError)
    ):
        return (
            409,
            "PROJECT_NOT_MANAGED",
            "the project must be adopted before controlled changes are allowed",
            False,
            {},
            ["adopt the project and retry"],
        )
    if isinstance(error, ProjectWorktreeDirtyError):
        return (
            409,
            "PROJECT_WORKTREE_DIRTY",
            "the project revision no longer matches its recorded snapshot",
            False,
            {},
            ["reconcile the project revision before retrying"],
        )
    if isinstance(error, CandidateNotReviewableError):
        return (
            409,
            "CANDIDATE_NOT_REVIEWABLE",
            "the proposal candidate is not reviewable",
            False,
            {},
            ["inspect the proposal and wait for a ready-for-review candidate"],
        )
    if isinstance(error, RevisionReconciliationRequiredError):
        return (
            409,
            "REVISION_RECONCILIATION_REQUIRED",
            "the project revision projections require reconciliation",
            False,
            {},
            ["reconcile the project and retry"],
        )
    if isinstance(error, GitOperationError):
        return (
            503,
            "GIT_OPERATION_FAILED",
            "the managed Git operation failed",
            True,
            {},
            ["retry the operation after checking repository availability"],
        )
    return (
        422,
        "REQUEST_INVALID",
        "the requested operation is invalid",
        False,
        {},
        ["review the request and retry"],
    )


def create_app(container: Container | None = None) -> FastAPI:
    services = container or build_container(Settings.from_env())
    app = FastAPI(title="pcbflow", version="0.1.0")
    app.state.container = services

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get(
            "X-Correlation-ID", new_id("cor")
        )
        with bind_log_context(trace_id=request.state.correlation_id):
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(
            request,
            error.status_code,
            error.code,
            str(error),
            retryable=error.retryable,
            details=error.details,
            actions=error.actions,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            422,
            _validation_code(request),
            "request body or headers failed strict schema validation",
            details=_validation_details(error),
            actions=["send a strict JSON request matching the endpoint schema"],
        )

    async def handle_domain_error(
        request: Request, error: BaseException
    ) -> JSONResponse:
        status, code, message, retryable, details, actions = _mapped_domain_error(error)
        return _error_response(
            request,
            status,
            code,
            message,
            retryable=retryable,
            details=details,
            actions=actions,
        )

    for exception_type in (
        RequirementsBlockedError,
        ApprovalDigestMismatchError,
        RevisionConflictError,
        RevisionProjectNotManagedError,
        ProposalProjectNotManagedError,
        ProjectWorktreeDirtyError,
        CandidateNotReviewableError,
        RevisionReconciliationRequiredError,
        GitOperationError,
    ):
        app.add_exception_handler(exception_type, handle_domain_error)

    @app.exception_handler(ProjectNotFoundError)
    async def handle_project_not_found(
        request: Request, error: ProjectNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "PROJECT_NOT_FOUND", "project not found"
        )

    @app.exception_handler(TaskNotFoundError)
    async def handle_task_not_found(
        request: Request, error: TaskNotFoundError
    ) -> JSONResponse:
        return _error_response(request, 404, "TASK_NOT_FOUND", "task not found")

    @app.exception_handler(RequirementSetNotFoundError)
    async def handle_requirement_not_found(
        request: Request, error: RequirementSetNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "REQUIREMENT_SET_NOT_FOUND", "requirement set not found"
        )

    @app.exception_handler(ProposalNotFoundError)
    async def handle_proposal_not_found(
        request: Request, error: ProposalNotFoundError
    ) -> JSONResponse:
        return _error_response(request, 404, "PROPOSAL_NOT_FOUND", "proposal not found")

    @app.exception_handler(ComponentRevisionNotFoundError)
    async def handle_component_revision_not_found(
        request: Request, error: ComponentRevisionNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request,
            404,
            "COMPONENT_REVISION_NOT_FOUND",
            "component revision not found",
        )

    @app.exception_handler(IdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, error: IdempotencyConflictError
    ) -> JSONResponse:
        return _error_response(
            request,
            409,
            "IDEMPOTENCY_CONFLICT",
            "the operation conflicts with an existing idempotent result",
            details={},
            actions=["reuse the original request or choose a new idempotency key"],
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/projects", status_code=201)
    def create_project(
        payload: CreateProjectRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        if services.settings.remote_mode:
            raise ApiError(
                403,
                "LOCAL_SOURCE_PATHS_DISABLED",
                "local source paths are disabled in remote mode",
            )
        try:
            project, created = services.projects.create_with_status(
                payload.name,
                Path(payload.source_path),
                idempotency_key,
            )
        except (OSError, ValueError) as error:
            raise ApiError(
                422,
                "PROJECT_SOURCE_INVALID",
                "project source must be an existing local directory",
                details={"field": "source_path"},
                actions=["provide an existing local project directory"],
            ) from error
        response.status_code = 201 if created else 200
        return jsonable_encoder(project)

    @app.get("/api/v1/projects")
    def list_projects():
        return jsonable_encoder(services.projects.list())

    @app.post("/api/v1/component-revisions", status_code=201)
    def import_component_revision(
        payload: CreateComponentRevisionRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        if services.settings.remote_mode:
            raise ApiError(
                403,
                "LOCAL_SOURCE_PATHS_DISABLED",
                "local source paths are disabled in remote mode",
            )
        existing = services.component_store.find_by_idempotency_key(idempotency_key)
        try:
            revision = services.components.import_revision(
                Path(payload.manifest_path), idempotency_key
            )
        except ValueError as error:
            raise ApiError(
                422,
                "COMPONENT_MANIFEST_INVALID",
                "component manifest or evidence is invalid",
                actions=["provide a verified local component manifest"],
            ) from error
        response.status_code = 200 if existing is not None else 201
        return jsonable_encoder(revision)

    @app.get("/api/v1/component-revisions/{component_revision_id}")
    def get_component_revision(component_revision_id: str):
        return jsonable_encoder(services.component_store.get(component_revision_id))

    @app.get("/api/v1/component-revisions")
    def list_component_revisions(component_key: str):
        return jsonable_encoder(
            services.component_store.list_for_component(component_key)
        )

    @app.post("/api/v1/projects/{project_id}:adopt")
    def adopt_project(
        project_id: str,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        project = services.revisions.adopt(project_id, idempotency_key)
        return jsonable_encoder(project)

    @app.post("/api/v1/projects/{project_id}/requirement-sets", status_code=201)
    def import_requirements(
        project_id: str,
        payload: dict[str, object],
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        try:
            validated = RequirementSetPayload.model_validate_json(
                canonical_json_bytes(payload), strict=True
            )
        except ValidationError as error:
            raise ApiError(
                422,
                "REQUIREMENTS_SCHEMA_INVALID",
                "requirement payload failed strict schema validation",
                details={
                    "fields": [
                        ".".join(str(part) for part in item["loc"])
                        for item in error.errors()[:32]
                    ]
                },
                actions=["send a JSON requirement payload matching schema 1.0"],
            ) from error
        existing = services.requirement_store.find_by_import_key(
            project_id, idempotency_key
        )
        result = services.requirements.import_draft(
            project_id,
            canonical_json_bytes(validated.model_dump(mode="json")),
            idempotency_key,
        )
        response.status_code = 200 if existing is not None else 201
        return _requirement_response(result)

    @app.get("/api/v1/requirement-sets/{requirement_set_id}")
    def get_requirement_set(requirement_set_id: str):
        return _requirement_response(services.requirement_store.get(requirement_set_id))

    @app.post("/api/v1/requirement-sets/{requirement_set_id}:submit")
    def submit_requirements(
        requirement_set_id: str,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        return _requirement_response(
            services.requirements.submit(requirement_set_id, idempotency_key)
        )

    @app.post("/api/v1/approvals")
    def decide_approval(
        payload: ApprovalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        result = services.approvals.decide_g1(
            requirement_set_id=payload.subject_id,
            subject_digest=payload.subject_digest,
            decision=payload.decision,
            actor_type=payload.actor.type,
            actor_id=payload.actor.id,
            comment=payload.comment,
            idempotency_key=idempotency_key,
        )
        return _requirement_response(result)

    @app.post("/api/v1/projects/{project_id}/proposals", status_code=202)
    def create_proposal(
        project_id: str,
        payload: dict[str, object],
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        data = canonical_json_bytes(payload)
        try:
            batch = load_command_batch(data)
        except DesignCommandSchemaError as error:
            raise ApiError(
                422,
                "DESIGN_COMMAND_SCHEMA_INVALID",
                "design command batch failed strict schema validation",
                details={},
                actions=["send a command batch matching schema 1.0"],
            ) from error
        if batch.project_id != project_id:
            raise ApiError(
                422,
                "DESIGN_COMMAND_SCHEMA_INVALID",
                "command batch project does not match the request path",
                details={},
                actions=["use the same project ID in the path and body"],
            )
        existing = services.proposal_store.find_existing(batch)
        result = services.proposals.create(data, idempotency_key)
        response.status_code = 200 if existing is not None else 202
        return jsonable_encoder(result)

    @app.get("/api/v1/proposals/{proposal_id}")
    def get_proposal(proposal_id: str):
        return jsonable_encoder(services.proposal_store.get(proposal_id))

    @app.get("/api/v1/proposals/{proposal_id}/diff")
    def get_proposal_diff(proposal_id: str):
        proposal = services.proposal_store.get(proposal_id)
        if proposal.semantic_diff_digest is None:
            raise ApiError(
                409,
                "CANDIDATE_NOT_REVIEWABLE",
                "the proposal has no semantic diff",
                actions=["wait for proposal execution to produce review evidence"],
            )
        try:
            if not services.artifacts.verify(proposal.semantic_diff_digest):
                raise ValueError("artifact verification failed")
            with services.artifacts.open(proposal.semantic_diff_digest) as artifact:
                raw = artifact.read()
            return json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ApiError(
                422,
                "KICAD_PARSE_FAILED",
                "the stored semantic diff could not be decoded",
                actions=["re-run proposal execution to regenerate evidence"],
            ) from error

    @app.post("/api/v1/proposals/{proposal_id}:accept")
    def accept_proposal(
        proposal_id: str,
        payload: AcceptProposalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        return jsonable_encoder(
            services.proposal_decisions.accept(
                proposal_id=proposal_id,
                candidate_digest=payload.candidate_digest,
                actor_type=payload.actor.type,
                actor_id=payload.actor.id,
                comment=payload.comment,
                idempotency_key=idempotency_key,
            )
        )

    @app.post("/api/v1/proposals/{proposal_id}:reject")
    def reject_proposal(
        proposal_id: str,
        payload: RejectProposalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        return jsonable_encoder(
            services.proposal_decisions.reject(
                proposal_id=proposal_id,
                reason=payload.reason,
                actor_type=payload.actor.type,
                actor_id=payload.actor.id,
                idempotency_key=idempotency_key,
            )
        )

    @app.post("/api/v1/projects/{project_id}/validations", status_code=202)
    def enqueue_validation(
        project_id: str,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        return jsonable_encoder(
            services.validation.enqueue(project_id, idempotency_key)
        )

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str):
        return jsonable_encoder(services.tasks.get(task_id))

    @app.post("/api/v1/worker:run-once")
    def run_worker_once() -> dict[str, bool]:
        if services.settings.remote_mode:
            raise ApiError(
                403,
                "REMOTE_WORKER_DISABLED",
                "worker execution is disabled in remote mode",
                actions=["run the worker in the local PCBFlow process"],
            )
        return {"handled": services.worker.run_once()}

    @app.get("/api/v1/projects/{project_id}/evidence")
    def list_evidence(project_id: str):
        services.projects.get(project_id)
        return jsonable_encoder(services.evidence.list_for_project(project_id))

    @app.get("/api/v1/projects/{project_id}/findings")
    def list_findings(project_id: str):
        services.projects.get(project_id)
        return jsonable_encoder(services.findings.list_for_project(project_id))

    return app
