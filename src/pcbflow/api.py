from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pcbflow.config import Settings
from pcbflow.container import Container, build_container
from pcbflow.domain import new_id
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    TaskNotFoundError,
)


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


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


def create_app(container: Container | None = None) -> FastAPI:
    services = container or build_container(Settings.from_env())
    app = FastAPI(title="pcbflow", version="0.1.0")
    app.state.container = services

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get(
            "X-Correlation-ID", new_id("cor")
        )
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

    @app.exception_handler(ProjectNotFoundError)
    async def handle_project_not_found(
        request: Request, error: ProjectNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "PROJECT_NOT_FOUND", f"project not found: {error}"
        )

    @app.exception_handler(TaskNotFoundError)
    async def handle_task_not_found(
        request: Request, error: TaskNotFoundError
    ) -> JSONResponse:
        return _error_response(
            request, 404, "TASK_NOT_FOUND", f"task not found: {error}"
        )

    @app.exception_handler(IdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, error: IdempotencyConflictError
    ) -> JSONResponse:
        return _error_response(
            request,
            409,
            "IDEMPOTENCY_CONFLICT",
            f"idempotency key was reused with different input: {error}",
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
                details={"source_path": payload.source_path},
            ) from error
        response.status_code = 201 if created else 200
        return jsonable_encoder(project)

    @app.get("/api/v1/projects")
    def list_projects():
        return jsonable_encoder(services.projects.list())

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
