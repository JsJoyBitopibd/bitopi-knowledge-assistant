"""Make `import ragbot` work from scripts/ without installing the package, and keep console output UTF-8
(Windows consoles default to cp1252, which cannot print '—', '›' or Bangla).

Also removes scripts/ from sys.path — Python auto-adds the script's directory, so `scripts/inspect.py`
would shadow the stdlib `inspect` module (imported transitively by numpy)."""
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_scripts = str(_root / "scripts")
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) != os.path.normcase(_scripts)]
sys.path.insert(0, str(_root / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
