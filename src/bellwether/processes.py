"""Subprocess and long-lived process management.

Two rules are enforced here and nowhere else in the package:

* **No shell.** Every command is an argument vector. ``shell=True`` with an
  interpolated URL was the remote-code-execution hole in the previous version.
* **Process groups.** Children are started in their own session, so killing a
  launched app kills the whole tree. The old code recorded ``$!`` after
  ``npm start``, which is the npm wrapper's PID -- killing it orphaned the node
  server that actually held the port, and the next deployment then failed to
  bind.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import BellwetherError
from .logging_setup import get_logger

log = get_logger(__name__)

LineSink = Callable[[str], None]

DEFAULT_GRACE_PERIOD_S = 5.0

# Handles for processes we deliberately detach. Popen warns (and cannot reap)
# if it is garbage-collected while its child still runs, so we hold the handle
# until the child is actually gone.
_detached: dict[int, subprocess.Popen[bytes]] = {}
_detached_lock = threading.Lock()


class CommandError(BellwetherError):
    """A command exited non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int, tail: str = "") -> None:
        rendered = " ".join(argv)
        message = f"command failed (exit {returncode}): {rendered}"
        if tail:
            message += f"\n{tail}"
        super().__init__(message)
        self.argv = list(argv)
        self.returncode = returncode
        self.tail = tail


class CommandTimeout(BellwetherError):
    """A command exceeded its timeout and was killed."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    output: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def which(program: str) -> str | None:
    """Resolve ``program`` on PATH, or return ``None``."""
    return shutil.which(program)


def require(program: str) -> str:
    """Resolve ``program`` on PATH or raise a clear, actionable error."""
    found = shutil.which(program)
    if not found:
        raise BellwetherError(
            f"required executable {program!r} was not found on PATH. "
            "Install it, or adjust the runtime detection for this repository."
        )
    return found


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    on_line: LineSink | None = None,
    check: bool = True,
    tail_lines: int = 40,
) -> CommandResult:
    """Run ``argv`` to completion, streaming combined output to ``on_line``.

    Never uses a shell. Output is streamed rather than buffered so a long
    dependency install shows progress in the dashboard instead of appearing to
    hang, and so a chatty build cannot fill the OS pipe buffer and deadlock --
    which is exactly what the previous version worked around by discarding
    output entirely.
    """
    if not argv:
        raise ValueError("argv must not be empty")

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    started = time.monotonic()
    collected: list[str] = []
    log.debug("running command", extra={"argv": list(argv), "cwd": str(cwd) if cwd else None})

    process = subprocess.Popen(  # noqa: S603 - argv form, never a shell string
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    # Drain the pipe on a dedicated thread. Reading inline would mean the
    # timeout could only be observed when the child happened to emit a line, so
    # a child that hangs silently would hang us with it -- and never reading the
    # pipe at all (the previous behaviour) deadlocks as soon as the child writes
    # more than one buffer's worth of output.
    def _drain() -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - stdout is always a pipe here
            return
        for line in stream:
            text = line.rstrip("\n")
            collected.append(text)
            if on_line is not None:
                try:
                    on_line(text)
                except Exception:
                    log.exception("output sink raised")

    reader = threading.Thread(target=_drain, name="cmd-reader", daemon=True)
    reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process.pid, sig=signal.SIGTERM)
        try:
            returncode = process.wait(timeout=DEFAULT_GRACE_PERIOD_S)
        except subprocess.TimeoutExpired:
            _terminate_group(process.pid, sig=signal.SIGKILL)
            returncode = process.wait(timeout=DEFAULT_GRACE_PERIOD_S)
    finally:
        reader.join(timeout=2.0)
        if process.stdout is not None:
            process.stdout.close()

    if timed_out:
        raise CommandTimeout(
            f"command timed out after {timeout:.0f}s and was terminated: {' '.join(argv)}"
        )

    result = CommandResult(
        argv=list(argv),
        returncode=returncode,
        output="\n".join(collected),
        duration_s=round(time.monotonic() - started, 3),
    )
    if check and not result.ok:
        raise CommandError(argv, returncode, "\n".join(collected[-tail_lines:]))
    return result


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
) -> int:
    """Start a long-lived background process and return its PID.

    stdout and stderr are redirected to ``log_path``. The previous version piped
    them into the parent and then never read the pipe, so any application that
    logged more than one pipe buffer's worth of output blocked forever.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(  # noqa: S603 - argv form, never a shell string
            list(argv),
            cwd=str(cwd),
            env=merged_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()

    with _detached_lock:
        _detached[process.pid] = process
        _reap_detached_locked()

    log.info(
        "spawned background process",
        extra={"pid": process.pid, "argv": list(argv), "log": str(log_path)},
    )
    return process.pid


def _reap_detached_locked() -> None:
    """Drop handles whose child has exited. Caller holds ``_detached_lock``."""
    for pid, handle in list(_detached.items()):
        if handle.poll() is not None:
            del _detached[pid]


def is_running(pid: int) -> bool:
    """Whether ``pid`` refers to a live process we are allowed to signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def _terminate_group(pid: int, *, sig: int = signal.SIGTERM) -> None:
    """Signal the whole process group, falling back to the bare PID."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


def terminate(pid: int, *, grace_period_s: float = DEFAULT_GRACE_PERIOD_S) -> bool:
    """Stop ``pid`` and its group, escalating SIGTERM to SIGKILL.

    Returns ``True`` if the process is gone afterwards. A graceful SIGTERM
    first gives servers a chance to finish in-flight requests; the previous
    implementation went straight to ``kill -9``, which drops connections and
    corrupts any half-written state.
    """
    if not is_running(pid):
        return True

    _terminate_group(pid, sig=signal.SIGTERM)
    deadline = time.monotonic() + grace_period_s
    while time.monotonic() < deadline:
        if not is_running(pid):
            _reap(pid)
            return True
        time.sleep(0.1)

    log.warning("process did not exit on SIGTERM; sending SIGKILL", extra={"pid": pid})
    _terminate_group(pid, sig=signal.SIGKILL)
    time.sleep(0.2)
    _reap(pid)
    return not is_running(pid)


def _reap(pid: int) -> None:
    """Collect the exit status so the child does not linger as a zombie."""
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


# ── PID files ─────────────────────────────────────────────────────────────────


def write_pid_file(path: Path, pid: int) -> None:
    from .atomicio import write_atomic

    write_atomic(path, str(pid))


def read_pid_file(path: Path) -> int | None:
    from .atomicio import read_text

    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("pid file is not an integer; ignoring", extra={"path": str(path)})
        return None


def stop_pid_file(path: Path, *, grace_period_s: float = DEFAULT_GRACE_PERIOD_S) -> bool:
    """Terminate the process recorded in ``path`` and remove the file."""
    pid = read_pid_file(path)
    path.unlink(missing_ok=True)
    if pid is None:
        return True
    stopped = terminate(pid, grace_period_s=grace_period_s)
    if stopped:
        log.info("stopped process", extra={"pid": pid, "pid_file": str(path)})
    else:
        log.error("failed to stop process", extra={"pid": pid, "pid_file": str(path)})
    return stopped


def free_port(port: int, *, host: str = "127.0.0.1") -> None:
    """Best-effort reclaim of ``port`` from a process we did not start.

    Uses ``lsof`` when available. Unlike the previous ``lsof -ti | xargs kill -9``
    one-liner this never runs ``kill`` with an empty argument list and never
    signals PID 0.
    """
    lsof = shutil.which("lsof")
    if not lsof:
        return
    try:
        completed = subprocess.run(  # noqa: S603 - argv form
            [lsof, "-ti", f"tcp@{host}:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return

    for token in completed.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0 and pid != os.getpid():
            log.warning("reclaiming port from stray process", extra={"port": port, "pid": pid})
            terminate(pid, grace_period_s=2.0)


def build_env(**overrides: str | None) -> dict[str, str]:
    """Environment overrides with ``None`` values dropped."""
    return {key: value for key, value in overrides.items() if value is not None}
