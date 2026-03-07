"""
PreMarketAnalyzer - 盘前成交量分析器

用于分析股票盘前成交量，识别异常放量情况。
支持单只股票分析和批量分析，提供异常评分和检测报告。
"""

from dataclasses import dataclass
from typing import List, Optional, Dict
import time
import random


@dataclass
class PreMarketReport:
    """
    盘前分析报告

    Attributes:
        symbol: 股票代码
        volume_ratio: 盘前量比 (盘前量/正常时段平均)
        is_abnormal: 是否异常 (>3 倍)
        abnormal_score: 异常程度评分 (0-100)
        bid_ask_spread: 买卖价差
        order_imbalance: 订单不平衡度
        timestamp: 时间戳
    """
    symbol: str
    volume_ratio: float
    is_abnormal: bool
    abnormal_score: int
    bid_ask_spread: float
    order_imbalance: float
    timestamp: float


class PreMarketAnalyzer:
    """
    盘前成交量分析器

    分析股票盘前成交量，识别异常放量情况。
    支持富途 API 集成或模拟数据。

    Attributes:
        futu_config: 富途 API 配置字典
        default_threshold: 默认异常检测阈值
    """

    def __init__(self, futu_config: Optional[Dict] = None):
        """
        初始化盘前分析器

        Args:
            futu_config: 富途 API 配置，可选。如果为 None，使用模拟数据
        """
        self.futu_config = futu_config
        self.default_threshold = 3.0
        self._last_reports: Dict[str, PreMarketReport] = {}

    def _simulate_premarket_data(self, symbol: str) -> Dict:
        """
        模拟盘前数据 (用于测试或无 API 配置时)

        Args:
            symbol: 股票代码

        Returns:
            包含盘前数据的字典
        """
        # 使用 symbol 的哈希值生成可重复的"随机"数据
        seed = sum(ord(c) for c in symbol)
        random.seed(seed)

        # 模拟量比 (大部分股票在 0.5-2.0 之间，少数异常)
        volume_ratio = random.uniform(0.5, 2.5)
        # 20% 概率出现异常放量
        if random.random() < 0.2:
            volume_ratio = random.uniform(3.0, 8.0)

        return {
            'volume_ratio': volume_ratio,
            'bid_ask_spread': random.uniform(0.01, 0.10),
            'order_imbalance': random.uniform(-0.5, 0.5),
            'timestamp': time.time()
        }

    def _calculate_abnormal_score(self, volume_ratio: float) -> int:
        """
        计算异常评分

        评分公式：abnormal_score = min(100, volume_ratio * 20)

        Args:
            volume_ratio: 量比

        Returns:
            异常评分 (0-100)
        """
        return min(100, int(volume_ratio * 20))

    def _is_abnormal_volume(self, volume_ratio: float, threshold: float = None) -> bool:
        """
        判断是否异常放量

        Args:
            volume_ratio: 量比
            threshold: 阈值，默认使用实例的 default_threshold

        Returns:
            是否异常
        """
        if threshold is None:
            threshold = self.default_threshold
        return volume_ratio >= threshold

    def analyze_volume(self, symbol: str) -> PreMarketReport:
        """
        分析单只股票的盘前成交量

        Args:
            symbol: 股票代码

        Returns:
            PreMarketReport: 盘前分析报告
        """
        # 获取盘前数据 (模拟或真实 API)
        if self.futu_config is not None:
            # TODO: 集成富途 API 获取真实数据
            # data = self._fetch_futu_premarket_data(symbol)
            data = self._simulate_premarket_data(symbol)
        else:
            data = self._simulate_premarket_data(symbol)

        volume_ratio = data['volume_ratio']
        abnormal_score = self._calculate_abnormal_score(volume_ratio)
        is_abnormal = self._is_abnormal_volume(volume_ratio)

        report = PreMarketReport(
            symbol=symbol,
            volume_ratio=volume_ratio,
            is_abnormal=is_abnormal,
            abnormal_score=abnormal_score,
            bid_ask_spread=data['bid_ask_spread'],
            order_imbalance=data['order_imbalance'],
            timestamp=data['timestamp']
        )

        # 缓存报告
        self._last_reports[symbol] = report

        return report

    def analyze_batch(self, symbols: List[str]) -> List[PreMarketReport]:
        """
        批量分析多只股票

        Args:
            symbols: 股票代码列表

        Returns:
            List[PreMarketReport]: 盘前分析报告列表
        """
        reports = []
        for symbol in symbols:
            report = self.analyze_volume(symbol)
            reports.append(report)
        return reports

    def get_abnormal_stocks(self, threshold: float = 3.0) -> List[str]:
        """
        获取异常放量股票列表

        基于最近一次分析结果，返回超过阈值的股票代码。

        Args:
            threshold: 量比阈值，默认 3.0

        Returns:
            List[str]: 异常放量股票代码列表
        """
        abnormal_stocks = []
        for symbol, report in self._last_reports.items():
            if report.volume_ratio >= threshold:
                abnormal_stocks.append(symbol)
        return abnormal_stocks

    def get_report(self, symbol: str) -> Optional[PreMarketReport]:
        """
        获取指定股票的最近分析报告

        Args:
            symbol: 股票代码

        Returns:
            PreMarketReport 或 None (如果未分析过)
        """
        return self._last_reports.get(symbol)

    def clear_cache(self) -> None:
        """清除缓存的分析报告"""
        self._last_reports.clear()
