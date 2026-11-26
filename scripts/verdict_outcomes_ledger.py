#!/usr/bin/env python3
"""判别力反事实台账 (verdict_outcomes) — record-only, 零交易行为变更.

为什么存在
----------
系统每天对上百只票给出方向判定 (DA checkpoint) 与指令 (playbook_instructions), 但
**只有被执行的那部分**有可靠的前向收益:
  - `playbook_instructions` 里 426/518 条 SKIP 行的 fwd_return_* 全为 NULL
    (ic_tracker 对 direction='SKIP' 直接 continue; 只有 G4/G6 门降级的 78 条被
    `scripts/deb20261008_blocked_outcome_backfill.py` 事后补过)
  - DA checkpoint (679 条 verdict/23 天) 的前向收益 **从未被计算**
⇒ "模型挑得准不准"与"不买的决策对不对"结构性不可判, 任何"让模型更有判别力"的改动
   都无法裁决 (DEB-20261114-selection-discrimination §3.9).

本脚本把**每个** verdict (含 SKIP / 未执行) 一律按固定窗口补成可比较的行, 并同时落
「同窗口全宇宙均值」, 于是"选择力"可被直接测量:

    excess_long = 标的收益 − 同窗口全宇宙均值      (这只票相对大盘/同池跑赢多少)
    excess_dir  = sign(方向) × excess_long        (方向判断赚到的相对收益)

口径 (写死, 不得事后改)
------------------------
1. **两套基准, 不混用**:
   - `basis='daily'`: 锚 = 当日开盘价 (盘前 verdict 的可成交价), 窗口 eod / t1 / t3。
   - `basis='1m'`   : 锚 = 该分钟 1m 收盘 (盘中 verdict), 窗口 30m / 1h / 2h。
2. **宇宙均值**: 同一 basis、同一天、同一窗口, 对宇宙内所有可算标的等权平均 (n≥20 才算)。
   宇宙 = 本地 `data/replay_bars_{1m,1m_extra,daily}` 全部符号 (非"当日候选池")。
3. **方向**: BUY=+1 / SELL_SHORT=-1; 被门改写成 SKIP 的行从 `skip_reason` 的
   「多数方 BUY|SELL_SHORT」恢复原方向 (`dir_source='skip_reason'`); 纯弃权
   (投票中性/无方向信号) 记 `dir_source='none'`, 只保留 excess_long —— 弃权质量
   用「若按多头做会怎样」衡量, 不用方向调整收益冒充。
4. **幂等**: PK (source, src_key, basis, horizon); `INSERT OR REPLACE` 全值覆盖。
   默认重算最近 N 天 (缺 bar 的旧行不会被删, 复算即可补齐)。
5. **只读**: 除本表外零写入; 不碰 trade_records / open_positions / playbook_instructions。

用法
----
  python scripts/verdict_outcomes_ledger.py                # 增量 (近 10 天) 落库
  python scripts/verdict_outcomes_ledger.py --all          # 全量重算
  python scripts/verdict_outcomes_ledger.py --report       # 落库 + 打印判别力面板
  python scripts/verdict_outcomes_ledger.py --report-only  # 只读面板 (不落库)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
PROD_DB = REPO / "data" / "ftseek_us.db"
BARS_1M_DIRS = [REPO / "data" / "replay_bars_1m", REPO / "data" / "replay_bars_1m_extra"]
BARS_DAILY_DIR = REPO / "data" / "replay_bars_daily"
DA_CKPT_DIR = REPO / "data" / "da_checkpoint"
OUT_JSON = REPO / "data" / "selection_ic_latest.json"

_ET = None
def _et():
    global _ET
    if _ET is None:
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
    return _ET

INTRADAY_H = (("30m", 30), ("1h", 60), ("2h", 120))
DAILY_H = ("eod", "t1", "t3")
OPEN_MIN = 9 * 60 + 30          # 09:30 ET
CLOSE_MIN = 16 * 60             # 16:00 ET
_MAJORITY_RE = re.compile(r"多数方\s*(BUY|SELL_SHORT)")


# ─────────────────────────── 纯函数 (可单测) ───────────────────────────

def minute_index(hour: int, minute: int) -> int:
    """ET 时刻 → 1m bar 下标 (09:31 → 1, 16:00 → 390)."""
    return hour * 60 + minute - OPEN_MIN


def anchor_index(ts_et: datetime) -> Tuple[int, bool]:
    """verdict 时刻 → (1m 锚下标, 是否被夹到开盘)。

    盘前 verdict (DA 08:25 ET) 在 1m 序列上无对应 bar ⇒ 夹到 09:31 (下标 1),
    即"以开盘价执行"的可成交口径。
    """
    idx = minute_index(ts_et.hour, ts_et.minute)
    if idx < 1:
        return 1, True
    return min(idx, 390), False


def recover_direction(direction: Optional[str], skip_reason: Optional[str]) -> Tuple[str, str]:
    """返回 (原方向, 取证来源)。

    direction 已被组合层门改写为 SKIP 时, 受害行恒为多数方 ⇒ 从 skip_reason 解析。
    纯弃权 (无方向) 原样保留 SKIP + 'none' —— 不得用多头冒充方向。
    """
    d = str(direction or "").upper()
    if d in ("BUY", "SELL_SHORT"):
        return d, "row"
    if d == "SKIP":
        m = _MAJORITY_RE.search(str(skip_reason or ""))
        if m:
            return m.group(1), "skip_reason"
    return "SKIP", "none"


def dir_sign(direction: str) -> Optional[float]:
    if direction == "BUY":
        return 1.0
    if direction == "SELL_SHORT":
        return -1.0
    return None


def excess(sym_ret: Optional[float], uni_ret: Optional[float]) -> Optional[float]:
    """同窗口超额 = 标的 − 宇宙; 任一缺失返回 None (不补 0)。"""
    if sym_ret is None or uni_ret is None:
        return None
    if not (math.isfinite(sym_ret) and math.isfinite(uni_ret)):
        return None
    return sym_ret - uni_ret


# ─────────────────────────── 数据装载 ───────────────────────────

# except:pass 棘轮处置 (2026-11-16): 本脚本原 5 处 `except Exception: continue` 静默吞掉
# 坏文件/坏bar/坏行 ⇒ 数据源损坏与"本来就没数据"不可区分。脚本无 logger ⇒ 走 stderr。
_BAD_COUNTS: Dict[str, int] = {}


def _note_bad(kind: str, exc: Exception, ctx: str = "", *, loud: bool = False) -> None:
    """计数留痕; 文件级失败 (罕见, 意味数据源损坏) loud=True 立即打一条。"""
    _BAD_COUNTS[kind] = _BAD_COUNTS.get(kind, 0) + 1
    if loud:
        print("[verdict_outcomes] 跳过坏%s %s: %s(%s)"
              % (kind, ctx, type(exc).__name__, exc), file=sys.stderr)


def _report_bad() -> None:
    """逐行级失败可能上千条 ⇒ 不逐条刷屏, 装载结束时汇总一次。"""
    for kind in sorted(_BAD_COUNTS):
        print("[verdict_outcomes] 坏%s 累计跳过 %d 处" % (kind, _BAD_COUNTS[kind]),
              file=sys.stderr)
    _BAD_COUNTS.clear()


def _sym_key(path: Path) -> str:
    return path.name[:-5]


def load_bars_1m(symbols: Optional[set] = None) -> Dict[str, Dict[str, Dict[int, float]]]:
    out: Dict[str, Dict[str, Dict[int, float]]] = {}
    for d in BARS_1M_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            sym = _sym_key(f)
            if symbols is not None and sym not in symbols:
                continue
            if sym in out:
                continue
            try:
                rows = json.loads(f.read_text())
            except Exception as _e:
                _note_bad("1m文件", _e, str(f), loud=True)
                continue
            by_day: Dict[str, Dict[int, float]] = {}
            for r in rows:
                try:
                    ts = str(r[0])
                    by_day.setdefault(ts[:10], {})[minute_index(int(ts[11:13]), int(ts[14:16]))] = float(r[4])
                except Exception as _e:
                    _note_bad("1m bar", _e, str(f))
                    continue
            if by_day:
                out[sym] = by_day
    _report_bad()
    return out


def load_bars_daily(symbols: Optional[set] = None) -> Dict[str, Dict[str, Dict[str, float]]]:
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    if not BARS_DAILY_DIR.exists():
        return out
    for f in sorted(BARS_DAILY_DIR.glob("*.json")):
        sym = _sym_key(f)
        if symbols is not None and sym not in symbols:
            continue
        try:
            rows = json.loads(f.read_text())
        except Exception as _e:
            _note_bad("日K文件", _e, str(f), loud=True)
            continue
        by_day: Dict[str, Dict[str, float]] = {}
        for r in rows:
            try:
                by_day[str(r[0])] = {"o": float(r[1]), "c": float(r[4])}
            except Exception as _e:
                _note_bad("日K行", _e, str(f))
                continue
        if by_day:
            out[sym] = by_day
    _report_bad()
    return out


class Universe:
    """同窗口全宇宙等权均值 (按 basis 分离, 每日缓存)。"""

    def __init__(self, bars1m, daily):
        self.bars1m = bars1m
        self.daily = daily
        self._cache_intra: Dict[Tuple[str, int, int], Optional[float]] = {}
        self._cache_daily: Dict[Tuple[str, str, str], Optional[float]] = {}

    def intraday(self, day: str, i: int, j: int, min_names: int = 20) -> Optional[float]:
        key = (day, i, j)
        if key in self._cache_intra:
            return self._cache_intra[key]
        vals = []
        for sym, by_day in self.bars1m.items():
            m = by_day.get(day)
            if not m:
                continue
            a, b = m.get(i), m.get(j)
            if a and b and a > 0:
                vals.append(b / a - 1.0)
        val = (sum(vals) / len(vals)) if len(vals) >= min_names else None
        self._cache_intra[key] = val
        return val

    def daily_ret(self, day: str, horizon: str, min_names: int = 20) -> Optional[float]:
        key = (day, horizon, "u")
        if key in self._cache_daily:
            return self._cache_daily[key]
        vals = []
        for sym, by_day in self.daily.items():
            days = by_day.get(day)
            if not days or days["o"] <= 0:
                continue
            if horizon == "eod":
                c = days["c"]
            else:
                k = 1 if horizon == "t1" else 3
                nxt = _nth_trading_day(by_day, day, k)
                c = by_day[nxt]["c"] if nxt else None
            if c and c > 0:
                vals.append(c / days["o"] - 1.0)
        val = (sum(vals) / len(vals)) if len(vals) >= min_names else None
        self._cache_daily[key] = val
        return val


def _nth_trading_day(by_day: Dict[str, Dict[str, float]], day: str, n: int) -> Optional[str]:
    """该符号自身日线序列里的第 n 个后续交易日 (跨周末/假日自洽)。"""
    days = [d for d in by_day if d > day]
    if len(days) < n:
        return None
    return sorted(days)[n - 1]


# ─────────────────────────── 行构造 ───────────────────────────

def load_instruction_verdicts(conn: sqlite3.Connection, since: Optional[str]) -> List[dict]:
    sql = ("SELECT id, date, symbol, direction, confidence, skip_reason, created_at "
           "FROM playbook_instructions WHERE row_kind='instruction'")
    args: List[object] = []
    if since:
        sql += " AND date >= ?"
        args.append(since)
    rows = []
    for r in conn.execute(sql, args):
        rows.append({
            "source": "instruction",
            "src_key": str(r["id"]),
            "verdict_date": (r["date"] or "")[:10],
            "symbol": str(r["symbol"] or ""),
            "direction": r["direction"],
            "skip_reason": r["skip_reason"],
            "confidence": r["confidence"],
            "created_at": r["created_at"],
        })
    return rows


def load_da_verdicts(since: Optional[str]) -> List[dict]:
    rows = []
    if not DA_CKPT_DIR.exists():
        return rows
    for day_dir in sorted(DA_CKPT_DIR.iterdir()):
        if not day_dir.is_dir():
            continue
        day = day_dir.name
        if since and day < since:
            continue
        for f in sorted(day_dir.glob("US*.json")):
            if f.name.endswith(".raw.json") or f.name.endswith(".bak"):
                continue
            try:
                doc = json.loads(f.read_text())
            except Exception as _e:
                _note_bad("DA checkpoint", _e, str(f), loud=True)
                continue
            llm = doc.get("_llm_output") or {}
            d = str(llm.get("direction") or "").upper()
            if d not in ("BUY", "SELL_SHORT", "SKIP"):
                continue
            ts = doc.get("timestamp") or llm.get("_timestamp")
            rows.append({
                "source": "da_checkpoint",
                "src_key": "%s|%s" % (day, doc.get("symbol") or f.name[:-5]),
                "verdict_date": day,
                "symbol": doc.get("symbol") or f.name[:-5],
                "direction": d,
                "skip_reason": llm.get("skip_reason"),
                "confidence": llm.get("score"),
                "created_at": datetime.fromtimestamp(float(ts), _et()).isoformat() if ts else None,
            })
    _report_bad()
    return rows


def _to_et(created_at: Optional[str]) -> Optional[datetime]:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(str(created_at))
    except Exception:
        return None
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(_et())


def build_rows(verdicts: List[dict], bars1m, daily, uni: Universe) -> List[dict]:
    """verdict → 固定窗口观测行 (含未执行/弃权)。"""
    out: List[dict] = []
    now = datetime.now(_et()).isoformat(timespec="seconds")
    for v in verdicts:
        sym_raw = str(v.get("symbol") or "")
        sym = sym_raw.replace(".", "_")
        day = v.get("verdict_date") or ""
        if not sym or len(day) != 10:
            continue
        direction, dir_source = recover_direction(v.get("direction"), v.get("skip_reason"))
        sign = dir_sign(direction)
        dt = _to_et(v.get("created_at"))
        common = {
            "source": v["source"], "src_key": v["src_key"], "verdict_date": day,
            "symbol": sym_raw, "direction": direction, "dir_source": dir_source,
            "confidence": v.get("confidence"), "computed_at": now,
        }

        # ---- basis='1m': 盘中锚, 30m/1h/2h ----
        if dt is not None and sym in bars1m:
            i, clamped = anchor_index(dt)
            m = bars1m[sym].get(day) or {}
            if m.get(i):
                for hz, mins in INTRADAY_H:
                    j = min(i + mins, 390)
                    b = m.get(j)
                    if not b:
                        continue
                    sym_ret = b / m[i] - 1.0
                    u = uni.intraday(day, i, j)
                    ex = excess(sym_ret, u)
                    out.append(dict(common, basis="1m", horizon=hz,
                                    anchor_hhmm="%02d:%02d" % (dt.hour, dt.minute),
                                    anchor_clamped=int(clamped),
                                    ret=round(sym_ret, 6),
                                    uni_ret=None if u is None else round(u, 6),
                                    excess_long=None if ex is None else round(ex, 6),
                                    excess_dir=None if (ex is None or sign is None) else round(sign * ex, 6)))

        # ---- basis='daily': 开盘锚, eod/t1/t3 ----
        if sym in daily:
            daily_sym = daily[sym].get(day)
            if daily_sym and daily_sym["o"] > 0:
                o = daily_sym["o"]
                for hz in DAILY_H:
                    if hz == "eod":
                        c = daily_sym["c"]
                    else:
                        nxt = _nth_trading_day(daily[sym], day, 1 if hz == "t1" else 3)
                        c = daily[sym][nxt]["c"] if nxt else None
                    if not c or c <= 0:
                        continue
                    sym_ret = c / o - 1.0
                    u = uni.daily_ret(day, hz)
                    ex = excess(sym_ret, u)
                    out.append(dict(common, basis="daily", horizon=hz,
                                    anchor_hhmm="09:30", anchor_clamped=1,
                                    ret=round(sym_ret, 6),
                                    uni_ret=None if u is None else round(u, 6),
                                    excess_long=None if ex is None else round(ex, 6),
                                    excess_dir=None if (ex is None or sign is None) else round(sign * ex, 6)))
    return out


# ─────────────────────────── 落库 ───────────────────────────

DDL = """CREATE TABLE IF NOT EXISTS verdict_outcomes (
    source TEXT NOT NULL,
    src_key TEXT NOT NULL,
    basis TEXT NOT NULL,
    horizon TEXT NOT NULL,
    verdict_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    dir_source TEXT NOT NULL,
    confidence REAL,
    anchor_hhmm TEXT,
    anchor_clamped INTEGER,
    ret REAL,
    uni_ret REAL,
    excess_long REAL,
    excess_dir REAL,
    computed_at TEXT,
    PRIMARY KEY (source, src_key, basis, horizon)
)"""


def persist(conn: sqlite3.Connection, rows: List[dict]) -> int:
    conn.execute(DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verdict_outcomes_date "
                 "ON verdict_outcomes (verdict_date, source, horizon)")
    cols = ("source", "src_key", "basis", "horizon", "verdict_date", "symbol",
            "direction", "dir_source", "confidence", "anchor_hhmm", "anchor_clamped",
            "ret", "uni_ret", "excess_long", "excess_dir", "computed_at")
    sql = "INSERT OR REPLACE INTO verdict_outcomes (%s) VALUES (%s)" % (
        ",".join(cols), ",".join("?" * len(cols)))
    with conn:
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


# ─────────────────────────── 面板 ───────────────────────────

def _stats(xs: List[float]) -> Tuple[float, float, int]:
    vals = [x for x in xs if x is not None and isinstance(x, (int, float)) and math.isfinite(x)]
    n = len(vals)
    if n < 3:
        return (float("nan"), float("nan"), n)
    m = sum(vals) / n
    var = sum((x - m) ** 2 for x in vals) / (n - 1)
    if var <= 0:
        return (m, float("nan"), n)
    return (m, m / math.sqrt(var / n), n)


def _cluster_t(pairs: List[Tuple[str, float]]) -> Tuple[float, float, int]:
    by: Dict[str, List[float]] = {}
    for d, v in pairs:
        if v is not None and isinstance(v, (int, float)) and math.isfinite(v):
            by.setdefault(d, []).append(v)
    daily = [sum(v) / len(v) for v in by.values()]
    m, t, n = _stats(daily)
    return m, t, len(by)


def _spearman(a: List[float], b: List[float]) -> Optional[float]:
    pairs = [(x, y) for x, y in zip(a, b)
             if isinstance(x, (int, float)) and isinstance(y, (int, float))
             and math.isfinite(x) and math.isfinite(y)]
    if len(pairs) < 8:
        return None
    ra = _rank([p[0] for p in pairs])
    rb = _rank([p[1] for p in pairs])
    if len(set(ra)) < 3:
        return None
    n = len(pairs)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va > 0 and vb > 0 else None


def _rank(xs: List[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    ranks = [0.0] * len(xs)
    for pos, k in enumerate(order):
        ranks[k] = float(pos)
    return ranks


def report(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    where = " WHERE 1=1"
    args: List[object] = []
    if since:
        where += " AND verdict_date >= ?"
        args.append(since)
    rows = [dict(r) for r in conn.execute(
        "SELECT source, basis, horizon, verdict_date, direction, dir_source, "
        "confidence, excess_long, excess_dir FROM verdict_outcomes" + where, args)]

    out: dict = {"as_of": datetime.now(_et()).isoformat(timespec="seconds"),
                 "since": since, "n_rows": len(rows), "blocks": []}
    print("=== verdict_outcomes 覆盖 ===")
    cov: Dict[Tuple[str, str], int] = {}
    for r in rows:
        cov[(r["source"], r["basis"])] = cov.get((r["source"], r["basis"]), 0) + 1
    for (src, basis), n in sorted(cov.items()):
        n_tot = conn.execute(
            "SELECT COUNT(DISTINCT src_key) FROM verdict_outcomes WHERE source=? AND basis=?",
            (src, basis)).fetchone()[0]
        print("  %-14s basis=%-5s 行=%-5d 独立verdict=%d" % (src, basis, n, n_tot))

    print("\n=== 选择力 (excess_dir, bps) — 方向调整, 已去同日全宇宙均值 ===")
    print("  %-14s %-6s %-6s %6s %5s %10s %8s %6s" %
          ("source", "basis", "hz", "n", "ndays", "mean", "t", "day_t"))
    for src in sorted({r["source"] for r in rows}):
        for basis in sorted({r["basis"] for r in rows if r["source"] == src}):
            for hz in sorted({r["horizon"] for r in rows if r["source"] == src and r["basis"] == basis}):
                seg = [r for r in rows if r["source"] == src and r["basis"] == basis
                       and r["horizon"] == hz and r["excess_dir"] is not None]
                if not seg:
                    continue
                m, t, n = _stats([r["excess_dir"] for r in seg])
                cm, ct, nd = _cluster_t([(r["verdict_date"], r["excess_dir"]) for r in seg])
                print("  %-14s %-6s %-6s %6d %5d %9.1fbps %8.2f %6.2f" %
                      (src, basis, hz, n, nd, m * 1e4, t, ct))
                out["blocks"].append({"source": src, "basis": basis, "horizon": hz,
                                      "metric": "excess_dir_bps", "n": n, "n_days": nd,
                                      "mean_bps": None if m != m else round(m * 1e4, 2),
                                      "t": None if t != t else round(t, 2),
                                      "day_cluster_t": None if ct != ct else round(ct, 2)})

    print("\n=== 方向分布 (含弃权) ===")
    for src in sorted({r["source"] for r in rows}):
        dist: Dict[str, int] = {}
        for r in rows:
            if r["source"] == src:
                dist[r["direction"]] = dist.get(r["direction"], 0) + 1
        tot = sum(dist.values())
        shares = ", ".join("%s=%.0f%%" % (k, 100.0 * v / tot) for k, v in sorted(dist.items(), key=lambda x: -x[1]))
        print("  %-14s n=%-6d %s" % (src, tot, shares))
        out.setdefault("direction_mix", {})[src] = {k: v for k, v in dist.items()}

    print("\n=== 置信度学会了吗 (rank IC: confidence vs excess_dir, 逐日横截面) ===")
    print("  ⚠️ 必须按方向分列: 混算会把「空头整体赚不赚」误读成「置信度准不准」")
    for src in sorted({r["source"] for r in rows}):
        for basis in sorted({r["basis"] for r in rows if r["source"] == src}):
            for hz in sorted({r["horizon"] for r in rows if r["source"] == src and r["basis"] == basis}):
                for dlabel, dflt in (("ALL", None), ("BUY", "BUY"), ("SELL_SHORT", "SELL_SHORT")):
                    ics = []
                    days = sorted({r["verdict_date"] for r in rows if r["source"] == src})
                    for day in days:
                        g = [r for r in rows if r["source"] == src and r["basis"] == basis
                             and r["horizon"] == hz and r["verdict_date"] == day
                             and r["excess_dir"] is not None
                             and (dflt is None or r["direction"] == dflt)]
                        ic = _spearman([r["confidence"] for r in g], [r["excess_dir"] for r in g])
                        if ic is not None:
                            ics.append(ic)
                    if len(ics) < 5:
                        continue
                    m, t, n = _stats(ics)
                    pos = sum(1 for x in ics if x > 0)
                    print("  %-14s %-6s %-6s %-11s IC=%+.3f t=%+.2f days=%d 同号=%d/%d" %
                          (src, basis, hz, dlabel, m, t, n, pos, n))
                    out["blocks"].append({"source": src, "basis": basis, "horizon": hz,
                                          "direction": dlabel,
                                          "metric": "confidence_rank_ic", "days": n,
                                          "ic": round(m, 4), "t": round(t, 2),
                                          "same_sign": "%d/%d" % (pos, n)})
    return out


# ─────────────────────────── main ───────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="判别力反事实台账 (record-only)")
    ap.add_argument("--db", default=None, help="默认生产库; 测试请显式指定")
    ap.add_argument("--since", default=None, help="只算该 ET 日期之后的 verdict")
    ap.add_argument("--all", action="store_true", help="全量重算 (等同 --since 空)")
    ap.add_argument("--days", type=int, default=10, help="增量窗口天数 (默认 10)")
    ap.add_argument("--report", action="store_true", help="落库后打印判别力面板")
    ap.add_argument("--report-only", action="store_true", help="只读面板, 不落库")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else PROD_DB
    out_path = Path(args.out)
    if os.environ.get("PYTEST_CURRENT_TEST") and not args.db:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy(str(PROD_DB), tmp.name)
        db, out_path = Path(tmp.name), Path(tempfile.mktemp(suffix=".json"))
    if not db.exists():
        print("DB 不存在: %s" % db, file=sys.stderr)
        return 2

    since = None if args.all else args.since
    if since is None and not args.all and not args.report_only:
        since = _days_ago(max(1, args.days))

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        if not args.report_only:
            verdicts = load_instruction_verdicts(conn, since) + load_da_verdicts(since)
            syms = {str(v["symbol"]).replace(".", "_") for v in verdicts}
            bars1m = load_bars_1m()
            daily = load_bars_daily()
            uni = Universe(bars1m, daily)
            rows = build_rows(verdicts, bars1m, daily, uni)
            n = persist(conn, rows)
            cob = sum(1 for r in rows if r["excess_dir"] is not None)
            print("verdict=%d → 观测行=%d (方向可调整 %d, %.0f%%) 落库=%d  since=%s" %
                  (len(verdicts), len(rows), cob, 100.0 * cob / max(len(rows), 1), n, since))
            miss = [v for v in verdicts
                    if not any(r["src_key"] == v["src_key"] and r["source"] == v["source"] for r in rows)]
            if miss:
                print("无可算窗口 (缺 bar): %d 条, 例: %s" %
                      (len(miss), ", ".join("%s/%s" % (m["verdict_date"], m["symbol"]) for m in miss[:5])))
        if args.report or args.report_only:
            payload = report(conn, since=None)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            print("\n面板已写: %s" % out_path)
    finally:
        conn.close()
    return 0


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(_et()) - timedelta(days=n)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sys.exit(main())