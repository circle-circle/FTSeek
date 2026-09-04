#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次0 三标签违例扫描器 + lessons 生命周期三闸 (DEB-20260904 §10.2, record-only).

定位: 纯确定性 SQL 规则, 零 LLM, 零新 cron/DB/推送通道。
  - 三标签每日重算不落库 (违例明细只进 stdout/JSON, 由 04:00 报告段消费);
  - lessons 表生命周期列 (status/recurrence_count/last_triggered_at/cost_since)
    是唯一持久化写入, 按 ET 日幂等增量 (同日重跑不重复计数)。

判定 SQL/规则单真源:
  ~/.hermes/skills/quantitative-trading/ftseek-trade-history-review/
  references/violation-taxonomy-batch0-20260904.md

三标签 (口径=glm 复现口径, 9/3 实测 8/12 违例已对账):
  CHASE_ENTRY    方向感知追价: LONG 成交价>cap / SHORT 成交价<cap;
                 cap=raw_instruction_json 顶层 chase_cap_price (JOIN ON decision_id,
                 重复 decision_id 取 MAX(id)); 无 cap 记 null 不编造、不算违例。
  OVERSIZED_RISK 单笔估算美元风险 qty × (1.5×ATR止损距离) > 当日中位数×2;
                 ATR 距离=atr_pct(小数)×entry_price; atr_pct 缺失记 null 不入中位。
  CLUSTER_BET    由 entry_time/exit_time/direction 重建 [entry, exit) 持仓区间
                 (未平仓=至今), 开仓时刻同向并发数(含自身) ≥4 → 打标该笔。

锁定口径三元组 (§11.3 立项4):
  窗口   = 滚动 --days (默认30) 个自然日, 终点=库内 MAX(trade_date);
  过滤   = data_quality='real' (与月度复盘/glm/kimi 复现一致);
  金额   = pnl_net, 缺失回退 realized_pnl (净口径)。

lessons 三闸 (只作用于 source_type='violation_scan' 的规范行, 每标签至多一行;
存量 201 行 legacy 'active' 教训一律不触碰 → 注入行为对旧教训逐字节等价):
  闸1 PENDING 门禁: 规范行创建即 PENDING (legacy injector 只认 'active', 天然不注入);
      recurrence_count ≥ 2 才升 ACTIVE (injector 已扩为 status IN ('active','ACTIVE'))。
  闸2 排序 recurrence×|cost|: 注入端 ORDER BY recurrence_count×ABS(cost_since) DESC,
      impact_score DESC (legacy 行两列为 0/NULL → 乘积 0 → 次序与改前完全一致)。
      cost_since 为带符号净成本 (= −Σpnl_net: 亏损 +, 盈利 −); 排序/展示一律取 ABS
      (2026-09-05 修正: 带符号排序把高复发净亏教训沉到 legacy 之下, 与 §10.1
      "顶掉低质模板" 意图相反); 符号保留在列中 — "复发但盈利" 矛盾信号 (Kimi P1)
      由报告段带符号呈现, 不靠排序沉底表达。
  闸3 30天自动退休: ACTIVE 且 last_triggered_at 距今 >30 天 → RETIRED;
      RETIRED 行再复发 → 复活为新生命周期 (recurrence_count=1, cost_since 重置 0)。

幂等约定: last_triggered_at 存 "已处理至该 ET 日" 标记 (YYYY-MM-DDT23:59:59, ET 墙钟);
只处理 day > date(last_triggered_at) 的违例日。首跑回补整个窗口 (历史复发即证据,
n≥2 立即 ACTIVE — 这是设计意图, 报告段第 1 天就有可考核的复发账)。

铁律: record-only / 不动退出参数 (exit-de49b1299290) / 不自动晋升任何教训为硬门
(generate_trade_constraints 无 CHASE_ENTRY 等分支, 已核; 注入仅软性 prompt 文本)。
守卫: 新写入方带 PYTEST_CURRENT_TEST 守卫 — 测试态指向 data/ 生产库直接拒写。

用法:
  python scripts/daily_violation_scan.py            # 扫描+写 lessons 闸 (cron 形态)
  python scripts/daily_violation_scan.py --no-write # 干跑, 只打印不写库
  python scripts/daily_violation_scan.py --json     # 机器可读输出 (供报告段解析)
  python scripts/daily_violation_scan.py --days 14 --db /tmp/x.db
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.core.db_utils import get_main_db  # noqa: E402
from app.review.lesson_injector import LessonInjector  # noqa: E402  (表+列防御性迁移 SSOT)
from utils.et_time import ET, naive_cst_to_et, parse_et_date  # noqa: E402  (RFC-070 时间尺子)

logger = logging.getLogger("ftseek_us.daily_violation_scan")

LABELS = ("CHASE_ENTRY", "OVERSIZED_RISK", "CLUSTER_BET")
LOOKBACK_DAYS = 30            # 滚动窗口 (口径三元组之一, 改窗=改口径, 须留痕)
CLUSTER_MIN_CONCURRENT = 4    # 同向并发 ≥4 (DEB §10.2)
OVERSIZED_MULT = 2.0          # > 当日中位 ×2
ATR_STOP_MULT = 1.5           # 止损距离 = 1.5×ATR
PROMOTE_MIN_RECURRENCE = 2    # 闸1: n≥2 升 ACTIVE
RETIRE_AFTER_DAYS = 30        # 闸3: 30 天零复发自动退休

LESSON_CATEGORY = "violation"
LESSON_SOURCE = "violation_scan"
TAXONOMY_DOC = ("~/.hermes/skills/quantitative-trading/ftseek-trade-history-review/"
                "references/violation-taxonomy-batch0-20260904.md")

LESSON_TEXT = {
    "CHASE_ENTRY": (
        "方向感知追价违例复发: LONG 成交价>指令 chase_cap_price / SHORT 成交价<cap "
        "(判定口径见 violation-taxonomy-batch0)。复发=追价上限被绕过或执行链未按指令语义拒单。"
    ),
    "OVERSIZED_RISK": (
        "单笔估算美元风险 (qty×1.5×ATR止损距离) 超当日中位数×2 复发 "
        "(判定口径见 violation-taxonomy-batch0)。复发=风险预算集中度失控。"
    ),
    "CLUSTER_BET": (
        "开仓时刻同向持仓并发≥4 复发 (entry/exit 区间重建, 见 violation-taxonomy-batch0)。"
        "复发=同向簇崩塌敞口重现 (8/14、8/18、8/28 三簇日模式)。"
    ),
}


# ─────────────────────────── 时间工具 ───────────────────────────

def _parse_dt(s):
    """混杂时间戳 → aware ET datetime (RFC-070: 裸时间戳视为 CST)。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return naive_cst_to_et(dt, keep_aware=True)
    return dt.astimezone(ET)


def _et_day(ts, fallback: str) -> str:
    """ET 交易日 (SSOT: utils.et_time.parse_et_date), 解析失败 warn 留痕后回退 trade_date。"""
    if ts:
        try:
            return parse_et_date(ts)
        except (ValueError, TypeError) as e:
            # R5 模式 (except_pass 棘轮): 新增异常处理必须 warn 留痕, 不静默吞
            logger.warning("[et_day] entry_time 解析失败回退 trade_date=%s: %r (%s)",
                           fallback, ts, e)
    return fallback


def _pnl_net(row) -> float | None:
    v = row["pnl_net"]
    if v is None:
        v = row["realized_pnl"]
    return None if v is None else float(v)


# ─────────────────────────── 数据加载 ───────────────────────────

def load_trades(conn: sqlite3.Connection, start: str) -> list[dict]:
    """只读 trade_records + playbook_instructions (LEFT JOIN, 重复 decision_id 取 MAX(id))。"""
    rows = conn.execute(
        """
        SELECT t.id, t.trade_date, t.symbol, t.direction,
               t.entry_time, t.exit_time, t.entry_price, t.quantity,
               t.pnl_net, t.realized_pnl, t.decision_id,
               pi.atr_pct,
               json_extract(pi.raw_instruction_json, '$.chase_cap_price') AS chase_cap
        FROM trade_records t
        LEFT JOIN (
            SELECT decision_id, MAX(id) AS mid
            FROM playbook_instructions
            WHERE decision_id IS NOT NULL AND decision_id != ''
            GROUP BY decision_id
        ) m ON m.decision_id = t.decision_id
        LEFT JOIN playbook_instructions pi ON pi.id = m.mid
        WHERE t.data_quality = 'real'
          AND t.trade_date >= ?
        ORDER BY t.entry_time
        """,
        (start,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────── 三标签判定 (纯函数) ───────────────────────────

def label_chase(t: dict) -> str | None:
    """'violation' / 'no_cap' / 'ok' / None(不可判: 无成交价)。cap 缺失记 null 不编造。"""
    cap, px = t.get("chase_cap"), t.get("entry_price")
    if cap is None:
        return "no_cap"
    if px is None:
        return None
    cap, px = float(cap), float(px)
    if t["direction"] == "LONG" and px > cap:
        return "violation"
    if t["direction"] == "SHORT" and px < cap:
        return "violation"
    return "ok"


def risk_usd(t: dict) -> float | None:
    """估算美元风险 = qty × 1.5×ATR止损距离; atr_pct(小数)/qty/px 任一缺失 → None。"""
    atr, qty, px = t.get("atr_pct"), t.get("quantity"), t.get("entry_price")
    if atr is None or qty is None or px is None:
        return None
    atr, qty, px = float(atr), float(qty), float(px)
    if atr <= 0 or qty <= 0 or px <= 0:
        return None
    return qty * ATR_STOP_MULT * atr * px


def scan_window(trades: list[dict], days: int) -> dict:
    """按 ET 日分组打三标签。返回 {day: {label: {...}}} + 逐笔明细。"""
    by_day: dict[str, list[dict]] = {}
    for t in trades:
        day = _et_day(t.get("entry_time"), t.get("trade_date"))
        t["_day"] = day
        by_day.setdefault(day, []).append(t)

    now = datetime.now(ET)
    # 全窗口持仓区间 (CLUSTER_BET 并发数与日分组无关, 跨日持仓自然计入)
    intervals = []
    for t in trades:
        start = _parse_dt(t.get("entry_time"))
        if start is None:
            continue
        end = _parse_dt(t.get("exit_time")) or now  # 未平仓=至今
        intervals.append((t["direction"], start, end, t))

    results: dict[str, dict] = {}
    for day in sorted(by_day):
        day_trades = by_day[day]
        # CHASE_ENTRY
        chase = {"violation": [], "no_cap": 0, "ok": 0, "unknown": 0}
        for t in day_trades:
            v = label_chase(t)
            if v == "violation":
                chase["violation"].append(
                    {"trade_id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
                     "entry_price": t["entry_price"], "chase_cap": t["chase_cap"],
                     "pnl_net": _pnl_net(t)}
                )
            elif v == "no_cap":
                chase["no_cap"] += 1
            elif v == "ok":
                chase["ok"] += 1
            else:
                chase["unknown"] += 1
        # OVERSIZED_RISK
        risks = [(t, risk_usd(t)) for t in day_trades]
        known = [r for _, r in risks if r is not None]
        med = median(known) if known else None
        oversized = []
        for t, r in risks:
            if r is not None and med is not None and r > OVERSIZED_MULT * med:
                oversized.append(
                    {"trade_id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
                     "risk_usd": round(r, 2), "day_median_usd": round(med, 2),
                     "pnl_net": _pnl_net(t)}
                )
        atr_null = sum(1 for _, r in risks if r is None)
        # CLUSTER_BET
        cluster = []
        for t in day_trades:
            ts = _parse_dt(t.get("entry_time"))
            if ts is None:
                continue
            concurrent = sum(
                1 for d, s, e, _ in intervals
                if d == t["direction"] and s <= ts < e
            )
            if concurrent >= CLUSTER_MIN_CONCURRENT:
                cluster.append(
                    {"trade_id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
                     "concurrent_at_entry": concurrent, "pnl_net": _pnl_net(t)}
                )
        results[day] = {
            "n_trades": len(day_trades),
            "CHASE_ENTRY": chase,
            "OVERSIZED_RISK": {"violation": oversized, "atr_null": atr_null,
                               "day_median_usd": round(med, 2) if med is not None else None},
            "CLUSTER_BET": {"violation": cluster},
        }
    # 窗口裁剪: 只保留 [start, end] 内的 ET 日
    if trades:
        end_day = max(t["trade_date"] for t in trades)
        start_day = (datetime.strptime(end_day, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        results = {d: v for d, v in results.items() if start_day <= d <= end_day}
    return results


# ─────────────────────────── lessons 三闸 (唯一写入方) ───────────────────────────

def _is_prod_db(db_path) -> bool:
    try:
        return Path(db_path).resolve().parent == (ROOT / "data").resolve()
    except OSError:
        return False


def apply_lesson_gates(db_path, day_results: dict, now_et: datetime) -> dict:
    """幂等更新 violation_scan 规范行的 recurrence/cost/status。返回逐标签动作日志。

    PYTEST_CURRENT_TEST 守卫: 测试态指向 data/ 生产库 → 拒写 (硬约束)。
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and _is_prod_db(db_path):
        raise RuntimeError(
            f"[daily_violation_scan] PYTEST_CURRENT_TEST 守卫: 拒绝在测试态写生产库 {db_path}"
        )
    # 表+4列幂等迁移 (SSOT 在 lesson_injector._init_db, 此处复用不复制 schema)
    LessonInjector(db_path=str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    log = {}
    try:
        for label in LABELS:
            row = conn.execute(
                "SELECT * FROM lessons WHERE source_type=? AND lesson_subcategory=? "
                "ORDER BY id LIMIT 1",
                (LESSON_SOURCE, label),
            ).fetchone()

            # 本标签窗口内未处理的违例日 (day > last_triggered_at 日期部分)
            processed_through = ""
            if row and row["last_triggered_at"]:
                processed_through = str(row["last_triggered_at"])[:10]
            pending_days = sorted(
                d for d, r in day_results.items()
                if r[label]["violation"] and d > processed_through
            )

            actions = {"created": False, "days_processed": pending_days,
                       "added_recurrence": 0, "added_cost": 0.0,
                       "status_before": row["status"] if row else None,
                       "status_after": None, "promoted": False, "retired": False,
                       "revived": False}

            if pending_days:
                add_n, add_cost = 0, 0.0
                for d in pending_days:
                    vios = day_results[d][label]["violation"]
                    add_n += len(vios)
                    # 带符号净烧钱: 亏损 +, 盈利 − (未平仓 pnl=NULL 计 0 但计次)
                    add_cost += sum(-p for p in (v["pnl_net"] for v in vios) if p is not None)
                last_day = pending_days[-1]
                triggered = f"{last_day}T23:59:59"  # ET 墙钟"已处理至该日"标记

                if row is None:
                    # 闸1: 首见即建 PENDING 行 (legacy injector 不认 PENDING → 天然不注入)
                    conn.execute(
                        """INSERT INTO lessons (
                               lesson_text, lesson_category, lesson_subcategory,
                               impact_score, confidence, symbols, source_type,
                               status, expiry_date, evidence,
                               recurrence_count, last_triggered_at, cost_since)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (LESSON_TEXT[label], LESSON_CATEGORY, label,
                         0.0, 1.0, "[]", LESSON_SOURCE,
                         "PENDING", "",
                         json.dumps({"rule_doc": TAXONOMY_DOC,
                                     "created_by": "scripts/daily_violation_scan.py",
                                     "window_violations_first_seen": last_day},
                                    ensure_ascii=False),
                         0, "", 0.0),
                    )
                    row = conn.execute(
                        "SELECT * FROM lessons WHERE source_type=? AND lesson_subcategory=? "
                        "ORDER BY id LIMIT 1", (LESSON_SOURCE, label)).fetchone()
                    actions["created"] = True

                status = row["status"]
                if status == "RETIRED":
                    # 闸3 复活: 退休后再复发 → 新生命周期 (计数/成本重置后累计全部未处理日)
                    new_n = sum(len(day_results[d][label]["violation"]) for d in pending_days)
                    new_cost = sum(
                        -p for d in pending_days
                        for p in (v["pnl_net"] for v in day_results[d][label]["violation"])
                        if p is not None)
                    conn.execute(
                        "UPDATE lessons SET recurrence_count=?, cost_since=?, "
                        "last_triggered_at=?, status=? WHERE id=?",
                        (new_n, round(new_cost, 2), triggered,
                         "ACTIVE" if new_n >= PROMOTE_MIN_RECURRENCE else "PENDING",
                         row["id"]),
                    )
                    actions["revived"] = True
                    actions["added_recurrence"] = new_n
                    actions["added_cost"] = round(new_cost, 2)
                else:
                    new_n = (row["recurrence_count"] or 0) + add_n
                    new_cost = (row["cost_since"] or 0.0) + add_cost
                    new_status = status
                    if status == "PENDING" and new_n >= PROMOTE_MIN_RECURRENCE:
                        new_status = "ACTIVE"   # 闸1: n≥2 升 ACTIVE
                        actions["promoted"] = True
                    conn.execute(
                        "UPDATE lessons SET recurrence_count=?, cost_since=?, "
                        "last_triggered_at=?, status=? WHERE id=?",
                        (new_n, round(new_cost, 2), triggered, new_status, row["id"]),
                    )
                    actions["added_recurrence"] = add_n
                    actions["added_cost"] = round(add_cost, 2)
                row = conn.execute("SELECT * FROM lessons WHERE id=?", (row["id"],)).fetchone()

            # 闸3: 30 天零复发自动退休 (仅本扫描器管辖行; legacy 行 last_triggered_at
            # 为空或 source_type 不符, 永不触碰)
            if row is not None and row["status"] == "ACTIVE" and row["last_triggered_at"]:
                last_day = str(row["last_triggered_at"])[:10]
                try:
                    age = (now_et.date() - datetime.strptime(last_day, "%Y-%m-%d").date()).days
                except ValueError as e:
                    logger.warning("[retire] last_triggered_at 日期解析失败, 跳过退休判定: %r (%s)",
                                   row["last_triggered_at"], e)
                    age = 0
                if age > RETIRE_AFTER_DAYS:
                    conn.execute("UPDATE lessons SET status='RETIRED' WHERE id=?", (row["id"],))
                    actions["retired"] = True
                    actions["status_after"] = "RETIRED"

            if row is not None:
                actions["status_after"] = actions["status_after"] or row["status"]
                actions["lesson_id"] = row["id"]
                actions["recurrence_count"] = row["recurrence_count"] or 0
                actions["cost_since"] = row["cost_since"] or 0.0
                actions["last_triggered_at"] = row["last_triggered_at"] or ""
                # ABS 排序键 (2026-09-05 team-lead 核验修正): 与注入端 ORDER BY 一致
                actions["rank_key"] = round(
                    (row["recurrence_count"] or 0) * abs(row["cost_since"] or 0.0), 2)
            log[label] = actions
        conn.commit()
    finally:
        conn.close()
    return log


def lessons_growth(conn: sqlite3.Connection) -> dict:
    """污染监控添头 (§11.2 立即采纳项): lessons 行数增长率, 零持久化 (由 created_at 重算)。"""
    total = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    now = datetime.now(ET)
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    d14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    last7 = conn.execute(
        "SELECT COUNT(*) FROM lessons WHERE substr(created_at,1,10) >= ?", (d7,)).fetchone()[0]
    prev7 = conn.execute(
        "SELECT COUNT(*) FROM lessons WHERE substr(created_at,1,10) >= ? AND substr(created_at,1,10) < ?",
        (d14, d7)).fetchone()[0]
    by_source = {r[0] or "null": r[1] for r in conn.execute(
        "SELECT source_type, COUNT(*) FROM lessons GROUP BY source_type")}
    return {"total_rows": total, "new_last_7d": last7, "new_prev_7d": prev7,
            "growth_delta": last7 - prev7, "by_source_type": by_source}


# ─────────────────────────── 报告渲染 ───────────────────────────

def run_scan(db_path=None, days: int = LOOKBACK_DAYS, write: bool = True) -> dict:
    db_path = str(db_path or get_main_db())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        max_date = conn.execute(
            "SELECT MAX(trade_date) FROM trade_records WHERE data_quality='real'").fetchone()[0]
        if not max_date:
            return {"error": "no real trades"}
        start = (datetime.strptime(max_date, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        trades = load_trades(conn, start)
        day_results = scan_window(trades, days)
        growth = lessons_growth(conn)
    finally:
        conn.close()

    now_et = datetime.now(ET)
    gate_log = apply_lesson_gates(db_path, day_results, now_et) if write else \
        {label: {"skipped": "no-write"} for label in LABELS}

    latest_day = max(day_results) if day_results else None
    label_totals = {
        label: sum(len(r[label]["violation"]) for r in day_results.values())
        for label in LABELS
    }
    return {
        "generated_at_et": now_et.isoformat(),
        "db": db_path,
        "caliber": {"window_days": days, "window_start": start, "window_end": max_date,
                    "filter": "data_quality='real'", "money": "pnl_net→realized_pnl 回退"},
        "latest_day": latest_day,
        "label_totals_window": label_totals,
        "daily": {d: {"n_trades": r["n_trades"],
                      "CHASE_ENTRY": len(r["CHASE_ENTRY"]["violation"]),
                      "OVERSIZED_RISK": len(r["OVERSIZED_RISK"]["violation"]),
                      "CLUSTER_BET": len(r["CLUSTER_BET"]["violation"])}
                  for d, r in day_results.items()},
        "latest_day_detail": day_results.get(latest_day) if latest_day else None,
        "lesson_gates": gate_log,
        "lessons_growth": growth,
        "write_applied": write,
    }


def render_text(rep: dict) -> str:
    L = []
    L.append("═══ 批次0 违例扫描 (DEB-20260904 §10.2, record-only) ═══")
    c = rep["caliber"]
    L.append(f"口径: 窗口 {c['window_start']}→{c['window_end']} ({c['window_days']}d) | "
             f"{c['filter']} | 金额={c['money']}")
    ld = rep["latest_day"]
    tot = rep["label_totals_window"]
    L.append(f"最新ET日 {ld} | 窗口三标签合计: "
             + " / ".join(f"{k}={tot[k]}" for k in LABELS))
    if rep.get("latest_day_detail"):
        d = rep["latest_day_detail"]
        L.append(f"  当日: n_trades={d['n_trades']} "
                 f"CHASE={len(d['CHASE_ENTRY']['violation'])}(带cap {d['CHASE_ENTRY']['ok'] + len(d['CHASE_ENTRY']['violation'])}"
                 f"/无cap {d['CHASE_ENTRY']['no_cap']}) "
                 f"OVERSIZED={len(d['OVERSIZED_RISK']['violation'])}(ATR缺失 {d['OVERSIZED_RISK']['atr_null']}"
                 f"/中位 ${d['OVERSIZED_RISK']['day_median_usd']}) "
                 f"CLUSTER={len(d['CLUSTER_BET']['violation'])}")
        for v in d["CHASE_ENTRY"]["violation"]:
            L.append(f"    [CHASE] {v['symbol']} {v['direction']} fill={v['entry_price']} "
                     f"cap={v['chase_cap']} pnl_net={v['pnl_net']}")
        for v in d["OVERSIZED_RISK"]["violation"]:
            L.append(f"    [OVERSIZED] {v['symbol']} {v['direction']} risk=${v['risk_usd']} "
                     f"(中位${v['day_median_usd']}) pnl_net={v['pnl_net']}")
        for v in d["CLUSTER_BET"]["violation"]:
            L.append(f"    [CLUSTER] {v['symbol']} {v['direction']} "
                     f"并发={v['concurrent_at_entry']} pnl_net={v['pnl_net']}")
    L.append("每日计数 (ET日 | n | CHASE/OVERSIZED/CLUSTER):")
    for day, row in sorted(rep["daily"].items()):
        L.append(f"  {day} | {row['n_trades']:>2} | {row['CHASE_ENTRY']}/{row['OVERSIZED_RISK']}/{row['CLUSTER_BET']}")
    L.append("教训复发段 (violation_scan 规范行, 三闸状态):")
    L.append("  [cost_since 口径: 正=违例笔净烧钱, 负=违例笔净赚 (矛盾信号, 排序按|cost|上浮);"
             " 单真源: violation-taxonomy-batch0 cost_since 行]")
    for label, g in rep["lesson_gates"].items():
        if g.get("skipped"):
            L.append(f"  {label}: [干跑未写库]")
            continue
        if "status_after" not in g:
            L.append(f"  {label}: 窗口内无违例, 无规范行")
            continue
        flags = "".join(f" <{k}>" for k in ("created", "promoted", "retired", "revived") if g.get(k))
        cost = g.get('cost_since') or 0.0
        # 符号语义注记 (2026-09-05, kimi 建议): cost_since=−Σpnl_net,
        # 正=违例笔净亏 (真烧钱), 负=复发但违例笔净盈利 (矛盾信号, 防读者以为抓错对象)
        if cost > 0:
            sem = "  ← 正成本=违例笔累计净亏 (真烧钱)"
        elif cost < 0:
            sem = "  ← ⚠ 复发但违例笔累计净赚 (矛盾信号: 该标签在窗口内不被自身pnl支持, 排序仍按|cost|上浮)"
        else:
            sem = ""
        L.append(f"  {label}: {g['status_after']} n={g.get('recurrence_count')} "
                 f"cost=${cost:+.2f} rank_key={g.get('rank_key')} "
                 f"last={g.get('last_triggered_at','')[:10]} "
                 f"本次+{g.get('added_recurrence',0)}笔/${g.get('added_cost',0):+.2f}{flags}{sem}")
    gr = rep["lessons_growth"]
    L.append(f"lessons 污染监控: 总行数={gr['total_rows']} 近7天新增={gr['new_last_7d']} "
             f"前7天新增={gr['new_prev_7d']} Δ={gr['growth_delta']}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--db", default=None, help="覆盖 DB 路径 (默认 get_main_db 路由)")
    ap.add_argument("--no-write", action="store_true", help="干跑: 不写 lessons 闸")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    rep = run_scan(db_path=args.db, days=args.days, write=not args.no_write)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, default=str))
    else:
        print(render_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
