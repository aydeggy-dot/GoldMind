"""Tests for utils.helpers — money/lot math and time helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from utils.helpers import (
    clamp,
    is_within_window,
    parse_hhmm,
    point_value_per_lot,
    points_distance,
    round_to_step,
    to_tz,
    utc_now,
)


def test_point_value_per_lot_xauusd_typical():
    info = {"trade_tick_value": 1.0, "trade_tick_size": 0.01, "point": 0.01}
    assert point_value_per_lot(info) == pytest.approx(1.0)


def test_point_value_rejects_zero_tick_size():
    with pytest.raises(ValueError):
        point_value_per_lot({"trade_tick_value": 1, "trade_tick_size": 0, "point": 0.01})


def test_round_to_step_floors():
    assert round_to_step(0.137, 0.01) == pytest.approx(0.13)
    assert round_to_step(0.1, 0.01) == pytest.approx(0.10)
    with pytest.raises(ValueError):
        round_to_step(1.0, 0)


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(99, 0, 10) == 10


def test_points_distance():
    assert points_distance(2000.50, 2000.00, 0.01) == pytest.approx(50)


def test_to_tz_requires_aware():
    with pytest.raises(ValueError):
        to_tz(datetime(2026, 1, 1), "UTC")
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    converted = to_tz(aware, "America/New_York")
    assert converted.utcoffset() is not None


def test_utc_now_is_aware():
    assert utc_now().tzinfo is not None


def test_window_within_and_overnight():
    london_open = parse_hhmm("08:00")
    london_close = parse_hhmm("16:30")
    now = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert is_within_window(now, london_open, london_close)

    overnight_start = parse_hhmm("22:00")
    overnight_end = parse_hhmm("06:00")
    midnight = datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc)
    assert is_within_window(midnight, overnight_start, overnight_end)
    midday = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert not is_within_window(midday, overnight_start, overnight_end)
