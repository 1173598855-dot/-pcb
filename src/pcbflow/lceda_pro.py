from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from pcbflow.eda import EdaCapability
from pcbflow.domain import EdaOperation, RequestInvalidError
from pcbflow.process import ProcessPort, ProcessTimeoutError
from pcbflow.board.adapter import CandidateWorkspace, ReleaseArtifacts, UnsupportedEdaOperationError
from pcbflow.board.ir import BoardSnapshot
from pcbflow.board.operations import BoardOperation
from pcbflow.board.rulepack import ManufacturingRulePack
from pcbflow.domain import ValidationReport


_VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
_SUPPORTED_VERSION = re.compile(r"3\.2\.\d+\Z")
_PROFILE_ID = "lceda-pro-v1"
_PROFILE_REVISION = 1
_MINIMAL_LIFECYCLE_OPERATIONS = frozenset(
    {
        EdaOperation.SNAPSHOT,
        EdaOperation.CREATE_CANDIDATE,
        EdaOperation.APPLY_OPERATIONS,
    }
)


class EdaCapabilityError(RequestInvalidError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LcedaProCapabilityError(EdaCapabilityError, UnsupportedEdaOperationError):
    pass


@dataclass(frozen=True, slots=True)
class OfficialBridgeVerification:
    create_save_reopen_snapshot_verified: bool
    verified_operations: frozenset[EdaOperation]


class OfficialAutomationBridge(Protocol):
    """A vendor-supported automation endpoint verified against the frozen fixture."""

    def identify(self, executable: Path, version: str) -> bool: ...

    def verify_minimal_contract(
        self, executable: Path, version: str, fixture_dir: Path
    ) -> OfficialBridgeVerification: ...


def require_lceda_operation(
    capability: EdaCapability, operation: EdaOperation
) -> None:
    if not capability.write_verified or operation not in capability.operations:
        raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")


class LcedaProAdapter:
    def __init__(
        self,
        runner: ProcessPort,
        executable: Path | None,
        timeout_seconds: float = 120,
        *,
        official_bridge: OfficialAutomationBridge | None = None,
        fixture_dir: Path | None = None,
        configured_bridge: Path | None = None,
    ) -> None:
        self._runner = runner
        self._executable = executable.resolve() if executable is not None else None
        self._timeout_seconds = timeout_seconds
        self._official_bridge = official_bridge
        self._fixture_dir = fixture_dir.resolve() if fixture_dir is not None else None
        self._configured_bridge = configured_bridge

    @staticmethod
    def locate(configured: Path | None = None) -> Path | None:
        if configured is None:
            return None
        try:
            candidate = configured.resolve()
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    def probe(self) -> EdaCapability:
        executable = self._executable
        if executable is None or not executable.is_file():
            return self._unavailable("lceda_pro_not_found")
        try:
            digest = self._hash_executable(executable)
        except OSError:
            return self._unavailable("executable_read_failed", executable)
        try:
            result = self._runner.run(
                [str(executable), "--version"], executable.parent, self._timeout_seconds
            )
        except ProcessTimeoutError:
            return self._after_version_failure(executable, digest, "version_command_timeout")
        except OSError:
            return self._after_version_failure(executable, digest, "version_command_failed")
        if not self._executable_matches(executable, digest):
            return self._unavailable("executable_changed", executable)
        if result.returncode != 0:
            return self._unavailable("version_command_failed", executable, digest=digest)
        match = _VERSION.search(result.stdout)
        if match is None:
            return self._unavailable("version_unparseable", executable, digest=digest)
        version = match.group(1)
        if _SUPPORTED_VERSION.fullmatch(version) is None:
            return self._unavailable(
                "unsupported_version", executable, version=version, digest=digest
            )

        capability = self._verify_bridge(executable, version, digest)
        return capability

    def require(self, operation: EdaOperation, capability: EdaCapability) -> None:
        require_lceda_operation(capability, operation)

    def load_snapshot(self, project_dir: Path) -> BoardSnapshot:
        self.require(EdaOperation.SNAPSHOT, self.probe())
        raise UnsupportedEdaOperationError("LCEDA_PRO_SNAPSHOT_BRIDGE_UNIMPLEMENTED")

    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace:
        self.require(EdaOperation.CREATE_CANDIDATE, self.probe())
        raise UnsupportedEdaOperationError("LCEDA_PRO_CANDIDATE_BRIDGE_UNIMPLEMENTED")

    def apply_operations(
        self,
        candidate: CandidateWorkspace,
        operations: tuple[BoardOperation, ...],
        expected_snapshot: BoardSnapshot,
    ) -> BoardSnapshot:
        # Capability is checked before inspecting the candidate or its native files.
        self.require(EdaOperation.APPLY_OPERATIONS, self.probe())
        raise UnsupportedEdaOperationError("LCEDA_PRO_WRITE_BRIDGE_UNIMPLEMENTED")

    def run_drc(self, candidate: CandidateWorkspace) -> tuple[ValidationReport, ...]:
        self.require(EdaOperation.RUN_DRC, self.probe())
        raise UnsupportedEdaOperationError("LCEDA_PRO_DRC_BRIDGE_UNIMPLEMENTED")

    def export_release(
        self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack
    ) -> ReleaseArtifacts:
        self.require(EdaOperation.EXPORT_RELEASE, self.probe())
        raise UnsupportedEdaOperationError("LCEDA_PRO_EXPORT_BRIDGE_UNIMPLEMENTED")

    def _verify_bridge(
        self, executable: Path, version: str, digest: str
    ) -> EdaCapability:
        if self._official_bridge is None:
            reason = (
                "official_bridge_not_supported"
                if self._configured_bridge is not None
                else "official_bridge_unconfigured"
            )
            return self._unverified(executable, version, digest, reason)
        if self._fixture_dir is None or not self._fixture_dir.is_dir():
            return self._unverified(executable, version, digest, "fixture_unavailable")
        try:
            identified = self._official_bridge.identify(executable, version)
        except Exception:
            return self._after_bridge_call(
                executable, version, digest, "official_bridge_contract_failed"
            )
        if not self._executable_matches(executable, digest):
            return self._unavailable("executable_changed", executable)
        if not identified:
            return self._unverified(
                executable, version, digest, "official_bridge_unidentified"
            )
        try:
            verification = self._official_bridge.verify_minimal_contract(
                executable, version, self._fixture_dir
            )
        except Exception:
            return self._after_bridge_call(
                executable, version, digest, "official_bridge_contract_failed"
            )
        if not self._executable_matches(executable, digest):
            return self._unavailable("executable_changed", executable)
        if not isinstance(verification, OfficialBridgeVerification):
            return self._unverified(
                executable, version, digest, "official_bridge_contract_incomplete"
            )
        try:
            operations = frozenset(verification.verified_operations)
        except TypeError:
            return self._unverified(
                executable, version, digest, "official_bridge_contract_incomplete"
            )
        if (
            not verification.create_save_reopen_snapshot_verified
            or not operations
            or not all(isinstance(operation, EdaOperation) for operation in operations)
            or not operations <= _MINIMAL_LIFECYCLE_OPERATIONS
        ):
            return self._unverified(
                executable, version, digest, "official_bridge_contract_incomplete"
            )
        return EdaCapability(
            available=True,
            executable=executable,
            version=version,
            executable_digest=digest,
            profile_id=_PROFILE_ID,
            profile_revision=_PROFILE_REVISION,
            operations=operations,
            write_verified=EdaOperation.APPLY_OPERATIONS in operations,
            reason=None,
        )

    def _after_bridge_call(
        self, executable: Path, version: str, digest: str, reason: str
    ) -> EdaCapability:
        if not self._executable_matches(executable, digest):
            return self._unavailable("executable_changed", executable)
        return self._unverified(executable, version, digest, reason)

    @staticmethod
    def _hash_executable(executable: Path) -> str:
        digest = hashlib.sha256()
        with executable.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @classmethod
    def _executable_matches(cls, executable: Path, expected_digest: str) -> bool:
        try:
            return cls._hash_executable(executable) == expected_digest
        except OSError:
            return False

    def _after_version_failure(
        self, executable: Path, digest: str, reason: str
    ) -> EdaCapability:
        if not self._executable_matches(executable, digest):
            return self._unavailable("executable_changed", executable)
        return self._unavailable(reason, executable, digest=digest)

    @staticmethod
    def _unavailable(
        reason: str,
        executable: Path | None = None,
        *,
        version: str | None = None,
        digest: str | None = None,
    ) -> EdaCapability:
        return EdaCapability(
            available=False,
            executable=executable,
            version=version,
            executable_digest=digest,
            profile_id=None,
            profile_revision=None,
            operations=frozenset(),
            write_verified=False,
            reason=reason,
        )

    @staticmethod
    def _unverified(
        executable: Path, version: str, digest: str, reason: str
    ) -> EdaCapability:
        return EdaCapability(
            available=True,
            executable=executable,
            version=version,
            executable_digest=digest,
            profile_id=_PROFILE_ID,
            profile_revision=_PROFILE_REVISION,
            operations=frozenset(),
            write_verified=False,
            reason=reason,
        )
