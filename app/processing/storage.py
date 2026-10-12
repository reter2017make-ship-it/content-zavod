from __future__ import annotations

import shutil
from pathlib import Path


def disk_usage_percent(path: Path) -> float:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return 100.0 * (usage.total - usage.free) / usage.total


def dir_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1024 ** 3
