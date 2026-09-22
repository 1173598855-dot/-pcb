from __future__ import annotations

import json
from ipaddress import ip_address
import stat
from pathlib import Path
from typing import Annotated, NoReturn
from datetime import UTC, datetime

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
from pcbflow.eda import (
    EdaAuthorityConflictError,
    ProjectEdaAuthorityInput,
    validate_authority_input,
)
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_candidates import (
    PcbCandidateNotFoundError,
    PcbCandidateNotReviewableError,
    validate_candidate_public_inputs,
)
from pcbflow.domain import EdaKind, utc_now
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
    TaskNotCancellableError,
    TaskNotFoundError,
)
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.requirements import RequirementSet, RequirementsBlockedError
from pcbflow.revisions import (
    GitOperationError,
    ProjectNotManagedError as RevisionProjectNotManagedError,
    ProjectWorktreeDirtyError,
)
from pcbflow.schematic.modules import ModuleRevisionNotFoundError
from pcbflow.worker_health import read_worker_health_state

app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
task_app = typer.Typer(no_args_is_help=True)
requirements_app = typer.Typer(no_args_is_help=True)
approval_app = typer.Typer(no_args_is_help=True)
proposal_app = typer.Typer(no_args_is_help=True)
component_app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer(no_args_is_help=True, invoke_without_command=True)
eda_app = typer.Typer(no_args_is_help=True)
pcb_app = typer.Typer(no_args_is_help=True)
pcb_candidate_app = typer.Typer(no_args_is_help=True)
pcb_release_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(requirements_app, name="requirements")
app.add_typer(approval_app, name="approval")
app.add_typer(proposal_app, name="proposal")
app.add_typer(component_app, name="component")
app.add_typer(worker_app, name="worker")
app.add_typer(eda_app, name="eda")
app.add_typer(pcb_app, name="pcb")
pcb_app.add_typer(pcb_candidate_app, name="candidate")
pcb_app.add_typer(pcb_release_app, name="release")


class CliInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _cli_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, LcedaProCapabilityError):
        return error.code, "LCEDA Pro write capability is unverified"
    if isinstance(error, PcbCandidateNotReviewableError):
        return "PCB_CANDIDATE_NOT_REVIEWABLE", "the PCB candidate is not reviewable"
    if isinstance(error, PcbCandidateNotFoundError):
        return "PCB_CANDIDATE_NOT_FOUND", "PCB candidate not found"
    if isinstance(error, Exception) and str(error) == "PCB_CANDIDATE_STALE":
        return "PCB_CANDIDATE_STALE", "the PCB candidate base revision is stale"
    if isinstance(error, Exception) and str(error) == "PCB_CAPABILITY_GATE_BLOCKED":
        return "PCB_CAPABILITY_GATE_BLOCKED", "the PCB candidate capability gate is blocked"
    if isinstance(error, CliInputError):
        return error.code, str(error)
    if isinstance(error, EdaAuthorityConflictError):
        return (
            "EDA_AUTHORITY_CONFLICT",
            "the project EDA authority is already immutable",
        )
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
    if isinstance(error, TaskNotCancellableError):
        return "TASK_NOT_CANCELLABLE", "the task is already in a terminal state"
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
        error,
        (
            ProjectNotFoundError,
            TaskNotFoundError,
            RequirementSetNotFoundError,
            ProposalNotFoundError,
        ),
    ):
        if isinstance(error, ProjectNotFoundError):
            return "PROJECT_NOT_FOUND", "project not found"
        if isinstance(error, TaskNotFoundError):
            return "TASK_NOT_FOUND", "task not found"
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


def _authority_input(
    eda_kind: str | None,
    eda_profile_id: str | None,
    board_profile_id: str | None,
    rulepack_digest: str | None,
) -> ProjectEdaAuthorityInput | None:
    values = (eda_kind, eda_profile_id, board_profile_id, rulepack_digest)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise CliInputError(
            "EDA_AUTHORITY_INVALID", "EDA authority options must be complete"
        )
    assert eda_kind is not None
    assert eda_profile_id is not None
    assert board_profile_id is not None
    assert rulepack_digest is not None
    try:
        kind = EdaKind(eda_kind)
    except ValueError as error:
        raise CliInputError("EDA_AUTHORITY_INVALID", "unsupported EDA kind") from error
    try:
        return validate_authority_input(ProjectEdaAuthorityInput(
            eda_kind=kind,
            eda_profile_id=eda_profile_id,
            board_profile_id=board_profile_id,
            rulepack_digest=rulepack_digest,
        ))
    except ValueError as error:
        raise CliInputError("EDA_AUTHORITY_INVALID", str(error)) from error


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        capability = container.kicad.probe()
        _emit(
            {"kicad_cli": capability, "lceda_pro": container.lceda_pro.probe()},
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
    eda_kind: Annotated[str | None, typer.Option("--eda-kind")] = None,
    eda_profile_id: Annotated[str | None, typer.Option("--eda-profile-id")] = None,
    board_profile_id: Annotated[str | None, typer.Option("--board-profile-id")] = None,
    rulepack_digest: Annotated[str | None, typer.Option("--rulepack-digest")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        authority = _authority_input(
            eda_kind, eda_profile_id, board_profile_id, rulepack_digest
        )
        project, created = container.projects.create_with_status(
            name, path, idempotency_key, authority
        )
    except Exception as error:
        _abort(error)
    else:
        _emit(project, json_output, project.id)
    finally:
        container.dispose()


@eda_app.command("probe")
def eda_probe(
    eda_name: str,
    project_id: Annotated[str | None, typer.Argument()] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if eda_name != "lceda-pro":
        raise typer.BadParameter("only lceda-pro is supported", param_hint="eda_name")
    container = _build()
    try:
        try:
            if project_id is not None:
                if idempotency_key is None:
                    raise CliInputError(
                        "IDEMPOTENCY_KEY_REQUIRED", "idempotency key is required"
                    )
                task = container.capability_gate.enqueue(project_id, idempotency_key)
                _emit(task, json_output, f"{task.id}: {task.status.value}")
                return
            capability = container.lceda_pro.probe()
            _emit(
                capability,
                json_output,
                "LCEDA Pro write capability verified"
                if capability.write_verified
                else "LCEDA Pro write capability unverified",
            )
        except Exception as error:
            _abort(error)
    finally:
        container.dispose()


@pcb_candidate_app.command("create")
def pcb_candidate_create(
    project_id: str,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    net_ids: Annotated[list[str] | None, typer.Option("--net-id")] = None,
    board_snapshot_digest: Annotated[str | None, typer.Option("--board-snapshot-digest")] = None,
    capability_digest: Annotated[str | None, typer.Option("--capability-digest")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if not idempotency_key:
        _abort(CliInputError("IDEMPOTENCY_KEY_REQUIRED", "idempotency key is required"))
    try:
        validate_candidate_public_inputs(
            seed=seed,
            net_ids=net_ids,
            board_snapshot_digest=board_snapshot_digest,
            capability_digest=capability_digest,
        )
    except Exception as error:
        _abort(error)
    container = _build()
    try:
        try:
            candidates = getattr(container, "pcb_candidates", None)
            if candidates is None:
                raise CliInputError("PCB_CAPABILITY_GATE_BLOCKED", "PCB candidate service is unavailable")
            candidate = candidates.create_from_public_inputs(
                project_id=project_id,
                seed=seed,
                net_ids=net_ids,
                board_snapshot_digest=board_snapshot_digest,
                capability_digest=capability_digest,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            _abort(error)
        _emit(candidate, json_output, f"{candidate.id}: {candidate.status.value}")
    finally:
        container.dispose()


@pcb_candidate_app.command("show")
def pcb_candidate_show(
    candidate_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            candidates = getattr(container, "pcb_candidates", None)
            if candidates is None:
                raise CliInputError("PCB_CAPABILITY_GATE_BLOCKED", "PCB candidate service is unavailable")
            candidate = candidates.get(candidate_id)
        except Exception as error:
            _abort(error)
        _emit(candidate, json_output, f"{candidate.id}: {candidate.status.value}")
    finally:
        container.dispose()


@pcb_candidate_app.command("approve-g3")
def pcb_candidate_approve_g3(
    candidate_id: str,
    candidate_digest: Annotated[str, typer.Option("--candidate-digest")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    actor_id: Annotated[str, typer.Option("--actor-id")],
    comment: Annotated[str, typer.Option("--comment")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            approvals = getattr(container, "pcb_approvals", None)
            if approvals is None:
                raise CliInputError(
                    "PCB_CAPABILITY_GATE_BLOCKED",
                    "PCB approval service is unavailable",
                )
            result = approvals.decide_g3(
                candidate_id=candidate_id,
                candidate_digest=candidate_digest,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                decision="approve" if approve else "reject",
                comment=comment,
            )
        except Exception as error:
            _abort(error)
        _emit(result, json_output, f"{result.id}: {result.status.value}")
    finally:
        container.dispose()


@pcb_release_app.command("export")
def pcb_release_export(
    candidate_id: str,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            release = getattr(container, "pcb_release", None)
            if release is None:
                raise CliInputError("PCB_RELEASE_CAPABILITY_BLOCKED", "PCB release service is unavailable")
            task = release.enqueue_export(candidate_id, idempotency_key)
        except Exception as error:
            _abort(error)
        _emit(task, json_output, f"{task.id}: {task.status.value}")
    finally:
        container.dispose()


@pcb_release_app.command("approve-g4")
def pcb_release_approve_g4(
    candidate_id: str,
    manifest_digest: Annotated[str, typer.Option("--manifest-digest")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    actor_id: Annotated[str, typer.Option("--actor-id")],
    comment: Annotated[str, typer.Option("--comment")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        try:
            approvals = getattr(container, "pcb_release_approvals", None)
            if approvals is None:
                raise CliInputError("PCB_RELEASE_CAPABILITY_BLOCKED", "G4 approval service is unavailable")
            candidate = approvals.decide_g4(
                candidate_id=candidate_id,
                manifest_digest=manifest_digest,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                decision="approve" if approve else "reject",
                comment=comment,
            )
        except Exception as error:
            _abort(error)
        _emit(candidate, json_output, f"{candidate.id}: {candidate.status.value}")
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


@worker_app.callback(invoke_without_command=True)
def run_worker(
    context: typer.Context,
    once: Annotated[bool, typer.Option("--once")] = False,
    run: Annotated[bool, typer.Option("--run")] = False,
    exit_when_idle: Annotated[bool, typer.Option("--exit-when-idle")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute worker tasks. Use --once for single task, --run for resident mode."""
    import signal
    import sys

    if context.invoked_subcommand is not None:
        if once or run or exit_when_idle or json_output:
            _abort(
                CliInputError(
                    "INVALID_ARGUMENT",
                    "worker execution options cannot be used with a subcommand",
                )
            )
        return

    if once and run:
        raise CliInputError(
            "INVALID_ARGUMENT", "cannot specify both --once and --run"
        )

    # Legacy --once behavior (or default when no flags)
    if once or (not run and not exit_when_idle):
        container = _build()
        try:
            handled = container.worker.run_once()
            _emit({"handled": handled}, json_output, "handled" if handled else "idle")
        finally:
            container.dispose()
        return

    # Resident worker mode
    container = _build()
    worker = None

    def shutdown_handler(signum, frame):
        if worker:
            worker.request_shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        from pcbflow.worker_service import WorkerService

        worker = WorkerService(container)

        while not worker.shutdown_requested:
            result = worker.run_one_cycle()

            if exit_when_idle and not result.claimed and not worker.active_tasks:
                break

        duration_seconds = (datetime.now(UTC) - worker.started_at).total_seconds()

        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "status": "stopped",
                        "worker_id": worker.worker_id,
                        "completed": worker.completed_count,
                        "failed": worker.failed_count,
                        "duration_seconds": duration_seconds,
                    }
                )
            )
        else:
            typer.echo(
                f"Worker stopped: {worker.completed_count} completed, "
                f"{worker.failed_count} failed, {duration_seconds:.1f}s"
            )

        sys.exit(0)

    except KeyboardInterrupt:
        if worker:
            worker.request_shutdown()
        sys.exit(0)
    finally:
        if worker:
            worker.shutdown_gracefully()
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


@project_app.command("eda-authority")
def project_eda_authority(
    project_id: str,
    eda_kind: Annotated[str, typer.Option("--eda-kind")],
    eda_profile_id: Annotated[str, typer.Option("--eda-profile-id")],
    board_profile_id: Annotated[str, typer.Option("--board-profile-id")],
    rulepack_digest: Annotated[str, typer.Option("--rulepack-digest")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        authority = _authority_input(
            eda_kind, eda_profile_id, board_profile_id, rulepack_digest
        )
        assert authority is not None
        configured = container.eda_authorities.configure(
            project_id, authority, idempotency_key
        )
    except Exception as error:
        _abort(error)
    else:
        _emit(configured, json_output, configured.project_id)
    finally:
        container.dispose()


@task_app.command("cancel")
def task_cancel(
    task_id: str,
    reason: Annotated[str, typer.Option("--reason")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    del idempotency_key
    container = _build()
    try:
        task = container.tasks.cancel(task_id, reason, utc_now())
    except Exception as error:
        _abort(error)
    finally:
        container.dispose()
    _emit(task, json_output, f"{task.id}: {task.status.value}")


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


@worker_app.command("health")
def worker_health(
    worker_file: Annotated[str, typer.Option("--file")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check worker health status. Use --file to read from worker state file."""
    state_path = (
        Path(worker_file) if worker_file else Settings.from_env().worker_state_file
    )
    if not state_path.exists():
        code = "FILE_NOT_FOUND" if worker_file else "WORKER_STATE_UNAVAILABLE"
        _abort(CliInputError(code, f"worker state file not found: {state_path}"))

    try:
        state = read_worker_health_state(state_path)
        _emit(
            state,
            json_output,
            f"Worker {state.get('worker_id', 'unknown')}: "
            f"{state.get('status', 'unknown')}",
        )
    except Exception as error:
        _abort(CliInputError("FILE_READ_ERROR", f"failed to read worker state: {error}"))


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
