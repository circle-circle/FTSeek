"""
回测引擎 (BacktestEngine)

借鉴 freqtrade + Qlib 的设计理念，提供事件驱动的回测框架。

特性:
1. 支持 StandardStrategy 和 BaseStrategy
2. 模拟交易执行 (考虑滑点、手续费)
3. 详细绩效指标 (Sharpe, Sortino, Max DD, Win Rate 等)
4. 参数网格搜索优化
5. Walk-Forward 验证
6. 结果可视化数据输出

架构:
  OHLCV 数据 → 策略分析 → 信号生成 → 模拟执行 → 绩效计算 → 报告

更新时间: 2026-04-13
"""

import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
import pandas as pd
import numpy as np

logger = logging.getLogger("ftseek.backtest")


# ── 数据模型 ──────────────────────────────────────────────────────

@dataclass
class Trade:
    """单笔交易记录"""
    trade_id: str
    pair: str
    enter_date: str
    exit_date: str
    enter_price: float
    exit_price: float
    amount: float
    direction: str  # 'long' or 'short'
    exit_reason: str  # 'stop_loss', 'take_profit', 'signal', 'trailing', 'custom'
    profit_pct: float
    profit_abs: float
    duration_bars: int
    max_profit_pct: float  # 持仓期间最大盈利
    max_drawdown_pct: float  # 持仓期间最大回撤
    stoploss: float
    fee_pct: float = 0.001  # 手续费 0.1%


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str
    start_date: str
    end_date: str
    timeframe: str
    initial_capital: float
    final_capital: float
    
    # 绩效指标
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration: int  # bars
    win_rate: float
    profit_factor: float
    expectancy: float  # 平均每笔盈利
    total_trades: int
    winning_trades: int
    losing_trades: int
    
    # 交易详情
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    daily_returns: List[float] = field(default_factory=list)
    
    # 新增：三重屏障标注指标
    quality_score: float = 0.0
    barrier_stats: Dict = field(default_factory=dict)
    
    # 参数
    params: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "strategy": self.strategy_name,
            "period": f"{self.start_date} to {self.end_date}",
            "timeframe": self.timeframe,
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "total_return_pct": f"{self.total_return_pct:.2%}",
            "annualized_return_pct": f"{self.annualized_return_pct:.2%}",
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "sortino_ratio": round(self.sortino_ratio, 3),
            "calmar_ratio": round(self.calmar_ratio, 3),
            "max_drawdown_pct": f"{self.max_drawdown_pct:.2%}",
            "max_drawdown_duration": self.max_drawdown_duration,
            "win_rate": f"{self.win_rate:.2%}",
            "profit_factor": round(self.profit_factor, 3),
            "expectancy": round(self.expectancy, 4),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "params": self.params,
        }
    
    def summary(self) -> str:
        """生成摘要文本"""
        lines = [
            f"📊 回测结果: {self.strategy_name}",
            f"{'=' * 50}",
            f"📅 周期: {self.start_date} 至 {self.end_date} ({self.timeframe})",
            f"💰 初始资金: ${self.initial_capital:,.2f} → 最终: ${self.final_capital:,.2f}",
            f"📈 总收益率: {self.total_return_pct:.2%}",
            f"📊 年化收益: {self.annualized_return_pct:.2%}",
            f"",
            f"⚡ 风险指标:",
            f"  Sharpe Ratio:  {self.sharpe_ratio:.3f}",
            f"  Sortino Ratio: {self.sortino_ratio:.3f}",
            f"  Calmar Ratio:  {self.calmar_ratio:.3f}",
            f"  最大回撤:      {self.max_drawdown_pct:.2%} ({self.max_drawdown_duration} bars)",
            f"",
            f"📋 交易统计:",
            f"  总交易数:  {self.total_trades}",
            f"  胜率:      {self.win_rate:.2%} ({self.winning_trades}W / {self.losing_trades}L)",
            f"  盈亏比:    {self.profit_factor:.2f}",
            f"  期望值:    {self.expectancy:.4f}",
        ]
        
        if self.params:
            lines.append(f"")
            lines.append(f"⚙️ 参数: {self.params}")
        
        return "\n".join(lines)


# ── 回测引擎 ──────────────────────────────────────────────────────

class BacktestEngine:
    """
    事件驱动回测引擎
    
    支持:
    - StandardStrategy (DataFrame 风格)
    - BaseStrategy (回调风格)
    - 参数网格搜索
    - Walk-Forward 验证
    """
    
    def __init__(
        self,
        initial_capital: float = 1_000_000,
        fee_pct: float = 0.001,      # 手续费 0.1%
        slippage_pct: float = 0.001, # 滑点 0.1%
        max_open_trades: int = 3,
        position_size_pct: float = 0.10,  # 每笔仓位 10%
    ):
        self.initial_capital = initial_capital
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.max_open_trades = max_open_trades
        self.position_size_pct = position_size_pct
    
    def run(
        self,
        strategy,
        dataframe: pd.DataFrame,
        pair: str = "UNKNOWN",
        start_date: str = None,
        end_date: str = None,
        strategy_params: Dict = None,
    ) -> BacktestResult:
        """
        运行单策略回测
        
        Args:
            strategy: StandardStrategy 实例
            dataframe: OHLCV DataFrame
            pair: 交易对
            start_date: 开始日期 (可选)
            end_date: 结束日期 (可选)
            strategy_params: 策略参数覆盖
        
        Returns:
            BacktestResult
        """
        # 数据预处理
        df = self._prepare_data(dataframe, start_date, end_date)
        
        if df.empty:
            logger.warning(f"[Backtest] 数据为空，跳过回测")
            return self._empty_result(strategy, df, pair)
        
        # 应用策略参数
        if strategy_params:
            for k, v in strategy_params.items():
                setattr(strategy, k, v)
        
        logger.info(
            f"[Backtest] 开始回测: {strategy.name} | "
            f"{pair} | {len(df)} bars | {df.index[0]} 至 {df.index[-1]}"
        )
        
        # 策略分析
        if hasattr(strategy, 'analyze_pair'):
            # StandardStrategy
            df = strategy.analyze_pair(df, {'pair': pair, 'timeframe': strategy.timeframe})
        else:
            logger.error(f"[Backtest] 不支持的策略类型: {type(strategy)}")
            return self._empty_result(strategy, df, pair)
        
        # 模拟交易
        trades, equity_curve, daily_returns = self._simulate_trades(
            df, strategy, pair
        )
        
        # 2. 计算绩效
        result = self._calculate_metrics(
            strategy=strategy,
            df=df,
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            pair=pair,
            params=strategy_params or {},
        )
        
        # 2.1 三重屏障标注 (新增)
        try:
            from app.factors.triple_barrier import TripleBarrierLabeler
            labeler = TripleBarrierLabeler(pt_sl=(1.5, 1.0), t_exp_bars=24) # 1.5倍止盈, 1倍止损, 24根K线
            atr = df.get('atr', None)
            
            labeled_trades = labeler.label_trades(trades, df, atr)
            if not labeled_trades.empty:
                result.quality_score = labeler.get_quality_score(labeled_trades)
                result.barrier_stats = labeler.get_barrier_stats(labeled_trades)
            else:
                result.quality_score = 0.0
                result.barrier_stats = {}
        except Exception as e:
            logger.warning(f"[Backtest] 三重屏障标注失败: {e}")
            result.quality_score = 0.0
            result.barrier_stats = {}
        
        logger.info(f"[Backtest] 回测完成: {result.summary()}")
        return result
    
    def optimize(
        self,
        strategy,
        dataframe: pd.DataFrame,
        param_grid: Dict[str, List] = None,
        pair: str = "UNKNOWN",
        metric: str = "sharpe_ratio",
        maximize: bool = True,
    ) -> Dict:
        """
        参数网格搜索优化
        
        Args:
            strategy: 策略实例
            dataframe: OHLCV 数据
            param_grid: 参数网格 {'stoploss': [-0.02, -0.03, -0.04], ...}
            pair: 交易对
            metric: 优化目标指标
            maximize: 是否最大化
        
        Returns:
            {
                'best_params': {...},
                'best_result': BacktestResult,
                'all_results': [...],
                'optimization_time': float,
            }
        """
        # 使用策略定义的参数网格 (如果未提供)
        if param_grid is None:
            if hasattr(strategy, 'get_param_grid'):
                param_grid = strategy.get_param_grid()
            else:
                param_grid = {}
        
        if not param_grid:
            logger.warning(f"[Optimize] 无参数网格，跳过优化")
            return {"best_params": {}, "best_result": None, "all_results": []}
        
        start_time = time.time()
        
        # 生成参数组合
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(product(*values))
        
        logger.info(
            f"[Optimize] 开始参数优化: {strategy.name} | "
            f"{len(combinations)} 组参数 | 目标: {metric}"
        )
        
        best_score = -float('inf') if maximize else float('inf')
        best_result = None
        best_params = None
        all_results = []
        
        for i, combo in enumerate(combinations, 1):
            params = dict(zip(keys, combo))
            
            # 创建新策略实例 (避免状态污染)
            strategy_copy = type(strategy)(config=getattr(strategy, 'config', {}))
            
            try:
                result = self.run(strategy_copy, dataframe, pair, strategy_params=params)
                
                score = result.to_dict().get(metric, 0)
                
                all_results.append({
                    "params": params,
                    "score": score,
                    "metrics": result.to_dict(),
                })
                
                # 更新最优
                if maximize and score > best_score:
                    best_score = score
                    best_result = result
                    best_params = params
                elif not maximize and score < best_score:
                    best_score = score
                    best_result = result
                    best_params = params
                
                if i % 10 == 0 or i == len(combinations):
                    logger.info(
                        f"[Optimize] 进度: {i}/{len(combinations)} | "
                        f"当前最优 {metric}: {best_score:.4f}"
                    )
            
            except Exception as e:
                logger.warning(f"[Optimize] 参数组 {i} 失败: {params} - {e}")
                continue
        
        opt_time = time.time() - start_time
        
        logger.info(
            f"[Optimize] 优化完成: {len(all_results)} 组 | "
            f"最优参数: {best_params} | "
            f"最优 {metric}: {best_score:.4f} | "
            f"耗时: {opt_time:.1f}s"
        )
        
        return {
            "best_params": best_params,
            "best_result": best_result,
            "all_results": all_results,
            "optimization_time": opt_time,
            "total_combinations": len(combinations),
        }
    
    def walk_forward(
        self,
        strategy,
        dataframe: pd.DataFrame,
        train_bars: int = 200,
        test_bars: int = 50,
        pair: str = "UNKNOWN",
        param_grid: Dict = None,
    ) -> Dict:
        """
        Walk-Forward 验证
        
        滚动训练/测试，避免过拟合
        
        Args:
            strategy: 策略实例
            dataframe: OHLCV 数据
            train_bars: 训练窗口大小
            test_bars: 测试窗口大小
            pair: 交易对
            param_grid: 参数网格
        
        Returns:
            {
                'results': [...],  # 每个窗口的结果
                'avg_metric': float,
                'stability': float,  # 结果稳定性 (标准差/均值)
            }
        """
        total_bars = len(dataframe)
        results = []
        
        logger.info(
            f"[WalkForward] 开始 Walk-Forward 验证: {strategy.name} | "
            f"train={train_bars}, test={test_bars} | "
            f"窗口数: {(total_bars - train_bars) // test_bars}"
        )
        
        for i in range(0, total_bars - train_bars, test_bars):
            train_start = i
            train_end = i + train_bars
            test_end = min(train_end + test_bars, total_bars)
            
            if test_end - train_end < 10:  # 测试数据太少，跳过
                continue
            
            # 训练集优化
            train_df = dataframe.iloc[train_start:train_end]
            test_df = dataframe.iloc[train_end:test_end]
            
            if param_grid:
                opt_result = self.optimize(
                    strategy, train_df, param_grid, pair
                )
                best_params = opt_result.get('best_params', {})
            else:
                best_params = {}
            
            # 测试集验证
            strategy_copy = type(strategy)(config=getattr(strategy, 'config', {}))
            result = self.run(strategy_copy, test_df, pair, strategy_params=best_params)
            
            results.append({
                "window": i // test_bars + 1,
                "train_period": f"{train_df.index[0]} 至 {train_df.index[-1]}",
                "test_period": f"{test_df.index[0]} 至 {test_df.index[-1]}",
                "params": best_params,
                "sharpe": result.sharpe_ratio,
                "total_return": result.total_return_pct,
                "max_drawdown": result.max_drawdown_pct,
                "win_rate": result.win_rate,
                "trades": result.total_trades,
            })
        
        # 汇总
        if results:
            sharpe_values = [r['sharpe'] for r in results]
            avg_sharpe = np.mean(sharpe_values)
            std_sharpe = np.std(sharpe_values)
            stability = std_sharpe / abs(avg_sharpe) if avg_sharpe != 0 else float('inf')
            
            logger.info(
                f"[WalkForward] 验证完成: {len(results)} 窗口 | "
                f"平均 Sharpe: {avg_sharpe:.3f} ± {std_sharpe:.3f} | "
                f"稳定性: {stability:.3f}"
            )
            
            return {
                "results": results,
                "avg_sharpe": avg_sharpe,
                "stability": stability,
                "num_windows": len(results),
            }
        
        return {"results": [], "avg_sharpe": 0, "stability": float('inf'), "num_windows": 0}
    
    # ── 内部方法 ────────────────────────────────────────────────────
    
    def _prepare_data(
        self, dataframe: pd.DataFrame, 
        start_date: str = None, end_date: str = None
    ) -> pd.DataFrame:
        """准备数据 (过滤日期、排序)"""
        df = dataframe.copy()
        
        # 确保必要列存在
        required = ['open', 'high', 'low', 'close', 'volume']
        for col in required:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")
        
        # 日期过滤
        if start_date:
            df = df[df.index >= start_date]
        if end_date:
            df = df[df.index <= end_date]
        
        return df
    
    def _simulate_trades(
        self, df: pd.DataFrame, strategy, pair: str
    ) -> Tuple[List[Trade], List[float], List[float]]:
        """
        模拟交易执行
        
        Returns:
            (trades, equity_curve, daily_returns)
        """
        trades = []
        equity_curve = [self.initial_capital]
        daily_returns = []
        
        capital = self.initial_capital
        open_position = None
        trade_count = 0
        peak_equity = capital
        
        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_row = df.iloc[i - 1]
            current_price = row['close']
            
            # 更新权益曲线
            if open_position:
                # 计算持仓盈亏
                if open_position['direction'] == 'long':
                    unrealized_pnl = (current_price - open_position['enter_price']) / open_position['enter_price']
                else:
                    unrealized_pnl = (open_position['enter_price'] - current_price) / open_position['enter_price']
                
                current_equity = capital * (1 + open_position['amount_pct'] * unrealized_pnl)
            else:
                current_equity = capital
            
            equity_curve.append(current_equity)
            
            # 计算日收益率
            if len(equity_curve) > 1:
                daily_ret = (equity_curve[-1] - equity_curve[-2]) / equity_curve[-2]
                daily_returns.append(daily_ret)
            
            # 检查出场条件 (已有持仓)
            if open_position:
                exit_reason = self._check_exit(
                    df.iloc[:i+1], open_position, current_price, strategy
                )
                
                if exit_reason:
                    # 执行出场
                    exit_price = current_price * (1 - self.slippage_pct if open_position['direction'] == 'long' else 1 + self.slippage_pct)
                    
                    if open_position['direction'] == 'long':
                        profit_pct = (exit_price - open_position['enter_price']) / open_position['enter_price']
                    else:
                        profit_pct = (open_position['enter_price'] - exit_price) / open_position['enter_price']
                    
                    # 扣除手续费
                    fee = self.fee_pct * 2  # 进出各一次
                    profit_pct -= fee
                    
                    profit_abs = capital * open_position['amount_pct'] * profit_pct
                    capital += profit_abs
                    
                    # 计算持仓期间最大盈利/回撤
                    # (简化处理，实际需要逐 bar 跟踪)
                    
                    trade = Trade(
                        trade_id=f"trade_{trade_count}",
                        pair=pair,
                        enter_date=str(prev_row.name) if hasattr(prev_row, 'name') else str(i-1),
                        exit_date=str(row.name) if hasattr(row, 'name') else str(i),
                        enter_price=open_position['enter_price'],
                        exit_price=exit_price,
                        amount=open_position['amount'],
                        direction=open_position['direction'],
                        exit_reason=exit_reason,
                        profit_pct=profit_pct,
                        profit_abs=profit_abs,
                        duration_bars=i - open_position['enter_bar'],
                        max_profit_pct=profit_pct,  # 简化
                        max_drawdown_pct=min(0, profit_pct),  # 简化
                        stoploss=open_position['stoploss'],
                        fee_pct=self.fee_pct * 2,
                    )
                    trades.append(trade)
                    trade_count += 1
                    
                    # 更新峰值
                    if capital > peak_equity:
                        peak_equity = capital
                    
                    open_position = None
            
            # 检查入场条件 (无持仓)
            if open_position is None and trade_count < self.max_open_trades:
                if self._check_entry(df.iloc[:i+1], strategy):
                    # 执行入场
                    enter_price = current_price * (1 + self.slippage_pct)
                    amount_pct = self.position_size_pct
                    amount = (capital * amount_pct) / enter_price
                    
                    open_position = {
                        'enter_price': enter_price,
                        'enter_bar': i,
                        'amount': amount,
                        'amount_pct': amount_pct,
                        'direction': 'long',  # 简化为只做多
                        'stoploss': getattr(strategy, 'stoploss', -0.03),
                    }
        
        return trades, equity_curve, daily_returns
    
    def _check_entry(self, df: pd.DataFrame, strategy) -> bool:
        """检查入场条件"""
        if df.empty:
            return False
        
        last_row = df.iloc[-1]
        
        # 检查 enter_long 列
        if 'enter_long' in last_row and last_row['enter_long'] == 1:
            # 检查自定义确认
            if hasattr(strategy, 'confirm_trade_entry'):
                return strategy.confirm_trade_entry(
                    df, 'market', 0, last_row['close'], df.index[-1], 'long'
                )
            return True
        
        return False
    
    def _check_exit(
        self, df: pd.DataFrame, position: Dict, 
        current_price: float, strategy
    ) -> Optional[str]:
        """
        检查出场条件
        
        Returns:
            出场原因字符串，或 None
        """
        if not df.empty:
            last_row = df.iloc[-1]
            
            # 1. 止损
            stop_price = position['enter_price'] * (1 + position['stoploss'])
            if current_price <= stop_price:
                return 'stop_loss'
            
            # 2. 出场信号
            if 'exit_long' in last_row and last_row['exit_long'] == 1:
                if hasattr(strategy, 'confirm_trade_exit'):
                    profit_pct = (current_price - position['enter_price']) / position['enter_price']
                    if strategy.confirm_trade_exit(
                        df, 'market', 0, current_price, profit_pct, df.index[-1], 'long', 'signal'
                    ):
                        return 'signal'
                else:
                    return 'signal'
            
            # 3. 追踪止损 (简化)
            if getattr(strategy, 'trailing_stop', False):
                trailing_pct = getattr(strategy, 'trailing_stop_positive', 0.02)
                trailing_stop_price = current_price * (1 - trailing_pct)
                if current_price <= trailing_stop_price and current_price > position['enter_price']:
                    return 'trailing_stop'
        
        return None
    
    def _calculate_metrics(
        self, strategy, df: pd.DataFrame, trades: List[Trade],
        equity_curve: List[float], daily_returns: List[float],
        pair: str, params: Dict
    ) -> BacktestResult:
        """计算绩效指标"""
        initial = self.initial_capital
        final = equity_curve[-1] if equity_curve else initial
        
        # 收益率
        total_return = (final - initial) / initial if initial > 0 else 0
        
        # 年化收益率
        num_bars = len(df)
        bars_per_year = self._bars_per_year(strategy.timeframe)
        years = num_bars / bars_per_year if bars_per_year > 0 else 1
        annualized = (1 + total_return) ** (1 / max(years, 0.01)) - 1
        
        # 夏普比率 (假设无风险利率 2%)
        rf_daily = 0.02 / 252
        if daily_returns and np.std(daily_returns) > 0:
            sharpe = (np.mean(daily_returns) - rf_daily) / np.std(daily_returns) * np.sqrt(252)
        else:
            sharpe = 0
        
        # 索提诺比率
        negative_returns = [r for r in daily_returns if r < 0]
        if negative_returns and np.std(negative_returns) > 0:
            sortino = (np.mean(daily_returns) - rf_daily) / np.std(negative_returns) * np.sqrt(252)
        else:
            sortino = 0
        
        # 最大回撤
        max_dd = 0
        max_dd_duration = 0
        peak = equity_curve[0]
        peak_idx = 0
        for i, eq in enumerate(equity_curve):
            if eq > peak:
                peak = eq
                peak_idx = i
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_duration = i - peak_idx
        
        # Calmar 比率
        calmar = annualized / max_dd if max_dd > 0 else 0
        
        # 交易统计
        winning = [t for t in trades if t.profit_pct > 0]
        losing = [t for t in trades if t.profit_pct <= 0]
        
        win_rate = len(winning) / len(trades) if trades else 0
        
        gross_profit = sum(t.profit_abs for t in winning) if winning else 0
        gross_loss = abs(sum(t.profit_abs for t in losing)) if losing else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        expectancy = np.mean([t.profit_pct for t in trades]) if trades else 0
        
        return BacktestResult(
            strategy_name=strategy.name,
            start_date=str(df.index[0]) if not df.empty else "",
            end_date=str(df.index[-1]) if not df.empty else "",
            timeframe=strategy.timeframe,
            initial_capital=initial,
            final_capital=final,
            total_return_pct=total_return,
            annualized_return_pct=annualized,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd,
            max_drawdown_duration=max_dd_duration,
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            params=params,
        )
    
    def _empty_result(self, strategy, df, pair) -> BacktestResult:
        """生成空结果"""
        return BacktestResult(
            strategy_name=strategy.name,
            start_date=str(df.index[0]) if not df.empty else "",
            end_date=str(df.index[-1]) if not df.empty else "",
            timeframe=strategy.timeframe,
            initial_capital=self.initial_capital,
            final_capital=self.initial_capital,
            total_return_pct=0,
            annualized_return_pct=0,
            sharpe_ratio=0,
            sortino_ratio=0,
            calmar_ratio=0,
            max_drawdown_pct=0,
            max_drawdown_duration=0,
            win_rate=0,
            profit_factor=0,
            expectancy=0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
        )
    
    def _bars_per_year(self, timeframe: str) -> int:
        """计算每年的 bar 数"""
        mapping = {
            "1m": 252 * 390,    # 每分钟
            "5m": 252 * 78,     # 每 5 分钟
            "15m": 252 * 26,    # 每 15 分钟
            "1h": 252 * 6.5,    # 每小时
            "1d": 252,          # 每天
        }
        return mapping.get(timeframe, 252)


# ── 便捷函数 ──────────────────────────────────────────────────────

_engine: Optional[BacktestEngine] = None

def get_backtest_engine(
    initial_capital: float = 1_000_000,
    fee_pct: float = 0.001,
    slippage_pct: float = 0.001,
    max_open_trades: int = 3,
    position_size_pct: float = 0.10,
) -> BacktestEngine:
    """获取回测引擎单例"""
    global _engine
    if _engine is None:
        _engine = BacktestEngine(
            initial_capital=initial_capital,
            fee_pct=fee_pct,
            slippage_pct=slippage_pct,
            max_open_trades=max_open_trades,
            position_size_pct=position_size_pct,
        )
    return _engine
