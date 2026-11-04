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
"""Allowlist for the firmware text shell (device_shell tools).

Only commands that match a rule here can be sent to the device; there is
no free command line. Every rule has a risk class (same classes as the
other actions) and a reason. A command that matches no rule is refused.

This module does not import Qt so that it can be tested without a display.

Input events (`input key|ptr|wheel`) are NOT device_shell commands: they
drive the device's own menus, which can do everything the shell list
excludes (DFU, saving a calibration, USB mode). They live in INPUT_RULES
and are sent by the separate device_input tool, which needs its own
permission (AIControl.input_allowed). `cal on` is not listed either: the
firmware sets the flag without checking that a valid calibration exists
(litevna-openvna#144); `cal off` and `recall` are enough.

Deliberately NOT here (refused because unlisted): anything that flashes or
reboots (`reset`, `config-reset`, bootloader and DFU commands), changes
the USB mode or the SD card (`usb ...`, `sd read|cs|...`, `fs`), writes
registers or pins (`pin mode`, `rf set`, `i2c`, `clk-drive`, `test-mode`)
and `clk-set` (changes the measurement clock; the owner decides whether
it belongs here, litevna-openvna#108). Calibration standards are
captured with the calibration_* actions, which ask the human to connect
the standard, not with `cal open|short|load`.
"""

import re
from dataclasses import dataclass

from .registry import Risk

MAX_COMMAND_LENGTH = 120

# printable ASCII, no command separators or redirection
_ALLOWED_CHARS = re.compile(r"[A-Za-z0-9 _.,:+\-]+")

_HEX = r"[0-9a-fA-F]{1,8}"
_INT = r"-?\d{1,6}"
_UINT = r"\d{1,12}"
_SLOT = r"[0-4]"
_TEXT = r"[A-Za-z0-9_.\- ]{1,40}"


@dataclass(frozen=True)
class ShellRule:
    pattern: re.Pattern[str]
    risk: Risk
    reason: str


def _r(pattern: str, risk: Risk, reason: str) -> ShellRule:
    return ShellRule(re.compile(pattern), risk, reason)


R, A, P = Risk.READ, Risk.ACT, Risk.PERSIST

RULES: tuple[ShellRule, ...] = (
    # ---- READ: diagnostics, nothing changes
    _r(r"help", R, "command list"),
    _r(r"version", R, "firmware version and protocol version (proto 1.x)"),
    _r(r"info", R, "board, build and clock"),
    _r(r"sweep-stats", R, "sweep timing and late-point counters"),
    _r(r"sd (stats|info)", R, "SD card statistics"),
    _r(r"cal", R, "calibration state"),
    _r(r"cal why", R, "why a stored calibration was rejected"),
    _r(r"msc (log|events)", R, "USB mass storage log"),
    _r(r"pin-log", R, "pin change log dump"),
    _r(r"usb (status|counters)", R, "USB state and error counters"),
    _r(r"input stats", R, "input event counters"),
    _r(r"ui-stats", R, "UI redraw statistics"),
    _r(r"meas", R, "measurement name fields"),
    _r(r"ref( show)?", R, "reference mode state"),
    _r(r"s21", R, "S21 measurement state"),
    _r(r"sweep", R, "current sweep range and points"),
    _r(r"sweep-avg", R, "averaging count"),
    _r(r"sweep-settle", R, "PLL settle time"),
    _r(r"ref-settle", R, "reference settle time"),
    _r(r"rf-range|irq-guard|fast-retune", R, "measurement option state"),
    _r(r"power", R, "output power"),
    _r(r"vbat", R, "battery voltage"),
    _r(r"time", R, "RTC time"),
    # ---- ACT: changes the running session, gone after reset
    _r(rf"sweep {_UINT} {_UINT}( {_UINT})?", A, "set sweep range"),
    _r(r"sweep-avg ([1-9]|1[0-6])", A, "set averaging"),
    _r(r"sweep-settle \d{2,4}", A, "set PLL settle (firmware clamps)"),
    _r(r"ref (on|off)", A, "switch reference measurement"),
    _r(r"s21 (on|off|norm|norm clear)", A,
       "S21 measurement; norm = sweep once as THRU baseline (RAM only)"),
    _r(r"power( lo)? [0-3]", A, "set output power"),
    _r(r"cal off", A, "bypass calibration (`cal on` is not listed: litevna-openvna#144)"),
    _r(rf"recall {_SLOT}", A, "load a calibration slot into RAM"),
    # ---- PERSIST: writes flash or the SD card, human confirms
    _r(rf"save {_SLOT}", P, "write calibration to flash slot (overwrites)"),
    _r(r"meas save", P, "write the sweep as .s1p to the SD card"),
    _r(rf"meas (antenna|test|operator|note) {_TEXT}", P,
       "set measurement name (stored in flash config)"),
)


# Events for the device's own user interface (device_input). Same character
# rules as the shell; sent only with the separate UI-control permission.
INPUT_RULES: tuple[ShellRule, ...] = (
    _r(rf"input key {_HEX} {_HEX} (up|down)", A, "press/release a key"),
    _r(rf"input ptr {_INT} {_INT} (down|up|move)", A, "pointer event"),
    _r(rf"input wheel {_INT}", A, "wheel event"),
)


class ShellRefused(ValueError):
    """Command is not allowed; the message says why."""


def normalize(command: str) -> str:
    cmd = " ".join(command.split())
    if not cmd:
        raise ShellRefused("Tyhjä komento.")
    if len(cmd) > MAX_COMMAND_LENGTH:
        raise ShellRefused(f"Komento on yli {MAX_COMMAND_LENGTH} merkkiä.")
    if not _ALLOWED_CHARS.fullmatch(cmd):
        raise ShellRefused(
            "Komennossa on sallimattomia merkkejä (sallittu: kirjaimet, "
            "numerot, väli ja _ . , : + -)."
        )
    return cmd


def classify(command: str) -> tuple[str, ShellRule]:
    """Normalized command and its rule, or ShellRefused."""
    cmd = normalize(command)
    for rule in RULES:
        if rule.pattern.fullmatch(cmd):
            return cmd, rule
    if any(r.pattern.fullmatch(cmd) for r in INPUT_RULES):
        raise ShellRefused(
            f"Komento '{cmd}' kuuluu työkaluun device_input "
            "(vaatii käyttäjän erillisen luvan laitteen käyttöliittymän ohjaukseen)."
        )
    raise ShellRefused(
        f"Komento '{cmd}' ei ole sallittujen listalla. "
        "Sallitut komennot: katso docs/MCP.md (device_shell)."
    )


def check(command: str, risk: Risk) -> str:
    """Normalized command if it is allowed at exactly this risk class,
    else ShellRefused naming the tool that fits."""
    cmd, rule = classify(command)
    if rule.risk is not risk:
        tool = {
            R: "device_shell",
            A: "device_shell_act",
            P: "device_shell_persist",
        }[rule.risk]
        raise ShellRefused(f"Komento '{cmd}' kuuluu työkaluun {tool}.")
    return cmd


def allowed_commands(risk: Risk) -> list[str]:
    """Rule patterns of one class, for the tool description."""
    return [r.pattern.pattern for r in RULES if r.risk is risk]


def check_input(command: str) -> str:
    """Normalized input event if it is one of INPUT_RULES, else ShellRefused."""
    cmd = normalize(command)
    for rule in INPUT_RULES:
        if rule.pattern.fullmatch(cmd):
            return cmd
    raise ShellRefused(
        f"Komento '{cmd}' ei ole syötetapahtuma. device_input hyväksyy vain: "
        "input key <hex> <hex> up|down, input ptr <x> <y> down|up|move, "
        "input wheel <n>."
    )


def confirm_text(command: str) -> str:
    """Text for the user before a PERSIST command: names the rule's own
    reason, so the user sees what is being confirmed."""
    try:
        cmd, rule = classify(command)
        reason = rule.reason
    except ShellRefused:
        cmd, reason = " ".join(command.split()), "kirjoittaa laitteen flash-muistiin tai SD-kortille"
    return (
        f"Tekoäly haluaa lähettää laitteelle komennon: {cmd}. "
        f"Mitä se tekee: {reason}. Sallitaanko?"
    )
