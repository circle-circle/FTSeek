#!/usr/bin/env python3
"""隔夜财报 carry 影子 (2026-09-22, 用户裁「按建议」— **record-only, 零下单**)。

**⛔ 已退役 (2026-09-24, 用户裁「彻底停采不重启」)**: 实测 **2 个交易日**
(9/22-9/23; 9/24 采集行在当日裁决时删, 无 9/24 数据) n_passes_floors=0/日 < 0.5 阈值
(9 月非财报淡季, 候选池∩财报门=∅ 结构性零供给), 且采集面存在数据质量缺陷 (jsonl 190 行明细键值
全 null, 仅 cohort summary 可用)。**判据字面要求 n≥3 交易日, 本次实采 2 日 ⇒ 记为提前停采
(例外, 见 docs/analysis/2026-09-24-carry-shadow-adjudication.md §2 判据达成度披露), 判据文字零改动。**两条 cron 行已删 (原文留档
docs/analysis/2026-09-24-carry-shadow-adjudication.md; 备份
/tmp/crontab_backup_20260924_carry.txt)。脚本与台账 data/overnight_earnings_
carry_shadow.jsonl 保留归档不删 — 若 10 月财报季要重启, **须重新预注册**
(判据/口径/采集字段修复项一并重写), 不得直接恢复 cron。

问题 (2026-09-22 零成交归因): 延长时段 9/21、9/22 两个交易日 0 成交。根因实测 =
候选池(价格异动榜 50 只) 与 earnings 门("当日同相位财报事件") **交集恒为 ∅**:
三条独立日历源当日名单(futu 6 / AV 18 / Finnhub 8) 与候选池交集全是空集,
19 只 ≥$5 候选无一在册; 即便绕开日历那一道, 19 只里只有 4 只有 Finnhub 正 surprise,
且这 4 只当天连价格/动量/量闸都没过 ⇒ 双门叠加 + 财报季空窗 = 结构性无供给。

本影子测**唯一回测出有供给的替代口径**:
  盘前窗: 「T-1 盘后(amc)财报票 → T 日盘前承接」
  盘后窗: 「T 日盘前(bmo)财报票 → T 日盘后承接」(反向对称)
回测 (2026-09-22, Finnhub 9/01-9/16 共 103 只 amc): 跳空 ≥+2% 32/95; 上界判定
(跳空≥2% + 全天量≥20万 + 价≥$5) **24/95 = 25% ≈ 2.7 只/日**; 跳空 ≤-2% 45/95
⇒ **下跌面比上涨面厚**(天然双向, 契合「核心=真正有空头产出」)。
回测唯一缺口 = **盘前量历史不可得**(只能用全天量作上界) —— 本影子逐 5min 记录真实
盘前/盘后量, 攒 3-5 个交易日后回答: 若把门的"同日同相位"放宽到"T-1 盘后",
当日能有几只过现行价/量/市值闸。

与既有 shadow 的分工: premarket_exit_shadow 管**持仓**的盘前兑现; 本影子管**入场供给**
口径(候选从哪来), 不碰持仓、不读 DB、不下单、不改配置。

判据 (预注册, 写死在此, 读数不得事后改):
  n≥3 交易日 ∧ 过闸上界 ≥2 只/日 ⇒ 值得立 DEB 提请放宽门的"同日"语义;
  过闸上界持续 <0.5 只/日 ⇒ 该路不成立, 停采(删 cron 行)。
局限 (必须随读数披露): ①开盘跳空 ≈ 但不是 盘前涨幅; ②本影子的"过闸"用**当时的**
盘前价量, 而真实入场要在 04:00-09:29 某个时点决策 ⇒ 逐轮快照才能给出时间维度;
③cohort 只含 Finnhub 覆盖到的标的 (免费版当日仅 6-18 只, 覆盖不全已知)。

用法: PYTHONPATH=. venv/bin/python3.11 scripts/overnight_earnings_carry_shadow.py [--dry]
      [--session premarket|afterhours] (手工指定) [--force] (窗外/假日也落盘, 行标 forced)
挂载: crontab */5 16-21 * * 1-5 (盘前窗) + */5 4-7 * * 2-6 (盘后窗), 与 ext_entry 同班。
      ⚠️ 冬令时 ET 窗右移 1h → 两行各推后 1h (与既有各环 11 月一并重评)
产物: data/overnight_earnings_carry_shadow.jsonl (append-only, 每 5min 一行)
回滚: 删两行 cron + 删本脚本 (jsonl 保留供分析)。
"""
import argparse
import json
import logging
import math
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

_log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    _log.warning("dotenv load failed", exc_info=True)

from app.utils.timezone import now_et                      # noqa: E402
from scripts.extended_hours_market_session import get_extended_hours_session  # noqa: E402
import scripts.extended_hours_entry_executor as EXEC       # noqa: E402  (价/量/市值闸 SSOT)

OUT = ROOT / "data" / "overnight_earnings_carry_shadow.jsonl"
LOOKBACK_DAYS = 5      # cohort 回看窗 (T-1 周五 → T 周一, 覆盖长周末)
SNAPSHOT_BATCH = 200


def _num(value):
    """futu/pandas 的 NaN/None → None; 否则 float。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) else parsed


def _finnhub_calendar(days_back=LOOKBACK_DAYS):
    """Finnhub /calendar/earnings 过去段 → [{symbol,date,hour,...}].

    自建而非复用 app/data/earnings_calendar.get_recently_reported: 后者被 watchlist
    过滤(36 只静态名单) + 日期锚用服务器 CST datetime.now() (TZ-2 钩子已标死) +
    零消费方 —— 实测 days_back=2 恒返回 0 只, 对本影子的 cohort 无可用性。
    """
    key = os.getenv("FINNHUB_API_KEY") or ""
    if not key:
        return [], "no_api_key"
    today = now_et().date()
    url = ("https://finnhub.io/api/v1/calendar/earnings?from=%s&to=%s&token=%s"
           % ((today - timedelta(days=days_back)).isoformat(), today.isoformat(), key))
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            rows = json.loads(resp.read().decode()).get("earningsCalendar") or []
        return rows, None
    except Exception as exc:
        return [], "%s: %s" % (type(exc).__name__, exc)


def build_cohort(session, rows, today_et):
    """按 session 选 cohort: 盘前=最近一个已过的 amc 披露日; 盘后=当日 bmo。"""
    if session == "premarket":
        want_hour = "amc"
        dates = sorted({str(r.get("date")) for r in rows
                        if str(r.get("hour")) == want_hour and str(r.get("date")) < today_et})
        if not dates:
            return [], None
        picked = dates[-1]
    else:
        want_hour = "bmo"
        picked = today_et
    cohort = [r for r in rows
              if str(r.get("hour")) == want_hour and str(r.get("date")) == picked]
    uniq, seen = [], set()
    for r in cohort:
        sym = str(r.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        uniq.append({"symbol": sym, "report_date": str(r.get("date")),
                     "hour": str(r.get("hour")),
                     "eps_estimate": _num(r.get("epsEstimate")),
                     "eps_actual": _num(r.get("epsActual"))})
    return uniq, picked


def evaluate(session, cohort, qctx, today_et, hour=None):
    # 量闸口径对齐 (2026-09-23): 生产用**按时段分档**的量门槛 (`_min_volume_now`:
    #   盘前 04-07 → 2 万, 08-09 → 20 万), 本影子此前固定用 `EXEC.MIN_VOLUME` (20 万)
    #   ⇒ 早段读数被系统性低估 (把生产会放行的票读成 volume_below_min = 假红),
    #   观测量与它要影子的那道闸不同口径 ⇒ 一律走 `EXEC._min_volume_now` 同一个真源。
    hour_et = EXEC._et_hour_now() if hour is None else hour
    vol_floor = EXEC._min_volume_now(session, hour_et)

    """逐票取延长时段报价 → 对照现行闸 (价/涨/量/市值) 算上界可否过闸。

    口径 (2026-09-22 干跑实测教训): futu 快照在**该票当日延长时段无成交**时,
    `pre_change_rate` 返回 **0.0** 而 `pre_price` 仍是**前一延长时段的旧价**
    (实测 BNTC update_time=2026-09-21 20:01 而当前 07:11 ET) ⇒ 直接用回报字段会
    把"陈旧/无成交"读成"持平"(假绿)。故:
      - `chg_computed` = (延长时段价 / 昨收 − 1)×100, 与入场执行器 `_snapshot_candidates`
        同口径 ⇒ **判闸用 computed**, 回报字段另存 `chg_field` 供交叉核对;
      - `stale_quote` = update_time 的日期 < 今日 ET ⇒ 该票本轮无新延长时段数据,
        分析时须能排除 (否则"无数据"会被当成"有数据且不涨")。
    """
    from futu import RET_OK

    codes = ["US." + c["symbol"] for c in cohort]
    quotes = {}
    for i in range(0, len(codes), SNAPSHOT_BATCH):
        batch = codes[i:i + SNAPSHOT_BATCH]
        try:
            ret, snap = qctx.get_market_snapshot(batch)
        except Exception:
            _log.warning("market snapshot failed", exc_info=True)
            continue
        if ret != RET_OK or snap is None or not hasattr(snap, "columns"):
            continue
        for _, r in snap.iterrows():
            code = str(r.get("code", ""))
            prev = _num(r.get("prev_close_price"))
            if session == "premarket":
                px = _num(r.get("pre_price")) or _num(r.get("last_price"))
                chg_field = _num(r.get("pre_change_rate"))
                vol = _num(r.get("pre_volume"))
            else:
                px = _num(r.get("after_price")) or _num(r.get("last_price"))
                chg_field = _num(r.get("after_change_rate"))
                vol = _num(r.get("after_volume"))
            update_time = str(r.get("update_time") or "")
            chg = ((px / prev - 1) * 100.0) if (px and prev) else None
            quotes[code] = {
                "px": px, "chg": chg, "chg_field": chg_field, "vol": vol,
                "prev_close": prev, "market_cap": _num(r.get("total_market_val")),
                "update_time": update_time,
                "stale_quote": bool(update_time) and update_time[:10] < today_et,
            }

    items, n_pass, n_chg, n_vol = [], 0, 0, 0
    for c in cohort:
        q = quotes.get("US." + c["symbol"])
        row = dict(c)
        if not q:
            row.update({"quote": None, "reject": ["no_quote"], "passes_floors": False})
            items.append(row)
            continue
        chg = q["chg"] if q["chg"] is not None else 0.0
        vol = q["vol"] or 0.0
        px = q["px"] or 0.0
        mc = q["market_cap"] or 0.0
        if chg >= EXEC.ENTRY_CHANGE_PCT:
            n_chg += 1
        if vol >= vol_floor:
            n_vol += 1
        reasons = []
        if q["stale_quote"]:
            # 陈旧报价 (该票当日延长时段无新数据) 不得计入"过闸上界" —— 否则会把
            # "无数据"读成"有数据且合格", 正是本影子要防的假绿。
            reasons.append("stale_quote")
        if px < EXEC.MIN_PRICE:
            reasons.append("price_below_min")
        if chg < EXEC.ENTRY_CHANGE_PCT:
            reasons.append("change_below_min")
        if vol < vol_floor:
            reasons.append("volume_below_min")
        if mc and mc < EXEC.MIN_MARKET_CAP_USD:
            reasons.append("market_cap_below_min")
        row.update({"quote": q, "reject": reasons, "passes_floors": not reasons and px > 0})
        if row["passes_floors"]:
            n_pass += 1
        items.append(row)
    return items, {"n_chg_ge_min": n_chg, "n_vol_ge_min": n_vol, "n_passes_floors": n_pass,
                   "et_hour": hour_et, "min_volume_applied": vol_floor}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=["premarket", "afterhours"], default=None)
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    ap.add_argument("--force", action="store_true", help="窗外/假日也落盘 (行标 forced)")
    args = ap.parse_args()

    now = now_et()
    session = args.session or get_extended_hours_session(now)
    forced = False
    if session is None:
        if not args.force:
            print("[carry-shadow] 非延长时段窗内 (%s ET) — 静默退出" % now.strftime("%H:%M"))
            return 0
        session, forced = args.session or "premarket", True

    rows, err = _finnhub_calendar()
    cohort, cohort_date = build_cohort(session, rows, now.date().isoformat())
    if not cohort:
        row = {"ts": now.isoformat(), "et_date": now.date().isoformat(),
               "et_time": now.strftime("%H:%M"), "session": session, "forced": forced,
               "cohort_date": cohort_date, "cohort_source": "finnhub_calendar",
               "calendar_error": err, "summary": {"cohort": 0}, "items": []}
        _emit(row, args.dry)
        print("[carry-shadow] %s: cohort 空 (日历错误=%s) — 已落空行供断流可见" % (session, err))
        return 0

    from futu import OpenQuoteContext
    qctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                            port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    try:
        items, stats = evaluate(session, cohort, qctx, now.date().isoformat(),
                                            int(now.strftime("%H")))
    finally:
        qctx.close()

    summary = {
        "cohort": len(cohort),
        "quoted": sum(1 for i in items if i.get("quote")),
        "stale_quotes": sum(1 for i in items if (i.get("quote") or {}).get("stale_quote")),
        "n_passes_floors": stats["n_passes_floors"],
        "n_chg_ge_min": stats["n_chg_ge_min"],
        "n_vol_ge_min": stats["n_vol_ge_min"],
        "floors": {"min_price": EXEC.MIN_PRICE, "min_change_pct": EXEC.ENTRY_CHANGE_PCT,
                   "min_volume": EXEC.MIN_VOLUME, "min_market_cap_usd": EXEC.MIN_MARKET_CAP_USD,
                   "min_volume_applied": stats["min_volume_applied"],
                   "et_hour_applied": stats["et_hour"]},
    }
    row = {"ts": now.isoformat(), "et_date": now.date().isoformat(),
           "et_time": now.strftime("%H:%M"), "session": session, "forced": forced,
           "cohort_date": cohort_date, "cohort_source": "finnhub_calendar_%s" % session,
           "calendar_error": err, "summary": summary, "items": items}
    _emit(row, args.dry)
    passed = [i["symbol"] for i in items if i.get("passes_floors")]
    print("[carry-shadow] %s cohort=%d(%s) 报价=%d 涨≥%.1f%%=%d 量≥%d=%d ★过闸上界=%d %s"
          % (session, len(cohort), cohort_date, summary["quoted"], EXEC.ENTRY_CHANGE_PCT,
             stats["n_chg_ge_min"], summary["floors"]["min_volume_applied"], stats["n_vol_ge_min"],
             stats["n_passes_floors"], passed))
    return 0


def _emit(row, dry):
    if dry:
        print(json.dumps(row, ensure_ascii=False)[:1200])
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    sys.exit(main())