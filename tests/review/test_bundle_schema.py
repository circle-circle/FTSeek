import pytest

from app.review.bundle_schema import Bundle, Evidence, EvidenceError, Metric


def _metric(evidence=None, unit="usd"):
    return Metric(name="net_pnl", value=-197.97, n=335, unit=unit, evidence=evidence)


def test_missing_evidence_names_the_metric():
    bundle = Bundle(report_date="2026-09-18", headline={"net_pnl": _metric()})

    with pytest.raises(EvidenceError) as exc:
        bundle.validate()

    assert "headline.net_pnl" in str(exc.value)


def test_sql_evidence_passes_without_integrity_block():
    bundle = Bundle(
        report_date="2026-09-18",
        headline={
            "net_pnl": _metric(
                Evidence(kind="sql", ref="select sum(pnl_net)", params=("2026-09-18",))
            )
        },
    )

    bundle.validate()


def test_unknown_unit_is_rejected():
    bundle = Bundle(
        report_date="2026-09-18",
        headline={
            "net_pnl": _metric(
                Evidence(kind="sql", ref="select 1"), unit="furlongs"
            )
        },
    )

    with pytest.raises(EvidenceError, match="unit"):
        bundle.validate()


def test_artifact_requires_all_four_integrity_keys():
    evidence = Evidence(
        kind="artifact",
        ref="data/reviews/bundle.json",
        integrity={"n": 335, "window": "2026-08-01..2026-09-18", "universe": "db"},
    )
    bundle = Bundle(report_date="2026-09-18", headline={"net_pnl": _metric(evidence)})

    with pytest.raises(EvidenceError, match="truncation_checked"):
        bundle.validate()


def test_derived_integrity_values_are_typed():
    evidence = Evidence(
        kind="derived",
        ref="headline = sum(stage slices)",
        integrity={
            "n": True,
            "window": "2026-08-01..2026-09-18",
            "universe": "db:trade_records",
            "truncation_checked": True,
        },
    )
    bundle = Bundle(report_date="2026-09-18", headline={"net_pnl": _metric(evidence)})

    with pytest.raises(EvidenceError, match="n"):
        bundle.validate()
