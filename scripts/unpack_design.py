"""Unpack a design export (one self-contained HTML bundle) into readable per-screen markup and its fonts.

The designer's export packs every screen as a gzip+base64 page inside a manifest, each screen again a bundle of
its own (fonts, the React runtime, embedded pages). This writes, per screen, the markup with its inline styles —
the exact reference when building a surface — without the font blobs or the runtime, and optionally the bundled
woff2 fonts. Stdlib only. Output for the current set: docs/design/source/ (see docs/design/DESIGN.md).

  python scripts/unpack_design.py "Bitopi Knowledge Assistant.html" docs/design/source [--fonts DIR]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
from pathlib import Path

_BLOCK = r'<script type="__bundler/{}">\s*(.*?)\s*</script>'
_FONT_FACE = re.compile(r"/\*[^*]*\*/\s*@font-face\s*\{[^}]*\}|@font-face\s*\{[^}]*\}", re.S)
_RUNTIME = re.compile(r'<script src="[0-9a-f-]{36}"></script>\s*')


def _block(html: str, name: str) -> str | None:
    m = re.search(_BLOCK.format(re.escape(name)), html, re.S)
    return m.group(1) if m else None


def _assets(html: str) -> tuple[dict[str, tuple[str, bytes]], str]:
    """(uuid → (mime, bytes), template) of one bundle level."""
    manifest = json.loads(_block(html, "manifest") or "{}")
    template = json.loads(_block(html, "template") or '""')
    out = {}
    for uuid, e in manifest.items():
        raw = base64.b64decode(e["data"])
        out[uuid] = (e["mime"], gzip.decompress(raw) if e.get("compressed") else raw)
    return out, template


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "screen"


def _readable(markup: str, assets: dict[str, tuple[str, bytes]]) -> str:
    """Markup without font-face blobs and runtime script tags; font references named, not inlined."""
    markup = _FONT_FACE.sub("", markup)
    markup = _RUNTIME.sub("<!-- design runtime (React) omitted -->\n", markup)
    for uuid, (mime, _) in assets.items():
        if mime.startswith("font/"):
            markup = markup.replace(uuid, f"bundled-font:{uuid[:8]}")
    return re.sub(r"<style>\s*</style>\s*", "", markup)


def unpack(bundle: Path, out: Path, fonts: Path | None = None) -> list[Path]:
    html = bundle.read_text(encoding="utf-8")
    pages, outer = _assets(html)
    order = json.loads(_block(html, "page_order") or "[]")
    titles = dict((u, t) for t, u in re.findall(r'<h2>([^<]+)</h2><iframe src="about:blank#([0-9a-f-]{36})"', outer))
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen_fonts: set[bytes] = set()
    for n, uuid in enumerate(order or list(pages), 1):
        mime, raw = pages[uuid]
        inner_assets, markup = _assets(raw.decode("utf-8"))
        name = f"{n:02d}-{_slug(titles.get(uuid, f'screen {n}'))}"
        path = out / f"{name}.html"
        path.write_text(_readable(markup, inner_assets), encoding="utf-8")
        written.append(path)
        k = 0
        for a_uuid, (a_mime, a_raw) in inner_assets.items():
            if a_mime == "text/html":                  # a page drawn inside the screen (e.g. the website behind the widget)
                k += 1
                emb = out / f"{name}--embedded-{k}.html"
                emb.write_text(_readable(a_raw.decode("utf-8"), inner_assets), encoding="utf-8")
                written.append(emb)
            elif fonts is not None and a_mime.startswith("font/") and a_raw not in seen_fonts:
                seen_fonts.add(a_raw)
                fonts.mkdir(parents=True, exist_ok=True)
                fpath = fonts / f"{_font_name(markup, a_uuid)}.woff2"
                fpath.write_bytes(a_raw)
                written.append(fpath)
    return written


def _font_name(markup: str, uuid: str) -> str:
    """'IBMPlexSans-latin' from the @font-face rule that points at this file (its subset comment precedes it)."""
    m = re.search(r"/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{[^}]*font-family:\s*'([^']+)'[^}]*" + re.escape(uuid), markup)
    return f"{m.group(2).replace(' ', '')}-{m.group(1)}" if m else uuid[:8]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--fonts", type=Path, help="also write the bundled woff2 files here")
    a = ap.parse_args()
    for p in unpack(a.bundle, a.out, a.fonts):
        print(p, p.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
