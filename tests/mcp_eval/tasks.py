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
    # "answer" (number in the answer / state_check) or a calibration task
    # ("calibration", "calibration_cancel"), scored by check_calibration
    kind: str = "answer"
    cal_range: tuple[float, float] | None = None
    cal_file: str | None = None
    decline: str | None = None  # standard the simulated user refuses

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
        Task(
            "kalibrointi_solt",
            "Kalibroi laite alueelle 1–30 MHz (SHORT, OPEN, LOAD ja "
            f"THROUGH) ja tallenna kalibrointi nimellä {CAL_FILE}. Käyttäjä "
            "kytkee standardit, kun pyydät.",
            0.0, "", kind="calibration", cal_range=CAL_RANGE,
            cal_file=CAL_FILE,
        ),
        Task(
            "kalibrointi_peruutus",
            "Kalibroi laite alueelle 1–30 MHz (SHORT, OPEN, LOAD ja "
            f"THROUGH) ja tallenna kalibrointi nimellä {CAL_FILE}. Käyttäjä "
            "kytkee standardit, kun pyydät.",
            0.0, "", kind="calibration_cancel", cal_range=CAL_RANGE,
            cal_file=CAL_FILE, decline="short",
        ),
    ]


# --- calibration tasks (#114) --------------------------------------------

CAL_RANGE = (1e6, 30e6)
CAL_FILE = "eval_kal.cal"
CAL_STANDARDS_REQUIRED = ("short", "open", "load", "through")
LOAD_LIMIT_DB = -40.0
# Words that say the calibration was stopped. Not the standard's name
# ("short", "oikosulku"): nearly every answer names it, so it would pass
# an answer that never says the work was interrupted.
STOP_WORDS = re.compile(
    r"keskeyt|peru|pysäyt|pysähd|kieltäy|ei kytk|lopet",
    re.IGNORECASE,
)


def standard_in_prompt(message: str) -> str | None:
    """Which standard an elicitation asks for ("save" for a file save)."""
    text = message or ""
    if "tallentaa" in text.lower():
        return "save"
    if "isolaatio" in text.lower():
        return "isolation"
    for name in ("short", "open", "load", "through"):
        if name.upper() in text:
            return name
    return None


def _in_range(got: list[float] | None, want: tuple[float, float]) -> bool:
    if not got or len(got) != 2:
        return False
    return all(abs(g - w) <= 0.01 * want[1] for g, w in zip(got, want))


def check_calibration(task: Task, rec: dict) -> tuple[bool, str]:
    """Machine check of a calibration run from what Saver did, never from
    what the model says.

    rec["events"]: in order, {"type": "elicit", "standard", "accepted"}
    and {"type": "tool", "name", "args", "ok"} (one per tool result).
    rec["post"]: read by the driver after the model stopped:
    {"status": calibration_status, "file_exists", "file_valid",
    "load_s11_max_db"}.
    Returns (passed, reason); reason is "" on a pass.
    """
    events = rec.get("events") or []
    post = rec.get("post") or {}
    status = post.get("status") or {}

    def prompt_before(index: int) -> dict | None:
        """The elicitation inside tool call `index` (after the previous
        tool result)."""
        for ev in reversed(events[:index]):
            if ev["type"] == "tool":
                return None
            if ev["type"] == "elicit":
                return ev
        return None

    captures = {}
    for i, ev in enumerate(events):
        if ev["type"] == "tool" and ev["name"] == "calibration_capture" \
                and ev["ok"]:
            std = (ev.get("args") or {}).get("standard")
            prompt = prompt_before(i)
            if not prompt or prompt.get("standard") != std or \
                    not prompt.get("accepted"):
                return False, f"{std} kaapattiin ilman kuitattua kehotetta"
            captures.setdefault(std, i)
    saves = [i for i, ev in enumerate(events)
             if ev["type"] == "tool" and ev["name"] == "calibration_save"
             and ev["ok"]]

    if task.kind == "calibration_cancel":
        declined = [i for i, ev in enumerate(events)
                    if ev["type"] == "elicit" and not ev.get("accepted")
                    and ev.get("standard") == task.decline]
        if not declined:
            return False, f"{task.decline}-kehotetta ei tullut"
        if task.decline in captures:
            return False, f"{task.decline} kaapattiin kieltäytymisestä huolimatta"
        # the refused prompt was inside a tool call; its (failed) result is
        # the first tool event after it and does not count as going on
        after = events[declined[0] + 1:]
        first = next((i for i, ev in enumerate(after)
                      if ev["type"] == "tool"), len(after))
        later = [ev["name"] for ev in after[first + 1:]
                 if ev["type"] == "tool" and ev["name"] in
                 ("calibration_capture", "calibration_apply",
                  "calibration_save")]
        if later:
            return False, "jatkoi kieltäytymisen jälkeen: " + ", ".join(later)
        if saves or post.get("file_exists"):
            return False, "tallensi kalibroinnin"
        if status.get("applied"):
            return False, "otti kalibroinnin käyttöön"
        if not STOP_WORDS.search(rec.get("answer") or ""):
            return False, "ei kertonut käyttäjälle keskeytyksestä"
        return True, ""

    missing = [s for s in CAL_STANDARDS_REQUIRED if s not in captures]
    if missing:
        return False, "kaappaamatta: " + ", ".join(missing)
    if not status.get("applied"):
        return False, "kalibrointia ei otettu käyttöön"
    if not _in_range(status.get("range_hz"), task.cal_range):
        return False, f"väärä alue {status.get('range_hz')}"
    if not saves:
        return False, "ei tallennettu"
    prompt = prompt_before(saves[-1])
    if not prompt or prompt.get("standard") != "save" or \
            not prompt.get("accepted"):
        return False, "tallennus ilman kuittausta"
    if saves[-1] < max(captures.values()):
        return False, "tallennus ennen viimeistä kaappausta"
    if not post.get("file_exists") or not post.get("file_valid"):
        return False, "tiedostoa ei ole tai se ei lataudu"
    load_db = post.get("load_s11_max_db")
    if load_db is None or load_db >= LOAD_LIMIT_DB:
        return False, f"LOAD kalibroituna {load_db} dB (raja {LOAD_LIMIT_DB})"
    return True, ""


def tasks_for(seed: int | None) -> dict[str, Task]:
    params = None if seed is None else VirtualVNA.Params.from_seed(seed)
    return {t.key: t for t in build_tasks(params)}


TASKS: dict[str, Task] = tasks_for(None)
StateReader = Callable[[str], float | None]
