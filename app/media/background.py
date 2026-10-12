"""Filter graphs that turn any source frame into a WxH vertical frame."""
from __future__ import annotations


def is_vertical(width: int, height: int, target_w: int, target_h: int, tolerance: float) -> bool:
    if not width or not height:
        return False
    target = target_w / target_h
    return abs(width / height - target) / target <= tolerance


def fill_crop(w: int, h: int, flags: str = "lanczos") -> str:
    """Scale to cover WxH, then centre-crop. Used for near-9:16 sources and the crop mode."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags={flags},crop={w}:{h},setsar=1"


def blurred_background(w: int, h: int, blur: int, darken: float, flags: str = "lanczos") -> str:
    """Blurred, darkened copy as background with the untouched original centred on top.

    The background is blurred at quarter resolution: same look, several times faster.
    Output label: [vbase].
    """
    bw, bh = max(2, w // 4 // 2 * 2), max(2, h // 4 // 2 * 2)
    return (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur={blur}:2,eq=brightness={-abs(darken):.3f},scale={w}:{h},setsar=1[bg];"
        f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease:flags={flags},setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[vbase]"
    )


def base_graph(mode: str, src_w: int, src_h: int, w: int, h: int, tolerance: float,
               blur: int, darken: float, flags: str = "lanczos") -> tuple[str, str]:
    """Returns (filter graph producing [vbase], profile name)."""
    if is_vertical(src_w, src_h, w, h, tolerance):
        return f"[0:v]{fill_crop(w, h, flags)}[vbase]", "vertical_original"
    if mode == "crop":
        return f"[0:v]{fill_crop(w, h, flags)}[vbase]", "smart_crop"
    return blurred_background(w, h, blur, darken, flags), "blurred"
