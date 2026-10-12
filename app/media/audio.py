from __future__ import annotations


def loudnorm(target_lufs: float) -> str:
    return f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11,aresample=48000"
