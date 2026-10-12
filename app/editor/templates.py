"""Ready-made templates (sport, stream clips, memes). They are added to the base once; the owner can
change or delete them like their own. Texts are left empty: the frame for a caption is there, the words
are typed in the editor (an empty text is not drawn, so a batch export never shows a placeholder)."""
from __future__ import annotations

from app.editor.spec import CANVAS_H, CANVAS_W, CHROMA_BLEND, CHROMA_SIMILARITY

W, H = CANVAS_W, CANVAS_H
CHROMA = {"similarity": CHROMA_SIMILARITY, "blend": CHROMA_BLEND}


def _text(y: int, size: int = 72, color: str = "#ffffff", box: bool = False, box_color: str = "#000000b0") -> dict:
    return {"text": "", "x": 60, "y": y, "w": W - 120, "size": size, "color": color, "box": box,
            "box_color": box_color, "font": "heavy", "align": "center"}


def _window(w: int, h: int, y: int | None = None, **kw) -> dict:
    return {"x": (W - w) // 2, "y": (H - h) // 2 if y is None else y, "w": w, "h": h, **kw}


BUILTIN: list[tuple[str, str, dict]] = [
    ("blur_center", "Размытый фон: ролик целиком по центру", {
        "background": {"type": "blur", "blur": 40, "darken": 0.25},
        "video": _window(W, H, fit="contain"),
    }),
    ("meme_top_bar", "Мем: белая плашка с текстом сверху", {
        "background": {"type": "blur", "blur": 36, "darken": 0.3},
        "video": _window(W, 960, y=560, fit="cover"),
        "texts": [_text(300, 76, "#111111", box=True, box_color="#ffffffff")],
    }),
    ("news_lower_third", "Новости: заголовок сверху, плашка снизу", {
        "background": {"type": "color", "color": "#0b1020"},
        "video": _window(W, 608, y=640, fit="cover"),
        "texts": [_text(380, 64, "#ffffff"), _text(1340, 52, "#ffffff", box=True, box_color="#c1121fee")],
    }),
    ("podcast_quote", "Подкаст / цитата: окно и крупные караоке-субтитры", {
        "background": {"type": "blur", "blur": 40, "darken": 0.45},
        "video": _window(900, 900, y=300, fit="cover", shape="rounded", radius=48, shadow=True),
        "subtitles": {"enabled": True, "preset": "karaoke", "font": "heavy", "size": 84, "color": "#ffffff",
                      "stroke": 0.10, "stroke_color": "#000000", "karaoke": True, "highlight": "#ffd600", "y": 1480},
    }),
    ("cinema", "Кино: чёрные полосы и тонкие подписи", {
        "background": {"type": "color", "color": "#000000"},
        "video": _window(W, 608, fit="cover"),
        "texts": [_text(420, 56, "#e5e5e5"), _text(1420, 44, "#bdbdbd")],
    }),
    ("sport_window", "Спорт: окно со скруглениями на размытом фоне", {
        "background": {"type": "blur", "blur": 30, "darken": 0.3},
        "video": _window(1000, 562, fit="cover", shape="rounded", radius=40, shadow=True),
        "texts": [_text(470, 80, "#ffd600")],
    }),
    ("sport_full_bar", "Спорт: весь экран, плашка снизу", {
        "background": {"type": "blur"},
        "video": _window(W, H, fit="cover"),
        "texts": [_text(1420, 64, "#ffffff", box=True, box_color="#000000c8")],
    }),
    ("stream_top", "Стрим: окно сверху, подпись снизу", {
        "background": {"type": "blur", "blur": 30, "darken": 0.35},
        "video": _window(W, 608, y=360, fit="cover", shape="rounded", radius=28),
        "texts": [_text(1060, 68)],
    }),
    ("stream_full", "Стрим: весь экран (обрезка по центру)", {
        "background": {"type": "blur"},
        "video": _window(W, H, fit="cover"),
    }),
    ("meme_white", "Мем: белая рамка, текст сверху", {
        "background": {"type": "color", "color": "#ffffff"},
        "video": _window(1000, 1000, y=640, fit="contain", shape="rounded", radius=24),
        "texts": [_text(330, 72, "#111111")],
    }),
    ("meme_black", "Мем: чёрная рамка, текст сверху", {
        "background": {"type": "color", "color": "#000000"},
        "video": _window(1000, 1000, y=640, fit="contain", shape="rounded", radius=24),
        "texts": [_text(330, 72, "#ffffff")],
    }),
    ("circle", "Круг в центре на размытом фоне", {
        "background": {"type": "blur", "blur": 34, "darken": 0.3},
        "video": _window(900, 900, fit="cover", shape="circle", border_width=12, border_color="#ffffff", shadow=True),
        "texts": [_text(320, 76)],
    }),
    ("square_gradient", "Квадрат со скруглениями на градиенте", {
        "background": {"type": "gradient", "color": "#1e1b4b", "color2": "#be185d", "angle": 160},
        "video": _window(980, 980, fit="cover", shape="rounded", radius=56, shadow=True),
        "texts": [_text(300, 72)],
    }),
]


def builtin_specs() -> list[tuple[str, str, dict]]:
    out = []
    for key, name, spec in BUILTIN:
        spec = {**spec, "banner": {"chroma": dict(CHROMA)}}
        if "subtitles" not in spec:
            spec["subtitles"] = {"enabled": True}
        out.append((key, name, spec))
    return out
