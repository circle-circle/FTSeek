"""
FTSeek-US 资本安全水位系统 (Capital Safety Water Levels)

核心理念: 像水库管理一样管理交易资本，分层防御，永不破产。

水位分级 (基于账户净值相对历史高点的回撤):
┌─────────────────────────────────────────────────────────────────┐
│  水位    │ 回撤范围   │ 状态   │ 行为规则                      │
├──────────┼────────────┼────────┼───────────────────────────────┤
│ 🟢 安全  │ 0% ~ -5%   │ 正常   │ 全额仓位, 正常风险/笔          │
│ 🟡 警戒  │ -5% ~ -10% │ 谨慎   │ 仓位减半, 风险/笔减半          │
│ 🟠 警告  │ -10% ~ -20%│ 防御   │ 仅 25% 仓位, 不开新仓          │
│ 🔴 危险  │ -20% ~ -35%│ 紧急   │ 停止开仓, 逐步减仓             │
│ ⚫ 熔断  │ > -35%     │ 破产线 │ 全部平仓, 系统锁定, 人工介入   │
└─────────────────────────────────────────────────────────────────┘

作者: FTSeek Team
日期: 2026-04-16
"""

import logging
import os
import sqlite3
import json
from datetime import datetime, date
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.capital_safety")

# P3 优化 2026-04-29: 中概 ADR 集中度限制
# 中概 ADR 共享同一政策风险因子，视为单一风险敞口
CHINA_ADRS = {
    "US.BABA", "US.PDD", "US.JD", "US.LI", "US.NIO",
    "US.XPEV", "US.BIDU", "US.NTES", "US.TME", "US.BILI",
}
MAX_ADR_EXPOSURE_PCT = 0.15  # ADR 总敞口不超过组合的 15%
MAX_ADR_POSITION_COUNT = 3   # ADR 最多同时持有 3 只


class WaterLevel(Enum):
    """资本安全水位"""
    GREEN = "green"          # 安全: 回撤 0% ~ -5%
    YELLOW = "yellow"        # 警戒: 回撤 -5% ~ -10%
    ORANGE = "orange"        # 警告: 回撤 -10% ~ -20%
    RED = "red"              # 危险: 回撤 -20% ~ -35%
    BLACKOUT = "blackout"    # 熔断: 回撤 > -35%


@dataclass
class WaterLevelConfig:
    """水位配置"""
    # 回撤阈值 (负值表示从高点回撤)
    green_threshold: float = -0.05      # -5% 以内为安全
    yellow_threshold: float = -0.10     # -5% ~ -10% 警戒
    orange_threshold: float = -0.20     # -10% ~ -20% 警告
    red_threshold: float = -0.35        # -20% ~ -35% 危险, >35% 熔断
    
    # 各水位的风险系数 (乘数)
    green_risk_multiplier: float = 1.0      # 正常风险
    yellow_risk_multiplier: float = 0.5     # 风险减半
    orange_risk_multiplier: float = 0.25    # 风险 1/4
    red_risk_multiplier: float = 0.0        # 零风险 (不开新仓)
    blackout_risk_multiplier: float = 0.0   # 熔断
    
    # 各水位的仓位系数
    green_position_multiplier: float = 1.0      # 全额仓位
    yellow_position_multiplier: float = 0.5     # 半仓
    orange_position_multiplier: float = 0.25    # 1/4 仓
    red_position_multiplier: float = 0.0        # 不开新仓
    blackout_position_multiplier: float = 0.0   # 全部平仓
    
    # 单笔最大风险占账户比例 (Kelly-inspired)
    max_risk_per_trade_pct: float = 0.035   # 3.5% (P2 修复: 2%→3.5% 提高资金效率)
    max_total_risk_pct: float = 0.12        # 12% (P2 修复: 6%→12% 支持 5-6 只持仓)


@dataclass
class AccountState:
    """账户状态快照"""
    initial_capital: float = 0.0        # 初始资金
    current_equity: float = 0.0         # 当前净值 (现金 + 持仓市值)
    peak_equity: float = 0.0            # 历史最高净值
    cash: float = 0.0                   # 可用现金
    market_value: float = 0.0           # 持仓市值
    buying_power: float = 0.0           # 购买力
    drawdown_pct: float = 0.0           # 当前回撤 %
    max_drawdown_pct: float = 0.0       # 历史最大回撤 %
    water_level: WaterLevel = WaterLevel.GREEN
    last_update: str = ""
    is_locked: bool = False             # 是否被锁定 (熔断后)
    lock_reason: str = ""


class CapitalSafetySystem:
    """
    资本安全水位系统
    
    职责:
    1. 追踪账户净值和历史高点
    2. 计算当前水位等级
    3. 提供风险/仓位系数
    4. 破产保护 (熔断 + 锁定)
    5. 持久化状态到 DB
    
    P1 修复 (2026-04-21): 支持模拟盘/实盘差异化资金检查
    - 实盘 (trade_env=0): 严格执行现金检查 (防强平)
    - 模拟 (trade_env=1): 放宽现金检查 (允许负现金/保证金模式)
    """
    
    def __init__(self, db_path: str = None, initial_capital: float = None,
                 trade_env: int = 1):
        self.config = WaterLevelConfig()
        self._load_yaml_config()  # P2 修复 (2026-05-02): 从 YAML 加载水位配置
        self.trade_env = trade_env  # 0=实盘, 1=模拟
        
        # SSOT: Capital Adaptivity - 从 unified_config.yaml 读取初始资金
        if initial_capital is None:
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                initial_capital = float(cfg.get_dotted("capital.fixed_amount", 82_000))  # SSOT: YAML=82000
            except Exception:
                # 2026-06-10: 实盘模式下 log warning (pre_trade_gate 会 fail-close)
                import logging as _log
                _log.getLogger("ftseek.capital_safety").warning(
                    "[CapitalSafety] initial_capital fallback $100K — "
                    "broker 连接后将被真实值覆盖"
                )
                initial_capital = 100_000  # Fallback (临时, broker 刷新后覆盖)

        self._state = AccountState(initial_capital=initial_capital)
        from app.core.db_utils import get_main_db
        self._db_path = db_path or str(get_main_db())
        self._peak_initialized = False
        
        self._ensure_db_table()
        self._load_state()
        
        mode_label = "实盘" if trade_env == 0 else "模拟"
        logger.info(
            f"[资本安全] 初始化 ({mode_label}) | 初始资金=${initial_capital:,.0f} | "
            f"单笔最大风险={self.config.max_risk_per_trade_pct*100:.0f}% | "
            f"总风险上限={self.config.max_total_risk_pct*100:.0f}%"
        )
    
    def update_account(self, current_equity: float, cash: float = 0, 
                       market_value: float = 0, buying_power: float = 0) -> AccountState:
        """
        更新账户状态
        
        Args:
            current_equity: 当前净值 (现金 + 持仓市值)
            cash: 可用现金
            market_value: 持仓市值
            buying_power: 购买力
        
        Returns:
            更新后的账户状态
        """
        if current_equity <= 0:
            logger.error(f"[资本安全] 净值为 0 或负数: ${current_equity:,.2f}")
            self._state.water_level = WaterLevel.BLACKOUT
            self._state.is_locked = True
            self._state.lock_reason = "净值为零或负数"
            return self._state
        
        self._state.current_equity = current_equity
        self._state.cash = cash
        self._state.market_value = market_value
        self._state.buying_power = buying_power
        self._state.last_update = datetime.now().isoformat()
        
        # P0 修复 (2026-04-23): 写入 _broker_snapshot，让 risk/engine.py 的
        # 持仓同步兜底分支 (L115-131) 能真正触发。
        # 原代码仅 hasattr 检查，但全仓库无写入，导致死代码。
        self._broker_snapshot = {
            'position_value': market_value,
            'cash': cash,
            'equity': current_equity,
            'buying_power': buying_power,
            'updated_at': self._state.last_update,
        }
        
        # P1 修复 (2026-04-16): 首次更新时，用实际账户净值作为初始资金基准
        # 避免初始资金硬编码 ($100K) 与实际账户 ($977K) 不一致导致回撤计算失真
        if not self._peak_initialized:
            self._state.initial_capital = current_equity
            self._state.peak_equity = current_equity
            self._state.drawdown_pct = 0.0
            self._state.max_drawdown_pct = 0.0
            self._state.water_level = WaterLevel.GREEN
            self._state.is_locked = False
            self._state.lock_reason = ""
            self._peak_initialized = True
            logger.info(f"[资本安全] 首次同步初始资金=${current_equity:,.0f} (重置回撤/水位/锁定)")
        elif current_equity > self._state.peak_equity:
            self._state.peak_equity = current_equity
        
        # P0 修复 (2026-06-23): 删除运行时 ">5x 突变自动重置 peak" 后门.
        #   原逻辑 (2026-05-01) 在净值与 peak 差异 >5x 时自动重置 peak/drawdown/water_level,
        #   意图处理账户切换 — 但该场景已在 _load_state 的 trade_env 检测中处理 (L807-814).
        #   运行时 >5x 必然是数据源 bug (如曾发生的双路径查不同 Futu 账户), 自动重置会让
        #   真实回撤被当成"基准重置"吞掉 → 回撤 3% 熔断静默失效 (fail-dangerous).
        #   现改为只告警不重置: 宁可触发误熔断 (fail-safe), 也不静默吞掉真实回撤.
        if self._state.peak_equity > 0 and current_equity > 0:
            ratio = max(current_equity / self._state.peak_equity,
                        self._state.peak_equity / current_equity)
            if ratio > 5.0:
                logger.error(
                    f"[资本安全] 净值与峰值差异 {ratio:.1f}x 异常 — 疑似数据源错误, "
                    f"当前=${current_equity:,.0f} vs peak=${self._state.peak_equity:,.0f}, "
                    f"不重置基准 (drawdown 计算照常, 可能触发熔断)"
                )
        
        # 计算回撤
        if self._state.peak_equity > 0:
            self._state.drawdown_pct = (current_equity - self._state.peak_equity) / self._state.peak_equity
            self._state.max_drawdown_pct = min(
                self._state.max_drawdown_pct,
                self._state.drawdown_pct
            )
        
        # 判断水位
        self._state.water_level = self._determine_water_level()
        
        # P0-4 修复 (2026-05-03): 水位恢复到安全区域时自动解锁
        # 原逻辑：一旦 is_locked=True 永远无法解锁 → 系统永久死亡
        # 新逻辑：当水位恢复到 GREEN/YELLOW 且回撤低于熔断线时自动解锁
        if self._state.is_locked:
            if self._state.water_level in (WaterLevel.GREEN, WaterLevel.YELLOW):
                # 确认回撤已恢复到熔断线以下 (使用绝对值比较)
                if abs(self._state.drawdown_pct) < abs(self.config.red_threshold):
                    self._state.is_locked = False
                    old_reason = self._state.lock_reason
                    self._state.lock_reason = ""
                    logger.info(
                        f"[资本安全] 🟢 自动解锁! 水位恢复到 {self._state.water_level.value} | "
                        f"回撤 {self._state.drawdown_pct*100:.1f}% (原锁定原因: {old_reason})"
                    )
        
        # 检查是否触发熔断
        if self._state.water_level == WaterLevel.BLACKOUT and not self._state.is_locked:
            self._state.is_locked = True
            self._state.lock_reason = f"回撤 {self._state.drawdown_pct*100:.1f}% 超过熔断线 {self.config.red_threshold*100:.0f}%"
            logger.critical(
                f"[资本安全] ⚫ 熔断触发! 回撤 {self._state.drawdown_pct*100:.1f}% | "
                f"净值 ${current_equity:,.2f} → 系统锁定, 全部平仓"
            )
        
        # 持久化
        self._save_state()
        
        return self._state
    
    def can_open_position(self) -> Tuple[bool, str]:
        """
        检查是否允许开新仓
        
        P1 修复 (2026-04-21): 模拟盘/实盘差异化检查
        - 实盘: 严格现金检查 (防强平)
        - 模拟: 放宽现金检查，只检查净值和系统锁定
        
        Returns:
            (是否允许, 原因)
        """
        if self._state.is_locked:
            return False, f"系统已锁定: {self._state.lock_reason}"
        
        # P1-1 fail-safe: DB 保存失败时拦截新开仓 (状态不可信)
        if getattr(self, '_save_failed', False):
            return False, "fail-safe: 资本状态 DB 保存失败，禁止新开仓直到下次成功更新"
        
        wl = self._state.water_level
        if wl == WaterLevel.RED:
            return False, "危险水位: 禁止开新仓, 请减仓"
        if wl == WaterLevel.BLACKOUT:
            return False, "熔断: 全部平仓, 禁止一切交易"
        
        # P1 水位透明化 (2026-05-08): YELLOW/ORANGE 允许开仓但记录警告
        if wl == WaterLevel.YELLOW:
            logger.warning(
                f"[资本安全·警告] YELLOW 水位 (回撤 {self._state.drawdown_pct*100:.1f}%) | "
                f"允许开仓但仓位缩放至 50% (risk_mult=0.5, pos_mult=0.5)"
            )
        elif wl == WaterLevel.ORANGE:
            logger.warning(
                f"[资本安全·警告] ORANGE 水位 (回撤 {self._state.drawdown_pct*100:.1f}%) | "
                f"允许开仓但仓位缩放至 25% (risk_mult=0.25, pos_mult=0.25)"
            )
        
        # P1 修复 (2026-04-21): 模拟盘放宽现金检查
        if self.trade_env == 0:
            # 实盘: 严格检查，现金必须 > 初始资金 5%
            if self._state.cash < self._state.initial_capital * 0.05:
                return False, "现金不足 (低于初始资金 5%)"
        else:
            # 模拟盘: 只检查总净值是否为正，不检查现金
            # 模拟盘可以使用保证金/负现金模式，只要总资产为正即可
            if self._state.current_equity <= 0:
                return False, f"模拟盘净值为非正: ${self._state.current_equity:,.2f}"
            # 模拟盘额外检查: 如果现金过低 (但不是负数警告)
            if self._state.cash < -self._state.initial_capital * 0.5:
                # 现金低于初始资金 50% 的负数时才拦截
                logger.warning(
                    f"[资本安全·模拟] 现金极低 "
                    f"(${self._state.cash:,.0f})，但允许开仓 (模拟盘)"
                )
        
        mode_label = "模拟盘" if self.trade_env == 1 else "实盘"
        return True, f"{mode_label} | 水位 {wl.value}: 允许开仓"

    def check_adr_limits(self, symbol: str, current_positions) -> Tuple[bool, str]:
        """
        P3 优化 2026-04-29: 中概 ADR 集中度限制
        中概 ADR 共享同一个地缘政治风险因子，视为单一敞口。
        """
        if symbol not in CHINA_ADRS:
            return True, ""

        adr_positions = [p for p in current_positions
                        if hasattr(p, 'symbol') and p.symbol in CHINA_ADRS]

        # 检查 ADR 持仓数量上限
        if len(adr_positions) >= MAX_ADR_POSITION_COUNT:
            return False, (
                f"ADR 持仓已达上限 ({len(adr_positions)}/{MAX_ADR_POSITION_COUNT}): "
                + ", ".join(p.symbol for p in adr_positions)
            )

        # 检查 ADR 总敞口
        total_value = sum(
            (p.size * p.avg_entry_price) if hasattr(p, 'size') else 0
            for p in adr_positions
        )
        if self._state.current_equity > 0:
            adr_exposure = total_value / self._state.current_equity
            if adr_exposure >= MAX_ADR_EXPOSURE_PCT:
                return False, f"ADR 总敞口 {adr_exposure*100:.1f}% >= {MAX_ADR_EXPOSURE_PCT*100:.0f}%"

        return True, ""
    
    def get_risk_multiplier(self) -> float:
        """获取当前水位的风险系数"""
        multipliers = {
            WaterLevel.GREEN: self.config.green_risk_multiplier,
            WaterLevel.YELLOW: self.config.yellow_risk_multiplier,
            WaterLevel.ORANGE: self.config.orange_risk_multiplier,
            WaterLevel.RED: self.config.red_risk_multiplier,
            WaterLevel.BLACKOUT: self.config.blackout_risk_multiplier,
        }
        return multipliers.get(self._state.water_level, 0.0)
    
    def get_position_multiplier(self) -> float:
        """获取当前水位的仓位系数"""
        multipliers = {
            WaterLevel.GREEN: self.config.green_position_multiplier,
            WaterLevel.YELLOW: self.config.yellow_position_multiplier,
            WaterLevel.ORANGE: self.config.orange_position_multiplier,
            WaterLevel.RED: self.config.red_position_multiplier,
            WaterLevel.BLACKOUT: self.config.blackout_position_multiplier,
        }
        return multipliers.get(self._state.water_level, 0.0)
    
    def calc_position_size(self, entry_price: float, stop_loss_price: float,
                           max_total_position_value: float = None) -> Dict[str, Any]:
        """
        基于风险的动态仓位计算 (Kelly-inspired)
        
        公式:
          风险金额 = 账户净值 × 单笔风险% × 水位系数
          每股风险 = 入场价 - 止损价
          仓位股数 = 风险金额 / 每股风险
          仓位上限 = 账户净值 × 总风险上限 × 水位系数 / 入场价
        
        Args:
            entry_price: 入场价格
            stop_loss_price: 止损价格
            max_total_position_value: 最大仓位市值 (可选, 覆盖默认计算)
        
        Returns:
            {size, risk_amount, risk_per_share, max_size, water_level, reason}
        """
        result = {
            "size": 0,
            "risk_amount": 0,
            "risk_per_share": 0,
            "max_size": 0,
            "water_level": self._state.water_level.value,
            "reason": "",
        }
        
        if entry_price <= 0 or stop_loss_price <= 0:
            result["reason"] = "价格无效"
            return result
        
        # 每股风险
        risk_per_share = abs(entry_price - stop_loss_price)
        if risk_per_share <= 0:
            result["reason"] = "止损价等于入场价"
            return result
        
        result["risk_per_share"] = risk_per_share
        
        # 可开仓检查
        allowed, reason = self.can_open_position()
        if not allowed:
            result["reason"] = f"禁止开仓: {reason}"
            return result
        
        # 风险金额 = 账户净值 × 单笔风险% × 水位系数
        risk_multiplier = self.get_risk_multiplier()
        # P0 修复 (2026-04-22): 使用购买力而非总净值计算风险金额
        # 当现金为负时 (保证金模式)，总净值不代表可用资金
        # 优先级: buying_power > max(cash, 0) > current_equity * 0.2
        if self._state.buying_power > 0:
            risk_base = self._state.buying_power
        elif self._state.cash > 0:
            risk_base = self._state.cash
        else:
            # 现金为负时，使用净值的 20% 作为保守风险基数 (模拟盘防过度杠杆)
            risk_base = self._state.current_equity * 0.20
            logger.warning(
                f"[资本安全·仓位计算] 购买力/现金均不可用, 使用净值 20% 作为风险基数: "
                f"${risk_base:,.0f}"
            )
        risk_amount = risk_base * self.config.max_risk_per_trade_pct * risk_multiplier
        result["risk_amount"] = risk_amount
        
        # 基于风险的仓位
        risk_based_size = risk_amount / risk_per_share
        
        # 仓位上限 (基于总风险上限)
        position_multiplier = self.get_position_multiplier()
        max_position_value = (
            max_total_position_value or 
            self._state.current_equity * self.config.max_total_risk_pct * position_multiplier
        )
        max_size_by_cap = max_position_value / entry_price if entry_price > 0 else 0
        result["max_size"] = max_size_by_cap
        
        # 最终仓位 = min(风险仓位, 资金上限)
        final_size = min(risk_based_size, max_size_by_cap)
        
        # 向下取整到整数股
        final_size = max(0, int(final_size))
        result["size"] = final_size
        
        if final_size <= 0:
            result["reason"] = f"仓位过小 (风险=${risk_amount:.0f}, 每股风险=${risk_per_share:.2f})"
        else:
            actual_risk = final_size * risk_per_share
            position_value = final_size * entry_price
            result["reason"] = (
                f"水位 {self._state.water_level.value} | "
                f"风险=${actual_risk:.0f} ({actual_risk/self._state.current_equity*100:.1f}% 账户) | "
                f"仓位=${position_value:.0f} ({position_value/self._state.current_equity*100:.1f}% 账户)"
            )
        
        return result
    
    def get_status_report(self) -> Dict[str, Any]:
        """获取水位状态报告 (供推送/日志使用)"""
        wl = self._state.water_level
        wl_emojis = {
            WaterLevel.GREEN: "🟢",
            WaterLevel.YELLOW: "🟡",
            WaterLevel.ORANGE: "🟠",
            WaterLevel.RED: "🔴",
            WaterLevel.BLACKOUT: "⚫",
        }
        wl_labels = {
            WaterLevel.GREEN: "安全",
            WaterLevel.YELLOW: "警戒",
            WaterLevel.ORANGE: "警告",
            WaterLevel.RED: "危险",
            WaterLevel.BLACKOUT: "熔断",
        }
        
        return {
            "water_level": wl.value,
            "water_label": wl_labels.get(wl, ""),
            "water_emoji": wl_emojis.get(wl, ""),
            "initial_capital": self._state.initial_capital,
            "current_equity": self._state.current_equity,
            "peak_equity": self._state.peak_equity,
            "cash": self._state.cash,
            "market_value": self._state.market_value,
            "drawdown_pct": round(self._state.drawdown_pct * 100, 2),
            "max_drawdown_pct": round(self._state.max_drawdown_pct * 100, 2),
            "risk_multiplier": self.get_risk_multiplier(),
            "position_multiplier": self.get_position_multiplier(),
            "max_risk_per_trade": round(self._state.current_equity * self.config.max_risk_per_trade_pct * self.get_risk_multiplier(), 0),
            "is_locked": self._state.is_locked,
            "lock_reason": self._state.lock_reason,
        }
    
    # ═══════════════════════════════════════════════════════════════════
    #  Task #4 (2026-06-15): 双链路统一敞口上限管控
    # ═══════════════════════════════════════════════════════════════════
    #
    #  规则:
    #    1. 全局上限 (auto + manual 合计) — 硬约束, 取自
    #       config.dual_mode.exposure_control.global_max_total (默认 80%)
    #    2. 自动交易额外受激进程度上限约束:
    #         conservative=30% / normal=60% / aggressive=75%
    #    3. 手动交易仅受全局上限约束 (不受 auto 激进度限制)
    #
    #  所有方法均带 try/except 兜底, 失败返回 0.0 不阻塞主流程
    # ═══════════════════════════════════════════════════════════════════

    # 自动交易激进度对应的总敞口上限
    _AUTO_LEVEL_CAPS = {
        "conservative": 0.30,
        "normal": 0.60,
        "aggressive": 0.75,
    }

    def _get_global_exposure_cap(self) -> float:
        """读取 config.dual_mode.exposure_control.global_max_total (默认 80%)"""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            cap = cfg.get_dotted("dual_mode.exposure_control.global_max_total", 0.80)
            return float(cap) if cap else 0.80
        except Exception:
            return 0.80

    def _get_position_store(self):
        """获取 PositionStore 单例 (通过 USPositionSync), 失败返回 None"""
        try:
            from app.core.position_sync import get_us_position_sync
            sync = get_us_position_sync()
            return getattr(sync, "position_store", None)
        except Exception as e:
            logger.debug(f"[DualMode] PositionStore 不可用: {e}")
            return None

    def _calculate_manual_exposure(self) -> float:
        """计算当前手动交易总敞口金额 (USD).

        优先级:
          1. PositionStore 有该 symbol → 用 size × avg_entry_price (成本基)
          2. 否则用 manual_hold_registry 中记录的 quantity × avg_entry_price (若可得)
          3. 兜底 0
        """
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
        except Exception:
            return 0.0

        store = self._get_position_store()
        total = 0.0
        try:
            holdings = manual_hold_registry.get_all_holdings()
        except Exception:
            holdings = {}

        for symbol, info in holdings.items():
            try:
                # 优先从 PositionStore 取真实持仓 (size × cost)
                pos = store.get(symbol) if store else None
                if pos is not None and getattr(pos, "avg_entry_price", 0) > 0:
                    qty = abs(float(getattr(pos, "size", 0) or 0))
                    price = float(pos.avg_entry_price)
                    total += qty * price
                else:
                    # 兜底: registry 自带的 quantity (无价格 → 跳过)
                    qty = abs(float(getattr(info, "quantity", 0) or 0))
                    if qty > 0 and pos is not None:
                        price = float(getattr(pos, "avg_entry_price", 0) or 0)
                        total += qty * price
            except Exception:
                continue
        return total

    def _calculate_auto_exposure(self) -> float:
        """计算当前自动交易总敞口金额 (USD).

        从 PositionStore 中筛选非手动持仓 (排除 manual_hold_registry 中的 symbol
        以及 source=MANUAL 的 Position), 用 size × avg_entry_price 求和.
        """
        store = self._get_position_store()
        if store is None:
            return 0.0

        manual_symbols = set()
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            manual_symbols = manual_hold_registry.get_all_manual_symbols()
        except Exception:
            pass

        try:
            from app.positions import get_position_source, PositionSource
        except Exception:
            get_position_source = None
            PositionSource = None

        total = 0.0
        try:
            positions = store.all()
        except Exception:
            positions = []

        for pos in positions:
            try:
                symbol = getattr(pos, "symbol", "")
                if symbol in manual_symbols:
                    continue
                # 双重过滤: 排除 source=MANUAL 的仓位
                if get_position_source is not None and PositionSource is not None:
                    if get_position_source(pos) == PositionSource.MANUAL:
                        continue
                qty = abs(float(getattr(pos, "size", 0) or 0))
                price = float(getattr(pos, "avg_entry_price", 0) or 0)
                if qty > 0 and price > 0:
                    total += qty * price
            except Exception:
                continue
        return total

    def get_available_budget_for_auto(self) -> float:
        """计算自动交易可用预算 (USD).

        规则:
          1. 全局上限 (默认 80%) 是硬约束 (auto + manual 合计不可超)
          2. 自动交易额外受激进度上限约束 (conservative=30% / normal=60% / aggressive=75%)
          3. 返回 max(0, min(global_remaining, auto_remaining))
        """
        try:
            equity = float(self._state.current_equity or 0)
            if equity <= 0:
                return 0.0

            manual_exposure = self._calculate_manual_exposure()
            auto_exposure = self._calculate_auto_exposure()

            # 全局剩余 = equity × 80% - 已用 (auto + manual)
            global_cap_pct = self._get_global_exposure_cap()
            global_max = equity * global_cap_pct
            global_remaining = global_max - manual_exposure - auto_exposure

            # 自动交易自身上限 (按激进度)
            try:
                from app.web_dashboard.mode_switch_service import get_mode_switch_service
                level = get_mode_switch_service().get_auto_aggression()
            except Exception:
                level = "conservative"
            auto_cap_pct = self._AUTO_LEVEL_CAPS.get(level, 0.30)
            auto_cap = equity * auto_cap_pct
            auto_remaining = auto_cap - auto_exposure

            available = max(0.0, min(global_remaining, auto_remaining))
            logger.debug(
                f"[DualMode] auto_budget: equity=${equity:,.0f} | "
                f"manual=${manual_exposure:,.0f} | auto=${auto_exposure:,.0f} | "
                f"global_remain=${global_remaining:,.0f} | "
                f"auto_cap({level})=${auto_cap:,.0f} | available=${available:,.0f}"
            )
            return available
        except Exception as e:
            logger.warning(f"[DualMode] Failed to calculate auto budget: {e}")
            return 0.0

    def get_available_budget_for_manual(self) -> float:
        """计算手动交易可用预算 (USD).

        规则:
          1. 手动不受 auto 的 cap 限制
          2. 仅受全局上限 (默认 80%) 约束
        """
        try:
            equity = float(self._state.current_equity or 0)
            if equity <= 0:
                return 0.0

            auto_exposure = self._calculate_auto_exposure()
            manual_exposure = self._calculate_manual_exposure()

            global_cap_pct = self._get_global_exposure_cap()
            global_max = equity * global_cap_pct
            available = max(0.0, global_max - auto_exposure - manual_exposure)
            logger.debug(
                f"[DualMode] manual_budget: equity=${equity:,.0f} | "
                f"auto=${auto_exposure:,.0f} | manual=${manual_exposure:,.0f} | "
                f"global_max=${global_max:,.0f} | available=${available:,.0f}"
            )
            return available
        except Exception as e:
            logger.warning(f"[DualMode] Failed to calculate manual budget: {e}")
            return 0.0

    def _determine_water_level(self) -> WaterLevel:
        """根据回撤确定水位等级
        
        回撤为负值 (如 -0.05 = -5% 回撤)
        回撤为 0 或正值表示无回撤 (在最高点或以上)
        """
        dd = self._state.drawdown_pct  # 负值表示回撤, 0 或正值表示无回撤
        
        # 无回撤或盈利 → 安全
        if dd >= self.config.green_threshold:  # dd >= -0.05
            return WaterLevel.GREEN
        elif dd > self.config.yellow_threshold:  # -0.05 > dd > -0.10
            return WaterLevel.YELLOW
        elif dd > self.config.orange_threshold:  # -0.10 > dd > -0.20
            return WaterLevel.ORANGE
        elif dd > self.config.red_threshold:  # -0.20 > dd > -0.35
            return WaterLevel.RED
        else:  # dd <= -0.35 (回撤超过 35%)
            return WaterLevel.BLACKOUT
    
    def _load_yaml_config(self):
        """P2 修复 (2026-05-02): 从 unified_config.yaml 加载水位配置"""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            cs = cfg.get_dotted("risk.capital_safety", {})
            wl = cs.get("water_levels", {})
            
            # 水位回撤阈值
            if "green" in wl:
                self.config.green_threshold = wl["green"].get("threshold", self.config.green_threshold)
            if "yellow" in wl:
                self.config.yellow_threshold = wl["yellow"].get("threshold", self.config.yellow_threshold)
            if "orange" in wl:
                self.config.orange_threshold = wl["orange"].get("threshold", self.config.orange_threshold)
            if "red" in wl:
                self.config.red_threshold = wl["red"].get("threshold", self.config.red_threshold)
            
            # 风险系数
            if "green" in wl:
                self.config.green_risk_multiplier = wl["green"].get("risk_mult", self.config.green_risk_multiplier)
            if "yellow" in wl:
                self.config.yellow_risk_multiplier = wl["yellow"].get("risk_mult", self.config.yellow_risk_multiplier)
            if "orange" in wl:
                self.config.orange_risk_multiplier = wl["orange"].get("risk_mult", self.config.orange_risk_multiplier)
            if "red" in wl:
                self.config.red_risk_multiplier = wl["red"].get("risk_mult", self.config.red_risk_multiplier)
            self.config.blackout_risk_multiplier = 0.0  # 熔断始终为 0
            
            # 仓位系数
            if "green" in wl:
                self.config.green_position_multiplier = wl["green"].get("pos_mult", self.config.green_position_multiplier)
            if "yellow" in wl:
                self.config.yellow_position_multiplier = wl["yellow"].get("pos_mult", self.config.yellow_position_multiplier)
            if "orange" in wl:
                self.config.orange_position_multiplier = wl["orange"].get("pos_mult", self.config.orange_position_multiplier)
            if "red" in wl:
                self.config.red_position_multiplier = wl["red"].get("pos_mult", self.config.red_position_multiplier)
            self.config.blackout_position_multiplier = 0.0  # 熔断始终为 0
            
            logger.debug(f"[资本安全] YAML 配置已加载 | green_th={self.config.green_threshold} | risk_mults=[{self.config.green_risk_multiplier},{self.config.yellow_risk_multiplier},{self.config.orange_risk_multiplier},{self.config.red_risk_multiplier}]")
        except Exception as e:
            logger.debug(f"[资本安全] YAML 配置加载失败，使用默认值: {e}")
    
    def _ensure_db_table(self):
        """创建或迁移资本状态表"""
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS capital_state (
                    id INTEGER PRIMARY KEY,
                    initial_capital REAL,
                    current_equity REAL,
                    peak_equity REAL,
                    cash REAL,
                    market_value REAL,
                    buying_power REAL,
                    drawdown_pct REAL,
                    max_drawdown_pct REAL,
                    water_level TEXT,
                    is_locked INTEGER,
                    lock_reason TEXT,
                    last_update TEXT
                )
            """)
            # P0 修复 (2026-05-01): 迁移 — 添加 trade_env 列
            try:
                c.execute("ALTER TABLE capital_state ADD COLUMN trade_env INTEGER DEFAULT 1")
                logger.info("[资本安全] DB 迁移: 已添加 trade_env 列")
            except Exception:
                pass  # 列已存在
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[资本安全] 创建 DB 表失败: {e}")
    
    def _load_state(self):
        """从 DB 加载状态 — 检测 trade_env 切换自动重置基准"""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            conn.close()
            
            if row:
                # P0 修复 (2026-05-01): 检查 trade_env 是否切换
                saved_env = row["trade_env"] if "trade_env" in row.keys() else None
                if saved_env is not None and saved_env != self.trade_env:
                    logger.warning(
                        f"[资本安全] 检测到交易环境切换 (saved={saved_env} → current={self.trade_env})，"
                        f"自动重置资金基准"
                    )
                    self._peak_initialized = False
                    return  # 不加载旧状态，让首次 update_account 重新初始化
                
                self._state.initial_capital = float(row["initial_capital"] or 0)
                self._state.current_equity = float(row["current_equity"] or 0)
                self._state.peak_equity = float(row["peak_equity"] or 0)
                self._state.cash = float(row["cash"] or 0)
                self._state.market_value = float(row["market_value"] or 0)
                self._state.buying_power = float(row["buying_power"] or 0)
                self._state.drawdown_pct = float(row["drawdown_pct"] or 0)
                self._state.max_drawdown_pct = float(row["max_drawdown_pct"] or 0)
                self._state.water_level = WaterLevel(row["water_level"] or "green")
                self._state.is_locked = bool(row["is_locked"])
                self._state.lock_reason = row["lock_reason"] or ""
                self._state.last_update = row["last_update"] or ""
                # P0 修复 (2026-06-14): 保留 peak_equity 跨重启, 防止回撤归零
                # 原问题: _peak_initialized=False 导致首次 update_account 重置 peak 为当前净值
                #   → 重启后回撤从 0% 开始, 丢失历史亏损记忆
                # 修复: trade_env 未变时保留 DB 中的 peak, 标记已初始化
                #   trade_env 变化已在 L613 单独处理 (重置 _peak_initialized=False)
                if self._state.peak_equity > 0:
                    self._peak_initialized = True
                    logger.info(
                        f"[资本安全] 状态已加载 | DB净值=${self._state.current_equity:,.0f} | "
                        f"peak=${self._state.peak_equity:,.0f} (保留) | "
                        f"回撤={self._state.drawdown_pct*100:.1f}% | "
                        f"水位={self._state.water_level.value}"
                    )
                else:
                    self._peak_initialized = False
                    logger.info(
                        f"[资本安全] 状态已加载 | DB净值=${self._state.current_equity:,.0f} | "
                        f"peak=0 (将从 broker 初始化) | "
                        f"回撤={self._state.drawdown_pct*100:.1f}% | "
                        f"水位={self._state.water_level.value}"
                    )
            else:
                # P1 修复 (2026-04-16): 无历史记录时，初始资金将在首次 update_account 时从实际账户同步
                self._peak_initialized = False
                logger.info(f"[资本安全] 无历史记录, 初始资金将在首次更新时同步")
        except Exception as e:
            logger.warning(f"[资本安全] 加载状态失败: {e}")
    
    def _save_state(self):
        """保存状态到 DB
        
        P1-1 修复: 保存失败时标记 _save_failed, 后续 can_open_position 将 fail-safe 拦截
        """
        self._save_failed = False
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("""
                INSERT INTO capital_state 
                (initial_capital, current_equity, peak_equity, cash, market_value, 
                 buying_power, drawdown_pct, max_drawdown_pct, water_level, 
                 is_locked, lock_reason, last_update, trade_env)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self._state.initial_capital,
                self._state.current_equity,
                self._state.peak_equity,
                self._state.cash,
                self._state.market_value,
                self._state.buying_power,
                self._state.drawdown_pct,
                self._state.max_drawdown_pct,
                self._state.water_level.value,
                int(self._state.is_locked),
                self._state.lock_reason,
                self._state.last_update,
                self.trade_env,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self._save_failed = True
            logger.warning(f"[资本安全] 保存状态失败 (fail-safe 将拦截新开仓): {e}")

    def reset_capital(self, new_capital: float = None):
        """
        P0 修复 (2026-05-01): 手动重置资金基准
        
        场景: 切换到实盘、重置资金量、清理历史回撤记录。
        调用后，首次 update_account 时会用实际账户净值重新初始化基准。
        
        Args:
            new_capital: 新的初始资金 (None = 等待首次 update_account 自动同步)
        """
        if new_capital is not None and new_capital > 0:
            self._state.initial_capital = new_capital
            self._state.peak_equity = new_capital
            self._state.current_equity = new_capital
            self._state.drawdown_pct = 0.0
            self._state.max_drawdown_pct = 0.0
            self._state.water_level = WaterLevel.GREEN
            self._state.is_locked = False
            self._state.lock_reason = ""
            logger.info(
                f"[资本安全] 资金基准已重置 → ${new_capital:,.0f} | "
                f"水位重置为 GREEN"
            )
        else:
            # 清除历史状态，等待首次 update_account 同步
            self._peak_initialized = False
            logger.info("[资本安全] 历史状态已清除, 等待首次 update_account 同步")
        
        try:
            self._save_state()
        except Exception as e:
            logger.warning(f"[资本安全] 重置后保存失败: {e}")


# 全局单例
_capital_instance: Optional[CapitalSafetySystem] = None

def get_capital_safety_system(initial_capital: Optional[float] = None,
                               trade_env: int = None) -> CapitalSafetySystem:
    """获取全局资本安全系统单例
    
    Args:
        initial_capital: 初始资金
        trade_env: 0=实盘, 1=模拟 (默认自动检测，模拟=1)
    """
    global _capital_instance
    if _capital_instance is None:
        # 自动检测交易环境：优先使用参数，其次从 ConfigContainer 读取
        if trade_env is None:
            try:
                from app.core.config_container import get_config
                trade_env = get_config().trade_env_int
            except Exception:
                trade_env = int(os.getenv("TRADE_ENV", "1"))
        
        _capital_instance = CapitalSafetySystem(
            initial_capital=initial_capital,
            trade_env=trade_env
        )
    return _capital_instance
