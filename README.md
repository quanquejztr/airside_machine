# Airside Machine

A single-player airline tycoon: buy or lease aircraft, open routes, schedule rotations, bid on gates and runway slots, and compete with AI carriers on a continuous game clock.

## Requirements

- **Python 3.10+** (3.11/3.12/3.14 fine)
- pip packages in `requirements.txt` (currently only `rich` for the CLI)
- A modern browser for the overlay UI
- **No API keys or paid map accounts** for the normal play path (Leaflet + OpenStreetMap tiles from public CDNs)

## Secrets / config

You do **not** need a `.env` or any third-party keys to install or run.

| Item | Needed? | Notes |
|------|---------|--------|
| Map / geocoding API key | No | Overlay uses Leaflet + OSM; Google Maps was removed |
| Database password | No | Local SQLite file under `db/` |
| `.env` | Optional / unused | Repo gitignores `.env` and `.env.*`; a future feature could use `.env.example` — none is required today |

Optional env vars some developers use (all optional):

- `AIRLINE_SIM_NO_LIVE_SCREEN` / `AIRLINE_SIM_LIVE_SCREEN` — CLI flight-board live-screen behavior

## Installation

```bash
git clone <your-repo-url> "airline_sim game"
cd "airline_sim game"

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

First launch creates and seeds `db/airline_sim.db` from CSV catalogs in `data/` if no save exists yet. Schema migrations run automatically on later starts.

## Run

```bash
# Terminal menus (CLI)
python3 main.py

# Overlay UI (map + HUD + dock windows) — recommended
python3 main.py --ui
```

Default overlay URL (binds **127.0.0.1 only**):

```
http://127.0.0.1:8765/
```

Custom port:

```bash
python3 main.py --ui 8877
```

After Python or JS changes, restart `--ui` and hard-refresh the browser (`Cmd+Shift+R` / `Ctrl+Shift+R`).

## What you manage

- **Airline** — name, callsign, hub
- **Fleet** — buy or lease (term in weeks); Fleet panel shows Owned/Leased and weeks left
- **Routes & fares** — open city pairs, set Y/W/J/F tickets
- **Schedule** — weekly rotations per tail; ferry reposition when needed
- **Gates & slots** — stand auctions vs weekly runway movement quotas (separate systems)
- **Bank** — loans and credit
- **Books** — week P&L (revenue, fuel, leases, fees, net) and fuel desk (hedge / reserve / dip)
- **Clock** — pause / 1× / 2× / 4× / 20× / 60×; weeks settle automatically

## Project layout

| Path | Role |
|------|------|
| `main.py` | Entry point (CLI + optional UI server) |
| `engine/` | Game logic (clock, demand, scheduling, settlement, AI, …) |
| `db/` | Schema, migrations, seed, live SQLite save |
| `data/` | CSV catalogs + `competitors.json` |
| `web/` | Overlay UI (`index.html`, `app.js`, `styles.css`) |
| `server/` | Localhost HTTP API for the overlay |
| `ui/` | Rich CLI panels / commands |
| `tests/` | Smoke / lifecycle tests |
| `docs/phase_implementation_detail.md` | What each build phase added |

## Saves

Player progress lives in `db/airline_sim.db` (plus `-wal` / `-shm` while the game runs). Those files are **gitignored** so clones start fresh and broken saves are not committed.

Tips:

- Quit the game cleanly before copying the DB.
- Avoid editing the same live save from two Macs via iCloud at once — SQLite + cloud sync often corrupts the file.
- Keep a closed backup copy if you care about a particular airline.

## Tests

```bash
source .venv/bin/activate
python3 -m unittest discover -s tests -v
```

## Authors

- Tri Cuong Luong — [luongtricuong2409@gmail.com](mailto:luongtricuong2409@gmail.com)

## License / status

Personal / WIP simulation. Phases 0–12 are built; marketing (13) and onboarding (14) are not. See `docs/phase_implementation_detail.md`.
