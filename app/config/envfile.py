"""Writes secrets to .env (the only place they live). Other lines and comments are kept as they are."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

_lock = threading.Lock()


def set_env_values(env_path: Path, values: dict[str, str]) -> None:
    with _lock:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        pending = dict(values)
        for i, line in enumerate(lines):
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(1) in pending:
                lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
        lines += [f"{k}={v}" for k, v in pending.items()]
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(env_path)
        for k, v in values.items():
            os.environ[k] = v
