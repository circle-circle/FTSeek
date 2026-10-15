#!/usr/bin/env python3
"""post_ca4487 观测窗首次产出核验 + stale_quote 回退生效核验 (一次性 job 载体).

排程: 2026-10-13 07:05 CST (= 周二, 两个前置都已跑过)
  ① daily_invariant_check cron `5 5 * * 2-6` ⇒ 处置提交 (625291fc8, 10-10 13:32 CST)
     **之后**的首次运行是周二 10-13 05:05 ⇒ 07:05 核验其产物
  ② DA main carrier cron `0 20/21 * * 1-5` ⇒ 周一 10-12 晚跑过一批, 其漏斗是
     stale_quote 回退 (skill 仓 ebc16e4) 的首次生产证据

背景 (为什么必须核验):
  DEB status 行写「post_ca4487 观测窗在跑」, 但处置提交晚于最后一次运行
  (产物 checked_at=2026-10-09T17:05 ET = 10-10 05:05 CST < 提交 10-10 13:32 CST)
  ⇒ 处置后代码从未运行过, 「在跑」当时不成立。已照抄生产 SQL 独立复算确认接线是活的
  (exit-ca4487172a6f n=7 ⇒ single_version, 非 skipped_old_schema), 但要看到**落盘产物**
  才算闭环。这是「代码在盘上 ≠ 生效」的核验。

核验项:
  A. invariant_check_latest.json 里 config_freeze.post_ca4487 块**首次出现**
     且 checked_at > 处置提交时刻 (2026-10-10T05:32Z)
  B. post_ca4487.status == single_version ∧ expected_hash == exit-ca4487172a6f
     (若 unexpected_single_version / hash_drift / no_rows ⇒ FAIL, 观测窗没按预期成形)
  C. oct_cohort 仍为 broken (KR1 已终局 BROKEN, 不应自愈)
     ∧ scripts/daily_invariant_check.py 里 _OCT_COHORT_START 仍是 "2026-09-17"
     ⇒ 若 oct_cohort 变 single_version 或常量被改 = 有人移动了判据面 (违规)
  D. 周一 DA 批漏斗: 单因 stale_quote 被拒应 = 0, selected 应回到历史区间
     (历史 21 批均值 19.6 中位 22 范围[8,26]; WIP 那批是 12 / 单因 stale_quote 33)
     周一 DA 未跑 (无 10-12 批) ⇒ SKIP 而非 FAIL

本载体只用 stdlib; 仓库 import 委托 venv python 子进程 (与生产解释器同源)。
stdout 即投递内容 (no_agent 契约): PASS/FAIL/SKIP 都推 —— 一次性核验结果必须可见。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
VENV_PY = REPO / "venv" / "bin" / "python"
LATEST = REPO / "data" / "invariant_check_latest.json"
FUNNEL = REPO / "data" / "shadow" / "candidate_funnel.jsonl"
CHECKER = REPO / "scripts" / "daily_invariant_check.py"

# 处置提交 625291fc8 的时刻 (10-10 13:32 CST)。只取**日期级**做比较锚:
# checked_at 落盘是 ET (-04:00), 若拿完整时间戳字符串跨时区比较会踩「字符串 date 比较」
# 陷阱 (MEMORY 记录在案)。处置→核验隔 2+ 天, 日期级比较足够且无时区歧义。
DISPOSAL_DATE = "2026-10-10"
EXPECTED_HASH = "exit-ca4487172a6f"
OCT_START_EXPECTED = "2026-09-17"
# D 段检测的 DA 批日期。默认周一 10-12 (DA carrier dow=1-5 的首个处置后批次)。
# 可用 env 覆盖仅用于**反向自测**: 拿已知含 stale_quote 残留的旧批 (如 10-09) 喂进来,
# 验证 D 段能正确抓到 33 笔单因 stale_quote 并判 FAIL —— 否则今天走 SKIP 分支的
# D 段逻辑从未被活体数据验过, 可能对着残留误报 PASS (假绿)。生产不传此 env。
MONDAY_BATCH_DATE = os.environ.get("VERIFY_DA_BATCH_DATE", "2026-10-12")
HIST_SEL_MEAN = 19.6

lines = []
fails = []
skips = []


def emit(s=""):
    lines.append(s)


emit("═══ post_ca4487 首次产出 + stale_quote 回退生效 核验 ═══")
emit("")

# ── A/B: post_ca4487 块 ─────────────────────────────────────────────
emit("【A/B】post_ca4487 观测窗产物")
if not LATEST.exists():
    fails.append("invariant_check_latest.json 不存在")
    emit("  ❌ 产物不存在: " + str(LATEST))
else:
    try:
        j = json.loads(LATEST.read_text())
    except Exception as e:
        fails.append(f"产物不可解析: {type(e).__name__}")
        emit(f"  ❌ 不可解析: {type(e).__name__}: {e}")
        j = {}
    if j:
        checked_at = str(j.get("checked_at") or j.get("generated_at") or "")
        cf = j.get("config_freeze") or {}
        post = cf.get("post_ca4487")
        emit(f"  checked_at = {checked_at}")
        emit(f"  config_freeze 键 = {sorted(cf.keys())}")

        # A: 首次出现 + 时刻晚于处置提交
        if post is None:
            # 三态区分 (Gate 6 纪律): 产物仍是处置前的 = 跑早了, 不是处置无效
            # checked_at 形如 2026-10-09T17:05:03-04:00 ⇒ 取前 10 位做日期级比较
            checked_date = checked_at[:10]
            if checked_date and checked_date <= DISPOSAL_DATE:
                skips.append(f"产物 checked_at={checked_at} (日期 {checked_date}) 仍早于/"
                             f"等于处置提交日 {DISPOSAL_DATE} "
                             "⇒ daily_invariant_check 尚未在处置后运行过, 不可判")
                emit(f"  ⏳ TOO_EARLY: 产物仍是处置前的 (checked_at={checked_at}), "
                     "核验应在 cron `5 5 * * 2-6` 的下一次运行之后")
            else:
                fails.append("产物 checked_at 已晚于处置提交日却仍无 post_ca4487 块 "
                             "⇒ 处置后的代码跑过了但观测窗块没产出 = 死门")
                emit("  ❌ 处置后已运行却无 post_ca4487 块 ⇒ 死门 (块被跳过/接线断)")
        else:
            checked_date = checked_at[:10]
            ok_ts = checked_date > DISPOSAL_DATE
            emit(f"  post_ca4487 存在 ✓   checked_at日期({checked_date}) > 处置提交日"
                 f"({DISPOSAL_DATE})? {'✓' if ok_ts else '✗'}")
            if not ok_ts:
                fails.append(f"checked_at={checked_at} 未晚于处置提交日 ⇒ 产物仍是处置前的旧版")
            # B: status / expected_hash
            st = post.get("status")
            exp = post.get("expected_hash")
            nu = post.get("n_unique")
            nc = post.get("n_closed")
            emit(f"    status={st}  n_unique={nu}  n_closed={nc}/200  expected_hash={exp}")
            emit(f"    versions={json.dumps(post.get('versions'), ensure_ascii=False)}")
            if st != "single_version":
                fails.append(f"post_ca4487.status={st} ≠ single_version ⇒ 观测窗未按预期成形")
            if exp != EXPECTED_HASH:
                fails.append(f"expected_hash={exp} ≠ {EXPECTED_HASH}")
            if not ok_ts or st == "single_version":
                pass

        # C: oct_cohort 仍 broken + 常量未动
        oc = cf.get("oct_cohort") or {}
        emit("")
        emit("【C】KR1 冻结窗 (判据面不得移动)")
        emit(f"  oct_cohort.status = {oc.get('status')}   n_unique={oc.get('n_unique')}   "
             f"n_closed={oc.get('n_closed')}/{oc.get('n_target')}")
        if oc.get("status") not in ("broken", None):
            fails.append(f"oct_cohort.status={oc.get('status')} — KR1 已终局 BROKEN, "
                         "自愈/变更须查是否有人动了判据面")
        elif oc.get("status") == "broken":
            emit("  ✓ 仍 broken (KR1 终局态, 未被静默恢复)")

# C 续: 常量字面值
if CHECKER.exists():
    txt = CHECKER.read_text(errors="replace")
    m = re.search(r'_OCT_COHORT_START\s*=\s*"([^"]+)"', txt)
    got = m.group(1) if m else None
    ok = got == OCT_START_EXPECTED
    emit(f"  _OCT_COHORT_START = {got!r}   期望 {OCT_START_EXPECTED!r}   {'✓' if ok else '✗'}")
    if not ok:
        fails.append(f"_OCT_COHORT_START 被改为 {got!r} ⇒ 判据面被移动 (违规, 须 DEB 落档)")

# ── D: 周一 DA 批漏斗 (stale_quote 回退生效) ─────────────────────────
emit("")
emit("【D】stale_quote 回退生效 (skill 仓 ebc16e4 → 周一 DA 批)")
if not FUNNEL.exists():
    skips.append("candidate_funnel.jsonl 不存在")
    emit("  ⏭️  漏斗产物不存在, 跳过")
else:
    recs = []
    bad = 0
    for ln in FUNNEL.read_text(errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            recs.append(json.loads(ln))
        except Exception:
            bad += 1  # 2026-10-15 R5: except 不裸吞, 坏行计数透出
    if bad:
        emit(f"  ⚠️ 漏斗坏行跳过 {bad} 条")
    flat = []
    for r in recs:
        for c in (r.get("candidates") or []):
            if isinstance(c, dict):
                c["_batch"] = r.get("batch_id")
                c["_src"] = r.get("source")
                flat.append(c)
    da = [c for c in flat if c["_src"] == "DA"]
    batches = {}
    for c in da:
        batches.setdefault(c["_batch"], []).append(c)

    # 找周一批: 用 observed_at 判日
    mon = {}
    for b, g in batches.items():
        obs = [str(x.get("observed_at") or "")[:10] for x in g]
        days = {d for d in obs if d}
        if any(MONDAY_BATCH_DATE in d for d in obs) or MONDAY_BATCH_DATE in days:
            mon[b] = g
    if not mon:
        # 退一步: 取最新一批并报告其日期
        newest = max(batches.items(), key=lambda kv: max(
            str(x.get("observed_at") or "") for x in kv[1])) if batches else None
        if newest:
            obs = sorted({str(x.get("observed_at") or "")[:10] for x in newest[1]})
            skips.append(f"无 {MONDAY_BATCH_DATE} 的 DA 批 (最新批日期={obs}) ⇒ DA 未跑")
            emit(f"  ⏭️  无 {MONDAY_BATCH_DATE} 批; 最新批 observed_at 日期 = {obs}")
        else:
            skips.append("漏斗里无 DA 批")
            emit("  ⏭️  漏斗里无 DA 批")
    else:
        for b, g in mon.items():
            sel = sum(1 for x in g if x.get("selection_status") == "selected")
            sq = sum(1 for x in g if (x.get("reasons") or []) == ["stale_quote"])
            asof_vals = sum(1 for x in g if x.get("source_asof"))
            emit(f"  批 {b}: 候选={len(g)} selected={sel} 单因stale_quote={sq} "
                 f"source_asof有值={asof_vals}")
            if sq != 0:
                fails.append(f"周一批 {b} 仍有 {sq} 笔单因 stale_quote 被拒 ⇒ 回退未生效")
            else:
                emit("    ✓ 单因 stale_quote = 0 (回退生效)")
            if asof_vals:
                fails.append(f"周一批 {b} 有 {asof_vals} 行 source_asof 非空 ⇒ 时间戳特性仍在灌值")
            else:
                emit("    ✓ source_asof 全空 (回到 HEAD 语义)")
            if sel < 8:
                fails.append(f"周一批 selected={sel} 低于历史区间下沿 8 ⇒ 候选池仍被削")
            else:
                emit(f"    ✓ selected={sel} 在历史区间 [8,26] 内 (均值 {HIST_SEL_MEAN})")

emit("")
emit("═══ 结论 ═══")
if fails:
    emit(f"❌ FAIL ({len(fails)})")
    for f in fails:
        emit("  • " + f)
elif skips:
    emit(f"⏳ 未全判 ({len(skips)} 项 TOO_EARLY/SKIP, 0 FAIL)")
    for s in skips:
        emit("  ⏭️  " + s)
    emit("  已判项全过; SKIP/TOO_EARLY 项需在下一次对应 cron 运行后重核。")
else:
    emit("✅ PASS — post_ca4487 观测窗已首次产出且 single_version; "
       "判据面未移动; stale_quote 回退在生产生效")

print("\n".join(lines))
# no_agent 载体契约 (与 verify_rfc135_expiry.py 同惯例): 一律 return 0 ——
# PASS/FAIL/TOO_EARLY 全靠 stdout 表达。非零退出会被载体当**载体级故障**发 error
# alert, 反而吞掉诊断输出 (FAIL 时最需要细节)。真·载体异常才该非零, 由解释器崩给出。
sys.exit(0)

