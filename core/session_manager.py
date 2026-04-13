"""DST-safe session manager.

NEVER hardcode UTC offsets. All time math is done with tz-aware datetimes
in IANA timezones. London/NY/Tokyo all observe DST on different schedules,
so static UTC offsets break twice a year.

Sessions: ASIAN / LONDON / NEW_YORK / NY_OVERLAP (London+NY both open) /
DEAD_ZONE / WEEKEND.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from utils.constants import Session
from utils.helpers import parse_hhmm

logger = logging.getLogger("goldmind")


@dataclass(frozen=True)
class SessionWindow:
    name: Session
    tz: ZoneInfo
    start: dtime
    end: dtime

    def contains(self, now_utc: datetime) -> bool:
        local = now_utc.astimezone(self.tz)
        t = local.time()
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end


class SessionManager:
    """Computes the active trading session in a DST-safe way."""

    def __init__(self, sessions_cfg: Mapping[str, Any], holidays_cfg: Mapping[str, Any]) -> None:
        self.cfg = sessions_cfg
        self.asian = SessionWindow(
            Session.ASIAN,
            ZoneInfo(sessions_cfg["asian"]["timezone"]),
            parse_hhmm(sessions_cfg["asian"]["start"]),
            parse_hhmm(sessions_cfg["asian"]["end"]),
        )
        self.london = SessionWindow(
            Session.LONDON,
            ZoneInfo(sessions_cfg["london"]["timezone"]),
            parse_hhmm(sessions_cfg["london"]["start"]),
            parse_hhmm(sessions_cfg["london"]["end"]),
        )
        self.new_york = SessionWindow(
            Session.NEW_YORK,
            ZoneInfo(sessions_cfg["new_york"]["timezone"]),
            parse_hhmm(sessions_cfg["new_york"]["start"]),
            parse_hhmm(sessions_cfg["new_york"]["end"]),
        )
        # Dead zone is broker-time agnostic; stored in NY time per typical conventions.
        self._dead_tz = ZoneInfo(sessions_cfg["new_york"]["timezone"])
        self.dead_start = parse_hhmm(sessions_cfg["dead_zone_start"])
        self.dead_end = parse_hhmm(sessions_cfg["dead_zone_end"])
        self.trade_sessions = {s.lower() for s in sessions_cfg.get("trade_sessions", [])}

        # Holidays
        self._closed_dates: set[date] = set()
        self._early_close_dates: set[date] = set()
        for key in ("closed_dates_2025", "closed_dates_2026", "closed_dates_2027"):
            for s in holidays_cfg.get(key, []) or []:
                self._closed_dates.add(date.fromisoformat(s))
        for key in ("early_close_dates_2025", "early_close_dates_2026", "early_close_dates_2027"):
            for s in holidays_cfg.get(key, []) or []:
                self._early_close_dates.add(date.fromisoformat(s))
        self.early_close_time = parse_hhmm(holidays_cfg.get("early_close_time", "13:00"))

    # ------------------------------------------------------------------
    # Session classification
    # ------------------------------------------------------------------
    def get_current_session(self, now_utc: datetime | None = None) -> Session:
        now_utc = self._ensure_utc(now_utc)

        if self.is_weekend(now_utc):
            return Session.WEEKEND
        if self.is_holiday(now_utc):
            return Session.WEEKEND  # treat full-closure days as weekend

        london_open = self.london.contains(now_utc)
        ny_open = self.new_york.contains(now_utc)
        if london_open and ny_open:
            return Session.NY_OVERLAP
        if london_open:
            return Session.LONDON
        if ny_open:
            return Session.NEW_YORK
        if self.asian.contains(now_utc):
            return Session.ASIAN
        if self._in_dead_zone(now_utc):
            return Session.DEAD_ZONE
        return Session.DEAD_ZONE

    def is_tradeable(self, now_utc: datetime | None = None) -> bool:
        sess = self.get_current_session(now_utc)
        if sess in (Session.WEEKEND, Session.DEAD_ZONE):
            return False
        return sess.value.lower() in self.trade_sessions or sess == Session.NY_OVERLAP and "ny_overlap" in self.trade_sessions

    # ------------------------------------------------------------------
    # Asian range — used by strategy for liquidity sweep levels
    # ------------------------------------------------------------------
    def get_asian_range(self, candles_h1: pd.DataFrame, now_utc: datetime | None = None) -> tuple[float, float] | None:
        """Return (low, high) of the most recent completed Asian session.

        Expects an H1 OHLC DataFrame with a tz-aware 'time' column in UTC.
        Returns None if not enough data falls inside the window.
        """
        if candles_h1 is None or candles_h1.empty:
            return None
        now_utc = self._ensure_utc(now_utc)
        local_today = now_utc.astimezone(self.asian.tz).date()
        # If we're before today's Asian open, use yesterday's range.
        local_now = now_utc.astimezone(self.asian.tz)
        ref_date = local_today
        if local_now.time() < self.asian.start:
            from datetime import timedelta
            ref_date = local_today - timedelta(days=1)

        start_local = datetime.combine(ref_date, self.asian.start, tzinfo=self.asian.tz)
        end_local = datetime.combine(ref_date, self.asian.end, tzinfo=self.asian.tz)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        times = pd.to_datetime(candles_h1["time"], utc=True)
        mask = (times >= start_utc) & (times <= end_utc)
        window = candles_h1.loc[mask]
        if window.empty:
            return None
        return float(window["low"].min()), float(window["high"].max())

    # ------------------------------------------------------------------
    # Holidays / early close / friday close
    # ------------------------------------------------------------------
    def is_holiday(self, now_utc: datetime | None = None) -> bool:
        now_utc = self._ensure_utc(now_utc)
        ny = now_utc.astimezone(self.new_york.tz).date()
        return ny in self._closed_dates

    def is_early_close_day(self, now_utc: datetime | None = None) -> bool:
        now_utc = self._ensure_utc(now_utc)
        ny = now_utc.astimezone(self.new_york.tz).date()
        return ny in self._early_close_dates

    def is_past_early_close(self, now_utc: datetime | None = None) -> bool:
        if not self.is_early_close_day(now_utc):
            return False
        now_utc = self._ensure_utc(now_utc)
        local = now_utc.astimezone(self.new_york.tz)
        return local.time() >= self.early_close_time

    def is_friday_close_time(
        self,
        friday_close_hhmm: str,
        now_utc: datetime | None = None,
    ) -> bool:
        """True if it's Friday and we've passed the configured close time.

        Time is interpreted in the NY timezone (US session reference).
        """
        now_utc = self._ensure_utc(now_utc)
        local = now_utc.astimezone(self.new_york.tz)
        if local.weekday() != 4:  # Friday
            return False
        return local.time() >= parse_hhmm(friday_close_hhmm)

    # ------------------------------------------------------------------
    # Weekend (broad — Fri 22:00 UTC -> Sun 22:00 UTC)
    # ------------------------------------------------------------------
    def is_weekend(self, now_utc: datetime | None = None) -> bool:
        now_utc = self._ensure_utc(now_utc)
        wd = now_utc.weekday()
        if wd == 5:
            return True
        if wd == 6 and now_utc.hour < 22:
            return True
        if wd == 4 and now_utc.hour >= 22:
            return True
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_utc(now_utc: datetime | None) -> datetime:
        if now_utc is None:
            return datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            raise ValueError("naive datetime; pass tz-aware UTC")
        return now_utc.astimezone(timezone.utc)

    def _in_dead_zone(self, now_utc: datetime) -> bool:
        local = now_utc.astimezone(self._dead_tz).time()
        if self.dead_start <= self.dead_end:
            return self.dead_start <= local <= self.dead_end
        return local >= self.dead_start or local <= self.dead_end
