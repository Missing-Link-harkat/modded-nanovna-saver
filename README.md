# Muokattu NanoVNA-Saver (Missing-Link)

Missing-Link Oy:n muokkaama versio [NanoVNA-Saverista](https://github.com/NanoVNA-Saver/nanovna-saver).
Käytetään LiteVNA 64 / Zeenko ZN-406 -laitteen PC-ohjelmistona
([litevna-openvna](https://github.com/Missing-LinkZeroOne/litevna-openvna)).
Alkuperäinen ylävirran ohje: [README.rst](README.rst).

## Pohja ja omat muutokset

Pohja on ylävirran commit `c42ddc4` (v0.7.3 + 14 committia, 30.11.2025), ja koko ylävirran historia on
tallessa. Omat muutokset ovat sen päällä erillisinä committeina:

| muutos | tiedostot |
|---|---|
| Segmentoitu pyyhkäisy: pitkä pyyhkäisy kehyksinä halutulla resoluutiolla | `Windows/CustomSweepWindow.py`, `Controls/SweepControl.py`, `NanoVNASaver.py` |
| VSWR-loki: jokainen piste CSV:ksi hakemistoon `segmented_sweeps/` | `SweepLogger.py` |
| MCP-tuki: Saverin toiminnot LLM-mallien käyttöön | `MCP/`, `Hardware/VirtualVNA.py`, [docs/MCP.md](docs/MCP.md) |

Näet kaikki omat muutokset komennolla:

```bash
git log --oneline c42ddc4..HEAD
```

### Ylävirran päivitysten tuonti

```bash
git remote add upstream https://github.com/NanoVNA-Saver/nanovna-saver.git
git fetch upstream
git merge upstream/main      # ratkaise konfliktit, aja testit
```

## Asennus

Python 3.10–3.13. Riippuvuudet ovat `pyproject.toml`:ssa, erillistä `requirements.txt`:iä ei ole.

```bash
git clone https://github.com/Missing-Link-harkat/modded-nanovna-saver.git
cd modded-nanovna-saver
python -m venv .venv
.venv\Scripts\activate             # Windows
source .venv/bin/activate          # Linux / macOS
pip install -e ".[mcp]"            # ilman MCP-tukea: pip install -e .
```

Käynnistys:

```bash
python -m NanoVNASaver
```

Hyödyllisiä valitsimia:
- `--virtual`: simuloitu laite ilman rautaa
- `--mcp http`: MCP-palvelin heti päälle (ks. [docs/MCP.md](docs/MCP.md))
- `-d`: debug-loki

## Testit

```bash
pip install pytest httpx
python -m pytest
```

Linuxissa ilman näyttöä aseta ensin `QT_QPA_PLATFORM=offscreen`. GitHub Actions ajaa testit
Python-versioilla 3.11–3.13 (`.github/workflows/tests.yml`).

## Lisenssi

NanoVNA-Saver on **GPL-3.0-or-later**, joten koko tämä muokattu versio, MCP-koodi mukaan lukien, on
GPL-3.0-or-later ([licenses/LICENSE.txt](licenses/LICENSE.txt)). Valinnainen riippuvuus, MCP:n Python-SDK (`mcp`), on MIT-lisensoitu,
ja MIT on yhteensopiva GPL-3.0:n kanssa. Jos levität ohjelmaa muutettuna, lähdekoodi on annettava saataville
GPL:n ehdoin.
