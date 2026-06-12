"""
盘前扫描仪 (Pre-Market Scanner)

在 LLM 调用之前运行，提供硬数据 (技术指标 + 板块轮动)。
消除 LLM 对 RSI/MACD/ATR 等数值的幻觉，让 LLM 专注于推理和策略。

数据源:
  - 日线技术指标: Futu K_DAY (60 根日线) → LV3TechnicalCalculator
  - 板块轮动: Futu 批量快照 11 个 SPDR 板块 ETF
  - 降级: TwelveData daily time_series (仅在 Futu 失败时)

用法:
    scanner = PreMarketScanner(scheduler=scheduler)
    result = await scanner.run_full_scan(watchlist_symbols)
    # result["technicals"] → Dict[str, Dict]  每只标的的技术指标
    # result["sectors"]    → Dict              板块强弱排名
    # result["sector_symbol_map"] → Dict       标的→板块归属

版本: 1.0 (2026-06-12)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek.us_pipeline.premarket_scanner")

# 板块 ETF 映射 (复用 pre_market_ranker)
SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial": "XLF",
    "Consumer": "XLY",
    "ConsumerStaples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "RealEstate": "XLRE",
    "Communication": "XLC",
}

# 板块中文名映射 (用于 LLM prompt 可读性)
_SECTOR_CN_NAMES = {
    "Technology": "科技",
    "Healthcare": "医疗",
    "Financial": "金融",
    "Consumer": "可选消费",
    "ConsumerStaples": "必需消费",
    "Energy": "能源",
    "Industrials": "工业",
    "Materials": "材料",
    "Utilities": "公用事业",
    "RealEstate": "房地产",
    "Communication": "通信",
}

# 个股→板块映射 (复用 sector_limits)
_SYMBOL_SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "GOOG": "Technology", "META": "Technology", "NVDA": "Technology",
    "AMD": "Technology", "INTC": "Technology", "CRM": "Technology",
    "ORCL": "Technology", "AVGO": "Technology", "TSM": "Technology",
    "AMZN": "Consumer", "TSLA": "Consumer", "NKE": "Consumer",
    "MCD": "Consumer", "SBUX": "Consumer", "WMT": "ConsumerStaples",
    "COST": "ConsumerStaples", "HD": "Consumer", "PEP": "ConsumerStaples",
    "KO": "ConsumerStaples", "NIO": "Consumer", "XPEV": "Consumer",
    "LI": "Consumer", "PDD": "Consumer",
    "JPM": "Financial", "BAC": "Financial", "GS": "Financial",
    "MS": "Financial", "WFC": "Financial", "V": "Financial",
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare", "LLY": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "CAT": "Industrials", "BA": "Industrials", "HON": "Industrials",
    "GE": "Industrials", "LMT": "Industrials",
    "DIS": "Communication", "NFLX": "Communication", "CMCSA": "Communication",
    "BABA": "Technology", "JD": "Consumer", "BIDU": "Technology",
    "NTES": "Communication",
}


class PreMarketScanner:
    """盘前扫描仪 — 在 LLM 调用前提供硬数据"""

    def __init__(self, scheduler=None):
        self.scheduler = scheduler
        self._router = None
        self._futu = None
        self._calculator = None
        self._technicals_cache: Dict[str, Dict] = {}
        self._sector_cache: Dict[str, Any] = {}
        self._sector_cache_time: float = 0
        self._cache_ttl = 14400  # 技术指标缓存 4 小时 (日线数据日内不变)

    def _get_router(self):
        """懒加载 MarketDataRouter"""
        if self._router is None:
            try:
                from app.data.market_data_router import MarketDataRouter
                self._router = MarketDataRouter(
                    twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                    finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                    alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                    futu_opend_host=os.environ.get("OPEND_HOST", "127.0.0.1"),
                    futu_opend_port=int(os.environ.get("OPEND_PORT", "11111")),
                    enable_futu_lv3=True,
                )
            except Exception as e:
                logger.warning(f"[盘前扫描] MarketDataRouter 初始化失败: {e}")
        return self._router

    def _get_futu(self):
        """懒加载 FutuLV3Provider"""
        if self._futu is None:
            router = self._get_router()
            if router:
                self._futu = getattr(router, 'futu', None)
        return self._futu

    def _get_calculator(self):
        """懒加载 LV3TechnicalCalculator"""
        if self._calculator is None:
            try:
                from app.data.lv3_technical_calculator import LV3TechnicalCalculator
                self._calculator = LV3TechnicalCalculator()
            except Exception as e:
                logger.warning(f"[盘前扫描] LV3TechnicalCalculator 初始化失败: {e}")
        return self._calculator

    # ── 主入口 ──────────────────────────────────────────────────

    async def run_full_scan(self, watchlist: List[str]) -> Dict[str, Any]:
        """
        执行完整扫描: 日线技术指标 + 板块轮动

        Args:
            watchlist: 股票代码列表 (Futu 格式, 如 "US.AAPL")

        Returns:
            {
                "technicals": Dict[str, Dict],  # 每只标的的技术指标
                "sectors": Dict,                 # 板块强弱排名
                "sector_symbol_map": Dict,       # 标的→板块归属
                "scan_time": str,                # 扫描时间
            }
        """
        logger.info(f"[盘前扫描] 开始: {len(watchlist)} 只标的")
        start = time.time()

        # 并行执行技术分析和板块扫描 (通过 to_thread 避免阻塞 event loop)
        # 2026-06-12 修复: Futu TCP 调用和 pandas 计算是同步阻塞的,
        # 直接 await 会卡住整个 asyncio event loop (信号扫描/持仓监控全部暂停)
        technicals, sectors = await asyncio.gather(
            asyncio.to_thread(self._compute_daily_technicals_sync, watchlist),
            asyncio.to_thread(self._scan_sector_rotation_sync),
        )

        sector_map = self._map_symbols_to_sectors(watchlist, sectors)

        elapsed = time.time() - start
        result = {
            "technicals": technicals,
            "sectors": sectors,
            "sector_symbol_map": sector_map,
            "scan_time": datetime.now().isoformat(),
        }

        # 保存扫描缓存 (供 Dashboard 读取)
        self._save_scan_cache(result)

        logger.info(
            f"[盘前扫描] 完成 ({elapsed:.1f}s): "
            f"{len(technicals)} 只技术指标, "
            f"{len(sectors.get('ranked', []))} 个板块"
        )
        return result

    # ── 日线技术指标 ────────────────────────────────────────────

    async def compute_daily_technicals(self, symbols: List[str]) -> Dict[str, Dict]:
        """Async wrapper — delegates to sync implementation via to_thread"""
        return await asyncio.to_thread(self._compute_daily_technicals_sync, symbols)

    def _compute_daily_technicals_sync(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        计算 watchlist 中每只标的的日线技术指标 (同步实现)

        数据流: Futu K_DAY (60 根日线) → LV3TechnicalCalculator
        降级: TwelveData get_time_series(daily, 60)

        Returns:
            {
                "US.AAPL": {
                    "rsi": 45.2,
                    "macd": 0.34, "macd_signal": 0.28, "macd_hist": 0.06,
                    "atr": 3.2, "atr_percent": 1.8,
                    "ma20": 178.5, "ma50": 175.2,
                    "bb_upper": 185.0, "bb_middle": 178.5, "bb_lower": 172.0,
                    "trend": "UP",
                    "volume_ratio": 1.3,
                    "price": 180.2,
                    "change_5d_pct": 2.1,
                    "supertrend": 176.8,
                },
                ...
            }
        """
        results = {}
        futu = self._get_futu()
        calc = self._get_calculator()
        router = self._get_router()

        if not calc:
            logger.error("[盘前扫描] 技术指标计算器不可用")
            return results

        for symbol in symbols:
            if not symbol:
                continue

            try:
                klines = None

                # ① Futu K_DAY (主要数据源)
                if futu and hasattr(futu, 'get_daily_kline'):
                    klines = futu.get_daily_kline(symbol, count=60)
                    if klines and len(klines) >= 30:
                        logger.debug(f"[盘前扫描] {symbol}: Futu 日线 {len(klines)} 根")

                # ② TwelveData 降级
                if not klines or len(klines) < 30:
                    klines = self._fetch_daily_from_twelvedata(symbol)

                if not klines or len(klines) < 30:
                    logger.debug(f"[盘前扫描] {symbol}: 日线数据不足, 跳过")
                    continue

                # 计算技术指标
                tech = calc.calculate_from_klines(klines)
                if not tech:
                    continue

                # 获取最新价格 (用 get_quote 覆盖, 更实时)
                quote = None
                if router:
                    try:
                        quote = router.get_quote(symbol)
                    except Exception:
                        pass

                current_price = 0
                if quote:
                    current_price = float(quote.get("price", 0) or 0)
                if current_price <= 0:
                    current_price = tech.get("close", klines[-1].get("close", 0))

                # 构建结果 (精简字段, 只保留 LLM 需要的)
                results[symbol] = {
                    "rsi": round(tech.get("rsi", 50), 1),
                    "macd": round(tech.get("macd", 0), 4),
                    "macd_signal": round(tech.get("macd_signal", 0), 4),
                    "macd_hist": round(tech.get("macd_histogram", 0), 4),
                    "atr": round(tech.get("atr", 0), 2),
                    "atr_percent": round(tech.get("atr_percent", 0), 2),
                    "ma20": round(tech.get("ma20", 0), 2),
                    "ma50": round(tech.get("ma50", 0), 2),
                    "bb_upper": round(tech.get("bb_upper", 0), 2),
                    "bb_middle": round(tech.get("bb_middle", 0), 2),
                    "bb_lower": round(tech.get("bb_lower", 0), 2),
                    "trend": tech.get("trend", "NEUTRAL"),
                    "volume_ratio": round(tech.get("volume_ratio", 1.0), 2),
                    "price": round(current_price, 2),
                    "supertrend": round(tech.get("supertrend", 0), 2),
                    "change_5d_pct": round(tech.get("change_5d_pct", 0), 2),
                }

                # 礼貌节流 (Futu OpenD 无硬限制, 但避免瞬间大量请求)
                time.sleep(0.1)

            except Exception as e:
                logger.warning(f"[盘前扫描] {symbol} 技术分析失败: {e}")
                continue

        return results

    def _fetch_daily_from_twelvedata(self, symbol: str) -> Optional[List[Dict]]:
        """TwelveData 降级: 获取日线数据

        注意: TwelveData.get_time_series() 返回处理后的 dict (仅含 close_prices),
        不含完整 OHLCV, 无法直接喂给 LV3TechnicalCalculator。
        因此 TwelveData 降级仅作为 Futu 不可用时的最后兜底 — 不做技术分析,
        仅返回 None 让调用方跳过该标的。
        """
        # TwelveData 返回的数据格式不兼容 LV3TechnicalCalculator (缺 OHLCV)
        # Futu 是主要数据源且无 API 限制, TwelveData 降级场景极少
        logger.debug(f"[盘前扫描] {symbol}: TwelveData 降级不可用 (缺 OHLCV), 跳过")
        return None

    # ── 板块轮动扫描 ────────────────────────────────────────────

    async def scan_sector_rotation(self) -> Dict[str, Any]:
        """Async wrapper — delegates to sync implementation via to_thread"""
        return await asyncio.to_thread(self._scan_sector_rotation_sync)

    def _scan_sector_rotation_sync(self) -> Dict[str, Any]:
        """
        扫描 11 个 SPDR 板块 ETF 的强弱 (同步实现)

        数据: MarketDataRouter.get_batch_quotes() — 1 次 Futu 批量快照
        排序: 按涨跌幅排名, 输出 top 3 强 / bottom 3 弱

        Returns:
            {
                "ranked": [{"sector": "Technology", "etf": "XLK", "change_pct": 1.2, "price": 220.5}, ...],
                "strong": [{"sector": "Technology", "etf": "XLK", "change_pct": 1.2}, ...],  # top 3
                "weak": [{"sector": "Utilities", "etf": "XLU", "change_pct": -0.8}, ...],    # bottom 3
                "summary": {"strong": ["XLK", "XLC"], "weak": ["XLU", "XLE"]},
            }
        """
        router = self._get_router()
        if not router:
            return {"ranked": [], "strong": [], "weak": [], "summary": {"strong": [], "weak": []}}

        # 构建批量查询的 symbol 列表
        etf_symbols = [f"US.{etf}" for etf in SECTOR_ETFS.values()]

        try:
            quotes = router.get_batch_quotes(etf_symbols)
        except Exception as e:
            logger.warning(f"[盘前扫描] 板块 ETF 批量报价失败: {e}")
            return {"ranked": [], "strong": [], "weak": [], "summary": {"strong": [], "weak": []}}

        if not quotes:
            return {"ranked": [], "strong": [], "weak": [], "summary": {"strong": [], "weak": []}}

        # 解析结果
        ranked = []
        for sector_name, etf in SECTOR_ETFS.items():
            sym = f"US.{etf}"
            q = quotes.get(sym, {})
            change_pct = float(q.get("change_percent", 0) or 0)
            price = float(q.get("price", 0) or 0)

            if price > 0:
                ranked.append({
                    "sector": sector_name,
                    "sector_cn": _SECTOR_CN_NAMES.get(sector_name, sector_name),
                    "etf": etf,
                    "change_pct": round(change_pct, 2),
                    "price": round(price, 2),
                })

        # 按涨跌幅排序
        ranked.sort(key=lambda x: x["change_pct"], reverse=True)

        strong = ranked[:3] if len(ranked) >= 3 else ranked
        weak = ranked[-3:] if len(ranked) >= 3 else ranked
        weak = list(reversed(weak))  # 弱势: 跌幅最大在前

        return {
            "ranked": ranked,
            "strong": strong,
            "weak": weak,
            "summary": {
                "strong": [s["etf"] for s in strong],
                "weak": [w["etf"] for w in weak],
            },
        }

    # ── 标的→板块映射 ──────────────────────────────────────────

    def _map_symbols_to_sectors(
        self, watchlist: List[str], sectors: Dict
    ) -> Dict[str, Dict]:
        """
        将 watchlist 标的映射到所属板块, 并标注板块强弱

        Returns:
            {"US.AAPL": {"sector": "Technology", "sector_etf": "XLK",
                         "sector_cn": "科技", "sector_strength": "STRONG"}, ...}
        """
        strong_etfs = set(sectors.get("summary", {}).get("strong", []))
        weak_etfs = set(sectors.get("summary", {}).get("weak", []))

        result = {}
        for symbol in watchlist:
            if not symbol:
                continue
            clean = symbol.replace("US.", "")
            sector = _SYMBOL_SECTOR_MAP.get(clean)

            # 降级: 从 sector_limits 查找
            if not sector:
                try:
                    from app.risk.sector_limits import DEFAULT_SECTOR_MAPPING, CHINA_CONCEPT_MAPPING
                    for s, syms in {**DEFAULT_SECTOR_MAPPING, **CHINA_CONCEPT_MAPPING}.items():
                        if clean in syms:
                            sector = s
                            break
                except Exception:
                    pass

            if not sector:
                sector = "Unknown"

            etf = SECTOR_ETFS.get(sector, "")
            if etf in strong_etfs:
                strength = "STRONG"
            elif etf in weak_etfs:
                strength = "WEAK"
            else:
                strength = "NEUTRAL"

            result[symbol] = {
                "sector": sector,
                "sector_etf": etf,
                "sector_cn": _SECTOR_CN_NAMES.get(sector, sector),
                "sector_strength": strength,
            }

        return result

    # ── LLM Prompt 格式化 ───────────────────────────────────────

    def format_technicals_for_llm(self, technicals: Dict[str, Dict]) -> str:
        """格式化技术指标为 LLM prompt 注入文本"""
        if not technicals:
            return "⚠️ 技术指标扫描不可用，请基于市场数据进行分析。"

        lines = ["【技术指标】(日线级别, 基于真实数据计算 — 请勿编造数值)"]
        lines.append("")

        for symbol, tech in technicals.items():
            rsi = tech.get("rsi", 50)
            rsi_label = "超买" if rsi > 70 else ("超卖" if rsi < 30 else "中性")

            macd_hist = tech.get("macd_hist", 0)
            macd_label = "金叉" if macd_hist > 0 else "死叉"

            trend = tech.get("trend", "NEUTRAL")
            trend_cn = {"UP": "上升趋势", "DOWN": "下降趋势", "NEUTRAL": "横盘"}.get(trend, "横盘")

            atr = tech.get("atr", 0)
            atr_pct = tech.get("atr_percent", 0)

            price = tech.get("price", 0)
            ma20 = tech.get("ma20", 0)
            ma50 = tech.get("ma50", 0)
            bb_upper = tech.get("bb_upper", 0)
            bb_lower = tech.get("bb_lower", 0)

            vol_ratio = tech.get("volume_ratio", 1.0)
            chg5d = tech.get("change_5d_pct", 0)

            lines.append(
                f"  {symbol}: RSI={rsi}({rsi_label}) | MACD{macd_label}(hist={macd_hist:+.4f}) | "
                f"趋势{trend_cn} | ATR=${atr:.2f}({atr_pct:.1f}%)"
            )
            lines.append(
                f"    MA20=${ma20:.2f} MA50=${ma50:.2f} | "
                f"BB:[${bb_lower:.2f}-${bb_upper:.2f}] 现价=${price:.2f}"
            )
            lines.append(
                f"    量比={vol_ratio:.1f}x | 5日涨幅={chg5d:+.1f}%"
            )

        return "\n".join(lines)

    def format_sectors_for_llm(self, sectors: Dict) -> str:
        """格式化板块轮动为 LLM prompt 注入文本"""
        ranked = sectors.get("ranked", [])
        strong = sectors.get("strong", [])
        weak = sectors.get("weak", [])

        if not ranked:
            return "⚠️ 板块数据不可用。"

        lines = ["【板块轮动】(按今日涨跌幅排序)"]
        lines.append("")

        # 强势板块
        strong_parts = []
        for s in strong:
            cn = s.get("sector_cn", s["sector"])
            strong_parts.append(f"{cn}({s['etf']} {s['change_pct']:+.2f}%)")
        if strong_parts:
            lines.append(f"  🔥 强势板块: {', '.join(strong_parts)}")

        # 弱势板块
        weak_parts = []
        for w in weak:
            cn = w.get("sector_cn", w["sector"])
            weak_parts.append(f"{cn}({w['etf']} {w['change_pct']:+.2f}%)")
        if weak_parts:
            lines.append(f"  ❄️ 弱势板块: {', '.join(weak_parts)}")

        # 全部排名
        all_parts = [f"{r['etf']}({r['change_pct']:+.2f}%)" for r in ranked]
        lines.append(f"  📊 全部排名: {' > '.join(all_parts)}")

        lines.append("")
        lines.append(
            "  > 建议: LONG 优先选择强势板块标的, SHORT 优先选择弱势板块标的"
        )

        return "\n".join(lines)

    def format_sector_for_symbol(self, symbol: str, sector_map: Dict) -> str:
        """格式化单个标的的板块信息"""
        info = sector_map.get(symbol, {})
        if not info:
            return ""
        sector = info.get("sector_cn", info.get("sector", "Unknown"))
        strength = info.get("sector_strength", "NEUTRAL")
        strength_cn = {"STRONG": "强势", "WEAK": "弱势", "NEUTRAL": "中性"}.get(strength, "中性")
        return f"{sector}({strength_cn})"

    def format_hard_constraints(self, technicals: Dict[str, Dict],
                                no_trade_windows: List[Dict]) -> str:
        """格式化硬约束规则为 LLM prompt 注入文本"""
        lines = ["【硬约束规则】(你的输出必须遵守以下规则, 违反将被系统自动修正)"]
        lines.append("")
        lines.append("  1. 止损计算: stop_loss = entry_zone 中点 ∓ (1.5 × ATR)")
        lines.append("     ATR 值见【技术指标】, 例: entry=$180, ATR=$3.2 → LONG stop_loss=$175.2")
        lines.append("  2. 风险收益比: |take_profit - entry| / |stop_loss - entry| 必须 ≥ 2.0")
        lines.append("  3. RSI 限制: 日线 RSI > 75 时禁止 LONG (超买), RSI < 25 时禁止 SHORT (超卖)")
        lines.append("  4. 板块对齐: LONG 优先选择强势板块标的, SHORT 优先选择弱势板块标的")
        lines.append("  5. 趋势一致: 日线 trend=DOWN 时不做多, trend=UP 时不做空")
        lines.append("  6. 入场区: entry_zone 必须在当前价的 ±3% 范围内 (基于【实时价格校准】)")

        if no_trade_windows:
            lines.append("  7. 禁止交易窗口 (经济数据发布, 波动率极高):")
            for w in no_trade_windows:
                lines.append(f"     • {w['start']}-{w['end']} ET: {w['reason']} ({w['impact']} 影响)")

        return "\n".join(lines)

    # ── 扫描缓存 ────────────────────────────────────────────────

    def _save_scan_cache(self, result: Dict):
        """保存扫描结果到文件 (供 Dashboard 读取)"""
        try:
            cache_path = Path(__file__).parent.parent.parent / "data" / "premarket_scan_cache.json"
            os.makedirs(cache_path.parent, exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            logger.debug(f"[盘前扫描] 缓存已保存: {cache_path}")
        except Exception as e:
            logger.warning(f"[盘前扫描] 缓存保存失败: {e}")

    @staticmethod
    def load_scan_cache() -> Optional[Dict]:
        """加载扫描缓存 (Dashboard 使用)"""
        try:
            cache_path = Path(__file__).parent.parent.parent / "data" / "premarket_scan_cache.json"
            if cache_path.exists():
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return None
