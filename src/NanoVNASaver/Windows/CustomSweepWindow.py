"""Segmented Sweep window: a long sweep at a fixed step, measured in frames.

Every frame is one Saver sweep with segments=1 and its own point count, so
the measured spacing is the requested step and frames do not overlap
(SegmentedSweep.plan_frames). The whole range is collected (S11 and S21,
calibrated as Saver applies it), written as Touchstone next to points.csv
and shown in the main window at the end (litevna-openvna#204)."""

import logging

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import SweepLogger
from ..RFTools import Datapoint
from ..SegmentedSweep import Plan, plan_frames
from ..Settings.Sweep import SweepMode

logger = logging.getLogger(__name__)

STATUS_DONE = "valmis"
STATUS_STOPPED = "pysäytetty"
STATUS_ERROR = "virhe"


class CustomSweepWindow(QDialog):
    # largest frame; the LiteVNA64 firmware measures at most 401 points
    MAX_FRAME_POINTS = 401
    PREVIEW_FRAMES = 50

    # emitted once when a segmented sweep ends, whatever the reason
    segmented_finished = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.sweep_active = False
        self.frames_data: list[tuple[int, int]] = []
        self.plan: Plan | None = None
        self.plan_error = ""
        self.current_frame_index = 0
        self.last_result: dict = {}
        self._stop_requested = False
        self._saved: dict = {}
        self._s11: list[Datapoint] = []
        self._s21: list[Datapoint] = []
        self._settings: dict = {}
        self._max_freq_error = 0

        self.setWindowTitle("Segmented Sweep")
        self.setMinimumWidth(420)

        main_layout = QVBoxLayout()

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Sweep Name:"))
        self.name_input = QLineEdit("")
        name_layout.addWidget(self.name_input)
        main_layout.addLayout(name_layout)

        start_layout = QHBoxLayout()
        start_layout.addWidget(QLabel("Start Frequency:"))
        self.start_input = QLineEdit("0")
        self.start_input.setValidator(QIntValidator(0, 2_000_000_000))
        start_layout.addWidget(self.start_input)
        self.start_unit = QComboBox()
        self.start_unit.addItems(["GHz", "MHz", "kHz", "Hz"])
        start_layout.addWidget(self.start_unit)
        main_layout.addLayout(start_layout)

        stop_layout = QHBoxLayout()
        stop_layout.addWidget(QLabel("Stop Frequency:"))
        self.stop_input = QLineEdit("6")
        self.stop_input.setValidator(QIntValidator(0, 2_000_000_000))
        stop_layout.addWidget(self.stop_input)
        self.stop_unit = QComboBox()
        self.stop_unit.addItems(["GHz", "MHz", "kHz", "Hz"])
        stop_layout.addWidget(self.stop_unit)
        main_layout.addLayout(stop_layout)

        resolution_layout = QHBoxLayout()
        resolution_layout.addWidget(QLabel("Resolution:"))
        self.resolution_input = QSpinBox()
        self.resolution_input.setRange(1, 1_000_000)
        self.resolution_input.setValue(1)
        resolution_layout.addWidget(self.resolution_input)
        self.resolution_unit = QComboBox()
        self.resolution_unit.addItems(["GHz", "MHz", "kHz", "Hz"])
        self.resolution_unit.setCurrentIndex(1)
        resolution_layout.addWidget(self.resolution_unit)
        main_layout.addLayout(resolution_layout)

        self.frames_summary = QLabel("Frames: -")
        self.frames_summary.setWordWrap(True)
        main_layout.addWidget(self.frames_summary)

        self.frames_container = QWidget()
        self.frames_layout = QVBoxLayout(self.frames_container)
        self.frames_label = QLabel("")
        self.frames_label.setWordWrap(True)
        self.frames_layout.addWidget(self.frames_label)
        self.frames_scroll = QScrollArea()
        self.frames_scroll.setWidgetResizable(True)
        self.frames_scroll.setWidget(self.frames_container)
        self.frames_scroll.setFixedHeight(100)
        main_layout.addWidget(self.frames_scroll)

        self.progress_label = QLabel("Progress: idle")
        self.progress_label.setWordWrap(True)
        main_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        button_layout = QHBoxLayout()
        self.sweep_button = QPushButton("Sweep")
        self.sweep_button.clicked.connect(self.start_sweep)
        button_layout.addWidget(self.sweep_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)
        button_layout.addWidget(self.stop_button)
        main_layout.addLayout(button_layout)

        self.setLayout(main_layout)

        self.start_input.textChanged.connect(self.update_frames)
        self.stop_input.textChanged.connect(self.update_frames)
        self.resolution_input.valueChanged.connect(self.update_frames)
        self.start_unit.currentIndexChanged.connect(self.update_frames)
        self.stop_unit.currentIndexChanged.connect(self.update_frames)
        self.resolution_unit.currentIndexChanged.connect(self.update_frames)

        if self.parent():
            self.parent().sweep_frame_finished.connect(self.on_frame_finished)
            # the main window's Stop stops the whole segmented sweep, not
            # only the frame that is running
            self.parent().sweep_control.btn_stop.clicked.connect(
                self._main_stop_clicked
            )

        self.update_frames()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def unit_multiplier(unit):
        return {
            "GHz": 1_000_000_000,
            "MHz": 1_000_000,
            "kHz": 1_000,
            "Hz": 1,
        }.get(unit, 1)

    @staticmethod
    def format_freq(hz):
        if hz >= 1e9:
            return f"{hz / 1e9:.6f} GHz"
        if hz >= 1e6:
            return f"{hz / 1e6:.6f} MHz"
        if hz >= 1e3:
            return f"{hz / 1e3:.3f} kHz"
        return f"{hz} Hz"

    def _inputs_hz(self) -> tuple[int, int, int] | None:
        try:
            start = float(self.start_input.text())
            stop = float(self.stop_input.text())
        except ValueError:
            return None
        return (
            int(round(start * self.unit_multiplier(self.start_unit.currentText()))),
            int(round(stop * self.unit_multiplier(self.stop_unit.currentText()))),
            int(self.resolution_input.value()
                * self.unit_multiplier(self.resolution_unit.currentText())),
        )

    def _point_limits(self) -> tuple[int, int]:
        app = self.parent()
        vna = getattr(app, "vna", None)
        max_points = getattr(vna, "sweep_points_max", self.MAX_FRAME_POINTS)
        min_points = getattr(vna, "sweep_points_min", 11)
        return min(max_points, self.MAX_FRAME_POINTS), min_points

    # --- frame plan ------------------------------------------------------

    def update_frames(self):
        if self.sweep_active:
            return
        self.plan = None
        self.frames_data = []
        values = self._inputs_hz()
        if values is None:
            return
        max_points, min_points = self._point_limits()
        try:
            plan = plan_frames(*values, max_points=max_points,
                               min_points=min_points)
        except ValueError as exc:
            self.plan_error = str(exc)
            self.frames_summary.setText(f"Frames: - ({exc})")
            self.frames_label.setText("")
            return
        self.plan = plan
        self.plan_error = ""
        self.frames_data = [(f.start_hz, f.stop_hz) for f in plan.frames]
        lines = [
            f"Frame {f.index + 1}: {self.format_freq(f.start_hz)} → "
            f"{self.format_freq(f.stop_hz)} ({f.points} points)"
            for f in plan.frames[: self.PREVIEW_FRAMES]
        ]
        if len(plan.frames) > self.PREVIEW_FRAMES:
            lines.append(f"… {len(plan.frames) - self.PREVIEW_FRAMES} more")
        self.frames_summary.setText(
            f"Frames: {len(plan.frames)}, points: {plan.points}, "
            f"step: {self.format_freq(plan.step_hz)}, "
            f"last point: {self.format_freq(plan.stop_hz)}"
        )
        self.frames_label.setText("\n".join(lines))

    # --- start -----------------------------------------------------------

    def start_sweep(self):
        """Sweep button: like begin(), errors shown in a dialog."""
        error = self.begin(self.name_input.text())
        if error:
            QMessageBox.warning(self, "Segmented Sweep", error)

    def begin(self, name: str) -> str | None:
        """Start the segmented sweep; returns an error text or None."""
        app = self.parent()
        name = (name or "").strip()
        if not name:
            return "Anna pyyhkäisylle nimi ennen aloitusta."
        if app is None:
            return "Pääikkunaa ei ole."
        if self.sweep_active or app.worker.isRunning():
            return "Pyyhkäisy on jo käynnissä."
        if not app.vna.connected():
            return "Laitetta ei ole yhdistetty."
        if "Customizable data points" not in app.vna.get_features():
            return ("Laite ei tue pistemäärän muuttamista, joten kehyksiä ei "
                    "voi mitata pyydetyllä askeleella.")
        if app.sweep.properties.mode == SweepMode.CONTINOUS:
            return ("Jatkuva pyyhkäisy on päällä, eikä kehys silloin pääty. "
                    "Valitse Single tai Averaged.")
        self.update_frames()
        if self.plan is None:
            return self.plan_error or "Kehyksiä ei syntynyt; tarkista arvot."
        plan = self.plan

        self._saved = {
            "start": app.sweep_control.get_start(),
            "end": app.sweep_control.get_end(),
            "segments": app.sweep_control.get_segments(),
            "datapoints": app.vna.datapoints,
            "logarithmic": app.sweep.properties.logarithmic,
        }
        max_points, _ = self._point_limits()
        self._settings = {
            "sweep_name": name,
            "start_hz": plan.start_hz,
            "stop_hz_requested": self._inputs_hz()[1],
            "stop_hz": plan.stop_hz,
            "step_hz": plan.step_hz,
            "points": plan.points,
            "frames": len(plan.frames),
            "max_points_per_frame": max_points,
            "mode": app.sweep.properties.mode.name.lower(),
            "averages": app.sweep.properties.averages[0],
            "calibration": self._calibration_note(plan),
        }
        try:
            SweepLogger.start_log(name, self._settings)
        except OSError as exc:
            return f"Lokikansiota ei voitu luoda: {exc}"

        self.sweep_active = True
        self._stop_requested = False
        self.current_frame_index = 0
        self._s11, self._s21 = [], []
        self._max_freq_error = 0
        self.last_result = {}
        app.sweep.set_logarithmic(False)

        self.progress_bar.setMaximum(len(plan.frames))
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"Frame 0 / {len(plan.frames)}")
        self.sweep_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        logger.info("Segmented sweep %s: %d points in %d frames", name,
                    plan.points, len(plan.frames))
        self.run_next_frame()
        return None

    def _calibration_note(self, plan: Plan) -> str:
        app = self.parent()
        cal = getattr(app, "calibration", None)
        dataset = getattr(cal, "dataset", None)
        if dataset is None or not dataset.data:
            return "ei kalibrointia"
        lo, hi = dataset.freq_min(), dataset.freq_max()
        if plan.start_hz < lo or plan.stop_hz > hi:
            note = (f"VAROITUS: alue ylittää kalibroinnin {lo}..{hi} Hz; "
                    "ulkopuoliset pisteet korjataan reunan termeillä")
            logger.warning("Segmented sweep: %s", note)
            return note
        return f"kalibrointi {lo}..{hi} Hz (Saverin interpolointi)"

    # --- frames ----------------------------------------------------------

    def run_next_frame(self):
        if not self.sweep_active:
            return
        app = self.parent()
        if self._stop_requested:
            self._finish(STATUS_STOPPED)
            return
        if not app.vna.connected():
            self._finish(STATUS_ERROR, "Laitteen yhteys katkesi.")
            return
        frame = self.plan.frames[self.current_frame_index]
        # the GUI fields only show the frame (their text is rounded); the
        # sweep itself is set exactly
        app.vna.datapoints = frame.points
        app.sweep_control.set_segments(1)
        app.sweep_control.set_start(frame.start_hz)
        app.sweep_control.set_end(frame.stop_hz)
        app.sweep.update(start=frame.start_hz, end=frame.stop_hz,
                         segments=1, points=frame.points)
        app.worker.error_message = ""
        app.sweep_start()

    def _main_stop_clicked(self):
        if self.sweep_active:
            self._stop_requested = True

    def request_stop(self):
        """Stop the segmented sweep: the running frame is interrupted and
        its partial data dropped; frames done so far are kept."""
        if not self.sweep_active:
            return
        self._stop_requested = True
        app = self.parent()
        if app is not None:
            app.worker.quit()

    def on_frame_finished(self):
        if not self.sweep_active:
            return
        app = self.parent()
        if app.worker.error_message:
            self._finish(STATUS_ERROR, app.worker.error_message.strip())
            return
        if self._stop_requested or getattr(app.worker, "_terminate", False):
            self._finish(STATUS_STOPPED)
            return

        frame = self.plan.frames[self.current_frame_index]
        with app.dataLock:
            s11 = app.data.s11[:]
            s21 = app.data.s21[:]
        if len(s11) != frame.points:
            self._finish(
                STATUS_ERROR,
                f"Kehys {frame.index + 1}: laite palautti {len(s11)} "
                f"pistettä, odotettiin {frame.points}.",
            )
            return
        for dp, want in zip(s11, frame.frequencies(), strict=True):
            self._max_freq_error = max(self._max_freq_error,
                                       abs(dp.freq - want))
        self._s11.extend(s11)
        if len(s21) == len(s11):
            self._s21.extend(s21)
        try:
            SweepLogger.log_points([d.freq for d in s11],
                                   [d.vswr for d in s11])
        except OSError as exc:
            self._finish(STATUS_ERROR, f"points.csv: {exc}")
            return

        self.current_frame_index += 1
        total = len(self.plan.frames)
        self.progress_bar.setValue(self.current_frame_index)
        self.progress_label.setText(f"Frame {self.current_frame_index} / {total}")
        if self.current_frame_index >= total:
            self._finish(STATUS_DONE)
            return
        QTimer.singleShot(0, self.run_next_frame)

    # --- end -------------------------------------------------------------

    def _finish(self, status: str, error: str = "") -> None:
        app = self.parent()
        self.sweep_active = False
        self._stop_requested = False
        SweepLogger.stop_log()
        if len(self._s21) != len(self._s11):
            self._s21 = []

        files: list[str] = []
        try:
            files = SweepLogger.write_touchstone(self._s11, self._s21)
        except OSError as exc:
            error = f"{error}; " if error else ""
            error += f"Touchstonea ei voitu kirjoittaa: {exc}"
            status = STATUS_ERROR

        if self._s11:
            s21 = self._s21 or [Datapoint(d.freq, 0, 0) for d in self._s11]
            app.saveData(self._s11, s21,
                         source=f"{self._settings.get('sweep_name')} "
                                f"(segmented, {len(self._s11)} points)")
            try:
                app.dataUpdated()
            except Exception:  # pylint: disable=broad-except
                # a chart that fails on a long sweep must not lose the data
                logger.exception("Updating views after segmented sweep")

        self._restore()

        result = {
            "status": status,
            "error": error,
            "frames": len(self.plan.frames) if self.plan else 0,
            "frames_done": self.current_frame_index,
            "points": self.plan.points if self.plan else 0,
            "points_measured": len(self._s11),
            "step_hz": self.plan.step_hz if self.plan else 0,
            "start_hz": self.plan.start_hz if self.plan else 0,
            "stop_hz": self.plan.stop_hz if self.plan else 0,
            "max_freq_error_hz": self._max_freq_error,
            "calibration": self._settings.get("calibration", ""),
            "log_dir": SweepLogger.current_dir(),
            "files": files,
        }
        try:
            SweepLogger.write_settings({
                **self._settings,
                "status": status,
                "error": error.replace("\n", " ").replace(",", ";"),
                "frames_done": result["frames_done"],
                "points_measured": result["points_measured"],
                "max_freq_error_hz": self._max_freq_error,
            })
        except OSError as exc:
            logger.error("settings.csv: %s", exc)
        self.last_result = result

        self.sweep_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        text = {
            STATUS_DONE: "Sweep complete",
            STATUS_STOPPED: "Sweep stopped",
            STATUS_ERROR: "Sweep failed",
        }[status]
        self.progress_label.setText(
            f"{text}: {result['points_measured']} / {result['points']} points"
            + (f" — {error}" if error else "")
        )
        logger.info("Segmented sweep ended: %s %s", status, error)
        self.segmented_finished.emit(result)

    def _restore(self) -> None:
        app = self.parent()
        saved = self._saved
        if not saved:
            return
        app.vna.datapoints = saved["datapoints"]
        app.sweep.set_logarithmic(saved["logarithmic"])
        app.sweep_control.set_segments(saved["segments"])
        app.sweep_control.set_start(saved["start"])
        app.sweep_control.set_end(saved["end"])
        app.sweep_control.update_step_size()
        self._saved = {}
