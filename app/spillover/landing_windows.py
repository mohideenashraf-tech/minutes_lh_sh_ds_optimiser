"""Landing window configuration (Day / Night) — no DBD dependency."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str) -> time:
    m = _TIME_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"Invalid time '{value}' (use HH:MM)")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time '{value}'")
    return time(hour, minute)


@dataclass
class LandingWindowConfig:
    day_start: str = "10:00"
    day_end: str = "18:00"
    night_start: str = "22:00"
    night_end: str = "04:00"
    active_part: str = "Day"  # Day | Night
    planning_date: date | None = None

    def validate(self) -> None:
        for label in ("day_start", "day_end", "night_start", "night_end"):
            parse_hhmm(getattr(self, label))
        if self.active_part not in {"Day", "Night"}:
            raise ValueError("active_part must be 'Day' or 'Night'")

    def bounds(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        self.validate()
        plan = self.planning_date or date.today()
        if self.active_part == "Day":
            start_t = parse_hhmm(self.day_start)
            end_t = parse_hhmm(self.day_end)
            w_start = pd.Timestamp(datetime.combine(plan, start_t))
            w_end = pd.Timestamp(datetime.combine(plan, end_t))
            if w_end <= w_start:
                w_end += timedelta(days=1)
            return w_start, w_end

        start_t = parse_hhmm(self.night_start)
        end_t = parse_hhmm(self.night_end)
        w_start = pd.Timestamp(datetime.combine(plan, start_t))
        w_end = pd.Timestamp(datetime.combine(plan, end_t))
        if w_end <= w_start:
            w_end += timedelta(days=1)
        return w_start, w_end

    def summary(self) -> str:
        s, e = self.bounds()
        return f"{self.active_part} {s.strftime('%H:%M')}–{e.strftime('%H:%M')} on {s.date()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_start": self.day_start,
            "day_end": self.day_end,
            "night_start": self.night_start,
            "night_end": self.night_end,
            "active_part": self.active_part,
            "planning_date": self.planning_date.isoformat() if self.planning_date else None,
        }


DEFAULT_LANDING = LandingWindowConfig()


def from_api_dict(data: dict[str, Any] | None) -> LandingWindowConfig:
    if not data:
        return LandingWindowConfig()
    plan = data.get("planning_date")
    return LandingWindowConfig(
        day_start=str(data.get("day_start", "10:00")),
        day_end=str(data.get("day_end", "18:00")),
        night_start=str(data.get("night_start", "22:00")),
        night_end=str(data.get("night_end", "04:00")),
        active_part=str(data.get("active_part", "Day")),
        planning_date=date.fromisoformat(plan) if plan else None,
    )


def apply_landing_windows(df: pd.DataFrame, config: LandingWindowConfig) -> pd.DataFrame:
    w_start, w_end = config.bounds()
    out = df.copy()
    out["window_start"] = w_start
    out["window_end"] = w_end
    return out
