"""Stay signed in across page reloads (auth.session_hours, 2026-10-26).

A Streamlit session ends with the browser tab or a reload, so without this every reload asked for the password
again. After a sign-in the browser keeps a cookie holding a signed token: who the user is, which provider signed
them in, when it was issued and when it expires. On a new page load the app reads the cookie (st.context.cookies),
checks the signature with a server-side secret, the expiry and the sign-out list, and rebuilds the user with
today's configuration (the scope is never taken from the cookie). Nothing secret is in the token, and the
password is never in it.

The secret is data/index/session_secret, created on first use; deleting it signs everyone out. Signing out writes
the account and the time to data/index/sign_outs.json, and every token issued to that account before then is
refused — on every browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..config import settings

COOKIE = "ka_session"
_LOCK = threading.Lock()


def _dir() -> Path:
    d = settings().path("index_dir", "data/index")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _secret(folder: Optional[Path] = None) -> bytes:
    f = (folder or _dir()) / "session_secret"
    with _LOCK:
        if not f.exists():
            try:
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(32))
            except FileExistsError:
                pass                                   # another process made it first: use theirs
        key = f.read_bytes()
    if len(key) < 32:
        raise RuntimeError(f"{f} is shorter than 32 bytes; delete it to make a new one (signs everyone out)")
    return key


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# Phase T0: the identity the ERP returned rides along, so a reload keeps "my" questions working without asking the
# service again. Short claim names; none of them is a secret, and the scope is never taken from the token.
IDENTITY_CLAIMS = {"e": "employee_code", "c": "company_id", "u": "unit_id", "dp": "department"}


def issue(name: str, display: str, provider: str, hours: float, account_id: str = "",
          now: Optional[float] = None, folder: Optional[Path] = None,
          identity: Optional[dict[str, Any]] = None) -> str:
    now = time.time() if now is None else now
    claims = {"n": name, "d": display, "a": account_id, "p": provider, "iat": now, "exp": now + hours * 3600}
    for short, field in IDENTITY_CLAIMS.items():
        v = (identity or {}).get(field)
        if v:
            claims[short] = str(v)
    payload = _b64(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _b64(hmac.new(_secret(folder), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def _signed_out(folder: Optional[Path] = None) -> dict[str, float]:
    f = (folder or _dir()) / "sign_outs.json"
    try:
        return {str(k): float(v) for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def read(token: Optional[str], provider: str, now: Optional[float] = None,
         folder: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The token's claims, or None: no token, a bad signature, another provider's token, expired, or issued
    before the account last signed out."""
    if not token or token.count(".") != 1 or len(token) > 4096:
        return None
    payload, sig = token.split(".")
    want = _b64(hmac.new(_secret(folder), payload.encode("ascii", "replace"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        claims = json.loads(_unb64(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("n"), str) or claims.get("p") != provider:
        return None
    now = time.time() if now is None else now
    try:
        if float(claims["exp"]) <= now or float(claims["iat"]) > now + 60:
            return None
        if float(claims["iat"]) <= _signed_out(folder).get(claims["n"], float("-inf")):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return claims


def identity_from(claims: dict[str, Any]) -> dict[str, str]:
    """The identity fields a token carried, by their long names ({} for a token issued before Phase T0)."""
    return {field: str(claims[short]) for short, field in IDENTITY_CLAIMS.items() if claims.get(short)}


def revoke(name: str, now: Optional[float] = None, folder: Optional[Path] = None) -> None:
    """Sign-out: every token issued to `name` until now stops working, on every browser."""
    f = (folder or _dir()) / "sign_outs.json"
    with _LOCK:
        d = _signed_out(folder)
        d[name] = time.time() if now is None else now
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)


def set_cookie_js(token: str, hours: float) -> str:
    """The script that stores the token in the browser (st.html with unsafe_allow_javascript). SameSite=Strict:
    the cookie is never sent with a request another site starts."""
    value = json.dumps(COOKIE + "=" + token).replace("</", "<\\/")   # "</" never closes the script tag
    return (f"<script>document.cookie = {value} + "
            f"'; Max-Age={int(hours * 3600)}; Path=/; SameSite=Strict';</script>")


def clear_cookie_js() -> str:
    return f"<script>document.cookie = '{COOKIE}=; Max-Age=0; Path=/; SameSite=Strict';</script>"


# Phase U1: under auth.provider: none everyone is the one shared user "open", so conversations are kept per browser:
# a random id in this cookie, 400 days, never a secret and never access — only whose Recent list this browser sees.
BROWSER_COOKIE = "ka_browser"


def valid_browser_id(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    return v if len(v) == 32 and all(c in "0123456789abcdef" for c in v) else None


def new_browser_id() -> str:
    return secrets.token_hex(16)


def set_browser_cookie_js(browser_id: str, days: int = 400) -> str:
    bid = valid_browser_id(browser_id) or ""
    return (f"<script>document.cookie = '{BROWSER_COOKIE}={bid}; Max-Age={int(days) * 86400}; Path=/; "
            "SameSite=Strict';</script>")
