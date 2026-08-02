from __future__ import annotations

import json
from ipaddress import ip_address
import stat
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn
from fastapi.encoders import jsonable_encoder

from pcbflow.api import _mapped_domain_error, create_app
from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.commands import DesignCommandSchemaError, load_command_batch
from pcbflow.component_binding_store import ComponentModuleBindingConflictError
from pcbflow.component_bindings import (
    ModuleCatalogUnavailableError,
    ModuleKicadMajorUnsupportedError,
)
from pcbflow.component_store import ComponentRevisionNotFoundError
from pcbflow.config import Settings
from pcbflow.container import Container, build_container
from pcbflow.observability import ensure_trace_id
from pcbflow.proposal_store import ProposalNotFoundError
from pcbflow.proposals import (
    CandidateNotReviewableError,
    ProjectNotManagedError as ProposalProjectNotManagedError,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    RevisionConflictError,
)
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.requirements import RequirementSet, RequirementsBlockedError
from pcbflow.revisions import (
    GitOperationError,
    ProjectNotManagedError as RevisionProjectNotManagedError,
    ProjectWorktreeDirtyError,
)
from pcbflow.schematic.modules import ModuleRevisionNotFoundError

app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
task_app = typer.Typer(no_args_is_help=True)
requirements_app = typer.Typer(no_args_is_help=True)
approval_app = typer.Typer(no_args_is_help=True)
proposal_app = typer.Typer(no_args_is_help=True)
component_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(requirements_app, name="requirements")
app.add_typer(approval_app, name="approval")
app.add_typer(proposal_app, name="proposal")
app.add_typer(component_app, name="component")


class CliInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _cli_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, CliInputError):
        return error.code, str(error)
    if isinstance(error, IdempotencyConflictError):
        return (
            "IDEMPOTENCY_CONFLICT",
            "the operation conflicts with an existing idempotent result",
        )
    if isinstance(error, RequirementsBlockedError):
        return "REQUIREMENTS_BLOCKED", "blocking requirement assumptions remain open"
    if isinstance(error, ApprovalDigestMismatchError):
        return "APPROVAL_DIGEST_MISMATCH", "the approval digest is no longer current"
    if isinstance(error, RevisionConflictError):
        return "PROJECT_REVISION_CONFLICT", "the project revision changed"
    if isinstance(
        error, (RevisionProjectNotManagedError, ProposalProjectNotManagedError)
    ):
        return "PROJECT_NOT_MANAGED", "the project must be adopted first"
    if isinstance(error, ProjectWorktreeDirtyError):
        return "PROJECT_WORKTREE_DIRTY", "the recorded project snapshot is dirty"
    if isinstance(error, CandidateNotReviewableError):
        return "CANDIDATE_NOT_REVIEWABLE", "the proposal candidate is not reviewable"
    if isinstance(error, DesignCommandSchemaError):
        return "DESIGN_COMMAND_SCHEMA_INVALID", "the design command batch is invalid"
    if isinstance(error, ComponentRevisionNotFoundError):
        return "COMPONENT_REVISION_NOT_FOUND", "component revision not found"
    if isinstance(error, ModuleRevisionNotFoundError):
        return "MODULE_REVISION_NOT_FOUND", "module revision not found"
    if isinstance(error, ModuleCatalogUnavailableError):
        return "MODULE_CATALOG_UNAVAILABLE", "module catalog is unavailable"
    if isinstance(error, ModuleKicadMajorUnsupportedError):
        return (
            "MODULE_KICAD_MAJOR_UNSUPPORTED",
            "module does not support the requested KiCad major",
        )
    if isinstance(error, ComponentModuleBindingConflictError):
        return (
            "COMPONENT_MODULE_BINDING_CONFLICT",
            "component revision already has a different module binding for this KiCad major",
        )
    if isinstance(
        error, (ProjectNotFoundError, RequirementSetNotFoundError, ProposalNotFoundError)
    ):
        if isinstance(error, ProjectNotFoundError):
            return "PROJECT_NOT_FOUND", "project not found"
        if isinstance(error, RequirementSetNotFoundError):
            return "REQUIREMENT_SET_NOT_FOUND", "requirement set not found"
        return "PROPOSAL_NOT_FOUND", "proposal not found"
    if isinstance(error, GitOperationError):
        return "GIT_OPERATION_FAILED", "the managed Git operation failed"
    if isinstance(error, OSError):
        return "INPUT_FILE_INVALID", "the input file cannot be read"
    mapped = _mapped_domain_error(error)
    return mapped[1], mapped[2]


def _abort(error: BaseException) -> NoReturn:
    code, message = _cli_error(error)
    typer.echo(f"{code}: {message}", err=True)
    raise typer.Exit(code=2)


def _read_input_file(path: Path, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CliInputError(
            "INPUT_FILE_INVALID", "the input file cannot be read"
        ) from error
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    ):
        raise CliInputError(
            "INPUT_FILE_INVALID", "the input must be a regular non-link file"
        )
    if metadata.st_size > max_bytes:
        raise CliInputError(
            "INPUT_FILE_TOO_LARGE", "the input file exceeds the size limit"
        )
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as error:
        raise CliInputError(
            "INPUT_FILE_INVALID", "the input file cannot be read"
        ) from error
    if len(data) > max_bytes:
        raise CliInputError(
            "INPUT_FILE_TOO_LARGE", "the input file exceeds the size limit"
        )
    return data


def _build() -> Container:
    ensure_trace_id()
    return build_container(Settings.from_env())


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip()
    if normalized.lower() == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _emit(value: object, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                jsonable_encoder(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(human)


def _requirement_view(value: RequirementSet) -> dict[str, object]:
    result = jsonable_encoder(value)
    result["subject_digest"] = (
        value.subject_digest() if value.candidate_revision is not None else None
    )
    return result


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        capability = container.kicad.probe()
        _emit(
            {"kicad_cli": capability},
            json_output,
            "KiCad CLI available" if capability.available else "KiCad CLI unavailable",
        )
    finally:
        container.dispose()


@project_app.command("add")
def project_add(
    path: Path,
    name: Annotated[str, typer.Option("--name")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        project = container.projects.create(name, path, idempotency_key)
        _emit(project, json_output, project.id)
    finally:
        container.dispose()


@project_app.command("list")
def project_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        projects = container.projects.list()
        _emit(projects, json_output, f"{len(projects)} project(s)")
    finally:
        container.dispose()


@project_app.command("adopt")
def project_adopt(
    project_id: str,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            project = container.revisions.adopt(project_id, idempotency_key)
        except Exception as error:
            _abort(error)
        _emit(project, json_output, project.id)
    finally:
        container.dispose()


@component_app.command("import")
def component_import(
    manifest_path: Path,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            if container.settings.remote_mode:
                raise CliInputError(
                    "LOCAL_SOURCE_PATHS_DISABLED",
                    "local source paths are disabled in remote mode",
                )
            revision = container.components.import_revision(
                manifest_path, idempotency_key
            )
        except Exception as error:
            _abort(error)
        _emit(revision, json_output, revision.id)
    finally:
        container.dispose()


@component_app.command("show")
def component_show(
    component_revision_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            revision = container.component_store.get(component_revision_id)
        except Exception as error:
            _abort(error)
        _emit(revision, json_output, revision.id)
    finally:
        container.dispose()


@component_app.command("list")
def component_list(
    component_key: Annotated[str, typer.Option("--component-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        revisions = container.component_store.list_for_component(component_key)
        _emit(revisions, json_output, f"{len(revisions)} component revision(s)")
    finally:
        container.dispose()


@component_app.command("bind-module")
def component_bind_module(
    component_revision_id: str,
    kicad_major: Annotated[int, typer.Option("--kicad-major")],
    module_revision_id: Annotated[str, typer.Option("--module-revision-id")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            binding = container.component_module_bindings.bind(
                component_revision_id,
                kicad_major,
                module_revision_id,
                idempotency_key,
            )
        except Exception as error:
            _abort(error)
        _emit(binding, json_output, binding.id)
    finally:
        container.dispose()


@component_app.command("bindings")
def component_bindings(
    component_revision_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            bindings = container.component_module_bindings.list_for_component_revision(
                component_revision_id
            )
        except Exception as error:
            _abort(error)
        _emit(bindings, json_output, f"{len(bindings)} component module binding(s)")
    finally:
        container.dispose()


@requirements_app.command("import")
def requirements_import(
    project_id: str,
    file_path: Annotated[Path, typer.Option("--file")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            data = _read_input_file(file_path, container.settings.max_project_bytes)
            requirement_set = container.requirements.import_draft(
                project_id, data, idempotency_key
            )
        except Exception as error:
            _abort(error)
        _emit(
            _requirement_view(requirement_set),
            json_output,
            f"{requirement_set.id}: {requirement_set.status.value}",
        )
    finally:
        container.dispose()


@requirements_app.command("show")
def requirements_show(
    requirement_set_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            requirement_set = container.requirement_store.get(requirement_set_id)
        except Exception as error:
            _abort(error)
        _emit(
            _requirement_view(requirement_set),
            json_output,
            f"{requirement_set.id}: {requirement_set.status.value}",
        )
    finally:
        container.dispose()


@requirements_app.command("submit")
def requirements_submit(
    requirement_set_id: str,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            requirement_set = container.requirements.submit(
                requirement_set_id, idempotency_key
            )
        except Exception as error:
            _abort(error)
        _emit(
            _requirement_view(requirement_set),
            json_output,
            f"{requirement_set.id}: {requirement_set.status.value}",
        )
    finally:
        container.dispose()


@approval_app.command("decide")
def approval_decide(
    subject_id: str,
    subject_digest: Annotated[str, typer.Option("--subject-digest")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
    reject: Annotated[bool, typer.Option("--reject")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = None,
    actor_type: Annotated[str, typer.Option("--actor-type")] = "human",
    comment: Annotated[str, typer.Option("--comment")] = None,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if approve == reject:
        _abort(
            CliInputError(
                "APPROVAL_DECISION_INVALID",
                "exactly one of --approve or --reject is required",
            )
        )
    if not actor_id or not comment or not idempotency_key:
        _abort(
            CliInputError(
                "REQUEST_SCHEMA_INVALID",
                "actor ID, comment, and idempotency key are required",
            )
        )
    container = _build()
    try:
        try:
            requirement_set = container.approvals.decide_g1(
                requirement_set_id=subject_id,
                subject_digest=subject_digest,
                decision="approve" if approve else "reject",
                actor_type=actor_type,
                actor_id=actor_id,
                comment=comment,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            _abort(error)
        _emit(
            _requirement_view(requirement_set),
            json_output,
            f"{requirement_set.id}: {requirement_set.status.value}",
        )
    finally:
        container.dispose()


@proposal_app.command("create")
def proposal_create(
    project_id: str,
    file_path: Annotated[Path, typer.Option("--file")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            data = _read_input_file(file_path, container.settings.max_project_bytes)
            batch = load_command_batch(data)
            if batch.project_id != project_id:
                raise CliInputError(
                    "DESIGN_COMMAND_SCHEMA_INVALID",
                    "command batch project does not match the request path",
                )
            proposal = container.proposals.create(data, idempotency_key)
        except Exception as error:
            _abort(error)
        _emit(
            proposal,
            json_output,
            f"{proposal.id}: {proposal.status.value}",
        )
    finally:
        container.dispose()


@proposal_app.command("show")
def proposal_show(
    proposal_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            proposal = container.proposal_store.get(proposal_id)
        except Exception as error:
            _abort(error)
        _emit(proposal, json_output, f"{proposal.id}: {proposal.status.value}")
    finally:
        container.dispose()


@proposal_app.command("diff")
def proposal_diff(
    proposal_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            proposal = container.proposal_store.get(proposal_id)
            if proposal.semantic_diff_digest is None:
                raise CandidateNotReviewableError("semantic diff is not available")
            if not container.artifacts.verify(proposal.semantic_diff_digest):
                raise ValueError("semantic diff artifact failed verification")
            with container.artifacts.open(proposal.semantic_diff_digest) as artifact:
                raw = artifact.read()
            value = json.loads(
                raw.decode("utf-8")
            )
        except Exception as error:
            _abort(error)
        _emit(value, json_output, f"{proposal_id}: diff")
    finally:
        container.dispose()


@proposal_app.command("accept")
def proposal_accept(
    proposal_id: str,
    candidate_digest: Annotated[str, typer.Option("--candidate-digest")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = None,
    actor_type: Annotated[str, typer.Option("--actor-type")] = "human",
    comment: Annotated[str, typer.Option("--comment")] = None,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if not actor_id or not comment or not idempotency_key:
        _abort(
            CliInputError(
                "REQUEST_SCHEMA_INVALID",
                "actor ID, comment, and idempotency key are required",
            )
        )
    container = _build()
    try:
        try:
            proposal = container.proposal_decisions.accept(
                proposal_id=proposal_id,
                candidate_digest=candidate_digest,
                actor_type=actor_type,
                actor_id=actor_id,
                comment=comment,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            _abort(error)
        _emit(proposal, json_output, f"{proposal.id}: {proposal.status.value}")
    finally:
        container.dispose()


@proposal_app.command("reject")
def proposal_reject(
    proposal_id: str,
    reason: Annotated[str, typer.Option("--reason")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = None,
    actor_type: Annotated[str, typer.Option("--actor-type")] = "human",
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if not actor_id or not idempotency_key:
        _abort(
            CliInputError(
                "REQUEST_SCHEMA_INVALID",
                "actor ID and idempotency key are required",
            )
        )
    container = _build()
    try:
        try:
            proposal = container.proposal_decisions.reject(
                proposal_id=proposal_id,
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            _abort(error)
        _emit(proposal, json_output, f"{proposal.id}: {proposal.status.value}")
    finally:
        container.dispose()


@app.command("validate")
def validate_project(
    project_id: str,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        task = container.validation.enqueue(project_id, idempotency_key)
        _emit(task, json_output, task.id)
    finally:
        container.dispose()


@app.command("worker")
def run_worker(
    once: Annotated[bool, typer.Option("--once")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if not once:
        raise typer.BadParameter("only --once is supported")
    container = _build()
    try:
        handled = container.worker.run_once()
        _emit({"handled": handled}, json_output, "handled" if handled else "idle")
    finally:
        container.dispose()


@task_app.command("show")
def task_show(
    task_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        task = container.tasks.get(task_id)
        _emit(task, json_output, f"{task.id}: {task.status.value}")
    finally:
        container.dispose()


@app.command("findings")
def list_findings(
    project_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        container.projects.get(project_id)
        findings = container.findings.list_for_project(project_id)
        _emit(findings, json_output, f"{len(findings)} finding(s)")
    finally:
        container.dispose()


@app.command("evidence")
def list_evidence(
    project_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        container.projects.get(project_id)
        evidence = container.evidence.list_for_project(project_id)
        _emit(evidence, json_output, f"{len(evidence)} evidence record(s)")
    finally:
        container.dispose()


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    if not _is_loopback_host(host):
        raise typer.BadParameter(
            "serve may only bind to a loopback address or localhost",
            param_hint="--host",
        )
    container = _build()
    try:
        uvicorn.run(create_app(container), host=host, port=port)
    finally:
        container.dispose()
