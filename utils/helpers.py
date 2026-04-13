"""Timezone-safe time helpers and money/lot math.

NEVER hardcode UTC offsets or pip values. All time math uses tz-aware datetimes.
All point/pip math reads from a fresh symbol_info dict.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Current UTC time, tz-aware."""
    return datetime.now(timezone.utc)


def to_tz(dt: datetime, tz_name: str) -> datetime:
    """Convert a tz-aware datetime to the given IANA timezone."""
    if dt.tzinfo is None:
        raise ValueError("Naive datetime passed to to_tz; always use tz-aware datetimes")
    return dt.astimezone(ZoneInfo(tz_name))


def parse_hhmm(s: str) -> dtime:
    """Parse 'HH:MM' into a time object."""
    h, m = s.split(":")
    return dtime(int(h), int(m))


def is_within_window(now_local: datetime, start: dtime, end: dtime) -> bool:
    """True if now_local time-of-day falls within [start, end] (handles overnight)."""
    t = now_local.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def is_weekend_utc(dt: datetime) -> bool:
    """Heuristic: Friday 22:00 UTC -> Sunday 22:00 UTC is broadly closed for FX/CFDs.

    Brokers vary; treat this as the default and let session/holiday filters refine.
    """
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    u = dt.astimezone(timezone.utc)
    wd = u.weekday()  # Mon=0
    if wd == 5:
        return True
    if wd == 6 and u.hour < 22:
        return True
    if wd == 4 and u.hour >= 22:
        return True
    return False


# ---------------------------------------------------------------------------
# Lot / point / money math (broker-spec driven)
# ---------------------------------------------------------------------------

def point_value_per_lot(symbol_info: Mapping[str, Any]) -> float:
    """Money value of one POINT for one lot, from fresh symbol_info.

    Formula: (tick_value / tick_size) * point.

    Raises ValueError if any required field is missing or invalid.
    """
    try:
        tick_value = float(symbol_info["trade_tick_value"])
        tick_size = float(symbol_info["trade_tick_size"])
        point = float(symbol_info["point"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"symbol_info missing tick/point fields: {e}") from e
    if tick_size <= 0 or point <= 0:
        raise ValueError("tick_size and point must be > 0")
    return (tick_value / tick_size) * point


def round_to_step(value: float, step: float) -> float:
    """Round value DOWN to nearest multiple of step (lot_step)."""
    if step <= 0:
        raise ValueError("step must be > 0")
    return math.floor(value / step) * step


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def points_distance(price_a: float, price_b: float, point: float) -> float:
    """Absolute distance between two prices expressed in points."""
    if point <= 0:
        raise ValueError("point must be > 0")
    return abs(price_a - price_b) / point
