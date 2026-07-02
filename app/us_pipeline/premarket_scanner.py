"""
美股盘前扫描仪 (US PreMarket Scanner)

职责:
1. 盘前批量扫描 watchlist，计算日线技术指标 (RSI/MACD/ATR/趋势等)
2. 板块轮动扫描 (11个 SPDR 板块 ETF 涨跌排名)
3. 经济事件检查 (今日/明日关键事件)
4. 输出格式化的扫描报告 (供 LLM planner 和 Dashboard 使用)

复用:
- LV3TechnicalCalculator: 技术指标计算 (已有完整实现)
- MarketDataRouter: 统一数据源路由
- EconomicCalendar: 经济日历查询
- FutuLV3Provider.get_daily_kline(): 日线K线数据

输出:
- premarket_scan_cache.json: 扫描结果缓存 (供 Dashboard 读取)
- 格式化文本: 供 LLM prompt 注入
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
import pytz

logger = logging.getLogger("ftseek_us.us_pipeline.premarket_scanner")


class USPreMarketScanner:
    """美股盘前扫描仪"""
    
    # SPDR 板块 ETF 映射
    SECTOR_ETFS = {
        "XLK": "Technology",
        "XLF": "Financials",
        "XLE": "Energy",
        "XLV": "Healthcare",
        "XLY": "Consumer Discretionary",
        "XLP": "Consumer Staples",
        "XLI": "Industrials",
        "XLB": "Materials",
        "XLRE": "Real Estate",
        "XLU": "Utilities",
        "XLC": "Communication Services"
    }
    
    def __init__(
        self,
        data_router,
        economic_calendar,
        cache_path: str = "data/premarket_scan_cache.json"
    ):
        """
        初始化盘前扫描仪
        
        Args:
            data_router: MarketDataRouter 实例
            economic_calendar: EconomicCalendar 实例
            cache_path: 扫描结果缓存路径
        """
        self.data_router = data_router
        self.calendar = economic_calendar
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 复用技术指标计算器
        from app.data.lv3_technical_calculator import LV3TechnicalCalculator
        self.tech_calculator = LV3TechnicalCalculator()
    
    def run_full_scan(
        self,
        watchlist: List[str],
        scan_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        运行完整扫描
        
        Args:
            watchlist: 扫描标的列表 (如 ["AAPL", "MSFT", "NVDA"])
            scan_date: 扫描日期 (默认今天 ET)
        
        Returns:
            扫描结果字典:
            {
                "scan_date": "2026-06-18",
                "scan_time": "2026-06-18T08:30:00-04:00",
                "targets": {
                    "AAPL": {"technical_summary": "...", "indicators": {...}},
                    ...
                },
                "sector_rotation": {
                    "strongest": ["XLK", "XLF"],
                    "weakest": ["XLU", "XLRE"],
                    "all_sectors": {...}
                },
                "economic_events": [...],
                "no_trade_windows": [...],
                "summary": "..."
            }
        """
        if scan_date is None:
            et_tz = pytz.timezone("US/Eastern")
            scan_date = datetime.now(et_tz).strftime("%Y-%m-%d")
        
        logger.info(f"[PreMarketScanner] 开始扫描 {len(watchlist)} 标的 ({scan_date})")
        
        # 1. 扫描所有标的技术指标
        targets = {}
        for symbol in watchlist:
            try:
                tech_data = self._scan_symbol_technicals(symbol)
                if tech_data:
                    targets[symbol] = tech_data
                    logger.debug(f"  ✅ {symbol}: RSI={tech_data['indicators'].get('rsi', 'N/A')}")
            except Exception as e:
                logger.warning(f"  ❌ {symbol}: {e}")
        
        logger.info(f"  技术指标扫描完成: {len(targets)}/{len(watchlist)} 成功")
        
        # 2. 板块轮动扫描
        sector_rotation = self._scan_sector_rotation()
        
        # 3. 经济事件检查
        economic_events = self.calendar.get_todays_events(scan_date)
        no_trade_windows = self.calendar.get_no_trade_windows(scan_date)
        
        # 4. 生成摘要
        summary = self._generate_summary(targets, sector_rotation, economic_events)
        
        # 5. 组装结果
        result = {
            "scan_date": scan_date,
            "scan_time": datetime.now(pytz.timezone("US/Eastern")).isoformat(),
            "targets": targets,
            "sector_rotation": sector_rotation,
            "economic_events": economic_events,
            "no_trade_windows": no_trade_windows,
            "summary": summary
        }
        
        # 6. 保存缓存
        self._save_cache(result)
        
        logger.info(f"[PreMarketScanner] 扫描完成，结果已保存到 {self.cache_path}")
        
        return result
    
    def _scan_symbol_technicals(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        扫描单个标的的技术指标
        
        Returns:
            {
                "symbol": "AAPL",
                "indicators": {
                    "rsi": 65.2,
                    "macd": {...},
                    "atr": 3.5,
                    "ma20": 180.5,
                    "ma50": 175.2,
                    "trend": "UP",
                    ...
                },
                "technical_summary": "RSI=65.2 (中性偏强), MACD金叉, 趋势UP, ATR=3.5 (1.9%)"
            }
        """
        # 获取日线K线 (复用 FutuLV3Provider.get_daily_kline)
        full_symbol = f"US.{symbol}" if not symbol.startswith("US.") else symbol
        
        # 2026-06-28 修复: 走Router.get_daily_bars()而非直接调futu
        # 原因: 富途模拟环境历史K线停滞在2025年, Router有TwelveData fallback
        try:
            klines = self.data_router.get_daily_bars(full_symbol, count=60)
        except Exception as e:
            logger.warning(f"获取 {full_symbol} 日线失败: {e}")
            return None
        
        if not klines or len(klines) < 30:
            logger.warning(f"{full_symbol} K线数据不足: {len(klines) if klines else 0}")
            return None
        
        # 计算技术指标 (复用 LV3TechnicalCalculator)
        indicators = self.tech_calculator.calculate_from_klines(klines)
        
        if not indicators:
            return None
        
        # 生成技术摘要文本
        technical_summary = self._format_technical_summary(symbol, indicators)
        
        return {
            "symbol": symbol,
            "indicators": indicators,
            "technical_summary": technical_summary
        }
    
    def _format_technical_summary(self, symbol: str, indicators: Dict) -> str:
        """生成技术指标摘要文本 (供 LLM 使用)"""
        parts = []
        
        # RSI
        rsi = indicators.get("rsi")
        if rsi is not None:
            if rsi > 70:
                parts.append(f"RSI={rsi:.1f} (超买)")
            elif rsi < 30:
                parts.append(f"RSI={rsi:.1f} (超卖)")
            elif rsi > 60:
                parts.append(f"RSI={rsi:.1f} (偏强)")
            elif rsi < 40:
                parts.append(f"RSI={rsi:.1f} (偏弱)")
            else:
                parts.append(f"RSI={rsi:.1f} (中性)")
        
        # MACD
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        if macd is not None and macd_signal is not None:
            if macd > macd_signal:
                parts.append("MACD金叉")
            else:
                parts.append("MACD死叉")
        
        # 趋势
        trend = indicators.get("trend")
        if trend:
            parts.append(f"趋势{trend}")
        
        # ATR (波动率)
        atr = indicators.get("atr")
        price = indicators.get("price")
        if atr and price:
            atr_pct = (atr / price) * 100
            parts.append(f"ATR={atr:.2f} ({atr_pct:.1f}%)")
        
        # 均线位置
        ma20 = indicators.get("ma20")
        ma50 = indicators.get("ma50")
        if price and ma20 and ma50:
            if price > ma20 > ma50:
                parts.append("价格在MA20/MA50上方 (多头排列)")
            elif price < ma20 < ma50:
                parts.append("价格在MA20/MA50下方 (空头排列)")
        
        return ", ".join(parts)
    
    def _scan_sector_rotation(self) -> Dict[str, Any]:
        """
        板块轮动扫描
        
        Returns:
            {
                "strongest": ["XLK", "XLF"],  # 今日涨幅前3
                "weakest": ["XLU", "XLRE"],   # 今日跌幅前3
                "all_sectors": {
                    "XLK": {"name": "Technology", "change_pct": 1.5, ...},
                    ...
                }
            }
        """
        sector_etfs = list(self.SECTOR_ETFS.keys())
        full_symbols = [f"US.{etf}" for etf in sector_etfs]
        
        # 批量获取行情 (复用 MarketDataRouter)
        quotes = self.data_router.get_batch_quotes(full_symbols)
        
        # 解析涨跌幅
        all_sectors = {}
        changes = []
        
        for etf, sector_name in self.SECTOR_ETFS.items():
            full_symbol = f"US.{etf}"
            quote = quotes.get(full_symbol, {})
            
            if not quote:
                continue
            
            price = quote.get("price") or quote.get("last_price")
            prev_close = quote.get("prev_close_price")
            
            if price and prev_close and prev_close > 0:
                change_pct = ((price - prev_close) / prev_close) * 100
            else:
                change_pct = 0.0
            
            all_sectors[etf] = {
                "name": sector_name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2)
            }
            
            changes.append((etf, change_pct))
        
        # 排序
        changes.sort(key=lambda x: x[1], reverse=True)
        
        strongest = [etf for etf, _ in changes[:3]]
        weakest = [etf for etf, _ in changes[-3:]]
        
        return {
            "strongest": strongest,
            "weakest": weakest,
            "all_sectors": all_sectors
        }
    
    def _generate_summary(
        self,
        targets: Dict[str, Any],
        sector_rotation: Dict[str, Any],
        economic_events: List[Dict]
    ) -> str:
        """生成扫描摘要"""
        parts = []
        
        # 技术指标统计
        rsi_overbought = sum(
            1 for t in targets.values()
            if t["indicators"].get("rsi", 50) > 70
        )
        rsi_oversold = sum(
            1 for t in targets.values()
            if t["indicators"].get("rsi", 50) < 30
        )
        
        if rsi_overbought > 0:
            parts.append(f"{rsi_overbought}个标的超买(RSI>70)")
        if rsi_oversold > 0:
            parts.append(f"{rsi_oversold}个标的超卖(RSI<30)")
        
        # 板块轮动
        strongest = sector_rotation.get("strongest", [])
        weakest = sector_rotation.get("weakest", [])
        
        if strongest:
            strongest_names = [str(self.SECTOR_ETFS.get(e, e)) for e in strongest]
            parts.append(f"强势板块: {', '.join(strongest_names)}")
        
        if weakest:
            weakest_names = [str(self.SECTOR_ETFS.get(e, e)) for e in weakest]
            parts.append(f"弱势板块: {', '.join(weakest_names)}")
        
        # 经济事件
        high_impact_events = [
            e for e in economic_events
            if e.get("impact") == "HIGH"
        ]
        
        if high_impact_events:
            parts.append(f"今日{len(high_impact_events)}个高影响经济事件")
        
        return "; ".join(parts) if parts else "无特殊信号"
    
    def _save_cache(self, result: Dict[str, Any]):
        """保存扫描结果到缓存文件"""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存扫描缓存失败: {e}")
    
    def format_for_llm(self, result: Dict[str, Any]) -> str:
        """
        格式化扫描结果为 LLM prompt 文本
        
        Returns:
            可直接注入到 LLM prompt 的文本
        """
        lines = []
        
        lines.append("## 盘前技术扫描报告")
        lines.append(f"扫描时间: {result['scan_time']}")
        lines.append("")
        
        # 技术指标
        lines.append("### 标的技术指标")
        for symbol, data in result["targets"].items():
            lines.append(f"**{symbol}**: {data['technical_summary']}")
        lines.append("")
        
        # 板块轮动
        sector_rotation = result["sector_rotation"]
        lines.append("### 板块轮动")
        
        if sector_rotation["strongest"]:
            strongest_names = [
                str(self.SECTOR_ETFS.get(e, e))
                for e in sector_rotation["strongest"]
            ]
            lines.append(f"强势板块: {', '.join(strongest_names)}")
        
        if sector_rotation["weakest"]:
            weakest_names = [
                str(self.SECTOR_ETFS.get(e, e))
                for e in sector_rotation["weakest"]
            ]
            lines.append(f"弱势板块: {', '.join(weakest_names)}")
        lines.append("")
        
        # 经济事件
        events = result["economic_events"]
        if events:
            lines.append("### 今日经济事件")
            for event in events:
                impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(
                    event.get("impact", "LOW"), "⚪"
                )
                lines.append(f"{impact_emoji} {event['time']} {event['name']} ({event['impact']})")
            lines.append("")
        
        # 禁止交易窗口
        windows = result["no_trade_windows"]
        if windows:
            lines.append("### ⚠️ 禁止交易窗口")
            for window in windows:
                lines.append(
                    f"🚫 {window['start_time']} - {window['end_time']}: "
                    f"{window['event_name']} ({window['impact']})"
                )
            lines.append("")
        
        # 摘要
        lines.append(f"**总结**: {result['summary']}")
        
        return "\n".join(lines)
