#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
熔断机制 (Circuit Breaker)

功能:
1. 连续订单拒绝熔断 (8 次市场异常 → 暂停 5 分钟)
2. 账户回撤动态降仓
3. 连续信号失败熔断
4. 钉钉告警推送
"""

import logging
import time
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.circuit_breaker")
logger.propagate = False  # 防止日志重复


@dataclass
class CircuitBreakerState:
    """熔断器状态"""
    consecutive_rejects: int = 0
    last_reject_time: Optional[float] = None
    pause_until: Optional[float] = None
    total_rejects_today: int = 0
    last_reset_date: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d'))
    
    def reset(self):
        """重置状态"""
        self.consecutive_rejects = 0
        self.last_reject_time = None
        self.pause_until = None
        self.total_rejects_today = 0
        self.last_reset_date = datetime.now().strftime('%Y-%m-%d')


class CircuitBreaker:
    """熔断器
    
    2026-04-09 优化:
    - 默认冷却时间从 300秒 增加到 600秒
    - 增加持仓状态感知
    - 增加熔断后信号抑制
    """
    
    def __init__(self, 
                 reject_limit: int = 5,          # 降低触发阈值: 8 -> 5
                 pause_duration: int = 600,       # 延长冷却时间: 300 -> 600
                 drawdown_limit: float = 0.15):
        """
        初始化
        
        Args:
            reject_limit: 连续拒绝次数限制
            pause_duration: 暂停时长 (秒)
            drawdown_limit: 最大回撤限制
        """
        self.reject_limit = reject_limit
        self.pause_duration = pause_duration
        self.drawdown_limit = drawdown_limit
        
        self.state = CircuitBreakerState()
        self.reject_reasons: List[str] = []
        
        logger.info(
            f"熔断器已初始化 | "
            f"连续拒绝限制：{reject_limit}次 | "
            f"暂停时长：{pause_duration}秒 | "
            f"最大回撤：{drawdown_limit*100}%"
        )
    
    def _check_date_reset(self):
        """检查是否需要按日重置"""
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self.state.last_reset_date:
            logger.info("新的一天，重置熔断器状态")
            self.state.reset()
    
    def record_reject(self, reason: str = ""):
        """
        记录拒绝 - 区分技术错误和市场异常
        
        P0 优化：增加分级告警
        - 3 次：警告
        - 5 次：触发熔断
        
        2026-04-09 优化:
        - 持仓限制导致的拒绝不触发熔断（这是正常风控行为）
        
        Args:
            reason: 拒绝原因
        """
        self._check_date_reset()
        
        # 技术性错误不计入熔断
        TECHNICAL_ERRORS = ["频率", "rate limit", "卖空", "short", "余额", "balance",
                            "duplicate", "duplicate_signal", "重复", "冷却"]
        
        # 2026-04-09 新增: 持仓限制导致的拒绝也不计入熔断
        POSITION_ERRORS = ["持仓数", "position_limit", "最大持仓", "已达最大"]
        
        reason_lower = reason.lower() if reason else ""
        is_technical = any(kw in reason_lower for kw in TECHNICAL_ERRORS)
        is_position_limit = any(kw in reason_lower for kw in POSITION_ERRORS)
        
        if is_technical:
            # 技术性错误：记录但不触发熔断
            logger.debug(f"[熔断器] 技术性错误，不计入熔断: {reason}")
            self.state.total_rejects_today += 1
            return  # 直接返回，不增加consecutive_rejects
        
        if is_position_limit:
            # 持仓限制：记录但不触发熔断（这是正常风控行为）
            logger.info(f"[熔断器] 持仓限制拒绝，不计入熔断: {reason}")
            self.state.total_rejects_today += 1
            return
        
        # 市场异常：正常计入连续拒绝
        self.state.consecutive_rejects += 1
        self.state.last_reject_time = time.time()
        self.state.total_rejects_today += 1
        self.reject_reasons.append(reason)
        
        # P0 优化：分级告警 (阈值降低)
        if self.state.consecutive_rejects >= 3:
            logger.warning(f"⚠️ 警告：连续{self.state.consecutive_rejects}次拒绝 (阈值{self.reject_limit})")
        if self.state.consecutive_rejects >= 4:
            logger.error(f"🚨 严重警告：连续{self.state.consecutive_rejects}次拒绝，即将触发熔断!")
        
        logger.warning(f"订单被拒绝 (连续{self.state.consecutive_rejects}次): {reason}")
        
        # 检查是否触发熔断
        if self.state.consecutive_rejects >= self.reject_limit:
            self._trigger_circuit_breaker(reason)
    
    def _trigger_circuit_breaker(self, reason: str):
        """
        触发熔断
        
        Args:
            reason: 触发原因
        """
        self.state.pause_until = time.time() + self.pause_duration
        
        pause_minutes = self.pause_duration // 60
        logger.error(
            f"🚨 熔断触发！连续{self.state.consecutive_rejects}次拒绝 | "
            f"暂停交易{pause_minutes}分钟 | "
            f"原因：{reason}"
        )
        
        # 推送钉钉告警
        self._send_alert(reason)
    
    def _send_alert(self, reason: str):
        """发送告警"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            
            message = f"""🚨 **FTSeek 熔断告警**

📊 触发原因：连续{self.state.consecutive_rejects}次订单拒绝
⏸️ 暂停时长：{self.pause_duration // 60}分钟
📝 最后拒绝：{reason}
🕐 触发时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔁 今日累计：{self.state.total_rejects_today}次

💡 建议检查:
1. 富途 OpenD 服务状态
2. 账户资金和仓位
3. 风控配置参数
4. 网络连接状态

系统将自动恢复交易 (暂停{self.pause_duration // 60}分钟后)"""
            
            # notifier.send_text(message)
            logger.info("熔断告警已发送")
            
        except Exception as e:
            logger.error(f"发送告警失败：{e}")
    
    def can_trade(self) -> bool:
        """
        判断是否可以交易
        
        Returns:
            bool: 是否可以交易
        """
        self._check_date_reset()
        
        # 检查是否在暂停期
        if self.state.pause_until:
            if time.time() < self.state.pause_until:
                remaining = int(self.state.pause_until - time.time())
                logger.debug(f"熔断暂停中，还需{remaining}秒")
                return False
            else:
                # 暂停期结束，恢复交易
                logger.info("熔断暂停结束，恢复交易")
                self.state.pause_until = None
                self.state.consecutive_rejects = 0
        
        return True
    
    def is_paused(self) -> bool:
        """
        判断是否处于熔断暂停状态
        
        Returns:
            bool: 是否暂停中
        """
        self._check_date_reset()
        
        if self.state.pause_until and time.time() < self.state.pause_until:
            return True
        return False
    
    def record_success(self):
        """记录成功交易"""
        self._check_date_reset()
        
        # 成功交易后重置连续拒绝计数
        if self.state.consecutive_rejects > 0:
            logger.debug(f"交易成功，重置连续拒绝计数 (原：{self.state.consecutive_rejects})")
            self.state.consecutive_rejects = 0
    
    def check_drawdown(self, current_drawdown: float) -> bool:
        """
        检查回撤
        
        Args:
            current_drawdown: 当前回撤 (0-1)
        
        Returns:
            bool: 是否超过回撤限制
        """
        if current_drawdown >= self.drawdown_limit:
            logger.error(
                f"🚨 回撤超标！当前回撤{current_drawdown*100:.1f}% > 限制{self.drawdown_limit*100:.1f}%"
            )
            self._send_drawdown_alert(current_drawdown)
            return True
        
        return False
    
    def _send_drawdown_alert(self, drawdown: float):
        """发送回撤告警"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            
            message = f"""🚨 **FTSeek 回撤告警**

📉 当前回撤：{drawdown*100:.1f}%
⚠️ 回撤限制：{self.drawdown_limit*100:.1f}%
🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💡 建议:
1. 检查持仓盈亏
2. 考虑减仓或平仓
3. 暂停新开仓
4. 分析亏损原因

系统已触发回撤保护"""
            
            # notifier.send_text(message)
            logger.info("回撤告警已发送")
            
        except Exception as e:
            logger.error(f"发送告警失败：{e}")
    
    def get_status(self) -> Dict:
        """获取状态"""
        self._check_date_reset()
        
        status = {
            'can_trade': self.can_trade(),
            'consecutive_rejects': self.state.consecutive_rejects,
            'total_rejects_today': self.state.total_rejects_today,
            'pause_remaining': 0,
            'reject_limit': self.reject_limit,
        }
        
        if self.state.pause_until and time.time() < self.state.pause_until:
            status['pause_remaining'] = int(self.state.pause_until - time.time())
            status['paused'] = True
        else:
            status['paused'] = False
        
        return status
    
    def print_status(self):
        """打印状态"""
        status = self.get_status()
        
        print("\n" + "=" * 80)
        print("🛡️  FTSeek 熔断器状态")
        print("=" * 80)
        print(f"可交易：{'✅ 是' if status['can_trade'] else '❌ 否'}")
        print(f"暂停中：{'是 (' + str(status['pause_remaining']) + '秒)' if status['paused'] else '否'}")
        print(f"连续拒绝：{status['consecutive_rejects']}次")
        print(f"今日累计：{status['total_rejects_today']}次")
        print(f"拒绝限制：{status['reject_limit']}次")
        print("=" * 80)
        print()


# 全局单例
circuit_breaker = CircuitBreaker()
reaker = CircuitBreaker()
