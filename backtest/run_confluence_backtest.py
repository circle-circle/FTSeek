#!/usr/bin/env python3
"""
Confluence V2 真实数据回测脚本

使用 Futu OpenD 获取真实历史 K 线数据，对 ConfluenceStrategyV2 进行回测。
输出: 胜率、盈亏比、夏普比率、最大回撤、Regime-wise 绩效。

用法:
  python3 backtest/run_confluence_backtest.py [--start 2025-10-01] [--end 2026-04-28]
  python3 backtest/run_confluence_backtest.py --codes HK.00700,HK.03690,HK.09988
"""
import argparse
import json
import logging
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logger = logging.getLogger("ftseek.backtest.confluence")

# ── 回测配置 ──────────────────────────────────────────────────────
DEFAULT_CODES = [
    "HK.00700",  # 腾讯
    "HK.09988",  # 阿里巴巴
    "HK.03690",  # 美团
    "HK.00941",  # 中国移动
    "HK.01810",  # 小米
]

BACKTEST_CONFIG = {
    "initial_capital": 1_000_000,
    "commission_rate": 0.0003,     # 佣金 0.03%
    "stamp_duty_rate": 0.0013,     # 印花税 0.13%
    "slippage_rate": 0.001,        # 滑点 0.1%
    "max_position_ratio": 0.08,    # 单票最大 8%
    "stop_loss_pct": 0.03,         # 止损 3%
    "take_profit_pct": 0.10,       # 止盈 10%
    "position_size_pct": 0.08,     # 每笔仓位 8%
}


class Trade:
    """单笔交易"""
    def __init__(self, code, action, price, qty, date, reason=""):
        self.code = code
        self.action = action
        self.price = price
        self.qty = qty
        self.date = date
        self.reason = reason
        self.exit_price = None
        self.exit_date = None
        self.exit_reason = None
        self.pnl_pct = 0.0
        self.pnl_abs = 0.0
        self.max_profit = 0.0
        self.max_loss = 0.0


class ConfluenceBacktester:
    """
    Confluence V2 真实数据回测器

    架构:
      Futu API → 历史K线 → ConfluenceStrategyV2.on_kline() → Signal → 模拟执行 → 绩效计算
    """

    def __init__(self, config=None):
        self.config = {**BACKTEST_CONFIG, **(config or {})}
        self.cash = self.config["initial_capital"]
        self.positions: Dict[str, Trade] = {}  # code → open trade
        self.closed_trades: List[Trade] = []
        self.equity_curve: List[float] = []
        self.daily_values: List[float] = []

    def fetch_history(self, codes: List[str], start: str, end: str) -> Dict[str, List[dict]]:
        """从 Futu OpenD 获取历史 K 线数据"""
        try:
            from futu import OpenQuoteContext, SubType, KLType

            host = os.environ.get("OPEND_HOST", "127.0.0.1")
            port = int(os.environ.get("OPEND_PORT", "11111"))

            quote_ctx = OpenQuoteContext(host=host, port=port)
            all_data = {}

            for code in codes:
                ret, data = quote_ctx.request_history_kline(
                    code,
                    start=start,
                    end=end,
                    ktype=KLType.K_DAY,  # 日K线
                    autype="qfq"  # 前复权
                )

                if ret == 0 and data is not None and not data.empty:
                    klines = []
                    for _, row in data.iterrows():
                        klines.append({
                            "time_key": str(row["time_key"]),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row["volume"]),
                            "turnover": float(row.get("turnover", 0)),
                        })
                    all_data[code] = klines
                    logger.info(f"[数据] {code}: {len(klines)} 根日K线")
                else:
                    logger.warning(f"[数据] {code}: 获取失败或无数据")
                    all_data[code] = []

            quote_ctx.close()
            return all_data

        except ImportError:
            logger.warning("Futu SDK 未安装，使用模拟数据")
            return self._generate_mock_data(codes, start, end)
        except Exception as e:
            logger.warning(f"Futu API 连接失败 ({e})，使用模拟数据")
            return self._generate_mock_data(codes, start, end)

    def _generate_mock_data(self, codes, start, end):
        """生成模拟数据用于离线测试"""
        import random
        random.seed(42)

        base_prices = {
            "HK.00700": 380.0, "HK.09988": 75.0, "HK.03690": 120.0,
            "HK.00941": 65.0, "HK.01810": 25.0,
        }

        all_data = {}
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        for code in codes:
            klines = []
            price = base_prices.get(code, 50.0)
            current = start_dt

            while current <= end_dt:
                # 跳过周末
                if current.weekday() < 5:
                    change = random.gauss(0, 0.02)
                    open_p = price
                    close_p = price * (1 + change)
                    high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.005)))
                    low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.005)))
                    volume = random.randint(5_000_000, 50_000_000)

                    klines.append({
                        "time_key": current.strftime("%Y-%m-%d"),
                        "open": round(open_p, 2),
                        "high": round(high_p, 2),
                        "low": round(low_p, 2),
                        "close": round(close_p, 2),
                        "volume": volume,
                    })
                    price = close_p
                current += timedelta(days=1)

            all_data[code] = klines
            logger.info(f"[模拟数据] {code}: {len(klines)} 根K线")

        return all_data

    def run(self, strategy, all_data: Dict[str, List[dict]]) -> dict:
        """
        运行回测

        Args:
            strategy: ConfluenceStrategyV2 实例
            all_data: {code: [kline, ...]} 按日期已排序
        """
        # 合并所有日期的数据
        date_map = defaultdict(dict)  # date → {code: kline}
        for code, klines in all_data.items():
            for kl in klines:
                date_str = kl["time_key"][:10]
                date_map[date_str][code] = kl

        sorted_dates = sorted(date_map.keys())

        logger.info(f"[回测] 开始 | {len(sorted_dates)} 个交易日 | {len(all_data)} 只股票")

        # 逐日回测 -- Phase 4: 统一管线入口
        for date_str in sorted_dates:
            day_data = date_map[date_str]

            for code, kline in day_data.items():
                # ── Phase 4: 走统一管线 (标准化 + 校验 + 执行) ──────────
                # 旧: signal = strategy.on_kline(code, kline, "K_DAY")
                # 新: pipeline.on_kline() → 自动标准化 + 校验 → 送入 sink
                pipeline.on_kline(code, kline, "K_DAY")
                # 注: pipeline 内部已调用 sink.consume() 执行交易

            # 检查止损/止盈
            self._check_exits(day_data, date_str)

            # 记录权益
            self._record_equity(day_data)

        # 平仓剩余持仓
        self._close_all(sorted_dates[-1] if sorted_dates else "2026-04-28")

        return self._calculate_metrics()

    def _handle_signal(self, signal, date_str):
        """处理策略信号"""
        from app.strategy.base import SignalAction

        code = signal.code
        price = signal.price

        if signal.action == SignalAction.BUY:
            if code not in self.positions:
                # 计算仓位
                position_value = self.cash * self.config["position_size_pct"]
                qty = int(position_value / price)

                if qty > 0 and self.cash >= price * qty:
                    trade = Trade(code, "BUY", price, qty, date_str,
                                  reason=signal.reason)
                    self.positions[code] = trade
                    self.cash -= price * qty * (1 + self.config["commission_rate"] + self.config["slippage_rate"])
                    logger.info(f"[买入] {code} x{qty} @ {price:.2f} | 现金: {self.cash:,.0f}")

        elif signal.action == SignalAction.SELL:
            if code in self.positions:
                self._close_position(code, price, date_str, "signal")

    def _check_exits(self, day_data: dict, date_str: str):
        """检查止损/止盈"""
        for code, trade in list(self.positions.items()):
            if code not in day_data:
                continue

            current_price = day_data[code]["close"]
            pnl_pct = (current_price - trade.price) / trade.price

            # 更新最大盈利/亏损
            trade.max_profit = max(trade.max_profit, pnl_pct)
            trade.max_loss = min(trade.max_loss, pnl_pct)

            # 止损检查
            if pnl_pct <= -self.config["stop_loss_pct"]:
                self._close_position(code, current_price, date_str, "stop_loss")
                continue

            # 止盈检查
            if pnl_pct >= self.config["take_profit_pct"]:
                self._close_position(code, current_price, date_str, "take_profit")

    def _close_position(self, code, price, date_str, reason):
        """平仓"""
        trade = self.positions.pop(code)
        trade.exit_price = price
        trade.exit_date = date_str
        trade.exit_reason = reason

        pnl_pct = (price - trade.price) / trade.price
        trade.pnl_pct = pnl_pct

        # 计算实际盈亏 (扣除手续费)
        sell_value = price * trade.qty
        cost = sell_value * (self.config["commission_rate"] + self.config["stamp_duty_rate"] + self.config["slippage_rate"])
        trade.pnl_abs = pnl_pct * trade.price * trade.qty - cost

        self.cash += sell_value - cost
        self.closed_trades.append(trade)

        logger.info(f"[平仓] {code} {reason} | 盈亏: {pnl_pct:+.2%} | 现金: {self.cash:,.0f}")

    def _close_all(self, date_str):
        """强制平仓"""
        for code in list(self.positions.keys()):
            # 使用最后一根K线的收盘价
            trade = self.positions[code]
            self._close_position(code, trade.price, date_str, "end_of_backtest")

    def _record_equity(self, day_data):
        """记录每日权益"""
        position_value = sum(
            t.qty * day_data.get(t.code, {}).get("close", t.price)
            for t in self.positions.values()
        )
        total = self.cash + position_value
        self.daily_values.append(total)

    def _calculate_metrics(self) -> dict:
        """计算回测指标"""
        initial = self.config["initial_capital"]
        final = self.cash
        total_return = (final - initial) / initial

        trades = self.closed_trades
        sell_trades = [t for t in trades if t.action == "BUY"]  # 完整交易（买入→卖出）

        wins = [t for t in sell_trades if t.pnl_pct > 0]
        losses = [t for t in sell_trades if t.pnl_pct <= 0]

        win_rate = len(wins) / len(sell_trades) if sell_trades else 0
        avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        # 夏普比率 (简化)
        if len(self.daily_values) > 2:
            import statistics
            daily_returns = [
                (self.daily_values[i] - self.daily_values[i - 1]) / self.daily_values[i - 1]
                for i in range(1, len(self.daily_values))
            ]
            if len(daily_returns) > 1:
                mean_ret = statistics.mean(daily_returns)
                std_ret = statistics.stdev(daily_returns)
                sharpe = (mean_ret / (std_ret + 1e-8)) * (252 ** 0.5) if std_ret > 0 else 0
            else:
                sharpe = 0
        else:
            sharpe = 0

        # 最大回撤
        peak = initial
        max_dd = 0
        for v in self.daily_values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

        # 交易详情
        trade_details = []
        for t in sell_trades:
            trade_details.append({
                "code": t.code,
                "entry_date": t.date,
                "exit_date": t.exit_date,
                "entry_price": t.price,
                "exit_price": t.exit_price,
                "qty": t.qty,
                "pnl_pct": round(t.pnl_pct, 4),
                "pnl_abs": round(t.pnl_abs, 2),
                "exit_reason": t.exit_reason,
                "max_profit": round(t.max_profit, 4),
                "max_loss": round(t.max_loss, 4),
            })

        result = {
            "period": {
                "start": sell_trades[0].date if sell_trades else "N/A",
                "end": sell_trades[-1].exit_date if sell_trades else "N/A",
            },
            "capital": {
                "initial": initial,
                "final": round(final, 2),
                "total_return_pct": round(total_return, 4),
            },
            "risk": {
                "sharpe_ratio": round(sharpe, 3),
                "max_drawdown_pct": round(max_dd, 4),
            },
            "trades": {
                "total": len(sell_trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 4),
                "avg_win_pct": round(avg_win, 4),
                "avg_loss_pct": round(avg_loss, 4),
                "profit_factor": round(profit_factor, 3),
            },
            "trade_details": trade_details,
        }

        return result


def main():
    parser = argparse.ArgumentParser(description="Confluence V2 真实数据回测")
    parser.add_argument("--start", default="2025-10-01", help="开始日期")
    parser.add_argument("--end", default="2026-04-28", help="结束日期")
    parser.add_argument("--codes", default=None, help="股票代码，逗号分隔")
    parser.add_argument("--output", default=None, help="输出文件路径")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    codes = args.codes.split(",") if args.codes else DEFAULT_CODES
    start = args.start
    end = args.end

    print(f"\n{'='*60}")
    print(f"  Confluence V2 回测")
    print(f"  股票: {', '.join(codes)}")
    print(f"  期间: {start} → {end}")
    print(f"{'='*60}\n")

    # 初始化策略
    from app.strategy.confluence_v2 import ConfluenceStrategyV2
    from app.strategy.pipeline_adapter import BacktestSink, StrategyPipeline
    from app.strategy.strategy_contract import SignalContract

    strategy = ConfluenceStrategyV2()

    # ── Phase 4: 统一管线入口 (2026-04-28) ──────────────────────────────
    # 策略只负责产生 Signal，pipeline 负责标准化 + 校验 + 送入 sink
    backtester = ConfluenceBacktester()
    sink = BacktestSink(backtester)
    pipeline = StrategyPipeline(strategy, sink, validate=True)

    # 获取数据
    print("→ 获取历史K线数据...")
    all_data = backtester.fetch_history(codes, start, end)

    # 检查是否有数据
    total_klines = sum(len(v) for v in all_data.values())
    if total_klines == 0:
        print("\n❌ 无可用K线数据。请确保 Futu OpenD 正在运行。")
        print("   或使用 --codes 指定其他股票代码。")
        sys.exit(1)

    print(f"→ 获取到 {total_klines} 根K线，开始回测...\n")

    # 运行回测
    result = backtester.run(strategy, all_data)

    # 输出结果
    print(f"\n{'='*60}")
    print(f"  回测结果")
    print(f"{'='*60}")
    print(f"  期间: {result['period']['start']} → {result['period']['end']}")
    print(f"")
    print(f"  资金:")
    print(f"    初始: {result['capital']['initial']:,.0f}")
    print(f"    最终: {result['capital']['final']:,.0f}")
    print(f"    总收益: {result['capital']['total_return_pct']:+.2%}")
    print(f"")
    print(f"  风险:")
    print(f"    夏普比率: {result['risk']['sharpe_ratio']:.3f}")
    print(f"    最大回撤: {result['risk']['max_drawdown_pct']:.2%}")
    print(f"")
    print(f"  交易:")
    print(f"    总笔数: {result['trades']['total']}")
    print(f"    胜率: {result['trades']['win_rate']:.1%} ({result['trades']['wins']}W / {result['trades']['losses']}L)")
    print(f"    平均盈利: {result['trades']['avg_win_pct']:+.2%}")
    print(f"    平均亏损: {result['trades']['avg_loss_pct']:+.2%}")
    print(f"    盈亏比: {result['trades']['profit_factor']:.2f}")
    print(f"{'='*60}")

    if result["trade_details"]:
        print(f"\n  交易明细:")
        print(f"  {'代码':<12} {'买入日':<12} {'卖出日':<12} {'盈亏%':>8} {'原因':<12}")
        print(f"  {'-'*60}")
        for t in result["trade_details"]:
            print(f"  {t['code']:<12} {t['entry_date']:<12} {t['exit_date']:<12} {t['pnl_pct']:>+7.2%} {t['exit_reason']:<12}")

    # 保存结果
    output_path = args.output or f"backtest/confluence_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 将不可序列化的字段转换为可序列化的
    serializable_result = {}
    for k, v in result.items():
        if isinstance(v, dict):
            serializable_result[k] = {kk: vv for kk, vv in v.items()}
        else:
            serializable_result[k] = v

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n→ 结果已保存: {output_path}")

    # 结论
    print(f"\n{'='*60}")
    if result["trades"]["win_rate"] < 0.45:
        print(f"  ⚠️  胜率 {result['trades']['win_rate']:.1%} 低于 45% 阈值")
    if result["trades"]["profit_factor"] < 1.0:
        print(f"  ⚠️  盈亏比 {result['trades']['profit_factor']:.2f} < 1.0 (负期望)")
    if result["risk"]["sharpe_ratio"] < 0:
        print(f"  ⚠️  夏普比率 {result['risk']['sharpe_ratio']:.3f} < 0")
    if result["trades"]["win_rate"] >= 0.45 and result["trades"]["profit_factor"] >= 1.0:
        print(f"  ✅ 策略通过基础门禁")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
