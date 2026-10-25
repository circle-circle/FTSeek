#!/usr/bin/env python3
"""空头近失带影子结算器 (RFC-167, 2026-10-25).

问题: 空侧 (跳空 ≥ SHORT_GAP_MIN=3.0%) 的 A10 实测是 +$70.3/笔, 是多头 ($16.8/笔) 的
4.2 倍, 但 6 周只有 4 个信号 —— 2–3% 跳空这一档被闸挡在门外且**从未被测量**。
直接降闸 = 交易行为变更 (改变成交集合) ⇒ 先由 `extended_hours_entry_executor.py`
的 `_record_short_nearmiss` 落 record-only 样本 (零下单), 本脚本负责结算。

结算口径 (预注册, 见 docs/decisions/RFC-167-*.md §3; 事后不得改):
  假设按记录价 px 在**记录时刻**开空 (LONG 侧闸不改, 故只测空侧)
  → 取记录时刻之后**第一个 RTH 交易时段**的日 K (high/low/close):
      mfe_pct   = (px - low)   / px * 100   # 空头最大有利偏移
      mae_pct   = (high - px)  / px * 100   # 空头最大不利偏移
      close_pct = (px - close) / px * 100   # 持到收盘
      verdict   = FADE if mfe_pct >= 0.50 else NO_FADE
  (0.50% = 往返费 0.18% + 薄盘滑点缓冲 0.32%; 原始三列全量留存, 便于日后按别的阈值重算,
   本文件的 verdict 只是**当下**预注册的那一个, 不得事后调)

数据源: ① `data/replay_bars/US_<sym>_<date>.json` (1m, 免费) ② 富途日K (复用
`settle_extended_hours._day_bars` 的限频+退避+抛 _KlineUnavailable 语义)。
接口不可用 ⇒ 该行保持未结算 (settled=false, reason=api_unavailable), 下轮补 —— 绝不静默当"无数据"。

用法:
  python scripts/settle_ext_short_nearmiss.py            # 结算所有未结算日
  python scripts/settle_ext_short_nearmiss.py --self-check   # Gate6 三态: TOO_EARLY/DEAD/LIVE
挂载: crontab `10 6 * * 2-6` (北京; ET 前一日收盘+盘后结束后, 05:35 bar 保障与 05:47
      replay 之后) — 见 docs/CONTROL-LOOP-LEDGER.md `crontab-ext-short-nearmiss-settle`。
回滚: 删 cron 行 + `EXT_ENTRY_SHORT_NEARMISS_BAND` (executor 两行 env) ⇒ 记录与结算双停;
      已落 jsonl/JSON 留档不删 (已产生样本不销毁)。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

SHADOW_DIR = ROOT / "data" / "shadow" / "ext_short_nearmiss"
SETTLED = ROOT / "data" / "shadow" / "ext_short_nearmiss_settled.jsonl"
LATEST = ROOT / "data" / "shadow" / "ext_short_nearmiss_latest.json"
BARS_DIR = ROOT / "data" / "replay_bars"
FADE_MFE_PCT = 0.50          # 预注册阈值 (见模块 docstring), 不得事后调
SAMPLE_GATE = 30             # 裁决最小已结算样本
FADE_RATE_GATE = 0.55
NET_EDGE_GATE = 0.30         # mean(mfe) - mean(mae) 的百分数下限


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _already_settled():
    """已结算集合 (date|session|symbol) —— 幂等, 重跑不重复落行。"""
    done = set()
    if not SETTLED.exists():
        return done
    with SETTLED.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("settled"):
                done.add("%s|%s|%s" % (r.get("et_date"), r.get("session"), r.get("symbol")))
    return done


def _rth_bars_from_replay(symbol, date_str):
    """replay_bars 1m (仅 RTH) → (high, low, close) 或 None。"""
    path = BARS_DIR / ("US_%s_%s.json" % (symbol.replace("US.", ""), date_str))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    highs = [float(b[2]) for b in data if len(b) >= 5]
    lows = [float(b[3]) for b in data if len(b) >= 5]
    closes = [float(b[4]) for b in data if len(b) >= 5]
    if not highs:
        return None
    return max(highs), min(lows), closes[-1]


_CTX = []


def _open_ctx():
    """惰性建 Futu 报价连接 (只在该行 replay_bars 缺数据时才建); 失败返回 None。

    不给测试/离线环境开局就建连接: 早期版本在循环外无条件建, 一次跑批就白白开一条
    OpenD 连接 (且在本机以外的环境会卡在超时上)。
    """
    if _CTX:
        return _CTX[0]
    try:
        from futu import OpenQuoteContext
        _CTX.append(OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                     port=int(os.getenv("FUTU_OPD_PORT", "11111"))))
    except Exception:
        _CTX.append(None)
    return _CTX[0]


def _settle_row(row):
    """单行结算 → dict(settled/reason/mfe_pct/mae_pct/close_pct/verdict/source)。"""
    symbol = str(row.get("symbol") or "")
    px = float(row.get("px") or 0)
    first_ts = str(row.get("first_ts") or "")
    if not symbol or px <= 0 or len(first_ts) < 10:
        return {"settled": False, "reason": "bad_row"}
    day = first_ts[:10]
    session = str(row.get("session") or "")
    # 记录时刻之后的第一个 RTH 时段: 盘前记录 ⇒ 当日 RTH; 盘后记录 ⇒ 次一交易日 RTH
    #   (replay_bars 只含 09:31-16:00, 故盘后记录当日无后续 bar, 直接找下一日文件)
    from datetime import timedelta
    src = None
    high = low = close = None
    d0 = datetime.strptime(day, "%Y-%m-%d")
    for offset in range(0, 6):
        cand = (d0 + timedelta(days=offset)).strftime("%Y-%m-%d")
        if offset == 0 and session == "afterhours":
            continue
        got = _rth_bars_from_replay(symbol, cand)
        if got:
            high, low, close = got
            src = "replay_bars:" + cand
            break
    if high is None:
        ctx = _open_ctx()
        if ctx is None:
            # 连不上 OpenD ≠ 没有 bar: 分开记, 否则"接口挂了"会被读成"这只票没数据"
            return {"settled": False, "reason": "ctx_unavailable"}
        try:
            from scripts.settle_extended_hours import _day_bars
            start = day if session != "afterhours" else (
                d0 + timedelta(days=1)).strftime("%Y-%m-%d")
            bars = _day_bars(ctx, symbol, start, 6)
            bars = [b for b in bars if b["date"] >= day and not (
                session == "afterhours" and b["date"] == day)]
            if bars:
                high, low, close = bars[0]["high"], bars[0]["low"], bars[0]["close"]
                src = "futu_daily:" + bars[0]["date"]
        except Exception as exc:   # 接口不可用 ≠ 无数据: 保持未结算, 下轮补
            return {"settled": False, "reason": "api_unavailable",
                    "detail": "%s: %s" % (type(exc).__name__, exc)}
    if high is None:
        return {"settled": False, "reason": "no_bars"}
    mfe = (px - low) / px * 100.0
    mae = (high - px) / px * 100.0
    close_pct = (px - close) / px * 100.0
    return {"settled": True, "source": src,
            "mfe_pct": round(mfe, 3), "mae_pct": round(mae, 3),
            "close_pct": round(close_pct, 3),
            "verdict": "FADE" if mfe >= FADE_MFE_PCT else "NO_FADE"}


def _stats(rows):
    """累计读数 (全部已结算行)。剔除最大 mfe 一笔的残差读数同批给出 (防单笔主导)。"""
    if not rows:
        return {"n": 0}
    mfe = sorted((r["mfe_pct"] for r in rows), reverse=True)
    mae = [r["mae_pct"] for r in rows]
    fade = sum(1 for r in rows if r.get("verdict") == "FADE")
    ex = mfe[1:] or [0.0]
    mae_ex = mae[1:] or [0.0]
    return {
        "n": len(rows),
        "fade_n": fade,
        "fade_rate": round(fade / len(rows), 4),
        "mean_mfe": round(sum(mfe) / len(mfe), 3),
        "mean_mae": round(sum(mae) / len(mae), 3),
        "edge": round(sum(mfe) / len(mfe) - sum(mae) / len(mae), 3),
        "ex_top1_n": len(ex),
        "ex_top1_fade_rate": round(sum(1 for v in ex if v >= FADE_MFE_PCT) / len(ex), 4),
        "ex_top1_edge": round(sum(ex) / len(ex) - sum(mae_ex) / len(mae_ex), 3),
    }


def _decidable(st):
    """预注册裁决门: n≥30 ∧ FADE率≥55% ∧ 净 edge≥0.30pp ∧ 剔最大单笔后仍成立。"""
    if st.get("n", 0) < SAMPLE_GATE:
        return False, "SAMPLE_%d/%d" % (st.get("n", 0), SAMPLE_GATE)
    if st["fade_rate"] < FADE_RATE_GATE:
        return False, "FADE_RATE_%.3f<%.2f" % (st["fade_rate"], FADE_RATE_GATE)
    if st["edge"] < NET_EDGE_GATE:
        return False, "EDGE_%.3f<%.2f" % (st["edge"], NET_EDGE_GATE)
    if st["ex_top1_edge"] < NET_EDGE_GATE:
        return False, "SINGLE_TRADE_DOMINATED(edge_ex=%.3f)" % st["ex_top1_edge"]
    return True, "READY"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true",
                    help="只读三态核验 (TOO_EARLY / DEAD / LIVE), 不结算")
    args = ap.parse_args()

    today = _now_et().strftime("%Y-%m-%d")
    files = sorted(SHADOW_DIR.glob("*.json")) if SHADOW_DIR.exists() else []

    if args.self_check:
        # 三态: 今日文件有行=LIVE; 无今日文件但存在历史文件=DEAD(埋点未落行);
        #       两者都无=TOO_EARLY(窗口未到/整段关闭), 不可判。
        todays = SHADOW_DIR / (today + ".json")
        if todays.exists():
            doc = json.loads(todays.read_text())
            n = len(doc.get("rows") or {})
            print("[self-check] LIVE: %s rows=%d band_lo=%s gap_min=%s"
                  % (todays.name, n, doc.get("band_lo"), doc.get("short_gap_min")))
            return 0 if n else 2
        if files:
            print("[self-check] DEAD: 有历史文件 %d 个但今日 %s 无落行 ⇒ 埋点未在生产触发"
                  % (len(files), today))
            return 3
        print("[self-check] TOO_EARLY: 无任何记录文件 (EXT 窗口未到或 band 未开) ⇒ 不可判")
        return 4

    settled_ids = _already_settled()
    new_rows, skipped = [], 0
    for path in files:
        et_date = path.stem
        if et_date >= today:      # 当日未收盘, 不结
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for key, row in (doc.get("rows") or {}).items():
            if "%s|%s|%s" % (et_date, row.get("session"), row.get("symbol")) in settled_ids:
                skipped += 1
                continue
            res = _settle_row(row)
            rec = {"et_date": et_date, "session": row.get("session"),
                   "symbol": row.get("symbol"), "px": row.get("px"),
                   "chg_pct": row.get("chg_pct"), "ticks": row.get("ticks"),
                   "first_ts": row.get("first_ts")}
            rec.update(res)
            new_rows.append(rec)

    if new_rows:
        SETTLED.parent.mkdir(parents=True, exist_ok=True)
        with SETTLED.open("a") as fh:
            for r in new_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    all_rows = []
    if SETTLED.exists():
        with SETTLED.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("settled"):
                    all_rows.append(r)
    st = _stats(all_rows)
    ok, why = _decidable(st)
    LATEST.write_text(json.dumps({"as_of": today, "stats": st, "decidable": ok,
                                  "why": why, "already_settled_skipped": skipped},
                                 ensure_ascii=False, indent=1))
    pend = sum(1 for r in new_rows if not r.get("settled"))
    print("[settle-short-nearmiss] %s 新增=%d (未结算 %d) 累计已结算 n=%d FADE=%s "
          "mean_mfe=%s mean_mae=%s edge=%s 裁决=%s"
          % (today, len(new_rows), pend, st.get("n", 0),
             st.get("fade_rate"), st.get("mean_mfe"), st.get("mean_mae"),
             st.get("edge"), why))
    return 0


if __name__ == "__main__":
    sys.exit(main())