from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    output_truncated: bool
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


class ProcessTimeoutError(TimeoutError):
    def __init__(self, argv: Sequence[str], timeout_seconds: float) -> None:
        super().__init__(f"process timed out after {timeout_seconds}s: {argv[0]}")
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds


class ProcessPort(Protocol):
    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult: ...


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(65_536):
                remaining = self._limit - len(self._data)
                if remaining > 0:
                    self._data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        finally:
            stream.close()

    def bytes(self) -> bytes:
        return bytes(self._data)

    def text(self) -> str:
        return self.bytes().decode("utf-8", errors="replace")


class ProcessRunner:
    def __init__(self, max_output_bytes: int) -> None:
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes must not be negative")
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        if not argv:
            raise ValueError("argv must not be empty")
        working_directory = cwd.resolve(strict=True)
        if not working_directory.is_dir():
            raise ValueError("cwd must be a directory")

        allowed_names = (
            "PATH",
            "PATHEXT",
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
        )
        process_env = {
            name: os.environ[name] for name in allowed_names if name in os.environ
        }
        if env is not None:
            process_env.update({str(key): str(value) for key, value in env.items()})

        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True

        process = subprocess.Popen(
            [str(value) for value in argv],
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=process_env,
            **options,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = _BoundedCapture(self._max_output_bytes)
        stderr = _BoundedCapture(self._max_output_bytes)
        stdout_thread = threading.Thread(target=stdout.drain, args=(process.stdout,))
        stderr_thread = threading.Thread(target=stderr.drain, args=(process.stderr,))
        stdout_thread.start()
        stderr_thread.start()

        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self._terminate_tree(process, process_env)
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise ProcessTimeoutError(argv, timeout_seconds) from error

        stdout_thread.join()
        stderr_thread.join()
        return ProcessResult(
            argv=tuple(str(value) for value in argv),
            returncode=returncode,
            stdout=stdout.text(),
            stderr=stderr.text(),
            output_truncated=stdout.truncated or stderr.truncated,
            stdout_bytes=stdout.bytes(),
            stderr_bytes=stderr.bytes(),
        )

    @staticmethod
    def _terminate_tree(
        process: subprocess.Popen[bytes], process_env: Mapping[str, str]
    ) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=5,
                    check=False,
                    env=dict(process_env),
                )
            except (OSError, subprocess.SubprocessError):
                process.kill()
            else:
                if result.returncode != 0 and process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
