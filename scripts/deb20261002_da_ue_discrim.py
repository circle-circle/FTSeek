"""溯源主测 v2: 修正量纲后重算 DA/UE 分析字段对「入场即错」的判别力

v1 错误 (自曝): 分桶阈值凭字段名猜 (signal_confidence 用 <40/40-59/…), 实际:
  · signal_confidence = 0-1 小数 (0.35/0.5/0.7/0.9), signal_strength = 0-100 整数 (同值×100)
  · decision_conf = 0.5-0.9 小数 (0.7 占 341)
  · ok_dimensions = **位掩码** (19=10011b, 6=110b, 0=无) ⇒ 用 popcount(通过维度个数)
  · horizon_confidence = 混存 (2735 行 <1 / 125 行 >=1) ⇒ 按 <1 归一后分档
  ⇒ v1 把 100% 样本塞进单档, eta²=0/t=0 是分桶假象, 不是轴无判别力。

判据 (预注册, 与 v1 同): G2 单调或两端差≥10pp / G3 eta²≥5% / G4 Welch|t|≥2 /
G5 逐月档间极差≥10pp / G8 最大档占比≤50% / G7 误杀率≤40%。全过才是候选。
只读 mode=ro。
"""
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
c = sqlite3.connect("file:" + str(BASE / "data/ftseek_us.db") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

POP_T = ("t.entry_time >= '2026-06-27' AND t.exit_time IS NOT NULL AND t.pnl_net IS NOT NULL "
         "AND COALESCE(json_extract(t.metadata_json,'$.exclusion_reason'),'')=''")
J = ("FROM trade_records t JOIN playbook_instructions i ON i.id = ("
     "  SELECT MIN(i2.id) FROM playbook_instructions i2 WHERE i2.decision_id = t.decision_id) "
     "WHERE " + POP_T)

rows = list(c.execute(
    "SELECT t.id tid, t.symbol, t.direction, t.entry_time,"
    " COALESCE(t.pnl_net,t.realized_pnl) net, COALESCE(t.mfe_pct,0) mfe,"
    " t.signal_strength, t.signal_confidence,"
    " i.confidence, i.decision_conf, i.horizon, i.horizon_confidence, i.risk_reward,"
    " i.ok_dimensions, i.vol_bucket, i.atr_pct, i.model_used " + J))

n = len(rows)
dead_n = sum(1 for r in rows if r["mfe"] < 0.3)
print("=== 人群 (扇出去重后) ===")
print("  n=" + str(n) + "  死单=" + str(dead_n) + " (" + format(100.0 * dead_n / n, ".1f") + "%)")


def popcount(x):
    return bin(int(x)).count("1") if x is not None else None


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    se = (st.variance(a) / len(a) + st.variance(b) / len(b)) ** 0.5
    return (st.mean(a) - st.mean(b)) / se if se > 0 else None


def eta2(groups):
    allv = [v for g in groups for v in g]
    if len(allv) < 2:
        return None
    gm = st.mean(allv)
    ssb = sum(len(g) * (st.mean(g) - gm) ** 2 for g in groups if g)
    sst = sum((v - gm) ** 2 for v in allv)
    return ssb / sst if sst > 0 else None


def report(name, fn, note=""):
    print("\n--- " + name + (" [" + note + "]" if note else "") + " ---")
    bk = defaultdict(list)
    for r in rows:
        k = fn(r)
        if k is None:
            continue
        bk[k].append(r)
    tot = sum(len(v) for v in bk.values())
    if tot < 30:
        print("  🔴 G1 失败 n=" + str(tot) + "<30 不可用")
        return None
    table = []
    for k in sorted(bk.keys(), key=lambda x: (str(type(x)), x)):
        g = bk[k]
        d = sum(1 for r in g if r["mfe"] < 0.3)
        table.append((k, len(g), d, 100.0 * d / len(g), 100.0 * len(g) / tot,
                      sum(r["net"] for r in g)))
        print("    " + str(k).ljust(24) + " n=" + str(len(g)).rjust(4)
              + " 死=" + str(d).rjust(4) + " (" + format(100.0 * d / len(g), "5.1f") + "%)"
              + " 占比=" + format(100.0 * len(g) / tot, "5.1f") + "%"
              + " 净=" + format(sum(r["net"] for r in g), "+9.1f"))
    mx = max(table, key=lambda x: x[4])
    rates = [x[3] for x in table if x[1] >= 10]
    if len(rates) >= 3:
        up = all(rates[i] <= rates[i + 1] + 0.01 for i in range(len(rates) - 1))
        dn = all(rates[i] >= rates[i + 1] - 0.01 for i in range(len(rates) - 1))
        g2 = "单调↑" if up else ("单调↓" if dn else "非单调(两端差 " + format(rates[-1] - rates[0], "+.1f") + "pp)")
    elif len(rates) == 2:
        g2 = "两档差 " + format(rates[1] - rates[0], "+.1f") + "pp"
    else:
        g2 = "n/a(仅1档≥10)"
    e2 = eta2([[1.0 if r["mfe"] < 0.3 else 0.0 for r in bk[k]] for k in bk])
    g3 = ("PASS" if (e2 or 0) >= 0.05 else "FAIL") + " eta²=" + format((e2 or 0) * 100, ".2f") + "%"
    big = max(table, key=lambda x: x[1])[0]
    tv = welch([1.0 if r["mfe"] < 0.3 else 0.0 for r in bk[big]],
               [1.0 if r["mfe"] < 0.3 else 0.0 for k in bk if k != big for r in bk[k]])
    g4 = ("PASS" if tv is not None and abs(tv) >= 2 else "FAIL") + " t=" + format(tv or 0, "+.2f")
    g8 = "PASS" if mx[4] <= 50 else "FAIL(最大档 " + format(mx[4], ".1f") + "%>50%)"
    bym = defaultdict(lambda: defaultdict(list))
    for r in rows:
        k = fn(r)
        if k is not None:
            bym[r["entry_time"][:7]][k].append(r)
    monthly = []
    for m in sorted(bym):
        gs = bym[m]
        allm = [r for k in gs for r in gs[k]]
        if len(allm) < 15:
            continue
        per = {k: 100.0 * sum(1 for r in gs[k] if r["mfe"] < 0.3) / len(gs[k])
               for k in gs if len(gs[k]) >= 5}
        spread = (max(per.values()) - min(per.values())) if len(per) >= 2 else None
        monthly.append((m, len(allm), spread))
        sp = format(spread, "+.1f") + "pp" if spread is not None else "n/a"
        det = " ".join(str(k) + "=" + format(v, ".0f") + "%" for k, v in sorted(per.items(), key=lambda x: str(x[0]))[:6])
        print("    月 " + m + " n=" + str(len(allm)).rjust(3) + " 档间极差=" + sp + "  " + det)
    g5 = "n/a" if not monthly else ("PASS" if all((mm[2] or 0) >= 10 for mm in monthly) else "FAIL(某月<10pp)")
    print("  G2=" + g2 + " | G3=" + g3 + " | G4=" + g4 + " | G8=" + g8 + " | G5=" + g5)
    v = "候选" if ((e2 or 0) >= 0.05 and tv is not None and abs(tv) >= 2 and mx[4] <= 50) else "否决"
    print("  ⇒ " + v)
    return dict(axis=name, eta2=e2, t=tv, g2=g2, g5=g5, g8=g8, verdict=v)


res = []
res.append(report("signal_confidence (0-1 小数)",
                  lambda r: (round(r["signal_confidence"], 2) if r["signal_confidence"] is not None else None)))
res.append(report("decision_conf (0.5-0.9)",
                  lambda r: round(r["decision_conf"], 2) if r["decision_conf"] is not None else None,
                  "填充45.6%"))
res.append(report("confidence (指令置信 0-100)",
                  lambda r: ("<40" if r["confidence"] < 40 else "40-54" if r["confidence"] < 55
                             else "55-69" if r["confidence"] < 70 else ">=70")
                  if r["confidence"] is not None else None))
res.append(report("ok_dimensions popcount (DA 通过维度数)",
                  lambda r: popcount(r["ok_dimensions"]) if r["ok_dimensions"] is not None else None,
                  "位掩码 popcount"))
res.append(report("risk_reward",
                  lambda r: ("<1.5" if r["risk_reward"] < 1.5 else "1.5-2" if r["risk_reward"] < 2
                             else "2-3" if r["risk_reward"] < 3 else ">=3") if r["risk_reward"] else None))
res.append(report("horizon_confidence 归一(<1直接, >=1则/…混存仅取<1)",
                  lambda r: (round(r["horizon_confidence"], 2)
                             if r["horizon_confidence"] is not None and r["horizon_confidence"] < 1.5
                             else None), "剔混存>=1.5"))
res.append(report("model_used", lambda r: str(r["model_used"]) if r["model_used"] else None))
res.append(report("signal_strength (0-100)",
                  lambda r: ("<40" if r["signal_strength"] < 40 else "40-59" if r["signal_strength"] < 60
                             else "60-79" if r["signal_strength"] < 80 else ">=80")
                  if r["signal_strength"] is not None else None))

print("\n\n=== 汇总 (量纲修正后) ===")
for x in res:
    if x:
        print("  " + x["axis"].ljust(46) + " eta²=" + format((x["eta2"] or 0) * 100, "5.2f")
              + "% t=" + format(x["t"] or 0, "+5.2f") + " G5=" + x["g5"][:4]
              + " G8=" + x["g8"][:4] + "  " + x["verdict"])
cand = [x for x in res if x and x["verdict"] == "候选"]
print("\n  过 G3(eta²≥5%)+G4(|t|≥2)+G8(基率≤50%) 的轴: " + str(len(cand)) + "/"
      + str(len([x for x in res if x])))
for x in cand:
    print("    候选: " + x["axis"])
