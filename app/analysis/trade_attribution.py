"""
交易归因引擎 — 每笔平仓后自动生成多维度归因分析

5 个维度:
  1. 方向正确性: 实际价格走势 vs 交易方向
  2. 入场时机: 入场价 vs 持仓期最优价
  3. 出场时机: 出场价 vs 持仓期最优价
  4. 持仓时间: 时长合理性 + 利润分布
  5. 盈亏比: 实际盈亏 vs 最大浮盈/浮亏

输出: 结构化归因结果, 可渲染为文字或卡片
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DimensionResult:
    """单维度归因结果"""
    name: str           # 维度名称
    verdict: str        # ✅/⚠️/❌
    label: str          # 简短结论
    detail: str         # 详细说明
    score: float        # -1.0 到 +1.0


@dataclass
class AttributionResult:
    """完整归因结果"""
    symbol: str
    direction: str          # LONG/SHORT
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str
    holding_minutes: float

    dimensions: List[DimensionResult] = field(default_factory=list)
    summary: str = ""           # 一句话总结
    improvement: str = ""       # 改进建议
    overall_score: float = 0    # -1.0 到 +1.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct * 100, 2),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "holding_minutes": round(self.holding_minutes, 1),
            "dimensions": [
                {"name": d.name, "verdict": d.verdict, "label": d.label, "detail": d.detail, "score": d.score}
                for d in self.dimensions
            ],
            "summary": self.summary,
            "improvement": self.improvement,
            "overall_score": round(self.overall_score, 2),
        }

    def to_text(self) -> str:
        """纯文字渲染 (用于消息推送)"""
        pnl_sign = "+" if self.pnl >= 0 else ""
        lines = [
            f"{'─' * 40}",
            f"📊 {self.symbol} {self.direction} 平仓 {pnl_sign}${self.pnl:.0f} ({self.pnl_pct*100:+.1f}%)",
            f"持仓 {self.holding_minutes:.0f}分钟 | 入场 ${self.entry_price:.2f} → 出场 ${self.exit_price:.2f}",
            f"{'─' * 40}",
        ]
        for d in self.dimensions:
            lines.append(f"{d.verdict} {d.label}: {d.detail}")
        if self.improvement:
            lines.append(f"")
            lines.append(f"💡 {self.improvement}")
        return "\n".join(lines)


def compute_attribution(
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    entry_time: str,
    exit_time: str,
    high_prices: Optional[List[float]] = None,
    low_prices: Optional[List[float]] = None,
) -> AttributionResult:
    """
    计算交易归因

    Args:
        high_prices: 持仓期间每根 K 线的最高价列表
        low_prices: 持仓期间每根 K 线的最低价列表
        如果为 None, 则用 entry/exit 价格近似
    """
    # 基础计算
    if direction == "LONG":
        pnl = (exit_price - entry_price) * quantity
    else:
        pnl = (entry_price - exit_price) * quantity
    pnl_pct = (pnl / (entry_price * quantity)) if entry_price > 0 and quantity > 0 else 0

    # 持仓时间
    holding_minutes = 0
    try:
        t1 = datetime.fromisoformat(entry_time)
        t2 = datetime.fromisoformat(exit_time)
        holding_minutes = (t2 - t1).total_seconds() / 60
    except Exception:
        pass

    result = AttributionResult(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        pnl=pnl,
        pnl_pct=pnl_pct,
        entry_time=entry_time,
        exit_time=exit_time,
        holding_minutes=holding_minutes,
    )

    # 价格范围
    all_highs = high_prices or [max(entry_price, exit_price)]
    all_lows = low_prices or [min(entry_price, exit_price)]
    period_high = max(all_highs)
    period_low = min(all_lows)

    # ── D1: 方向正确性 ──
    if direction == "LONG":
        actual_move = exit_price - entry_price
        best_possible = period_high - entry_price
    else:
        actual_move = entry_price - exit_price
        best_possible = entry_price - period_low

    if actual_move > 0:
        if best_possible > 0 and actual_move / best_possible > 0.7:
            result.dimensions.append(DimensionResult(
                "direction", "✅", "方向正确",
                f"价格{'上涨' if direction == 'LONG' else '下跌'} {abs(actual_move/entry_price*100):.1f}%",
                1.0
            ))
        else:
            result.dimensions.append(DimensionResult(
                "direction", "✅", "方向正确",
                f"价格{'上涨' if direction == 'LONG' else '下跌'} {abs(actual_move/entry_price*100):.1f}%, 但未充分利用",
                0.5
            ))
    else:
        result.dimensions.append(DimensionResult(
            "direction", "❌", "方向错误",
            f"价格反向移动 {abs(actual_move/entry_price*100):.1f}%",
            -1.0
        ))

    # ── D2: 入场时机 ──
    if direction == "LONG":
        best_entry = period_low  # LONG 理想入场 = 最低点
        entry_slippage = entry_price - best_entry
        entry_cost_pct = entry_slippage / entry_price * 100 if entry_price > 0 else 0
    else:
        best_entry = period_high  # SHORT 理想入场 = 最高点
        entry_slippage = best_entry - entry_price
        entry_cost_pct = entry_slippage / entry_price * 100 if entry_price > 0 else 0

    if abs(entry_cost_pct) < 0.5:
        result.dimensions.append(DimensionResult(
            "entry", "✅", "入场精准",
            f"入场 ${entry_price:.2f} 接近最优 ${best_entry:.2f}",
            1.0
        ))
    elif entry_cost_pct < 2.0:
        result.dimensions.append(DimensionResult(
            "entry", "⚠️", "入场偏晚",
            f"入场 ${entry_price:.2f}, 最优 ${best_entry:.2f} (差距 {entry_cost_pct:.1f}%)",
            -0.3
        ))
    else:
        result.dimensions.append(DimensionResult(
            "entry", "⚠️", "入场过晚",
            f"入场 ${entry_price:.2f}, 最优 ${best_entry:.2f} (差距 {entry_cost_pct:.1f}%)",
            -0.7
        ))

    # ── D3: 出场时机 ──
    if direction == "LONG":
        best_exit = period_high  # LONG 理想出场 = 最高点
        exit_slippage = best_exit - exit_price
    else:
        best_exit = period_low  # SHORT 理想出场 = 最低点
        exit_slippage = exit_price - best_exit

    exit_cost_pct = exit_slippage / exit_price * 100 if exit_price > 0 else 0

    if abs(exit_cost_pct) < 0.5:
        result.dimensions.append(DimensionResult(
            "exit", "✅", "出场精准",
            f"出场 ${exit_price:.2f} 接近最优 ${best_exit:.2f}",
            1.0
        ))
    elif exit_cost_pct < 2.0:
        result.dimensions.append(DimensionResult(
            "exit", "⚠️", "出场偏早",
            f"出场 ${exit_price:.2f}, 最优 ${best_exit:.2f} (差距 {exit_cost_pct:.1f}%)",
            -0.3
        ))
    else:
        result.dimensions.append(DimensionResult(
            "exit", "⚠️", "出场过晚",
            f"出场 ${exit_price:.2f}, 最优 ${best_exit:.2f} (差距 {exit_cost_pct:.1f}%)",
            -0.7
        ))

    # ── D4: 持仓时间 ──
    if holding_minutes < 5:
        result.dimensions.append(DimensionResult(
            "duration", "⚠️", "持仓极短",
            f"{holding_minutes:.0f}分钟, 可能是冲动交易",
            -0.5
        ))
    elif holding_minutes < 30:
        result.dimensions.append(DimensionResult(
            "duration", "✅", "短线合理",
            f"{holding_minutes:.0f}分钟",
            0.5
        ))
    elif holding_minutes < 240:
        result.dimensions.append(DimensionResult(
            "duration", "✅", "日内合理",
            f"{holding_minutes:.0f}分钟 ({holding_minutes/60:.1f}小时)",
            0.7
        ))
    else:
        result.dimensions.append(DimensionResult(
            "duration", "⚠️", "持仓偏长",
            f"{holding_minutes:.0f}分钟 ({holding_minutes/60:.1f}小时), 考虑是否有隔夜风险",
            -0.3
        ))

    # ── D5: 盈亏比 (锁利效率) ──
    if direction == "LONG":
        max_profit = (period_high - entry_price) * quantity  # 最大浮盈
        max_loss = (entry_price - period_low) * quantity     # 最大浮亏
    else:
        max_profit = (entry_price - period_low) * quantity
        max_loss = (period_high - entry_price) * quantity

    if max_profit > 0 and pnl > 0:
        lock_ratio = pnl / max_profit  # 锁住了多少利润
        if lock_ratio > 0.7:
            result.dimensions.append(DimensionResult(
                "efficiency", "✅", f"锁利优秀 ({lock_ratio*100:.0f}%)",
                f"最大浮盈 ${max_profit:.0f}, 实际锁利 ${pnl:.0f}",
                0.8
            ))
        elif lock_ratio > 0.4:
            result.dimensions.append(DimensionResult(
                "efficiency", "⚠️", f"利润回吐 ({lock_ratio*100:.0f}%)",
                f"最大浮盈 ${max_profit:.0f}, 实际锁利 ${pnl:.0f}, 回吐 ${(1-lock_ratio)*max_profit:.0f}",
                -0.2
            ))
        else:
            result.dimensions.append(DimensionResult(
                "efficiency", "❌", f"严重回吐 ({lock_ratio*100:.0f}%)",
                f"最大浮盈 ${max_profit:.0f}, 实际锁利 ${pnl:.0f}, 回吐 ${(1-lock_ratio)*max_profit:.0f}",
                -0.8
            ))
    elif pnl < 0 and max_profit > 0:
        result.dimensions.append(DimensionResult(
            "efficiency", "❌", "浮盈变亏损",
            f"曾浮盈 ${max_profit:.0f}, 最终亏损 ${abs(pnl):.0f}",
            -1.0
        ))
    elif pnl < 0:
        if max_loss > 0:
            cut_ratio = abs(pnl) / max_loss  # 亏损控制
            if cut_ratio < 0.5:
                result.dimensions.append(DimensionResult(
                    "efficiency", "✅", f"止损果断 ({cut_ratio*100:.0f}%)",
                    f"最大浮亏 ${max_loss:.0f}, 实际亏损 ${abs(pnl):.0f}",
                    0.5
                ))
            else:
                result.dimensions.append(DimensionResult(
                    "efficiency", "⚠️", f"扛单较久 ({cut_ratio*100:.0f}%)",
                    f"最大浮亏 ${max_loss:.0f}, 实际亏损 ${abs(pnl):.0f}",
                    -0.5
                ))
        else:
            result.dimensions.append(DimensionResult(
                "efficiency", "⚠️", "亏损",
                f"亏损 ${abs(pnl):.0f}",
                -0.5
            ))
    else:
        result.dimensions.append(DimensionResult(
            "efficiency", "⚪", "盈亏持平", "", 0
        ))

    # ── 综合评分 ──
    scores = [d.score for d in result.dimensions]
    result.overall_score = sum(scores) / len(scores) if scores else 0

    # ── 改进建议 ──
    weak_dims = [d for d in result.dimensions if d.score < 0]
    if not weak_dims:
        result.improvement = "执行良好, 继续保持"
    elif len(weak_dims) == 1:
        result.improvement = f"改进: {weak_dims[0].label}"
    else:
        result.improvement = f"改进: {'、'.join(d.label for d in weak_dims[:2])}"

    # ── 总结 ──
    if pnl > 0 and result.overall_score > 0.5:
        result.summary = f"优秀交易: 方向正确 + 执行到位"
    elif pnl > 0:
        result.summary = f"盈利交易, 但执行有提升空间"
    elif pnl < 0 and result.overall_score > -0.3:
        result.summary = f"亏损但控制得当, 方向判断需改进"
    elif pnl < 0:
        result.summary = f"亏损交易, 方向和执行均需反思"
    else:
        result.summary = f"盈亏持平"

    return result


def attribute_from_db(trade_row: dict) -> Optional[AttributionResult]:
    """从 trade_records 行生成归因 (含价格序列获取)"""
    symbol = trade_row.get("symbol", "")
    direction = trade_row.get("direction", "LONG")
    entry_price = float(trade_row.get("entry_price", 0) or 0)
    exit_price = float(trade_row.get("exit_price", 0) or 0)
    quantity = int(trade_row.get("quantity", 0) or 0)
    entry_time = trade_row.get("entry_time", "")
    exit_time = trade_row.get("exit_time", "")

    if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
        return None

    # 尝试获取持仓期间价格序列
    high_prices = None
    low_prices = None
    try:
        from app.data.twelvedata_provider import TwelveDataProvider
        td = TwelveDataProvider()
        ts = td.get_time_series(symbol, outputsize=5)
        if ts and "close_prices" in ts:
            # 用日线近似 (对于日内交易不够精确, 但好过没有)
            high_prices = [max(entry_price, exit_price)]
            low_prices = [min(entry_price, exit_price)]
    except Exception:
        pass

    return compute_attribution(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        entry_time=entry_time,
        exit_time=exit_time,
        high_prices=high_prices,
        low_prices=low_prices,
    )
