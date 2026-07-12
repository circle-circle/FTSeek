#!/usr/bin/env python3
"""IC backfill health check"""
import sqlite3, math, json, os, sys

os.chdir("/opt/ftseek/FTSeek-US")
sys.path.insert(0, ".")

# 1. signal_snapshots.db
print("=" * 60)
print("1. signal_snapshots.db 健康检查")
print("=" * 60)

snap_db = sqlite3.connect("data/signal_snapshots.db")
snap_db.row_factory = sqlite3.Row

total = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots").fetchone()[0]
r1 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE realized >= 1").fetchone()[0]
r3 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE realized = 3").fetchone()[0]
pending = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE realized = 0").fetchone()[0]
has_t1 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL").fetchone()[0]
has_t5 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t5 IS NOT NULL").fetchone()[0]
has_t20 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t20 IS NOT NULL").fetchone()[0]
has_factors = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE factors_json IS NOT NULL AND factors_json != 'null'").fetchone()[0]

print(f"总快照: {total}")
print(f"已回填T1: {r1} ({r1/max(total,1)*100:.1f}%)")
print(f"完全回填T1+T5+T20: {r3}")
print(f"待回填(realized=0): {pending} ({pending/max(total,1)*100:.1f}%)")
print(f"有fwd_ret_t1: {has_t1}")
print(f"有fwd_ret_t5: {has_t5}")
print(f"有fwd_ret_t20: {has_t20}")
print(f"有factors_json: {has_factors} ({has_factors/max(total,1)*100:.1f}%)")

# Pending by date
print("\n待回填按日期分布 (TOP 10):")
pending_by_date = snap_db.execute(
    "SELECT trade_date, COUNT(*) as cnt FROM signal_snapshots WHERE realized = 0 GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10"
).fetchall()
for r in pending_by_date:
    print(f"  {r['trade_date']}: {r['cnt']}")

# Stale pending
stale = snap_db.execute(
    "SELECT COUNT(*) FROM signal_snapshots WHERE realized = 0 AND trade_date < date('now', '-30 days')"
).fetchone()[0]
print(f"\n过期待回填 (>30天): {stale}")

# Recent dates
print("\n近期 trade_date 分布:")
recent = snap_db.execute(
    "SELECT trade_date, COUNT(*) as cnt, SUM(CASE WHEN realized>=1 THEN 1 ELSE 0 END) as filled FROM signal_snapshots GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10"
).fetchall()
for r in recent:
    pct = r["filled"] / max(r["cnt"], 1) * 100
    print(f"  {r['trade_date']}: {r['cnt']}条, 已回填{r['filled']} ({pct:.0f}%)")

# IC calculation
print("\n" + "=" * 60)
print("2. IC 实际值检查 (30天窗口)")
print("=" * 60)

rows = snap_db.execute("""
    SELECT direction, strength, fwd_ret_t1, trade_date
    FROM signal_snapshots
    WHERE fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
    AND trade_date >= date('now', '-30 days')
""").fetchall()
print(f"30天内有fwd_ret_t1的样本: {len(rows)}")

if len(rows) >= 5:
    longs = [(r["strength"], r["fwd_ret_t1"]) for r in rows if r["direction"] == "LONG"]
    shorts = [(r["strength"], r["fwd_ret_t1"]) for r in rows if r["direction"] == "SHORT"]
    print(f"  LONG: {len(longs)} 样本")
    print(f"  SHORT: {len(shorts)} 样本")
    
    def pearson(pairs):
        if len(pairs) < 3: return None
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        n = len(xs)
        mx, my = sum(xs)/n, sum(ys)/n
        cov = sum((x-mx)*(y-my) for x,y in pairs)
        vx = sum((x-mx)**2 for x in xs)
        vy = sum((y-my)**2 for y in ys)
        if vx == 0 or vy == 0: return None
        return cov / math.sqrt(vx * vy)
    
    if longs:
        ic = pearson(longs)
        print(f"  LONG IC (Pearson): {ic:+.4f}" if ic else "  LONG IC: 样本不足")
    if shorts:
        ic = pearson(shorts)
        print(f"  SHORT IC (Pearson): {ic:+.4f}" if ic else "  SHORT IC: 样本不足")
    
    # Signed IC (LONG=+s, SHORT=-s)
    signed = [(r["strength"] if r["direction"]=="LONG" else -r["strength"], r["fwd_ret_t1"]) 
              for r in rows if r["direction"] in ("LONG","SHORT")]
    ic_signed = pearson(signed)
    print(f"  整体signed IC: {ic_signed:+.4f}" if ic_signed else "  整体signed IC: 样本不足")
else:
    print("  ⚠️ 样本不足，无法计算IC")

# Check for close_t0 missing
print("\n" + "=" * 60)
print("3. 回填质量检查")
print("=" * 60)
missing_t0 = snap_db.execute("SELECT COUNT(*) FROM signal_snapshots WHERE realized >= 1 AND close_t0 IS NULL").fetchone()[0]
print(f"已回填但close_t0缺失: {missing_t0}")

# Check for NULL fwd_ret but close prices exist
inconsistent = snap_db.execute("""
    SELECT COUNT(*) FROM signal_snapshots 
    WHERE close_t1 IS NOT NULL AND fwd_ret_t1 IS NULL
""").fetchone()[0]
print(f"有close_t1但fwd_ret_t1为NULL: {inconsistent}")

# Sample some pending rows
print("\n待回填样本(5条):")
sample_pending = snap_db.execute("""
    SELECT decision_id, symbol, trade_date, direction, strength, realized
    FROM signal_snapshots WHERE realized = 0
    ORDER BY trade_date DESC LIMIT 5
""").fetchall()
for r in sample_pending:
    print(f"  {r['decision_id']} | {r['symbol']} | {r['trade_date']} | {r['direction']} s={r['strength']} r={r['realized']}")

snap_db.close()

# 2. ftseek_us.db - playbook_instructions and voting_predictions
print("\n" + "=" * 60)
print("4. ftseek_us.db IC相关表")
print("=" * 60)

ft_db = sqlite3.connect("data/ftseek_us.db")
ft_db.row_factory = sqlite3.Row

# voting_predictions
vp_total = ft_db.execute("SELECT COUNT(*) FROM voting_predictions").fetchone()[0]
vp_filled = ft_db.execute("SELECT COUNT(*) FROM voting_predictions WHERE outcome_match IS NOT NULL").fetchone()[0]
print(f"voting_predictions: 总{vp_total}, 已回填outcome {vp_filled} ({vp_filled/max(vp_total,1)*100:.1f}%)")

# playbook_instructions with fwd_return
pi_total = ft_db.execute("SELECT COUNT(*) FROM playbook_instructions WHERE direction != 'SKIP'").fetchone()[0]
pi_t1 = ft_db.execute("SELECT COUNT(*) FROM playbook_instructions WHERE fwd_return_t1 IS NOT NULL").fetchone()[0]
pi_t3 = ft_db.execute("SELECT COUNT(*) FROM playbook_instructions WHERE fwd_return_t3 IS NOT NULL").fetchone()[0]
pi_t5 = ft_db.execute("SELECT COUNT(*) FROM playbook_instructions WHERE fwd_return_t5 IS NOT NULL").fetchone()[0]
print(f"playbook_instructions (non-SKIP): 总{pi_total}")
print(f"  有fwd_return_t1: {pi_t1} ({pi_t1/max(pi_total,1)*100:.1f}%)")
print(f"  有fwd_return_t3: {pi_t3} ({pi_t3/max(pi_total,1)*100:.1f}%)")
print(f"  有fwd_return_t5: {pi_t5} ({pi_t5/max(pi_total,1)*100:.1f}%)")

# Check factor_ic_latest.json
print("\n" + "=" * 60)
print("5. factor_ic_latest.json 检查")
print("=" * 60)
if os.path.exists("data/factor_ic_latest.json"):
    with open("data/factor_ic_latest.json") as f:
        factor_ic = json.load(f)
    print(f"因子数: {len(factor_ic)}")
    positive = {k: v for k, v in factor_ic.items() if v.get("ic", 0) > 0.05}
    negative = {k: v for k, v in factor_ic.items() if v.get("ic", 0) < -0.05}
    print(f"  正alpha (IC>0.05): {len(positive)}")
    print(f"  负alpha (IC<-0.05): {len(negative)}")
    # Show a few
    for k, v in sorted(factor_ic.items(), key=lambda x: -abs(x[1].get("ic",0)))[:5]:
        print(f"  {k}: IC={v.get('ic',0):+.4f} n={v.get('n',0)} trade_ic={v.get('trade_ic','N/A')}")
else:
    print("⚠️ data/factor_ic_latest.json 不存在")

ft_db.close()

# 3. Check cron for backfill scripts
print("\n" + "=" * 60)
print("6. Crontab IC回填任务")
print("=" * 60)
os.system("crontab -l 2>/dev/null | grep -i 'backfill\\|ic\\|factor' || echo 'No IC-related cron jobs found'")
