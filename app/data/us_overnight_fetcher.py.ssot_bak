#!/usr/bin/env python3
"""
美股隔夜数据获取器 (修复版 - 2026-04-14)

功能:
1. 从FTSeek-US项目复用数据源 (TwelveData + Finnhub)
2. 获取美股隔夜收盘价和涨跌幅
3. 返回 OvernightChange 列表供跨市场套利使用

修复记录:
- 2026-04-14: 修复 Python 命名空间冲突 (FTSeek 的 app 包与 FTSeek-US 的 app 包冲突)
  - 使用 importlib 直接加载 FTSeek-US 模块文件，避免 sys.path 污染
  - 使用 USDataConfig 获取 API Key (支持 .env.us 降级)
- 原版问题: sys.path.insert(0, FTSeek-US) 导致 app.data.twelvedata_provider 找不到

架构:
  FTSeek (港股) → importlib 加载 → FTSeek-US (美股数据源)
"""

import sys
import os
import logging
import time
import importlib.util
from datetime import datetime
from typing import List, Optional
from pathlib import Path

# FTSeek-US 路径 (不添加到 sys.path，避免命名空间冲突)
FTSEEK_US_PATH = Path(__file__).parent.parent.parent.parent / "FTSeek-US"

logger = logging.getLogger("ftseek.us_overnight_fetcher")


def _load_ftseek_us_module(module_name: str, file_path: str):
    """
    使用 importlib 直接加载 FTSeek-US 模块，避免 app 命名空间冲突
    
    Args:
        module_name: 模块名 (如 'ftseek_us_twelvedata')
        file_path: 模块文件路径
    
    Returns:
        加载的模块对象
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"FTSeek-US 模块不存在: {file_path}")
    
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建模块规范: {file_path}")
    
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # 缓存到 sys.modules
    spec.loader.exec_module(module)
    return module


def _get_ftseek_us_data_path() -> Path:
    """获取 FTSeek-US 的 app/data 目录"""
    return FTSEEK_US_PATH / "app" / "data"


# 缓存已加载的模块
_loaded_modules = {}


def _get_twelvedata_provider_class():
    """懒加载 TwelveDataProvider 类"""
    if 'twelvedata' not in _loaded_modules:
        file_path = str(_get_ftseek_us_data_path() / "twelvedata_provider.py")
        _loaded_modules['twelvedata'] = _load_ftseek_us_module(
            'ftseek_us_twelvedata', file_path
        )
    return _loaded_modules['twelvedata'].TwelveDataProvider


def _get_finnhub_provider_class():
    """懒加载 FinnhubProvider 类"""
    if 'finnhub' not in _loaded_modules:
        file_path = str(_get_ftseek_us_data_path() / "finnhub_provider.py")
        _loaded_modules['finnhub'] = _load_ftseek_us_module(
            'ftseek_us_finnhub', file_path
        )
    return _loaded_modules['finnhub'].FinnhubProvider


# 导入 OvernightChange (这是 FTSeek 本地的模块)
from app.strategy.cross_market import OvernightChange


class USOvernightDataFetcher:
    """
    美股隔夜数据获取器
    
    使用 FTSeek-US 的 TwelveData + Finnhub 双源架构
    通过 importlib 加载避免 Python 命名空间冲突
    """
    
    def __init__(self):
        self._twelvedata = None
        self._finnhub = None
        self._enabled = False
        
        self._init_providers()
    
    def _init_providers(self):
        """初始化数据源提供商 (使用 importlib + USDataConfig)"""
        try:
            # 使用 USDataConfig 获取 API Key (支持 .env.us 降级)
            from app.data.us_data_config import get_us_data_config
            us_config = get_us_data_config()
            
            # 初始化 TwelveDataProvider
            td_class = _get_twelvedata_provider_class()
            td_api_key = us_config.get_api_key('twelvedata')
            if td_api_key:
                self._twelvedata = td_class(api_key=td_api_key)
                logger.info(f"TwelveDataProvider 已初始化 (API Key: {td_api_key[:8]}...)")
            else:
                logger.warning("TwelveData API Key 未配置")
            
            # 初始化 FinnhubProvider
            fh_class = _get_finnhub_provider_class()
            fh_api_key = us_config.get_api_key('finnhub')
            if fh_api_key:
                self._finnhub = fh_class(api_key=fh_api_key)
                logger.info(f"FinnhubProvider 已初始化 (API Key: {fh_api_key[:8]}...)")
            else:
                logger.warning("Finnhub API Key 未配置")
            
            self._enabled = (
                (self._twelvedata and self._twelvedata.enabled) or
                (self._finnhub and self._finnhub.enabled)
            )
            
            if self._enabled:
                logger.info("美股隔夜数据获取器已初始化 (TwelveData + Finnhub)")
            else:
                logger.warning("美股数据源未配置 (需要 TWELVEDATA_API_KEY 或 FINNHUB_API_KEY)")
        
        except FileNotFoundError as e:
            logger.error(f"FTSeek-US 模块文件不存在: {e}")
            self._enabled = False
        except Exception as e:
            logger.error(f"美股数据源初始化失败: {e}", exc_info=True)
            self._enabled = False
    
    def fetch_overnight_data(self, symbols: Optional[List[str]] = None) -> List[OvernightChange]:
        """
        获取美股隔夜数据
        
        Args:
            symbols: 美股代码列表 (如 ['TCEHY', 'BABA', 'JD'])
                    如果为 None,从映射文件自动获取
        
        Returns:
            OvernightChange 列表
        """
        if not self._enabled:
            logger.warning("美股数据源未启用,返回空列表")
            return []
        
        # 获取需要监控的美股代码
        if symbols is None:
            symbols = self._get_us_symbols_from_mapping()
        
        if not symbols:
            logger.warning("无美股代码,跳过获取")
            return []
        
        logger.info(f"开始获取 {len(symbols)} 只美股隔夜数据...")
        
        overnight_changes = []
        failed_symbols = []
        
        for symbol in symbols:
            change = self._fetch_single_symbol_with_retry(symbol)
            if change:
                overnight_changes.append(change)
            else:
                failed_symbols.append(symbol)
        
        if failed_symbols:
            if len(failed_symbols) == len(symbols):
                logger.warning(
                    f"[告警] 全部 {len(symbols)} 只美股隔夜数据获取失败, "
                    f"返回空列表! 失败清单: {failed_symbols}"
                )
            else:
                logger.warning(
                    f"部分美股数据获取失败 ({len(failed_symbols)}/{len(symbols)}): {failed_symbols}"
                )
        
        logger.info(f"美股隔夜数据获取完成: {len(overnight_changes)}/{len(symbols)} 只")
        
        return overnight_changes
    
    def _fetch_single_symbol_with_retry(
        self, symbol: str, max_retries: int = 3
    ) -> Optional[OvernightChange]:
        """带重试机制获取单只美股隔夜数据"""
        retry_delays = [1, 2, 4]
        last_error = None
        
        for attempt in range(max_retries):
            try:
                change = self._fetch_single_symbol(symbol)
                if change:
                    if attempt > 0:
                        logger.info(f"重试成功: {symbol} (第{attempt + 1}次尝试)")
                    return change
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    logger.warning(
                        f"[{symbol}] 第{attempt + 1}次尝试失败: {e}, "
                        f"{delay}s 后重试..."
                    )
                    time.sleep(delay)
        
        logger.error(
            f"[{symbol}] 所有 {max_retries} 次尝试均失败, 最后错误: {last_error}"
        )
        return None

    def _fetch_single_symbol(self, symbol: str) -> Optional[OvernightChange]:
        """获取单只美股的隔夜数据"""
        # 优先使用 TwelveData
        if self._twelvedata and self._twelvedata.enabled:
            try:
                quote = self._twelvedata.get_quote(symbol)
                if quote:
                    return self._parse_twelvedata_quote(symbol, quote)
            except Exception as e:
                logger.warning(f"TwelveData 获取 {symbol} 失败: {e}")
        
        # Fallback 到 Finnhub
        if self._finnhub and self._finnhub.enabled:
            try:
                quote = self._finnhub.get_quote(symbol)
                if quote:
                    return self._parse_finnhub_quote(symbol, quote)
            except Exception as e:
                logger.warning(f"Finnhub 获取 {symbol} 失败: {e}")
        
        logger.warning(f"所有数据源获取 {symbol} 失败")
        return None
    
    def _parse_twelvedata_quote(self, symbol: str, quote: dict) -> OvernightChange:
        """解析 TwelveData 报价"""
        close_price = float(quote.get('close', 0))
        change_pct = float(quote.get('percent_change', 0)) / 100.0
        volume = float(quote.get('volume', 0))
        turnover = close_price * volume
        
        return OvernightChange(
            code=symbol,
            name=quote.get('name', symbol),
            close_price=close_price,
            change_pct=change_pct,
            volume=volume,
            turnover=turnover,
            market='US',
            timestamp=datetime.now(),
        )
    
    def _parse_finnhub_quote(self, symbol: str, quote: dict) -> OvernightChange:
        """解析 Finnhub 报价"""
        close_price = float(quote.get('c', 0))
        change_pct = float(quote.get('dp', 0)) / 100.0
        volume = float(quote.get('v', 0))
        
        return OvernightChange(
            code=symbol,
            name=symbol,
            close_price=close_price,
            change_pct=change_pct,
            volume=volume,
            turnover=0.0,
            market='US',
            timestamp=datetime.now(),
        )
    
    def _get_us_symbols_from_mapping(self) -> List[str]:
        """从美港股映射文件获取美股代码列表"""
        try:
            from app.strategy.stock_pool import get_stock_pool_manager
            
            stock_pool = get_stock_pool_manager()
            pairs = stock_pool.get_enabled_correlation_pairs()
            us_symbols = list(set([pair.us_code for pair in pairs]))
            
            logger.info(f"从映射文件获取到 {len(us_symbols)} 只美股")
            return us_symbols
        
        except Exception as e:
            logger.error(f"获取映射文件失败: {e}")
            return []
    
    def get_status(self) -> dict:
        """获取数据源状态"""
        return {
            'enabled': self._enabled,
            'twelvedata_enabled': self._twelvedata.enabled if self._twelvedata else False,
            'finnhub_enabled': self._finnhub.enabled if self._finnhub else False,
        }


# 全局单例
_fetcher: Optional[USOvernightDataFetcher] = None


def get_overnight_fetcher() -> USOvernightDataFetcher:
    """获取全局单例"""
    global _fetcher
    if _fetcher is None:
        _fetcher = USOvernightDataFetcher()
    return _fetcher


def init_overnight_fetcher() -> USOvernightDataFetcher:
    """初始化全局单例"""
    global _fetcher
    _fetcher = USOvernightDataFetcher()
    return _fetcher
