from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.domain import TaskStatus
from pcbflow.kicad import RawValidationReport
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    TaskRepository,
)
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
            ),
            RawValidationReport(
                "drc",
                (self.fixture_dir / "drc.json").read_bytes(),
                ("kicad-cli", "pcb", "drc"),
                0,
                "9.0.2",
            ),
        )


def test_validation_handler_persists_raw_evidence_and_findings(
    session_factory: sessionmaker[Session], tmp_path: Path
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
    handler = ValidationTaskHandler(
        projects, evidence, findings, store, fake_kicad, max_files=100, max_bytes=1_000_000
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
