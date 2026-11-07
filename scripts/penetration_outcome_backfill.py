#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""穿透影子离场后结局回填 (2026-09-03, DEB-20260903 议题二后续, 用户裁"A").

口径: 对 data/shadow/penetration_shadow.jsonl 中 type=penetration 事件,
用当日 1 分 bar (data/replay_bars/US_<SYM>_<et_date>.json, 端标) 回填【离场后】结局:
  - recovered_1m/2m/3m/5m/10m: 离场后 N 分钟内价格是否回摆到净保本地板
    (LONG: bar high >= floor; SHORT: bar low <= floor) — True = V型反转
    (若当时挂地板限价单而非 marketable limit, 该分钟内可成交于地板价)
  - continued_bps_10m: 离场后 10 分钟内最劣延续幅度 (负=续跌, 正=回摆方向)
  - cf_floor_fill_saved_bps: 若 10m 内回摆, "持有挂地板限价" vs 实盘 marketable
    limit 的成交价差 (正=持有更优)。无 limit_px 的旧行为 None。
  - n_bars_scanned < 10 → partial=True (临近收盘, 口径内可见性不足)
幂等: 已有 outcome 的 event_id 跳过。默认处理全部未回填事件 (自愈漏跑),
--date 可限定单日。record-only 生态, 不碰任何交易路径。

用法: python3 scripts/penetration_outcome_backfill.py [--date YYYY-MM-DD]
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BAR_DIR = REPO / "data" / "replay_bars"
SHADOW = REPO / "data" / "shadow" / "penetration_shadow.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "penetration_outcome_latest.json"
SCAN_N = 10


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_events():
    events, done, bad = [], set(), 0
    if not SHADOW.exists():
        return events, done
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = _loads(line)
        if r is None:
            bad += 1
            continue
        if r.get("type") == "penetration":
            events.append(r)
        elif r.get("type") == "outcome" and r.get("event_id"):
            done.add(r["event_id"])
    if bad:
        print(f"WARNING: penetration_shadow.jsonl 有 {bad} 行解析失败已跳过")
    return events, done


def load_bars(symbol: str, date: str):
    code = str(symbol or "")
    if not code.startswith("US."):
        code = f"US.{code}"
    p = BAR_DIR / f"{code.replace('.', '_')}_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def label_one(ev, bars):
    """返回 outcome dict (不含 event_id/type); bars 为当日全量 1 分 bar。"""
    direction = str(ev.get("direction") or "LONG").upper()
    is_long = direction == "LONG"
    floor = float(ev.get("floor_px") or 0)
    trig = float(ev.get("trigger_price") or 0)
    entry = float(ev.get("entry") or 0)
    if floor <= 0 or trig <= 0 or entry <= 0:
        return {"status": "bad_event_fields"}
    hm = str(ev.get("et_hm") or "")
    # bar 为端标 (标签=分钟结束时刻): 标签 hm+1min 的 bar 覆盖触发分钟本身, 其 high/low
    # 大概率先于触发时刻 (DELL 9/2: 触发 09:48:54 落在 bar'09:49', 该 bar high 479.996 为
    # 触发前价格 → 会制造假 V-recovery 标签). 故窗口从触发分钟+2 起算, 宁可保守不前瞻。
    try:
        _mm = int(hm[:2]) * 60 + int(hm[3:5]) + 1  # +1min = 触发分钟 bar 的端标
        _anchor = f"{_mm // 60:02d}:{_mm % 60:02d}"
    except (ValueError, IndexError):
        return {"status": "bad_event_fields"}
    after = [b for b in bars if b[0][11:16] > _anchor][:SCAN_N]
    if not after:
        return {"status": "no_bars_after"}
    rec = {}
    for n in (1, 2, 3, 5, 10):
        window = after[:n]
        if len(window) < n:
            rec[f"recovered_{n}m"] = None  # 可见窗不足, null 而非 False (防假零, 同DEB-20260902口径)
            rec[f"close_recovered_{n}m"] = None
        elif is_long:
            rec[f"recovered_{n}m"] = any(b[2] >= floor for b in window)
            rec[f"close_recovered_{n}m"] = any(b[4] >= floor for b in window)
        else:
            rec[f"recovered_{n}m"] = any(b[3] <= floor for b in window)
            rec[f"close_recovered_{n}m"] = any(b[4] <= floor for b in window)
    if is_long:
        worst = min(b[3] for b in after)
        continued_bps = (worst - trig) / trig * 10000.0
    else:
        worst = max(b[2] for b in after)
        continued_bps = (trig - worst) / trig * 10000.0
    continued_bps_2m = None
    close_move_bps_2m = None
    hard_sl_during_2m = None
    if len(after) >= 2:
        two = after[:2]
        if is_long:
            worst_2m = min(b[3] for b in two)
            continued_bps_2m = (worst_2m - trig) / trig * 10000.0
            close_move_bps_2m = (two[-1][4] - trig) / trig * 10000.0
        else:
            worst_2m = max(b[2] for b in two)
            continued_bps_2m = (trig - worst_2m) / trig * 10000.0
            close_move_bps_2m = (trig - two[-1][4]) / trig * 10000.0
        initial_sl = ev.get("initial_sl")
        if initial_sl:
            initial_sl = float(initial_sl)
            hard_sl_during_2m = any(
                (b[3] <= initial_sl) if is_long else (b[2] >= initial_sl) for b in two)
    cf_saved = None
    limit_px = ev.get("limit_px")
    if rec.get("recovered_10m") and limit_px:
        limit_px = float(limit_px)
        cf_saved = ((floor - limit_px) if is_long else (limit_px - floor)) / entry * 10000.0
    return {
        "status": "labeled",
        "partial": len(after) < SCAN_N,
        "n_bars_scanned": len(after),
        **rec,
        "continued_bps_2m": round(continued_bps_2m, 1) if continued_bps_2m is not None else None,
        "close_move_bps_2m": round(close_move_bps_2m, 1) if close_move_bps_2m is not None else None,
        "hard_sl_during_2m": hard_sl_during_2m,
        "continued_bps_10m": round(continued_bps, 1),
        "cf_floor_fill_saved_bps": (round(cf_saved, 1) if cf_saved is not None else None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="只回填该 et_date (缺省=全部未回填事件)")
    args = ap.parse_args()

    # 载体心跳 (2026-09-03 复盘批): 穿透是稀有事件 (家族 5 案/12 交易日, 8/21→9/1 零案),
    #   "文件不存在"与"当期无事件"对注册表产出核对长得一模一样 → 初生豁免期 (10 天) 过后
    #   必误报黄灯 (黄灯永悬正是 DEB-20260903-trade-attribution-zombie-watch 退役的那类)。
    #   回填环每日跑, 由它保证载体存在并推进 mtime: 此后 mtime 新鲜度 = 回填环活着
    #   (断流 >_STALE_DAYS 即告), 行数 = 样本量, 样本新鲜度看行内 et_date。
    try:
        SHADOW.parent.mkdir(parents=True, exist_ok=True)
        SHADOW.touch()
    except Exception as e:
        print(f"WARNING: 载体心跳失败 (不影响回填): {e}")

    events, done = load_events()
    todo = [e for e in events
            if e.get("event_id") not in done
            and (not args.date or e.get("et_date") == args.date)]
    out_rows, stats = [], {"events_total": len(events), "already": len(events) - len(todo),
                           "labeled": 0, "no_bars": 0, "no_bars_after": 0, "bad": 0}
    for ev in todo:
        date = ev.get("et_date") or ""
        bars = load_bars(ev.get("symbol"), date) if date else None
        if bars is None:
            stats["no_bars"] += 1
            continue  # 不落 outcome → 下次自愈重试
        lab = label_one(ev, bars)
        # status 必须留在落盘行内 (2026-09-03 复盘批 P0 修): 三个消费端都按
        #   `type=="outcome" and status=="labeled"` 选行 — penetration_llm_analysis
        #   .load_samples (判分/毕业门 n_labeled)、shadow_board 段11 (攒样看板)、
        #   以及本脚本的 v_recovery_5m_this_run 汇总与逐行明细打印。
        #   原写法 pop 掉 status 再 **lab 落盘 → 三处永远选到 0 行: 研究环与看板
        #   结构性静默零 (cron 照跑/jsonl 照长/看板照渲染, 但永远 0/20 不毕业)。
        st = lab.get("status")
        if st != "labeled":
            stats[st if st in stats else "bad"] += 1
            if st == "no_bars_after":
                # 事件在当日最后一根 bar 之后 (收盘尾段) — 落 outcome 封账, 避免永久重试
                out_rows.append({"type": "outcome", "event_id": ev.get("event_id"),
                                 "et_date": date, "symbol": ev.get("symbol"),
                                 "path": ev.get("path"),
                                 "status": "no_bars_after", "backfilled_at": datetime.now().isoformat(timespec="seconds")})
            continue
        stats["labeled"] += 1
        out_rows.append({"type": "outcome", "event_id": ev.get("event_id"),
                         "et_date": date, "symbol": ev.get("symbol"),
                         "direction": ev.get("direction"), "side": ev.get("side"),
                         "exit_reason": ev.get("exit_reason"),
                         "path": ev.get("path"),
                         "depth_bps": ev.get("depth_bps"),
                         **lab,
                         "backfilled_at": datetime.now().isoformat(timespec="seconds")})
    if out_rows:
        with SHADOW.open("a", encoding="utf-8") as f:
            for r in out_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    labeled_rows = [r for r in out_rows if r.get("status") == "labeled"]
    v5 = [r for r in labeled_rows if r.get("recovered_5m") is True]
    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               **stats, "written": len(out_rows),
               "v_recovery_5m_this_run": len(v5)}
    OUT_LATEST.write_text(json.dumps({"summary": summary, "rows": out_rows},
                                     ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in out_rows:
        if r.get("status") == "labeled":
            print(f"{r['symbol']} {r.get('side')}/{r.get('exit_reason')} depth={r.get('depth_bps')}bps "
                  f"→ rec1/2/3/5/10={r.get('recovered_1m')}/{r.get('recovered_2m')}/"
                  f"{r.get('recovered_3m')}/{r.get('recovered_5m')}/{r.get('recovered_10m')} "
                  f"close2={r.get('close_recovered_2m')} hard2={r.get('hard_sl_during_2m')} "
                  f"续跌={r.get('continued_bps_10m')}bps cf_saved={r.get('cf_floor_fill_saved_bps')}")


if __name__ == "__main__":
    main()
