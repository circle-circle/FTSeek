#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场环境真值 read-through —— 「口径 + as-of」的唯一出口 (RFC-190 第三轮)

背景 (为什么需要这个模块):
  RFC-190 实测同一天存在两个 VIX, 且**两边都不写口径与观测日**:
    - 执行门/regime 侧: VIXY×0.85 代理。盘前读的是 Futu 快照 `last_price`,
      实测 2026-10-07..11-06 共 23 个交易日**全部等于上一交易日收盘**
      (RFC-190 §1A) ⇒ 它既不"实时", 也不是现货。
    - 看板/health 侧: FRED VIXCLS 现货收盘, 但常滞后 1 天 (RFC-190 §7)。
  同一个数字 (28.18) 在不同时点可能是"现货收盘"、"代理估算"或"两天前的收盘",
  而读数面只有一个裸浮点 ⇒ 人/模型会把 T-2 的 18 当成"此刻平静"。

本模块只做三件事 (零取数、零缓存、不改任何判定):
  1. 读 `data/market_env_daily.jsonl` (CBOE 官方日线 + SPY 已实现波动, 由
     `scripts/market_env_ledger.py` 产出) 的最新一行 → 真值 + 年龄 (as-of)。
  2. 把"值 + 口径 + 观测日 + 代理偏离"合成**一行可读文本**与**结构化 payload**,
     供看板 / 日评分 / 交易论据等读数面共用。
  3. 口径词表唯一入口 (`caliber_label` / `caliber_short`) —— 与
     `app/data/vix_source.py` 的取数/换算 SSOT 互补: 那里管"怎么算", 这里管
     "算出来的是哪个口径、哪一天"。

刻意不做的事:
  - **不设阈值、不参与任何交易判定** (RFC-190 §1C: 现货>28 的硬门在 A10 窗口内
    只拦掉最赚钱的一天, 属负 edge)。本模块是"让真值可见", 不是"让真值关门"。
  - 不建缓存/不联网: 账本由 `market_env_ledger.py build` 维护, 过期只如实标注。
  - 不替调用方兜底: 账本缺失/过期返回 truth=None + 明确 stale 原因, 绝不编数值。

sign 约定 (与 `market_env_ledger.proxy_gap` 同一公式):
  gap_pct = (proxy / truth - 1) * 100
  **负数 = 代理低于真值 (低估)**; 正数 = 代理高估。
  (RFC-190 §1B 引用的是负数口径: 11-06 代理 18.04 vs 真值 25.64 = -29.7%。)

回滚: 删除本文件 + 各读数面改回裸数值 (本模块不写任何生产文件)。
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
LEDGER = _ROOT / "data" / "market_env_daily.jsonl"
LEDGER_MAX_AGE_HOURS = 30.0

# VIX 取数口径词表 (与各生产者的 source 字段逐字对齐; 新增来源必须在此登记,
# 否则 label 会是"未知口径" —— 那本身就是需要被看见的状态, 不静默成"现货")。
_CALIBER = {
    "fred": "FRED VIXCLS 现货收盘",
    "alphavantage": "AlphaVantage ^VIX 现货",
    "yahoo_fallback": "Yahoo ^VIX 现货",
    "finnhub_vixy_proxy": "VIXY×0.85 期货代理",
    "vixy_proxy_live": "VIXY×0.85 期货代理",
}
_SHORT = {
    "fred": "现货收盘",
    "alphavantage": "现货",
    "yahoo_fallback": "现货",
    "finnhub_vixy_proxy": "代理",
    "vixy_proxy_live": "代理",
}
_PROXY_SOURCES = ("finnhub_vixy_proxy", "vixy_proxy_live")


def caliber_label(source: Optional[str]) -> str:
    """source 字段 → 人类可读口径。未知来源如实标"未知口径"。"""
    if not source:
        return "未知口径"
    return _CALIBER.get(source, "未知口径(%s)" % source)


def caliber_short(source: Optional[str]) -> str:
    """看板角标用的短口径 (代理 / 现货收盘 / 未知)。"""
    if not source:
        return "未知"
    return _SHORT.get(source, "未知")


def is_proxy_source(source: Optional[str]) -> bool:
    return source in _PROXY_SOURCES


def load_latest(max_age_hours: float = LEDGER_MAX_AGE_HOURS) -> Optional[Dict[str, Any]]:
    """读账本最新一行并附 `_age_hours` / `_stale`; 账本缺失或不可解析返回 None。

    过期 **不返回 None** —— 过期也是事实, 必须能被读数面标出来; 由调用方按
    `_stale` 决定是否只展示"真值不可用"。
    """
    try:
        if not LEDGER.exists():
            return None
        lines = [ln for ln in LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        row = json.loads(lines[-1])
        age_h = (time.time() - LEDGER.stat().st_mtime) / 3600.0
        row["_age_hours"] = round(age_h, 1)
        row["_stale"] = age_h > max_age_hours
        return row
    except Exception as exc:  # 读账本失败必须可见 (否则"真值不可用"与"账本坏了"无法区分)
        logger.warning("[market_env_truth] 读取环境账本失败: %s", exc, exc_info=True)
        return None


def gap_pct(proxy_value: Optional[float], truth_value: Optional[float]) -> Optional[float]:
    """代理相对真值的偏离 %; 负数 = 代理低估。无效输入返回 None (不兜底)。"""
    try:
        p = float(proxy_value) if proxy_value is not None else 0.0
        t = float(truth_value) if truth_value is not None else 0.0
    except (TypeError, ValueError):
        return None
    if p <= 0 or t <= 0:
        return None
    return round((p / t - 1) * 100, 2)


def _term_text(slope: Optional[float]) -> str:
    if slope is None:
        return "期限 n/a"
    return "期限 %+.1f%% %s" % (slope * 100, "倒挂" if slope > 0 else "正常")


def describe(proxy_value: Optional[float] = None,
             proxy_source: Optional[str] = None,
             proxy_as_of: Optional[str] = None,
             max_age_hours: float = LEDGER_MAX_AGE_HOURS) -> Dict[str, Any]:
    """合成结构化 payload —— 所有读数面共用的唯一形状。

    Returns:
        {
          "truth": {"date","vix_close","vix9d","vix3m","term_slope","term_text",
                    "vix_pct252","rv20_spy","proxy_premarket","proxy_gap_pct",
                    "age_hours","stale"} | None,
          "proxy": {"value","source","caliber","as_of"} | None,
          "gap_pct": float | None,        # 负数 = 代理低估
          "line": str,                    # 一行可读文本 (含口径 + as-of)
        }
    """
    row = load_latest(max_age_hours=max_age_hours)
    truth = None
    if row is not None:
        truth = {
            "date": row.get("date"),
            "vix_close": row.get("vix_close"),
            "vix9d": row.get("vix9d"),
            "vix3m": row.get("vix3m"),
            "term_slope": row.get("term_slope"),
            "term_text": _term_text(row.get("term_slope")),
            "vix_pct252": row.get("vix_pct252"),
            "rv20_spy": row.get("rv20_spy"),
            "proxy_premarket": row.get("vix_proxy_premarket"),
            "proxy_gap_pct": row.get("proxy_gap_pct"),
            "age_hours": row.get("_age_hours"),
            "stale": bool(row.get("_stale")),
        }

    proxy = None
    if proxy_value is not None:
        proxy = {
            "value": round(float(proxy_value), 2),
            "source": proxy_source or "unknown",
            "caliber": caliber_label(proxy_source),
            "short": caliber_short(proxy_source),
            "as_of": proxy_as_of,
        }

    gap = gap_pct(proxy["value"] if proxy else None,
                  truth["vix_close"] if truth else None)

    # ── 一行文本: 值在前 (人先看数), 口径/as-of 紧跟, 真值与偏离在后 ──
    if proxy:
        head = "VIX %.2f (%s%s)" % (
            proxy["value"], proxy["caliber"],
            "·as of %s" % proxy["as_of"] if proxy["as_of"] else "·as of 未知")
    elif truth:
        head = "VIX %.2f 现货收盘 %s" % (truth["vix_close"], truth["date"])
    else:
        head = "VIX 不可用"

    if truth is None:
        tail = "环境真值账本不可用 (缺失或不可解析)"
    else:
        parts = []
        if proxy is not None:  # 无代理值时 head 已是真值, 不再重复
            parts.append("真值 %.2f 现货收盘 %s" % (truth["vix_close"], truth["date"]))
        if truth["vix_pct252"] is not None:
            parts.append("分位 %s%%" % truth["vix_pct252"])
        parts.append(truth["term_text"])
        if truth["rv20_spy"] is not None:
            parts.append("rv20 %s%%" % truth["rv20_spy"])
        if gap is not None:
            if is_proxy_source(proxy_source):
                parts.append("代理%s %.1f%%" % ("低估" if gap < 0 else "高估", abs(gap)))
            else:
                # 现货源与真值同口径: 偏离只可能来自滞后 (FRED 比 CBOE 慢一天),
                # 不能说成"低估/高估" —— 那是给代理用的词。
                parts.append("相对真值 %+.1f%% (口径/滞后差)" % gap)
        if truth["stale"]:
            parts.append("⚠️账本过期 %.1fh" % (truth["age_hours"] or 0))
        tail = " | ".join(parts)

    return {"truth": truth, "proxy": proxy, "gap_pct": gap,
            "line": "%s | %s" % (head, tail)}


def line(proxy_value: Optional[float] = None,
         proxy_source: Optional[str] = None,
         proxy_as_of: Optional[str] = None,
         max_age_hours: float = LEDGER_MAX_AGE_HOURS) -> str:
    """`describe(...)["line"]` 的快捷入口。"""
    return describe(proxy_value=proxy_value, proxy_source=proxy_source,
                    proxy_as_of=proxy_as_of, max_age_hours=max_age_hours)["line"]