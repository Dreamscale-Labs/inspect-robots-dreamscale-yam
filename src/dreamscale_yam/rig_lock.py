"""Advisory per-rig locks so hardware commands never overlap on one rig.

``run`` holds its rig's lock for its whole lifetime. Commands that disturb the
hardware from outside a run (the CAN unplug flow, the camera preview) take
every configured rig's lock without waiting, and refuse with a plain message
when a run holds one. POSIX ``flock`` locks are released by the kernel when a
process exits, so a crashed run can never leave a stale lock behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from dreamscale_yam.config import rig_profiles, state_home
from dreamscale_yam.errors import UserFacingError


def lock_path(rig: Path) -> Path:
    """Return the lock file for one rig configuration path."""
    digest = hashlib.sha256(str(rig.expanduser().absolute()).encode()).hexdigest()[:16]
    return state_home() / "locks" / f"{rig.stem}-{digest}.lock"


def _holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 256).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return text


@contextlib.contextmanager
def hold_rig_locks(rigs: Iterable[Path], *, purpose: str) -> Iterator[None]:
    """Hold every listed rig's lock, or refuse at once if any is held elsewhere."""
    import fcntl

    unique = sorted({lock_path(rig): rig for rig in rigs}.items())
    held: list[int] = []
    try:
        for path, rig in unique:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = _holder(fd)
                os.close(fd)
                detail = f" ({holder})" if holder else ""
                raise UserFacingError(
                    f"The rig configured in {rig} is in use by another dreamscale-yam "
                    f"command{detail}",
                    "Wait for that command to finish, or stop it with Ctrl-C in its terminal, "
                    "then repeat this command",
                ) from None
            held.append(fd)
            os.ftruncate(fd, 0)
            os.write(fd, f"PID {os.getpid()}: {purpose}".encode())
        yield
    finally:
        for fd in held:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def all_rig_paths(*extra: Path) -> list[Path]:
    """Return every configured rig plus any extra rig path about to be written."""
    return [*rig_profiles().values(), *extra]
