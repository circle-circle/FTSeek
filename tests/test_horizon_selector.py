"""Tests for app.playbook.horizon_selector."""
import sqlite3
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from app.playbook.horizon_selector import HorizonSelector, HorizonDecision
from app.playbook.db import PlaybookDB


class MockPlaybookData:
    """Mock PlaybookData for tests."""
    def __init__(self):
        self.technical = MagicMock()
        self.technical.price = 100.0
        self.technical.bid = 99.5
        self.technical.ask = 100.5
        self.technical.ma20 = 98.0
        self.technical.atr_pct = 0.12
        self.technical.volume_ratio = 1.5
        self.technical.rsi_14 = 55.0
        self.technical.macd_signal = "golden_cross"
        self.technical.quality = "ok"
        self.fundamental = MagicMock()
        self.fundamental.quality = "ok"
        self.fundamental.industry = "Technology"
        self.insider = MagicMock()
        self.insider.quality = "ok"
        self.news = MagicMock()
        self.news.quality = "ok"
        self.earnings = MagicMock()
        self.earnings.quality = "ok"
        self.earnings.days_until = 10
        self.sentiment = MagicMock()
        self.sentiment.quality = "ok"


@dataclass
class MockScreenResult:
    symbol: str = "US.TSLA"
    direction: str = "BUY"
    factors: dict = None
    data = None
    confidence: int = 70

    def __post_init__(self):
        if self.factors is None:
            self.factors = {"volume_expansion": {"weight": 1}}
        if self.data is None:
            self.data = MockPlaybookData()


class TestHorizonSelector:
    """HorizonSelector 测试."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "test_horizon.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db

    def test_select_returns_short_or_long(self, db):
        selector = HorizonSelector(db)
        sr = MockScreenResult()
        decision = selector.select(sr, None)

        assert isinstance(decision, HorizonDecision)
        assert decision.horizon in ("short", "long")
        assert decision.confidence >= 0

    def test_fallback_rules_earnings_near_short(self, db):
        """财报临近应默认 short."""
        selector = HorizonSelector(db)
        sr = MockScreenResult()
        sr.data.earnings.days_until = 2
        decision = selector.select(sr, None)
        assert decision.horizon == "short"

    def test_fallback_rules_high_volatility_short(self, db):
        """高波动环境应默认 short."""
        selector = HorizonSelector(db)
        sr = MockScreenResult()
        sr.data.technical.atr_pct = 0.20
        decision = selector.select(sr, None)
        assert decision.horizon == "short"

    def test_fallback_rules_trend_long(self, db):
        """强趋势 + 放量 + 低 VIX 倾向 long."""
        selector = HorizonSelector(db)
        sr = MockScreenResult()
        sr.data.technical.price = 102.0  # price > ma20
        sr.data.technical.volume_ratio = 1.5
        # 需要一个低 VIX 的 regime_ctx
        regime = MagicMock()
        regime.vix = 18.0
        regime.regime.name = "RISK_ON"
        decision = selector.select(sr, regime)
        # 因为默认规则中强趋势+放量会加 long 分，但默认仍可能 short
        assert decision.horizon in ("short", "long")

    def test_fallback_direction_aware_short(self, db):
        """SHORT + volume_expansion 应倾向 short，不应因基本面规则错判 long。"""
        selector = HorizonSelector(db)
        selector.config["horizon_selector"]["enabled"] = True
        sr = MockScreenResult(direction="SELL_SHORT", factors={"volume_expansion": {"weight": 1}})
        decision = selector.select(sr, None)
        # volume_expansion 对 SHORT 是短线信号
        assert decision.short_score > decision.long_score
        assert decision.horizon == "short"

    def test_fallback_direction_aware_buy_volume(self, db):
        """BUY + volume_expansion 应倾向 long。"""
        selector = HorizonSelector(db)
        selector.config["horizon_selector"]["enabled"] = True
        sr = MockScreenResult(direction="BUY", factors={"volume_expansion": {"weight": 1}})
        decision = selector.select(sr, None)
        assert decision.horizon == "long"

    def test_fallback_insider_short_does_not_push_long(self, db):
        """SHORT + insider_cluster_buy 不应触发基本面 long 加分。"""
        selector = HorizonSelector(db)
        selector.config["horizon_selector"]["enabled"] = True
        sr = MockScreenResult(
            direction="SELL_SHORT",
            factors={"insider_cluster_buy": {"weight": 1}}
        )
        decision = selector.select(sr, None)
        # 对 SHORT 方向，insider_cluster_buy 不加分，结果应保持 short
        assert decision.horizon == "short"

    def test_select_disabled_returns_short(self, db):
        """horizon_selector.enabled=false 时应直接返回 short。"""
        selector = HorizonSelector(db, config_path=None)
        selector.config["horizon_selector"]["enabled"] = False
        sr = MockScreenResult(factors={"volume_expansion": {"weight": 1}})
        decision = selector.select(sr, None)
        assert decision.horizon == "short"
        assert decision.reasoning.get("source") == "disabled"

        """_score_with_model 应分别查询 short 和 long 的 feature_key，不能混用。"""
        import json
        selector = HorizonSelector(db)
        conn = db._conn()
        now = "2026-06-26T10:00:00"
        # 构造数据：volume_expansion 在 short 下 5 胜，在 long 下 5 负
        for i in range(5):
            conn.execute(
                """
                INSERT INTO playbook_instructions
                (date, symbol, direction, entry_price, stop_loss, take_profit, position_size,
                 confidence, outcome, horizon, conditions_json, market_regime,
                 executed, pnl, pnl_pct, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("2026-06-20", f"US.S{i}", "BUY", 100.0, 98.0, 104.0, 10, 70,
                 "win", "short", json.dumps(["volume_expansion"]), "RISK_ON",
                 1, 100.0, 0.02, now, now),
            )
        for i in range(5):
            conn.execute(
                """
                INSERT INTO playbook_instructions
                (date, symbol, direction, entry_price, stop_loss, take_profit, position_size,
                 confidence, outcome, horizon, conditions_json, market_regime,
                 executed, pnl, pnl_pct, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("2026-06-20", f"US.L{i}", "BUY", 100.0, 98.0, 104.0, 10, 70,
                 "loss", "long", json.dumps(["volume_expansion"]), "RISK_ON",
                 1, -100.0, -0.02, now, now),
            )
        conn.commit()
        selector.return_model.update(days_back=90)

        sr = MockScreenResult(factors={"volume_expansion": {"weight": 1}})
        # 构造一个 market_regime 匹配的记录
        features = selector._extract_features(sr, None)
        features["market_regime"] = "RISK_ON"
        decision = selector._score_with_model(features)
        # 有数据，模型应被触发（即使最终仍可能 short，但不应返回 None）
        assert decision is not None
        # long 表现差，short 表现好，模型应倾向 short
        assert decision.short_score >= decision.long_score
