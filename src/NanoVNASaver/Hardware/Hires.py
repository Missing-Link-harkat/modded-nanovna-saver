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


def start_command(
    start_hz: int, stop_hz: int, step_hz: int, avg: int = 1, name: str = "HIRES"
) -> str:
    if not NAME_RE.match(name):
        raise HiresError(
            f"Nimi '{name}' ei kelpaa: 1-20 merkkiä A-Z, a-z, 0-9, _ tai -."
        )
    return (f"hires start {int(start_hz)} {int(stop_hz)} {int(step_hz)} "
            f"{int(avg)} {name}")


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
