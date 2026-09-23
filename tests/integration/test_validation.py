import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import TaskStatus
from pcbflow.kicad import (
    KicadDesignFormatError,
    KicadInputLimitError,
    KicadReportFormatError,
    RawValidationReport,
)
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    StaleLeaseError,
    TaskRepository,
)
from pcbflow.tables import TaskAttemptRow
from pcbflow.tasks import Worker
from pcbflow.validation import (
    VALIDATION_TASK_KIND,
    ProjectCopyLimitError,
    ValidationService,
    ValidationTaskHandler,
)

NOW = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)


class FakeKicad:
    def __init__(self, fixture_dir: Path, source: Path) -> None:
        self.fixture_dir = fixture_dir
        self.source = source.resolve()
        self.calls: list[tuple[Path, Path]] = []

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        project = project_dir.resolve()
        output = output_dir.resolve()
        assert project != self.source
        assert not output.is_relative_to(project)
        assert (project / "board.kicad_sch").read_text(encoding="utf-8") == "schematic"
        assert (project / "board.kicad_pcb").read_text(encoding="utf-8") == "board"
        output.mkdir(parents=True, exist_ok=True)
        self.calls.append((project, output))
        return (
            RawValidationReport(
                "erc",
                (self.fixture_dir / "erc.json").read_bytes(),
                ("kicad-cli", "sch", "erc"),
                0,
                "9.0.2",
                "sha256:" + "9" * 64,
                "kicad-9-v1",
                1,
            ),
            RawValidationReport(
                "drc",
                (self.fixture_dir / "drc.json").read_bytes(),
                ("kicad-cli", "pcb", "drc"),
                0,
                "9.0.2",
                "sha256:" + "9" * 64,
                "kicad-9-v1",
                1,
            ),
        )


def test_validation_handler_persists_raw_evidence_and_findings(
    session_factory: sessionmaker[Session], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"

    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    evidence = EvidenceRepository(session_factory)
    findings = FindingRepository(session_factory)
    store = ContentAddressedStore(tmp_path / "artifacts")
    fake_kicad = FakeKicad(fixture_dir, source)
    project = projects.create("Controller", source, "project-1")
    service = ValidationService(projects, tasks)
    active_checks: list[tuple[str, str, datetime]] = []
    original_assert_active = tasks.assert_active

    def record_active(task_id: str, lease_token: str, now: datetime) -> None:
        active_checks.append((task_id, lease_token, now))
        original_assert_active(task_id, lease_token, now)

    monkeypatch.setattr(tasks, "assert_active", record_active)
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        store,
        fake_kicad,
        max_files=100,
        max_bytes=1_000_000,
        tasks=tasks,
        clock=lambda: NOW,
    )
    worker = Worker(
        tasks,
        "worker-a",
        {VALIDATION_TASK_KIND: handler},
        lambda: NOW,
        30,
    )
    task = service.enqueue(project.id, "validate-project-1-r1")

    assert worker.run_once()

    completed = tasks.get(task.id)
    stored_evidence = evidence.list_for_project(project.id)
    stored_findings = findings.list_for_project(project.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result == {
        "evidence_ids": [item.id for item in stored_evidence],
        "finding_count": 2,
        "tool": {
            "version": "9.0.2",
            "executable_digest": "sha256:" + "9" * 64,
            "profile_id": "kicad-9-v1",
            "profile_revision": 1,
        },
    }
    assert [item.kind for item in stored_evidence] == ["kicad_erc", "kicad_drc"]
    assert [item.rule_id for item in stored_findings] == [
        "KICAD.ERC.PIN_NOT_CONNECTED",
        "KICAD.DRC.CLEARANCE",
    ]
    assert all(
        item.evidence_id in {record.id for record in stored_evidence}
        for item in stored_findings
    )
    assert all(store.verify(item.artifact_digest) for item in stored_evidence)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    assert len(fake_kicad.calls) == 1
    assert active_checks


def test_same_task_report_write_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    evidence = EvidenceRepository(session_factory)
    findings = FindingRepository(session_factory)
    project = projects.create("Controller", source, "project-idempotent")
    task = tasks.enqueue(VALIDATION_TASK_KIND, {"project_id": project.id}, "task-1", project.id)
    lease = tasks.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == task.id
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        ContentAddressedStore(tmp_path / "artifacts"),
        FakeKicad(fixture_dir, source),
        max_files=100,
        max_bytes=1_000_000,
    )

    first = handler(lease)
    second = handler(lease)

    assert first == second
    assert len(evidence.list_for_project(project.id)) == 2
    assert len(findings.list_for_project(project.id)) == 2


def test_validation_report_write_is_fenced_after_lease_replacement(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    evidence = EvidenceRepository(session_factory)
    findings = FindingRepository(session_factory)
    store = ContentAddressedStore(tmp_path / "artifacts")
    project = projects.create("Controller", source, "lease-fenced-project")
    task = tasks.enqueue(
        VALIDATION_TASK_KIND,
        {"project_id": project.id},
        "lease-fenced-task",
        project.id,
    )
    lease = tasks.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == task.id
    tasks.start(task.id, lease.lease_token, NOW)

    class OneReportKicad:
        def validate(self, project_dir: Path, output_dir: Path):
            return (
                RawValidationReport(
                    "erc",
                    b'{"version":"1.0","source":"board.kicad_sch","violations":[]}',
                    ("kicad-cli", "sch", "erc"),
                    0,
                    "9.0.2",
                    "sha256:" + "9" * 64,
                    "kicad-9-v1",
                    1,
                ),
            )

    original_put_bytes = store.put_bytes
    replaced = False

    def replace_lease_before_register(data: bytes, media_type: str):
        nonlocal replaced
        if not replaced:
            replaced = True
            replacement = tasks.claim_next(
                "worker-b", NOW + timedelta(seconds=31), 30
            )
            assert replacement is not None and replacement.task_id == task.id
        return original_put_bytes(data, media_type)

    monkeypatch.setattr(store, "put_bytes", replace_lease_before_register)
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        store,
        OneReportKicad(),
        max_files=100,
        max_bytes=1_000_000,
        tasks=tasks,
        clock=lambda: NOW,
    )

    with pytest.raises(StaleLeaseError):
        handler(lease)

    assert evidence.list_for_project(project.id) == []


def test_invalid_validation_report_is_parsed_before_artifact_write(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    evidence = EvidenceRepository(session_factory)
    findings = FindingRepository(session_factory)
    store = ContentAddressedStore(tmp_path / "artifacts")
    project = projects.create("Controller", source, "invalid-report-project")
    task = tasks.enqueue(
        VALIDATION_TASK_KIND,
        {"project_id": project.id},
        "invalid-report-task",
        project.id,
    )
    lease = tasks.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == task.id

    class InvalidReportKicad:
        def validate(self, project_dir: Path, output_dir: Path):
            return (
                RawValidationReport(
                    "erc", b"not-json", ("kicad-cli", "sch", "erc"), 0,
                    "9.0.2", "sha256:" + "9" * 64, "kicad-9-v1", 1
                ),
            )

    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        store,
        InvalidReportKicad(),
        max_files=100,
        max_bytes=1_000_000,
    )

    with pytest.raises(KicadReportFormatError):
        handler(lease)

    assert not list(store.root.joinpath("objects").rglob("*"))


def test_unsupported_kicad_format_is_a_terminal_compatibility_error(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "unsupported-format"
    source.mkdir()
    (source / "board.kicad_sch").write_text(
        '(kicad_sch (version 20990101) (uuid "00000000-0000-0000-0000-000000000001"))',
        encoding="utf-8",
    )
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    project = projects.create("Unsupported", source, "unsupported-project")
    task = tasks.enqueue(
        VALIDATION_TASK_KIND,
        {"project_id": project.id},
        "unsupported-validation",
        project.id,
    )

    class UnsupportedFormatKicad:
        def validate(self, project_dir: Path, output_dir: Path):
            raise KicadDesignFormatError(
                project_dir / "board.kicad_sch", "20990101", "kicad-10-v1"
            )

    handler = ValidationTaskHandler(
        projects,
        EvidenceRepository(session_factory),
        FindingRepository(session_factory),
        ContentAddressedStore(tmp_path / "artifacts"),
        UnsupportedFormatKicad(),
        max_files=100,
        max_bytes=1_000_000,
    )
    worker = Worker(
        tasks,
        "worker-a",
        {VALIDATION_TASK_KIND: handler},
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    failed = tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "KICAD_FILE_FORMAT_UNSUPPORTED"


def test_kicad_input_limit_is_a_terminal_validation_error(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "input-limit"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    project = projects.create("Input limit", source, "input-limit-project")
    task = tasks.enqueue(
        VALIDATION_TASK_KIND,
        {"project_id": project.id},
        "input-limit-validation",
        project.id,
    )
    class LimitedKicad:
        def validate(self, project_dir: Path, output_dir: Path):
            raise KicadInputLimitError(
                "KICAD_REPORT_LIMIT_EXCEEDED", output_dir / "erc.json", 32
            )

    handler = ValidationTaskHandler(
        projects,
        EvidenceRepository(session_factory),
        FindingRepository(session_factory),
        ContentAddressedStore(tmp_path / "artifacts"),
        LimitedKicad(),
        max_files=100,
        max_bytes=1_000_000,
        tasks=tasks,
        clock=lambda: NOW,
    )
    worker = Worker(
        tasks,
        "worker-a",
        {VALIDATION_TASK_KIND: handler},
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    failed = tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "KICAD_REPORT_LIMIT_EXCEEDED"


def test_validation_rejects_project_over_file_limit(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    projects = ProjectRepository(session_factory)
    tasks = TaskRepository(session_factory)
    project = projects.create("Controller", source, "limited-project")
    task = tasks.enqueue(VALIDATION_TASK_KIND, {"project_id": project.id}, "limited-task", project.id)
    lease = tasks.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == task.id
    handler = ValidationTaskHandler(
        projects,
        EvidenceRepository(session_factory),
        FindingRepository(session_factory),
        ContentAddressedStore(tmp_path / "artifacts"),
        FakeKicad(Path("unused"), source),
        max_files=1,
        max_bytes=1_000_000,
    )

    try:
        handler(lease)
    except ProjectCopyLimitError as error:
        assert error.code == "PROJECT_FILE_LIMIT_EXCEEDED"
    else:
        raise AssertionError("expected ProjectCopyLimitError")


def test_expired_running_validation_is_completed_after_container_restart(
    tmp_path: Path,
) -> None:
    source = tmp_path / "restart-source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path / "data")})
    fake_kicad = FakeKicad(fixture_dir, source)
    first = build_container(settings, kicad_override=fake_kicad, clock=lambda: NOW)
    project = first.projects.create("Controller", source, "restart-project")
    task = first.validation.enqueue(project.id, "restart-validation")
    lease = first.tasks.claim_next("crashed-worker", NOW, 1)
    assert lease is not None
    first.tasks.start(task.id, lease.lease_token, NOW)

    first.dispose()

    after_expiry = lease.lease_expires_at + timedelta(seconds=1)
    second = build_container(
        settings,
        kicad_override=fake_kicad,
        clock=lambda: after_expiry,
    )
    try:
        assert second.worker.run_once()
        completed = second.tasks.get(task.id)
        assert completed.status is TaskStatus.SUCCEEDED
        assert completed.attempt_count == 2
        assert len(second.evidence.list_for_project(project.id)) == 2
        with second.sessions() as session:
            attempts = session.scalars(
                select(TaskAttemptRow)
                .where(TaskAttemptRow.task_id == task.id)
                .order_by(TaskAttemptRow.attempt_number)
            ).all()
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert attempts[0].finished_at is not None
        assert attempts[0].outcome == "lease_expired"
        assert attempts[1].outcome == "succeeded"
    finally:
        second.dispose()


def test_managed_validation_reads_database_revision_not_changed_import_source(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    expected_schematic = (source / "board.kicad_sch").read_bytes()
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_TASK_LEASE_SECONDS": "30",
            "PCBFLOW_PROCESS_TIMEOUT_SECONDS": "20",
        }
    )

    class InspectingKicad:
        def validate(self, project_dir: Path, output_dir: Path):
            assert (project_dir / "board.kicad_sch").read_bytes() == expected_schematic
            output_dir.mkdir(parents=True, exist_ok=True)
            return (
                RawValidationReport(
                    "erc",
                    (fixtures / "kicad" / "erc.json").read_bytes(),
                    ("kicad-cli", "sch", "erc"),
                    0,
                    "9.0.2",
                    "sha256:" + "9" * 64,
                    "kicad-9-v1",
                    1,
                ),
            )

    container = build_container(settings, kicad_override=InspectingKicad())
    try:
        project = container.projects.create("Controller", source, "managed-validation")
        managed = container.revisions.adopt(project.id, "managed-validation-adopt")
        (source / "board.kicad_sch").write_bytes(b"changed outside pcbflow")

        task = container.validation.enqueue(managed.id, "managed-validation-run")
        assert container.worker.run_once()
        assert container.tasks.get(task.id).status is TaskStatus.SUCCEEDED
    finally:
        container.dispose()
