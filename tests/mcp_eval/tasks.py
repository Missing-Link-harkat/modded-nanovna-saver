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
"""MCP evaluation tasks and their checker.

Every task is answered against the virtual VNA without a file, so the
correct answer is computed from VirtualVNA.model() itself, not typed in
by hand. With a seed (tasks_for) the antenna and filter are drawn from
VirtualVNA.Params.from_seed, the same values Saver simulates with
`--virtual-seed`, so the answer cannot come from memory.

A task passes when the model's final line "VASTAUS: <number> <unit>"
(or for an action task, Saver's state afterwards) is within the
tolerance of the truth AND a sweep (sweep_run, or the one
device_connect runs) covered the frequency the answer depends on
(Task.at_hz): a right number without a measurement is a guess, not a
pass.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from NanoVNASaver.Hardware import VirtualVNA

ANSWER_RE = re.compile(
    r"VASTAUS\s*:\s*([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*([a-zA-Zµ]*)",
)
PREFIX = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}


@dataclass
class Task:
    key: str
    prompt: str
    truth: float
    unit: str  # "Hz", "dB" or "" (ratio)
    rel_tol: float = 0.0
    abs_tol: float = 0.0
    # For action tasks: reads Saver's state through the MCP session and
    # returns the value to compare instead of the model's answer.
    state_check: str | None = None
    # A sweep must have covered this frequency for the answer to count.
    at_hz: float | None = None
    notes: list[str] = field(default_factory=list)

    def tolerance(self) -> float:
        return max(self.abs_tol, abs(self.truth) * self.rel_tol)

    def ok(self, value: float | None) -> bool:
        return value is not None and abs(value - self.truth) <= self.tolerance()


def parse_answer(text: str, unit: str) -> float | None:
    """Last "VASTAUS: ..." line as a number in the task's base unit.

    Returns None when there is no answer line or the unit is wrong, so a
    missing answer is not mistaken for 0.
    """
    matches = ANSWER_RE.findall(text or "")
    if not matches:
        return None
    number, suffix = matches[-1]
    value = float(number.replace(",", "."))
    suffix = suffix.replace("µ", "u")
    if unit == "Hz":
        low = suffix.lower()
        if not low.endswith("hz"):
            return None if suffix else value
        return value * PREFIX.get(low[:-2], math.nan)
    if unit == "dB":
        return value if suffix.lower() in ("", "db") else None
    return value if suffix == "" else None


def _fine(start: float, stop: float, n: int = 200_001) -> np.ndarray:
    return np.linspace(start, stop, n)


def _antenna(p: VirtualVNA.Params) -> tuple[float, float]:
    f = _fine(3e6, 30e6)
    s11, _ = VirtualVNA.model(f, p)
    i = int(np.argmin(np.abs(s11)))
    g = abs(s11[i])
    return float(f[i]), float((1 + g) / (1 - g))


def _filter(p: VirtualVNA.Params) -> tuple[float, float]:
    f = _fine(100e6, 200e6)
    _, s21 = VirtualVNA.model(f, p)
    mag = np.abs(s21)
    i = int(np.argmax(mag))
    band = f[mag >= mag[i] / math.sqrt(2)]
    return float(f[i]), float(band[-1] - band[0])


def _s21_db(p: VirtualVNA.Params, freq: float) -> float:
    return 20 * math.log10(abs(VirtualVNA.model(np.array([freq]), p)[1][0]))


def build_tasks(p: VirtualVNA.Params | None = None) -> list[Task]:
    p = p or VirtualVNA.Params()
    f_res, vswr_min = _antenna(p)
    f_c, bw = _filter(p)
    # a whole-MHz point on the filter's upper slope
    f_meas = round(p.f_filter * 1.03 / 1e6) * 1e6
    tail = (
        " Lopeta vastauksesi rivillä muodossa "
        "'VASTAUS: <luku> <yksikkö>'."
    )
    return [
        Task(
            "antenni_resonanssi",
            "Laitteeseen on kytketty HF-antenni. Millä taajuudella sen "
            "VSWR on pienin 3–30 MHz:n alueella?" + tail,
            f_res, "Hz", rel_tol=0.01, at_hz=f_res,
        ),
        Task(
            "antenni_vswr",
            "Laitteeseen on kytketty HF-antenni. Mikä on sen pienin VSWR "
            "3–30 MHz:n alueella? Anna VSWR pelkkänä lukuna ilman "
            "yksikköä." + tail,
            vswr_min, "", abs_tol=0.05, at_hz=f_res,
        ),
        Task(
            "suodatin_keskitaajuus",
            "Laitteen porttien välissä on kaistanpäästösuodatin jossain "
            "100–200 MHz:n välillä. Millä taajuudella sen läpäisy (S21) "
            "on suurin?" + tail,
            f_c, "Hz", rel_tol=0.01, at_hz=f_c,
        ),
        Task(
            "suodatin_kaista",
            "Laitteen porttien välissä on kaistanpäästösuodatin jossain "
            "100–200 MHz:n välillä. Mikä on sen −3 dB:n kaistanleveys?"
            + tail,
            bw, "Hz", rel_tol=0.05, at_hz=f_c,
        ),
        Task(
            "suodatin_vaimennus",
            "Laitteen porttien välissä on suodatin. Mittaa sen läpäisy "
            f"(S21) taajuudella {f_meas / 1e6:.0f} MHz desibeleinä." + tail,
            _s21_db(p, f_meas), "dB", abs_tol=0.2, at_hz=f_meas,
        ),
        Task(
            "markkeri_resonanssiin",
            "Laitteeseen on kytketty HF-antenni. Etsi taajuus, jolla sen "
            "VSWR on pienin 3–30 MHz:n alueella, ja siirrä markkeri 1 "
            "(indeksi 0) täsmälleen siihen. Kerro lopuksi markkerin "
            "taajuus." + tail,
            f_res, "Hz", rel_tol=0.01, state_check="marker0_hz",
            at_hz=f_res,
        ),
    ]


def tasks_for(seed: int | None) -> dict[str, Task]:
    params = None if seed is None else VirtualVNA.Params.from_seed(seed)
    return {t.key: t for t in build_tasks(params)}


TASKS: dict[str, Task] = tasks_for(None)
StateReader = Callable[[str], float | None]
