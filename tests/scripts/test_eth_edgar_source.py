"""scripts/eth_edgar_source.py hermetic 测试 (9/24 增强批).

覆盖:
1. 8-K 筛选 (form=8-K + items 含 2.02)
2. items 缺失降级 (无 items 字段 → 返回空)
3. 习惯先验计算 (8/8 AMC, 5/3 混合, 历史不足)
4. 失败 fail-soft 返回 None
5. 缓存命中不发请求
6. UA 头存在断言
7. env 开关 ETH_PLAN_EDGAR_ENABLED=false → 零调用
8. edgar_phase_evidence (BMO/AMC/中间时段)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    """加载 eth_edgar_source 模块."""
    import importlib
    spec = importlib.util.spec_from_file_location(
        "eth_edgar_source", ROOT / "scripts" / "eth_edgar_source.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def edgar_mod():
    return _load()


# ── Fixture 数据 ──

def _make_submissions(filings: list[dict]) -> dict:
    """构造 submissions.recent 平行数组结构."""
    forms = []
    dates = []
    items = []
    accept_times = []
    accessions = []
    for f in filings:
        forms.append(f.get("form", ""))
        dates.append(f.get("filingDate", ""))
        items.append(f.get("items", ""))
        accept_times.append(f.get("acceptanceDateTime", ""))
        accessions.append(f.get("accessionNumber", ""))
    return {
        "form": forms,
        "filingDate": dates,
        "items": items,
        "acceptanceDateTime": accept_times,
        "accessionNumber": accessions,
    }


AAPL_8K_2020 = [
    {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02,9.01",
     "acceptanceDateTime": "2026-07-30T20:30:28.000Z", "accessionNumber": "0000320193-26-000050"},
    {"form": "8-K", "filingDate": "2026-04-30", "items": "2.02,9.01",
     "acceptanceDateTime": "2026-04-30T20:30:41.000Z", "accessionNumber": "0000320193-26-000030"},
    {"form": "8-K", "filingDate": "2026-01-30", "items": "2.02,9.01",
     "acceptanceDateTime": "2026-01-30T21:00:00.000Z", "accessionNumber": "0000320193-26-000010"},
    {"form": "8-K", "filingDate": "2025-10-30", "items": "2.02,9.01",
     "acceptanceDateTime": "2025-10-30T20:30:00.000Z", "accessionNumber": "0000320193-25-000080"},
    {"form": "8-K", "filingDate": "2025-07-31", "items": "2.02,9.01",
     "acceptanceDateTime": "2025-07-31T20:30:00.000Z", "accessionNumber": "0000320193-25-000060"},
    {"form": "8-K", "filingDate": "2025-05-01", "items": "2.02,9.01",
     "acceptanceDateTime": "2025-05-01T20:30:00.000Z", "accessionNumber": "0000320193-25-000040"},
    {"form": "8-K", "filingDate": "2025-01-30", "items": "2.02,9.01",
     "acceptanceDateTime": "2025-01-30T21:00:00.000Z", "accessionNumber": "0000320193-25-000010"},
    {"form": "8-K", "filingDate": "2024-10-31", "items": "2.02,9.01",
     "acceptanceDateTime": "2024-10-31T20:30:00.000Z", "accessionNumber": "0000320193-24-000080"},
]

# 8-K/A 修正件 (不应被筛入)
FILINGS_WITH_AMENDMENT = AAPL_8K_2020 + [
    {"form": "8-K/A", "filingDate": "2026-09-01", "items": "5.02",
     "acceptanceDateTime": "2026-09-01T16:00:00.000Z", "accessionNumber": "amend-001"},
]

# 混合 BMO + AMC (5 AMC + 3 BMO)
MIXED_FILINGS = [
    {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02",
     "acceptanceDateTime": "2026-07-30T20:30:00.000Z", "accessionNumber": "amc-1"},  # 16:30 ET AMC
    {"form": "8-K", "filingDate": "2026-04-30", "items": "2.02",
     "acceptanceDateTime": "2026-04-30T20:30:00.000Z", "accessionNumber": "amc-2"},  # 16:30 ET AMC
    {"form": "8-K", "filingDate": "2026-01-30", "items": "2.02",
     "acceptanceDateTime": "2026-01-30T08:00:00.000Z", "accessionNumber": "bmo-1"},  # 03:00 ET BMO
    {"form": "8-K", "filingDate": "2025-10-30", "items": "2.02",
     "acceptanceDateTime": "2025-10-30T20:30:00.000Z", "accessionNumber": "amc-3"},  # 16:30 ET AMC
    {"form": "8-K", "filingDate": "2025-07-31", "items": "2.02",
     "acceptanceDateTime": "2025-07-31T08:00:00.000Z", "accessionNumber": "bmo-2"},  # 04:00 ET BMO
    {"form": "8-K", "filingDate": "2025-05-01", "items": "2.02",
     "acceptanceDateTime": "2025-05-01T20:30:00.000Z", "accessionNumber": "amc-4"},  # 16:30 ET AMC
    {"form": "8-K", "filingDate": "2025-01-30", "items": "2.02",
     "acceptanceDateTime": "2025-01-30T21:30:00.000Z", "accessionNumber": "amc-5"},  # 16:30 ET AMC (EST)
    {"form": "8-K", "filingDate": "2024-10-31", "items": "2.02",
     "acceptanceDateTime": "2024-10-31T08:00:00.000Z", "accessionNumber": "bmo-3"},  # 04:00 ET BMO
]

# 历史不足 (< 3 次可分类)
SCARCE_FILINGS = [
    {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02",
     "acceptanceDateTime": "2026-07-30T20:30:00.000Z", "accessionNumber": "only-1"},
    {"form": "8-K", "filingDate": "2026-04-30", "items": "2.02",
     "acceptanceDateTime": "2026-04-30T14:00:00.000Z", "accessionNumber": "midday-1"},  # 中间时段
]


# ── 1. 8-K 筛选 ──

class TestFind8K2020:
    """_find_8k_2020_filings: form=8-K + items 含 2.02."""

    def test_finds_8k_2020(self, edgar_mod):
        recent = _make_submissions(AAPL_8K_2020)
        results = edgar_mod._find_8k_2020_filings(recent)
        assert len(results) == 8

    def test_excludes_8k_amendment(self, edgar_mod):
        """8-K/A 修正件不被筛入."""
        recent = _make_submissions(FILINGS_WITH_AMENDMENT)
        results = edgar_mod._find_8k_2020_filings(recent)
        assert len(results) == 8  # 不含 8-K/A

    def test_target_date_filter(self, edgar_mod):
        recent = _make_submissions(AAPL_8K_2020)
        results = edgar_mod._find_8k_2020_filings(recent, target_date="2026-07-30")
        assert len(results) == 1
        assert results[0]["filingDate"] == "2026-07-30"

    def test_target_date_no_match(self, edgar_mod):
        recent = _make_submissions(AAPL_8K_2020)
        results = edgar_mod._find_8k_2020_filings(recent, target_date="2099-01-01")
        assert results == []

    def test_items_without_2020_excluded(self, edgar_mod):
        """8-K 但 items 不含 2.02 → 不筛入."""
        filings = [
            {"form": "8-K", "filingDate": "2026-09-01", "items": "5.02",
             "acceptanceDateTime": "2026-09-01T16:00:00.000Z", "accessionNumber": "other"},
        ]
        recent = _make_submissions(filings)
        results = edgar_mod._find_8k_2020_filings(recent)
        assert results == []


# ── 2. items 缺失降级 ──

class TestItemsMissing:
    """items 字段缺失/空 → 返回空列表."""

    def test_empty_items_field(self, edgar_mod):
        filings = [
            {"form": "8-K", "filingDate": "2026-07-30", "items": "",
             "acceptanceDateTime": "2026-07-30T20:30:00.000Z", "accessionNumber": "no-items"},
        ]
        recent = _make_submissions(filings)
        results = edgar_mod._find_8k_2020_filings(recent)
        assert results == []

    def test_no_items_key_at_all(self, edgar_mod):
        """submissions.recent 完全没有 items 字段."""
        recent = {
            "form": ["8-K"],
            "filingDate": ["2026-07-30"],
            "acceptanceDateTime": ["2026-07-30T20:30:00.000Z"],
            "accessionNumber": ["x"],
            # 无 items 键
        }
        results = edgar_mod._find_8k_2020_filings(recent)
        assert results == []


# ── 3. 习惯先验 ──

class TestPhaseHabit:
    """edgar_phase_habit: 近 8 次 2.02 申报的时刻分布."""

    def test_all_amc(self, edgar_mod):
        """8/8 全 AMC → phase=amc, consistency=1.0."""
        recent = _make_submissions(AAPL_8K_2020)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_habit("AAPL")
        assert result is not None
        assert result["phase"] == "amc"
        assert result["consistency"] == 1.0
        assert result["n_history"] == 8
        assert result["basis"] == "edgar_8k_history"

    def test_mixed_5_3(self, edgar_mod):
        """5 AMC + 3 BMO → phase=amc, consistency=0.625."""
        recent = _make_submissions(MIXED_FILINGS)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_habit("AAPL")
        assert result is not None
        assert result["phase"] == "amc"
        assert result["consistency"] == 0.625
        assert result["n_history"] == 8

    def test_insufficient_history(self, edgar_mod):
        """< 3 次可分类 → None."""
        recent = _make_submissions(SCARCE_FILINGS)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_habit("AAPL")
        # SCARCE_FILINGS: 1 AMC + 0 BMO + 1 midday = 1 classified < 3
        assert result is None

    def test_no_cik(self, edgar_mod):
        """票无 CIK → None."""
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=None):
            result = edgar_mod.edgar_phase_habit("FAKE")
        assert result is None

    def test_submissions_fail(self, edgar_mod):
        """submissions 加载失败 → None."""
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=None):
            result = edgar_mod.edgar_phase_habit("AAPL")
        assert result is None


# ── 4. fail-soft ──

class TestFailSoft:
    """EDGAR 失败 → 返回 None (不崩)."""

    def test_edgar_8k_arrival_fetch_fail(self, edgar_mod):
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=None):
            result = edgar_mod.edgar_8k_arrival("AAPL", "2026-07-30")
        assert result is None

    def test_edgar_phase_evidence_no_data(self, edgar_mod):
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=None):
            result = edgar_mod.edgar_phase_evidence("AAPL", "2026-07-30")
        assert result is None

    def test_fetch_exception_returns_none(self, edgar_mod):
        """urlopen 异常 → _fetch 返回 None (内部捕获)."""
        with patch("urllib.request.urlopen", side_effect=Exception("network down")):
            result = edgar_mod._fetch("https://example.com")
        assert result is None


# ── 5. 缓存命中 ──

class TestCaching:
    """缓存命中不发请求."""

    def test_ticker_map_cache_hit(self, edgar_mod, tmp_path, monkeypatch):
        """ticker→CIK 映射缓存命中时不调 _fetch."""
        monkeypatch.setattr(edgar_mod, "CACHE_DIR", tmp_path)
        # 写缓存
        cache_file = tmp_path / "edgar_company_tickers.json"
        cache_file.write_text(json.dumps({"AAPL": 320193, "MSFT": 789019}))

        # 读缓存 (不调 _fetch)
        mock_fetch = MagicMock()
        with patch.object(edgar_mod, "_fetch", mock_fetch):
            result = edgar_mod.ticker_to_cik("AAPL")
        assert result == 320193
        mock_fetch.assert_not_called()

    def test_submissions_cache_hit(self, edgar_mod, tmp_path, monkeypatch):
        """submissions 缓存命中时不调 _fetch."""
        monkeypatch.setattr(edgar_mod, "CACHE_DIR", tmp_path)
        cik_padded = "0000320193"
        cache_file = tmp_path / f"edgar_submissions_{cik_padded}.json"
        recent = _make_submissions(AAPL_8K_2020)
        cache_file.write_text(json.dumps(recent))

        mock_fetch = MagicMock()
        with patch.object(edgar_mod, "_fetch", mock_fetch):
            result = edgar_mod._load_submissions(320193)
        assert result is not None
        assert "form" in result
        mock_fetch.assert_not_called()


# ── 6. UA 头 ──

class TestUserAgent:
    """User-Agent 必须声明身份."""

    def test_default_ua(self, edgar_mod):
        ua = edgar_mod._ua()
        assert "FTSeek" in ua or "admin" in ua
        assert "@" in ua  # 含 email

    def test_env_override_ua(self, edgar_mod, monkeypatch):
        monkeypatch.setenv("EDGAR_UA", "CustomBot test@example.org")
        assert edgar_mod._ua() == "CustomBot test@example.org"

    def test_fetch_sends_ua_header(self, edgar_mod):
        """_fetch 发送的请求含 UA 头."""
        import urllib.request
        captured = {}

        class MockResponse:
            def read(self):
                return b'{}'

        def mock_urlopen(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return MockResponse()

        with patch("urllib.request.urlopen", mock_urlopen):
            edgar_mod._fetch("https://example.com/test")
        assert "User-agent" in captured or "ua" in captured
        if "ua" in captured:
            assert "@" in captured["ua"]


# ── 7. env 开关 ──

class TestEnvSwitch:
    """ETH_PLAN_EDGAR_ENABLED=false → 零 EDGAR 调用."""

    def test_disabled_arrival(self, edgar_mod, monkeypatch):
        monkeypatch.setenv("ETH_PLAN_EDGAR_ENABLED", "false")
        result = edgar_mod.edgar_8k_arrival("AAPL", "2026-07-30")
        assert result is None

    def test_disabled_evidence(self, edgar_mod, monkeypatch):
        monkeypatch.setenv("ETH_PLAN_EDGAR_ENABLED", "false")
        result = edgar_mod.edgar_phase_evidence("AAPL", "2026-07-30")
        assert result is None

    def test_disabled_habit(self, edgar_mod, monkeypatch):
        monkeypatch.setenv("ETH_PLAN_EDGAR_ENABLED", "false")
        result = edgar_mod.edgar_phase_habit("AAPL")
        assert result is None

    def test_disabled_ticker_to_cik(self, edgar_mod, monkeypatch):
        monkeypatch.setenv("ETH_PLAN_EDGAR_ENABLED", "false")
        result = edgar_mod.ticker_to_cik("AAPL")
        assert result is None


# ── 8. edgar_phase_evidence ──

class TestPhaseEvidence:
    """edgar_phase_evidence: BMO/AMC/中间时段."""

    def test_bmo_evidence(self, edgar_mod):
        """提交时刻 < 09:30 ET → bmo."""
        # acceptanceDateTime 08:00 UTC = 04:00 ET (BMO)
        filings = [
            {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02",
             "acceptanceDateTime": "2026-07-30T08:00:00.000Z", "accessionNumber": "bmo-1"},
        ]
        recent = _make_submissions(filings)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_evidence("AAPL", "2026-07-30")
        assert result == "bmo"

    def test_amc_evidence(self, edgar_mod):
        """提交时刻 ≥ 16:00 ET → amc."""
        # acceptanceDateTime 20:30 UTC = 16:30 ET (AMC)
        filings = [
            {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02",
             "acceptanceDateTime": "2026-07-30T20:30:00.000Z", "accessionNumber": "amc-1"},
        ]
        recent = _make_submissions(filings)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_evidence("AAPL", "2026-07-30")
        assert result == "amc"

    def test_midday_no_evidence(self, edgar_mod):
        """中间时段 (09:30-15:59 ET) → None."""
        # acceptanceDateTime 14:00 UTC = 10:00 ET (中间)
        filings = [
            {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02",
             "acceptanceDateTime": "2026-07-30T14:00:00.000Z", "accessionNumber": "mid-1"},
        ]
        recent = _make_submissions(filings)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_evidence("AAPL", "2026-07-30")
        assert result is None

    def test_no_filing_on_date(self, edgar_mod):
        """该日无申报 → None."""
        recent = _make_submissions(AAPL_8K_2020)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_phase_evidence("AAPL", "2099-01-01")
        assert result is None


# ── 9. edgar_8k_arrival ──

class TestEdgar8KArrival:
    """edgar_8k_arrival: 该票该日是否有 2.02 申报."""

    def test_arrival_found(self, edgar_mod):
        recent = _make_submissions(AAPL_8K_2020)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_8k_arrival("AAPL", "2026-07-30")
        assert result is not None
        assert "filed_at" in result
        assert "accession" in result
        # filed_at 是 ET ISO
        assert "2026-07-30" in result["filed_at"]

    def test_arrival_not_found(self, edgar_mod):
        recent = _make_submissions(AAPL_8K_2020)
        with patch.object(edgar_mod, "_edgar_enabled", return_value=True), \
             patch.object(edgar_mod, "ticker_to_cik", return_value=320193), \
             patch.object(edgar_mod, "_load_submissions", return_value=recent):
            result = edgar_mod.edgar_8k_arrival("AAPL", "2099-01-01")
        assert result is None
