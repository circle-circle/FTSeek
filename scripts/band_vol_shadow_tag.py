#!/usr/bin/env python3.11
"""入场带宽波动率化影子打标 (2026-08-17, record-only, 零执行路径改动).

背景 (DEB-20260817-symbol-profile-market-sense, 建议②):
  entry_band.py 扁平 band (LONG [-3%,+5%] / SHORT [-5%,+3%]) 全池一套, 模块内
  注释自认"一刀切不合理"且已预留 volatility_pct 桩。本影子用与 SL/TP 同源的
  atr_pct 分桶口径, 计算"波动率化 band"的反事实触达, 攒样本裁决带宽是否该认票。

波动率化口径 (与 _compute_volatility_based_sl_tp 同源, 以 atr_pct 为驱动):
  scale = atr_pct / REF_ATR (REF_ATR=0.025, medium 桶中心)
  knife_scaled = KNIFE_FLOOR_PCT * scale, clamp [KNIFE_MIN, KNIFE_MAX]
  chase_scaled = CHASE_ZONE_PCT  * scale, clamp [CHASE_MIN, CHASE_MAX]
  高波动股 band 放宽 (±3% 是噪音), 低波动股 band 收紧 (±3% 是大动作)。
  参数为影子探索值, 非生产; 裁决前不改 entry_band.py。

反事实指标 (触达率是带宽平凡产物, 无判别力 — 回填实证两臂均100%):
  1. full_day_reach: 当日全 range 是否触带 — 波动率化能否救活扁平死锚
  2. first_touch_min: 首触时滞 (band 是否开盘即触 = 无筛选力)
  3. best_30m_dev_pct: 30min 内有利侧最深偏离 (BUY=低于entry最大幅度,
     SHORT=高于entry), ATR 归一化 — 成交价质量上限对照

回滚: 配置键 band_vol_shadow_enabled=false + 重启。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "band_vol_shadow.jsonl"
DB = REPO / "data" / "ftseek_us.db"
RB_DIR = REPO / "data" / "replay_bars"  # 分钟bar: US_{SYM}_{DATE}.json

# 扁平 band 常量 (与 entry_band.py 单一真源一致, 只读引用)
CHASE_ZONE_PCT = 0.05
KNIFE_FLOOR_PCT = 0.03
# 波动率化探索参数 (影子专用)
REF_ATR = 0.025
KNIFE_MIN, KNIFE_MAX = 0.015, 0.06
CHASE_MIN, CHASE_MAX = 0.02, 0.10
WINDOW_MIN = 30


def load_bars(sym: str, date: str):
    """分钟bar [ts,o,h,l,c] 升序; 无缓存返回 None。自包含内联 (同 plan_vs_actual_report 口径)。"""
    fp = RB_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    out = []
    for r in rows:
        try:
            out.append((datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S"), r[1], r[2], r[3], r[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


def _seen_keys() -> set:
    keys = set()
    if OUT.exists():
        for ln in OUT.read_text().splitlines():
            try:
                d = json.loads(ln)
                keys.add((d["date"], d["id"]))
            except Exception:
                continue
    return keys


def run_check(scheduler=None) -> None:
    main()


def main() -> int:
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.band_vol_shadow_enabled", True)):
            return 0
    except Exception:
        pass

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    insts = [dict(r) for r in c.execute(
        "SELECT id, date, symbol, direction, entry_price, entry_low, entry_high, "
        "skip_reason, triggered, executed, decision_id, vol_bucket, atr_pct, "
        "execution_price "
        "FROM playbook_instructions WHERE date >= '2026-06-14' AND entry_price > 0 "
        "ORDER BY date").fetchall()]
    c.close()

    seen = _seen_keys()
    new_rows = []
    n_skip_noatr = 0
    for inst in insts:
        if inst["skip_reason"] or inst["entry_low"] is None:
            continue
        key = (inst["date"], inst["id"])
        if key in seen:
            continue
        atr = inst["atr_pct"]
        if not atr or atr <= 0:
            n_skip_noatr += 1
            continue
        bars = load_bars(inst["symbol"], inst["date"])
        if not bars:
            continue
        e = inst["entry_price"]
        is_short = inst["direction"] == "SELL_SHORT"

        # 扁平 band (生产现状)
        if is_short:
            f_lo, f_hi = e * (1 - CHASE_ZONE_PCT), e * (1 + KNIFE_FLOOR_PCT)
        else:
            f_lo, f_hi = e * (1 - KNIFE_FLOOR_PCT), e * (1 + CHASE_ZONE_PCT)

        # 波动率化 band (反事实)
        scale = atr / REF_ATR
        knife = max(KNIFE_MIN, min(KNIFE_MAX, KNIFE_FLOOR_PCT * scale))
        chase = max(CHASE_MIN, min(CHASE_MAX, CHASE_ZONE_PCT * scale))
        if is_short:
            v_lo, v_hi = e * (1 - chase), e * (1 + knife)
        else:
            v_lo, v_hi = e * (1 - knife), e * (1 + chase)

        first30 = bars[:WINDOW_MIN]

        def _touch_idx(lo_b, hi_b, bars_seq):
            for i, b in enumerate(bars_seq):
                if b[3] <= hi_b and b[2] >= lo_b:
                    return i
            return None

        flat_first = _touch_idx(f_lo, f_hi, bars)
        vol_first = _touch_idx(v_lo, v_hi, bars)
        # 30min 有利侧最深偏离 (BUY: entry-最低价; SHORT: 最高价-entry), ATR归一化
        if is_short:
            best30 = max((b[2] - e) for b in first30) if first30 else 0.0
        else:
            best30 = max((e - b[3]) for b in first30) if first30 else 0.0

        # 成交价质量 (真正的判别轴 — 触达率两臂均100%无判别力):
        #   exec_dev_atr = 成交价相对 entry 的不利偏离 (追高/追空深度), ATR归一
        #   would_reject_flat/vol = 成交价若在该臂 band 之外 → 该臂会拒掉此成交
        exec_px = inst.get("execution_price")
        exec_dev_atr = None
        rej_flat = rej_vol = None
        if exec_px and exec_px > 0:
            if is_short:
                dev = (exec_px - e) / e  # 空单成交高于 entry = 不利 (追空)
            else:
                dev = (exec_px - e) / e  # 多单成交高于 entry = 不利 (追高)
            exec_dev_atr = round(dev / atr, 3)
            rej_flat = not (f_lo <= exec_px <= f_hi)
            rej_vol = not (v_lo <= exec_px <= v_hi)

        new_rows.append({
            "date": inst["date"], "id": inst["id"], "symbol": inst["symbol"],
            "direction": inst["direction"], "vol_bucket": inst["vol_bucket"],
            "atr_pct": round(atr, 5), "scale": round(scale, 3),
            "flat_band": [round(f_lo, 4), round(f_hi, 4)],
            "vol_band": [round(v_lo, 4), round(v_hi, 4)],
            "flat_width_pct": round((f_hi - f_lo) / e * 100, 2),
            "vol_width_pct": round((v_hi - v_lo) / e * 100, 2),
            "flat_full_day_reach": flat_first is not None,
            "vol_full_day_reach": vol_first is not None,
            "flat_first_touch_min": flat_first,
            "vol_first_touch_min": vol_first,
            "best_30m_dev_atr": round(best30 / (e * atr), 3) if atr else None,
            "exec_dev_atr": exec_dev_atr,
            "would_reject_flat": rej_flat, "would_reject_vol": rej_vol,
            "triggered": inst["triggered"], "executed": inst["executed"],
        })

    if new_rows:
        with OUT.open("a") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = len(seen) + len(new_rows)
    print(f"[band_vol_shadow] 累计 {total} 行 (+{len(new_rows)}), 无atr跳过 {n_skip_noatr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
