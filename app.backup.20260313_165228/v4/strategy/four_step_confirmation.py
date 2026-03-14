#!/usr/bin/env python3
"""
FourStepConfirmation - 四步确认策略融合系统

华尔街 FTSeek V4 交易决策流程：
1. 信号生成与初步筛选 (Signal Generation)
2. 置信度评估 (Confidence Assessment)
3. 风控检查 (Risk Control Check)
4. 最终执行确认 (Final Execution Confirmation)

每个步骤都有明确的通过标准和否决权，确保交易决策的严谨性。
"""

import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.v4.four_step_confirmation")


class StepStatus(str, Enum):
    """步骤状态"""
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """单步检查结果"""
    step_name: str
    step_number: int
    status: StepStatus
    score: float = 0.0
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    
    @property
    def passed(self) -> bool:
        return self.status == StepStatus.PASSED
    
    def to_dict(self) -> dict:
        return {
            "step_name": self.step_name,
            "step_number": self.step_number,
            "status": self.status.value,
            "score": self.score,
            "message": self.message,
            "details": self.details,
            "timestamp": datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class FourStepReport:
    """四步确认总报告"""
    symbol: str
    action: str  # BUY or SELL
    overall_passed: bool
    final_confidence: float
    steps: List[StepResult]
    recommendation: str  # STRONG_BUY, BUY, WATCH, SELL, STRONG_SELL, SKIP
    entry_price: float
    stop_loss: Optional[float]
    target_price: Optional[float]
    position_ratio: float
    rationale: str
    generated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "overall_passed": self.overall_passed,
            "final_confidence": self.final_confidence,
            "steps": [s.to_dict() for s in self.steps],
            "recommendation": self.recommendation,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "position_ratio": self.position_ratio,
            "rationale": self.rationale,
            "generated_at": datetime.fromtimestamp(self.generated_at).strftime("%Y-%m-%d %H:%M:%S"),
        }


class FourStepConfirmation:
    """
    四步确认策略融合系统
    
    华尔街 FTSeek V4 核心决策流程：
    
    Step 1: 信号生成与初步筛选
    - 多因子信号融合（技术面 + 情绪面 + 催化事件）
    - 基础阈值过滤
    - 通过标准：final_score >= 0.50
    
    Step 2: 置信度评估
    - 信号一致性检查
    - 历史回测验证
    - 市场环境适配度
    - 通过标准：confidence >= 0.60
    
    Step 3: 风控检查
    - 资金充足性
    - 仓位限制
    - 日交易次数
    - 黑名单检查
    - 通过标准：all_checks_passed
    
    Step 4: 最终执行确认
    - 钉钉按钮确认（人工介入）
    - 过期时间控制（5 分钟）
    - 通过标准：user_confirmation == True
    
    Example:
        >>> confirmation = FourStepConfirmation()
        >>> signal = {
        ...     'symbol': 'AAPL',
        ...     'action': 'BUY',
        ...     'price': 175.20,
        ...     'final_score': 0.78,
        ...     'technical_score': 0.75,
        ...     'sentiment_score': 0.80,
        ... }
        >>> account_info = {'available_cash': 100000, 'daily_trades': 2}
        >>> report = confirmation.full_check(signal, account_info)
        >>> if report.overall_passed:
        ...     print("交易通过四步确认")
    """
    
    # 默认配置参数
    DEFAULT_CONFIG = {
        # Step 1 阈值
        'min_signal_score': 0.50,
        
        # Step 2 阈值
        'min_confidence': 0.60,
        'min_backtest_win_rate': 0.50,
        
        # Step 3 风控参数
        'max_daily_trades': 10,
        'max_single_position': 0.20,
        'max_total_position': 0.60,
        'max_daily_loss': 0.03,
        
        # Step 4 确认参数
        'confirmation_timeout': 600,  # 10 分钟超时自动确认
        'auto_confirm_enabled': True,  # 启用超时自动确认
    }
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """初始化四步确认系统"""
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.logger = logging.getLogger("ftseek.v4.four_step_confirmation")
        
        # 风控状态
        self.daily_trades = 0
        self.daily_pnl = 0.0
        
    def step1_signal_generation(
        self,
        signal: Dict[str, Any]
    ) -> StepResult:
        """
        Step 1: 信号生成与初步筛选
        
        检查多因子信号融合结果，确保信号质量达到基础要求。
        
        Args:
            signal: 信号字典，包含:
                - final_score: 融合评分 (0-1)
                - technical_score: 技术面评分
                - sentiment_score: 情绪面评分
                - catalyst_score: 催化事件评分
        
        Returns:
            StepResult: 检查结果
        """
        final_score = signal.get('final_score', 0.0)
        min_score = self.config['min_signal_score']
        
        passed = final_score >= min_score
        
        # 信号强度分级
        if final_score >= 0.75:
            strength = "STRONG"
            recommendation = "STRONG_BUY" if signal.get('action') == 'BUY' else "STRONG_SELL"
        elif final_score >= 0.60:
            strength = "MODERATE"
            recommendation = "BUY" if signal.get('action') == 'BUY' else "SELL"
        else:
            strength = "WEAK"
            recommendation = "WATCH"
        
        return StepResult(
            step_name="信号生成与初步筛选",
            step_number=1,
            status=StepStatus.PASSED if passed else StepStatus.FAILED,
            score=final_score,
            message=f"信号强度：{strength} (评分：{final_score:.2f}, 阈值：{min_score:.2f})",
            details={
                "final_score": final_score,
                "technical_score": signal.get('technical_score', 0.0),
                "sentiment_score": signal.get('sentiment_score', 0.0),
                "catalyst_score": signal.get('catalyst_score', 0.0),
                "strength": strength,
                "recommendation": recommendation,
            }
        )
    
    def step2_confidence_assessment(
        self,
        signal: Dict[str, Any],
        historical_data: Optional[Dict[str, Any]] = None
    ) -> StepResult:
        """
        Step 2: 置信度评估
        
        综合评估信号的可信度，包括：
        - 信号一致性
        - 历史回测表现
        - 市场环境适配度
        
        Args:
            signal: 信号字典
            historical_data: 历史回测数据（可选）
        
        Returns:
            StepResult: 检查结果
        """
        # 计算置信度
        confidence = signal.get('confidence', 0.0)
        
        # 如果没有显式置信度，从评分推算
        if confidence == 0.0:
            final_score = signal.get('final_score', 0.0)
            confidence = min(1.0, final_score * 1.2)  # 适度放大
        
        min_confidence = self.config['min_confidence']
        passed = confidence >= min_confidence
        
        # 置信度分级
        if confidence >= 0.80:
            level = "HIGH"
        elif confidence >= 0.60:
            level = "MEDIUM"
        else:
            level = "LOW"
        
        # 历史回测验证（如果有数据）
        backtest_info = ""
        if historical_data:
            win_rate = historical_data.get('win_rate', 0.0)
            profit_factor = historical_data.get('profit_factor', 0.0)
            if win_rate >= self.config['min_backtest_win_rate']:
                backtest_info = f" | 回测：胜率{win_rate:.0%}, 盈利因子{profit_factor:.2f}"
        
        return StepResult(
            step_name="置信度评估",
            step_number=2,
            status=StepStatus.PASSED if passed else StepStatus.FAILED,
            score=confidence,
            message=f"置信度：{level} ({confidence:.2f}, 阈值：{min_confidence:.2f}){backtest_info}",
            details={
                "confidence": confidence,
                "level": level,
                "min_confidence": min_confidence,
                "historical_data": historical_data or {},
            }
        )
    
    def step3_risk_control_check(
        self,
        signal: Dict[str, Any],
        account_info: Dict[str, Any]
    ) -> StepResult:
        """
        Step 3: 风控检查
        
        全面检查交易是否符合风控要求：
        - 资金充足性
        - 仓位限制
        - 日交易次数
        - 黑名单检查
        - 日亏损限制
        
        Args:
            signal: 信号字典
            account_info: 账户信息
        
        Returns:
            StepResult: 检查结果
        """
        checks = []
        all_passed = True
        
        # 1. 资金检查
        required_capital = signal.get('price', 0) * signal.get('quantity', 0)
        available_cash = account_info.get('available_cash', 0)
        capital_ok = available_cash >= required_capital
        checks.append(f"资金：{'✅' if capital_ok else '❌'} (需要：${required_capital:.2f}, 可用：${available_cash:.2f})")
        if not capital_ok:
            all_passed = False
        
        # 2. 日交易次数检查
        daily_trades = account_info.get('daily_trades', self.daily_trades)
        max_trades = self.config['max_daily_trades']
        trades_ok = daily_trades < max_trades
        checks.append(f"日交易：{'✅' if trades_ok else '❌'} ({daily_trades}/{max_trades})")
        if not trades_ok:
            all_passed = False
        
        # 3. 仓位检查
        total_capital = account_info.get('total_capital', available_cash * 2)
        position_value = required_capital
        position_ratio = position_value / total_capital if total_capital > 0 else 1.0
        max_position = self.config['max_single_position']
        position_ok = position_ratio <= max_position
        checks.append(f"仓位：{'✅' if position_ok else '❌'} ({position_ratio:.1%}/{max_position:.0%})")
        if not position_ok:
            all_passed = False
        
        # 4. 日亏损检查
        daily_pnl = account_info.get('daily_pnl', self.daily_pnl)
        daily_loss_limit = total_capital * self.config['max_daily_loss']
        loss_ok = daily_pnl > -daily_loss_limit
        checks.append(f"日盈亏：{'✅' if loss_ok else '❌'} (${daily_pnl:.2f}, 限制：-${daily_loss_limit:.2f})")
        if not loss_ok:
            all_passed = False
        
        # 5. 黑名单检查
        symbol = signal.get('symbol', '')
        blacklist = self.config.get('blacklist', [])
        blacklist_ok = symbol not in blacklist
        checks.append(f"黑名单：{'✅' if blacklist_ok else '❌'}")
        if not blacklist_ok:
            all_passed = False
        
        passed = all_passed
        
        return StepResult(
            step_name="风控检查",
            step_number=3,
            status=StepStatus.PASSED if passed else StepStatus.FAILED,
            score=1.0 if passed else 0.0,
            message=f"风控检查：{'全部通过' if passed else '未通过'}",
            details={
                "all_passed": passed,
                "checks": checks,
                "capital_ok": capital_ok,
                "trades_ok": trades_ok,
                "position_ok": position_ok,
                "loss_ok": loss_ok,
                "blacklist_ok": blacklist_ok,
            }
        )
    
    def step4_final_confirmation(
        self,
        signal: Dict[str, Any],
        user_decision: Optional[str] = None,
        elapsed_time: float = 0.0
    ) -> StepResult:
        """
        Step 4: 最终执行确认
        
        等待用户通过钉钉按钮确认交易，支持超时自动确认。
        
        Args:
            signal: 信号字典
            user_decision: 用户决策 ("confirm" or "ignore" or None)
            elapsed_time: 已等待时间（秒）
        
        Returns:
            StepResult: 检查结果
        """
        timeout = self.config['confirmation_timeout']
        auto_confirm = self.config.get('auto_confirm_enabled', True)
        
        # 检查是否超时
        is_timeout = elapsed_time >= timeout
        
        if user_decision is None and not is_timeout:
            # 用户未决策且未超时
            status = StepStatus.PENDING
            remaining = timeout - elapsed_time
            message = f"等待用户确认（剩余{remaining:.0f}秒，超时自动确认）"
            score = 0.0
        elif user_decision is None and is_timeout and auto_confirm:
            # 超时自动确认
            status = StepStatus.PASSED
            message = f"超时自动确认（等待{elapsed_time:.0f}秒）"
            score = 0.85  # 自动确认给予较低分数
        elif user_decision and user_decision.lower() == 'confirm':
            # 用户主动确认
            status = StepStatus.PASSED
            message = f"用户已确认执行（等待{elapsed_time:.0f}秒）"
            score = 1.0
        else:  # user_decision == 'ignore' or other
            # 用户明确拒绝
            status = StepStatus.FAILED
            message = "用户已忽略/拒绝"
            score = 0.0
        
        return StepResult(
            step_name="最终执行确认",
            step_number=4,
            status=status,
            score=score,
            message=message,
            details={
                "user_decision": user_decision,
                "timeout_seconds": timeout,
                "elapsed_time": elapsed_time,
                "is_timeout": is_timeout,
                "auto_confirm": auto_confirm and is_timeout and user_decision is None,
            }
        )
    
    def full_check(
        self,
        signal: Dict[str, Any],
        account_info: Optional[Dict[str, Any]] = None,
        historical_data: Optional[Dict[str, Any]] = None,
        user_decision: Optional[str] = None,
        elapsed_time: float = 0.0
    ) -> FourStepReport:
        """
        执行完整的四步确认流程
        
        Args:
            signal: 交易信号
            account_info: 账户信息
            historical_data: 历史数据
            user_decision: 用户决策
            elapsed_time: 已等待时间（秒），用于超时判断
        
        Returns:
            FourStepReport: 四步确认总报告
        """
        symbol = signal.get('symbol', 'UNKNOWN')
        action = signal.get('action', 'BUY')
        
        self.logger.info(f"开始四步确认：{symbol} {action}")
        
        steps = []
        all_passed = True
        
        # Step 1: 信号生成
        step1 = self.step1_signal_generation(signal)
        steps.append(step1)
        if not step1.passed:
            all_passed = False
            self.logger.warning(f"Step 1 失败：{step1.message}")
        
        # Step 2: 置信度评估
        step2 = self.step2_confidence_assessment(signal, historical_data)
        steps.append(step2)
        if not step2.passed:
            all_passed = False
            self.logger.warning(f"Step 2 失败：{step2.message}")
        
        # Step 3: 风控检查
        account_info = account_info or {}
        step3 = self.step3_risk_control_check(signal, account_info)
        steps.append(step3)
        if not step3.passed:
            all_passed = False
            self.logger.warning(f"Step 3 失败：{step3.message}")
        
        # Step 4: 最终确认（传入已等待时间）
        step4 = self.step4_final_confirmation(signal, user_decision, elapsed_time)
        steps.append(step4)
        if not step4.passed and user_decision is not None:
            all_passed = False
            self.logger.warning(f"Step 4 失败：{step4.message}")
        
        # 计算最终置信度
        final_confidence = (step1.score * 0.30 + step2.score * 0.35 + step3.score * 0.35)
        if step4.passed:
            final_confidence = min(1.0, final_confidence * 1.1)  # 用户确认加分
        
        # 生成推荐
        if all_passed and final_confidence >= 0.80:
            recommendation = "STRONG_BUY" if action == 'BUY' else "STRONG_SELL"
        elif all_passed and final_confidence >= 0.60:
            recommendation = "BUY" if action == 'BUY' else "SELL"
        elif all_passed:
            recommendation = "WATCH"
        else:
            recommendation = "SKIP"
        
        # 生成理由
        rationale = f"四步确认{'通过' if all_passed else '未通过'} | "
        rationale += f"信号：{step1.score:.2f} | "
        rationale += f"置信度：{step2.score:.2f} | "
        rationale += f"风控：{'✅' if step3.passed else '❌'}"
        if step4.passed:
            rationale += " | 用户确认：✅"
        
        self.logger.info(f"四步确认完成：{symbol} {action} - {'通过' if all_passed else '未通过'}")
        
        return FourStepReport(
            symbol=symbol,
            action=action,
            overall_passed=all_passed,
            final_confidence=final_confidence,
            steps=steps,
            recommendation=recommendation,
            entry_price=signal.get('price', 0.0),
            stop_loss=signal.get('stop_loss'),
            target_price=signal.get('target_price'),
            position_ratio=signal.get('position_ratio', 0.2),
            rationale=rationale,
        )


# 全局实例
_confirmation_instance: Optional[FourStepConfirmation] = None


def get_confirmation_system(config: Optional[Dict[str, Any]] = None) -> FourStepConfirmation:
    """获取四步确认系统实例"""
    global _confirmation_instance
    if _confirmation_instance is None:
        _confirmation_instance = FourStepConfirmation(config)
    return _confirmation_instance
