"""Phase K4: data-quality checks — the real check file validates, evaluation is pure, one failing check
does not stop the report."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ragbot.config import ROOT
from ragbot.data.guard import GuardError
from ragbot.data.quality import evaluate, load_checks, render_markdown, run_checks

TODAY = date(2026, 10, 6)


def test_real_checks_load_and_are_safe_selects():
    checks = load_checks(ROOT / "config" / "data_quality.yaml")
    assert len(checks) >= 10
    assert {c["kind"] for c in checks} <= {"freshness", "count", "share"}


def test_a_write_statement_is_refused(tmp_path: Path):
    f = tmp_path / "dq.yaml"
    f.write_text("checks:\n  - name: bad\n    database: X\n    kind: count\n    sql: DELETE FROM dbo.T\n", encoding="utf-8")
    with pytest.raises(GuardError):
        load_checks(f)


def test_duplicate_names_and_unknown_kinds_are_refused(tmp_path: Path):
    f = tmp_path / "dq.yaml"
    f.write_text("checks:\n  - {name: a, database: X, kind: count, sql: SELECT 1 AS n}\n"
                 "  - {name: a, database: X, kind: count, sql: SELECT 1 AS n}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_checks(f)
    f.write_text("checks:\n  - {name: a, database: X, kind: guess, sql: SELECT 1 AS n}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_checks(f)


def test_freshness():
    c = {"kind": "freshness", "max_age_days": 3}
    assert evaluate(c, ["last_date"], [[datetime(2026, 10, 4, 9, 0)]], TODAY)[0] == "ok"
    status, value = evaluate(c, ["last_date"], [[date(2026, 8, 22)]], TODAY)
    assert status == "stale" and "45 day(s) ago" in value
    assert evaluate(c, ["last_date"], [[None]], TODAY) == ("stale", "no rows")


def test_count_and_information_only():
    assert evaluate({"kind": "count"}, ["n"], [[0]], TODAY)[0] == "ok"
    assert evaluate({"kind": "count"}, ["n"], [[5]], TODAY)[0] == "issue"
    assert evaluate({"kind": "count", "max": None}, ["n"], [[47]], TODAY) == ("info", "47")


def test_share():
    c = {"kind": "share", "max_share": 0.2}
    assert evaluate(c, ["bad", "total"], [[15, 100]], TODAY)[0] == "ok"
    status, value = evaluate(c, ["BAD", "TOTAL"], [[96, 100]], TODAY)     # column case does not matter
    assert status == "issue" and "96%" in value
    assert evaluate(c, ["bad", "total"], [[0, 0]], TODAY)[0] == "ok"


def test_one_failing_check_does_not_stop_the_others():
    checks = [{"name": "boom", "database": "D", "kind": "count", "sql": "SELECT 1 AS n"},
              {"name": "fine", "database": "D", "kind": "count", "sql": "SELECT 0 AS n"},
              {"name": "nodb", "database": "Missing", "kind": "count", "sql": "SELECT 0 AS n"}]
    cats = {"D": SimpleNamespace(engine="sqlserver", connection_env="X", connection_database=None)}

    def runner(engine, sql, params, **kw):
        if "1" in sql:
            raise RuntimeError("timeout")
        assert kw["tool"].startswith("quality:") and params == {}
        return ["n"], [[0]]
    res = {r.name: r for r in run_checks(checks, cats, runner, today=TODAY)}
    assert res["boom"].status == "error" and "timeout" in res["boom"].value
    assert res["fine"].status == "ok"
    assert res["nodb"].status == "error"
    md = render_markdown(list(res.values()), datetime(2026, 10, 6, 9, 0))
    assert md.startswith("# Data-quality report") and "| error |" in md
