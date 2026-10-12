"""Exact (SHA-256) and perceptual (per-frame DCT pHash) duplicate detection."""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path

from app.media.ffmpeg import gray_frame

SIZE = 32
LOW = 8


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=1)
def _dct_matrix() -> list[list[float]]:
    return [[math.cos(math.pi * (2 * x + 1) * u / (2 * SIZE)) for x in range(SIZE)] for u in range(LOW)]


def frame_phash(pixels: bytes) -> int:
    """64-bit pHash: low 8×8 block of the 2-D DCT of a 32×32 grayscale frame, thresholded at its median."""
    m = _dct_matrix()
    rows = [pixels[y * SIZE:(y + 1) * SIZE] for y in range(SIZE)]
    # DCT along x for each row, keeping LOW coefficients, then along y.
    tmp = [[sum(c * p for c, p in zip(m[u], row)) for u in range(LOW)] for row in rows]
    coeffs = [sum(m[v][y] * tmp[y][u] for y in range(SIZE)) for v in range(LOW) for u in range(LOW)]
    ac = coeffs[1:]  # the DC term only reflects overall brightness
    median = sorted(ac)[len(ac) // 2]
    bits = 0
    for c in coeffs:
        bits = (bits << 1) | (c > median)
    return bits


def video_phash(path: Path, duration: float, frames: int = 5) -> str:
    """Hashes `frames` evenly spaced frames (skipping the very start/end) → hex string."""
    hashes = []
    for i in range(frames):
        at = duration * (i + 1) / (frames + 1)
        hashes.append(f"{frame_phash(gray_frame(path, at, SIZE)):016x}")
    return "".join(hashes)


def phash_distance(a: str, b: str) -> float:
    """Mean Hamming distance (0–64) between matching frame hashes."""
    n = min(len(a), len(b)) // 16
    if n == 0:
        return 64.0
    total = sum(bin(int(a[i*16:(i+1)*16], 16) ^ int(b[i*16:(i+1)*16], 16)).count("1") for i in range(n))
    return total / n
