from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from pcbflow import cli
from pcbflow.api import _mapped_domain_error, _validation_code, _validation_details
from pcbflow.approvals import ApprovalDigestMismatchError, ApprovalService
from pcbflow.commands import DesignCommandSchemaError
from pcbflow.kicad import (
    AmbiguousKicadProjectError,
    KicadCli,
    KicadProjectNotFoundError,
    KicadReportFormatError,
    parse_kicad_report,
)
from pcbflow.process import ProcessResult, ProcessRunner, ProcessTimeoutError
from pcbflow.proposal_store import ProposalNotFoundError
from pcbflow.proposals import (
    CandidateNotReviewableError,
    RevisionReconciliationRequiredError,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    RevisionConflictError,
    TaskNotCancellableError,
)
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.requirements import RequirementsBlockedError
from pcbflow.revisions import (
    GitOperationError,
    ProjectWorktreeDirtyError,
)
from pcbflow.revisions import (
    ProjectNotManagedError as RevisionProjectNotManagedError,
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (cli.CliInputError("CUSTOM", "custom"), "CUSTOM"),
        (IdempotencyConflictError("key"), "IDEMPOTENCY_CONFLICT"),
        (RequirementsBlockedError(("REQ-1",)), "REQUIREMENTS_BLOCKED"),
        (ApprovalDigestMismatchError("expected", "actual"), "APPROVAL_DIGEST_MISMATCH"),
        (RevisionConflictError("old", "new"), "PROJECT_REVISION_CONFLICT"),
        (RevisionProjectNotManagedError("project"), "PROJECT_NOT_MANAGED"),
        (cli.ProposalProjectNotManagedError("project"), "PROJECT_NOT_MANAGED"),
        (ProjectWorktreeDirtyError("project"), "PROJECT_WORKTREE_DIRTY"),
        (CandidateNotReviewableError("candidate"), "CANDIDATE_NOT_REVIEWABLE"),
        (DesignCommandSchemaError("batch"), "DESIGN_COMMAND_SCHEMA_INVALID"),
        (ProjectNotFoundError("project"), "PROJECT_NOT_FOUND"),
        (RequirementSetNotFoundError("requirement"), "REQUIREMENT_SET_NOT_FOUND"),
        (ProposalNotFoundError("proposal"), "PROPOSAL_NOT_FOUND"),
        (GitOperationError(("git", "show"), 1), "GIT_OPERATION_FAILED"),
        (TaskNotCancellableError("task", "succeeded"), "TASK_NOT_CANCELLABLE"),
        (OSError("unreadable"), "INPUT_FILE_INVALID"),
        (ValueError("invalid"), "REQUEST_INVALID"),
    ],
)
def test_cli_error_mapping_is_stable(error: BaseException, code: str) -> None:
    assert cli._cli_error(error)[0] == code


@pytest.mark.parametrize(
    "error",
    [
        RequirementsBlockedError(("REQ-1",)),
        ApprovalDigestMismatchError("expected", "actual"),
        RevisionConflictError("old", "new"),
        RevisionProjectNotManagedError("project"),
        ProjectWorktreeDirtyError("project"),
        CandidateNotReviewableError("candidate"),
        TaskNotCancellableError("task", "succeeded"),
        RevisionReconciliationRequiredError("project"),
        GitOperationError(("git", "show"), 1),
    ],
)
def test_api_domain_error_mapping_preserves_stable_contract(
    error: BaseException,
) -> None:
    status, code, _message, _retryable, _details, _actions = _mapped_domain_error(
        error
    )
    assert status in {422, 409, 503}
    assert code


def test_api_validation_error_helpers_choose_stable_codes_and_fields() -> None:
    assert _validation_code(SimpleNamespace(url=SimpleNamespace(path="/api/v1/requirement-sets"))) == "REQUIREMENTS_SCHEMA_INVALID"
    assert _validation_code(SimpleNamespace(url=SimpleNamespace(path="/api/v1/proposals"))) == "DESIGN_COMMAND_SCHEMA_INVALID"
    assert _validation_code(SimpleNamespace(url=SimpleNamespace(path="/api/v1/other"))) == "REQUEST_SCHEMA_INVALID"

    class ErrorDetails:
        @staticmethod
        def errors():
            return [{"loc": ("body", "name")}, {"loc": ()}]

    assert _validation_details(ErrorDetails()) == {"fields": ["body.name", ""]}


@pytest.mark.parametrize(
    ("requirement_set", "error"),
    [
        (
            SimpleNamespace(
                subject_digest=lambda: "sha256:actual",
                candidate_revision="git:revision",
                candidate_snapshot_digest="sha256:snapshot",
            ),
            ApprovalDigestMismatchError,
        ),
        (
            SimpleNamespace(
                subject_digest=lambda: "sha256:expected",
                candidate_revision=None,
                candidate_snapshot_digest="sha256:snapshot",
            ),
            ValueError,
        ),
        (
            SimpleNamespace(
                subject_digest=lambda: "sha256:expected",
                candidate_revision="git:revision",
                candidate_snapshot_digest=None,
            ),
            ValueError,
        ),
    ],
)
def test_approval_service_rejects_invalid_candidate_metadata(
    requirement_set: SimpleNamespace, error: type[BaseException]
) -> None:
    service = ApprovalService(
        requirements=SimpleNamespace(get=lambda _identifier: requirement_set),
        projects=SimpleNamespace(),
        decisions=SimpleNamespace(),
    )

    with pytest.raises(error):
        service.decide_g1(
            requirement_set_id="reqset",
            subject_digest="sha256:expected",
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="candidate validation",
            idempotency_key="candidate-validation",
        )


@pytest.mark.parametrize(
    "error",
    [
        cli.CliInputError("APPROVAL_DECISION_INVALID", "decision"),
        cli.CliInputError("REQUEST_SCHEMA_INVALID", "request"),
    ],
)
def test_abort_emits_code_and_exits(error: BaseException, capsys) -> None:
    with pytest.raises(typer.Exit) as raised:
        cli._abort(error)
    assert raised.value.exit_code == 2
    assert str(error).split(":", 1)[0] in capsys.readouterr().err


def test_read_input_file_rejects_missing_directories_and_large_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(cli.CliInputError, match="cannot be read"):
        cli._read_input_file(tmp_path / "missing", 10)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(cli.CliInputError, match="regular"):
        cli._read_input_file(directory, 10)

    large = tmp_path / "large"
    large.write_bytes(b"0123456789")
    with pytest.raises(cli.CliInputError, match="size limit"):
        cli._read_input_file(large, 4)

    small = tmp_path / "small"
    small.write_bytes(b"ok")
    assert cli._read_input_file(small, 4) == b"ok"


def test_read_input_file_maps_read_errors_and_short_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "read-error"
    path.write_bytes(b"ok")
    original_open = Path.open

    def fail_open(self, *args, **kwargs):
        if self == path:
            raise OSError("read failed")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(cli.CliInputError, match="cannot be read"):
        cli._read_input_file(path, 4)

    def oversized_open(self, *args, **kwargs):
        if self == path:
            return BytesIO(b"12345")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", oversized_open)
    with pytest.raises(cli.CliInputError, match="size limit"):
        cli._read_input_file(path, 4)


def test_emit_supports_json_and_human_output(capsys) -> None:
    cli._emit({"value": "text"}, True, "human")
    assert json.loads(capsys.readouterr().out) == {"value": "text"}
    cli._emit(None, False, "human")
    assert capsys.readouterr().out.strip() == "human"


def test_cli_preflight_rejects_invalid_write_options() -> None:
    runner = CliRunner()
    cases = [
        (["approval", "decide", "req", "--subject-digest", "sha256:x"], "APPROVAL_DECISION_INVALID"),
        (["approval", "decide", "req", "--subject-digest", "sha256:x", "--approve"], "REQUEST_SCHEMA_INVALID"),
        (["proposal", "accept", "proposal", "--candidate-digest", "sha256:x"], "REQUEST_SCHEMA_INVALID"),
        (["proposal", "reject", "proposal", "--reason", "no"], "REQUEST_SCHEMA_INVALID"),
    ]
    for arguments, code in cases:
        result = runner.invoke(cli.app, arguments)
        assert result.exit_code == 2, result.output
        assert code in result.output


def test_worker_group_exposes_execution_options_and_health_subcommand() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.app, ["worker", "--help"])

    assert result.exit_code == 0, result.output
    # typer/rich force ANSI styling when GITHUB_ACTIONS is set, and the
    # highlighter can split literals like "--once" with escape codes; compare
    # against a de-styled rendering instead.
    plain = _ANSI_ESCAPE_RE.sub("", result.output)
    assert "--once" in plain
    assert "--run" in plain
    assert "health" in plain


def test_worker_execution_options_cannot_be_combined_with_subcommand() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.app, ["worker", "--once", "health"])

    assert result.exit_code == 2, result.output
    assert "INVALID_ARGUMENT" in result.output


def test_worker_health_reads_the_persisted_default_state(tmp_path: Path) -> None:
    state = {
        "status": "healthy",
        "worker_id": "resident-worker",
        "state": "EXECUTING",
        "started_at": "2026-08-04T00:00:00+00:00",
        "uptime_seconds": 42.0,
        "slots_total": 2,
        "slots_active": 1,
        "slots_available": 1,
        "tasks_completed": 9,
        "tasks_failed": 1,
        "tasks_active": 1,
        "backoff_attempts": 0,
    }
    (tmp_path / "worker-state.json").write_text(json.dumps(state), encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        ["worker", "health", "--json"],
        env={"PCBFLOW_DATA_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == state


def test_resident_worker_waits_for_active_tasks_before_idle_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeContainer:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        instance: FakeWorker | None = None

        def __init__(self, _container) -> None:
            self.worker_id = "test-worker"
            self.started_at = datetime.now(UTC)
            self.completed_count = 1
            self.failed_count = 0
            self.shutdown_requested = False
            self.active_tasks = {"task-active": object()}
            self.cycles = 0
            self.graceful_shutdown_calls = 0
            FakeWorker.instance = self

        def run_one_cycle(self):
            self.cycles += 1
            if self.cycles == 1:
                return SimpleNamespace(claimed=True)
            if self.cycles == 2:
                return SimpleNamespace(claimed=False)
            self.active_tasks.clear()
            return SimpleNamespace(claimed=False)

        def request_shutdown(self) -> None:
            self.shutdown_requested = True

        def shutdown_gracefully(self) -> bool:
            self.graceful_shutdown_calls += 1
            self.shutdown_requested = True
            return True

    container = FakeContainer()
    monkeypatch.setattr(cli, "_build", lambda: container)
    monkeypatch.setattr("pcbflow.worker_service.WorkerService", FakeWorker)

    result = CliRunner().invoke(
        cli.app,
        ["worker", "--run", "--exit-when-idle", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert FakeWorker.instance is not None
    assert FakeWorker.instance.cycles == 3
    assert FakeWorker.instance.graceful_shutdown_calls == 1
    assert container.disposed


def test_module_entrypoint_shows_help(monkeypatch) -> None:
    import runpy

    monkeypatch.setattr(sys, "argv", ["pcbflow", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("pcbflow.__main__", run_name="__main__")
    assert exc_info.value.code == 0


class _FailingServiceContainer:
    settings = SimpleNamespace(max_project_bytes=32)

    def __init__(self) -> None:
        self.revisions = SimpleNamespace(adopt=self._fail)
        self.requirements = SimpleNamespace(
            import_draft=self._fail,
            submit=self._fail,
        )
        self.requirement_store = SimpleNamespace(get=self._fail)
        self.approvals = SimpleNamespace(decide_g1=self._fail)
        self.proposals = SimpleNamespace(create=self._fail)
        self.proposal_store = SimpleNamespace(get=self._fail)
        self.proposal_decisions = SimpleNamespace(
            accept=self._fail,
            reject=self._fail,
        )
        self.disposed = False

    @staticmethod
    def _fail(*args, **kwargs):
        raise ValueError("forced command failure")

    def dispose(self) -> None:
        self.disposed = True


def test_cli_command_failures_abort_and_dispose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    services = _FailingServiceContainer()
    monkeypatch.setattr(cli, "_build", lambda: services)
    calls = (
        lambda: cli.project_adopt("project", "key"),
        lambda: cli.requirements_import("project", tmp_path / "missing", "key"),
        lambda: cli.requirements_show("requirement"),
        lambda: cli.requirements_submit("requirement", "key"),
        lambda: cli.approval_decide(
            "requirement",
            "sha256:digest",
            approve=True,
            actor_id="actor",
            comment="comment",
            idempotency_key="key",
        ),
        lambda: cli.proposal_create("project", tmp_path / "missing", "key"),
        lambda: cli.proposal_show("proposal"),
        lambda: cli.proposal_diff("proposal"),
        lambda: cli.proposal_accept(
            "proposal",
            "sha256:digest",
            actor_id="actor",
            comment="comment",
            idempotency_key="key",
        ),
        lambda: cli.proposal_reject(
            "proposal", "reason", actor_id="actor", idempotency_key="key"
        ),
    )
    for call in calls:
        with pytest.raises(typer.Exit):
            call()
    assert services.disposed
    assert "REQUEST_INVALID" in capsys.readouterr().err


class _ProbeRunner:
    def __init__(self, result: ProcessResult | None = None, error: BaseException | None = None):
        self.result = result
        self.error = error

    def run(self, argv, cwd, timeout_seconds):
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.mark.parametrize(
    ("result", "error", "reason"),
    [
        (None, ProcessTimeoutError(("kicad-cli", "--version"), 1), "version_command_timeout"),
        (None, OSError("read"), "executable_read_failed"),
        (ProcessResult(("kicad-cli", "--version"), 0, "not a version", "", False), None, "version_unparseable"),
    ],
)
def test_kicad_probe_reports_unavailable_reasons(
    tmp_path: Path,
    result: ProcessResult | None,
    error: BaseException | None,
    reason: str,
) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    runner = _ProbeRunner(result=result, error=error)
    report = KicadCli(runner, executable, 1).probe()
    assert report.available is False
    assert report.reason == reason


def test_kicad_validate_rejects_missing_and_ambiguous_projects(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    kicad = KicadCli(_ProbeRunner(ProcessResult(("kicad-cli", "--version"), 0, "9.0.2", "", False)), executable, 1)
    with pytest.raises(KicadProjectNotFoundError):
        kicad.validate(tmp_path / "missing", tmp_path / "out")

    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(KicadProjectNotFoundError):
        kicad.validate(project, tmp_path / "out")
    (project / "one.kicad_sch").write_text("x", encoding="utf-8")
    (project / "two.kicad_sch").write_text("x", encoding="utf-8")
    with pytest.raises(AmbiguousKicadProjectError):
        kicad.validate(project, tmp_path / "out")


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"violations":{}}',
        b'{"sheets":{}}',
        b'{"sheets":[1]}',
        b'{"sheets":[{"violations":{}}]}',
        b'{"violations":[1]}',
        b'{"violations":[{"type":"x","severity":"error","description":""}]}',
    ],
)
def test_kicad_report_parser_rejects_invalid_shapes(payload: bytes) -> None:
    with pytest.raises(KicadReportFormatError):
        parse_kicad_report("erc", payload)


def test_process_runner_rejects_negative_limit_and_non_directory_cwd(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="negative"):
        ProcessRunner(-1)
    file_path = tmp_path / "file"
    file_path.write_bytes(b"x")
    with pytest.raises(ValueError, match="directory"):
        ProcessRunner(10).run([sys.executable, "-c", ""], file_path, 1)
