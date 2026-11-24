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


# --- the stream (litevna-openvna #210) --------------------------------------


def _stream_bytes():
    vals0 = [complex(0.5, -0.25), complex(0.125, 0.0)]
    vals1 = [complex(1e-3, 2e-3), complex(-0.5, 0.75)]
    return (b"hires: started, file X\r\nch> "
            + Hires.encode_begin(1000, 10, 4, 2, 2, avg=1, out=3, name="X.s1p")
            + Hires.encode_frame(0, 0, vals0) + b"hires: running\r\nch> "
            + Hires.encode_frame(1, 2, vals1) + Hires.encode_end(2, 4, 2) + b"\r\nch> ")


def test_stream_packets_are_found_between_shell_text():
    st = Hires.Stream().feed(_stream_bytes())
    assert st.bad == 0 and st.end["state"] == "done"
    assert [p[0] for p in st.points()] == [1000, 1010, 1020, 1030]
    assert st.rows().splitlines()[0] == "1000 5.000000e-01 -2.500000e-01"
    assert "hires: running" in st.text_lines()
    # in small pieces, as a serial port gives them
    data = _stream_bytes()
    st = Hires.Stream()
    for i in range(0, len(data), 5):
        st.feed(data[i:i + 5])
    assert len(st.points()) == 4 and st.end is not None
    # one flipped bit: that frame is dropped, never half-used
    bad = bytearray(data)
    bad[data.index(b"\xa5\x21") + 20] ^= 1
    st = Hires.Stream().feed(bytes(bad))
    assert st.bad == 1 and sorted(st.frames) == [1]


def test_stream_command_and_feature():
    assert Hires.start_command(1, 2, 3, 1, "A", "usb").endswith(" A out=usb")
    assert "out=" not in Hires.start_command(1, 2, 3, 1, "A")
    with pytest.raises(Hires.HiresError, match="out="):
        Hires.start_command(1, 2, 3, 1, "A", "disk")
    assert Hires.supports_stream(["Board: x", "Features: hires (out=sd|usb|both, stream v1)"])
    assert not Hires.supports_stream(["Board: x"])
    assert Hires.parse_started_any([PLAN, "hires: started, file (none), stream on"])[1] == ""


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


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and ln[0].isdigit()]


@e2e
def test_stream_both_is_the_file_bit_for_bit(saver):
    """out=both (#210): the frames over USB rebuild the SD file's rows exactly."""
    _, window, _, _ = saver
    ok(call(saver, "device_connect", {"port": "virtual"}))
    res = ok(call(saver, "device_hires_run", {**GRID, "name": "both", "out": "both"}))
    assert res["status"] == "done" and res["points_measured"] == 766
    assert res["device_file"].startswith("MEAS/NONAME/both_")
    streamed = open(res["local_file"], encoding="ascii").read()
    on_card = window.vna.hires.files[res["device_file"]].decode("ascii")
    assert _rows(streamed) == _rows(on_card)
    with window.dataLock:
        assert len(window.data.s11) == 766


@e2e
def test_stream_usb_only_and_stop(saver):
    qapp, window, svc, _ = saver
    ok(call(saver, "device_connect", {"port": "virtual"}))
    res = ok(call(saver, "device_hires_run", {**GRID, "name": "usb", "out": "usb"}))
    assert res["status"] == "done" and res["device_file"] == ""
    assert res["local_file"].endswith("usb_stream.s1p") and res["points_measured"] == 766
    assert "usb" not in str(list(window.vna.hires.files))       # no file on the card

    window.vna.hires.point_s = 0.002

    async def go():
        async with Client(svc.server) as a, Client(svc.server) as b:
            task = asyncio.create_task(a.call_tool(
                "device_hires_run", {**GRID, "name": "us", "out": "usb"}))
            await asyncio.sleep(0.9)
            status = await b.call_tool("device_hires_status", {})
            stop = await b.call_tool("device_hires_stop", {})
            return status, stop, await task

    status, stop, res = run(qapp, go)
    window.vna.hires.point_s = 0.0002
    assert ok(status)["saver_following"] is True
    assert ok(stop)["stopped"] is True
    res = ok(res)
    assert res["status"] == "aborted"
    assert res["points_measured"] in (0, 383)    # only whole frames are streamed
