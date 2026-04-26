"""
RSI 均值回归策略 (示例 StandardStrategy)

策略逻辑:
- 入场: RSI < 30 (超卖) 且 价格在布林带下轨附近
- 出场: RSI > 50 (回归中性) 或 止损

借鉴 freqtrade 的设计风格，展示 StandardStrategy 的用法。

更新时间: 2026-04-13
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional

from app.strategy.standard_strategy import StandardStrategy

logger = logging.getLogger("ftseek.strategy.rsi_mean_reversion")


class RSIMeanReversion(StandardStrategy):
    """RSI 均值回归策略"""
    
    name = "rsi_mean_reversion"
    version = "1.0.0"
    description = "RSI 超卖买入 + 布林带确认的均值回归策略"
    author = "FTSeek"
    
    # 交易参数
    timeframe = "1h"
    stoploss = -0.05
    trailing_stop = True
    trailing_stop_positive = 0.03
    
    # 策略参数
    rsi_period = 14
    rsi_oversold = 30
    rsi_exit = 50
    bb_period = 20
    bb_std = 2.0
    
    def __init__(self, config: Dict = None):
        super().__init__(config)
        
        # 从配置覆盖参数
        if 'rsi_period' in self.config:
            self.rsi_period = self.config['rsi_period']
        if 'rsi_oversold' in self.config:
            self.rsi_oversold = self.config['rsi_oversold']
        if 'stoploss' in self.config:
            self.stoploss = self.config['stoploss']
    
    def populate_indicators(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """计算 RSI + 布林带"""
        close = df['close']
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        
        avg_gain = gain.rolling(window=self.rsi_period, min_periods=1).mean()
        avg_loss = loss.rolling(window=self.rsi_period, min_periods=1).mean()
        
        rs = avg_gain / avg_loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # 布林带
        sma = close.rolling(window=self.bb_period).mean()
        std = close.rolling(window=self.bb_period).std()
        
        df['bb_upper'] = sma + self.bb_std * std
        df['bb_middle'] = sma
        df['bb_lower'] = sma - self.bb_std * std
        df['bb_pct'] = (close - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])
        
        # 成交量均线
        df['volume_sma'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_sma']
        
        return df
    
    def populate_entry_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """入场条件: RSI 超卖 + 布林带下轨"""
        df['enter_long'] = 0
        
        df.loc[
            (df['rsi'] < self.rsi_oversold) &
            (df['close'] <= df['bb_lower'] * 1.01),  # 接近或低于下轨
            'enter_long'
        ] = 1
        
        return df
    
    def populate_exit_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """出场条件: RSI 回归中性"""
        df['exit_long'] = 0
        
        df.loc[
            (df['rsi'] > self.rsi_exit),
            'exit_long'
        ] = 1
        
        return df
    
    def get_param_grid(self) -> Dict:
        """参数优化网格"""
        return {
            'rsi_period': [10, 14, 18, 21],
            'rsi_oversold': [25, 30, 35],
            'rsi_exit': [45, 50, 55],
            'stoploss': [-0.03, -0.05, -0.07],
            'trailing_stop_positive': [0.02, 0.03, 0.04],
        }


"""
双均线交叉策略 (示例 StandardStrategy)

策略逻辑:
- 入场: 快速 MA 上穿慢速 MA (金叉)
- 出场: 快速 MA 下穿慢速 MA (死叉) 或 止损

更新时间: 2026-04-13
"""


class MACross(StandardStrategy):
    """双均线交叉策略"""
    
    name = "ma_cross"
    version = "1.0.0"
    description = "简单双均线交叉策略"
    author = "FTSeek"
    
    timeframe = "1h"
    stoploss = -0.04
    trailing_stop = False
    
    ma_fast = 10
    ma_slow = 30
    
    def __init__(self, config: Dict = None):
        super().__init__(config)
        
        if 'ma_fast' in self.config:
            self.ma_fast = self.config['ma_fast']
        if 'ma_slow' in self.config:
            self.ma_slow = self.config['ma_slow']
    
    def populate_indicators(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """计算均线"""
        df['ma_fast'] = df['close'].rolling(window=self.ma_fast).mean()
        df['ma_slow'] = df['close'].rolling(window=self.ma_slow).mean()
        
        # 交叉信号
        df['ma_diff'] = df['ma_fast'] - df['ma_slow']
        df['ma_diff_prev'] = df['ma_diff'].shift(1)
        
        return df
    
    def populate_entry_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """金叉入场"""
        df['enter_long'] = 0
        
        df.loc[
            (df['ma_diff'] > 0) & (df['ma_diff_prev'] <= 0),
            'enter_long'
        ] = 1
        
        return df
    
    def populate_exit_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """死叉出场"""
        df['exit_long'] = 0
        
        df.loc[
            (df['ma_diff'] < 0) & (df['ma_diff_prev'] >= 0),
            'exit_long'
        ] = 1
        
        return df
    
    def get_param_grid(self) -> Dict:
        return {
            'ma_fast': [5, 10, 15, 20],
            'ma_slow': [20, 30, 50, 60],
            'stoploss': [-0.03, -0.04, -0.05],
        }


"""
MACD 动量策略 (示例 StandardStrategy)

策略逻辑:
- 入场: MACD 线 上穿 信号线 (金叉) 且 MACD 柱状图为正
- 出场: MACD 线 下穿 信号线 (死叉) 或 止损

更新时间: 2026-04-13
"""


class MACDMomentum(StandardStrategy):
    """MACD 动量策略"""
    
    name = "macd_momentum"
    version = "1.0.0"
    description = "MACD 金叉买入策略"
    author = "FTSeek"
    
    timeframe = "1h"
    stoploss = -0.04
    trailing_stop = True
    trailing_stop_positive = 0.03
    
    macd_fast = 12
    macd_slow = 26
    macd_signal = 9
    
    def __init__(self, config: Dict = None):
        super().__init__(config)
        
        if 'macd_fast' in self.config:
            self.macd_fast = self.config['macd_fast']
        if 'macd_slow' in self.config:
            self.macd_slow = self.config['macd_slow']
    
    def populate_indicators(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """计算 MACD"""
        close = df['close']
        
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=self.macd_signal, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        df['macd_hist_prev'] = df['macd_hist'].shift(1)
        
        return df
    
    def populate_entry_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """MACD 金叉入场"""
        df['enter_long'] = 0
        
        df.loc[
            (df['macd_hist'] > 0) & (df['macd_hist_prev'] <= 0),
            'enter_long'
        ] = 1
        
        return df
    
    def populate_exit_signals(self, df: pd.DataFrame, metadata: Dict) -> pd.DataFrame:
        """MACD 死叉出场"""
        df['exit_long'] = 0
        
        df.loc[
            (df['macd_hist'] < 0) & (df['macd_hist_prev'] >= 0),
            'exit_long'
        ] = 1
        
        return df
    
    def get_param_grid(self) -> Dict:
        return {
            'macd_fast': [8, 12, 16],
            'macd_slow': [20, 26, 30],
            'macd_signal': [7, 9, 11],
            'stoploss': [-0.03, -0.04, -0.05],
        }
