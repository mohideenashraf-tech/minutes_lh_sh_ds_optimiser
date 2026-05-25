"""Static lat/lon reference from Google Sheets (optional local cache for offline dev)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

from app.spillover.gsheets_client import fetch_static_reference, sheets_configured

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_CACHE = PROJECT_ROOT / "data" / "static_reference_cache.xlsx"

KNOWN_COORDS: dict[str, tuple[float, float]] = {
    "bhi_pad_wh_nl_05nl": (19.384798, 73.20907),
    "ben_hos_wh_nl_02nl": (12.932, 77.602),
    "bin_sh_wh_nl_01nl": (28.535, 77.391),
    "ulu_sh_wh_nl_01nl": (8.524, 76.936),
    "ane_gsh_wh_nl_01nl": (12.838, 77.697),
    "son_gsh_wh_nl_01nl": (21.145, 79.088),
    "sai_gsh_wh_nl_01nl": (18.520, 73.856),
    "hyd_gsh_wh_nl_01nl": (17.385, 78.486),
    "che_gsh_wh_nl_01nl": (13.082, 80.270),
    "luc_gsh_wh_nl_01nl": (26.846, 80.946),
    "kol_gsh_wh_nl_01nl": (22.572, 88.363),
    "pat_sh_wh_nl_01nl": (25.594, 85.137),
    "ahm_sh_wh_nl_02nl": (23.022, 72.571),
    "new_new_wh_nl_01nl": (28.613, 77.209),
    "jai_sh_wh_nl_01nl": (26.912, 75.787),
    "guw_gsh_wh_nl_01nl": (26.115, 91.736),
    "bhu_gsh_wh_nl_02nl": (20.296, 85.824),
    "vij_gsh_wh_nl_02nl": (16.506, 80.648),
    "pun_sh_wh_nl_01nl": (18.520, 73.856),
    "coi_gsh_wh_nl_02nl": (11.016, 76.954),
    "bal_gsh_wh_nl_01nl": (28.613, 77.209),
    "hub_xd_wh_nl_01nl": (19.076, 72.877),
}

CITY_BASE: dict[str, tuple[float, float]] = {
    "mum": (19.076, 72.877),
    "pun": (18.520, 73.856),
    "ben": (12.971, 77.594),
    "che": (13.082, 80.270),
    "hyd": (17.385, 78.486),
    "kol": (22.572, 88.363),
    "del": (28.613, 77.209),
    "gur": (28.459, 77.026),
    "noi": (28.535, 77.391),
    "far": (28.408, 77.317),
    "bar": (21.145, 79.088),
    "tri": (8.524, 76.936),
    "rom": (19.014, 73.130),
    "nag": (21.145, 79.088),
    "sur": (21.170, 72.831),
    "ind": (22.719, 75.857),
    "bhi": (19.384, 73.209),
}


def approx_coords(store_id: str) -> tuple[float, float]:
    sid = str(store_id).strip().lower()
    if sid in KNOWN_COORDS:
        return KNOWN_COORDS[sid]
    m = re.match(r"([a-z]+)_(\d+)", sid)
    if not m:
        return (19.384, 73.209)
    num = int(m.group(2))
    lat, lon = CITY_BASE.get(m.group(1), (19.384, 73.209))
    return lat + (num % 50) * 0.002, lon + (num % 50) * 0.002


def build_cache_from_csv(csv_path: Path) -> None:
    raw = pd.read_csv(csv_path, usecols=["destination_warehouse", "NextStop", "source_warehouse"], low_memory=False)
    ids = sorted(
        set(raw["destination_warehouse"].dropna().astype(str))
        | set(raw["NextStop"].dropna().astype(str))
        | set(raw["source_warehouse"].dropna().astype(str))
    )
    rows = []
    for sid in ids:
        sid = sid.strip()
        lat, lon = KNOWN_COORDS.get(sid, approx_coords(sid))
        rows.append({"id": sid, "lat": lat, "lon": lon})
    df_dests = pd.DataFrame(rows).drop_duplicates(subset=["id"])
    source_ids = sorted(set(raw["source_warehouse"].dropna().astype(str)))
    df_sources = df_dests[df_dests["id"].isin(source_ids)].copy()
    windows = pd.DataFrame(
        [
            {"Part": "Day", "Window Start": "10:00", "Window End": "18:00"},
            {"Part": "Night", "Window Start": "22:00", "Window End": "04:00"},
        ]
    )
    STATIC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(STATIC_CACHE, engine="openpyxl") as writer:
        df_sources.to_excel(writer, sheet_name="Sources", index=False)
        df_dests.to_excel(writer, sheet_name="Destinations", index=False)
        windows.to_excel(writer, sheet_name="Landing_Window", index=False)


def load_static_reference() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    if sheets_configured():
        try:
            return fetch_static_reference()
        except Exception as exc:
            if os.environ.get("SPILLOVER_ALLOW_STATIC_CACHE_FALLBACK", "").lower() not in (
                "1",
                "true",
                "yes",
            ):
                raise RuntimeError(f"Could not load static reference from Google Sheets: {exc}") from exc

    if not STATIC_CACHE.exists():
        sample = PROJECT_ROOT / "data" / "sample.csv"
        if sample.exists():
            build_cache_from_csv(sample)
        else:
            raise FileNotFoundError(
                "Google Sheets credentials not configured and no static_reference_cache.xlsx. "
                "Set GOOGLE_SERVICE_ACCOUNT_JSON and share both spreadsheets with the service account."
            )
    df_sources = pd.read_excel(STATIC_CACHE, sheet_name="Sources", engine="openpyxl")
    df_dests = pd.read_excel(STATIC_CACHE, sheet_name="Destinations", engine="openpyxl")
    windows_df = pd.read_excel(STATIC_CACHE, sheet_name="Landing_Window", engine="openpyxl")
    windows = {
        str(r["Part"]).strip(): (str(r["Window Start"]).strip(), str(r["Window End"]).strip())
        for _, r in windows_df.iterrows()
        if str(r.get("Part", "")).strip()
    }
    for frame in (df_sources, df_dests):
        if "Sources" in frame.columns:
            frame.rename(columns={"Sources": "id"}, inplace=True)
        if "Destinations" in frame.columns:
            frame.rename(columns={"Destinations": "id"}, inplace=True)
        frame["id"] = frame["id"].astype(str).str.strip()
        frame["lat"] = pd.to_numeric(frame["lat"], errors="coerce")
        frame["lon"] = pd.to_numeric(frame["lon"], errors="coerce")
    return df_sources, df_dests, windows
