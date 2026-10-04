"""F1: scope model, AD group -> scope mapping, the document filter, and the sign-in providers."""
import bcrypt
import pytest
import yaml

from ragbot.auth import providers
from ragbot.auth.filters import scope_where
from ragbot.auth.models import Scope
from ragbot.auth.providers import LdapProvider, LocalProvider, LoginThrottle, account_name, sign_in
from ragbot.auth.scopes import scope_for_groups

CFG = {"groups": {"KA-Staff-TAL": {"factories": ["TAL"]},
                  "KA-Staff-RHL": {"factories": ["rhl"]},
                  "KA-Managers": {"factories": ["*"]},
                  "KA-HR-Restricted": {"departments": ["HR"], "confidentiality": "restricted"},
                  "KA-Buyer-MARCO": {"factories": ["TAL"], "confidentiality": "public", "buyer_codes": ["MARCO"],
                                     "departments": ["BuyerFacing"]}},
       "admins": ["KA-Admins"]}


# ---------------------------------------------------------------- scope
def test_factory_codes_are_normalised_and_validated():
    assert Scope(factories=["tal", " rhl"]).factories == {"TAL", "RHL"}
    for bad in ("TAL');--", "T AL", "", "TOOLONGCODE"):
        with pytest.raises(ValueError):
            Scope(factories=[bad])


def test_scope_key_is_canonical():
    a = Scope(factories=["TAL", "RHL"])
    b = Scope(factories=["RHL", "TAL"])
    assert a.key() == b.key() and a.key() != Scope(factories=["TAL"]).key()
    assert Scope.unrestricted().key() != a.key()


def test_db_factories_and_levels():
    s = Scope(factories=["TAL", "ALL"], confidentiality_max="internal")
    assert s.db_factories() == ["TAL"] and s.allowed_levels() == ["public", "internal"]
    assert Scope.unrestricted().all_factories and Scope.unrestricted().allowed_levels()[-1] == "restricted"
    assert s.allows_factory("tal") and not s.allows_factory("RHL") and Scope.unrestricted().allows_factory("RHL")


# ---------------------------------------------------------------- AD groups -> scope
def test_scope_is_the_union_of_the_users_groups():
    s = scope_for_groups(["ka-staff-tal", "KA-Staff-RHL", "Domain Users"], CFG)
    assert s.factories == {"TAL", "RHL"} and s.all_departments and s.confidentiality_max == "internal"
    assert not s.is_admin and s.buyer_codes is None


def test_a_user_in_no_configured_group_is_not_enrolled():
    assert scope_for_groups(["Domain Users"], CFG) is None
    assert scope_for_groups([], {"groups": {}, "admins": []}) is None


def test_joining_a_group_only_adds_access():
    tal = scope_for_groups(["KA-Staff-TAL"], CFG)
    both = scope_for_groups(["KA-Staff-TAL", "KA-HR-Restricted"], CFG)
    assert both.factories >= tal.factories and both.all_departments          # HR's department list adds, never narrows
    assert both.confidentiality_max == "restricted"
    hr_only = scope_for_groups(["KA-HR-Restricted"], CFG)
    assert hr_only.factories == frozenset() and hr_only.departments == {"HR"}


def test_admins_and_buyers():
    assert scope_for_groups(["KA-Staff-TAL", "ka-admins"], CFG) is not None
    assert scope_for_groups(["KA-Staff-TAL", "ka-admins"], CFG).is_admin
    buyer = scope_for_groups(["KA-Buyer-MARCO"], CFG)
    assert buyer.buyer_codes == {"MARCO"} and buyer.confidentiality_max == "public"
    assert scope_for_groups(["KA-Buyer-MARCO", "KA-Staff-TAL"], CFG).buyer_codes is None   # staff: every buyer


# ---------------------------------------------------------------- document filter
def test_scope_where_for_a_factory_user_and_for_everything():
    assert scope_where(Scope.unrestricted()) == {}
    w = scope_where(scope_for_groups(["KA-Staff-TAL"], CFG))
    assert w == {"factory": ["ALL", "TAL"], "confidentiality": ["public", "internal"]}
    b = scope_where(scope_for_groups(["KA-Buyer-MARCO"], CFG))
    assert b["buyer_code"] == ["", "MARCO"] and b["department"] == ["BuyerFacing", "Common"]
    assert b["confidentiality"] == ["public"]


# ---------------------------------------------------------------- local provider + throttle
def _local(tmp_path, users):
    f = tmp_path / "users.yaml"
    f.write_text(yaml.safe_dump({"users": users}), encoding="utf-8")
    return LocalProvider(f, CFG)


def _hash(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()


def test_local_provider(tmp_path):
    p = _local(tmp_path, {"jdoe": {"password_hash": _hash("correct horse"), "groups": ["KA-Staff-TAL"]},
                          "guest": {"password_hash": _hash("guest password"), "groups": ["Domain Users"]}})
    ok = p.authenticate("BITOPI\\JDoe", "correct horse")
    assert ok.reason == "ok" and ok.user.name == "jdoe" and ok.user.scope.factories == {"TAL"}
    assert p.authenticate("jdoe", "wrong").reason == "invalid"
    assert p.authenticate("jdoe", "").reason == "invalid"
    assert p.authenticate("nobody", "x").reason == "invalid"          # unknown user: same answer as a wrong password
    assert p.authenticate("guest", "guest password").reason == "not_enrolled"


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_no_sign_in_gives_one_user_with_the_open_groups_scope(monkeypatch):
    monkeypatch.setattr(providers, "settings", lambda: _Settings({"auth.provider": "None",
                                                                 "auth.open_groups": ["KA-Staff-TAL"]}))
    monkeypatch.setattr(providers, "load_scopes", lambda: CFG)
    u = providers.open_user()
    assert u.name == providers.OPEN_USER and u.scope.factories == {"TAL"} and not u.scope.is_admin
    assert not providers.sign_in_required()
    with pytest.raises(RuntimeError):
        providers.get_provider()                                       # no provider to sign in with


def test_no_sign_in_fails_closed_and_is_off_unless_chosen(monkeypatch):
    monkeypatch.setattr(providers, "load_scopes", lambda: CFG)
    for groups in (None, [], ["Domain Users"]):                        # nothing configured: refuse, never open all
        monkeypatch.setattr(providers, "settings", lambda g=groups: _Settings({"auth.provider": "none",
                                                                              "auth.open_groups": g}))
        with pytest.raises(RuntimeError):
            providers.open_user()
    for kind in (None, "ldap", "local"):
        monkeypatch.setattr(providers, "settings", lambda k=kind: _Settings({} if k is None else {"auth.provider": k}))
        assert providers.sign_in_required() and providers.open_user() is None


def test_account_names():
    assert account_name(" BITOPI\\JDoe ") == "jdoe" and account_name("jdoe@bitopibd.com") == "jdoe"
    assert account_name("j*doe") == "" and account_name("") == ""


def test_throttle_locks_after_repeated_failures(tmp_path):
    p = _local(tmp_path, {"jdoe": {"password_hash": _hash("correct horse"), "groups": ["KA-Staff-TAL"]}})
    t = LoginThrottle(max_failures=3, window_s=600)
    for _ in range(3):
        assert sign_in("jdoe", "wrong", p, t).reason == "invalid"
    assert sign_in("jdoe", "correct horse", p, t).reason == "locked"   # even the right password waits
    t2 = LoginThrottle(max_failures=3, window_s=600)
    sign_in("jdoe", "wrong", p, t2)
    assert sign_in("jdoe", "correct horse", p, t2).reason == "ok"
    assert t2.allowed("jdoe") and not t2._fails.get("jdoe")            # success clears the count


# ---------------------------------------------------------------- LDAP provider (fake ldap3 connection)
class FakeEntry:
    def __init__(self, groups, display="Jane Doe"):
        self._attrs = {"memberOf": type("A", (), {"values": groups})(),
                       "displayName": type("A", (), {"value": display})()}

    def __contains__(self, k):
        return k in self._attrs

    def __getattr__(self, k):
        try:
            return self._attrs[k]
        except KeyError:
            raise AttributeError(k)


class FakeConnection:
    """Behaves like ldap3 2.9's Connection where it matters: open() returns None on success and raises
    LDAPSocketOpenError when the server cannot be reached; start_tls() and bind() return booleans. (An
    earlier fake returned True from open(), which hid a bug that refused every real sign-in.)"""
    instances = []

    def __init__(self, server, user=None, password=None, authentication=None, **kw):
        self.server, self.user, self.password, self.auth = server, user, password, authentication
        self.search_filter, self.tls_started, self.entries, self.closed = None, False, [], True
        FakeConnection.instances.append(self)

    def open(self):
        from ldap3.core.exceptions import LDAPSocketOpenError
        if not FakeConnection.reachable:
            raise LDAPSocketOpenError("socket connection error")
        self.closed = False

    def start_tls(self):
        self.tls_started = True
        return True

    def bind(self):
        return self.password == "right password"

    def search(self, base, flt, attributes=None):
        self.search_filter = flt
        self.entries = [FakeEntry(["CN=KA-Staff-TAL,OU=Groups,DC=bitopi,DC=local",
                                   "CN=Staff\\, all,OU=Groups,DC=bitopi,DC=local"])]

    def unbind(self):
        pass


@pytest.fixture
def fake_ldap(monkeypatch):
    import ldap3
    FakeConnection.instances, FakeConnection.reachable = [], True
    monkeypatch.setattr(ldap3, "Connection", FakeConnection)
    monkeypatch.setattr(ldap3, "Server", lambda *a, **kw: ("server", a, kw))
    return FakeConnection


def test_ldap_binds_as_the_user_and_maps_groups(fake_ldap):
    p = LdapProvider("dc1.bitopi.local", "bitopi.local", "DC=bitopi,DC=local", scopes_cfg=CFG)
    r = p.authenticate("BITOPI\\jdoe", "right password")
    assert r.reason == "ok" and r.user.scope.factories == {"TAL"} and r.user.display == "Jane Doe"
    conn = fake_ldap.instances[-1]
    assert conn.user == "jdoe@bitopi.local" and "Staff, all" in r.user.groups


def test_ldap_refuses_an_empty_password_without_binding(fake_ldap):
    p = LdapProvider("dc1", "bitopi.local", "DC=bitopi,DC=local", scopes_cfg=CFG)
    assert p.authenticate("jdoe", "").reason == "invalid"
    assert fake_ldap.instances == []                                   # no connection at all: AD would say "ok"


def test_ldap_escapes_the_name_and_uses_starttls_without_ldaps(fake_ldap):
    p = LdapProvider("dc1", "bitopi.local", "DC=bitopi,DC=local", use_ssl=False, scopes_cfg=CFG)
    assert p.authenticate("jdoe", "wrong").reason == "invalid"
    assert fake_ldap.instances[-1].tls_started
    assert account_name("jd(o)e*") == ""                               # filter metacharacters never reach a search
    p.authenticate("jdoe", "right password")
    assert fake_ldap.instances[-1].search_filter == "(&(objectClass=user)(sAMAccountName=jdoe))"


def test_ldap_unreachable_server(fake_ldap):
    fake_ldap.reachable = False
    p = LdapProvider("dc1", "bitopi.local", "DC=bitopi,DC=local", scopes_cfg=CFG)
    assert p.authenticate("jdoe", "right password").reason == "unavailable"
