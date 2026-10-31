# MCP: NanoVNA-Saver LLM-mallien käyttöön

Saverin sisällä toimii [MCP](https://modelcontextprotocol.io)-palvelin. Sen kautta LLM-malli
(Claude Code, Claude Desktop, paikallinen malli MCP-asiakkaan kautta) voi käyttää
Saverin toimintoja: kytkeä laitteen, asettaa ja ajaa pyyhkäisyn, lukea tulokset, ottaa
kaaviokuvia, ajaa analyyseja ja kalibroida ihmisen kanssa.

Epic: Missing-LinkZeroOne/litevna-openvna#87 (issuet #88–#94).

## Asennus

```bash
pip install -e ".[mcp]"        # tai: pip install "mcp>=1.12,<3" uvicorn
```

Ilman `mcp`-pakettia Saver toimii kuten ennenkin. AI-paneelissa näkyy silloin asennusohje.

## Käyttö

### A. Saver auki, malli ohjaa (suositus)

1. Käynnistä Saver tavalliseen tapaan. Vasemmassa sarakkeessa on **AI (MCP)** -paneeli.
2. Paina **Käynnistä MCP-palvelin**. Palvelin kuuntelee osoitteessa `http://127.0.0.1:8765/mcp`.
   Tunniste (token) tallentuu tiedostoon `~/.nanovna-saver/mcp.json`.
3. Paina **Kopioi**. Leikepöydälle tulee Claude Code -komento:
   ```bash
   claude mcp add --transport http nanovna-saver http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
   ```
4. Ruksaa **Salli AI-ohjaus**. Ilman ruksia malli voi vain lukea tietoja.

Komentoriviltä sama käynnistyy valitsimilla `python -m NanoVNASaver --mcp http [--mcp-port 8765] [--mcp-allow]`.

Claude Desktop ja muut pelkkää stdio-yhteyttä tukevat asiakkaat saa kytkettyä HTTP-palvelimeen
yleisellä sillalla, esim. `npx mcp-remote http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"`.
Toinen vaihtoehto on tila B.

### B. Ikkunaton (asiakas käynnistää Saverin)

```json
{
  "mcpServers": {
    "nanovna-saver": {
      "command": "python",
      "args": ["-m", "NanoVNASaver", "--headless", "--mcp", "stdio"],
      "env": {"PYTHONPATH": "/polku/modded-nanovna-saver/src"}
    }
  }
}
```

Ikkunattomassa tilassa AI-ohjaus on sallittu, koska käyttäjä käynnisti Saverin
nimenomaan mallia varten. Tiedostojen tallennus kysytään silloin asiakkaan kautta
(elicitation). Jos asiakas ei tue kysymistä, tallennus estetään.

### C. Ilman laitetta: virtuaalilaite

`--virtual` lisää porttilistaan laitteen `virtual`. Se simuloi antennia, jonka resonanssi
on 14,2 MHz (S11), ja 145 MHz:n kaistanpäästösuodatinta (S21). Kun valitsimelle antaa
tiedoston (`--virtual mittaus.s2p`), virtuaalilaite toistaa sitä. Virtuaalilaitteella voi kehittää ja
testata, vaikka oikea laite ei vielä tunnistu (#19).

```bash
python -m NanoVNASaver --virtual --mcp http --mcp-allow
```

## Turvallisuus

| taso | mitä | ehto |
|---|---|---|
| **read** | tilan ja datan lukeminen, kaaviot, analyysit | aina, kun palvelin on käynnissä |
| **act** | kytkentä, pyyhkäisy, asetukset, markkerit, kalibrointi | käyttäjä on sallinut AI-ohjauksen |
| **persist** | tiedostojen tallennus (Touchstone, .cal) | lisäksi käyttäjän kuittaus joka kerta |
| — | firmware, flashaus, laitteen asetusmuistit | ei MCP:ssä lainkaan |

- **STOP** (paneelin punainen painike tai Ctrl+Shift+S) pysäyttää pyyhkäisyn ja ottaa
  AI-ohjauksen heti pois. Ohjaus pitää sallia uudelleen.
- Kun malli ohjaa, paneelissa näkyy "AI ohjaa: <työkalu>". Kaikki kutsut kirjataan paneelin
  lokiin ja tiedostoon `~/.nanovna-saver/mcp-log.jsonl`.
- Vain yksi muuttava toiminto voi olla kerrallaan käynnissä. Jos käyttäjän oma pyyhkäisy
  on käynnissä, malli saa vastauksen "odota".
- Tiedostoja luetaan ja kirjoitetaan vain työhakemistossa `~/NanoVNASaver-MCP`
  (valitsin `--mcp-dir`).
- HTTP kuuntelee vain osoitteessa 127.0.0.1 ja vaatii tunnisteen. Selainpyynnöt muista
  origineista hylätään.
- Kalibroinnissa malli ei voi ohittaa ihmistä: jokainen standardi kysytään erikseen
  ("kytke SHORT"), ja mittaus alkaa vasta kuittauksen jälkeen.

## Mitä mallilla on käytössä

**Työkalut** (41) generoidaan toimintorekisteristä (`src/NanoVNASaver/MCP/api.py`, `@action`).
Ryhmät:

- **device**: `device_list_ports`, `device_connect`, `device_disconnect`, `device_info`,
  `device_set_datapoints`, `device_set_bandwidth`, `device_set_validation`, `device_screenshot`
- **sweep**: `sweep_get`, `sweep_configure`, `band_list`, `sweep_set_band`,
  `sweep_set_attenuator`, `sweep_run`, `sweep_stop`, `sweep_segmented`
- **data**: `data_summary`, `data_points`, `data_at`, `reference_set`, `reference_reset`
- **markers**: `marker_list`, `marker_set`, `marker_add`, `marker_remove`
- **analysis**: `analysis_list`, `analysis_run`, `tdr_result`
- **charts**: `chart_list`, `chart_image` (PNG)
- **files**: `file_list`, `file_load_sweep`, `file_load_reference`, `file_export_touchstone`
- **calibration**: `calibration_status`, `calibration_capture`, `calibration_apply`,
  `calibration_reset`, `calibration_set_offset_delay`, `calibration_load`, `calibration_save`

Työkalut palauttavat **yhteenvedon** (esim. pienin VSWR, resonanssit, VSWR ≤ 2 -kaistat).
Täysi data on saatavilla **resursseina**, jotta mallin konteksti ei täyty tuhansista pisteistä:

- `saver://sweep/latest.s1p`, `saver://sweep/latest.s2p`: koko mittaus Touchstone-muodossa
- `saver://sweep/summary`, `saver://sweep/settings`, `saver://device`,
  `saver://calibration`, `saver://markers` (JSON)
- `saver://ai-log`: mitä malli on tehnyt tässä istunnossa

**Promptit** ovat valmiita mittausreseptejä: `antenna(start_hz, stop_hz)`,
`filter(start_hz, stop_hz)`, `cable_length`, `compare`.

## Kehittäjälle

- Uusi toiminto lisätään metodina `SaverAPI`:in ja merkitään `@action`. Docstring on se
  teksti, jonka malli lukee, joten kirjoita siihen mitä toiminto tekee, yksiköt ja mitä se
  palauttaa.
- Jokainen GUI-painike on kirjattu tiedostoon `MCP/inventory.py`: joko MCP-toiminto tai
  perusteltu poisjättö. `tests/test_mcp_inventory.py` kaatuu, jos uudelle painikkeelle ei
  ole tehty päätöstä.
- Toiminnot ajetaan Qt:n pääsäikeessä (`MCP/bridge.py`). Palvelimen säikeestä ei saa koskea
  widgetteihin suoraan.
- `tests/test_mcp_e2e.py` ajaa MCP-asiakkaan virtuaalilaitetta vastaan (yhdistäminen,
  pyyhkäisy, kuvat, kuittaukset, kalibrointi, STOP).

## Ei vielä tehty

- Arviointisarja oikealla mallilla: tehtävät, joissa mitataan kuinka usein malli onnistuu
  (n ≥ 10 per tehtävä). Kuuluu #93:een.
- Kaistataulukon muokkaus, mittaustiedostoihin perustuvat kalibrointistandardit ja
  kalibrointisarjojen tallennus (rajattu pois, ks. `inventory.py`).
- Oikealla LiteVNA:lla todentaminen, joka odottaa laitteen tunnistusta (#19).
