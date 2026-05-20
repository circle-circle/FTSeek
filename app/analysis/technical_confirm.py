"""
技术面确认模块

功能:
1. RSI 确认 - 避免超买区追高
2. MACD 确认 - 金叉信号
3. 成交量确认 - 确认突破有效性

使用示例:
    from app.analysis.technical_confirm import TechnicalConfirm
    
    confirm = TechnicalConfirm()
    if confirm.check("AAPL"):
        # 技术面确认通过
        pass
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass
import os

import requests

logger = logging.getLogger(__name__)

# API 配置
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# 确认阈值
RSI_MIN = 50  # RSI 下限（多头）
RSI_MAX = 70  # RSI 上限（避免超买）
VOLUME_RATIO_MIN = 1.0  # 成交量/20 日均量最小值


@dataclass
class TechnicalConfirmResult:
    """技术面确认结果"""
    symbol: str
    passed: bool
    rsi_ok: bool
    rsi_value: Optional[float]
    macd_ok: bool
    macd_value: Optional[float]
    macd_signal: Optional[float]
    macd_histogram: Optional[float]
    volume_ok: bool
    volume_ratio: Optional[float]
    failed_criteria: List[str]


class TechnicalConfirm:
    """
    技术面确认器
    
    确认条件:
    1. RSI(14) > 50 且 < 70（多头）
    2. MACD 线 > 信号线（金叉）
    3. 成交量 > 20 日均量
    """
    
    def __init__(
        self,
        rsi_min: float = RSI_MIN,
        rsi_max: float = RSI_MAX,
        volume_ratio_min: float = VOLUME_RATIO_MIN,
    ):
        """
        初始化技术面确认器
        
        Args:
            rsi_min: RSI 下限
            rsi_max: RSI 上限
            volume_ratio_min: 成交量比率下限
        """
        self.rsi_min = rsi_min
        self.rsi_max = rsi_max
        self.volume_ratio_min = volume_ratio_min
        
        # 缓存
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = timedelta(minutes=15)  # 缓存 15 分钟
        
        logger.info("技术面确认器已初始化")
    
    def _fetch_rsi(self, symbol: str, period: int = 14) -> Optional[float]:
        """
        获取 RSI 指标
        
        Args:
            symbol: 股票代码
            period: RSI 周期（默认 14）
        
        Returns:
            RSI 值或 None
        """
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("未配置 Alpha Vantage API Key")
            return None
        
        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "RSI",
                "symbol": symbol,
                "interval": "daily",
                "time_period": period,
                "series_type": "close",
                "apikey": ALPHAVANTAGE_API_KEY,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "Technical Analysis: RSI" in data:
                time_series = data["Technical Analysis: RSI"]
                latest_date = sorted(time_series.keys())[0]
                rsi = float(time_series[latest_date]["RSI"])
                logger.debug(f"{symbol} RSI({period})={rsi:.2f}")
                return rsi
            else:
                logger.warning(f"RSI 数据格式异常：{data.get('Note', 'Unknown error')}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"获取 RSI 数据失败 {symbol}: {e}")
        except (KeyError, ValueError) as e:
            logger.error(f"解析 RSI 数据失败 {symbol}: {e}")
        
        return None
    
    def _fetch_macd(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        获取 MACD 指标
        
        Args:
            symbol: 股票代码
        
        Returns:
            {'macd': float, 'signal': float, 'histogram': float} 或 None
        """
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("未配置 Alpha Vantage API Key")
            return None
        
        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "MACD",
                "symbol": symbol,
                "interval": "daily",
                "series_type": "close",
                "apikey": ALPHAVANTAGE_API_KEY,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "Technical Analysis: MACD" in data:
                time_series = data["Technical Analysis: MACD"]
                latest_date = sorted(time_series.keys())[0]
                ts = time_series[latest_date]
                
                macd_data = {
                    "macd": float(ts["MACD"]),
                    "signal": float(ts["MACD_Signal"]),
                    "histogram": float(ts["MACD_Hist"]),
                }
                logger.debug(f"{symbol} MACD={macd_data}")
                return macd_data
            else:
                logger.warning(f"MACD 数据格式异常：{data.get('Note', 'Unknown error')}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"获取 MACD 数据失败 {symbol}: {e}")
        except (KeyError, ValueError) as e:
            logger.error(f"解析 MACD 数据失败 {symbol}: {e}")
        
        return None
    
    def _fetch_volume(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        获取成交量数据（当日成交量和 20 日均量）
        
        Args:
            symbol: 股票代码
        
        Returns:
            {'current_volume': float, 'avg_volume_20': float} 或 None
        """
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("未配置 Alpha Vantage API Key")
            return None
        
        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": ALPHAVANTAGE_API_KEY,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "Time Series (Daily)" in data:
                time_series = data["Time Series (Daily)"]
                dates = sorted(time_series.keys(), reverse=True)
                
                if len(dates) < 20:
                    logger.warning(f"{symbol} 数据不足 20 天")
                    return None
                
                # 当日成交量
                current_volume = float(time_series[dates[0]]["5. volume"])
                
                # 20 日平均成交量
                volumes_20 = [float(time_series[date]["5. volume"]) for date in dates[:20]]
                avg_volume_20 = sum(volumes_20) / len(volumes_20)
                
                logger.debug(f"{symbol} 成交量={current_volume:.0f}, 20 日均量={avg_volume_20:.0f}")
                return {
                    "current_volume": current_volume,
                    "avg_volume_20": avg_volume_20,
                }
            else:
                logger.warning(f"成交量数据格式异常：{data.get('Note', 'Unknown error')}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"获取成交量数据失败 {symbol}: {e}")
        except (KeyError, ValueError) as e:
            logger.error(f"解析成交量数据失败 {symbol}: {e}")
        
        return None
    
    def check_rsi(self, symbol: str) -> tuple:
        """
        检查 RSI 条件
        
        Returns:
            (是否通过，RSI 值)
        """
        rsi = self._fetch_rsi(symbol)
        if rsi is None:
            logger.warning(f"{symbol} RSI 数据获取失败，默认通过")
            return (True, None)  # 数据获取失败时默认放行
        
        passed = self.rsi_min < rsi < self.rsi_max
        
        if passed:
            logger.info(f"[RSI] {symbol} RSI={rsi:.2f} ({self.rsi_min}-{self.rsi_max}) ✓")
        else:
            logger.info(f"[RSI] {symbol} RSI={rsi:.2f} 不在区间 ({self.rsi_min}-{self.rsi_max}) ✗")
        
        return (passed, rsi)
    
    def check_macd(self, symbol: str) -> tuple:
        """
        检查 MACD 条件（金叉）
        
        Returns:
            (是否通过，MACD 数据字典)
        """
        macd_data = self._fetch_macd(symbol)
        if macd_data is None:
            logger.warning(f"{symbol} MACD 数据获取失败，默认通过")
            return (True, None)  # 数据获取失败时默认放行
        
        macd = macd_data["macd"]
        signal = macd_data["signal"]
        histogram = macd_data["histogram"]
        
        # 金叉条件：MACD > 信号线 且 柱状图由负转正（或为正）
        passed = macd > signal and histogram > 0
        
        if passed:
            logger.info(f"[MACD] {symbol} 金叉 MACD={macd:.3f} > Signal={signal:.3f} ✓")
        else:
            logger.info(f"[MACD] {symbol} 未金叉 MACD={macd:.3f} Signal={signal:.3f} ✗")
        
        return (passed, macd_data)
    
    def check_volume(self, symbol: str) -> tuple:
        """
        检查成交量条件
        
        Returns:
            (是否通过，成交量比率)
        """
        volume_data = self._fetch_volume(symbol)
        if volume_data is None:
            logger.warning(f"{symbol} 成交量数据获取失败，默认通过")
            return (True, None)  # 数据获取失败时默认放行
        
        current = volume_data["current_volume"]
        avg_20 = volume_data["avg_volume_20"]
        
        if avg_20 <= 0:
            return (True, None)
        
        ratio = current / avg_20
        passed = ratio >= self.volume_ratio_min
        
        if passed:
            logger.info(f"[成交量] {symbol} 比率={ratio:.2f}x (>{self.volume_ratio_min}) ✓")
        else:
            logger.info(f"[成交量] {symbol} 比率={ratio:.2f}x (<{self.volume_ratio_min}) ✗")
        
        return (passed, ratio)
    
    def check(self, symbol: str) -> TechnicalConfirmResult:
        """
        执行完整技术面确认
        
        Args:
            symbol: 股票代码
        
        Returns:
            技术面确认结果
        """
        # 检查缓存
        now = datetime.now()
        if symbol in self._cache:
            result, cached_at = self._cache[symbol]
            if now - cached_at < self._cache_ttl:
                logger.debug(f"使用缓存的技术面确认结果 {symbol}")
                return result
        
        # 执行检查
        rsi_ok, rsi_value = self.check_rsi(symbol)
        macd_ok, macd_data = self.check_macd(symbol)
        volume_ok, volume_ratio = self.check_volume(symbol)
        
        # 汇总结果
        failed_criteria = []
        if not rsi_ok:
            failed_criteria.append(f"RSI={rsi_value:.2f} 不在区间 ({self.rsi_min}-{self.rsi_max})")
        if not macd_ok:
            if macd_data:
                failed_criteria.append(f"MACD 未金叉 (MACD={macd_data['macd']:.3f}, Signal={macd_data['signal']:.3f})")
            else:
                failed_criteria.append("MACD 数据获取失败")
        if not volume_ok:
            failed_criteria.append(f"成交量比率={volume_ratio:.2f}x (<{self.volume_ratio_min}x)")
        
        passed = rsi_ok and macd_ok and volume_ok
        
        result = TechnicalConfirmResult(
            symbol=symbol,
            passed=passed,
            rsi_ok=rsi_ok,
            rsi_value=rsi_value,
            macd_ok=macd_ok,
            macd_value=macd_data["macd"] if macd_data else None,
            macd_signal=macd_data["signal"] if macd_data else None,
            macd_histogram=macd_data["histogram"] if macd_data else None,
            volume_ok=volume_ok,
            volume_ratio=volume_ratio,
            failed_criteria=failed_criteria,
        )
        
        # 更新缓存
        self._cache[symbol] = (result, now)
        
        if passed:
            logger.info(f"[技术面确认] {symbol} 所有条件满足 ✓")
        else:
            logger.warning(f"[技术面确认] {symbol} 未通过：{failed_criteria}")
        
        return result
    
    def check_batch(self, symbols: List[str]) -> Dict[str, TechnicalConfirmResult]:
        """
        批量检查技术面
        
        Args:
            symbols: 股票代码列表
        
        Returns:
            确认结果字典
        """
        results = {}
        for symbol in symbols:
            results[symbol] = self.check(symbol)
        return results
    
    def get_passed_symbols(self, symbols: List[str]) -> List[str]:
        """
        获取通过技术面确认的股票列表
        
        Args:
            symbols: 股票代码列表
        
        Returns:
            通过确认的股票列表
        """
        results = self.check_batch(symbols)
        return [symbol for symbol, result in results.items() if result.passed]


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    confirm = TechnicalConfirm()
    
    # 测试确认
    test_symbols = ["AAPL", "MSFT", "NVDA"]
    for symbol in test_symbols:
        result = confirm.check(symbol)
        print(f"\n{symbol}:")
        print(f"  通过：{result.passed}")
        print(f"  RSI: {result.rsi_value} (OK={result.rsi_ok})")
        print(f"  MACD: {result.macd_value} (OK={result.macd_ok})")
        print(f"  成交量比率：{result.volume_ratio} (OK={result.volume_ok})")
        if result.failed_criteria:
            print(f"  未通过原因：{result.failed_criteria}")
