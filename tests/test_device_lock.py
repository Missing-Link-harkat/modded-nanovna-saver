"""Machine-wide device lock shared with litevna-openvna tools/vna_lock.py
(litevna-openvna#188)."""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from NanoVNASaver.device_lock import (
    DeviceLock,
    LockBusy,
    lock_path,
    read_holder,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the other holder is simulated with fcntl"
)

# Same steps as vna_lock.device_lock(): flock, then write the holder file.
HOLDER = textwrap.dedent(
    """
    import fcntl, json, os, sys, time
    path = sys.argv[1]
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(path + ".holder", "w") as f:
        json.dump({"user": "lfai", "host": "marko", "pid": os.getpid(),
                   "since": time.time(), "purpose": "savutesti"}, f)
    print("held", flush=True)
    time.sleep(30)
    """
)


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "litevna64.lock")
    monkeypatch.setenv("LITEVNA_LOCK", p)
    monkeypatch.delenv("LITEVNA_LOCK_HELD", raising=False)
    return p


@pytest.fixture
def other_holder(path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, path],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    yield proc
    proc.kill()
    proc.wait()


def test_path_follows_env(path):
    assert lock_path() == path


def test_acquire_writes_holder_and_release_empties_it(path):
    lock = DeviceLock()
    lock.acquire("NanoVNA-Saver")
    assert lock.held
    with open(path + ".holder", encoding="utf-8") as f:
        holder = json.load(f)
    assert set(holder) == {"user", "host", "pid", "since", "purpose"}
    assert holder["pid"] == os.getpid()
    assert holder["purpose"] == "NanoVNA-Saver"
    lock.acquire("again")  # idempotent
    lock.release()
    assert not lock.held
    # emptied, not deleted: another user's file could not be deleted
    assert os.path.getsize(path + ".holder") == 0
    assert read_holder(path) is None
    lock.release()  # idempotent


def test_files_of_another_user_open_without_o_creat(path, monkeypatch):
    """/run/lock is sticky; with fs.protected_regular O_CREAT on an
    existing file owned by another user fails with PermissionError, even
    at mode 0666 (found on the test machine, #156). Simulated here."""
    for name in (path, path + ".holder"):
        with open(name, "w", encoding="utf-8"):
            pass  # created "by another user"
    real_open = os.open

    def protected_open(name, flags, *args):
        if flags & os.O_CREAT and os.path.exists(name):
            raise PermissionError(13, "Permission denied", name)
        return real_open(name, flags, *args)

    monkeypatch.setattr(os, "open", protected_open)
    lock = DeviceLock()
    lock.acquire("NanoVNA-Saver")
    assert lock.held  # before the fix: a warning and no lock at all
    assert read_holder(path)["purpose"] == "NanoVNA-Saver"
    lock.release()
    assert read_holder(path) is None


def test_busy_lock_is_refused_with_holder(path, other_holder):
    lock = DeviceLock()
    assert lock.busy_by_other()
    with pytest.raises(LockBusy) as exc:
        lock.acquire("NanoVNA-Saver")
    assert exc.value.holder["purpose"] == "savutesti"
    msg = str(exc.value)
    assert "lfai@marko" in msg and "savutesti" in msg
    assert not lock.held


def test_lock_frees_when_holder_dies(path, other_holder):
    other_holder.kill()
    other_holder.wait()
    lock = DeviceLock()
    deadline = time.time() + 5
    while lock.busy_by_other() and time.time() < deadline:
        time.sleep(0.05)
    lock.acquire("NanoVNA-Saver")
    assert lock.held
    lock.release()


def test_other_process_sees_our_lock(path):
    lock = DeviceLock()
    lock.acquire("NanoVNA-Saver")
    try:
        assert not lock.busy_by_other()  # ours, not someone else's
        probe = subprocess.run(
            [sys.executable, "-c",
             "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR);"
             "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)", path],
            capture_output=True,
        )
        assert probe.returncode != 0  # BlockingIOError in the other process
    finally:
        lock.release()


def test_parent_held_lock_is_not_taken_again(path, other_holder, monkeypatch):
    # `vna_lock.py run -- saver` holds the lock and sets LITEVNA_LOCK_HELD=1
    monkeypatch.setenv("LITEVNA_LOCK_HELD", "1")
    lock = DeviceLock()
    assert not lock.busy_by_other()
    lock.acquire("NanoVNA-Saver")  # no LockBusy
    assert not lock.held


def test_unopenable_lock_file_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("LITEVNA_LOCK", str(tmp_path / "missing-dir" / "x.lock"))
    monkeypatch.delenv("LITEVNA_LOCK_HELD", raising=False)
    lock = DeviceLock()
    lock.acquire("NanoVNA-Saver")
    assert not lock.held
    assert not lock.busy_by_other()
