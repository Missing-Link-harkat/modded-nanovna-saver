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
- `tests/test_mcp_panel.py` käy AI-paneelin läpi oikeassa pääikkunassa (palvelinnappi,
  Kopioi, "Salli AI-ohjaus", tila AI:n toimiessa, STOP, Ctrl+Shift+S, loki).
  `MCP_PANEL_SHOTS=<kansio>` tallentaa paneelista kuvan jokaisessa vaiheessa
  (offscreen-tilassa lisää `QT_QPA_FONTDIR`, esim. `C:/Windows/Fonts`, muuten tekstit
  ovat laatikoita).

## Arviointisarja: kuinka usein malli onnistuu

`tests/mcp_eval/` mittaa oikealla mallilla, kuinka usein se saa tehtävän oikein näillä
työkaluilla. Jokainen ajo käynnistää puhtaan ikkunattoman Saverin virtuaalilaitteella,
antaa mallille yhden tehtävän ja kaikki MCP-työkalut, ja tarkistaa vastauksen.

| tehtävä | mitä kysytään | oikea vastaus | toleranssi |
|---|---|---|---|
| `antenni_resonanssi` | antennin resonanssi 3–30 MHz | 14,2 MHz | ±1 % |
| `antenni_vswr` | pienin VSWR | 1,19 | ±0,05 |
| `suodatin_keskitaajuus` | suodattimen keskitaajuus 100–200 MHz | 145 MHz | ±1 % |
| `suodatin_kaista` | −3 dB:n kaistanleveys | 11,66 MHz | ±5 % |
| `suodatin_vaimennus` | S21 taajuudella 145 MHz | −0,45 dB | ±0,2 dB |
| `markkeri_resonanssiin` | markkeri 1 resonanssiin (tarkistetaan Saverin tilasta) | 14,2 MHz | ±1 % |

Oikeat vastaukset lasketaan virtuaalilaitteen mallista (`VirtualVNA.model`), ei kirjoiteta
käsin. Malli vastaa rivillä `VASTAUS: <luku> <yksikkö>`.

```
python tests/mcp_eval/run_eval.py --model qwen3-coder:30b \
    --base-url http://<ollama>:11434 --n 10 --jobs 3 --out tulokset.jsonl
python tests/mcp_eval/run_eval.py --summary tulokset.jsonl
```

- `--backend ollama` (oletus) käyttää Ollaman omaa rajapintaa ja asettaa kontekstin
  (`--num-ctx`, oletus 32768), koska 41 työkalun kuvaukset eivät mahdu 4k:n oletukseen.
  `--backend openai` + `--base-url …/v1` + `--api-key-env <muuttuja>` toimii minkä tahansa
  OpenAI-yhteensopivan rajapinnan kanssa (avain luetaan ympäristömuuttujasta).
- Jokainen ajo tallentuu JSONL-riviksi: malli, tehtävä, vastaus, kutsutut työkalut,
  vuorot, tokenit ja kesto.
- Tulos on **kolme tilaa**: `pass`, `fail` (malli vastasi väärin tai ei lainkaan) ja
  `error` (ajo itse hajosi: mallipalvelin, Saver tai aikaraja). Virhe-ajot raportoidaan
  erikseen eikä niitä lasketa mallin onnistumisiin tai epäonnistumisiin.
- Raportoi aste (esim. 8/10), ei kyllä/ei. n = 10 ei erota 90 %:a 100 %:sta.
- `--jobs` > 1 ajaa ajoja rinnakkain (kukin oma Saver). Kesto sisältää silloin myös
  mallipalvelimen jonon, joten kestoja ei voi verrata eri `--jobs`-arvoilla.
- `tests/mcp_eval/test_eval_tasks.py` (CI) tarkistaa oikeat vastaukset ja jäsentimen
  ilman mallia.

## Ei vielä tehty

- Arviointisarjan ajo pilvimallilla (ajuri tukee, avain puuttuu) ja kalibrointitehtävä,
  jossa vahvistus simuloidaan.
- Kaistataulukon muokkaus, mittaustiedostoihin perustuvat kalibrointistandardit ja
  kalibrointisarjojen tallennus (rajattu pois, ks. `inventory.py`).
- Oikealla LiteVNA:lla todentaminen, joka odottaa laitteen tunnistusta (#19).
