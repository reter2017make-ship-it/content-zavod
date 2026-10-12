"""Wikimedia Commons (MediaWiki API). No key needed. Each file's licence is mapped to a rights status;
anything that does not allow commercial reuse with modification is blocked."""
from __future__ import annotations

import html
import re

from app.sources.base import ApiConnector, DiscoveredItem

API = "https://commons.wikimedia.org/w/api.php"


def _plain(value: str | None) -> str | None:
    if not value:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip() or None


def map_license(short_name: str | None) -> tuple[str, dict]:
    """→ (rights_status, facts about commercial use / derivatives / attribution)."""
    name = (short_name or "").strip().lower()
    facts = {"commercial_use_allowed": False, "derivative_allowed": False, "attribution_required": True}
    if not name:
        return "REQUIRES_REVIEW", facts
    if "-nc" in name or " nc" in name or "-nd" in name or " nd" in name or "noncommercial" in name:
        return "REJECTED", facts  # non-commercial or no-derivatives: cannot be reused as Shorts
    if name.startswith("cc0") or name == "cc-zero":
        return "CC0", {"commercial_use_allowed": True, "derivative_allowed": True, "attribution_required": False}
    if "public domain" in name or name.startswith("pd"):
        return "PUBLIC_DOMAIN", {"commercial_use_allowed": True, "derivative_allowed": True,
                                 "attribution_required": False}
    if re.match(r"cc[ -]by[ -]sa", name):
        return "CC_BY_SA", {"commercial_use_allowed": True, "derivative_allowed": True, "attribution_required": True}
    if re.match(r"cc[ -]by", name):
        return "CC_BY", {"commercial_use_allowed": True, "derivative_allowed": True, "attribution_required": True}
    return "REQUIRES_REVIEW", facts  # GFDL, FAL, custom terms...


class WikimediaSource(ApiConnector):
    name = "wikimedia"
    # Wikimedia's robot policy rejects API clients whose User-Agent has no contact address.
    required_env = ("WIKIMEDIA_CONTACT",)

    def default_headers(self) -> dict:
        contact = self.keys.get("WIKIMEDIA_CONTACT", "")
        return {"User-Agent": f"ContentFactory/0.3 ({contact}) python-httpx"}

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        found: list[DiscoveredItem] = []
        per_query = int(self.cfg.get("per_query", 5))
        seen = set(known_ids)
        for q in self.cfg.get("queries", []):
            query, category = (q["query"], q.get("category", "other")) if isinstance(q, dict) else (q, "other")
            data = self.client.get_json(API, params={
                "action": "query", "format": "json", "formatversion": "2",
                "generator": "search", "gsrsearch": f"filetype:video {query}", "gsrnamespace": "6",
                "gsrlimit": per_query * 3,
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata|user", "iiextmetadatafilter":
                    "LicenseShortName|LicenseUrl|Artist|Credit|AttributionRequired|UsageTerms|ImageDescription",
            })
            taken = 0
            for page in (data.get("query") or {}).get("pages", []):
                info = (page.get("imageinfo") or [{}])[0]
                sid = f"wikimedia:{page.get('pageid')}"
                if sid in seen or not info.get("url") or not str(info.get("mime", "")).startswith("video/"):
                    continue
                if not self.duration_ok(info.get("duration")) or (info.get("size") or 0) > self.max_bytes:
                    continue
                meta = info.get("extmetadata") or {}
                lic = (meta.get("LicenseShortName") or {}).get("value")
                rights, facts = map_license(lic)
                if rights == "REJECTED" and not self.cfg.get("keep_blocked", False):
                    continue
                author = _plain((meta.get("Artist") or {}).get("value")) or info.get("user")
                title = page.get("title", "").removeprefix("File:")
                lic_url = (meta.get("LicenseUrl") or {}).get("value")
                seen.add(sid)
                found.append(DiscoveredItem(
                    source=self.name, source_id=sid, source_url=info.get("descriptionurl"),
                    title=title, author=author, category=category,
                    caption=_plain((meta.get("ImageDescription") or {}).get("value")),
                    rights_status=rights, license=lic, license_url=lic_url,
                    attribution=f"«{title}» — {author or 'unknown'}, {lic}, via Wikimedia Commons "
                                f"({info.get('descriptionurl')})",
                    media_url=info["url"], duration=info.get("duration"),
                    extra={"source_score": self.cfg.get("source_score", 55), "mime": info.get("mime"),
                           "size": info.get("size"), **facts},
                ))
                taken += 1
                if len(found) >= limit:
                    return found
                if taken >= per_query:
                    break
        return found

    def metadata(self, item_source_id: str) -> dict:
        data = self.client.get_json(API, params={
            "action": "query", "format": "json", "formatversion": "2",
            "pageids": item_source_id.removeprefix("wikimedia:"), "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|user"})
        page = ((data.get("query") or {}).get("pages") or [{}])[0]
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value")
        rights, facts = map_license(lic)
        return {"title": page.get("title"), "url": info.get("descriptionurl"), "license": lic,
                "rights_status": rights, "author": _plain((meta.get("Artist") or {}).get("value")), **facts}
