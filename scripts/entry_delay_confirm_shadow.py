#!/usr/bin/env python3
"""D2' 触发延迟确认影子 (DEB-20260909 §5.2 最高优先级, 2026-09-09 用户裁决「同意」落地).

对每笔 PLAYBOOK_SIM 已平仓穿越触发单, 回放当日 1 分 bar 回答反事实:
「若触发后不立即进场, 延迟 N∈{2,5} 分钟复核 — 价格仍站在触发方向上则进场, 否则放弃」。

判定口径 (方向归一):
  仍触发 (delayed fill): price_at(entry+N) 对 LONG ≥ entry_price / 对 SHORT ≤ entry_price
    ⇒ 机会成本 = 方向归一漂移 (LONG: (px−ep)/ep; SHORT: (ep−px)/ep), 正 = 延迟让你成交更贵
  回避 (avoided): 价格已回落/反抽过 entry_price ⇒ 延迟闸会放过这笔
    ⇒ 回避价值 = −pnl_net (回避掉亏损笔为正贡献; 回避掉盈利笔为误杀成本)

price_at(t) = 首根 bar_time ≥ t 的 bar 开盘价 (该时点之后最早可成交价, 确定性口径);
bar 加载/截断补拉/写回复用 post_exit_path_shadow 同一实现 (防双份漂移)。本影子只读
单点价不算日内聚合, 故窗口 bar 文件不构成本脚的截断污染 (post_exit P1 教训不适用,
但补拉纪律照用: 缺文件时 lazy-futu 补拉全天写回, 纯增益超集)。

预注册毕业判据 (DEB-20260909 §5.2 逐字): 回放 n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%;
不达标退役。人群注记 (DEB 原文未指定人群, 双人群并列呈裁): 避免率主判读人群 =
SL_INITIAL 出血族 (本影子动机 — 19 笔尸检 16 笔 MFE=0 / 11 笔 ≤25min);
机会成本在全体仍触发笔上计。两个人群的判据值都在 latest.json 并列, 裁决时二选一
不算挪门柱 (数值已冻结落盘)。

强制披露 (DEB §5.2 写清, 随每次汇总输出):
  ① confirm_gate 反向先验: ue_confirm_gate 影子 8 轮实证「等 +10min/+30min 再确认」
     方向 92% 不变但代价 +0.50%/笔 (t=5.67) — 延迟机制在本系统有成本前科。
     口径差异: confirm_gate 是盘前注入信号的盘中确认 (趋势衰减域), 本影子是
     开盘窗穿越触发的微结构复核 (噪声带回落域), 先验作参考不定罪。
  ② E3 TTL=300s 冲突: 现役 PULLBACK_LIMIT TTL=300s 与延迟确认互斥; D2' 若毕业
     转正, 须二选一或 TTL 重议 (转正裁决时裁, 本影子不预设)。

幂等: (trade_id, delay_min) 去重; 零交易行为变更; DB 只读 (mode=ro);
entry_time 混存时区纪律同 post_exit_path (数值统一 ET, [11:16] 切片比较)。

运行: ./venv/bin/python scripts/entry_delay_confirm_shadow.py [--days 60] [--date YYYY-MM-DD] [--no-fetch]
输出: data/shadow/entry_delay_confirm.jsonl (逐笔×N) + entry_delay_confirm_latest.json (汇总+判据)
cron: 58 5 * * 2-6 (post_exit_path 45 → gap_align 52 → unit_cell 55 同班, 台账 §2.3)
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import post_exit_path_shadow as _pep  # noqa: E402  bar 加载/补拉/写回复用同一实现 (防双份漂移)

DB = REPO / "data" / "ftseek_us.db"
GAP_JSONL = REPO / "data" / "shadow" / "gap_alignment_shadow.jsonl"
OUT = REPO / "data" / "shadow" / "entry_delay_confirm.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "entry_delay_confirm_latest.json"

DELAYS_MIN = (2, 5)
GRAD_MIN_N = 20
GRAD_MIN_AVOID_RATE = 0.30
GRAD_MAX_OPP_COST_PCT = 0.3   # %, 严格小于
# [RFC-081盲区1] forward 新窗起点 = DEB §5.2 预注册攒样日 (9/10 影子落地次日)
FORWARD_SINCE = "2026-09-10"

DISCLOSURES = [
    "confirm_gate 反向先验: 等待确认历史成本 +0.50%/笔 (t=5.67, n=8轮); "
    "口径差异 = 盘前注入趋势衰减域 vs 本影子开盘窗微结构噪声域, 作参考不定罪。",
    "E3 TTL=300s 冲突: PULLBACK_LIMIT 现役 TTL 与延迟确认互斥, 转正时二选一或 TTL 重议。",
    # [2026-09-16 RFC-081 增补盲区1 批注] 强制披露:
    "分流人群分层: 按成交单口径, forward 窗 ~72% 属 D 分支候选 (可路由限价单) — "
    "B 案原 ALL 块是混合人群读数; 分流落地后 B 只管 by_branch.B_only / "
    "forward.B_only 人群。裁决日 B 分支须以 forward_B_only_pass 为主判读, "
    "ALL_pass 只作历史参考, 不得用混合人群给分流后的 B 背书。",
    # [2026-09-16 宪章第 9 律批注] 每个 _agg_block 新增 day_clustered 读数:
    "日聚类+MDE(宪章9): 各块新增 day_clustered (延迟门净价值/笔按日等权聚类后的 t 与 "
    "MDE(80%)); n_days<3 → None 不可判。判据 (避免率/机会成本) 数值不动 — 该读数只回答 "
    "'B 案净效果与 0 是否可分辨', 不改变毕业门槛。",
]


def _log(msg: str):
    print(f"[entry-delay-cf] {msg}", file=sys.stderr)


def _load_done() -> set:
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("type") == "entry_delay" and r.get("trade_id") and r.get("delay_min"):
                    done.add((r["trade_id"], int(r["delay_min"])))
            except Exception as e:
                _log(f"幂等集合脏行跳过: {e}")
    return done


def _load_sources() -> dict:
    """trade_id → source (gap_alignment_shadow 单一真源); 覆盖缺口记 '?' (如 BWXT 8/28)."""
    m = {}
    if GAP_JSONL.exists():
        for ln in GAP_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
                if r.get("trade_id"):
                    m[r["trade_id"]] = r.get("source") or "?"
            except Exception as e:
                _log(f"gap jsonl 脏行跳过: {e}")
    return m


def _load_routable_map(con) -> dict:
    """[2026-09-16 RFC-081 增补盲区1] trade_id → routable_candidate (bool).

    RFC-081 分流融合按成交单口径下, D 分支候选 (PULLBACK_LIMIT/FADE_SHORT +
    有效 confirmation.price_level) 占 forward 窗成交单 ~72% — B 案的判据是在
    混合人群上过的, 分流落地后 B 只管非可路由人群。本函数给每笔 trade 打
    「分流后归 B 分支还是 D 分支候选」标签, summary 按人群分层呈裁。
    纯 record-only 观测: 不改变 ALL/SL_INITIAL 原块与原毕业判据。
    """
    routable_dids = set()
    try:
        # decision_id 是表列, 不在 raw_instruction_json 里 (实测 raw keys 无此字段)
        for did, raw in con.execute(
                "SELECT decision_id, raw_instruction_json FROM playbook_instructions "
                "WHERE decision_id IS NOT NULL AND decision_id != ''"):
            try:
                r = json.loads(raw or "{}")
            except Exception:
                continue
            if (r.get("entry_archetype") in ("PULLBACK_LIMIT", "FADE_SHORT")
                    and (r.get("confirmation") or {}).get("price_level")):
                routable_dids.add(did)
    except Exception as e:
        _log(f"routable 映射查询失败(分层块将为空): {e}")
        return {}
    if not routable_dids:
        return {}
    m = {}
    try:
        for tid, did in con.execute(
                "SELECT id, decision_id FROM trade_records WHERE decision_id != ''"):
            m[tid] = did in routable_dids
    except Exception as e:
        _log(f"trade→decision 映射失败: {e}")
    return m


def _hhmm_to_min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _min_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _price_at(bars, target_hhmm: str):
    """首根 bar_time ≥ target 的 bar 开盘价; 覆盖不到返回 None."""
    for b in bars:
        if b[0][11:16] >= target_hhmm:
            return float(b[1])
    return None


def _eval_delay(direction: str, entry_price: float, px: float):
    """返回 (still_triggers, opp_cost_pct). opp_cost 方向归一, 正=延迟让你成交更贵."""
    if direction == "LONG":
        return px >= entry_price, (px - entry_price) / entry_price * 100
    return px <= entry_price, (entry_price - px) / entry_price * 100


def _day_clustered_t(daily_values):
    """[2026-09-16 用户裁「按照建议执行」] 宪章第 9 律读数 — 日聚类 t + MDE(80%).

    与 scripts/exit_scorecard.py 同一口径: 先按日等权取均值, 再对日均值做单样本 t
    (同日多笔高度相关, pooled 统计会被大日绑架 — RFC-081 窗实测 9/10 单日占 42% 配对)。
    单位声明: t 无量纲; day_mean / mde_80pct 与入参同量纲 (本调用 = 美元/笔)。
    n_days<3 或零方差 → None (不可判, 不得当成 0 或 PASS)。
    """
    n_days = len(daily_values)
    if n_days < 3:
        return None
    day_mean = statistics.mean(daily_values)
    sd = statistics.stdev(daily_values)
    if sd == 0:
        return None
    t = day_mean / (sd / math.sqrt(n_days))
    return {
        "t": round(t, 2),
        "n_days": n_days,
        "day_mean": round(day_mean, 2),
        "mde_80pct": round(2.802 * sd / math.sqrt(n_days), 2),
        "distinguishable": bool(abs(t) >= 2.0),
        "unit": "usd_per_trade (day_mean/mde_80pct; t 无量纲)",
    }


def _full_day(symbol: str, et_date: str, allow_fetch: bool):
    return _pep._fetch_full_day(symbol, et_date, allow_fetch)


def _agg_block(recs: list) -> dict:
    ok = [r for r in recs if r.get("status") == "ok"]
    av = [r for r in ok if not r["still_triggers"]]
    dl = [r for r in ok if r["still_triggers"]]
    avoided_net = sum(float(r.get("pnl_net") or 0) for r in av)
    opp = [float(r["opp_cost_pct"]) for r in dl]
    # [宪章第 9 律] 延迟门净价值/笔: avoided→省下的钱 (−pnl_net);
    # delayed→付出的机会成本 (−opp_cost_pct/100×notional)。按日聚类后与 0 可辨
    # 才允许说「B 案有/没有净效果」— 判据 (避免率/机会成本) 本身不动。
    _by_day = {}
    for r in ok:
        if not r["still_triggers"]:
            val = -(float(r.get("pnl_net") or 0))
        else:
            val = -(float(r.get("opp_cost_pct") or 0) / 100.0
                    * float(r.get("notional") or 0))
        _by_day.setdefault(str(r.get("date") or "?"), []).append(val)
    day_clustered = _day_clustered_t(
        [sum(v) / len(v) for v in _by_day.values()] if _by_day else [])
    # [RFC-081盲区1] 误杀 = 回避组里的盈利单 (延迟门放过的本来会赚钱的单)
    miskilled = [r for r in av if float(r.get("pnl_net") or 0) > 0]
    return {
        "n": len(ok),
        "n_avoided": len(av),
        "avoidance_rate": round(len(av) / len(ok), 4) if ok else None,
        "avoided_net_usd": round(avoided_net, 2),
        "avoided_value_usd": round(-avoided_net, 2),
        "n_miskilled": len(miskilled),
        "miskill_rate": round(len(miskilled) / len(av), 4) if av else None,
        "n_delayed": len(dl),
        "opp_cost_mean_pct": round(statistics.mean(opp), 4) if opp else None,
        "opp_cost_median_pct": round(statistics.median(opp), 4) if opp else None,
        "opp_cost_usd": round(sum(float(r["opp_cost_pct"]) / 100 * float(r["notional"])
                                  for r in dl), 2),
        "net_value_usd": round(sum(sum(v) for v in _by_day.values()), 2) if ok else None,
        "day_clustered": day_clustered,
    }


def _grad_pass(block: dict) -> bool:
    """预注册毕业判据 (DEB §5.2): n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%. None 一律不过."""
    return (block["n"] >= GRAD_MIN_N
            and block["avoidance_rate"] is not None
            and block["avoidance_rate"] >= GRAD_MIN_AVOID_RATE
            and block["opp_cost_mean_pct"] is not None
            and block["opp_cost_mean_pct"] < GRAD_MAX_OPP_COST_PCT)


def main(days: int = 60, only_date: str | None = None, allow_fetch: bool = True) -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    done = _load_done()
    sources = _load_sources()
    routable_map = _load_routable_map(con)  # [RFC-081盲区1] trade_id→D分支候选

    where = ("entry_time >= datetime('now', ?) " if not only_date else "date(entry_time) = ? ")
    params = (only_date,) if only_date else (f"-{days} day",)
    rows = con.execute(
        "SELECT id, symbol, direction, entry_time, entry_price, quantity, "
        "exit_reason, pnl_net FROM trade_records "
        "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
        "AND entry_price IS NOT NULL AND entry_price > 0 "
        "AND exit_price IS NOT NULL AND exit_price > 0 "
        "AND exit_reason != 'PHANTOM_RECONCILE_VOID' AND " + where +
        "ORDER BY entry_time",
        params
    ).fetchall()

    n_new = n_refetch = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for r in rows:
            pending = [n for n in DELAYS_MIN if (r["id"], n) not in done]
            if not pending:
                continue
            symbol, direction = r["symbol"], (r["direction"] or "LONG")
            entry_iso = str(r["entry_time"]).replace("T", " ")
            et_date, t_entry = entry_iso[:10], entry_iso[11:16]
            ep = float(r["entry_price"])
            notional = ep * float(r["quantity"] or 0)
            bars, refreshed = _full_day(symbol, et_date, allow_fetch)
            if refreshed:
                n_refetch += 1
            for n in pending:
                rec = {"type": "entry_delay", "trade_id": r["id"], "delay_min": n,
                       "symbol": symbol, "direction": direction, "date": et_date,
                       "entry_min": t_entry, "entry_price": ep, "notional": notional,
                       "exit_reason": r["exit_reason"],
                       "pnl_net": float(r["pnl_net"]) if r["pnl_net"] is not None else None,
                       "source": sources.get(r["id"], "?"),
                       # [RFC-081盲区1] 分流后归属: True=D分支候选(可路由限价),
                       # False=B分支(延迟复核), None=映射不到(老单/无decision_id)
                       "routable_candidate": routable_map.get(r["id"])}
                if not bars:
                    rec["status"] = "no_bars"
                else:
                    target = _min_to_hhmm(_hhmm_to_min(t_entry) + n)
                    px = _price_at(bars, target)
                    if px is None:
                        rec["status"] = "no_bar_at_target"
                        rec["bar_end_min"] = bars[-1][0][11:16]
                    else:
                        still, opp = _eval_delay(direction, ep, px)
                        rec.update({"status": "ok", "target_min": target,
                                    "px_at_target": px, "still_triggers": still,
                                    "opp_cost_pct": round(opp, 4)})
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_new += 1
    con.close()
    if _pep._ctx is not None:
        try:
            _pep._ctx.close()
        except Exception as e:
            _log(f"ctx.close 异常(无副作用): {e}")

    # ── 汇总 (读全量 jsonl 含历史) ──
    all_rows = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                rr = json.loads(line)
                if rr.get("type") == "entry_delay":
                    # [RFC-081盲区1] 历史行无 routable_candidate → 汇总期从映射回填
                    # (jsonl 本体不重写; 行级标签以本次运行映射为准, 幂等无副作用)
                    if rr.get("routable_candidate") is None and rr.get("trade_id") in routable_map:
                        rr["routable_candidate"] = routable_map[rr["trade_id"]]
                    all_rows.append(rr)
            except Exception as e:
                _log(f"汇总读取脏行跳过: {e}")

    delays = {}
    for n in DELAYS_MIN:
        recs = [r for r in all_rows if r.get("delay_min") == n]
        blk_all = _agg_block(recs)
        blk_sl = _agg_block([r for r in recs if r.get("exit_reason") == "SL_INITIAL"])
        delays[str(n)] = {
            "ALL": blk_all,
            "SL_INITIAL": blk_sl,
            # [RFC-081盲区1] 分流人群分层 (record-only 观测, 原 ALL/SL_INITIAL 块不动):
            #   B分支 = routable_candidate=False (分流后延迟复核只管这些);
            #   D候选 = True (分流后归限价路由, B 判据不应再拿它背书);
            #   unmapped = None (老单/无 decision_id 映射)
            "by_branch": {
                "B_only": _agg_block([r for r in recs if r.get("routable_candidate") is False]),
                "D_candidate": _agg_block([r for r in recs if r.get("routable_candidate") is True]),
                "unmapped": _agg_block([r for r in recs if r.get("routable_candidate") is None]),
            },
            # forward 新窗 (DEB §5.2 预注册裁决口径: 自 9/10 攒样日起)
            "forward": {
                "ALL": _agg_block([r for r in recs if r.get("date", "") >= FORWARD_SINCE]),
                "B_only": _agg_block([r for r in recs if r.get("date", "") >= FORWARD_SINCE
                                      and r.get("routable_candidate") is False]),
                "D_candidate": _agg_block([r for r in recs if r.get("date", "") >= FORWARD_SINCE
                                           and r.get("routable_candidate") is True]),
            },
            "by_source": {s: _agg_block([r for r in recs if r.get("source") == s])
                          for s in sorted({r.get("source") for r in recs})},
            "status_counts": {s: sum(1 for r in recs if r.get("status") == s)
                              for s in sorted({r.get("status") for r in recs})},
        }
    graduation = {str(n): {
        "ALL_pass": _grad_pass(delays[str(n)]["ALL"]),
        "SL_INITIAL_pass": _grad_pass(delays[str(n)]["SL_INITIAL"]),
        # [RFC-081盲区1] forward + 分流人群毕业读数 — 裁决日主判读 (预注册于
        # RFC-081 增补批注§六): B 分支落地后只管非可路由人群, 其 forward B_only
        # 块才是分流后的真实验收人群; ALL_pass 原样保留不替换 (不挪门柱, 只加读数)。
        "forward_ALL_pass": _grad_pass(delays[str(n)]["forward"]["ALL"]),
        "forward_B_only_pass": _grad_pass(delays[str(n)]["forward"]["B_only"]),
    } for n in DELAYS_MIN}

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"days": days, "only_date": only_date},
        "n_new": n_new, "n_refetch": n_refetch, "n_rows_total": len(all_rows),
        "delays": delays,
        "graduation": graduation,
        "criteria": "DEB-20260909 §5.2 预注册: 回放 n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%; "
                    "不达标退役。人群注记: 避免率主判读人群=SL_INITIAL 出血族, 机会成本在"
                    "全体仍触发笔上计; DEB 原文未指定人群, 双人群并列呈裁不算挪门柱。",
        "disclosures": DISCLOSURES,
        "caliber": "price_at(t)=首根 bar_time≥t 的 bar 开盘价; 回避价值=−pnl_net; "
                   "机会成本=方向归一漂移(正=延迟更贵); record-only 零交易行为变更。",
    }
    OUT_LATEST.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[entry-delay-cf] 新增 {n_new} 行 (补拉 {n_refetch}), 累计 {len(all_rows)} 行")
    for n in DELAYS_MIN:
        d = delays[str(n)]
        a, s = d["ALL"], d["SL_INITIAL"]
        print(f"  N={n}min | ALL n={a['n']} 回避 {a['n_avoided']} "
              f"({100 * (a['avoidance_rate'] or 0):.0f}%) 回避价值 ${a['avoided_value_usd']:+.2f} "
              f"机会成本均值 {a['opp_cost_mean_pct']}% (${a['opp_cost_usd']:+.2f}) "
              f"| SL_INITIAL n={s['n']} 回避率 {s['avoidance_rate']} "
              f"| 毕业: ALL={'✓' if graduation[str(n)]['ALL_pass'] else '✗'} "
              f"SL_INITIAL={'✓' if graduation[str(n)]['SL_INITIAL_pass'] else '✗'}")
        # [RFC-081盲区1] 分流人群分层 + forward 读数一行
        f_all = d["forward"]["ALL"]; f_b = d["forward"]["B_only"]; f_d = d["forward"]["D_candidate"]
        print(f"    ├ 分流: B_only n={d['by_branch']['B_only']['n']} / "
              f"D候选 n={d['by_branch']['D_candidate']['n']} / "
              f"unmapped n={d['by_branch']['unmapped']['n']}")
        print(f"    └ forward(≥{FORWARD_SINCE}): ALL n={f_all['n']} "
              f"opp={f_all['opp_cost_mean_pct']}% grad={'✓' if graduation[str(n)]['forward_ALL_pass'] else '✗'} | "
              f"B_only n={f_b['n']} 回避率={f_b['avoidance_rate']} opp={f_b['opp_cost_mean_pct']}% "
              f"误杀率={f_b['miskill_rate']} grad={'✓' if graduation[str(n)]['forward_B_only_pass'] else '✗'} | "
              f"D候选 n={f_d['n']}")
        for _tag, _blk in (("ALL", a), ("fwd_B_only", f_b)):
            _dc = _blk.get("day_clustered")
            if _dc:
                print(f"    日聚类[{_tag}](宪章9): n_days={_dc['n_days']} "
                      f"day_mean={_dc['day_mean']:+.2f}美元/笔 t={_dc['t']} "
                      f"MDE(80%)=±{_dc['mde_80pct']}美元 "
                      f"⇒ {'与0可分辨' if _dc['distinguishable'] else '与0不可分辨'}")
    print(f"→ {OUT_LATEST}")
    return n_new


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="D2' 触发延迟确认影子 (record-only)")
    ap.add_argument("--days", type=int, default=60, help="回看天数 (默认60)")
    ap.add_argument("--date", type=str, default=None, help="只跑单日 (YYYY-MM-DD)")
    ap.add_argument("--no-fetch", action="store_true", help="禁止补拉 (只用本地 bar)")
    a = ap.parse_args()
    main(days=a.days, only_date=a.date, allow_fetch=not a.no_fetch)
