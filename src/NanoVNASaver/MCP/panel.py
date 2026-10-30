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
"""AI (MCP) panel in the main window: server on/off, allow AI control,
what the AI is doing now, STOP and the AI log."""

import logging
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Signal

from .control import AIControl

if TYPE_CHECKING:
    from .server import SaverMCP

logger = logging.getLogger(__name__)


class MCPPanel(QtWidgets.QGroupBox):
    changed = Signal()

    def __init__(self, app: QtWidgets.QWidget, control: AIControl,
                 service_factory, port: int) -> None:
        super().__init__("AI (MCP)")
        self.app = app
        self.control = control
        self.service_factory = service_factory
        self.service: "SaverMCP | None" = None
        self.port = port

        layout = QtWidgets.QFormLayout(self)

        self.btn_server = QtWidgets.QPushButton("Käynnistä MCP-palvelin")
        self.btn_server.setMinimumHeight(20)
        self.btn_server.clicked.connect(self.toggle_server)
        self.btn_copy = QtWidgets.QPushButton("Kopioi")
        self.btn_copy.setToolTip(
            "Kopioi Claude Code -lisäyskomento (osoite + tunniste)"
        )
        self.btn_copy.setFixedWidth(60)
        self.btn_copy.setMinimumHeight(20)
        self.btn_copy.clicked.connect(self.copy_connection)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.btn_server, stretch=1)
        row.addWidget(self.btn_copy, stretch=0)
        layout.addRow(row)

        self.check_allow = QtWidgets.QCheckBox("Salli AI-ohjaus")
        self.check_allow.setToolTip(
            "Ilman tätä tekoäly voi vain lukea tietoja. Tiedostojen "
            "tallennus kysyy aina erikseen."
        )
        self.check_allow.toggled.connect(
            lambda on: self.control.allow(on, by="user")
        )
        self.btn_stop = QtWidgets.QPushButton("STOP")
        self.btn_stop.setMinimumHeight(20)
        self.btn_stop.setStyleSheet(
            "QPushButton { color: white; background: #b00020;"
            " font-weight: bold; }"
        )
        self.btn_stop.setToolTip(
            "Pysäyttää pyyhkäisyn ja ottaa AI-ohjauksen pois"
        )
        self.btn_stop.clicked.connect(self.stop)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.check_allow, stretch=1)
        row.addWidget(self.btn_stop, stretch=0)
        layout.addRow(row)

        self.label_status = QtWidgets.QLabel()
        self.label_status.setWordWrap(True)
        layout.addRow(self.label_status)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(90)
        self.log_view.setLineWrapMode(
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
        )
        layout.addRow(self.log_view)

        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+S"), app, self.stop)

        self.changed.connect(self.refresh, QtCore.Qt.ConnectionType.QueuedConnection)
        self.control.listeners.append(self.changed.emit)
        self.refresh()

    def toggle_server(self) -> None:
        if self.service is None or not self.service.url:
            try:
                self.service = self.service_factory()
                self.service.start_http(self.port)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("MCP server start failed")
                QtWidgets.QMessageBox.warning(
                    self, "MCP", f"Palvelimen käynnistys epäonnistui:\n{exc}"
                )
                self.service = None
        else:
            self.service.stop_http()
            self.control.allow(False, by="server stop")
        self.refresh()

    def copy_connection(self) -> None:
        if self.service is None or not self.service.url:
            return
        QtWidgets.QApplication.clipboard().setText(
            "claude mcp add --transport http nanovna-saver "
            f"{self.service.url} --header "
            f'"Authorization: Bearer {self.service.token}"'
        )

    def stop(self) -> None:
        self.app.worker.quit()
        custom = self.app.windows.get("custom_sweep")
        if custom is not None and getattr(custom, "sweep_active", False):
            custom.sweep_active = False
        self.control.stop()

    def refresh(self) -> None:
        running = self.service is not None and bool(self.service.url)
        self.btn_server.setText(
            "Pysäytä MCP-palvelin" if running else "Käynnistä MCP-palvelin"
        )
        self.btn_copy.setEnabled(running)
        self.check_allow.blockSignals(True)
        self.check_allow.setChecked(self.control.allowed)
        self.check_allow.blockSignals(False)
        if self.control.active_tool:
            status = f"<b>AI ohjaa:</b> {self.control.active_tool}"
            self.setStyleSheet("QGroupBox { color: #b00020; }")
        elif running:
            status = f"Kuuntelee {self.service.url}"
            self.setStyleSheet("")
        else:
            status = "Palvelin ei käynnissä"
            self.setStyleSheet("")
        self.label_status.setText(status)
        lines = [e.line() for e in self.control.entries[-30:]]
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )


def gui_confirm(parent: QtWidgets.QWidget, message: str) -> bool:
    answer = QtWidgets.QMessageBox.question(
        parent,
        "AI (MCP) pyytää vahvistusta",
        message,
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.No,
    )
    return answer == QtWidgets.QMessageBox.StandardButton.Yes
