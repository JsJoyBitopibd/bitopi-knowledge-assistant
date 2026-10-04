"""Sign-in providers (PRD FR-4.1). `auth.provider` in settings picks one:

- ldap: the user's Active Directory account over LDAPS (or StartTLS), SIMPLE bind as user@domain, then
  the account's groups (memberOf, direct membership) -> scope via config/scopes.yaml. .env: LDAP_HOST,
  LDAP_DOMAIN, LDAP_BASE_DN; optional LDAP_PORT, LDAP_USE_SSL (default true; false = StartTLS),
  LDAP_CA_FILE, LDAP_TLS_VALIDATE (default true).
- local: config/users.yaml with bcrypt hashes (scripts/add_local_user.py) — development and tests only.

Passwords are never logged or stored. An empty password is refused before any bind: Active Directory
treats a simple bind with an empty password as an anonymous bind and reports success.
"""
from __future__ import annotations

import re
import ssl
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml

from ..config import env, settings
from .models import User
from .scopes import load_scopes, scope_for_groups

_ACCOUNT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class AuthResult:
    user: Optional[User]
    reason: str = "ok"          # ok | invalid | not_enrolled | locked | unavailable


class AuthProvider(Protocol):
    def authenticate(self, username: str, password: str) -> AuthResult: ...


def account_name(username: str) -> str:
    """'BITOPI\\jdoe', 'jdoe@bitopibd.com', ' JDoe ' -> 'jdoe'; '' when it is not a plain account name."""
    u = (username or "").strip()
    u = u.split("\\")[-1].split("@")[0].strip()
    return u.lower() if _ACCOUNT.match(u) else ""


def _result(name: str, display: str, groups: list[str], cfg: dict[str, Any]) -> AuthResult:
    scope = scope_for_groups(groups, cfg)
    if scope is None:
        return AuthResult(None, "not_enrolled")
    return AuthResult(User(name=name, display=display or name, groups=groups, scope=scope))


class LocalProvider:
    def __init__(self, users_file: Optional[Path] = None, scopes_cfg: Optional[dict[str, Any]] = None):
        self.users_file = users_file or settings().path("users_file", "config/users.yaml")
        self.cfg = scopes_cfg if scopes_cfg is not None else load_scopes()

    def authenticate(self, username: str, password: str) -> AuthResult:
        import bcrypt
        name = account_name(username)
        if not name or not password:
            return AuthResult(None, "invalid")
        users = {}
        if self.users_file.exists():
            users = (yaml.safe_load(self.users_file.read_text(encoding="utf-8")) or {}).get("users") or {}
        u = {k.lower(): v for k, v in users.items()}.get(name)
        if not u or not bcrypt.checkpw(password.encode("utf-8"), str(u.get("password_hash", "")).encode("utf-8")):
            return AuthResult(None, "invalid")
        return _result(name, u.get("name", ""), list(u.get("groups") or []), self.cfg)


def _cn(dn: str) -> str:
    """'CN=KA-Staff-TAL,OU=Groups,DC=bitopi,DC=local' -> 'KA-Staff-TAL' (escaped commas kept)."""
    first = re.split(r"(?<!\\),", dn, maxsplit=1)[0]
    return first.split("=", 1)[1].replace("\\,", ",") if "=" in first else first


class LdapProvider:
    def __init__(self, host: str, domain: str, base_dn: str, *, port: Optional[int] = None, use_ssl: bool = True,
                 ca_file: Optional[str] = None, validate_tls: bool = True, timeout: int = 5,
                 scopes_cfg: Optional[dict[str, Any]] = None):
        self.host, self.domain, self.base_dn = host, domain, base_dn
        self.port, self.use_ssl, self.ca_file, self.validate_tls, self.timeout = port, use_ssl, ca_file, validate_tls, timeout
        self.cfg = scopes_cfg if scopes_cfg is not None else load_scopes()

    @classmethod
    def from_env(cls) -> "LdapProvider":
        port = env("LDAP_PORT", "") or None
        return cls(env("LDAP_HOST"), env("LDAP_DOMAIN"), env("LDAP_BASE_DN"), port=int(port) if port else None,
                   use_ssl=env("LDAP_USE_SSL", "true").lower() in ("1", "true", "yes"),
                   ca_file=env("LDAP_CA_FILE", "") or None,
                   validate_tls=env("LDAP_TLS_VALIDATE", "true").lower() in ("1", "true", "yes"))

    def authenticate(self, username: str, password: str) -> AuthResult:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
        name = account_name(username)
        if not name or not password:                       # empty password = anonymous bind on AD
            return AuthResult(None, "invalid")
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if self.validate_tls else ssl.CERT_NONE, ca_certs_file=self.ca_file)
        server = ldap3.Server(self.host, port=self.port, use_ssl=self.use_ssl, tls=tls, connect_timeout=self.timeout)
        conn = ldap3.Connection(server, user=f"{name}@{self.domain}", password=password, authentication=ldap3.SIMPLE,
                                receive_timeout=self.timeout, raise_exceptions=False)
        try:
            # ldap3's open() returns None on success and raises LDAPSocketOpenError when the server cannot
            # be reached (handled below); its return value must not be read as success or failure.
            conn.open()
            if conn.closed:
                return AuthResult(None, "unavailable")
            if not self.use_ssl and not conn.start_tls():  # never send the password in clear text
                return AuthResult(None, "unavailable")
            if not conn.bind():
                return AuthResult(None, "invalid")
            conn.search(self.base_dn, f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(name)}))",
                        attributes=["memberOf", "displayName"])
            if not conn.entries:
                return AuthResult(None, "not_enrolled")
            entry = conn.entries[0]
            groups = [_cn(dn) for dn in (entry.memberOf.values if "memberOf" in entry else [])]
            display = str(entry.displayName.value) if "displayName" in entry and entry.displayName.value else name
        except Exception:
            return AuthResult(None, "unavailable")
        finally:
            try:
                conn.unbind()
            except Exception:
                pass
        return _result(name, display, groups, self.cfg)


class LoginThrottle:
    """At most `max_failures` failed sign-ins per account within `window_s`; then the account is refused
    until the oldest failure leaves the window. Per process, shared by every browser session."""

    def __init__(self, max_failures: int = 5, window_s: int = 600):
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def allowed(self, username: str) -> bool:
        with self._lock:
            return len(self._trim(account_name(username) or "?", time.monotonic())) < self.max_failures

    def failed(self, username: str) -> None:
        with self._lock:
            self._fails[account_name(username) or "?"].append(time.monotonic())

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._fails.pop(account_name(username) or "?", None)


_THROTTLE: Optional[LoginThrottle] = None


def sign_in(username: str, password: str, provider: Optional[AuthProvider] = None,
            throttle: Optional[LoginThrottle] = None) -> AuthResult:
    """Throttled authenticate(): the one entry point the UI calls."""
    global _THROTTLE
    if throttle is None:
        if _THROTTLE is None:
            s = settings()
            _THROTTLE = LoginThrottle(int(s.get("auth.max_failures", 5)), int(s.get("auth.lockout_seconds", 600)))
        throttle = _THROTTLE
    if not throttle.allowed(username):
        return AuthResult(None, "locked")
    res = (provider or get_provider()).authenticate(username, password)
    if res.user is None and res.reason == "invalid":
        throttle.failed(username)
    elif res.user is not None:
        throttle.succeeded(username)
    return res


def get_provider() -> AuthProvider:
    kind = str(settings().get("auth.provider", "local")).lower()
    if kind == "ldap":
        return LdapProvider.from_env()
    if kind == "local":
        return LocalProvider()
    if kind == "none":
        raise RuntimeError("auth.provider is 'none': there is no sign-in (the app uses open_user())")
    raise RuntimeError(f"auth.provider must be 'ldap', 'local' or 'none', not {kind!r}")


OPEN_USER = "open"


def sign_in_required() -> bool:
    return str(settings().get("auth.provider", "local")).lower() != "none"


def open_user() -> Optional[User]:
    """auth.provider: none — no sign-in form: every visitor is the one shared user `open`, whose scope is the
    union of `auth.open_groups` from config/scopes.yaml, enforced like a signed-in user's. None while sign-in
    is on. Fails closed: groups that config/scopes.yaml does not configure raise, never open everything."""
    if sign_in_required():
        return None
    groups = [str(g) for g in settings().get("auth.open_groups") or []]
    res = _result(OPEN_USER, "Open access (no sign-in)", groups, load_scopes())
    if res.user is None:
        raise RuntimeError(f"auth.provider 'none' needs auth.open_groups that config/scopes.yaml configures; "
                           f"got {groups!r}")
    return res.user
