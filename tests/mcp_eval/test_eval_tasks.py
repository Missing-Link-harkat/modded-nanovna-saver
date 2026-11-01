"""The evaluation's truths and answer parser, without a model."""

import math

import pytest

from NanoVNASaver.Hardware import VirtualVNA

from .run_eval import score
from .tasks import TASKS, parse_answer, tasks_for


def test_truths_match_the_virtual_device():
    assert TASKS["antenni_resonanssi"].truth == pytest.approx(14.2e6, rel=1e-3)
    assert TASKS["antenni_vswr"].truth == pytest.approx(50 / 42, abs=1e-3)
    assert TASKS["suodatin_keskitaajuus"].truth == pytest.approx(145e6, rel=1e-4)
    # |S21| = 0.95 / (1 + x^2), x = Q (f/F - F/f): -3 dB at x^2 = sqrt(2) - 1
    bw = 145e6 * (2**0.5 - 1) ** 0.5 / 8
    assert TASKS["suodatin_kaista"].truth == pytest.approx(bw, rel=1e-3)
    # asked at 149 MHz (145 MHz * 1.03, whole MHz)
    x = 8 * (149 / 145 - 145 / 149)
    s21_db = 20 * math.log10(0.95 / (1 + x * x))
    assert TASKS["suodatin_vaimennus"].truth == pytest.approx(s21_db, abs=1e-3)
    assert "149 MHz" in TASKS["suodatin_vaimennus"].prompt


@pytest.mark.parametrize("seed", range(1000, 1020))
def test_seeded_device_stays_inside_the_task_ranges(seed):
    p = VirtualVNA.Params.from_seed(seed)
    assert p == VirtualVNA.Params.from_seed(seed)  # reproducible
    tasks = tasks_for(seed)
    assert 3e6 < tasks["antenni_resonanssi"].truth < 30e6
    assert tasks["antenni_resonanssi"].truth == pytest.approx(p.f0, rel=1e-3)
    f_c = tasks["suodatin_keskitaajuus"].truth
    bw = tasks["suodatin_kaista"].truth
    assert 100e6 < f_c - bw / 2 and f_c + bw / 2 < 200e6


def test_seeds_give_different_devices():
    truths = {tasks_for(s)["antenni_resonanssi"].truth for s in range(10)}
    assert len(truths) == 10


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("Resonanssi on tässä.\nVASTAUS: 14.2 MHz", "Hz", 14.2e6),
        ("VASTAUS: 14,21 MHz", "Hz", 14.21e6),
        ("VASTAUS: 11660 kHz", "Hz", 11.66e6),
        ("VASTAUS: 145000000 Hz", "Hz", 145e6),
        ("VASTAUS: 145000000", "Hz", 145e6),
        ("VASTAUS: 1.19", "", 1.19),
        ("VASTAUS: -0.45 dB", "dB", -0.45),
        ("ensin VASTAUS: 1 MHz, sitten VASTAUS: 2 MHz", "Hz", 2e6),
    ],
)
def test_parse_answer(text, unit, expected):
    assert parse_answer(text, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "unit"),
    [("Resonanssi on 14.2 MHz", "Hz"), ("VASTAUS: 1.19 dB", ""),
     ("VASTAUS: 3 MHz", "dB"), ("", "Hz")],
)
def test_missing_or_wrong_unit_is_none(text, unit):
    assert parse_answer(text, unit) is None


def _rec(answer, sweeps):
    return {"answer": answer, "sweeps": sweeps}


def test_right_answer_without_a_sweep_is_a_guess():
    task = TASKS["antenni_resonanssi"]
    answer = f"VASTAUS: {task.truth / 1e6:.3f} MHz"
    guess = score(task, _rec(answer, []))
    assert not guess["pass"]
    assert guess["reason"] == "ei mitattu (arvaus osui)"
    # a sweep that missed the resonance does not count either
    assert not score(task, _rec(answer, [(20e6, 30e6)]))["pass"]
    measured = score(task, _rec(answer, [(3e6, 30e6)]))
    assert measured["pass"] and measured["reason"] == ""


def test_wrong_measured_answer_and_no_answer():
    task = TASKS["antenni_resonanssi"]
    wrong = score(task, _rec("VASTAUS: 20 MHz", [(3e6, 30e6)]))
    assert wrong["reason"] == "väärä arvo"
    none = score(task, _rec("en tiedä", [(3e6, 30e6)]))
    assert none["reason"] == "ei vastausta" and none["value"] is None


def test_tolerance():
    task = TASKS["antenni_resonanssi"]
    assert task.ok(task.truth * 1.009)
    assert not task.ok(task.truth * 1.02)
    assert not task.ok(None)
