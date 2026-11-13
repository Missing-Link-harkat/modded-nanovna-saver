"""Segmented sweep through MCP on the virtual device (litevna-openvna#204):
the requested step is what gets measured, the whole range is kept (data,
Touchstone, MCP) and Stop stops the frame loop."""

import asyncio
import csv
import json
import threading
import time

import pytest

pytest.importorskip("mcp")

from PySide6 import QtWidgets  # noqa: E402

from NanoVNASaver import SweepLogger  # noqa: E402
from NanoVNASaver.Hardware import VirtualVNA  # noqa: E402
from NanoVNASaver.MCP import MCPOptions  # noqa: E402
from NanoVNASaver.Touchstone import Touchstone  # noqa: E402

try:
    from mcp import Client
except ImportError:  # mcp 1.x
    Client = None

pytestmark = pytest.mark.skipif(Client is None, reason="needs mcp >= 2")


@pytest.fixture(scope="module")
def saver(tmp_path_factory):
    VirtualVNA.enable()
    root = tmp_path_factory.mktemp("segmented")
    old_root = SweepLogger.LOG_ROOT
    SweepLogger.LOG_ROOT = str(root)
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from NanoVNASaver.NanoVNASaver import NanoVNASaver

    window = NanoVNASaver(
        MCPOptions(headless=True, workspace=tmp_path_factory.mktemp("ws"))
    )
    svc = window.mcp.build_service()
    window.mcp.control.allow(True)
    yield qapp, window, svc
    SweepLogger.LOG_ROOT = old_root
    window.mcp.control.allow(False)
    if window.worker.isRunning():
        window.worker.quit()
        window.worker.wait(5000)


def pump(qapp, thread, timeout):
    deadline = time.monotonic() + timeout
    while thread.is_alive():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.monotonic() < deadline, "timeout"


def call(saver, name, args=None, timeout=300, during=None):
    """Call a tool; `during(window)` runs on the Qt thread once while the
    call is in progress (e.g. to press Stop)."""
    qapp, window, svc = saver
    box = {}

    async def go():
        async with Client(svc.server) as c:
            return await c.call_tool(name, args or {})

    def worker():
        try:
            box["value"] = asyncio.run(go())
        except BaseException as exc:  # pylint: disable=broad-except
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    if during is not None:
        deadline = time.monotonic() + timeout
        while not during(window):
            qapp.processEvents()
            time.sleep(0.005)
            assert time.monotonic() < deadline, "timeout before stop"
    pump(qapp, thread, timeout)
    if "error" in box:
        raise box["error"]
    return box["value"]


def ok(result):
    assert not (getattr(result, "is_error", None)
                or getattr(result, "isError", False)), result.content[0].text
    return json.loads(result.content[0].text)


def error_text(result):
    assert getattr(result, "is_error", None) or getattr(result, "isError", False)
    return result.content[0].text


def test_connect(saver):
    ok(call(saver, "device_connect", {"port": "virtual"}))
    # a main sweep that would have broken the step before the fix
    ok(call(saver, "sweep_configure", {"start_hz": 1_000_000,
                                       "stop_hz": 30_000_000, "segments": 3}))
    ok(call(saver, "device_set_datapoints", {"points": 101}))


def test_step_is_honoured_and_whole_range_is_kept(saver):
    _, window, _ = saver
    start, stop, step = 23_500_000, 100_000_000, 100_000
    res = ok(call(saver, "sweep_segmented", {
        "name": "askel", "start_hz": start, "stop_hz": stop, "step_hz": step}))
    n = (stop - start) // step + 1
    assert res["status"] == "valmis"
    assert res["points"] == res["points_measured"] == n
    assert res["frames"] == res["frames_done"] == 2
    assert res["max_freq_error_hz"] == 0

    want = [start + k * step for k in range(n)]
    with window.dataLock:
        assert [d.freq for d in window.data.s11] == want
        assert len(window.data.s21) == n

    with open(f"{res['log_dir']}/points.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [int(r["frequency_hz"]) for r in rows] == want

    ts = Touchstone(f"{res['log_dir']}/sweep.s1p")
    ts.load()
    assert [d.freq for d in ts.s11] == want
    assert any(p.endswith("sweep.s2p") for p in res["files"])

    with open(f"{res['log_dir']}/settings.csv", encoding="utf-8") as f:
        settings = dict(csv.reader(f))
    assert settings["status"] == "valmis"
    assert settings["step_hz"] == str(step)

    # MCP data sees the whole range, not the last frame
    summary = ok(call(saver, "data_summary"))
    assert summary["points"] == n

    # the main sweep settings are back
    assert window.vna.datapoints == 101
    assert window.sweep_control.get_segments() == 3
    assert window.sweep.start == 1_000_000


def test_stop_stops_the_frame_loop(saver):
    _, window, _ = saver
    win = window.windows["custom_sweep"]

    def press_stop(w):
        # the main window's Stop is enabled only while a frame runs
        if (win.sweep_active and win.current_frame_index >= 1
                and w.sweep_control.btn_stop.isEnabled()):
            w.sweep_control.btn_stop.click()  # the main window's Stop
            return True
        return False

    res = ok(call(saver, "sweep_segmented", {
        "name": "seis", "start_hz": 25_000_000, "stop_hz": 200_000_000,
        "step_hz": 50_000}, during=press_stop))
    assert res["status"] == "pysäytetty"
    assert 1 <= res["frames_done"] < res["frames"]
    assert res["points_measured"] == sum(
        f.points for f in win.plan.frames[: res["frames_done"]])
    assert not win.sweep_active
    assert not window.worker.isRunning() or window.worker.wait(5000)


def test_refusals(saver):
    assert "vain" in error_text(call(saver, "sweep_segmented", {
        "name": "x", "start_hz": 25_000_000, "stop_hz": 25_000_500,
        "step_hz": 100}))
    ok(call(saver, "sweep_configure", {"mode": "continuous"}))
    try:
        assert "Jatkuva" in error_text(call(saver, "sweep_segmented", {
            "name": "x", "start_hz": 25_000_000, "stop_hz": 30_000_000,
            "step_hz": 10_000}))
    finally:
        ok(call(saver, "sweep_configure", {"mode": "single"}))
