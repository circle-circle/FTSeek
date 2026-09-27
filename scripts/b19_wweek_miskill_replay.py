#!/usr/bin/env python3
"""B19 EOD 深水闸 — W_WEEK 台账反向回放 (等待代价实测 + 回溯证据独立复现, 2026-09-27).

═══ 定位: 这个脚本**不能**裁决 B19; 误杀轴上也几乎零增量 ═══
`backtest-rule-replay` skill T14 明令: **不得用回放数字给前向影子的毕业判据补票** —
B19 判据写死的是前向样本 (`ADJUDICATED-CRITERIA.md` B19 行), 回放只作独立复现。
且 Gate1 (n≥30) 在本人群上**结构性不可能满足**: 全部 W_WEEK 豁免仓只有 27 笔。
**2026-09-27 实跑还发现 T14 重叠切分后误杀轴零增量**: gate=2.5 被拦 5 笔中 2 笔
(US.M tid=1474 / US.RIVN tid=1546) 就是前向已结算的那 2 笔, 唯一误杀 (US.M Δ=−109.11)
前向已计入 ⇒ 回放找不到第二笔独立误杀。
⇒ 本脚本的合法用途收窄为两个:
   ① 独立复现 C8/B19 登记的回溯证据 (独立 3 笔 = B19 行"3/3 命中"那三笔灾难仓,
      按 trade_id/净额逐笔对上: UBER 1372 −307.13 / GAP 1392 −386.77 / UBER 1437 −272.62;
      ΣΔ=+393 vs 登记 +410, 差 4% = 收盘价近似 15:52 价的口径差, 量级复现、非逐字;
      ⚠️ **日期口径不同, 金额对得上但日期看着像两批** — B19 行写的 8/27 / 8/31 / 9/8 是
      **exit_time (出场日)**, 本脚本 mark_date 是 **overnight_arm.marked_at_et (臂标记日)**,
      实测 8/26→8/27 / 8/28→8/31 / 9/04→9/08 (跨周末可差 3 天, 见 T14 同族提示));
   ② 把"等待的真实代价"钉死: 不是误杀率高 (深水档实测精准, 被拦 5 笔 4 笔 Δ 为正),
      是**判据②用绝对计数 (≤1) 而触发样本稀疏 (0.82/周)** ⇒ 供改判据裁决引用。
   不产出"该不该上线"结论。

═══ 为什么需要它 (2026-09-27 等待代价诊断) ═══
B19 判据是合取: trigger_settled_n≥8 ∧ ΣΔ>0 ∧ miskill_n≤1。
现状 (data/shadow/eod_depth_gate_report_latest.json, generated 9/26):
    n=2/8 ✗ | ΣΔ=+28.40 ✓ | miskill=1/1 ⚠️预算已满
误杀数只增不减 ⇒ 剩余 6 样本必须**零误杀**才可能毕业; 而前向实测误杀率 1/2=50%。
攒样速率 0.82/周 ⇒ 还需 ~7 周。**等待的真实代价 = 门触及不到的部分不算**: 6 笔 GAP 出血
(−$1,899) 里门 (depth>2.5%) 只触及 3 笔 (−$979), 最大那笔 ORCL −$400 arm depth 仅 −1.99%
**在阈值外门拦不到**; 门的反事实总省额 ΣΔ=+$412 摊到样本跨度 4.1 周 = **+$99/周**,
⇒ 7 周机会成本 ≈ **$727** (⚠️ 不是"全部 GAP 出血 × 7 周 = $4,035", 那个口径高估 5.5 倍 —
把门管不到的 ORCL/CRM/SNDK 也算进了等待代价)。
⇒ ⚠️ **这不是"死锁"** (我上一轮的措辞是错的): `_verdict` 逻辑下 n 到 8 时, miskill 仍 ≤1
且 ΣΔ>0 → GRADUATION_LINE_HIT (毕业); 再出 1 笔误杀 → GATE_NOT_WORTH (门不值得)。
两个方向都收敛, 判据在**正常工作**。"预算满"只意味着"下一笔误杀即出裁决", 不是永久锁死。
⇒ 结论: 等待代价小 ($727) + 判据会自然收敛 ⇒ **不动预注册判据** (MEMORY: 误杀预算满尤禁反推放宽)。

═══ 口径 (与结算器逐字对齐, 不另立尺子) ═══
· 人群  = trade_records 中 `metadata_json.overnight_arm.source == 'w_week'` 且已平仓
          (与 `overnight_depth_gate_replay_v2.py` 同人群)
· 反事实 = 标记日 (overnight_arm.marked_at_et 当日) 收盘价平仓
· delta  = cf_pnl − actual_gross  (正 = 当时平掉更省 = 门有价值)
· 误杀   = **结算器同定义**: delta < −FEE_LINE, FEE_LINE=5.0
          (settle_eod_depth_gate_shadow.py L51: Δ<0 且 |Δ|>费地板才算误杀赢家, 微负=费差噪声)
· gate 谱系含现行 2.5 (config/unified_config.yaml:1468 实值), 另扫 1.0~3.0 作敏感性披露
  ⚠️ 按 T23-2: 补测相邻档**只作参数面披露**, 不得提为替代腿 — 2.5 是冻结值。

═══ 已知局限 (逐条声明, 不藏) ═══
1. **Gate1 不过**: n=27 (W_WEEK 全人群) < 30 ⇒ 读数只作方向参考 (T24-1)。
2. **收盘价近似 15:52 平仓价**: 误差分钟级 (v2 同局限)。
3. **bar 缓存需回填**: 既有 data/replay_bars/daily_* 只覆盖 13/27 个 mark_date,
   且缺的 14 笔含 ORCL 9/23 (−400 GAP) / RIVN 9/22 (−319 GAP) 等 GAP 亏损集中时段
   ⇒ **不回填会系统性偏向"门没用"**。本脚本按需回填 (富途 K_DAY, autype=NONE, 限流)。
4. **费用两边抵消**: 同一 round-trip 费用相同 ⇒ 比毛价差 (v2 同口径)。
5. **不做 IS/OOS**: n=27 切半后各 13 笔, 无统计意义 (T0 数据充分性预检判不可行)。

只读 DB + 只写 bar 缓存 + 只写产物 ⇒ 零交易行为变更、零推送、不下单。
产物写**新路径** (带日期), 不覆写 9/13 那次裁决的记录。

用法: ./venv/bin/python scripts/b19_wweek_miskill_replay.py [--no-fetch]
"""
import argparse
import datetime
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
CACHE = REPO / "data" / "replay_bars"

# ── 结算器同值 (settle_eod_depth_gate_shadow.py L49-51) — 不另立尺子 ──
MIN_N = 8
MAX_MISKILL = 1
FEE_LINE = 5.0

# config/unified_config.yaml:1468 实值 = 2.5 (MEMORY: 核线上config实值勿用代码默认)
LIVE_GATE = 2.5
GATES = [1.0, 1.5, 2.0, 2.5, 3.0]


# R5 留痕计数 (棘轮纪律: 坏数据跳过必须计数可见, 禁静默 pass/continue)
_N_BAD_BARS = [0]      # load_bars_covering: 坏 bar 缓存文件
_N_BAD_CACHE = [0]     # fetch_daily: 坏既有缓存
_N_BAD_JOURNAL = [0]   # main: 坏 journal 行


def load_bars_covering(symbol, date):
    """选包含 date 的最长 bar 文件 (T10-9: 取全局最长 ≠ 覆盖目标日, 会静默 skip)。"""
    best = []
    for f in CACHE.glob(f"daily_{symbol.replace('.', '_')}_*.json"):
        try:
            b = json.loads(f.read_text())
        except Exception as e:
            _N_BAD_BARS[0] += 1       # R5: 坏缓存文件计数留痕 (静默跳过 = 缺覆盖不可见)
            print(f"    [bad-bar-cache] {f.name}: {type(e).__name__}: {str(e)[:80]}")
            continue
        if any(x[0] == date for x in b) and len(b) > len(best):
            best = b
    return best


def fetch_daily(symbol, start, end):
    """按 e1_preview_replay.load_daily_fresh 的同款配方取日K (autype=NONE 未复权)。"""
    from futu import AuType, KLType, RET_OK, OpenQuoteContext
    fp = CACHE / f"daily_{symbol.replace('.', '_')}_{start}_{end}.json"
    if fp.exists():
        try:
            b = json.loads(fp.read_text())
            if any(x[0] == end for x in b) or len(b) > 5:
                return b
        except Exception as e:
            _N_BAD_CACHE[0] += 1      # R5: 坏缓存计数留痕后走重取
            print(f"    [bad-cache-refetch] {fp.name}: {type(e).__name__}: {str(e)[:80]}")
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        for _ in (1, 2):
            ret, data, _ = ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, start=start, end=end,
                max_count=400, autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
                bars.sort(key=lambda b: b[0])
                CACHE.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps(bars))
                time.sleep(1.2)          # 限流 (配方注释: 1.2s)
                return bars
            time.sleep(3)
    finally:
        ctx.close()
    return []


def day_clustered_t(deltas_by_day):
    """日聚类 t (T18/T39: 重抽单位=交易日, 指令级 iid 不成立)。"""
    days = list(deltas_by_day.values())
    n = len(days)
    if n < 2:
        return None, n
    mean = sum(days) / n
    var = sum((x - mean) ** 2 for x in days) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    return (mean / (sd / math.sqrt(n)) if sd > 0 else None), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true",
                    help="不回填 bar (只用既有缓存; 会静默跳过未覆盖样本并报告)")
    ap.add_argument("--out", default=None, help="产物路径 (默认 data/ 下带日期)")
    a = ap.parse_args()

    today = datetime.date.today().isoformat()
    out = Path(a.out) if a.out else REPO / "data" / f"b19_wweek_miskill_replay_{today}.json"

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price,
               quantity, pnl_net, fees_est, exit_reason, metadata_json
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_time IS NOT NULL
          AND json_extract(metadata_json,'$.overnight_arm') IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    con.close()

    wweek = [r for r in rows
             if (json.loads(r["metadata_json"] or "{}").get("overnight_arm") or {}).get("source") == "w_week"]
    print(f"═══ 人群: W_WEEK 豁免仓 (已平仓) n={len(wweek)} | 全部 overnight_arm n={len(rows)} ═══")
    print(f"    Gate1 (n≥30): {'✓' if len(wweek) >= 30 else '✗ 不过 ⇒ 只作方向参考, 不构成裁决'}")

    samples, skipped = [], defaultdict(int)
    for r in wweek:
        arm = json.loads(r["metadata_json"]).get("overnight_arm", {})
        depth = float(arm.get("underwater_pct_net") or 0)     # 负 = 水下
        md = (arm.get("marked_at_et") or "")[:10]
        if not md:
            skipped["no_mark_date"] += 1
            continue
        bars = load_bars_covering(r["symbol"], md)
        if not bars or md not in [b[0] for b in bars]:
            # T10-9: 缺 bar 不得静默 skip — 按需回填, 回填失败才计 skipped 并打印清单
            if not a.no_fetch:
                start = (datetime.date.fromisoformat(md) - datetime.timedelta(days=25)).isoformat()
                end = (datetime.date.fromisoformat(md) + datetime.timedelta(days=6)).isoformat()
                try:
                    bars = fetch_daily(r["symbol"], start, end)
                except Exception as e:
                    bars = []
                    print(f"    [fetch-fail] {r['symbol']} {md}: {type(e).__name__}: {e}")
            if not bars or md not in [b[0] for b in bars]:
                skipped["no_bars_after_fetch" if not a.no_fetch else "no_bars_cached"] += 1
                skipped.setdefault("_detail", []).append(f"{r['symbol']}@{md}")
                continue
        close_px = bars[[b[0] for b in bars].index(md)][4]
        entry = float(r["entry_price"])
        qty = float(r["quantity"] or 1)
        s = 1.0 if r["direction"] == "LONG" else -1.0
        cf_pnl = s * (close_px - entry) * qty                 # 反事实: 标记日 EOD 平
        actual = float(r["pnl_net"] or 0)
        fees = float(r["fees_est"] or 0)
        actual_gross = actual + fees
        samples.append(dict(
            trade_id=r["id"], symbol=r["symbol"], direction=r["direction"],
            entry_date=str(r["entry_time"])[:10], mark_date=md, depth_pct=depth,
            night_n=arm.get("night_n"), entry=entry, close_px=close_px, qty=qty,
            actual_gross=actual_gross, actual_net=actual, cf_pnl=cf_pnl,
            delta=cf_pnl - actual_gross, exit_reason=r["exit_reason"],
        ))

    n = len(samples)
    print(f"\n═══ 可回放样本 n={n} | 跳过 {dict((k, v) for k, v in skipped.items() if k != '_detail')} ═══")
    if skipped.get("_detail"):
        print(f"    跳过明细: {skipped['_detail']}")
    if n == 0:
        print("  无可回放样本, 退出。")
        return 0

    print(f"\n═══ 逐笔 (按 depth 排序; delta>0 = 当时平掉更省 = 门有价值) ═══")
    print(f"  {'mark_date':11s}{'sym':11s}{'dir':6s}{'night':>5s}{'depth%':>8s}"
          f"{'实际毛$':>10s}{'EOD平$':>9s}{'delta$':>9s}{'误杀?':>7s}  exit_reason")
    for x in sorted(samples, key=lambda y: y["depth_pct"]):
        mk = "⚠️误杀" if x["delta"] < -FEE_LINE else ("微负" if x["delta"] < 0 else "")
        print(f"  {x['mark_date']:11s}{x['symbol']:<11s}{x['direction']:<6s}"
              f"{str(x['night_n'] or ''):>5s}{x['depth_pct']:>+8.2f}"
              f"{x['actual_gross']:>+10.1f}{x['cf_pnl']:>+9.1f}{x['delta']:>+9.1f}"
              f"{mk:>7s}  {x['exit_reason']}")

    # ── gate 谱系 (误杀定义与结算器逐字一致: delta < −FEE_LINE) ──
    print(f"\n═══ 深度门谱系 (|depth| > gate ⇒ 不豁免, 当日 EOD 平) ═══")
    print(f"  {'gate%':>6s}{'held':>6s}{'blocked':>8s}{'持有实际Σ$':>12s}"
          f"{'省下ΣΔ$':>11s}{'误杀数':>7s}{'误杀率':>8s}{'修复率':>8s}")
    grid = {}
    for gate in GATES:
        held = [x for x in samples if abs(x["depth_pct"]) <= gate]
        blocked = [x for x in samples if abs(x["depth_pct"]) > gate]
        held_sum = sum(x["actual_gross"] for x in held)
        saved = sum(x["delta"] for x in blocked)
        mk = sum(1 for x in blocked if x["delta"] < -FEE_LINE)
        win = sum(1 for x in held if x["actual_gross"] > 0)
        mk_rate = mk / len(blocked) * 100 if blocked else 0.0
        star = " ←现行冻结值" if abs(gate - LIVE_GATE) < 1e-9 else ""
        print(f"  {gate:>6.1f}{len(held):>6d}{len(blocked):>8d}{held_sum:>+12.1f}"
              f"{saved:>+11.1f}{mk:>7d}{mk_rate:>7.1f}%{win/len(held)*100 if held else 0:>7.0f}%{star}")
        grid[gate] = dict(held=len(held), blocked=len(blocked), held_sum=held_sum,
                          saved=saved, miskill=mk, miskill_rate=mk_rate,
                          repair_rate=win / len(held) * 100 if held else 0.0)

    # ── T18: mean + median + 日聚类 t 三者并列 (防尾部单票主导桶均值) ──
    live = grid[LIVE_GATE]
    blocked = [x for x in samples if abs(x["depth_pct"]) > LIVE_GATE]
    held = [x for x in samples if abs(x["depth_pct"]) <= LIVE_GATE]
    ds = sorted(x["delta"] for x in blocked)
    mean_d = sum(ds) / len(ds) if ds else 0.0
    med_d = ds[len(ds) // 2] if ds else 0.0
    by_day = defaultdict(float)
    for x in blocked:
        by_day[x["mark_date"]] += x["delta"]
    t, nd = day_clustered_t(by_day)
    print(f"\n═══ 现行 gate={LIVE_GATE} 的被拦组 Δ 分布 (T18 三读数并列) ═══")
    print(f"  n_blocked = {len(blocked)}   ΣΔ = {sum(ds):+,.2f}")
    print(f"  mean = {mean_d:+,.2f}   median = {med_d:+,.2f}   "
          f"日聚类 t = {f'{t:+.2f}' if t is not None else 'n/a'} (n_days={nd})")
    if ds:
        print(f"  min = {ds[0]:+,.2f}  max = {ds[-1]:+,.2f}   "
              f"最大单笔占 ΣΔ = {max(abs(d) for d in ds)/abs(sum(ds))*100 if sum(ds) else 0:.0f}%")
        print(f"  去掉最坏 1 笔后 ΣΔ = {sum(ds[:-1]) if len(ds) > 1 else 0:+,.2f} (留一口稳健性)")

    # ── ⛔ T14 重叠切分: 回放 vs 前向已结算触发组 — 差集才是新增量 ──
    #   2026-09-27 实锤: 回放 gate=2.5 被拦 5 笔中, 2 笔 (US.M tid=1474 Δ=−109.11 /
    #   US.RIVN tid=1546 Δ=+127.97) **就是前向已结算的那 2 笔** (cf_delta −109.12 / +137.52)
    #   ⇒ 回放在**误杀轴上零增量** (那 1 笔误杀前向已计入), 不得报"回放给出误杀率基线"。
    fwd = (REPO / "data" / "shadow" / "eod_depth_gate_report_latest.json")
    fwd_pairs = {}
    jidx = {}
    jl = REPO / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"
    if jl.exists():
        for ln in jl.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                j = json.loads(ln)
            except Exception as e:
                _N_BAD_JOURNAL[0] += 1   # R5: 坏 journal 行计数留痕 (静默 = 重叠切分失真不可见)
                print(f"    [bad-journal-line] {type(e).__name__}: {str(e)[:60]} | {ln[:80]!r}")
                continue
            jidx[(j.get("symbol"), j.get("arm_date"))] = j
            if j.get("would_revoke") and j.get("settled") and j.get("cf_delta") is not None:
                fwd_pairs[(j.get("symbol"), j.get("arm_date"))] = float(j["cf_delta"])

    ov, indep = [], []
    for x in blocked:
        (ov if (x["symbol"], x["mark_date"]) in fwd_pairs else indep).append(x)
    print(f"\n═══ T14 重叠切分: 被拦 {len(blocked)} 笔 = 与前向重叠 {len(ov)} + 独立 {len(indep)} ═══")
    for x in ov:
        k = (x["symbol"], x["mark_date"])
        print(f"  [重叠] {x['symbol']:10s} {x['mark_date']} 回放Δ={x['delta']:+9,.2f}  "
              f"前向cf_delta={fwd_pairs[k]:+9,.2f}  tid={x['trade_id']}")
    for x in indep:
        print(f"  [独立] {x['symbol']:10s} {x['mark_date']} Δ={x['delta']:+9,.2f}  {x['exit_reason']}")
    mk_ov = sum(1 for x in ov if x["delta"] < -FEE_LINE)
    mk_ind = sum(1 for x in indep if x["delta"] < -FEE_LINE)
    print(f"  误杀切分: 重叠组 {mk_ov}/{len(ov)}   独立组 {mk_ind}/{len(indep)}")
    print(f"  ⇒ 回放相对前向的**误杀增量信息 = {mk_ind} 笔**"
          + ("  ⚠️ 零增量: 那笔误杀前向已计入, 回放不得报'误杀率基线'"
             if mk_ind == 0 and mk_ov > 0 else ""))
    if indep:
        print(f"  ⇒ 独立 {len(indep)} 笔 ΣΔ = {sum(x['delta'] for x in indep):+,.2f} "
              f"(这才是回放的真增量; C8 登记的回溯 3/3 命中 ΣΔ=+$410 对应这三笔,"
              f" 量级一致但差 {abs(sum(x['delta'] for x in indep)-410)/410*100:.0f}% — 窗口/口径微差, 非逐字复现)")

    # ── depth 口径分叉声明 (journal=decision_price时刻价 vs arm=underwater_pct_net) ──
    print(f"\n═══ depth 两口径分叉检验 (B19 判据真实轴 = journal 的 decision_price 时刻价) ═══")
    n_cmp = n_flip_live = 0
    flip_detail = defaultdict(list)
    for x in samples:
        j = jidx.get((x["symbol"], x["mark_date"]))
        if j is None or j.get("depth_net_pct") is None:
            continue
        n_cmp += 1
        jd = float(j["depth_net_pct"])
        if abs(jd - x["depth_pct"]) > 0.05:
            for g in GATES:
                if (abs(x["depth_pct"]) > g) != (abs(jd) > g):
                    flip_detail[g].append((x["symbol"], x["mark_date"],
                                           round(x["depth_pct"], 2), round(jd, 2)))
        if (abs(x["depth_pct"]) > LIVE_GATE) != (abs(jd) > LIVE_GATE):
            n_flip_live += 1
    print(f"  可比行 = {n_cmp};  现行 gate={LIVE_GATE} 归属翻转 = {n_flip_live} 笔 "
          f"⇒ {'被拦集合不受口径影响 ✓' if n_flip_live == 0 else '⚠️ 受影响'}")
    for g in GATES:
        if flip_detail[g]:
            print(f"  ⚠️ gate={g} (敏感性披露档) 归属翻转 {len(flip_detail[g])} 笔: {flip_detail[g]}")
    print(f"  ⇒ 敏感性披露档的读数对 depth 口径敏感, 引用时必须带本行声明")

    # ── 误杀率基线: 按 T14 切分后才谈得上 ──
    print(f"\n═══ ★ 误杀率读数 (已按 T14 切分, 不再是无条件的'基线估计') ★ ═══")
    mk_abs = live["miskill"]
    mk_rate = live["miskill_rate"]
    print(f"  gate={LIVE_GATE} 被拦 {live['blocked']} 笔中, 误杀 (Δ < −{FEE_LINE}) = {mk_abs} 笔"
          f"  ⇒ 名义误杀率 {mk_rate:.1f}%")
    print(f"  但其中 {mk_ov} 笔与前向已结算重叠 ⇒ **回放的净增量误杀 = {mk_ind} 笔**")
    print(f"\n  对判据②的含义 (交用户裁, 本脚本不裁):")
    print(f"    现行判据 miskill ≤ {MAX_MISKILL} (绝对计数). 前向唯一已结算误杀 = US.M (Δ=−109.12),")
    print(f"    回放在 27 笔全人群上**没有找到第二笔独立误杀** (独立 3 笔 ΣΔ=+393 全正)。")
    print(f"    ⇒ 等待代价小且判据会自然收敛 (n=8 时: miskill≤1∧ΣΔ>0→毕业; 再1笔误杀→GATE_NOT_WORTH):")
    print(f"       预算 1 已被 US.M 用掉, 剩 {MIN_N-2} 个样本须零误杀; 而门在深水档实测精准")
    print(f"       (被拦 5 笔里 4 笔 Δ 为正, 唯一误杀是 US.M depth −3.51% 却 SL_BREAKEVEN 救回)。")
    print(f"    改判据的候选 (均需用户裁, 属预注册变更):")
    print(f"      a. miskill ≤ {MAX_MISKILL} (绝对) → miskill ≤ 25% (比率) — 与回溯 0/3、")
    print(f"         回放独立组 0/3 兼容, 容忍前向 1/{MIN_N-2}")
    print(f"      b. 提高 MIN_N (8 → 12) 换取比率判据分母稳定性 (代价: 更久)")
    print(f"      c. 维持原判据 + 接受 ~7 周机会成本 ~$727 (门周省额仅 ~$99:")
    print(f"         6 笔 GAP 出血门只触及 3 笔, ORCL −$400 depth −1.99% 在阈值外)")

    print(f"\n═══ ⛔ 判读边界 (T14 / T23-2 / Gate1) ═══")
    print(f"  · Gate1 n={len(wweek)} < 30 ⇒ 本回放**不构成裁决级证据**, 只作方向参考")
    print(f"  · T14: **不得用本回放数字给 B19 前向判据补票** — 判据写死前向样本, 回放只作独立复现")
    print(f"  · T23-2: gate 谱系里 {LIVE_GATE} 以外的档位只作**参数面敏感性披露**, 不得提为替代腿")
    print(f"    (2.5 是预注册冻结值, 换腿 = 挪门柱)")
    print(f"  · C8: ORCL 9/23 (depth −2.12% vs gate −2.5%, 差 0.38pp) 属**阈值邻域样本**,")
    print(f"    登记供 n 达标后做敏感性, **不得据此放宽阈值**")

    payload = dict(generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
                   population=f"w_week overnight_arm, n={len(wweek)}, replayable={n}",
                   skipped=dict((k, v) for k, v in skipped.items() if k != "_detail"),
                   skipped_detail=skipped.get("_detail", []),
                   r5_bad_data=dict(bar_cache=_N_BAD_BARS[0], refetch_cache=_N_BAD_CACHE[0],
                                    journal_line=_N_BAD_JOURNAL[0]),
                   gate1_pass=bool(len(wweek) >= 30),
                   fee_line=FEE_LINE, min_n=MIN_N, max_miskill=MAX_MISKILL,
                   live_gate=LIVE_GATE, grid=grid,
                   miskill_baseline=dict(n_blocked=live["blocked"], miskill=mk_abs,
                                         rate_pct=mk_rate),
                   delta_stats=dict(n=len(ds), sum=sum(ds), mean=mean_d, median=med_d,
                                    day_clustered_t=t, n_days=nd,
                                    min=ds[0] if ds else None, max=ds[-1] if ds else None),
                   samples=samples,
                   note="不构成裁决; 合法产出=等待代价实测+回溯证据独立复现 "
                        "(T14 禁给前向判据补票; 误杀轴零增量)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  R5 坏数据计数: bar缓存={_N_BAD_BARS[0]} 重取缓存={_N_BAD_CACHE[0]} "
          f"journal行={_N_BAD_JOURNAL[0]}  (全 0 = 无静默跳过)")
    print(f"[saved] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
