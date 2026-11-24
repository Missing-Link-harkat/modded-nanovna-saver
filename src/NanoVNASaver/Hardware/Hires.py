"""The device's own high-resolution sweep (litevna-openvna #207, #212).

The Missing Link firmware measures a wide range in frames by itself
(`hires plan|start|status|stop`, docs/PROTOCOL.md) and writes raw S11 to
the SD card; the file is fetched with `fs get` in CRC-checked chunks. This
module parses those replies and fetches the file. It has no Qt and no
Saver state: it talks to the device through a `shell(cmd, timeout)`
callable that returns the reply lines without the prompt.
"""

import base64
import re
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

Shell = Callable[[str, float], list[str]]

STATES = ("idle", "running", "done", "aborted", "failed")
FINISHED = ("done", "aborted", "failed")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")

_PLAN = re.compile(
    r"hires: (\d+) points, (\d+) frames of up to (\d+), last point (\d+) Hz,"
    r" estimated (\d+) s \(avg (\d+)\)"
)
_STATUS = re.compile(
    r"hires: (idle|running|done|aborted|failed)"
    r"(?:, frame (\d+)/(\d+), point (\d+)/(\d+), elapsed (\d+) s,"
    r" eta (\d+) s, avg (\d+))?$"
)


class HiresError(Exception):
    """The device refused or the reply could not be understood."""


@dataclass
class Plan:
    points: int
    frames: int
    per_frame: int
    last_hz: int
    estimate_s: int
    avg: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Status:
    state: str
    frame: int = 0
    frames: int = 0
    points_done: int = 0
    points: int = 0
    elapsed_s: int = 0
    eta_s: int = 0
    avg: int = 0
    file: str = ""
    message: str = ""

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _refusal(lines: list[str]) -> None:
    for ln in lines:
        if ln.startswith("?"):
            raise HiresError(ln.lstrip("?").strip())


def plan_command(start_hz: int, stop_hz: int, step_hz: int, avg: int = 1) -> str:
    return f"hires plan {int(start_hz)} {int(stop_hz)} {int(step_hz)} {int(avg)}"


OUTS = ("sd", "usb", "both")


def start_command(
    start_hz: int, stop_hz: int, step_hz: int, avg: int = 1, name: str = "HIRES",
    out: str = "sd",
) -> str:
    if not NAME_RE.match(name):
        raise HiresError(
            f"Nimi '{name}' ei kelpaa: 1-20 merkkiä A-Z, a-z, 0-9, _ tai -."
        )
    if out not in OUTS:
        raise HiresError(f"out='{out}' ei kelpaa: sd, usb tai both.")
    return (f"hires start {int(start_hz)} {int(stop_hz)} {int(step_hz)} "
            f"{int(avg)} {name}" + ("" if out == "sd" else f" out={out}"))


def parse_plan(lines: list[str]) -> Plan:
    _refusal(lines)
    plan = None
    warnings = []
    for ln in lines:
        m = _PLAN.match(ln)
        if m:
            plan = Plan(*(int(g) for g in m.groups()))
        elif ln.startswith("hires: warning:"):
            warnings.append(ln[len("hires: warning:"):].strip())
    if plan is None:
        raise HiresError("Laitteen hires-vastausta ei tunnistettu: "
                         + " | ".join(lines)[:300])
    plan.warnings = warnings
    return plan


def parse_started(lines: list[str]) -> tuple[Plan, str]:
    plan = parse_plan(lines)
    for ln in lines:
        if ln.startswith("hires: started, file "):
            return plan, ln[len("hires: started, file "):].strip()
    raise HiresError("Laite ei ilmoittanut aloittaneensa: "
                     + " | ".join(lines)[:300])


def parse_status(lines: list[str]) -> Status:
    _refusal(lines)
    status = None
    for ln in lines:
        m = _STATUS.match(ln)
        if m and status is None:
            nums = [int(g) if g is not None else 0 for g in m.groups()[1:]]
            status = Status(m.group(1), *nums)
        elif status is not None and ln.startswith("hires: file "):
            status.file = ln[len("hires: file "):].strip()
        elif status is not None and ln.startswith("hires: "):
            status.message = ln[len("hires: "):].strip()
    if status is None:
        raise HiresError("hires status -vastausta ei tunnistettu: "
                         + " | ".join(lines)[:300])
    return status


def fetch_file(
    shell: Shell,
    path: str,
    retries: int = 5,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bytes:
    """Read a file from the device's SD card with `fs get` (128-byte
    base64 chunks, CRC-32 per chunk and for the whole file)."""
    head = shell(f"fs get {path}", 30.0)
    _refusal(head)
    parts = head[0].split() if head else []
    if len(parts) < 4 or parts[0] != "size" or parts[2] != "crc":
        raise HiresError(f"fs get {path}: " + " | ".join(head)[:300])
    size, want = int(parts[1]), int(parts[3], 16)
    out = bytearray()
    bad = 0
    while len(out) < size:
        if cancelled is not None and cancelled():
            raise HiresError("tiedoston haku keskeytettiin")
        reply = shell(f"fs get {path} {len(out)}", 5.0)
        p = reply[0].split() if reply else []
        chunk = b""
        ok = len(p) == 5 and p[0] == "data" and int(p[1]) == len(out)
        if ok:
            try:
                chunk = base64.b64decode(p[4], validate=True)
            except ValueError:
                ok = False
            ok = ok and len(chunk) == int(p[2]) and (
                zlib.crc32(chunk) & 0xFFFFFFFF) == int(p[3], 16)
        if not ok or not chunk:
            bad += 1
            if bad > retries:
                raise HiresError(f"fs get {path}: liikaa virheitä kohdassa "
                                 f"{len(out)}: " + " | ".join(reply)[:200])
            continue
        bad = 0
        out += chunk
        if progress is not None:
            progress(len(out), size)
    got = zlib.crc32(bytes(out)) & 0xFFFFFFFF
    if got != want:
        raise HiresError(f"fs get {path}: CRC {got:08x}, laite sanoi {want:08x}")
    return bytes(out)


def parse_started_any(lines: list[str]) -> tuple[Plan, str]:
    """As parse_started, also for `out=usb` (file "(none)")."""
    plan = parse_plan(lines)
    for ln in lines:
        if ln.startswith("hires: started, file "):
            path = ln[len("hires: started, file "):].split(",")[0].strip()
            return plan, "" if path.startswith("(none") else path
    raise HiresError("Laite ei ilmoittanut aloittaneensa: "
                     + " | ".join(lines)[:300])


# --- the stream (litevna-openvna #210, src/core/hires_stream.h) -------------
#
# With `out=usb|both` the device sends every finished frame on the same serial
# port as the shell: 0xA5, type, uint16 length, payload (little endian), the
# payload ending in a CRC-32 (zlib) of what is before it. 0x20 BEGIN, 0x21
# FRAME, 0x22 END. Shell text between packets is ASCII (never 0xA5). The same
# parser as the reference tools/hires_stream.py of the firmware repository.

MAGIC, P_BEGIN, P_FRAME, P_END = 0xA5, 0x20, 0x21, 0x22
_CAL = {0: "none", 1: "interp", 2: "interp-reim"}
_STATE = {2: "done", 3: "aborted", 4: "failed"}


def supports_stream(info_lines: list[str]) -> bool:
    """The `info` reply of a firmware that can stream hires frames."""
    return any("Features:" in ln and "hires" in ln and "stream v1" in ln
               for ln in info_lines)


def _packet(typ: int, payload: bytes) -> bytes:
    body = payload + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
    return bytes([MAGIC, typ]) + struct.pack("<H", len(body)) + body


def encode_begin(start: int, step: int, total: int, frames: int, per_frame: int,
                 avg: int = 1, cal: int = 0, out: int = 2, run: int = 0,
                 name: str = "") -> bytes:
    return _packet(P_BEGIN, struct.pack("<BBBBQQIIHH", 1, cal, avg, out, start, step,
                                        total, frames, per_frame, run)
                   + name.encode("ascii"))


def encode_frame(frame: int, first: int, values) -> bytes:
    vals = list(values)
    return _packet(P_FRAME, struct.pack("<IIH", frame, first, len(vals))
                   + b"".join(struct.pack("<ff", v.real, v.imag) for v in vals))


def encode_end(state: int, points: int, frames: int, msg: str = "") -> bytes:
    return _packet(P_END, struct.pack("<BII", state, points, frames)
                   + msg.encode("ascii"))


class Stream:
    """Packets out of a byte stream that also carries shell text."""

    def __init__(self) -> None:
        self.begin: dict | None = None
        self.frames: dict[int, tuple[int, list[tuple[float, float]]]] = {}
        self.end: dict | None = None
        self.bad = 0
        self.text = bytearray()
        self._buf = bytearray()

    def feed(self, data: bytes) -> "Stream":
        self._buf += data
        b = self._buf
        i = 0
        while i < len(b):
            if b[i] != MAGIC:
                j = b.find(bytes([MAGIC]), i)
                j = len(b) if j < 0 else j
                self.text += b[i:j]
                i = j
                continue
            if len(b) - i < 4:
                break
            typ, ln = b[i + 1], b[i + 2] | (b[i + 3] << 8)
            if typ not in (P_BEGIN, P_FRAME, P_END):
                self.text.append(b[i])
                i += 1
                continue
            if len(b) - i < 4 + ln:
                break
            payload = bytes(b[i + 4:i + 4 + ln])
            good = ln >= 4 and (zlib.crc32(payload[:-4]) & 0xFFFFFFFF) == \
                struct.unpack_from("<I", payload, ln - 4)[0]
            if not good or not self._decode(typ, payload[:-4]):
                self.bad += 1
                self.text.append(b[i])
                i += 1
                continue
            i += 4 + ln
        del b[:i]
        return self

    def _decode(self, typ: int, p: bytes) -> bool:
        try:
            if typ == P_BEGIN:
                (ver, cal, avg, out, start, step, total, frames, per,
                 run) = struct.unpack_from("<BBBBQQIIHH", p, 0)
                self.begin = {"version": ver, "cal": _CAL.get(cal, str(cal)),
                              "avg": avg, "out": out, "start": start,
                              "step": step, "total": total, "frames": frames,
                              "per_frame": per, "run": run,
                              "file": p[32:].decode("ascii", "replace")}
            elif typ == P_FRAME:
                frame, first, n = struct.unpack_from("<IIH", p, 0)
                if len(p) != 10 + 8 * n:
                    return False
                v = struct.unpack_from(f"<{2 * n}f", p, 10)
                self.frames[frame] = (first, list(zip(v[0::2], v[1::2])))
            else:
                state, points, frames = struct.unpack_from("<BII", p, 0)
                self.end = {"state": _STATE.get(state, str(state)),
                            "points": points, "frames": frames,
                            "message": p[9:].decode("ascii", "replace")}
            return True
        except struct.error:
            return False

    def text_lines(self) -> list[str]:
        text = self.text.decode("ascii", "replace").replace("ch> ", "\n")
        return [ln.strip() for ln in text.splitlines()
                if ln.strip() and ln.strip() != "ch>"]

    def points(self) -> list[tuple[int, float, float]]:
        if not self.begin:
            return []
        s, st = self.begin["start"], self.begin["step"]
        out = []
        for fr in sorted(self.frames):
            first, vals = self.frames[fr]
            out += [(s + (first + k) * st, re, im) for k, (re, im) in enumerate(vals)]
        return out

    def rows(self) -> str:
        """The SD file's data rows, byte for byte (hires_format_row)."""
        return "".join(f"{f} {re:.6e} {im:.6e}\r\n" for f, re, im in self.points())

    def touchstone(self) -> bytes:
        b = self.begin or {}
        head = (f"! Touchstone from the hires stream (#210): {b.get('start')} Hz + "
                f"k * {b.get('step')} Hz, {len(self.points())} points, calibration "
                f"{b.get('cal')}, avg {b.get('avg')}, device file "
                f"{b.get('file') or '-'}\r\n# Hz S RI R 50\r\n")
        return (head + self.rows()).encode("ascii")


class Link:
    """What a streamed job needs besides the shell: send a command without
    reading the reply, and read whatever bytes arrive."""

    def __init__(self, shell: Shell, send: Callable[[str], None],
                 read: Callable[[float], bytes]) -> None:
        self.shell, self.send, self.read = shell, send, read


def serial_link(port) -> Link:
    def send(cmd: str) -> None:
        with port.lock:
            port.write(f"{cmd}\r".encode("ascii"))

    def read(timeout: float) -> bytes:
        with port.lock:              # held only for one read: a stop can get in
            old = port.timeout
            port.timeout = timeout
            try:
                data = port.read(1)
                if data:
                    data += port.read(port.in_waiting or 0)
            finally:
                port.timeout = old
        return data

    return Link(serial_shell(port), send, read)


def follow_stream(
    link: Link,
    cmd: str,
    timeout: float,
    progress: Callable[[Stream], None] | None = None,
) -> Stream:
    """Start a job with out=usb|both and collect its stream to the END packet.
    A refusal before the BEGIN packet raises HiresError with the reason."""
    st = Stream()
    link.send(cmd)
    deadline = time.monotonic() + timeout
    while st.end is None:
        if time.monotonic() > deadline:
            link.send("hires stop")
            raise HiresError("työ kesti yli arvion, joten se pysäytettiin")
        st.feed(link.read(0.5))
        if st.begin is None:
            for ln in st.text_lines():
                if ln.startswith("?"):
                    raise HiresError(ln.lstrip("?").strip())
        if progress is not None:
            progress(st)
    return st


def serial_shell(port) -> Shell:
    """A fast shell over a pyserial port: reads up to the prompt instead of
    waiting for silence (Saver's exec_command waits 50 ms per command,
    which would make fetching a 1 MB file take a quarter of an hour)."""

    def run(cmd: str, timeout: float) -> list[str]:
        with port.lock:
            old = port.timeout
            port.timeout = timeout
            try:
                port.reset_input_buffer()
                port.write(f"{cmd}\r".encode("ascii"))
                raw = port.read_until(b"ch> ")
            finally:
                port.timeout = old
        if not raw.endswith(b"ch> "):
            raise OSError(f"laite ei vastannut komentoon '{cmd}' ajoissa")
        text = raw[:-4].decode("ascii", "replace")
        return [ln.strip() for ln in text.splitlines()
                if ln.strip() and ln.strip() != cmd]

    return run
