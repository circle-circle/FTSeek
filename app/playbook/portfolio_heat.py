"""组合在险总帽 (portfolio heat) — 全仓同时打到止损时亏多少。

背景 (DEB-20261031-budget-design): 现存仓位控制只有**单笔**两层帽 (风险预算 risk_pct、
名义额 max_position_pct), **没有组合层在险上限** —— `max_open_positions=999`,
实测并发持仓峰值 18 笔 (¥106k 名义 > 权益), 18×$284 ≈ 5.1% 权益同时暴露在止损上。

本模块只做一件事: 把所有未平仓位的「到止损还要亏多少」加总, 与配置的百分比上限比较。
- 0 = 关闭 (默认, 与 admission 的 `0=不拦` 语义一致)
- 任何异常 ⇒ **fail-open** (返回 None 不拦): 组合帽读不到库时不应阻断交易,
  但要留 warning, 否则"帽静默失效"会伪装成"帽起作用了"。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "ftseek_us.db"
_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None}
_CACHE_TTL_S = 20.0


def _stop_of(row: sqlite3.Row) -> Optional[float]:
    """取有效止损: current_sl → initial_sl → stop_loss_price (任一为正)。"""
    for key in ("current_sl", "initial_sl", "stop_loss_price"):
        try:
            v = row[key]
        except (IndexError, KeyError):
            v = None
        if v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return f
    return None


def open_risk_usd(db_path: Optional[Path] = None, *, use_cache: bool = True) -> Tuple[float, int]:
    """当前所有未平仓位的在险金额合计 (美元) 与笔数。

    在险 = |入场 − 有效止损| × 股数。止损缺失的行 **按 0 计**并计入 `missing_stop` 日志,
    不臆造风险值 (宁可低估也不虚报)。
    """
    now = time.time()
    if use_cache and _CACHE["value"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        return _CACHE["value"]  # type: ignore[return-value]

    path = db_path or _DB_PATH
    total = 0.0
    count = 0
    missing = 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT symbol, size, avg_entry_price, current_sl, initial_sl, stop_loss_price "
                "FROM open_positions"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            count += 1
            try:
                qty = float(row["size"] or 0)
                entry = float(row["avg_entry_price"] or 0)
            except (TypeError, ValueError):
                continue
            stop = _stop_of(row)
            if stop is None or qty <= 0 or entry <= 0:
                missing += 1
                continue
            total += abs(entry - stop) * qty
    except Exception as exc:  # noqa: BLE001 — fail-open 是设计意图
        logger.warning(f"[portfolio_heat] 读 open_positions 失败, 本次不拦: {exc}")
        return (0.0, 0)

    if missing:
        logger.warning(f"[portfolio_heat] {missing}/{count} 笔持仓缺止损, 其风险未计入合计")
    result = (round(total, 2), count)
    _CACHE["ts"], _CACHE["value"] = now, result
    return result


def heat_reason(cfg: Dict[str, Any], candidate_risk_usd: float, equity: float,
                *, db_path: Optional[Path] = None) -> Optional[str]:
    """超帽则返回判据字符串 (供 admission reasons), 否则 None。

    cfg: admission 配置 dict, 读 `max_open_risk_pct` (百分比, 0=关闭)。
    """
    try:
        cap_pct = float(cfg.get("max_open_risk_pct") or 0.0)
    except (TypeError, ValueError):
        cap_pct = 0.0
    if cap_pct <= 0 or equity <= 0 or candidate_risk_usd <= 0:
        return None
    cap_usd = equity * cap_pct / 100.0
    # 显式传 db_path 时绕开进程级缓存 —— 否则第二个调用方 (尤其测试) 会读到上一个库的值。
    cur, _n = open_risk_usd(db_path, use_cache=(db_path is None))
    if cur + candidate_risk_usd > cap_usd:
        return (f"PORTFOLIO_HEAT({cur + candidate_risk_usd:.0f}>${cap_usd:.0f},"
                f"在险{cur:.0f}+本笔{candidate_risk_usd:.0f})")
    return None


def reset_cache() -> None:
    _CACHE["ts"], _CACHE["value"] = 0.0, None