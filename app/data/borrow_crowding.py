# -*- coding: utf-8 -*-
"""借券池拥挤度信号 (2026-08-29, RFC-062, 用户裁"继续采集并获取拥挤度信号").

输入: data/shadow/borrow_rate_series.jsonl (borrow_rate_snapshot.py 每日盘前采集).
输出: 每标的拥挤度信号 {level, score, reasons}, 供:
  1. premarket_enrichment 注入 (决策参考, record-only 不改交易行为)
  2. scripts/borrow_crowding_eval.py 历史评估

预注册判据 (2026-08-29, 数据实证定标, 不得事后修改):
  主维度 = fee_rate (年化借券费率):
    - 中位/众数 = 3.00% (94.6% 行), >3.0% 即异常
    - WATCH:    fee > 3.0%
    - WARNING:  fee > 5.0%
    - CRITICAL: fee > 8.0%  (BIRK 8/14 12.7% 实证事件后连跌5天)
  辅维度 = pool 绝对值 (券池余量):
    - pool < 10,000 股 → 升级一档 (BIRK 2,600股 实证)
  禁用维度 = pool 日间比值 (实证陷阱):
    - 74个"池枯竭"事件全是大盘股报价噪声 (AAPL 426M→50M / MSFT 206M→2.3M
      但费率恒3.0) — 券商券池报价本身数量级波动, 非拥挤信号。

已知局限 (如实标注):
  - SIM 环境费率静态 (47个观测≥5天标的仅 QQQ 有微小变化), 信号事件稀疏
  - 历史真事件仅 5 个 (BIRK/SAN×2/FUTU/QQQ), 评估脚本样本不足时只观察不裁决
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
SERIES_PATH = BASE_DIR / "data" / "shadow" / "borrow_rate_series.jsonl"

# 预注册阈值 (2026-08-29 数据实证: 费率众数3.0%, 池p10=63,881)
FEE_WATCH = 3.0
FEE_WARNING = 5.0
FEE_CRITICAL = 8.0
POOL_TINY = 10_000      # 券池 <1万股 → 升级一档 (可借券稀缺)
POOL_LOW = 100_000      # 券池 <10万股 → 标注 (不做空拥挤信号, 供参考)


def load_series(path: Optional[Path] = None) -> Dict[str, Dict[str, Dict]]:
    """读借券序列 → {symbol: {date: row}}, 每标的每日取最后一行."""
    p = path or SERIES_PATH
    out: Dict[str, Dict[str, Dict]] = {}
    if not p.exists():
        return out
    with open(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            sym, date = r.get("symbol"), r.get("date")
            if sym and date:
                out.setdefault(sym, {})[date] = r
    return out


def compute_signal(row: Dict[str, Any]) -> Dict[str, Any]:
    """单行快照 → 拥挤度信号 (预注册判据).

    Returns: {level: NONE|WATCH|WARNING|CRITICAL, score: float, reasons: [str]}
    """
    reasons: List[str] = []
    fee = row.get("fee_rate")
    pool = row.get("pool")

    level = "NONE"
    score = 0.0

    if not isinstance(fee, (int, float)):
        return {"level": "NONE", "score": 0.0, "reasons": ["no_fee_data"]}

    # 语义隔离 (2026-08-29 首评实弹纠错): "无借券市场" ≠ 拥挤。
    #   shortable=False + pool=0 + 费率基线 = 券商对该票根本没有借券池
    #   (小盘/新上市), 不是做空拥挤; 且已被下单前门 borrow_pool_gate
    #   (is_short_permit=False) 覆盖 — 再标记为拥挤 = 重复职责 + 稀释真信号。
    if (row.get("shortable") is False
            and (not isinstance(pool, (int, float)) or pool <= 0)
            and fee <= FEE_WATCH):
        return {"level": "NONE", "score": 0.0,
                "reasons": ["no_borrow_market"], "no_borrow_market": True}

    # 主维度: 费率
    if fee > FEE_CRITICAL:
        level, score = "CRITICAL", 3.0
        reasons.append(f"费率 {fee:.2f}% > {FEE_CRITICAL}% (借券极贵, 做空高度拥挤)")
    elif fee > FEE_WARNING:
        level, score = "WARNING", 2.0
        reasons.append(f"费率 {fee:.2f}% > {FEE_WARNING}% (借券昂贵)")
    elif fee > FEE_WATCH:
        level, score = "WATCH", 1.0
        reasons.append(f"费率 {fee:.2f}% > {FEE_WATCH}% 基线 (借券成本抬升)")

    # 辅维度: 小池升级 (只在有信号时升级, 小池本身不构成拥挤)
    if isinstance(pool, (int, float)) and pool < POOL_TINY and level != "NONE":
        if level == "WATCH":
            level, score = "WARNING", 2.0
        elif level == "WARNING":
            level, score = "CRITICAL", 3.0
        reasons.append(f"券池仅 {pool:,.0f} 股 (<{POOL_TINY:,}, 可借券稀缺)")
    elif isinstance(pool, (int, float)) and pool < POOL_LOW:
        reasons.append(f"券池偏低 {pool:,.0f} 股 (<{POOL_LOW:,})")

    # 有池但被券商标记不可卖空 (罕见): 仍属拥挤预警语义
    if row.get("shortable") is False:
        _order = "NONE WATCH WARNING CRITICAL".split()
        if _order.index(level) < _order.index("WARNING"):
            level, score = "WARNING", max(score, 2.0)
        reasons.append("券商不可卖空 (shortable=False)")

    return {"level": level, "score": score, "reasons": reasons}


def scan(series: Optional[Dict[str, Dict[str, Dict]]] = None,
         date: Optional[str] = None,
         symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """扫描最新快照 → 拥挤度信号汇总.

    Args:
        series: 借券序列 (默认读盘)
        date: 指定日期 (默认序列内最新日期)
        symbols: 只看这些标的 (默认全量)

    Returns:
        {"date": str, "n_scanned": int, "flagged": [
            {symbol, level, score, fee_rate, pool, reasons}, ...]}
        flagged 按 score 降序。
    """
    series = series or load_series()
    if not series:
        return {"date": None, "n_scanned": 0, "flagged": []}

    all_dates = {d for days in series.values() for d in days}
    target = date or max(all_dates)

    flagged = []
    n_scanned = 0
    for sym, days in series.items():
        if symbols is not None and sym not in symbols:
            continue
        row = days.get(target)
        if not row:
            continue
        n_scanned += 1
        sig = compute_signal(row)
        if sig["level"] != "NONE":
            flagged.append({
                "symbol": sym, "level": sig["level"], "score": sig["score"],
                "fee_rate": row.get("fee_rate"), "pool": row.get("pool"),
                "reasons": sig["reasons"],
            })
    flagged.sort(key=lambda x: -x["score"])
    return {"date": target, "n_scanned": n_scanned, "flagged": flagged}


def format_for_context(summary: Dict[str, Any]) -> str:
    """格式化为盘前上下文文本 (仅在有信号时非空)."""
    flagged = summary.get("flagged", [])
    if not flagged:
        return ""
    lines = ["📉 SHORT BORROW CROWDING (借券拥挤度, record-only):"]
    for f in flagged[:5]:
        sym = f["symbol"].replace("US.", "")
        lines.append(f"  ⚠️ {sym} [{f['level']}] fee={f['fee_rate']:.2f}% "
                     f"pool={f['pool']:,.0f}股 — {'; '.join(f['reasons'][:2])}")
    lines.append("  (做空拥挤 → 轧空风险↑ / 借券成本↑; 不构成方向指令)")
    return "\n".join(lines)
