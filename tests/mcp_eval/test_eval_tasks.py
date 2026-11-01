"""The evaluation's truths and answer parser, without a model."""

import pytest

from .tasks import TASKS, parse_answer


def test_truths_match_the_virtual_device():
    assert TASKS["antenni_resonanssi"].truth == pytest.approx(14.2e6, rel=1e-3)
    assert TASKS["antenni_vswr"].truth == pytest.approx(50 / 42, abs=1e-3)
    assert TASKS["suodatin_keskitaajuus"].truth == pytest.approx(145e6, rel=1e-4)
    # |S21| = 0.95 / (1 + x^2), x = Q (f/F - F/f): -3 dB at x^2 = sqrt(2) - 1
    bw = 145e6 * (2**0.5 - 1) ** 0.5 / 8
    assert TASKS["suodatin_kaista"].truth == pytest.approx(bw, rel=1e-3)
    assert TASKS["suodatin_vaimennus"].truth == pytest.approx(-0.446, abs=1e-3)


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


def test_tolerance():
    task = TASKS["antenni_resonanssi"]
    assert task.ok(task.truth * 1.009)
    assert not task.ok(task.truth * 1.02)
    assert not task.ok(None)
