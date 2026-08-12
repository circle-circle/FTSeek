#!/usr/bin/env python3
"""陈旧锚影子打标 (2026-08-12 Plan B, record-only, 零执行路径改动).

背景 (8/12 入场价格机制评估): 6笔DA指令锚价 vs 盘前价对照暴露两类陈旧:
  - AVGO: 锚$403 vs 盘前$421 (锚低4.3%, 趋势上行锚追不上 → 可能整天等不到)
  - HON:  锚$239.5 vs 盘前$231 (锚高3.7%, 支撑已破位锚悬空)
既有门已覆盖深度场景: drift门(逆向>5%永久作废) / extreme_gap(>8%逆作废) /
出带影子(shadow_portfolio_entries). 真实缺口 = 趋势逃逸但锚陈旧、未触发又错过.

本脚本用 trigger_miss_telemetry (每轮观测价, 11k+条) 重建每笔指令当日价格路径,
对【未触发】指令打陈旧度标签 (锚太保守/太乐观/在带内) + 结局, 攒样验证
"陈旧锚是否与错过盈利/接破位亏损相关", n≥30 再裁是否加硬门或接 entry_reference.
每行双标签: stale=价格空间(与方向无关), stale_dir_normalized=交易空间
(BUY.too_low ≡ SELL_SHORT.too_high = conservative_miss; 2026-08-12 审查补).

挂载: scheduler_core AFTER_HOURS, 异常隔离.
输出: data/stale_anchor_shadow.jsonl, 按 (inst_id, date) 去重幂等.
回滚: playbook.stale_anchor_shadow_enabled 置 false + 重启.
"""
import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("ftseek.stale_anchor_shadow")

_BASE = Path(__file__).resolve().parents[1]
_OUT = _BASE / "data" / "stale_anchor_shadow.jsonl"
_DB = _BASE / "data" / "ftseek_us.db"
_MISS = _BASE / "data" / "trigger_miss_telemetry.jsonl"

_KNIFE = 0.03   # 与 entry_band.KNIFE_FLOOR_PCT 同源
_CHASE = 0.05   # 与 entry_band.CHASE_ZONE_PCT 同源

# 方向归一化 (价格空间标签 → 交易空间语义):
# BUY.too_low ≡ SELL_SHORT.too_high = 锚太保守, 趋势错过; 镜像为太乐观.
_DIR_NORM = {
    ("BUY", "too_low"): "conservative_miss",
    ("SELL_SHORT", "too_high"): "conservative_miss",
    ("BUY", "too_high"): "optimistic_miss",
    ("SELL_SHORT", "too_low"): "optimistic_miss",
    ("BUY", "below_center"): "weak_conservative",
    ("SELL_SHORT", "above_center"): "weak_conservative",
    ("BUY", "above_center"): "weak_optimistic",
    ("SELL_SHORT", "below_center"): "weak_optimistic",
}


def _dir_norm(stale: str, direction: str) -> str:
    return _DIR_NORM.get((direction, stale), stale)


def _enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.stale_anchor_shadow_enabled", True))
    except Exception:
        return True


def _seen_keys() -> set:
    seen = set()
    if _OUT.exists():
        for line in _OUT.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                seen.add((d.get("inst_id"), d.get("date")))
            except Exception:
                continue
    return seen


def _load_price_paths() -> dict:
    """trigger_miss → {inst_id: [观测价,...]} (每轮 current_price)."""
    paths = defaultdict(list)
    if not _MISS.exists():
        return {}
    for line in _MISS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        iid = d.get("inst_id")
        cp = d.get("current_price")
        if iid is None or cp is None:
            continue
        paths[iid].append(float(cp))
    return paths


def _classify(anchor, prices) -> dict:
    """锚陈旧度分类 (价格空间, 与方向无关): 锚低于当日观测带=too_low, 高于=too_high.
    交易空间语义 (谁被错过) 由 _dir_norm 按方向归一化推导.
    用观测中位数定'真实价格中枢', 锚相对中枢的偏离定陈旧方向."""
    if not prices or anchor <= 0:
        return {"stale": "no_data", "dev_pct": None}
    prices = [p for p in prices if p and p > 0]
    if not prices:
        return {"stale": "no_data", "dev_pct": None}
    prices_sorted = sorted(prices)
    med = prices_sorted[len(prices_sorted) // 2]
    if med <= 0:
        return {"stale": "no_data", "dev_pct": None}
    dev = (anchor - med) / med * 100.0  # 锚相对价格中枢偏离%
    hi, lo = max(prices), min(prices)
    if anchor < lo * (1 - _KNIFE):
        cls = "too_low"      # 锚低于当日观测低点-knife → 趋势上行, 锚太保守, 大概率错过
    elif anchor > hi * (1 + _CHASE):
        cls = "too_high"     # 锚高于当日观测高点+chase → 锚太乐观, 追不上
    elif anchor < med * (1 - _KNIFE):
        cls = "below_center" # 锚低于中枢-knife (弱保守)
    elif anchor > med * (1 + _KNIFE):
        cls = "above_center" # 锚高于中枢+knife (弱乐观)
    else:
        cls = "in_band"
    return {"stale": cls, "dev_pct": round(dev, 2),
            "obs_lo": round(lo, 4), "obs_hi": round(hi, 4),
            "obs_med": round(med, 4), "n_obs": len(prices)}


def run_check(scheduler=None) -> None:
    if not _enabled():
        return
    seen = _seen_keys()
    paths = _load_price_paths()

    # 防早产打标 (2026-08-12 独立复查补): 指令日期 D 只在 D 的美东收盘后 (≥16:05 ET)
    # 才允许打标。盘前/盘中手动运行时当日遥测尚不完整, 若落盘 no_data 会被幂等键锁死,
    # 盘后重跑跳过 → OOS 样本永久污染 (实锤: 8/12 盘前 run [08:11 ET] 把当日 6 条指令
    # 落盘 no_data, 后续重跑含 09:00 ET 手动再生全被幂等键跳过; 其中 BWXT/GOOG/AVGO
    # 随后 10:18 ET 实际成交, jsonl executed 字段永远冻结 False)。源头过滤, 零改下游。
    try:
        from datetime import datetime as _dt
        import pytz as _pytz
        _now_et = _dt.now(_pytz.timezone("America/New_York"))
    except Exception:
        _now_et = None
    _today_et = _now_et.strftime("%Y-%m-%d") if _now_et else None
    _day_over = _now_et is not None and (_now_et.hour * 60 + _now_et.minute) >= 965

    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    insts = conn.execute("""
        SELECT id, date, symbol, direction, entry_price anchor, triggered, executed,
               skip_reason, pnl, confidence, atr_pct, decision_id
        FROM playbook_instructions
        WHERE entry_price > 0
        ORDER BY date, id
    """).fetchall()

    # 结局 (已执行指令的净盈亏)
    outcomes = {}
    for r in conn.execute("""
        SELECT decision_id, pnl_net, exit_reason FROM trade_records
        WHERE decision_id IS NOT NULL AND decision_id != ''
    """):
        outcomes.setdefault(r["decision_id"], (r["pnl_net"], r["exit_reason"]))
    conn.close()

    new_rows = []
    n_deferred = 0
    for inst in insts:
        key = (inst["id"], inst["date"])
        if key in seen:
            continue
        # 防早产: 当日指令等收盘后 (≥16:05 ET) 再打标
        if inst["date"] == _today_et and not _day_over:
            n_deferred += 1
            continue
        iid = inst["id"]
        prices = paths.get(iid, [])
        anchor = inst["anchor"]
        cls = _classify(anchor, prices)
        oc = outcomes.get(inst["decision_id"], (None, None))
        row = {
            "date": inst["date"], "inst_id": iid, "symbol": inst["symbol"],
            "direction": inst["direction"], "decision_id": inst["decision_id"],
            "anchor": anchor, "confidence": inst["confidence"],
            "atr_pct": inst["atr_pct"],
            # 陈旧度 (价格空间 + 方向归一化交易空间)
            **cls,
            "stale_dir_normalized": _dir_norm(cls["stale"], inst["direction"]),
            # 触发/执行结局
            "triggered": bool(inst["triggered"]), "executed": bool(inst["executed"]),
            "skip_reason": inst["skip_reason"],
            "trade_pnl_net": oc[0], "trade_exit_reason": oc[1],
        }
        new_rows.append(row)

    if not new_rows:
        return
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    n_stale = 0
    with _OUT.open("a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if row["stale_dir_normalized"] in ("conservative_miss", "optimistic_miss"):
                n_stale += 1
    logger.info(
        f"[陈旧锚影子] 打标 {len(new_rows)} 笔 (强陈旧 {n_stale} 笔"
        + (f", 当日 {n_deferred} 笔延迟到收盘后" if n_deferred else "")
        + f") → {_OUT.name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_check()
    print(f"done → {_OUT}")
