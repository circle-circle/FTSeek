"""45 笔 DA- 前缀却标 deterministic 的成因定性 — 是「陈旧覆盖退化」还是「写入侧缺口」

db.py:41 的退化签名 = decision_id 内嵌日期 ≠ date 列 (历史仅 5 行, 全 9/1)。
本次 45 行全在 9 月 ⇒ 需判别两种成因:
  A) 同签名 (陈旧批覆盖) ⇒ 已知问题, 有守卫
  B) did 日期 == date 列 (无覆盖) ⇒ DA 侧根本没写 model_used = **活的遥测缺口**
     (db.py:323 注释: model_used 用途 = scorecard 按模型分层 ⇒ 缺口会让分层失真)
判据: 逐行核 did 内嵌日期 vs date 列; 并按日聚合看是否集中在某几日 (覆盖事件是批量的)
"""
import re
import sqlite3
from app.storage.db_pool import get_db_path
from collections import defaultdict

c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

DA_RE = re.compile(r"^DA-(\d{4})(\d{2})(\d{2})")

rows = c.execute("""
  SELECT i.id, i.decision_id, i.date, i.symbol, i.direction, i.model_used,
         i.confidence, i.skip_reason, i.created_at, i.raw_instruction_json,
         t.id tid, t.pnl_net, COALESCE(t.mfe_pct,0) mfe, t.entry_time
  FROM playbook_instructions i
  LEFT JOIN trade_records t ON t.decision_id = i.decision_id
       AND t.entry_time>='2026-06-27' AND t.entry_time < '2026-10-02 00:00:00' AND t.exit_time IS NOT NULL AND t.pnl_net IS NOT NULL
  WHERE i.model_used = 'deterministic' AND i.decision_id LIKE 'DA-%'
    AND i.date <= '2026-10-01'
  ORDER BY i.date
""").fetchall()
print(f"DA- 前缀且标 deterministic: n={len(rows)}")

sig_A, sig_B = [], []
for r in rows:
    m = DA_RE.match(r["decision_id"] or "")
    did_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
    col_date = (r["date"] or "")[:10]
    (sig_A if did_date != col_date else sig_B).append((r, did_date, col_date))

print(f"\n=== 成因定性 ===")
print(f"  A) did日期 ≠ date列 (陈旧覆盖退化, db.py:41 同签名): n={len(sig_A)}")
print(f"  B) did日期 == date列 (无覆盖 ⇒ 写入侧缺口):        n={len(sig_B)}")

for lbl, grp in (("A 覆盖退化", sig_A), ("B 写入缺口", sig_B)):
    if not grp:
        continue
    print(f"\n--- {lbl} 逐日聚合 ---")
    by_d = defaultdict(list)
    for r, dd, cd in grp:
        by_d[cd].append(r)
    for d in sorted(by_d)[:12]:
        g = by_d[d]
        syms = ",".join(sorted({x["symbol"].replace("US.", "") for x in g})[:6])
        print(f"  {d}: n={len(g):<3} did日期={sorted({DA_RE.match(x['decision_id']).group(0)[3:] for x in g if DA_RE.match(x['decision_id'] or '')})[:3]} 标的={syms}")

# B 组细看: raw_instruction_json 里有没有 model 线索
print(f"\n=== B 组写入侧缺口取证 (raw_instruction_json 是否含 model 键) ===")
import json
has_model = 0
keys_seen = defaultdict(int)
for r, dd, cd in sig_B[:60]:
    try:
        j = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    for k in j:
        keys_seen[k] += 1
    if any("model" in k.lower() for k in j):
        has_model += 1
print(f"  B 组 n={len(sig_B)}, raw_json 含 model* 键: {has_model}")
print(f"  raw_json 顶层键 top12: {sorted(keys_seen.items(), key=lambda x:-x[1])[:12]}")

# B 组样例 3 行
print(f"\n=== B 组样例 (前 4 行) ===")
for r, dd, cd in sig_B[:4]:
    print(f"  {cd} {r['symbol']:<10} did={r['decision_id'][:26]:<28} dir={r['direction']:<11} "
          f"conf={r['confidence']} created={str(r['created_at'])[:19]}")

# 影响面: B 组成交笔的死单率 (我上一条把它算进 14.0% 的那部分)
traded = [(r, dd, cd) for r, dd, cd in sig_B if r["tid"]]
n = len(traded)
d = sum(1 for r, _, _ in traded if r["mfe"] < 0.3)
net = sum(r["pnl_net"] for r, _, _ in traded)
print(f"\n=== B 组中已成交且结算: n={n} 死率={round(100*d/n,1) if n else None}% 净={net:+.1f} ===")
print(f"  ⇒ 这 {n} 笔是 DA 真实产出, 被错标 deterministic, 污染了 model_used 分层")

# 对照: 同期 DA- 且标真模型名的行
ctl = c.execute("""
  SELECT COUNT(*) n, i.model_used FROM playbook_instructions i
  WHERE i.decision_id LIKE 'DA-%' AND i.date >= '2026-09-01' AND i.date < '2026-10-02 00:00:00' AND i.date <= '2026-10-01'
    AND i.model_used IS NOT NULL AND i.model_used != 'deterministic'
  GROUP BY i.model_used ORDER BY n DESC
""").fetchall()
print(f"\n=== 对照: 9月起 DA- 且标真模型名 ===")
for r in ctl:
    print(f"  {r['model_used']:<28} n={r['n']}")
tot_da = c.execute("SELECT COUNT(*) n FROM playbook_instructions WHERE decision_id LIKE 'DA-%' AND date>='2026-09-01' AND date < '2026-10-02 00:00:00' AND date<='2026-10-01'").fetchone()["n"]
# ⚠️ 占比分子必须是「9月起全部被错标行」= A组(陈旧覆盖) + B组(写入缺口), 只算 B 组会漏 A 组
det_sep = c.execute("""
  SELECT COUNT(*) FROM playbook_instructions
  WHERE decision_id LIKE 'DA-%' AND date>='2026-09-01' AND date < '2026-10-02 00:00:00' AND date<='2026-10-01' AND model_used='deterministic'
""").fetchone()[0]
nB_sep = len([x for x in sig_B if x[0]["date"][:7] >= "2026-09"])
print(f"  9月起 DA- 总数={tot_da} ⇒ deterministic 占比={round(100*det_sep/tot_da,1) if tot_da else None}%")
print(f"  分子拆解: 9月起被错标共 {det_sep} 行 = A组(陈旧覆盖) {det_sep-nB_sep} + B组(写入缺口) {nB_sep}")
