"""Parsing calendar: on which weekdays and at what times the channels are checked.

A schedule without a channel covers every channel that has no enabled schedule of its own. A slot that
was missed (the Mac was asleep or off) is caught up only within `catch_up_hours`; older ones are skipped.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database.models import Channel, ScanSchedule, utcnow

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    pass


def parse_days(days) -> list[int]:
    out = sorted({int(d) for d in days if str(d).strip() != "" and 0 <= int(d) <= 6})
    if not out:
        raise ScheduleError("выберите хотя бы один день")
    return out


def parse_times(times) -> list[str]:
    out = set()
    for t in times:
        m = TIME_RE.match(str(t).strip())
        if not m:
            raise ScheduleError(f"время «{t}»: нужен формат ЧЧ:ММ")
        out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if not out:
        raise ScheduleError("добавьте хотя бы одно время")
    if len(out) > 24:
        raise ScheduleError("не больше 24 запусков в день")
    return sorted(out)


class ParseCalendar:
    def __init__(self, settings, db):
        self.db = db
        cfg = settings.get("schedule", {}) or {}
        self.tz = ZoneInfo(cfg.get("timezone") or settings.get("publishing.timezone", "Europe/Moscow"))
        self.catch_up = timedelta(hours=float(cfg.get("catch_up_hours", 3)))
        self.default_times = cfg.get("default_times") or ["10:00", "19:00"]

    # ---------- slots ----------
    def _local(self, utc_naive: datetime) -> datetime:
        return utc_naive.replace(tzinfo=timezone.utc).astimezone(self.tz)

    @staticmethod
    def _to_utc(local: datetime) -> datetime:
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def _slots(self, sch: ScanSchedule, around: datetime, back_days: int, fwd_days: int):
        days = {int(d) for d in sch.days.split(",") if d != ""}
        times = [t for t in sch.times.split(",") if t]
        base = around.date()
        for off in range(-back_days, fwd_days + 1):
            day = base + timedelta(days=off)
            if day.weekday() not in days:
                continue
            for t in times:
                h, m = map(int, t.split(":"))
                yield datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)

    def last_slot(self, sch: ScanSchedule, now_utc: datetime) -> datetime | None:
        now = self._local(now_utc)
        past = [s for s in self._slots(sch, now, 8, 0) if s <= now]
        return self._to_utc(max(past)) if past else None

    def next_slot(self, sch: ScanSchedule, now_utc: datetime) -> datetime | None:
        now = self._local(now_utc)
        future = [s for s in self._slots(sch, now, 0, 8) if s > now]
        return self._to_utc(min(future)) if future else None

    # ---------- what to run ----------
    def ensure_default(self) -> None:
        """First start: one calendar for all channels (what used to be «every 30 minutes»)."""
        with self.db.session() as s:
            if s.scalar(select(ScanSchedule.id).limit(1)) is None:
                s.add(ScanSchedule(channel_id=None, days="0,1,2,3,4,5,6", times=",".join(parse_times(self.default_times)),
                                   enabled=True, last_run_at=utcnow()))

    def due(self, now_utc: datetime | None = None) -> list[tuple[int, list[int]]]:
        """[(schedule id, channel ids to parse)] for every schedule whose slot has come."""
        now = now_utc or utcnow()
        out = []
        with self.db.session() as s:
            scheds = list(s.scalars(select(ScanSchedule).where(ScanSchedule.enabled.is_(True))))
            own = {x.channel_id for x in scheds if x.channel_id is not None}
            enabled = {c.id for c in s.scalars(select(Channel).where(Channel.enabled.is_(True)))}
            for sch in scheds:
                slot = self.last_slot(sch, now)
                if slot is None or now - slot > self.catch_up:
                    continue
                if sch.last_run_at and sch.last_run_at >= slot:
                    continue
                ids = ([sch.channel_id] if sch.channel_id in enabled else []) if sch.channel_id is not None \
                    else sorted(enabled - own)
                out.append((sch.id, ids))
        return out

    def mark_run(self, schedule_id: int, when: datetime | None = None) -> None:
        with self.db.session() as s:
            sch = s.get(ScanSchedule, schedule_id)
            if sch:
                sch.last_run_at = when or utcnow()

    def next_run(self, now_utc: datetime | None = None) -> datetime | None:
        now = now_utc or utcnow()
        with self.db.session() as s:
            nxt = [self.next_slot(x, now) for x in s.scalars(select(ScanSchedule).where(ScanSchedule.enabled.is_(True)))]
        nxt = [x for x in nxt if x]
        return min(nxt) if nxt else None

    # ---------- editing (web UI) ----------
    def as_json(self, sch: ScanSchedule, channel: Channel | None, now: datetime) -> dict:
        iso = lambda d: d.isoformat() + "Z" if d else None  # noqa: E731
        return {"id": sch.id, "channel_id": sch.channel_id, "channel": channel.username if channel else None,
                "days": [int(d) for d in sch.days.split(",") if d != ""], "times": [t for t in sch.times.split(",") if t],
                "enabled": sch.enabled, "last_run_at": iso(sch.last_run_at),
                "next_run": iso(self.next_slot(sch, now)) if sch.enabled else None}

    def list(self) -> dict:
        now = utcnow()
        with self.db.session() as s:
            chans = {c.id: c for c in s.scalars(select(Channel).order_by(Channel.username))}
            rows = s.scalars(select(ScanSchedule).order_by(ScanSchedule.channel_id.is_not(None), ScanSchedule.id))
            items = [self.as_json(x, chans.get(x.channel_id), now) for x in rows]
        nxt = self.next_run(now)
        return {"schedules": items, "next_run": nxt.isoformat() + "Z" if nxt else None,
                "timezone": str(self.tz), "channels": [{"id": c.id, "username": c.username} for c in chans.values()]}

    def create(self, channel_id: int | None, days, times, enabled: bool = True) -> int:
        days, times = parse_days(days), parse_times(times)
        with self.db.session() as s:
            if channel_id is not None and s.get(Channel, channel_id) is None:
                raise ScheduleError("нет такого канала")
            # last_run_at = now: a new calendar starts from its next slot, it does not fire at once
            sch = ScanSchedule(channel_id=channel_id, days=",".join(map(str, days)), times=",".join(times),
                               enabled=enabled, last_run_at=utcnow())
            s.add(sch)
            s.flush()
            return sch.id

    def update(self, schedule_id: int, days=None, times=None, enabled: bool | None = None) -> None:
        with self.db.session() as s:
            sch = s.get(ScanSchedule, schedule_id)
            if sch is None:
                raise ScheduleError("нет такого расписания")
            if days is not None:
                sch.days = ",".join(map(str, parse_days(days)))
            if times is not None:
                sch.times = ",".join(parse_times(times))
            if enabled is not None:
                sch.enabled = enabled
            sch.last_run_at = utcnow()  # an edited calendar waits for its next slot

    def delete(self, schedule_id: int) -> None:
        with self.db.session() as s:
            sch = s.get(ScanSchedule, schedule_id)
            if sch:
                s.delete(sch)
