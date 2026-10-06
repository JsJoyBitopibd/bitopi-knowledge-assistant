"""Phase L: watch rules, the signals store, and the morning brief (health score, ranking, scope)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from ragbot.auth.models import Scope
from ragbot.data.catalog import load_catalogs
from ragbot.domain_agents import Agent, load_agents
from ragbot.domain_agents import brief as bf
from ragbot.domain_agents.signals import latest, write_run
from ragbot.domain_agents.watch import Signal, level, rules, run_all, run_rule, validate


def _sig(factory="TAL", lv="red", value=10.0, weight=1.0, metric="", rule="r"):
    return Signal(agent="order", rule=rule, factory=factory, level=lv, value=value, title=f"{rule} {factory}",
                  question=f"Which {factory} orders are behind schedule?", weight=weight, metric=metric,
                  database="BitopiSplint", views=["rag.vw_OrderShipment"], sql="SELECT 1", as_of="2026-10-06T09:00:00")


# ---------------------------------------------------------------- rules
def test_real_watch_rules_validate():
    agents = load_agents()
    assert len(rules(agents)) >= 6
    assert validate(agents, load_catalogs()) == []


def test_validate_catches_bad_rules():
    a = Agent(name="x", title="X", extra={"watch": [
        {"name": "w1", "title": "t", "database": "Nope", "sql": "SELECT Factory FROM rag.v", "value": "n"},
        {"name": "w2", "title": "t", "database": "BitopiSplint", "value": "n", "amber": 5,
         "sql": "SELECT Factory, COUNT(*) AS n FROM rag.vw_ExportOrder GROUP BY Factory"},
        {"name": "w3", "title": "t", "database": "BitopiSplint", "value": "n", "amber": 9, "red": 2,
         "sql": "DELETE FROM rag.vw_ExportOrder"},
    ]})
    problems = validate({"x": a}, load_catalogs())
    assert any("Nope" in p for p in problems)
    assert any("both amber and red" in p for p in problems)
    assert any("wrong order" in p for p in problems) and any("guard refuses" in p for p in problems)


def test_levels_above_below_and_info():
    up = {"amber": 1, "red": 50}
    assert [level(up, v) for v in (0, 1, 49, 50)] == ["green", "amber", "amber", "red"]
    down = {"amber": 90, "red": 80, "direction": "below"}
    assert [level(down, v) for v in (95, 90, 81, 80)] == ["green", "amber", "amber", "red"]
    assert level({}, 7) == "info" and level(up, None) == "green"


def test_run_rule_guards_scopes_nothing_and_fills_titles():
    cat = load_catalogs()["BitopiSplint"]
    rule = {"agent": "order", "name": "behind", "title": "{n} late on {day}", "question": "Which {factory} orders are behind schedule?",
            "database": "BitopiSplint", "value": "n", "amber": 1, "red": 50, "weight": 3, "metric": "Behind",
            "sql": "SELECT Factory, COUNT(*) AS n FROM rag.vw_OrderShipment GROUP BY Factory"}
    seen = {}

    def runner(engine, sql, params, **kw):
        seen.update(sql=sql, **kw)
        return ["Factory", "n", "day"], [["TAL", 1234, datetime(2026, 9, 20)], ["RHL", 0, datetime(2026, 9, 20)]]
    out = run_rule(rule, cat, runner)
    assert "rag_vw_OrderShipment" in seen["sql"] and "IN ('" not in seen["sql"]   # every factory, filtered on read
    assert seen["tool"] == "watch:order.behind"
    tal, rhl = out
    assert (tal.level, rhl.level) == ("red", "green")
    assert tal.title == "1,234 late on 20 Sep 2026"
    assert tal.question == "Which TAL orders are behind schedule?"
    assert tal.views == ["rag.vw_OrderShipment"]


def test_a_failing_rule_does_not_stop_the_others():
    cats = {"D": SimpleNamespace(engine="sqlserver", connection_env="X", connection_database=None, database="D",
                                 view_names=frozenset({"rag.vw_a"}), dialect="tsql", view=lambda n: None)}
    a = Agent(name="x", title="X", extra={"watch": [
        {"name": "bad", "title": "t", "database": "Missing", "sql": "SELECT 1", "value": "n"}]})
    signals, errors = run_all({"x": a}, cats, lambda *a, **k: ([], []))
    assert signals == [] and errors and "Missing" in errors[0]


# ---------------------------------------------------------------- store
def test_store_round_trip_and_scope_filter(tmp_path):
    db = tmp_path / "signals.db"
    assert latest(Scope.unrestricted(), db) == (None, [])
    write_run([_sig("TAL"), _sig("RHL", "amber")], datetime(2026, 10, 6, 9), [], db)
    write_run([_sig("TAL", "green"), _sig("RHL", "red"), _sig("02", "red")], datetime(2026, 10, 6, 10), ["x: boom"], db)
    run, sigs = latest(Scope.unrestricted(), db)
    assert run["id"] == 2 and run["errors"] == ["x: boom"] and len(sigs) == 3          # the newest run only
    tal_only = Scope(factories=frozenset({"TAL"}), departments=frozenset({"*"}), confidentiality_max="internal")
    _, mine = latest(tal_only, db)
    assert [s.factory for s in mine] == ["TAL"] and mine[0].level == "green"


# ---------------------------------------------------------------- brief
def test_health_formula():
    h = bf.health([_sig(lv="red"), _sig(lv="amber"), _sig(lv="green"), _sig(lv="green"), _sig(lv="info")])
    assert (h.red, h.amber, h.green) == (1, 1, 2)
    assert h.score == round(100 * (1 - (1 + 0.5) / 4)) == 62
    assert "= 62" in h.how
    assert bf.health([_sig(lv="info")]).score is None


def test_data_checks_are_warnings_not_health_or_attention():
    stale = _sig("TAL", "red", 16, rule="line_data_age")
    stale.kind = "data"
    sigs = [stale, _sig("TAL", "green")]
    b = bf.build(sigs)
    assert b.health.score == 100 and b.attention == [] and b.data_warnings == [stale]


def test_kind_survives_the_store(tmp_path):
    s = _sig("TAL", "red")
    s.kind = "data"
    write_run([s], datetime(2026, 10, 6, 9), [], tmp_path / "s.db")
    assert latest(Scope.unrestricted(), tmp_path / "s.db")[1][0].kind == "data"


def test_a_store_from_before_kind_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE watch_run (id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, "
                      "finished TEXT NOT NULL, signals INTEGER NOT NULL, errors TEXT NOT NULL DEFAULT '[]');"
                      "CREATE TABLE signal (run_id INTEGER NOT NULL, agent TEXT NOT NULL, rule TEXT NOT NULL, "
                      "factory TEXT NOT NULL, level TEXT NOT NULL, value REAL, title TEXT NOT NULL, question TEXT NOT NULL, "
                      "weight REAL NOT NULL, metric TEXT NOT NULL, database TEXT NOT NULL, views TEXT NOT NULL, "
                      "sql TEXT NOT NULL, as_of TEXT NOT NULL, vals TEXT NOT NULL);")
    con.close()
    write_run([_sig("TAL")], datetime(2026, 10, 6, 9), [], db)
    assert latest(Scope.unrestricted(), db)[1][0].kind == "ops"


def test_attention_ranks_red_then_weight_then_value():
    sigs = [_sig("A", "amber", 99, 9), _sig("B", "red", 1, 1), _sig("C", "red", 5, 3), _sig("D", "red", 50, 3),
            _sig("E", "green", 1000, 9), _sig("F", "info", 1000, 9)]
    assert [s.factory for s in bf.attention(sigs, 10)] == ["D", "C", "B", "A"]
    assert len(bf.attention(sigs, 2)) == 2


def test_metrics_sum_over_the_viewers_factories():
    sigs = [_sig("TAL", "red", 700, metric="Behind"), _sig("RHL", "amber", 61, metric="Behind"),
            _sig("TAL", "info", 900, metric="Shipping")]
    assert bf.metrics(sigs) == [("Behind", 761), ("Shipping", 900)]


def test_greeting_uses_factory_time():
    assert bf.greeting(datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)) == "Good morning"     # 08:00 in Dhaka
    assert bf.greeting(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)) == "Good afternoon"   # 14:00
    assert bf.greeting(datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)) == "Good evening"    # 19:00


def test_stale_check():
    now = datetime(2026, 10, 6, 12, 0)
    assert not bf.is_stale("2026-10-06T11:00:00", now, 180)
    assert bf.is_stale("2026-10-06T08:00:00", now, 180) and bf.is_stale("garbage", now, 180)
