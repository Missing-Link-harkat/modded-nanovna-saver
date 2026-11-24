#  NanoVNASaver
#
#  A python program to view and export Touchstone data from a NanoVNA
#  Copyright (C) 2019, 2020  Rune B. Broberg
#  Copyright (C) 2020,2021 NanoVNA-Saver Authors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Simulated VNA for development, tests and MCP evaluation.

Without a file it simulates an antenna (series RLC resonant at 14.2 MHz
behind 1 m of coax) on S11 and a band-pass filter on S21. With a
Touchstone file it replays the file, interpolated to the requested
frequencies. Enable with `--virtual [file]`; it then appears as port
"virtual" in the port list. `--virtual-seed N` draws the antenna and
filter from N instead (Params.from_seed), so an evaluation run cannot
be answered from memory of the default values.

It also has a simulated 480x320 device screen (getScreenshot), so the
device_screenshot tool and UI tests can be developed without hardware.
"""

import base64
import logging
import math
import zlib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from time import monotonic, sleep

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap

from ..Touchstone import Touchstone
from ..utils.version import Version
from .Serial import Interface
from .VNA import VNA

logger = logging.getLogger(__name__)

ENABLED = False
SOURCE: str | None = None

F0 = 14.2e6  # antenna resonance
R_ANT = 42.0
Q_ANT = 12.0
COAX_M = 1.0
VF = 0.66
F_FILTER = 145e6  # band-pass centre
Q_FILTER = 8.0
GAIN_FILTER = 0.95


@dataclass(frozen=True)
class Params:
    """The simulated antenna (S11) and band-pass filter (S21)."""

    f0: float = F0
    r_ant: float = R_ANT
    q_ant: float = Q_ANT
    coax_m: float = COAX_M
    vf: float = VF
    f_filter: float = F_FILTER
    q_filter: float = Q_FILTER
    gain: float = GAIN_FILTER

    @classmethod
    def from_seed(cls, seed: int) -> "Params":
        """Random but reproducible values. The ranges keep the antenna
        inside 3-30 MHz and the filter's -3 dB band inside 100-200 MHz,
        the ranges the evaluation tasks name."""
        rng = np.random.default_rng(seed)
        return cls(
            f0=round(rng.uniform(4e6, 28e6), -3),
            r_ant=round(rng.uniform(25.0, 75.0), 1),
            q_ant=round(rng.uniform(6.0, 20.0), 1),
            coax_m=round(rng.uniform(0.5, 3.0), 2),
            f_filter=round(rng.uniform(115e6, 185e6), -4),
            q_filter=round(rng.uniform(5.0, 12.0), 1),
            gain=round(rng.uniform(0.7, 0.98), 3),
        )


PARAMS = Params()

# Calibration bench (--virtual-bench FILE, litevna-openvna#114): the file
# names what is connected to the ports, written by whoever plays the user
# (the evaluation driver), never through MCP. The device then measures
# through a raw error model, so only a correct calibration gives a clean
# result: an uncorrected LOAD reads about -26 dB, a corrected one far
# below -40 dB.
BENCH: Path | None = None
BENCH_STANDARDS = ("dut", "short", "open", "load", "through", "isolation")
GAMMA = {"short": -1.0, "open": 1.0, "load": 0.0}


def enable(
    source: str | None = None,
    seed: int | None = None,
    bench: str | Path | None = None,
) -> None:
    global ENABLED, SOURCE, PARAMS, BENCH  # noqa: PLW0603  pylint: disable=global-statement
    ENABLED = True
    SOURCE = source
    PARAMS = Params() if seed is None else Params.from_seed(seed)
    BENCH = Path(bench) if bench else None


def bench_connected(path: Path | None = None) -> str:
    """What the bench file says is connected ("dut" when empty/missing)."""
    path = path or BENCH
    try:
        text = path.read_text(encoding="utf-8").strip().lower() if path else ""
    except OSError:
        text = ""
    return text if text in BENCH_STANDARDS else "dut"


def raw_errors(freqs: np.ndarray) -> dict[str, np.ndarray]:
    """Error terms of the simulated, uncalibrated device: directivity
    e00, source match e11, reflection tracking e10e01 (with a little
    cable delay), isolation e30 and transmission tracking e10e32."""
    w = 2 * math.pi * freqs
    tau = 0.35e-9  # one way, about 7 cm of cable inside the box
    return {
        "e00": 0.05 * np.exp(-1j * w * 0.6e-9),
        "e11": 0.10 * np.exp(-1j * w * 0.9e-9),
        "e10e01": 0.90 * np.exp(-2j * w * tau),
        "e30": np.full(len(freqs), 0.001 + 0j),
        "e10e32": 0.92 * np.exp(-2j * w * tau),
    }


def bench_values(
    freqs: np.ndarray, connected: str, p: Params | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Raw S11, S21 with `connected` on the ports."""
    if connected in GAMMA:
        gamma = np.full(len(freqs), GAMMA[connected], dtype=complex)
        s21 = np.zeros(len(freqs), dtype=complex)
    elif connected == "through":
        gamma = np.zeros(len(freqs), dtype=complex)
        s21 = np.ones(len(freqs), dtype=complex)
    elif connected == "isolation":
        gamma = np.zeros(len(freqs), dtype=complex)
        s21 = np.zeros(len(freqs), dtype=complex)
    else:
        gamma, s21 = model(freqs, p)
    e = raw_errors(freqs)
    s11m = e["e00"] + e["e10e01"] * gamma / (1 - e["e11"] * gamma)
    s21m = e["e30"] + e["e10e32"] * s21
    return s11m, s21m


def make_interface() -> "VirtualInterface":
    iface = VirtualInterface("serial", "VirtualVNA")
    iface.port = "virtual"
    return iface


class VirtualInterface(Interface):
    """Interface that never touches a real serial port."""

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def isOpen(self) -> bool:  # noqa: N802 (pyserial API)
        return self.is_open

    def _reconfigure_port(self, *args, **kwargs) -> None:
        pass


def model(
    freqs: np.ndarray, p: Params | None = None
) -> tuple[np.ndarray, np.ndarray]:
    p = p or PARAMS
    w = 2 * math.pi * freqs
    w0 = 2 * math.pi * p.f0
    ind = p.q_ant * p.r_ant / w0
    cap = 1 / (w0 * w0 * ind)
    z = p.r_ant + 1j * (w * ind - 1 / (w * cap))
    gamma = (z - 50) / (z + 50)
    delay = 2 * p.coax_m / (p.vf * 299_792_458)
    s11 = gamma * np.exp(-1j * w * delay)
    x = p.q_filter * (freqs / p.f_filter - p.f_filter / freqs)
    s21 = p.gain / (1 + 1j * x) ** 2
    return s11, s21


SCREEN_W, SCREEN_H = 480, 320  # LiteVNA64 display
SCREEN_DB_TOP, SCREEN_DB_BOTTOM = 0.0, -40.0  # S11 log-mag scale
_PLOT = (40, 24, 440, 272)  # x, y, w, h of the trace area


def render_screen(
    start: int, stop: int, s11: np.ndarray, title: str = "VirtualVNA"
) -> QImage:
    """The simulated device screen: header, 10 x 8 grid, the S11 log-mag
    trace of `s11` (evenly spaced over start..stop) and the range.
    Deterministic for the same input, so tests can compare pixels."""
    img = QImage(SCREEN_W, SCREEN_H, QImage.Format.Format_RGB32)
    img.fill(QColor(0, 0, 0))
    x0, y0, w, h = _PLOT
    p = QPainter(img)
    try:
        p.setPen(QPen(QColor(64, 64, 64), 1))
        for i in range(11):
            x = x0 + w * i / 10
            p.drawLine(QPointF(x, y0), QPointF(x, y0 + h))
        for i in range(9):
            y = y0 + h * i / 8
            p.drawLine(QPointF(x0, y), QPointF(x0 + w, y))
        db = 20 * np.log10(np.maximum(np.abs(s11), 1e-6))
        span = SCREEN_DB_TOP - SCREEN_DB_BOTTOM
        frac = np.clip((SCREEN_DB_TOP - db) / span, 0.0, 1.0)
        pts = [
            QPointF(x0 + w * i / max(len(db) - 1, 1), y0 + h * f)
            for i, f in enumerate(frac)
        ]
        p.setPen(QPen(QColor(255, 255, 0), 2))
        for a, b in pairwise(pts):
            p.drawLine(a, b)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Sans", 9))
        p.drawText(4, 16, f"{title}  S11 LOGMAG 5dB/")
        p.drawText(
            4, SCREEN_H - 8,
            f"START {start / 1e6:.3f} MHz   STOP {stop / 1e6:.3f} MHz",
        )
    finally:
        p.end()
    return img


class VirtualHires:
    """The firmware's `hires` job and `fs get` (litevna-openvna #207), as
    the shell shows them: the job advances with wall time (point_s per
    point and average) and writes raw S11 of the simulated device to a
    Touchstone file kept in memory."""

    F_LOW = 23_500_000
    F_HIGH = 6_000_000_000
    TOTAL_MAX = 200_000
    FRAME = 401
    CHUNK = 128
    BUSY = "?busy: a hires job is running (hires status, hires stop)"
    ALLOWED = {"hires", "help", "version", "info", "sweep-stats", "usb",
               "reset", "capture"}

    def __init__(self, vna: "VirtualVNA") -> None:
        self.vna = vna
        self.point_s = 0.0002
        self.files: dict[str, bytes] = {}
        self.run = 0
        self.job: dict | None = None

    # --- helpers ---------------------------------------------------------

    def _s11(self, freqs: np.ndarray) -> np.ndarray:
        """Raw S11 as the firmware keeps it: float32 re/im (the file's rows
        and the stream's values are then the same numbers, #210)."""
        return self.vna._values(freqs)[0].astype(np.complex64)  # pylint: disable=protected-access

    def link(self):
        """A byte-level link as the serial port gives it: shell text, the
        prompt and, with out=usb|both, the stream packets (#210)."""
        from . import Hires  # pylint: disable=import-outside-toplevel
        buf = bytearray()

        def send(cmd: str) -> None:
            lines = self.handle(cmd.split())
            if lines is None:
                lines = list(self.vna.exec_command(cmd))
            job = self.job
            if job is not None and job.pop("begin_due", False):
                plan = job["plan"]
                buf.extend(Hires.encode_begin(
                    plan["start"], plan["step"], plan["total"], plan["frames"],
                    plan["per"], plan["avg"], 0, {"usb": 2, "both": 3}[job["out"]],
                    self.run, job["path"]))
            buf.extend(("\r\n".join(lines) + "\r\nch> ").encode("ascii"))

        def read(timeout: float) -> bytes:
            self._stream(buf, Hires)
            if not buf:
                sleep(min(timeout, 0.02))
                self._stream(buf, Hires)
            data = bytes(buf)
            buf.clear()
            return data

        return Hires.Link(lambda c, t: list(self.vna.exec_command(c)), send, read)

    def _stream(self, buf: bytearray, hires) -> None:
        """Frames finished since the last call, and END when the job is over."""
        job = self.job
        if job is None or job.get("out", "sd") == "sd" or job.get("ended"):
            return
        done = self._progress()
        plan = job["plan"]
        complete = plan["frames"] if job["state"] == "done" else done // plan["per"]
        while job["sent"] < complete:
            fr = job["sent"]
            first = fr * plan["per"]
            n = min(plan["per"], plan["total"] - first)
            freqs = np.array([plan["start"] + (first + k) * plan["step"]
                              for k in range(n)], dtype=float)
            buf.extend(hires.encode_frame(fr, first, self._s11(freqs)))
            job["sent"] += 1
        if job["state"] != "running":
            buf.extend(hires.encode_end({"done": 2, "aborted": 3}.get(job["state"], 4),
                                        job["done"], job["sent"],
                                        "" if job["state"] == "done" else "stop requested"))
            job["ended"] = True

    def _plan(self, words: list[str]) -> tuple[dict | None, list[str]]:
        try:
            a, b, st = (int(w) for w in words[2:5])
            avg = int(words[5]) if len(words) > 5 else 1
        except ValueError:
            return None, ["?usage: hires plan <start> <stop> <step> [avg]"]
        if not 1 <= avg <= 16:
            return None, ["?hires: avg must be 1..16"]
        if st == 0:
            return None, ["?hires: step must be above 0"]
        if b < a:
            return None, ["?hires: stop is below start"]
        if a < self.F_LOW:
            return None, ["?hires: start is below 23.5 MHz (the synthesizer "
                          "cannot go lower)"]
        if b > self.F_HIGH:
            return None, ["?hires: stop is above 6.0 GHz (the synthesizer "
                          "cannot go higher)"]
        total = (b - a) // st + 1
        if total > self.TOTAL_MAX:
            return None, ["?hires: too many points (limit 200000)"]
        frames = math.ceil(total / self.FRAME)
        per = math.ceil(total / frames)
        frames = math.ceil(total / per)
        plan = {"start": a, "step": st, "total": total, "frames": frames,
                "per": per, "avg": avg}
        est = int(total * avg * self.point_s)
        lines = [f"hires: {total} points, {frames} frames of up to {per}, "
                 f"last point {a + (total - 1) * st} Hz, estimated {est} s "
                 f"(avg {avg})"]
        # synthesizer step at the top: 12 kHz above 3 GHz, halved per octave
        res, f = 12000, a + (total - 1) * st
        while f < 3_000_000_000 and res > 1:
            f, res = f * 2, res // 2
        if st < res:
            lines.append("hires: warning: step is finer than the synthesizer "
                         "step at the top of the range (12 kHz above 3 GHz, "
                         "half per octave below)")
        return plan, lines

    def _progress(self) -> int:
        job = self.job
        if job["state"] != "running":
            return job["done"]
        done = int((monotonic() - job["t0"]) / (self.point_s * job["plan"]["avg"]))
        if done >= job["plan"]["total"]:
            self._finish("done", job["plan"]["total"])
            return job["done"]
        return done

    def _finish(self, state: str, done: int) -> None:
        job = self.job
        job["state"], job["done"] = state, done
        job["t1"] = monotonic()
        plan = job["plan"]
        if "sd" not in job.get("out", "sd") and job.get("out") != "both":
            return                                    # out=usb: no file (#210)
        freqs = np.array([plan["start"] + k * plan["step"] for k in range(done)],
                         dtype=float)
        s11 = self._s11(freqs) if done else []
        rows = [f"{int(f)} {v.real:.6e} {v.imag:.6e}" for f, v in zip(freqs, s11)]
        note = (f"! finished: virtual, {done} points" if state == "done" else
                f"! aborted at frame {done // plan['per'] + 1} (stopped), "
                f"{done} points written")
        text = "\r\n".join([
            "! Touchstone export from VirtualVNA, hires sweep (#207)",
            f"! hires: {plan['start']} Hz .. step {plan['step']} Hz, "
            f"{plan['total']} points",
            "! calibration: none (raw S11)",
            "# Hz S RI R 50", *rows, note, ""])
        self.files[job["path"]] = text.encode("ascii")

    def _status(self) -> list[str]:
        if self.job is None:
            return ["hires: idle"]
        done = self._progress()
        job, plan = self.job, self.job["plan"]
        end = job.get("t1", monotonic())
        frame = min(plan["frames"], done // plan["per"] + 1)
        eta = int(max(0, plan["total"] - done) * plan["avg"] * self.point_s)
        lines = [f"hires: {job['state']}, frame {frame}/{plan['frames']}, "
                 f"point {done}/{plan['total']}, elapsed {int(end - job['t0'])} s, "
                 f"eta {eta if job['state'] == 'running' else 0} s, "
                 f"avg {plan['avg']}",
                 f"hires: file {job['path']}"]
        if job["state"] == "aborted":
            lines.append("hires: stopped")
        return lines

    def _fs_get(self, words: list[str]) -> list[str]:
        data = self.files.get(words[2])
        if data is None:
            return [f"?fs get: {words[2]}: no such file"]
        if len(words) == 3:
            return [f"size {len(data)} crc {zlib.crc32(data) & 0xFFFFFFFF:08x} "
                    f"chunk {self.CHUNK}"]
        off = int(words[3])
        if off >= len(data):
            return ["eof"]
        chunk = data[off:off + self.CHUNK]
        return [f"data {off} {len(chunk)} {zlib.crc32(chunk) & 0xFFFFFFFF:08x} "
                f"{base64.b64encode(chunk).decode()}"]

    # --- shell -------------------------------------------------------------

    def running(self) -> bool:
        if self.job is None or self.job["state"] != "running":
            return False
        self._progress()  # may finish the job
        return self.job["state"] == "running"

    def handle(self, words: list[str]) -> list[str] | None:
        """Reply lines for a hires/fs get command or a busy refusal; None
        when the command is not ours and no job blocks it."""
        if not words:
            return None
        if words[0] == "fs" and len(words) >= 3 and words[1] == "get":
            return [self.BUSY] if self.running() else self._fs_get(words)
        if words[0] != "hires":
            if self.running() and words[0] not in self.ALLOWED:
                return [self.BUSY]
            return None
        sub = words[1] if len(words) > 1 else ""
        out = "sd"                                    # #210: out=sd|usb|both anywhere
        for w in [w for w in words if w.startswith("out=")]:
            out = w[4:]
            words = [x for x in words if x != w]
            if out not in ("sd", "usb", "both"):
                return ["?hires: out=sd|usb|both"]
        if sub == "status":
            return self._status()
        if sub == "stop":
            if not self.running():
                return ["hires: no job running"]
            self._finish("aborted", self._progress())
            return ["hires: stopping (the file keeps what was measured)"]
        if sub in ("plan", "start") and len(words) >= 5:
            plan, lines = self._plan(words)
            if plan is None or sub == "plan":
                return lines
            if self.running():
                return [*lines, "?hires: a hires job is already running"]
            name = words[6] if len(words) > 6 else "HIRES"
            self.run += 1
            path = "" if out == "usb" else f"MEAS/NONAME/{name}_{self.run:03d}.s1p"
            self.job = {"plan": plan, "path": path, "state": "running",
                        "done": 0, "t0": monotonic(), "out": out, "sent": 0,
                        "begin_due": out != "sd"}
            tail = "" if out == "sd" else ", stream on (0xA5 packets 0x20-0x22)"
            return [*lines, f"hires: started, file {path or '(none)'}{tail}"]
        return ["?usage: hires plan <start> <stop> <step> [avg] | hires start "
                "<start> <stop> <step> [avg] [name] | hires status | hires stop"]


class VirtualVNA(VNA):
    name = "VirtualVNA"
    valid_datapoints = (101, 201, 401, 51, 11)
    sweep_points_min = 11
    sweep_points_max = 1001
    sweep_max_freq_hz = 6e9
    SN = "VIRTUAL-0001"
    hardware_revision = "virtual"

    def __init__(self, iface: Interface):
        self._start = 3_600_000
        self._stop = 30_000_000
        self._file: Touchstone | None = None
        self._rng = np.random.default_rng(1)
        self.shell_log: list[str] = []
        self.noise = 0.0005
        self.segment_delay = 0.05
        self.hires = VirtualHires(self)
        if SOURCE:
            self._file = Touchstone(SOURCE)
            self._file.load()
            logger.info("VirtualVNA replays %s", SOURCE)
        super().__init__(iface)

    def read_fw_version(self) -> Version:
        return Version.parse("1.0.0")

    def exec_command(self, command: str, wait: float = 0.0):
        """Simulated firmware text shell for tests (device_shell tools):
        answers like the Missing Link firmware for a few commands."""
        self.shell_log.append(command)
        words = command.split()
        reply = self.hires.handle(words)
        if reply is not None:
            yield from reply
            return
        if command == "version":
            yield "Version 1.0.0"
            yield "proto 1.0"
        elif command == "info":
            yield "Board: VIRTUAL (NanoVNA-H 4 compatible, virtual)"
            yield "Features: hires (out=sd|usb|both, stream v1), hires-cal (interp|reim)"
        elif command == "sweep-stats":
            yield "sweeps: 0 late: 0"
        elif command == "sweep" and len(words) == 1:
            yield f"{self._start} {self._stop} 101"
        elif words and words[0] == "bogus":
            yield "?unknown command: bogus"
        else:
            yield f"ok: {command}"

    def init_features(self) -> None:
        self.features.add("Customizable data points")
        self.features.add("Screenshots")

    def screen_image(self) -> QImage:
        """The simulated screen for the current range, without noise.

        With --virtual-bench this draws the raw, uncalibrated measurement
        of whatever the bench file says is connected, as a real device's
        own screen would (Saver's calibration is not applied on it)."""
        freqs = self._freqs()
        s11, _ = self._values(freqs)
        return render_screen(self._start, self._stop, np.asarray(s11))

    def getScreenshot(self) -> QPixmap:
        return QPixmap.fromImage(self.screen_image())

    def get_bandwidths(self) -> list[int]:
        return [1000]

    def set_bandwidth(self, bandwidth: int) -> None:
        self.bandwidth = bandwidth

    def getCalibration(self) -> str:
        return "virtual (ideal)"

    def flushSerialBuffers(self) -> None:
        pass

    def getSerialNumber(self) -> str:
        return self.SN

    def readFirmware(self) -> str:
        return "VirtualVNA 1.0.0"

    def setSweep(self, start, stop) -> None:
        self._start = int(start)
        self._stop = int(stop)

    def resetSweep(self, start: int, stop: int) -> None:
        self.setSweep(start, stop)

    def _freqs(self) -> np.ndarray:
        return np.linspace(self._start, self._stop, self.datapoints)

    def read_frequencies(self) -> list[int]:
        return [round(f) for f in self._freqs()]

    def _values(self, freqs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if BENCH is not None:
            return bench_values(freqs, bench_connected())
        if self._file is None:
            return model(freqs)
        out = []
        for data in (self._file.s11, self._file.s21):
            if not data:
                out.append(np.zeros(len(freqs), dtype=complex))
                continue
            f = np.array([d.freq for d in data], dtype=float)
            re = np.interp(freqs, f, [d.re for d in data])
            im = np.interp(freqs, f, [d.im for d in data])
            out.append(re + 1j * im)
        return out[0], out[1]

    def readValues(self, value) -> list[complex]:
        freqs = self._freqs()
        if value == "frequencies":
            return [complex(f, 0) for f in freqs]
        s11, s21 = self._values(freqs)
        data = s11 if value == "data 0" else s21
        if value == "data 0":
            sleep(self.segment_delay)  # a real sweep takes time
        noise = self._rng.normal(0, self.noise, (2, len(data)))
        return [complex(v) + complex(a, b) for v, a, b in
                zip(data, noise[0], noise[1])]
