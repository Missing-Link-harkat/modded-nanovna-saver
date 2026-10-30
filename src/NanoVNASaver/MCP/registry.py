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
"""Action registry: every Saver function exposed to automation.

The registry is the single source of truth for what an automation client
(MCP, scripts, tests) can do. It does not import Qt so that it can be
inspected and tested without a display. MCP tools are generated from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Risk(Enum):
    """How much an action may change.

    READ     reads state only.
    ACT      changes the running session (sweep range, markers, ...),
             nothing survives a restart.
    PERSIST  writes files or other state that outlives the session;
             needs a human confirmation for every call.

    Firmware, flashing and device configuration storage are deliberately
    not in any level: they are never exposed.
    """

    READ = "read"
    ACT = "act"
    PERSIST = "persist"


@dataclass(frozen=True)
class Action:
    name: str
    func: Callable[..., Any]
    description: str
    risk: Risk
    category: str
    # GUI controls this action is the equivalent of, as "Window: control".
    # Used by the coverage test and the inventory document.
    gui: tuple[str, ...] = ()
    # Text shown to the human before the action runs. Actions with
    # risk PERSIST always need one; others may (calibration standards).
    confirm: Callable[..., str] | None = None
    # Seconds the action may take; sweeps take long.
    timeout: float = 30.0
    tags: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, Action] = {}


def action(
    name: str,
    *,
    risk: Risk,
    category: str,
    gui: tuple[str, ...] = (),
    confirm: Callable[..., str] | None = None,
    timeout: float = 30.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method of SaverAPI as an action.

    The docstring of the method is the description the model sees, so
    write it for the model: what it does, units, what it returns.
    """

    def register(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"duplicate action {name}")
        if risk is Risk.PERSIST and confirm is None:
            raise ValueError(f"{name}: PERSIST action needs confirm text")
        doc = (func.__doc__ or "").strip()
        if not doc:
            raise ValueError(f"{name}: action needs a docstring")
        REGISTRY[name] = Action(
            name=name,
            func=func,
            description=doc,
            risk=risk,
            category=category,
            gui=gui,
            confirm=confirm,
            timeout=timeout,
        )
        return func

    return register


def actions(category: str | None = None) -> list[Action]:
    return [
        a
        for a in REGISTRY.values()
        if category is None or a.category == category
    ]
