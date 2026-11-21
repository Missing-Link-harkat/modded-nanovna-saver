"""The device's own hires job through Saver (litevna-openvna #212): the
firmware's replies are parsed, the SD file is fetched CRC-checked, and
the MCP tools run the job end to end on the virtual device."""

import asyncio
import base64
import json
import threading
import time
import zlib

import pytest

from NanoVNASaver.Hardware import Hires

# --- replies of the real firmware (src/protocol/shell.c cmd_hires) ----------

PLAN = ("hires: 29766 points, 75 frames of up to 397, last point "
        "3000000000 Hz, estimated 83 s (avg 1)")


def test_plan_and_start_are_parsed():
    plan = Hires.parse_plan([PLAN])
    assert (plan.points, plan.frames, plan.per_frame) == (29766, 75, 397)
    assert plan.last_hz == 3_000_000_000 and plan.estimate_s == 83
    assert plan.warnings == []
    plan, path = Hires.parse_started(
        [PLAN, "hires: started, file MEAS/NONAME/HIRES_003.s1p"])
    assert path == "MEAS/NONAME/HIRES_003.s1p"
    warned = Hires.parse_plan([PLAN, "hires: warning: step is finer than "
                               "the synthesizer step at the top"])
    assert warned.warnings and warned.warnings[0].startswith("step is finer")


def test_refusals_carry_the_device_reason():
    with pytest.raises(Hires.HiresError, match="below 23.5 MHz"):
        Hires.parse_plan(["?hires: start is below 23.5 MHz (the synthesizer "
                          "cannot go lower)"])
    with pytest.raises(Hires.HiresError, match="busy"):
        Hires.parse_status(["?busy: a hires job is running"])
    with pytest.raises(Hires.HiresError, match="ei kelpaa"):
        Hires.start_command(1, 2, 3, 1, "x y")


def test_status_lines():
    st = Hires.parse_status(["hires: idle"])
    assert st.state == "idle" and not st.finished
    st = Hires.parse_status([
        "hires: aborted, frame 3/75, point 1191/29766, elapsed 4 s, eta 0 s, avg 1",
        "hires: file MEAS/NONAME/HIRES_004.s1p",
        "hires: stopped",
    ])
    assert st.finished and st.state == "aborted"
    assert (st.frame, st.frames, st.points_done, st.points) == (3, 75, 1191, 29766)
    assert st.file == "MEAS/NONAME/HIRES_004.s1p" and st.message == "stopped"


class FakeCard:
    """fs get as the firmware answers it, with optional damaged replies."""

    def __init__(self, data: bytes, damage: int = 0, lie_crc: bool = False):
        self.data, self.damage, self.lie = data, damage, lie_crc
        self.calls = 0

    def __call__(self, cmd: str, _timeout: float) -> list[str]:
        self.calls += 1
        w = cmd.split()
        crc = zlib.crc32(self.data) & 0xFFFFFFFF
        if len(w) == 3:
            return [f"size {len(self.data)} crc {crc ^ self.lie:08x} chunk 128"]
        off = int(w[3])
        chunk = self.data[off:off + 128]
        c = zlib.crc32(chunk) & 0xFFFFFFFF
        if self.damage:
            self.damage -= 1
            c ^= 1
        return [f"data {off} {len(chunk)} {c:08x} {base64.b64encode(chunk).decode()}"]


def test_fetch_file_checks_every_chunk_and_the_whole():
    data = bytes(range(256)) * 7 + b"tail"
    seen = []
    assert Hires.fetch_file(FakeCard(data), "A.s1p",
                            progress=lambda d, s: seen.append(d)) == data
    assert seen[-1] == len(data)
    # a damaged chunk is asked again
    card = FakeCard(data, damage=2)
    assert Hires.fetch_file(card, "A.s1p") == data
    with pytest.raises(Hires.HiresError, match="liikaa"):
        Hires.fetch_file(FakeCard(data, damage=99), "A.s1p", retries=3)
    with pytest.raises(Hires.HiresError, match="CRC"):
        Hires.fetch_file(FakeCard(data, lie_crc=True), "A.s1p")
    with pytest.raises(Hires.HiresError, match="no such file"):
        Hires.fetch_file(lambda c, t: ["?fs get: X: no such file"], "X")


# --- end to end on the virtual device through MCP ------------------------

mcp = pytest.importorskip("mcp")
try:
    from mcp import Client
except ImportError:  # mcp 1.x
    Client = None

from PySide6 import QtWidgets  # noqa: E402

from NanoVNASaver.Hardware import VirtualVNA  # noqa: E402
from NanoVNASaver.MCP import MCPOptions  # noqa: E402

e2e = pytest.mark.skipif(Client is None, reason="needs mcp >= 2")


@pytest.fixture(scope="module")
def saver(tmp_path_factory):
    VirtualVNA.enable()
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from NanoVNASaver.NanoVNASaver import NanoVNASaver

    ws = tmp_path_factory.mktemp("ws")
    window = NanoVNASaver(MCPOptions(headless=True, workspace=ws))
    svc = window.mcp.build_service()
    window.mcp.control.allow(True)
    yield qapp, window, svc, ws
    window.mcp.control.allow(False)


def run(qapp, factory, timeout=120):
    box = {}

    def worker():
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # pylint: disable=broad-except
            box["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    deadline = time.monotonic() + timeout
    while t.is_alive():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.monotonic() < deadline, "timeout"
    if "error" in box:
        raise box["error"]
    return box["value"]


def call(saver, name, args=None):
    qapp, _, svc, _ = saver

    async def go():
        async with Client(svc.server) as c:
            return await c.call_tool(name, args or {})

    return run(qapp, go)


def ok(result):
    assert not (getattr(result, "is_error", None)
                or getattr(result, "isError", False)), result.content[0].text
    return json.loads(result.content[0].text)


def err(result):
    assert getattr(result, "is_error", None) or getattr(result, "isError", False)
    return result.content[0].text


GRID = {"start_hz": 23_500_000, "stop_hz": 100_000_000, "step_hz": 100_000}


@e2e
def test_plan_run_and_data(saver):
    _, window, _, ws = saver
    ok(call(saver, "device_connect", {"port": "virtual"}))
    plan = ok(call(saver, "device_hires_plan", GRID))
    assert plan["points"] == 766 and plan["frames"] == 2

    res = ok(call(saver, "device_hires_run", {**GRID, "name": "e2e"}))
    assert res["status"] == "done"
    assert res["points"] == res["points_measured"] == 766
    assert res["device_file"].startswith("MEAS/NONAME/e2e_")
    assert (ws / "hires").is_dir() and res["local_file"].endswith(".s1p")
    want = [23_500_000 + k * 100_000 for k in range(766)]
    with window.dataLock:
        assert [d.freq for d in window.data.s11] == want
    assert res["summary"]["points"] == 766
    # the job is over: normal commands work again
    assert ok(call(saver, "device_hires_status"))["state"] == "done"
    ok(call(saver, "sweep_get"))


@e2e
def test_refused_range_names_the_reason(saver):
    text = err(call(saver, "device_hires_run",
                    {"start_hz": 10_000_000, "stop_hz": 20_000_000,
                     "step_hz": 10_000}))
    assert "23.5 MHz" in text


@e2e
def test_stop_through_mcp_while_the_run_holds_the_lock(saver):
    qapp, window, svc, _ = saver
    window.vna.hires.point_s = 0.002  # 766 points: about 1.5 s

    async def go():
        async with Client(svc.server) as a, Client(svc.server) as b:
            task = asyncio.create_task(
                a.call_tool("device_hires_run", {**GRID, "name": "stop"}))
            await asyncio.sleep(0.4)
            stop = await b.call_tool("device_hires_stop", {})
            return stop, await task

    stop, res = run(qapp, go)
    window.vna.hires.point_s = 0.0002
    assert ok(stop)["stopped"] is True
    res = ok(res)
    assert res["status"] == "aborted"
    assert 0 < res["points_measured"] < 766
    with window.dataLock:
        assert len(window.data.s11) == res["points_measured"]
