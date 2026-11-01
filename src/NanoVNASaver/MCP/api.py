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
"""SaverAPI: every Saver function as a plain method.

Methods run on the Qt main thread (the MCP server calls them through
MainThreadBridge). They return JSON-serialisable values, PngImage, or a
Pending for things that finish later (sweeps).

Each method is registered with @action; its docstring is what the model
reads, so it says what the action does, the units and what it returns.
Do not add GUI dialogs here: everything must work headless.
"""

import logging
import math
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from PySide6 import QtCore, QtWidgets

from .. import SweepLogger
from ..Formatting import format_frequency
from ..RFTools import Datapoint
from ..Settings.Sweep import SweepMode
from ..Touchstone import Touchstone
from .bridge import Pending
from .registry import Risk, action

if TYPE_CHECKING:
    from ..NanoVNASaver import NanoVNASaver as vna_app

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = Path.home() / "NanoVNASaver-MCP"

CAL_STANDARDS = ("short", "open", "load", "through", "isolation")

_STANDARD_TEXT = {
    "short": "SHORT-standardi porttiin 1 (S11)",
    "open": "OPEN-standardi porttiin 1 (S11)",
    "load": "LOAD-standardi (50 ohm) porttiin 1 (S11)",
    "through": "THROUGH-liitin porttien 1 ja 2 väliin",
    "isolation": "LOAD-standardit molempiin portteihin (isolaatio)",
}


@dataclass
class PngImage:
    data: bytes
    description: str = ""


class ActionError(RuntimeError):
    """Error the model can act on; the message says what to do."""


def _confirm_save(path: str, **_: Any) -> str:
    return f"Tekoäly haluaa tallentaa tiedoston: {path}. Sallitaanko?"


def _confirm_standard(standard: str, **_: Any) -> str:
    text = _STANDARD_TEXT.get(standard, standard)
    return (
        f"Kalibrointi: kytke {text}. Kuittaa kun standardi on kytketty, "
        "niin pyyhkäisy mitataan."
    )


def _round(value: float, digits: int = 4) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def _point(dp: Datapoint, parameter: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "freq_hz": dp.freq,
        "db": _round(dp.gain, 3),
        "phase_deg": _round(math.degrees(dp.phase), 2),
        "re": _round(dp.re, 5),
        "im": _round(dp.im, 5),
    }
    if parameter == "s11":
        z = dp.impedance()
        out["vswr"] = _round(dp.vswr, 3)
        out["return_loss_db"] = _round(-dp.gain, 3)
        out["r_ohm"] = _round(z.real, 2)
        out["x_ohm"] = _round(z.imag, 2)
    return out


def _ranges_below(data: list[Datapoint], limit: float) -> list[dict]:
    ranges = []
    start = None
    prev = None
    for dp in data:
        if dp.vswr <= limit:
            if start is None:
                start = dp.freq
            prev = dp.freq
        elif start is not None:
            ranges.append((start, prev))
            start = None
    if start is not None:
        ranges.append((start, prev))
    return [
        {"start_hz": a, "stop_hz": b, "bandwidth_hz": b - a}
        for a, b in ranges
    ]


def _resonances(data: list[Datapoint]) -> list[dict]:
    """Series resonances: reactance crosses zero going up."""
    out = []
    for prev, cur in zip(data, data[1:]):
        x0 = prev.impedance().imag
        x1 = cur.impedance().imag
        if x0 < 0 <= x1 and x1 - x0 < 1e4:
            frac = -x0 / (x1 - x0) if x1 != x0 else 0
            freq = round(prev.freq + frac * (cur.freq - prev.freq))
            out.append(
                {
                    "freq_hz": freq,
                    "r_ohm": _round(cur.impedance().real, 2),
                    "vswr": _round(cur.vswr, 3),
                }
            )
    return out[:10]


class SaverAPI:
    def __init__(
        self,
        app: "vna_app",
        workspace: Path = DEFAULT_WORKSPACE,
        headless: bool = False,
    ) -> None:
        self.app = app
        self.workspace = workspace
        self.headless = headless
        self.last_error = ""
        orig_show_error = app.showError

        def show_error(text: str) -> None:
            self.last_error = text
            if self.headless:
                logger.error("%s", text)
            else:
                orig_show_error(text)

        # Instance attribute shadows the method for every caller.
        app.showError = show_error  # type: ignore[method-assign]

    # --- helpers ---------------------------------------------------------

    def _path(self, path: str, suffixes: tuple[str, ...]) -> Path:
        base = self.workspace.resolve()
        base.mkdir(parents=True, exist_ok=True)
        p = Path(path)
        p = (p if p.is_absolute() else base / p).resolve()
        if not p.is_relative_to(base):
            raise ActionError(
                f"Polku {path} on työhakemiston {base} ulkopuolella. "
                "Käytä suhteellista polkua."
            )
        if suffixes and p.suffix.lower() not in suffixes:
            raise ActionError(f"Tiedostopäätteen pitää olla {suffixes}")
        return p

    def _require_idle(self) -> None:
        if self.app.worker.isRunning():
            raise ActionError(
                "Pyyhkäisy on käynnissä. Odota tai käytä sweep_stop."
            )

    def _require_device(self) -> None:
        if not self.app.vna.connected():
            raise ActionError(
                "Laitetta ei ole kytketty. Käytä device_list_ports ja "
                "device_connect."
            )

    def _data(self) -> tuple[list[Datapoint], list[Datapoint]]:
        with self.app.dataLock:
            return self.app.data.s11[:], self.app.data.s21[:]

    def _start_sweep(self) -> Future:
        """Start a sweep; the future resolves when the worker finishes."""
        self._require_device()
        self._require_idle()
        if self.app.sweep.properties.mode == SweepMode.CONTINOUS:
            raise ActionError(
                "Jatkuva pyyhkäisy on päällä, se ei pääty itsestään. "
                "Aseta sweep_configure(mode='single') ensin."
            )
        fut: Future = Future()
        signals = self.app.worker.signals

        def finished() -> None:
            disconnect()
            if not fut.done():
                fut.set_result(True)

        def failed() -> None:
            disconnect()
            if not fut.done():
                fut.set_exception(
                    ActionError(
                        self.app.worker.error_message or "Pyyhkäisy epäonnistui"
                    )
                )

        def disconnect() -> None:
            for sig, slot in ((signals.finished, finished),
                              (signals.sweep_error, failed)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        signals.finished.connect(finished)
        signals.sweep_error.connect(failed)
        self.app.sweep_start()
        return fut

    # --- device ----------------------------------------------------------

    @action(
        "device_list_ports",
        risk=Risk.READ,
        category="device",
        gui=("Serial port control: Rescan",),
    )
    def device_list_ports(self) -> dict:
        """List VNA devices found on serial ports.

        Returns {ports: [{port, type}], connected: bool}. Scanning is
        skipped while a device is connected.
        """
        sc = self.app.serial_control
        if not self.app.vna.connected():
            sc.rescanSerialPort()
        ports = []
        for i in range(sc.inp_port.count()):
            iface = sc.inp_port.itemData(i)
            if iface is not None:
                ports.append({"port": iface.port, "type": iface.comment})
        return {"ports": ports, "connected": self.app.vna.connected()}

    @action(
        "device_connect",
        risk=Risk.ACT,
        category="device",
        gui=("Serial port control: Connect to device",),
        timeout=120,
    )
    def device_connect(self, port: str | None = None) -> Any:
        """Connect to a VNA. port is e.g. "COM5" or "/dev/ttyACM0"; leave
        empty when exactly one device is found.

        Saver reads the device's current range and runs one sweep after
        connecting; this waits for it. Returns device info and a data
        summary.
        """
        if self.app.vna.connected():
            return {"note": "Laite on jo kytketty", **self.device_info()}
        sc = self.app.serial_control
        sc.rescanSerialPort()
        combo = sc.inp_port
        found = [
            (i, combo.itemData(i))
            for i in range(combo.count())
            if combo.itemData(i) is not None
        ]
        if port:
            found = [(i, f) for i, f in found if f.port == port]
        if not found:
            raise ActionError(
                f"Laitetta ei löytynyt{' portista ' + port if port else ''}."
                " Tarkista kaapeli ja käytä device_list_ports."
            )
        if len(found) > 1:
            raise ActionError(
                "Useita laitteita: "
                + ", ".join(f.port for _, f in found)
                + ". Anna port."
            )
        combo.setCurrentIndex(found[0][0])
        # connect_device starts an initial sweep; listen before it starts
        fut: Future = Future()
        signals = self.app.worker.signals

        def done() -> None:
            signals.finished.disconnect(done)
            if not fut.done():
                fut.set_result(True)

        signals.finished.connect(done)
        sc.connect_device()
        if not self.app.vna.connected():
            signals.finished.disconnect(done)
            raise ActionError(
                f"Yhteys {found[0][1].port} epäonnistui. "
                f"{self.last_error}".strip()
            )
        if not self.app.worker.isRunning():
            signals.finished.disconnect(done)
            return self.device_info()
        return Pending(
            fut, lambda _: {**self.device_info(), **self.data_summary()}
        )

    @action(
        "device_disconnect",
        risk=Risk.ACT,
        category="device",
        gui=("Serial port control: Disconnect",),
    )
    def device_disconnect(self) -> dict:
        """Disconnect the VNA and release the serial port."""
        if self.app.worker.isRunning():
            self.app.worker.quit()
            self.app.worker.wait(10_000)
        self.app.serial_control.disconnect_device()
        return {"connected": self.app.vna.connected()}

    @action(
        "device_info",
        risk=Risk.READ,
        category="device",
        gui=("Device settings: Refresh",),
    )
    def device_info(self) -> dict:
        """Connected device: name, firmware version, features, data points,
        IF bandwidth, frequency limits and serial number."""
        vna = self.app.vna
        if not vna.connected():
            return {"connected": False}
        return {
            "connected": True,
            "name": vna.name,
            "firmware": str(vna.version),
            "hardware": str(vna.hardware_revision),
            "serial_number": vna.SN,
            "features": sorted(vna.get_features()),
            "datapoints": vna.datapoints,
            "valid_datapoints": sorted(vna.valid_datapoints),
            "bandwidth_hz": vna.bandwidth,
            "max_freq_hz": vna.sweep_max_freq_hz,
            "port": str(vna.serial.port),
        }

    @action(
        "device_set_datapoints",
        risk=Risk.ACT,
        category="device",
        gui=(
            "Device settings: Datapoints",
            "Device settings: Custom points",
        ),
    )
    def device_set_datapoints(self, points: int) -> dict:
        """Set points per segment (e.g. 101, 201, 401). Total points of a
        sweep is points * segments."""
        self._require_device()
        self._require_idle()
        vna = self.app.vna
        if "Customizable data points" not in vna.get_features():
            raise ActionError("Laite ei tue pistemäärän muuttamista")
        if not vna.sweep_points_min <= points <= vna.sweep_points_max:
            raise ActionError(
                f"Pistemäärä {vna.sweep_points_min}..{vna.sweep_points_max}"
            )
        vna.datapoints = points
        self.app.sweep.set_points(points)
        self.app.sweep_control.update_step_size()
        return self.sweep_get()

    @action(
        "device_set_bandwidth",
        risk=Risk.ACT,
        category="device",
        gui=("Device settings: Bandwidth",),
    )
    def device_set_bandwidth(self, bandwidth_hz: int) -> dict:
        """Set the IF bandwidth in Hz. Lower is less noisy but slower.
        device_info lists the allowed values under features."""
        self._require_device()
        self._require_idle()
        vna = self.app.vna
        if "Bandwidth" not in vna.get_features():
            raise ActionError("Laite ei tue kaistanleveyden muuttamista")
        allowed = vna.get_bandwidths()
        if bandwidth_hz not in allowed:
            raise ActionError(f"Sallitut arvot: {allowed}")
        vna.set_bandwidth(bandwidth_hz)
        return {"bandwidth_hz": vna.bandwidth}

    @action(
        "device_set_validation",
        risk=Risk.ACT,
        category="device",
        gui=("Device settings: Validate received data",),
    )
    def device_set_validation(self, enabled: bool) -> dict:
        """Reject implausible values (|S| > 9.5) from the device and
        re-read them. Off by default."""
        self.app.vna.validateInput = enabled
        return {"validation": enabled}

    @action(
        "device_screenshot",
        risk=Risk.READ,
        category="device",
        gui=("Device settings: Screenshot",),
    )
    def device_screenshot(self) -> PngImage:
        """Screenshot of the device's own display as PNG."""
        self._require_device()
        self._require_idle()
        if "Screenshots" not in self.app.vna.get_features():
            raise ActionError("Laite ei tue kuvakaappausta")
        pixmap = self.app.vna.getScreenshot()
        return PngImage(_pixmap_png(pixmap), "device screen")

    # --- sweep -----------------------------------------------------------

    @action("sweep_get", risk=Risk.READ, category="sweep",
            gui=("Sweep control: Start/Stop/Center/Span/Segments",))
    def sweep_get(self) -> dict:
        """Current sweep settings: range in Hz, segments, points per
        segment, mode, averaging, logarithmic, name and whether a sweep
        is running."""
        s = self.app.sweep
        return {
            "start_hz": s.start,
            "stop_hz": s.end,
            "center_hz": round((s.start + s.end) / 2),
            "span_hz": s.span,
            "segments": s.segments,
            "points_per_segment": s.points,
            "total_points": s.points * s.segments,
            "step_hz": s.stepsize,
            "mode": s.properties.mode.name.lower(),
            "averages": s.properties.averages[0],
            "truncates": s.properties.averages[1],
            "logarithmic": s.properties.logarithmic,
            "name": s.properties.name,
            "running": self.app.worker.isRunning(),
            "attenuator_db": self.app.s21att,
        }

    @action(
        "sweep_configure",
        risk=Risk.ACT,
        category="sweep",
        gui=(
            "Sweep control: Start/Stop/Center/Span/Segments",
            "Sweep settings: Single/Continuous/Averaged sweep",
            "Sweep settings: Averages",
            "Sweep settings: Logarithmic sweep",
            "Sweep settings: Sweep name",
        ),
    )
    def sweep_configure(
        self,
        start_hz: int | None = None,
        stop_hz: int | None = None,
        center_hz: int | None = None,
        span_hz: int | None = None,
        segments: int | None = None,
        mode: Literal["single", "continuous", "average"] | None = None,
        averages: int | None = None,
        truncates: int | None = None,
        logarithmic: bool | None = None,
        name: str | None = None,
    ) -> dict:
        """Change sweep settings; only the given fields change.

        Give either start_hz/stop_hz or center_hz/span_hz (Hz). More
        segments give more points (points_per_segment * segments).
        mode "average" sweeps `averages` times and drops `truncates`
        outliers. Use mode "single" for sweep_run. Returns the settings.
        """
        self._require_idle()
        sc = self.app.sweep_control
        if (start_hz or stop_hz) and (center_hz or span_hz):
            raise ActionError("Anna joko start/stop tai center/span")
        new_start = start_hz if start_hz is not None else sc.get_start()
        new_stop = stop_hz if stop_hz is not None else sc.get_end()
        if center_hz is not None or span_hz is not None:
            c = center_hz if center_hz is not None else sc.get_center()
            sp = span_hz if span_hz is not None else sc.get_span()
            new_start, new_stop = round(c - sp / 2), round(c + sp / 2)
        if new_start < 1 or new_stop <= new_start:
            raise ActionError("Alkutaajuuden pitää olla > 0 ja < lopputaajuus")
        max_freq = self.app.vna.sweep_max_freq_hz
        if self.app.vna.connected() and max_freq and new_stop > max_freq:
            raise ActionError(f"Laitteen yläraja on {int(max_freq)} Hz")
        if segments is not None and not 1 <= segments <= 1000:
            raise ActionError("segments 1..1000")
        sc.set_start(new_start)
        sc.set_end(new_stop)
        if segments is not None:
            sc.set_segments(segments)
        sc.update_center_span()
        sweep = self.app.sweep
        if mode is not None:
            sweep.set_mode(
                {
                    "single": SweepMode.SINGLE,
                    "continuous": SweepMode.CONTINOUS,
                    "average": SweepMode.AVERAGE,
                }[mode]
            )
        if averages is not None or truncates is not None:
            amount = averages or sweep.properties.averages[0]
            trunc = (
                truncates
                if truncates is not None
                else sweep.properties.averages[1]
            )
            if amount < 1 or trunc < 0 or trunc >= amount:
                raise ActionError("averages >= 1 ja 0 <= truncates < averages")
            sweep.set_averages(amount, trunc)
        if logarithmic is not None:
            sweep.set_logarithmic(logarithmic)
        if name is not None:
            self.app.windows["sweep_settings"].update_title(name)
        return self.sweep_get()

    @action("band_list", risk=Risk.READ, category="sweep",
            gui=("Sweep settings: Band list",))
    def band_list(self) -> list[dict]:
        """Frequency bands defined in Saver (name, start_hz, stop_hz)."""
        return [
            {"name": name, "start_hz": int(start), "stop_hz": int(stop)}
            for name, start, stop in self.app.bands.bands
        ]

    @action("sweep_set_band", risk=Risk.ACT, category="sweep",
            gui=("Sweep settings: Set band sweep",))
    def sweep_set_band(self, band: str, padding_percent: int = 0) -> dict:
        """Set the sweep range to a band from band_list, optionally widened
        by padding_percent of the span on both sides."""
        for name, start, stop in self.app.bands.bands:
            if name.lower() == band.lower():
                start, stop = int(start), int(stop)
                pad = round((stop - start) * padding_percent / 100)
                return self.sweep_configure(
                    start_hz=max(1, start - pad), stop_hz=stop + pad
                )
        raise ActionError(f"Kaistaa {band} ei ole. Katso band_list.")

    @action("sweep_set_attenuator", risk=Risk.ACT, category="sweep",
            gui=("Sweep settings: S21 attenuator",))
    def sweep_set_attenuator(self, db: float) -> dict:
        """Attenuator in dB connected before port 2; S21 is corrected by
        this amount. 0 = none."""
        if db < 0:
            raise ActionError("Vaimennus annetaan positiivisena dB-arvona")
        self.app.s21att = db
        return {"attenuator_db": db}

    @action(
        "sweep_run",
        risk=Risk.ACT,
        category="sweep",
        gui=("Sweep control: Sweep",),
        timeout=600,
    )
    def sweep_run(self) -> Pending:
        """Run one sweep with the current settings and wait until it is
        done. Returns a summary of the result (data_summary); use
        data_points or the saver://sweep resources for the full data."""
        return Pending(self._start_sweep(), lambda _: self.data_summary())

    @action("sweep_stop", risk=Risk.ACT, category="sweep",
            gui=("Sweep control: Stop",))
    def sweep_stop(self) -> dict:
        """Stop a running sweep (also a segmented sweep)."""
        win = self.app.windows["custom_sweep"]
        if getattr(win, "sweep_active", False):
            win.sweep_active = False
            SweepLogger.stop_log()
        self.app.worker.quit()
        return {"running": self.app.worker.isRunning()}

    @action(
        "sweep_segmented",
        risk=Risk.ACT,
        category="sweep",
        gui=("Segmented Sweep: Sweep",),
        timeout=3600,
    )
    def sweep_segmented(
        self, name: str, start_hz: int, stop_hz: int, step_hz: int
    ) -> Pending:
        """Long high-resolution sweep in frames (the Segmented Sweep
        window): start_hz..stop_hz with step_hz resolution. VSWR of every
        point is logged to segmented_sweeps/<time>_<name>/points.csv.
        Returns the log folder and frame count. Can take long."""
        self._require_device()
        self._require_idle()
        if not name.strip():
            raise ActionError("Anna pyyhkäisylle nimi")
        if stop_hz <= start_hz or step_hz <= 0:
            raise ActionError("start < stop ja step > 0")
        win = self.app.windows["custom_sweep"]
        win.name_input.setText(name)
        for edit, combo, value in (
            (win.start_input, win.start_unit, start_hz),
            (win.stop_input, win.stop_unit, stop_hz),
        ):
            edit.setText(str(value))
            combo.setCurrentText("Hz")
        win.resolution_unit.setCurrentText("Hz")
        win.resolution_input.setMaximum(max(win.resolution_input.maximum(),
                                            step_hz))
        win.resolution_input.setValue(step_hz)
        win.update_frames()
        if not win.frames_data:
            raise ActionError("Kehyksiä ei syntynyt; tarkista arvot")
        frames = len(win.frames_data)
        fut: Future = Future()

        def frame_done() -> None:
            # CustomSweepWindow's own slot runs first and clears
            # sweep_active after the last frame.
            if not win.sweep_active:
                self.app.sweep_frame_finished.disconnect(frame_done)
                if not fut.done():
                    fut.set_result(True)

        self.app.sweep_frame_finished.connect(frame_done)
        win.start_sweep()
        return Pending(
            fut,
            lambda _: {
                "frames": frames,
                "frames_done": win.current_frame_index,
                "log_dir": SweepLogger._current_dir,  # noqa: SLF001
            },
        )

    # --- data ------------------------------------------------------------

    @action("data_summary", risk=Risk.READ, category="data",
            gui=("Main window: S11/S21 statistics",))
    def data_summary(self) -> dict:
        """Summary of the current sweep data: range, points, S11 minimum
        VSWR (frequency, return loss, impedance), ranges with VSWR <= 2
        and <= 1.5, series resonances (X = 0), and S21 min/max gain with
        the -3 dB band around the maximum."""
        s11, s21 = self._data()
        out: dict[str, Any] = {
            "source": self.app.sweepSource,
            "points": len(s11),
        }
        if s11:
            best = min(s11, key=lambda d: d.vswr)
            out["start_hz"] = s11[0].freq
            out["stop_hz"] = s11[-1].freq
            out["s11"] = {
                "min_vswr": _point(best, "s11"),
                "vswr_le_2": _ranges_below(s11, 2.0),
                "vswr_le_1_5": _ranges_below(s11, 1.5),
                "resonances": _resonances(s11),
            }
        if s21 and any(d.re or d.im for d in s21):
            lo = min(s21, key=lambda d: d.gain)
            hi = max(s21, key=lambda d: d.gain)
            band = [d.freq for d in s21 if d.gain >= hi.gain - 3]
            out["s21"] = {
                "min_gain": _point(lo, "s21"),
                "max_gain": _point(hi, "s21"),
                "minus_3db": {"start_hz": band[0], "stop_hz": band[-1]},
            }
        out["calibrated"] = bool(self.app.calibration.isCalculated)
        if self.app.ref_data.s11:
            out["reference"] = self.app.referenceSource
        return out

    @action("data_points", risk=Risk.READ, category="data",
            gui=("Charts: all S11/S21 chart values",))
    def data_points(
        self,
        parameter: Literal["s11", "s21"] = "s11",
        start_hz: int | None = None,
        stop_hz: int | None = None,
        max_points: int = 101,
    ) -> dict:
        """Measured points of the current sweep. Each point has freq_hz,
        db, phase_deg, re, im and for S11 also vswr, return_loss_db,
        r_ohm, x_ohm. Points are evenly decimated to max_points (<= 1001);
        narrow with start_hz/stop_hz for detail. Full data:
        saver://sweep/latest.s2p."""
        s11, s21 = self._data()
        data = s11 if parameter == "s11" else s21
        if start_hz is not None:
            data = [d for d in data if d.freq >= start_hz]
        if stop_hz is not None:
            data = [d for d in data if d.freq <= stop_hz]
        max_points = max(2, min(max_points, 1001))
        total = len(data)
        if total > max_points:
            step = (total - 1) / (max_points - 1)
            data = [data[round(i * step)] for i in range(max_points)]
        return {
            "parameter": parameter,
            "total_points": total,
            "returned": len(data),
            "points": [_point(d, parameter) for d in data],
        }

    @action("data_at", risk=Risk.READ, category="data",
            gui=("Marker: values at marker frequency",))
    def data_at(self, freq_hz: int) -> dict:
        """S11 and S21 values at the measured point nearest to freq_hz."""
        s11, s21 = self._data()
        if not s11:
            raise ActionError("Ei mittausdataa. Aja sweep_run ensin.")
        i = min(range(len(s11)), key=lambda k: abs(s11[k].freq - freq_hz))
        out = {"s11": _point(s11[i], "s11")}
        if i < len(s21):
            out["s21"] = _point(s21[i], "s21")
        return out

    @action("reference_set", risk=Risk.ACT, category="data",
            gui=("Reference sweep: Set current as reference",))
    def reference_set(self) -> dict:
        """Store the current sweep as the reference trace (shown in the
        charts for comparison)."""
        if not self.app.data.s11:
            raise ActionError("Ei mittausdataa")
        self.app.setReference()
        return {"reference": self.app.referenceSource}

    @action("reference_reset", risk=Risk.ACT, category="data",
            gui=("Reference sweep: Reset reference",))
    def reference_reset(self) -> dict:
        """Remove the reference trace."""
        self.app.resetReference()
        return {"reference": None}

    # --- markers ---------------------------------------------------------

    @action("marker_list", risk=Risk.READ, category="markers",
            gui=("Markers: marker values",))
    def marker_list(self) -> list[dict]:
        """Markers with their frequency and the S11/S21 values there."""
        s11, s21 = self._data()
        out = []
        for i, m in enumerate(self.app.markers):
            item: dict[str, Any] = {
                "index": i,
                "name": m.name,
                "freq_hz": getattr(m, "freq", None),
            }
            if 0 <= m.location < len(s11):
                # The data point the marker sits on; Marker.freq can lag
                # behind it (it reported the initial position after
                # marker_set).
                item["freq_hz"] = s11[m.location].freq
                item["s11"] = _point(s11[m.location], "s11")
                if m.location < len(s21):
                    item["s21"] = _point(s21[m.location], "s21")
            out.append(item)
        return out

    @action("marker_set", risk=Risk.ACT, category="markers",
            gui=("Markers: frequency input",))
    def marker_set(self, index: int, freq_hz: int) -> list[dict]:
        """Move marker number `index` (0-based, see marker_list) to
        freq_hz."""
        if not 0 <= index < len(self.app.markers):
            raise ActionError(f"Markkereita on {len(self.app.markers)}")
        self.app.markers[index].setFrequency(str(freq_hz))
        return self.marker_list()

    @action("marker_add", risk=Risk.ACT, category="markers",
            gui=("Display settings: Add marker",))
    def marker_add(self) -> list[dict]:
        """Add a marker (placed in the middle of the data until moved
        with marker_set). Returns all markers."""
        self.app.windows["setup"].addMarker()
        return self.marker_list()

    @action("marker_remove", risk=Risk.ACT, category="markers",
            gui=("Display settings: Remove marker",))
    def marker_remove(self) -> list[dict]:
        """Remove the last marker (at least one always remains)."""
        self.app.windows["setup"].removeMarker()
        return self.marker_list()

    # --- analysis --------------------------------------------------------

    @action("analysis_list", risk=Risk.READ, category="analysis",
            gui=("Analysis: Analysis type",))
    def analysis_list(self) -> list[str]:
        """Names of the analyses available for analysis_run."""
        combo = self.app.windows["analysis"].analysis_list
        return [combo.itemText(i) for i in range(combo.count())]

    @action("analysis_run", risk=Risk.READ, category="analysis",
            gui=("Analysis: Run analysis",))
    def analysis_run(self, analysis: str) -> dict:
        """Run a Saver analysis (filter -3 dB points, peak search, VSWR,
        resonance, EFHW, MagLoop) on the current data. Returns the result
        lines as text."""
        win = self.app.windows["analysis"]
        combo = win.analysis_list
        idx = next(
            (
                i
                for i in range(combo.count())
                if combo.itemText(i).lower() == analysis.lower()
            ),
            -1,
        )
        if idx < 0:
            raise ActionError(
                f"Tuntematon analyysi. Vaihtoehdot: {self.analysis_list()}"
            )
        if not self.app.data.s11:
            raise ActionError("Ei mittausdataa. Aja sweep_run ensin.")
        combo.setCurrentIndex(idx)
        win.runAnalysis()
        labels = win.analysis.widget().findChildren(QtWidgets.QLabel)
        lines = [lbl.text() for lbl in labels if lbl.text().strip()]
        return {"analysis": combo.itemText(idx), "result": lines}

    @action("tdr_result", risk=Risk.READ, category="analysis",
            gui=("Main window: Time Domain Reflectometry",))
    def tdr_result(self) -> dict:
        """Time-domain reflectometry of S11: estimated cable length to the
        strongest reflection. Set the velocity factor in the TDR window."""
        win = self.app.windows["tdr"]
        win.updateTDR()
        return {
            "cable_length": win.tdr_result_label.text(),
            "velocity_factor": win.tdr_velocity_input.text(),
        }

    # --- charts ----------------------------------------------------------

    def _charts(self) -> dict[str, Any]:
        charts = {
            f"{group}.{key}": chart
            for group, items in self.app.charts.items()
            for key, chart in items.items()
        }
        charts["tdr"] = self.app.tdr_chart
        return charts

    @action("chart_list", risk=Risk.READ, category="charts",
            gui=("Display setup: chart selection",))
    def chart_list(self) -> list[dict]:
        """Charts that chart_image can render (id and title)."""
        return [
            {"id": key, "title": chart.name}
            for key, chart in self._charts().items()
        ]

    @action("chart_image", risk=Risk.READ, category="charts",
            gui=("Charts: view",))
    def chart_image(
        self, chart: str = "s11.smith", width: int = 800, height: int = 600
    ) -> PngImage:
        """Render a chart of the current data as PNG, e.g. "s11.smith",
        "s11.vswr", "s21.log_mag", "tdr". See chart_list."""
        charts = self._charts()
        if chart not in charts:
            raise ActionError(f"Tuntematon kaavio. Vaihtoehdot: {list(charts)}")
        widget = charts[chart]
        width = max(200, min(width, 2000))
        height = max(200, min(height, 2000))
        if widget.isVisible():
            pixmap = widget.grab()
        else:
            widget.resize(width, height)
            pixmap = widget.grab()
        return PngImage(_pixmap_png(pixmap), widget.name)

    # --- files -----------------------------------------------------------

    @action("file_list", risk=Risk.READ, category="files",
            gui=("Files: file dialogs",))
    def file_list(self) -> dict:
        """Files in the MCP work folder (Touchstone and calibration files
        can only be read from and written to this folder)."""
        base = self.workspace.resolve()
        base.mkdir(parents=True, exist_ok=True)
        files = sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and p.suffix.lower() in {".s1p", ".s2p", ".cal"}
        )
        return {"workspace": str(base), "files": files}

    @action("file_load_sweep", risk=Risk.ACT, category="files",
            gui=("Files: Load as sweep",))
    def file_load_sweep(self, path: str) -> dict:
        """Load a Touchstone file (.s1p/.s2p) from the work folder as the
        current sweep. Returns data_summary."""
        p = self._path(path, (".s1p", ".s2p"))
        self.app.windows["file"].load_sweep(str(p))
        return self.data_summary()

    @action("file_load_reference", risk=Risk.ACT, category="files",
            gui=("Files: Load reference",))
    def file_load_reference(self, path: str) -> dict:
        """Load a Touchstone file from the work folder as the reference
        trace."""
        p = self._path(path, (".s1p", ".s2p"))
        self.app.windows["file"].load_reference(str(p))
        return {"reference": self.app.referenceSource}

    @action(
        "file_export_touchstone",
        risk=Risk.PERSIST,
        category="files",
        gui=(
            "Files: Save 1-Port file (S1P)",
            "Files: Save 2-Port file (S2P)",
        ),
        confirm=_confirm_save,
    )
    def file_export_touchstone(self, path: str) -> dict:
        """Save the current sweep in the work folder. The suffix decides
        the format: .s1p (S11) or .s2p (S11 + S21). Needs the user's
        confirmation."""
        p = self._path(path, (".s1p", ".s2p"))
        if not self.app.data.s11:
            raise ActionError("Ei tallennettavaa dataa")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.app.windows["file"].export_touchstone(
            str(p), 4 if p.suffix.lower() == ".s2p" else 1
        )
        return {"saved": str(p)}

    # --- calibration -----------------------------------------------------

    @action("calibration_status", risk=Risk.READ, category="calibration",
            gui=("Calibration: status",))
    def calibration_status(self) -> dict:
        """Calibration state: points captured per standard, whether
        1-port (SOL) and 2-port data is complete, whether it is applied,
        source and frequency range."""
        cal = self.app.calibration
        ds = cal.dataset
        out = {
            "applied": bool(cal.isCalculated),
            "source": cal.source,
            "standards": {
                name: cal.data_size(name) for name in CAL_STANDARDS
            },
            "complete_1port": ds.complete1port(),
            "complete_2port": ds.complete2port(),
            "offset_delay_ps": round(self.app.worker.offsetDelay * 1e12, 3),
        }
        if ds.data:
            out["range_hz"] = [ds.freq_min(), ds.freq_max()]
            out["range_text"] = (
                f"{format_frequency(ds.freq_min())} - "
                f"{format_frequency(ds.freq_max())}"
            )
        return out

    @action(
        "calibration_capture",
        risk=Risk.ACT,
        category="calibration",
        gui=(
            "Calibration: Short",
            "Calibration: Open",
            "Calibration: Load",
            "Calibration: Through",
            "Calibration: Isolation",
            "Calibration: Calibration assistant",
        ),
        confirm=_confirm_standard,
        timeout=600,
    )
    def calibration_capture(
        self,
        standard: Literal["short", "open", "load", "through", "isolation"],
    ) -> Pending:
        """Measure one calibration standard. The user is asked to connect
        it first and must confirm. Order: short, open, load (1-port);
        add through and isolation for 2-port. Then calibration_apply.
        The sweep range should already be the range to calibrate."""
        if self.app.calibration.isCalculated:
            raise ActionError(
                "Kalibrointi on jo käytössä, joten mittaus olisi korjattua "
                "dataa. Aja calibration_reset ennen uutta kalibrointia."
            )
        win = self.app.windows["calibration"]

        def store(_: Any) -> dict:
            win.cal_save(standard)
            self.app.calibration.source = "MCP"
            return self.calibration_status()

        return Pending(self._start_sweep(), store)

    @action("calibration_apply", risk=Risk.ACT, category="calibration",
            gui=("Calibration: Apply",))
    def calibration_apply(self) -> dict:
        """Calculate and apply the calibration from the captured standards
        (ideal standards). Needs short, open and load."""
        self._require_idle()
        self.last_error = ""
        self.app.windows["calibration"].calculate()
        if not self.app.calibration.isCalculated:
            raise ActionError(
                self.last_error or "Kalibroinnin laskenta epäonnistui"
            )
        return self.calibration_status()

    @action("calibration_reset", risk=Risk.ACT, category="calibration",
            gui=("Calibration: Reset",))
    def calibration_reset(self) -> dict:
        """Remove the application calibration; data is shown as measured
        (device calibration only)."""
        self._require_idle()
        self.app.windows["calibration"].reset()
        return self.calibration_status()

    @action("calibration_set_offset_delay", risk=Risk.ACT,
            category="calibration", gui=("Calibration: Offset delay",))
    def calibration_set_offset_delay(self, delay_ps: float) -> dict:
        """Electrical delay (ps) removed from the data, e.g. for an
        adapter after the calibration plane."""
        win = self.app.windows["calibration"]
        win.setOffsetDelay(delay_ps)
        return self.calibration_status()

    @action("calibration_load", risk=Risk.ACT, category="calibration",
            gui=("Calibration: Load calibration",))
    def calibration_load(self, path: str) -> dict:
        """Load and apply a .cal file from the work folder."""
        self._require_idle()
        p = self._path(path, (".cal",))
        if not p.exists():
            raise ActionError(f"Tiedostoa {path} ei ole")
        self.app.windows["calibration"].loadCalibration(str(p))
        return self.calibration_status()

    @action(
        "calibration_save",
        risk=Risk.PERSIST,
        category="calibration",
        gui=("Calibration: Save calibration",),
        confirm=_confirm_save,
    )
    def calibration_save(self, path: str) -> dict:
        """Save the applied calibration as a .cal file in the work folder.
        Needs the user's confirmation."""
        p = self._path(path, (".cal",))
        if not self.app.calibration.isCalculated:
            raise ActionError("Kalibrointia ei ole otettu käyttöön")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.last_error = ""
        self.app.windows["calibration"].saveCalibration(str(p))
        if self.last_error:
            raise ActionError(self.last_error)
        return {"saved": str(p)}

    # --- raw data for resources --------------------------------------------

    def touchstone_text(self, ports: int) -> str:
        s11, s21 = self._data()
        ts = Touchstone()
        ts.sdata[0] = s11
        if ports == 2:
            ts.sdata[1] = s21
            ts.sdata[2] = [Datapoint(d.freq, 0, 0) for d in s11]
            ts.sdata[3] = [Datapoint(d.freq, 0, 0) for d in s11]
        return ts.saves(4 if ports == 2 else 1)


def _pixmap_png(pixmap: Any) -> bytes:
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())

