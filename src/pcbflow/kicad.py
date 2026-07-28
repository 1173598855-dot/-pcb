from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pcbflow.process import ProcessPort, ProcessTimeoutError

_VERSION = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)")


@dataclass(frozen=True, slots=True)
class KicadCapability:
    available: bool
    path: Path | None
    version: str | None
    executable_digest: str | None
    reason: str | None


class KicadCli:
    def __init__(
        self,
        runner: ProcessPort,
        executable: Path | None,
        timeout_seconds: float,
    ) -> None:
        self._runner = runner
        self._executable = executable.resolve() if executable is not None else None
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def locate(configured: Path | None = None) -> Path | None:
        if configured is not None:
            candidate = configured.resolve()
            return candidate if candidate.is_file() else None

        from_path = shutil.which("kicad-cli")
        if from_path:
            candidate = Path(from_path).resolve()
            if candidate.is_file():
                return candidate

        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            candidates = sorted(
                program_files.glob("KiCad/*/bin/kicad-cli.exe"),
                key=lambda path: path.parts[-3],
                reverse=True,
            )
            for candidate in candidates:
                if candidate.is_file():
                    return candidate.resolve()
        return None

    def probe(self) -> KicadCapability:
        if self._executable is None or not self._executable.is_file():
            return KicadCapability(False, None, None, None, "kicad_cli_not_found")

        try:
            digest = self._hash_executable(self._executable)
            result = self._runner.run(
                [str(self._executable), "--version"],
                self._executable.parent,
                self._timeout_seconds,
            )
        except ProcessTimeoutError:
            return KicadCapability(
                False, self._executable, None, None, "version_command_timeout"
            )
        except OSError:
            return KicadCapability(
                False, self._executable, None, None, "executable_read_failed"
            )

        if result.returncode != 0:
            return KicadCapability(
                False, self._executable, None, digest, "version_command_failed"
            )
        match = _VERSION.search(result.stdout)
        if match is None:
            return KicadCapability(
                False, self._executable, None, digest, "version_unparseable"
            )
        return KicadCapability(
            True, self._executable, match.group(1), digest, None
        )

    @staticmethod
    def _hash_executable(executable: Path) -> str:
        digest = hashlib.sha256()
        with executable.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
