#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FTSeek-US 周六周检 (no_agent, 2026-08-23 立, 用户裁"需要"):
一次跑齐 OKR-2026-09 的三个周度检查点, 输出一段紧凑汇报 (stdout 即推送):

  1. 实盘四锁 + 毕业门禁 (KR4: 打标前严格不切实盘)
  2. 参数冻结核查 (KR2: 08-24 起 config_version 唯一值数必须=1)
  3. VWAP 斜率证伪门周检 (DEB-20260823-phase1, 预注册判据, 宪章第9律)

设计: 确定性脚本零推理; 任一检查异常输出告警行 (不静默)。
挂载: Hermes cron 周六 09:05 CST (=ET 周五 21:05, 全周数据已落)。
回滚: 删本脚本 + 对应 cron job。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
REPORT = REPO / "data" / "falsification_gate_review_report.json"
FREEZE_START = "2026-08-24"  # OKR-2026-09 KR2 冻结窗口起点


def check_locks():
    lines = (REPO / ".env.us").read_text(encoding="utf-8").splitlines()
    env = {l.split("=", 1)[0]: l.split("=", 1)[1]
           for l in lines if "=" in l and not l.strip().startswith("#")}
    cfg = (REPO / "config/unified_config.yaml").read_text(encoding="utf-8")
    locks = {
        "AUTO_TRADE_ACCOUNT": env.get("AUTO_TRADE_ACCOUNT", "?"),
        "ALLOW_REAL_TRADING": env.get("ALLOW_REAL_TRADING", "?"),
        "execute_mode": "futu_simulate" if "execute_mode: futu_simulate" in cfg
        else "⚠️ 非simulate",
    }
    # 逐锁记闭合态 (而非只算一个总 ok): 破例需要知道**哪把**锁松着才能判精确覆盖。
    # 三处字面值与 scripts/okr_2026_09_scoring.LOCK_EXPECT 逐字同源
    # (tests/test_okr_2026_09_scoring.py::test_lock_literals_match_weekly_checkpoint 钉)。
    closed = {
        "AUTO_TRADE_ACCOUNT": locks["AUTO_TRADE_ACCOUNT"] == "sim",
        "ALLOW_REAL_TRADING": locks["ALLOW_REAL_TRADING"] == "false",
        "execute_mode": locks["execute_mode"] == "futu_simulate",
    }
    ok = all(closed.values())

    # ── RFC-135 四锁破例 (2026-10-06 用户裁 A) ────────────────────────────
    # ok 保持实测真相, 不因有授权而翻 True (翻 True = 撒谎, 且会掩盖下一次真松动)。
    # 破例只决定"要不要吵醒值班人", 体现在返回的 waiver 段 + suppressed 标志。
    # 判定源复用 app.playbook.live_arm_gate.read_live_probe_waiver = 与 gate 到期
    # 判定同一真源 (两处各写一份日期解析会分叉, 正是上面四锁字面值同源钉所防的病)。
    # fail-close: 读不到判定源 / 文件缺失 / 过期 / 坏 JSON ⇒ suppressed=False, 告警照响。
    waiver = {"active": False, "reason": "reader_unavailable"}
    suppressed = False
    uncovered = [k for k, v in closed.items() if not v]
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from app.playbook.live_arm_gate import read_live_probe_waiver
        w = read_live_probe_waiver(REPO / WAIVER_FILE)
        waiver = {"active": bool(w.get("active")), "reason": w.get("reason"),
                  "expires": w.get("expires"), "days_left": w.get("days_left"),
                  "authorization_id": w.get("authorization_id"),
                  "waiver_id": (w.get("raw") or {}).get("waiver_id"),
                  "locks_waived": w.get("locks_waived") or {}}
        if w.get("active"):
            waived = w.get("locks_waived") or {}
            # 精确覆盖: 松动的锁必须在授权清单里且实测值==授权值, 少一条即不抑制
            uncovered = [k for k in uncovered
                         if not (k in waived and locks.get(k) == waived[k])]
            suppressed = not uncovered
    except Exception as exc:                       # noqa: BLE001
        waiver = {"active": False, "reason": f"reader_error: {type(exc).__name__}: {exc}"}
    return ok, locks, waiver, suppressed, uncovered


WAIVER_FILE = "data/live_probe_lock_waiver.json"


def check_edge_vs_fee():
    """经济边际读数 (2026-09-13 立, STRATEGY-AUDIT-20260913 §十).

    立起理由: 7-8月亏损的真实机制是**双向挤压** — 毛edge 掉一半 (14.47→6.19bps)
    同时费率翻倍 (5.46→10.43bps, 名义变小→固定佣金$3.98占比升高)。9月已修复但
    **余量仅 +3.64bps**, 这是系统生死线, 此前周检无任何读数盯它。

    口径 (纯测量, 只读 DB, 零交易行为变更):
      notional  = entry_price × quantity  (派生量, trade_records 无 notional 列)
      毛edge_bps = sum(realized_pnl)/sum(notional)×1e4
      费率_bps   = sum(fees_est)/sum(notional)×1e4
      净_bps     = 毛 - 费
    分桶按 trade_date (ET 日, RFC-070 公共尺子)。
    公式已验证逐位复现 STRATEGY-AUDIT-20260913 §十 表 (全期/7月/8月/9月)。
    """
    from datetime import date as _date
    try:
        cc = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cc.row_factory = sqlite3.Row
        rows = [dict(r) for r in cc.execute(
            "SELECT trade_date, entry_price, quantity, realized_pnl, fees_est "
            "FROM trade_records WHERE exit_time IS NOT NULL "
            "AND realized_pnl IS NOT NULL AND entry_price IS NOT NULL "
            "AND quantity IS NOT NULL")]
        cc.close()
    except Exception as e:
        return {"error": str(e)[:80]}
    if not rows:
        return {"error": "无平仓样本"}

    def agg(rs):
        notional = sum(r["entry_price"] * r["quantity"] for r in rs)
        if not notional:
            return None
        gross = sum(r["realized_pnl"] or 0 for r in rs)
        fee = sum(r["fees_est"] or 0 for r in rs)
        return {"n": len(rs),
                "gross_bps": round(gross / notional * 1e4, 2),
                "fee_bps": round(fee / notional * 1e4, 2),
                "net_bps": round((gross - fee) / notional * 1e4, 2)}

    def _ym(d):
        return f"{d.year}-{d.month:02d}"

    today = _date.today()
    this_ym = _ym(today)
    # 滚动 3 月 = 本月 + 前两月 (自然月)
    roll3 = set()
    y, m = today.year, today.month
    for _ in range(3):
        roll3.add(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    out = {"this_month": None, "roll3": None, "this_ym": this_ym}
    out["this_month"] = agg([r for r in rows if str(r["trade_date"])[:7] == this_ym])
    out["roll3"] = agg([r for r in rows if str(r["trade_date"])[:7] in roll3])
    return out


def check_fee_gate_shadow():
    """T3 fee-gate forward shadow summary (2026-09-20 three-week plan).

    The weekly checkpoint is the decision-side consumer: it reports the current
    production gate, the safe incremental notional proposal, and whether the
    raw candidate would loosen the existing price floor. It is read-only.

    2026-11-07 修复 (周六周检复查): 原先是**进程内** `from scripts.fee_gate_shadow_report
    import ...`。周检脚本由 Hermes cron 用运行时 python (3.14) 执行, 而该解释器没有
    PyYAML (该报表模块 import yaml) ⇒ 自 10-17 起连续 4 周只输出
    "No module named 'yaml'", 费用门读数在周检面完全断供 (采集/报表 cron 本身正常,
    所以同一条消息里出现「T3 死」+「fee_gate_shadow READY 261/30」的自相矛盾)。
    修法 = 与 check_gate/check_graduation_gate 同模式: 用 REPO venv 子进程跑,
    不给自己进程装第三方依赖 (也不改生产依赖)。
    """
    code = (
        "import json;"
        "from scripts.fee_gate_shadow_report import build_report, render_report;"
        "r = build_report();"
        "print(json.dumps({'status': r['status'], 'n': r['n'], 'min_n': r['min_n'],"
        " 'excluded_rows': r['excluded_rows'],"
        " 'lines': render_report(r).splitlines()}, ensure_ascii=False))"
    )
    try:
        r = subprocess.run([str(REPO / "venv/bin/python"), "-c", code],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(REPO),
                           env={**os.environ, "PYTHONPATH": str(REPO)})
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "").strip().splitlines()[-1][:120]
                               if (r.stderr or "").strip() else f"rc={r.returncode}")
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}


def check_graduation():
    """毕业门禁实测 (打标状态)。异常按 fail-close 报。

    ⚠️ 2026-08-24: 底层 ic_tracker._check_graduation 已 DEPRECATED
    (confidence-based 判据测常量无意义, 8/24 修尺子裁决)。
    graduated 值仅供兼容展示, 不作任何决策依据; 新判据待按日聚类尺子预注册+用户批准。
    """
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    code = ("from app.playbook.pipeline import _check_graduation_gate; "
            "import json; print(json.dumps(_check_graduation_gate()))")
    r = subprocess.run([str(REPO / "venv/bin/python"), "-c", code],
                       capture_output=True, text=True, timeout=90,
                       cwd=str(REPO), env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"graduated": False, "reason": f"查询失败: {r.stderr[-120:]}"}


def check_freeze():
    cc = sqlite3.connect(str(DB), timeout=5)
    rows = cc.execute(
        "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),'(null)') v,"
        " COUNT(*) n FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_time IS NOT NULL AND trade_date>=? GROUP BY v",
        (FREEZE_START,)).fetchall()
    # 2026-11-07 (周六周检复查): 埋点修复后的「零缺失笔数」原先写死 "82", 到 11-07 实际
    # 已 270 笔 ⇒ 每周印一个三个月前的静态数字。改为现算 (口径: 同窗口, 修复起点 9/18)。
    n_fixed = cc.execute(
        "SELECT COUNT(*) FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_time IS NOT NULL AND trade_date>='2026-09-18' "
        "AND json_extract(metadata_json,'$.config_version') IS NOT NULL").fetchone()[0]
    cc.close()
    total = sum(n for _, n in rows)
    return rows, total, n_fixed


def check_gate():
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts/falsification_gate_weekly_review.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    if r.returncode != 0:
        return {"error": f"周检脚本失败: {r.stderr[-150:]}"}
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "报告文件读取失败"}


def check_service():
    r = subprocess.run(["systemctl", "is-active", "ftseek-us"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def check_graduation_gate():
    """锚限价影子毕业主门 (DEB-20260806 §4.3 预注册; 停止规则: 每周五机械判定)."""
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts" / "limit_entry_graduation_gate.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    try:
        rep = json.loads((REPO / "data" / "limit_entry_graduation_report.json")
                         .read_text(encoding="utf-8"))
        return rep
    except Exception:
        return {"error": r.stderr[-150:] or "报告读取失败"}


def check_e2_gate():
    """E2 入场动量确认毕业门 (DEB-20260807 §5.1 预注册)."""
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts" / "e2_momentum_graduation_gate.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    try:
        rep = json.loads((REPO / "data" / "e2_momentum_graduation_report.json")
                         .read_text(encoding="utf-8"))
        return rep
    except Exception:
        return {"error": r.stderr[-150:] or "报告读取失败"}


def _cut_section(text, name):
    """从 summary() 文本里删掉一个 [NAME] 段 (标题行到下一个 '[' 标题行之前)."""
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith(f"[{name}]"):
            skip = True
            continue
        if skip and line.startswith("["):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


def _section_count(text, name):
    """取 summary() 里 '[NAME] (N)' 的 N; 找不到返回 None。"""
    for line in text.splitlines():
        if line.startswith(f"[{name}] ("):
            try:
                return int(line.split("(", 1)[1].split(")", 1)[0])
            except Exception:
                return None
    return None


def check_shadow_registry():
    """影子注册表汇总 (2026-08-23 接入, 原模块零引用): 各影子攒样进度 + 状态变迁.

    2026-11-07 (周六周检复查): 额外返回 ready 明细 + 每项 adjudication_state。
    病灶: get_ready_to_evaluate() 是「样本量曾经达标」的**只进不退**集合, 26 项里
    23 项判据早读过 (read_at 10-31 等)、needs_user=false ⇒ 周检却统一印成
    「样本量达标 … 判据需实测」, 让 88% 的条目看着像待办, 把真正没读过的 3 项埋掉。
    """
    code = ("from app.playbook.shadow_registry import refresh_counts, summary, check_mounting, "
            "check_intent_uniqueness, check_artifact_consumers, get_ready_to_evaluate, adjudication_state; "
            "import json; notifs = refresh_counts(); "
            "ready = [{'id': s.id, 'n': s.current_n, 'min_n': s.min_n, "
            "'criteria': s.graduation_criteria, 'state': adjudication_state(s)} "
            "for s in get_ready_to_evaluate()]; "
            "print(json.dumps({'notifs': notifs, 'ready': ready, 'summary': summary(), "
            "'mounts': check_mounting(), "
            "'consumers': check_artifact_consumers(), "
            "'intents': check_intent_uniqueness()}, ensure_ascii=False))")
    r = subprocess.run([str(REPO / "venv/bin/python"), "-c", code],
                       capture_output=True, text=True, timeout=90,
                       cwd=str(REPO))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def main():
    from datetime import date
    out = [f"【FTSeek-US 周六周检 {date.today()}】"]

    # 0. 服务
    svc = check_service()
    out.append(f"服务: {'✓' if svc == 'active' else '⚠️ ' + svc}")

    # 1. 四锁 + 打标
    ok, locks, waiver, suppressed, uncovered = check_locks()
    grad = check_graduation()
    # 三态标记: ✓ 全闭且未打标 | 🔕 四锁松动但被有效预注册破例覆盖 (RFC-135) | ⚠️ 其余
    # 破例期不显示 ✓ — 锁确实松着, 显示 ✓ 会让值班人误判为安全侧;
    # 也不显示 ⚠️ — 那是已裁决的已知例外, 每周重复报警会让告警失去信号价值 (狼来了)。
    # 判定顺序要紧: 🔕 只在"四锁松动"这一种情形下成立。若写成 elif suppressed 而不带
    # `not ok`, 则 ok=True ∧ graduated=True 时 suppressed 也为 True (uncovered 为空)
    # ⇒ 已打标 (用户明令硬拦) 会被误渲染成 🔕 静默。graduated=True 必须走 ⚠️。
    if ok and not grad.get("graduated"):
        mark = "✓"
    elif (not ok) and suppressed:
        mark = "🔕"
    else:
        mark = "⚠️"
    out.append(f"{mark} 四锁: {locks['AUTO_TRADE_ACCOUNT']}/"
               f"{locks['ALLOW_REAL_TRADING']}/{locks['execute_mode']} | "
               f"毕业门禁 graduated={grad.get('graduated')} "
               f"({'deprecated 兼容值, 非拦截依据 (实际拦截=四锁)' if not grad.get('graduated') else '⚠️ 已打标! 需用户签字确认'})")
    if not ok:
        if suppressed:
            out.append(f"  🔕 破例生效 (RFC-135): {waiver.get('waiver_id')} "
                       f"授权={waiver.get('authorization_id')} "
                       f"覆盖={sorted(waiver.get('locks_waived') or {})} "
                       f"到期={waiver.get('expires')} (剩 {waiver.get('days_left')} 天) "
                       f"— 到期后告警自动复响; 撤销=删 data/live_probe_lock_waiver.json")
        else:
            out.append(f"  ⚠️ 四锁松动且无有效破例覆盖: 松动={uncovered} "
                       f"| 破例状态={waiver.get('reason')} "
                       f"| 到期={waiver.get('expires')} — 立即呈裁, 不得继续跑任何结算")

    # 2. 参数冻结
    rows, total, n_fixed = check_freeze()
    if total == 0:
        out.append(f"参数冻结: 窗口({FREEZE_START}起)尚无平仓样本")
    elif len(rows) == 1:
        out.append(f"✓ 参数冻结: config_version 唯一 ({rows[0][0][:16]}…, n={total})")
    else:
        # KR2 例外通道: config_freeze_baseline.json exit_versions 豁免清单
        # (已裁决版本分裂不重复告警; 2026-09-01 体检定性 8/29 early_tight_sl 毕业案后加)
        exempt = {}
        _exempt_err = None
        try:
            _bl = json.loads((REPO / "data" / "config_freeze_baseline.json")
                             .read_text(encoding="utf-8"))
            exempt = _bl.get("exit_versions", {})
        except Exception as e:
            # R5 留痕: 豁免清单不可读必须显性 (否则退回全告警, 不静默)
            _exempt_err = str(e)[:80]
        unknown = [(v, n) for v, n in rows if v not in exempt]
        # A2 (2026-10-06 推送治理批): 分离「缺字段」与「真版本分裂」两类 —
        #   实测病灶: 本查询口径 (source_type='PLAYBOOK_SIM' ∧ trade_date≥FREEZE_START) 下
        #   有 8 笔平仓 metadata_json 缺 config_version (写方埋点缺口, 退出日 9/16–9/17),
        #   SQL 的 COALESCE 把它们折成字面量 '(null)' ⇒ 与真 exit-* 哈希混在同一维计数,
        #   于是每周渲染成「⚠️ 参数冻结已破 (KR2 破, 需复盘)」永久重放。
        #   写方已修 (同口径 9/18 起 82 笔平仓零缺失) ⇒ 这是**一次性历史缺口**, 不是参数漂移。
        # 为什么修渲染层而不进豁免清单: 把 '(null)' 登记进 exit_versions 会让**未来写方
        #   埋点再次回归时静默通过** (豁免键 = 从此不再告警, 见 test_kr2_freeze_exemption
        #   的红绿纪律)。缺字段必须是自己的告警类别, 语义与版本分裂不同。
        missing = [(v, n) for v, n in unknown if v == "(null)"]
        split = [(v, n) for v, n in unknown if v != "(null)"]
        if not split and not missing:
            desc = " / ".join(f"{v[:16]}…:{n}" for v, n in rows)
            out.append(f"✓ 参数冻结: {len(rows)} 个版本均在豁免清单 (KR2 例外通道, "
                       f"见 config_freeze_baseline.json) — {desc}")
        if split:
            desc = " / ".join(f"{v[:16]}…:{n}" for v, n in split)
            out.append(f"⚠️ 参数冻结已破: {len(split)} 个未豁免版本 — {desc} (KR2 破, 需复盘)")
        if missing:
            n = sum(c for _, c in missing)
            out.append(f"ℹ️ 退出哈希缺字段: {n} 笔 metadata_json 无 config_version "
                       f"(写方埋点缺口, 非参数分裂) — 实测 9/16–9/17 一次性, 写方已修 "
                       f"(9/18 起 {n_fixed} 笔零缺失); 不计入 KR2 版本分裂判分")
        if _exempt_err:
            out.append(f"⚠️ 豁免清单读取失败 (按未豁免告警口径处理): {_exempt_err}")

    # 2b. 经济边际读数 (2026-09-13 立): 毛edge vs 费率 — 系统生死线
    ef = check_edge_vs_fee()
    if ef.get("error"):
        out.append(f"⚠️ 经济边际读数: {ef['error']}")
    else:
        def _fmt(tag, b):
            if not b:
                return f"{tag}: 无平仓样本"
            return (f"{tag}: n={b['n']} 毛edge {b['gross_bps']:+.2f}bps | "
                    f"费率 {b['fee_bps']:.2f}bps | 净 {b['net_bps']:+.2f}bps")
        out.append(_fmt(f"经济边际(本月{ef['this_ym']})", ef.get("this_month")))
        out.append("  " + _fmt("滚动3月", ef.get("roll3")))
        # 2026-11-07 (周六周检复查): 结论行改造三处 —
        #   ① n<30 不下边际结论 (10-03 曾以 n=11 印出「余量 +64.81bps」);
        #   ② 结论同屏带滚动3月 (本月是 5 个交易日的部分月, 单看它会在三月窗为负时给假安心);
        #   ③ 删掉写死的「9月修复后 +3.64bps」陈旧锚点; 补「月内读数随迟结算漂移」披露
        #      (实测 10 月: 10-31 印 +3.61bps/n=154 → 月末真值 −4.69bps/n=156, 差的两笔
        #       trade_date=10-30 但 11-03/11-04 才平仓的 MRK/LMT 共 −$694)。
        _tm = ef.get("this_month")
        _r3 = ef.get("roll3")
        _r3_txt = ""
        if _r3:
            _r3_txt = f" | 滚动3月 {_r3['net_bps']:+.2f}bps"
            if _r3["net_bps"] < 0:
                _r3_txt += " ⚠️ 三月窗仍为负"
        if _tm and _tm["n"] < 30:
            out.append(f"  ⚖️ 本月样本不足 (n={_tm['n']}<30) — 不作边际结论, 以滚动3月为准{_r3_txt}")
        elif _tm and _tm["net_bps"] < 0:
            out.append(f"  ⚠️ 告警: 本月净edge {_tm['net_bps']:+.2f}bps < 0 — 毛edge 未覆盖费用线"
                       f"{_r3_txt}")
        elif _tm:
            out.append(f"  余量 {_tm['net_bps']:+.2f}bps (本月, n={_tm['n']}; 净edge<0 即系统亏损)"
                       f"{_r3_txt} — 月内读数随迟结算漂移, 月末后再引用")

    # 2c. T3 费用门影子 (2026-09-20 三周计划): 净利诊断与现行生产门对照
    fee_gate = check_fee_gate_shadow()
    if fee_gate.get("error"):
        out.append(f"⚠️ T3费用门影子: {fee_gate['error']}")
    else:
        out.append(f"✓ T3费用门影子: n={fee_gate['n']}/{fee_gate['min_n']} "
                    f"status={fee_gate['status']} excluded={fee_gate['excluded_rows']}")
        out.extend(f"  {line}" for line in fee_gate["lines"][1:3])

    # 3. 证伪门
    g = check_gate()
    if g.get("error"):
        out.append(f"⚠️ 证伪门周检: {g['error']}")
    else:
        _extra = ""
        if g.get("n_da_would_block_pre_enforce"):
            _extra = f" + DA对比组(9/12前只记不拦) {g['n_da_would_block_pre_enforce']}"
        out.append(
            f"证伪门(VWAP斜率): 遥测拦 {g.get('n_telemetry_blocks', g.get('n_shadow_rows', 0))} = "
            f"真被拦 {g.get('n_true_blocked', 0)} + 延迟成交 "
            f"{g.get('n_delayed_executed', 0)}{_extra}")
        if g.get("n_with_counterfactual"):
            out.append(
                f"  被拦组净收益 pooled={g.get('blocked_pooled_mean_net_ret')} | "
                f"日聚类差值={g.get('blocked_daily_clustered_diff_mean')} "
                f"t={g.get('blocked_daily_clustered_t')} MDE={g.get('mde_daily_clustered')}")
        out.append(f"  判定: {g.get('verdict')}")

    # 4. 锚限价影子毕业主门 (DEB-20260806 §4.3, 每周五机械判定; 未过=继续攒样, 不重置)
    lg = check_graduation_gate()
    if lg.get("error"):
        out.append(f"⚠️ 锚限价毕业门: {lg['error']}")
    else:
        sf = lg.get("sign_flip_permutation", {})
        dc = lg.get("double_condition", {})
        sg = lg.get("sign_common_gate", {})
        out.append(
            f"锚限价影子(主门5m): n={lg.get('n_paired_passive')} "
            f"δ̄=${lg.get('delta_mean')} 置换p={sf.get('one_sided_p')} | "
            f"双条件[δ̄≥$1:{dc.get('mean_ge_1')}, CI下界>0:{dc.get('ci_lo_gt_0')}] | "
            f"符号共门p={sg.get('one_sided_p')}")
        out.append(f"  判定: {lg.get('verdict')} (停止规则: 只降级不重置)")

    # 4b. E2 入场动量确认门 (DEB-20260807 §5.1) — 2026-09-13 用户裁「无效就停止，退役」
    # 2026-11-07 (周六周检复查): 原实现每周仍印 3 行冻结统计 (拦截/放行净额 + 日聚类 t/MDE),
    #   而该门恒判 RETIRED_NO_PROMOTION、样本冻结在退役日, 数字永不再变 ⇒ 纯噪音。
    #   改成一行状态; 退役证据与判据原文留在 data/e2_momentum_graduation_report.json。
    #   ⚠️ 该报告 verdict 里的「统计不可分辨(|diff|<MDE)」一条已登记待更正 (MDE 按配对差
    #   多乘了 √2 ⇒ |diff|=29.91 实为 > 真 MDE 28.91); 退役结论不依赖单条支柱,
    #   但修正判据公式属判据级变更, 须单独呈裁后再动脚本, 本处不改脚本。
    e2 = check_e2_gate()
    if e2.get("error"):
        out.append(f"⚠️ E2动量毕业门: {e2['error']}")
    else:
        out.append("E2动量确认: 🪦 已退役 (2026-09-13 用户裁) — 读数冻结, 不再周报; "
                   "证据见 data/e2_momentum_graduation_report.json")

    # 5. 影子注册表 (分流对比组 + 全部影子攒样进度)
    reg = check_shadow_registry()
    if reg is None:
        out.append("⚠️ 影子注册表读取失败")
    else:
        # 挂载核对哨兵 (2026-09-01): 悬空观察/产出断流告警优先于进度汇总
        for m in reg.get("mounts", []):
            out.append(m)
        # 反向挂载哨兵 (2026-09-26): 终态产物仍有 cron 活跃消费者 ⇒ 摘采集 cron 会断生产料
        for m in reg.get("consumers", []):
            out.append(m)
        # 意图唯一性哨兵 (2026-09-11, DEB-20260911 P0-3①): 同假设双影子未互指 = 双转正风险
        for m in reg.get("intents", []):
            out.append(m)
        # B4 (2026-10-06 推送治理批): READY 横幅从「逐条整段」压成「一行一项」。
        #   病灶: refresh_counts() 是内存态不落盘 (shadow_board.py:639 同注), 每次调用
        #   都重新「发现」同一批 READY ⇒ 每周把 N 条模板整段重推。实测 10-03 那次投递:
        #   29 条 🔔 横幅 = 17.7KB, 占正文 31.5KB 的 **58%** (单条均值 624B)。
        #   压缩量用同代码 A/B 变异对照实测 (非跨版本相减 — 会混入 A1 出数增量):
        #   旧渲染 34475B → 本渲染 26330B = 省 8145B (24%); 横幅块 18.1KB → 10.0KB。
        #   口径: 全文按 len(encode()) 字节数 (中文 3B/字, 与字符数差 1.51 倍, 勿混算)。
        #   为什么不整段删: 横幅里的 graduation_criteria 是该影子的预注册判据, 而下方
        #   summary() 只渲染 notes[:60] ⇒ 直接删会丢判据。改逐条抽 id/n/min_n/判据成单行。
        #   为什么不指向「待裁决面」了事: 实测 29 条里 ue_confirm_gate **不在**待裁面
        #   (已判 KEEP_SAMPLING、10/31 重看, 注册表 read_at 已记) ⇒ 压缩必须自持信息,
        #   不依赖另一载体的覆盖面 (跨载体去重会静默丢项)。
        #   同一批影子的白话结论 + eval 指引在周六 09:14 待裁决通知里质量更高, 本段只留
        #   判据备查, 避免两处各写一遍长文。
        _ready = reg.get("ready") or []
        # 2026-11-07 (周六周检复查): 只把「样本量达标 ∧ 判据从未读/待你处置」当待办。
        #   原实现把 get_ready_to_evaluate() 全量 26 项印成「判据需实测」, 实测其中 23 项
        #   read_at 已有值、needs_user=false (10-31 已读毕, 自动续观), 且与 09:15 待裁决
        #   通知逐项重复 (原文 25.6% 正文 = 100% 重复内容)。
        _unread = [r for r in _ready
                   if not r["state"].get("adjudicated") or r["state"].get("needs_user")]
        _read = [r for r in _ready if r not in _unread]
        if _unread:
            out.append(f"🔔 待裁决 {len(_unread)} 项 (样本量达标 ∧ 判据从未读过/待你处置) "
                       f"— 白话结论+eval 指引见周六待裁决通知")
            for r in _unread:
                out.append(f"  · {r['id']} n={r['n']}/{r['min_n']}"
                           + (f" | {str(r['criteria']).strip()}" if r.get("criteria") else ""))
        if _read:
            _line = (f"  (另 {len(_read)} 项样本量达标但判据已读毕 → 自动续观, 不构成待办;"
                     f" 明细见 09:15 待裁决通知)")
            _overdue = [r["id"] for r in _read if r["state"].get("overdue")]
            if _overdue:
                _line += f"; ⏰ 已到重看日 {len(_overdue)} 项: {', '.join(_overdue)}"
            out.append(_line)
        # 2026-11-07 (周六周检复查): summary() 的 [READY_TO_EVALUATE] 段与上面「待裁决 /
        #   已读毕」两块逐项重复 (26-27 行), 且已读毕项的明细以 09:15 待裁决通知为准
        #   ⇒ 周检里剪掉该段, 只留 OBSERVING 与终态账本。summary() 本体不动 (其他载体照用)。
        #   [KILLED]/[SUPERSEDED] 是**永不变化的死账本** (46+4 行, 实测占正文 47.4%),
        #   周检重印没有决策价值 ⇒ 压成一行计数 (明细仍由 scripts/shadow_board.py 与
        #   注册表本体提供; 09:15 载体另有「⑤ 已否决轴」清单)。合计正文 30.9KB → ~9KB。
        _sum = reg.get("summary", "")
        _nk = _section_count(_sum, "KILLED")
        _ns = _section_count(_sum, "SUPERSEDED")
        for _sec in ("READY_TO_EVALUATE", "KILLED", "SUPERSEDED"):
            _sum = _cut_section(_sum, _sec)
        out.append(_sum)
        _term = " / ".join(x for x in (
            f"KILLED {_nk}" if _nk is not None else "",
            f"SUPERSEDED {_ns}" if _ns is not None else "") if x)
        if _term:
            out.append(f"[终态账本] {_term} — 死账本不逐条重印; 明细见 scripts/shadow_board.py "
                       f"或 app/playbook/shadow_registry.py (09:15 载体另列『已否决轴』)")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
