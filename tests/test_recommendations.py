"""推荐系统冒烟测试"""
import pytest
import json
from unittest.mock import patch, MagicMock


class TestRecommendationSynthesizer:
    """测试推荐合成器"""

    def test_synthesize_basic(self):
        """测试基本合成逻辑"""
        from app.recommendations.synthesizer import RecommendationSynthesizer

        synth = RecommendationSynthesizer()

        battle_plan = {
            "date": "2026-05-28",
            "market_regime": "BULLISH",
            "targets": [
                {
                    "symbol": "NVDA",
                    "direction": "LONG",
                    "entry_logic": "Gap up on earnings",
                    "entry_zone": {"low": 145.0, "high": 147.0},
                    "stop_loss": 140.0,
                    "take_profit": 155.0,
                    "catalyst": "Earnings Beat",
                    "urgency": "HIGH",
                }
            ],
            "restricted": ["LCID"],
        }

        voting_results = {
            "NVDA": {
                "final_strength": 72,
                "confidence": 0.75,
                "key_reasons": ["财报超预期", "AI芯片需求强劲"],
                "risk_factors": ["VIX升高"],
            }
        }

        debate_summary = {
            "core_thesis": "NVDA short-term bullish",
            "confidence": 0.70,
        }

        result = synth.synthesize(battle_plan, voting_results, debate_summary)

        # 验证输出结构
        assert "date" in result
        assert "recommendations" in result
        assert "summary" in result
        assert "sector_allocation" in result
        assert "action_items" in result
        assert len(result["recommendations"]) >= 1

        rec = result["recommendations"][0]
        assert rec["symbol"] == "NVDA"
        assert rec["action"] in ("BUY", "SELL")
        assert rec["confidence_level"] in ("HIGH", "MEDIUM", "LOW")
        assert "risk_reward" in rec

    def test_synthesize_empty_inputs(self):
        """测试空输入容错"""
        from app.recommendations.synthesizer import RecommendationSynthesizer

        synth = RecommendationSynthesizer()
        result = synth.synthesize({}, {}, {})

        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_confidence_levels(self):
        """测试置信度分级"""
        from app.recommendations.synthesizer import RecommendationSynthesizer

        synth = RecommendationSynthesizer()

        # HIGH confidence
        plan = {
            "targets": [
                {
                    "symbol": "AAPL",
                    "direction": "LONG",
                    "entry_zone": {"low": 180, "high": 185},
                    "stop_loss": 175,
                    "take_profit": 195,
                }
            ],
            "restricted": [],
        }
        voting = {
            "AAPL": {
                "confidence": 0.85,
                "final_strength": 80,
                "key_reasons": ["Strong momentum"],
            }
        }
        debate = {"confidence": 0.80}

        result = synth.synthesize(plan, voting, debate)
        if result["recommendations"]:
            assert result["recommendations"][0]["confidence_level"] == "HIGH"


class TestPriceCalibrator:
    """测试价格校准器"""

    def test_calibrate_with_market_data(self):
        """测试带行情数据的校准"""
        from app.recommendations.price_calibrator import PriceCalibrator

        calibrator = PriceCalibrator(config={"enabled": True})

        recommendations = {
            "recommendations": [
                {"symbol": "NVDA", "entry_zone": "145.00-147.00", "action": "BUY"}
            ]
        }

        market_data = {"NVDA": {"price": 146.5, "change_pct": 1.2}}

        result = calibrator.calibrate(recommendations, market_data)
        assert isinstance(result, dict)

    def test_calibrate_missing_price(self):
        """测试行情缺失时的容错"""
        from app.recommendations.price_calibrator import PriceCalibrator

        calibrator = PriceCalibrator(config={"enabled": True})

        recommendations = {
            "recommendations": [
                {
                    "symbol": "UNKNOWN_TICKER",
                    "entry_zone": "100.00-102.00",
                    "action": "BUY",
                }
            ]
        }

        # 不提供 market_data，且 router 获取会失败
        result = calibrator.calibrate(recommendations, market_data={})
        # 不应抛异常
        assert result is not None

    def test_calibrate_disabled(self):
        """测试禁用状态"""
        from app.recommendations.price_calibrator import PriceCalibrator

        calibrator = PriceCalibrator(config={"enabled": False})
        recommendations = {
            "recommendations": [
                {"symbol": "AAPL", "entry_zone": "180.00-185.00", "action": "BUY"}
            ]
        }
        result = calibrator.calibrate(recommendations)
        # 禁用时应原样返回
        assert result == recommendations


class TestPushTrigger:
    """测试推送触发器"""

    def test_format_recommendation_card(self):
        """测试推荐卡片格式化"""
        from app.recommendations.push_trigger import PremarketPushTrigger

        trigger = PremarketPushTrigger()

        # 使用 push_trigger 实际期望的 'tickers' 字段
        recommendations = {
            "date": "2026-05-28",
            "market_tone": "BULLISH",
            "vix": 18.5,
            "tickers": [
                {
                    "rank": 1,
                    "symbol": "NVDA",
                    "direction": "BUY",
                    "confidence_level": "HIGH",
                    "confidence_score": 0.75,
                    "zh_thesis": "财报超预期",
                    "entry_zone": "145.50-147.00",
                    "target": "155.00",
                    "stop_loss": "140.00",
                    "risk_reward": 2.0,
                    "catalyst": "Earnings Beat",
                }
            ],
            "sector_allocation": {"Technology": 0.45},
            "avoid": [{"symbol": "LCID", "reason": "风险过高"}],
        }

        card = trigger.format_recommendation_card(recommendations)
        assert "NVDA" in card
        assert "BUY" in card or "▲" in card
        assert len(card) > 50

    def test_format_accuracy_card(self):
        """测试准确率卡片格式化"""
        from app.recommendations.push_trigger import PremarketPushTrigger

        trigger = PremarketPushTrigger()

        report = {
            "date": "2026-05-28",
            "today_correct": 5,
            "today_total": 7,
            "today_accuracy": 0.714,
            "today_avg_return": 0.018,
            "results": [
                {
                    "symbol": "NVDA",
                    "direction": "BUY",
                    "pnl_pct": 3.2,
                    "correct": True,
                    "close_price": 151.2,
                    "entry_price": 146.25,
                }
            ],
            "best": {"symbol": "NVDA", "pnl_pct": 3.2},
            "worst": {"symbol": "TSLA", "pnl_pct": -1.5},
            "insight": "命中率 71%，表现优异",
        }

        card = trigger.format_accuracy_card(report)
        assert "71" in card
        assert "NVDA" in card


class TestAccuracyTracker:
    """测试准确率追踪器"""

    def test_evaluate_no_data(self):
        """测试无数据时的容错"""
        from app.recommendations.accuracy_tracker import RecommendationAccuracyTracker

        tracker = RecommendationAccuracyTracker()
        result = tracker.evaluate_daily("2020-01-01")  # 不存在的日期

        # 不应抛异常，应返回空报告
        assert result is not None
        assert result["today_total"] == 0
        assert result["results"] == []

    def test_evaluate_with_mock_data(self, tmp_path):
        """测试带模拟数据的评估"""
        from app.recommendations.accuracy_tracker import RecommendationAccuracyTracker

        # 准备测试数据
        daily_file = tmp_path / "daily_2026-05-27.json"
        daily_file.write_text(
            json.dumps(
                {
                    "date": "2026-05-27",
                    "recommendations": [
                        {
                            "symbol": "NVDA",
                            "action": "BUY",
                            "entry_zone": "145.00-147.00",
                        },
                        {
                            "symbol": "AAPL",
                            "action": "BUY",
                            "entry_zone": {"low": 180, "high": 185},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Mock router
        mock_router = MagicMock()
        mock_router.get_quote.side_effect = lambda sym: {
            "NVDA": {"price": 150.0, "close": 150.0},
            "AAPL": {"price": 178.0, "close": 178.0},
        }.get(sym)

        tracker = RecommendationAccuracyTracker(
            data_dir=str(tmp_path), router=mock_router
        )
        result = tracker.evaluate_daily("2026-05-27")

        assert result["today_total"] == 2
        # NVDA: entry mid=146, close=150 → correct
        # AAPL: entry mid=182.5, close=178 → incorrect (BUY but went down)
        assert result["today_correct"] == 1
        assert len(result["results"]) == 2
