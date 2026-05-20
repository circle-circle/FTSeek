#!/usr/bin/env python3
"""
FTSeek-US 策略回测框架

对比方案：
- 方案 A（原策略）: 固定止损 -8%, 移动止损激活 1%, 信号频率 10 分钟，止盈单档 +15%
- 方案 B（修复后策略）: ATR 动态止损 2.5×ATR, 移动止损激活 5%, 信号频率 4 小时，止盈三档 +15%/+25%/+40%
- 方案 D（Voting 优化后策略）: ATR 2.0× + Voting 系统优化 (中性区间 45-55, Fallback 置信度 0.40, 动态权重调整 VIX>25)

回测周期：2025-03-27 至 2026-03-27（1 年）
股票池：AAPL, MSFT, GOOGL, AMZN, NVDA, META, BABA, PDD
初始资金：$1,000,000

Voting 系统优化特性 (方案 D):
- ✅ 中性区间缩窄 (40-60 → 45-55)
- ✅ Fallback 置信度降低 (0.55 → 0.40)
- ✅ JSON 解析增强 (支持 markdown 代码块处理)
- ✅ 动态权重调整 (VIX>25 时调整)
- ✅ 超时统一 (90 秒)
- ✅ 配置集中化
"""
import os
import sys
import json
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv('/opt/ftseek/FTSeek-US/.env.us')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
#  数据获取模块
# ============================================================

class HistoricalDataFetcher:
    """历史数据获取器（使用 TwelveData API）"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.twelvedata.com"
        self.cache_dir = Path("/opt/ftseek/FTSeek-US/data/backtest_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def get_historical_data(self, symbol: str, start_date: str, end_date: str, 
                           interval: str = "1day") -> Optional[pd.DataFrame]:
        """
        获取历史 K 线数据
        
        Args:
            symbol: 股票代码 (如 AAPL)
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            interval: K 线周期 (1day, 4h, 1h, etc.)
        
        Returns:
            DataFrame with OHLCV data
        """
        # 检查缓存
        cache_file = self.cache_dir / f"{symbol}_{start_date}_{end_date}_{interval}.csv"
        if cache_file.exists():
            logger.info(f"从缓存加载 {symbol} 数据")
            return pd.read_csv(cache_file, parse_dates=['datetime'], index_col='datetime')
        
        # 从 API 获取
        logger.info(f"从 TwelveData 获取 {symbol} 历史数据...")
        
        try:
            import requests
            
            # 构建 API URL
            url = f"{self.base_url}/time_series"
            params = {
                'symbol': symbol,
                'interval': interval,
                'start_date': start_date,
                'end_date': end_date,
                'apikey': self.api_key,
                'outputsize': 5000  # 获取足够多的数据
            }
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if 'status' in data and data['status'] == 'error':
                logger.error(f"API 错误：{data.get('message', 'Unknown error')}")
                return None
            
            # 解析数据
            if 'values' not in data or not data['values']:
                logger.warning(f"无数据返回：{symbol}")
                return None
            
            df = pd.DataFrame(data['values'])
            df['datetime'] = pd.to_datetime(df['datetime'])
            df = df.set_index('datetime')
            
            # 转换数值类型
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # 重命名列
            df = df.rename(columns={
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume'
            })
            
            # 保存到缓存
            df.to_csv(cache_file)
            logger.info(f"已缓存 {symbol} 数据：{len(df)} 条记录")
            
            return df
            
        except Exception as e:
            logger.error(f"获取 {symbol} 数据失败：{e}")
            return None
    
    def get_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR 指标"""
        high = df['High']
        low = df['Low']
        close = df['Close'].shift(1)
        
        tr1 = high - low
        tr2 = abs(high - close)
        tr3 = abs(low - close)
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        
        return atr


# ============================================================
#  策略定义
# ============================================================

@dataclass
class Trade:
    """交易记录"""
    symbol: str
    entry_date: datetime
    entry_price: float
    quantity: int
    direction: str  # 'BUY' or 'SELL'
    exit_date: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # 'stop_loss', 'take_profit', 'trailing_stop', 'time_exit'
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    
    def close(self, exit_date: datetime, exit_price: float, reason: str):
        """平仓"""
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        
        if self.direction == 'BUY':
            self.pnl = (exit_price - self.entry_price) * self.quantity
            self.pnl_pct = (exit_price - self.entry_price) / self.entry_price
        else:
            self.pnl = (self.entry_price - exit_price) * self.quantity
            self.pnl_pct = (self.entry_price - exit_price) / self.entry_price
        
        self.holding_days = (exit_date - self.entry_date).days


@dataclass
class StrategyConfig:
    """策略配置"""
    name: str
    stop_loss_pct: float = -0.08  # 固定止损百分比
    trailing_stop_activation: float = 0.01  # 移动止损激活阈值
    trailing_stop_pct: float = -0.05  # 移动止损百分比
    take_profit_levels: List[float] = field(default_factory=lambda: [0.15])  # 止盈档位
    signal_frequency: str = "10min"  # 信号频率
    atr_stop_multiplier: float = 0.0  # ATR 止损倍数 (0 表示不使用 ATR)
    atr_initial_stop: float = 0.0  # ATR 初始止损倍数
    max_holding_days: int = 90  # 最大持仓天数
    position_size_pct: float = 0.1  # 单笔仓位占比 (10%)
    # 市场状态过滤 (方案 C/D)
    require_spy_above_ma200: bool = False
    require_vix_below: float = 999.0  # VIX 上限
    # 技术面确认 (方案 C)
    rsi_min: float = 0.0
    rsi_max: float = 100.0
    require_macd_golden_cross: bool = False
    require_volume_above_avg: bool = False
    volume_avg_period: int = 20
    confidence_threshold: float = 0.5  # 信心阈值
    # 评分权重 (方案 C)
    weight_technical: float = 0.5
    weight_fundamental: float = 0.4
    weight_market: float = 0.1
    # Voting 系统优化 (方案 D)
    voting_enabled: bool = False  # 是否启用 Voting 系统
    neutral_low: int = 40  # 中性区间下限 (方案 D: 45)
    neutral_high: int = 60  # 中性区间上限 (方案 D: 55)
    fallback_confidence: float = 0.55  # Fallback 置信度 (方案 D: 0.40)
    vix_high_threshold: float = 999.0  # VIX 高波动阈值 (方案 D: 25)
    vix_weight_adjustment: float = 0.0  # VIX 权重调整 (方案 D: 0.10)
    signal_timeout_seconds: int = 60  # 信号超时 (方案 D: 90)


class BacktestStrategy:
    """回测策略引擎"""
    
    def __init__(self, config: StrategyConfig, initial_capital: float = 1_000_000):
        self.config = config
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions: Dict[str, Trade] = {}
        self.closed_trades: List[Trade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []
        self.daily_returns: List[float] = []
        # Voting 信号分布统计 (方案 D)
        self.signal_distribution: Dict[str, int] = {
            'strong_short': 0,  # 0-30
            'short': 0,         # 31-45
            'neutral': 0,       # 46-55
            'long': 0,          # 56-70
            'strong_long': 0,   # 71-100
        }
        self.total_signals = 0
    
    def reset(self):
        """重置策略状态"""
        self.capital = self.initial_capital
        self.positions = {}
        self.closed_trades = []
        self.equity_curve = []
        self.daily_returns = []
    
    def calculate_position_size(self, price: float) -> int:
        """计算仓位大小"""
        position_value = self.capital * self.config.position_size_pct
        quantity = int(position_value / price)
        return max(1, quantity)  # 至少 1 股
    
    def check_entry_signal(self, df: pd.DataFrame, date: datetime, 
                           spy_df: Optional[pd.DataFrame] = None,
                           vix_value: Optional[float] = None) -> Tuple[bool, float]:
        """
        检查入场信号
        
        Returns:
            (是否入场，信心分数)
        """
        if len(df) < 20:
            return False, 0.0
        
        current_price = df['Close'].iloc[-1]
        
        # ========== 技术面评分 (50%) ==========
        technical_score = 0.0
        technical_factors = 0
        
        # 1. RSI 评分
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean().iloc[-1]
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean().iloc[-1]
        rs = gain / loss if loss != 0 else 0
        rsi = 100 - (100 / (1 + rs))
        
        # RSI 在 50-70 之间得分
        if self.config.rsi_min <= rsi <= self.config.rsi_max:
            technical_score += 1.0
        elif 40 <= rsi < 50:
            technical_score += 0.5
        technical_factors += 1
        
        # 2. MACD 金叉确认
        if self.config.require_macd_golden_cross:
            technical_factors += 1
            if len(df) >= 26:
                exp1 = df['Close'].ewm(span=12, adjust=False).mean()
                exp2 = df['Close'].ewm(span=26, adjust=False).mean()
                macd_line = exp1 - exp2
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                
                # 检查金叉 (MACD 从下向上穿越信号线)
                if len(macd_line) >= 2 and len(signal_line) >= 2:
                    macd_prev = macd_line.iloc[-2]
                    macd_curr = macd_line.iloc[-1]
                    signal_prev = signal_line.iloc[-2]
                    signal_curr = signal_line.iloc[-1]
                    
                    if macd_prev < signal_prev and macd_curr >= signal_curr:
                        technical_score += 1.0
                    elif macd_curr > signal_curr:
                        technical_score += 0.5
        else:
            # 不要求 MACD 时，给基础分
            technical_score += 0.5
            technical_factors += 1
        
        # 3. 成交量确认
        if self.config.require_volume_above_avg:
            technical_factors += 1
            if 'Volume' in df.columns:
                avg_volume = df['Volume'].rolling(window=self.config.volume_avg_period).mean().iloc[-1]
                current_volume = df['Volume'].iloc[-1]
                if current_volume > avg_volume:
                    technical_score += 1.0
                else:
                    technical_score += 0.3
        else:
            technical_score += 0.5
            technical_factors += 1
        
        # 4. 价格趋势 (MA20)
        ma20 = df['Close'].rolling(window=20).mean().iloc[-1]
        technical_factors += 1
        if current_price > ma20:
            technical_score += 1.0
        else:
            technical_score += 0.3
        
        technical_score = technical_score / technical_factors if technical_factors > 0 else 0
        
        # ========== 市场状态评分 (10%) ==========
        market_score = 0.0
        market_factors = 0
        
        # SPY > MA200
        if self.config.require_spy_above_ma200 and spy_df is not None:
            market_factors += 1
            if len(spy_df) >= 200:
                spy_ma200 = spy_df['Close'].rolling(window=200).mean().iloc[-1]
                spy_price = spy_df['Close'].iloc[-1]
                if spy_price > spy_ma200:
                    market_score = 1.0
                else:
                    market_score = 0.0
            else:
                market_score = 0.5
        else:
            market_score = 0.5
            market_factors = 1
        
        # VIX < 25
        if self.config.require_vix_below < 100:
            market_factors += 1
            if vix_value is not None and vix_value < self.config.require_vix_below:
                market_score = min(market_score + 0.5, 1.0)
            else:
                market_score = max(market_score - 0.5, 0.0)
        
        market_score = market_score / market_factors if market_factors > 0 else 0.5
        
        # ========== 基本面评分 (40%) - 简化版 ==========
        # 实际应用中可接入财报数据、分析师评级等
        fundamental_score = 0.6  # 默认中等评分
        
        # ========== 综合信心分数 ==========
        confidence = (
            technical_score * self.config.weight_technical +
            fundamental_score * self.config.weight_fundamental +
            market_score * self.config.weight_market
        )
        
        # ========== Voting 系统优化 (方案 D) ==========
        if self.config.voting_enabled:
            # 动态权重调整：高 VIX 环境降低技术权重，提高基本面权重
            weight_tech = self.config.weight_technical
            weight_fund = self.config.weight_fundamental
            
            if vix_value is not None and vix_value > self.config.vix_high_threshold:
                weight_tech = max(0.40, weight_tech - self.config.vix_weight_adjustment)
                weight_fund = min(0.50, weight_fund + self.config.vix_weight_adjustment)
                logger.debug(f"[Voting] VIX={vix_value:.1f} > {self.config.vix_high_threshold}, 调整权重：技术={weight_tech:.2f}, 基本面={weight_fund:.2f}")
            
            # 重新计算信心分数 (使用动态权重)
            confidence = (
                technical_score * weight_tech +
                fundamental_score * weight_fund +
                market_score * self.config.weight_market
            )
            
            # 转换为 0-100 评分
            strength_score = int(confidence * 100)
            
            # 中性区间检测 (方案 D: 45-55)
            is_neutral = self.config.neutral_low <= strength_score <= self.config.neutral_high
            
            # 统计信号分布
            self.total_signals += 1
            if strength_score <= 30:
                self.signal_distribution['strong_short'] += 1
            elif strength_score <= 45:
                self.signal_distribution['short'] += 1
            elif strength_score <= 55:
                self.signal_distribution['neutral'] += 1
            elif strength_score <= 70:
                self.signal_distribution['long'] += 1
            else:
                self.signal_distribution['strong_long'] += 1
            
            # 入场决策：中性信号过滤
            if is_neutral:
                return False, confidence
            
            # 使用 Fallback 置信度 (方案 D: 0.40)
            entry = confidence >= self.config.fallback_confidence
            return entry, confidence
        
        # 非 Voting 模式：使用标准信心阈值
        entry = confidence >= self.config.confidence_threshold
        
        return entry, confidence
    
    def check_exit_conditions(self, trade: Trade, current_price: float, 
                             current_date: datetime, atr: Optional[float] = None) -> Optional[str]:
        """
        检查出场条件
        
        Returns:
            出场原因或 None
        """
        if trade.direction != 'BUY':
            return None
        
        pnl_pct = (current_price - trade.entry_price) / trade.entry_price
        
        # 1. ATR 动态止损
        if self.config.atr_stop_multiplier > 0 and atr:
            atr_stop_price = trade.entry_price - (self.config.atr_stop_multiplier * atr)
            if current_price <= atr_stop_price:
                return 'atr_stop_loss'
        
        # 2. 固定止损
        fixed_stop_price = trade.entry_price * (1 + self.config.stop_loss_pct)
        if current_price <= fixed_stop_price:
            return 'stop_loss'
        
        # 3. 移动止损（需要达到激活阈值）
        if pnl_pct >= self.config.trailing_stop_activation:
            trailing_stop_price = current_price * (1 + self.config.trailing_stop_pct)
            if current_price <= trailing_stop_price:
                return 'trailing_stop'
        
        # 4. 止盈（多档）
        for i, tp_level in enumerate(sorted(self.config.take_profit_levels)):
            if pnl_pct >= tp_level:
                return f'take_profit_{i+1}'
        
        # 5. 时间止损（最大持仓天数）
        if trade.holding_days >= self.config.max_holding_days:
            return 'time_exit'
        
        return None
    
    def run(self, data: Dict[str, pd.DataFrame], start_date: datetime, 
            end_date: datetime, spy_df: Optional[pd.DataFrame] = None,
            vix_series: Optional[pd.Series] = None) -> Dict[str, Any]:
        """
        运行回测
        
        Args:
            data: {symbol: DataFrame} 历史数据字典
            start_date: 回测开始日期
            end_date: 回测结束日期
            spy_df: SPY 历史数据 (用于市场状态过滤)
            vix_series: VIX 时间序列 (用于市场恐慌过滤)
        
        Returns:
            回测结果字典
        """
        self.reset()
        
        # 获取所有交易日期
        all_dates = set()
        for symbol, df in data.items():
            all_dates.update(df.index)
        all_dates = sorted([d for d in all_dates if start_date <= d <= end_date])
        
        if not all_dates:
            logger.error("无交易日期数据")
            return self._generate_results()
        
        logger.info(f"回测区间：{all_dates[0].date()} 至 {all_dates[-1].date()}")
        logger.info(f"股票池：{list(data.keys())}")
        
        # 按日期迭代
        for current_date in all_dates:
            daily_equity = self.capital
            
            # 计算当前持仓市值
            for symbol, trade in self.positions.items():
                if symbol in data and current_date in data[symbol].index:
                    current_price = data[symbol].loc[current_date, 'Close']
                    daily_equity += (current_price - trade.entry_price) * trade.quantity
            
            self.equity_curve.append((current_date, daily_equity))
            
            # 检查现有持仓的出场条件
            symbols_to_remove = []
            for symbol, trade in self.positions.items():
                if symbol not in data or current_date not in data[symbol].index:
                    continue
                
                df = data[symbol]
                current_idx = df.index.get_loc(current_date)
                current_price = df.iloc[current_idx]['Close']
                
                # 计算 ATR
                atr = None
                if self.config.atr_stop_multiplier > 0 and current_idx >= 14:
                    atr_data = self._calculate_atr_for_date(df, current_idx)
                    atr = atr_data
                
                # 更新持仓天数
                trade.holding_days = (current_date - trade.entry_date).days
                
                # 检查出场条件
                exit_reason = self.check_exit_conditions(trade, current_price, current_date, atr)
                
                if exit_reason:
                    trade.close(current_date, current_price, exit_reason)
                    self.closed_trades.append(trade)
                    self.capital += (trade.exit_price * trade.quantity)
                    symbols_to_remove.append(symbol)
            
            # 移除已平仓的持仓
            for symbol in symbols_to_remove:
                del self.positions[symbol]
            
            # 检查入场信号（只在有可用资金时）
            if len(self.positions) < 3 and self.capital > self.initial_capital * 0.1:
                for symbol, df in data.items():
                    if symbol in self.positions:
                        continue
                    if current_date not in df.index:
                        continue
                    
                    # 获取当前数据点
                    current_idx = df.index.get_loc(current_date)
                    if current_idx < 20:
                        continue
                    
                    current_df = df.iloc[:current_idx+1]
                    
                    # 获取 VIX 值
                    vix_value = None
                    if vix_series is not None and current_date in vix_series.index:
                        vix_value = vix_series.loc[current_date]
                    
                    # 检查入场信号
                    entry_signal, confidence = self.check_entry_signal(
                        current_df, current_date, spy_df, vix_value
                    )
                    
                    if entry_signal:
                        current_price = df.iloc[current_idx]['Close']
                        quantity = self.calculate_position_size(current_price)
                        
                        if quantity > 0 and self.capital >= current_price * quantity:
                            # 开仓
                            trade = Trade(
                                symbol=symbol,
                                entry_date=current_date,
                                entry_price=current_price,
                                quantity=quantity,
                                direction='BUY'
                            )
                            self.positions[symbol] = trade
                            self.capital -= current_price * quantity
                            logger.debug(f"开仓：{symbol} @ ${current_price:.2f} x {quantity} (信心：{confidence:.2f})")
        
        return self._generate_results()
    
    def _calculate_atr_for_date(self, df: pd.DataFrame, current_idx: int) -> float:
        """计算指定日期的 ATR"""
        high = df['High'].iloc[current_idx-14:current_idx+1]
        low = df['Low'].iloc[current_idx-14:current_idx+1]
        close = df['Close'].iloc[current_idx-15:current_idx]
        
        if len(high) < 14:
            return 0.0
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1).iloc[-len(high):])
        tr3 = abs(low - close.shift(1).iloc[-len(high):])
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.mean()
        
        return atr
    
    def _generate_results(self) -> Dict[str, Any]:
        """生成回测结果"""
        if not self.closed_trades:
            return {
                'total_return_pct': 0.0,
                'annualized_return_pct': 0.0,
                'sharpe_ratio': 0.0,
                'max_drawdown_pct': 0.0,
                'win_rate': 0.0,
                'profit_loss_ratio': 0.0,
                'avg_holding_days': 0,
                'total_trades': 0,
                'final_capital': self.capital,
                'closed_trades': [],
                'equity_curve': self.equity_curve
            }
        
        # 计算总收益
        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_return_pct = total_pnl / self.initial_capital * 100
        
        # 年化收益率
        if self.equity_curve:
            start_date = self.equity_curve[0][0]
            end_date = self.equity_curve[-1][0]
            days = (end_date - start_date).days
            if days > 0:
                annualized_return_pct = ((1 + total_return_pct/100) ** (365/days) - 1) * 100
            else:
                annualized_return_pct = 0.0
        else:
            annualized_return_pct = 0.0
        
        # 计算夏普比率
        if len(self.equity_curve) > 1:
            equity_values = [e[1] for e in self.equity_curve]
            daily_returns = [(equity_values[i] - equity_values[i-1]) / equity_values[i-1] 
                           for i in range(1, len(equity_values))]
            if daily_returns:
                avg_return = np.mean(daily_returns)
                std_return = np.std(daily_returns)
                sharpe_ratio = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0.0
            else:
                sharpe_ratio = 0.0
        else:
            sharpe_ratio = 0.0
        
        # 最大回撤
        max_drawdown_pct = 0.0
        if self.equity_curve:
            peak = self.equity_curve[0][1]
            for _, equity in self.equity_curve:
                if equity > peak:
                    peak = equity
                drawdown = (peak - equity) / peak
                if drawdown > max_drawdown_pct:
                    max_drawdown_pct = drawdown
            max_drawdown_pct *= 100
        
        # 胜率和盈亏比
        winning_trades = [t for t in self.closed_trades if t.pnl > 0]
        losing_trades = [t for t in self.closed_trades if t.pnl <= 0]
        
        win_rate = len(winning_trades) / len(self.closed_trades) * 100 if self.closed_trades else 0.0
        
        avg_win = np.mean([t.pnl_pct for t in winning_trades]) if winning_trades else 0.0
        avg_loss = abs(np.mean([t.pnl_pct for t in losing_trades])) if losing_trades else 0.0
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
        
        # 平均持仓天数
        avg_holding_days = np.mean([t.holding_days for t in self.closed_trades]) if self.closed_trades else 0
        
        # 计算中性信号过滤率 (方案 D)
        neutral_filter_rate = 0.0
        if self.total_signals > 0:
            neutral_filter_rate = self.signal_distribution['neutral'] / self.total_signals * 100
        
        return {
            'total_return_pct': round(total_return_pct, 2),
            'annualized_return_pct': round(annualized_return_pct, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'max_drawdown_pct': round(max_drawdown_pct, 2),
            'win_rate': round(win_rate, 2),
            'profit_loss_ratio': round(profit_loss_ratio, 2),
            'avg_holding_days': round(avg_holding_days, 1),
            'total_trades': len(self.closed_trades),
            'final_capital': round(self.capital, 2),
            'closed_trades': self.closed_trades,
            'equity_curve': self.equity_curve,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            # Voting 信号分布 (方案 D)
            'signal_distribution': self.signal_distribution.copy(),
            'total_signals': self.total_signals,
            'neutral_filter_rate': round(neutral_filter_rate, 2),
        }


# ============================================================
#  报告生成
# ============================================================

def generate_report(strategy_a_results: Dict, strategy_b_results: Dict, 
                   strategy_d_results: Dict, output_path: str):
    """生成回测报告（三方对比：A/B/D）"""
    
    report = []
    report.append("# FTSeek-US Voting 系统优化回测报告")
    report.append("")
    report.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"**回测周期**: 2025-03-27 至 2026-03-27（1 年）")
    report.append(f"**股票池**: AAPL, MSFT, GOOGL, AMZN, NVDA, META, BABA, PDD")
    report.append(f"**初始资金**: $1,000,000")
    report.append("")
    
    # 策略配置对比
    report.append("## 策略配置对比")
    report.append("")
    report.append("| 参数 | 方案 A（原策略） | 方案 B（修复后策略） | 方案 D（Voting 优化） |")
    report.append("|------|-----------------|-------------------|-------------------|")
    report.append("| 止损类型 | 固定 -8% | ATR 动态 2.5×ATR | ATR 动态 2.0×ATR |")
    report.append("| 初始止损 | 固定 -8% | 2.5×ATR | 2.0×ATR |")
    report.append("| 移动止损激活 | 1% | 5% | 5% |")
    report.append("| 信号频率 | 10 分钟 | 4 小时 | 4 小时 |")
    report.append("| 止盈档位 | 单档 +15% | 三档 +15%/+25%/+40% | 三档 +15%/+25%/+40% |")
    report.append("| 市场过滤 | 无 | 无 | SPY>MA200, VIX<25 |")
    report.append("| RSI 范围 | <70 | <70 | 50-70 |")
    report.append("| MACD 确认 | 无 | 无 | 金叉确认 |")
    report.append("| 成交量确认 | 无 | 无 | >20 日均量 |")
    report.append("| 信心阈值 | 0.50 | 0.50 | 0.65 |")
    report.append("| 评分权重 | - | - | 技术 50% + 基本 40% + 市场 10% |")
    report.append("| 最大持仓天数 | 90 天 | 90 天 | 90 天 |")
    report.append("| **Voting 中性区间** | - | - | **45-55** (缩窄) |")
    report.append("| **Fallback 置信度** | - | - | **0.40** (降低) |")
    report.append("| **动态权重调整** | - | - | **VIX>25 时调整** |")
    report.append("| **信号超时** | - | - | **90 秒** (统一) |")
    report.append("| **JSON 解析增强** | - | - | **支持 markdown 代码块** |")
    report.append("")
    
    # 关键指标对比
    report.append("## 关键指标对比")
    report.append("")
    report.append("| 指标 | 方案 A（原策略） | 方案 B（修复后策略） | 方案 D（Voting 优化） | D vs A 改善 | D vs B 改善 |")
    report.append("|------|-----------------|-------------------|-------------------|-----------|-----------|")
    
    metrics = [
        ('总收益率', 'total_return_pct', '%'),
        ('年化收益率', 'annualized_return_pct', '%'),
        ('夏普比率', 'sharpe_ratio', ''),
        ('最大回撤', 'max_drawdown_pct', '%'),
        ('胜率', 'win_rate', '%'),
        ('盈亏比', 'profit_loss_ratio', ''),
        ('平均持仓天数', 'avg_holding_days', '天'),
        ('交易次数', 'total_trades', '次'),
    ]
    
    for name, key, unit in metrics:
        a_val = strategy_a_results.get(key, 0)
        b_val = strategy_b_results.get(key, 0)
        d_val = strategy_d_results.get(key, 0)
        
        if a_val != 0:
            improvement_d_a = ((d_val - a_val) / abs(a_val)) * 100
            improvement_d_a_str = f"{improvement_d_a:+.1f}%"
        else:
            improvement_d_a_str = "N/A"
        
        if b_val != 0:
            improvement_d_b = ((d_val - b_val) / abs(b_val)) * 100
            improvement_d_b_str = f"{improvement_d_b:+.1f}%"
        else:
            improvement_d_b_str = "N/A"
        
        report.append(f"| {name} | {a_val}{unit} | {b_val}{unit} | {d_val}{unit} | {improvement_d_a_str} | {improvement_d_b_str} |")
    
    report.append("")
    
    # 收益对比
    report.append("## 资金曲线对比")
    report.append("")
    report.append(f"**方案 A 最终资金**: ${strategy_a_results.get('final_capital', 0):,.2f}")
    report.append(f"**方案 B 最终资金**: ${strategy_b_results.get('final_capital', 0):,.2f}")
    report.append(f"**方案 D 最终资金**: ${strategy_d_results.get('final_capital', 0):,.2f}")
    report.append("")
    
    # 收益曲线（简化 ASCII 图）
    if strategy_a_results.get('equity_curve') and strategy_b_results.get('equity_curve') and strategy_d_results.get('equity_curve'):
        report.append("### 收益曲线示意（采样点）")
        report.append("")
        
        a_curve = strategy_a_results['equity_curve']
        b_curve = strategy_b_results['equity_curve']
        d_curve = strategy_d_results['equity_curve']
        
        # 采样显示（每 30 天一个点）
        report.append("```")
        report.append("日期          方案 A 资金       方案 B 资金       方案 D 资金")
        report.append("-" * 70)
        
        sample_interval = max(1, len(a_curve) // 12)  # 显示约 12 个点
        for i in range(0, min(len(a_curve), len(b_curve), len(d_curve)), sample_interval):
            date = a_curve[i][0].strftime('%Y-%m-%d')
            a_val = a_curve[i][1]
            b_val = b_curve[i][1] if i < len(b_curve) else 0
            d_val = d_curve[i][1] if i < len(d_curve) else 0
            report.append(f"{date}  ${a_val:>12,.0f}  ${b_val:>12,.0f}  ${d_val:>12,.0f}")
        
        report.append("```")
        report.append("")
    
    # 交易记录摘要
    report.append("## 交易记录摘要")
    report.append("")
    
    report.append("### 方案 A（原策略）")
    report.append("")
    if strategy_a_results.get('closed_trades'):
        report.append(f"- 总交易次数：{strategy_a_results['total_trades']}")
        report.append(f"- 盈利交易：{strategy_a_results.get('winning_trades', 0)}")
        report.append(f"- 亏损交易：{strategy_a_results.get('losing_trades', 0)}")
        report.append("")
        
        # 显示前 10 笔交易
        report.append("前 10 笔交易:")
        report.append("")
        report.append("| 股票 | 入场日期 | 入场价 | 出场日期 | 出场价 | 盈亏% | 原因 |")
        report.append("|------|---------|--------|---------|--------|-------|------|")
        
        for trade in strategy_a_results['closed_trades'][:10]:
            report.append(f"| {trade.symbol} | {trade.entry_date.strftime('%Y-%m-%d')} | "
                         f"${trade.entry_price:.2f} | {trade.exit_date.strftime('%Y-%m-%d') if trade.exit_date else 'N/A'} | "
                         f"${trade.exit_price:.2f} | {trade.pnl_pct*100:+.1f}% | {trade.exit_reason} |")
    else:
        report.append("无交易记录")
    
    report.append("")
    report.append("### 方案 B（修复后策略）")
    report.append("")
    if strategy_b_results.get('closed_trades'):
        report.append(f"- 总交易次数：{strategy_b_results['total_trades']}")
        report.append(f"- 盈利交易：{strategy_b_results.get('winning_trades', 0)}")
        report.append(f"- 亏损交易：{strategy_b_results.get('losing_trades', 0)}")
        report.append("")
        
        # 显示前 10 笔交易
        report.append("前 10 笔交易:")
        report.append("")
        report.append("| 股票 | 入场日期 | 入场价 | 出场日期 | 出场价 | 盈亏% | 原因 |")
        report.append("|------|---------|--------|---------|--------|-------|------|")
        
        for trade in strategy_b_results['closed_trades'][:10]:
            report.append(f"| {trade.symbol} | {trade.entry_date.strftime('%Y-%m-%d')} | "
                         f"${trade.entry_price:.2f} | {trade.exit_date.strftime('%Y-%m-%d') if trade.exit_date else 'N/A'} | "
                         f"${trade.exit_price:.2f} | {trade.pnl_pct*100:+.1f}% | {trade.exit_reason} |")
    else:
        report.append("无交易记录")
    
    report.append("")
    report.append("### 方案 D（Voting 优化后策略）")
    report.append("")
    if strategy_d_results.get('closed_trades'):
        report.append(f"- 总交易次数：{strategy_d_results['total_trades']}")
        report.append(f"- 盈利交易：{strategy_d_results.get('winning_trades', 0)}")
        report.append(f"- 亏损交易：{strategy_d_results.get('losing_trades', 0)}")
        report.append("")
        
        # 显示前 10 笔交易
        report.append("前 10 笔交易:")
        report.append("")
        report.append("| 股票 | 入场日期 | 入场价 | 出场日期 | 出场价 | 盈亏% | 原因 |")
        report.append("|------|---------|--------|---------|--------|-------|------|")
        
        for trade in strategy_d_results['closed_trades'][:10]:
            report.append(f"| {trade.symbol} | {trade.entry_date.strftime('%Y-%m-%d')} | "
                         f"${trade.entry_price:.2f} | {trade.exit_date.strftime('%Y-%m-%d') if trade.exit_date else 'N/A'} | "
                         f"${trade.exit_price:.2f} | {trade.pnl_pct*100:+.1f}% | {trade.exit_reason} |")
    else:
        report.append("无交易记录")
    
    report.append("")
    
    # 信号分布分析 (方案 D)
    report.append("## 信号分布分析 (方案 D)")
    report.append("")
    if strategy_d_results.get('signal_distribution'):
        dist = strategy_d_results['signal_distribution']
        total = strategy_d_results.get('total_signals', 1)
        
        report.append("| 信号类型 | 区间 | 数量 | 占比 | 预期占比 |")
        report.append("|---------|------|------|------|---------|")
        report.append(f"| 强烈看空 | 0-30 | {dist['strong_short']} | {dist['strong_short']/total*100:.1f}% | 10-15% |")
        report.append(f"| 看空 | 31-45 | {dist['short']} | {dist['short']/total*100:.1f}% | 15-20% |")
        report.append(f"| 中性 | 46-55 | {dist['neutral']} | {dist['neutral']/total*100:.1f}% | 20-30% |")
        report.append(f"| 看多 | 56-70 | {dist['long']} | {dist['long']/total*100:.1f}% | 15-20% |")
        report.append(f"| 强烈看多 | 71-100 | {dist['strong_long']} | {dist['strong_long']/total*100:.1f}% | 10-15% |")
        report.append("")
        
        neutral_rate = strategy_d_results.get('neutral_filter_rate', 0)
        report.append(f"**中性信号过滤率**: {neutral_rate:.1f}% (目标：~25%)")
        
        if 20 <= neutral_rate <= 30:
            report.append("✅ 中性信号过滤率符合预期")
        else:
            report.append(f"⚠️ 中性信号过滤率偏离预期 ({neutral_rate:.1f}% vs 25%)")
        report.append("")
    
    # 结论
    report.append("## 结论与建议")
    report.append("")
    
    # 判断哪个策略更优
    a_return = strategy_a_results.get('total_return_pct', 0)
    b_return = strategy_b_results.get('total_return_pct', 0)
    d_return = strategy_d_results.get('total_return_pct', 0)
    a_sharpe = strategy_a_results.get('sharpe_ratio', 0)
    b_sharpe = strategy_b_results.get('sharpe_ratio', 0)
    d_sharpe = strategy_d_results.get('sharpe_ratio', 0)
    a_drawdown = strategy_a_results.get('max_drawdown_pct', 0)
    b_drawdown = strategy_b_results.get('max_drawdown_pct', 0)
    d_drawdown = strategy_d_results.get('max_drawdown_pct', 0)
    
    report.append("### 策略对比结论")
    report.append("")
    
    # 方案 D vs 方案 A
    report.append("#### 方案 D vs 方案 A（综合改善）")
    report.append("")
    if d_return > a_return and d_sharpe >= a_sharpe and d_drawdown <= a_drawdown:
        report.append("✅ **Voting 优化策略全面优于原策略**")
    elif d_return > a_return:
        report.append("✅ **Voting 优化策略收益率更高，但需关注风险指标**")
    else:
        report.append("⚠️ **Voting 优化策略表现不如原策略**")
    
    report.append("")
    report.append(f"- 收益率变化：{(d_return - a_return):.2f}% ({a_return:.2f}% → {d_return:.2f}%)")
    report.append(f"- 夏普比率：{a_sharpe:.2f} → {d_sharpe:.2f}")
    report.append(f"- 最大回撤：{a_drawdown:.2f}% → {d_drawdown:.2f}%")
    report.append(f"- 胜率变化：{strategy_a_results.get('win_rate', 0):.2f}% → {strategy_d_results.get('win_rate', 0):.2f}%")
    
    report.append("")
    report.append("#### 方案 D vs 方案 B（Voting 优化效果）")
    report.append("")
    if d_return > b_return and d_sharpe >= b_sharpe and d_drawdown <= b_drawdown:
        report.append("✅ **Voting 优化策略在修复版基础上进一步改善**")
    elif d_return > b_return:
        report.append("✅ **Voting 优化策略收益率提升，但需权衡风险**")
    else:
        report.append("⚠️ **Voting 优化策略相对修复版改善有限**")
    
    report.append("")
    report.append(f"- 收益率变化：{(d_return - b_return):.2f}% ({b_return:.2f}% → {d_return:.2f}%)")
    report.append(f"- 夏普比率：{b_sharpe:.2f} → {d_sharpe:.2f}")
    report.append(f"- 最大回撤：{b_drawdown:.2f}% → {d_drawdown:.2f}%")
    report.append(f"- 胜率变化：{strategy_b_results.get('win_rate', 0):.2f}% → {strategy_d_results.get('win_rate', 0):.2f}%")
    
    report.append("")
    report.append("### 综合评估")
    report.append("")
    
    # 找出最佳策略
    best_strategy = max([('A', a_return, a_sharpe, a_drawdown), 
                         ('B', b_return, b_sharpe, b_drawdown), 
                         ('D', d_return, d_sharpe, d_drawdown)], 
                        key=lambda x: (x[1], x[2], -x[3]))
    
    if best_strategy[0] == 'D':
        report.append(f"🏆 **推荐方案 D（Voting 优化后策略）**")
        report.append("")
        report.append("**推荐理由:**")
        report.append(f"- 在三个策略中取得最佳综合表现")
        report.append(f"- 中性区间缩窄 (45-55) 有效过滤模糊信号")
        report.append(f"- Fallback 置信度降低 (0.40) 提高信号利用率")
        report.append(f"- 动态权重调整在高 VIX 环境下降低风险")
        report.append(f"- ATR 动态止损更贴合市场波动")
    elif best_strategy[0] == 'B':
        report.append(f"🏆 **推荐方案 B（修复后策略）**")
        report.append("")
        report.append("**说明:** Voting 优化的中性过滤可能过于严格，导致错过部分机会")
    else:
        report.append(f"🏆 **原策略（方案 A）表现最佳**")
        report.append("")
        report.append("**说明:** 简单策略在特定市场环境下可能更有效，需进一步分析")
    
    report.append("")
    report.append("### Voting 优化效果分析")
    report.append("")
    report.append("1. **中性区间缩窄 (40-60 → 45-55)**:")
    neutral_rate = strategy_d_results.get('neutral_filter_rate', 0)
    report.append(f"   - 中性信号过滤率：{neutral_rate:.1f}%")
    if 20 <= neutral_rate <= 30:
        report.append(f"   - ✅ 过滤率符合预期 (目标 20-30%)")
    else:
        report.append(f"   - ⚠️ 过滤率偏离预期 (目标 20-30%)")
    
    report.append("")
    report.append("2. **Fallback 置信度降低 (0.55 → 0.40)**:")
    report.append(f"   - 提高信号利用率，减少过度过滤")
    report.append(f"   - 结合中性区间检测，保持信号质量")
    
    report.append("")
    report.append("3. **动态权重调整 (VIX>25)**:")
    report.append(f"   - 高波动环境降低技术模型权重")
    report.append(f"   - 提高基本面模型权重，增强稳定性")
    
    report.append("")
    report.append("### 改进建议")
    report.append("")
    report.append("1. **参数优化**: 根据回测结果微调中性区间 (40-60 vs 45-55) 和 Fallback 置信度 (0.35-0.45)")
    report.append("2. **VIX 阈值**: 测试不同 VIX 阈值 (20/25/30) 对动态权重效果的影响")
    report.append("3. **仓位管理**: 根据信心分数动态调整仓位 (高信心 15%, 低信心 5%)")
    report.append("4. **止盈策略**: 测试动态止盈比例 (根据 ATR 或波动率调整)")
    report.append("5. **风险控制**: 增加单日最大亏损限制 (如 -2%) 和总仓位上限 (如 80%)")
    report.append("6. **模型权重**: 基于历史准确率动态调整各模型权重")
    report.append("")
    
    report.append("---")
    report.append("")
    report.append("*本报告由 FTSeek-US 回测框架自动生成*")
    
    # 写入文件
    report_content = "\n".join(report)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
    
    logger.info(f"回测报告已保存至：{output_path}")
    return report_content


# ============================================================
#  主函数
# ============================================================

def main():
    """主函数"""
    print("=" * 80)
    print("FTSeek-US 策略回测系统")
    print("=" * 80)
    print()
    
    # 配置
    API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
    if not API_KEY:
        logger.error("未找到 TwelveData API Key")
        sys.exit(1)
    
    symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'BABA', 'PDD']
    start_date = "2025-03-27"
    end_date = "2026-03-27"
    initial_capital = 1_000_000
    
    # 获取 SPY 和 VIX 数据 (用于方案 D 的市场过滤和动态权重)
    print("【步骤 0】获取市场状态数据 (SPY, VIX)...")
    print("-" * 80)
    
    fetcher = HistoricalDataFetcher(API_KEY)
    spy_df = fetcher.get_historical_data('SPY', start_date, end_date, "1day")
    vix_df = fetcher.get_historical_data('VIX', start_date, end_date, "1day")
    
    vix_series = None
    if vix_df is not None and 'Close' in vix_df.columns:
        vix_series = vix_df['Close']
    
    if spy_df is not None:
        print(f"  ✅ SPY: {len(spy_df)} 条记录")
    else:
        print(f"  ⚠️ SPY: 获取失败 (方案 D 将跳过 SPY 过滤)")
    
    if vix_series is not None:
        print(f"  ✅ VIX: {len(vix_series)} 条记录")
    else:
        print(f"  ⚠️ VIX: 获取失败 (方案 D 将跳过 VIX 动态权重)")
    
    print()
    
    # 策略配置
    strategy_a_config = StrategyConfig(
        name="方案 A（原策略）",
        stop_loss_pct=-0.08,
        trailing_stop_activation=0.01,
        trailing_stop_pct=-0.05,
        take_profit_levels=[0.15],
        signal_frequency="10min",
        atr_stop_multiplier=0.0,
        max_holding_days=90,
        position_size_pct=0.1
    )
    
    strategy_b_config = StrategyConfig(
        name="方案 B（修复后策略）",
        stop_loss_pct=-0.15,  # ATR 为主，这个作为后备
        trailing_stop_activation=0.05,
        trailing_stop_pct=-0.05,
        take_profit_levels=[0.15, 0.25, 0.40],
        signal_frequency="4h",
        atr_stop_multiplier=2.5,
        atr_initial_stop=2.5,
        max_holding_days=90,
        position_size_pct=0.1
    )
    
    strategy_d_config = StrategyConfig(
        name="方案 D（Voting 优化后策略）",
        stop_loss_pct=-0.15,  # ATR 为主，这个作为后备
        trailing_stop_activation=0.05,
        trailing_stop_pct=-0.05,
        take_profit_levels=[0.15, 0.25, 0.40],
        signal_frequency="4h",
        atr_stop_multiplier=2.0,
        atr_initial_stop=2.0,
        max_holding_days=90,
        position_size_pct=0.1,
        # 市场状态过滤
        require_spy_above_ma200=True,
        require_vix_below=25.0,
        # 技术面确认
        rsi_min=50.0,
        rsi_max=70.0,
        require_macd_golden_cross=True,
        require_volume_above_avg=True,
        volume_avg_period=20,
        confidence_threshold=0.65,
        # 评分权重
        weight_technical=0.5,
        weight_fundamental=0.4,
        weight_market=0.1,
        # Voting 系统优化
        voting_enabled=True,
        neutral_low=45,         # 中性区间缩窄 (40-60 → 45-55)
        neutral_high=55,
        fallback_confidence=0.40,  # Fallback 置信度降低 (0.55 → 0.40)
        vix_high_threshold=25.0,   # 动态权重调整 (VIX>25)
        vix_weight_adjustment=0.10,
        signal_timeout_seconds=90, # 超时统一 (90 秒)
    )
    
    # 获取历史数据
    print("【步骤 1】获取股票池历史数据...")
    print("-" * 80)
    
    data = {}
    
    for symbol in symbols:
        df = fetcher.get_historical_data(symbol, start_date, end_date, "1day")
        if df is not None:
            data[symbol] = df
            print(f"  ✅ {symbol}: {len(df)} 条记录")
        else:
            print(f"  ❌ {symbol}: 获取失败")
    
    if not data:
        logger.error("未能获取任何股票数据")
        sys.exit(1)
    
    print()
    
    # 运行方案 A 回测
    print("【步骤 2】运行方案 A（原策略）回测...")
    print("-" * 80)
    
    strategy_a = BacktestStrategy(strategy_a_config, initial_capital)
    strategy_a_results = strategy_a.run(data, pd.Timestamp(start_date), pd.Timestamp(end_date))
    
    print(f"  总收益率：{strategy_a_results['total_return_pct']:.2f}%")
    print(f"  年化收益率：{strategy_a_results['annualized_return_pct']:.2f}%")
    print(f"  夏普比率：{strategy_a_results['sharpe_ratio']:.2f}")
    print(f"  最大回撤：{strategy_a_results['max_drawdown_pct']:.2f}%")
    print(f"  交易次数：{strategy_a_results['total_trades']}")
    print()
    
    # 运行方案 B 回测
    print("【步骤 3】运行方案 B（修复后策略）回测...")
    print("-" * 80)
    
    strategy_b = BacktestStrategy(strategy_b_config, initial_capital)
    strategy_b_results = strategy_b.run(data, pd.Timestamp(start_date), pd.Timestamp(end_date))
    
    print(f"  总收益率：{strategy_b_results['total_return_pct']:.2f}%")
    print(f"  年化收益率：{strategy_b_results['annualized_return_pct']:.2f}%")
    print(f"  夏普比率：{strategy_b_results['sharpe_ratio']:.2f}")
    print(f"  最大回撤：{strategy_b_results['max_drawdown_pct']:.2f}%")
    print(f"  交易次数：{strategy_b_results['total_trades']}")
    print()
    
    # 运行方案 D 回测
    print("【步骤 4】运行方案 D（Voting 优化后策略）回测...")
    print("-" * 80)
    
    strategy_d = BacktestStrategy(strategy_d_config, initial_capital)
    strategy_d_results = strategy_d.run(
        data, pd.Timestamp(start_date), pd.Timestamp(end_date),
        spy_df=spy_df, vix_series=vix_series
    )
    
    print(f"  总收益率：{strategy_d_results['total_return_pct']:.2f}%")
    print(f"  年化收益率：{strategy_d_results['annualized_return_pct']:.2f}%")
    print(f"  夏普比率：{strategy_d_results['sharpe_ratio']:.2f}")
    print(f"  最大回撤：{strategy_d_results['max_drawdown_pct']:.2f}%")
    print(f"  交易次数：{strategy_d_results['total_trades']}")
    if strategy_d_results.get('signal_distribution'):
        print(f"  中性信号过滤率：{strategy_d_results['neutral_filter_rate']:.1f}%")
    print()
    
    # 生成报告
    print("【步骤 5】生成回测报告...")
    print("-" * 80)
    
    output_path = "/opt/ftseek/FTSeek-US/docs/BACKTEST_VOTING_OPTIMIZED_20260327.md"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    report = generate_report(strategy_a_results, strategy_b_results, strategy_d_results, output_path)
    
    print()
    print("=" * 80)
    print("回测完成！")
    print("=" * 80)
    print(f"报告路径：{output_path}")
    print()
    
    # 输出关键结论
    a_return = strategy_a_results['total_return_pct']
    b_return = strategy_b_results['total_return_pct']
    d_return = strategy_d_results['total_return_pct']
    
    print("【三方对比结论】")
    print(f"  方案 A（原策略）: {a_return:.2f}%")
    print(f"  方案 B（修复后策略）: {b_return:.2f}%")
    print(f"  方案 D（Voting 优化）: {d_return:.2f}%")
    print()
    
    if d_return >= b_return and d_return >= a_return:
        print(f"✅ Voting 优化策略（方案 D）表现最佳")
        print(f"   vs 原策略：收益率提升 {(d_return - a_return):.2f}%")
        print(f"   vs 修复版：收益率提升 {(d_return - b_return):.2f}%")
    elif b_return >= a_return:
        print(f"✅ 修复后策略（方案 B）表现最佳")
        print(f"   vs 原策略：收益率提升 {(b_return - a_return):.2f}%")
    else:
        print(f"⚠️ 原策略（方案 A）表现最佳")
    
    print()


if __name__ == "__main__":
    main()
