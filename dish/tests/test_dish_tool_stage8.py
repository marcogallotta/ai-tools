"""Legacy Stage 8 reports were replaced by the Step 10 operation model."""

from pathlib import Path


def test_legacy_reports_are_replaced_by_step10_operational_reports():
    reports = (
        Path(__file__).resolve().parents[1] / "dish-reports.sql"
    ).read_text(encoding="utf-8")
    for name in (
        "compatibility_failures",
        "schema_migrations_and_failures",
        "drift_and_stale_baselines",
        "write_outcomes_and_uncertain_recovery",
        "verification_cycles",
        "verification_routes",
        "post_signoff_invalidations",
        "signoff_vs_movement",
        "tool_protocol_disagreements",
        "movement_outcomes",
    ):
        assert f"-- report: {name}" in reports
    assert "-- report: rejection_rates" not in reports
