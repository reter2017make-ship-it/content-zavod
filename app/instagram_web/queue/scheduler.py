"""When each queued Reel goes out: inside the daily window (e.g. 10:00–23:00), not more often than the interval,
not more than N a day. Pure functions: the publishing loop and the UI use the same plan."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _hm(s: str) -> time:
    h, m = (int(x) for x in str(s).split(":"))
    return time(h, m)


class Planner:
    def __init__(self, cfg):
        self.tz = ZoneInfo(cfg.timezone)
        self.start, self.end = _hm(cfg.window_start), _hm(cfg.window_end)
        self.interval = timedelta(minutes=float(cfg.interval_minutes))
        self.limit = int(cfg.max_posts_per_day)  # 0 = no daily limit
        self.all_day = self.start == self.end or (self.start == time(0, 0) and self.end >= time(23, 59))

    def local(self, utc_naive: datetime) -> datetime:
        return utc_naive.replace(tzinfo=timezone.utc).astimezone(self.tz)

    @staticmethod
    def utc(local: datetime) -> datetime:
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def day_start_utc(self, utc_naive: datetime) -> datetime:
        d = self.local(utc_naive)
        return self.utc(datetime(d.year, d.month, d.day, tzinfo=self.tz))

    def in_window(self, utc_naive: datetime) -> datetime:
        """The first moment ≥ t that is inside the publishing window."""
        if self.all_day:
            return utc_naive
        t = self.local(utc_naive)
        start = t.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        end = t.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        if t < start:
            return self.utc(start)
        if t >= end:
            return self.utc(start + timedelta(days=1))
        return utc_naive

    def plan(self, waiting: list[tuple[int, datetime | None]], now: datetime, published_today: int,
             last_published_at: datetime | None) -> dict[int, datetime]:
        """waiting: [(id, earliest UTC or None)] in queue order → {id: planned UTC}."""
        out: dict[int, datetime] = {}
        prev, count, day = last_published_at, published_today, self.day_start_utc(now)
        for pid, earliest in waiting:
            t = max(now, earliest or now)
            if prev:
                t = max(t, prev + self.interval)
            for _ in range(400):
                t = self.in_window(t)
                d = self.day_start_utc(t)
                if d != day:
                    day, count = d, 0
                if not self.limit or count < self.limit:
                    break
                t = self.day_start_utc(t) + timedelta(days=1)  # the daily limit is reached: next day
            out[pid] = t
            prev, count = t, count + 1
        return out
