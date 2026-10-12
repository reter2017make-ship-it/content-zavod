from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.config.settings import PROJECT_ROOT, Settings
from app.database.database import Database
from app.media.ffmpeg import make_test_video
from app.processing.pipeline import Pipeline


def _strict(cfg: dict) -> None:
    """The filter mechanics are tested with the old strict values; the owner's config only marks."""
    cfg["filters"].update(min_duration=5, min_width=360, min_height=360)
    cfg["scores"]["moderation_reject"] = True
    cfg["scores"]["quality_gates"] = {"max_black_ratio": 0.5, "max_frozen_ratio": 0.8, "max_blur": 15}
    cfg["duplicates"]["visual_reject"] = True


@pytest.fixture(scope="session")
def base_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.example.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["processing"]["preset"] = "ultrafast"
    # The manual-review path stays in the code behind rights.auto_approve; most tests check it.
    # The owner's default (auto_approve: true) is covered in test_auto_approve.py.
    cfg["rights"] = {"auto_approve": False}
    cfg["processing"]["auto_render"] = True  # mechanics tests render in the background like before
    _strict(cfg)
    cfg["factory"]["retry_delay_minutes"] = 0
    cfg["sources"]["filesystem"]["folders"] = [
        {"path": "data/inbox", "category": "other", "rights_status": "USER_OWNED", "source_score": 70}]
    # Stage-1 mechanics tests use synthetic clips without speech: no whisper, no score thresholds.
    # Tests of the analysis stage switch these back to the real config values.
    cfg["transcription"]["enabled"] = False
    cfg["ai"]["provider"] = "rules"  # deterministic; the local model is checked separately (test_llm.py)
    cfg["scores"].update(min_quality_score=0, min_viral_score=0, min_viral_score_rules=0, min_content_score=0)
    return cfg


@pytest.fixture(scope="session")
def real_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.example.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["processing"]["preset"] = "ultrafast"
    cfg["ai"]["provider"] = "rules"
    cfg["rights"] = {"auto_approve": False}
    _strict(cfg)
    cfg["scores"].update(min_quality_score=60, min_viral_score=50, min_viral_score_rules=40, min_content_score=55)
    return cfg


@pytest.fixture
def settings(tmp_path: Path, base_config: dict) -> Settings:
    s = Settings(root=tmp_path, raw=copy.deepcopy(base_config))
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings):
    database = Database(settings.database_path)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def pipeline(settings: Settings, db: Database) -> Pipeline:
    return Pipeline(settings, db)


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> dict[str, Path]:
    """Synthetic clips shared by all tests (generated once)."""
    d = tmp_path_factory.mktemp("clips")
    return {
        "landscape": make_test_video(d / "landscape.mp4", 1280, 720, 6),
        "vertical_silent": make_test_video(d / "vertical.mp4", 720, 1280, 6, with_audio=False, pattern="smptebars"),
        "short": make_test_video(d / "short.mp4", 1280, 720, 2, pattern="rgbtestsrc"),
        "other": make_test_video(d / "other.mp4", 1280, 720, 6, pattern="mandelbrot", freq=700),
    }
