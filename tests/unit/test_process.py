import sys
from time import perf_counter
from pathlib import Path

import pytest

from pcbflow.process import ProcessRunner, ProcessTimeoutError


def test_runner_uses_argument_array_and_captures_output(tmp_path: Path) -> None:
    result = ProcessRunner(max_output_bytes=1_024).run(
        [sys.executable, "-c", "print('ready')"], tmp_path, 5
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ready"
    assert result.stderr == ""
    assert result.argv[0] == sys.executable
    assert not result.output_truncated


def test_runner_bounds_captured_output(tmp_path: Path) -> None:
    result = ProcessRunner(max_output_bytes=4).run(
        [sys.executable, "-c", "print('abcdefgh', end='')"], tmp_path, 5
    )

    assert result.stdout == "abcd"
    assert result.output_truncated


def test_runner_reports_timeout(tmp_path: Path) -> None:
    started = perf_counter()
    with pytest.raises(ProcessTimeoutError) as raised:
        ProcessRunner(max_output_bytes=1_024).run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            tmp_path,
            0.05,
        )

    assert raised.value.argv[0] == sys.executable
    assert raised.value.timeout_seconds == 0.05
    assert perf_counter() - started < 1.5


def test_runner_rejects_empty_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv"):
        ProcessRunner(max_output_bytes=1_024).run([], tmp_path, 1)
