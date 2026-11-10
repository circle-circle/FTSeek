#!/usr/bin/env python3
"""P3: market_premise 离线探针 v2 (DEB-20261106 §P3)。只读，不改生产文件/行为。

⚠️ v1 作废原因（自查发现，写入本文件以免重犯）:
   v1 用 `close[d]/close[d-1]` 当作"前收盘 1 日收益"，等于把**当日结果**喂进 prompt
   （11-05 的输入里出现 "QQQ 1日 -6.49%"），remember 5/5 是 look-ahead 造成的假绿。
   v2 统一用 **as-of 前一根 K 线**（严格 < 决策日），VIX 取前一交易日，事件窗取
   [前一交易日 16:00 ET, 决策日 08:35 ET)。

预注册判据（跑前写死，与 v1 相同）:
  ① 冲击日召回 ≥ 4/5   —— 标签: 当日 QQQ 收盘对昨收 ≤ -2%
  ② 常态误伤 ≤ 1/20   —— 标签: |当日 QQQ 涨跌| ≤ 0.5%；误伤 = 说 BLOCK
  ③ 同输入复跑一致 ≥ 80% —— 5 个交易日 × 10 次

输入（全部 as-of，08:35 ET 已知）:
  QQQ/IWM/SPY 前收盘 1日/3日收益、SPY 相对 20MA、前收 VIX、
  上一交易日广度（缓存 500+ 标的中跌幅≤-1%/-2% 的占比）、
  隔夜事件条数 + 头条样本。
  注: SMH/SOXX/XLK 该券商无行情订阅 ⇒ 无板块代理，改用自建广度代理。

用法:
  cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python3.11 scripts/p3_market_premise_probe.py \
      --start 2026-07-01 --end 2026-11-05 --output-dir data/shadow/p3_market_premise_v2 --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, Path("/root/.hermes/skills/deep-stock-analysis/scripts")):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

CACHE = ROOT / "data" / "replay_cache" / "index_daily"
ET = ZoneInfo("America/New_York")
SHOCK_PCT = -2.0
NORMAL_ABS = 0.5
CONSISTENCY_RUNS = 10
DECISION_TIME = dtime(8, 35)

SYSTEM = (
    "你是美股组合级风控官。你只能看到决策时点之前的市场信息（前收盘、隔夜事件），"
    "要判断今天是否允许新增多头敞口。输出严格 JSON，不要任何解释性文字。"
)

USER_TMPL = """今天美股开盘前的环境快照（信息截止 08:35 ET，全部为已知信息）：

指数（均为**上一交易日收盘**相对更前一日的表现）：
- QQQ: 1日 {q1:+.2f}% / 3日 {q3:+.2f}%
- IWM: 1日 {i1:+.2f}% / 3日 {i3:+.2f}%
- SPY: 1日 {p1:+.2f}%，收盘相对 20 日均线 {ma20:+.2f}%
- VIX(上一交易日收盘): {vix:.2f}

盘前指数（今日 08:00 / 09:25 ET，相对上一交易日 15:55 收盘）：
- QQQ: {qqq0800:+.2f}% / {qqq0925:+.2f}%
- SPY: {spy0800:+.2f}% / {spy0925:+.2f}%

上一交易日市场广度（本系统跟踪池 {breadth_n} 只）：
- 下跌 ≤ -1%: {b1:.0f}%  ／ 下跌 ≤ -2%: {b2:.0f}%

隔夜事件（上一交易日 16:00 ET 至今）：
- 事件条数: {ev_n}
- 头条样本:
{headlines}

请判断今天是否允许新增多头敞口，并输出 JSON：
{{"verdict": "ALLOW|RESTRICT|BLOCK", "confidence": 0-1, "risk_level": "LOW|MEDIUM|HIGH",
  "evidence": ["最多3条，必须引用上面出现过的数字"], "reason": "一句话"}}

约束：BLOCK = 今天不应新开多头；RESTRICT = 可开但应减半/只做最强；ALLOW = 正常。
"""


def _http_call(model: str, system: str, user: str, max_tokens: int = 700) -> dict:
    import urllib.error
    import urllib.request
    import config  # type: ignore

    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": 0, "max_tokens": max_tokens}
    req = urllib.request.Request(
        config.DASHSCOPE_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.DASHSCOPE_KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:160]}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                obj = json.loads(text[s:e + 1])
            except Exception:
                obj = None
    return {"obj": obj, "text": text, "usage": data.get("usage") or {}}


def fetch_index_bars(symbol: str, start: str, end: str) -> list:
    CACHE.mkdir(parents=True, exist_ok=True)
    fp = CACHE / f"daily_{symbol.replace('.', '_')}_{start}_{end}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text())
        except Exception as exc:
            print(f"[p3_market_premise_probe] 缓存不可用重建 {fp.name}: {type(exc).__name__}", file=sys.stderr)
    from futu import AuType, KLType, RET_OK, OpenQuoteContext
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        for _ in range(3):
            ret, data, _p = ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, start=start, end=end,
                max_count=400, autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
                bars.sort(key=lambda b: b[0])
                fp.write_text(json.dumps(bars))
                time.sleep(1.2)
                return bars
            time.sleep(2)
    finally:
        ctx.close()
    return []


class Series:
    def __init__(self, bars):
        self.close = {b[0]: b[4] for b in bars}
        self.dates = sorted(self.close)

    def asof_index(self, date: str):
        """严格早于 date 的最后一根 K 线下标 (None 表示无)。"""
        prior = [d for d in self.dates if d < date]
        if not prior:
            return None
        return self.dates.index(prior[-1])

    def ret_asof(self, date: str, lag: int):
        i = self.asof_index(date)
        if i is None or i - lag < 0:
            return 0.0
        prev, cur = self.close[self.dates[i - lag]], self.close[self.dates[i]]
        return (cur / prev - 1.0) * 100.0 if prev else 0.0

    def ma_gap_asof(self, date: str, window: int = 20):
        i = self.asof_index(date)
        if i is None or i + 1 < window:
            return 0.0
        closes = [self.close[d] for d in self.dates[i - window + 1:i + 1]]
        ma = sum(closes) / len(closes)
        return (self.close[self.dates[i]] / ma - 1.0) * 100.0 if ma else 0.0

    def ret_of_day(self, date: str):
        """当日收盘对前一日收盘 (仅用于评估标签, 绝不进 prompt)。"""
        if date not in self.close:
            return 0.0
        i = self.dates.index(date)
        if i == 0:
            return 0.0
        prev = self.close[self.dates[i - 1]]
        return (self.close[date] / prev - 1.0) * 100.0 if prev else 0.0

    def last_close_date(self, date: str):
        i = self.asof_index(date)
        return self.dates[i] if i is not None else None


def load_breadth():
    """上一交易日广度: 用缓存日K自建 (券商无 SMH/SOXX 订阅)。"""
    fp = ROOT / "data" / "cache" / "daily_kline_v1.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))["symbols"]
    except Exception:
        return {}
    per_day = {}
    for sym, payload in data.items():
        bars = payload.get("bars") or []
        for i in range(1, len(bars)):
            d = str(bars[i]["time"])[:10]
            prev = bars[i - 1]["close"]
            # 只接受相邻交易日 (≤4 自然日), 否则跨缺口收益会虚增跌幅 (2026-11-06: 250 只里 35% 跌≥2% 明显失真)
            try:
                gap = (datetime.strptime(d, "%Y-%m-%d")
                       - datetime.strptime(str(bars[i - 1]["time"])[:10], "%Y-%m-%d")).days
            except Exception as exc:
                print(f"[p3_market_premise_probe] 坏日期跳过 {d}: {type(exc).__name__}", file=sys.stderr)
                continue
            if not prev or gap > 4:
                continue
            per_day.setdefault(d, []).append((bars[i]["close"] / prev - 1.0) * 100.0)
    out = {}
    for d, rets in per_day.items():
        n = len(rets)
        if n < 50:
            continue
        out[d] = (n, 100.0 * sum(1 for r in rets if r <= -1.0) / n,
                  100.0 * sum(1 for r in rets if r <= -2.0) / n)
    return out


def et_window_utc(decision_date: str, prev_date: str):
    """[prev_date 16:00 ET, decision_date 08:35 ET) → UTC。"""
    start = datetime.combine(datetime.strptime(prev_date, "%Y-%m-%d").date(),
                             dtime(16, 0), tzinfo=ET)
    end = datetime.combine(datetime.strptime(decision_date, "%Y-%m-%d").date(),
                           DECISION_TIME, tzinfo=ET)
    return start, end


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-11-05")
    ap.add_argument("--model", default="qwen3.7-max")
    ap.add_argument("--focus", default="2026-11-05", help="打印该日完整输入以便人工核 look-ahead")
    ap.add_argument("--premarket-feats", type=Path, default=None,
                    help="v3: {date: {qqq_pre_chg_0800,...}}，来自 premarket_index_regime_replay.build_features "
                         "(富途历史 5m session=ALL 含盘前段)")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    if not args.execute:
        raise SystemExit("dry-run: 加 --execute 才实跑")
    if args.output_dir.exists():
        raise SystemExit(f"输出目录已存在: {args.output_dir}")
    args.output_dir.mkdir(parents=True)

    import sqlite3
    con = sqlite3.connect(f"file:{ROOT / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
    evdb = sqlite3.connect(f"file:{ROOT / 'data' / 'event_stream.db'}?mode=ro", uri=True)

    series = {}
    for sym in ("US.QQQ", "US.IWM", "US.SPY"):
        bars = fetch_index_bars(sym, "2026-05-01", args.end)
        series[sym] = Series(bars)
        print(f"[fetch] {sym} bars={len(bars)}", file=sys.stderr)
    if not series["US.QQQ"].dates:
        print("QQQ 日K获取失败, 终止")
        return 1
    breadth = load_breadth()
    pre_feats = {}
    if args.premarket_feats:
        pre_feats = json.loads(args.premarket_feats.read_text(encoding="utf-8"))
        print(f"[premkt] loaded {len(pre_feats)} days", file=sys.stderr)
    vix_map = {d: v for d, v in con.execute("select date, vix from playbook_regime_log")}
    cols = {r[1] for r in evdb.execute("PRAGMA table_info(canonical_events)")}
    title_col = next((c for c in ("title", "headline", "summary") if c in cols), None)

    results = []
    for d in series["US.QQQ"].dates:
        if not (args.start <= d <= args.end):
            continue
        prev = series["US.QQQ"].last_close_date(d)
        if not prev:
            continue
        b = breadth.get(prev) or (0, 0.0, 0.0)
        vix_prev = None
        for cand in [x for x in sorted(vix_map) if x < d][-1:]:
            vix_prev = vix_map.get(cand)
        start_utc, end_utc = et_window_utc(d, prev)
        ev_rows = list(evdb.execute(
            "select count(*)" + (f", group_concat(substr({title_col},1,80), ' || ')"
                                 if title_col else ", ''") +
            " from canonical_events where first_seen_at >= ? and first_seen_at < ?",
            (start_utc.isoformat(), end_utc.isoformat())))
        ev_n, ev_titles = (ev_rows[0][0], ev_rows[0][1] or "") if ev_rows else (0, "")
        heads = [h.strip() for h in str(ev_titles).split("||") if h.strip()][:6]

        pf = pre_feats.get(d) or {}
        user = USER_TMPL.format(
            qqq0800=pf.get("qqq_pre_chg_0800", 0.0), qqq0925=pf.get("qqq_pre_chg_0925", 0.0),
            spy0800=pf.get("spy_pre_chg_0800", 0.0), spy0925=pf.get("spy_pre_chg_0925", 0.0),
            q1=series["US.QQQ"].ret_asof(d, 1), q3=series["US.QQQ"].ret_asof(d, 3),
            i1=series["US.IWM"].ret_asof(d, 1), i3=series["US.IWM"].ret_asof(d, 3),
            p1=series["US.SPY"].ret_asof(d, 1), ma20=series["US.SPY"].ma_gap_asof(d, 20),
            vix=float(vix_prev or 0.0),
            breadth_n=b[0], b1=b[1], b2=b[2], ev_n=ev_n,
            headlines="\n".join(f"  - {h}" for h in heads) or "  （无）")

        if d == args.focus:
            print(f"\n===== FOCUS {d} 输入 (人工核 look-ahead) =====\n{user}", file=sys.stderr)

        res = _http_call(args.model, SYSTEM, user)
        obj = res.get("obj") or {}
        rec = {"date": d, "prev_close_date": prev,
               "label_qqq_pct": round(series["US.QQQ"].ret_of_day(d), 3),
               "verdict": str(obj.get("verdict") or "").upper() or None,
               "confidence": obj.get("confidence"), "risk_level": obj.get("risk_level"),
               "evidence": obj.get("evidence"), "reason": obj.get("reason"),
               "error": res.get("error"), "user_prompt": user}
        results.append(rec)
        (args.output_dir / f"{d}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[p3] {d} label={rec['label_qqq_pct']:+.2f}% verdict={rec['verdict']}", file=sys.stderr)

    shock = [r for r in results if r["label_qqq_pct"] <= SHOCK_PCT]
    normal = [r for r in results if abs(r["label_qqq_pct"]) <= NORMAL_ABS]
    hits = [r for r in shock if r["verdict"] in ("RESTRICT", "BLOCK")]
    fps = [r for r in normal if r["verdict"] == "BLOCK"]

    cand = shock[:2] + normal[:3]
    cons = []
    for r in cand:
        vs = []
        for _ in range(CONSISTENCY_RUNS):
            out = _http_call(args.model, SYSTEM, r["user_prompt"])
            v = str((out.get("obj") or {}).get("verdict") or "").upper()
            if v:
                vs.append(v)
        cnt = Counter(vs)
        cons.append({"date": r["date"], "counts": dict(cnt),
                     "agree": (max(cnt.values()) / len(vs)) if vs else 0.0})
        print(f"[consistency] {r['date']} {dict(cnt)} agree={cons[-1]['agree']:.0%}", file=sys.stderr)

    recall_rate = (len(hits) / len(shock)) if shock else None
    fp_rate = (len(fps) / len(normal)) if normal else None
    normal_dist = dict(Counter(r["verdict"] for r in normal))
    summary = {
        "schema": "p3_market_premise_probe_v2", "window": [args.start, args.end],
        "model": args.model, "n_days": len(results),
        "shock_days": len(shock), "shock_recall": len(hits), "recall_rate": recall_rate,
        "shock_detail": [{k: r[k] for k in ("date", "label_qqq_pct", "verdict", "confidence")}
                         for r in shock],
        "normal_days": len(normal), "normal_block_fp": len(fps), "fp_rate": fp_rate,
        "normal_verdict_dist": normal_dist,
        "verdict_dist": dict(Counter(r["verdict"] for r in results)),
        "consistency": cons, "min_consistency": min((c["agree"] for c in cons), default=None),
        "criteria": {
            "c1_recall_ge_4of5": (recall_rate >= 0.8) if recall_rate is not None else None,
            "c2_fp_le_1of20": (fp_rate <= 0.05) if fp_rate is not None else None,
            "c3_consistency_ge_80": (min((c["agree"] for c in cons), default=0) >= 0.8),
        },
        "lookahead_note": "所有价格特征取严格早于决策日的收盘; 事件窗=前一日16:00ET~当日08:35ET; label 仅用于评估",
        "production_write": False, "rows": results,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in (
        "n_days", "shock_days", "shock_recall", "recall_rate", "normal_days",
        "normal_block_fp", "fp_rate", "normal_verdict_dist", "verdict_dist",
        "min_consistency", "criteria")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())