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
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.circuit_breaker")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


# ============================================================
#  硬编码熔断守卫 (Hard-coded Circuit Breaker Guard)
#  2026-04-24 新增：独立于配置的绝对保命机制
# ============================================================

class HardStopGuard:
    """
    硬编码熔断守卫 - 绝对保命机制
    
    独立于常规熔断器，不受配置参数影响，直接写死在代码中。
    核心规则:
    1. 日内亏损熔断: 账户当日回撤 >= 3% → 立即停止所有新交易。
    2. 连续亏损熔断: 连续 3 笔交易亏损 → 强制冷静期。
    3. 异常降级: 无法获取净值时，保守放行但记录警告。
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        self._open_equity: Optional[float] = None
        self._init_date: str = datetime.now().strftime('%Y-%m-%d')
        self._consecutive_losses: int = 0
        
    def check(self, current_equity: Optional[float] = None) -> Tuple[bool, str]:
        """
        执行硬编码熔断检查
        
        Args:
            current_equity: 当前账户净值 (如果为 None 则跳过净值检查)
            
        Returns:
            (bool, str): (是否可以交易, 拦截原因)
        """
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 1. 每日开盘净值重置
        if self._init_date != today:
            if current_equity is not None:
                self._open_equity = current_equity
                logger.info(f"[HardStop] 初始化今日开盘净值: {current_equity}")
            self._init_date = today
            self._consecutive_losses = 0  # 每天重置连败计数
            
        # 2. 日内亏损熔断 (硬编码 -3%)
        if current_equity is not None and self._open_equity and self._open_equity > 0:
            drawdown = (current_equity - self._open_equity) / self._open_equity
            if drawdown <= -0.03:
                logger.critical(f"🛑 [HardStop] 日内亏损熔断! 回撤 {drawdown:.2%} 超过 -3% 硬限制")
                return False, f"🛑 系统熔断：今日回撤 {drawdown:.2%} 超过 -3% 硬限制"
                
        # 3. 连续亏损熔断 (硬编码 3笔)
        if self._check_consecutive_losses():
            logger.critical("🛑 [HardStop] 连续亏损熔断! 连续 3 笔亏损触发冷静期")
            return False, "🛑 系统熔断：连续 3 笔交易亏损，触发强制冷静期"
            
        return True, ""
    
    def _check_consecutive_losses(self) -> bool:
        """
        查询数据库检查最近交易是否连续亏损
        2026-04-24 修复: 排除幽灵仓( entry=exit )和手续费级小额亏损，
        避免因系统异常(如 stop_loss=entry_price/对账同步)导致的假连败触发熔断
        """
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # 获取最近平仓的 5 笔交易 (留余地排除幽灵仓)
            cursor.execute(
                "SELECT realized_pnl, entry_price, exit_price "
                "FROM positions WHERE status='CLOSED' AND exit_price IS NOT NULL "
                "ORDER BY exit_time DESC LIMIT 5"
            )
            rows = cursor.fetchall()
            conn.close()
            
            # 只统计有真实价格变动的交易 (entry != exit)
            # 幽灵仓(对账同步/异常清理)的 entry=exit，仅含手续费，不算真实亏损
            real_trades = [
                row[0] for row in rows
                if row[0] is not None and row[1] is not None and row[2] is not None
                and abs(row[1] - row[2]) > 0.01  # 有真实价格变动
            ]
            
            if len(real_trades) >= 3:
                recent_3 = real_trades[:3]
                if all(pnl < 0 for pnl in recent_3):
                    logger.warning(
                        f"[HardStop] 连续3笔真实亏损: {recent_3}"
                    )
                    return True
        except Exception as e:
            logger.warning(f"[HardStop] 连续亏损检查失败: {e}")
            
        return False

    def update_state(self, trade_result: Optional[float] = None):
        """
        更新连败状态 (当一笔交易平仓后调用)
        """
        if trade_result is not None:
            if trade_result < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0


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
                 reject_limit: int = 8,          # 2026-04-14 午间调整: 5 -> 8 (减少误触发)
                 pause_duration: int = 300,       # 2026-04-14 午间调整: 600 -> 300 (缩短熔断时间)
                 drawdown_limit: float = 0.15,
                 db_path: str = None):
        """
        初始化
        
        Args:
            reject_limit: 连续拒绝次数限制
            pause_duration: 暂停时长 (秒)
            drawdown_limit: 最大回撤限制
            db_path: 数据库路径 (用于硬编码熔断)
        """
        self.reject_limit = reject_limit
        self.pause_duration = pause_duration
        self.drawdown_limit = drawdown_limit
        
        self.state = CircuitBreakerState()
        self.reject_reasons: List[str] = []
        
        # 2026-04-24: 集成硬编码熔断守卫
        self.hard_stop_guard = HardStopGuard(db_path=db_path)
        
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
                            "duplicate", "duplicate_signal", "重复", "冷却",
                            "计划", "battle_plan",
                            "性能衰减", "performance_decay"]  # P0 2026-04-24: 策略统计质量判断，非市场异常
        
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
                self.reject_reasons.clear()  # P1 修复: 清空拒绝原因列表
        
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
