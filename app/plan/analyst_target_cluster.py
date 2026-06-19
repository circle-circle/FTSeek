"""分析师目标价簇解析

对标 deep research 报告中的目标价簇分析（如 MU 的"7+ 家 6 月调价全部落 $900-$1,750，簇中枢 ~$1,050-$1,250"）。

核心逻辑:
1. 从 FMP analyst endpoint 或 WebSearch 获取逐条目标价
2. 过滤明显过时的旧目标价（>60 天前的不采用）
3. 计算近期调价的区间（低/高）和中枢（median）
4. 与当前价格对比，计算上行/下行空间
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 缓存目录
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "analyst_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _parse_target_price(text: str) -> Optional[float]:
    """从文本中提取目标价数字"""
    if not text:
        return None
    # 匹配 $XXX 或纯数字
    m = re.search(r'\$?\s*([\d,]+(?:\.\d+)?)', str(text))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _fetch_fmp_analysts(symbol: str) -> Optional[List[Dict]]:
    """从 FMP 获取分析师目标价（有 API 限制时降级）"""
    try:
        import requests
        bare = symbol.replace("US.", "")
        # FMP free tier 可能受限，尝试后降级
        url = f"https://financialmodelingprep.com/api/v3/price-target?symbol={bare}&apikey=demo"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.debug(f"[target_cluster] FMP fetch failed for {symbol}: {e}")
    return None


def _load_cache(symbol: str, max_age_hours: int = 24) -> Optional[Dict]:
    """加载缓存"""
    bare = symbol.replace("US.", "")
    cache_file = _CACHE_DIR / f"{bare}_targets.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=max_age_hours):
            return data
    except Exception:
        pass
    return None


def _save_cache(symbol: str, data: Dict):
    """保存缓存"""
    bare = symbol.replace("US.", "")
    cache_file = _CACHE_DIR / f"{bare}_targets.json"
    try:
        data["cached_at"] = datetime.now().isoformat()
        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


def compute_target_cluster(
    symbol: str,
    current_price: float,
    manual_targets: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """计算分析师目标价簇。

    Args:
        symbol: 股票代码 (如 "US.MU")
        current_price: 当前价格
        manual_targets: 手动提供的目标价列表
            [{"institution": "Goldman", "date": "2026-06-10", "old": 400, "new": 900, "rating": "Neutral"}, ...]

    Returns:
        {
            "cluster": {"low": 900, "high": 1750, "median": 1100, "count": 7},
            "upside_pct": 12.1,
            "position_in_cluster": "下沿",  # 下沿/中部/上沿/超出
            "recent_actions": [...],
            "consensus_rating": "Strong Buy",
            "data_source": "FMP/WebSearch/cache",
            "stale_warning": false,
        }
    """
    bare = symbol.replace("US.", "")
    result = {
        "cluster": None,
        "upside_pct": None,
        "position_in_cluster": "N/A",
        "recent_actions": [],
        "consensus_rating": "N/A",
        "data_source": "none",
        "stale_warning": False,
    }

    # 1. 尝试手动提供的目标价
    targets = manual_targets or []

    # 2. 尝试缓存
    if not targets:
        cached = _load_cache(symbol)
        if cached:
            targets = cached.get("targets", [])
            result["data_source"] = "cache"

    # 3. 尝试 FMP
    if not targets:
        fmp_data = _fetch_fmp_analysts(symbol)
        if fmp_data:
            for item in fmp_data[:20]:
                target = _parse_target_price(item.get("priceTarget") or item.get("price_target"))
                if target and target > 0:
                    targets.append({
                        "institution": item.get("analystFirm", item.get("analyst", "Unknown")),
                        "date": item.get("publishedDate", item.get("date", ""))[:10],
                        "target": target,
                        "rating": item.get("rating", "N/A"),
                    })
            if targets:
                result["data_source"] = "FMP"

    if not targets:
        result["stale_warning"] = True
        return result

    # 4. 过滤和提取目标价
    valid_targets = []
    now = datetime.now()

    for t in targets:
        target_price = t.get("target") or t.get("new") or _parse_target_price(str(t.get("target_price", "")))
        if not target_price or target_price <= 0:
            continue

        # 过滤过时的（>60天前）
        date_str = str(t.get("date", ""))[:10]
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                if (now - dt).days > 60:
                    continue
            except ValueError:
                pass  # 日期解析失败，仍然采用

        valid_targets.append({
            "institution": t.get("institution", "Unknown"),
            "date": date_str,
            "target": target_price,
            "rating": t.get("rating", "N/A"),
            "old_target": t.get("old"),
        })

    if not valid_targets:
        result["stale_warning"] = True
        return result

    # 5. 计算簇
    prices = [t["target"] for t in valid_targets]
    prices.sort()

    cluster_low = prices[0]
    cluster_high = prices[-1]
    cluster_median = prices[len(prices) // 2] if len(prices) % 2 == 1 else (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) / 2

    result["cluster"] = {
        "low": round(cluster_low, 2),
        "high": round(cluster_high, 2),
        "median": round(cluster_median, 2),
        "count": len(valid_targets),
    }

    # 6. 计算上行空间和位置
    if current_price > 0:
        upside_to_median = (cluster_median - current_price) / current_price * 100
        result["upside_pct"] = round(upside_to_median, 1)

        if current_price < cluster_low:
            result["position_in_cluster"] = "低于簇下沿"
        elif current_price < cluster_low + (cluster_high - cluster_low) * 0.33:
            result["position_in_cluster"] = "簇下沿"
        elif current_price < cluster_low + (cluster_high - cluster_low) * 0.67:
            result["position_in_cluster"] = "簇中部"
        elif current_price <= cluster_high:
            result["position_in_cluster"] = "簇上沿"
        else:
            result["position_in_cluster"] = "超出簇上沿"

    # 7. 共识评级
    ratings = [t["rating"] for t in valid_targets if t.get("rating") and t["rating"] != "N/A"]
    if ratings:
        buy_count = sum(1 for r in ratings if "buy" in r.lower() or "overweight" in r.lower() or "outperform" in r.lower())
        hold_count = sum(1 for r in ratings if "hold" in r.lower() or "neutral" in r.lower() or "equal" in r.lower())
        sell_count = sum(1 for r in ratings if "sell" in r.lower() or "underperform" in r.lower())
        total = len(ratings)
        if buy_count > total * 0.6:
            result["consensus_rating"] = "Strong Buy"
        elif buy_count > total * 0.4:
            result["consensus_rating"] = "Buy"
        elif hold_count > total * 0.5:
            result["consensus_rating"] = "Hold"
        else:
            result["consensus_rating"] = f"Buy {buy_count}/Hold {hold_count}/Sell {sell_count}"

    result["recent_actions"] = valid_targets[:10]  # 最多 10 条

    # 缓存
    _save_cache(symbol, {"targets": valid_targets, "cluster": result["cluster"]})

    return result


def format_cluster_text(cluster_data: Dict) -> str:
    """格式化为 prompt 友好的文本"""
    if not cluster_data or not cluster_data.get("cluster"):
        return "(分析师目标价数据缺失)"

    c = cluster_data["cluster"]
    lines = [
        f"- 目标价簇: ${c['low']}-${c['high']} (中位数 ${c['median']}, {c['count']}家机构)",
        f"- 当前位置: {cluster_data.get('position_in_cluster', 'N/A')}",
    ]
    if cluster_data.get("upside_pct") is not None:
        lines.append(f"- 至中位数上行: {cluster_data['upside_pct']:+.1f}%")
    if cluster_data.get("consensus_rating"):
        lines.append(f"- 共识评级: {cluster_data['consensus_rating']}")

    # 逐条动作
    actions = cluster_data.get("recent_actions", [])
    if actions:
        lines.append("- 近期调价:")
        for a in actions[:5]:
            old = f"${a['old_target']}" if a.get("old_target") else ""
            lines.append(f"  · {a['institution']} {a['date']}: {old}→${a['target']} ({a.get('rating', '')})")

    return "\n".join(lines)


__all__ = [
    "compute_target_cluster",
    "format_cluster_text",
]
