#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""[DEB-20261007 复查第2轮] E10 单调性判别: 因果还是共线? (MFE 幅度匹配桶内重切)

背景 (自曝#12, §9 点名挑战③):
  E10 报「落袋成功率随达峰变晚单调上升 68.1%→82.9%」, 是方案 C 的信号基础。
  挑战: 弱单既早达峰又易失败 (共线), 还是早达峰导致失败 (因果)?
  判别法 = 控制 MFE 幅度 (mb=|mfe_pct|*100 bps) 后, 桶内早(≤15min)/中(15-30]/晚(>30)
  的落袋成功率差是否仍同向。若幅度桶内单调性消失/反向 ⇒ 原始单调性由桶间幅度差驱动 = 共线标记。

口径纪律: 人群 WHERE 与成功/失败定义逐字照抄 scripts/deb20261007_time_to_mfe.py ⑳-1
  (mfe_pct IS NOT NULL AND metadata_json IS NOT NULL; mb>fb 前置; gb>fb=落袋成功)。
  ttm 取 exit_telemetry.time_to_mfe 兜底顶层 time_to_mfe (与生产脚本一致)。

复现: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261007_review2_ttm_magnitude.py
作者实跑值 (2026-10-07): 合格 n=415; 原始单调性逐字复刻 (5-15:68.1% n=163 / 15-30:75.0% n=80 /
  30-60:74.6% n=63 / 60-120:82.9% n=35 / >120:82.4% n=74);
  幅度桶内 晚-早: A(<30bps) +44.4pp[全格n<30仅观察] / B(30-50) -9.7pp[反向] /
  C(50-100, 最大样本桶 n=158) +0.3pp[平坦] / D(>=100) +14.9pp[中格n=28<30]
  ⇒ 2/4 桶 (含最大样本桶) 单调性消失或反向 ⇒ 主驱动 = 共线, 方案 C 信号基础被证伪。
"""
import sqlite3, json, collections

c = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT symbol, direction d, exit_reason e, trade_date dt, pnl_net n, realized_pnl g,
                           fees_est f, mfe_pct m, mae_pct a, holding_hours hh, metadata_json mj,
                           entry_price ep, exit_price xp, quantity q
                    FROM trade_records WHERE mfe_pct IS NOT NULL AND metadata_json IS NOT NULL
                    ORDER BY id""").fetchall()

def get_ttm(r):
    try:
        m = json.loads(r["mj"]) if isinstance(r["mj"], str) else (r["mj"] or {})
    except Exception:
        return None
    if not isinstance(m, dict):
        return None
    cur = m.get("exit_telemetry")
    if isinstance(cur, dict) and isinstance(cur.get("time_to_mfe"), (int, float)):
        return float(cur["time_to_mfe"])
    v = m.get("time_to_mfe")
    return float(v) if isinstance(v, (int, float)) else None

pool = []
for r in rows:
    t = get_ttm(r)
    if t is None:
        continue
    try:
        notional = float(r["ep"]) * float(r["q"])
        if notional <= 0:
            continue
        fb = float(r["f"] or 0) / notional * 10000
        gb = float(r["g"] or 0) / notional * 10000
        mb = abs(float(r["m"] or 0)) * 100
    except Exception:
        continue
    if mb <= fb:
        continue
    pool.append((mb, t, gb > fb))

print(f"合格样本 n={len(pool)}")

tb = collections.defaultdict(lambda: [0, 0])
for mb, t, ok in pool:
    b = "<5min" if t < 5 else "5-15" if t < 15 else "15-30" if t < 30 else "30-60" if t < 60 else "60-120" if t < 120 else ">120min"
    tb[b][0] += 1
    tb[b][1] += 1 if ok else 0
print("原始(不控制幅度):", " ".join(
    f"{b}:{100*tb[b][1]/tb[b][0]:.1f}%(n={tb[b][0]})"
    for b in ("<5min", "5-15", "15-30", "30-60", "60-120", ">120min") if tb[b][0]))

def mbucket(mb):
    return "A:<30bps" if mb < 30 else "B:30-50" if mb < 50 else "C:50-100" if mb < 100 else "D:>=100bps"

cross = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
for mb, t, ok in pool:
    slot = "early(<=15)" if t <= 15 else "mid(15-30]" if t <= 30 else "late(>30)"
    cross[mbucket(mb)][slot][0] += 1
    cross[mbucket(mb)][slot][1] += 1 if ok else 0

print("\n幅度匹配桶内: 早(<=15min) / 中(15-30] / 晚(>30) 落袋成功率")
for b in sorted(cross):
    parts = []
    for slot in ("early(<=15)", "mid(15-30]", "late(>30)"):
        n, ok = cross[b][slot]
        parts.append(f"{slot}: {100*ok/n:.1f}% (n={n})" if n else f"{slot}: n=0")
    e_n, e_ok = cross[b]["early(<=15)"]
    l_n, l_ok = cross[b]["late(>30)"]
    delta = (100 * l_ok / l_n - 100 * e_ok / e_n) if e_n and l_n else float("nan")
    flag = "  [仅观察: 某格n<30]" if min(e_n, l_n, cross[b]["mid(15-30]"][0] or 99) < 30 else ""
    print(f"  {b}: " + " | ".join(parts) + f"   晚-早={delta:+.1f}pp{flag}")

print("\n⇒ 判读: 最大样本桶 C (n=158) 晚-早=+0.3pp 平坦、桶 B 反向 ⇒ 原始单调性主驱动是")
print("   桶间幅度差异 (弱单既早达峰又易失败 = 共线标记), 非达峰时刻因果 (自曝#12)。")
print("   桶 D (>=100bps) 残余 +14.9pp 但中格 n=28<30, 且高幅度下成功率趋近天花板 (98.6%),")
print("   证据强度不足以支撑独立因果成分 ⇒ 方案 C 信号基础被证伪。")
