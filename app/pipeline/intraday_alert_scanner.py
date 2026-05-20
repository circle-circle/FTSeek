"""
盘中异动扫描器 — 交易时段每 30 分钟运行一次，纯规则驱动。

流程:
  1. 扫描 watchlist 所有港股的实时快照
  2. 筛选量比 > 2.0x 且涨幅 > 1.5% 的标的
  3. 加入动态池 INTRADAY_ALERT 层 (2h TTL)
  4. 触发 Push 订阅 (DataSubscriber._sync_subscription)

设计:
  - 比午间刷新器门槛更低 (2.0x vs 3.0x, 1.5% vs 2%)
  - 频率更高 (30min vs 12:00 一次性)
  - 自动订阅异动标的，让共识引擎开始监控
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.pipeline.intraday_alert")

# 盘中异动门槛 (比午间更灵敏，因为是持续扫描)
INTRADAY_MIN_VOLUME_RATIO = 2.0      # 量比 > 2x
INTRADAY_MIN_PRICE_CHANGE = 0.015    # 涨幅 > 1.5% (防噪音)
INTRADAY_MAX_ALERTS = 15             # 同时最多 15 只异动标的


class IntradayAlertScanner:
    """盘中异动扫描器 — 持续监控 watchlist 量价异动"""

    def __init__(self, quote_ctx=None):
        self._quote_ctx = quote_ctx
        self._last_alerts = {}  # code -> last_alert_time, 防重复

    def scan(self) -> List[Dict]:
        """执行一次异动扫描，返回新增的异动标的列表"""
        results = []
        
        try:
            from app import config
            watchlist = getattr(config, 'WATCH_LIST_HK', [])
        except Exception:
            return results

        if not self._quote_ctx or not watchlist:
            return results

        # 分批获取快照 (Futu API 每次最多 300 只)
        batch_size = 200
        for i in range(0, len(watchlist), batch_size):
            batch = watchlist[i:i + batch_size]
            try:
                ret, data = self._quote_ctx.get_market_snapshot(batch)
                if ret != 0 or data is None or data.empty:
                    continue
                    
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    if not code.startswith("HK."):
                        continue
                    
                    vol_ratio = float(row.get("volume_ratio", 0) or 0)
                    change_pct = float(row.get("change_rate", 0) or 0) / 100.0
                    
                    if vol_ratio >= INTRADAY_MIN_VOLUME_RATIO and abs(change_pct) >= INTRADAY_MIN_PRICE_CHANGE:
                        # 防重复: 同一标的 2 小时内不重复报警
                        now = datetime.now()
                        last_time = self._last_alerts.get(code)
                        if last_time and (now - last_time).total_seconds() < 7200:
                            continue
                        
                        direction = "LONG" if change_pct > 0 else "SHORT"
                        alert = {
                            "code": code,
                            "direction": direction,
                            "vol_ratio": vol_ratio,
                            "change_pct": change_pct,
                            "last_price": float(row.get("last_price", 0)),
                            "trigger": f"量比{vol_ratio:.1f}x 涨跌{change_pct:+.1%}",
                        }
                        results.append(alert)
                        self._last_alerts[code] = now
                        logger.info(
                            f"[盘中异动] {code} | {direction} | "
                            f"量比={vol_ratio:.1f}x | 涨跌={change_pct:+.1%} | "
                            f"价格={alert['last_price']:.2f}"
                        )
            except Exception as e:
                logger.debug(f"[盘中异动] 批次 {i}-{i+batch_size} 扫描失败: {e}")
                continue

        return results

    def add_to_pool(self, alerts: List[Dict]) -> int:
        """将异动标的加入动态池"""
        if not alerts:
            return 0

        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            added = 0
            
            for alert in alerts:
                try:
                    pool.add_intraday_alert(
                        code=alert["code"],
                        reason=alert["trigger"],
                    )
                    added += 1
                except Exception as e:
                    logger.debug(f"[盘中异动] 加入动态池失败 {alert['code']}: {e}")
            
            if added:
                logger.info(f"[盘中异动] {added} 只标的已加入动态池")
            
            return added
        except Exception as e:
            logger.error(f"[盘中异动] 动态池操作失败: {e}")
            return 0


def get_intraday_scanner(quote_ctx=None) -> IntradayAlertScanner:
    """获取或创建单例扫描器"""
    if not hasattr(get_intraday_scanner, '_instance'):
        get_intraday_scanner._instance = IntradayAlertScanner(quote_ctx=quote_ctx)
    elif quote_ctx and get_intraday_scanner._instance._quote_ctx is None:
        get_intraday_scanner._instance._quote_ctx = quote_ctx
    return get_intraday_scanner._instance
