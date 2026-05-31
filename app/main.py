"""Spillover milk-run web app (Source Grid + XD Grid from live grid pendency sheet)."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.spillover.data_loader import fetch_live_raw, list_source_warehouses, load_raw_file
from app.spillover.gsheets_client import (
    RAW_SHEET,
    SHEET_URL,
    check_sheets_connection,
    invalidate_raw_cache,
    sheets_configured,
)
from app.spillover.landing_windows import DEFAULT_LANDING, from_api_dict
from app.spillover.pipeline import FLEET_SIZES, run_optimizations
from app.spillover.static_ref import STATIC_CACHE, build_cache_from_csv

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
SAMPLE_CSV = DATA_DIR / "sample.csv"

DEFAULT_FLEET = {"07FT": 9, "08FT": 9, "10FT": 9, "14FT": 9, "17FT": 0, "20FT": 0, "22FT": 0}
DEFAULT_MAX_DOCKS = 9
DEFAULT_MAX_HOPS = 2
DEFAULT_MAX_TOTES = 40.0
DEFAULT_DBD_PAST_HRS = 4.0
DEFAULT_DBD_FUTURE_HRS = 12.0
LIVE_UPLOAD_ID = "live"

BUILD_ID = "google-sheets-raw1-v3-multi-source"

app = FastAPI(title="Spillover Milkrun Optimizer", version="1.2.0")

static_dir = APP_DIR / "static"
INDEX_HTML = static_dir / "index.html"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


class LandingWindowBody(BaseModel):
    day_start: str = "10:00"
    day_end: str = "18:00"
    night_start: str = "22:00"
    night_end: str = "04:00"
    active_part: str = "Day"
    planning_date: str | None = None


class OptimizeRequest(BaseModel):
    upload_id: str = LIVE_UPLOAD_ID
    source_warehouses: list[str] = Field(default_factory=list)
    source_warehouse: str | None = None
    fleet: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_FLEET))
    max_source_km: float = 80
    max_totes: float = Field(DEFAULT_MAX_TOTES, ge=0)
    dbd_past_cutoff_hrs: float = Field(DEFAULT_DBD_PAST_HRS, ge=0, le=72)
    dbd_future_cutoff_hrs: float = Field(DEFAULT_DBD_FUTURE_HRS, ge=0, le=72)
    max_docks: int = Field(DEFAULT_MAX_DOCKS, ge=1, le=50)
    max_hops: int = Field(DEFAULT_MAX_HOPS, ge=1, le=5)
    landing: LandingWindowBody = Field(default_factory=LandingWindowBody)

    @model_validator(mode="after")
    def normalize_sources(self) -> OptimizeRequest:
        sources = [s.strip() for s in self.source_warehouses if s.strip()]
        if self.source_warehouse and self.source_warehouse.strip():
            legacy = self.source_warehouse.strip()
            if legacy not in sources:
                sources.insert(0, legacy)
        if not sources:
            raise ValueError("Select at least one source warehouse.")
        self.source_warehouses = sources
        return self


def _ensure_sample() -> Path:
    if not STATIC_CACHE.exists() and SAMPLE_CSV.exists():
        build_cache_from_csv(SAMPLE_CSV)
    return SAMPLE_CSV


def _resolve_csv(upload_id: str | None) -> Path:
    if upload_id in (None, "", "sample"):
        path = _ensure_sample()
        if not path.exists():
            raise HTTPException(404, "Sample CSV not found. Upload a file or configure Google Sheets.")
        return path
    path = UPLOAD_DIR / f"{upload_id}.csv"
    if not path.exists():
        raise HTTPException(404, f"Upload '{upload_id}' not found.")
    return path


def _is_live_upload(upload_id: str | None) -> bool:
    return upload_id in (None, "", LIVE_UPLOAD_ID)


def _load_raw_dataframe(*, upload_id: str = LIVE_UPLOAD_ID, refresh: bool = False) -> pd.DataFrame:
    if not _is_live_upload(upload_id):
        return load_raw_file(_resolve_csv(upload_id))

    if not sheets_configured():
        if SAMPLE_CSV.exists():
            return load_raw_file(SAMPLE_CSV)
        raise HTTPException(
            503,
            "Google Sheets not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON and share the pendency workbook.",
        )

    try:
        return fetch_live_raw(use_cache=not refresh)
    except Exception as exc:
        if SAMPLE_CSV.exists() and upload_id == "sample":
            return load_raw_file(SAMPLE_CSV)
        raise HTTPException(502, f"Could not fetch live sheet '{RAW_SHEET}': {exc}") from exc


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
    html = INDEX_HTML.read_text(encoding="utf-8") if INDEX_HTML.exists() else ""
    return {
        "build_id": BUILD_ID,
        "has_live_data_section": "liveDataStatus" in html,
        "has_source_checkboxes": "sourceCheckboxes" in html,
        "has_hop_landings": "Hop landings" in html,
        "has_dock_chart": "dockChartPanel" in html,
        "data_source": "google_sheets",
    }


@app.get("/api/config")
async def config():
    sheets = check_sheets_connection()
    using_sample_fallback = not sheets.get("configured") and SAMPLE_CSV.exists()
    return {
        "data_source": "google_sheets" if sheets.get("configured") else "sample_fallback",
        "fleet_sizes": FLEET_SIZES,
        "default_fleet": DEFAULT_FLEET,
        "has_sample": SAMPLE_CSV.exists(),
        "sheets_configured": sheets.get("configured", False),
        "raw_sheet": RAW_SHEET,
        "sheet_url": SHEET_URL,
        "using_sample_fallback": using_sample_fallback,
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
        "ok": True,
        "build_id": BUILD_ID,
        "data_source": "google_sheets",
        "sheets": sheets,
        "raw_sheet": RAW_SHEET,
        "sample_fallback": SAMPLE_CSV.exists(),
        "static_cache": STATIC_CACHE.exists(),
        "ors_configured": bool(os.environ.get("ORS_API_KEY")),
    }


@app.get("/api/live-data")
async def live_data(refresh: bool = False):
    """Status and row counts for the live grid pendency Raw_1 tab."""
    if not sheets_configured():
        if SAMPLE_CSV.exists():
            df = load_raw_file(SAMPLE_CSV)
            return {
                "ok": True,
                "mode": "sample_fallback",
                "upload_id": "sample",
                "rows": len(df),
                "sources": len(list_source_warehouses(df)),
                "message": "Google Sheets not configured — using bundled sample data.",
            }
        raise HTTPException(503, "Google Sheets not configured and no sample file available.")

    try:
        df = _load_raw_dataframe(upload_id=LIVE_UPLOAD_ID, refresh=refresh)
        conn = check_sheets_connection()
        return {
            "ok": True,
            "mode": "live",
            "upload_id": LIVE_UPLOAD_ID,
            "rows": len(df),
            "sources": len(list_source_warehouses(df)),
            "raw_sheet": RAW_SHEET,
            "spreadsheet_title": conn.get("spreadsheet_title"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "refreshed": refresh,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/refresh")
async def refresh_live_data():
    """Force-refresh Raw_1 from Google Sheets (bypass cache)."""
    invalidate_raw_cache()
    return await live_data(refresh=True)


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
    """Optional fallback: upload Raw_1 CSV/Excel when live sheet is unavailable."""
    if not file.filename:
        raise HTTPException(400, "No file selected.")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".csv", ".xlsx", ".xls", ".xlsm"}:
        raise HTTPException(400, "Upload CSV or Excel (.xlsx).")

    upload_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{upload_id}.csv"
    raw_bytes = await file.read()

    if ext == ".csv":
        dest.write_bytes(raw_bytes)
    else:
        import io

        df = pd.read_excel(io.BytesIO(raw_bytes), engine="openpyxl")
        df.to_csv(dest, index=False)

    try:
        df = load_raw_file(dest)
        sources = list_source_warehouses(df)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not parse file: {exc}") from exc

    build_cache_from_csv(dest)
    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "rows": len(df),
        "sources": sources,
    }


@app.get("/api/sources")
async def sources(upload_id: str = LIVE_UPLOAD_ID, refresh: bool = False):
    df = _load_raw_dataframe(upload_id=upload_id, refresh=refresh)
    return {
        "upload_id": upload_id if not _is_live_upload(upload_id) else LIVE_UPLOAD_ID,
        "sources": list_source_warehouses(df),
    }


@app.post("/api/optimize")
async def optimize(body: OptimizeRequest):
    fleet = {k: int(body.fleet.get(k, 0)) for k in FLEET_SIZES}
    landing = from_api_dict(body.landing.model_dump())
    try:
        landing.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if _is_live_upload(body.upload_id):
        raw_df = _load_raw_dataframe(upload_id=LIVE_UPLOAD_ID, refresh=False)
        csv_path = None
    else:
        csv_path = _resolve_csv(body.upload_id)
        raw_df = None

    try:
        result = run_optimizations(
            body.source_warehouses,
            fleet,
            csv_path=csv_path,
            raw_df=raw_df,
            max_source_km=body.max_source_km,
            max_totes=body.max_totes,
            dbd_past_cutoff_hrs=body.dbd_past_cutoff_hrs,
            dbd_future_cutoff_hrs=body.dbd_future_cutoff_hrs,
            max_docks=body.max_docks,
            max_hops=body.max_hops,
            landing=landing,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(content=json.loads(json.dumps(result, default=str)))


@app.post("/api/optimize-form")
async def optimize_form(
    source_warehouse: str = Form(...),
    upload_id: str = Form(LIVE_UPLOAD_ID),
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
        upload_id=upload_id,
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
