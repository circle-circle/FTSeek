#!/usr/bin/env python3
"""盘后出场影子监控 (2026-08-27, 用户动议·record-only, 零下单).

动议演化 (同日两次澄清): 初始"盘后有机会盈利就走" → 修正为"水下隔夜仓在盘后/夜盘
延续, 留足机会和时间净盈利乃至提前获得TP" (持有修复优先, 非见好就收).

回放+修复路径分析裁决 (scripts/ah_exit_replay.py n=12 + 14夜分析, 2026-08-27):
  - 静态TP/SL挂盘后 0/12 触发 (死单, 排除)
  - 水下入夜11夜: 盘后触及entry 18% (全是横盘蹭线) / 触及净盈利垫 0 / 触及TP 0
    → 盘后是惯性时段非修复时段, 水下仓修复引擎=次日RTH (ORCL 8/25夜-0.43%→次晨+2.8%)
  - 深红≥1%收盘的夜 5/6 是下跌中继 (RED_CUT_1PCT 臂 +251$/p=0.219 样本内推导)

影子臂结构 (意图对齐):
  red_cut_1pct = 唯一决策臂 (防跌: 深红≥1%收盘割), 触发构造 would_be_order
    (manual_order_service.close_position 契约 LIMIT+fill_outside_rth) 不发单;
  touch_entry / touch_be / touch_tp = 观测臂 (record-only): 每夜记录盘后是否触及
    entry(修复) / entry±0.6%(净盈利垫) / TP(提前TP, 事件夜期权). 结算时对触及夜算
    "触及即锁(限价线价) vs 实际延续到exit" 反事实 delta — 用数据回答"修复了要不要抓"
    与"事件夜提前TP值多少". 汇总按入夜水下/水上分组.

时序: AFTER_HOURS 19:50+ 实时预评 (live_preliminary); OVERNIGHT 20:05+ 完结终评
  (升级同夜行); 历史夜由缓存幂等回填.
数据: Futu request_history_kline K_1M extended_time=True (盘后历史 2026-08-27 验证可达).
  TP 经 trade_records.decision_id → playbook_instructions.take_profit 关联.
幂等: (symbol, entry_time[:19], night) 去重, 重启不丢.
回滚: playbook.ah_exit_shadow_enabled=false + 重启 (零执行路径改动, 不碰订单).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.utils.timezone import now_et  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
SHADOW = REPO / "data" / "shadow" / "ah_exit_shadow.jsonl"
SUMMARY = REPO / "data" / "shadow" / "ah_exit_shadow_summary.json"
CACHE = REPO / "data" / "replay_1m_ah"
CACHE.mkdir(parents=True, exist_ok=True)

BE_PAD = 0.6      # touch_be 线 / green 武装线 = 生产保本垫 (fee 0.4 + slip 0.2)
RED_PCT = 1.0     # red_cut_1pct 深红线 (回放样本内推导, 见 ah_exit_replay docstring)
AH_SLIP_PCT = 0.2  # 影子出场滑点 (生产 slip_cushion_pct 口径)
LIVE_GATE = "19:50"   # AFTER_HOURS 实时预评门槛 (AH 20:00 收)
COMPLETE_GATE = "20:05"  # 夜完结门槛 (缓存可落盘)
RULES = ("red_cut_1pct",)          # 决策臂 (would_be_order / 首触生效)
OBS_ARMS = ("touch_entry", "touch_be", "touch_tp")  # 观测臂 (每夜独立反事实)

logger = logging.getLogger("ftseek.ah_exit_shadow")

# 回声抑制 (2026-08-28): 调度器 2min tick 每轮都调 run_check, 数据静止时同一组
#   汇总/观测行会整夜重放 (实测 8/28 晨 99 轮 × 3 行完全相同), 把真事件 (⚡/结算)
#   埋进噪声. 按 tag 缓存上次渲染结果: 内容未变 → debug, 变化 → INFO. 变数 (价格/
#   计数) 进 line 本身, 移动即自然重打, 无需状态机. 调度器 _load_script_module 缓存
#   模块 → 常驻进程 tick 间共享本 dict; CLI 单进程首打必 INFO, 人工跑行为零变化.
#   回滚: 删 _echo 并把调用点还原 logger.info.
_ECHO_CACHE: dict = {}


def _echo(tag: str, line: str) -> None:
    if _ECHO_CACHE.get(tag) == line:
        logger.debug("[AH影子] %s (回声抑制)", line)
        return
    _ECHO_CACHE[tag] = line
    logger.info("[AH影子] %s", line)


def _enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.ah_exit_shadow_enabled", True))
    except Exception:
        return True


# ── 数据 ──────────────────────────────────────────────
def load_open_positions() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, direction, size, avg_entry_price, entry_time "
        "FROM open_positions WHERE is_filled=1 AND avg_entry_price > 0").fetchall()
    con.close()
    out = []
    for r in rows:
        et = str(r["entry_time"] or "")
        if len(et) < 19:
            continue
        out.append(dict(symbol=r["symbol"], direction=r["direction"],
                        qty=float(r["size"] or 0), entry=float(r["avg_entry_price"]),
                        entry_time=et, pos_key=(r["symbol"], r["direction"], et[:19])))
    return out


def find_tp(pos_key) -> float | None:
    """持仓的 TP (playbook_instructions 最新行). open_positions 无 TP 元数据,
    经 trade_records(在仓行).decision_id 关联; 缺失则 touch_tp 臂记 None 不阻塞.
    2026-08-31 加固: open_positions 与 trade_records 的 entry_time 偶有秒级错位
    (实测 8/27 US.AVGO :34 vs :33) 致精确秒匹配漏关联, touch_tp 臂失明 →
    首选"该标的方向在仓行"语义关联 (在仓评估语义下恒成立), 秒级匹配降为兜底."""
    sym, drn, et19 = pos_key
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT p.take_profit FROM playbook_instructions p "
        "JOIN trade_records t ON t.decision_id = p.decision_id "
        "WHERE t.symbol=? AND t.direction=? AND t.exit_time IS NULL "
        "AND p.take_profit > 0 ORDER BY p.updated_at DESC LIMIT 1",
        (sym, drn)).fetchone()
    if row is None:
        row = con.execute(
            "SELECT p.take_profit FROM playbook_instructions p "
            "JOIN trade_records t ON t.decision_id = p.decision_id "
            "WHERE t.symbol=? AND t.direction=? AND substr(t.entry_time,1,19)=? "
            "AND p.take_profit > 0 ORDER BY p.updated_at DESC LIMIT 1",
            (sym, drn, et19)).fetchone()
    con.close()
    return float(row["take_profit"]) if row else None


def find_actual_exit(pos_key) -> dict | None:
    """持仓实际平仓行 (trade_records). 无则 None (仍开仓)."""
    sym, drn, et19 = pos_key
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT exit_price, exit_time, pnl_net, realized_pnl, exit_reason FROM trade_records "
        "WHERE symbol=? AND direction=? AND substr(entry_time,1,19)=? "
        "AND exit_time IS NOT NULL AND exit_price > 0 ORDER BY exit_time DESC LIMIT 1",
        (sym, drn, et19)).fetchone()
    con.close()
    return dict(row) if row else None


def fetch_night_ah(qctx, symbol: str, night: str, complete: bool) -> list[dict]:
    """单夜盘后 1M bars (extended_time=True), 存 o/h/l/c (触及判定需要极值).
    完结夜幂等缓存, live 夜不落盘."""
    ck = CACHE / f"{symbol.replace('.', '_')}_{night}_shadow.json"
    if ck.exists():
        try:
            payload = json.loads(ck.read_text())
            if payload.get("complete") and payload.get("schema") == "ohlc":
                return payload.get("bars", [])
        except Exception:
            pass
    from futu import KLType, AuType, RET_OK
    ret, data, page_key = qctx.request_history_kline(
        code=symbol, start=night, end=night, ktype=KLType.K_1M,
        autype=AuType.NONE, max_count=1000, extended_time=True)
    if ret != RET_OK:
        raise RuntimeError(f"history_kline {symbol} {night}: {data}")
    bars = []
    for _, row in data.iterrows():
        ts = str(row["time_key"])
        hhmm = ts[11:16]
        if "16:00" <= hhmm <= "20:00":
            bars.append({"ts": ts, "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]), "c": float(row["close"])})
    if complete:
        ck.write_text(json.dumps({"complete": True, "schema": "ohlc", "bars": bars}))
    return bars


# ── 评估 ──────────────────────────────────────────────
def eval_rules(direction: str, entry: float, tp: float | None, bars: list[dict]) -> dict:
    """2026-08-27 意图对齐重构 (用户: 水下仓延续修复, 留足机会, 净盈利乃至提前TP):
    - red_cut_1pct = 唯一决策臂 (防跌, 回放有支撑), 触发构造 would_be_order
    - touch_entry / touch_be / touch_tp = 观测臂 (record-only 无卖出建议):
      盘后是否触及 entry(修复) / entry±0.6%(净盈利垫) / TP(提前TP, 事件夜期权).
      结算时算 "触及即锁" 反事实, 用数据回答 "修复了要不要抓".
    14夜基线 (2026-08-27 分析): 水下入夜 touch_entry 18% / touch_be 0% / touch_tp 0%.
    """
    long = direction == "LONG"
    o0 = bars[0]["o"]
    best = max(b["h"] for b in bars) if long else min(b["l"] for b in bars)
    worst = min(b["l"] for b in bars) if long else max(b["h"] for b in bars)
    c_end = bars[-1]["c"]

    def _fav(px):
        return (px - entry) / entry * 100 * (1 if long else -1)

    be_line = entry * (1 + BE_PAD / 100) if long else entry * (1 - BE_PAD / 100)
    out = dict(
        in_water_at_open=bool(_fav(o0) < 0),
        ah_best_fav_pct=round(_fav(best), 3),
        ah_worst_fav_pct=round(_fav(worst), 3),
        ah_end_fav_pct=round(_fav(c_end), 3),
        touch_entry=dict(hit=_fav(best) >= 0, line=round(entry, 4)),
        touch_be=dict(hit=_fav(best) >= BE_PAD, line=round(be_line, 4)),
        touch_tp=dict(hit=(tp is not None and ((best >= tp) if long else (best <= tp))),
                      line=None if tp is None else round(tp, 4)),
    )
    # 决策臂: 深红收盘割 (回放 RED_CUT_1PCT, 样本内推导 +251$/p=0.219)
    ah_close = c_end
    deep = entry * (1 - RED_PCT / 100) if long else entry * (1 + RED_PCT / 100)
    would_px = ah_close * (1 - AH_SLIP_PCT / 100) if long else ah_close * (1 + AH_SLIP_PCT / 100)
    out["red_cut_1pct"] = dict(
        triggered=bool((ah_close <= deep) if long else (ah_close >= deep)),
        would_price=round(would_px, 4), line=round(deep, 4))
    return out


def would_be_order(pos: dict, rule: str, would_price: float) -> dict:
    """manual_order_service.close_position 契约载荷 (LIMIT+fill_outside_rth). 只构造不发送."""
    return dict(
        channel="manual_order_service.close_position",
        symbol=pos["symbol"], action="close", close_qty=pos["qty"],
        side="SELL" if pos["direction"] == "LONG" else "BUY",
        order_type="LIMIT", price=round(would_price, 2),
        fill_outside_rth=True, time_in_force="DAY",
        reason=f"ah_exit_shadow:{rule}",
    )


def complete_nights(entry_date: str, today: str, now) -> list[str]:
    """[entry_date..today] 中已完结的夜 (d<today 或 今日且now≥20:05)."""
    d0, d1 = date.fromisoformat(entry_date), date.fromisoformat(today)
    gate = COMPLETE_GATE
    out = []
    while d0 <= d1:
        s = d0.isoformat()
        if s < today or (s == today and now.strftime("%H:%M") >= gate):
            out.append(s)
        d0 += timedelta(days=1)
    return out


# ── 主流程 ──────────────────────────────────────────────
def _load_rows() -> list[dict]:
    if not SHADOW.exists():
        return []
    return [json.loads(l) for l in SHADOW.read_text().splitlines() if l.strip()]


def _save_rows(rows: list[dict]) -> None:
    SHADOW.parent.mkdir(parents=True, exist_ok=True)
    SHADOW.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def run_pass(live: bool = False, evaluate: bool = True) -> dict:
    """评估+结算一遍. live=True 时含今晚实时预评 (AFTER_HOURS 19:50+). 返回统计."""
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    rows = _load_rows()
    stats = dict(evaluated=0, updated=0, settled=0, triggered=0)

    positions = load_open_positions() if evaluate else []
    todo = []  # (pos, night, existing_row|None, is_complete) — 有活才开 Futu 连接
    hhmm = now.strftime("%H:%M")
    for pos in positions:
        entry_date = pos["entry_time"][:10]
        if entry_date > today:
            continue
        nights = complete_nights(entry_date, today, now)
        if live and today not in nights and LIVE_GATE <= hhmm < COMPLETE_GATE:
            nights.append(today)  # 今晚实时预评 (live_preliminary)
        for night in nights:
            key = (pos["symbol"], pos["pos_key"][2], night)
            existing = next((r for r in rows
                             if (r["symbol"], r["entry_time"][:19], r["night"]) == key), None)
            if existing and existing.get("eval_stage") == "final":
                continue
            is_complete = night < today or hhmm >= COMPLETE_GATE
            todo.append((pos, night, existing, is_complete))

    if todo:
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
        try:
            for pos, night, existing, is_complete in todo:
                try:
                    bars = fetch_night_ah(qctx, pos["symbol"], night, complete=is_complete)
                except Exception as e:
                    logger.warning("[AH影子] %s %s 拉取失败 (留待下次): %s",
                                   pos["symbol"], night, e)
                    continue
                if not bars:
                    # 非交易日/无盘后数据: 记空行防反复重拉
                    if not existing:
                        rows.append(dict(night=night, symbol=pos["symbol"],
                                         direction=pos["direction"],
                                         entry_time=pos["entry_time"],
                                         entry_price=pos["entry"], qty=pos["qty"],
                                         ah_close=None, eval_stage="final",
                                         rules={r: dict(triggered=False) for r in RULES},
                                         note="no_ah_bars", settled=False))
                        stats["evaluated"] += 1
                    continue
                ah_close = bars[-1]["c"]
                tp = find_tp(pos["pos_key"])
                rules = eval_rules(pos["direction"], pos["entry"], tp, bars)
                trig_rule = next((r for r in RULES if rules[r]["triggered"]), None)
                row = dict(night=night, symbol=pos["symbol"], direction=pos["direction"],
                           entry_time=pos["entry_time"], entry_price=pos["entry"],
                           qty=pos["qty"], ah_close=ah_close, tp=tp,
                           ah_last_bar=bars[-1]["ts"],
                           eval_stage="final" if is_complete else "live_preliminary",
                           rules=rules, settled=False)
                if trig_rule:
                    row["would_be_order"] = would_be_order(pos, trig_rule,
                                                           rules[trig_rule]["would_price"])
                    stats["triggered"] += 1
                    _echo(f"arm:{pos['symbol']}:{night}:{trig_rule}",
                          f"⚡ {pos['symbol']} {night}夜 触发 {trig_rule} "
                          f"@ ah_close={ah_close:.2f} (载荷已构造, record-only)")
                for arm in OBS_ARMS:
                    if rules.get(arm, {}).get("hit"):
                        _echo(f"obs:{pos['symbol']}:{night}:{arm}",
                              f"👁 {pos['symbol']} {night}夜 观测臂 {arm} 触及 "
                              f"(best={rules.get('ah_best_fav_pct', 0):+.2f}%, "
                              f"in_water={rules.get('in_water_at_open')})")
                if existing:
                    rows[rows.index(existing)] = row
                    stats["updated"] += 1
                else:
                    rows.append(row)
                    stats["evaluated"] += 1
        finally:
            qctx.close()
    else:
        logger.debug("[AH影子] 无待评估夜")

    # ── 结算: 持仓实际平仓 → 配对 delta ──
    pos_keys = {(r["symbol"], r["direction"], r["entry_time"][:19])
                for r in rows if not r.get("settled")}
    for pk in pos_keys:
        actual = find_actual_exit(pk)
        if not actual:
            continue
        actual_exit = float(actual["exit_price"])
        prows = sorted((r for r in rows if not r.get("settled")
                        and (r["symbol"], r["direction"], r["entry_time"][:19]) == pk),
                       key=lambda r: r["night"])
        sgn = 1.0 if pk[1] == "LONG" else -1.0
        first_hit = {r: False for r in RULES}
        for r in prows:
            if r.get("ah_close") is None:
                r["settled"] = True
                continue
            for rule in RULES:
                trig = bool(r.get("rules", {}).get(rule, {}).get("triggered"))
                if trig and not first_hit[rule]:
                    first_hit[rule] = True
                    wp = float(r["rules"][rule]["would_price"])
                    r[f"delta_{rule}"] = round(sgn * (wp - actual_exit) * r["qty"], 2)
                else:
                    r[f"delta_{rule}"] = 0.0
                    if trig:
                        r[f"{rule}_superseded"] = True
            # 观测臂反事实: 该夜触及线 → "触及即锁"(限价) vs 实际延续到 exit.
            # 每夜独立样本 (那晚锁 vs 实际), 无首触消歧 — 回答"修复了要不要抓".
            for arm in OBS_ARMS:
                line = r.get("rules", {}).get(arm, {}).get("line")
                hit = bool(r.get("rules", {}).get(arm, {}).get("hit"))
                if hit and line:
                    r[f"delta_{arm}"] = round(
                        sgn * (float(line) - actual_exit) * r["qty"], 2)
                else:
                    r[f"delta_{arm}"] = None
            r["actual_exit"] = actual_exit
            r["actual_exit_reason"] = actual["exit_reason"]
            r["settled"] = True
            stats["settled"] += 1

    _save_rows(rows)
    _write_summary(rows)
    return stats


def _write_summary(rows: list[dict]) -> None:
    from ah_exit_replay import sign_test_p
    settled = [r for r in rows if r.get("settled") and r.get("ah_close") is not None]
    summary = dict(updated_at=now_et().isoformat(), n_nights=len(rows),
                   n_settled=len(settled),
                   criteria="n>=15 + 符号置换双侧p<0.05 + 净差>0 才议灰度 (DEB-20260805格式)",
                   rules={}, obs_arms={})
    for rule in RULES:
        deltas = [r.get(f"delta_{rule}") or 0.0 for r in settled]
        # 触发计数含未结算行 (区分"已触发待结算"与"从未触发"); Σdelta/胜负只用已结算
        trig_all = [r for r in rows if r.get("ah_close") is not None
                    and r.get("rules", {}).get(rule, {}).get("triggered")
                    and not r.get(f"{rule}_superseded")]
        trig_settled = [r for r in trig_all if r.get("settled")]
        p, k, m = sign_test_p([r.get(f"delta_{rule}") or 0.0 for r in trig_settled])
        summary["rules"][rule] = dict(
            triggered=len(trig_all), triggered_settled=len(trig_settled),
            sum_delta=round(sum(deltas), 2),
            mean_delta=round(sum(deltas) / len(deltas), 2) if deltas else 0.0,
            p_sign=p, note="防跌决策臂(深红≥1%收盘割, 回放样本内推导)",
        )
    # 观测臂: 触及率 + 触及即锁反事实 (按入夜水下/水上分组 — 用户意图: 水下仓修复机会)
    for arm in OBS_ARMS:
        for grp in ("underwater", "above"):
            grp_rows = [r for r in rows if r.get("ah_close") is not None
                        and r.get("rules", {}).get("in_water_at_open") == (grp == "underwater")]
            hits = [r for r in grp_rows if r.get("rules", {}).get(arm, {}).get("hit")]
            hit_settled = [r for r in hits if r.get("settled")
                           and r.get(f"delta_{arm}") is not None]
            ds = [r[f"delta_{arm}"] for r in hit_settled]
            p, k, m = sign_test_p(ds) if ds else (None, 0, 0)
            summary["obs_arms"][f"{arm}:{grp}"] = dict(
                nights=len(grp_rows), touched=len(hits),
                touch_rate=round(100 * len(hits) / len(grp_rows), 1) if grp_rows else None,
                lock_vs_hold=[round(sum(ds), 2), round(sum(ds) / len(ds), 2) if ds else 0.0],
                p_sign=p,
            )
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    for rule in RULES:
        s = summary["rules"][rule]
        _echo(f"summary:{rule}",
              f"{rule}: 触发{s['triggered']}/{summary['n_settled']} "
              f"Σdelta={s['sum_delta']:+.0f} p={s['p_sign']}")
    for key, s in summary["obs_arms"].items():
        if s["touched"]:
            _echo(f"summary:obs:{key}",
                  f"观测 {key}: 触及{s['touched']}/{s['nights']}夜"
                  f"({s['touch_rate'] or 0:.0f}%) 锁vs持={s['lock_vs_hold'][0]:+.0f}")


def run_check(scheduler=None) -> int:
    """调度器钩子 (AFTER_HOURS / OVERNIGHT, 2min tick 内自幂等)."""
    if not _enabled():
        return 0
    try:
        now = now_et()
        hhmm = now.strftime("%H:%M")
        live = LIVE_GATE <= hhmm < COMPLETE_GATE  # AFTER_HOURS 实时预评窗
        stats = run_pass(live=live)
        if stats["evaluated"] or stats["settled"] or stats["triggered"]:
            _echo("stats",
                  f"评估{stats['evaluated']} 更新{stats['updated']} "
                  f"结算{stats['settled']} 触发{stats['triggered']}")
        return stats["settled"]
    except Exception as e:
        logger.warning("[AH影子] 异常 (已隔离): %s", e)
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle-only", action="store_true", help="只结算不评估新夜")
    args = ap.parse_args()
    stats = run_pass(live=False, evaluate=not args.settle_only)
    print(stats)
