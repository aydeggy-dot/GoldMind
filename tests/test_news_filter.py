"""Tests for NewsFilter — block windows around high-impact events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.news_filter import CalendarEvent, NewsFilter


@pytest.fixture()
def cfg() -> dict:
    return {
        "enabled": True,
        "block_events": ["NonFarm Payrolls", "FOMC Rate Decision", "CPI"],
        "pre_event_block_minutes": 30,
        "post_event_block_minutes": 15,
        "fomc_pre_block_minutes": 60,
        "fomc_post_block_minutes": 30,
    }


def test_blocks_pre_window_for_nfp(cfg):
    nfp = CalendarEvent("NonFarm Payrolls", datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc))
    nf = NewsFilter(cfg, lambda s, e: [nfp])
    now = datetime(2026, 5, 1, 12, 5, tzinfo=timezone.utc)  # 25 min before
    res = nf.is_blocked(now)
    assert res.blocked
    assert "NonFarm" in res.reason


def test_no_block_outside_window(cfg):
    nfp = CalendarEvent("NonFarm Payrolls", datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc))
    nf = NewsFilter(cfg, lambda s, e: [nfp])
    now = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)  # 90 min before
    res = nf.is_blocked(now)
    assert not res.blocked


def test_fomc_uses_longer_window(cfg):
    fomc = CalendarEvent("FOMC Rate Decision", datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc))
    nf = NewsFilter(cfg, lambda s, e: [fomc])
    # 50 min before — outside standard 30 but inside FOMC 60
    now = datetime(2026, 6, 17, 17, 10, tzinfo=timezone.utc)
    assert nf.is_blocked(now).blocked


def test_low_importance_not_blocked(cfg):
    minor = CalendarEvent("CPI", datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc), importance=1)
    nf = NewsFilter(cfg, lambda s, e: [minor])
    now = datetime(2026, 5, 1, 12, 25, tzinfo=timezone.utc)
    assert not nf.is_blocked(now).blocked


def test_event_not_in_block_list_ignored(cfg):
    other = CalendarEvent("Beige Book", datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc))
    nf = NewsFilter(cfg, lambda s, e: [other])
    now = datetime(2026, 5, 1, 17, 50, tzinfo=timezone.utc)
    assert not nf.is_blocked(now).blocked


def test_disabled_filter_passes(cfg):
    cfg["enabled"] = False
    nf = NewsFilter(cfg, lambda s, e: [])
    assert not nf.is_blocked(datetime.now(timezone.utc)).blocked


def test_no_fetcher_returns_unblocked(cfg):
    nf = NewsFilter(cfg, None)
    assert not nf.is_blocked(datetime.now(timezone.utc)).blocked


def test_post_event_block(cfg):
    nfp = CalendarEvent("NonFarm Payrolls", datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc))
    nf = NewsFilter(cfg, lambda s, e: [nfp])
    now = datetime(2026, 5, 1, 12, 40, tzinfo=timezone.utc)  # 10 min after
    assert nf.is_blocked(now).blocked
    later = datetime(2026, 5, 1, 12, 50, tzinfo=timezone.utc)  # 20 min after
    assert not nf.is_blocked(later).blocked
