from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"

DATA_SUBDIRS = (
    "inbox", "inbox_unverified", "raw", "downloads", "processing", "final", "rejected",
    "failed", "thumbnails", "temp", "review", "assets", "editor_cache",
)


@dataclass
class Settings:
    root: Path
    raw: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def data_dir(self) -> Path:
        return self.path(self.get("paths.data_dir", "data"))

    def data(self, sub: str) -> Path:
        return self.data_dir / sub

    @property
    def database_path(self) -> Path:
        return self.path(self.get("paths.database", "database/content.db"))

    @property
    def logs_dir(self) -> Path:
        return self.path(self.get("paths.logs_dir", "logs"))

    @property
    def lock_path(self) -> Path:
        return self.root / ".factory.lock"

    def ensure_dirs(self) -> None:
        for sub in DATA_SUBDIRS:
            self.data(sub).mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def env_file(self) -> Path:
        """Where keys saved from the UI go: .env in the project, or FACTORY_ENV_FILE (Docker keeps it in a volume)."""
        return Path(os.environ.get("FACTORY_ENV_FILE") or self.root / ".env")

    @staticmethod
    def env(name: str) -> str:
        return os.environ.get(name, "").strip()


def load_settings(config_path: str | Path | None = None, root: str | Path | None = None) -> Settings:
    config_path = Path(config_path or os.environ.get("FACTORY_CONFIG") or DEFAULT_CONFIG)
    example = DEFAULT_CONFIG.with_name("config.example.yaml")
    if config_path == DEFAULT_CONFIG and not config_path.exists() and example.exists():
        import shutil
        shutil.copyfile(example, config_path)  # fresh clone: the user's own config starts from the example
    root_path = Path(root) if root else PROJECT_ROOT
    load_dotenv(Path(os.environ.get("FACTORY_ENV_FILE") or root_path / ".env"), override=False)
    with open(config_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Settings(root=root_path, raw=raw)
