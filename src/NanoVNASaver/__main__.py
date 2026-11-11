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
"""
NanoVNASaver

A multiplatform tool to save Touchstone files from the
NanoVNA, sweep frequency spans in segments to gain more
data points, and generally display and analyze the
resulting data.
"""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from NanoVNASaver import NanoVNASaver
from NanoVNASaver.About import INFO, VERSION
from NanoVNASaver.Hardware import VirtualVNA
from NanoVNASaver.MCP import MCPOptions
from NanoVNASaver.MCP.stdio_wire import claim_stdout
from NanoVNASaver.Touchstone import Touchstone
from NanoVNASaver.utils import get_runtime_information


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-d", "--debug", action="store_true", help="Set loglevel to debug"
    )
    parser.add_argument(
        "-D", "--debug-file", help="File to write debug logging output to"
    )
    parser.add_argument(
        "-a",
        "--auto-connect",
        action="store_true",
        help="Auto connect if one device detected",
    )
    parser.add_argument(
        "-f",
        "--file",
        help="Touchstone file to load as sweep for off device usage",
    )
    parser.add_argument(
        "-r",
        "--ref-file",
        help="Touchstone file to load as reference for off device usage",
    )
    parser.add_argument(
        "--mcp",
        choices=("off", "http", "stdio"),
        default="off",
        help="Start the MCP server for LLM clients (see docs/MCP.md)",
    )
    parser.add_argument(
        "--mcp-port", type=int, default=8765, help="MCP HTTP port"
    )
    parser.add_argument(
        "--mcp-allow",
        action="store_true",
        help="Allow AI control at start (otherwise the user ticks it)",
    )
    parser.add_argument(
        "--mcp-dir",
        type=Path,
        help="Work folder for files read/written by MCP",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="No window (with --mcp stdio); AI control is allowed",
    )
    parser.add_argument(
        "--virtual",
        nargs="?",
        const="",
        metavar="TOUCHSTONE",
        help="Add a simulated VNA (optionally replaying a .s1p/.s2p file)",
    )
    parser.add_argument(
        "--virtual-seed",
        type=int,
        metavar="N",
        help="Simulated antenna and filter drawn from seed N",
    )
    parser.add_argument(
        "--version", action="version", version=f"NanoVNASaver {VERSION}"
    )
    args = parser.parse_args()

    if args.mcp == "stdio":
        # before Qt or anything else can write to fd 1 (#189)
        claim_stdout()
    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.virtual is not None:
        VirtualVNA.enable(args.virtual or None, seed=args.virtual_seed)

    console_log_level = logging.WARNING
    file_log_level = logging.DEBUG

    # with --mcp stdio, stdout carries the MCP protocol: print elsewhere
    print(INFO, file=sys.stderr if args.mcp == "stdio" else sys.stdout)

    if args.debug:
        console_log_level = logging.DEBUG

    logger = logging.getLogger("NanoVNASaver")
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(console_log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    if args.debug_file:
        fh = logging.FileHandler(args.debug_file)
        fh.setLevel(file_log_level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    # Print diagnostic data
    logger.debug("Runtime information:")
    for lib in get_runtime_information():
        logger.debug(" - %s", lib)

    logger.info("Startup...")

    app = QtWidgets.QApplication(sys.argv)
    window = NanoVNASaver(
        MCPOptions(
            mode=args.mcp,
            port=args.mcp_port,
            allow=args.mcp_allow,
            workspace=args.mcp_dir,
            headless=args.headless,
        )
    )
    if not args.headless:
        window.show()

    if args.auto_connect:
        window.auto_connect()
    if args.file:
        t = Touchstone(args.file)
        t.load()
        window.saveData(t.s11, t.s21, args.file)
        window.dataUpdated()
    if args.ref_file:
        t = Touchstone(args.ref_file)
        t.load()
        window.setReference(t.s11, t.s21, args.ref_file)
        window.dataUpdated()
    on_exit = app.quit
    if args.headless:
        on_exit = _headless_exit(window, app)
    window.mcp.start(on_exit=on_exit)
    try:
        app.exec()
    except BaseException as exc:
        logger.exception("%s", exc)
        raise exc
    if args.headless and args.mcp == "stdio":
        # The device is released already (_headless_exit). With mcp 1.x the
        # stdio reader thread can still be blocked on sys.stdin, and normal
        # interpreter shutdown then aborts ("could not acquire lock for
        # <stdin>"), so leave without it.
        logging.shutdown()
        sys.stderr.flush()
        os._exit(0)


def _headless_exit(window: NanoVNASaver, app: QtWidgets.QApplication):
    """Exit cleanly when the client goes away (stdin EOF, e.g. the ssh
    connection closes) or on SIGTERM/SIGHUP: stop the sweep and release the
    serial port before quitting (#189)."""
    done = False

    def finish() -> None:
        nonlocal done
        if done:
            return
        done = True
        try:
            window.shutdown()
        finally:
            app.quit()

    def on_signal(signum: int, _frame: object) -> None:
        logging.getLogger("NanoVNASaver").info(
            "Signal %d, shutting down", signum
        )
        QtCore.QTimer.singleShot(0, finish)

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)
    # Python runs signal handlers only between bytecodes; wake up regularly
    # so a signal is not left waiting inside the Qt event loop.
    tick = QtCore.QTimer(app)
    tick.timeout.connect(lambda: None)
    tick.start(250)
    return finish


if __name__ == "__main__":
    main()
