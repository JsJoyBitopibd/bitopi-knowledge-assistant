"""Phase M: the Quality domain's data-quality checks.

Why these exist at all: the stage ladder (docs/AGENTS_DESIGN.md §4) gates Stage 2 per domain on "the
data-quality report for the domain is clean enough that owners trust its counts". Quality is the one
domain whose data is live, but it had no checks, so it could not evidence that gate either way — it was
invisible rather than failing. These are also the FIRST checks that read the VistaQ host; every other
check reads the production server.

The constraint that shapes them: a check's SQL goes through virtual.validate_definition(), which rejects
rag.* references, parameters, TOP/LIMIT, a leading WITH and ORDER BY without TOP. So a check reads raw
dbo.* and uses a scalar subquery (or a cross-joined derived table) for "the newest day".
"""
from pathlib import Path

from ragbot.data.catalog import load_catalogs
from ragbot.data.quality import load_checks

ROOT = Path(__file__).resolve().parents[1]
QUALITY = {
    "vistaq_inspections_fresh",
    "vistaq_defects_fresh",
    "vistaq_factory_coverage",
    "vistaq_summary_job_lag",
    "vistaq_targets_sane",
    "vistaq_inspections_without_a_file",
}


def _checks():
    return load_checks(ROOT / "config" / "data_quality.yaml")


def _quality():
    return [c for c in _checks() if c.get("agent") == "quality"]


def test_the_quality_domain_has_checks_so_it_can_evidence_its_gate():
    """Before this the report had no `## quality` section at all, and `render_markdown()` builds a section
    per distinct agent — so the domain's gate could not be shown to be passed or failed."""
    assert {c["name"] for c in _quality()} == QUALITY


def test_every_quality_check_reads_the_vistaq_catalog_that_is_actually_loaded():
    """run_checks() looks up catalogs[check["database"]] — a name that is not a loaded catalog fails only
    at run time, against a live server."""
    cats = load_catalogs()
    for c in _quality():
        assert c["database"] == "VistaQ_Default_RHL", c["name"]
        assert c["database"] in cats, c["name"]


def test_no_quality_check_uses_a_curated_view_or_a_row_cap():
    """validate_definition() rejects both, but a check that tripped it would be caught only when the file
    is loaded — assert the rule here so the reason stays written down beside the checks."""
    for c in _quality():
        sql = c["sql"].lower()
        assert "rag." not in sql, c["name"]
        assert " top " not in sql and " limit " not in sql, c["name"]
        assert "@" not in sql and "?" not in sql, c["name"]


def test_the_big_table_is_only_ever_read_through_its_date_only_index():
    """dbo.QualityInspection holds ~88 million rows. The catalog header records that filtering on
    InspectionDate instead of InspectionDateOnly turns 0.2 s into 4-6 s; at this table's size a check that
    did it would run for minutes and time out."""
    for c in _quality():
        sql = c["sql"]
        if "dbo.QualityInspection" in sql and "dbo.QualityInspectionDefect" not in sql:
            assert "InspectionDateOnly" in sql, c["name"]
        if "dbo.QualityInspectionDefect" in sql:
            assert "InspectionDefectDateOnly" in sql, c["name"]


def test_the_summary_lag_check_watches_both_summary_tables():
    """The lag was reported to the VistaQ owners on 7 Oct 2026 (DefectSummary 29 Sep, QCSummary 5 Oct) and
    nothing measured it afterwards. Both tables are in exclude_tables, so no answer reads them — which is
    exactly why only a check can see the job is behind."""
    sql = next(c["sql"] for c in _quality() if c["name"] == "vistaq_summary_job_lag")
    assert "dbo.QCSummary" in sql and "dbo.DefectSummary" in sql
    assert "dbo.QualityInspection" in sql and "dbo.QualityInspectionDefect" in sql


def test_the_file_coverage_check_bounds_the_risk_grain():
    """Phase M attaches the quality risk level to the VistaQ file (FR number). An inspection whose plan
    carries no file cannot be given a level, so this check is the measure of how much of production the
    risk level can actually cover."""
    c = next(c for c in _quality() if c["name"] == "vistaq_inspections_without_a_file")
    assert "dbo.ProductionPlan" in c["sql"] and "FileId" in c["sql"]
    assert c["kind"] == "share"


def test_the_target_check_guards_the_source_of_every_amber_level():
    """Amber is VistaQ's own target (dbo.Threshold), not a number this project chose. A missing, duplicated
    or expired row would move every amber level silently."""
    c = next(c for c in _quality() if c["name"] == "vistaq_targets_sane")
    assert "dbo.Threshold" in c["sql"] and c["kind"] == "count" and c["max"] == 0


def test_the_quality_agent_watches_vistaq_s_own_freshness():
    """Without a `kind: data` rule a VistaQ stall reads as "DHU 0" or "no rows" rather than as a warning.
    kind: data keeps it out of the health score and in the brief's data-warning line (brief.data_warnings)."""
    import yaml
    agent = yaml.safe_load((ROOT / "config" / "agents" / "quality.yaml").read_text(encoding="utf-8"))
    rule = next((r for r in agent.get("watch") or [] if r["name"] == "vistaq_data_age"), None)
    assert rule is not None, "the Quality agent has no VistaQ data-age rule"
    assert rule["kind"] == "data"
    assert rule["amber"] < rule["red"]
    assert "GROUP BY Factory" in rule["sql"]      # one row per factory, so a stalled factory is named
