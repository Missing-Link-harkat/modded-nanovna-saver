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
"""Measurement recipes as MCP prompts.

A prompt is a ready-made instruction the user picks in the client
("/antenna" etc.). Each one names the tools in the order a careful
operator would use them, and what to report.
"""

from typing import Any


def antenna(start_hz: int, stop_hz: int, target_vswr: float = 2.0) -> str:
    return f"""\
Mittaa antenni NanoVNA-Saverilla ja raportoi tulos.

1. device_info; jos laitetta ei ole kytketty: device_list_ports ja
   device_connect.
2. calibration_status. Jos kalibrointia ei ole tai sen alue ei kata
   {start_hz}-{stop_hz} Hz: sweep_configure(start_hz={start_hz},
   stop_hz={stop_hz}, mode="single"), sitten calibration_reset ja
   calibration_capture short, open, load (käyttäjä kytkee standardit
   kaapelin päähän, ei laitteeseen) ja calibration_apply.
3. Pyydä käyttäjää kytkemään antenni. sweep_configure(start_hz={start_hz},
   stop_hz={stop_hz}, mode="single") ja sweep_run.
4. Raportoi data_summarysta: resonanssitaajuus, pienin VSWR ja
   paluuvaimennus siinä, impedanssi R + jX, kaistat joilla
   VSWR <= {target_vswr}. Liitä chart_image("s11.smith") ja
   chart_image("s11.vswr").
5. Jos resonanssi on alueen ulkopuolella tai VSWR > {target_vswr}
   kaikkialla, kerro mihin suuntaan antennia pitää säätää (liian pitkä
   -> resonanssi alempana) ja ehdota laajempaa pyyhkäisyä.
"""


def filter_response(start_hz: int, stop_hz: int) -> str:
    return f"""\
Mittaa suodattimen läpäisy (S21) ja heijastus (S11).

1. device_info / device_connect kuten tarpeen.
2. 2-porttikalibrointi alueelle {start_hz}-{stop_hz} Hz:
   calibration_capture short, open, load, through (ja isolation jos
   tarkkuutta tarvitaan), sitten calibration_apply.
3. Suodatin porttien 1 ja 2 väliin, sweep_configure(start_hz={start_hz},
   stop_hz={stop_hz}, segments=4), sweep_run.
4. analysis_list ja sopiva analysis_run (Low-pass / High-pass /
   Band-pass / Band-stop filter). Raportoi -3 dB -pisteet,
   läpäisyvaimennus, estokaistan vaimennus ja S11:n paluuvaimennus
   läpäisykaistalla. Liitä chart_image("s21.log_mag").
"""


def cable_length(velocity_factor: float = 0.66) -> str:
    return f"""\
Mittaa kaapelin pituus TDR:llä.

1. Laaja pyyhkäisy: sweep_configure(start_hz=50000,
   stop_hz=900000000, segments=4), kaapelin toinen pää auki.
2. sweep_run, sitten tdr_result. Muistuta käyttäjää, että
   nopeuskerroin (nyt oletettu {velocity_factor}) asetetaan TDR-ikkunassa
   kaapelityypin mukaan.
3. Raportoi pituus ja chart_image("tdr").
"""


def compare_reference() -> str:
    return """\
Vertaa kahta mittausta.

1. sweep_run ensimmäiselle kohteelle ja reference_set.
2. Pyydä käyttäjää vaihtamaan kohde, sweep_run.
3. Vertaa data_summary-tuloksia (resonanssi, VSWR, kaistat) ja kerro
   erot. Liitä chart_image("s11.vswr"), jossa referenssi näkyy.
"""


PROMPTS = {
    "antenna": (antenna, "Antennin mittaus: resonanssi, VSWR, kaista"),
    "filter": (filter_response, "Suodattimen S21/S11-mittaus"),
    "cable_length": (cable_length, "Kaapelin pituus TDR:llä"),
    "compare": (compare_reference, "Kahden mittauksen vertailu"),
}


def register_prompts(server: Any) -> None:
    for name, (func, description) in PROMPTS.items():
        server.prompt(name=name, description=description)(func)
