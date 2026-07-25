#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期望值 vs confidence 的 IC 回测（只读）
==========================================
目的: 验证"期望值替代 confidence"是否有数据基础（P0 前置验证，零风险）。

算三组 Spearman IC（目标变量 = 实际 pnl_pct）:
  [1] IC(signal_confidence, pnl_pct)      —— 对照组, 验证 confidence 是否反指
  [2] IC(edge_exp_conf,    pnl_pct)      —— confidence 桶期望值 (latest.json 的
                                             expectancy_by_bucket)。注意: 该表由历史
                                             同一批数据拟合, 全量 IC 存在"样本内虚高",
                                             故另做"时间切分样本外"验证 (见 [4])。
  [3] IC(E_struct,         pnl_pct)      —— 独立结构期望值 (entry 时 SL/TP 风险归一,
                                             不依赖历史拟合, 无样本内偏差), 这才是
                                             真正的"期望值是否正指"验证。
  [4] 时间切分样本外 IC(edge_exp_conf)    —— 前 70% 拟合桶期望, 后 30% 验证。

数据清洗（筛选严重偏差和污染数据）:
  - data_quality IN ('clean','real')     剔除 direction_mismatch / manual / outlier_external
  - source_type NOT IN ('MANUAL','POSITION_SYNC')  剔除手动单 / 持仓同步单 (非系统自动信号)
  - signal_confidence / pnl_pct 非空
  - |pnl_pct| 超 MAD 极端值剔除 (默认 5×MAD, 稳健)
  - 构造 E_struct 时剔除 SL/TP 缺失或方向异常 (down<=0 / up<=0)

只读: 不修改任何 DB / 文件。
运行: PYTHONPATH=. ./venv/bin/python scripts/expectancy_ic_backtest.py
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
LATEST = ROOT / "data" / "signal_quality" / "latest.json"

# 清洗规则常量
KEEP_DATA_QUALITY = ("clean", "real")          # 剔除 direction_mismatch/manual/outlier_external
DROP_SOURCE_TYPE = ("MANUAL", "POSITION_SYNC")  # 剔除手动单/持仓同步单
MAD_K = 5.0                                     # 极端值剔除阈值 (5×MAD)


# ---------- Spearman (纯 Python, 与 signal_ic_tracker 同逻辑) ----------
def _rank(values):
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    return ranks


def spearman(x, y):
    """返回 (ic, n)。剔除 None 对; n<3 或零方差返回 (None, n)。"""
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    n = len(pairs)
    if n < 3:
        return None, n
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    xr, yr = _rank(xs), _rank(ys)
    mx, my = sum(xr) / n, sum(yr) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    vx = sum((a - mx) ** 2 for a in xr)
    vy = sum((b - my) ** 2 for b in yr)
    if vx == 0 or vy == 0:
        return None, n
    return cov / math.sqrt(vx * vy), n


# ---------- confidence 桶 ----------
def conf_bucket(c):
    if c < 0.3:
        return "0-0.3"
    if c < 0.5:
        return "0.3-0.5"
    if c < 0.7:
        return "0.5-0.7"
    return "0.7-1.0"


# ---------- 独立结构期望值 (entry SL/TP 风险归一) ----------
def struct_expectancy(direction, entry, sl, tp):
    """返回 (E_struct, RR)。E_struct = up% - down% (假设 50/50 胜率); RR = up%/down%。
    剔除 SL/TP 缺失或方向异常 (返回 (None, None))。"""
    try:
        entry, sl, tp = float(entry), float(sl), float(tp)
    except (TypeError, ValueError):
        return None, None
    if entry <= 0 or sl <= 0 or tp <= 0:
        return None, None
    d = (direction or "").upper()
    if d == "LONG":
        up, down = (tp - entry) / entry, (entry - sl) / entry
    elif d == "SHORT":
        up, down = (entry - tp) / entry, (sl - entry) / entry
    else:
        return None, None
    if down <= 0 or up <= 0:  # SL/TP 在错误侧 → 异常
        return None, None
    return (up - down), (up / down)


# ---------- 数据加载 + 清洗 ----------
def load_closed():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT symbol, direction, entry_price, exit_price, pnl_pct, realized_pnl,
               signal_confidence, signal_strength, source_type, data_quality,
               strategy_id, exit_time, metadata_json
        FROM trade_records
        WHERE exit_price IS NOT NULL AND realized_pnl IS NOT NULL AND pnl_pct IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def main():
    raw = load_closed()
    n0 = len(raw)

    # 清洗 1: data_quality
    step1 = [r for r in raw if (r.get("data_quality") or "") in KEEP_DATA_QUALITY]
    # 清洗 2: source_type
    step2 = [r for r in step1 if (r.get("source_type") or "") not in DROP_SOURCE_TYPE]
    # 清洗 3: conf 非空
    step3 = [r for r in step2 if r.get("signal_confidence") is not None]

    # 清洗 4: |pnl_pct| MAD 极端值剔除
    pnls = [r["pnl_pct"] for r in step3]
    med = median(pnls)
    abs_dev = [abs(p - med) for p in pnls]
    mad = median(abs_dev) or 0.0
    thr = med + MAD_K * mad if mad > 0 else None
    if thr is not None:
        step4 = [r for r in step3 if abs(r["pnl_pct"] - med) <= MAD_K * mad]
    else:
        step4 = step3
    n_outlier = len(step3) - len(step4)

    rows = step4
    n = len(rows)

    print("=" * 64)
    print("期望值 vs confidence  IC 回测 (只读)")
    print("=" * 64)
    print(f"数据源: {DB.name}  |  expectancy表: {LATEST.name}")
    print(f"\n[数据清洗] 原始已平仓 {n0}")
    print(f"  - data_quality∈{KEEP_DATA_QUALITY}: 剔 {n0 - len(step1)}  (剩 {len(step1)})")
    print(f"  - source_type∉{DROP_SOURCE_TYPE}: 剔 {len(step1) - len(step2)}  (剩 {len(step2)})")
    print(f"  - conf 非空: 剔 {len(step2) - len(step3)}  (剩 {len(step3)})")
    print(f"  - |pnl_pct| MAD 极端 (med={med:.4f}, MAD={mad:.4f}, 阈值±{MAD_K}MAD): 剔 {n_outlier}  (剩 {n})")

    # 读 expectancy_by_bucket (latest.json)
    try:
        latest = json.load(open(LATEST))
        eb = latest.get("threshold_recommendations", {}).get("expectancy_by_bucket", {}) or {}
    except Exception as e:
        eb = {}
        print(f"\n[警告] latest.json 读取失败: {e}")
    print(f"\n[expectancy_by_bucket] {json.dumps(eb, ensure_ascii=False)}")

    # 构造每笔的特征
    conf_x, pnl_y = [], []
    exp_conf_x = []           # confidence 桶期望值 (样本内)
    estruct_x, rr_x = [], []
    n_struct = 0
    for r in rows:
        c = r.get("signal_confidence")
        y = r.get("pnl_pct")
        conf_x.append(c)
        pnl_y.append(y)
        exp_conf_x.append(eb.get(conf_bucket(c)))
        # 解析 metadata_json 的 SL/TP
        e_s, rr = None, None
        mj = r.get("metadata_json")
        if mj:
            try:
                md = json.loads(mj)
                sl = md.get("stop_loss")
                tp = md.get("take_profit")
                if sl is not None and tp is not None:
                    e_s, rr = struct_expectancy(r.get("direction"), r.get("entry_price"), sl, tp)
            except Exception:
                pass
        if e_s is not None:
            n_struct += 1
        estruct_x.append(e_s)
        rr_x.append(rr)

    # [1] confidence IC (对照)
    ic_conf, n_conf = spearman(conf_x, pnl_y)
    # [2] confidence 桶期望值 IC (样本内, 虚高)
    ic_ebc, n_ebc = spearman(exp_conf_x, pnl_y)
    # [3] 独立结构期望值 IC (无样本内偏差)
    ic_es, n_es = spearman(estruct_x, pnl_y)
    ic_rr, n_rr = spearman(rr_x, pnl_y)

    print("\n" + "=" * 64)
    print("[全量 IC]  (目标 = 实际 pnl_pct)")
    print("=" * 64)
    print(f"  [1] signal_confidence      IC={_f(ic_conf)}  n={n_conf}   <- 对照(验证反指)")
    print(f"  [2] edge_exp_conf(桶)      IC={_f(ic_ebc)}  n={n_ebc}   <- 样本内, 虚高仅供参考")
    print(f"  [3] E_struct(SL/TP结构)    IC={_f(ic_es)}  n={n_es}   <- 独立期望值, 真验证")
    print(f"  [3b] RR(风险回报比)        IC={_f(ic_rr)}  n={n_rr}")
    print(f"  (E_struct 可构造样本: {n_struct}/{n})")

    # [4] 时间切分样本外 IC (edge_exp_conf)
    print("\n" + "=" * 64)
    print("[时间切分样本外 IC]  edge_exp_conf (前70%拟合 → 后30%验证)")
    print("=" * 64)
    srt = sorted(rows, key=lambda r: r.get("exit_time") or "")
    cut = int(len(srt) * 0.7)
    train, test = srt[:cut], srt[cut:]
    bp = {}
    for r in train:
        bp.setdefault(conf_bucket(r["signal_confidence"]), []).append(r["pnl_pct"])
    fit = {b: sum(v) / len(v) for b, v in bp.items() if v}
    tx = [fit.get(conf_bucket(r["signal_confidence"])) for r in test]
    ty = [r["pnl_pct"] for r in test]
    ic_oos, n_oos = spearman(tx, ty)
    print(f"  拟合桶期望: {json.dumps({k: round(v, 4) for k, v in fit.items()})}")
    print(f"  [4] 样本外 edge_exp_conf  IC={_f(ic_oos)}  n={n_oos}")

    # 结论提示
    print("\n" + "=" * 64)
    print("[判读]  IC>0.05 有预测力 | IC≈0 噪音 | IC<0 反向")
    print("=" * 64)
    print(f"  confidence:      {_verdict(ic_conf)}")
    print(f"  E_struct(独立):  {_verdict(ic_es)}")
    print(f"  样本外 conf桶:   {_verdict(ic_oos)}")
    print("\n说明: [2]全量 conf桶 IC 因样本内拟合必然偏高, 以 [4]样本外 与 [3]独立E_struct 为准。")
    print("只读完成, 未修改任何数据。")


def _f(ic):
    return f"{ic:+.4f}" if ic is not None else "  N/A "


def _verdict(ic):
    if ic is None:
        return "N/A (样本不足或零方差)"
    if ic > 0.05:
        return f"{ic:+.4f} 正指 ✓"
    if ic < -0.05:
        return f"{ic:+.4f} 反指 ✗"
    return f"{ic:+.4f} ≈噪音"


if __name__ == "__main__":
    main()
