"""Subprocess and process-group management.

The rule these enforce: no shell, and kill the whole tree. The previous
implementation used ``shell=True`` everywhere and recorded the wrapper's PID
rather than the process actually holding the port.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from bellwether import processes
from bellwether.errors import BellwetherError


class TestRun:
    def test_captures_output(self) -> None:
        result = processes.run([sys.executable, "-c", "print('hello')"])
        assert result.ok
        assert "hello" in result.output

    def test_streams_lines_to_the_sink(self) -> None:
        lines: list[str] = []
        processes.run([sys.executable, "-c", "for i in range(5): print(i)"], on_line=lines.append)
        assert lines == ["0", "1", "2", "3", "4"]

    def test_nonzero_exit_raises_with_the_output(self) -> None:
        with pytest.raises(processes.CommandError) as excinfo:
            processes.run([sys.executable, "-c", "import sys; print('why'); sys.exit(3)"])
        assert excinfo.value.returncode == 3
        assert "why" in str(excinfo.value)

    def test_check_false_returns_instead_of_raising(self) -> None:
        result = processes.run([sys.executable, "-c", "import sys; sys.exit(9)"], check=False)
        assert result.returncode == 9
        assert not result.ok

    def test_shell_metacharacters_are_inert(self, tmp_path: Path) -> None:
        """The RCE regression, at the execution layer.

        Under ``shell=True`` this would create the file. With an argv vector it
        is just an odd argument to `echo`.
        """
        canary = tmp_path / "pwned"
        processes.run(["echo", f"x; touch {canary}"], check=False)
        assert not canary.exists()

    def test_large_output_does_not_deadlock(self) -> None:
        """More than one pipe buffer's worth. The old code never drained it."""
        result = processes.run([sys.executable, "-c", "print('x' * 200000)"], timeout=30)
        assert result.ok
        assert len(result.output) > 100_000

    def test_a_silent_hang_still_times_out(self) -> None:
        """Reading inline would only notice the timeout when a line arrived."""
        started = time.monotonic()
        with pytest.raises(processes.CommandTimeout):
            processes.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
        assert time.monotonic() - started < 12

    def test_env_overrides_are_applied(self) -> None:
        result = processes.run(
            [sys.executable, "-c", "import os; print(os.environ['BW_TEST'])"],
            env={"BW_TEST": "value"},
        )
        assert "value" in result.output

    def test_cwd_is_respected(self, tmp_path: Path) -> None:
        result = processes.run(
            [sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path
        )
        # Resolve both sides: on macOS /tmp is a symlink into /private/tmp.
        assert Path(result.output.strip()).resolve() == tmp_path.resolve()

    def test_empty_argv_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            processes.run([])

    def test_a_raising_sink_does_not_fail_the_command(self) -> None:
        def bad(_line: str) -> None:
            raise RuntimeError("sink is broken")

        assert processes.run([sys.executable, "-c", "print(1)"], on_line=bad).ok


class TestRequire:
    def test_resolves_an_existing_program(self) -> None:
        assert processes.require("echo")

    def test_missing_program_raises_an_actionable_error(self) -> None:
        with pytest.raises(BellwetherError, match="not found on PATH"):
            processes.require("definitely-not-a-real-program-xyz")

    def test_which_returns_none_rather_than_raising(self) -> None:
        assert processes.which("definitely-not-a-real-program-xyz") is None


class TestLifecycle:
    def test_spawn_writes_a_log_and_the_process_runs(self, tmp_path: Path) -> None:
        log_path = tmp_path / "out.log"
        pid = processes.spawn(
            [
                sys.executable,
                "-c",
                "import sys,time; print('up'); sys.stdout.flush(); time.sleep(20)",
            ],
            cwd=tmp_path,
            log_path=log_path,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and "up" not in log_path.read_text():
                time.sleep(0.1)
            assert "up" in log_path.read_text()
            assert processes.is_running(pid)
        finally:
            processes.terminate(pid)
        assert not processes.is_running(pid)

    def test_is_running_is_false_for_a_dead_pid(self) -> None:
        assert processes.is_running(999_999) is False
        assert processes.is_running(0) is False
        assert processes.is_running(-1) is False

    def test_terminate_escalates_to_sigkill(self, tmp_path: Path) -> None:
        """A process ignoring SIGTERM must still be stopped."""
        script = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        )
        pid = processes.spawn(
            [sys.executable, "-c", script], cwd=tmp_path, log_path=tmp_path / "l.log"
        )
        time.sleep(0.7)
        assert processes.terminate(pid, grace_period_s=0.5) is True
        assert not processes.is_running(pid)

    def test_terminate_kills_the_whole_group(self, tmp_path: Path) -> None:
        """``npm start`` holds no port; its child does. Killing one must kill both."""
        script = (
            "import subprocess, sys, time;"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(c.pid, flush=True); time.sleep(60)"
        )
        log_path = tmp_path / "l.log"
        parent = processes.spawn([sys.executable, "-c", script], cwd=tmp_path, log_path=log_path)

        deadline = time.monotonic() + 10
        child = None
        while time.monotonic() < deadline:
            text = log_path.read_text().strip()
            if text.isdigit():
                child = int(text)
                break
            time.sleep(0.1)
        assert child is not None, "child pid was never reported"

        processes.terminate(parent, grace_period_s=1.0)
        time.sleep(0.5)
        assert not processes.is_running(parent)
        assert not processes.is_running(child), "the child outlived its group"

    def test_terminating_a_dead_pid_is_a_noop(self) -> None:
        assert processes.terminate(999_999) is True


class TestPidFiles:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "x.pid"
        processes.write_pid_file(path, 4242)
        assert processes.read_pid_file(path) == 4242

    def test_missing_file_reads_as_none(self, tmp_path: Path) -> None:
        assert processes.read_pid_file(tmp_path / "absent.pid") is None

    def test_garbage_reads_as_none(self, tmp_path: Path) -> None:
        path = tmp_path / "x.pid"
        path.write_text("not-a-pid")
        assert processes.read_pid_file(path) is None

    def test_stop_removes_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "x.pid"
        processes.write_pid_file(path, 999_999)
        assert processes.stop_pid_file(path) is True
        assert not path.exists()

    def test_stop_with_no_file_succeeds(self, tmp_path: Path) -> None:
        assert processes.stop_pid_file(tmp_path / "absent.pid") is True


class TestBuildEnv:
    def test_drops_none_values(self) -> None:
        assert processes.build_env(A="1", B=None) == {"A": "1"}


class TestFreePort:
    def test_never_signals_the_current_process(self) -> None:
        """``lsof -ti | xargs kill -9`` with no matches used to run bare kill."""
        processes.free_port(1)  # nothing listens here; must be a clean no-op
        assert processes.is_running(os.getpid())
