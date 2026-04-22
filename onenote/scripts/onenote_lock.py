#!python3
"""Process-level lock using `fcntl.flock` — same pattern as sync.py.

Two guarantees:
  1. Concurrency: only one process holds the named lock at a time; parallel
     invocations (e.g., two harnesses running fetch-media simultaneously)
     fail fast rather than racing.
  2. Zombie recovery: the lockfile body carries {pid, started_at, hostname}
     so `unstick(name)` can SIGTERM/SIGKILL a hung owner even if the kernel
     hasn't released the fcntl lock yet (which it should, on process exit).

Usage:
    from onenote_lock import ProcessLock, LockHeldError, unstick, read_lock_body

    with ProcessLock('fetch_media'):
        ...do work...
"""
import fcntl
import json
import os
import signal
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from onenote_cache import REFS_DIR


class LockHeldError(RuntimeError):
    """Raised when the named lock is held by another process."""


class DurationExceeded(Exception):
    """Raised by a SIGALRM handler when a `duration_limit` block exceeds
    its max wall-clock time. Propagates through asyncio.run, subprocess
    waits, network I/O, etc. — the signal fires regardless of what the
    process is blocked on."""


def _alarm_handler(signum, frame):
    raise DurationExceeded('exceeded max-duration')


@contextmanager
def duration_limit(seconds: int, label: str = 'operation'):
    """Scope-based SIGALRM timeout. `seconds<=0` disables the alarm.

    Usage:
        with duration_limit(600, 'fetch-media'):
            asyncio.run(long_running_work())

    On timeout, raises DurationExceeded. Not reentrant — nested calls
    would step on each other's SIGALRM handler. Restoring the previous
    handler in `finally` is best-effort.
    """
    if seconds <= 0:
        yield
        return
    prev = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def _lock_path(name: str) -> Path:
    return REFS_DIR / f'.{name}.lock'


class ProcessLock:
    """Non-blocking fcntl-based exclusive lock.

    On acquisition, writes {pid, started_at, hostname} to the lockfile body.
    The kernel auto-releases the fcntl lock on process exit so crashes can't
    wedge the system; the body remains for diagnostics / manual unstick.
    """

    def __init__(self, name: str):
        self.name = name
        self.path = _lock_path(name)
        self._fd = None

    def __enter__(self):
        REFS_DIR.mkdir(parents=True, exist_ok=True)
        # Open without O_TRUNC so a concurrent loser doesn't clobber the
        # winner's body (which --status / --unstick rely on).
        raw_fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(raw_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(raw_fd)
            body = read_lock_body(self.name)
            pid = body.get('pid', '?')
            started = body.get('started_at', '?')
            host = body.get('hostname', '?')
            raise LockHeldError(
                f"another '{self.name}' run is in progress "
                f"(pid={pid} on {host}, started {started}). "
                f"If wedged: onenote_ops.py "
                f"{self.name.replace('_', '-')} --unstick"
            )
        # Lock held — now safe to replace body with our identity.
        os.ftruncate(raw_fd, 0)
        self._fd = os.fdopen(raw_fd, 'w')
        self._fd.write(json.dumps({
            'pid':         os.getpid(),
            'started_at':  datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'hostname':    socket.gethostname(),
        }) + '\n')
        self._fd.flush()
        os.fsync(self._fd.fileno())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fd.close()
            self._fd = None
        return False


def read_lock_body(name: str) -> dict:
    """Return {pid, started_at, hostname} from the lockfile body, or {}."""
    try:
        return json.loads(_lock_path(name).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def is_locked(name: str) -> bool:
    """True iff a process currently holds the named lock. Probes with a
    non-blocking flock (and releases immediately) rather than trusting the
    lockfile body, which can be stale after an abnormal exit."""
    path = _lock_path(name)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def unstick(name: str, timeout_sec: float = 5.0) -> dict:
    """Identify and terminate a stuck process holding the named lock.

    Returns dict with `action` ∈ {no_lock, no_pid, already_gone,
    terminated, killed, no_permission} plus pid/elapsed where relevant.
    """
    body = read_lock_body(name)
    path = _lock_path(name)
    if not body:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return {'action': 'no_lock'}

    pid = body.get('pid')
    if not pid:
        return {'action': 'no_pid', 'body': body}

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'action': 'already_gone', 'pid': pid}
    except PermissionError:
        return {'action': 'no_permission', 'pid': pid}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {'action': 'already_gone', 'pid': pid}

    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _clear_stale_body(name)
            return {'action': 'terminated', 'pid': pid,
                    'elapsed': round(time.time() - t0, 1)}
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
        _clear_stale_body(name)
        return {'action': 'killed', 'pid': pid}
    except ProcessLookupError:
        _clear_stale_body(name)
        return {'action': 'already_gone', 'pid': pid}


def _clear_stale_body(name: str) -> None:
    """Remove the lockfile so --status doesn't ghost. Safe: the kernel has
    already released the fcntl lock when the owner exited, and anyone else
    acquiring after us will recreate the file."""
    try:
        _lock_path(name).unlink()
    except FileNotFoundError:
        pass
