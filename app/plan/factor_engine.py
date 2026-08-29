"""
Factor Engine — 量化预筛 (Stage 1 of Battle Plan Pipeline)

5因子等权评分，从~35只活跃股中选出Top 8候选。
纯Python计算，无LLM依赖，可独立验证因子有效性。

因子:
  1. 板块动量 (sector_momentum) — 所属板块ETF 5日涨幅
  2. 相对强度 (relative_strength) — 个股20日涨幅 vs SPY
  3. 均线排列 (ma_alignment) — MA5>MA10>MA20多空信号
  4. 量价突破 (volume_breakout) — 当日量/20日均量
  5. RSI甜区 (rsi_sweet_spot) — RSI 30-50超卖反弹最优

Regime条件权重:
  RISK_ON:  板块30% 相对强度25% 均线20% 量价15% RSI10%
  RISK_OFF: 板块10% 相对强度10% 均线30% 量价10% RSI40%
  ROTATION: 板块40% 相对强度20% 均线15% 量价15% RSI10%
  NEUTRAL:  等权各20%
"""

import logging
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek_us.factor_engine")

# ── 股票池: 活跃池中去掉ETF和非美股 ──
_EXCLUDE_PREFIXES = {"US.HK."}
_EXCLUDE_SYMBOLS = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM",
                    "US.XLK", "US.XLF", "US.XLI", "US.XLV",
                    "US.XLE", "US.XLY", "US.XLP", "US.XLB",
                    "US.XLRE", "US.XLU", "US.XLC"}

# ── 板块映射 (ticker → sector ETF symbol) ──
_SECTOR_MEMBERS = {
    "Technology": ["NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMD", "ORCL", "CRM"],
    "Semiconductors": ["NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM"],
    "Consumer": ["AMZN", "TSLA", "NKE", "WMT", "SBUX"],
    "Financials": ["JPM", "BAC", "GS", "MS", "V", "MA"],
    "Healthcare": ["JNJ", "UNH", "LLY", "PFE", "MRK"],
    "Energy": ["XOM", "CVX", "COP"],
    "China_ADR": ["BABA", "PDD", "JD", "BIDU", "NIO", "NTES", "BILI", "LI"],
    "Communication": ["GOOG", "GOOGL", "META", "NFLX", "DIS"],
}

_TICKER_TO_SECTORS: Dict[str, List[str]] = {}
for _sec, _tickers in _SECTOR_MEMBERS.items():
    for _t in _tickers:
        _TICKER_TO_SECTORS.setdefault(_t, []).append(_sec)

# ── Regime权重矩阵 (P2: 2026-06-21 加入基本面+情绪因子) ──
# 技术面权重 85%→65%, 基本面+情绪 0%→35%
# insider_signal 保留但降权 (insider本身也是基本面)
REGIME_WEIGHTS = {
    "RISK_ON":  {"sector_momentum": 0.18, "relative_strength": 0.15, "ma_alignment": 0.12, "volume_breakout": 0.12, "rsi_sweet_spot": 0.08, "insider_signal": 0.12, "fundamental_quality": 0.13, "sentiment_crowding": 0.10},
    "RISK_OFF": {"sector_momentum": 0.08, "relative_strength": 0.08, "ma_alignment": 0.20, "volume_breakout": 0.08, "rsi_sweet_spot": 0.24, "insider_signal": 0.12, "fundamental_quality": 0.12, "sentiment_crowding": 0.08},
    "ROTATION": {"sector_momentum": 0.24, "relative_strength": 0.12, "ma_alignment": 0.12, "volume_breakout": 0.08, "rsi_sweet_spot": 0.08, "insider_signal": 0.16, "fundamental_quality": 0.12, "sentiment_crowding": 0.08},
    "NEUTRAL":  {"sector_momentum": 0.13, "relative_strength": 0.13, "ma_alignment": 0.13, "volume_breakout": 0.13, "rsi_sweet_spot": 0.13, "insider_signal": 0.12, "fundamental_quality": 0.12, "sentiment_crowding": 0.11},
}


# ── 质量闸门常量 ──
_QUALITY_GATE_MIN_PRICE = 5.0       # 最低价格 $5
_LEVERAGED_KEYWORDS = {"3X", "2X", "BULL", "BEAR", "ULTRA", "INVERSE"}


def _load_active_pool() -> Tuple[List[str], set]:
    """加载核心活跃池 (原 _get_stock_pool 逻辑)
    
    Returns:
        (symbols, position_symbols): 过滤后的符号列表 + 持仓符号集合(闸门豁免用)
    """
    pool_path = Path(__file__).parent.parent.parent / "data" / "daily_active_pool.json"
    if not pool_path.exists():
        logger.warning("[FactorEngine] daily_active_pool.json not found")
        return [], set()
    try:
        data = json.loads(pool_path.read_text())
        entries = data.get("entries", [])
        symbols = []
        position_symbols = set()
        for e in entries:
            code = e.get("code", "")
            if not code or code in _EXCLUDE_SYMBOLS:
                continue
            if any(code.startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            if not code.startswith("US."):
                continue
            symbols.append(code)
            if e.get("source") == "POSITION":
                position_symbols.add(code)
        return sorted(set(symbols)), position_symbols
    except Exception as e:
        logger.error(f"[FactorEngine] pool read error: {e}")
        return [], set()


def _discover_finnhub_movers() -> List[str]:
    """多源异动标的发现 (v3 — 2026-06-17 重构)
    
    数据源 (按优先级):
      1. premarket_enrichment 缓存 — 已有 gainers/losers 数据, 零 API 消耗
      2. Finnhub quote 异动检测 — MOVER_POOL + dragonhead_pool 全量扫描 (>2%)
      3. Finnhub general_news related — 新闻中高频提及
    
    降级: 每个源独立失败不影响其他源
    约束: Finnhub API ≤ 30次 (免费 60次/分钟), 总超时 20s
    """
    discovered = set()
    sources_hit = []  # 用于日志
    t0 = time.time()
    
    # ── Source 1: 复用 premarket_enrichment 缓存 (零API消耗) ──
    try:
        from app.data.premarket_enrichment import _cache as pm_cache
        movers = pm_cache.get("premarket_movers", {})
        if movers and isinstance(movers, dict):
            for entry in movers.get("gainers", []) + movers.get("losers", []):
                sym = entry.get("symbol", "").upper()
                sym = sym.replace("US.", "")  # 防双重前缀
                if sym and len(sym) <= 5 and sym.isalpha():
                    code = f"US.{sym}"
                    if code not in _EXCLUDE_SYMBOLS:
                        discovered.add(code)
            if discovered:
                sources_hit.append(f"premarket_cache({len(discovered)})")
    except Exception as e:
        logger.debug(f"[FactorEngine] premarket cache read error: {e}")

    # ── Source 1b: 富途全市场盘前榜缓存 (2026-08-29, 零API消耗) ──
    #   与 premarket_movers (20只硬编码池) 互补: 全市场 18064 只的涨跌幅榜,
    #   让盘中新异动标的进候选池 (此前盘中冒出的票进不了发现链)。
    try:
        from app.data.premarket_enrichment import _cache as _pm_cache2
        _fpr = _pm_cache2.get("futu_premarket_rank", {})
        if _fpr and isinstance(_fpr, dict):
            _added = 0
            for entry in _fpr.get("gainers", []) + _fpr.get("losers", []):
                sym = str(entry.get("symbol", "")).upper().replace("US.", "")
                if sym and len(sym) <= 5 and sym.isalpha():
                    code = f"US.{sym}"
                    if code not in _EXCLUDE_SYMBOLS and code not in discovered:
                        discovered.add(code)
                        _added += 1
            if _added:
                sources_hit.append(f"futu_premarket_rank({_added})")
    except Exception as e:
        logger.debug(f"[FactorEngine] futu premarket rank cache read error: {e}")
    
    # ── Source 2: Finnhub quote 异动扫描 (MOVER_POOL + dragonhead_pool) ──
    try:
        from app.data.premarket_enrichment import _get_finnhub_client, MOVER_POOL
        client = _get_finnhub_client()
        if client and (time.time() - t0 < 18):
            # 合并扫描池: MOVER_POOL + dragonhead US stocks
            scan_pool = set(MOVER_POOL)
            try:
                from app.utils.dragonhead_pool import US_DRAGON_HEADS
                for sector_data in US_DRAGON_HEADS.values():
                    if isinstance(sector_data, dict):
                        for stock in sector_data.get("stocks", []):
                            code = stock.get("code", "")
                            if code.startswith("US."):
                                scan_pool.add(code.replace("US.", ""))
            except Exception:
                pass
            
            api_calls = 0
            quote_hits = 0
            for sym in scan_pool:
                if api_calls >= 30 or (time.time() - t0 > 18):
                    break
                code = f"US.{sym}"
                if code in _EXCLUDE_SYMBOLS:
                    continue
                try:
                    q = client.quote(sym)
                    api_calls += 1
                    pc = q.get("pc", 0)
                    c = q.get("c", 0)
                    if pc and c and pc > 0:
                        change_pct = abs((c - pc) / pc * 100)
                        if change_pct >= 2.0:  # 降低阈值从 3% 到 2%
                            discovered.add(code)
                            quote_hits += 1
                except Exception:
                    pass
                time.sleep(0.05)  # rate limit: ~20 calls/sec
            if quote_hits:
                sources_hit.append(f"finnhub_quote({quote_hits}/{api_calls})")
    except Exception as e:
        logger.debug(f"[FactorEngine] finnhub mover scan error: {e}")
    
    # ── Source 3: Finnhub general news related symbols ──
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        client = _get_finnhub_client()
        if client and (time.time() - t0 < 19):
            news_count = 0
            try:
                news = client.general_news('general', min_id=0)
                if news and isinstance(news, list):
                    for article in news[:50]:
                        related = article.get("related", "")
                        if related:
                            for sym in related.split(","):
                                sym = sym.strip().upper()
                                if sym and len(sym) <= 5 and sym.isalpha():
                                    code = f"US.{sym}"
                                    if code not in _EXCLUDE_SYMBOLS:
                                        discovered.add(code)
                                        news_count += 1
            except Exception as e:
                logger.debug(f"[FactorEngine] finnhub news error: {e}")
            if news_count:
                sources_hit.append(f"finnhub_news({news_count})")
    except Exception as e:
        logger.debug(f"[FactorEngine] finnhub discovery failed: {e}")
    
    logger.info(
        f"[FactorEngine] 多源发现: {len(discovered)}只标的, "
        f"来源=[{', '.join(sources_hit)}] ({time.time()-t0:.1f}s)"
    )
    
    return list(discovered)


def _apply_quality_gate(symbols: List[str], position_symbols: set) -> List[str]:
    """质量闸门 v2 (对标 market_scan 的过闸逻辑)
    
    剔除规则:
      - price < $5 (仙股)
      - ETF/杠杆/反向基金
      - market_cap < $2B (新增: 流动性不足)
    豁免: 持仓标的 (position_symbols) 不受闸门影响
    降级: API失败的标的保留 (宁可多不可漏)
    """
    _QUALITY_GATE_MIN_MCAP = 2_000_000_000  # $2B 市值闸值
    
    if not symbols:
        return []
    
    passed = []
    rejected_reasons = {}  # 用于日志
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        client = _get_finnhub_client()
        
        for code in symbols:
            # 持仓标的直接通过
            if code in position_symbols:
                passed.append(code)
                continue
            
            # 已知 ETF 排除
            if code in _EXCLUDE_SYMBOLS:
                rejected_reasons[code] = "excluded_etf"
                continue
            
            short = code.replace("US.", "")
            
            # 杠杆/反向 ETF 关键词检测
            if any(kw in short.upper() for kw in _LEVERAGED_KEYWORDS):
                rejected_reasons[code] = "leveraged"
                continue
            
            # 如果无 client，跳过API检查直接通过
            if not client:
                passed.append(code)
                continue
            
            # 价格检查 + 市值检查 (使用 Finnhub quote + profile2)
            try:
                q = client.quote(short)
                price = q.get("c", 0)
                if price and price < _QUALITY_GATE_MIN_PRICE:
                    rejected_reasons[code] = f"price_${price:.1f}"
                    continue
            except Exception:
                pass  # API失败则保留
            
            # 市值检查 (仅对非核心池发现的标的, 避免过多API调用)
            # Finnhub profile2 提供 marketCapitalization (M)
            try:
                profile = client.company_profile2(symbol=short)
                if profile and isinstance(profile, dict):
                    mcap = profile.get("marketCapitalization", 0)
                    # Finnhub 返回的是百万美元单位
                    if mcap and (mcap * 1_000_000) < _QUALITY_GATE_MIN_MCAP:
                        rejected_reasons[code] = f"mcap_${mcap:.0f}M"
                        continue
            except Exception:
                pass  # profile失败则保留
            
            time.sleep(0.05)  # rate limit
            passed.append(code)
    except Exception as e:
        logger.debug(f"[FactorEngine] quality gate error: {e}, passing all")
        return symbols
    
    if rejected_reasons:
        logger.info(
            f"[QualityGate] 剔除{len(rejected_reasons)}只: "
            f"{dict(list(rejected_reasons.items())[:10])}"
        )
    
    return passed


def _get_stock_pool() -> Tuple[List[str], Dict]:
    """获取候选股票池: active_pool + Finnhub异动 → 质量闸门
    
    Returns:
        (filtered_pool, pool_meta): 过滤后的池 + 发现元数据
    """
    # 1. 核心池 (现有逻辑)
    core_pool, position_symbols = _load_active_pool()
    
    # 2. Finnhub 异动发现 (新增, 失败不影响核心池)
    event_pool = _discover_finnhub_movers()
    
    # 3. 合并去重
    combined = list(set(core_pool + event_pool))
    
    # 4. 质量闸门 (持仓豁免)
    filtered = _apply_quality_gate(combined, position_symbols)
    
    # 5. 池元数据 (供 pipeline discovery_meta 使用)
    pool_meta = {
        "core_pool_size": len(core_pool),
        "event_discovered": len(event_pool),
        "event_symbols": [s for s in event_pool if s not in core_pool],
        "quality_gate_removed": len(combined) - len(filtered),
        "final_pool_size": len(filtered),
        "position_symbols": list(position_symbols),
    }
    
    logger.info(
        f"[FactorEngine] 池构建: 核心{len(core_pool)} + 异动{len(event_pool)} "
        f"→ 合并{len(combined)} → 过闸{len(filtered)}"
    )
    
    return sorted(filtered), pool_meta


def _get_sector_momentum() -> Dict[str, float]:
    """获取板块ETF 5日涨幅 (from Finnhub quote)
    Returns: {"Technology": 2.3, "Financials": -0.5, ...}
    """
    etf_map = {
        "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer",
        "XLP": "Consumer Staples", "XLC": "Communication",
    }
    result = {}
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        client = _get_finnhub_client()
        if not client:
            return result
        for etf, sector in etf_map.items():
            try:
                q = client.quote(etf)
                prev = q.get("pc", 0)
                cur = q.get("c", 0)
                if prev and cur and prev > 0:
                    result[sector] = round((cur - prev) / prev * 100, 2)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[FactorEngine] sector momentum error: {e}")
    return result


def _compute_technicals(data_router, symbol: str) -> Optional[Dict]:
    """计算单只股票的技术因子 (via MarketDataRouter 日K线 + LV3TechnicalCalculator)"""
    try:
        bars = data_router.get_daily_bars(symbol, count=60)
        if not bars or len(bars) < 20:
            return None

        from app.data.lv3_technical_calculator import LV3TechnicalCalculator
        tech = LV3TechnicalCalculator().calculate_from_klines(bars)
        if not tech:
            return None

        result = {"symbol": symbol, "cur_price": tech.get("price", 0)}

        # MA alignment from LV3 MA5/MA10/MA20
        ma5 = tech.get("ma5", 0)
        ma10 = tech.get("ma10", 0)
        ma20 = tech.get("ma20", 0)
        result["ma5"] = round(ma5, 2)
        result["ma10"] = round(ma10, 2)
        result["ma20"] = round(ma20, 2)
        if ma5 > ma10 > ma20:
            result["ma_alignment"] = "BULLISH"
        elif ma5 < ma10 < ma20:
            result["ma_alignment"] = "BEARISH"
        else:
            result["ma_alignment"] = "MIXED"

        # Volume ratio
        result["vol_ratio"] = tech.get("volume_ratio", 1.0)

        # RSI-14
        result["rsi14"] = tech.get("rsi", 50.0)

        # Price changes
        result["change_20d"] = tech.get("change_20d_pct", 0.0)
        result["change_5d"] = tech.get("change_5d_pct", 0.0)

        # 20d high/low from raw bars
        if len(bars) >= 20:
            result["high_20d"] = round(max(float(b["high"]) for b in bars[-20:]), 2)
            result["low_20d"] = round(min(float(b["low"]) for b in bars[-20:]), 2)

        return result

    except Exception as e:
        logger.debug(f"[FactorEngine] technicals error for {symbol}: {e}")
        return None


def _score_factor(name: str, value: float, direction: str = "LONG") -> float:
    """将因子原始值转换为0-1标准化得分 (方向感知)

    LONG视角: 上涨/多头信号 → 高分
    SHORT视角: 下跌/空头信号 → 高分 (镜像对称)
    """
    is_short = direction == "SHORT"

    if name == "sector_momentum":
        # LONG: -3%→0, 0%→0.5, +3%→1.0
        # SHORT: +3%→0, 0%→0.5, -3%→1.0 (板块跌=做空机会)
        if is_short:
            return max(0, min(1, (-value + 3) / 6))
        return max(0, min(1, (value + 3) / 6))

    elif name == "relative_strength":
        # LONG: -5%→0, 0%→0.5, +5%→1.0
        # SHORT: +5%→0, 0%→0.5, -5%→1.0 (跑输=做空机会)
        if is_short:
            return max(0, min(1, (-value + 5) / 10))
        return max(0, min(1, (value + 5) / 10))

    elif name == "ma_alignment":
        # LONG: BULLISH=1.0, MIXED=0.5, BEARISH=0.0
        # SHORT: BEARISH=1.0, MIXED=0.5, BULLISH=0.0 (均线空头=做空信号)
        mapping_long = {"BULLISH": 1.0, "MIXED": 0.5, "BEARISH": 0.0}
        mapping_short = {"BEARISH": 1.0, "MIXED": 0.5, "BULLISH": 0.0}
        return (mapping_short if is_short else mapping_long).get(value, 0.5)

    elif name == "volume_breakout":
        # 量比方向无关: 放量=高关注度(无论涨跌)
        return max(0, min(1, (value - 0.5) / 1.5))

    elif name == "rsi_sweet_spot":
        # LONG: 30-50最优(超卖反弹), 50-70中等, <30差, >70差
        # SHORT: 70-90最优(超买回落), 50-70中等, <30差, >90极端
        if is_short:
            if 70 <= value <= 90:
                return 1.0
            elif 50 < value < 70:
                return 0.6
            elif 30 <= value <= 50:
                return 0.4
            else:
                return 0.2
        if 30 <= value <= 50:
            return 1.0
        elif 50 < value <= 70:
            return 0.6
        elif 20 <= value < 30:
            return 0.4  # 超卖可能反弹
        else:
            return 0.2  # 极端区域

    return 0.5


# ════════════════════════════════════════════════════════════════
#  P2 (2026-06-21): 基本面+情绪因子 — 复用已有数据源, 零新依赖
# ════════════════════════════════════════════════════════════════

def _fetch_fundamental_score(symbol: str) -> tuple:
    """获取基本面质量评分

    P2 (2026-06-21): 优先用富途snapshot(实时PE/PB/EPS/净资产/净利润), 降级用Finnhub(5min缓存)
    原因: 富途数据实时(5s缓存), Finnhub 24h缓存可能过期; 富途无API限频
    富途snapshot字段: pe_ratio, pe_ttm_ratio, pb_ratio, earning_per_share, net_asset, net_profit

    Returns:
        (score_0_1, detail_str)
    """
    import time

    # 缓存 (5分钟)
    cache_key = f"_fund_cache_{symbol}"
    cached = getattr(_fetch_fundamental_score, cache_key, None)
    if cached:
        cached_time, cached_result = cached
        if time.time() - cached_time < 300:
            return cached_result

    # ── 优先: 富途snapshot (实时, 无限频) ──
    try:
        from app.web_dashboard.data_service import get_data_service
        ds = get_data_service()
        import asyncio
        # P0修复: 线程中asyncio.get_event_loop()可能抛RuntimeError
        # 用asyncio.run()最安全——它会创建新事件循环, 执行完自动关闭
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在asyncio事件循环内, 用新线程执行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    quotes = pool.submit(
                        asyncio.run, ds.get_batch_quotes_detailed([symbol])
                    ).result(timeout=10)
            else:
                quotes = loop.run_until_complete(ds.get_batch_quotes_detailed([symbol]))
        except RuntimeError:
            # "There is no current event loop" — 在子线程中, 用asyncio.run
            quotes = asyncio.run(ds.get_batch_quotes_detailed([symbol]))

        q = quotes.get(symbol, {})
        pe = q.get("pe_ttm") or q.get("pe_ratio") or 0
        pb = q.get("pb_ratio") or 0
        eps = q.get("eps") or 0
        net_asset = q.get("net_asset") or 0
        net_profit = q.get("net_profit") or 0

        if pe > 0:
            # PE评分: 5-25 → 1.0, 25-50 → 0.6, >50 → 0.3
            if pe <= 25:
                pe_score = 1.0
            elif pe <= 50:
                pe_score = 0.6
            else:
                pe_score = 0.3

            # ROE = net_profit / net_asset (富途不直接返回ROE, 需计算)
            roe = (net_profit / net_asset * 100) if net_asset > 0 else 0
            if roe > 15:
                roe_score = 1.0
            elif roe > 5:
                roe_score = 0.6
            else:
                roe_score = 0.3

            # PB评分: <3 → 1.0, 3-10 → 0.6, >10 → 0.3
            if pb > 0 and pb <= 3:
                pb_score = 1.0
            elif pb > 0 and pb <= 10:
                pb_score = 0.6
            else:
                pb_score = 0.3

            score = pe_score * 0.4 + roe_score * 0.3 + pb_score * 0.3
            detail = f"PE={pe:.0f} ROE={roe:.1f}% PB={pb:.1f} [Futu]"
            result = (round(score, 3), detail)
            setattr(_fetch_fundamental_score, cache_key, (time.time(), result))
            return result
    except Exception as e:
        logger.debug(f"[FactorEngine] futu snapshot fundamental failed for {symbol}: {e}")

    # ── 降级: Finnhub REST API (5min缓存) ──
    try:
        import os, requests
        key = os.environ.get("FINNHUB_API_KEY", "")
        if not key:
            from pathlib import Path
            env_path = Path(__file__).parent.parent.parent / ".env.us"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("FINNHUB_API_KEY=") and not line.startswith("#"):
                        key = line.split("=", 1)[1].strip()
                        break

        if not key:
            return 0.5, "no api key"

        bare = symbol.replace("US.", "").replace("US", "")
        resp = requests.get(
            f"https://finnhub.io/api/v1/stock/metric",
            params={"symbol": bare, "metric": "all", "token": key},
            timeout=8,
        )

        if resp.status_code != 200:
            return 0.5, f"api {resp.status_code}"

        m = resp.json().get("metric", {})
        if not m:
            return 0.5, "no metric data"

        pe = float(m.get("peNormalizedAnnual", 0) or 0)
        roe = float(m.get("roeTTM", 0) or 0)
        revenue_yoy = float(m.get("revenueGrowth5Y", 0) or 0)

        # PE评分: 5-25 → 1.0, 25-50 → 0.6, >50或负 → 0.2
        if pe <= 0:
            pe_score = 0.2
        elif pe <= 25:
            pe_score = 1.0
        elif pe <= 50:
            pe_score = 0.6
        else:
            pe_score = 0.3

        # ROE评分: >15% → 1.0, 5-15% → 0.6, <5% → 0.3
        if roe > 15:
            roe_score = 1.0
        elif roe > 5:
            roe_score = 0.6
        else:
            roe_score = 0.3

        # 收入增长: >10% → 1.0, 0-10% → 0.6, <0% → 0.2
        if revenue_yoy > 10:
            growth_score = 1.0
        elif revenue_yoy > 0:
            growth_score = 0.6
        else:
            growth_score = 0.2

        score = pe_score * 0.4 + roe_score * 0.3 + growth_score * 0.3
        detail = f"PE={pe:.0f} ROE={roe:.1f}% RevYoY={revenue_yoy:.1f}% [Finnhub]"
        result = (round(score, 3), detail)
        setattr(_fetch_fundamental_score, cache_key, (time.time(), result))
        return result

    except Exception as e:
        logger.debug(f"[FactorEngine] fundamental score error for {symbol}: {e}")
        try:
            from app.core.degradation_monitor import get_degradation_monitor
            get_degradation_monitor().record("factor_fundamental_fetch", str(e))
        except Exception:
            pass
        return 0.5, "error"


def _fetch_sentiment_crowding(symbol: str, direction: str) -> tuple:
    """获取情绪拥挤度评分

    P2 (2026-06-21): 优先用富途snapshot资金流数据(真实钱), 降级用StockTwits(社交媒体)
    原因: 富途bid_ask_ratio+volume_ratio反映真实买卖盘力量, 比社交媒体情绪更可靠
          且富途数据实时(5s缓存), StockTwits 10min缓存且限200次/小时

    逻辑: 买盘强(bid_ask_ratio>0) → LONG有利/SHORT危险
         卖盘强(bid_ask_ratio<0) → SHORT有利/LONG危险
         量比高 → 趋势确认(放大当前方向)

    Returns:
        (score_0_1, detail_str) — score越高=对当前方向越有利
    """
    import time as _time

    # 缓存5分钟 (富途snapshot是5s缓存, 但因子评分不需要那么频繁)
    cache_key = f"_sent_cache_{symbol}"
    cached = getattr(_fetch_sentiment_crowding, cache_key, None)
    if cached:
        cached_time, cached_result = cached
        if _time.time() - cached_time < 300:
            score = cached_result[0] if direction == "LONG" else cached_result[1]
            return score, cached_result[2]

    # ── 优先: 富途snapshot bid_ask_ratio + volume_ratio ──
    try:
        from app.web_dashboard.data_service import get_data_service
        ds = get_data_service()
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    quotes = pool.submit(
                        asyncio.run, ds.get_batch_quotes_detailed([symbol])
                    ).result(timeout=10)
            else:
                quotes = loop.run_until_complete(ds.get_batch_quotes_detailed([symbol]))
        except RuntimeError:
            quotes = asyncio.run(ds.get_batch_quotes_detailed([symbol]))

        q = quotes.get(symbol, {})
        bid_ask_ratio = q.get("bid_ask_ratio")
        volume_ratio = q.get("volume_ratio")

        if bid_ask_ratio is not None:
            # bid_ask_ratio: >0 买盘强, <0 卖盘强
            # 映射到0-1: ratio>20 → 1.0(极强买), ratio<-20 → 0.0(极强卖)
            bar = float(bid_ask_ratio)
            bar_score = max(0.0, min(1.0, (bar + 20) / 40))

            # volume_ratio: >1 放量(确认趋势), <1 缩量(趋势弱)
            vr = float(volume_ratio) if volume_ratio else 1.0
            vol_confirm = max(0.5, min(1.0, vr / 3))  # vr=3 → 1.0, vr=1 → 0.5

            # LONG: 买盘强 → 高分
            long_score = max(0.2, min(1.0, bar_score * 0.7 + vol_confirm * 0.3))
            # SHORT: 卖盘强 → 高分
            short_score = max(0.2, min(1.0, (1 - bar_score) * 0.7 + vol_confirm * 0.3))

            crowd_label = '强买' if bar > 10 else '强卖' if bar < -10 else '均衡'
            label = f"B/A={bar:.1f}({crowd_label}) VR={vr:.1f}x [Futu]"

            setattr(_fetch_sentiment_crowding, cache_key, (_time.time(), (long_score, short_score, label)))
            score = long_score if direction == "LONG" else short_score
            return round(score, 3), label
    except Exception as e:
        logger.debug(f"[FactorEngine] futu snapshot sentiment failed for {symbol}: {e}")

    # ── 降级: StockTwits API (10min缓存, 200次/小时限频) ──
    try:
        import requests
        bare = symbol.replace("US.", "")
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{bare}.json"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "FTSeek/1.0"})
        if resp.status_code == 429:
            try:
                from app.core.degradation_monitor import get_degradation_monitor
                get_degradation_monitor().record("stocktwits_rate_limit", f"429 for {symbol}")
            except Exception:
                pass
            return 0.5, "rate_limited"
        if resp.status_code != 200:
            return 0.5, f"api {resp.status_code}"

        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return 0.5, "no messages"

        bullish = 0
        bearish = 0
        for msg in messages[:30]:
            sentiment = msg.get("entities", {}).get("sentiment")
            if isinstance(sentiment, dict):
                basic = sentiment.get("basic", "")
                if basic == "Bullish":
                    bullish += 1
                elif basic == "Bearish":
                    bearish += 1

        total = max(bullish + bearish, 1)
        bull_pct = bullish / total

        long_score = max(0.2, min(1.0, 1.0 - bull_pct))
        short_score = max(0.2, min(1.0, bull_pct))

        crowd_label = '拥挤' if bull_pct > 0.7 else '中性' if bull_pct > 0.4 else '恐慌'
        label = f"bull={bull_pct:.0%}({crowd_label})@{bullish}/{bearish} [ST]"

        setattr(_fetch_sentiment_crowding, cache_key, (_time.time(), (long_score, short_score, label)))
        score = long_score if direction == "LONG" else short_score
        return round(score, 3), label

    except Exception as e:
        logger.debug(f"[FactorEngine] sentiment score error for {symbol}: {e}")
        try:
            from app.core.degradation_monitor import get_degradation_monitor
            get_degradation_monitor().record("factor_sentiment_fetch", str(e))
        except Exception:
            pass
        return 0.5, "error"


def _detect_regime(sector_data: Dict[str, float], spy_change: float = 0) -> str:
    """基于板块离散度和SPY趋势判断市场regime"""
    if not sector_data:
        return "NEUTRAL"

    changes = list(sector_data.values())
    avg = sum(changes) / len(changes) if changes else 0
    spread = max(changes) - min(changes) if changes else 0

    # 板块分散度>4% = 轮动
    if spread > 4:
        return "ROTATION"
    # 大部分板块涨 + SPY涨 = RISK_ON
    elif avg > 0.5 and spy_change > 0:
        return "RISK_ON"
    # 大部分板块跌 = RISK_OFF
    elif avg < -0.5:
        return "RISK_OFF"
    else:
        return "NEUTRAL"


def run_factor_screen(top_n: int = 8) -> Dict:
    """
    执行5因子量化预筛。
    
    Returns: {
        "timestamp": "...",
        "regime": "RISK_ON|...",
        "regime_weights": {...},
        "candidates": [
            {
                "symbol": "US.NVDA",
                "composite_score": 0.78,
                "factors": {
                    "sector_momentum": {"raw": 2.1, "score": 0.85, "weight": 0.20},
                    ...
                },
                "technicals": {...},
                "sectors": ["Technology", "Semiconductors"],
                "direction_hint": "LONG"
            },
            ...
        ],
        "pool_size": 34,
        "sector_momentum": {...},
        "spy_change_20d": 1.2
    }
    """
    t0 = time.time()

    # 1. Get stock pool (with discovery + quality gate)
    pool, pool_meta = _get_stock_pool()
    if not pool:
        return {"error": "empty_pool", "candidates": [], "pool_meta": {}, "timestamp": datetime.now(timezone.utc).isoformat()}

    # 2. Get sector momentum
    sector_momentum = _get_sector_momentum()

    # 3. Get SPY 20d change + technicals via MarketDataRouter (eliminates ManualOrderService)
    spy_change_20d = 0
    technicals = {}
    try:
        # 2026-07-11: 复用进程级共享单例, 避免每次因子扫描新建 Futu 连接+watchdog
        from app.data.market_data_router import get_market_data_router
        data_router = get_market_data_router()

        # SPY 20d change
        spy_bars = data_router.get_daily_bars("US.SPY", count=25)
        if spy_bars and len(spy_bars) >= 20:
            spy_closes = [float(b["close"]) for b in spy_bars]
            spy_change_20d = round((spy_closes[-1] - spy_closes[-20]) / spy_closes[-20] * 100, 2)

        # Compute technicals for all pool stocks
        for symbol in pool:
            tech = _compute_technicals(data_router, symbol)
            if tech:
                technicals[symbol] = tech

    except Exception as e:
        logger.error(f"[FactorEngine] data_router error: {e}")
        technicals = {}

    # 5. Detect regime
    regime = _detect_regime(sector_momentum, spy_change_20d)
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["NEUTRAL"])

    # 6. Score each stock (directional: compute both LONG and SHORT, pick the stronger)
    candidates = []
    for symbol in pool:
        tech = technicals.get(symbol)
        if not tech:
            continue

        ticker = symbol.replace("US.", "")
        stock_sectors = _TICKER_TO_SECTORS.get(ticker, [])

        # ── Raw factor inputs (direction-agnostic) ──
        best_sector_chg = 0
        best_sector_name = ""
        for sec in stock_sectors:
            chg = sector_momentum.get(sec, 0)
            if chg > best_sector_chg or not best_sector_name:
                best_sector_chg = chg
                best_sector_name = sec

        rs = tech.get("change_20d", 0) - spy_change_20d
        ma_align = tech.get("ma_alignment", "MIXED")
        vol_ratio = tech.get("vol_ratio", 1.0)
        rsi = tech.get("rsi14", 50)

        # F6: Insider signal (shared between directions)
        insider_score = 0.5
        insider_detail = "no data"
        try:
            from app.data.enrichment_router import get_insider
            insider_data = get_insider(symbol)
            if insider_data.get("status") == "ok":
                d = insider_data["data"]
                buy_count = d.get("buy_count_30d", 0)
                sell_count = d.get("sell_count_30d", 0)
                net_shares = d.get("net_shares_30d", 0)
                signal = d.get("insider_signal", "neutral")

                if signal == "strong_buy":
                    insider_score = 1.0
                    insider_detail = f"STRONG BUY: {buy_count} buys, {net_shares:+,} shares"
                elif signal == "buy":
                    insider_score = 0.8
                    insider_detail = f"BUY: {buy_count} buys, {net_shares:+,} shares"
                elif sell_count > buy_count:
                    insider_score = 0.2
                    insider_detail = f"SELL: {sell_count} sells > {buy_count} buys"
                else:
                    insider_score = 0.5
                    insider_detail = f"NEUTRAL: {buy_count}B/{sell_count}S"
        except Exception as e:
            logger.debug(f"[FactorEngine] insider fetch failed for {symbol}: {e}")

        # Insider: mirror for SHORT (sell=1.0 for short, buy=0.2)
        insider_score_long = insider_score
        if insider_score >= 0.7:
            insider_score_short = 0.2  # strong buy = bearish for short
        elif insider_score <= 0.3:
            insider_score_short = 0.8  # sell = bullish for short
        else:
            insider_score_short = 0.5

        # P2 (2026-06-21): 基本面质量 — 优先用富途snapshot(实时), 降级用Finnhub(5min缓存)
        fund_score, fund_detail = _fetch_fundamental_score(symbol)
        # 基本面对两个方向都是同一评分 (高质量公司做多做空都有价值)
        fund_score_long = fund_score
        fund_score_short = 1.0 - fund_score  # 基本面差的公司更适合做空

        # P2 (2026-06-21): 情绪拥挤度 — 优先用富途资金流(真实钱), 降级用StockTwits(社交媒体)
        sent_score_long, sent_detail_long = _fetch_sentiment_crowding(symbol, "LONG")
        sent_score_short, sent_detail_short = _fetch_sentiment_crowding(symbol, "SHORT")

        # ── Compute LONG composite ──
        long_factors = {
            "sector_momentum": {"raw": best_sector_chg, "score": _score_factor("sector_momentum", best_sector_chg, "LONG"), "weight": weights["sector_momentum"], "detail": f"{best_sector_name} {best_sector_chg:+.1f}%" if best_sector_name else "N/A"},
            "relative_strength": {"raw": round(rs, 2), "score": _score_factor("relative_strength", rs, "LONG"), "weight": weights["relative_strength"], "detail": f"{tech.get('change_20d', 0):+.1f}% vs SPY {spy_change_20d:+.1f}%"},
            "ma_alignment": {"raw": ma_align, "score": _score_factor("ma_alignment", ma_align, "LONG"), "weight": weights["ma_alignment"], "detail": f"MA5={tech.get('ma5',0):.0f} MA10={tech.get('ma10',0):.0f} MA20={tech.get('ma20',0):.0f}"},
            "volume_breakout": {"raw": vol_ratio, "score": _score_factor("volume_breakout", vol_ratio, "LONG"), "weight": weights["volume_breakout"], "detail": f"{vol_ratio:.1f}x avg"},
            "rsi_sweet_spot": {"raw": rsi, "score": _score_factor("rsi_sweet_spot", rsi, "LONG"), "weight": weights["rsi_sweet_spot"], "detail": f"RSI={rsi:.0f}"},
            "insider_signal": {"raw": insider_score_long, "score": insider_score_long, "weight": weights.get("insider_signal", 0.12), "detail": insider_detail},
            "fundamental_quality": {"raw": fund_score_long, "score": fund_score_long, "weight": weights.get("fundamental_quality", 0.13), "detail": fund_detail},
            "sentiment_crowding": {"raw": sent_score_long, "score": sent_score_long, "weight": weights.get("sentiment_crowding", 0.10), "detail": sent_detail_long},
        }
        long_composite = sum(f["score"] * f["weight"] for f in long_factors.values())

        # ── Compute SHORT composite ──
        short_factors = {
            "sector_momentum": {"raw": best_sector_chg, "score": _score_factor("sector_momentum", best_sector_chg, "SHORT"), "weight": weights["sector_momentum"], "detail": f"{best_sector_name} {best_sector_chg:+.1f}%" if best_sector_name else "N/A"},
            "relative_strength": {"raw": round(rs, 2), "score": _score_factor("relative_strength", rs, "SHORT"), "weight": weights["relative_strength"], "detail": f"{tech.get('change_20d', 0):+.1f}% vs SPY {spy_change_20d:+.1f}%"},
            "ma_alignment": {"raw": ma_align, "score": _score_factor("ma_alignment", ma_align, "SHORT"), "weight": weights["ma_alignment"], "detail": f"MA5={tech.get('ma5',0):.0f} MA10={tech.get('ma10',0):.0f} MA20={tech.get('ma20',0):.0f}"},
            "volume_breakout": {"raw": vol_ratio, "score": _score_factor("volume_breakout", vol_ratio, "SHORT"), "weight": weights["volume_breakout"], "detail": f"{vol_ratio:.1f}x avg"},
            "rsi_sweet_spot": {"raw": rsi, "score": _score_factor("rsi_sweet_spot", rsi, "SHORT"), "weight": weights["rsi_sweet_spot"], "detail": f"RSI={rsi:.0f}"},
            "insider_signal": {"raw": insider_score_short, "score": insider_score_short, "weight": weights.get("insider_signal", 0.12), "detail": insider_detail},
            "fundamental_quality": {"raw": fund_score_short, "score": fund_score_short, "weight": weights.get("fundamental_quality", 0.13), "detail": fund_detail},
            "sentiment_crowding": {"raw": sent_score_short, "score": sent_score_short, "weight": weights.get("sentiment_crowding", 0.10), "detail": sent_detail_short},
        }
        short_composite = sum(f["score"] * f["weight"] for f in short_factors.values())

        # ── Direction selection with conflict arbitration ──
        # P0 修复 (2026-06-21): 方向冲突仲裁
        # 原: long_composite >= short_composite → 直接选LONG (无视差异大小)
        # 新: 差异 < 0.15 → 方向不明确, 标记NEUTRAL, 不进入推荐
        # 原理: 强行从两个相近的方向中选一个=猜, 不如不推荐
        _DIRECTION_DIFF_THRESHOLD = 0.15

        if abs(long_composite - short_composite) < _DIRECTION_DIFF_THRESHOLD:
            direction_hint = "NEUTRAL"
            composite = max(long_composite, short_composite)
            factor_results = long_factors if long_composite >= short_composite else short_factors
        elif long_composite > short_composite:
            direction_hint = "LONG"
            composite = long_composite
            factor_results = long_factors
        else:
            direction_hint = "SHORT"
            composite = short_composite
            factor_results = short_factors

        candidates.append({
            "symbol": symbol,
            "composite_score": round(composite, 3),
            "composite_score_long": round(long_composite, 3),
            "composite_score_short": round(short_composite, 3),
            "direction_diff": round(abs(long_composite - short_composite), 3),
            "factors": factor_results,
            "technicals": {k: v for k, v in tech.items() if k != "symbol"},
            "sectors": stock_sectors,
            "direction_hint": direction_hint,
        })

    # 7. Sort by composite score (descending)
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    # Mark discovery source on each candidate
    event_set = set(pool_meta.get("event_symbols", []))
    for c in candidates:
        c["discovery_source"] = "event_discovery" if c["symbol"] in event_set else "core_pool"

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "regime_weights": weights,
        "candidates": candidates[:top_n],
        "pool_size": len(pool),
        "pool_meta": pool_meta,
        "scored_count": len(candidates),
        "sector_momentum": sector_momentum,
        "spy_change_20d": spy_change_20d,
        "elapsed_sec": round(time.time() - t0, 1),
        # all_scores: lightweight list for watchlist/dashboard (symbol + score only)
        "all_scores": [{"symbol": c["symbol"], "composite_score": c["composite_score"], "direction_hint": c.get("direction_hint", ""), "composite_score_long": c.get("composite_score_long", 0), "composite_score_short": c.get("composite_score_short", 0)} for c in candidates],
    }

    # 8. Save to file
    try:
        out_path = Path(__file__).parent.parent.parent / "data" / "factor_screen_latest.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    except Exception as e:
        logger.debug(f"[FactorEngine] save error: {e}")

    logger.info(
        f"[FactorEngine] ✅ {len(pool)}只→评分{len(candidates)}只→Top{top_n} | "
        f"Regime={regime} | {result['elapsed_sec']}s"
    )
    return result


if __name__ == "__main__":
    """CLI: python app/plan/factor_engine.py"""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = run_factor_screen(top_n=int(sys.argv[1]) if len(sys.argv) > 1 else 8)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
