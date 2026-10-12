"""«Мои каналы из Telegram»: the channels the signed-in account is subscribed to, with a tick «брать видео».

The list lives in the `channels` table (added_by = "account"). A channel already added by link is the same
row (no duplicates). Ticked channels are parsed like any other, always through the Telegram API, by the
parsing calendar. Nothing is changed in the Telegram account."""
from __future__ import annotations

import re

from sqlalchemy import select

from app.database.models import Channel, utcnow


def key(d: dict) -> str:
    return d["username"] or f"id{d['tg_id']}"


def refresh(db, api) -> dict:
    dialogs = api.dialogs()
    added = 0
    seen = set()
    with db.session() as s:
        rows = list(s.scalars(select(Channel).where(Channel.platform == "telegram")))
        by_id = {r.tg_id: r for r in rows if r.tg_id}
        by_name = {r.username.lower(): r for r in rows}
        for d in dialogs:
            ch = by_id.get(d["tg_id"]) or by_name.get(key(d).lower())
            if ch is None:
                ch = Channel(username=key(d), category="other", mode="import", via="api", enabled=False,
                             state="new", added_by="account")
                s.add(ch)
                added += 1
            ch.tg_id, ch.members, ch.in_account = d["tg_id"], d["members"], True
            ch.title = ch.title or d["title"]
            if ch.added_by == "account" or not d["username"]:
                ch.via = "api"  # private channels have no web version
            seen.add(d["tg_id"])
        # unsubscribed in Telegram: no longer listed as the account's (channels added by link stay)
        for r in rows:
            if r.in_account and r.tg_id not in seen:
                r.in_account = False
                if r.added_by == "account":
                    r.enabled = False
    return {"found": len(dialogs), "added": added, "at": utcnow().isoformat() + "Z"}


def hidden(db) -> list[dict]:
    with db.session() as s:
        return [{"id": c.id, "username": c.username, "title": c.title or c.username}
                for c in s.scalars(select(Channel).where(Channel.hidden.is_(True)).order_by(Channel.title))]


def listing(db) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(select(Channel).where(Channel.in_account.is_(True), Channel.hidden.is_not(True))
                         .order_by(Channel.title))
        return [{"id": c.id, "username": c.username, "public": not re.fullmatch(r"id\d+", c.username),
                 "title": c.title or c.username, "members": c.members, "enabled": c.enabled, "category": c.category,
                 "added_by": c.added_by, "avatar": f"/api/telegram-api/avatar/{c.id}"} for c in rows]


def select_channels(db, ids: list[int], enabled: bool) -> list[int]:
    """Ticks or unticks; returns the ids that were switched on (they get scanned at once)."""
    switched = []
    with db.session() as s:
        for c in s.scalars(select(Channel).where(Channel.id.in_(ids), Channel.hidden.is_not(True))):
            if enabled and not c.enabled:
                switched.append(c.id)
            c.enabled = enabled
    return switched
