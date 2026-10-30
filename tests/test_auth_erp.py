"""Phase T0: sign-in through the ERP's verify service (auth/erp.py), the identity it returns, and the sign-in log.

No network: the provider takes an `opener` that stands in for urllib, and the end-to-end case runs the mock service
(scripts/mock_erp_verify.py) on a free local port.
"""
import json
import socket
import sys
import threading
from pathlib import Path

import bcrypt
import pytest

from ragbot.auth import providers
from ragbot.auth.erp import ErpProvider, clean_name, home_factory
from ragbot.auth.models import Identity, ident_field
from ragbot.auth.providers import LoginThrottle, sign_in
from ragbot.auth.scopes import restriction_problems

CFG = {"groups": {"KA-Managers": {"factories": ["*"]}, "KA-Staff-TAL": {"factories": ["TAL"]},
                  "KA-HR": {"factories": ["*"]}},
       "admins": ["KA-Admins"]}
CFG["groups"]["KA-Admins"] = {"factories": ["*"]}

VALID = {"valid": True, "user_code": "U0123", "user_name": "JDoe", "name": "John Doe",
         "employee_code": "E0456", "company_id": "06", "unit_id": "1", "department": "Merchandising"}


class Opener:
    """Records each request and answers with a fixed (status, body) — or raises, like an unreachable server."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises, self.requests = status, body, raises, []

    def __call__(self, req):
        self.requests.append(req)
        if self.raises:
            raise self.raises
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode("utf-8")
        return self.status, raw


def _provider(opener, **kw):
    return ErpProvider("https://erp.example/verify", "k3y", groups=kw.pop("groups", ["KA-Managers"]),
                       scopes_cfg=CFG, opener=opener, **kw)


# ---------------------------------------------------------------- the contract
def test_a_valid_reply_signs_in_with_the_configured_groups_and_the_identity():
    op = Opener(body=VALID)
    res = _provider(op).authenticate("  JDoe ", "secret")
    u = res.user
    assert u is not None and u.name == "jdoe" and u.display == "John Doe" and u.account_id == "U0123"
    assert u.groups == ["KA-Managers"] and u.scope.all_factories and not u.scope.is_admin
    assert u.identity == Identity(employee_code="E0456", company_id="06", unit_id="1", department="Merchandising",
                                  home_factory="TAL")          # company 06 is Tarasima → TAL (entities.yaml)
    body = json.loads(op.requests[0].data.decode("utf-8"))
    assert body == {"username": "JDoe", "password": "secret"}
    assert op.requests[0].get_header("Authorization") == "Bearer k3y"


@pytest.mark.parametrize("opener,reason", [
    (Opener(body={"valid": False}), "invalid"),
    (Opener(status=401, body={}), "unavailable"),
    (Opener(status=429, body={}), "unavailable"),
    (Opener(status=503, body={}), "unavailable"),
    (Opener(raises=TimeoutError("slow")), "unavailable"),
    (Opener(body=b"<html>not json</html>"), "unavailable"),
    (Opener(body=["valid"]), "unavailable"),
    (Opener(body={"valid": "yes"}), "unavailable"),
    (Opener(body={"valid": True, "user_name": "jdoe"}), "unavailable"),          # valid without saying who
    (Opener(body={"valid": True, "user_code": " ", "user_name": "jdoe"}), "unavailable"),
])
def test_anything_but_a_valid_reply_is_never_a_sign_in(opener, reason):
    res = _provider(opener).authenticate("jdoe", "secret")
    assert res.user is None and res.reason == reason


def test_an_empty_password_or_name_never_reaches_the_service():
    op = Opener(body=VALID)
    for name, pw in (("jdoe", ""), ("", "secret"), ("   ", "secret"), ("a\x00b", "secret")):
        assert _provider(op).authenticate(name, pw).reason == "invalid"
    assert op.requests == []
    assert clean_name("x" * 129) == "" and clean_name(" ok ") == "ok"


def test_plain_http_is_refused_unless_allowed_on_purpose():
    with pytest.raises(RuntimeError, match="https"):
        ErpProvider("http://erp.example/verify", "k", groups=["KA-Managers"], scopes_cfg=CFG, opener=Opener())
    ErpProvider("http://127.0.0.1:8601/verify", "k", groups=["KA-Managers"], scopes_cfg=CFG, opener=Opener(),
                allow_http=True)
    with pytest.raises(RuntimeError, match="API_KEY"):
        ErpProvider("https://erp.example/verify", "", groups=["KA-Managers"], scopes_cfg=CFG, opener=Opener())


def test_admins_get_the_admin_groups_and_an_unconfigured_group_is_not_enrolled():
    u = _provider(Opener(body=VALID), admins=["JDOE"]).authenticate("jdoe", "pw").user
    assert u.scope.is_admin and "KA-Admins" in u.groups
    res = _provider(Opener(body=VALID), groups=["Nobody"]).authenticate("jdoe", "pw")
    assert res.user is None and res.reason == "not_enrolled"


# ---------------------------------------------------------------- the identity
def test_identity_fields_are_kept_only_when_they_are_short_plain_codes():
    odd = {**VALID, "employee_code": "E1'; DROP", "company_id": 6, "unit_id": True, "department": "<b>HR</b>"}
    ident = _provider(Opener(body=odd)).authenticate("jdoe", "pw").user.identity
    assert ident.employee_code == "" and ident.company_id == "6" and ident.unit_id == "" and ident.department == ""
    assert ident.home_factory == "TAL"                         # "6" is zero-filled to company 06
    missing = {k: v for k, v in VALID.items() if k in ("valid", "user_code", "user_name")}
    bare = _provider(Opener(body=missing)).authenticate("jdoe", "pw").user.identity
    assert bare == Identity() and not bare.known()
    assert ident_field("x" * 33) == "" and ident_field("T-0001") == "T-0001"


def test_home_factory_from_the_company_then_the_unit_map():
    assert home_factory("04", "") == "BGL"
    assert home_factory("02", "") == ""                        # Misami has no factory code yet
    assert home_factory("02", "1", {"1": "tal"}) == "TAL"      # the unit map fills the gap
    assert home_factory("", "9", {"9": "NOT A CODE"}) == ""    # only a code the scope rule would accept
    p = _provider(Opener(body={**VALID, "company_id": "02", "unit_id": "1"}), unit_factories={"1": "RHL"})
    assert p.authenticate("jdoe", "pw").user.identity.home_factory == "RHL"


def test_identity_key_is_canonical_and_never_widens_the_scope():
    a = Identity(employee_code="E1", company_id="06", unit_id="1", home_factory="TAL")
    assert a.key() == "e=E1;c=06;u=1;h=TAL" and a.known()
    staff = ErpProvider("https://erp.example/verify", "k", groups=["KA-Staff-TAL"], scopes_cfg=CFG,
                        opener=Opener(body={**VALID, "company_id": "09"}))
    u = staff.authenticate("jdoe", "pw").user
    assert u.identity.home_factory == "RHL" and u.scope.factories == {"TAL"}   # identity says RHL; scope stays TAL


def test_restore_rebuilds_the_user_and_identity_without_asking_the_service():
    op = Opener(raises=AssertionError("the service must not be called"))
    u = _provider(op).restore("JDoe", "John Doe", "U0123", identity={"employee_code": "E0456", "company_id": "04"})
    assert u.name == "jdoe" and u.account_id == "U0123" and u.identity.home_factory == "BGL"
    assert _provider(op).restore("", "x") is None
    assert _provider(op).restore("jdoe", "John").identity == Identity()      # a token from before T0


# ---------------------------------------------------------------- the log and the throttle
class _Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_every_attempt_is_logged_without_the_password(monkeypatch, tmp_path):
    rows = []
    monkeypatch.setattr(providers, "settings", lambda: _Settings({"auth.provider": "erp"}))
    import ragbot.logs as logs
    monkeypatch.setattr(logs, "append_row", lambda name, header, row: rows.append((name, row)))
    thr = LoginThrottle(5, 600)
    sign_in("jdoe", "pa55word-secret", provider=_provider(Opener(body=VALID)), throttle=thr)
    sign_in("jdoe", "wrong-secret", provider=_provider(Opener(body={"valid": False})), throttle=thr)
    assert [r[1][3] for r in rows] == ["ok", "invalid"] and all(r[0] == "auth.csv" for r in rows)
    assert all(r[1][1] == "erp" and r[1][2] == "jdoe" for r in rows)
    assert not any("secret" in str(v) for _, row in rows for v in row)


def test_five_failures_lock_the_account_and_an_unreachable_service_does_not(monkeypatch):
    monkeypatch.setattr(providers, "settings", lambda: _Settings({"auth.provider": "erp"}))
    import ragbot.logs as logs
    monkeypatch.setattr(logs, "append_row", lambda *a: None)
    thr = LoginThrottle(5, 600)
    down = _provider(Opener(status=503, body={}))
    for _ in range(6):
        assert sign_in("jdoe", "pw", provider=down, throttle=thr).reason == "unavailable"
    bad = _provider(Opener(body={"valid": False}))
    for _ in range(5):
        assert sign_in("Some.User", "pw", provider=bad, throttle=thr).reason == "invalid"
    assert sign_in("some.user", "pw", provider=_provider(Opener(body=VALID)), throttle=thr).reason == "locked"
    assert sign_in("other.user", "pw", provider=_provider(Opener(body=VALID)), throttle=thr).user is not None


# ---------------------------------------------------------------- check_catalog: a half-armed ERP configuration
class _Cat:
    def __init__(self, name, restricted_to=()):
        self.name, self.restricted_to, self.offered_tables, self.sensitive_patterns = name, restricted_to, [], None


def test_restriction_problems_cover_the_erp_provider():
    cats = {"HR": _Cat("HR", ["KA-HR"])}
    p, _ = restriction_problems(cats, CFG, "erp", [], {}, erp_groups=["KA-Managers"])
    assert any("auth.provider is erp" in x for x in p)
    p, _ = restriction_problems(cats, CFG, "erp", [], {}, erp_groups=["KA-Managers", "KA-HR"])
    assert not any("auth.provider is erp" in x for x in p)


# ---------------------------------------------------------------- end to end against the mock service
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_end_to_end_against_the_mock_service(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import mock_erp_verify
    h = bcrypt.hashpw(b"right-pw", bcrypt.gensalt(4)).decode()
    users = tmp_path / "users.yaml"
    users.write_text("users:\n"
                     f"  test.user: {{password_hash: '{h}', user_code: T0001, name: Test User,"
                     " employee_code: T-0001, company_id: '06', unit_id: '1', department: Cutting}\n"
                     f"  gone.user: {{password_hash: '{h}', user_code: T0002, active: false}}\n", encoding="utf-8")
    server = mock_erp_verify.make_server(mock_erp_verify.load_accounts(users), "mock-key", port=_free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/verify"
        p = ErpProvider(url, "mock-key", groups=["KA-Managers"], scopes_cfg=CFG, allow_http=True, timeout=5)
        u = p.authenticate("Test.User", "right-pw").user
        assert u is not None and u.name == "test.user" and u.account_id == "T0001"
        assert u.identity == Identity(employee_code="T-0001", company_id="06", unit_id="1", department="Cutting",
                                      home_factory="TAL")
        assert p.authenticate("test.user", "wrong-pw").reason == "invalid"
        assert p.authenticate("gone.user", "right-pw").reason == "invalid"
        assert p.authenticate("nobody", "right-pw").reason == "invalid"
        wrong_key = ErpProvider(url, "other-key", groups=["KA-Managers"], scopes_cfg=CFG, allow_http=True, timeout=5)
        assert wrong_key.authenticate("test.user", "right-pw").reason == "unavailable"
    finally:
        server.shutdown()
