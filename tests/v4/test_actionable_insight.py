"""
ActionableInsight 单元测试 - 可执行建议生成模块

测试覆盖:
- 标准化交易建议格式
- 入场/止损/目标价格
- 胜率/盈亏比表达
- 无模糊语言验证
"""
import pytest
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.persona.actionable_insight import (
    ActionableInsight,
    TradeRecommendation,
    InsightQuality
)


class TestTradeRecommendation:
    """TradeRecommendation 数据类测试"""
    
    def test_recommendation_creation(self):
        """测试推荐创建"""
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破关键阻力位"
        )
        
        assert rec.code == "HK.00700"
        assert rec.action == "BUY"
        assert rec.entry_price == 100.0
        assert rec.confidence == 0.85
    
    def test_risk_reward_calculation(self):
        """测试盈亏比计算"""
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        # 盈亏比 = (110-100) / (100-97) = 10/3 = 3.33
        assert abs(rec.risk_reward_ratio - 3.33) < 0.01
    
    def test_potential_profit_loss(self):
        """测试潜在盈亏计算"""
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            quantity=100
        )
        
        assert rec.potential_profit == 1000.0  # (110-100) * 100
        assert rec.potential_loss == 300.0     # (100-97) * 100
    
    def test_to_dict(self):
        """测试转字典"""
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        rec_dict = rec.to_dict()
        
        assert rec_dict["code"] == "HK.00700"
        assert rec_dict["action"] == "BUY"
        assert "risk_reward_ratio" in rec_dict


class TestInsightQuality:
    """InsightQuality 枚举测试"""
    
    def test_quality_enum(self):
        """测试质量枚举"""
        assert InsightQuality.HIGH == "HIGH"
        assert InsightQuality.MEDIUM == "MEDIUM"
        assert InsightQuality.LOW == "LOW"


class TestActionableInsight:
    """ActionableInsight 核心功能测试"""
    
    def test_insight_initialization(self):
        """测试引擎初始化"""
        engine = ActionableInsight()
        
        assert engine.require_stop_loss == True
        assert engine.require_target == True
        assert engine.min_confidence == 0.50
    
    def test_standard_format_generation(self):
        """测试标准格式生成"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破关键阻力位，成交量放大"
        )
        
        output = engine.generate(rec)
        
        # 应包含必需字段
        assert "入场" in output or "entry" in output.lower()
        assert "止损" in output or "stop" in output.lower()
        assert "目标" in output or "target" in output.lower()
        assert "胜率" in output or "置信" in output.lower()
    
    def test_no_vague_language(self):
        """测试无模糊语言"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="技术面良好"
        )
        
        output = engine.generate(rec)
        
        # 不应包含模糊词汇
        vague_words = ["可能", "也许", "大概", "或许", "考虑", "关注"]
        for word in vague_words:
            assert word not in output, f"检测到模糊语言：{word}"
    
    def test_required_fields_validation(self):
        """测试必需字段验证"""
        engine = ActionableInsight(require_stop_loss=True, require_target=True)
        
        # 缺少止损
        rec_no_stop = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=None,
            target_price=110.0,
            confidence=0.85
        )
        
        is_valid, errors = engine.validate(rec_no_stop)
        
        assert is_valid == False
        assert any("止损" in err for err in errors)
    
    def test_complete_recommendation(self):
        """测试完整推荐验证"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破"
        )
        
        is_valid, errors = engine.validate(rec)
        
        assert is_valid == True
        assert len(errors) == 0
    
    def test_confidence_threshold(self):
        """测试置信度阈值"""
        engine = ActionableInsight(min_confidence=0.70)
        
        # 低置信度
        rec_low = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.50
        )
        
        is_valid, errors = engine.validate(rec_low)
        
        assert is_valid == False
        assert any("置信度" in err for err in errors)
    
    def test_risk_reward_display(self):
        """测试盈亏比显示"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = engine.generate(rec)
        
        # 应包含盈亏比
        assert "盈亏" in output or "1:" in output or "risk" in output.lower()
    
    def test_action_clarity(self):
        """测试行动指令清晰度"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = engine.generate(rec)
        
        # 应有明确的行动指令
        assert any(word in output for word in ["买入", "BUY", "做多", "进场"])
    
    def test_quantity_calculation(self):
        """测试数量计算"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            quantity=100
        )
        
        output = engine.generate(rec)
        
        # 应包含数量相关信息
        assert "100" in output or "股" in output or "quantity" in output.lower()
    
    def test_percentage_display(self):
        """测试百分比显示"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = engine.generate(rec)
        
        # 应包含百分比 (止损幅度/目标幅度/胜率)
        assert "%" in output or "％" in output
    
    def test_concise_output(self):
        """测试简洁输出"""
        engine = ActionableInsight(max_length=120)
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="这是一个非常长的理由用来测试输出长度限制"
        )
        
        output = engine.generate(rec)
        
        assert len(output) <= 120
    
    def test_multiple_targets(self):
        """测试多目标位支持"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            targets=[105.0, 110.0, 115.0]  # 多目标位
        )
        
        output = engine.generate(rec)
        
        # 应包含多目标位信息
        assert "105" in output or "110" in output or "115" in output
    
    def test_partial_exit_strategy(self):
        """测试部分退出策略"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            exit_strategy="50% @ 105, 50% @ 110"
        )
        
        output = engine.generate(rec)
        
        # 应包含退出策略
        assert "退出" in output or "止盈" in output or "50%" in output
    
    def test_time_horizon(self):
        """测试时间周期"""
        engine = ActionableInsight()
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            time_horizon="1-3 天"
        )
        
        output = engine.generate(rec)
        
        # 应包含时间周期
        assert "天" in output or "日" in output or "time" in output.lower()


class TestInsightFormatting:
    """格式测试"""
    
    def test_dingtalk_format(self):
        """测试钉钉格式"""
        engine = ActionableInsight(platform="dingtalk")
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = engine.generate(rec)
        
        assert output is not None
        assert len(output) > 0
    
    def test_telegram_format(self):
        """测试 Telegram 格式"""
        engine = ActionableInsight(platform="telegram")
        
        rec = TradeRecommendation(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = engine.generate(rec)
        
        assert output is not None


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
