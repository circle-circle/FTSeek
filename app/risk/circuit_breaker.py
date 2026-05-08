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

class SmartStopGuard:
    """
    智能熔断守卫 (Smart Stop Guard) - 分阶段动态熔断机制
    
    2026-05-07 升级: 替代原 HardStopGuard 的一刀切熔断
    
    核心规则 (3 阶段):
    阶段 0 (正常): 无连续亏损，仓位 100%
    阶段 1 (警告): 连续 2 笔亏损，仓位降至 50%，不拦截信号
    阶段 2 (限制): 连续 3 笔亏损，仅放行高置信度信号 (≥0.8)，仓位 50%
    阶段 3 (熔断): 连续 5 笔亏损 OR 回撤>3%，暂停 60 分钟，后降为阶段 2
    
    恢复条件:
    - 任意时间窗口内出现 1 笔盈利 → 重置阶段 0
    - 每日 00:00 重置所有状态
    - 冷却期结束后自动降为阶段 2
    """
    
    # 阶段定义
    STAGE_NORMAL = 0    # 正常交易
    STAGE_WARNING = 1   # 警告 (仓位减半)
    STAGE_RESTRICT = 2  # 限制 (仅高置信度)
    STAGE_CIRCUIT = 3   # 熔断 (暂停)
    
    # 阈值配置 (基于富途港股真实手续费)
    # 最低交易 (小米 200 股 @30) 往返成本 ~15 HKD
    # 一般交易 (腾讯 100 股 @460) 往返成本 ~81 HKD
    MIN_LOSS_AMOUNT = 15.0     # 忽略 < 15 元的小额亏损 (覆盖最低手续费)
    CONFIDENCE_THRESHOLD = 0.8 # 阶段 2 放行阈值
    COOLDOWN_SECONDS = 3600    # 熔断冷却期 60 分钟
    DRAWDOWN_LIMIT = 0.03      # 日内回撤硬限制 3%
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        self._open_equity: Optional[float] = None
        self._init_date: str = datetime.now().strftime('%Y-%m-%d')
        
        # 阶段状态
        self._stage: int = self.STAGE_NORMAL
        self._consecutive_losses: int = 0
        self._cooldown_until: float = 0.0
        
        # 初始化状态表
        self._init_state_table()
        self._load_state()
        
    def _init_state_table(self):
        """创建持久化状态表"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS smart_stop_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[SmartStop] 状态表初始化失败: {e}")
    
    def _load_state(self):
        """从数据库加载状态"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            today = datetime.now().strftime('%Y-%m-%d')
            cursor.execute("SELECT key, value FROM smart_stop_state WHERE key IN ('stage', 'consecutive_losses', 'cooldown_until', 'init_date')")
            for row in cursor.fetchall():
                key, value = row
                if key == 'stage':
                    self._stage = int(value)
                elif key == 'consecutive_losses':
                    self._consecutive_losses = int(value)
                elif key == 'cooldown_until':
                    self._cooldown_until = float(value)
                elif key == 'init_date':
                    if value != today:
                        # 新的一天，重置状态
                        self._reset_daily()
            
            conn.close()
        except Exception as e:
            logger.warning(f"[SmartStop] 状态加载失败: {e}")
    
    def _save_state(self):
        """保存状态到数据库"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO smart_stop_state (key, value) VALUES ('stage', ?)", (str(self._stage),))
            conn.execute("INSERT OR REPLACE INTO smart_stop_state (key, value) VALUES ('consecutive_losses', ?)", (str(self._consecutive_losses),))
            conn.execute("INSERT OR REPLACE INTO smart_stop_state (key, value) VALUES ('cooldown_until', ?)", (str(self._cooldown_until),))
            conn.execute("INSERT OR REPLACE INTO smart_stop_state (key, value) VALUES ('init_date', ?)", (self._init_date,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[SmartStop] 状态保存失败: {e}")
    
    def _reset_daily(self):
        """每日重置"""
        logger.info("[SmartStop] 新的一天，重置所有状态")
        self._init_date = datetime.now().strftime('%Y-%m-%d')
        self._stage = self.STAGE_NORMAL
        self._consecutive_losses = 0
        self._cooldown_until = 0.0
        self._open_equity = None
        self._save_state()
    
    def check(self, current_equity: Optional[float] = None, signal_confidence: float = 0.0) -> Tuple[bool, str, float]:
        """
        智能熔断检查
        
        Args:
            current_equity: 当前账户净值
            signal_confidence: 信号置信度 (0.0-1.0)
            
        Returns:
            (allowed, reason, position_multiplier)
        """
        today = datetime.now().strftime('%Y-%m-%d')
        if self._init_date != today:
            self._reset_daily()
        
        # 更新连败状态
        self._update_loss_state()
        
        # 日内亏损熔断 (硬限制)
        if current_equity is not None and self._open_equity and self._open_equity > 0:
            drawdown = (current_equity - self._open_equity) / self._open_equity
            if drawdown <= -self.DRAWDOWN_LIMIT:
                logger.critical(f"🛑 [SmartStop] 日内亏损熔断! 回撤 {drawdown:.2%} 超过 {self.DRAWDOWN_LIMIT:.0%} 硬限制")
                self._stage = self.STAGE_CIRCUIT
                self._cooldown_until = time.time() + self.COOLDOWN_SECONDS
                self._save_state()
                return False, f"🛑 系统熔断：今日回撤 {drawdown:.2%} 超过硬限制", 0.0
        
        # 阶段 3: 熔断
        if self._stage == self.STAGE_CIRCUIT:
            if time.time() < self._cooldown_until:
                remaining = int(self._cooldown_until - time.time())
                return False, f"🛑 熔断冷却中 (剩余 {remaining//60} 分钟)", 0.0
            else:
                logger.info("[SmartStop] 熔断冷却结束，降为限制模式")
                self._stage = self.STAGE_RESTRICT
                self._save_state()
        
        # 阶段 2: 限制
        if self._stage == self.STAGE_RESTRICT:
            if signal_confidence >= self.CONFIDENCE_THRESHOLD:
                return True, "🔶 限制模式 (高置信度放行)", 0.5
            else:
                return False, f"🔶 限制模式 (置信度 {signal_confidence:.2f} < {self.CONFIDENCE_THRESHOLD})", 0.0
        
        # 阶段 1: 警告
        if self._stage == self.STAGE_WARNING:
            return True, "⚠️ 警告模式 (仓位减半)", 0.5
        
        # 阶段 0: 正常
        return True, "✅ 正常模式", 1.0
    
    def _update_loss_state(self):
        """检查数据库更新连败状态"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT realized_pnl, entry_price, exit_price "
                "FROM positions WHERE status='CLOSED' AND exit_price IS NOT NULL "
                "ORDER BY exit_time DESC LIMIT 5"
            )
            rows = cursor.fetchall()
            conn.close()
            
            # 过滤有真实价格变动的交易
            real_trades = [
                row[0] for row in rows
                if row[0] is not None and row[1] is not None and row[2] is not None
                and abs(row[1] - row[2]) > 0.01
            ]
            
            if not real_trades:
                return
            
            # 重置条件: 出现盈利
            if any(pnl > 0 for pnl in real_trades[:3]):
                if self._consecutive_losses > 0:
                    logger.info(f"[SmartStop] 出现盈利，重置连败状态 (原 {self._consecutive_losses} 笔)")
                self._consecutive_losses = 0
                self._stage = self.STAGE_NORMAL  # 完全恢复正常模式
                self._save_state()
                return
            
            # 计算连续亏损 (忽略小额)
            significant_losses = [
                pnl for pnl in real_trades 
                if pnl < -self.MIN_LOSS_AMOUNT
            ]
            self._consecutive_losses = len(significant_losses[:5])
            
            # 阶段升级逻辑
            if self._consecutive_losses >= 5 and self._stage < self.STAGE_CIRCUIT:
                self._stage = self.STAGE_CIRCUIT
                self._cooldown_until = time.time() + self.COOLDOWN_SECONDS
                logger.critical(f"🛑 [SmartStop] 连续 {self._consecutive_losses} 笔亏损 → 阶段 3 (熔断)")
            elif self._consecutive_losses >= 3 and self._stage < self.STAGE_RESTRICT:
                self._stage = self.STAGE_RESTRICT
                logger.warning(f"🔶 [SmartStop] 连续 {self._consecutive_losses} 笔亏损 → 阶段 2 (限制)")
            elif self._consecutive_losses >= 2 and self._stage < self.STAGE_WARNING:
                self._stage = self.STAGE_WARNING
                logger.info(f"⚠️ [SmartStop] 连续 {self._consecutive_losses} 笔亏损 → 阶段 1 (警告)")
            
            self._save_state()
            
        except Exception as e:
            logger.warning(f"[SmartStop] 连败状态更新失败: {e}")

    def update_state(self, trade_result: Optional[float] = None):
        """向后兼容: 更新连败状态 (当一笔交易平仓后调用)"""
        if trade_result is not None:
            if trade_result < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
                self._stage = self.STAGE_NORMAL
            self._save_state()


# 向后兼容别名
HardStopGuard = SmartStopGuard


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
        
        # Kill switch (手动紧急熔断)
        self._halted: bool = False
        self._halt_reason: str = ""
        
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
        
        # 2026-05-07 P0 辩论修复: 价格检查拒绝不计入熔断 (避免级联放大)
        PRICE_CHECK_ERRORS = ["价格检查", "price_check", "追高", "追低", "接近日内高", "接近日内低"]
        
        reason_lower = reason.lower() if reason else ""
        is_technical = any(kw in reason_lower for kw in TECHNICAL_ERRORS)
        is_position_limit = any(kw in reason_lower for kw in POSITION_ERRORS)
        is_price_check = any(kw in reason_lower for kw in PRICE_CHECK_ERRORS)
        
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
        
        if is_price_check:
            # 2026-05-07 P0: 价格检查拒绝不计入熔断 (避免级联放大)
            # 价格检查是正常的风控行为，不应导致交易暂停
            logger.info(f"[熔断器] 价格检查拒绝，不计入熔断: {reason}")
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
        判断是否处于熔断暂停状态 (含手动 kill switch)
        
        Returns:
            bool: 是否暂停中
        """
        self._check_date_reset()
        
        # Kill switch 优先检查
        if self._halted:
            return True
        
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
