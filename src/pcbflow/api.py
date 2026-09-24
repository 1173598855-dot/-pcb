from __future__ import annotations

import json
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, cast

from fastapi import FastAPI, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pcbflow.api_schemas import (
    AcceptProposalRequest,
    ActorRequest,
    ApprovalRequest,
    CancelTaskRequest,
    ConfigureEdaAuthorityRequest,
    CreateComponentModuleBindingRequest,
    CreateComponentRevisionRequest,
    CreatePcbCandidateRequest,
    CreateProjectRequest,
    DecidePcbG3Request,
    DecidePcbG4Request,
    ExportPcbReleaseRequest,
    RejectProposalRequest,
)
from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.commands import (
    Actor,
    DesignCommandSchemaError,
    load_command_batch,
)
from pcbflow.component_binding_store import ComponentModuleBindingConflictError
from pcbflow.component_bindings import (
    ModuleCatalogUnavailableError,
    ModuleKicadMajorUnsupportedError,
)
from pcbflow.component_store import ComponentRevisionNotFoundError
from pcbflow.config import Settings
from pcbflow.container import Container, build_container
from pcbflow.domain import RequestInvalidError, new_id, utc_now
from pcbflow.eda import (
    EdaAuthorityConflictError,
    validate_idempotency_key,
)
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.observability import bind_log_context
from pcbflow.pcb_candidates import (
    PcbCandidateNotFoundError,
    PcbCandidateNotReviewableError,
)
from pcbflow.proposal_store import ProposalNotFoundError
from pcbflow.proposals import (
    CandidateNotReviewableError,
    RevisionReconciliationRequiredError,
)
from pcbflow.proposals import (
    ProjectNotManagedError as ProposalProjectNotManagedError,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    RevisionConflictError,
    TaskNotCancellableError,
    TaskNotFoundError,
)
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.requirements import (
    RequirementsBlockedError,
    RequirementSet,
    RequirementSetPayload,
)
from pcbflow.revisions import (
    GitOperationError,
    ProjectWorktreeDirtyError,
)
from pcbflow.revisions import (
    ProjectNotManagedError as RevisionProjectNotManagedError,
)
from pcbflow.schematic.modules import ModuleRevisionNotFoundError


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


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > self._max_bytes:
                await self._reject(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope, receive, send) -> None:
        state = scope.get("state")
        correlation_id = (
            state.get("correlation_id") if isinstance(state, dict) else None
        )
        if not isinstance(correlation_id, str) or not correlation_id:
            correlation_id = new_id("cor")
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "REQUEST_BODY_TOO_LARGE",
                    "message": "request body exceeds the configured size limit",
                    "retryable": False,
                    "correlation_id": correlation_id,
                    "details": {"max_bytes": self._max_bytes},
                    "actions": ["reduce the request body size and retry"],
                }
            },
            headers={"X-Correlation-ID": correlation_id},
        )
        await response(scope, receive, send)


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


def _request_actor(
    request: Request, actor: ActorRequest | Actor
) -> tuple[str, str]:
    authenticated = getattr(request.state, "authenticated_actor", None)
    if authenticated is not None:
        return authenticated
    return actor.type, actor.id


def _with_authenticated_actor(batch, actor_type: str, actor_id: str):
    actor = batch.actor.model_copy(update={"type": actor_type, "id": actor_id})
    commands = tuple(
        command.model_copy(update={"actor": actor}) for command in batch.commands
    )
    return batch.model_copy(update={"actor": actor, "commands": commands})


def _mapped_domain_error(
    error: BaseException,
) -> tuple[int, str, str, bool, dict[str, object], list[str]]:
    if isinstance(error, LcedaProCapabilityError):
        return (
            422,
            error.code,
            "LCEDA Pro write capability is unverified",
            False,
            {},
            ["complete the verified official bridge contract before requesting writes"],
        )
    if isinstance(error, PcbCandidateNotReviewableError):
        return (
            409,
            "PCB_CANDIDATE_NOT_REVIEWABLE",
            "the PCB candidate is not reviewable",
            False,
            {},
            ["wait for a ready_for_g3 candidate and refresh its evidence"],
        )
    if isinstance(error, RequestInvalidError) and str(error) == "PCB_CANDIDATE_STALE":
        return (
            409,
            "PCB_CANDIDATE_STALE",
            "the PCB candidate base revision is stale",
            False,
            {},
            ["refresh the project revision and retry candidate creation"],
        )
    if isinstance(error, RequestInvalidError) and str(error) == "PCB_CAPABILITY_GATE_BLOCKED":
        return (
            422,
            "PCB_CAPABILITY_GATE_BLOCKED",
            "the PCB candidate capability gate is blocked",
            False,
            {},
            ["configure a frozen LCEDA Pro authority and verified BoardIR/capability evidence"],
        )
    if isinstance(error, RequestInvalidError) and str(error) in {
        "PCB_RELEASE_ALREADY_REQUESTED",
        "PCB_RELEASE_RETRY_KEY_REQUIRED",
    }:
        return (
            409,
            str(error),
            "the PCB release request conflicts with an existing release attempt",
            False,
            {},
            ["use the existing release task or provide a new idempotency key after failure"],
        )
    if isinstance(error, EdaAuthorityConflictError):
        return (
            409,
            "EDA_AUTHORITY_CONFLICT",
            "the project EDA authority is already immutable",
            False,
            {},
            ["reuse the frozen authority tuple for this project"],
        )
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
    if isinstance(error, TaskNotCancellableError):
        return (
            409,
            "TASK_NOT_CANCELLABLE",
            "the task is already in a terminal state",
            False,
            {"task_id": error.task_id, "status": error.status},
            ["inspect the task state instead of requesting cancellation"],
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
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=services.settings.max_api_body_bytes,
    )

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get("X-Correlation-ID") or new_id(
            "cor"
        )
        if services.settings.remote_mode and request.url.path != "/health":
            authorization = request.headers.get("Authorization", "")
            scheme, separator, provided_token = authorization.partition(" ")
            expected_token = services.settings.api_token
            if (
                scheme.lower() != "bearer"
                or not separator
                or not expected_token
                or not provided_token.isascii()
                or not compare_digest(provided_token, expected_token)
            ):
                response = _error_response(
                    request,
                    401,
                    "REMOTE_AUTH_REQUIRED",
                    "a valid bearer token is required in remote mode",
                    actions=["provide the configured bearer token and retry"],
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Correlation-ID"] = request.state.correlation_id
                return response
            request.state.authenticated_actor = (
                "service",
                services.settings.api_actor_id,
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
        RequestInvalidError,
        RequirementsBlockedError,
        ApprovalDigestMismatchError,
        RevisionConflictError,
        RevisionProjectNotManagedError,
        ProposalProjectNotManagedError,
        ProjectWorktreeDirtyError,
        CandidateNotReviewableError,
        PcbCandidateNotReviewableError,
        TaskNotCancellableError,
        RevisionReconciliationRequiredError,
        GitOperationError,
    ):
        app.add_exception_handler(exception_type, handle_domain_error)

    @app.exception_handler(PcbCandidateNotFoundError)
    async def handle_pcb_candidate_not_found(
        request: Request, error: PcbCandidateNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "PCB_CANDIDATE_NOT_FOUND", "PCB candidate not found"
        )

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

    @app.exception_handler(ModuleRevisionNotFoundError)
    async def handle_module_revision_not_found(
        request: Request, error: ModuleRevisionNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "MODULE_REVISION_NOT_FOUND", "module revision not found"
        )

    @app.exception_handler(ModuleCatalogUnavailableError)
    async def handle_module_catalog_unavailable(
        request: Request, error: ModuleCatalogUnavailableError
    ) -> JSONResponse:
        return _error_response(
            request,
            409,
            "MODULE_CATALOG_UNAVAILABLE",
            "module catalog is unavailable",
        )

    @app.exception_handler(ModuleKicadMajorUnsupportedError)
    async def handle_module_kicad_major_unsupported(
        request: Request, error: ModuleKicadMajorUnsupportedError
    ) -> JSONResponse:
        return _error_response(
            request,
            422,
            "MODULE_KICAD_MAJOR_UNSUPPORTED",
            "module does not support the requested KiCad major",
        )

    @app.exception_handler(ComponentModuleBindingConflictError)
    async def handle_component_module_binding_conflict(
        request: Request, error: ComponentModuleBindingConflictError
    ) -> JSONResponse:
        return _error_response(
            request,
            409,
            "COMPONENT_MODULE_BINDING_CONFLICT",
            "component revision already has a different module binding for this KiCad major",
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
        authority_input = payload.authority_input()
        validate_idempotency_key(idempotency_key)
        try:
            project, created = services.projects.create_with_status(
                payload.name,
                Path(payload.source_path),
                idempotency_key,
                authority_input,
            )
        except RequestInvalidError:
            raise
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

    @app.post("/api/v1/projects/{project_id}/pcb-candidates", status_code=202)
    def create_pcb_candidate(
        project_id: str,
        payload: CreatePcbCandidateRequest,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ):
        """Queue a BoardIR-only candidate; native LCEDA writes are never performed here."""
        candidates = getattr(services, "pcb_candidates", None)
        if candidates is None:
            raise ApiError(
                422,
                "PCB_CAPABILITY_GATE_BLOCKED",
                "PCB candidate service is unavailable",
                actions=["configure the candidate lifecycle service before retrying"],
            )
        try:
            result = candidates.create_from_public_inputs(
                project_id=project_id,
                seed=payload.seed,
                net_ids=payload.net_ids,
                board_snapshot_digest=payload.board_snapshot_digest,
                capability_digest=payload.capability_digest,
                idempotency_key=idempotency_key,
            )
        except LcedaProCapabilityError as error:
            raise ApiError(
                422,
                "PCB_CAPABILITY_GATE_BLOCKED",
                "LCEDA Pro write capability is unverified",
                actions=["run the LCEDA Pro capability probe and retry"],
            ) from error
        response.status_code = 202
        return jsonable_encoder(result)

    @app.get("/api/v1/pcb-candidates/{candidate_id}")
    def get_pcb_candidate(candidate_id: str):
        candidates = getattr(services, "pcb_candidates", None)
        if candidates is None:
            raise ApiError(422, "PCB_CAPABILITY_GATE_BLOCKED", "PCB candidate service is unavailable")
        return jsonable_encoder(candidates.get(candidate_id))

    @app.post("/api/v1/pcb-candidates/{candidate_id}:approve-g3")
    def decide_pcb_g3(
        candidate_id: str,
        payload: DecidePcbG3Request,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        approvals = getattr(services, "pcb_approvals", None)
        if approvals is None:
            raise ApiError(
                422,
                "PCB_CAPABILITY_GATE_BLOCKED",
                "PCB approval service is unavailable",
            )
        actor_type, actor_id = _request_actor(request, payload.actor)
        if actor_type != "human":
            raise RequestInvalidError("G3 decisions require a human actor")
        return jsonable_encoder(
            approvals.decide_g3(
                candidate_id=candidate_id,
                candidate_digest=payload.candidate_digest,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                decision=payload.decision,
                comment=payload.comment,
            )
        )

    @app.post("/api/v1/pcb-candidates/{candidate_id}:export-release", status_code=202)
    def export_pcb_release(
        candidate_id: str,
        payload: ExportPcbReleaseRequest,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ):
        del payload
        releases = getattr(services, "pcb_release", None)
        if releases is None:
            raise ApiError(422, "PCB_RELEASE_CAPABILITY_BLOCKED", "PCB release service is unavailable")
        task = releases.enqueue_export(candidate_id, idempotency_key)
        response.status_code = 202
        return jsonable_encoder(task)

    @app.post("/api/v1/pcb-candidates/{candidate_id}:approve-g4")
    def decide_pcb_g4(
        candidate_id: str,
        payload: DecidePcbG4Request,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ):
        approvals = getattr(services, "pcb_release_approvals", None)
        if approvals is None:
            raise ApiError(422, "PCB_RELEASE_CAPABILITY_BLOCKED", "G4 approval service is unavailable")
        actor_type, actor_id = _request_actor(request, payload.actor)
        if actor_type != "human":
            raise RequestInvalidError("G4 decisions require a human actor")
        return jsonable_encoder(
            approvals.decide_g4(
                candidate_id=candidate_id,
                manifest_digest=payload.manifest_digest,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                decision=payload.decision,
                comment=payload.comment,
            )
        )

    @app.post(
        "/api/v1/projects/{project_id}/eda-authority", status_code=201
    )
    def configure_eda_authority(
        project_id: str,
        payload: ConfigureEdaAuthorityRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        existing = services.eda_authorities.find_by_project_id(project_id)
        authority = services.eda_authorities.configure(
            project_id,
            payload.authority_input(),
            idempotency_key,
        )
        if existing is not None:
            response.status_code = 200
        return jsonable_encoder(authority)

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

    @app.post(
        "/api/v1/component-revisions/{component_revision_id}/module-bindings",
        status_code=201,
    )
    def create_component_module_binding(
        component_revision_id: str,
        payload: CreateComponentModuleBindingRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        existing = services.component_module_binding_store.find_by_idempotency_key(
            idempotency_key
        )
        binding = services.component_module_bindings.bind(
            component_revision_id,
            payload.kicad_major,
            payload.module_revision_id,
            idempotency_key,
        )
        response.status_code = 200 if existing is not None else 201
        return jsonable_encoder(binding)

    @app.get("/api/v1/component-revisions/{component_revision_id}/module-bindings")
    def list_component_module_bindings(component_revision_id: str):
        return jsonable_encoder(
            services.component_module_bindings.list_for_component_revision(
                component_revision_id
            )
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
        except (ValidationError, ValueError) as error:
            raise ApiError(
                422,
                "REQUIREMENTS_SCHEMA_INVALID",
                "requirement payload failed strict schema validation",
                details={
                    "fields": [
                        ".".join(
                            str(part)
                            for part in cast("list[object]", item["loc"])
                        )
                        for item in (
                            cast("list[dict[str, object]]", error.errors())
                            if isinstance(error, ValidationError)
                            else []
                        )[:32]
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
        request: Request,
        payload: ApprovalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        actor_type, actor_id = _request_actor(request, payload.actor)
        result = services.approvals.decide_g1(
            requirement_set_id=payload.subject_id,
            subject_digest=payload.subject_digest,
            decision=payload.decision,
            actor_type=actor_type,
            actor_id=actor_id,
            comment=payload.comment,
            idempotency_key=idempotency_key,
        )
        return _requirement_response(result)

    @app.post("/api/v1/projects/{project_id}/proposals", status_code=202)
    def create_proposal(
        project_id: str,
        request: Request,
        payload: dict[str, object],
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        try:
            data = canonical_json_bytes(payload)
            batch = load_command_batch(data)
        except (DesignCommandSchemaError, ValueError) as error:
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
        actor_type, actor_id = _request_actor(request, batch.actor)
        if actor_type != batch.actor.type or actor_id != batch.actor.id:
            batch = _with_authenticated_actor(batch, actor_type, actor_id)
            data = canonical_json_bytes(batch.model_dump(mode="json"))
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
        request: Request,
        payload: AcceptProposalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        actor_type, actor_id = _request_actor(request, payload.actor)
        return jsonable_encoder(
            services.proposal_decisions.accept(
                proposal_id=proposal_id,
                candidate_digest=payload.candidate_digest,
                actor_type=actor_type,
                actor_id=actor_id,
                comment=payload.comment,
                idempotency_key=idempotency_key,
            )
        )

    @app.post("/api/v1/proposals/{proposal_id}:reject")
    def reject_proposal(
        proposal_id: str,
        request: Request,
        payload: RejectProposalRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        actor_type, actor_id = _request_actor(request, payload.actor)
        return jsonable_encoder(
            services.proposal_decisions.reject(
                proposal_id=proposal_id,
                reason=payload.reason,
                actor_type=actor_type,
                actor_id=actor_id,
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

    @app.post(
        "/api/v1/projects/{project_id}/eda-capability-probes", status_code=202
    )
    def enqueue_lceda_capability_probe(
        project_id: str,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        return jsonable_encoder(
            services.capability_gate.enqueue(project_id, idempotency_key)
        )

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str):
        return jsonable_encoder(services.tasks.get(task_id))

    @app.post("/api/v1/tasks/{task_id}:cancel")
    def cancel_task(
        task_id: str,
        payload: CancelTaskRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
    ):
        del idempotency_key
        return jsonable_encoder(
            services.tasks.cancel(task_id, payload.reason, utc_now())
        )

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
