"""
VoiceEngine - 语气生成引擎模块

功能:
- 交易老手语气生成
- 自信简洁表达
- 数据驱动输出
- 概率思维表达
- 风险意识提示
"""
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.v4.voice_engine")
logger.propagate = False  # 防止日志重复


class MessageTone(str, Enum):
    """消息语气"""
    CONFIDENT = "CONFIDENT"    # 自信
    CAUTIOUS = "CAUTIOUS"      # 谨慎
    NEUTRAL = "NEUTRAL"        # 中性
    URGENT = "URGENT"          # 紧急


class ToneStyle(str, Enum):
    """语气风格"""
    DIRECT = "DIRECT"              # 直接
    ANALYTICAL = "ANALYTICAL"      # 分析型
    CONVERSATIONAL = "CONVERSATIONAL"  # 对话型


@dataclass
class SignalMessage:
    """信号消息数据类"""
    code: str
    direction: str  # BUY or SELL
    entry_price: float
    stop_loss: float
    target_price: float
    confidence: float  # 0.0 - 1.0
    reason: str = ""
    market_context: str = ""
    
    @property
    def risk_reward_ratio(self) -> float:
        """盈亏比"""
        if self.direction == "BUY":
            reward = self.target_price - self.entry_price
            risk = self.entry_price - self.stop_loss
        else:
            reward = self.entry_price - self.target_price
            risk = self.stop_loss - self.entry_price
        
        return reward / risk if risk > 0 else 0.0
    
    @property
    def win_rate(self) -> str:
        """胜率表达"""
        if self.confidence >= 0.80:
            return "高胜率"
        elif self.confidence >= 0.60:
            return "中等胜率"
        else:
            return "低胜率"
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "confidence": self.confidence,
            "risk_reward_ratio": self.risk_reward_ratio,
            "win_rate": self.win_rate,
            "reason": self.reason,
        }


class VoiceEngine:
    """语气生成引擎"""
    
    def __init__(
        self,
        tone: MessageTone = MessageTone.CONFIDENT,
        style: ToneStyle = ToneStyle.DIRECT,
        max_length: int = 200,
        platform: str = "dingtalk"
    ):
        """
        初始化语气引擎
        
        Args:
            tone: 语气类型
            style: 语气风格
            max_length: 最大长度
            platform: 输出平台 (dingtalk/telegram)
        """
        self.tone = tone
        self.style = style
        self.max_length = max_length
        self.platform = platform
        
        # 语气词汇库
        self.confident_words = ["强烈", "明确", "确定", "必须", "果断"]
        self.cautious_words = ["注意", "谨慎", "观察", "等待", "风险"]
        self.urgent_words = ["紧急", "立即", "快速", "机会", "！"]
        
        self.logger = logging.getLogger("ftseek.v4.voice_engine")
logger.propagate = False  # 防止日志重复
    
    def generate(self, msg: SignalMessage) -> str:
        """
        生成消息
        
        Args:
            msg: 信号消息
            
        Returns:
            生成的消息文本
        """
        # 根据置信度选择语气
        if msg.confidence >= 0.80:
            tone = MessageTone.CONFIDENT
        elif msg.confidence >= 0.60:
            tone = MessageTone.NEUTRAL
        else:
            tone = MessageTone.CAUTIOUS
        
        # 生成基础消息
        if self.style == ToneStyle.DIRECT:
            output = self._generate_direct(msg, tone)
        elif self.style == ToneStyle.ANALYTICAL:
            output = self._generate_analytical(msg, tone)
        else:
            output = self._generate_conversational(msg, tone)
        
        # 添加市场环境
        if msg.market_context:
            output = self._add_market_context(output, msg.market_context)
        
        # 截断到最大长度
        if len(output) > self.max_length:
            output = output[:self.max_length - 3] + "..."
        
        self.logger.debug(f"生成消息：{output[:50]}...")
        return output
    
    def _generate_direct(self, msg: SignalMessage, tone: MessageTone) -> str:
        """生成直接风格消息"""
        direction_text = "买入" if msg.direction == "BUY" else "卖出"
        
        # 根据语气添加前缀
        if tone == MessageTone.CONFIDENT:
            prefix = f"【{self.confident_words[0]}推荐】"
        elif tone == MessageTone.CAUTIOUS:
            prefix = f"【{self.cautious_words[0]}观察】"
        elif tone == MessageTone.URGENT:
            prefix = f"【{self.urgent_words[0]}】"
        else:
            prefix = "【信号】"
        
        # 核心信息
        core = (
            f"{msg.code} {direction_text}\n"
            f"入场：{msg.entry_price} 止损：{msg.stop_loss} 目标：{msg.target_price}\n"
            f"胜率：{msg.confidence:.0%} 盈亏比：1:{msg.risk_reward_ratio:.1f}"
        )
        
        # 理由
        reason = f"理由：{msg.reason}" if msg.reason else ""
        
        output = f"{prefix}\n{core}"
        if reason:
            output += f"\n{reason}"
        
        return output
    
    def _generate_analytical(self, msg: SignalMessage, tone: MessageTone) -> str:
        """生成分析型消息"""
        direction_text = "做多" if msg.direction == "BUY" else "做空"
        
        # 分析性描述
        analysis = (
            f"{msg.code} {direction_text}机会\n"
            f"• 入场位：{msg.entry_price}\n"
            f"• 止损位：{msg.stop_loss} (-{(msg.entry_price - msg.stop_loss)/msg.entry_price:.1%})\n"
            f"• 目标位：{msg.target_price} (+{(msg.target_price - msg.entry_price)/msg.entry_price:.1%})\n"
            f"• 置信度：{msg.confidence:.0%} ({msg.win_rate})\n"
            f"• 盈亏比：1:{msg.risk_reward_ratio:.1f}"
        )
        
        if msg.reason:
            analysis += f"\n• 依据：{msg.reason}"
        
        return analysis
    
    def _generate_conversational(self, msg: SignalMessage, tone: MessageTone) -> str:
        """生成对话型消息"""
        direction_text = "做多" if msg.direction == "BUY" else "做空"
        
        # 对话式开场
        if tone == MessageTone.CONFIDENT:
            opening = f"老铁，{msg.code}有个{direction_text}机会不错"
        elif tone == MessageTone.CAUTIOUS:
            opening = f"注意，{msg.code}可以{direction_text}但要小心"
        else:
            opening = f"看看{msg.code}，{direction_text}机会"
        
        # 关键数据
        details = (
            f"入场{msg.entry_price}，止损{msg.stop_loss}，目标{msg.target_price}。"
            f"胜率{msg.confidence:.0%}，盈亏比 1:{msg.risk_reward_ratio:.1f}。"
        )
        
        output = f"{opening}。{details}"
        
        if msg.reason:
            output += f" {msg.reason}"
        
        return output
    
    def _add_market_context(self, output: str, context: str) -> str:
        """添加市场环境"""
        # 简洁版市场背景
        context_prefix = f"背景：{context}"
        
        if len(output) + len(context_prefix) + 10 <= self.max_length:
            return f"{output}\n{context_prefix}"
        return output
    
    def generate_alert(self, alert_type: str, message: str) -> str:
        """
        生成警报消息
        
        Args:
            alert_type: 警报类型 (STOP_LOSS/PROFIT_TARGET/RISK_WARNING)
            message: 警报内容
            
        Returns:
            警报消息
        """
        icons = {
            "STOP_LOSS": "⚠️",
            "PROFIT_TARGET": "✅",
            "RISK_WARNING": "🚨",
            "CIRCUIT_BREAK": "🛑"
        }
        
        icon = icons.get(alert_type, "📢")
        
        return f"{icon}【{alert_type}】\n{message}"
    
    def generate_summary(self, trades: List[Dict[str, Any]]) -> str:
        """
        生成交易总结
        
        Args:
            trades: 交易列表
            
        Returns:
            总结消息
        """
        if not trades:
            return "今日无交易"
        
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        win_rate = wins / total_trades if total_trades > 0 else 0
        
        summary = (
            f"【今日总结】\n"
            f"交易：{total_trades}笔\n"
            f"胜率：{win_rate:.0%}\n"
            f"盈亏：{total_pnl:+.2f}"
        )
        
        return summary
    
    def format_for_platform(self, text: str) -> str:
        """
        根据平台格式化
        
        Args:
            text: 原始文本
            
        Returns:
            格式化后的文本
        """
        if self.platform == "dingtalk":
            # 钉钉支持 markdown
            return text
        elif self.platform == "telegram":
            # Telegram 支持 markdown
            return text.replace("**", "__").replace("`", "```")
        else:
            return text
    
    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "tone": self.tone.value,
            "style": self.style.value,
            "max_length": self.max_length,
            "platform": self.platform,
        }


# 全局实例 (可选)
voice_engine: Optional[VoiceEngine] = None


def get_voice_engine() -> VoiceEngine:
    """获取 VoiceEngine 单例"""
    global voice_engine
    if voice_engine is None:
        voice_engine = VoiceEngine()
    return voice_engine
