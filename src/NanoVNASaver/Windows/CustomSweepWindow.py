from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton,
    QComboBox, QScrollArea, QWidget, QProgressBar,
    QMessageBox
)

from PySide6.QtGui import QIntValidator
from PySide6.QtCore import QTimer

from ..SweepLogger import start_log, log_points, stop_log

import math


class CustomSweepWindow(QDialog):

    CHUNK_SIZE = 200

    def __init__(self, parent=None):
        super().__init__(parent)

        self.sweep_active = False
        self.frames_data = []
        self.current_frame_index = 0
        self.last_logged_freq = None

        self.setWindowTitle("Segmented Sweep")
        self.setMinimumWidth(420)

        main_layout = QVBoxLayout()

        # -------------------------
        # Sweep Name
        # -------------------------

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Sweep Name:"))

        self.name_input = QLineEdit("")
        name_layout.addWidget(self.name_input)

        main_layout.addLayout(name_layout)

        # -------------------------
        # Start Frequency
        # -------------------------

        start_layout = QHBoxLayout()
        start_layout.addWidget(QLabel("Start Frequency:"))

        self.start_input = QLineEdit("0")
        self.start_input.setValidator(QIntValidator(0, 10_000_000))
        start_layout.addWidget(self.start_input)

        self.start_unit = QComboBox()
        self.start_unit.addItems(["GHz", "MHz", "kHz", "Hz"])
        start_layout.addWidget(self.start_unit)

        main_layout.addLayout(start_layout)

        # -------------------------
        # Stop Frequency
        # -------------------------

        stop_layout = QHBoxLayout()
        stop_layout.addWidget(QLabel("Stop Frequency:"))

        self.stop_input = QLineEdit("6")
        self.stop_input.setValidator(QIntValidator(0, 10_000_000))
        stop_layout.addWidget(self.stop_input)

        self.stop_unit = QComboBox()
        self.stop_unit.addItems(["GHz", "MHz", "kHz", "Hz"])
        stop_layout.addWidget(self.stop_unit)

        main_layout.addLayout(stop_layout)

        # -------------------------
        # Resolution
        # -------------------------

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

        # -------------------------
        # Frame summary
        # -------------------------

        self.frames_summary = QLabel("Frames: -")
        main_layout.addWidget(self.frames_summary)

        # -------------------------
        # Frame preview list
        # -------------------------

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

        # -------------------------
        # Progress display
        # -------------------------

        self.progress_label = QLabel("Progress: idle")
        main_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        # -------------------------
        # Sweep button
        # -------------------------

        self.sweep_button = QPushButton("Sweep")
        self.sweep_button.clicked.connect(self.start_sweep)

        main_layout.addWidget(self.sweep_button)

        self.setLayout(main_layout)

        # -------------------------
        # Signals
        # -------------------------

        self.start_input.textChanged.connect(self.update_frames)
        self.stop_input.textChanged.connect(self.update_frames)
        self.resolution_input.valueChanged.connect(self.update_frames)

        self.start_unit.currentIndexChanged.connect(self.update_frames)
        self.stop_unit.currentIndexChanged.connect(self.update_frames)
        self.resolution_unit.currentIndexChanged.connect(self.update_frames)

        if self.parent():
            self.parent().sweep_frame_finished.connect(self.on_frame_finished)

        self.update_frames()

    # --------------------------------------------------
    # Unit conversion
    # --------------------------------------------------

    def unit_multiplier(self, unit):

        return {
            "GHz": 1_000_000_000,
            "MHz": 1_000_000,
            "kHz": 1_000,
            "Hz": 1
        }.get(unit, 1)

    # --------------------------------------------------
    # Frequency formatting
    # --------------------------------------------------

    def format_freq(self, hz):

        if hz >= 1e9:
            return f"{hz/1e9:.3f} GHz"

        if hz >= 1e6:
            return f"{hz/1e6:.3f} MHz"

        if hz >= 1e3:
            return f"{hz/1e3:.3f} kHz"

        return f"{hz} Hz"

    # --------------------------------------------------
    # Frame calculation
    # --------------------------------------------------

    def update_frames(self):

        try:
            start = float(self.start_input.text())
            stop = float(self.stop_input.text())
        except ValueError:
            return

        start_hz = int(start * self.unit_multiplier(self.start_unit.currentText()))
        stop_hz = int(stop * self.unit_multiplier(self.stop_unit.currentText()))

        step_hz = int(
            self.resolution_input.value() *
            self.unit_multiplier(self.resolution_unit.currentText())
        )

        if stop_hz <= start_hz or step_hz <= 0:
            return

        span = stop_hz - start_hz
        steps = span // step_hz

        frames = math.ceil(steps / self.CHUNK_SIZE)

        self.frames_data = []

        lines = []

        for i in range(frames):

            start_step = i * self.CHUNK_SIZE
            end_step = min((i + 1) * self.CHUNK_SIZE, steps)

            frame_start = start_hz + start_step * step_hz
            frame_end = start_hz + end_step * step_hz

            self.frames_data.append((frame_start, frame_end))

            lines.append(
                f"Frame {i+1}: {self.format_freq(frame_start)} → {self.format_freq(frame_end)}"
            )

        self.frames_summary.setText(f"Frames: {frames}")
        self.frames_label.setText("\n".join(lines))

    # --------------------------------------------------
    # Start sweep
    # --------------------------------------------------

    def start_sweep(self):

        name = self.name_input.text().strip()

        if not name:
            QMessageBox.warning(
                self,
                "Sweep Name Required",
                "Please enter a name for the sweep before starting."
            )
            return

        if not self.frames_data:
            return

        self.sweep_active = True
        self.current_frame_index = 0
        self.last_logged_freq = None

        total = len(self.frames_data)

        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"Frame 0 / {total}")

        settings = {
            "sweep_name": name,
            "start_frequency": f"{self.start_input.text()} {self.start_unit.currentText()}",
            "stop_frequency": f"{self.stop_input.text()} {self.stop_unit.currentText()}",
            "resolution": f"{self.resolution_input.value()} {self.resolution_unit.currentText()}",
            "frames": len(self.frames_data),
            "points_per_frame": self.CHUNK_SIZE
        }

        start_log(name, settings)

        self.run_next_frame()

    # --------------------------------------------------
    # Run frame
    # --------------------------------------------------

    def run_next_frame(self):

        if self.current_frame_index >= len(self.frames_data):
            return

        app = self.parent()

        if not app:
            return

        start, stop = self.frames_data[self.current_frame_index]

        app.sweep_control.set_start(start)
        app.sweep_control.set_end(stop)

        app.sweep_start()

    # --------------------------------------------------
    # Frame finished
    # --------------------------------------------------

    def on_frame_finished(self):

        if not self.sweep_active:
            return

        app = self.parent()

        freqs = []
        vswrs = []

        try:

            with app.dataLock:
                data = app.data.s11[:]

            for d in data:

                if self.last_logged_freq == d.freq:
                    continue

                freqs.append(d.freq)
                vswrs.append(d.vswr)

                self.last_logged_freq = d.freq

        except Exception as e:
            print("Sweep read error:", e)

        if freqs:
            log_points(freqs, vswrs)

        # update frame index
        self.current_frame_index += 1

        total = len(self.frames_data)

        self.progress_bar.setValue(self.current_frame_index)
        self.progress_label.setText(
            f"Frame {self.current_frame_index} / {total}"
        )

        # sweep finished
        if self.current_frame_index >= total:

            self.sweep_active = False
            self.progress_label.setText("Sweep complete")

            stop_log()

            return

        QTimer.singleShot(0, self.run_next_frame)
