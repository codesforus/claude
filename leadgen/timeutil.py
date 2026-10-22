"""Time handling. Timestamps are stored as UTC ISO strings so they sort lexicographically;
calling windows are evaluated in the configured local timezone (Europe/Prague)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import holidays

from leadgen.settings import CallingCfg


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_window(spec: str) -> tuple[time, time]:
    start, end = (time.fromisoformat(part.strip()) for part in spec.split("-"))
    if end <= start:
        raise ValueError(f"neplatné okno pro volání: {spec}")
    return start, end


class CallingCalendar:
    """Decides when the AI salesman is allowed to call."""

    def __init__(self, cfg: CallingCfg, tz: str):
        self.tz = ZoneInfo(tz)
        self.windows = sorted(_parse_window(w) for w in cfg.windows)
        self.weekdays = set(cfg.weekdays)
        self.skip_holidays = cfg.skip_public_holidays
        self._holidays = holidays.country_holidays("CZ")

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def is_working_day(self, d: date) -> bool:
        if d.isoweekday() not in self.weekdays:
            return False
        return not (self.skip_holidays and d in self._holidays)

    def in_window(self, dt: datetime) -> bool:
        local = self.local(dt)
        if not self.is_working_day(local.date()):
            return False
        return any(start <= local.time() < end for start, end in self.windows)

    def next_slot(self, dt: datetime) -> datetime:
        """Earliest moment at or after dt that falls inside a calling window (UTC)."""
        local = self.local(dt)
        for offset in range(0, 60):
            day = local.date() + timedelta(days=offset)
            if not self.is_working_day(day):
                continue
            for start, end in self.windows:
                window_start = datetime.combine(day, start, self.tz)
                window_end = datetime.combine(day, end, self.tz)
                if local < window_end:
                    return max(local, window_start).astimezone(timezone.utc)
        raise RuntimeError("v příštích 60 dnech není žádné okno pro volání")

    def day_start(self, dt: datetime) -> datetime:
        """Local midnight of dt's day, in UTC."""
        local = self.local(dt)
        return datetime.combine(local.date(), time(0), self.tz).astimezone(timezone.utc)
