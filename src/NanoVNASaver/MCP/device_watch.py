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
"""Follow the connected LiteVNA across resets, flashing and USB mode changes
(litevna-openvna#190).

While a real device is connected, its USB port path is polled every
POLL_MS. When the device leaves the application state (reset, update mode,
Storage mode, unplugged):

* a running sweep fails at once with the state that was seen,
* the serial port is closed (this also releases the device lock),
* ``changed`` is emitted (the MCP server turns it into a notification).

When the same port path shows the application again, Saver connects to it
by itself, also when the serial port name changed. A deliberate disconnect
(button, device_disconnect) stops following the device.

All of this runs on the Qt main thread (QTimer); nothing here blocks.
"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future

from PySide6.QtCore import QObject, QTimer, Signal

from ..Hardware import usb_state
from ..Hardware.usb_state import APP, GONE, UsbDevice

logger = logging.getLogger(__name__)

POLL_MS = 500
RETRY_S = 2.0  # between reconnect attempts while the firmware boots
SETTLE_S = 1.5  # app port up this long before the first probe
MAX_WAIT_S = 300


class DeviceWatch(QObject):
    changed = Signal(dict)

    def __init__(
        self,
        app,
        scan: Callable[[], list[UsbDevice]] = usb_state.scan,
        poll_ms: int = POLL_MS,
    ) -> None:
        super().__init__()
        self.app = app
        self._scan = scan
        self.location: str | None = None  # identity of the followed device
        self.state: str | None = None
        self.port: str | None = None
        self._devnum = 0  # USB device number when (re)connected
        self._kind = ""  # Saver's detected type at connect, e.g. "H4"
        self._had_connection = False  # connected to a followed device
        self._app_since: float | None = None  # app port seen since (lost)
        self.lost = False  # left the app state while connected
        self.reason = ""
        self.reconnects = 0
        self._own_change = False  # our own connect/disconnect in progress
        self._next_try = 0.0
        self._waiters: list[dict] = []
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.poll)
        app.serial_control.connected.connect(self._on_connected)

    # --- public ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "state": self.state or ("unknown" if self.location else GONE),
            "location": self.location,
            "port": self.port,
            "connected": bool(self.app.vna.connected()),
            "followed": self.location is not None,
            "lost": self.lost,
            "reason": self.reason,
            "reconnects": self.reconnects,
        }

    def wait(
        self, state: str, timeout_s: float, expect_change: bool = False
    ) -> Future:
        """Future resolved with snapshot() once the device is in `state`
        (for "app": also reconnected and idle), or at the timeout.

        expect_change: the caller has just restarted the device (reset,
        flash). The device stays on USB for a moment after the command,
        so without this "app" would be true at once (seen on the real
        device 11.11.); with it the wait first needs to see the device
        leave `state`."""
        if state not in usb_state.STATES:
            raise ValueError(
                f"Tuntematon tila {state!r}; sallitut: "
                + ", ".join(usb_state.STATES)
            )
        timeout_s = max(0.0, min(float(timeout_s), MAX_WAIT_S))
        now = time.monotonic()
        waiter = {"want": state, "fut": Future(), "started": now,
                  "deadline": now + timeout_s, "need_change": expect_change}
        self._waiters.append(waiter)
        self.poll()
        return waiter["fut"]

    # --- internals -------------------------------------------------------

    def _connected_port(self) -> str | None:
        if not self.app.vna.connected():
            return None
        return str(getattr(self.app.serial_control.interface, "port", ""))

    def _on_connected(self, connected: bool) -> None:
        if self._own_change:
            return
        if not connected:
            # the user (or device_disconnect) closed it: stop following
            self.location = None
            self._had_connection = False
            self.lost = False
            self.reason = ""
            self._update(None, None, "disconnected")
            return
        port = self._connected_port()
        dev = usb_state.by_port(self._scan(), port) if port else None
        if dev is None:  # virtual device or not a LiteVNA
            self.location = None
            return
        self.location = dev.location
        self._had_connection = True
        self._devnum = dev.devnum
        self._kind = getattr(self.app.serial_control.interface, "comment", "")
        self.lost = False
        self.reason = ""
        self._update(dev.state, dev.port, "connected")
        self._timer.start()

    def _update(self, state: str | None, port: str | None, event: str) -> None:
        if state == self.state and port == self.port and event == "poll":
            return
        self.state, self.port = state, port
        for waiter in self._waiters:
            if state != waiter["want"] or event == "lost":
                waiter["need_change"] = False  # it has left: now wait
        info = {"event": event, **self.snapshot()}
        logger.info("Device state: %s", info)
        self.changed.emit(info)

    def poll(self) -> None:
        devices = self._scan()
        if self.location is None and self._waiters:
            # device_wait before any connection: follow the only LiteVNA
            if len(devices) == 1:
                self.location = devices[0].location
        if self.location is not None:
            dev = usb_state.find(devices, self.location)
            state = dev.state if dev else GONE
            port = dev.port if dev else None
            connected_port = self._connected_port()
            renumbered = bool(
                dev and dev.devnum and self._devnum
                and dev.devnum != self._devnum
            )
            if connected_port and (
                state != APP or port != connected_port or renumbered
            ):
                self._lose(state if not renumbered else "gone", port)
            elif self.lost and state == APP and port:
                now = time.monotonic()
                if self._app_since is None:
                    self._app_since = now
                if (now - self._app_since >= SETTLE_S
                        and now >= self._next_try):
                    self._reconnect(port)
            if state != APP:
                self._app_since = None
            self._update(state, port, "poll")
        elif not devices:
            self._update(GONE, None, "poll")
        self._check_waiters()
        if self.location is None and not self._waiters:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    def _lose(self, state: str, port: str | None) -> None:
        self.lost = True
        self.reason = (
            f"Laite poistui sovellustilasta (USB-tila nyt: {state}"
            + (f", portti {port}" if port and state == APP else "")
            + "). Saver yhdistää uudelleen, kun laite palaa sovellustilaan;"
            " odota device_wait('app')."
        )
        logger.warning("%s", self.reason)
        worker = self.app.worker
        if worker.isRunning():
            worker.error_message = self.reason
            worker.quit()
            worker.signals.sweep_error.emit()  # fails a waiting sweep_run
            worker.wait(3000)
        self._own_change = True
        try:
            self.app.serial_control.disconnect_device()
        finally:
            self._own_change = False
        self._next_try = time.monotonic() + RETRY_S
        self._update(state, port, "lost")

    def _reconnect(self, port: str) -> None:
        self._next_try = time.monotonic() + RETRY_S
        sc = self.app.serial_control
        sc.rescanSerialPort()
        combo = sc.inp_port
        index = next(
            (
                i
                for i in range(combo.count())
                if getattr(combo.itemData(i), "port", None) == port
            ),
            -1,
        )
        if index < 0:
            return  # not answering yet; try again later
        kind = getattr(combo.itemData(index), "comment", "")
        if self._kind and kind != self._kind:
            # Real device 11.11.: the CDC port is up before the firmware
            # shell answers; detection then says "Unknown" and every sweep
            # fails. Only reconnect when it is recognised as before.
            logger.info("Reconnect waits: %s detected as %r, expected %r",
                        port, kind, self._kind)
            return
        combo.setCurrentIndex(index)
        self._own_change = True
        try:
            sc.connect_device()
        finally:
            self._own_change = False
        if self.app.vna.connected():
            dev = usb_state.find(self._scan(), self.location or "")
            self._devnum = dev.devnum if dev else 0
            self.lost = False
            self.reason = ""
            self.reconnects += 1
            self._update(APP, port, "reconnected")

    def _ready(self, want: str) -> bool:
        if want != APP:
            return self.state == want
        if not self._had_connection:
            # never connected in this session: nothing to reconnect, the
            # USB state is the answer (device_connect is the caller's)
            return self.state == APP
        return (
            self.state == APP
            and not self.lost
            and (self.location is None or self._connected_port() is not None)
            and not self.app.worker.isRunning()
        )

    def _check_waiters(self) -> None:
        now = time.monotonic()
        keep = []
        for w in self._waiters:
            fut = w["fut"]
            if fut.done():
                continue
            waited = round(now - w["started"], 1)
            if not w["need_change"] and self._ready(w["want"]):
                fut.set_result({**self.snapshot(), "waited_s": waited})
            elif now >= w["deadline"]:
                fut.set_result(
                    {
                        **self.snapshot(),
                        "waited_s": waited,
                        "timeout": True,
                        "wanted": w["want"],
                        "saw_change": not w["need_change"],
                    }
                )
            else:
                keep.append(w)
        self._waiters = keep
