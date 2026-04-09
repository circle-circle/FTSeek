"""
ActionableInsight - 可执行建议生成模块

功能:
- 标准化交易建议格式
- 入场/止损/目标价格
- 胜率/盈亏比表达
- 无模糊语言验证
"""
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.v4.actionable_insight")
logger.propagate = False  # 防止日志重复


class InsightQuality(str, Enum):
    """建议质量"""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class TradeRecommendation:
    """交易推荐数据类"""
    code: str
    action: str  # BUY or SELL
    entry_price: float
    stop_loss: Optional[float]
    target_price: float
    confidence: float  # 0.0 - 1.0
    reason: str = ""
    quantity: Optional[int] = None
    targets: Optional[List[float]] = None  # 多目标位
    exit_strategy: Optional[str] = None  # 退出策略
    time_horizon: Optional[str] = None  # 时间周期
    market_context: Optional[str] = None  # 市场背景
    
    @property
    def risk_reward_ratio(self) -> float:
        """盈亏比"""
        if self.action == "BUY":
            reward = self.target_price - self.entry_price
            risk = self.entry_price - (self.stop_loss or self.entry_price * 0.97)
        else:
            reward = self.entry_price - self.target_price
            risk = (self.stop_loss or self.entry_price * 1.03) - self.entry_price
        
        return reward / risk if risk > 0 else 0.0
    
    @property
    def potential_profit(self) -> float:
        """潜在盈利"""
        if not self.quantity:
            return 0.0
        if self.action == "BUY":
            return (self.target_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.target_price) * self.quantity
    
    @property
    def potential_loss(self) -> float:
        """潜在亏损"""
        if not self.quantity or not self.stop_loss:
            return 0.0
        if self.action == "BUY":
            return (self.entry_price - self.stop_loss) * self.quantity
        else:
            return (self.stop_loss - self.entry_price) * self.quantity
    
    @property
    def stop_loss_percent(self) -> float:
        """止损幅度"""
        if not self.stop_loss:
            return 0.0
        if self.action == "BUY":
            return (self.entry_price - self.stop_loss) / self.entry_price
        else:
            return (self.stop_loss - self.entry_price) / self.entry_price
    
    @property
    def target_percent(self) -> float:
        """目标幅度"""
        if self.action == "BUY":
            return (self.target_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - self.target_price) / self.entry_price
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "action": self.action,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "confidence": self.confidence,
            "risk_reward_ratio": self.risk_reward_ratio,
            "potential_profit": self.potential_profit,
            "potential_loss": self.potential_loss,
            "stop_loss_percent": f"{self.stop_loss_percent:.1%}",
            "target_percent": f"{self.target_percent:.1%}",
            "reason": self.reason,
        }


class ActionableInsight:
    """可执行建议生成引擎"""
    
    def __init__(
        self,
        require_stop_loss: bool = True,
        require_target: bool = True,
        min_confidence: float = 0.50,
        max_length: int = 200,
        platform: str = "dingtalk"
    ):
        """
        初始化建议生成器
        
        Args:
            require_stop_loss: 必须包含止损
            require_target: 必须包含目标价
            min_confidence: 最低置信度
            max_length: 最大长度
            platform: 输出平台
        """
        self.require_stop_loss = require_stop_loss
        self.require_target = require_target
        self.min_confidence = min_confidence
        self.max_length = max_length
        self.platform = platform
        
        # 模糊词汇黑名单
        self.vague_words = [
            "可能", "也许", "大概", "或许", "考虑", "关注",
            "看看", "观察", "等待", "试试", "应该"
        ]
        
        self.logger = logging.getLogger("ftseek.v4.actionable_insight")
logger.propagate = False  # 防止日志重复
    
    def validate(self, rec: TradeRecommendation) -> Tuple[bool, List[str]]:
        """
        验证推荐
        
        Args:
            rec: 交易推荐
            
        Returns:
            (是否有效，错误列表)
        """
        errors = []
        
        # 检查止损
        if self.require_stop_loss and not rec.stop_loss:
            errors.append("缺少止损价格")
        
        # 检查目标价
        if self.require_target and not rec.target_price:
            errors.append("缺少目标价格")
        
        # 检查置信度
        if rec.confidence < self.min_confidence:
            errors.append(f"置信度低于最低要求 ({rec.confidence:.0%} < {self.min_confidence:.0%})")
        
        # 检查代码
        if not rec.code:
            errors.append("缺少股票代码")
        
        # 检查行动
        if rec.action not in ["BUY", "SELL"]:
            errors.append(f"无效的行动方向：{rec.action}")
        
        # 检查价格合理性
        if rec.action == "BUY":
            if rec.stop_loss and rec.stop_loss >= rec.entry_price:
                errors.append("买入止损应低于入场价")
            if rec.target_price <= rec.entry_price:
                errors.append("买入目标价应高于入场价")
        else:
            if rec.stop_loss and rec.stop_loss <= rec.entry_price:
                errors.append("卖出止损应高于入场价")
            if rec.target_price >= rec.entry_price:
                errors.append("卖出目标价应低于入场价")
        
        is_valid = len(errors) == 0
        self.logger.debug(f"验证结果：{is_valid}, 错误：{errors}")
        return is_valid, errors
    
    def generate(self, rec: TradeRecommendation) -> str:
        """
        生成可执行建议
        
        Args:
            rec: 交易推荐
            
        Returns:
            建议文本
        """
        # 验证
        is_valid, errors = self.validate(rec)
        if not is_valid:
            self.logger.warning(f"推荐验证失败：{errors}")
            return f"⚠️ 无效推荐：{', '.join(errors)}"
        
        # 生成标准格式
        output = self._generate_standard(rec)
        
        # 截断到最大长度
        if len(output) > self.max_length:
            output = output[:self.max_length - 3] + "..."
        
        self.logger.info(f"生成建议：{output[:50]}...")
        return output
    
    def _generate_standard(self, rec: TradeRecommendation) -> str:
        """生成标准格式建议"""
        action_text = "买入" if rec.action == "BUY" else "卖出"
        direction_icon = "🟢" if rec.action == "BUY" else "🔴"
        
        # 标题
        title = f"{direction_icon}【{action_text}】{rec.code}"
        
        # 核心交易要素
        core_lines = [
            f"入场：{rec.entry_price}",
            f"止损：{rec.stop_loss or '无'} ({rec.stop_loss_percent:.1%})",
            f"目标：{rec.target_price} (+{rec.target_percent:.1%})",
        ]
        
        # 多目标位
        if rec.targets and len(rec.targets) > 1:
            targets_text = " / ".join(f"{t}" for t in rec.targets)
            core_lines.append(f"分批目标：{targets_text}")
        
        # 盈亏比和胜率
        metrics = [
            f"盈亏比：1:{rec.risk_reward_ratio:.1f}",
            f"胜率：{rec.confidence:.0%}"
        ]
        
        # 数量信息
        if rec.quantity:
            metrics.append(f"数量：{rec.quantity}股")
            metrics.append(f"潜在盈利：{rec.potential_profit:.0f}")
            metrics.append(f"潜在亏损：{rec.potential_loss:.0f}")
        
        # 退出策略
        if rec.exit_strategy:
            metrics.append(f"退出：{rec.exit_strategy}")
        
        # 时间周期
        if rec.time_horizon:
            metrics.append(f"周期：{rec.time_horizon}")
        
        # 组装输出
        output = title + "\n"
        output += "\n".join(core_lines) + "\n"
        output += " | ".join(metrics)
        
        # 理由
        if rec.reason:
            # 清理模糊语言
            clean_reason = self._clean_vague_language(rec.reason)
            output += f"\n理由：{clean_reason}"
        
        # 市场背景
        if rec.market_context:
            output += f"\n背景：{rec.market_context}"
        
        return output
    
    def _clean_vague_language(self, text: str) -> str:
        """清理模糊语言"""
        clean_text = text
        for word in self.vague_words:
            clean_text = clean_text.replace(word, "")
        return clean_text.strip()
    
    def generate_quick(self, rec: TradeRecommendation) -> str:
        """
        生成快速版建议 (更简洁)
        
        Args:
            rec: 交易推荐
            
        Returns:
            快速版建议
        """
        action_text = "买" if rec.action == "BUY" else "卖"
        
        return (
            f"{action_text} {rec.code} @ {rec.entry_price} | "
            f"止损{rec.stop_loss} | 目标{rec.target_price} | "
            f"胜率{rec.confidence:.0%}"
        )
    
    def generate_detailed(self, rec: TradeRecommendation) -> str:
        """
        生成详细版建议
        
        Args:
            rec: 交易推荐
            
        Returns:
            详细版建议
        """
        action_text = "买入" if rec.action == "BUY" else "卖出"
        
        detailed = f"""
【交易建议】{rec.code}

方向：{action_text}
入场：{rec.entry_price}
止损：{rec.stop_loss or '无'} (-{rec.stop_loss_percent:.1%})
目标：{rec.target_price} (+{rec.target_percent:.1%})

盈亏比：1:{rec.risk_reward_ratio:.1f}
胜率：{rec.confidence:.0%} ({self._get_quality(rec.confidence)})
"""
        
        if rec.quantity:
            detailed += f"""
仓位：{rec.quantity}股
潜在盈利：+{rec.potential_profit:.0f}
潜在亏损：-{rec.potential_loss:.0f}
"""
        
        if rec.reason:
            detailed += f"\n依据：{rec.reason}"
        
        if rec.exit_strategy:
            detailed += f"\n退出策略：{rec.exit_strategy}"
        
        return detailed.strip()
    
    def _get_quality(self, confidence: float) -> str:
        """获取质量评级"""
        if confidence >= 0.80:
            return "高"
        elif confidence >= 0.60:
            return "中"
        else:
            return "低"
    
    def format_for_platform(self, text: str) -> str:
        """根据平台格式化"""
        if self.platform == "dingtalk":
            # 钉钉 markdown
            return text
        elif self.platform == "telegram":
            # Telegram markdown
            return text.replace("**", "__").replace("`", "```")
        else:
            return text
    
    def get_status(self) -> dict:
        """获取生成器状态"""
        return {
            "require_stop_loss": self.require_stop_loss,
            "require_target": self.require_target,
            "min_confidence": self.min_confidence,
            "max_length": self.max_length,
            "platform": self.platform,
            "vague_words_count": len(self.vague_words),
        }


# 全局实例 (可选)
actionable_insight: Optional[ActionableInsight] = None


def get_actionable_insight() -> ActionableInsight:
    """获取 ActionableInsight 单例"""
    global actionable_insight
    if actionable_insight is None:
        actionable_insight = ActionableInsight()
    return actionable_insight
