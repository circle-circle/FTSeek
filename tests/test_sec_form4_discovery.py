"""SEC Form 4 全市场发现 (shadow) 测试 (2026-10-18)."""
from app.data.sec_form4_provider import InsiderTransaction
from app.data import sec_form4_discovery as mod
from app.event_stream.sources.sec_provider import build_provider_events


def _txn(code="P", amount=120_000):
    return InsiderTransaction(
        symbol="ACME", company_name="ACME CORP", insider_name="Jane Doe",
        insider_cik="0000000001", insider_role="Officer", officer_title="CFO",
        transaction_code=code, transaction_date="2026-10-16",
        shares=1000, price_per_share=120, dollar_amount=amount,
        shares_after=5000, ownership_type="D", filing_date="2026-10-16",
        accession_number="0000000000-26-000001",
    )


class _FakeProvider:
    def get_ticker(self, cik):
        return "ACME" if int(cik) == 1234567 else None

    def _parse_form4_filing(self, url, symbol, company, filing_date, accession):
        return [_txn("P", 120_000), _txn("S", 20_000)]


def test_parse_issuer_entries_only_issuer():
    atom = """<feed>
      <entry><title>4 - ACME CORP (0001234567) (Issuer)</title>
        <summary>Filed: 2026-10-16 AccNo: 0000000000-26-000001</summary>
        <updated>2026-10-16T12:00:00Z</updated><link href="https://sec.gov/x"/></entry>
      <entry><title>4 - Jane Doe (0000000001) (Reporting)</title>
        <summary>Filed: 2026-10-16 AccNo: 0000000000-26-000001</summary>
        <updated>2026-10-16T12:00:00Z</updated><link href="https://sec.gov/x"/></entry>
    </feed>"""
    rows = mod.parse_issuer_entries(atom)
    assert len(rows) == 1
    assert rows[0]["cik"] == 1234567
    assert rows[0]["company"] == "ACME CORP"


def test_discover_recent_form4_filters_and_aggregates(tmp_path, monkeypatch):
    atom = """<feed>
      <entry><title>4 - ACME CORP (0001234567) (Issuer)</title>
        <summary>Filed: 2026-10-16 AccNo: 0000000000-26-000001</summary>
        <updated>2026-10-16T12:00:00Z</updated><link href="https://sec.gov/x"/></entry>
    </feed>"""
    monkeypatch.setattr(mod, "CACHE_FILE", tmp_path / "form4.json")
    monkeypatch.setattr(mod, "_fetch_atom", lambda: atom)
    rows = mod.discover_recent_form4(limit=5, provider=_FakeProvider())
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "ACME"
    assert row["buy_count"] == 1
    assert row["sell_count"] == 0  # $20K sale is below $50K filter
    assert row["net_buy_amount"] == 120_000


def test_form4_rows_become_canonical_events():
    rows = [{
        "ticker": "ACME", "company": "ACME CORP", "form": "4",
        "updated": "2026-10-16T12:00:00Z", "url": "https://sec.gov/f4",
        "buy_count": 2, "sell_count": 0, "net_buy_amount": 150000,
        "transactions": [{"transaction_code": "P", "dollar_amount": 150000}],
    }]
    events = build_provider_events([], [], rows)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "SEC_FORM4_FILING"
    assert event.facts["form"] == "4"
    assert event.facts["net_buy_amount"] == 150000
    assert "Form 4" in event.headline
