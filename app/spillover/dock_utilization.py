"""Compute source dock usage over time from scheduled trips (matches CP-SAT cumulative windows)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd


def _parse_dt(val: Any) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return None


def compute_dock_utilization(
    trips: list[dict],
    max_docks: int,
    *,
    bin_minutes: int = 15,
) -> dict[str, Any]:
    """
    Each trip occupies one dock from VPT until dispatch_from_dock (loading complete).
    Returns a time series for charting and summary stats.
    """
    max_docks = max(1, int(max_docks))
    events: list[tuple[datetime, int, str]] = []
    trip_intervals: list[dict[str, Any]] = []

    for i, t in enumerate(trips):
        vpt = _parse_dt(t.get("vpt"))
        dock_end = _parse_dt(t.get("dispatch_from_dock"))
        load_hrs = t.get("load_hrs")
        if vpt and not dock_end and load_hrs is not None:
            try:
                dock_end = vpt + timedelta(hours=float(load_hrs))
            except (TypeError, ValueError):
                dock_end = None
        if not vpt or not dock_end or dock_end <= vpt:
            continue
        tag = str(t.get("trip_tag") or "")
        route = str(t.get("route") or "")
        label = f"{tag} {t.get('type', '')} — {route}".strip()
        events.append((vpt, +1, label))
        events.append((dock_end, -1, label))
        trip_intervals.append(
            {
                "trip_index": i + 1,
                "route": route,
                "trip_tag": tag,
                "type": t.get("type"),
                "truck": t.get("truck"),
                "vpt": vpt.isoformat(sep=" "),
                "dock_free": dock_end.isoformat(sep=" "),
                "load_hrs": float(load_hrs) if load_hrs is not None else None,
            }
        )

    if not events:
        return {
            "max_docks": max_docks,
            "peak_docks_used": 0,
            "peak_time": None,
            "avg_docks_used": 0.0,
            "utilization_pct_peak": 0.0,
            "timeline": [],
            "trip_intervals": [],
            "bins": [],
        }

    events.sort(key=lambda e: (e[0], -e[1]))

    timeline: list[dict[str, Any]] = []
    current = 0
    peak = 0
    peak_time: datetime | None = None
    prev_t: datetime | None = None

    for t, delta, _ in events:
        if prev_t is not None and t > prev_t:
            timeline.append(
                {
                    "time": prev_t.isoformat(sep=" "),
                    "label": prev_t.strftime("%H:%M"),
                    "docks_in_use": current,
                    "max_docks": max_docks,
                }
            )
        current += delta
        if current > peak:
            peak = current
            peak_time = t
        prev_t = t

    if prev_t is not None:
        timeline.append(
            {
                "time": prev_t.isoformat(sep=" "),
                "label": prev_t.strftime("%H:%M"),
                "docks_in_use": current,
                "max_docks": max_docks,
            }
        )

    t_min = min(e[0] for e in events)
    t_max = max(e[0] for e in events)
    bin_delta = timedelta(minutes=max(1, bin_minutes))

    bins: list[dict[str, Any]] = []
    total_person_minutes = 0.0
    t = t_min
    while t <= t_max:
        t_next = t + bin_delta
        mid = t + (t_next - t) / 2
        count = sum(1 for iv in trip_intervals if _parse_dt(iv["vpt"]) <= mid < _parse_dt(iv["dock_free"]))
        bins.append(
            {
                "time": t.isoformat(sep=" "),
                "label": t.strftime("%H:%M"),
                "docks_in_use": count,
                "max_docks": max_docks,
            }
        )
        total_person_minutes += count * bin_minutes
        t = t_next

    span_minutes = max(1, (t_max - t_min).total_seconds() / 60)
    avg_used = total_person_minutes / span_minutes

    return {
        "max_docks": max_docks,
        "peak_docks_used": peak,
        "peak_time": peak_time.isoformat(sep=" ") if peak_time else None,
        "peak_time_label": peak_time.strftime("%H:%M") if peak_time else None,
        "avg_docks_used": round(avg_used, 2),
        "utilization_pct_peak": round(100.0 * peak / max_docks, 1),
        "utilization_pct_avg": round(100.0 * avg_used / max_docks, 1),
        "window_start": t_min.isoformat(sep=" "),
        "window_end": t_max.isoformat(sep=" "),
        "timeline": timeline,
        "bins": bins,
        "trip_intervals": trip_intervals,
    }
