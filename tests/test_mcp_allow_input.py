"""--mcp-allow-input: device_input permission for a headless test machine
(litevna-openvna#187)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from NanoVNASaver.MCP.control import AIControl

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def control():
    c = AIControl()
    state = {"conn": object()}
    c.connection = lambda: state["conn"]
    c.allow(True, by="command line")
    return c, state


def test_default_is_off(control):
    c, _ = control
    assert not c.input_allowed
    assert c.input_status() == {"allowed": False, "granted": None}


def test_standing_permission_survives_reconnects_and_is_logged(control):
    c, state = control
    c.allow_input_standing()
    assert c.input_allowed
    assert "--mcp-allow-input" in c.input_status()["granted"]
    entry = c.entries[-1]
    assert entry.tool == "ui_control" and entry.outcome == "ok"
    assert "--mcp-allow-input" in entry.detail
    state["conn"] = object()  # reset + reconnect (#190)
    assert c.input_allowed
    state["conn"] = None  # no device: not allowed, but not revoked
    assert not c.input_allowed
    state["conn"] = object()
    assert c.input_allowed


def test_stop_ends_the_standing_permission(control):
    c, _ = control
    c.allow_input_standing()
    c.stop()
    assert not c.input_allowed
    c.allow(True)  # AI control again does not bring UI control back
    assert not c.input_allowed
    assert any("päättyi" in e.detail for e in c.entries)


@pytest.mark.parametrize(
    ("headless", "flag", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_start_option_only_in_headless_mode(headless, flag, expected):
    from NanoVNASaver.MCP import MCPOptions, MCPSupport

    class _VNA:
        def connected(self):
            return True

    app = type("App", (), {"vna": _VNA()})()
    support = MCPSupport(app, MCPOptions(mode="off", headless=headless,
                                         allow_input=flag))
    support.control.log_file = None
    support.options.mode = "none"  # not "off": run the permission part only
    support.start()
    assert support.control.input_allowed is expected


def test_command_line_refuses_the_option_without_headless():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p),
        QT_QPA_PLATFORM="offscreen")
    out = subprocess.run(
        [sys.executable, "-m", "NanoVNASaver", "--mcp-allow-input"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 2
    assert "--mcp-allow-input needs --headless" in out.stderr


# --- through the MCP server, virtual device ---------------------------------

def _call_device_input(tmp_path, monkeypatch, standing):
    import asyncio
    import threading
    import time

    mcp = pytest.importorskip("mcp")
    Client = getattr(mcp, "Client", None)
    if Client is None:
        pytest.skip("needs mcp >= 2")
    import serial.tools.list_ports as list_ports
    from PySide6 import QtWidgets

    from NanoVNASaver.Hardware import VirtualVNA
    from NanoVNASaver.MCP import MCPOptions
    from NanoVNASaver.NanoVNASaver import NanoVNASaver

    monkeypatch.setattr(list_ports, "comports", lambda *a, **k: [])
    VirtualVNA.enable()
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = NanoVNASaver(MCPOptions(headless=True, workspace=tmp_path,
                                     allow_input=standing))
    window.mcp.control.log_file = None
    svc = window.mcp.build_service()
    window.mcp.control.allow(True, by="command line")
    if standing:
        window.mcp.control.allow_input_standing()
    box = {}

    async def go():
        async with Client(svc.server) as c:
            await c.call_tool("device_connect", {"port": "virtual"})
            info = await c.call_tool("device_info", {})
            res = await c.call_tool("device_input", {"command": "input wheel 1"})
            return info.content[0].text, res.content[0].text

    t = threading.Thread(target=lambda: box.setdefault("r", asyncio.run(go())))
    t.start()
    deadline = time.monotonic() + 60
    while t.is_alive():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.monotonic() < deadline
    if window.worker.isRunning():
        window.worker.quit()
        window.worker.wait(5000)
    window.serial_control.disconnect_device()
    return box["r"]


def test_device_input_refused_without_the_option(tmp_path, monkeypatch):
    info, text = _call_device_input(tmp_path, monkeypatch, standing=False)
    assert '"allowed": false' in info
    assert "käyttöliittymän ohjaus ei ole sallittu" in text


def test_device_input_passes_the_permission_with_the_option(
        tmp_path, monkeypatch):
    info, text = _call_device_input(tmp_path, monkeypatch, standing=True)
    assert '"allowed": true' in info and "--mcp-allow-input" in info
    assert "ei ole sallittu" not in text  # past the permission gate
    assert '"output": "ok: input wheel 1"' in text  # and it ran
