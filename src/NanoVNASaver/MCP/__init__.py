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
"""MCP support: Saver's functions for LLM clients.

The `mcp` package is optional (pip install "NanoVNASaver[mcp]"). Without
it Saver runs as before and the AI panel tells how to enable it.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6 import QtWidgets

from .control import AIControl

if TYPE_CHECKING:
    from ..NanoVNASaver import NanoVNASaver as vna_app
    from .server import SaverMCP

logger = logging.getLogger(__name__)

LOG_FILE = Path.home() / ".nanovna-saver" / "mcp-log.jsonl"


@dataclass
class MCPOptions:
    mode: str = "off"  # off | http | stdio
    port: int = 8765
    allow: bool = False
    workspace: Path | None = None
    headless: bool = False


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401  pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    return True


class MCPSupport:
    """Glue between the Saver window and the MCP server."""

    def __init__(self, app: "vna_app", options: MCPOptions) -> None:
        self.app = app
        self.options = options
        self.control = AIControl(LOG_FILE)
        self.service: "SaverMCP | None" = None
        self.panel: QtWidgets.QWidget | None = None

    def build_service(self) -> "SaverMCP":
        """Create the server (main thread). Imports mcp lazily."""
        if self.service is not None:
            return self.service
        # pylint: disable=import-outside-toplevel
        from .api import DEFAULT_WORKSPACE, SaverAPI
        from .bridge import MainThreadBridge
        from .panel import gui_confirm
        from .server import SaverMCP

        api = SaverAPI(
            self.app,
            self.options.workspace or DEFAULT_WORKSPACE,
            headless=self.options.headless,
        )
        confirm: Any = None
        if not self.options.headless:
            confirm = lambda message: gui_confirm(self.app, message)  # noqa: E731
        self.service = SaverMCP(api, self.control, MainThreadBridge(),
                                gui_confirm=confirm)
        return self.service

    def create_panel(self) -> QtWidgets.QWidget:
        if not mcp_available():
            box = QtWidgets.QGroupBox("AI (MCP)")
            layout = QtWidgets.QVBoxLayout(box)
            label = QtWidgets.QLabel(
                'MCP-tuki puuttuu: pip install "mcp" uvicorn'
            )
            label.setWordWrap(True)
            layout.addWidget(label)
            self.panel = box
            return box
        # pylint: disable=import-outside-toplevel
        from .panel import MCPPanel

        self.panel = MCPPanel(
            self.app, self.control, self.build_service, self.options.port
        )
        return self.panel

    def start(self, on_exit: Any = None) -> None:
        """Start the transport chosen on the command line."""
        if self.options.mode == "off":
            return
        if self.options.allow or self.options.headless:
            self.control.allow(True, by="command line")
        if self.options.mode == "stdio":
            self.build_service().start_stdio(on_exit=on_exit)
        elif self.options.mode == "http":
            if self.panel is not None and hasattr(self.panel, "toggle_server"):
                self.panel.toggle_server()
            else:
                self.build_service().start_http(self.options.port)
