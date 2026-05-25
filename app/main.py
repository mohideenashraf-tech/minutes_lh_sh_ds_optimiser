"""Spillover milk-run web app (Source Grid + XD Grid via Google Sheets)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.spillover.data_loader import list_source_warehouses
from app.spillover.gsheets_client import RAW_SHEET, check_sheets_connection, invalidate_raw_cache, sheets_configured
from app.spillover.landing_windows import DEFAULT_LANDING, from_api_dict
from app.spillover.pipeline import FLEET_SIZES, run_optimization

APP_DIR = Path(__file__).resolve().parent

DEFAULT_FLEET = {"07FT": 9, "08FT": 9, "10FT": 9, "14FT": 9, "17FT": 0, "20FT": 0, "22FT": 0}
DEFAULT_MAX_DOCKS = 9
DEFAULT_MAX_HOPS = 2
DEFAULT_MAX_TOTES = 40.0
DEFAULT_DBD_PAST_HRS = 4.0
DEFAULT_DBD_FUTURE_HRS = 12.0

# Bumped when UI/API change — visible in /api/health after deploy
BUILD_ID = "google-sheets-raw1-v2"

app = FastAPI(title="Spillover Milkrun Optimizer", version="1.1.0")

static_dir = APP_DIR / "static"
INDEX_HTML = static_dir / "index.html"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class LandingWindowBody(BaseModel):
    day_start: str = "10:00"
    day_end: str = "18:00"
    night_start: str = "22:00"
    night_end: str = "04:00"
    active_part: str = "Day"
    planning_date: str | None = None


class OptimizeRequest(BaseModel):
    source_warehouse: str
    fleet: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_FLEET))
    max_source_km: float = 80
    max_totes: float = Field(DEFAULT_MAX_TOTES, ge=0)
    dbd_past_cutoff_hrs: float = Field(DEFAULT_DBD_PAST_HRS, ge=0, le=72)
    dbd_future_cutoff_hrs: float = Field(DEFAULT_DBD_FUTURE_HRS, ge=0, le=72)
    max_docks: int = Field(DEFAULT_MAX_DOCKS, ge=1, le=50)
    max_hops: int = Field(DEFAULT_MAX_HOPS, ge=1, le=5)
    landing: LandingWindowBody = Field(default_factory=LandingWindowBody)


def _require_sheets() -> None:
    if not sheets_configured():
        raise HTTPException(
            503,
            "Google Sheets not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON on the server "
            "and share the Raw_1 and static reference spreadsheets with that service account.",
        )


@app.get("/", response_class=HTMLResponse)
async def index():
    if not INDEX_HTML.exists():
        raise HTTPException(500, "UI file missing at app/static/index.html")
    return FileResponse(
        INDEX_HTML,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/ui-version")
async def ui_version():
    """Lets the browser confirm which UI bundle the server is serving."""
    html = INDEX_HTML.read_text(encoding="utf-8") if INDEX_HTML.exists() else ""
    return {
        "commit_hint": "sheets-v1.1",
        "has_live_data_section": "Live data" in html and "csvFile" not in html,
        "data_source": "google_sheets",
    }


@app.get("/api/config")
async def config():
    sheets = check_sheets_connection()
    return {
        "data_source": "google_sheets",
        "raw_sheet": RAW_SHEET,
        "sheets": sheets,
        "fleet_sizes": FLEET_SIZES,
        "default_fleet": DEFAULT_FLEET,
        "landing": DEFAULT_LANDING.to_dict(),
        "default_max_docks": DEFAULT_MAX_DOCKS,
        "default_max_hops": DEFAULT_MAX_HOPS,
        "max_route_hops": 5,
        "ors_interhop_budget": int(os.environ.get("SPILLOVER_MAX_PAIR_API_CALLS", "300")),
        "default_max_totes": DEFAULT_MAX_TOTES,
        "default_dbd_past_hrs": DEFAULT_DBD_PAST_HRS,
        "default_dbd_future_hrs": DEFAULT_DBD_FUTURE_HRS,
    }


@app.get("/api/health")
async def health():
    sheets = check_sheets_connection()
    return {
        "ok": sheets.get("ok", False),
        "build_id": BUILD_ID,
        "data_source": "google_sheets",
        "sheets": sheets,
        "ors_configured": bool(os.environ.get("ORS_API_KEY")),
        "legacy_deploy": sheets.get("ok") is False and "sample" not in str(sheets),
    }


@app.post("/api/refresh-data")
async def refresh_data():
    _require_sheets()
    invalidate_raw_cache()
    return {"ok": True}


@app.get("/api/sources")
async def sources():
    _require_sheets()
    try:
        return {"sources": list_source_warehouses()}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/optimize")
async def optimize(body: OptimizeRequest):
    _require_sheets()
    fleet = {k: int(body.fleet.get(k, 0)) for k in FLEET_SIZES}
    landing = from_api_dict(body.landing.model_dump())
    try:
        landing.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        result = run_optimization(
            body.source_warehouse.strip(),
            fleet,
            max_source_km=body.max_source_km,
            max_totes=body.max_totes,
            dbd_past_cutoff_hrs=body.dbd_past_cutoff_hrs,
            dbd_future_cutoff_hrs=body.dbd_future_cutoff_hrs,
            max_docks=body.max_docks,
            max_hops=body.max_hops,
            landing=landing,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(content=json.loads(json.dumps(result, default=str)))


@app.post("/api/optimize-form")
async def optimize_form(
    source_warehouse: str = Form(...),
    max_source_km: float = Form(80),
    max_totes: float = Form(DEFAULT_MAX_TOTES),
    dbd_past_cutoff_hrs: float = Form(DEFAULT_DBD_PAST_HRS),
    dbd_future_cutoff_hrs: float = Form(DEFAULT_DBD_FUTURE_HRS),
    max_docks: int = Form(DEFAULT_MAX_DOCKS),
    max_hops: int = Form(DEFAULT_MAX_HOPS),
    day_start: str = Form("10:00"),
    day_end: str = Form("18:00"),
    night_start: str = Form("22:00"),
    night_end: str = Form("04:00"),
    active_part: str = Form("Day"),
    fleet_07FT: int = Form(9),
    fleet_08FT: int = Form(9),
    fleet_10FT: int = Form(9),
    fleet_14FT: int = Form(9),
    fleet_17FT: int = Form(0),
    fleet_20FT: int = Form(0),
    fleet_22FT: int = Form(0),
):
    fleet = {
        "07FT": fleet_07FT,
        "08FT": fleet_08FT,
        "10FT": fleet_10FT,
        "14FT": fleet_14FT,
        "17FT": fleet_17FT,
        "20FT": fleet_20FT,
        "22FT": fleet_22FT,
    }
    body = OptimizeRequest(
        source_warehouse=source_warehouse,
        fleet=fleet,
        max_source_km=max_source_km,
        max_totes=max_totes,
        dbd_past_cutoff_hrs=dbd_past_cutoff_hrs,
        dbd_future_cutoff_hrs=dbd_future_cutoff_hrs,
        max_docks=max_docks,
        max_hops=max_hops,
        landing=LandingWindowBody(
            day_start=day_start,
            day_end=day_end,
            night_start=night_start,
            night_end=night_end,
            active_part=active_part,
        ),
    )
    return await optimize(body)
