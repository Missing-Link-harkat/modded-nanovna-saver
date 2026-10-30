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
"""Run code on the Qt main thread from the MCP server thread.

All widgets, the sweep worker signals and the application state belong to
the Qt main thread. The MCP server runs its own asyncio loop in another
thread and must never touch them directly. Every action is therefore
submitted to the main thread through a queued signal, and the server
awaits a concurrent.futures.Future.

A queued connection is used instead of BlockingQueuedConnection: the
server thread never blocks the main thread, and a modal dialog on the
main thread (confirmation, error box) does not deadlock the server.
"""

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)


@dataclass
class Pending:
    """Returned by an action that finishes later (e.g. a sweep).

    The server awaits `future` (resolved on the main thread by a Qt
    signal) and then runs `then` on the main thread to build the result.
    """

    future: Future
    then: Callable[[Any], Any] | None = None


class MainThreadBridge(QObject):
    _invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._invoke.connect(
            self._run, Qt.ConnectionType.QueuedConnection
        )

    @Slot(object)
    def _run(self, job: tuple[Callable[[], Any], Future]) -> None:
        func, fut = job
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(func())
        except BaseException as exc:  # pylint: disable=broad-except
            fut.set_exception(exc)

    def submit(self, func: Callable[[], Any]) -> Future:
        fut: Future = Future()
        self._invoke.emit((func, fut))
        return fut

    async def call(self, func: Callable[[], Any], timeout: float = 30.0):
        """Run func on the main thread; follow a Pending to completion."""
        result = await asyncio.wait_for(
            asyncio.wrap_future(self.submit(func)), timeout
        )
        if isinstance(result, Pending):
            value = await asyncio.wait_for(
                asyncio.wrap_future(result.future), timeout
            )
            if result.then is not None:
                then = result.then
                result = await asyncio.wait_for(
                    asyncio.wrap_future(self.submit(lambda: then(value))),
                    30.0,
                )
            else:
                result = value
        return result
