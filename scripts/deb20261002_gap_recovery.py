"""用户设想裁决: 隔夜跳空仓「扩大SL / 补仓」能否回本?

数据源: data/loss_exit_recovery_2026-09-28.json (仓内既有产物, loss_exit_recovery_check.py 生成,
        方向感知回翻口径, cache_snapshot=2026-09-28 — 引用必须带此快照日期)
字段实测 (不凭记忆): reason / bkt / ttr_close / ttr_touch / ttr_be_arm / loss_pct /
                    max_close_gain_pct / net_recov / win_avail / direction / net / symbol / tid
只读: 零副作用。

三问:
  Q1 OVERNIGHT_GAP_STOP 族离场后, 价格回翻到入场价(=回本)的比例与耗时? 按亏损深度分桶。
  Q2 与「扩大SL」等价的反事实: 若不出场继续持有 ⇒ 净结局 (net_recov vs net)。
  Q3 「补仓摊平」算术: 该族隔夜跳空穿透 vs SL 距离 ⇒ 补仓后新保本线是否可达?
"""
import json
from collections import defaultdict
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
d = json.load(open(BASE / "data/loss_exit_recovery_2026-09-28.json"))
trades = d["trades"]
print(f"产物: cache_snapshot={d['cache_snapshot']} n={d['n']} "
      f"direction_aware={d.get('direction_aware')} windows={d.get('windows')} "
      f"net_pad={d.get('net_pad')} be_arm_pad={d.get('be_arm_pad')}")
print(f"删失规则: {d.get('censor_rule')}  n_short={d.get('n_short')} r5_bad={d.get('r5_bad')}")

print("\n═══ 该产物的 reason 分布 (人群是什么) ═══")
by_r = defaultdict(list)
for t in trades:
    by_r[t.get("reason") or "(NULL)"].append(t)
for r, ts in sorted(by_r.items(), key=lambda kv: -len(kv[1])):
    print(f"  {r:<26} n={len(ts):>3}")

print("\n═══ Q1: 各亏损离场族的回翻率 (LONG 上行期天然偏高, 须分层读) ═══")
rows = []
for r, ts in by_r.items():
    longs = [t for t in ts if str(t.get("direction", "")).upper() in ("LONG", "BUY")]
    if len(longs) < 3:
        rows.append((r, len(ts), len(longs), None, None))
        continue
    rec = [t for t in longs if isinstance(t.get("ttr_close"), int) and t["ttr_close"] >= 0]
    ttrs = sorted(t["ttr_close"] for t in rec)
    med = ttrs[len(ttrs) // 2] if ttrs else None
    rows.append((r, len(ts), len(longs), len(rec), med))
rows.sort(key=lambda x: -(x[3] / x[2] if x[2] and x[3] is not None else -1))
print(f"{'reason':<26} {'n':>4} {'LONG':>5} {'回翻':>4} {'回翻率':>7} {'耗时中位':>7}")
for r, n, nl, rec, med in rows:
    pct = f"{100*rec/nl:>6.1f}%" if rec is not None else "  n<3"
    print(f"{r:<26} {n:>4} {nl:>5} {(rec if rec is not None else '-'):>4} {pct} {str(med or '-'):>7}")

print("\n═══ Q1b: OVERNIGHT_GAP_STOP 逐笔 ═══")
gap = by_r.get("OVERNIGHT_GAP_STOP", [])
print(f"n={len(gap)}")
for t in sorted(gap, key=lambda x: x.get("exit_date", "")):
    print(f"  {t.get('symbol'):<8} {t.get('direction'):<5} 出{t.get('exit_date')} "
          f"亏{t.get('loss_pct'):>6}% 桶={t.get('bkt'):<5} "
          f"ttr_close={t.get('ttr_close')} ttr_touch={t.get('ttr_touch')} "
          f"ttr_be_arm={t.get('ttr_be_arm')} max_close_gain={t.get('max_close_gain_pct')}% "
          f"net={t.get('net')} net_recov={t.get('net_recov')} win_avail={t.get('win_avail')}")

print("\n═══ Q2: '不出场继续持有' 反事实 — ⚠️ 本产物 net_recov 是 **bool**(是否回翻) 不是金额 ═══")
print("⇒ 无法用本产物算持有反事实的美元结局; 美元口径见 scripts/deb20261002_gap_sl_replay.py")
for bkt in ("deep", "mid", "small"):
    ts = [t for t in trades if t.get("bkt") == bkt
          and str(t.get("direction", "")).upper() in ("LONG", "BUY")]
    if not ts:
        continue
    rec = [t for t in ts if isinstance(t.get("ttr_close"), int) and t["ttr_close"] >= 0]
    s_net = sum(t.get("net") or 0 for t in ts)
    n_rec = sum(1 for t in ts if t.get("net_recov") is True)
    print(f"  {bkt:<6} n={len(ts):>3} 回翻(ttr_close≥0) {len(rec):>3} "
          f"({100*len(rec)/len(ts):>5.1f}%)  net_recov=True {n_rec:>3}  Σ实际net={s_net:>9.1f}")

print("\n═══ Q2b: GAP 族单独 (用户设想的目标人群) ═══")
s_net = sum(t.get("net") or 0 for t in gap)
n_rec = len([t for t in gap if isinstance(t.get("ttr_close"), int) and t["ttr_close"] >= 0])
n_flag = sum(1 for t in gap if t.get("net_recov") is True)
print(f"  n={len(gap)} 回翻 {n_rec} (net_recov=True {n_flag})  Σ实际={s_net:>9.1f}")
print(f"  win_avail (可用窗长) 分布: {[t.get('win_avail') for t in gap]}")
print("  ⚠️ 右删失: ORCL 窗仅 1 日 / RIVN 2 日 / CRM 6 日 ⇒ '未回翻' 含窗口不足的可能;"
      " GAP 窗 18 日、UBER 13 日、SNDK 11 日 窗口够长, 结论对这三笔是硬的")

print("\n═══ Q3: 全体亏损离场族的回翻率总览 (bool 口径, 不求和金额) ═══")
n_rec = len([t for t in trades if isinstance(t.get("ttr_close"), int) and t["ttr_close"] >= 0])
s_net = sum(t.get("net") or 0 for t in trades)
print(f"  全体 n={len(trades)}  回翻 {n_rec} ({100*n_rec/len(trades):.1f}%)  Σ实际net={s_net:>10.1f}")
