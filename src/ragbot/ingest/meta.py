"""Access attributes of a PDF (PRD section 7): factory, department, confidentiality, buyer_code.

Defaults: factory = the first folder under data/pdfs when it is a factory code (data/pdfs/TAL/...),
otherwise ALL (a group-wide document); department Common; confidentiality internal; no buyer. A
`meta.yaml` in any folder sets any of the four for every file below it; a deeper folder overrides:

    # data/pdfs/HR/meta.yaml
    department: HR
    confidentiality: restricted

Changing a folder's meta.yaml (or moving a file) retags its documents on the next ingest pass without
re-embedding (ingest/pipeline.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..auth.models import LEVELS
from ..config import settings

DEFAULT_FACTORY_CODES = ("TAL", "RHL", "BGL", "KTL", "CKDL")
KEYS = ("factory", "department", "confidentiality", "buyer_code")
_CODE = re.compile(r"^[A-Z0-9]{1,8}$")
_TEXT = re.compile(r"^[A-Za-z0-9 _&.-]{1,40}$")


def factory_codes() -> set[str]:
    return {str(c).upper() for c in settings().get("scope.factory_codes", DEFAULT_FACTORY_CODES)}


def _read_meta(folder: Path, cache: dict[Path, dict]) -> dict:
    if folder not in cache:
        f = folder / "meta.yaml"
        d = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}
        unknown = set(d) - set(KEYS)
        if unknown:
            raise ValueError(f"{f}: unknown key(s) {sorted(unknown)}; allowed: {', '.join(KEYS)}")
        cache[folder] = d
    return cache[folder]


def attributes_for(path: Path, root: Path, cache: dict[Path, dict] | None = None) -> dict[str, str]:
    """The four access attributes of `path` (a PDF under `root`). Raises ValueError on an invalid
    meta.yaml, so the file is recorded as failed with the reason instead of being indexed wrongly."""
    cache = {} if cache is None else cache
    rel = path.relative_to(root)
    first = rel.parts[0].upper() if len(rel.parts) > 1 else ""
    attrs = {"factory": first if first in factory_codes() else "ALL", "department": "Common",
             "confidentiality": "internal", "buyer_code": ""}
    folder = root
    for part in ("",) + rel.parts[:-1]:                    # root first, then each folder down to the file's
        folder = folder / part if part else folder
        attrs.update({k: str(v).strip() for k, v in _read_meta(folder, cache).items() if v is not None})
    attrs["factory"] = attrs["factory"].upper()
    if attrs["factory"] != "ALL" and not _CODE.match(attrs["factory"]):
        raise ValueError(f"invalid factory {attrs['factory']!r} for {rel} (a short code such as TAL, or ALL)")
    if attrs["confidentiality"] not in LEVELS:
        raise ValueError(f"invalid confidentiality {attrs['confidentiality']!r} for {rel}: one of {', '.join(LEVELS)}")
    if not _TEXT.match(attrs["department"]):
        raise ValueError(f"invalid department {attrs['department']!r} for {rel}")
    if attrs["buyer_code"] and not _TEXT.match(attrs["buyer_code"]):
        raise ValueError(f"invalid buyer_code {attrs['buyer_code']!r} for {rel}")
    return attrs
