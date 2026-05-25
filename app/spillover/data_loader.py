"""Source Grid + XD Grid demand loading from Raw_1 CSV."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

from app.spillover.landing_windows import DEFAULT_LANDING, LandingWindowConfig, apply_landing_windows
from app.spillover.static_ref import load_static_reference

IST = pytz.timezone("Asia/Kolkata")


def load_raw_file(path: Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        df = pd.read_excel(path, engine="openpyxl")
    else:
        df = pd.read_csv(path, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    df["Count_Box_ID"] = pd.to_numeric(df["Count_Box_ID"], errors="coerce").fillna(0)
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)
    return df


def list_source_warehouses(df: pd.DataFrame) -> list[dict]:
    sg = df[df["box_final_status"] == "Source Grid"].copy()
    if sg.empty:
        return []
    summary = (
        sg.groupby("source_warehouse")
        .agg(
            sg_rows=("Count_Box_ID", "size"),
            box_count=("Count_Box_ID", "sum"),
            next_stops=("NextStop", "nunique"),
            destinations=("destination_warehouse", "nunique"),
        )
        .reset_index()
        .sort_values("box_count", ascending=False)
    )
    out = []
    for _, r in summary.iterrows():
        out.append(
            {
                "id": str(r["source_warehouse"]),
                "sg_rows": int(r["sg_rows"]),
                "box_count": float(r["box_count"]),
                "next_stops": int(r["next_stops"]),
                "destinations": int(r["destinations"]),
            }
        )
    return out


def pivot_source_grid(df: pd.DataFrame) -> pd.DataFrame:
    sg = df[df["box_final_status"] == "Source Grid"].copy()
    return (
        sg.groupby(["source_warehouse", "NextStop"], dropna=False)
        .agg(
            box_count=("Count_Box_ID", "sum"),
            quantity=("Quantity", "sum"),
            destinations=("destination_warehouse", "nunique"),
        )
        .reset_index()
        .rename(columns={"source_warehouse": "source"})
    )


def filter_rows_by_dbd_window(
    df: pd.DataFrame,
    past_hrs: float,
    future_hrs: float,
    logs: list[str],
) -> pd.DataFrame:
    """Keep rows whose dbd is within [now_IST - past_hrs, now_IST + future_hrs]."""
    if "dbd" not in df.columns:
        logs.append("DBD column not found in file — DBD time filter skipped.")
        return df
    out = df.copy()
    out["_dbd_dt"] = pd.to_datetime(out["dbd"], errors="coerce")
    out = out[out["_dbd_dt"].notna()].copy()
    if out.empty:
        logs.append("DBD filter: no rows with parseable dbd values.")
        return out
    if out["_dbd_dt"].dt.tz is None:
        out["_dbd_dt"] = out["_dbd_dt"].dt.tz_localize(IST, ambiguous="infer", nonexistent="shift_forward")
    else:
        out["_dbd_dt"] = out["_dbd_dt"].dt.tz_convert(IST)
    now_ts = datetime.now(IST)
    low = now_ts - timedelta(hours=float(past_hrs))
    high = now_ts + timedelta(hours=float(future_hrs))
    before = len(out)
    out = out[(out["_dbd_dt"] >= low) & (out["_dbd_dt"] <= high)].copy()
    dropped = before - len(out)
    if dropped:
        logs.append(
            f"DBD window [{low.strftime('%H:%M')}–{high.strftime('%H:%M')} IST]: "
            f"dropped {dropped} rows"
        )
    return out.drop(columns=["_dbd_dt"], errors="ignore")


def pivot_xd_grid(df: pd.DataFrame) -> pd.DataFrame:
    xd = df[df["box_final_status"] == "XD Grid"].copy()
    return (
        xd.groupby(["NextStop", "destination_warehouse"], dropna=False)
        .agg(box_count=("Count_Box_ID", "sum"), quantity=("Quantity", "sum"))
        .reset_index()
        .rename(columns={"NextStop": "next_stop"})
    )


def build_demand(
    path: Path,
    source_warehouse: str,
    *,
    max_source_km: float = 80.0,
    max_totes: float = 40.0,
    dbd_past_cutoff_hrs: float = 4.0,
    dbd_future_cutoff_hrs: float = 12.0,
    landing: LandingWindowConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Build solver demand from Source Grid + XD Grid (Raw_1 format CSV)."""
    logs: list[str] = []
    landing = landing or DEFAULT_LANDING

    df_raw = load_raw_file(path)
    logs.append(f"Loaded {len(df_raw):,} rows from {path.name}")
    df_raw = filter_rows_by_dbd_window(df_raw, dbd_past_cutoff_hrs, dbd_future_cutoff_hrs, logs)
    if df_raw.empty:
        raise ValueError("No rows remain after DBD filter.")

    df_sources, df_dests, _windows = load_static_reference()

    sg_rows = df_raw[
        (df_raw["box_final_status"] == "Source Grid") & (df_raw["source_warehouse"] == source_warehouse)
    ].copy()
    xd_rows = df_raw[df_raw["box_final_status"] == "XD Grid"].copy()
    logs.append(f"Source Grid rows for {source_warehouse}: {len(sg_rows):,}")
    logs.append(f"XD Grid rows (all): {len(xd_rows):,}")

    sg_pivot = pivot_source_grid(df_raw)
    sg_pivot = sg_pivot[sg_pivot["source"] == source_warehouse].copy()
    xd_pivot = pivot_xd_grid(df_raw)
    next_stops = set(sg_pivot["NextStop"].dropna().astype(str))
    xd_for_source = xd_pivot[xd_pivot["next_stop"].astype(str).isin(next_stops)].copy()
    logs.append(f"Next stops from source grid: {len(next_stops)} | XD legs matched: {len(xd_for_source)}")

    chained = sg_pivot.merge(
        xd_for_source,
        left_on="NextStop",
        right_on="next_stop",
        how="inner",
        suffixes=("_sg", "_xd"),
    )
    chained_dest = pd.DataFrame(columns=["id", "box_count", "quantity", "path"])
    if not chained.empty:
        chained["box_count"] = chained[["box_count_sg", "box_count_xd"]].min(axis=1)
        chained["quantity"] = chained[["quantity_sg", "quantity_xd"]].min(axis=1)
        chained_dest = (
            chained.groupby("destination_warehouse", dropna=False)
            .agg(box_count=("box_count", "sum"), quantity=("quantity", "sum"))
            .reset_index()
            .rename(columns={"destination_warehouse": "id"})
        )
        chained_dest["path"] = "source_grid_x_xd_grid"

    sg_direct = (
        sg_rows.groupby("destination_warehouse", dropna=False)
        .agg(box_count=("Count_Box_ID", "sum"), quantity=("Quantity", "sum"))
        .reset_index()
        .rename(columns={"destination_warehouse": "id"})
    )
    sg_direct["path"] = "source_grid_direct"

    if not xd_for_source.empty:
        xd_only = (
            xd_rows[xd_rows["NextStop"].astype(str).isin(next_stops)]
            .groupby("destination_warehouse", dropna=False)
            .agg(box_count=("Count_Box_ID", "sum"), quantity=("Quantity", "sum"))
            .reset_index()
            .rename(columns={"destination_warehouse": "id"})
        )
        xd_only["path"] = "xd_grid"
    else:
        xd_only = pd.DataFrame(columns=["id", "box_count", "quantity", "path"])

    parts = [p for p in (sg_direct, chained_dest, xd_only) if not p.empty]
    if not parts:
        raise ValueError("No demand after Source Grid + XD Grid combine.")

    combined = pd.concat(parts, ignore_index=True)
    combined["id"] = combined["id"].astype(str).str.strip()

    agg = (
        combined.groupby("id", dropna=False)
        .agg(box_count=("box_count", "sum"), volume=("quantity", "sum"))
        .reset_index()
    )
    agg["totes"] = agg["box_count"]
    if "volume" not in agg.columns or agg["volume"].isna().all():
        agg["volume"] = agg["box_count"] * 20
    else:
        agg["volume"] = agg["volume"].fillna(agg["box_count"] * 20)

    logs.append(f"Combined grid demand: {len(agg)} destinations | box_count {agg['box_count'].sum():,.0f}")

    if max_totes > 0:
        above = agg[agg["totes"] > max_totes]
        if not above.empty:
            logs.append(f"Dropped {len(above)} destinations with box_count > {max_totes}")
        agg = agg[agg["totes"] <= max_totes].copy()
        if agg.empty:
            raise ValueError(f"No destinations after max box filter ({max_totes}).")

    df = apply_landing_windows(agg, landing)
    logs.append(f"Delivery windows: {landing.summary()}")

    df_lat = df_dests[["id", "lat", "lon"]].drop_duplicates(subset=["id"])
    df = df.merge(df_lat, on="id", how="left")
    df["max_ft"] = float("nan")
    df["max_ft_int"] = 99

    src_row = df_sources[df_sources["id"] == source_warehouse]
    if not src_row.empty:
        src_lat = float(src_row.iloc[0]["lat"])
        src_lon = float(src_row.iloc[0]["lon"])

        def _haversine_km(lat1, lon1, lat2, lon2):
            r = 6371
            l1, ln1, l2, ln2 = map(math.radians, [lat1, lon1, lat2, lon2])
            a = math.sin((l2 - l1) / 2) ** 2 + math.cos(l1) * math.cos(l2) * math.sin((ln2 - ln1) / 2) ** 2
            return 2 * r * math.asin(math.sqrt(a))

        df["_dist"] = df.apply(
            lambda r: _haversine_km(src_lat, src_lon, r["lat"], r["lon"]) if pd.notna(r["lat"]) else float("nan"),
            axis=1,
        )
        too_far = df[df["_dist"] > max_source_km]
        if not too_far.empty:
            logs.append(f"Dropped {len(too_far)} destinations beyond {max_source_km} km")
        df = df[df["_dist"] <= max_source_km].drop(columns=["_dist"]).copy()

    missing = df["lat"].isna().sum()
    if missing:
        logs.append(f"Dropped {missing} destinations missing lat/lon")
        df = df[df["lat"].notna()].copy()

    if df.empty:
        raise ValueError("No routable destinations after filters.")

    logs.append(f"Demand ready: {len(df)} destinations | box_count {df['box_count'].sum():,.0f}")

    legs = pd.concat(
        [
            sg_pivot.assign(leg="source_to_nextstop", grid_type="Source Grid"),
            xd_for_source.assign(leg="nextstop_to_dest", grid_type="XD Grid"),
        ],
        ignore_index=True,
    )
    return df, legs, logs
