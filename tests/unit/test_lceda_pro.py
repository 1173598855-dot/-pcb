from __future__ import annotations

from pathlib import Path

import pytest

from pcbflow.board.adapter import UnsupportedEdaOperationError
from pcbflow.domain import EdaOperation
from pcbflow.lceda_pro import (
    LcedaProAdapter,
    LcedaProCapabilityError,
    OfficialBridgeVerification,
)
from pcbflow.process import ProcessResult, ProcessTimeoutError


class FakeRunner:
    def __init__(self, version: str, *, mutate: Path | None = None) -> None:
        self._version = version
        self._mutate = mutate

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: float
    ) -> ProcessResult:
        if self._mutate is not None:
            self._mutate.write_bytes(b"changed")
        return ProcessResult(tuple(argv), 0, self._version, "", False)


class OfficialBridge:
    def __init__(self) -> None:
        self.identified: tuple[Path, str] | None = None
        self.contract_fixture: Path | None = None

    def identify(self, executable: Path, version: str) -> bool:
        self.identified = (executable, version)
        return True

    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        self.contract_fixture = fixture_dir
        assert self.identified == (executable, version)
        return OfficialBridgeVerification(
            create_save_reopen_snapshot_verified=True,
            verified_operations=frozenset(
                {
                    EdaOperation.SNAPSHOT,
                    EdaOperation.CREATE_CANDIDATE,
                    EdaOperation.APPLY_OPERATIONS,
                }
            ),
        )


class IncompleteOfficialBridge(OfficialBridge):
    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        self.contract_fixture = fixture_dir
        return OfficialBridgeVerification(
            create_save_reopen_snapshot_verified=False,
            verified_operations=frozenset({EdaOperation.SNAPSHOT}),
        )


class MutateThenFalseBridge(OfficialBridge):
    def identify(self, executable: Path, version: str) -> bool:
        executable.write_bytes(b"changed")
        return False


class MutateThenRaiseIdentifyBridge(OfficialBridge):
    def identify(self, executable: Path, version: str) -> bool:
        executable.write_bytes(b"changed")
        raise RuntimeError("bridge identify failed")


class MutateThenRaiseVerifyBridge(OfficialBridge):
    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        executable.write_bytes(b"changed")
        raise RuntimeError("bridge verification failed")


class MutableOperationBridge(OfficialBridge):
    def __init__(self, operations: object) -> None:
        super().__init__()
        self.operations = operations

    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        return OfficialBridgeVerification(
            create_save_reopen_snapshot_verified=True,
            verified_operations=self.operations,  # type: ignore[arg-type]
        )


def test_locate_treats_an_unreadable_configured_path_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "blocked-lceda-pro.exe"
    original_is_file = Path.is_file

    def deny_configured_path(path: Path) -> bool:
        if path == configured.resolve():
            raise PermissionError("access denied")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", deny_configured_path)

    assert LcedaProAdapter.locate(configured) is None


def test_probe_reports_an_installed_gui_without_claiming_write_support(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    adapter = LcedaProAdapter(FakeRunner("3.2.166"), executable)

    capability = adapter.probe()

    assert capability.available is True
    assert capability.profile_id == "lceda-pro-v1"
    assert capability.write_verified is False
    assert capability.operations == frozenset()
    assert capability.reason == "official_bridge_unconfigured"
    with pytest.raises(
        LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
    ):
        adapter.require(EdaOperation.APPLY_OPERATIONS, capability)


def test_probe_reports_a_missing_executable(tmp_path: Path) -> None:
    capability = LcedaProAdapter(FakeRunner("3.2.166"), tmp_path / "missing.exe").probe()

    assert capability.available is False
    assert capability.executable is None
    assert capability.reason == "lceda_pro_not_found"


def test_probe_rejects_an_executable_that_changes_during_discovery(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(
        FakeRunner("3.2.166", mutate=executable), executable
    ).probe()

    assert capability.available is False
    assert capability.reason == "executable_changed"


def test_probe_rejects_an_unparseable_version(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(FakeRunner("build latest"), executable).probe()

    assert capability.available is False
    assert capability.reason == "version_unparseable"


def test_probe_rejects_an_unsupported_version(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(FakeRunner("4.0.0"), executable).probe()

    assert capability.available is False
    assert capability.version == "4.0.0"
    assert capability.reason == "unsupported_version"


def test_probe_marks_writes_verified_only_after_official_bridge_contract(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()
    bridge = OfficialBridge()
    adapter = LcedaProAdapter(
        FakeRunner("3.2.166"), executable, official_bridge=bridge, fixture_dir=fixture_dir
    )

    capability = adapter.probe()

    assert bridge.identified == (executable.resolve(), "3.2.166")
    assert bridge.contract_fixture == fixture_dir.resolve()
    assert capability.write_verified is True
    assert capability.operations == frozenset(
        {
            EdaOperation.SNAPSHOT,
            EdaOperation.CREATE_CANDIDATE,
            EdaOperation.APPLY_OPERATIONS,
        }
    )
    assert capability.reason is None
    adapter.require(EdaOperation.APPLY_OPERATIONS, capability)
    with pytest.raises(
        LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
    ):
        adapter.require(EdaOperation.RUN_DRC, capability)


def test_probe_rejects_an_incomplete_official_bridge_contract(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=IncompleteOfficialBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.write_verified is False
    assert capability.operations == frozenset()
    assert capability.reason == "official_bridge_contract_incomplete"


def test_probe_snapshots_a_mutable_bridge_operation_set(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()
    bridge_operations = {
        EdaOperation.SNAPSHOT,
        EdaOperation.CREATE_CANDIDATE,
        EdaOperation.APPLY_OPERATIONS,
    }
    bridge = MutableOperationBridge(bridge_operations)

    adapter = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=bridge,
        fixture_dir=fixture_dir,
    )
    capability = adapter.probe()
    bridge_operations.update({EdaOperation.RUN_DRC, EdaOperation.EXPORT_RELEASE})

    assert isinstance(capability.operations, frozenset)
    assert capability.operations == frozenset(
        {
            EdaOperation.SNAPSHOT,
            EdaOperation.CREATE_CANDIDATE,
            EdaOperation.APPLY_OPERATIONS,
        }
    )
    for operation in (EdaOperation.RUN_DRC, EdaOperation.EXPORT_RELEASE):
        with pytest.raises(
            LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
        ):
            adapter.require(operation, capability)


@pytest.mark.parametrize(
    "operations",
    [
        object(),
        {EdaOperation.SNAPSHOT, "not-an-eda-operation"},
        {EdaOperation.SNAPSHOT, EdaOperation.RUN_DRC},
    ],
)
def test_probe_rejects_invalid_or_out_of_scope_bridge_operations(
    tmp_path: Path, operations: object
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=MutableOperationBridge(operations),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.write_verified is False
    assert capability.operations == frozenset()
    assert capability.reason == "official_bridge_contract_incomplete"


@pytest.mark.parametrize(
    "bridge",
    [MutateThenFalseBridge(), MutateThenRaiseIdentifyBridge(), MutateThenRaiseVerifyBridge()],
)
def test_probe_discards_stale_evidence_when_a_bridge_changes_the_executable(
    tmp_path: Path, bridge: OfficialBridge
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=bridge,
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.available is False
    assert capability.executable == executable.resolve()
    assert capability.version is None
    assert capability.executable_digest is None
    assert capability.profile_id is None
    assert capability.profile_revision is None
    assert capability.operations == frozenset()
    assert capability.write_verified is False
    assert capability.reason == "executable_changed"


class _FailingRunner:
    """A runner that always fails like a broken spawn would."""

    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: float
    ) -> ProcessResult:
        raise self._exception


class _ExitCodeRunner:
    """A runner reporting an executable that exits nonzero."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self._returncode = returncode
        self._stderr = stderr

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: float
    ) -> ProcessResult:
        return ProcessResult(tuple(argv), self._returncode, "", self._stderr, False)


class _MutatingTimeoutRunner:
    """A runner that swaps the executable and then times out."""

    def __init__(self, mutate: Path) -> None:
        self._mutate = mutate

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: float
    ) -> ProcessResult:
        self._mutate.write_bytes(b"changed")
        raise ProcessTimeoutError(tuple(argv), timeout_seconds)


class UnidentifiedBridge(OfficialBridge):
    def identify(self, executable: Path, version: str) -> bool:
        return False


class RaiseIdentifyBridge(OfficialBridge):
    def identify(self, executable: Path, version: str) -> bool:
        raise RuntimeError("bridge identify failed")


class MutateThenValidBridge(OfficialBridge):
    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        executable.write_bytes(b"changed")
        return OfficialBridgeVerification(
            create_save_reopen_snapshot_verified=True,
            verified_operations=frozenset({EdaOperation.SNAPSHOT}),
        )


class ForeignContractBridge(OfficialBridge):
    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification:
        return object()  # type: ignore[return-value]


def _deny_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(_executable: Path) -> str:
        raise OSError("read denied")

    monkeypatch.setattr(LcedaProAdapter, "_hash_executable", staticmethod(raise_oserror))


def _fixture_file(tmp_path: Path) -> Path:
    fixture = tmp_path / "minimal-fixture.txt"
    fixture.write_text("not a directory", encoding="utf-8")
    return fixture


def test_probe_reports_an_unreadable_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    _deny_hash(monkeypatch)

    capability = LcedaProAdapter(FakeRunner("3.2.166"), executable).probe()

    assert capability.available is False
    assert capability.reason == "executable_read_failed"


def test_probe_reports_a_version_command_timeout(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(
        _FailingRunner(ProcessTimeoutError(("lceda-pro.exe", "--version"), 120.0)),
        executable,
    ).probe()

    assert capability.available is False
    assert capability.reason == "version_command_timeout"
    assert capability.executable_digest is not None


def test_probe_reports_a_failed_version_spawn(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(
        _FailingRunner(OSError("spawning failed")), executable
    ).probe()

    assert capability.available is False
    assert capability.reason == "version_command_failed"


def test_probe_reports_a_nonzero_version_exit(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(_ExitCodeRunner(2, "crashed"), executable).probe()

    assert capability.available is False
    assert capability.reason == "version_command_failed"
    assert capability.executable_digest is not None


def test_probe_discards_evidence_when_the_executable_changes_before_version_output(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")

    capability = LcedaProAdapter(_MutatingTimeoutRunner(executable), executable).probe()

    assert capability.available is False
    assert capability.reason == "executable_changed"
    assert capability.executable_digest is None


@pytest.mark.parametrize(
    "fixture_factory",
    [
        lambda tmp_path: None,
        lambda tmp_path: tmp_path / "missing-minimal-dir",
        lambda tmp_path: _fixture_file(tmp_path),
    ],
    ids=["unset", "missing-dir", "file-instead-of-dir"],
)
def test_probe_reports_a_missing_verification_fixture(
    tmp_path: Path, fixture_factory
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = fixture_factory(tmp_path)

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=OfficialBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.available is True
    assert capability.write_verified is False
    assert capability.reason == "fixture_unavailable"


def test_probe_reports_an_unidentified_bridge(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=UnidentifiedBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.write_verified is False
    assert capability.reason == "official_bridge_unidentified"


def test_probe_reports_a_bridge_contract_failure_without_mutation(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=RaiseIdentifyBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.available is True
    assert capability.executable_digest is not None
    assert capability.reason == "official_bridge_contract_failed"


def test_probe_discards_evidence_when_a_valid_contract_changes_the_executable(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=MutateThenValidBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.available is False
    assert capability.reason == "executable_changed"


def test_probe_rejects_a_foreign_contract_result(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()

    capability = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=ForeignContractBridge(),
        fixture_dir=fixture_dir,
    ).probe()

    assert capability.write_verified is False
    assert capability.reason == "official_bridge_contract_incomplete"


def test_executable_matches_treats_an_unreadable_file_as_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deny_hash(monkeypatch)

    assert LcedaProAdapter._executable_matches(Path("unused.exe"), "sha256:x") is False


def test_verified_bridge_operations_still_fail_closed_on_unimplemented_native_calls(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    fixture_dir = tmp_path / "minimal"
    fixture_dir.mkdir()
    adapter = LcedaProAdapter(
        FakeRunner("3.2.166"),
        executable,
        official_bridge=OfficialBridge(),
        fixture_dir=fixture_dir,
    )

    with pytest.raises(
        UnsupportedEdaOperationError, match="LCEDA_PRO_SNAPSHOT_BRIDGE_UNIMPLEMENTED"
    ):
        adapter.load_snapshot(tmp_path)
    with pytest.raises(
        UnsupportedEdaOperationError, match="LCEDA_PRO_CANDIDATE_BRIDGE_UNIMPLEMENTED"
    ):
        adapter.create_candidate(tmp_path, tmp_path)
    with pytest.raises(
        UnsupportedEdaOperationError, match="LCEDA_PRO_WRITE_BRIDGE_UNIMPLEMENTED"
    ):
        adapter.apply_operations(None, (), None)
    # RUN_DRC and EXPORT_RELEASE are outside the frozen minimal-lifecycle
    # contract: even a fully verified bridge cannot authorize them here.
    with pytest.raises(
        LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
    ):
        adapter.run_drc(None)
    with pytest.raises(
        LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
    ):
        adapter.export_release(None, None)
