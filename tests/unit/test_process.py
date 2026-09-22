import os
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic, perf_counter, sleep

import pytest

from pcbflow.cancellation import TaskCancelledError, task_cancellation_scope
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
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            0.05,
        )

    assert raised.value.argv[0] == sys.executable
    assert raised.value.timeout_seconds == 0.05
    # Timeout handling must return before the child would have exited on its
    # own. Reaping the tree uses `taskkill /T /F`, which gives itself up to
    # five seconds to complete, so the bound has to cover that worst case
    # rather than the child's original sleep duration.
    assert perf_counter() - started < 30


def test_runner_terminates_a_process_when_its_task_is_cancelled(tmp_path: Path) -> None:
    cancelled = Event()
    marker = tmp_path / "child-started"
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with task_cancellation_scope(cancelled.is_set):
                ProcessRunner(max_output_bytes=1_024).run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; import sys, time; "
                            "Path(sys.argv[1]).write_text('started', encoding='utf-8'); "
                            "time.sleep(300)"
                        ),
                        str(marker),
                    ],
                    tmp_path,
                    300,
                )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    deadline = monotonic() + 2
    while not marker.exists() and monotonic() < deadline:
        sleep(0.01)
    assert marker.exists()

    cancelled.set()
    # Cancellation reaps the tree through `taskkill /T /F`, which may take up
    # to five seconds on a loaded machine; allow that before failing.
    thread.join(timeout=20)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TaskCancelledError)


def test_runner_reaps_process_when_cancellation_probe_raises(tmp_path: Path) -> None:
    heartbeat = tmp_path / "child-heartbeat"
    stop = tmp_path / "child-stop"

    def broken_checker() -> bool:
        if heartbeat.exists():
            raise RuntimeError("cancellation state is unavailable")
        return False

    try:
        with pytest.raises(RuntimeError, match="cancellation state"):
            with task_cancellation_scope(broken_checker):
                ProcessRunner(max_output_bytes=1_024).run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path\n"
                            "import sys\n"
                            "import time\n"
                            "heartbeat = Path(sys.argv[1])\n"
                            "stop = Path(sys.argv[2])\n"
                            "while not stop.exists():\n"
                            "    heartbeat.write_text(str(time.monotonic()), encoding='utf-8')\n"
                            "    time.sleep(0.01)\n"
                        ),
                        str(heartbeat),
                        str(stop),
                    ],
                    tmp_path,
                    30,
                )

        deadline = monotonic() + 2
        while not heartbeat.exists() and monotonic() < deadline:
            sleep(0.01)
        assert heartbeat.exists()
        before = heartbeat.read_text(encoding="utf-8")
        sleep(0.15)
        assert heartbeat.read_text(encoding="utf-8") == before
    finally:
        stop.write_text("stop", encoding="utf-8")


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
