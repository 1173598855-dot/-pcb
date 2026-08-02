import os
import sys
from pathlib import Path
from time import perf_counter

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
    # Windows taskkill /T /F may take a couple of seconds to reap a process
    # tree, but timeout handling must still return well before the child exits.
    assert perf_counter() - started < 4


def test_runner_rejects_empty_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv"):
        ProcessRunner(max_output_bytes=1_024).run([], tmp_path, 1)


def test_runner_uses_controlled_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PCBFLOW_SECRET_SHOULD_NOT_LEAK", "secret")
    runner = ProcessRunner(10_000)
    result = runner.run(
        [
            sys.executable,
            "-c",
            (
                "import os;"
                "print(os.environ.get('PCBFLOW_VISIBLE'));"
                "print(os.environ.get('PCBFLOW_SECRET_SHOULD_NOT_LEAK'))"
            ),
        ],
        tmp_path,
        10,
        env={"PCBFLOW_VISIBLE": "yes"},
    )

    assert result.stdout.splitlines() == ["yes", "None"]
    expected_bytes = b"yes\r\nNone\r\n" if os.name == "nt" else b"yes\nNone\n"
    assert result.stdout_bytes == expected_bytes
