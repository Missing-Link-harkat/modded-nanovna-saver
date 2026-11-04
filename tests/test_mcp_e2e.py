"""MCP end to end: an MCP client drives Saver with the virtual VNA.

The client runs in a thread with its own asyncio loop (as the real
server does); the test's main thread runs the Qt event loop.
"""

import asyncio
import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

mcp = pytest.importorskip("mcp")

from PySide6 import QtWidgets  # noqa: E402

from NanoVNASaver.Hardware import VirtualVNA  # noqa: E402
from NanoVNASaver.MCP import MCPOptions  # noqa: E402

try:
    from mcp import Client
except ImportError:  # mcp 1.x
    Client = None

pytestmark = pytest.mark.skipif(Client is None, reason="needs mcp >= 2")


@pytest.fixture(scope="module")
def saver(tmp_path_factory):
    VirtualVNA.enable()
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from NanoVNASaver.NanoVNASaver import NanoVNASaver

    window = NanoVNASaver(
        MCPOptions(headless=True, workspace=tmp_path_factory.mktemp("ws"))
    )
    svc = window.mcp.build_service()
    yield qapp, window, svc
    window.mcp.control.allow(False)
    if window.worker.isRunning():
        window.worker.quit()
        window.worker.wait(5000)


def run(qapp, coro_factory, timeout=120):
    """Run a coroutine in a thread while pumping the Qt event loop."""
    box = {}

    def worker():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # pylint: disable=broad-except
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.monotonic() < deadline, "timeout"
    if "error" in box:
        raise box["error"]
    return box["value"]


def is_error(result):
    return getattr(result, "is_error", None) or getattr(result, "isError", False)


def text(result):
    assert not is_error(result), result.content[0].text
    return json.loads(result.content[0].text)


def call(saver, name, args=None, elicit=None):
    qapp, _, svc = saver

    async def go():
        async with Client(svc.server, elicitation_callback=elicit) as c:
            return await c.call_tool(name, args or {})

    return run(qapp, go)


def test_tools_listed(saver):
    qapp, _, svc = saver

    async def go():
        async with Client(svc.server) as c:
            tools = await c.list_tools()
            prompts = await c.list_prompts()
            return tools, prompts

    tools, prompts = run(qapp, go)
    names = {t.name for t in tools.tools}
    assert {"sweep_run", "device_connect", "calibration_capture"} <= names
    assert {p.name for p in prompts.prompts} >= {"antenna", "filter"}


def test_act_needs_permission(saver):
    _, window, _ = saver
    window.mcp.control.allow(False)
    result = call(saver, "sweep_configure", {"start_hz": 1_000_000})
    assert is_error(result)
    assert "AI-ohjaus ei ole sallittu" in result.content[0].text
    # panel log line is Finnish and says the same as the error message,
    # the machine-readable outcome stays as it is
    entry = window.mcp.control.entries[-1]
    assert entry.outcome == "denied"
    assert entry.line().endswith("-> estetty: AI-ohjaus ei ole sallittu")
    # reading still works
    assert "start_hz" in text(call(saver, "sweep_get"))


def test_measure_virtual_antenna(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    ports = text(call(saver, "device_list_ports"))
    assert {"port": "virtual", "type": "VirtualVNA"} in ports["ports"]
    info = text(call(saver, "device_connect", {"port": "virtual"}))
    assert info["name"] == "VirtualVNA"
    cfg = text(
        call(
            saver,
            "sweep_configure",
            {"start_hz": 10_000_000, "stop_hz": 20_000_000, "segments": 2},
        )
    )
    assert cfg["total_points"] == 202
    summary = text(call(saver, "sweep_run"))
    assert summary["points"] == 202
    best = summary["s11"]["min_vswr"]
    assert abs(best["freq_hz"] - VirtualVNA.F0) < 150_000
    assert best["vswr"] < 1.3
    assert summary["s11"]["vswr_le_2"]
    assert any(
        abs(r["freq_hz"] - VirtualVNA.F0) < 150_000
        for r in summary["s11"]["resonances"]
    )
    log = window.mcp.control.entries
    assert [e.tool for e in log if e.outcome == "ok"][-1] == "sweep_run"


def test_chart_image(saver):
    result = call(saver, "chart_image", {"chart": "s11.smith"})
    assert not is_error(result)
    img = result.content[0]
    assert img.type == "image"
    assert (getattr(img, "mime_type", None)
            or getattr(img, "mimeType", None)) == "image/png"


def test_persist_asks_user(saver, tmp_path):
    _, window, _ = saver
    window.mcp.control.allow(True)

    async def decline(_ctx, _params):
        return mcp.types.ElicitResult(action="decline")

    result = call(saver, "file_export_touchstone", {"path": "a.s1p"},
                  elicit=decline)
    assert is_error(result)
    assert not (window.mcp.service.api.workspace / "a.s1p").exists()

    async def accept(_ctx, params):
        assert "a.s2p" in params.message
        return mcp.types.ElicitResult(action="accept", content={"ok": True})

    saved = text(call(saver, "file_export_touchstone", {"path": "a.s2p"},
                      elicit=accept))
    content = open(saved["saved"], encoding="utf-8").read()
    assert content.startswith("# HZ S RI R 50")


def test_path_outside_workspace_refused(saver):
    result = call(saver, "file_load_sweep", {"path": "../../etc/x.s1p"})
    assert is_error(result)
    assert "ulkopuolella" in result.content[0].text


def test_calibration_capture_asks_to_connect(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    asked = []

    async def accept(_ctx, params):
        asked.append(params.message)
        return mcp.types.ElicitResult(action="accept", content={"ok": True})

    status = text(call(saver, "calibration_capture", {"standard": "short"},
                       elicit=accept))
    assert "SHORT" in asked[0]
    assert status["standards"]["short"] == 202


def test_filter_minus_3db_band(saver):
    """VirtualVNA band-pass (145 MHz, Q = 8): -3 dB width within 2 %
    of the model's exact value."""
    _, window, _ = saver
    window.mcp.control.allow(True)
    f0, q = VirtualVNA.F_FILTER, VirtualVNA.Q_FILTER
    x = (10 ** (3 / 20) - 1) ** 0.5 / q
    f_lo = f0 * (-x + (x * x + 4) ** 0.5) / 2
    f_hi = f0 * (x + (x * x + 4) ** 0.5) / 2
    text(call(saver, "sweep_configure",
              {"start_hz": 100_000_000, "stop_hz": 200_000_000,
               "segments": 1}))
    band = text(call(saver, "sweep_run"))["s21"]["minus_3db"]
    assert band["edges_interpolated"] is True
    assert not band["open_low"] and not band["open_high"]
    assert abs(band["bandwidth_hz"] - (f_hi - f_lo)) / (f_hi - f_lo) < 0.02
    assert abs(band["center_hz"] - f0) / f0 < 0.01


def test_stop_revokes_control(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    window.mcp.control.stop()
    result = call(saver, "sweep_run")
    assert is_error(result)
    assert window.mcp.control.entries[-1].outcome == "denied"
    stop = next(e for e in reversed(window.mcp.control.entries)
                if e.tool == "STOP")
    assert stop.outcome == "stopped"
    assert stop.line().endswith(
        "STOP() -> pysäytetty: käyttäjä painoi STOP, AI-ohjaus on pois"
    )



def test_http_requires_token(saver):
    httpx = pytest.importorskip("httpx")
    qapp, _, svc = saver
    url = svc.start_http(port=18765, token="testtoken")

    async def go():
        async with httpx.AsyncClient() as http:
            for _ in range(100):
                try:
                    missing = await http.post(url, json={})
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            foreign = await http.post(
                url,
                json={},
                headers={
                    "Authorization": "Bearer testtoken",
                    "Origin": "http://evil.example",
                },
            )
            return missing.status_code, foreign.status_code

    try:
        assert run(qapp, go, timeout=30) == (401, 403)
    finally:
        svc.stop_http()


def test_device_shell(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    out = text(call(saver, "device_shell", {"command": "version"}))
    assert "proto 1.0" in out["output"] and out["error"] is False
    assert out["ms"] >= 0
    assert text(call(saver, "device_shell", {"command": "sweep"}))["output"]

    # unlisted and wrong-class commands are refused, nothing reaches the device
    log = window.vna.shell_log
    before = len(log)
    for cmd, tool in [("reset", "device_shell"),
                      ("version; reset", "device_shell"),
                      ("sweep-avg 3", "device_shell")]:
        res = call(saver, tool, {"command": cmd})
        assert is_error(res), cmd
    assert len(log) == before

    # ACT class needs AI control
    act = text(call(saver, "device_shell_act", {"command": "sweep-avg 3"}))
    assert act["output"] == "ok: sweep-avg 3"
    window.mcp.control.allow(False)
    denied = call(saver, "device_shell_act", {"command": "sweep-avg 3"})
    assert is_error(denied)
    assert "AI-ohjaus ei ole sallittu" in denied.content[0].text
    # READ works without AI control
    assert text(call(saver, "device_shell", {"command": "info"}))["output"]
    window.mcp.control.allow(True)


def test_device_shell_persist_asks_user(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    before = len(window.vna.shell_log)

    async def decline(_ctx, _params):
        return mcp.types.ElicitResult(action="decline")

    res = call(saver, "device_shell_persist", {"command": "save 2"},
               elicit=decline)
    assert is_error(res)
    assert not any(c == "save 2" for c in window.vna.shell_log[before:])

    async def accept(_ctx, params):
        assert "save 2" in params.message
        assert "overwrites" in params.message  # the rule's own reason
        return mcp.types.ElicitResult(action="accept", content={"ok": True})

    out = text(call(saver, "device_shell_persist", {"command": "save 2"},
                    elicit=accept))
    assert out["output"] == "ok: save 2"


def test_device_shell_refused_on_other_firmware(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    window.vna._mcp_our_firmware = False
    try:
        res = call(saver, "device_shell", {"command": "version"})
        assert is_error(res)
        assert "Missing Linkin laiteohjelmistolla" in res.content[0].text
    finally:
        del window.vna._mcp_our_firmware


def test_device_shell_and_sweep_do_not_mix_replies(saver):
    """100 device_shell commands with a sweep between every tenth, in ONE client session (as a real client works): every
    shell reply must be its own command's output, not sweep data. Not a sweep between every command: 50 sweeps (each starts
    and ends the Qt sweep worker thread) made Python 3.11 abort at interpreter shutdown, after the tests had passed, with a
    refcount error in a C extension; ten sweeps do not."""
    qapp, window, svc = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    call(saver, "sweep_configure", {"start_hz": 3_600_000,
                                    "stop_hz": 30_000_000})
    cmds = {"sweep": "3600000 30000000 101", "version": "Version 1.0.0\nproto 1.0"}

    async def go():
        async with Client(svc.server) as c:
            await c.call_tool("sweep_run", {})   # the stub reports the last sweep range
            for i in range(100):
                if i % 10 == 9:
                    res = await c.call_tool("sweep_run", {})
                    assert not is_error(res), (i, res.content[0].text)
                cmd = "sweep" if i % 2 == 0 else "version"
                res = await c.call_tool("device_shell", {"command": cmd})
                assert not is_error(res), (i, res.content[0].text)
                out = json.loads(res.content[0].text)
                assert out["output"] == cmds[cmd], (i, cmd, out)

    run(qapp, go, timeout=240)


def test_device_input_needs_the_separate_ui_permission(saver):
    _, window, _ = saver
    ctl = window.mcp.control
    ctl.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    ctl.allow_input(False)
    log = window.vna.shell_log
    before = len(log)
    res = call(saver, "device_input", {"command": "input key 28 00 down"})
    assert is_error(res)
    assert "käyttöliittymän ohjaus ei ole sallittu" in res.content[0].text
    assert len(log) == before                      # nothing reached the device
    assert ctl.entries[-1].outcome == "denied"
    # the shell tools do not take input events either
    res = call(saver, "device_shell_act", {"command": "input key 28 00 down"})
    assert is_error(res) and "device_input" in res.content[0].text
    assert len(log) == before


def test_device_input_works_with_permission_and_is_logged(saver):
    _, window, _ = saver
    ctl = window.mcp.control
    ctl.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    assert ctl.allow_input(True)
    # the first shell call also probes the firmware ("version"): do it before counting
    call(saver, "device_shell", {"command": "version"})
    log = window.vna.shell_log
    before = len(log)
    out = text(call(saver, "device_input", {"command": "input key 28 00 down"}))
    assert out["output"] == "ok: input key 28 00 down"
    text(call(saver, "device_input", {"command": "input key 28 00 up"}))
    assert log[before:] == ["input key 28 00 down", "input key 28 00 up"]
    logged = [e for e in ctl.entries if e.tool == "device_input"]
    assert [e.outcome for e in logged[-2:]] == ["ok", "ok"]
    # only input events: other commands are refused and not sent
    before = len(log)
    for cmd in ("reset", "version", "sweep-avg 3", "input key 28 00 down; reset"):
        assert is_error(call(saver, "device_input", {"command": cmd})), cmd
    assert len(log) == before


def test_device_input_permission_ends_on_stop_and_on_reconnect(saver):
    _, window, _ = saver
    ctl = window.mcp.control
    ctl.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    assert ctl.allow_input(True)
    ctl.stop()                                     # STOP revokes both
    ctl.allow(True)
    assert not ctl.input_allowed
    res = call(saver, "device_input", {"command": "input key 28 00 down"})
    assert is_error(res)
    # a dropped or new connection ends it: disconnect, reconnect, still denied
    assert ctl.allow_input(True)
    call(saver, "device_disconnect")
    assert not ctl.input_allowed
    call(saver, "device_connect", {"port": "virtual"})
    assert not ctl.input_allowed                   # must be granted again
    before = len(window.vna.shell_log)
    res = call(saver, "device_input", {"command": "input key 28 00 down"})
    assert is_error(res)
    assert len(window.vna.shell_log) == before
    assert any("laiteyhteys vaihtui" in e.detail for e in ctl.entries
               if e.tool == "ui_control")
    assert ctl.allow_input(True)                   # granting again works

