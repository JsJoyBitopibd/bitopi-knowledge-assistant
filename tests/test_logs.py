"""F1 audit: every log file's rows match its header; an administrator can read back one user's rows."""
import csv

from ragbot.logs import append_row, rows_for_user

H1 = ["ts", "user", "route", "question"]
H2 = ["ts", "user", "scope", "route", "question"]


def _read(f):
    with open(f, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_rows_are_appended_under_one_header(tmp_path):
    append_row("chat.csv", H2, ["t1", "jdoe", "f=TAL", "documents", "q1"], folder=tmp_path)
    append_row("chat.csv", H2, ["t2", "jdoe", "f=TAL", "data", "q2"], folder=tmp_path)
    assert _read(tmp_path / "chat.csv") == [H2, ["t1", "jdoe", "f=TAL", "documents", "q1"],
                                            ["t2", "jdoe", "f=TAL", "data", "q2"]]


def test_a_file_with_an_older_header_is_rotated_not_mixed(tmp_path):
    append_row("chat.csv", H1, ["t0", "jdoe", "documents", "old question"], folder=tmp_path)
    append_row("chat.csv", H2, ["t1", "jdoe", "f=TAL", "documents", "new question"], folder=tmp_path)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2 and "chat.csv" in files
    rotated = next(p for p in tmp_path.iterdir() if p.name != "chat.csv")
    assert _read(rotated)[0] == H1 and _read(tmp_path / "chat.csv")[0] == H2


def test_rows_for_user_reads_current_and_rotated_files(tmp_path):
    append_row("chat.csv", H1, ["2026-09-27T10:00:00", "JDoe", "documents", "old"], folder=tmp_path)
    append_row("chat.csv", H2, ["2026-09-28T09:00:00", "jdoe", "f=TAL", "data", "new"], folder=tmp_path)
    append_row("chat.csv", H2, ["2026-09-28T09:05:00", "someone", "f=*", "data", "other"], folder=tmp_path)
    append_row("chat_other.csv", H2, ["2026-09-28T09:06:00", "jdoe", "f=*", "data", "not chat.csv"], folder=tmp_path)
    rows = rows_for_user("chat.csv", "jdoe", folder=tmp_path)
    assert [r["question"] for r in rows] == ["old", "new"]
    assert rows[0].get("scope") is None and rows[1]["scope"] == "f=TAL"


def test_sql_rows_carry_the_database_they_ran_on(tmp_path, monkeypatch):
    """P0: 19 catalogs share one connection line, so the statement alone did not say where it ran."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    from ragbot.data.connectors import SQL_HEADER, _log
    _log("sqlserver", "generated", "SELECT 1", "SELECT 1", {}, 1, 12.0, user="u", scope="f=*", database="Inventory")
    rows = _read(tmp_path / "sql.csv")
    assert rows[0] == SQL_HEADER and rows[0][-1] == "database"
    assert rows[1][SQL_HEADER.index("database")] == "Inventory"


def test_generation_log_records_the_reply_the_database_line_and_guard_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    from ragbot.data.schema_usage import GEN_HEADER, log_generation
    log_generation(user="u", agent="", kind="guard", considered=["FM", "Inventory"], database_line="DATABASE: FM",
                   database_used="FM", guard_error="GuardError: forbidden token 'delete'", stop_reason="stop",
                   raw_reply="DATABASE: FM\nDELETE FROM x")
    log_generation(user="u", agent="", kind="generate", considered=["FM", "Inventory"], database_used="Inventory",
                   fallback=True, rows=0, raw_reply="SELECT 1")
    rows = _read(tmp_path / "sql_gen.csv")
    assert rows[0] == GEN_HEADER
    first, second = dict(zip(GEN_HEADER, rows[1])), dict(zip(GEN_HEADER, rows[2]))
    assert first["kind"] == "guard" and first["database_line"] == "DATABASE: FM" and "delete" in first["guard_error"]
    assert first["databases_considered"] == "FM;Inventory" and first["raw_reply"].startswith("DATABASE: FM")
    assert second["fallback"] == "yes" and second["database_used"] == "Inventory" and second["rows"] == "0"
