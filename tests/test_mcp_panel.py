"""AI (MCP) panel in a real main window (offscreen): server button,
copy, "Salli AI-ohjaus", status while the AI acts, STOP button,
Ctrl+Shift+S and the log.

Set MCP_PANEL_SHOTS=<dir> to save a PNG of the panel at each step.
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("mcp")

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from NanoVNASaver.Hardware import VirtualVNA  # noqa: E402
from NanoVNASaver.MCP import MCPOptions  # noqa: E402

try:
    from mcp import Client
except ImportError:  # mcp 1.x
    Client = None

pytestmark = pytest.mark.skipif(Client is None, reason="needs mcp >= 2")

PORT = 18766


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    VirtualVNA.enable()
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from NanoVNASaver.NanoVNASaver import NanoVNASaver

    win = NanoVNASaver(MCPOptions(workspace=tmp_path_factory.mktemp("ws")))
    win.mcp.panel.port = PORT
    win.show()
    yield qapp, win
    panel = win.mcp.panel
    if panel.service is not None and panel.service.url:
        panel.service.stop_http()
    if win.worker.isRunning():
        win.worker.quit()
        win.worker.wait(5000)


def pump(qapp, until, timeout=120):
    deadline = time.monotonic() + timeout
    while not until():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.monotonic() < deadline, "timeout"
    qapp.processEvents()


def call_in_thread(qapp, server, name, args=None, *, wait=True):
    box = {}

    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})

    def worker():
        try:
            box["value"] = asyncio.run(go())
        except BaseException as exc:  # pylint: disable=broad-except
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    if wait:
        pump(qapp, lambda: not thread.is_alive(), timeout=300)
        if "error" in box:
            raise box["error"]
    return thread, box


def is_error(result):
    return getattr(result, "is_error", None) or getattr(result, "isError", False)


def shot(panel, name):
    folder = os.environ.get("MCP_PANEL_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        panel.grab().save(str(Path(folder) / f"{name}.png"))


def test_panel_flow(window):
    qapp, win = window
    panel = win.mcp.panel

    # 1. start state: server off, AI control off
    assert panel.btn_server.text() == "Käynnistä MCP-palvelin"
    assert not panel.btn_copy.isEnabled()
    assert not panel.check_allow.isChecked()
    assert panel.label_status.text() == "Palvelin ei käynnissä"
    shot(panel, "1_alku")

    # 2. server button starts HTTP; copy gives the claude mcp add line
    QTest.mouseClick(panel.btn_server, QtCore.Qt.MouseButton.LeftButton)
    pump(qapp, lambda: panel.service is not None and panel.service.url)
    assert panel.btn_server.text() == "Pysäytä MCP-palvelin"
    assert panel.btn_copy.isEnabled()
    assert panel.label_status.text() == f"Kuuntelee {panel.service.url}"
    assert f":{PORT}/" in panel.service.url
    QTest.mouseClick(panel.btn_copy, QtCore.Qt.MouseButton.LeftButton)
    clip = QtWidgets.QApplication.clipboard().text()
    assert clip.startswith("claude mcp add --transport http nanovna-saver ")
    assert panel.service.url in clip
    assert f"Bearer {panel.service.token}" in clip
    shot(panel, "2_palvelin_kaynnissa")

    server = panel.service.server
    # 3. without "Salli AI-ohjaus" an ACT tool is refused, READ works
    _, box = call_in_thread(qapp, server, "device_connect", {"port": "virtual"})
    assert is_error(box["value"])
    _, box = call_in_thread(qapp, server, "device_list_ports")
    assert not is_error(box["value"])

    # 4. tick the box: ACT works and shows in the status while running
    panel.check_allow.click()
    pump(qapp, lambda: panel.control.allowed)
    _, box = call_in_thread(qapp, server, "device_connect", {"port": "virtual"})
    assert not is_error(box["value"]), box["value"].content[0].text
    call_in_thread(qapp, server, "sweep_configure",
                   {"start_hz": 1_000_000, "stop_hz": 30_000_000})
    thread, box = call_in_thread(qapp, server, "sweep_run", wait=False)
    pump(qapp, lambda: "AI ohjaa" in panel.label_status.text()
         or not thread.is_alive(), timeout=300)
    seen_active = "AI ohjaa" in panel.label_status.text()
    if seen_active:
        assert "sweep_run" in panel.label_status.text()
        shot(panel, "3_ai_ohjaa")
    pump(qapp, lambda: not thread.is_alive(), timeout=300)
    assert not is_error(box["value"]), box["value"].content[0].text
    assert seen_active, "status never showed the running tool"
    pump(qapp, lambda: "AI ohjaa" not in panel.label_status.text())
    log = panel.log_view.toPlainText()
    for name in ("device_connect", "sweep_configure", "sweep_run"):
        assert name in log
    shot(panel, "4_loki")

    # 5. STOP button revokes control and logs it
    QTest.mouseClick(panel.btn_stop, QtCore.Qt.MouseButton.LeftButton)
    pump(qapp, lambda: not panel.check_allow.isChecked())
    assert not panel.control.allowed
    _, box = call_in_thread(qapp, server, "sweep_run")
    assert is_error(box["value"])
    shot(panel, "5_stop")

    # 6. Ctrl+Shift+S does the same from anywhere in the window
    panel.check_allow.click()
    pump(qapp, lambda: panel.control.allowed)
    win.activateWindow()
    QTest.keyClick(win, QtCore.Qt.Key.Key_S,
                   QtCore.Qt.KeyboardModifier.ControlModifier
                   | QtCore.Qt.KeyboardModifier.ShiftModifier)
    pump(qapp, lambda: not panel.control.allowed)
    assert not panel.check_allow.isChecked()

    # 7. stopping the server also takes AI control away
    panel.check_allow.click()
    pump(qapp, lambda: panel.control.allowed)
    QTest.mouseClick(panel.btn_server, QtCore.Qt.MouseButton.LeftButton)
    pump(qapp, lambda: not panel.service.url)
    assert panel.btn_server.text() == "Käynnistä MCP-palvelin"
    assert not panel.btn_copy.isEnabled()
    assert not panel.control.allowed
    assert not panel.check_allow.isChecked()
    shot(panel, "6_palvelin_pysaytetty")
