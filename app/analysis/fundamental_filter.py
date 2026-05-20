"""
基本面筛选模块 - P1 改进

功能:
- 市值 > $100 亿
- 营收增长率 > 10%
- 毛利率 > 20%
- 负债率 < 60%

数据源：
- Finnhub API (财务指标)
- Alpha Vantage (财报数据)

优化 2026-03-27:
- 优化 3.2：调整评分权重
  - 技术面：70% → 50%
  - 基本面：30% → 40%
  - 市场状态：新增 10%
"""
import logging
import os
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

# API 配置
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")

# 筛选阈值
MIN_MARKET_CAP = 10_000_000_000  # $100 亿
MIN_REVENUE_GROWTH = 0.10  # 10%
MIN_GROSS_MARGIN = 0.20  # 20%
MAX_DEBT_RATIO = 0.60  # 60%

# 评分权重（优化 3.2）
TECHNICAL_WEIGHT = 0.50  # 技术面权重 50%
FUNDAMENTAL_WEIGHT = 0.40  # 基本面权重 40%
MARKET_WEIGHT = 0.10  # 市场状态权重 10%


@dataclass
class FundamentalMetrics:
    """基本面指标"""
    symbol: str
    market_cap: float  # 市值 (美元)
    revenue_growth: float  # 营收增长率
    gross_margin: float  # 毛利率
    debt_ratio: float  # 负债率
    pe_ratio: Optional[float] = None  # 市盈率
    pb_ratio: Optional[float] = None  # 市净率
    roe: Optional[float] = None  # ROE
    last_updated: Optional[datetime] = None


@dataclass
class ScreeningResult:
    """筛选结果"""
    symbol: str
    passed: bool
    metrics: FundamentalMetrics
    failed_criteria: List[str]


class FundamentalFilter:
    """
    基本面筛选器
    
    P1 改进：
    - 市值 > $100 亿
    - 营收增长率 > 10%
    - 毛利率 > 20%
    - 负债率 < 60%
    """
    
    def __init__(
        self,
        min_market_cap: float = MIN_MARKET_CAP,
        min_revenue_growth: float = MIN_REVENUE_GROWTH,
        min_gross_margin: float = MIN_GROSS_MARGIN,
        max_debt_ratio: float = MAX_DEBT_RATIO,
    ):
        """
        初始化基本面筛选器
        
        Args:
            min_market_cap: 最小市值
            min_revenue_growth: 最小营收增长率
            min_gross_margin: 最小毛利率
            max_debt_ratio: 最大负债率
        """
        self.min_market_cap = min_market_cap
        self.min_revenue_growth = min_revenue_growth
        self.min_gross_margin = min_gross_margin
        self.max_debt_ratio = max_debt_ratio
        
        # 缓存
        self._cache: Dict[str, Tuple[FundamentalMetrics, datetime]] = {}
        self._cache_ttl = timedelta(hours=24)  # 缓存 24 小时
        
        logger.info(
            f"基本面筛选器已初始化 | "
            f"市值>${min_market_cap/1e9:.0f}B | "
            f"营收增长>{min_revenue_growth:.0%} | "
            f"毛利率>{min_gross_margin:.0%} | "
            f"负债率<{max_debt_ratio:.0%}"
        )
    
    def _fetch_from_finnhub(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Finnhub 获取财务指标"""
        if not FINNHUB_API_KEY:
            logger.warning("未配置 Finnhub API Key")
            return None
        
        try:
            # 获取公司基本面
            url = f"https://finnhub.io/api/v1/stock/metric"
            params = {
                "symbol": symbol,
                "metric": "all",
                "token": FINNHUB_API_KEY,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "metric" in data:
                return data["metric"]
        except requests.exceptions.RequestException as e:
            logger.error(f"Finnhub API 请求失败 {symbol}: {e}")
        except (KeyError, ValueError) as e:
            logger.error(f"解析 Finnhub 数据失败 {symbol}: {e}")
        
        return None
    
    def _fetch_from_alpha_vantage(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Alpha Vantage 获取财报数据"""
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("未配置 Alpha Vantage API Key")
            return None
        
        try:
            # 获取公司概述
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "OVERVIEW",
                "symbol": symbol,
                "apikey": ALPHAVANTAGE_API_KEY,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "MarketCapitalization" in data:
                return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Alpha Vantage API 请求失败 {symbol}: {e}")
        except (KeyError, ValueError) as e:
            logger.error(f"解析 Alpha Vantage 数据失败 {symbol}: {e}")
        
        return None
    
    def get_fundamental_metrics(self, symbol: str) -> Optional[FundamentalMetrics]:
        """
        获取股票基本面指标
        
        Args:
            symbol: 股票代码
        
        Returns:
            基本面指标，获取失败返回 None
        """
        # 检查缓存
        if symbol in self._cache:
            metrics, cached_at = self._cache[symbol]
            if datetime.now() - cached_at < self._cache_ttl:
                logger.debug(f"使用缓存的基本面数据 {symbol}")
                return metrics
        
        # 清理代码格式
        clean_symbol = symbol.upper().replace("US.", "").replace("HK.", "")
        
        # 获取数据
        finnhub_data = self._fetch_from_finnhub(clean_symbol)
        alpha_data = self._fetch_from_alpha_vantage(clean_symbol)
        
        if not finnhub_data and not alpha_data:
            logger.warning(f"无法获取 {symbol} 基本面数据")
            return None
        
        # 解析市值
        market_cap = 0.0
        if finnhub_data and "marketCapitalization" in finnhub_data:
            market_cap = float(finnhub_data.get("marketCapitalization", 0) or 0)
        elif alpha_data and "MarketCapitalization" in alpha_data:
            try:
                market_cap = float(alpha_data["MarketCapitalization"])
            except (ValueError, TypeError):
                market_cap = 0.0
        
        # 解析营收增长率
        revenue_growth = 0.0
        if finnhub_data:
            revenue_growth = float(finnhub_data.get("revenueGrowthYOY", 0) or 0)
        
        # 解析毛利率
        gross_margin = 0.0
        if finnhub_data:
            gross_margin = float(finnhub_data.get("grossMargin", 0) or 0)
        
        # 解析负债率
        debt_ratio = 0.0
        if finnhub_data:
            debt_ratio = float(finnhub_data.get("debtToEquity", 0) or 0) / 100  # Finnhub 返回百分比
        
        metrics = FundamentalMetrics(
            symbol=symbol,
            market_cap=market_cap,
            revenue_growth=revenue_growth,
            gross_margin=gross_margin,
            debt_ratio=debt_ratio,
            pe_ratio=finnhub_data.get("peRatio") if finnhub_data else None,
            pb_ratio=finnhub_data.get("pbRatio") if finnhub_data else None,
            roe=finnhub_data.get("returnOnEquity") if finnhub_data else None,
            last_updated=datetime.now(),
        )
        
        # 更新缓存
        self._cache[symbol] = (metrics, datetime.now())
        
        return metrics
    
    def screen(self, symbol: str) -> ScreeningResult:
        """
        筛选单只股票
        
        Args:
            symbol: 股票代码
        
        Returns:
            筛选结果
        """
        metrics = self.get_fundamental_metrics(symbol)
        
        if not metrics:
            return ScreeningResult(
                symbol=symbol,
                passed=False,
                metrics=FUNDAMENTAL_METRICS_EMPTY,
                failed_criteria=["数据获取失败"],
            )
        
        failed_criteria = []
        
        # 检查市值
        if metrics.market_cap < self.min_market_cap:
            failed_criteria.append(
                f"市值 {metrics.market_cap/1e9:.1f}B < ${self.min_market_cap/1e9:.0f}B"
            )
        
        # 检查营收增长率
        if metrics.revenue_growth < self.min_revenue_growth:
            failed_criteria.append(
                f"营收增长 {metrics.revenue_growth:.1%} < {self.min_revenue_growth:.0%}"
            )
        
        # 检查毛利率
        if metrics.gross_margin < self.min_gross_margin:
            failed_criteria.append(
                f"毛利率 {metrics.gross_margin:.1%} < {self.min_gross_margin:.0%}"
            )
        
        # 检查负债率
        if metrics.debt_ratio > self.max_debt_ratio:
            failed_criteria.append(
                f"负债率 {metrics.debt_ratio:.1%} > {self.max_debt_ratio:.0%}"
            )
        
        passed = len(failed_criteria) == 0
        
        if passed:
            logger.info(f"[基本面] {symbol} 筛选通过 ✓")
        else:
            logger.info(f"[基本面] {symbol} 筛选未通过：{failed_criteria}")
        
        return ScreeningResult(
            symbol=symbol,
            passed=passed,
            metrics=metrics,
            failed_criteria=failed_criteria,
        )
    
    def screen_batch(self, symbols: List[str]) -> Dict[str, ScreeningResult]:
        """
        批量筛选股票
        
        Args:
            symbols: 股票代码列表
        
        Returns:
            筛选结果字典
        """
        results = {}
        for symbol in symbols:
            results[symbol] = self.screen(symbol)
        return results
    
    def get_passed_symbols(self, symbols: List[str]) -> List[str]:
        """
        获取通过筛选的股票列表
        
        Args:
            symbols: 股票代码列表
        
        Returns:
            通过筛选的股票列表
        """
        results = self.screen_batch(symbols)
        return [symbol for symbol, result in results.items() if result.passed]


# 空指标对象（用于数据获取失败时）
FUNDAMENTAL_METRICS_EMPTY = FundamentalMetrics(
    symbol="",
    market_cap=0,
    revenue_growth=0,
    gross_margin=0,
    debt_ratio=0,
)


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    filter = FundamentalFilter()
    
    # 测试筛选
    test_symbols = ["AAPL", "MSFT", "NVDA", "TSLA"]
    for symbol in test_symbols:
        result = filter.screen(symbol)
        print(f"\n{symbol}:")
        print(f"  通过：{result.passed}")
        print(f"  市值：${result.metrics.market_cap/1e9:.1f}B")
        print(f"  营收增长：{result.metrics.revenue_growth:.1%}")
        print(f"  毛利率：{result.metrics.gross_margin:.1%}")
        print(f"  负债率：{result.metrics.debt_ratio:.1%}")
        if result.failed_criteria:
            print(f"  未通过原因：{result.failed_criteria}")
