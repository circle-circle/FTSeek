#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[DEB-20260901 选项1] 首批影子判读报告 (一次性, 2026-09-02 06:05 CST).

读取 9/1 首个结构化字段生产夜的影子产物, 出判读报告 + 飞书推送, 然后自删本
crontab 行 (防一次性行年度复燃, 同 verify_da_ue_batch_20260831 模式)。

数据源 (全部只读):
  - playbook_instructions (archetype 队列健康: n/分布/解析)
  - data/shadow/pullback_limit_shadow.jsonl (触发行 + chase_cap 违例)
  - data/shadow/pullback_limit_replay_latest.json + replay.jsonl (CF 重放, 累计配对)
  - data/shadow/gap_alignment_shadow_latest.json (缺口对齐影子)

判据预注册 (ADR-035): n≥20 配对 / 期望改善≥30% / 成交率≥50% / mfe=0 占比降。
首批判读定性=管道健康 + 方向性证据, 样本不足不出裁决。

用法: PYTHONPATH=. ./venv/bin/python scripts/deb20260901_shadow_first_report.py
      [--date YYYY-MM-DD] [--no-push]
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
SHADOW_DIR = REPO / "data" / "shadow"
REPLAY_LATEST = SHADOW_DIR / "pullback_limit_replay_latest.json"
REPLAY_JSONL = SHADOW_DIR / "pullback_limit_replay.jsonl"
PB_SHADOW = SHADOW_DIR / "pullback_limit_shadow.jsonl"
GAP_LATEST = SHADOW_DIR / "gap_alignment_shadow_latest.json"
CRON_MARKER = "DEB20260901-ONE-SHOT"


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def cohort_health(date):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, direction, raw_instruction_json, triggered, executed, skip_reason "
        "FROM playbook_instructions WHERE date = ? AND direction != 'SKIP'",
        (date,)).fetchall()
    con.close()
    archs, parse_fail, total = Counter(), 0, len(rows)
    triggered = executed = 0
    for r in rows:
        try:
            raw = json.loads(r["raw_instruction_json"] or "{}")
        except Exception:
            parse_fail += 1
            continue
        a = raw.get("entry_archetype")
        if a:
            archs[str(a).upper()] += 1
            if r["triggered"]:
                triggered += 1
            if r["executed"]:
                executed += 1
    return {"total": total, "archs": dict(archs), "parse_fail": parse_fail,
            "triggered": triggered, "executed": executed}


def replay_summary():
    if not REPLAY_LATEST.exists():
        return None
    try:
        return json.loads(REPLAY_LATEST.read_text())
    except Exception:
        return None


def cumulative_pairs():
    """累计配对样本: 同指令既有 CF 结果又有实际执行结果."""
    if not REPLAY_JSONL.exists():
        return 0, []
    rows = [_loads(l) for l in REPLAY_JSONL.read_text().splitlines()]
    rows = [r for r in rows if r]
    pairs = [r for r in rows
             if r.get("status") in ("entered", "expired_unfilled")
             and r.get("actual_executed")]
    return len(pairs), rows


def shadow_rows(date):
    if not PB_SHADOW.exists():
        return [], []
    trig, viol = [], []
    for line in PB_SHADOW.read_text().splitlines():
        r = _loads(line)
        if not r or r.get("date") != date:
            continue
        (viol if r.get("type") == "chase_cap_violation" else trig).append(r)
    return trig, viol


def gap_latest():
    if not GAP_LATEST.exists():
        return None
    try:
        return json.loads(GAP_LATEST.read_text())
    except Exception:
        return None


def build_report(date):
    ch = cohort_health(date)
    rep = replay_summary()
    n_pairs, all_rows = cumulative_pairs()
    trig, viol = shadow_rows(date)
    gap = gap_latest()

    lines = []
    lines.append(f"# DEB-20260901 影子首批判读报告 ({date})")
    lines.append("")
    lines.append(f"生成: {datetime.now().isoformat(timespec='seconds')} · 定性: "
                 "管道健康检查 + 方向性证据 (样本不足不出裁决)")
    lines.append("")

    # 1) 队列健康
    lines.append("## 一、DA bridge 结构化字段健康")
    arch_str = ", ".join(f"{k}×{v}" for k, v in sorted(ch["archs"].items())) or "0 (字段未到达!)"
    lines.append(f"- 当日非SKIP指令 {ch['total']} 条; 带 entry_archetype "
                 f"{sum(ch['archs'].values())} 条 ({arch_str}); JSON 解析失败 {ch['parse_fail']}")
    lines.append(f"- archetype 指令: 触发 {ch['triggered']} / 执行 {ch['executed']}")
    if sum(ch["archs"].values()) == 0:
        lines.append("- ⚠️ **零结构化字段**: DA bridge 未带字段到达 — 查 skill 仓 pipeline "
                     "与 bridge 文件 (首个生产夜断流 = 管道故障, 非策略信号)")
    lines.append("")

    # 2) 影子触发
    lines.append("## 二、触发影子 (pullback_limit_shadow)")
    if not PB_SHADOW.exists():
        lines.append("- ⚠️ 影子文件不存在 → 当日未采集 (触发行/违例计数无效, "
                     "chase_cap 维度自次日采集日起算 — 非'零违例')")
    else:
        lines.append(f"- 触发行 {len(trig)}; chase_cap 违例 {len(viol)}")
        for t in trig[:12]:
            lines.append(f"  - {t.get('symbol')} {t.get('entry_archetype')}: "
                         f"确认位 {(t.get('confirmation') or {}).get('price_level')} "
                         f"触发价 {t.get('trigger_price')} 入场价 {t.get('entry_price')}")
    lines.append("")

    # 3) CF 重放
    lines.append("## 三、确认位反事实重放")
    if rep and rep.get("summary", {}).get("date") == date:
        sm = rep["summary"]
        lines.append(f"- 池 {sm['n_instructions_with_archetype']}: CF入场 {sm['n_cf_entered']} / "
                     f"TTL未触及 {sm['n_expired_unfilled']} / 无bar {sm['n_no_bars']}")
        lines.append(f"- CF 净 {sm['cf_net_sum']} vs 实际净 {sm['actual_net_sum']} "
                     f"(实际执行 {sm['n_actually_executed']} 笔)")
        lines.append(f"- 口径: {sm['note']}")
        for x in rep.get("rows", [])[:12]:
            st = x.get("status")
            if st == "entered":
                lines.append(f"  - {x['symbol']} {x.get('entry_archetype')}: CF入场 "
                             f"{x.get('cf_entry')} 净 {x.get('cf_pnl')} | 实际净 {x.get('actual_net')}")
            elif st == "expired_unfilled":
                lines.append(f"  - {x['symbol']} {x.get('entry_archetype')}: 确认位 "
                             f"{x.get('confirm_level')} TTL未触及 → 不进场 | 实际净 {x.get('actual_net')}")
            else:
                lines.append(f"  - {x['symbol']}: {st}")
    else:
        lines.append("- ⚠️ 重放无当日输出 (查 05:47 cron 日志 logs/cf_pullback_limit_replay.log)")
    lines.append("")

    # 4) 累计判据进度
    lines.append("## 四、预注册判据进度 (累计)")
    lines.append(f"- 配对样本 n={n_pairs} / 目标 ≥20")
    if n_pairs > 0:
        deltas = [(r.get("cf_pnl") or 0) - (r.get("actual_net") or 0)
                  for r in all_rows if r.get("actual_executed")
                  and r.get("status") in ("entered", "expired_unfilled")]
        cf_sum = sum(r.get("cf_pnl") or 0 for r in all_rows
                     if r.get("status") == "entered")
        act_sum = sum(r.get("actual_net") or 0 for r in all_rows if r.get("actual_executed")
                      and r.get("status") in ("entered", "expired_unfilled"))
        lines.append(f"- 累计 CF 净 {round(cf_sum, 2)} vs 配对实际净 {round(act_sum, 2)} "
                     f"(Δ {round(sum(deltas), 2)})")
    lines.append("- 判据: ①n≥20 配对 ②期望改善≥30% ③成交率≥50% ④mfe=0 占比降 — 四条全过才翻路由开关")
    lines.append("")

    # 5) 缺口对齐
    lines.append("## 五、缺口对齐影子 (gap_alignment)")
    if gap:
        lines.append(f"- 累计 {gap.get('n_total', '?')} 笔 (最新汇总: "
                     f"{json.dumps(gap.get('summary', gap), ensure_ascii=False)[:400]})")
    else:
        lines.append("- 无输出 (查 05:52 cron 日志)")
    lines.append("")

    # 6) 结论
    lines.append("## 六、今夜结论 (自动判读)")
    ok = sum(ch["archs"].values()) > 0 and ch["parse_fail"] == 0
    lines.append(f"- 管道: {'✅ 结构化字段到达且解析无误' if ok else '⚠️ 见第一/三节告警'}")
    if n_pairs >= 20:
        lines.append("- 样本已达判据门槛 — 可出正式裁决读数 (人工复核后)")
    else:
        lines.append(f"- 样本积累中 ({n_pairs}/20), 只攒证据不裁决; 每晚自动重放累计")
    return "\n".join(lines)


def _self_remove_cron():
    """自删一次性 crontab 行 (成功/失败都删, 防固定日期行年度复燃)。"""
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True).stdout
    except Exception as e:
        return f"crontab -l 失败 (跳过自删): {e}"
    kept = [l for l in cur.splitlines() if CRON_MARKER not in l]
    if len(kept) == len(cur.splitlines()):
        return "crontab 中未找到一次性行 (可能已删)"
    try:
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
        return "一次性 crontab 行已自删"
    except Exception as e:
        return f"crontab 自删失败 (需人工): {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    report = build_report(date)
    out = SHADOW_DIR / f"DEB20260901_shadow_report_{date}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[report] 已落盘 {out}")

    cron_note = _self_remove_cron()
    print(f"[report] {cron_note}")

    if not args.no_push:
        try:
            from app.notifier.feishu import send_text
            digest = [f"📋 DEB-20260901 影子首批判读 ({date})", ""]
            for ln in report.splitlines():
                if ln.startswith(("#", "-")) and "## 六" not in ln:
                    digest.append(ln.lstrip("#").strip())
            digest = digest[:40]
            digest.append("")
            digest.append(f"全文: {out}")
            send_text("\n".join(digest))
            print("[report] 飞书已推送")
        except Exception as e:
            print(f"[report] 飞书推送失败: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
