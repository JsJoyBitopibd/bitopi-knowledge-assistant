"""Sign-in with the company system's own accounts (auth.provider: erp, 2026-10-26).

Staff type the user name and password they use for BIS / BiMob. The assistant does not read or compare
passwords: it posts what was typed to the ERP team's verify service (docs/ERP_SIGN_IN_API.md, the contract)
and gets back "valid, this is user X" or "not valid". Nothing about the ERP's stored credentials is read here,
and nothing is written to any database.

.env: ERP_VERIFY_URL, ERP_VERIFY_API_KEY; optional ERP_VERIFY_CA_FILE, ERP_VERIFY_TLS_VALIDATE (default true),
ERP_VERIFY_ALLOW_HTTP (default false: the password is in the request body, so a plain http:// address is
refused unless this is set on purpose for a stand-in on the same machine).
settings: auth.erp.groups (what every ERP user may see, from config/scopes.yaml), auth.erp.admins (ERP user
names that also hold the admin groups), auth.erp.timeout_seconds.

The password is never logged, stored or put in an error message; a reply that does not follow the contract is
"unavailable", never a sign-in.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import env, settings
from .models import Identity
from .scopes import load_scopes

MAX_NAME = 128


def clean_name(username: str) -> str:
    """The user name as the ERP will compare it: spaces at either end removed; '' when it is empty, too long or
    holds a control character. ERP user names are not Windows account names, so nothing else is stripped."""
    u = (username or "").strip()
    if not u or len(u) > MAX_NAME or any(ord(c) < 32 or ord(c) == 127 for c in u):
        return ""
    return u


class ErpProvider:
    def __init__(self, url: str, api_key: str, *, groups: list[str], admins: list[str] = (), timeout: float = 5,
                 ca_file: Optional[str] = None, validate_tls: bool = True, allow_http: bool = False,
                 scopes_cfg: Optional[dict[str, Any]] = None, opener: Any = None,
                 unit_factories: Optional[dict[str, str]] = None):
        if not url.lower().startswith("https://") and not (allow_http and url.lower().startswith("http://")):
            raise RuntimeError("ERP_VERIFY_URL must be an https:// address (ERP_VERIFY_ALLOW_HTTP=true allows http:// "
                               "for a stand-in on the same machine)")
        if not api_key:
            raise RuntimeError("ERP_VERIFY_API_KEY is empty")
        self.url, self.api_key, self.timeout = url, api_key, timeout
        self.groups = [str(g) for g in groups]
        self.admins = {str(a).strip().lower() for a in admins or () if str(a).strip()}
        self.cfg = scopes_cfg if scopes_cfg is not None else load_scopes()
        self._ctx = None
        if url.lower().startswith("https://"):
            self._ctx = ssl.create_default_context(cafile=ca_file or None)
            if not validate_tls:
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_NONE
        self._open = opener or self._urlopen
        self.unit_factories = {str(k): str(v) for k, v in (unit_factories or {}).items()}

    @classmethod
    def from_settings(cls) -> "ErpProvider":
        s = settings()
        flag = lambda name, default: env(name, default).strip().lower() in ("1", "true", "yes")   # noqa: E731
        return cls(env("ERP_VERIFY_URL"), env("ERP_VERIFY_API_KEY"),
                   groups=list(s.get("auth.erp.groups") or []), admins=list(s.get("auth.erp.admins") or []),
                   timeout=float(s.get("auth.erp.timeout_seconds", 5)),
                   ca_file=env("ERP_VERIFY_CA_FILE", "") or None, validate_tls=flag("ERP_VERIFY_TLS_VALIDATE", "true"),
                   allow_http=flag("ERP_VERIFY_ALLOW_HTTP", "false"),
                   unit_factories=dict(s.get("auth.erp.unit_factories") or {}))

    def _urlopen(self, req: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.status, resp.read(65536)
        except urllib.error.HTTPError as e:
            return e.code, b""

    def groups_for(self, name: str) -> list[str]:
        """The configured groups every ERP user holds, plus the admin groups for a listed user name."""
        extra = list(self.cfg.get("admins") or []) if name.lower() in self.admins else []
        return self.groups + [g for g in extra if g not in self.groups]

    def authenticate(self, username: str, password: str):
        from .providers import AuthResult, _result
        name = clean_name(username)
        if not name or not password:
            return AuthResult(None, "invalid")            # an empty password never reaches the service
        body = json.dumps({"username": name, "password": password}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        try:
            status, raw = self._open(req)
        except Exception:
            return AuthResult(None, "unavailable")       # unreachable, timeout, TLS: never a sign-in
        if status != 200:
            return AuthResult(None, "unavailable")       # 401/403 = our key, 429, 5xx (docs/ERP_SIGN_IN_API.md)
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return AuthResult(None, "unavailable")
        if not isinstance(reply, dict) or not isinstance(reply.get("valid"), bool):
            return AuthResult(None, "unavailable")
        if reply["valid"] is not True:
            return AuthResult(None, "invalid")
        user_name, user_code = reply.get("user_name"), reply.get("user_code")
        if not isinstance(user_name, str) or not clean_name(user_name) or not isinstance(user_code, str) \
                or not user_code.strip():
            return AuthResult(None, "unavailable")       # "valid" without saying who: not a sign-in
        account = clean_name(user_name).lower()
        display = reply.get("name") if isinstance(reply.get("name"), str) and reply["name"].strip() else user_name
        res = _result(account, display.strip(), self.groups_for(account), self.cfg)
        if res.user is not None:
            res.user.account_id = user_code.strip()
            res.user.identity = self.identity(reply)
        return res

    def identity(self, reply: dict[str, Any]) -> Identity:
        """The optional fields of a valid reply (employee_code, company_id, unit_id, department), each kept only when
        it is a short plain code, plus the factory they point at. A missing or odd field is simply left empty."""
        base = Identity(employee_code=reply.get("employee_code"), company_id=reply.get("company_id"),
                        unit_id=reply.get("unit_id"), department=reply.get("department"))
        return base.model_copy(update={"home_factory": home_factory(base.company_id, base.unit_id, self.unit_factories)})

    def restore(self, name: str, display: str, account_id: str = "", identity: Optional[dict[str, Any]] = None):
        """A user from a signed session (auth/session.py) without asking the service again: the scope is rebuilt
        from today's configuration, so a changed group or admin list applies on the next page load. The identity
        comes back from the token's claims; its factory is worked out again from today's maps."""
        from .providers import _result
        account = clean_name(name).lower()
        if not account:
            return None
        res = _result(account, display, self.groups_for(account), self.cfg)
        if res.user is not None:
            res.user.account_id = account_id
            res.user.identity = self.identity(identity or {})
        return res.user


def home_factory(company_id: str, unit_id: str, unit_factories: Optional[dict[str, str]] = None) -> str:
    """The factory code a person belongs to: the company's own (config/reference/entities.yaml, `factory:`), else
    the unit's (settings auth.erp.unit_factories), else ''. Only a code that passes the scope's own rule is kept."""
    code = ""
    if company_id:
        try:
            from ..data.entities import load_entities
            c = load_entities().company(company_id)
            code = c.factory if c is not None else ""
        except Exception:
            code = ""
    if not code and unit_id:
        code = str((unit_factories or {}).get(str(unit_id), "") or "")
    code = code.strip().upper()
    return code if code and len(code) <= 8 and code.isalnum() else ""
