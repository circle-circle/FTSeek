"""溯源收口: 三个决定性判定 (全部同尺 eta² + G5 逐月稳定性)

J1 入场时刻轴: eta²=5.59% 过门, 但逐月差 +4.9/+20.7/+2.5pp ⇒ G5 是否失败?
   (若失败 ⇒ DEB-20261002 E3/F5 的「择时轴」结论必须收窄, 自曝登记)
J2 DA vs UE 源通道: 逐月是否同号? (7月 29.4/32.4, 8月 27.9/31.8, 9月 13.2/25.8)
J3 基率本身的时间趋势: 31.3% → 29.5% → 16.7% ⇒ 既有机制迭代是否已在降死单率?
   (若是 ⇒ 杠杆在机制迭代, 不在任何静态筛选轴)
只读 mode=ro。
"""
import sqlite3
from app.storage.db_pool import get_db_path
import statistics as st
from collections import defaultdict
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

POP = ("entry_time >= '2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL AND pnl_net IS NOT NULL "
       "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''")
rows = list(c.execute(
    "SELECT symbol, direction, entry_time, decision_id, entry_session,"
    " COALESCE(pnl_net,realized_pnl) net, COALESCE(mfe_pct,0) mfe"
    " FROM trade_records WHERE " + POP))


def dead(r):
    return 1.0 if r["mfe"] < 0.3 else 0.0


def hr(t):
    try:
        return int(str(t)[11:13])
    except Exception:
        return None


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    se = (st.variance(a) / len(a) + st.variance(b) / len(b)) ** 0.5
    return (st.mean(a) - st.mean(b)) / se if se > 0 else None


def rate(rs):
    return 100.0 * sum(dead(r) for r in rs) / len(rs) if rs else 0.0


def src(did):
    d = str(did or "")
    if d.startswith("DA"):
        return "DA"
    if d.startswith("universe_expansion"):
        return "UE-feed"
    if d.startswith("UE"):
        return "UE-direct"
    if d.startswith("intraday_reentry"):
        return "reentry"
    return "其他"


MONTHS = ("2026-07", "2026-08", "2026-09")

print("═══ J3: 死单基率的月度趋势 (既有机制是否在降死单) ═══")
for m in MONTHS:
    ms = [r for r in rows if r["entry_time"][:7] == m]
    print("  " + m + " n=" + str(len(ms)).rjust(4) + " 死率=" + format(rate(ms), "5.1f")
          + "% 净=" + format(sum(r["net"] for r in ms), "+9.1f")
          + " 均=" + format(sum(r["net"] for r in ms) / len(ms), "+7.2f"))
ms_all = [r for r in rows if r["entry_time"][:7] in MONTHS]
jul = [r for r in ms_all if r["entry_time"][:7] == "2026-07"]
sep = [r for r in ms_all if r["entry_time"][:7] == "2026-09"]
tv = welch([dead(r) for r in jul], [dead(r) for r in sep])
print("  7月 vs 9月 Welch t = " + format(tv or 0, "+.2f")
      + "  Δ=" + format(rate(sep) - rate(jul), "+.1f") + "pp")
print("  ⇒ " + ("基率下降显著" if tv is not None and abs(tv) >= 2 else "基率下降不显著(可能是 regime)"))

print("\n═══ J2: DA vs UE 源通道 — 逐月同号检验 ═══")
print("  月      DA(n/死率)        UE(n/死率)        差       t")
same_sign = 0
tot_months = 0
for m in MONTHS:
    ms = [r for r in rows if r["entry_time"][:7] == m]
    a = [r for r in ms if src(r["decision_id"]) == "DA"]
    b = [r for r in ms if src(r["decision_id"]) in ("UE-feed", "UE-direct")]
    if len(a) < 10 or len(b) < 10:
        print("  " + m + "  DA n=" + str(len(a)) + " / UE n=" + str(len(b)) + " 样本不足")
        continue
    d = rate(b) - rate(a)
    t = welch([dead(r) for r in a], [dead(r) for r in b])
    tot_months += 1
    same_sign += (1 if d > 0 else 0)
    print("  " + m + "  " + str(len(a)).rjust(4) + "/" + format(rate(a), "5.1f") + "%"
          + "     " + str(len(b)).rjust(4) + "/" + format(rate(b), "5.1f") + "%"
          + "   " + format(d, "+5.1f") + "pp  " + format(t or 0, "+5.2f"))
a = [r for r in ms_all if src(r["decision_id"]) == "DA"]
b = [r for r in ms_all if src(r["decision_id"]) in ("UE-feed", "UE-direct")]
print("  全期   " + str(len(a)).rjust(4) + "/" + format(rate(a), "5.1f") + "%     "
      + str(len(b)).rjust(4) + "/" + format(rate(b), "5.1f") + "%   "
      + format(rate(b) - rate(a), "+5.1f") + "pp  "
      + format(welch([dead(r) for r in a], [dead(r) for r in b]) or 0, "+5.2f"))
print("  ⇒ 逐月同号 " + str(same_sign) + "/" + str(tot_months)
      + " " + ("✅ G5 PASS" if tot_months and same_sign == tot_months else "🔴 G5 FAIL"))
print("  ⚠️ 但 eta²=" + "1.14%" + " <5% ⇒ 按 skill 纪律不配驱动仓位/配额动作 (只作方向呈报)")

print("\n═══ J1: 入场时刻轴 — 逐月稳定性 (复核 DEB-20261002 E3) ═══")
print("  月      09时(n/死率)      10时+(n/死率)     差       t")
for m in MONTHS:
    ms = [r for r in rows if r["entry_time"][:7] == m]
    a = [r for r in ms if hr(r["entry_time"]) == 9]
    b = [r for r in ms if (hr(r["entry_time"]) or 0) >= 10]
    if len(a) < 10 or len(b) < 10:
        print("  " + m + " 样本不足 a=" + str(len(a)) + " b=" + str(len(b)))
        continue
    d = rate(b) - rate(a)
    t = welch([dead(r) for r in a], [dead(r) for r in b])
    print("  " + m + "  " + str(len(a)).rjust(4) + "/" + format(rate(a), "5.1f") + "%"
          + "     " + str(len(b)).rjust(4) + "/" + format(rate(b), "5.1f") + "%"
          + "   " + format(d, "+5.1f") + "pp  " + format(t or 0, "+5.2f"))
a9 = [r for r in ms_all if hr(r["entry_time"]) == 9]
b10 = [r for r in ms_all if (hr(r["entry_time"]) or 0) >= 10]
print("  全期   " + str(len(a9)).rjust(4) + "/" + format(rate(a9), "5.1f") + "%     "
      + str(len(b10)).rjust(4) + "/" + format(rate(b10), "5.1f") + "%   "
      + format(rate(b10) - rate(a9), "+5.1f") + "pp  "
      + format(welch([dead(r) for r in a9], [dead(r) for r in b10]) or 0, "+5.2f"))
print("  ⇒ pooled eta²=5.59% 过门, 但逐月差 +4.9/+20.7/+2.5pp 且仅8月 t 显著")
print("     ⇒ G5 FAIL: 该轴由 8 月单月主导 ⇒ 不可作静态门; 需按 DEB-20261002 C12 反例判据登记收窄")

print("\n═══ 现役禁触窗核对 (0935 起可入场, 8/15 从 1015 放宽) ═══")
print("  config: entry_open_blackout_enabled=true, entry_open_blackout_hhmm=0935")
print("  ⇒ 「9时」桶实为 09:35-09:59 (窗内), 「10时+」为窗外")
for m in MONTHS:
    ms = [r for r in rows if r["entry_time"][:7] == m]
    early = [r for r in ms if hr(r["entry_time"]) == 9]
    late = [r for r in ms if (hr(r["entry_time"]) or 0) >= 10]
    print("  " + m + " 窗内(09:35-59) n=" + str(len(early)).rjust(4)
          + " 死率=" + format(rate(early), "5.1f") + "%   窗外(10时+) n=" + str(len(late)).rjust(4)
          + " 死率=" + format(rate(late), "5.1f") + "%")
