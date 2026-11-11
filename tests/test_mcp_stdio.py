"""Headless ``--mcp stdio`` as a real subprocess (litevna-openvna#189).

This is how a client runs Saver over ssh: stdout must carry only JSON-RPC,
and when the client goes away (stdin closes) or the process gets SIGTERM /
SIGHUP, Saver must release the device and exit by itself.

Serial ports are hidden from the child, so these tests never open a real
device; they use ``--virtual``.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

SRC = Path(__file__).resolve().parents[1] / "src"

# Runs in the child before main(): no real serial ports, and junk written to
# stdout (Python and fd level) while Saver is starting up. Output from
# import time, before main() runs, is not covered.
SITECUSTOMIZE = textwrap.dedent(
    """
    import os, sys, threading, time
    import serial.tools.list_ports as _lp
    _lp.comports = lambda *a, **k: []

    def _stray():
        # after main() has claimed stdout, while Qt and the window start
        while sys.stdout is not sys.stderr:
            time.sleep(0.01)
        print("STRAY python print")
        sys.stdout.flush()
        os.write(1, b"STRAY fd 1 write\\n")

    threading.Thread(target=_stray, daemon=True).start()
    """
)


def _start(tmp_path: Path) -> subprocess.Popen:
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(site), str(SRC), env.get("PYTHONPATH", "")) if p
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["HOME"] = str(tmp_path)  # keep settings away from the user's
    env["XDG_CONFIG_HOME"] = str(tmp_path / ".config")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "NanoVNASaver",
            "--headless",
            "--mcp",
            "stdio",
            "--virtual",
            "--debug",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=(tmp_path / "stderr.txt").open("wb"),
        env=env,
    )


class _Wire:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.lines: list[bytes] = []

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj).encode() + b"\n")
        self.proc.stdin.flush()

    def recv(self, want_id: int) -> dict:
        while True:
            line = self.proc.stdout.readline()
            assert line, f"server closed stdout; got {self.lines!r}"
            self.lines.append(line)
            msg = json.loads(line)  # anything but JSON-RPC fails here
            if msg.get("id") == want_id:
                return msg

    def call(self, rid: int, name: str, args: dict) -> dict:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        return self.recv(rid)["result"]


def _session(tmp_path: Path) -> _Wire:
    w = _Wire(_start(tmp_path))
    time.sleep(1.0)  # let the stray writes happen before the handshake
    w.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
    )
    w.recv(1)
    w.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    res = w.call(2, "device_connect", {"port": "virtual"})
    assert not res.get("isError"), res
    return w


def _stderr(tmp_path: Path) -> str:
    return (tmp_path / "stderr.txt").read_text(errors="replace")


def test_stdout_is_only_protocol_and_eof_releases_device(tmp_path):
    w = _session(tmp_path)
    assert not w.call(3, "sweep_run", {}).get("isError")
    w.proc.stdin.close()  # what happens when the ssh connection closes
    assert w.proc.wait(timeout=30) == 0
    rest = w.proc.stdout.read()
    assert all(line.startswith(b"{") for line in w.lines), w.lines
    assert rest.strip() == b"", rest
    err = _stderr(tmp_path)
    assert "STRAY python print" in err
    assert "STRAY fd 1 write" in err
    assert "Closing connection" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_signal_releases_device_and_exits_cleanly(tmp_path, sig):
    w = _session(tmp_path)
    w.proc.send_signal(getattr(signal, sig))
    assert w.proc.wait(timeout=30) == 0
    w.proc.stdin.close()
    err = _stderr(tmp_path)
    assert "Closing connection" in err
