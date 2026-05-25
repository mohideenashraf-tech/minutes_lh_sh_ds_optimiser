# Minutes LH / SH / DS Optimiser

Spillover milk-run optimizer: **Source Grid + XD Grid** demand from live **Google Sheets Raw_1**, OR-Tools routing, web UI.

## Repository

- **GitHub:** https://github.com/mohideenashraf-tech/minutes_lh_sh_ds_optimiser
- **Render (when deployed):** https://spillover-milkrun-optimizer.onrender.com

## Project layout

```
minutes-lh-sh-ds-optimiser/
├── app/                      # FastAPI application
│   ├── main.py               # Routes & API
│   ├── static/index.html     # Web UI
│   └── spillover/            # Solver & demand engine
│       ├── gsheets_client.py # Google Sheets API (Raw_1 + static ref)
│       ├── data_loader.py    # Source Grid + XD Grid pivots from Raw_1
│       ├── landing_windows.py
│       ├── pipeline.py
│       ├── solver_core.py    # OR-Tools (regenerate via scripts/)
│       └── static_ref.py
├── data/
│   ├── sample.csv            # Bundled Raw_1 sample (~12 MB)
│   ├── static_reference_cache.xlsx
│   └── uploads/              # Legacy (unused; sheets-only)
├── scripts/
│   ├── build_solver_core.py  # Patch solver from spillover_planner_extracted.py
│   └── fix_html.py
├── docs/                     # Architecture & deploy notes
├── .github/                  # Collaboration templates
├── render.yaml               # Render Blueprint
├── requirements.txt
└── run_server.bat            # Local dev (Windows)
```

## Quick start (local)

```powershell
cd minutes-lh-sh-ds-optimiser
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

Open http://127.0.0.1:8002

## Environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Required on Render.** Full JSON for a Google service account with read access to both spreadsheets |
| `SPILLOVER_SHEET_URL` | Raw_1 workbook URL (optional; has default) |
| `SPILLOVER_STATIC_SHEET_URL` | Static lat/lon workbook URL (optional; has default) |
| `ORS_API_KEY` | OpenRouteService key (optional; Haversine fallback) |
| `SPILLOVER_MAX_PAIR_API_CALLS` | Max pair ORS calls (default 300 local, 50 on Render) |

Share both Google Sheets with the service account email (`client_email` in the JSON).

## Regenerate solver core

After editing `spillover_planner_extracted.py`:

```powershell
python scripts/build_solver_core.py
```

## Collaborators

See [docs/COLLABORATION.md](docs/COLLABORATION.md) for adding team members on GitHub.
