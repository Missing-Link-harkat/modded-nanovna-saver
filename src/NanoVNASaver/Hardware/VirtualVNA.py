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

import logging
import math
from dataclasses import dataclass
from itertools import pairwise
from time import sleep

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


def enable(source: str | None = None, seed: int | None = None) -> None:
    global ENABLED, SOURCE, PARAMS  # noqa: PLW0603  pylint: disable=global-statement
    ENABLED = True
    SOURCE = source
    PARAMS = Params() if seed is None else Params.from_seed(seed)


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
        self.noise = 0.0005
        self.segment_delay = 0.05
        if SOURCE:
            self._file = Touchstone(SOURCE)
            self._file.load()
            logger.info("VirtualVNA replays %s", SOURCE)
        super().__init__(iface)

    def read_fw_version(self) -> Version:
        return Version.parse("1.0.0")

    def init_features(self) -> None:
        self.features.add("Customizable data points")
        self.features.add("Screenshots")

    def screen_image(self) -> QImage:
        """The simulated screen for the current range, without noise."""
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
