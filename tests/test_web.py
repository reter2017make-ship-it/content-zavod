import httpx
import pytest
from fastapi.testclient import TestClient

from app.sources.telegram import TelegramSource
from app.web.server import create_app
from app.web.worker import FactoryWorker
from tests.test_sources import FakeTelegramWeb


@pytest.fixture
def web(settings, db, clips, monkeypatch):
    monkeypatch.setattr("app.sources.http.time.sleep", lambda s: None)
    settings.raw["sources"]["telegram"] = {"enabled": True, "page_interval_seconds": 0.01, "parallel_channels": 3,
                                           "parallel_downloads": 3, "per_channel": 20, "recheck_minutes": 60}
    fake = FakeTelegramWeb({
        "cooking": [{"id": 1, "video": "ok", "text": "Готовим пасту с чесноком и оливковым маслом"},
                    {"id": 2, "video": "big"}],
        "memes": [{"id": 5, "video": "ok", "text": "🔥"}],
    }, {"cooking": clips["landscape"].read_bytes(), "memes": clips["other"].read_bytes()})
    tg = TelegramSource(settings, transport=httpx.MockTransport(fake), db=db)
    worker = FactoryWorker(settings, db, telegram=tg)
    with TestClient(create_app(settings, db, worker)) as client:
        yield client, worker, fake


def test_add_links_scans_in_background_and_shows_stats(web):
    client, worker, fake = web
    r = client.post("/api/channels", json={"links": "https://t.me/cooking @memes t.me/s/cooking", "category": "recipes"})
    assert r.status_code == 200 and r.json()["added"] == 2
    assert worker.wait_idle(60)

    chans = {c["username"]: c for c in client.get("/api/channels").json()["channels"]}
    cooking = chans["cooking"]
    assert cooking["state"] == "ok" and cooking["found"] == 1 and cooking["skipped"] == 1
    assert cooking["downloaded"] == 1 and cooking["review"] == 1 and cooking["title"] == "Кухня"
    assert "слишком большое" in cooking["last_skipped"][0]

    again = client.post("/api/channels", json={"links": "t.me/cooking"}).json()
    assert again == {"added": 0, "already_there": 1, "queued": 1, "restored": []}
    assert client.post("/api/channels", json={"links": "просто текст"}).status_code == 400


def test_recheck_takes_only_new_posts(web):
    client, worker, fake = web
    client.post("/api/channels", json={"links": "t.me/cooking"})
    assert worker.wait_idle(60)
    cid = client.get("/api/channels").json()["channels"][0]["id"]
    fake.channels["cooking"].append({"id": 3, "video": "ok", "text": "Новый рецепт супа"})
    fake.requests.clear()
    assert client.post(f"/api/channels/{cid}/check").json()["queued"] is True
    assert worker.wait_idle(60)
    ch = client.get("/api/channels").json()["channels"][0]
    assert ch["found"] == 2 and ch["last_seen_post_id"] == 3
    assert len([r for r in fake.requests if r.url.path.startswith("/s/")]) == 1


def test_toggle_delete_and_category_controls(web):
    client, worker, _ = web
    client.post("/api/channels", json={"links": "t.me/cooking t.me/memes", "category": "memes"})
    assert worker.wait_idle(60)
    chans = {c["username"]: c for c in client.get("/api/channels").json()["channels"]}
    cid = chans["memes"]["id"]

    assert client.patch(f"/api/channels/{cid}", json={"enabled": False}).json()["enabled"] is False
    assert client.post(f"/api/channels/{cid}/check").status_code == 409
    assert client.patch(f"/api/channels/{cid}", json={"category": "stream_clips", "mode": "discovery"}).status_code == 200
    assert client.patch(f"/api/channels/{cid}", json={"category": "nonsense"}).status_code == 400
    assert client.delete(f"/api/channels/{cid}").json()["ok"]
    assert [c["username"] for c in client.get("/api/channels").json()["channels"]] == ["cooking"]

    items = client.get("/api/items").json()["items"]
    by_channel = {i["channel"]: i for i in items}
    assert by_channel["cooking"]["category"] == "recipes"
    assert by_channel["memes"]["category"] == "uncertain"  # "🔥" and no speech: not enough to decide
    assert client.get("/api/items", params={"category": "recipes"}).json()["total"] == 1

    mid = by_channel["memes"]["id"]
    assert client.patch(f"/api/items/{mid}", json={"category": "memes"}).json()["ok"]
    item = next(i for i in client.get("/api/items").json()["items"] if i["id"] == mid)
    assert (item["category"], item["category_source"], item["category_confidence"]) == ("memes", "manual", 100.0)


def test_pages_are_served(web):
    client, _, _ = web
    assert "Сводка" in client.get("/").text  # the dashboard is the start page
    assert "Добавить каналы" in client.get("/sources").text
    assert "Ролики" in client.get("/videos").text
    assert client.get("/static/app.css").status_code == 200
    assert len(client.get("/api/categories").json()["categories"]) > 10


def test_review_from_ui_and_media(web):
    client, worker, _ = web
    client.post("/api/channels", json={"links": "t.me/cooking"})
    assert worker.wait_idle(60)
    item = next(i for i in client.get("/api/items").json()["items"] if i["status"] == "MANUAL_REVIEW")
    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["can_review"] and detail["video"] and detail["transcript"] is None
    assert client.get(detail["video"]).status_code == 200
    assert client.get(item["thumbnail"]).headers["content-type"] == "image/jpeg"

    assert client.post(f"/api/items/{item['id']}/approve", json={}).json()["status"] == "APPROVED"
    assert client.post(f"/api/items/{item['id']}/reject", json={}).status_code == 409  # no longer in review
    assert worker.wait_idle(60)
    assert client.get(f"/api/items/{item['id']}").json()["status"] == "READY_TO_PUBLISH"

    s = client.get("/api/summary").json()
    assert s["channels"] == 1 and s["ready"] >= 1
    assert client.get("/api/items", params={"q": "пасту"}).json()["total"] == 1


def test_pages_do_not_redeclare_shared_globals():
    """A page script that declares a name already declared in app.js/compose.js breaks the whole page
    (it happened with STATUS on «Instagram»)."""
    import re
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
    decl = re.compile(r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M)
    shared = set(decl.findall((static / "app.js").read_text(encoding="utf-8")))
    shared |= set(decl.findall((static / "compose.js").read_text(encoding="utf-8")))
    for page in static.glob("*.html"):
        scripts = "\n".join(re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S))
        clash = set(decl.findall(scripts)) & shared
        assert not clash, f"{page.name}: {sorted(clash)}"
