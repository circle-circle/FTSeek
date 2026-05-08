#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态止盈止损管理器 (Dynamic Exit Manager)

功能:
1. 根据市场状态动态调整止盈止损参数
2. 加载 unified_config.yaml (SSOT) 配置
3. 提供参数查询接口
4. 支持运行时切换
5. 考虑交易费用调整止盈止损 (2026-03-24 新增)

市场状态:
- strong_trend_up: 强上涨
- weak_trend_up: 弱上涨
- range_bound: 震荡
- weak_trend_down: 弱下跌
- strong_trend_down: 强下跌
- high_volatility: 高波动
- low_volatility: 低波动
"""

import logging
import time
import yaml
import os
from dataclasses import dataclass
from typing import Dict, Optional
from pathlib import Path

# 导入交易费用模块
from app.risk.trading_fees import (
    TradingFees, DEFAULT_FEES,
    adjust_stop_loss_for_fees,
    adjust_take_profit_for_fees,
    calculate_break_even_price
)

logger = logging.getLogger("ftseek.dynamic_exit")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出

# 配置文件路径
DYNAMIC_EXIT_CONFIG_PATH = '/opt/ftseek/FTSeek/config/unified_config.yaml'  # SSOT


@dataclass
class ExitDecision:
    """退出决策（组合式退出引擎的返回结果）"""
    should_exit: bool
    reason: str = ""
    priority: int = 99       # 优先级，越小越紧急
    close_ratio: float = 1.0 # 平仓比例 (1.0=全平, 0.3=30%)
    is_stop_loss: bool = False
    # 状态机新增字段
    new_state: str = None    # 新退出状态 (如 'BE_ACTIVE', 'TRAILING')
    new_stop_loss: float = None  # 新止损价 (如保本上移后的止损)


class DynamicExitManager:
    """动态止盈止损管理器"""
    
    def __init__(self, config_path: str = None):
        """
        初始化
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path or DYNAMIC_EXIT_CONFIG_PATH
        self.config = {}
        self.current_regime = 'default'
        self.current_params = {}
        self._load_config()

        # P3 修复 (2026-05-06): 保本止损日志去抖 — 每个持仓 60s 内只记录一次
        self._breakeven_log_cache: Dict[str, float] = {}  # code → last_log_time

        logger.info(f"动态止盈止损管理器已初始化")
    
    def _load_config(self):
        """加载统一配置 (SSOT: unified_config.yaml)"""
        try:
            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件不存在：{self.config_path}")
                return
            
            with open(self.config_path, 'r', encoding='utf-8') as f:
                full_config = yaml.safe_load(f)
            
            # SSOT 兼容: 从 unified_config 提取 dynamic_exit_params
            self.config = full_config.get('dynamic_exit_params', full_config)
            
            logger.info(f"动态止盈配置已加载：{len(self.config)}个状态")
            
            # 加载默认参数
            self._apply_params('default')
            
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
            self.config = {}
    
    def _apply_params(self, regime: str):
        """应用指定状态的参数"""
        if regime not in self.config:
            logger.warning(f"状态 {regime} 不存在，使用默认参数")
            regime = 'default'
        
        params = self.config[regime]
        
        self.current_regime = regime
        self.current_params = {
            'stop_loss': params.get('stop_loss', -0.06),
            'take_profit': params.get('take_profit', 0.12),
            'partial_take_profit': params.get('partial_take_profit', {}),
            'trailing_stop': params.get('trailing_stop', {}),
            'atr_stop': params.get('atr_stop', {}),
            'time_stop': params.get('time_stop', {}),
            'breakeven_stop': params.get('breakeven_stop', {}),
            'intraday_stop': params.get('intraday_stop', {}),
        }
        
        # 日志输出
        breakeven = params.get('breakeven_stop', {})
        intraday = params.get('intraday_stop', {})
        breakeven_info = f"保本触发=+{breakeven.get('trigger_profit', 0.03):.1%}" if breakeven.get('enabled', False) else "无"
        intraday_info = f"日内止损={intraday.get('stop_percent', 0.02):.1%}" if intraday.get('enabled', False) else "无"
        
        logger.info(
            f"应用止盈止损参数 [{regime}]: "
            f"止损={self.current_params['stop_loss']:.1%}, "
            f"止盈={self.current_params['take_profit']:.1%}, "
            f"{breakeven_info}, {intraday_info}"
        )
    
    def update_regime(self, regime: str, adx: float = None, atr_pct: float = None):
        """
        更新市场状态并应用对应参数
        
        Args:
            regime: 市场状态
            adx: ADX 值 (可选)
            atr_pct: ATR 百分位 (可选)
        """
        if regime == self.current_regime:
            return
        
        self._apply_params(regime)
    
    def get_stop_loss(self) -> float:
        """获取止损比例"""
        return self.current_params.get('stop_loss', -0.06)
    
    def get_take_profit(self) -> float:
        """获取止盈比例"""
        return self.current_params.get('take_profit', 0.12)
    
    def get_stop_loss_price_with_fees(
        self, 
        entry_price: float, 
        quantity: int = 100,
        fees: TradingFees = None
    ) -> float:
        """
        获取考虑交易费用的止损价
        
        Args:
            entry_price: 入场价
            quantity: 数量
            fees: 费用配置
            
        Returns:
            止损价 (已考虑费用)
        """
        stop_loss_pct = self.get_stop_loss()
        return adjust_stop_loss_for_fees(entry_price, stop_loss_pct, quantity, fees)
    
    def get_take_profit_price_with_fees(
        self,
        entry_price: float,
        quantity: int = 100,
        fees: TradingFees = None
    ) -> float:
        """
        获取考虑交易费用的止盈价
        
        Args:
            entry_price: 入场价
            quantity: 数量
            fees: 费用配置
            
        Returns:
            止盈价 (已考虑费用)
        """
        take_profit_pct = self.get_take_profit()
        return adjust_take_profit_for_fees(entry_price, take_profit_pct, quantity, fees)
    
    def get_breakeven_price(
        self,
        entry_price: float,
        quantity: int = 100,
        fees: TradingFees = None
    ) -> float:
        """
        获取保本价格 (覆盖交易费用)
        
        Args:
            entry_price: 入场价
            quantity: 数量
            fees: 费用配置
            
        Returns:
            保本价格
        """
        return calculate_break_even_price(entry_price, quantity, fees)
    
    def get_partial_take_profit_levels(self) -> list:
        """获取分批止盈级别"""
        levels = self.current_params.get('partial_take_profit', {}).get('levels', [])
        return [
            {
                # 兼容 'trigger' 和 'target' 两种键名 (配置文件使用 trigger)
                'target': level.get('trigger') or level.get('target', 0.08),
                'ratio': level.get('ratio', 0.30),
            }
            for level in levels
        ]
    
    def get_trailing_stop_params(self) -> dict:
        """获取移动止盈参数"""
        return self.current_params.get('trailing_stop', {
            'enabled': True,
            'trigger_profit': 0.05,   # 盈利 5% 后启动追踪止盈 (对齐 YAML default)
            'trail_percent': 0.02,    # 回撤 2% 触发 (对齐 YAML default)
            'atr_multiplier': 2.5,    # ATR 移动止损倍数
        })
    
    def get_market_params(self, market: str = 'hk') -> Dict:
        """获取指定市场的退出参数"""
        if market.lower() == 'us' and 'us' in self.config:
            return self.config['us']
        return self.config.get('default', {})

    def get_atr_stop_params(self) -> dict:
        """获取 ATR 止损参数"""
        return self.current_params.get('atr_stop', {
            'enabled': True,
            'multiplier': 2.0,
            'min_stop': 0.05,
            'max_stop': 0.10,
        })
    
    def get_time_stop_params(self) -> dict:
        """获取时间止损参数"""
        return self.current_params.get('time_stop', {
            'enabled': True,
            'max_holding_hours': 72,
            'min_profit_hours': 24,
            'extend_if_uptrend': True,
        })
    
    def get_breakeven_stop_params(self) -> dict:
        """
        获取保本止损参数 (新增 2026-03-17)
        
        功能：盈利达到阈值后，止损上移到成本价，防止盈利变亏损
        
        Returns:
            dict: {
                enabled: bool - 是否启用
                trigger_profit: float - 触发阈值 (如 0.03 = 盈利3%后启用)
                stop_level: float - 止损水平 (0.00 = 成本价)
            }
        """
        return self.current_params.get('breakeven_stop', {
            'enabled': True,
            'trigger_profit': 0.03,
            'stop_level': 0.00,
        })
    
    def get_intraday_stop_params(self) -> dict:
        """
        获取日内止损参数 (新增 2026-03-17)
        
        功能：T+0 当日更敏感的止损，适应港股日内交易
        
        Returns:
            dict: {
                enabled: bool - 是否启用
                stop_percent: float - 日内止损阈值 (如 0.02 = 2%)
                session_end_force: bool - 收盘前是否强制平仓
            }
        """
        return self.current_params.get('intraday_stop', {
            'enabled': True,
            'stop_percent': 0.02,
            'session_end_force': False,
        })
    
    def check_breakeven_stop(self, entry_price: float, current_price: float, peak_price: float,
                             trigger_override: float = None) -> tuple:
        """
        检查是否触发保本止损 (新增 2026-03-17)
        
        Args:
            entry_price: 入场价
            current_price: 当前价
            peak_price: 持仓期间最高价
            trigger_override: 时段覆写的触发阈值 (TimeGate)
        
        Returns:
            (should_update: bool, new_stop_price: float)
        """
        params = self.get_breakeven_stop_params()
        
        if not params.get('enabled', True):
            return False, None
        
        trigger_profit = trigger_override if trigger_override is not None else params.get('trigger_profit', 0.03)
        stop_level = params.get('stop_level', 0.00)
        
        # 计算当前收益率 (使用最高价判断)
        if entry_price <= 0:
            return False, None
        
        peak_return = (peak_price - entry_price) / entry_price
        
        # 如果曾盈利超过触发阈值，止损上移到成本价
        if peak_return >= trigger_profit:
            new_stop_price = entry_price * (1 + stop_level)
            # P3 修复 (2026-05-06): 日志去抖 — 同一标的 60s 内只记录一次
            code = getattr(self, '_last_code', 'unknown')
            now = time.time()
            last_log = self._breakeven_log_cache.get(code, 0)
            if now - last_log >= 60:
                logger.info(
                    f"保本止损激活: 最高收益={peak_return:.1%} >= 触发线={trigger_profit:.1%}, "
                    f"止损上移到 {stop_level:.1%} (成本价: {new_stop_price:.2f})"
                )
                self._breakeven_log_cache[code] = now
            return True, new_stop_price
        
        return False, None
    
    def check_intraday_stop(self, entry_price: float, current_price: float, 
                            entry_time, is_same_day: bool = True,
                            stop_override: float = None) -> tuple:
        """
        检查是否触发日内止损 (新增 2026-03-17)
        
        Args:
            entry_price: 入场价
            current_price: 当前价
            entry_time: 入场时间
            is_same_day: 是否当日买入
            stop_override: 时段覆写的止损百分比 (TimeGate)
        
        Returns:
            (should_exit: bool, reason: str)
        """
        params = self.get_intraday_stop_params()
        
        if not params.get('enabled', True):
            return False, None
        
        # 只对当日买入的持仓生效
        if not is_same_day:
            return False, None
        
        stop_percent = stop_override if stop_override is not None else params.get('stop_percent', 0.02)
        
        # 计算收益率
        if entry_price <= 0:
            return False, None
        
        current_return = (current_price - entry_price) / entry_price
        
        # 日内亏损超过阈值，触发止损
        if current_return <= -stop_percent:
            reason = f"日内止损触发: 亏损={abs(current_return):.1%} >= {stop_percent:.1%}"
            logger.info(reason)
            return True, reason
        
        return False, None
    
    def calculate_atr_stop_price(self, entry_price: float, atr: float, direction: str) -> float:
        """
        计算 ATR 止损价
        
        Args:
            entry_price: 入场价
            atr: ATR 值
            direction: BUY/SELL
        
        Returns:
            float: 止损价
        """
        params = self.get_atr_stop_params()
        
        if not params.get('enabled', True):
            return entry_price * (1 + self.get_stop_loss()) if direction == 'BUY' else entry_price * (1 - abs(self.get_stop_loss()))
        
        multiplier = params.get('multiplier', 2.0)
        min_stop = params.get('min_stop', 0.05)
        max_stop = params.get('max_stop', 0.10)
        
        # 计算 ATR 止损幅度
        atr_stop_distance = (atr * multiplier) / entry_price
        atr_stop_distance = max(min_stop, min(max_stop, atr_stop_distance))
        
        # 计算止损价
        if direction == 'BUY':
            stop_price = entry_price * (1 - atr_stop_distance)
        else:
            stop_price = entry_price * (1 + atr_stop_distance)
        
        return round(stop_price, 2)
    
    def get_all_params(self) -> Dict:
        """获取所有参数"""
        return {
            'regime': self.current_regime,
            'stop_loss': self.get_stop_loss(),
            'take_profit': self.get_take_profit(),
            'partial_take_profit': self.get_partial_take_profit_levels(),
            'trailing_stop': self.get_trailing_stop_params(),
            'atr_stop': self.get_atr_stop_params(),
            'time_stop': self.get_time_stop_params(),
            'breakeven_stop': self.get_breakeven_stop_params(),
            'intraday_stop': self.get_intraday_stop_params(),
        }
    
    def print_params(self):
        """打印当前参数"""
        params = self.get_all_params()
        
        print("\n" + "=" * 80)
        print(f"📊 动态止盈止损参数 - {params['regime']}")
        print("=" * 80)
        print(f"止损：{params['stop_loss']:.1%}")
        print(f"止盈：{params['take_profit']:.1%}")
        
        # 保本止损
        be = params['breakeven_stop']
        if be.get('enabled', False):
            print(f"\n保本止损:")
            print(f"  启用：✅")
            print(f"  触发：+{be.get('trigger_profit', 0.03):.1%}")
            print(f"  止损位：成本价 + {be.get('stop_level', 0.0):.1%}")
        
        # 日内止损
        intraday = params['intraday_stop']
        if intraday.get('enabled', False):
            print(f"\n日内止损 (T+0):")
            print(f"  启用：✅")
            print(f"  阈值：{intraday.get('stop_percent', 0.02):.1%}")
            print(f"  收盘平仓：{'是' if intraday.get('session_end_force', False) else '否'}")
        
        print(f"\n分批止盈:")
        for level in params['partial_take_profit']:
            print(f"  +{level['target']:.1%} → 平仓{level['ratio']:.0%}")
        
        print(f"\n移动止盈:")
        ts = params['trailing_stop']
        print(f"  启用：{ts.get('enabled', True)}")
        print(f"  触发：+{ts.get('trigger_profit', 0.08):.1%}")
        print(f"  回撤：{ts.get('trail_percent', 0.04):.1%}")
        
        print(f"\nATR 止损:")
        atr = params['atr_stop']
        print(f"  启用：{atr.get('enabled', True)}")
        print(f"  乘数：{atr.get('multiplier', 2.0)}x")
        print(f"  范围：{atr.get('min_stop', 0.05):.1%} - {atr.get('max_stop', 0.10):.1%}")
        
        print(f"\n时间止损:")
        time_params = params['time_stop']
        print(f"  启用：{time_params.get('enabled', True)}")
        print(f"  最大持仓：{time_params.get('max_holding_hours', 72)}小时")
        print("=" * 80)
        print()

    def check_exit_priority(
        self,
        code: str,
        entry_price: float,
        current_price: float,
        peak_price: float,
        entry_time,
        atr_value: float,
        direction: str,
        is_same_day: bool = True,
        current_volume: float = 0,   # P6 新增: 当前成交量
        avg_volume: float = 0,       # P6 新增: 20日平均成交量
    ) -> 'ExitDecision':
        """
        组合式退出优先级检查，按优先级依次判断各退出条件。

        优先级顺序（数字越小越先执行）：
          0. 熔断清仓          (系统级风险控制)
          1. ATR 硬止损        (止损=1.5×ATR)
          2. 结构止损          (固定百分比 stop_loss)
          3. 日内止损          (T+0 当日更敏感止损)
          4. 时间止损          (持仓超过 max_holding_hours)
          4.5. 盘末退出         (距收盘 <=15min: 亏损全平/微利减半/≥0.5ATR过夜)
          5. 波动止损          (日内涨跌区间 > 3×ATR)
          6. 保本止损          (盈利触发后上移止损)
          7. 移动止损          (盈利>trigger_profit 后, 距峰值>trail 触发)
          8. 分批止盈第一目标   (close_ratio=0.3)
          9. 分批止盈第二目标   (close_ratio=0.3)
          10. 固定止盈         (close_ratio=1.0)

        Args:
            code:          股票代码
            entry_price:   入场价
            current_price: 当前价
            peak_price:    持仓期间最高价（多头）或最低价（空头）
            entry_time:    入场时间（datetime 或 ISO 字符串）
            atr_value:     当前 ATR 值（如无则传 0）
            direction:     'BUY' 或 'SELL'
            is_same_day:   是否当日买入（用于日内止损判断）

        Returns:
            ExitDecision
        """
        # P3 修复 (2026-05-06): 传递 code 给保本止损去抖逻辑
        self._last_code = code
        if entry_price <= 0 or current_price <= 0:
            return ExitDecision(should_exit=False)

        pnl_pct = (current_price - entry_price) / entry_price
        if direction == 'SELL':
            pnl_pct = -pnl_pct  # 空头盈亏方向反转

        # ── 1. ATR 硬止损 ──────────────────────────────────────────
        # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
        from app.strategy.time_gate import get_exit_param
        if atr_value and atr_value > 0:
            atr_mult = get_exit_param("atr_hard_mult", default=1.5, entry_time=entry_time)
            atr_stop_dist = atr_mult * atr_value / entry_price
            if pnl_pct <= -atr_stop_dist:
                return ExitDecision(
                    should_exit=True, reason=f"ATR硬止损(1.5x): 亏损={pnl_pct:.1%}",
                    priority=1, close_ratio=1.0, is_stop_loss=True
                )

        # ── 2. 结构止损（固定百分比）─────────────────────────────────
        struct_stop = abs(self.get_stop_loss())
        if pnl_pct <= -struct_stop:
            return ExitDecision(
                should_exit=True, reason=f"结构止损: 亏损={pnl_pct:.1%}",
                priority=2, close_ratio=1.0, is_stop_loss=True
            )

        # ── 3. 日内止损（T+0 当日更敏感）────────────────────────────
        # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
        intraday_pct = get_exit_param("intraday_stop_pct", default=0.02, entry_time=entry_time)
        intraday_exit, intraday_reason = self.check_intraday_stop(
            entry_price, current_price, entry_time, is_same_day,
            stop_override=intraday_pct
        )
        if intraday_exit:
            return ExitDecision(
                should_exit=True, reason=intraday_reason,
                priority=3, close_ratio=1.0, is_stop_loss=True
            )

        # ── 4. 时间止损 ───────────────────────────────────────────
        time_params = self.get_time_stop_params()
        if time_params.get('enabled', True) and entry_time:
            try:
                from datetime import datetime
                if isinstance(entry_time, str):
                    et = datetime.fromisoformat(entry_time)
                else:
                    et = entry_time
                holding_hours = (datetime.now() - et).total_seconds() / 3600
                # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
                max_hours = get_exit_param("max_holding_hours", default=time_params.get('max_holding_hours', 72), entry_time=entry_time)
                if holding_hours >= max_hours:
                    return ExitDecision(
                        should_exit=True,
                        reason=f"时间止损: 持仓{holding_hours:.1f}h >= {max_hours}h",
                        priority=4, close_ratio=1.0, is_stop_loss=False
                    )
            except Exception:
                pass

        # ── 4.5. 盘末退出 (Pre-Close Exit) — P1 2026-05-06 ──────────
        # 港股 15:45 收盘撮合前分层退出，避免隔夜跳空风险
        # 规则: 亏损全平 / 0~0.5ATR 微利减半 / ≥0.5ATR 保留
        if getattr(self, '_pre_close_exit_enabled', True):
            try:
                window_min = getattr(self, '_pre_close_window_minutes', 15)
                from datetime import datetime
                now = datetime.now()
                hk_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
                minutes_to_close = (hk_close - now).total_seconds() / 60.0

                if 0 < minutes_to_close <= window_min and atr_value and atr_value > 0:
                    # 计算盈利距入场价 (ATR 口径)
                    profit_atr = (current_price - entry_price) / atr_value if direction == 'BUY' \
                        else (entry_price - current_price) / atr_value

                    if profit_atr < 0:
                        # 亏损 → 全平
                        return ExitDecision(
                            should_exit=True,
                            reason=f"盘末退出: 亏损 {profit_atr:.1f}ATR, 不持有亏损过夜",
                            priority=4.5, close_ratio=1.0, is_stop_loss=True
                        )
                    elif profit_atr < 0.5:
                        # 微利 (0~0.5 ATR) → 减半
                        return ExitDecision(
                            should_exit=True,
                            reason=f"盘末退出: 微利 {profit_atr:.1f}ATR, 减半降低隔夜风险",
                            priority=4.5, close_ratio=0.5, is_stop_loss=False
                        )
                    # ≥0.5 ATR → 保留过夜, 不触发
            except Exception:
                pass

        # ── 4.5. P6 放量止盈 (Volume Surge TP) ─────────────────────
        # 辩证设计: 仅在浮盈 > 1.5% 且成交量 > 3.5倍均量时触发，部分止盈 50%
        # 防止低位放量下跌被误杀，同时避免一次性清空筹码
        if current_volume > 0 and avg_volume > 0:
            vol_cfg = getattr(config, 'PHASE1_HK_OPT', {}).get('exit_strategy', {}).get('volume_surge', {})
            if vol_cfg.get('enabled', True):
                vol_mult = vol_cfg.get('vol_multiplier', 3.5)
                min_pnl = vol_cfg.get('min_profit_pct', 0.015)
                
                if current_volume >= avg_volume * vol_mult and pnl_pct >= min_pnl:
                    return ExitDecision(
                        should_exit=True,
                        reason=f"放量止盈(P6): 量比={current_volume/avg_volume:.1f}x, 浮盈={pnl_pct:.1%}",
                        priority=4.5, close_ratio=0.5, is_stop_loss=False # 兑现一半
                    )

        # ── 5. 波动止损（日内价格区间 > 3×ATR）──────────────────────
        if atr_value and atr_value > 0:
            intraday_range = abs(peak_price - current_price) / entry_price
            if intraday_range > 3 * (atr_value / entry_price) and pnl_pct < 0:
                return ExitDecision(
                    should_exit=True,
                    reason=f"波动止损: 区间={intraday_range:.1%} > 3×ATR",
                    priority=5, close_ratio=1.0, is_stop_loss=True
                )

        # ── 6. 保本止损（盈利触发后上移止损）────────────────────────
        # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
        be_trigger = get_exit_param("breakeven_trigger", default=0.015, entry_time=entry_time)
        breakeven_triggered, breakeven_price = self.check_breakeven_stop(
            entry_price, current_price, peak_price, trigger_override=be_trigger
        )
        if breakeven_triggered and current_price <= breakeven_price:
            return ExitDecision(
                should_exit=True,
                reason=f"保本止损触发: 当前价{current_price:.2f} <= 保本价{breakeven_price:.2f}",
                priority=6, close_ratio=1.0, is_stop_loss=False
            )

        # ── 7. 移动止损 ───────────────────────────────────────────
        # P0-2 优化 (2026-04-06): 默认使用 ATR 倍数，避免固定百分比过早触发
        trail_params = self.get_trailing_stop_params()
        if trail_params.get('enabled', True):
            trigger = trail_params.get('trigger_profit', 0.08)
            trail_pct = trail_params.get('trail_percent', 0.04)

            # P0-2 优化：ATR 移动止损优先 (从 2x提升至 2.5x)
            # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
            if atr_value and atr_value > 0:
                trail_mult = get_exit_param("trailing_atr_mult", default=trail_params.get('atr_multiplier', 2.5), entry_time=entry_time)
                trail_dist = trail_mult * atr_value / entry_price
            else:
                trail_dist = trail_pct

            if pnl_pct >= trigger:
                # 距峰值回落超过 trail_dist
                peak_pnl = (peak_price - entry_price) / entry_price if direction == 'BUY' \
                    else (entry_price - peak_price) / entry_price
                drawdown_from_peak = peak_pnl - pnl_pct
                if drawdown_from_peak >= trail_dist:
                    return ExitDecision(
                        should_exit=True,
                        reason=f"移动止损: 从峰值回撤={drawdown_from_peak:.1%} >= {trail_dist:.1%}",
                        priority=7, close_ratio=1.0, is_stop_loss=False
                    )

        # ── 8-10. 分批止盈 & 固定止盈 ─────────────────────────────
        # P1 修复 (2026-05-04): 接入 TimeGate 时段参数覆写
        tp_mult = get_exit_param("take_profit_atr_mult", default=1.0, entry_time=entry_time)
        partial_levels = self.get_partial_take_profit_levels()
        if partial_levels:
            for idx, level in enumerate(sorted(partial_levels, key=lambda x: x['target'])):
                target = level['target'] * tp_mult  # 时段调整止盈目标
                ratio = level['ratio']
                if pnl_pct >= target:
                    priority = 8 + idx
                    return ExitDecision(
                        should_exit=True,
                        reason=f"分批止盈第{idx+1}目标: +{target:.1%}",
                        priority=priority, close_ratio=ratio, is_stop_loss=False
                    )

        # 固定止盈（全平）
        take_profit = self.get_take_profit() * tp_mult  # 时段调整
        if pnl_pct >= take_profit:
            return ExitDecision(
                should_exit=True, reason=f"固定止盈: +{pnl_pct:.1%}",
                priority=10, close_ratio=1.0, is_stop_loss=False
            )

        return ExitDecision(should_exit=False)


# 全局单例
dynamic_exit_manager = DynamicExitManager()
