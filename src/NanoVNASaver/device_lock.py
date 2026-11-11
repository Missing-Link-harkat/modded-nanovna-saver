#  NanoVNASaver
#
#  A python program to view and export Touchstone data from a NanoVNA
#  Copyright (C) 2019, 2020  Rune B. Broberg
#  Copyright (C) 2020,2021 NanoVNA-Saver Authors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Machine-wide device lock, shared with litevna-openvna `tools/vna_lock.py`.

A remote test run and Saver must not drive the device at the same time
(litevna-openvna#188, docs/ETATESTAUS.md section 1 in that repo). Saver
takes the same lock as `vna_lock.py` when it connects to a real device and
releases it on disconnect. This module does not import Qt.

The lock format is defined by `vna_lock.py`; keep both in step:

- lock file: $LITEVNA_LOCK, else /run/lock/litevna64.lock when /run/lock
  is writable, else <tempdir>/litevna64.lock; created with mode 0666
- the lock itself: flock(LOCK_EX | LOCK_NB) on Linux/macOS, msvcrt
  LK_NBLCK on byte 0 on Windows. The kernel releases it when the holder
  exits or dies, so there is never a stale lock to clean up.
- holder info (courtesy only, not the lock): "<lock file>.holder", JSON
  {"user", "host", "pid", "since" (epoch seconds), "purpose"}, written
  after taking the lock, removed before releasing it
- LITEVNA_LOCK_HELD=1 in the environment: the parent process
  (`vna_lock.py run -- ...`) already holds the lock for us; do not take it
  again (a second flock in a child would see it as busy).
"""

import contextlib
import getpass
import json
import logging
import os
import socket
import tempfile
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)


class LockBusy(RuntimeError):
    """Someone else holds the device lock; .holder is their info or None."""

    def __init__(self, holder: dict | None, path: str):
        super().__init__(
            f"LiteVNA on varattu ({path}): {describe(holder)}. "
            "Odota, että pitäjä vapauttaa lukon, tai tarkista tilanne: "
            "python3 tools/vna_lock.py status"
        )
        self.holder = holder
        self.path = path


def lock_path() -> str:
    if os.environ.get("LITEVNA_LOCK"):
        return os.environ["LITEVNA_LOCK"]
    if os.path.isdir("/run/lock") and os.access("/run/lock", os.W_OK):
        return "/run/lock/litevna64.lock"
    return os.path.join(tempfile.gettempdir(), "litevna64.lock")


def held_by_parent() -> bool:
    return os.environ.get("LITEVNA_LOCK_HELD") == "1"


def describe(holder: dict | None) -> str:
    if not holder:
        return "pitäjä ei tiedossa (lukko otettu, pitäjän tietoja ei kirjoitettu)"
    age = time.time() - holder.get("since", time.time())
    return (
        f"{holder.get('user')}@{holder.get('host')} pid {holder.get('pid')}, "
        f"{int(age)} s, tarkoitus: {holder.get('purpose')}"
    )


def read_holder(path: str | None = None) -> dict | None:
    path = path or lock_path()
    try:
        with open(path + ".holder", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _open_shared(path: str, flags: int) -> int:
    """Open a file that several users share in a sticky directory such as
    /run/lock (same as tools/vna_lock.py, litevna-openvna cb8a57c).

    With fs.protected_regular (1 or 2 on current Linux) open() with
    O_CREAT is refused for an EXISTING file that another user owns in a
    sticky world-writable directory, even with mode 0666. So open an
    existing file without O_CREAT and create it (0666) only when missing."""
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    old = os.umask(0)
    try:
        return os.open(path, flags | os.O_CREAT, 0o666)
    except FileExistsError:  # another process created it in between
        return os.open(path, flags)
    finally:
        os.umask(old)


def _open(path: str) -> int:
    return _open_shared(path, os.O_RDWR)


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class DeviceLock:
    """One per process. acquire() is non-blocking and idempotent."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> str:
        return self._path or lock_path()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, purpose: str) -> None:
        """Take the lock or raise LockBusy. No-op when we already hold it
        or when the parent process holds it for us. If the lock file cannot
        be opened at all (permissions), log a warning and go on: the lock
        is a courtesy between cooperating programs, not a security
        boundary, and the serial port is still opened exclusively."""
        if self._fd is not None or held_by_parent():
            return
        path = self.path
        try:
            fd = _open(path)
        except OSError as exc:
            logger.warning("Laitelukkoa %s ei voi avata: %s", path, exc)
            return
        if not _try_lock(fd):
            os.close(fd)
            raise LockBusy(read_holder(path), path)
        self._fd = fd
        holder = {
            "user": getpass.getuser(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "since": time.time(),
            "purpose": purpose,
        }
        try:
            with open(
                _open_shared(path + ".holder", os.O_WRONLY | os.O_TRUNC),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(holder, f)
        except OSError:
            pass  # holder info is a courtesy; the flock is the lock
        logger.info("Laitelukko otettu: %s (%s)", path, purpose)

    def release(self) -> None:
        if self._fd is None:
            return
        # Empty it instead of deleting: in a sticky directory only the
        # owner may delete a file, so another user's holder file would stay
        # and show a stale holder.
        with contextlib.suppress(OSError):
            os.close(_open_shared(self.path + ".holder",
                                  os.O_WRONLY | os.O_TRUNC))
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None
        logger.info("Laitelukko vapautettu: %s", self.path)

    def busy_by_other(self) -> bool:
        """True when another process holds the lock (not us, not our
        parent). False when free, ours, or not checkable."""
        if self._fd is not None or held_by_parent():
            return False
        try:
            fd = _open(self.path)
        except OSError:
            return False
        try:
            if _try_lock(fd):
                _unlock(fd)
                return False
            return True
        finally:
            os.close(fd)


# The process-wide lock that Saver's connect/disconnect use.
DEVICE_LOCK = DeviceLock()
