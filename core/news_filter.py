"""Economic calendar / news filter.

Block trading around high-impact events. Sources:
- MT5 economic calendar (mt5.calendar_value_history) — preferred
- Optional injected fetcher for testability

Block window logic:
- Standard high-impact: pre_event_block_minutes / post_event_block_minutes
- FOMC-class events: fomc_pre_block / fomc_post_block (longer)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Sequence

logger = logging.getLogger("goldmind")


@dataclass(frozen=True)
class CalendarEvent:
    name: str
    timestamp: datetime              # tz-aware UTC
    importance: int = 3              # 1=low, 2=medium, 3=high
    currency: str = "USD"


@dataclass(frozen=True)
class NewsCheck:
    blocked: bool
    reason: str = ""
    next_event: CalendarEvent | None = None
    minutes_until: float | None = None


# Caller-supplied fetcher: (window_start_utc, window_end_utc) -> events
EventFetcher = Callable[[datetime, datetime], Sequence[CalendarEvent]]


class NewsFilter:
    """Determines whether a high-impact news event blocks the next trade."""

    FOMC_KEYWORDS = ("FOMC", "Fed Chair", "Federal Reserve Rate")

    def __init__(
        self,
        news_cfg: Mapping,
        event_fetcher: EventFetcher | None = None,
    ) -> None:
        self.cfg = news_cfg
        self._fetch = event_fetcher
        self._block_names = {n.lower() for n in news_cfg.get("block_events", [])}
        self.pre_block = int(news_cfg.get("pre_event_block_minutes", 30))
        self.post_block = int(news_cfg.get("post_event_block_minutes", 15))
        self.fomc_pre = int(news_cfg.get("fomc_pre_block_minutes", 60))
        self.fomc_post = int(news_cfg.get("fomc_post_block_minutes", 30))

    def is_blocked(self, now_utc: datetime | None = None) -> NewsCheck:
        if not self.cfg.get("enabled", True):
            return NewsCheck(False, "news filter disabled")
        if self._fetch is None:
            return NewsCheck(False, "no event source configured")

        now = self._ensure_utc(now_utc)
        max_pre = max(self.pre_block, self.fomc_pre)
        max_post = max(self.post_block, self.fomc_post)
        try:
            events = list(self._fetch(now - timedelta(minutes=max_post),
                                      now + timedelta(minutes=max_pre + 1)))
        except Exception as e:  # noqa: BLE001
            logger.warning("Event fetch failed: %s", e)
            return NewsCheck(False, f"event fetch error: {e}")

        for ev in self._filter_blocking(events):
            pre, post = self._window_for(ev)
            start = ev.timestamp - timedelta(minutes=pre)
            end = ev.timestamp + timedelta(minutes=post)
            if start <= now <= end:
                delta = (ev.timestamp - now).total_seconds() / 60.0
                return NewsCheck(
                    True,
                    f"blocked by {ev.name} at {ev.timestamp.isoformat()} ({delta:+.1f}min)",
                    next_event=ev,
                    minutes_until=delta,
                )

        upcoming = [ev for ev in self._filter_blocking(events) if ev.timestamp > now]
        upcoming.sort(key=lambda e: e.timestamp)
        nxt = upcoming[0] if upcoming else None
        return NewsCheck(False, "no blocking event in window", next_event=nxt,
                         minutes_until=((nxt.timestamp - now).total_seconds() / 60.0) if nxt else None)

    # ------------------------------------------------------------------
    def _filter_blocking(self, events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for ev in events:
            if ev.importance < 3:
                continue
            name = ev.name.lower()
            if not self._block_names:
                out.append(ev)
                continue
            if any(b in name for b in self._block_names):
                out.append(ev)
        return out

    def _window_for(self, ev: CalendarEvent) -> tuple[int, int]:
        if any(k.lower() in ev.name.lower() for k in self.FOMC_KEYWORDS):
            return self.fomc_pre, self.fomc_post
        return self.pre_block, self.post_block

    @staticmethod
    def _ensure_utc(now_utc: datetime | None) -> datetime:
        if now_utc is None:
            return datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            raise ValueError("naive datetime")
        return now_utc.astimezone(timezone.utc)


# ----------------------------------------------------------------------
# Default MT5 calendar fetcher (optional — engine wires this in if mt5 is available)
# ----------------------------------------------------------------------

def make_mt5_event_fetcher(currencies: Sequence[str] = ("USD", "XAU")) -> EventFetcher:
    """Return an EventFetcher backed by mt5.calendar_value_history.

    Imports mt5 lazily so this module remains importable on non-Windows CI.
    """
    def _fetch(start: datetime, end: datetime) -> list[CalendarEvent]:
        import MetaTrader5 as mt5  # type: ignore
        out: list[CalendarEvent] = []
        for ccy in currencies:
            try:
                rows = mt5.calendar_value_history(start, end, currency=ccy) or []
            except TypeError:
                rows = mt5.calendar_value_history(start, end) or []
            for r in rows:
                ts = getattr(r, "time", None)
                if ts is None:
                    continue
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                out.append(CalendarEvent(
                    name=str(getattr(r, "event_name", "") or getattr(r, "name", "")),
                    timestamp=dt,
                    importance=int(getattr(r, "importance", 3)),
                    currency=ccy,
                ))
        return out
    return _fetch
