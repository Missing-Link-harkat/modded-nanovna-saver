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
"""Which state a LiteVNA64 is in, seen from USB (litevna-openvna#190).

The firmware (application), the vendor bootloader and the application in
Storage mode all use the same VID:PID 04b4:0008, and the serial number
string ("ZN406001", last letter changed on re-enumeration) is the same on
every unit. A device is therefore identified by its USB port path
(``location``, e.g. "1-1" on Linux), which stays the same across a reset,
a flash and a USB mode change as long as the cable stays in the same
socket.

| state       | how it looks on USB                                    |
|-------------|--------------------------------------------------------|
| app         | 04b4:0008 with a CDC interface (serial port)           |
| loader      | reserved: the loader has no USB yet (#184 adds CDC)    |
| vendor_bl   | 04b4:0008, product "CDC-ACM Demo" (dark screen update) |
| storage     | 04b4:0008, mass storage only, no serial port           |
| rom_dfu     | 2e3c:df11 (AT32 ROM DFU, BOOT0)                        |
| gone        | nothing on that port path                              |

Linux reads /sys/bus/usb/devices and sees every state. Elsewhere only
pyserial's port list is used, so storage and rom_dfu show up as "gone".
"""

import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

APP = "app"
LOADER = "loader"
VENDOR_BL = "vendor_bl"
STORAGE = "storage"
ROM_DFU = "rom_dfu"
GONE = "gone"
STATES = (APP, LOADER, VENDOR_BL, STORAGE, ROM_DFU, GONE)

LITEVNA_IDS = ((0x04B4, 0x0008),)
ROM_DFU_IDS = ((0x2E3C, 0xDF11),)
VENDOR_BL_PRODUCT = "CDC-ACM Demo"

SYSFS_USB = Path("/sys/bus/usb/devices")

_CLASS_CDC = {0x02, 0x0A}
_CLASS_MSC = 0x08


@dataclass(frozen=True)
class UsbDevice:
    location: str  # USB port path, the identity
    vid: int
    pid: int
    state: str
    port: str | None = None  # serial port when there is one
    product: str = ""
    serial: str = ""
    # Linux USB device number: new on every enumeration, so a reboot that
    # is over between two polls still shows (0 = unknown)
    devnum: int = 0

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "location": self.location,
            "port": self.port,
            "usb_id": f"{self.vid:04x}:{self.pid:04x}",
            "product": self.product,
        }


def classify(
    vid: int, pid: int, product: str, has_cdc: bool, has_msc: bool
) -> str | None:
    """State of one USB device, or None when it is not a LiteVNA."""
    if (vid, pid) in ROM_DFU_IDS:
        return ROM_DFU
    if (vid, pid) not in LITEVNA_IDS:
        return None
    if product.strip() == VENDOR_BL_PRODUCT:
        return VENDOR_BL
    if has_cdc:
        return APP
    if has_msc:
        return STORAGE
    return None


def location_of(port_location: str | None) -> str:
    """pyserial gives "1-1:1.0" (device path + interface); keep the device
    path. Other platforms give an opaque string that is used as is."""
    if not port_location:
        return ""
    return port_location.split(":", 1)[0]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _hex(text: str) -> int | None:
    try:
        return int(text, 16)
    except ValueError:
        return None


def scan_sysfs(root: Path = SYSFS_USB) -> list[UsbDevice]:
    """Every LiteVNA state visible in Linux sysfs."""
    found = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for name in entries:
        if ":" in name or name.startswith("usb"):
            continue  # interfaces and root hubs
        dev = root / name
        vid = _hex(_read(dev / "idVendor"))
        pid = _hex(_read(dev / "idProduct"))
        if vid is None or pid is None:
            continue
        has_cdc = has_msc = False
        port = None
        for intf in sorted(root.glob(f"{name}:*")):
            cls = _hex(_read(intf / "bInterfaceClass"))
            has_cdc |= cls in _CLASS_CDC
            has_msc |= cls == _CLASS_MSC
            tty = intf / "tty"
            if port is None and tty.is_dir():
                ttys = sorted(os.listdir(tty))
                if ttys:
                    port = f"/dev/{ttys[0]}"
        product = _read(dev / "product")
        state = classify(vid, pid, product, has_cdc, has_msc)
        if state is None:
            continue
        devnum = _read(dev / "devnum")
        found.append(
            UsbDevice(name, vid, pid, state, port, product,
                      _read(dev / "serial"),
                      int(devnum) if devnum.isdigit() else 0)
        )
    return found


def scan_serial(comports: Callable[[], Iterable] | None = None) -> list[UsbDevice]:
    """LiteVNA serial ports from pyserial (app and vendor_bl only)."""
    if comports is None:
        # pylint: disable=import-outside-toplevel
        from serial.tools import list_ports

        comports = list_ports.comports
    found = []
    for p in comports():
        vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
        if vid is None or pid is None:
            continue
        product = getattr(p, "product", None) or ""
        state = classify(vid, pid, product, True, False)
        if state is None:
            continue
        found.append(
            UsbDevice(
                location_of(getattr(p, "location", None)) or p.device,
                vid, pid, state, p.device, product,
                getattr(p, "serial_number", None) or "",
            )
        )
    return found


def scan() -> list[UsbDevice]:
    if sys.platform.startswith("linux") and SYSFS_USB.is_dir():
        return scan_sysfs()
    return scan_serial()


def find(devices: Iterable[UsbDevice], location: str) -> UsbDevice | None:
    for d in devices:
        if d.location == location:
            return d
    return None


def by_port(devices: Iterable[UsbDevice], port: str) -> UsbDevice | None:
    for d in devices:
        if d.port == port:
            return d
    return None
