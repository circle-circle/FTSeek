#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中微观结构前向采集 (2026-10-31, record-only, 零交易行为变更).

背景 (先查台账, 不重复造影子):
  同轴已有 `scripts/archive/orderbook_s2_capture.py` (2026-08-04→08-18, 4 只 × 30min):
  真值 depth_imbalance_10 std=0.325, 对 ~30/60min 前瞻收益 8 个 IC 检验 **0/8 显著**,
  pooled Fisher-z IC = +0.046 (30m) / +0.060 (60m), 而 **MDE = 0.22** ⇒
  裁决为 inconclusive (功效不足, 不是关门证据), 结论原文: "若未来要裁决需更高频采集攒功效"。
  本脚本 = 该结论的直接执行者: 同样 record-only, 但把**标的数 × 时段数**抬到 MDE≈0.03 档。

设计:
  - 独立 OpenQuoteContext (同 S2: 探活+重试+订阅+unsubscribe+close), 不挂主进程, 不动 config 的 stock_sub_types
  - 标的 = 当日有指令/持仓的票 (触发人群, 与预注册判据同人群) ∪ 固定核心名单, 上限 40
  - 每个 ET :00 / :30 采样一次: 10 档盘口 (OBI/spread/microprice) + 最近逐笔 (tick 主动买卖差) + snapshot 免费字段
  - 落 data/shadow/orderflow_forward.jsonl (schema v1), 按 (date, symbol, slot) 幂等去重
  - 前向收益不在此算 (settle 在外, 见 --settle; 避免采集器依赖 bar 缓存完整性)

Cron (self-gating, CST 注释 ET 等价):
  0,30 20-23,0-5 * * 1-5   # ET RTH 09:30-16:00; EDT=21:30-04:00 CST / EST=22:30-05:00 CST ⇒ 窗口取并集, 脚本内按 ET 自门
回滚: 删 cron 两行 + 删 data/shadow/orderflow_forward.jsonl (纯影子, 无下游消费者)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "shadow" / "orderflow_forward.jsonl"
LOG = ROOT / "logs" / "orderflow_forward_capture.log"
DB = ROOT / "data" / "ftseek_us.db"
CORE = ["US.SPY", "US.QQQ", "US.NVDA", "US.MSFT", "US.META", "US.TSLA"]
MAX_SYMBOLS = 40

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [obf] %(levelname)s: %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("ftseek.orderflow.forward_capture")


def _make_ctx(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    ok = sock.connect_ex((host, port)) == 0
    sock.close()
    if not ok:
        return None
    try:
        import futu.common.ft_logger as _ftl
        _ftl.logger.file_level = logging.WARNING
        _ftl.logger.console_level = logging.WARNING
    except Exception as e:  # noqa: BLE001 — 留痕
        logger.warning("ft_logger 静音失败 (富途库日志将更吵): %s", e, exc_info=True)
    from futu import OpenQuoteContext, SysConfig
    SysConfig.enable_proto_encrypt(False)
    last = None
    for _ in range(3):
        try:
            ctx = OpenQuoteContext(host=host, port=port)
            ret, _st = ctx.get_global_state()
            if ret == 0:
                return ctx
            last = f"get_global_state ret={ret}"
            try:
                ctx.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("close 失败 (OpenD 上下文未干净关闭): %s", e, exc_info=True)
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2)
    logger.warning("OpenD %s:%s 连接失败: %s", host, port, last)
    return None


def _universe():
    """触发人群: 当日非 SKIP 指令 ∪ 持仓; 缺失则回落核心名单."""
    syms, ctx_info = [], {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        from app.utils.timezone import now_et
        today = now_et().date().isoformat()
        rows = con.execute(
            "SELECT symbol, direction, decision_id, id FROM playbook_instructions "
            "WHERE date=? AND direction IN ('BUY','SELL_SHORT') ORDER BY id", (today,)).fetchall()
        for r in rows:
            s = r["symbol"]
            if s not in ctx_info:
                syms.append(s)
            ctx_info[s] = {"direction": r["direction"], "decision_id": r["decision_id"],
                           "inst_id": r["id"]}
        try:
            pos = con.execute("SELECT symbol FROM open_positions").fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("open_positions 读取失败: %s", e, exc_info=True)
            pos = []
        for r in pos:
            if r["symbol"] not in ctx_info:
                syms.append(r["symbol"])
        con.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("universe 从 DB 读取失败 (回落核心名单): %s", e, exc_info=True)
    for s in CORE:
        if s not in ctx_info:
            syms.append(s)
    seen, out = set(), []
    for s in syms:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    if len(out) > MAX_SYMBOLS:
        logger.info("universe 截断 %d → %d (指令优先, 核心名单取前若干)", len(out), MAX_SYMBOLS)
        out = out[:MAX_SYMBOLS]
    return out, ctx_info


def _book(ctx, sym):
    ret, data = ctx.get_order_book(sym, num=10)
    if ret != 0 or data is None:
        return None, f"ret={ret}"
    try:
        if isinstance(data, dict):
            bids = list(data.get("Bid") or [])[:10]
            asks = list(data.get("Ask") or [])[:10]
        else:
            row = data.iloc[0]
            bids = list(row.get("Bid") or [])[:10]
            asks = list(row.get("Ask") or [])[:10]
        bp = [float(x[0]) for x in bids]
        bv = [float(x[1]) for x in bids]
        ap = [float(x[0]) for x in asks]
        av = [float(x[1]) for x in asks]
    except Exception as e:  # noqa: BLE001
        return None, f"parse:{e}"
    if not bp or not ap or bp[0] <= 0 or ap[0] <= 0:
        return None, "empty"
    b1, a1 = bp[0], ap[0]
    mid = (b1 + a1) / 2
    bt, at = sum(bv), sum(av)
    b5, a5 = sum(bv[:5]), sum(av[:5])
    # 量加权 microprice (用前 5 档, 卖压大则 microprice 靠买侧)
    sw_b = sum(bv[:5]) + sum(av[:5])
    micro = ((sum(p * v for p, v in zip(bp[:5], bv[:5])) +
              sum(p * v for p, v in zip(ap[:5], av[:5]))) / sw_b) if sw_b > 0 else mid
    return {
        "bid1": b1, "ask1": a1, "mid": mid, "micro": micro,
        "spread_bps": (a1 - b1) / mid * 10000 if mid > 0 else None,
        "obi10": (bt - at) / (bt + at) if (bt + at) > 0 else None,
        "obi5": (b5 - a5) / (b5 + a5) if (b5 + a5) > 0 else None,
        "bid_depth10": bt, "ask_depth10": at,
        "micro_dev_bps": (micro - mid) / mid * 10000 if mid > 0 else None,
    }, None


def _ticks(ctx, sym, num=40):
    """最近逐笔 → tick rule 主动买卖差 (uptick=主动买, downtick=主动卖)."""
    ret, data = ctx.get_rt_ticker(sym, num=num)
    if ret != 0 or data is None:
        return None, f"ret={ret}"
    try:
        if hasattr(data, "to_dict"):
            recs = data.to_dict("records")
        else:
            recs = list(data)
        if not recs:
            return None, "empty"
        key_t = "time" if "time" in recs[0] else ("timestamp" if "timestamp" in recs[0] else None)
        recs.sort(key=lambda r: str(r.get(key_t) or ""))
        buy = sell = 0.0
        last_p = None
        n_up = n_dn = 0
        for r in recs:
            p = float(r.get("price") or 0)
            v = float(r.get("volume") or 0)
            if p <= 0 or v <= 0:
                continue
            if last_p is not None:
                if p > last_p:
                    buy += v
                    n_up += 1
                elif p < last_p:
                    sell += v
                    n_dn += 1
            last_p = p
        tot = buy + sell
        return {
            "tick_n": len(recs), "tick_buy_vol": buy, "tick_sell_vol": sell,
            "tick_delta": (buy - sell) / tot if tot > 0 else None,
            "tick_up_n": n_up, "tick_dn_n": n_dn,
            "tick_last": last_p,
            "tick_span": str(recs[0].get(key_t) or "")[-8:] if key_t else None,
        }, None
    except Exception as e:  # noqa: BLE001
        return None, f"parse:{e}"


def _snapshot(ctx, sym):
    ret, df = ctx.get_market_snapshot([sym])
    if ret != 0 or df is None or getattr(df, "empty", True):
        return {}, f"ret={ret}"
    r = df.iloc[0]
    out = {}
    for k in ("last_price", "volume", "turnover", "bid_ask_ratio", "volume_ratio",
              "amplitude", "turnover_rate", "avg_price", "high_price", "low_price",
              "open_price", "prev_close_price"):
        try:
            v = r.get(k)
            out[f"snap_{k}"] = float(v) if v is not None and v == v else None
        except (TypeError, ValueError):
            out[f"snap_{k}"] = None
    return out, None


def capture_once(argv, force=False):
    from app.utils.timezone import now_et
    now = now_et()
    hhmm = now.strftime("%H:%M")
    if now.weekday() >= 5:
        logger.info("非交易日 (%s), 跳过", now.strftime("%a"))
        return 0
    if not ("09:30" <= hhmm <= "16:00"):
        if not force:
            logger.info("非 RTH (%s ET), 跳过", hhmm)
            return 0
        logger.warning("force: 跳过 RTH 自门 (%s ET) — 仅调试/铅管验证, 数据不得进裁决", hhmm)
    if hhmm[-2:] not in ("00", "30") and not force:
        logger.info("非采样点 (%s ET), 跳过", hhmm)
        return 0
    slot = hhmm if hhmm <= "16:00" else "16:00"
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    syms, ctx_info = _universe()
    if not syms:
        logger.warning("universe 为空, 本轮跳过")
        return 0
    ctx = _make_ctx(host, port)
    if ctx is None:
        return 0
    rows, n_book_fail, n_tick_fail, n_snap_fail = [], 0, 0, 0
    try:
        from futu import SubType
        ret, msg = ctx.subscribe(syms, [SubType.ORDER_BOOK, SubType.TICKER], subscribe_push=False)
        if ret != 0:
            logger.warning("订阅失败 (%d 只): %s", len(syms), msg)
            return 0
        time.sleep(3)
        for sym in syms:
            book, err_b = _book(ctx, sym)
            if book is None:
                n_book_fail += 1
                logger.warning("%s 盘口失败: %s", sym, err_b)
                continue
            ticks, err_t = _ticks(ctx, sym)
            if ticks is None:
                n_tick_fail += 1
            snap, err_s = _snapshot(ctx, sym)
            if not snap:
                n_snap_fail += 1
            info = ctx_info.get(sym, {})
            rows.append({
                "ts": now.isoformat(timespec="seconds"), "date": now.date().isoformat(),
                "slot": slot, "schema": "v1", "symbol": sym,
                "direction": info.get("direction"), "decision_id": info.get("decision_id"),
                "inst_id": info.get("inst_id"),
                "px_last": (snap or {}).get("snap_last_price"),
                **book, **(ticks or {}), **snap,
            })
        try:
            ctx.unsubscribe(syms, [SubType.ORDER_BOOK, SubType.TICKER])
        except Exception as e:  # noqa: BLE001
            logger.warning("unsubscribe 失败 (退订未确认; OpenD 侧可能残留订阅): %s", e, exc_info=True)
    finally:
        try:
            ctx.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("close 失败 (OpenD 上下文未干净关闭): %s", e, exc_info=True)

    if rows:
        # 幂等: 同 (date, symbol, slot) 已存在则跳过 (重跑/cron 重叠安全)
        seen = set()
        if OUT.exists():
            for line in OUT.open(encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                seen.add((r.get("date"), r.get("symbol"), r.get("slot")))
        new = [r for r in rows if (r["date"], r["symbol"], r["slot"]) not in seen]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        logger.info("slot %s: 落盘 %d/%d (去重跳过 %d) | 盘口失败 %d / 逐笔失败 %d / snapshot失败 %d",
                    slot, len(new), len(syms), len(rows) - len(new),
                    n_book_fail, n_tick_fail, n_snap_fail)
    return 0


def settle(days=5):
    """结算: 用本地 1m bar 算各前向窗口收益 + 逐特征 IC/命中率. 只读.  """
    import math
    import statistics
    from collections import defaultdict
    bars = {}
    for d in (ROOT / "data" / "replay_bars_1m", ROOT / "data" / "replay_bars_1m_extra"):
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.name.endswith(".json"):
                continue
            sym = f.name[:-5].replace("_", ".", 1)
            if sym in bars:
                continue
            try:
                j = json.loads(f.read_text())
            except Exception as e:  # noqa: BLE001
                logger.warning("bar 读失败 %s: %s", f.name, e, exc_info=True)
                continue
            byday = defaultdict(dict)
            for r in j:
                byday[r[0][:10]][r[0][11:16]] = float(r[4])
            bars[sym] = byday
    if not OUT.exists():
        msg = "NO_SAMPLE_YET (载体已装, 等首个 RTH 采样点)"
        print(f"[status] {msg}")
        p0 = ROOT / "data" / "reports" / "orderflow_forward_settle_latest.json"
        p0.parent.mkdir(parents=True, exist_ok=True)
        p0.write_text(json.dumps({"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
                                  "status": msg, "n_rows": 0, "results": {}},
                                 ensure_ascii=False, indent=1))
        return 0
    rows = []
    n_bad_json = 0
    for line in OUT.open(encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001 — 计数留痕
            n_bad_json += 1
    # 三态自检 (TOO_EARLY / LIVE / STALE) — 防"零行"被读成"采集死"或"已生效"
    from app.utils.timezone import now_et
    _today = now_et().date().isoformat()
    _days = sorted({r.get("date") for r in rows if r.get("date")})
    if not rows:
        status = "NO_SAMPLE_YET (载体已装, 等首个 RTH 采样点)"
    elif _days and _days[-1] >= _today:
        status = "LIVE (今日已有落盘)"
    else:
        status = f"STALE? 末次日={_days[-1] if _days else None} vs 今日ET={_today} (若非交易日/未到采样点属正常)"
    print(f"[status] {status} | rows={len(rows)} bad_json={n_bad_json} days={len(_days)}")
    def fwd(sym, day, slot, minutes):
        hm = bars.get(sym, {}).get(day)
        if not hm or slot not in hm:
            return None
        h, m = slot.split(":")
        from datetime import datetime as _dt, timedelta as _td
        t = _dt(2000, 1, 1, int(h), int(m)) + _td(minutes=minutes)
        tgt = t.strftime("%H:%M")
        keys = sorted(k for k in hm if k >= tgt)
        return (hm[keys[0]] / hm[slot] - 1) if keys and hm[slot] > 0 else None
    feats = ("obi10", "obi5", "spread_bps", "micro_dev_bps", "tick_delta")
    out = {}
    for minutes in (15, 30, 60):
        for f in feats:
            pairs = []
            for r in rows:
                y = fwd(r["symbol"], r["date"], r["slot"], minutes)
                x = r.get(f)
                if y is None or x is None:
                    continue
                pairs.append((x, y, r["date"]))
            if len(pairs) < 30:
                continue
            def rank_corr(ps):
                n = len(ps)
                ox = sorted(range(n), key=lambda i: ps[i][0])
                oy = sorted(range(n), key=lambda i: ps[i][1])
                rx = [0] * n
                ry = [0] * n
                for rk, i in enumerate(ox):
                    rx[i] = rk
                for rk, i in enumerate(oy):
                    ry[i] = rk
                mx, my = sum(rx) / n, sum(ry) / n
                num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
                den = math.sqrt(sum((v - mx) ** 2 for v in rx) * sum((v - my) ** 2 for v in ry))
                return num / den if den else None
            ic = rank_corr(pairs)
            hit = sum(1 for a, b, _ in pairs if (a > 0) == (b > 0)) / len(pairs)
            # 日聚类 t
            by = defaultdict(list)
            for a, b, d in pairs:
                by[d].append((1 if a > 0 else -1) * b)
            tv = None
            if len(by) >= 5:
                vals = [sum(v) / len(v) for v in by.values()]
                sd = statistics.pstdev(vals)
                tv = (statistics.mean(vals) / (sd / math.sqrt(len(vals)))) if sd else None
            out[f"{f}@{minutes}m"] = {"n": len(pairs), "ic": round(ic, 4) if ic else None,
                                      "hit": round(hit, 4), "day_t": round(tv, 2) if tv else None,
                                      "n_days": len(by)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    p = ROOT / "data" / "reports" / "orderflow_forward_settle_latest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "status": status, "n_rows": len(rows), "n_bad_json": n_bad_json,
                             "days": len(_days), "last_date": (_days[-1] if _days else None),
                             "results": out}, ensure_ascii=False, indent=1))
    print(f"→ {p}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", action="store_true", help="只结算已采集数据")
    ap.add_argument("--force", action="store_true", help="忽略 ET 时段自门 (调试用)")
    ap.add_argument("--out", default=None, help="覆盖落盘路径 (调试用, 防污染真影子)")
    args = ap.parse_args()
    global OUT
    if args.out:
        OUT = Path(args.out)
        logger.warning("落盘路径被覆盖为 %s (调试模式)", OUT)
    if args.settle:
        return settle()
    return capture_once(sys.argv, force=args.force)


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)