"""Local model smoke test (llama.cpp + Qwen). Skipped when the model is not downloaded."""
import pytest

from app.ai.analyzer import Analyzer
from tests.test_analysis import _ctx


@pytest.fixture(scope="module")
def llm_analyzer(base_config):
    import copy

    from app.config.settings import PROJECT_ROOT, Settings
    cfg = copy.deepcopy(base_config)
    cfg["ai"]["provider"] = "llamacpp"
    a = Analyzer(Settings(root=PROJECT_ROOT, raw=cfg))
    a.provider.allow_download = False
    ready, why = a.provider.available()
    if not ready:
        pytest.skip(why)
    return a


def test_local_model_categorises_and_scores(llm_analyzer):
    res = llm_analyzer.analyze(_ctx(transcript="Сегодня готовим быструю пасту: чеснок, оливковое масло, помидоры.",
                                    category_hint="recipes"))
    assert res.provider == "llamacpp" and res.category == "recipes" and res.category_confidence >= 60
    assert 0 <= res.viral_score <= 100 and res.category_reason


def test_local_model_does_not_guess_on_thin_data(llm_analyzer):
    res = llm_analyzer.analyze(_ctx(transcript=None, title="🔥", caption=None))
    assert res.category == "uncertain"
