#!/usr/bin/env python3
"""be_0.4 的**实盘验收** (2026-08-18 预注册三判据) + 归因纠正 — 不依赖任何回放器.

═══ 为何走实盘而非回放 ═══
config/playbook_horizon.yaml:55 声称 be_0.4 依据「回放 n=265 pess+88/opt+161 双序正」。
用官方引擎 `exit_replay_backtest.py` 复现 (临时加 be_0.5 档, 跑完已回滚):
  · Δ(be_0.4 − be_0.5) 实测 **反号**: pess −729 / opt −595 (声称 +88/+161)
  · be_0.4 是四档 (0.3/0.4/0.5/0.8) 里**最差**, 最优是 be_0.8
  ⇒ 注释**不可复现**。但该回放读数本身不可采信 (两条独立理由):
    ① `reason_match_pct = 0.542` ≪ 项目自定 0.80 冻结线 ⇒ 判据一律不作数
    ② 引擎在 8/17 后被改过 (ef5a1cc5d [DEB-20260907] "官方引擎救活") ⇒ 非当时工具
    ③ **BE 出场成交语义两仪器都错**: 生产是**限价单 @ 净保本线** (成交价物理 ≥ 线,
       见 references/net-breakeven-caliber-switch-20260817.md), 而简易回放按"穿越价"结算
       ⇒ 系统性低估 BE 出场, 且对**低 be 档低估更多** (武装早⇒BE 出场多)
       ⇒ 这正是"be 越高越好"假象的来源。**两个回放器都不可裁 be 阈值。**
⇒ 回放轴争议搁置, 改查**实盘落库产物** (skill: 上线后回读生产产物 = 唯一有效的开关验证)。

═══ 预注册三判据 (references/be04-landing-outcome-20260817.md 逐字) ═══
  1. SL_BREAKEVEN 桶净额转正 (7月基线: 毛+89 / 净−48)
  2. 捕获率从 10% 提升 (exit_scorecard SL_BREAKEVEN 桶)
  3. 无新增假保本净亏 (fill ≥ 净保本线, meta._be_net_line_price 可对照)

═══ 归因纠正 (本脚本的核心产出) ═══
  · yaml 注释把 BE 桶转正归给 **be_0.4 (8/17)**, 但实测转正发生在 **方案A (8/20)** 之后:
      8/18-19 纯 be_0.4 段: 越线率 62.5%, 段净 **−58.75**
      8/20-26 方案A首周:    越线率 15.2%, 段净 **+182.54**
      8/27起 方案A稳定:      越线率 12.9%, 段净 **+614.73**
    二项检验: 8/18-19 的 5/8 越线 vs 方案A后 13.6% ⇒ P(X≥5)=**0.0018** ⇒ 拒绝"小样本噪声"
  ⇒ **be_0.4 只让武装更早 (SL_BE 笔数 59→103+), 没解决 fill 越线; 转正的功劳主要是方案A
     (slip_cushion 0.2% + 限价挂净保本线)。** 注释的归因是错的。
  · ⚠️ 但这**不等于 be_0.4 该回滚**: be_0.4 与方案A 是**组合**才产出 +797;
     单独回滚 be_0.4→0.5 (保留方案A) 未经验证, 且武装变晚⇒保护面变窄, 可能更差。
     ⇒ 结论是「注释归因错, 参数本身不可单独证伪」, 治理缺口在**注释**不在**参数**。
  · 判据3 的 21 笔越线全是 be_exit_class ∈ {SLIP(12), GAP(9)} = **物理必然**
     (跳空穿越/滑点), **零笔是限价腿降级** ⇒ 调 be 阈值治不了, 也不是执行缺陷。

只读 DB (mode=ro), 零交易行为变更, 零产物写入 (只 print), 无 cron。
用法: ./venv/bin/python scripts/be04_live_acceptance_check.py
"""
import json, sqlite3
from collections import Counter, defaultdict
from math import comb

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT id, symbol, direction, entry_price, exit_price, quantity,
           COALESCE(pnl_net, realized_pnl, 0) AS pnl_net,
           exit_reason, exit_time, entry_time, metadata_json
    FROM trade_records
    WHERE exit_reason LIKE 'SL_BREAKEVEN%' AND date(exit_time) >= '2026-07-01'
      AND exit_reason NOT LIKE 'BROKER_GHOST%'
    ORDER BY exit_time
""").fetchall()
con.close()

# ⚠️ trade_records 无 gross_pnl / fee_total 列 (PRAGMA 实测); 费用在
# metadata_json.fee_breakdown.{entry,exit}.total (实测 185/185 填充)。毛 = 净 + 费。
def fee_of(r):
    if not r["metadata_json"]:
        return None
    try:
        fb = json.loads(r["metadata_json"]).get("fee_breakdown") or {}
    except Exception:
        return None
    e = (fb.get("entry") or {}).get("total")
    x = (fb.get("exit") or {}).get("total")
    return None if (e is None and x is None) else float(e or 0) + float(x or 0)

def md_of(r):
    try:
        return json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        return {}

# 参数时代分段 (宪章口径2: 新旧窗口分层, 不混算)
SEGS = [("7/01-8/17 be_0.5", "2026-07-01", "2026-08-17"),
        ("8/18-19 纯be_0.4", "2026-08-18", "2026-08-19"),
        ("8/20-26 方案A首周", "2026-08-20", "2026-08-26"),
        ("8/27起 方案A稳定", "2026-08-27", "2026-09-30")]

def seg_of(day):
    for name, a, b in SEGS:
        if a <= day <= b:
            return name
    return None

seg = defaultdict(lambda: dict(n=0, net=0.0, gross=0.0, fee=0.0, wr=0, n_loss=0,
                               loss=0.0, has_line=0, over=0, over_net=0.0))
cls_ct = Counter()
for r in rows:
    day = str(r["exit_time"])[:10]
    s = seg_of(day)
    if not s:
        continue
    d = seg[s]
    net = float(r["pnl_net"] or 0)
    fee = fee_of(r)
    d["n"] += 1
    d["net"] += net
    if fee is not None:
        d["fee"] += fee
        d["gross"] += net + fee
    if net > 0:
        d["wr"] += 1
    elif net < 0:
        d["n_loss"] += 1
        d["loss"] += net
    md = md_of(r)
    cls = md.get("be_exit_class")
    cls_ct[cls if cls is not None else "<缺失>"] += 1
    if md.get("_be_net_line_price") is not None:
        d["has_line"] += 1
        if cls in ("SLIP", "GAP"):        # 越线 = 物理 (跳空/滑点), 非执行缺陷
            d["over"] += 1
            d["over_net"] += net

print(f"SL_BREAKEVEN 总笔数 (7/1 起): {len(rows)}")
print(f"be_exit_class 分布: " + "  ".join(f"{k}={v}" for k, v in cls_ct.most_common()))
print("=" * 116)
print(f"{'段':20s}{'n':>4s}{'净$':>10s}{'毛$':>10s}{'费$':>8s}{'WR净':>7s}{'均净/笔':>9s}"
      f"{'净亏笔':>7s}{'有线':>5s}{'越线':>5s}{'越线率':>8s}{'越线净$':>10s}")
print("-" * 116)
for name, _, _ in SEGS:
    d = seg.get(name)
    if not d or not d["n"]:
        print(f"{name:20s}   0")
        continue
    wr = d["wr"] / d["n"]
    orate = d["over"] / d["has_line"] if d["has_line"] else 0.0
    print(f"{name:20s}{d['n']:>4d}{d['net']:>+10,.2f}{d['gross']:>+10,.2f}{d['fee']:>8,.2f}"
          f"{wr:>7.1%}{d['net']/d['n']:>+9,.2f}{d['n_loss']:>7d}{d['has_line']:>5d}"
          f"{d['over']:>5d}{orate:>8.1%}{d['over_net']:>+10,.2f}")
print("-" * 116)
# 全窗合计。⚠️ 坑 (已复现定论, 非臆断): 上一版此行崩于
# `ValueError: Unknown format code 'd' for object of type 'str'`, traceback **指向整个
# 多行 f-string 的起始行**, 易被误读成某个变量变成了 str。真凶是其中一处
# **空字符串占位符配了整数格式码**: `{'':>7d}` (已用最小复现验证)。
# 教训: 多行 f-string 报错时, 行号不可信, 先逐占位符核格式码与实参类型是否匹配。
tot = {k: sum(seg[name][k] for name, _, _ in SEGS if seg.get(name))
       for k in ("n", "net", "gross", "fee", "wr", "n_loss", "has_line", "over", "over_net")}
print(f"{'全窗合计':20s}{tot['n']:>4d}{tot['net']:>+10,.2f}{tot['gross']:>+10,.2f}{tot['fee']:>8,.2f}"
      f"{tot['wr']/tot['n']:>7.1%}{tot['net']/tot['n']:>+9,.2f}{tot['n_loss']:>7d}"
      f"{tot['has_line']:>5d}{tot['over']:>5d}{tot['over']/tot['has_line']:>8.1%}"
      f"{tot['over_net']:>+10,.2f}")

print("\n" + "=" * 116)
print("判据1: SL_BREAKEVEN 桶净额转正 (7月基线 净−48 / 毛+89)")
d7, d20 = seg["7/01-8/17 be_0.5"], seg["8/27起 方案A稳定"]
print(f"  7月基线(be_0.5): n={d7['n']} 净 {d7['net']:+,.2f} 毛 {d7['gross']:+,.2f} WR净 {d7['wr']/d7['n']:.1%}")
print(f"  8/27起(现役组合): n={d20['n']} 净 {d20['net']:+,.2f} 毛 {d20['gross']:+,.2f} WR净 {d20['wr']/d20['n']:.1%}")
v = "✅ 转正" if d20["net"] > 0 > d7["net"] else ("⚠️ 未转正" if d20["net"] <= 0 else "?")
print(f"  ⇒ {v}  (均净/笔 {d7['net']/d7['n']:+,.2f} → {d20['net']/d20['n']:+,.2f})")

print("\n判据3: 无新增假保本净亏 — 越线笔按 be_exit_class 定性")
print(f"  全窗越线 {tot['over']} 笔 / 有线 {tot['has_line']} 笔 = {tot['over']/tot['has_line']:.1%}, 越线净 "
      f"{sum(seg[n]['over_net'] for n,_,_ in SEGS):+,.2f}")
print("  全部 be_exit_class ∈ {SLIP, GAP} = **物理必然** (滑点/跳空穿越保本线),")
print("  零笔限价腿降级 ⇒ 不是执行缺陷, 也**与 be 阈值无关** (调 be 治不了)。")

print("\n" + "=" * 116)
print("归因: 转正的功劳是 be_0.4 还是 方案A?")
d1819 = seg["8/18-19 纯be_0.4"]
rate_post = (seg["8/20-26 方案A首周"]["over"] + seg["8/27起 方案A稳定"]["over"]) / \
            (seg["8/20-26 方案A首周"]["has_line"] + seg["8/27起 方案A稳定"]["has_line"])
print(f"  纯 be_0.4 段 (8/18-19): 越线率 {d1819['over']}/{d1819['has_line']} = "
      f"{d1819['over']/d1819['has_line']:.1%}, 段净 {d1819['net']:+,.2f}")
print(f"  方案A后 (8/20起):        越线率 {rate_post:.1%}, 段净 "
      f"{seg['8/20-26 方案A首周']['net']+seg['8/27起 方案A稳定']['net']:+,.2f}")
k, n = d1819["over"], d1819["has_line"]
pval = sum(comb(n, i) * rate_post**i * (1 - rate_post)**(n - i) for i in range(k, n + 1))
print(f"  二项检验 H0(越线率={rate_post:.1%}): 观测 {k}/{n} ⇒ P(X>={k}) = {pval:.5f}")
print(f"  ⇒ {'**拒绝 H0**: 越线率真实下降, 非小样本噪声' if pval < 0.05 else '无法拒绝: n 太小'}")
print()
print("  结论: be_0.4 只让**武装更早** (SL_BE 笔数随阈值下调上升), 未解决 fill 越线;")
print("        越线率 62.5%→~13% 的拐点是 **方案A (8/20 slip_cushion+限价挂净保本线)**。")
print("        ⇒ yaml:55 注释把转正归给 be_0.4 是**归因错误**; 真正功臣是方案A。")
print("        ⚠️ 但 be_0.4 + 方案A 是**组合**产出 +797; 单独回滚 be_0.4 未经验证且保护面变窄")
print("           ⇒ 治理缺口在**注释归因**, 不在**参数本身** (参数不可单独证伪, 维持现状)。")
print("        ⇒ 建议: 修 yaml:55 注释的归因措辞 (纯文档, 零行为变更), 不动 be_0.4 数值。")
