"""Pexels video API (https://www.pexels.com/api/documentation/#videos). Pexels License: free use, no
attribution required (credit still saved); selling unaltered copies is not allowed."""
from __future__ import annotations

from app.sources.base import ApiConnector, DiscoveredItem

API = "https://api.pexels.com/videos/search"
ORIENTATIONS = ("portrait", "landscape", "square")


class PexelsSource(ApiConnector):
    name = "pexels"
    required_env = ("PEXELS_API_KEY",)

    def default_headers(self) -> dict:
        return {"Authorization": self.keys.get("PEXELS_API_KEY", "")}

    @staticmethod
    def best_file(files: list[dict], max_height: int = 1920) -> dict | None:
        mp4 = [f for f in files if f.get("file_type") == "video/mp4" and f.get("link")]
        fitting = [f for f in mp4 if (f.get("height") or 0) <= max_height and (f.get("width") or 0) <= max_height]
        pool = fitting or mp4
        return max(pool, key=lambda f: (f.get("width") or 0) * (f.get("height") or 0), default=None)

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        found: list[DiscoveredItem] = []
        per_query = int(self.cfg.get("per_query", 5))
        seen = set(known_ids)
        orientations = self.cfg.get("orientations") or list(ORIENTATIONS)
        for q in self.cfg.get("queries", []):
            query, category = (q["query"], q.get("category", "other")) if isinstance(q, dict) else (q, "other")
            for orientation in orientations:  # portrait > landscape > square for Shorts
                if len(found) >= limit:
                    return found
                data = self.client.get_json(API, params={"query": query, "orientation": orientation,
                                                         "per_page": per_query, "size": "medium"})
                for v in data.get("videos", []):
                    sid = f"pexels:{v['id']}"
                    if sid in seen or not self.duration_ok(v.get("duration")):
                        continue
                    f = self.best_file(v.get("video_files", []))
                    if not f:
                        continue
                    user = v.get("user") or {}
                    seen.add(sid)
                    found.append(DiscoveredItem(
                        source=self.name, source_id=sid, source_url=v.get("url"),
                        title=f"{query} ({orientation})", author=user.get("name"), channel=user.get("url"),
                        category=category, rights_status="LICENSED", license="Pexels License",
                        license_url="https://www.pexels.com/license/",
                        attribution=f"Video by {user.get('name', 'unknown')} on Pexels: {v.get('url')}",
                        media_url=f["link"], duration=v.get("duration"),
                        extra={"source_score": self.cfg.get("source_score", 60), "pexels_video_id": v["id"],
                               "orientation": orientation, "width": v.get("width"), "height": v.get("height")},
                    ))
                    if len(found) >= limit:
                        return found
        return found

    def metadata(self, item_source_id: str) -> dict:
        v = self.client.get_json(f"https://api.pexels.com/videos/videos/{item_source_id.removeprefix('pexels:')}")
        user = v.get("user") or {}
        return {"url": v.get("url"), "author": user.get("name"), "duration": v.get("duration"),
                "width": v.get("width"), "height": v.get("height"), "license": "Pexels License"}
