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
"""Frame plan of a segmented (high resolution) sweep.

The range start..stop with a fixed step is cut into frames the device can
measure in one sweep. Every point of the plan is start + k*step exactly:
frames do not share edge points, and every frame is measured with its own
point count, so the measured spacing is the requested step
(litevna-openvna#204)."""

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class Frame:
    index: int
    first: int  # index of the frame's first point in the whole plan
    start_hz: int
    points: int
    step_hz: int

    @property
    def stop_hz(self) -> int:
        return self.start_hz + (self.points - 1) * self.step_hz

    def frequencies(self) -> list[int]:
        return [self.start_hz + i * self.step_hz for i in range(self.points)]


@dataclass(frozen=True)
class Plan:
    start_hz: int
    stop_hz: int  # last point of the grid (requested stop rounded down)
    step_hz: int
    frames: tuple[Frame, ...]

    @property
    def points(self) -> int:
        return sum(f.points for f in self.frames)


def plan_frames(
    start_hz: int,
    stop_hz: int,
    step_hz: int,
    max_points: int,
    min_points: int = 11,
) -> Plan:
    """Cut start..stop (step step_hz) into frames of at most max_points.

    The frames are balanced (their sizes differ by at most one point), so
    no frame falls below the device minimum. The last grid point is the
    largest start + k*step that is <= stop. Raises ValueError with a
    message for the user when the request cannot be measured."""
    start_hz, stop_hz, step_hz = int(start_hz), int(stop_hz), int(step_hz)
    if step_hz <= 0:
        raise ValueError("Askeleen (resoluution) pitää olla yli 0 Hz.")
    if start_hz <= 0 or stop_hz <= start_hz:
        raise ValueError("Alkutaajuuden pitää olla yli 0 ja pienempi kuin "
                         "lopputaajuus.")
    if max_points < min_points or min_points < 2:
        raise ValueError(f"Laitteen pistemäärä {min_points}..{max_points} "
                         "ei kelpaa kehyksille.")
    total = (stop_hz - start_hz) // step_hz + 1
    if total < min_points:
        raise ValueError(
            f"Alueella on vain {total} pistettä tällä askeleella; laite "
            f"mittaa vähintään {min_points}. Pienennä askelta."
        )
    count = ceil(total / max_points)
    base, extra = divmod(total, count)
    frames = []
    first = 0
    for i in range(count):
        n = base + (1 if i < extra else 0)
        frames.append(Frame(i, first, start_hz + first * step_hz, n, step_hz))
        first += n
    last = start_hz + (total - 1) * step_hz
    return Plan(start_hz, last, step_hz, tuple(frames))
