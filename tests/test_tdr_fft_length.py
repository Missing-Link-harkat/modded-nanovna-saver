"""TDR with more sweep points than FFT points (#11).

np.pad got a negative padding and raised "index can't contain negative
values" on every segment of a long sweep. Lowpass mirrors the spectrum to
2N-1 points, so with line drawing (4096 FFT points) it already broke above
2048 sweep points. The FFT now grows to the next power of two that holds the
data; short sweeps keep exactly the old result.
"""

from types import SimpleNamespace

import numpy as np
import pytest
from PySide6 import QtGui, QtWidgets
from scipy.signal import convolve  # type: ignore

from NanoVNASaver.Charts.TDR import TDRChart
from NanoVNASaver.RFTools import Datapoint
from NanoVNASaver.Windows.TDR import TDRWindow, fft_length

LOWPASS = ("|Z| (lowpass)", "S11 (lowpass)", "VSWR (lowpass)", "Refl (lowpass)")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def sweep(n: int, start: float = 1e6, stop: float = 900e6):
    """A cable-like S11: an open end 1 m away, a little loss."""
    rng = np.random.default_rng(11)
    f = np.linspace(start, stop, n)
    delay = 2 * 1.0 / (0.66 * 299_792_458)
    g = 0.9 * np.exp(-2j * np.pi * f * delay)
    g = g + 1e-3 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return [Datapoint(int(fi), gi.real, gi.imag) for fi, gi in zip(f, g)]


def make_window(qapp, points, draw_lines=False):
    chart = TDRChart("TDR")
    chart.flag.draw_lines = draw_lines
    # the app gives every chart its markers; the max-point dot uses the first
    chart.markers = [SimpleNamespace(color=QtGui.QColor("red"))]
    app = SimpleNamespace(
        dataUpdated=lambda *a: None,
        tdr_chart=chart,
        tdr_result_label=QtWidgets.QLabel(),
        data=SimpleNamespace(s11=points),
    )
    window = TDRWindow(app)
    chart.tdrWindow = window
    return window


def select(window, fmt):
    i = window.format_dropdown.findText(fmt)
    assert i >= 0, fmt
    window.format_dropdown.setCurrentIndex(i)


def test_fft_length():
    assert fft_length(201, 4096) == 4096
    assert fft_length(4096, 4096) == 4096
    assert fft_length(4097, 4096) == 8192
    assert fft_length(81001, 16384) == 131072
    assert fft_length(81002, 4096) == 131072


@pytest.mark.parametrize("fmt", LOWPASS + ("Refl (bandpass)",))
def test_long_sweep_40501_points(qapp, fmt):
    """The issue's case: 401 segments x 101 points."""
    window = make_window(qapp, sweep(40501))
    select(window, fmt)
    window.updateTDR()
    n = 131072 if "lowpass" in fmt else 65536
    assert window.fft_points == n
    assert len(window.td) == n
    assert len(window.distance_axis) == n
    assert np.all(np.isfinite(np.real(window.td)))
    # the chart draws it too (on Python < 3.12 this needs a PySide6 without the
    # None leak of #22, see test_qpainter_none_refcount.py)
    window.app.tdr_chart.resize(600, 400)
    window.app.tdr_chart.grab()


def test_line_drawing_limit_2049_points(qapp):
    """4096 FFT points with lines: 2049 sweep points = 4097 mirrored points."""
    window = make_window(qapp, sweep(2049), draw_lines=True)
    select(window, "|Z| (lowpass)")
    window.updateTDR()
    assert window.fft_points == 8192


def old_lowpass(windowed, fmt, s11, tdr_window, n):
    """_tdr_lowpass as it was before #11 (main 27e9119), for comparison."""
    pad = (n - len(windowed)) // 2
    w = np.fft.ifftshift(np.pad(windowed, [pad + 1, pad]))
    td = np.fft.ifft(w)
    step = np.ones(n)
    sr = convolve(td, step) + convolve(td[::-1], step)
    z = 50 * (1 + sr) / (1 - sr)
    refl = np.abs((z - 50) / (z + 50))
    if fmt == "|Z| (lowpass)":
        return td, np.abs(z)
    if fmt == "S11 (lowpass)":
        return td, 20 * np.log10(refl)
    if fmt == "VSWR (lowpass)":
        return td, np.abs((1 + refl) / (1 - refl))
    return td, np.real(
        td * n / tdr_window["corr"](len(s11), tdr_window["arg"])
    )


@pytest.mark.parametrize("draw_lines", (False, True))
@pytest.mark.parametrize("fmt", LOWPASS)
def test_101_points_unchanged(qapp, fmt, draw_lines):
    points = sweep(101)
    window = make_window(qapp, points, draw_lines)
    select(window, fmt)
    window.updateTDR()
    n = 4096 if draw_lines else 16384
    assert window.fft_points == n

    s11 = np.array([complex(d.re, d.im) for d in points])
    s11 = np.fft.fftshift(np.concatenate([s11, np.conj(s11[-1:0:-1])]))
    tdr_window = window.window_dropdown.currentData()
    if tdr_window["arg"] is None:
        windowed = tdr_window["function"](len(s11)) * s11
    else:
        windowed = tdr_window["function"](len(s11), tdr_window["arg"]) * s11
    td, z = old_lowpass(windowed, fmt, s11, tdr_window, n)
    np.testing.assert_array_equal(np.asarray(window.td), td)
    np.testing.assert_array_equal(window.step_response_Z, z)
