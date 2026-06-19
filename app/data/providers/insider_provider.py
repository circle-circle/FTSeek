"""
内部人交易数据 Provider

聚合多个数据源，按优先级降级：
  1. SEC EDGAR (主) - 官方 Form 4 文件
  2. Finnhub (备) - insider_transactions API

内部人买入是强看多信号（历史胜率 60%+），是提升 IC 的关键因子。
"""
import logging
import requests
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from app.data.providers.base import BaseDataProvider

logger = logging.getLogger(__name__)


class InsiderProvider(BaseDataProvider):
    """内部人交易 Provider（多源聚合）"""

    def __init__(self):
        super().__init__(name="insider", cache_ttl_seconds=86400)  # 24h 缓存

    def get_data(self, symbol: str, **kwargs) -> Dict[str, Any]:
        """获取内部人交易数据"""
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # 尝试数据源 1: SEC EDGAR
        result = self._fetch_sec_edgar(symbol)
        if result and result.get("status") == "ok":
            return result
        
        # 降级 2: Finnhub
        logger.info(f"[InsiderProvider] SEC EDGAR failed, trying Finnhub: {symbol}")
        result = self._fetch_finnhub(symbol)
        if result and result.get("status") == "ok":
            return result
        
        # 全部失败
        return {
            "status": "failed",
            "error": "All data sources failed",
            "data_source": "none",
            "fetched_at": now_iso,
        }

    def _fetch_sec_edgar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 SEC EDGAR 获取 Form 4 数据"""
        try:
            from app.data.sec_form4_provider import SECForm4Provider
            sec = SECForm4Provider()
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            
            # 获取近 30 天的内部人交易
            transactions = sec.get_form4_transactions(bare, days=30)
            
            if transactions is not None:
                now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                
                # 计算因子
                buy_count = sum(1 for t in transactions if t.get("type") == "buy")
                sell_count = sum(1 for t in transactions if t.get("type") == "sell")
                net_shares = sum(t.get("shares", 0) for t in transactions if t.get("type") == "buy")
                net_shares -= sum(abs(t.get("shares", 0)) for t in transactions if t.get("type") == "sell")
                
                return {
                    "status": "ok",
                    "data": {
                        "buy_count_30d": buy_count,
                        "sell_count_30d": sell_count,
                        "net_shares_30d": net_shares,
                        "insider_signal": "strong_buy" if buy_count >= 3 and net_shares > 10000 else "buy" if buy_count > 0 else "neutral",
                        "transactions": transactions[:10],  # 最近 10 笔
                    },
                    "data_source": "sec_edgar",
                    "fetched_at": now_iso,
                }
        except Exception as e:
            logger.debug(f"[InsiderProvider] SEC EDGAR error: {e}")
        
        return None

    def _fetch_finnhub(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Finnhub 获取内部人交易（降级）"""
        try:
            import os
            from urllib3.exceptions import InsecureRequestWarning
            import urllib3
            urllib3.disable_warnings(InsecureRequestWarning)
            
            api_key = os.getenv("FINNHUB_API_KEY", "")
            if not api_key or api_key == "***":
                return None
            
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Finnhub insider transactions API
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/insider-transactions",
                params={"symbol": bare},
                headers={"X-Finnhub-Token": api_key},
                timeout=15,
                verify=False,
            )
            
            if resp.status_code == 200:
                data = resp.json()
                transactions = data.get("data", [])
                
                # 计算因子
                buy_count = sum(1 for t in transactions if t.get("transactionType") in ["P", "Purchase"])
                sell_count = sum(1 for t in transactions if t.get("transactionType") in ["S", "Sale"])
                net_shares = sum(t.get("change", 0) for t in transactions if t.get("transactionType") in ["P", "Purchase"])
                
                return {
                    "status": "ok",
                    "data": {
                        "buy_count_30d": buy_count,
                        "sell_count_30d": sell_count,
                        "net_shares_30d": net_shares,
                        "insider_signal": "strong_buy" if buy_count >= 3 and net_shares > 10000 else "buy" if buy_count > 0 else "neutral",
                        "transactions": transactions[:10],
                    },
                    "data_source": "finnhub",
                    "fetched_at": now_iso,
                }
        except Exception as e:
            logger.debug(f"[InsiderProvider] Finnhub error: {e}")
        
        return None
