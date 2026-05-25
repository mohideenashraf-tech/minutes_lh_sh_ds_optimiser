"""Spillover milk-run web app (Source Grid + XD Grid)."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.spillover.data_loader import list_source_warehouses, load_raw_file
from app.spillover.landing_windows import DEFAULT_LANDING, from_api_dict
from app.spillover.pipeline import FLEET_SIZES, run_optimization
from app.spillover.static_ref import STATIC_CACHE, build_cache_from_csv

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
SAMPLE_CSV = DATA_DIR / "sample.csv"

DEFAULT_FLEET = {"07FT": 9, "08FT": 9, "10FT": 9, "14FT": 9, "17FT": 0, "20FT": 0, "22FT": 0}
DEFAULT_MAX_DOCKS = 9
DEFAULT_MAX_HOPS = 2
DEFAULT_MIN_TOTES = 40.0
DEFAULT_DBD_PAST_HRS = 4.0
DEFAULT_DBD_FUTURE_HRS = 12.0

app = FastAPI(title="Spillover Milkrun Optimizer", version="1.0.0")

static_dir = APP_DIR / "static"
INDEX_HTML = static_dir / "index.html"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_sample() -> Path:
    """Ensure bundled sample + static cache exist (production has no local Downloads path)."""
    if not STATIC_CACHE.exists() and SAMPLE_CSV.exists():
        build_cache_from_csv(SAMPLE_CSV)
    return SAMPLE_CSV


def _resolve_csv(upload_id: str | None) -> Path:
    if upload_id in (None, "", "sample"):
        path = _ensure_sample()
        if not path.exists():
            raise HTTPException(404, "Sample CSV not found. Upload a file first.")
        return path
    path = UPLOAD_DIR / f"{upload_id}.csv"
    if not path.exists():
        raise HTTPException(404, f"Upload '{upload_id}' not found.")
    return path


class LandingWindowBody(BaseModel):
    day_start: str = "10:00"
    day_end: str = "18:00"
    night_start: str = "22:00"
    night_end: str = "04:00"
    active_part: str = "Day"
    planning_date: str | None = None


class OptimizeRequest(BaseModel):
    upload_id: str = "sample"
    source_warehouse: str
    fleet: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_FLEET))
    max_source_km: float = 80
    min_totes: float = Field(DEFAULT_MIN_TOTES, ge=0)
    dbd_past_cutoff_hrs: float = Field(DEFAULT_DBD_PAST_HRS, ge=0, le=72)
    dbd_future_cutoff_hrs: float = Field(DEFAULT_DBD_FUTURE_HRS, ge=0, le=72)
    max_docks: int = Field(DEFAULT_MAX_DOCKS, ge=1, le=50)
    max_hops: int = Field(DEFAULT_MAX_HOPS, ge=1, le=5)
    landing: LandingWindowBody = Field(default_factory=LandingWindowBody)


@app.get("/", response_class=HTMLResponse)
async def index():
    _ensure_sample()
    if not INDEX_HTML.exists():
        raise HTTPException(500, "UI file missing at app/static/index.html")
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/api/config")
async def config():
    _ensure_sample()
    return {
        "fleet_sizes": FLEET_SIZES,
        "default_fleet": DEFAULT_FLEET,
        "has_sample": SAMPLE_CSV.exists(),
        "landing": DEFAULT_LANDING.to_dict(),
        "default_max_docks": DEFAULT_MAX_DOCKS,
        "default_max_hops": DEFAULT_MAX_HOPS,
        "max_route_hops": 5,
        "ors_interhop_budget": int(os.environ.get("SPILLOVER_MAX_PAIR_API_CALLS", "300")),
        "default_min_totes": DEFAULT_MIN_TOTES,
        "default_dbd_past_hrs": DEFAULT_DBD_PAST_HRS,
        "default_dbd_future_hrs": DEFAULT_DBD_FUTURE_HRS,
    }


@app.get("/api/health")
async def health():
    import os

    return {
        "ok": True,
        "sample": SAMPLE_CSV.exists(),
        "static_cache": STATIC_CACHE.exists(),
        "ors_configured": bool(os.environ.get("ORS_API_KEY")),
    }


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
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
async def sources(upload_id: str = "sample"):
    path = _resolve_csv(upload_id)
    df = load_raw_file(path)
    return {"upload_id": upload_id, "sources": list_source_warehouses(df)}


@app.post("/api/optimize")
async def optimize(body: OptimizeRequest):
    path = _resolve_csv(body.upload_id)
    fleet = {k: int(body.fleet.get(k, 0)) for k in FLEET_SIZES}
    landing = from_api_dict(body.landing.model_dump())
    try:
        landing.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        result = run_optimization(
            path,
            body.source_warehouse.strip(),
            fleet,
            max_source_km=body.max_source_km,
            min_totes=body.min_totes,
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
    upload_id: str = Form("sample"),
    max_source_km: float = Form(80),
    min_totes: float = Form(DEFAULT_MIN_TOTES),
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
        min_totes=min_totes,
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
