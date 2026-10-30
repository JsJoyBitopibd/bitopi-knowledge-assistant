"""Phase T0: staying signed in across reloads (auth/session.py) and the identity the token carries."""
import json

import pytest

from ragbot.auth import providers, session
from ragbot.auth.models import Identity

T0 = 1_800_000_000.0


def test_a_token_round_trips_with_its_identity(tmp_path):
    ident = {"employee_code": "E0456", "company_id": "06", "unit_id": "1", "department": "Cutting",
             "home_factory": "TAL"}
    tok = session.issue("jdoe", "John Doe", "erp", 12, account_id="U1", now=T0, folder=tmp_path, identity=ident)
    claims = session.read(tok, "erp", now=T0 + 60, folder=tmp_path)
    assert claims["n"] == "jdoe" and claims["d"] == "John Doe" and claims["a"] == "U1"
    assert session.identity_from(claims) == {"employee_code": "E0456", "company_id": "06", "unit_id": "1",
                                             "department": "Cutting"}       # the factory is derived again, not carried
    assert "home_factory" not in json.dumps(claims) and "TAL" not in json.dumps(claims)


def test_a_token_without_identity_reads_as_empty(tmp_path):
    tok = session.issue("jdoe", "John", "local", 12, now=T0, folder=tmp_path)
    claims = session.read(tok, "local", now=T0, folder=tmp_path)
    assert session.identity_from(claims) == {} and not {"e", "c", "u", "dp"} & set(claims)


def test_expiry_provider_and_clock_skew(tmp_path):
    tok = session.issue("jdoe", "J", "erp", 1, now=T0, folder=tmp_path)
    assert session.read(tok, "erp", now=T0 + 3599, folder=tmp_path) is not None
    assert session.read(tok, "erp", now=T0 + 3600, folder=tmp_path) is None          # expired
    assert session.read(tok, "local", now=T0, folder=tmp_path) is None               # another provider's
    assert session.read(tok, "erp", now=T0 - 3600, folder=tmp_path) is None          # issued in the future


def test_a_tampered_or_malformed_token_is_refused(tmp_path):
    tok = session.issue("jdoe", "J", "erp", 12, now=T0, folder=tmp_path)
    payload, sig = tok.split(".")
    forged = session._b64(json.dumps({"n": "admin", "p": "erp", "iat": T0, "exp": T0 + 99}).encode())
    for bad in (f"{forged}.{sig}", f"{payload}.{sig[:-2]}AA", "", None, "a.b.c", "x" * 5000, f"{payload}."):
        assert session.read(bad, "erp", now=T0, folder=tmp_path) is None


def test_sign_out_revokes_every_earlier_token_on_every_browser(tmp_path):
    a = session.issue("jdoe", "J", "erp", 12, now=T0, folder=tmp_path)
    b = session.issue("jdoe", "J", "erp", 12, now=T0 + 10, folder=tmp_path)
    other = session.issue("ann", "A", "erp", 12, now=T0, folder=tmp_path)
    session.revoke("jdoe", now=T0 + 20, folder=tmp_path)
    assert session.read(a, "erp", now=T0 + 30, folder=tmp_path) is None
    assert session.read(b, "erp", now=T0 + 30, folder=tmp_path) is None
    assert session.read(other, "erp", now=T0 + 30, folder=tmp_path) is not None
    later = session.issue("jdoe", "J", "erp", 12, now=T0 + 40, folder=tmp_path)
    assert session.read(later, "erp", now=T0 + 50, folder=tmp_path) is not None


def test_the_secret_is_made_once_and_a_short_one_is_refused(tmp_path):
    k1 = session._secret(tmp_path)
    assert len(k1) == 32 and session._secret(tmp_path) == k1
    (tmp_path / "session_secret").write_bytes(b"short")
    with pytest.raises(RuntimeError, match="32 bytes"):
        session._secret(tmp_path)


def test_cookie_scripts_quote_the_token_and_clear_it():
    js = session.set_cookie_js("abc.def", 12)
    assert '"ka_session=abc.def"' in js and "Max-Age=43200" in js and "SameSite=Strict" in js
    evil = session.set_cookie_js('x"</script><script>alert(1)//', 1)
    assert evil.count("</script>") == 1 and evil.endswith("</script>")     # the token cannot close the tag
    assert "Max-Age=0" in session.clear_cookie_js()


class _Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_start_and_restore_session_carry_the_identity(monkeypatch, tmp_path):
    """providers.start_session → cookie → restore_session gives back the same person with the same identity."""
    monkeypatch.setattr(providers, "settings", lambda: _Settings({"auth.provider": "erp", "auth.session_hours": 12}))
    monkeypatch.setattr(session, "_dir", lambda: tmp_path)
    from ragbot.auth.models import Scope, User
    me = User(name="jdoe", display="John", scope=Scope(factories=["*"]), account_id="U1",
              identity=Identity(employee_code="E1", company_id="04", unit_id="2", home_factory="BGL"))
    seen = {}

    class Fake:
        def restore(self, name, display="", account_id="", identity=None):
            seen.update(name=name, display=display, account_id=account_id, identity=identity)
            return "restored"

    monkeypatch.setattr(providers, "get_provider", lambda: Fake())
    tok = providers.start_session(me)
    assert providers.restore_session(tok) == "restored"
    assert seen == {"name": "jdoe", "display": "John", "account_id": "U1",
                    "identity": {"employee_code": "E1", "company_id": "04", "unit_id": "2"}}
    providers.end_session(me)
    assert providers.restore_session(tok) is None


def test_ldap_and_open_access_keep_no_session(monkeypatch):
    for kind in ("ldap", "none"):
        monkeypatch.setattr(providers, "settings", lambda k=kind: _Settings({"auth.provider": k}))
        assert providers.session_hours() == 0.0 and providers.restore_session("a.b") is None
