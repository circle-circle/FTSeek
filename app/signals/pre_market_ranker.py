"""
盘前选股排名器 (Pre-Market Stock Ranker)

每天盘前对 watchlist 中的所有标的做截面排名，选出最适合交易的标的。

核心逻辑：
1. 动量排名 — 谁涨得最稳（不是涨得最多）
2. 行业强度 — 个股所在板块是否在涨
3. 资金流 — 成交量是否配合
4. 波动率 — 排除波动过大/过小的

输出：每个标的的综合分数 + 推荐方向 + 置信度

数据来源：
- 历史 K 线: Futu API (已有) / TwelveData (已有)
- 行业 ETF: 11 个标准 SPDR sector ETFs
- VIX: CBOE (已有)
"""
import logging
import math
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.ranker")


# ============================================================
# 行业 ETF 映射 (SPDR Select Sector)
# ============================================================
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

# 个股 → 行业映射 (从 sector_limits.DEFAULT_SECTOR_MAPPING 读取)
def _get_symbol_sector(symbol: str) -> Optional[str]:
    """查个股属于哪个行业"""
    try:
        from app.risk.sector_limits import DEFAULT_SECTOR_MAPPING, CHINA_CONCEPT_MAPPING
        clean_sym = symbol.replace("US.", "")
        for sector, symbols in {**DEFAULT_SECTOR_MAPPING, **CHINA_CONCEPT_MAPPING}.items():
            if clean_sym in symbols:
                return sector
    except Exception:
        pass
    return None


# ============================================================
# 数据获取
# ============================================================
def _get_kline_data(symbol: str, days: int = 20) -> Optional[List[dict]]:
    """获取日线数据 (Futu K_DAY 日线)"""
    try:
        from app.data.futu_lv3_provider import get_futu_lv3_provider
        provider = get_futu_lv3_provider()
        # ktype="K_DAY", count 需要多取一些 (周末/假日)
        klines = provider.get_kline(symbol, ktype="K_DAY", count=days + 5)
        if klines and len(klines) >= days:
            # 返回格式: [{"close": float, "open": float, "high": float, "low": float, "volume": int}, ...]
            return klines[-days:]
    except Exception as e:
        logger.debug(f"[Ranker] K线获取失败 {symbol}: {e}")
    
    # 回退: 持久化缓存目录
    try:
        import json, os
        cache_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'kline_cache')
        cache_file = os.path.join(cache_dir, f"{symbol.replace('.', '_')}_day.json")
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) >= days:
                return data[-days:]
    except Exception:
        pass
    
    return None


def _calc_returns(klines: List[dict], periods: List[int]) -> Dict[int, float]:
    """计算多个周期的收益率"""
    if not klines or len(klines) < max(periods) + 1:
        return {p: 0.0 for p in periods}
    
    closes = [float(k.get('close', k.get('Close', 0))) for k in reversed(klines)]
    if not closes or closes[0] <= 0:
        return {p: 0.0 for p in periods}
    
    result = {}
    current = closes[-1]
    for p in periods:
        if len(closes) > p:
            prev = closes[-(p+1)]
            result[p] = (current - prev) / prev if prev > 0 else 0.0
        else:
            result[p] = 0.0
    return result


def _calc_volatility(klines: List[dict], periods: int = 10) -> float:
    """计算波动率 (日收益率标准差)"""
    if not klines or len(klines) < periods + 1:
        return 0.0
    
    closes = [float(k.get('close', k.get('Close', 0))) for k in reversed(klines)]
    if len(closes) < periods + 1:
        return 0.0
    
    returns = []
    for i in range(-periods, 0):
        prev = closes[i-1]
        curr = closes[i]
        if prev > 0:
            returns.append(math.log(curr / prev))
    
    if len(returns) < 3:
        return 0.0
    
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance)


def _calc_volume_trend(klines: List[dict], periods: int = 5) -> float:
    """计算成交量趋势 (近5日均量 / 前5日均量)"""
    if not klines or len(klines) < periods * 2:
        return 1.0
    
    vols = [float(k.get('volume', k.get('Volume', 0))) for k in reversed(klines)]
    recent = vols[:periods]
    prev = vols[periods:periods*2]
    
    avg_recent = sum(recent) / len(recent) if recent else 0
    avg_prev = sum(prev) / len(prev) if prev else 0
    
    return avg_recent / avg_prev if avg_prev > 0 else 1.0


# ============================================================
# 截面排名
# ============================================================
@dataclass
class RankedStock:
    symbol: str
    sector: Optional[str]
    direction: str          # "LONG" or "SHORT"
    momentum_score: float   # 0-100, 动量分数
    sector_score: float     # 0-100, 行业强度
    volume_score: float     # 0-100, 成交量配合度
    volatility_score: float # 0-100, 波动率合理性
    total_score: float      # 0-100, 综合分数
    ret_5d: float = 0.0    # 5日收益率
    ret_10d: float = 0.0   # 10日收益率
    ret_20d: float = 0.0   # 20日收益率
    volatility: float = 0.0
    volume_ratio: float = 1.0


def rank_watchlist(watchlist: List[str]) -> List[RankedStock]:
    """
    对 watchlist 做截面排名
    
    Returns: 按 total_score 降序排列的 RankedStock 列表
    """
    # 1. 计算每个行业 ETF 的动量
    sector_returns = {}
    for sector, etf in SECTOR_ETFS.items():
        klines = _get_kline_data(f"US.{etf}", days=20)
        if klines:
            rets = _calc_returns(klines, [5, 10, 20])
            sector_returns[sector] = rets
    
    # 2. 计算每个标的的各项指标
    candidates = []
    for symbol in watchlist:
        # 跳过 ETF (SPY/QQQ/IWM 不做截面排名)
        clean = symbol.replace("US.", "")
        if clean in ("SPY", "QQQ", "IWM", "DIA") or clean in SECTOR_ETFS.values():
            continue
        
        klines = _get_kline_data(symbol, days=20)
        if not klines or len(klines) < 10:
            continue
        
        rets = _calc_returns(klines, [5, 10, 20])
        vol = _calc_volatility(klines, 10)
        vol_ratio = _calc_volume_trend(klines, 5)
        
        sector = _get_symbol_sector(clean)
        
        candidates.append({
            "symbol": symbol,
            "sector": sector,
            "ret_5d": rets.get(5, 0),
            "ret_10d": rets.get(10, 0),
            "ret_20d": rets.get(20, 0),
            "volatility": vol,
            "volume_ratio": vol_ratio,
        })
    
    if not candidates:
        return []
    
    # 3. 截面排名 (每个维度独立排名，然后加权)
    n = len(candidates)
    
    # 动量分数: 20日收益排名 (正收益 = 做多候选, 负收益 = 做空候选)
    ret_20d_sorted = sorted(range(n), key=lambda i: candidates[i]["ret_20d"])
    for rank_idx, i in enumerate(ret_20d_sorted):
        candidates[i]["momentum_rank"] = rank_idx / (n - 1) if n > 1 else 0.5
    
    # 波动率分数: 适中最好 (太低没波动赚不到钱, 太高太危险)
    for c in candidates:
        # 理想日波动率: 1.5%-3.5%, 用高斯分布打分
        ideal_vol = 0.025  # 2.5%
        vol_score = math.exp(-((c["volatility"] - ideal_vol) ** 2) / (2 * 0.015 ** 2))
        c["vol_score"] = vol_score
    
    # 成交量分数: 放量加分 (ratio > 1.2 加分, < 0.8 扣分)
    for c in candidates:
        if c["volume_ratio"] >= 1.2:
            c["vol_trend_score"] = min(1.0, 0.5 + (c["volume_ratio"] - 1.0) * 0.5)
        elif c["volume_ratio"] <= 0.8:
            c["vol_trend_score"] = max(0.0, c["volume_ratio"] * 0.5)
        else:
            c["vol_trend_score"] = 0.5
    
    # 行业分数: 做多时行业越强越好, 做空时行业越弱越好
    # 先计算行业排名
    sector_momentum = {}
    for sector, rets in sector_returns.items():
        sector_momentum[sector] = rets.get(20, 0)
    
    sectors_sorted = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)
    sector_rank = {s: i / max(1, len(sectors_sorted) - 1) for i, (s, _) in enumerate(sectors_sorted)}
    
    # 4. 综合评分
    results = []
    for c in candidates:
        # 确定方向: 20日动量 > 0 做多, < 0 做空
        direction = "LONG" if c["ret_20d"] > 0 else "SHORT"
        
        # 动量分数 (0-100)
        if direction == "LONG":
            momentum_score = c["momentum_rank"] * 100  # 排名越靠前越高
        else:
            momentum_score = (1 - c["momentum_rank"]) * 100  # 排名越靠后越高(做空)
        
        # 行业分数
        if c["sector"] and c["sector"] in sector_rank:
            if direction == "LONG":
                sector_score = (1 - sector_rank[c["sector"]]) * 100  # 行业排名越靠前越高
            else:
                sector_score = sector_rank[c["sector"]] * 100  # 做空: 行业排名越靠后越高
        else:
            sector_score = 50  # 未知行业给中等分
        
        # 成交量分数 (0-100)
        volume_score = c["vol_trend_score"] * 100
        
        # 波动率分数 (0-100)
        volatility_score = c["vol_score"] * 100
        
        # 综合分数 (加权)
        total = (
            0.35 * momentum_score +    # 动量最重要
            0.25 * sector_score +       # 行业确认
            0.20 * volume_score +       # 成交量配合
            0.20 * volatility_score     # 波动率合理性
        )
        
        results.append(RankedStock(
            symbol=c["symbol"],
            sector=c["sector"],
            direction=direction,
            momentum_score=round(momentum_score, 1),
            sector_score=round(sector_score, 1),
            volume_score=round(volume_score, 1),
            volatility_score=round(volatility_score, 1),
            total_score=round(total, 1),
            ret_5d=round(c["ret_5d"], 4),
            ret_10d=round(c["ret_10d"], 4),
            ret_20d=round(c["ret_20d"], 4),
            volatility=round(c["volatility"], 4),
            volume_ratio=round(c["volume_ratio"], 2),
        ))
    
    # 按综合分数降序
    results.sort(key=lambda r: r.total_score, reverse=True)
    return results


def get_top_candidates(watchlist: List[str], max_count: int = 5) -> List[RankedStock]:
    """
    获取排名前 N 的候选标的
    
    用法: 在 signal_pipeline 入口处调用，
          只允许 Top N 标的进入后续信号生成流程
    """
    ranked = rank_watchlist(watchlist)
    
    if ranked:
        top = ranked[:max_count]
        logger.info(
            f"[Ranker] Top {len(top)} candidates from {len(ranked)} total:\n" +
            "\n".join(
                f"  #{i+1} {r.symbol} {r.direction} score={r.total_score:.1f} "
                f"(mom={r.momentum_score:.0f} sect={r.sector_score:.0f} "
                f"vol={r.volume_score:.0f} vola={r.volatility_score:.0f}) "
                f"ret20d={r.ret_20d*100:+.1f}%"
                for i, r in enumerate(top)
            )
        )
        return top
    
    return []


# ============================================================
# 行业强度查询 (供 trend_first L2 使用)
# ============================================================
_sector_strength_cache: Dict[str, float] = {}
_sector_cache_time: float = 0
SECTOR_CACHE_TTL = 3600  # 1小时缓存


def get_sector_strength(sector: str) -> float:
    """
    获取行业动态强度分数 (0-100)
    
    替代 trend_first L2 的静态 sector_score。
    动态计算，基于行业 ETF 的实际表现。
    
    用法:
        # trend_first.py L2:
        # 旧: sector_score = ctx.metadata.get('sector_score', 50)
        # 新: sector_score = get_sector_strength(sector_name)
    """
    import time
    global _sector_strength_cache, _sector_cache_time
    
    now = time.time()
    if now - _sector_cache_time > SECTOR_CACHE_TTL or not _sector_strength_cache:
        # 刷新缓存
        for s, etf in SECTOR_ETFS.items():
            klines = _get_kline_data(f"US.{etf}", days=20)
            if klines:
                rets = _calc_returns(klines, [5, 10, 20])
                # 综合分数: 短期 40% + 中期 60%
                score = 50 + (rets.get(5, 0) * 40 + rets.get(20, 0) * 60) * 100
                _sector_strength_cache[s] = max(0, min(100, score))
            else:
                _sector_strength_cache[s] = 50.0
        _sector_cache_time = now
    
    return _sector_strength_cache.get(sector, 50.0)


def get_sector_direction_bias() -> Dict[str, str]:
    """
    获取每个行业的方向偏向
    
    Returns: {"Technology": "LONG", "Energy": "SHORT", ...}
    
    用法:
        # 只从 LONG 偏向行业选做多标的
        # 只从 SHORT 偏向行业选做空标的
    """
    import time
    global _sector_strength_cache, _sector_cache_time
    
    now = time.time()
    if now - _sector_cache_time > SECTOR_CACHE_TTL or not _sector_strength_cache:
        get_sector_strength("Technology")  # 触发缓存刷新
    
    result = {}
    for sector, score in _sector_strength_cache.items():
        if score >= 60:
            result[sector] = "LONG"
        elif score <= 40:
            result[sector] = "SHORT"
        else:
            result[sector] = "NEUTRAL"
    
    return result
