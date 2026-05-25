"""Google Sheets API access for Raw_1 demand and static reference data."""
from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from typing import Any

import pandas as pd

# Live pendency workbook (tab: Raw_1)
SHEET_URL = os.environ.get(
    "SPILLOVER_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1EQ377yHq2C-KP5CBqtRy_IIewnYViiPsX0yFpjvZH8Y/edit",
)
RAW_SHEET = os.environ.get("SPILLOVER_RAW_SHEET", "Raw_1")

# Static lat/lon + landing window reference
STATIC_SHEET_URL = os.environ.get(
    "SPILLOVER_STATIC_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/11nQJuyaxgIbyXosw9MswdDfcfNalMATDeHvWhceTHrw/edit",
)
TAB_SOURCES = "Sources"
TAB_DESTS = "Destinations"
TAB_WINDOWS = "Landing Window"

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_RAW_CACHE: tuple[float, pd.DataFrame] | None = None
_RAW_CACHE_TTL_SEC = float(os.environ.get("SPILLOVER_RAW_CACHE_SEC", "90"))


def sheets_configured() -> bool:
    """True when service-account JSON or application-default credentials are available."""
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        return True
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if cred_path and os.path.isfile(cred_path):
        return True
    try:
        import google.auth

        google.auth.default(scopes=_SCOPES)
        return True
    except Exception:
        return False


def _service_account_info() -> dict[str, Any] | None:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    return json.loads(raw)


@lru_cache(maxsize=1)
def get_gspread_client():
    """Return an authorized gspread client (cached)."""
    import gspread
    from google.oauth2.service_account import Credentials

    info = _service_account_info()
    if info:
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        return gspread.authorize(creds)

    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if cred_path and os.path.isfile(cred_path):
        creds = Credentials.from_service_account_file(cred_path, scopes=_SCOPES)
        return gspread.authorize(creds)

    import google.auth

    creds, _ = google.auth.default(scopes=_SCOPES)
    return gspread.authorize(creds)


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for h in headers:
        h = str(h).strip()
        if h in seen:
            seen[h] += 1
            out.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 0
            out.append(h)
    return out


def worksheet_to_dataframe(ws) -> pd.DataFrame:
    """Read a worksheet via get_values() and return a DataFrame with deduped headers."""
    all_vals = ws.get_values()
    if not all_vals:
        return pd.DataFrame()
    headers = _dedupe_headers(all_vals[0])
    df = pd.DataFrame(all_vals[1:], columns=headers)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def fetch_raw_1(*, use_cache: bool = True) -> pd.DataFrame:
    """Fetch full Raw_1 tab from the live Google Sheet."""
    global _RAW_CACHE
    now = time.time()
    if use_cache and _RAW_CACHE is not None:
        ts, cached = _RAW_CACHE
        if now - ts < _RAW_CACHE_TTL_SEC:
            return cached.copy()

    gc = get_gspread_client()
    sh = gc.open_by_url(SHEET_URL)
    ws = sh.worksheet(RAW_SHEET)
    df = worksheet_to_dataframe(ws)
    if df.empty:
        raise ValueError(f"Sheet '{RAW_SHEET}' is empty.")

    if "Count_Box_ID" in df.columns:
        df["Count_Box_ID"] = pd.to_numeric(df["Count_Box_ID"], errors="coerce").fillna(0)
    if "Quantity" in df.columns:
        df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)

    _RAW_CACHE = (now, df)
    return df.copy()


def invalidate_raw_cache() -> None:
    global _RAW_CACHE
    _RAW_CACHE = None


def fetch_static_reference() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, tuple[str, str]]]:
    """Sources, destinations (id/lat/lon), and landing window map from static sheet."""
    gc = get_gspread_client()
    sh = gc.open_by_url(STATIC_SHEET_URL)

    ws_src = sh.worksheet(TAB_SOURCES)
    rows = ws_src.get_values()[1:]
    df_sources = pd.DataFrame(rows, columns=["id", "lat", "lon"])
    df_sources["lat"] = pd.to_numeric(df_sources["lat"], errors="coerce")
    df_sources["lon"] = pd.to_numeric(df_sources["lon"], errors="coerce")
    df_sources["id"] = df_sources["id"].astype(str).str.strip()

    ws_dst = sh.worksheet(TAB_DESTS)
    rows = ws_dst.get_values()[1:]
    df_dests = pd.DataFrame(rows, columns=["id", "lat", "lon"])
    df_dests["lat"] = pd.to_numeric(df_dests["lat"], errors="coerce")
    df_dests["lon"] = pd.to_numeric(df_dests["lon"], errors="coerce")
    df_dests["id"] = df_dests["id"].astype(str).str.strip()

    ws_win = sh.worksheet(TAB_WINDOWS)
    windows: dict[str, tuple[str, str]] = {}
    for row in ws_win.get_values()[1:]:
        if len(row) >= 3 and str(row[0]).strip():
            windows[str(row[0]).strip()] = (str(row[1]).strip(), str(row[2]).strip())

    return df_sources, df_dests, windows


def check_sheets_connection() -> dict[str, Any]:
    """Lightweight connectivity check for health/config endpoints."""
    out: dict[str, Any] = {
        "configured": sheets_configured(),
        "raw_sheet": RAW_SHEET,
        "static_tabs": [TAB_SOURCES, TAB_DESTS, TAB_WINDOWS],
    }
    if not out["configured"]:
        out["ok"] = False
        out["error"] = "Set GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_APPLICATION_CREDENTIALS)."
        return out
    try:
        gc = get_gspread_client()
        sh = gc.open_by_url(SHEET_URL)
        out["ok"] = True
        out["spreadsheet_title"] = sh.title
        out["raw_worksheet"] = RAW_SHEET
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out
