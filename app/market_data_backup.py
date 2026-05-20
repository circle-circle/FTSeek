"""
备用市场数据源集成

当富途 OpenAPI 无法获取美股行情时，使用备用数据源：
1. TwelveData (主要备用)
2. Finnhub (次要备用)
3. Alpha Vantage (第三备用)

优先级：Futu OpenD → TwelveData → Finnhub → Alpha Vantage
"""
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger("ftseek.market_backup")

# 导入备用数据源
try:
    from app.data.market_data_router import MarketDataRouter
    ROUTER_AVAILABLE = True
    logger.info("市场数据路由器已加载")
except ImportError as e:
    ROUTER_AVAILABLE = False
    logger.warning(f"市场数据路由器不可用：{e}")

# 导入 Alpha Vantage SDK (兼容 v2 和 v3)
try:
    from alpha_vantage.timeseries import TimeSeries
    ALPHA_TIMESERIES_AVAILABLE = True
except ImportError:
    ALPHA_TIMESERIES_AVAILABLE = False

# GlobalQuotes 仅在旧版 SDK (v2) 中存在
try:
    from alpha_vantage.globalquotes import GlobalQuotes
    ALPHA_GLOBALQUOTES_AVAILABLE = True
except ImportError:
    ALPHA_GLOBALQUOTES_AVAILABLE = False

ALPHA_AVAILABLE = ALPHA_TIMESERIES_AVAILABLE

if not ALPHA_AVAILABLE:
    logger.warning("Alpha Vantage SDK 未安装")
elif not ALPHA_GLOBALQUOTES_AVAILABLE:
    logger.info("Alpha Vantage SDK v3 已加载 (TimeSeries 可用，GlobalQuotes 已弃用)")


class MarketDataBackup:
    """备用市场数据查询器"""
    
    def __init__(self):
        self.router = None
        self.alpha_ts = None
        self.alpha_gq = None
        
        # 加载 API Keys
        self.twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "")
        self.finnhub_key = os.getenv("FINNHUB_API_KEY", "")
        self.alpha_key = os.getenv("ALPHAVANTAGE_API_KEY", "LNCGEWOCNT6AAYY2")
        
        # 初始化
        self._initialize()
    
    def _initialize(self):
        """初始化数据源"""
        # 1. 尝试初始化路由器 (最优)
        if ROUTER_AVAILABLE:
            try:
                self.router = MarketDataRouter()
                logger.info("市场数据路由器已初始化")
                return
            except Exception as e:
                logger.warning(f"路由器初始化失败：{e}")
        
        # 2. 初始化 Alpha Vantage (备用)
        if ALPHA_AVAILABLE and self.alpha_key:
            try:
                self.alpha_ts = TimeSeries(key=self.alpha_key, output_format='json')
                self.alpha_gq = GlobalQuotes(key=self.alpha_key, output_format='json') if ALPHA_GLOBALQUOTES_AVAILABLE else None
                if self.alpha_gq:
                    logger.info("Alpha Vantage 已初始化 (TimeSeries + GlobalQuotes)")
                else:
                    logger.info("Alpha Vantage 已初始化 (TimeSeries only, v3 SDK)")
            except Exception as e:
                logger.warning(f"Alpha Vantage 初始化失败：{e}")
    
    def get_price(self, symbol: str) -> Optional[float]:
        """
        获取股票价格
        
        Args:
            symbol: 股票代码 (格式：US.AAPL 或 AAPL)
        
        Returns:
            价格或 None
        """
        # 标准化股票代码
        clean_symbol = self._clean_symbol(symbol)
        if not clean_symbol:
            return None
        
        # 1. 尝试路由器
        if self.router:
            try:
                quote = self.router.get_quote(symbol)
                if quote and quote.get('price'):
                    logger.info(f"[Router] {symbol}: ${quote['price']}")
                    return float(quote['price'])
            except Exception as e:
                logger.warning(f"路由器获取价格失败 {symbol}: {e}")
        
        # 2. 尝试 Alpha Vantage (GlobalQuotes 或 TimeSeries)
        if self.alpha_gq:
            try:
                data, meta = self.alpha_gq.get_quote(symbol)
                if data:
                    price_key = '05. price'
                    if price_key in data:
                        price = float(data[price_key])
                        logger.info(f"[AlphaVantage] {symbol}: ${price}")
                        return price
            except Exception as e:
                logger.warning(f"Alpha Vantage GlobalQuotes 获取价格失败 {symbol}: {e}")
        
        # 2b. Fallback: 使用 TimeSeries 获取最新价格 (v3 SDK)
        if self.alpha_ts and not self.alpha_gq:
            try:
                data, meta = self.alpha_ts.get_quote(symbol)
                if data:
                    price_key = '05. price'
                    if price_key in data:
                        price = float(data[price_key])
                        logger.info(f"[AlphaVantage-TS] {symbol}: ${price}")
                        return price
            except Exception as e:
                logger.debug(f"Alpha Vantage TimeSeries 获取价格失败 {symbol}: {e}")
        
        # 3. 尝试 TwelveData API (直接调用)
        if self.twelvedata_key:
            try:
                import requests
                url = f"https://api.twelvedata.com/price"
                params = {
                    'symbol': clean_symbol,
                    'apikey': self.twelvedata_key
                }
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if 'price' in data:
                        price = float(data['price'])
                        logger.info(f"[TwelveData] {symbol}: ${price}")
                        return price
            except Exception as e:
                logger.warning(f"TwelveData 获取价格失败 {symbol}: {e}")
        
        # 4. 尝试 Finnhub API (直接调用)
        if self.finnhub_key:
            try:
                import requests
                url = f"https://finnhub.io/api/v1/quote"
                params = {
                    'symbol': clean_symbol,
                    'token': self.finnhub_key
                }
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if 'c' in data and data['c'] > 0:  # c = current price
                        price = float(data['c'])
                        logger.info(f"[Finnhub] {symbol}: ${price}")
                        return price
            except Exception as e:
                logger.warning(f"Finnhub 获取价格失败 {symbol}: {e}")
        
        logger.error(f"所有数据源均失败：{symbol}")
        return None
    
    def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场数据
        
        Returns:
            {
                'price': float,
                'change': float,
                'change_percent': float,
                'volume': int,
                'high': float,
                'low': float,
                'open': float,
                'prev_close': float
            }
        """
        clean_symbol = self._clean_symbol(symbol)
        if not clean_symbol:
            return None
        
        # 1. 尝试路由器
        if self.router:
            try:
                data = self.router.get_market_data(symbol)
                if data:
                    logger.info(f"[Router] {symbol}: 完整数据")
                    return data
            except Exception as e:
                logger.warning(f"路由器获取市场数据失败 {symbol}: {e}")
        
        # 2. 尝试 Alpha Vantage
        if self.alpha_ts:
            try:
                data, meta = self.alpha_ts.get_daily(symbol=symbol, outputsize='compact')
                if data and 'Time Series (Daily)' in data:
                    ts = data['Time Series (Daily)']
                    latest_date = sorted(ts.keys())[0]
                    latest = ts[latest_date]
                    
                    result = {
                        'price': float(latest['4. close']),
                        'open': float(latest['1. open']),
                        'high': float(latest['2. high']),
                        'low': float(latest['3. low']),
                        'volume': int(latest['5. volume']),
                        'prev_close': float(latest['4. close']),  # 简化处理
                        'change': 0,
                        'change_percent': 0
                    }
                    logger.info(f"[AlphaVantage] {symbol}: 完整数据")
                    return result
            except Exception as e:
                logger.warning(f"Alpha Vantage 获取市场数据失败 {symbol}: {e}")
        
        # 3. 尝试 TwelveData
        if self.twelvedata_key:
            try:
                import requests
                url = f"https://api.twelvedata.com/quote"
                params = {
                    'symbol': clean_symbol,
                    'apikey': self.twelvedata_key
                }
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    result = {
                        'price': float(data.get('price', 0)),
                        'open': float(data.get('open', 0)),
                        'high': float(data.get('high', 0)),
                        'low': float(data.get('low', 0)),
                        'volume': int(data.get('volume', 0)),
                        'prev_close': float(data.get('previous_close', 0)),
                        'change': float(data.get('change', 0)),
                        'change_percent': float(data.get('percent_change', 0))
                    }
                    logger.info(f"[TwelveData] {symbol}: 完整数据")
                    return result
            except Exception as e:
                logger.warning(f"TwelveData 获取市场数据失败 {symbol}: {e}")
        
        return None
    
    def _clean_symbol(self, symbol: str) -> str:
        """
        清理股票代码格式
        
        US.AAPL → AAPL
        HK.00700 → 00700.HK (TwelveData 格式)
        """
        if not symbol:
            return None
        
        # 移除 US. 前缀
        if symbol.startswith('US.'):
            return symbol[3:]
        
        # 转换 HK. 格式
        if symbol.startswith('HK.'):
            code = symbol[3:]
            return f"{code}.HK"
        
        return symbol
    
    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        status = {
            'healthy': False,
            'timestamp': datetime.now().isoformat(),
            'sources': {}
        }
        
        # 检查路由器
        if self.router:
            status['sources']['router'] = 'available'
        
        # 检查 Alpha Vantage
        if self.alpha_key:
            status['sources']['alpha_vantage'] = 'configured'
        
        # 检查 TwelveData
        if self.twelvedata_key:
            status['sources']['twelvedata'] = 'configured'
        
        # 检查 Finnhub
        if self.finnhub_key:
            status['sources']['finnhub'] = 'configured'
        
        # 至少一个可用
        status['healthy'] = len(status['sources']) > 0
        
        return status


# 单例
_backup = None

def get_market_backup() -> MarketDataBackup:
    """获取备用数据源单例"""
    global _backup
    if _backup is None:
        _backup = MarketDataBackup()
    return _backup
