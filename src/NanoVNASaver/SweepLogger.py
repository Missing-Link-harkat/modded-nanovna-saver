"""Files of a segmented sweep: segmented_sweeps/<time>_<name>/

points.csv   frequency_hz,vswr, appended frame by frame while measuring
settings.csv parameters and, at the end, the outcome (status, error)
sweep.s1p    full range S11 (Touchstone), written at the end
sweep.s2p    full range S11 and S21 (S12/S22 zero), written at the end

Write errors are raised (OSError) so that the caller can stop the sweep
instead of measuring on into a file that is not written."""

import os
from datetime import datetime

from .RFTools import Datapoint
from .Touchstone import Touchstone

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
LOG_ROOT = os.path.join(BASE_DIR, "segmented_sweeps")

_current_file = None
_current_dir = None


def current_dir() -> str | None:
    return _current_dir


def start_log(sweep_name, settings_dict) -> str:
    global _current_file
    global _current_dir

    stop_log()
    os.makedirs(LOG_ROOT, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in sweep_name.strip()
    )
    _current_dir = os.path.join(LOG_ROOT, f"{timestamp}_{safe_name}")
    os.makedirs(_current_dir, exist_ok=True)

    _current_file = open(  # noqa: SIM115  closed in stop_log
        os.path.join(_current_dir, "points.csv"), "w", encoding="utf-8"
    )
    _current_file.write("frequency_hz,vswr\n")
    write_settings(settings_dict)
    return _current_dir


def write_settings(settings_dict) -> None:
    if _current_dir is None:
        return
    with open(os.path.join(_current_dir, "settings.csv"), "w",
              encoding="utf-8") as f:
        f.write("parameter,value\n")
        for key, value in settings_dict.items():
            f.write(f"{key},{value}\n")
        f.write(f"timestamp,{datetime.now()}\n")


def log_points(freqs, vswrs):
    if _current_file is None:
        return
    for f, v in zip(freqs, vswrs, strict=True):
        _current_file.write(f"{f},{v}\n")
    _current_file.flush()


def write_touchstone(s11: list[Datapoint], s21: list[Datapoint]) -> list[str]:
    """Full range Touchstone files; returns their paths."""
    if _current_dir is None or not s11:
        return []
    paths = []
    ts = Touchstone(os.path.join(_current_dir, "sweep.s1p"))
    ts.sdata[0] = s11
    ts.save(1)
    paths.append(ts.filename)
    if len(s21) == len(s11):
        ts = Touchstone(os.path.join(_current_dir, "sweep.s2p"))
        ts.sdata[0] = s11
        ts.sdata[1] = s21
        ts.sdata[2] = [Datapoint(d.freq, 0, 0) for d in s11]
        ts.sdata[3] = [Datapoint(d.freq, 0, 0) for d in s11]
        ts.save(4)
        paths.append(ts.filename)
    return paths


def stop_log():
    global _current_file
    if _current_file:
        _current_file.close()
        _current_file = None
