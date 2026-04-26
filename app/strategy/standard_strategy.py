"""
标准策略基类 (StandardStrategy)

借鉴 freqtrade 的 IStrategy 设计模式，提供 DataFrame 风格的策略接口。

与现有 BaseStrategy 的关系:
- BaseStrategy: on_quote/on_kline 回调模式 (实时交易使用)
- StandardStrategy: DataFrame 风格 (回测/批量分析使用)
- 两者通过 StrategyAdapter 互相转换

特性:
1. 标准化接口: populate_indicators → populate_entry_signals → populate_exit_signals
2. 策略元数据: name, version, timeframe, stoploss 等
3. 参数优化支持: 所有参数可通过 config 注入
4. 回测兼容: 直接用于 BacktestEngine
5. 向后兼容: 可转换为 BaseStrategy 用于实时交易

更新时间: 2026-04-13
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import logging
import pandas as pd

logger = logging.getLogger("ftseek.strategy.standard")


@dataclass
class StrategyMetadata:
    """策略元数据"""
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    
    # 交易参数
    timeframe: str = "5m"
    can_short: bool = False
    stoploss: float = -0.03
    trailing_stop: bool = True
    trailing_stop_positive: float = 0.02
    trailing_stop_positive_offset: float = 0.025
    
    # 仓位管理
    use_custom_stoploss: bool = False
    use_custom_exit: bool = False
    
    # 信号要求
    min_confidence: float = 0.35          # 2026-04-24 统一
    require_volume: bool = False
    min_volume_ratio: float = 1.5


class StandardStrategy(ABC):
    """
    标准策略基类 (借鉴 freqtrade IStrategy)
    
    所有策略必须实现三个核心方法:
    1. populate_indicators: 计算技术指标
    2. populate_entry_signals: 生成入场信号
    3. populate_exit_signals: 生成出场信号
    
    示例用法:
    ```python
    class MyStrategy(StandardStrategy):
        name = "my_strategy"
        timeframe = "5m"
        stoploss = -0.03
        
        def populate_indicators(self, df, metadata):
            df['rsi'] = ta.RSI(df['close'], timeperiod=14)
            df['macd'], df['macdsignal'], df['macdhist'] = ta.MACD(df['close'])
            return df
        
        def populate_entry_signals(self, df, metadata):
            df.loc[
                (df['rsi'] < 30) & (df['macd'] > df['macdsignal']),
                'enter_long'
            ] = 1
            return df
        
        def populate_exit_signals(self, df, metadata):
            df.loc[
                (df['rsi'] > 70) & (df['macd'] < df['macdsignal']),
                'exit_long'
            ] = 1
            return df
    ```
    """
    
    # 策略元数据 (子类覆盖)
    name: str = "standard_strategy"
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    
    # 交易参数 (子类覆盖)
    timeframe: str = "5m"
    can_short: bool = False
    stoploss: float = -0.03
    trailing_stop: bool = True
    trailing_stop_positive: float = 0.02
    trailing_stop_positive_offset: float = 0.025
    
    # 信号要求
    min_confidence: float = 0.35          # 2026-04-24 统一
    require_volume: bool = False
    min_volume_ratio: float = 1.5
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化策略
        
        Args:
            config: 策略配置字典 (可覆盖类属性)
        """
        self.config = config or {}
        self.metadata = self._build_metadata()
        self._apply_config_overrides()
        
        logger.info(
            f"[{self.name}] 标准策略已初始化 | "
            f"timeframe={self.timeframe} | "
            f"stoploss={self.stoploss:.2%} | "
            f"trailing={self.trailing_stop}"
        )
    
    def _build_metadata(self) -> StrategyMetadata:
        """构建策略元数据"""
        return StrategyMetadata(
            name=self.name,
            version=self.version,
            description=self.description,
            author=self.author,
            timeframe=self.timeframe,
            can_short=self.can_short,
            stoploss=self.stoploss,
            trailing_stop=self.trailing_stop,
            trailing_stop_positive=self.trailing_stop_positive,
            trailing_stop_positive_offset=self.trailing_stop_positive_offset,
            use_custom_stoploss=hasattr(self, 'custom_stoploss'),
            use_custom_exit=hasattr(self, 'custom_exit'),
            min_confidence=self.min_confidence,
            require_volume=self.require_volume,
            min_volume_ratio=self.min_volume_ratio,
        )
    
    def _apply_config_overrides(self):
        """应用配置覆盖"""
        for key in ['timeframe', 'stoploss', 'trailing_stop', 
                    'min_confidence', 'can_short']:
            if key in self.config:
                setattr(self, key, self.config[key])
    
    # ── 核心抽象方法 (必须实现) ─────────────────────────────────────
    
    @abstractmethod
    def populate_indicators(self, dataframe: pd.DataFrame, 
                           metadata: Dict) -> pd.DataFrame:
        """
        计算所有技术指标
        
        在此方法中添加所有需要的指标列:
        - RSI, MACD, Bollinger Bands, SuperTrend 等
        - 自定义因子
        
        Args:
            dataframe: OHLCV 数据 (columns: open, high, low, close, volume)
            metadata: 附加信息 (如 {'pair': 'HK.00700', 'timeframe': '5m'})
        
        Returns:
            添加了指标列的 DataFrame
        """
        pass
    
    @abstractmethod
    def populate_entry_signals(self, dataframe: pd.DataFrame, 
                              metadata: Dict) -> pd.DataFrame:
        """
        生成入场信号
        
        设置 'enter_long' 列为 1 表示买入信号:
        df.loc[condition, 'enter_long'] = 1
        
        可选: 设置 'enter_long' = 1 和 'enter_short' = 1 支持双向
        
        Args:
            dataframe: 包含指标的 DataFrame
            metadata: 附加信息
        
        Returns:
            添加了入场信号的 DataFrame
        """
        pass
    
    @abstractmethod
    def populate_exit_signals(self, dataframe: pd.DataFrame, 
                             metadata: Dict) -> pd.DataFrame:
        """
        生成出场信号
        
        设置 'exit_long' 列为 1 表示卖出信号:
        df.loc[condition, 'exit_long'] = 1
        
        Args:
            dataframe: 包含指标的 DataFrame
            metadata: 附加信息
        
        Returns:
            添加了出场信号的 DataFrame
        """
        pass
    
    # ── 可选覆盖方法 ────────────────────────────────────────────────
    
    def custom_stoploss(self, dataframe: pd.DataFrame, current_rate: float,
                       current_profit: float, current_time: Any,
                       side: str, **kwargs) -> Optional[float]:
        """
        自定义止损逻辑
        
        Args:
            dataframe: 当前 K 线数据
            current_rate: 当前价格
            current_profit: 当前盈亏比例
            current_time: 当前时间
            side: 'long' 或 'short'
        
        Returns:
            止损价格，或 None 使用默认 stoploss
        """
        return None
    
    def custom_exit(self, dataframe: pd.DataFrame, current_rate: float,
                   current_profit: float, current_time: Any,
                   side: str, **kwargs) -> Optional[str]:
        """
        自定义出场逻辑
        
        Args:
            dataframe: 当前 K 线数据
            current_rate: 当前价格
            current_profit: 当前盈亏比例
            current_time: 当前时间
            side: 'long' 或 'short'
        
        Returns:
            出场原因字符串，或 None 不触发额外出场
        """
        return None
    
    def confirm_trade_entry(self, dataframe: pd.DataFrame, order_type: str,
                          amount: float, current_rate: float,
                          current_time: Any, side: str, **kwargs) -> bool:
        """
        确认是否执行入场交易
        
        Args:
            dataframe: 当前 K 线数据
            order_type: 订单类型
            amount: 数量
            current_rate: 当前价格
            current_time: 当前时间
            side: 'long' 或 'short'
        
        Returns:
            True 确认入场，False 拒绝入场
        """
        return True
    
    def confirm_trade_exit(self, dataframe: pd.DataFrame, order_type: str,
                         amount: float, current_rate: float,
                         current_profit: float, current_time: Any,
                         side: str, exit_reason: str, **kwargs) -> bool:
        """
        确认是否执行出场交易
        
        Returns:
            True 确认出场，False 拒绝出场
        """
        return True
    
    # ── 辅助方法 ────────────────────────────────────────────────────
    
    def get_param_grid(self) -> Dict[str, List]:
        """
        返回参数网格 (用于回测优化)
        
        子类可覆盖此方法定义要优化的参数范围:
        {
            'stoploss': [-0.02, -0.03, -0.04, -0.05],
            'rsi_period': [10, 14, 18, 21],
        }
        """
        return {}
    
    def analyze_pair(self, dataframe: pd.DataFrame, 
                    metadata: Dict = None) -> pd.DataFrame:
        """
        完整分析流程 (便捷方法)
        
        按顺序执行: populate_indicators → populate_entry_signals → populate_exit_signals
        
        Args:
            dataframe: OHLCV 数据
            metadata: 附加信息
        
        Returns:
            完整的分析结果 DataFrame
        """
        metadata = metadata or {'pair': 'unknown', 'timeframe': self.timeframe}
        
        df = dataframe.copy()
        df = self.populate_indicators(df, metadata)
        df = self.populate_entry_signals(df, metadata)
        df = self.populate_exit_signals(df, metadata)
        
        return df
    
    def __repr__(self) -> str:
        return (
            f"StandardStrategy(name='{self.name}', version='{self.version}', "
            f"timeframe='{self.timeframe}', stoploss={self.stoploss:.2%})"
        )
