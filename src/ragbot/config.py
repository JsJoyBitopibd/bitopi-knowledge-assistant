"""Load config/settings.yaml and .env once; expose typed settings and helpers."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


class Settings:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, path: str) -> Any:
        """settings['retrieval.final_top_k']"""
        cur: Any = self._d
        for part in path.split("."):
            cur = cur[part]
        return cur

    def get(self, path: str, default: Any = None) -> Any:
        try:
            return self[path]
        except (KeyError, TypeError):
            return default

    def path(self, key: str, default: str | None = None) -> Path:
        """paths.<key>, relative to the repo root. `default` covers keys added after a local
        config/settings.yaml was copied from the example."""
        raw = self.get("paths." + key, default)
        if raw is None:
            raise KeyError(f"paths.{key}")
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def settings() -> Settings:
    f = ROOT / "config" / "settings.yaml"
    if not f.exists():
        f = ROOT / "config" / "settings.example.yaml"
    with open(f, encoding="utf-8") as fh:
        return Settings(yaml.safe_load(fh))


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing environment variable {name} (see .env.example)")
    return v


def prompt(name: str) -> str:
    """prompt('system_answer') -> contents of prompts/system_answer.txt"""
    return (settings().path("prompts_dir") / f"{name}.txt").read_text(encoding="utf-8")


def log_dir() -> Path:
    d = Path(os.getenv("LOG_DIR", "logs"))
    d = d if d.is_absolute() else ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d
