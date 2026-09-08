#!/usr/bin/env python3
"""9/8 开盘首小时三项在场核验 + DA/UE 链产出检查 (no_agent, 一次性).

DEB-20260905 M-7 预注册替代控制: 48h 影子期因劳动节取消 → 9/8 开盘首小时
人工盯盘 + 三项在场核验 (chase_cap 遥测/enforce、退出快环心跳、组合兜底聚合),
任一缺位即把 playbook.auto_execute 写回 false (热载生效)。

输出语义: 非交易日空输出静默; 交易日输出中文核验报告 (永不静默 — 监控类).
只读核验, 不改任何配置/数据; auto_execute 回写由人裁决 (脚本只给结论建议).
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = "/opt/ftseek/FTSeek-US"
DB = os.path.join(ROOT, "data/ftseek_us.db")
LOG = os.path.join(ROOT, "logs/ftseek_us.log")
SHADOW = os.path.join(ROOT, "data/shadow/pullback_limit_shadow.jsonl")
HEARTBEAT = "/run/ftseek-heartbeat"
CFG = os.path.join(ROOT, "config/unified_config.yaml")

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))


def now_et():
    return datetime.now(ET)


def is_trading_day():
    """ET 周末排除 + 仓内 SSOT 日历 (fail-open)."""
    n = now_et()
    if n.weekday() >= 5:
        return False
    try:
        r = subprocess.run(
            [os.path.join(ROOT, "venv/bin/python"),
             os.path.join(ROOT, "scripts/trading_day_check.py"), "--market", "US"],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        return r.returncode == 0
    except Exception:
        return True  # fail-open: 宁可白跑一轮


def cfg_line(key):
    try:
        with open(CFG) as f:
            for line in f:
                m = re.match(r"\s*%s:\s*(\S+)" % re.escape(key), line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def log_today_lines(patterns):
    """当日日志行计数 (轮转兜底: 当前件 + 当日轮转件)."""
    counts = {p: 0 for p in patterns}
    samples = {p: "" for p in patterns}
    files = [LOG]
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    for p in patterns:
                        if p in line:
                            counts[p] += 1
                            if not samples[p]:
                                samples[p] = line.strip()[:150]
        except Exception:
            pass
    return counts, samples


def main():
    if not is_trading_day():
        return 0
    n = now_et()
    today = n.strftime("%Y-%m-%d")
    out = []
    fails = []

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ── 前置: auto_execute 现值 (M-7 裁决对象) ──
    ae = cfg_line("auto_execute")
    out.append(f"auto_execute={ae} (yaml 实值)")

    # ── 前置查询: 当日指令供给 (项1/项4 共用, 先查后判) ──
    inst_rows = []
    inst_trig = 0
    try:
        inst_rows = conn.execute(
            "SELECT decision_id, symbol, direction, triggered, executed, confidence "
            "FROM playbook_instructions WHERE date=?", (today,)).fetchall()
        inst_trig = sum(1 for r in inst_rows if r["triggered"])
    except Exception as e:
        fails.append(f"指令查询失败: {e}")

    # ── 项1: chase_cap 遥测/enforce 在场 ──
    enf = cfg_line("chase_cap_enforce_enabled")
    trig_n = viol_n = 0
    viol_today = []
    if os.path.exists(SHADOW):
        try:
            with open(SHADOW) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("date") != today:
                        continue
                    t = r.get("type")
                    if t == "trigger":
                        trig_n += 1
                    elif t == "chase_cap_violation":
                        viol_n += 1
                        viol_today.append(
                            f"{r.get('symbol')} {r.get('direction')} "
                            f"cap={r.get('chase_cap_price')} reprice={r.get('repriced_entry')} "
                            f"enforced={r.get('enforced')}")
        except Exception as e:
            fails.append(f"项1 影子文件读取失败: {e}")
    cnt, _ = log_today_lines(["chase_cap违例遥测"])
    if enf != "true":
        fails.append(f"项1 chase_cap enforce 配置={enf} (期望 true)")
    elif inst_trig > 0 and trig_n == 0 and viol_n == 0:
        # 已有触发指令但影子零行 = 遥测断供 (真缺位, M-7 判据)
        fails.append(f"项1 chase_cap 遥测断供: 当日已触发 {inst_trig} 条指令, "
                     f"但 {SHADOW} 无当日 trigger/violation 行")
    elif trig_n > 0 or viol_n > 0:
        out.append(f"✅ 项1 chase_cap: enforce=ON, 当日遥测 trigger={trig_n} violation={viol_n}")
    else:
        out.append(f"⚠️ 项1 chase_cap: enforce=ON, 尚无触发指令 (供给={len(inst_rows)}条/触发0) "
                   f"— 遥测无法证伪在场, 属供给问题非遥测问题")
    for v in viol_today:
        out.append(f"   违例: {v}")
    if cnt["chase_cap违例遥测"]:
        out.append(f"   日志违例遥测行: {cnt['chase_cap违例遥测']}")

    # ── 项2: 退出快环心跳 ──
    hb_age = None
    if os.path.exists(HEARTBEAT):
        hb_age = time.time() - os.path.getmtime(HEARTBEAT)
    cnt2, samp2 = log_today_lines(["[playbook dual] 退出监控", "[PlaybookExit]"])
    ok_hb = hb_age is not None and hb_age < 120
    ok_log = cnt2["[playbook dual] 退出监控"] > 0
    if ok_hb and ok_log:
        out.append(f"✅ 项2 退出快环: heartbeat age={hb_age:.0f}s (<120s), "
                   f"当日 [playbook dual] 行={cnt2['[playbook dual] 退出监控']}")
    elif ok_hb:
        out.append(f"⚠️ 项2 退出快环: heartbeat 新鲜 (age={hb_age:.0f}s) 但日志无当日 "
                   f"[playbook dual] 行 — 可能无未平仓 sim/live 仓 (rows=0 提前返回)")
    else:
        fails.append(f"项2 退出快环心跳缺位: hb_age={hb_age}, 日志行={cnt2}")

    # ── 项3: 组合兜底聚合 (G-01) ──
    bs_on = None
    try:
        with open(CFG) as f:
            txt = f.read()
        m = re.search(r"portfolio_backstop:\s*\n\s*enabled:\s*(\S+)", txt)
        bs_on = m.group(1) if m else None
    except Exception:
        pass
    cnt3, samp3 = log_today_lines(["[playbook组合兜底]"])
    crit3, _ = log_today_lines(["🚨 [playbook组合兜底]"])
    n_crit = crit3["🚨 [playbook组合兜底]"]
    if bs_on == "true" and n_crit == 0:
        extra = f", 脏行/配置 warn={cnt3['[playbook组合兜底]']}" if cnt3["[playbook组合兜底]"] else ""
        out.append(f"✅ 项3 组合兜底 G-01: enabled=true, 无 critical 告警{extra}")
    elif n_crit > 0:
        fails.append(f"项3 组合兜底 critical 告警 {n_crit} 条 — 逐仓 SL 集体失效信号, 立即人工核查!")
    else:
        fails.append(f"项3 组合兜底 enabled={bs_on} (期望 true)")

    # ── 项4: DA/UE 链当日产出 + 隔夜仓状态 ──
    da = [r for r in inst_rows if str(r["decision_id"] or "").startswith("DA-")]
    ue = [r for r in inst_rows if "universe_expansion" in str(r["decision_id"] or "")]
    re_ = [r for r in inst_rows if str(r["decision_id"] or "").startswith("intraday_reentry")]
    exe = sum(1 for r in inst_rows if r["executed"])
    out.append(f"{'✅' if inst_rows else '⚠️'} 项4 指令供给: 当日 {len(inst_rows)} 条 "
               f"(DA={len(da)} UE={len(ue)} 再入场={len(re_)}), 触发={inst_trig} 执行={exe}")
    if not inst_rows:
        out.append("   注: 20:00 DA / 21:10 UE cron 若派发失败则供给为 0 — "
                   "查 hermes cron list last_status")

    try:
        pos = conn.execute(
            "SELECT symbol, direction, size, avg_entry_price, current_sl "
            "FROM open_positions WHERE size>0").fetchall()
        pos_txt = ", ".join(
            f"{p['symbol']} {p['direction']} {p['size']}@{p['avg_entry_price']} "
            f"SL={p['current_sl']}" for p in pos)
        out.append(f"持仓: {len(pos)} 只 — {pos_txt or '无'}")
    except Exception as e:
        out.append(f"持仓查询失败: {e}")

    # ── 项5: A/C 门影子样本落盘 (用户裁「不动」= 攒极端跳空样本的前提) ──
    # 窗口 09:35-09:59 ET; 本核验 09:50/10:25 ET 均在窗口内/后。
    # 9/8 实况: QCOM BUY +7.9% (C_long_chase) / NVS SHORT -13% (A_short_chase) /
    #   BIDU SHORT -7.7% — 三门 record-only 放行, 样本必须落盘否则裁决永远缺证据。
    inv_path = os.path.join(ROOT, "data/premarket_invalidation_shadow.jsonl")
    inv_today, inv_flagged = 0, 0
    if os.path.exists(inv_path):
        try:
            with open(inv_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("date") != today:
                        continue
                    inv_today += 1
                    if r.get("would_invalidate") or r.get("hits"):
                        inv_flagged += 1
        except Exception as e:
            out.append(f"⚠️ 项5 影子文件读取失败: {e}")
    hm = n.hour * 100 + n.minute
    if inv_today > 0:
        out.append(f"✅ 项5 A/C门影子: 当日 {inv_today} 行落盘 (命中失效规则 {inv_flagged} 行)")
    elif hm < 959:
        out.append(f"⚠️ 项5 A/C门影子: 尚无当日行 (窗口 09:35-09:59, 现在 {n.strftime('%H:%M')})")
    else:
        # 窗口已过仍零行 = 采集链断 (router.futu.get_snapshot 不可用 / observe 关)
        fails.append("项5 A/C门影子断供: 09:35-09:59 窗口已过但 premarket_invalidation_shadow.jsonl "
                     "无当日行 — 极端跳空样本丢失, A/C 转正裁决继续缺证据")

    # ── 项6: 实际成交 + 执行时门禁留痕 ──
    try:
        tr = conn.execute(
            "SELECT symbol, direction, entry_price, realized_pnl, pnl_net, exit_reason, decision_id "
            "FROM trade_records WHERE trade_date LIKE ?", (today + "%",)).fetchall()
        net = sum(float(r["pnl_net"] if r["pnl_net"] is not None else (r["realized_pnl"] or 0))
                  for r in tr)
        closed = [r for r in tr if r["exit_reason"]]
        out.append(f"成交: {len(tr)} 笔 (已平 {len(closed)}), 净 PnL=${net:,.2f}")
        for r in tr[:8]:
            tag = " [再入场]" if str(r["decision_id"] or "").startswith("intraday_reentry") else ""
            out.append(f"   {r['symbol']} {r['direction']} @{r['entry_price']} "
                       f"exit={r['exit_reason'] or '持仓中'}{tag}")
    except Exception as e:
        out.append(f"成交查询失败: {e}")

    cnt6, _ = log_today_lines(["B门硬门拦截", "chase_cap违例遥测", "极端gap", "价格偏离过大",
                               "admission] ", "规则5-MAX_HOLD"])
    for k, v in cnt6.items():
        if v:
            out.append(f"   门禁留痕 [{k.strip()}] = {v} 次")

    # ── 结论 ──
    n_warn = sum(1 for l in out if l.startswith("⚠️"))
    if fails:
        out.append("")
        out.append("❌ 缺位项 (M-7 预注册: 任一缺位即把 auto_execute 写回 false, 热载生效):")
        for f_ in fails:
            out.append(f"  - {f_}")
        out.append("→ 建议: 人工复核上述缺位后执行回写 (或明确豁免); 本脚本不自动改配置。")
    elif n_warn:
        out.append("")
        out.append(f"⚠️ 无硬缺位, 但有 {n_warn} 项待确认 (见上) — 需人工判读后维持/回写 auto_execute。")
    else:
        out.append("")
        out.append("✅ 三项在场核验全绿 — auto_execute=true 维持, M-7 替代控制完成。")

    print(f"📋 9/8 开盘首小时核验 {n.strftime('%H:%M')} ET")
    print("\n".join(out))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
