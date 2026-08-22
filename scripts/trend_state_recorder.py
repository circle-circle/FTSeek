#!/usr/bin/env python3
"""趋势态记录器 — 执行准确性蓝图 P0-1 (docs/design/2026-08-22_execution_accuracy_blueprint.md)

每日收盘后为指令池标的批量计算三级趋势态中的周级+日级事实, 落 JSONL 攒样本,
供终裁后 P1 趋势一致性门的影子 A/B 使用。只读 DB (mode=ro), 只写自己的产物。

口径 (蓝图 §二):
  weekly_bias      = close vs MA20 且 MA20 五日斜率同向 → up/down, 否则 flat
  daily_structure  = 近10日 low 序列 higher-low/lower-low 净计数 → up/down/flat
  盘中势不在此录 (属触发时机, 非门 A/B 所需)。

预注册边界: 本文件是事实记录, 不是门, 不拦截任何东西; P1 门的判据另立。

cron: 26 6 * * 2-6 (回滚 = 删本行 + 本脚本 + data/trend_states/)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # 使 app.utils.timezone(now_et) 可导入, 免 UTC fallback
DB_PATH = PROJECT_ROOT / "data" / "ftseek_us.db"
DAILY_DIR = PROJECT_ROOT / "data" / "replay_bars_daily"
OUT_DIR = PROJECT_ROOT / "data" / "trend_states"
OUT_JSONL = OUT_DIR / "trend_states.jsonl"

UNIVERSE_LOOKBACK_DAYS = 45   # 指令池回看窗口
MA_SLOW = 20
MA_FAST = 5
SLOPE_WINDOW = 5              # MA20 斜率回看交易日
STRUCT_WINDOW = 10            # 日级结构回看交易日
BARS_MIN = MA_SLOW + SLOPE_WINDOW + 1


def _now_et_str() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().isoformat()
    except Exception:
        return datetime.utcnow().isoformat() + "+00:00(utc-fallback)"


def _today_et() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def _load_universe() -> list[str]:
    """近45日 playbook_instructions 标的 ∪ 当前持仓 (只读)。"""
    syms: set[str] = set()
    cutoff = (datetime.now() - timedelta(days=UNIVERSE_LOOKBACK_DAYS)).date().isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        for (s,) in conn.execute(
                "SELECT DISTINCT symbol FROM playbook_instructions WHERE date >= ? AND symbol IS NOT NULL",
                (cutoff,)):
            if isinstance(s, str) and s.startswith("US."):
                syms.add(s)
        for (s,) in conn.execute("SELECT DISTINCT symbol FROM open_positions WHERE symbol LIKE 'US.%'"):
            syms.add(s)
    finally:
        conn.close()
    return sorted(syms)


def _cache_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol.replace('.', '_')}.json"


def _load_bars(symbol: str, allow_fetch: bool):
    """[[date,o,h,l,c]] 升序; 与 t5_hold_counterfactual 同一缓存与补拉参数 (QFQ)。"""
    cp = _cache_path(symbol)
    if cp.exists():
        try:
            rows = json.loads(cp.read_text())
            if rows:
                return rows
        except Exception:
            pass
    if not allow_fetch:
        return None
    try:
        from futu import KLType, RET_OK, AuType, OpenQuoteContext
        ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                               port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
        rows: list[list] = []
        page_key = None
        for _page in range(4):
            ret, data, page_key = ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, start="2026-01-01", end="2026-12-31",
                max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
            if ret != RET_OK or data is None:
                break
            for _, r in data.iterrows():
                rows.append([str(r.get("time_key", ""))[:10], float(r["open"]), float(r["high"]),
                             float(r["low"]), float(r["close"])])
            if page_key is None:
                break
            time.sleep(1.2)
        ctx.close()
        time.sleep(1.2)
        if not rows:
            return None
        rows.sort(key=lambda b: b[0])
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(rows))
        return rows
    except Exception as e:
        print(f"[trend_state] {symbol} 补拉失败: {e}")
        return None


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def compute_weekly_bias(bars: list[list]) -> tuple[str, dict]:
    closes = [b[4] for b in bars]
    if len(closes) < BARS_MIN:
        return "insufficient_bars", {}
    ma20_now = _mean(closes[-MA_SLOW:])
    ma20_prev = _mean(closes[-MA_SLOW - SLOPE_WINDOW:-SLOPE_WINDOW])
    slope_pct = (ma20_now / ma20_prev - 1) * 100 if ma20_prev else 0.0
    close = closes[-1]
    above = close > ma20_now
    rising = slope_pct > 0
    bias = "up" if (above and rising) else ("down" if (not above and not rising) else "flat")
    high_52w = max(b[2] for b in bars[-252:]) if len(bars) >= 20 else max(b[2] for b in bars)
    detail = {
        "close": round(close, 4),
        "ma5": round(_mean(closes[-MA_FAST:]), 4),
        "ma20": round(ma20_now, 4),
        "ma20_slope5d_pct": round(slope_pct, 3),
        "pct_from_52w_high_pct": round((close / high_52w - 1) * 100, 2),
        "above_ma20": above,
    }
    return bias, detail


def compute_daily_structure(bars: list[list]) -> str:
    lows = [b[3] for b in bars]
    if len(lows) < STRUCT_WINDOW + 1:
        return "insufficient_bars"
    win = lows[-(STRUCT_WINDOW + 1):]
    net = sum(1 for i in range(1, len(win)) if win[i] > win[i - 1]) \
        - sum(1 for i in range(1, len(win)) if win[i] < win[i - 1])
    if net >= 3:
        return "up"
    if net <= -3:
        return "down"
    return "flat"


def main(allow_fetch: bool = True) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = _today_et()
    done: set[tuple[str, str]] = set()
    if OUT_JSONL.exists():
        for line in OUT_JSONL.read_text().splitlines():
            try:
                r = json.loads(line)
                done.add((r.get("date"), r.get("symbol")))
            except Exception:
                continue

    universe = _load_universe()
    print(f"[trend_state] {today} universe={len(universe)} already_done={len(done)}")
    written = Counter()
    skipped_censored = 0
    with OUT_JSONL.open("a") as fh:
        for sym in universe:
            if (today, sym) in done:
                continue
            bars = _load_bars(sym, allow_fetch=allow_fetch)
            if not bars or len(bars) < BARS_MIN:
                skipped_censored += 1
                continue
            # 只用今天(ET)已收盘的 bar, 防盘中半根K线污染
            bars = [b for b in bars if b[0] <= today] or bars
            bias, detail = compute_weekly_bias(bars)
            if bias == "insufficient_bars":
                skipped_censored += 1
                continue
            rec = {
                "date": today,
                "symbol": sym,
                "weekly_bias": bias,
                "daily_structure": compute_daily_structure(bars),
                **detail,
                "bars_used": len(bars),
                "computed_at": _now_et_str(),
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written[bias] += 1
    print(f"[trend_state] written={sum(written.values())} censored={skipped_censored} "
          f"bias_dist={dict(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
