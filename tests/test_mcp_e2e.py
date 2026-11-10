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


def test_marker_set_reports_new_frequency(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    # same sweep as test_measure_virtual_antenna; later tests expect 202 points
    call(saver, "sweep_configure",
         {"start_hz": 10_000_000, "stop_hz": 20_000_000, "segments": 2})
    call(saver, "sweep_run")
    call(saver, "marker_set", {"index": 0, "freq_hz": 14_200_000})
    result = call(saver, "marker_list")
    assert not is_error(result)
    # list results come as one content item per element
    markers = [json.loads(c.text) for c in result.content]
    first = next(m for m in markers if m["index"] == 0)
    assert first["freq_hz"] == first["s11"]["freq_hz"]
    assert abs(first["freq_hz"] - 14_200_000) <= 50_000

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


def test_data_at_parameter(saver):
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    call(saver, "sweep_configure", {"start_hz": 140_000_000, "stop_hz": 146_000_000})
    text(call(saver, "sweep_run"))
    both = text(call(saver, "data_at", {"freq_hz": 143_000_000}))
    assert list(both)[0] == "s21" and "s11" in both
    only = text(call(saver, "data_at", {"freq_hz": 143_000_000, "parameter": "s21"}))
    assert list(only) == ["s21"]
    assert only["s21"]["db"] == both["s21"]["db"]
    assert isinstance(both["s21"]["interpolated"], bool) and both["s21"]["step_hz"] > 0
    # exactly at a measured point: that point, not interpolated
    pt = text(call(saver, "data_points", {"parameter": "s21", "max_points": 5}))["points"][1]
    at_pt = text(call(saver, "data_at", {"freq_hz": pt["freq_hz"], "parameter": "s21"}))
    assert at_pt["s21"]["interpolated"] is False and at_pt["s21"]["db"] == pt["db"]
    # between two measured points: interpolated, with the spacing of the points used
    mid = text(call(saver, "data_at", {"freq_hz": 143_030_000, "parameter": "s21"}))
    assert mid["s21"]["interpolated"] is True and mid["s21"]["freq_hz"] == 143_030_000
    # outside the sweep: an error that says what to do, not the value of the edge point
    far = call(saver, "data_at", {"freq_hz": 500_000_000, "parameter": "s21"})
    assert is_error(far) and "ulkopuolella" in far.content[0].text
    only11 = text(call(saver, "data_at", {"freq_hz": 143_000_000, "parameter": "s11"}))
    assert list(only11) == ["s11"]


def test_unknown_argument_refused(saver):
    """A guessed parameter is an error, not a silent "ok" (the model
    would believe it was used)."""
    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    before = text(call(saver, "sweep_get"))["start_hz"]
    result = call(saver, "sweep_configure",
                  {"start_hz": before + 1_000_000, "bogus_param": 123})
    assert is_error(result)
    msg = result.content[0].text
    assert "Tuntematon parametri bogus_param" in msg
    assert "start_hz" in msg and "stop_hz" in msg
    # nothing was applied
    assert text(call(saver, "sweep_get"))["start_hz"] == before
    # a tool without parameters says so
    none = call(saver, "marker_add", {"freq_hz": 14_200_000})
    assert is_error(none)
    assert "ei parametreja" in none.content[0].text


def test_schema_forbids_additional_properties(saver):
    qapp, _, svc = saver

    async def go():
        async with Client(svc.server) as c:
            return (await c.list_tools()).tools

    for tool in run(qapp, go):
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        assert schema.get("additionalProperties") is False, tool.name


def test_persist_without_confirmation_channel(saver):
    """Headless with a client that cannot be asked: refused with a
    reason, not as if the user had said no."""
    _, window, _ = saver
    window.mcp.control.allow(True)
    result = call(saver, "file_export_touchstone", {"path": "noask.s1p"})
    assert is_error(result)
    msg = result.content[0].text
    assert "Kuittausta ei voitu pyytää" in msg
    assert "ei hyväksynyt" not in msg
    assert not (window.mcp.service.api.workspace / "noask.s1p").exists()
    entry = window.mcp.control.entries[-1]
    assert entry.outcome == "denied"
    assert entry.detail == "kuittausta ei voitu pyytää"


def test_virtual_device_screenshot(saver):
    """VirtualVNA has a simulated 480x320 screen. The same range gives
    the same image (no noise), another range a different one."""
    from PySide6.QtGui import QImage  # pylint: disable=import-outside-toplevel

    _, window, _ = saver
    window.mcp.control.allow(True)
    call(saver, "device_connect", {"port": "virtual"})
    call(saver, "sweep_configure",
         {"start_hz": 10_000_000, "stop_hz": 20_000_000, "segments": 1})
    call(saver, "sweep_run")  # the device's range follows the sweep

    def shot():
        result = call(saver, "device_screenshot")
        assert not is_error(result), result.content[0].text
        img = result.content[0]
        assert img.type == "image"
        return img.data

    first, again = shot(), shot()
    assert first == again
    import base64  # pylint: disable=import-outside-toplevel

    image = QImage.fromData(base64.b64decode(first))
    assert (image.width(), image.height()) == (480, 320)
    yellow = sum(
        1
        for x in range(40, 480, 4)
        for y in range(24, 296, 2)
        if image.pixelColor(x, y).name() == "#ffff00"
    )
    assert yellow > 20  # the S11 trace is drawn
    call(saver, "sweep_configure",
         {"start_hz": 1_000_000, "stop_hz": 5_000_000, "segments": 1})
    call(saver, "sweep_run")
    assert shot() != first
