import copy
import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.analyzer import Analyzer, build_provider, parse_llm_json
from app.ai.base import AnalysisContext, LLMError, LLMProvider
from app.ai.openai import OpenAICompatibleProvider
from app.config.settings import Settings
from app.database.database import Database
from app.database.models import ContentItem, RightsStatus, Status
from app.media.ffprobe import MediaInfo
from app.media.metrics import MediaMetrics
from app.media.samples import make_samples
from app.processing import review
from app.processing.pipeline import Pipeline
from app.scoring import moderation
from app.scoring.decision import decide
from app.scoring.quality import quality_gate, quality_score
from app.scoring.virality import content_score, format_score, freshness_score

THRESHOLDS = {"min_quality_score": 60, "min_viral_score": 70, "min_viral_score_rules": 40, "min_content_score": 55}


def _info(w=1920, h=1080, audio=True, bitrate=5_000_000):
    return MediaInfo(duration=10, width=w, height=h, fps=30, codec="h264", bitrate=bitrate,
                     filesize=1, has_audio=audio)


def _ctx(**kw):
    base = dict(title="clip", caption=None, source="filesystem", channel=None, source_category="other",
                rights_status="USER_OWNED", duration=10, width=1080, height=1920, has_audio=True,
                transcript="Знаете ли вы этот удивительный факт? У осьминога три сердца! Смотрите до конца!",
                transcript_language="ru", quality_score=90, quality_parts={}, metrics={})
    return AnalysisContext(**{**base, **kw})


# ---------- scores ----------

def test_quality_score_good_vs_bad():
    good = MediaMetrics(10, 0.0, 0.0, 4.0, -18.0, 0.1)
    q_good, parts = quality_score(_info(), good)
    assert q_good == 100 and set(parts) == {"resolution", "bitrate", "sharpness", "motion", "not_black", "audio"}
    bad = MediaMetrics(10, 0.9, 1.0, None, -91.0, 1.0)
    assert quality_score(_info(audio=True), bad)[0] < 40
    assert "чёрный" in quality_gate(bad, {})
    assert "размыто" in quality_gate(MediaMetrics(10, 0, 0, 30.0, -18, 0), {})
    assert quality_gate(good, {}) is None


def test_content_score_uses_configurable_weights():
    scores = {"viral": 100, "quality": 0, "originality": 0, "source": 0, "freshness": 0, "format": 0}
    assert content_score(scores) == 40.0
    assert content_score(scores, {"viral": 1, "quality": 0, "originality": 0, "source": 0,
                                  "freshness": 0, "format": 0}) == 100.0


def test_freshness_and_format():
    now = datetime(2026, 10, 6)
    assert freshness_score(now, now) == 100
    assert freshness_score(now - timedelta(days=365), now) == 30
    assert format_score(1080, 1920) > format_score(1080, 1080) > format_score(1920, 1080)


def test_moderation_keywords():
    assert moderation.keyword_flags("как сделать бомбу и купить наркотики") == {"illegal"}
    assert moderation.keyword_flags("мой кот бомбически смешной") == set()
    assert moderation.verdict({"illegal"}) == "reject"
    assert moderation.verdict({"dangerous"}) == "review"
    assert moderation.verdict(set()) is None


@pytest.mark.parametrize("kw,expected", [
    ({}, Status.APPROVED),
    ({"rights": RightsStatus.UNKNOWN}, Status.MANUAL_REVIEW),
    ({"rights": RightsStatus.REQUIRES_REVIEW}, Status.MANUAL_REVIEW),
    ({"flags": {"illegal"}}, Status.REJECTED),
    ({"flags": {"dangerous"}}, Status.MANUAL_REVIEW),
    ({"quality": 40}, Status.REJECTED),
    ({"gate_reason": "чёрный"}, Status.REJECTED),
    ({"viral": 50, "provider": "rules"}, Status.APPROVED),
    ({"viral": 50, "provider": "openai"}, Status.REJECTED),
    ({"content": 30}, Status.REJECTED),
    ({"ai_action": "reject"}, Status.MANUAL_REVIEW),
])
def test_decision_rules(kw, expected):
    args = dict(quality=90, gate_reason=None, viral=80, content=80, provider="openai", flags=set(),
                ai_action="approve", rights=RightsStatus.USER_OWNED, thresholds=THRESHOLDS)
    assert decide(**{**args, **kw}).status == expected


# ---------- AI providers ----------

def test_parse_llm_json_clamps_and_validates():
    text = 'Sure! {"category": "Recipes", "viral_score": 140, "moderation_flags": ["Gore"], ' \
           '"recommended_action": "approve", "originality_score": "x"}'
    r = parse_llm_json(text, ["recipes", "other"], "openai")
    assert (r.category, r.viral_score, r.moderation_flags, r.originality_score) == ("recipes", 100, ["gore"], None)
    assert parse_llm_json('{"category": "unknown", "viral_score": 5}', ["other"], "x").category == "uncertain"
    with pytest.raises(LLMError):
        parse_llm_json("no json here", ["other"], "x")
    with pytest.raises(LLMError):
        parse_llm_json('{"category": "other"}', ["other"], "x")


def test_provider_is_switched_by_config_only(settings):
    settings.raw["ai"]["provider"] = "rules"
    assert build_provider(settings) is None
    settings.raw["ai"]["provider"] = "openai"
    assert isinstance(build_provider(settings), OpenAICompatibleProvider)
    settings.raw["ai"]["provider"] = "nope"
    with pytest.raises(ValueError):
        build_provider(settings)


class _Broken(LLMProvider):
    name = "broken"

    def available(self):
        return True, ""

    def generate(self, system, prompt, max_tokens=700):
        return "I refuse to answer in JSON"


def test_analyzer_falls_back_to_rules_on_bad_ai_output(settings):
    a = Analyzer(settings)
    a.provider = _Broken()
    res = a.analyze(_ctx())
    assert res.provider == "rules" and res.category == "facts" and res.category_confidence >= 60
    assert any("использованы правила" in n for n in res.notes)


class _FakeOpenAI(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOpenAI.requests.append((self.path, self.headers.get("Authorization"), body))
        answer = {"category": "facts", "category_confidence": 85, "category_reason": "факт о животном",
                  "viral_score": 88, "originality_score": 70, "rights_risk_score": 20,
                  "moderation_flags": [], "recommended_action": "approve", "reason": "strong hook",
                  "summary": "Факт об осьминоге", "hook": "Три сердца!"}
        out = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_openai(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _FakeOpenAI.requests.clear()
    yield _FakeOpenAI.requests
    server.shutdown()


def test_openai_compatible_provider_over_http(settings, fake_openai):
    settings.raw["ai"]["provider"] = "openai"
    a = Analyzer(settings)
    res = a.analyze(_ctx())
    assert (res.provider, res.category, res.viral_score, res.hook) == ("openai", "facts", 88, "Три сердца!")
    path, auth, body = fake_openai[0]
    assert path == "/v1/chat/completions" and auth == "Bearer test-key"
    assert body["messages"][0]["role"] == "system" and "осьминога" in body["messages"][1]["content"]


# ---------- review ----------

def test_review_approve_and_reject(settings, db):
    raw = settings.data("raw") / "x.mp4"
    raw.write_bytes(b"x")
    with db.session() as s:
        a = ContentItem(source="t", source_id="a", original_file=str(raw), original_filename="a.mp4",
                        status=Status.MANUAL_REVIEW, rights_status=RightsStatus.UNKNOWN)
        b = ContentItem(source="t", source_id="b", original_file=str(raw), original_filename="b.mp4",
                        status=Status.MANUAL_REVIEW, rights_status=RightsStatus.UNKNOWN)
        s.add_all([a, b])
        s.flush()
        review.review_link(settings, a)
        review.approve(settings, a, "автор разрешил")
        review.reject(settings, b)
        with pytest.raises(ValueError):
            review.approve(settings, b)
    assert (a.status, a.rights_status) == (Status.APPROVED, RightsStatus.VERIFIED)
    assert b.status == Status.REJECTED and b.reviewed_at
    assert not list(settings.data("review").iterdir())


# ---------- full analysis on the 10 sample clips ----------

@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    return make_samples(tmp_path_factory.mktemp("samples"))


def test_ten_samples_bad_rejected_good_pass(tmp_path, real_config, samples):
    cfg = copy.deepcopy(real_config)
    cfg["ai"]["provider"] = "rules"
    cfg["sources"]["filesystem"]["folders"] = [
        {"path": "in", "category": "other", "rights_status": "USER_OWNED", "source_score": 70},
        {"path": "in_foreign", "category": "other", "rights_status": "UNKNOWN", "source_score": 50},
    ]
    settings = Settings(root=tmp_path, raw=cfg)
    settings.ensure_dirs()
    (tmp_path / "in").mkdir()
    (tmp_path / "in_foreign").mkdir()
    for path, _ in samples:
        (tmp_path / "in" / path.name).symlink_to(path)
    # Same kind of clip but from a folder with unknown rights ("life" video differs on every run).
    (foreign_src, _), = make_samples(tmp_path / "gen", only=["good_fact"])
    foreign_src.rename(tmp_path / "in_foreign" / "foreign_fact.mp4")

    db = Database(settings.database_path)
    db.create_all()
    Pipeline(settings, db).run_once()
    with db.session() as s:
        items = {Path(i.source_id).stem: i for i in s.scalars(select(ContentItem))}
    db.dispose()

    for path, expected in samples:
        item = items[path.stem]
        record = json.loads(item.analysis_json)
        if expected == "good":
            assert record["decision"] == {"status": "APPROVED", "rule": "passed", "reason": item.decision_reason}
            assert item.status == Status.READY_TO_PUBLISH, (path.stem, item.decision_reason)
            assert item.transcript and item.transcript_language == "ru"
            assert item.subtitle_file and item.category != "other"
            assert record["scores"]["viral"] == item.viral_score and record["content_score"] == item.content_score
        else:
            assert item.status == Status.REJECTED, (path.stem, item.decision_reason)
            assert record["decision"]["status"] == "REJECTED" and record["decision"]["rule"] != "passed"

    assert items["bad_moderation"].moderation_flags == "illegal"
    foreign = items["foreign_fact"]
    assert (foreign.rights_status, foreign.status) == (RightsStatus.UNKNOWN, Status.MANUAL_REVIEW)
    assert (settings.data("review") / f"{foreign.id}_foreign_fact.mp4").is_symlink()
    assert not list(settings.data("final").glob(f"*_{foreign.id}.mp4"))


# ---------- categories ----------

def test_category_uncertain_when_data_is_thin(settings):
    a = Analyzer(settings)
    music_only = a.analyze(_ctx(transcript=None, title="клип", caption=None))
    assert music_only.category == "uncertain" and "мало данных" in music_only.category_reason
    recipe = a.analyze(_ctx(transcript=None, title="Рецепт",
                            caption="Готовим пасту с чесноком и оливковым маслом за 10 минут"))
    assert recipe.category == "recipes" and recipe.category_confidence >= 60


def test_channel_category_is_only_a_hint(settings):
    a = Analyzer(settings)
    res = a.analyze(_ctx(transcript="Невероятный гол на последней минуте, вратарь не успел", category_hint="recipes"))
    assert res.category == "sports"
    hinted = a.analyze(_ctx(transcript="Стример в чате ответил донатеру", category_hint="stream_clips"))
    assert hinted.category == "stream_clips" and "категорией канала" in hinted.category_reason


def test_low_ai_confidence_becomes_uncertain(settings):
    class Unsure(LLMProvider):
        name = "unsure"

        def available(self):
            return True, ""

        def generate(self, system, prompt, max_tokens=700):
            return json.dumps({"category": "memes", "category_confidence": 30, "viral_score": 50})
    a = Analyzer(settings)
    a.provider = Unsure()
    res = a.analyze(_ctx())
    assert res.category == "uncertain" and "предполагалось: memes" in res.category_reason


def test_ai_only_moderation_flags_go_to_review():
    args = dict(quality=90, gate_reason=None, viral=80, content=80, provider="llamacpp", flags=set(),
                ai_action="approve", rights=RightsStatus.USER_OWNED, thresholds=THRESHOLDS)
    d = decide(**args, ai_flags={"hate"})
    assert d.status == Status.MANUAL_REVIEW and "hate" in d.reason
    assert decide(**{**args, "flags": {"illegal"}}, ai_flags={"illegal"}).status == Status.REJECTED
