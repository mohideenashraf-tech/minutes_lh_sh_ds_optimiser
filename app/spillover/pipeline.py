"""Run spillover milk-run optimisation end-to-end."""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.spillover import solver_core as sc
from app.spillover.data_loader import build_demand
from app.spillover.static_ref import load_static_reference

FLEET_SIZES = ["07FT", "08FT", "10FT", "14FT", "17FT", "20FT", "22FT"]
TOTE_CAPS = {"07FT": 90, "08FT": 126, "10FT": 140, "14FT": 210, "17FT": 336, "20FT": 350, "22FT": 385}


def _serialize_dt(val: Any) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, pd.Timestamp):
        return val.isoformat(sep=" ")
    if isinstance(val, datetime):
        return val.isoformat(sep=" ")
    return str(val)


def _trips_to_records(trips: list[dict]) -> list[dict]:
    out = []
    for t in trips:
        row = {}
        for k, v in t.items():
            if k in {"vpt", "dispatch_from_dock", "dispatch_from_source", "return_time", "hop1", "hop2", "hop3"}:
                row[k] = _serialize_dt(v)
            else:
                row[k] = v
        out.append(row)
    return out


def run_optimization(
    csv_path: Path,
    source_warehouse: str,
    fleet: dict[str, int],
    *,
    max_source_km: float = 80.0,
    landing=None,
) -> dict[str, Any]:
    from app.spillover.landing_windows import DEFAULT_LANDING

    landing = landing or DEFAULT_LANDING
    demand, legs, demand_logs = build_demand(
        csv_path,
        source_warehouse,
        max_source_km=max_source_km,
        landing=landing,
    )

    df_sources, _df_dests, _w = load_static_reference()
    src_row = df_sources[df_sources["id"] == source_warehouse]
    if src_row.empty:
        raise ValueError(f"Source '{source_warehouse}' not found in static reference.")
    src = (float(src_row.iloc[0]["lat"]), float(src_row.iloc[0]["lon"]))

    available_fleet = {k: int(v) for k, v in fleet.items() if int(v) > 0}
    if not available_fleet:
        raise ValueError("Enter at least one truck in fleet.")

    available_types = [t for t in FLEET_SIZES if available_fleet.get(t, 0) > 0]
    df_trucks = pd.DataFrame(
        [
            {"type": t, "cap": TOTE_CAPS[t], "size_int": sc.parse_truck_cap(t, "truck_type")}
            for t in available_types
        ]
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        opt_trips, base_orders, dropped_ixs, _df_trucks = sc.solve_with_ortools(
            demand, df_trucks, src, available_fleet
        )
        try:
            first_date = pd.to_datetime(demand["window_start"].iloc[0])
            base_d = datetime(first_date.year, first_date.month, first_date.day)
        except Exception:
            base_d = datetime(2024, 1, 2)
        adhoc_trips, unserviceable = sc.generate_adhoc_trips(
            dropped_ixs, base_orders, df_trucks, src, base_d
        )
        all_trips = opt_trips + adhoc_trips
        fleet = sc.run_fleet_rotation(all_trips, initial_fleet=[]) if all_trips else []

    solver_log = buf.getvalue()
    logs = demand_logs + [solver_log]

    trip_records = _trips_to_records(all_trips)
    direct_n = sum(1 for t in trip_records if t.get("type") == "Direct")
    pair_n = sum(1 for t in trip_records if t.get("type") == "Pair")
    primary_n = sum(1 for t in trip_records if t.get("trip_tag") == "Primary")
    adhoc_n = sum(1 for t in trip_records if t.get("trip_tag") == "Ad-Hoc")

    rotation = []
    for v in fleet:
        for t in v.get("trips", []):
            rotation.append(
                {
                    "physical_truck_id": v["id"],
                    "fleet_type": v["type"],
                    "trip_tag": t.get("trip_tag"),
                    "trip_type": t.get("type"),
                    "route": t.get("route"),
                    "vpt": _serialize_dt(t.get("vpt")),
                    "dispatch": _serialize_dt(t.get("dispatch_from_source")),
                    "hop1": _serialize_dt(t.get("hop1")),
                    "hop2": _serialize_dt(t.get("hop2")),
                    "return_time": _serialize_dt(t.get("return_time")),
                }
            )

    return {
        "source_warehouse": source_warehouse,
        "source_coords": {"lat": src[0], "lon": src[1]},
        "destinations": int(len(demand)),
        "total_box_count": float(demand["box_count"].sum()),
        "trips": trip_records,
        "fleet_rotation": rotation,
        "unserviceable": unserviceable,
        "grid_legs": legs.fillna("").astype(str).to_dict(orient="records"),
        "demand_preview": demand[["id", "box_count", "totes", "lat", "lon", "window_start", "window_end"]]
        .astype(str)
        .to_dict(orient="records"),
        "landing_window": landing.to_dict(),
        "summary": {
            "primary_trips": primary_n,
            "adhoc_trips": adhoc_n,
            "direct_trips": direct_n,
            "pair_milk_runs": pair_n,
            "physical_trucks": len([v for v in fleet if v.get("trips")]),
            "unserviceable_count": len(unserviceable),
        },
        "logs": logs,
    }
