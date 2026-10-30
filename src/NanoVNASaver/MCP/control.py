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
"""Who may control Saver right now, and what the AI has done.

Rules:
  * AI control is off by default. READ actions work whenever the server
    runs; ACT and PERSIST actions need the human to allow AI control
    (GUI checkbox, or --mcp-allow / headless mode).
  * STOP (GUI button or Esc in the AI panel) stops the sweep and revokes
    AI control at once. The human has to allow it again.
  * Every call is logged (memory + JSON lines file) so the human can see
    afterwards what the model did.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_KEEP = 500


@dataclass
class LogEntry:
    time: float
    tool: str
    risk: str
    args: dict[str, Any]
    outcome: str  # "ok", "denied", "error", "timeout", "stopped"
    detail: str = ""
    duration_ms: int = 0

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.time))
        args = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        text = f"{stamp} {self.tool}({args}) -> {self.outcome}"
        if self.detail:
            text += f": {self.detail}"
        return text


class AIControl:
    def __init__(self, log_file: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._allowed = False
        self._active_tool: str | None = None
        self.entries: list[LogEntry] = []
        self.log_file = log_file
        # Called (from any thread) after state changes; the GUI panel
        # connects a queued signal here.
        self.listeners: list[Callable[[], None]] = []

    # --- state -----------------------------------------------------------

    @property
    def allowed(self) -> bool:
        return self._allowed

    @property
    def active_tool(self) -> str | None:
        return self._active_tool

    def allow(self, allowed: bool, by: str = "user") -> None:
        with self._lock:
            changed = allowed != self._allowed
            self._allowed = allowed
        if changed:
            logger.info("AI control %s by %s", "allowed" if allowed else
                        "revoked", by)
            self._notify()

    def stop(self) -> None:
        """Human pressed STOP: revoke control. Sweep stop is done by GUI."""
        self.allow(False, by="STOP")
        self.record(LogEntry(time.time(), "STOP", "user", {}, "stopped",
                             "AI control revoked by user"))

    def begin(self, tool: str) -> None:
        self._active_tool = tool
        self._notify()

    def end(self) -> None:
        self._active_tool = None
        self._notify()

    # --- log -------------------------------------------------------------

    def record(self, entry: LogEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            del self.entries[:-LOG_KEEP]
        if self.log_file is not None:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(asdict(entry), default=str) + "\n")
            except OSError as exc:
                logger.warning("Cannot write AI log %s: %s",
                               self.log_file, exc)
        self._notify()

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # pylint: disable=broad-except
                logger.exception("AI control listener failed")
