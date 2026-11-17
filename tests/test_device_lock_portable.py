"""The device lock between two real processes, on every platform including
Windows (msvcrt.locking). test_device_lock.py simulates the other holder
with fcntl and is skipped on Windows; here both sides use DeviceLock, so the
Windows branch of device_lock.py is exercised by CI (litevna-openvna#188,
last EI MITATTU row)."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from NanoVNASaver.device_lock import DeviceLock, LockBusy, read_holder

CHILD = textwrap.dedent(
    """
    import sys, time
    from NanoVNASaver.device_lock import DeviceLock
    lock = DeviceLock()
    lock.acquire("lapsiprosessi")
    print("held", flush=True)
    time.sleep(60)
    """
)


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "litevna64.lock")
    monkeypatch.setenv("LITEVNA_LOCK", p)
    monkeypatch.delenv("LITEVNA_LOCK_HELD", raising=False)
    return p


@pytest.fixture
def child(path):
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ, LITEVNA_LOCK=path),
    )
    assert proc.stdout.readline().strip() == "held"
    yield proc
    proc.kill()
    proc.wait()


def test_second_process_is_refused_and_sees_the_holder(path, child):
    lock = DeviceLock()
    assert lock.busy_by_other()
    with pytest.raises(LockBusy) as exc:
        lock.acquire("NanoVNA-Saver")
    assert not lock.held
    assert exc.value.holder is not None
    assert exc.value.holder["purpose"] == "lapsiprosessi"
    assert exc.value.holder["pid"] == child.pid


def test_lock_frees_when_the_holder_process_dies(path, child):
    child.kill()
    child.wait()
    lock = DeviceLock()
    deadline = time.time() + 10
    while lock.busy_by_other() and time.time() < deadline:
        time.sleep(0.1)
    lock.acquire("NanoVNA-Saver")
    assert lock.held
    assert read_holder(path)["purpose"] == "NanoVNA-Saver"
    lock.release()
    assert read_holder(path) is None


def test_our_lock_is_seen_by_another_process(path):
    lock = DeviceLock()
    lock.acquire("NanoVNA-Saver")
    try:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "from NanoVNASaver.device_lock import DeviceLock, LockBusy\n"
                "try:\n"
                "    DeviceLock().acquire('toinen')\n"
                "except LockBusy:\n"
                "    sys.exit(7)\n"
                "sys.exit(0)\n",
            ],
            capture_output=True,
            env=dict(os.environ, LITEVNA_LOCK=path),
        )
        assert probe.returncode == 7, probe.stderr
    finally:
        lock.release()
    free = subprocess.run(
        [sys.executable, "-c",
         "from NanoVNASaver.device_lock import DeviceLock\n"
         "l = DeviceLock(); l.acquire('toinen'); l.release()"],
        capture_output=True,
        env=dict(os.environ, LITEVNA_LOCK=path),
    )
    assert free.returncode == 0, free.stderr
