"""
VoiceEngine 单元测试 - 语气生成引擎模块

测试覆盖:
- 自信简洁语气
- 数据驱动表达
- 概率思维
- 风险意识
- 交易老手风格
"""
import pytest
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.persona.voice_engine import (
    VoiceEngine,
    MessageTone,
    SignalMessage,
    ToneStyle
)


class TestMessageTone:
    """消息语气枚举测试"""
    
    def test_tone_enum(self):
        """测试语气枚举"""
        assert MessageTone.CONFIDENT == "CONFIDENT"
        assert MessageTone.CAUTIOUS == "CAUTIOUS"
        assert MessageTone.NEUTRAL == "NEUTRAL"
        assert MessageTone.URGENT == "URGENT"


class TestToneStyle:
    """风格枚举测试"""
    
    def test_style_enum(self):
        """测试风格枚举"""
        assert ToneStyle.DIRECT == "DIRECT"
        assert ToneStyle.ANALYTICAL == "ANALYTICAL"
        assert ToneStyle.CONVERSATIONAL == "CONVERSATIONAL"


class TestSignalMessage:
    """SignalMessage 数据类测试"""
    
    def test_message_creation(self):
        """测试消息创建"""
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破关键阻力位，成交量放大"
        )
        
        assert msg.code == "HK.00700"
        assert msg.direction == "BUY"
        assert msg.entry_price == 100.0
        assert msg.confidence == 0.85
    
    def test_message_to_dict(self):
        """测试消息转字典"""
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        msg_dict = msg.to_dict()
        
        assert msg_dict["code"] == "HK.00700"
        assert msg_dict["direction"] == "BUY"
        assert msg_dict["confidence"] == 0.85


class TestVoiceEngine:
    """VoiceEngine 核心功能测试"""
    
    def test_engine_initialization(self):
        """测试引擎初始化"""
        engine = VoiceEngine()
        
        assert engine.tone == MessageTone.CONFIDENT
        assert engine.style == ToneStyle.DIRECT
        assert engine.max_length == 200
    
    def test_confident_tone_generation(self):
        """测试自信语气生成"""
        engine = VoiceEngine(tone=MessageTone.CONFIDENT)
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破关键阻力位"
        )
        
        output = engine.generate(msg)
        
        # 自信语气应包含强烈词汇
        assert any(word in output for word in ["强烈", "必须", "明确", "确定"]) or \
               any(word in output for word in ["BUY", "买入", "做多"])
    
    def test_cautious_tone_generation(self):
        """测试谨慎语气生成"""
        engine = VoiceEngine(tone=MessageTone.CAUTIOUS)
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.55,
            reason="技术面转好但量能不足"
        )
        
        output = engine.generate(msg)
        
        # 谨慎语气应包含风险提示
        assert any(word in output for word in ["注意", "风险", "谨慎", "观察", "等待"]) or \
               "止损" in output
    
    def test_data_driven_output(self):
        """测试数据驱动输出"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="RSI 超卖 + 突破 20 日均线"
        )
        
        output = engine.generate(msg)
        
        # 应包含具体数字
        has_numbers = any(char.isdigit() for char in output)
        assert has_numbers == True
        
        # 应包含价格或百分比
        assert any(word in output for word in ["%", "元", "港元", "HK$", "$"]) or \
               any(char.isdigit() for char in output)
    
    def test_no_vague_language(self):
        """测试无模糊语言"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="技术面良好"
        )
        
        output = engine.generate(msg)
        
        # 不应包含模糊词汇
        vague_words = ["可能", "也许", "大概", "或许", "考虑一下", "看看"]
        for word in vague_words:
            assert word not in output, f"检测到模糊语言：{word}"
    
    def test_risk_warning_included(self):
        """测试风险警告包含"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破形态"
        )
        
        output = engine.generate(msg)
        
        # 应包含止损信息
        assert "止损" in output or "stop" in output.lower()
    
    def test_confidence_expression(self):
        """测试置信度表达"""
        engine = VoiceEngine()
        
        # 高置信度
        msg_high = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.90,
            reason="多重信号共振"
        )
        
        # 低置信度
        msg_low = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.50,
            reason="单一信号"
        )
        
        output_high = engine.generate(msg_high)
        output_low = engine.generate(msg_low)
        
        # 高置信度应表达更强烈
        assert any(word in output_high for word in ["强烈", "高", "明确"]) or \
               "90%" in output_high or "0.9" in output_high
    
    def test_message_length_limit(self):
        """测试消息长度限制"""
        engine = VoiceEngine(max_length=150)
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="这是一个非常长的理由，用来测试消息长度限制功能是否正常工作"
        )
        
        output = engine.generate(msg)
        
        assert len(output) <= 150
    
    def test_actionable_format(self):
        """测试可执行格式"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破交易"
        )
        
        output = engine.generate(msg)
        
        # 应包含关键交易要素
        assert "入场" in output or "entry" in output.lower() or "100" in output
        assert "止损" in output or "stop" in output.lower() or "97" in output
        assert "目标" in output or "target" in output.lower() or "110" in output
    
    def test_win_rate_expression(self):
        """测试胜率表达"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.75,
            reason="高胜率形态"
        )
        
        output = engine.generate(msg)
        
        # 应包含胜率或置信度
        assert "胜率" in output or "置信" in output or "%" in output or \
               "0.7" in output or "75" in output
    
    def test_risk_reward_ratio(self):
        """测试盈亏比表达"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="良好盈亏比"
        )
        
        output = engine.generate(msg)
        
        # 应包含盈亏比信息 (10/3 ≈ 3.3:1)
        assert "盈亏" in output or "比" in output or "3:" in output or \
               "1:" in output or "risk" in output.lower()
    
    def test_short_concise_output(self):
        """测试简洁输出"""
        engine = VoiceEngine(max_length=100)
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="简洁信号"
        )
        
        output = engine.generate(msg)
        
        # 应简洁明了
        assert len(output) <= 100
        # 不应有多余修饰
        assert output.count("，") <= 3
        assert output.count("。") <= 2
    
    def test_market_context(self):
        """测试市场环境表达"""
        engine = VoiceEngine()
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破",
            market_context="恒生指数上涨 2%，科技股领涨"
        )
        
        output = engine.generate(msg)
        
        # 应包含市场背景
        assert "恒指" in output or "指数" in output or "市场" in output or \
               "科技" in output or "2%" in output
    
    def test_urgent_tone(self):
        """测试紧急语气"""
        engine = VoiceEngine(tone=MessageTone.URGENT)
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.95,
            reason="紧急突破信号"
        )
        
        output = engine.generate(msg)
        
        # 紧急语气应更简短有力
        assert len(output) <= 150
        # 可能包含紧急标记
        assert any(word in output for word in ["紧急", "立即", "快速", "!", "🚨"]) or \
               len(output.split()) <= 30


class VoiceEngineFormatting:
    """格式测试"""
    
    def test_dingtalk_format(self):
        """测试钉钉格式"""
        engine = VoiceEngine(platform="dingtalk")
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="测试"
        )
        
        output = engine.generate(msg)
        
        # 钉钉格式可能包含特殊标记
        assert output is not None
        assert len(output) > 0
    
    def test_telegram_format(self):
        """测试 Telegram 格式"""
        engine = VoiceEngine(platform="telegram")
        
        msg = SignalMessage(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="测试"
        )
        
        output = engine.generate(msg)
        
        # Telegram 格式可能包含 markdown
        assert output is not None


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
