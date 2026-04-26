"""
港股南向资金数据采集器 (Southbound Flow Collector)

双源架构:
1. 市场级别 (沪港通+深港通合计): 东方财富 HTTP API
2. 个股级别 (每只股票的资金分布/流向): 富途 OpenD API

功能:
- 获取南向资金 (沪港通+深港通) 净流入
- 获取个股资金分布 (主力/大户/中户/散户)
- 提供近期趋势判断 (3日/5日/10日均值)
- 缓存数据避免频繁请求

使用:
    collector = SouthboundFlowCollector()
    # 市场级别
    flow = collector.get_latest()
    trend = collector.get_trend_summary()
    # 个股级别 (需要已连接的 Futu ctx)
    stock_flow = collector.get_stock_capital_flow("HK.00700", ctx)
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

import requests

logger = logging.getLogger("ftseek.southbound_flow")

EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/kamtbskline/get"
CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "southbound_cache.json"
CACHE_TTL = 3600  # 1 小时缓存


class SouthboundFlowCollector:
    """南向资金数据采集器 (双源: 东方财富 + 富途)"""

    def __init__(self, cache_ttl: int = CACHE_TTL):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Any] = {}
        self._load_cache()

    def _load_cache(self):
        """加载缓存"""
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    if time.time() - data.get("timestamp", 0) < self.cache_ttl:
                        self._cache = data
                        logger.debug(f"[南向资金] 缓存命中: {data.get('date', 'N/A')}")
                    else:
                        logger.debug("[南向资金] 缓存已过期")
            except Exception:
                pass

    def _save_cache(self, data: Dict[str, Any]):
        """保存缓存"""
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, 'w') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[南向资金] 缓存保存失败: {e}")

    # ================================================================
    #  源1: 东方财富 — 市场级别南向资金
    # ================================================================

    def fetch_market_flow(self) -> Optional[Dict[str, Any]]:
        """
        从东方财富获取最新南向资金数据 (市场级别)

        Returns:
            {
                "date": "2026-04-21",
                "net_inflow": 123.45,
                "sh_hk_inflow": 45.67,
                "sz_hk_inflow": 77.78,
                "trend_3d": 100.12,
                "trend_5d": 80.56,
                "trend_10d": 60.23,
                "signal": "strong_inflow",
            }
        """
        try:
            params = {
                "fields1": "f1,f3,f5",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "lmt": "10",
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "_": str(int(time.time() * 1000)),
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://data.eastmoney.com/hsgt/index.html",
            }

            resp = requests.get(EASTMONEY_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            klines = data.get("data", {}).get("s2n", [])
            if not klines:
                logger.warning("[南向资金] 东方财富未返回数据")
                return None

            latest = klines[-1].split(",")
            result = {
                "date": latest[0],
                "sh_hk_inflow": float(latest[1]) if latest[1] != "-" else 0,
                "sz_hk_inflow": float(latest[2]) if latest[2] != "-" else 0,
                "net_inflow": float(latest[3]) if latest[3] != "-" else 0,
            }

            inflows = []
            for kline in klines[-10:]:
                parts = kline.split(",")
                if len(parts) > 3 and parts[3] != "-":
                    inflows.append(float(parts[3]))

            if inflows:
                result["trend_3d"] = sum(inflows[-3:]) / min(3, len(inflows))
                result["trend_5d"] = sum(inflows[-5:]) / min(5, len(inflows))
                result["trend_10d"] = sum(inflows[-10:]) / min(10, len(inflows))

            net = result["net_inflow"]
            if net > 100:
                result["signal"] = "strong_inflow"
            elif net > 30:
                result["signal"] = "inflow"
            elif net > -30:
                result["signal"] = "neutral"
            elif net > -100:
                result["signal"] = "outflow"
            else:
                result["signal"] = "strong_outflow"

            result["timestamp"] = time.time()
            self._save_cache(result)
            self._cache = result

            logger.info(
                f"[南向资金-市场] {result['date']} 净流入 {result['net_inflow']:.1f} 亿 "
                f"(沪={result['sh_hk_inflow']:.1f}, 深={result['sz_hk_inflow']:.1f}) "
                f"信号={result['signal']}"
            )
            return result

        except Exception as e:
            logger.error(f"[南向资金-市场] 获取失败: {e}")
            return None

    # ================================================================
    #  源2: 富途 OpenD — 个股级别资金流向/分布
    # ================================================================

    def get_stock_capital_flow(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """
        通过富途 OpenD 获取个股资金流向

        Args:
            symbol: 股票代码 (如 HK.00700)
            ctx: OpenQuoteContext 实例

        Returns:
            {
                "symbol": "HK.00700",
                "inflow": 12345678.0,     # 净流入
                "outflow": 8765432.0,     # 净流出
                "net_inflow": 3580246.0,  # 净流入 - 净流出
                "ratio": 0.12,            # 净流入占比
            }
        """
        try:
            ret, data = ctx.get_capital_flow(symbol)
            if ret != 0 or data is None or len(data) == 0:
                logger.debug(f"[南向资金-个股] {symbol} 富途资金流向无数据")
                return None

            # 获取最新一条 (INTRADAY)
            row = data.iloc[0]
            inflow = float(row.get("in_flow", 0))
            outflow = float(row.get("out_flow", 0))
            net = inflow - outflow
            total = inflow + outflow
            ratio = net / total if total > 0 else 0

            result = {
                "symbol": symbol,
                "inflow": inflow,
                "outflow": outflow,
                "net_inflow": net,
                "ratio": ratio,
                "source": "futu",
            }
            logger.debug(
                f"[南向资金-个股] {symbol} 净流入={net:,.0f} 占比={ratio:.2%}"
            )
            return result

        except Exception as e:
            logger.debug(f"[南向资金-个股] {symbol} 富途获取失败: {e}")
            return None

    def get_stock_capital_distribution(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """
        通过富途 OpenD 获取个股资金分布 (主力/大户/中户/散户)

        Args:
            symbol: 股票代码
            ctx: OpenQuoteContext 实例

        Returns:
            {
                "symbol": "HK.00700",
                "main_force": {"inflow": ..., "outflow": ..., "ratio": ...},
                "big": {"inflow": ..., "outflow": ..., "ratio": ...},
                "mid": {"inflow": ..., "outflow": ..., "ratio": ...},
                "small": {"inflow": ..., "outflow": ..., "ratio": ...},
            }
        """
        try:
            ret, data = ctx.get_capital_distribution(symbol)
            if ret != 0 or data is None or len(data) == 0:
                logger.debug(f"[资金分布] {symbol} 富途无数据")
                return None

            row = data.iloc[0]
            result = {
                "symbol": symbol,
                "source": "futu",
                "main_force": {
                    "inflow": float(row.get("capital_in_big", 0)),
                    "outflow": float(row.get("capital_out_big", 0)),
                },
                "big": {
                    "inflow": float(row.get("capital_in_mid", 0)),
                    "outflow": float(row.get("capital_out_mid", 0)),
                },
                "mid": {
                    "inflow": float(row.get("capital_in_small", 0)),
                    "outflow": float(row.get("capital_out_small", 0)),
                },
                "small": {
                    "inflow": float(row.get("capital_in_super_big", 0)),
                    "outflow": float(row.get("capital_out_super_big", 0)),
                },
            }

            # 计算各类型净流入
            for key in ["main_force", "big", "mid", "small"]:
                d = result[key]
                d["net"] = d["inflow"] - d["outflow"]
                total = d["inflow"] + d["outflow"]
                d["ratio"] = d["net"] / total if total > 0 else 0

            logger.debug(
                f"[资金分布] {symbol} 主力净流入={result['main_force']['net']:,.0f}"
            )
            return result

        except Exception as e:
            logger.debug(f"[资金分布] {symbol} 富途获取失败: {e}")
            return None

    # ================================================================
    #  统一接口
    # ================================================================

    def get_latest(self) -> Optional[Dict[str, Any]]:
        """获取最新市场级别南向资金 (带缓存)"""
        if self._cache and "net_inflow" in self._cache:
            return self._cache
        return self.fetch_market_flow()

    def get_trend_summary(self) -> str:
        """返回人类可读的趋势摘要 (用于注入 LLM prompt)"""
        data = self.get_latest()
        if not data:
            return "南向资金数据暂不可用"

        signal_labels = {
            "strong_inflow": "🟢 强劲流入",
            "inflow": "🟡 温和流入",
            "neutral": "⚪ 平衡",
            "outflow": "🟠 温和流出",
            "strong_outflow": "🔴 强劲流出",
        }

        summary = (
            f"南向资金 ({data['date']}): {data['net_inflow']:.1f} 亿 | "
            f"信号: {signal_labels.get(data['signal'], data['signal'])}"
        )
        if "trend_5d" in data:
            summary += f" | 5日均值: {data['trend_5d']:.1f} 亿"
        return summary
