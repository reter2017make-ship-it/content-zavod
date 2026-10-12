"""Pixabay video API (https://pixabay.com/api/docs/#api_search_videos). Pixabay Content License."""
from __future__ import annotations

from app.sources.base import ApiConnector, DiscoveredItem

API = "https://pixabay.com/api/videos/"
SIZES = ("large", "medium", "small", "tiny")


class PixabaySource(ApiConnector):
    name = "pixabay"
    required_env = ("PIXABAY_API_KEY",)

    @staticmethod
    def best_file(videos: dict) -> dict | None:
        for size in SIZES:
            f = videos.get(size) or {}
            if f.get("url") and (f.get("width") or 0) > 0:
                return f
        return None

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        found: list[DiscoveredItem] = []
        per_query = max(3, int(self.cfg.get("per_query", 5)))  # API minimum is 3
        seen = set(known_ids)
        for q in self.cfg.get("queries", []):
            query, category = (q["query"], q.get("category", "other")) if isinstance(q, dict) else (q, "other")
            data = self.client.get_json(API, params={"key": self.keys["PIXABAY_API_KEY"], "q": query,
                                                     "per_page": per_query, "safesearch": "true"})
            for h in data.get("hits", []):
                sid = f"pixabay:{h['id']}"
                if sid in seen or not self.duration_ok(h.get("duration")):
                    continue
                f = self.best_file(h.get("videos") or {})
                if not f:
                    continue
                seen.add(sid)
                found.append(DiscoveredItem(
                    source=self.name, source_id=sid, source_url=h.get("pageURL"),
                    title=h.get("tags") or query, author=h.get("user"), category=category,
                    rights_status="LICENSED", license="Pixabay Content License",
                    license_url="https://pixabay.com/service/license-summary/",
                    attribution=f"Video by {h.get('user', 'unknown')} from Pixabay: {h.get('pageURL')}",
                    media_url=f["url"], duration=h.get("duration"),
                    extra={"source_score": self.cfg.get("source_score", 60), "pixabay_id": h["id"],
                           "views": h.get("views"), "likes": h.get("likes"), "downloads": h.get("downloads")},
                ))
                if len(found) >= limit:
                    return found
        return found

    def metadata(self, item_source_id: str) -> dict:
        data = self.client.get_json(API, params={"key": self.keys["PIXABAY_API_KEY"],
                                                 "id": item_source_id.removeprefix("pixabay:")})
        h = (data.get("hits") or [{}])[0]
        return {"url": h.get("pageURL"), "author": h.get("user"), "duration": h.get("duration"),
                "tags": h.get("tags"), "license": "Pixabay Content License"}
