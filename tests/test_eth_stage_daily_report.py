import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.eth_stage_daily_report as report  # noqa: E402


def test_refresh_postmarket_reports_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        report,
        "_generate_daily_review",
        lambda day: {"summary": {"total_trades": 8, "closed_trades": 8,
                                 "total_pnl_net": 34.56}},
        raising=True,
    )

    assert report.refresh_postmarket("2026-09-18") is True
    out = capsys.readouterr().out
    assert "终稿已刷新: 2026-09-18" in out
    assert "total=8" in out
    assert "closed=8" in out
    assert "net=34.56" in out


def test_refresh_postmarket_failure_is_visible(monkeypatch, capsys):
    monkeypatch.setattr(
        report,
        "_generate_daily_review",
        lambda day: (_ for _ in ()).throw(RuntimeError("db locked")),
        raising=True,
    )

    assert report.refresh_postmarket("2026-09-18") is False
    out = capsys.readouterr().out
    assert "[postmarket][ERROR]" in out
    assert "db locked" in out


def test_render_includes_research_exposure(monkeypatch):
    empty = {"price_gate": 0, "pretrade_gate": 0, "attempts": 0, "filled": 0,
             "errors": [], "pretrade_reasons": {}}
    monkeypatch.setattr(report, "funnel", lambda day: {
        "premarket": dict(empty), "afterhours": dict(empty),
    }, raising=True)
    monkeypatch.setattr(report, "rows_from_db", lambda db, start, end: [], raising=True)
    monkeypatch.setattr(report, "holding_ext", lambda: [], raising=True)
    monkeypatch.setattr(report, "recent_rounds", lambda: "", raising=True)
    monkeypatch.setattr(report, "research_exposure", lambda day: {
        "Semiconductors": {"n": 2, "open": 1, "notional": 10000.0,
                           "symbols": ["US.MU", "US.NVDA"]},
    }, raising=True)

    body = report.render("2026-10-19")
    assert "研究臂暴露" in body
    assert "Semiconductors" in body
    assert "$10,000" in body


def test_main_refreshes_only_when_extended_sample_exists(monkeypatch):
    calls = []
    monkeypatch.setattr(report, "funnel", lambda day: {
        "premarket": {"attempts": 0, "price_gate": 0},
        "afterhours": {"attempts": 0, "price_gate": 0},
    }, raising=True)
    monkeypatch.setattr(report, "rows_from_db", lambda db, start, end: [], raising=True)
    monkeypatch.setattr(report, "holding_ext", lambda: [], raising=True)
    monkeypatch.setattr(report, "render", lambda day: "report", raising=True)
    monkeypatch.setattr(report, "refresh_postmarket", lambda day: calls.append(day) or True,
                        raising=True)

    assert report.main(["--date", "2026-09-18", "--refresh-postmarket"]) == 0
    assert calls == []
