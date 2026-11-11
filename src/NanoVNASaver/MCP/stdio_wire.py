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
"""Keep stdout clean for MCP over stdio (litevna-openvna#189).

With ``--mcp stdio`` the client reads JSON-RPC from our stdout, also over
``ssh``. One stray byte there (a print, a Qt or C library message) breaks
the protocol. ``claim_stdout()`` runs first thing in ``main()``, before Qt:
it keeps the real stdout on a private file descriptor for the MCP
transport and points fd 1 and ``sys.stdout`` at stderr, so everything else
ends up in stderr. mcp 2.x does a similar diversion itself, but only once
the transport starts (after the window is built); mcp 1.x does not do it.
"""

import logging
import os
import sys
from typing import BinaryIO

logger = logging.getLogger(__name__)

_wire: BinaryIO | None = None


def claim_stdout() -> BinaryIO | None:
    """Move the MCP wire off fd 1; return it (None if it cannot be done)."""
    global _wire  # pylint: disable=global-statement
    if _wire is not None:
        return _wire
    try:
        sys.stdout.flush()
        private_fd = os.dup(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except (AttributeError, OSError, ValueError):
        os.close(private_fd)
        return None
    _wire = os.fdopen(private_fd, "wb")
    sys.stdout = sys.stderr
    return _wire


def wire() -> BinaryIO | None:
    """The stream claimed by claim_stdout(), or None."""
    return _wire
