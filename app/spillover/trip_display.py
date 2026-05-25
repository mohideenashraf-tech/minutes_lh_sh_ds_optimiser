"""Format trip legs for UI: hop landings, per-connection totes, and fleet."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _landing_label(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, str):
        return dt[11:16] if len(dt) >= 16 else dt
    return dt.strftime("%H:%M")


def build_trip_legs(
    candidate: dict,
    base_orders: list[dict],
    arrivals: list[datetime],
    *,
    source_label: str = "Source",
) -> list[dict[str, Any]]:
    """One row per hop: from → to, landing time, totes at stop, fleet assigned."""
    truck = str(candidate.get("truck") or "")
    ids = candidate.get("ids") or []
    if not ids:
        return []

    legs: list[dict[str, Any]] = []
    if candidate.get("type") == "Direct" or len(ids) == 1:
        o = base_orders[ids[0]]
        landing = arrivals[0] if arrivals else None
        legs.append(
            {
                "hop": 1,
                "from": source_label,
                "to": o["id"],
                "landing": landing,
                "landing_label": _landing_label(landing),
                "totes": round(float(o["vol"]) / 20, 1),
                "fleet": truck,
            }
        )
        return legs

    orders = [base_orders[ix] for ix in ids]
    for k, o in enumerate(orders):
        landing = arrivals[k] if k < len(arrivals) else None
        legs.append(
            {
                "hop": k + 1,
                "from": source_label if k == 0 else orders[k - 1]["id"],
                "to": o["id"],
                "landing": landing,
                "landing_label": _landing_label(landing),
                "totes": round(float(o["vol"]) / 20, 1),
                "fleet": truck,
            }
        )
    return legs


def serialize_trip_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for leg in legs:
        row = {k: v for k, v in leg.items() if k != "landing"}
        landing = leg.get("landing")
        if isinstance(landing, datetime):
            row["landing"] = landing.isoformat(sep=" ")
            row["landing_label"] = landing.strftime("%H:%M")
        elif landing is not None:
            row["landing"] = str(landing)
            row["landing_label"] = leg.get("landing_label") or _landing_label(str(landing))
        else:
            row["landing"] = None
            row["landing_label"] = "—"
        out.append(row)
    return out
