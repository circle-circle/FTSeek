"""
Shadow 交易桥接器 — 将新因子+风控模块接入 Shadow Trading

功能:
1. 因子信号评估 → 入场决策 (FactorAssembly + ConditionEvaluator)
2. ATR 仓位计算 (PositionSizing)
3. 成本感知 Edge 过滤 (CostModel)
4. 入场前风控检查 (DynamicRiskExit.check_entry_allowed)
5. 持仓退出评估 (DynamicRiskExit.evaluate)
6. 交易结果记录 (DynamicRiskExit.record_trade_result)
7. 写入 shadow_trades 表

设计:
- Overlay 模式: 不修改现有 signal_pipeline.py
- 旁路运行: 接收市场数据 → 独立评估 → 写入 shadow_trades
- 可回测: 同一逻辑用于历史回测和实时 shadow

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.factors.assembly import FactorAssembly, StrategyVariantLibrary
from app.factors.registry import FactorRegistry
from app.exit.dynamic_risk import DynamicRiskExit
from app.exit.cost_model import CostModel
from app.exit.position_sizing import PositionSizing
from app.exit.base import ExitProfile

logger = logging.getLogger(__name__)


class ShadowBridge:
    """
    Shadow 交易桥接器

    将因子框架 + 风控模块接入 shadow trading。

    Usage:
        bridge = ShadowBridge(
            strategy_variant="mean_reversion_risk_managed",
            exit_profile_name="mean_reversion_risk_managed",
        )

        # 入场评估
        signal = bridge.evaluate_entry(symbol="US.AAPL", market_data=md)
        if signal and signal["should_trade"]:
            # 记录 shadow 开仓
            bridge.open_shadow_trade(signal)

        # 退出评估 (定期调用)
        decision = bridge.evaluate_exit(position, ctx)
        if decision.action == "CLOSE":
            bridge.close_shadow_trade(trade_id, decision)
    """

    def __init__(
        self,
        strategy_variant: str = "daily_calibrated_mean_reversion",
        exit_profile_name: str = "mean_reversion_risk_managed",
        db_path: str = "data/ftseek_us.db",
        capital: float = 100000.0,
    ):
        self.strategy_variant = strategy_variant
        self.exit_profile_name = exit_profile_name
        self.db_path = db_path
        self.capital = capital

        # 初始化组件
        self._factor_registry = FactorRegistry.instance()
        self._factor_registry.auto_discover()

        self._lib = StrategyVariantLibrary()
        self._lib.load("config/strategy_variants.yaml")

        self._assembly = self._lib.get(strategy_variant)
        if self._assembly is None:
            logger.warning("策略变体 %s 未找到, 使用空 assembly", strategy_variant)

        # 退出模块
        self._risk_exit = DynamicRiskExit()

        # 成本模型
        self._cost_model = CostModel.ibkr()

        # 仓位控制
        self._sizer = PositionSizing(
            capital=capital,
            risk_per_trade_pct=0.02,
            max_position_pct=0.15,
            min_shares=10,
        )

        # Exit profile
        from app.exit.registry import get_exit_registry
        exit_reg = get_exit_registry()
        exit_reg.load_profiles("config/exit_profiles.yaml")
        self._exit_profile = exit_reg.get_profile(exit_profile_name)

        # 风控参数
        self._risk_params = {
            "max_stock_loss_pct": 0.01,
            "max_consecutive_losses": 3,
            "blacklist": ["NVDA", "META"],
            "capital": capital,
        }

        # 当前持仓跟踪
        self._open_positions: Dict[str, Dict] = {}  # trade_id → position info

        logger.info(
            "[ShadowBridge] 初始化: variant=%s, exit=%s, capital=$%s",
            strategy_variant, exit_profile_name, f"{capital:,.0f}",
        )

    def evaluate_entry(
        self,
        symbol: str,
        market_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        评估是否应该入场

        Returns:
            None: 不入场
            Dict: 入场信号 {should_trade, direction, shares, cost, edge, ...}
        """
        if self._assembly is None:
            return None

        # 1. 入场前风控检查
        allowed, reason = self._risk_exit.check_entry_allowed(symbol, self._risk_params)
        if not allowed:
            logger.debug("[ShadowBridge] %s 入场被拒: %s", symbol, reason)
            return None

        # 2. 因子信号评估
        result = self._assembly.evaluate(symbol, market_data, registry=self._factor_registry)
        if not result.passed:
            return None

        # 3. 仓位计算
        price = float(market_data.get("price", 0))
        atr = float(market_data.get("atr_14", 0))
        if price <= 0 or atr <= 0:
            return None

        sizing = self._sizer.calculate(price, atr, self._cost_model)
        if not sizing.should_trade:
            logger.debug("[ShadowBridge] %s 仓位过滤: %s", symbol, sizing.reason)
            return None

        return {
            "should_trade": True,
            "symbol": symbol,
            "direction": result.direction,
            "shares": sizing.shares,
            "price": price,
            "atr": atr,
            "position_value": sizing.position_value,
            "cost_estimate": sizing.cost_estimate,
            "edge_estimate": sizing.edge_estimate,
            "edge_cost_ratio": sizing.edge_cost_ratio,
            "composite_score": result.composite_score,
            "confidence": result.confidence,
            "factor_values": {
                k: v.to_dict() for k, v in result.factor_values.items()
            },
            "conditions": [
                {"condition": c, "passed": p, "reason": r}
                for c, p, r in result.condition_results
            ],
            "strategy_variant": self.strategy_variant,
            "timestamp": datetime.now().isoformat(),
        }

    def evaluate_exit(
        self,
        trade_id: str,
        current_price: float,
        atr: float,
        holding_minutes: float,
    ) -> Optional[Dict[str, Any]]:
        """
        评估是否应该退出

        Args:
            trade_id: shadow trade ID
            current_price: 当前价格
            atr: 当前 ATR
            holding_minutes: 持仓分钟数

        Returns:
            None: 无持仓
            Dict: 退出决策
        """
        pos_info = self._open_positions.get(trade_id)
        if pos_info is None:
            return None

        # 构建 position 对象
        class SimplePos:
            pass
        position = SimplePos()
        position.symbol = pos_info["symbol"]
        position.avg_entry_price = pos_info["entry_price"]
        position.direction = pos_info["direction"]
        position.quantity = pos_info["shares"]

        ctx = {
            "current_price": current_price,
            "atr": atr,
            "holding_minutes": holding_minutes,
        }

        if self._exit_profile is None:
            return None

        decision = self._risk_exit.evaluate(position, ctx, self._exit_profile)

        return {
            "trade_id": trade_id,
            "action": decision.action,
            "reason": decision.reason,
            "qty_pct": decision.qty_pct,
            "priority": decision.priority,
            "metadata": decision.metadata,
            "timestamp": datetime.now().isoformat(),
        }

    def open_shadow_trade(self, signal: Dict[str, Any]) -> str:
        """
        记录 shadow 开仓到 DB

        Returns:
            trade_id
        """
        trade_id = f"shadow_{uuid.uuid4().hex[:12]}"

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO shadow_trades (
                    trade_id, symbol, strategy, direction, signal_score,
                    signal_detail, entry_time, entry_price, quantity,
                    entry_slippage_pct, spread_pct, commission,
                    closed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
            """, (
                trade_id,
                signal["symbol"],
                signal.get("strategy_variant", "factor_bridge"),
                signal["direction"],
                int(signal.get("composite_score", 0) * 100),
                json.dumps({
                    "edge_cost_ratio": signal.get("edge_cost_ratio"),
                    "confidence": signal.get("confidence"),
                    "factors": signal.get("factor_values"),
                }, ensure_ascii=False)[:1000],
                signal["timestamp"],
                signal["price"],
                signal["shares"],
                0,  # entry_slippage_pct (estimated in cost)
                0,  # spread_pct
                signal.get("cost_estimate", 0) / 2,  # half = entry cost
            ))
            conn.commit()
        finally:
            conn.close()

        # Track internally
        self._open_positions[trade_id] = {
            "symbol": signal["symbol"],
            "entry_price": signal["price"],
            "direction": signal["direction"],
            "shares": signal["shares"],
            "entry_time": signal["timestamp"],
        }

        logger.info(
            "[ShadowBridge] 开仓: %s %s %s %d股 @$%.2f (edge=%.2f, cost=$%.0f)",
            trade_id, signal["direction"], signal["symbol"],
            signal["shares"], signal["price"],
            signal.get("edge_estimate", 0), signal.get("cost_estimate", 0),
        )

        return trade_id

    def close_shadow_trade(
        self,
        trade_id: str,
        exit_info: Dict[str, Any],
        exit_price: float,
    ) -> Optional[float]:
        """
        记录 shadow 平仓

        Returns:
            realized_pnl (or None if not found)
        """
        pos_info = self._open_positions.pop(trade_id, None)
        if pos_info is None:
            logger.warning("[ShadowBridge] 未知 trade_id: %s", trade_id)
            return None

        entry_price = pos_info["entry_price"]
        direction = pos_info["direction"]
        shares = pos_info["shares"]

        # Calculate PnL
        if direction == "LONG":
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price

        gross_pnl = pnl_pct * entry_price * shares
        cost = self._cost_model.estimate(
            entry_price, pos_info.get("atr", 3.0), shares
        ).round_trip
        net_pnl = gross_pnl - cost

        # Update DB
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                UPDATE shadow_trades SET
                    exit_time = ?, exit_price = ?, realized_pnl = ?,
                    pnl_pct = ?, exit_reason = ?, commission = ?,
                    closed = 1
                WHERE trade_id = ?
            """, (
                exit_info.get("timestamp", datetime.now().isoformat()),
                exit_price,
                net_pnl,
                pnl_pct,
                exit_info.get("reason", "UNKNOWN"),
                cost,
                trade_id,
            ))
            conn.commit()
        finally:
            conn.close()

        # Record for risk tracking
        self._risk_exit.record_trade_result(
            pos_info["symbol"], net_pnl, self._risk_params
        )

        logger.info(
            "[ShadowBridge] 平仓: %s %s PnL=$%.0f (%.2f%%) reason=%s",
            trade_id, pos_info["symbol"], net_pnl, pnl_pct * 100,
            exit_info.get("reason"),
        )

        return net_pnl

    def get_status(self) -> Dict[str, Any]:
        """获取桥接器状态"""
        return {
            "strategy_variant": self.strategy_variant,
            "exit_profile": self.exit_profile_name,
            "capital": self.capital,
            "open_positions": len(self._open_positions),
            "risk_summary": self._risk_exit.get_risk_summary(),
            "sizer_params": {
                "risk_per_trade_pct": self._sizer.risk_per_trade_pct,
                "max_position_pct": self._sizer.max_position_pct,
                "min_edge_ratio": self._sizer.min_edge_ratio,
            },
        }

    def reset(self):
        """重置所有状态 (新交易日)"""
        self._risk_exit.reset()
        self._open_positions.clear()
        logger.info("[ShadowBridge] 状态已重置")


# For JSON serialization
import json
