"""RFC-218 §25 · HK→US 时间差行业/动态趋势参考输入 — 影子采集器 (record-only).

用户裁决 (2026-11-27): 「港股的波动趋势，不仅仅只是看个股，看行业和动态趋势，可以作为
信息参考输入，打港股和美股的时间差」。本批 = 影子先行第一步 (DEB-20261127-hk-us-sector-
trend-context): 只读 OpenD 快照 → 指数 / 行业篮子 / ADR 港股腿 的日内序列 + 翻红/翻绿
事件 → ``data/shadow/hk_us_linkage/<HK交易日>.json``。

硬边界 (本批零判定消费):
  - 不接 DA prompt / sizing / gates / 方向判定; 不下单; 不落库; 不推送 (无料不推, 观测
    数据本身不是待裁料, n≥60 交易日 + 传导研究后才谈 DA 参考输入注入, 再走 §10.4 A/B)。
  - 只用 ``get_market_snapshot`` (权限探针 §23/§25 已证 ret_code=0), 无订阅 / 无历史 K 线。
  - 写盘失败 / OpenD 不可达 → 打 warning, 绝不抛进 cron; 进程恒 exit 0 (fail-safe)。
  - 并发安全: flock 锁内读-改-写 + mkstemp/os.replace 原子落盘 (复用 §23 观测原子性范式)。
  - 计数轴 = HK 交易日数 (每日 1 文档, ticks=int; 注册表目录分支按日计 1, 不是 ticks 累加)。
    ``--force`` 验证 tick 落 ``_verify/`` 子目录 (注册表 glob 不递归 ⇒ 不污染样本计数)。

回滚 = 删 crontab 两行 (7,22,37,52 9-11,13-15 * * 1-5 / 2 12,16 * * 1-5); 产物留档。
"""

import argparse
import contextlib
import fcntl
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")  # HKT == CST (UTC+8), 港股时段与服务器时区一致

OUT_DIR = Path("data/shadow/hk_us_linkage")
SCHEMA_VERSION = 1
MAX_TICK_ROWS = 40          # 预期 ~24 tick/日 (含两收盘锚), 40 留余量防异常刷屏
FLAT_BAND_PP = 0.05         #  breadth 平盘带: |chg| < 0.05 个百分点记 flat (单位声明见 UNITS)

# 量纲声明 (军规 2026-09-02: 新字段必带单位 + 契约断言)。
UNITS = {
    "chg_pct": "percent",            # (last/prev_close - 1) * 100
    "high_chg_pct": "percent",
    "low_chg_pct": "percent",
    "chg_pct_eq": "percent",         # 行业等权均值
    "dispersion_pp": "percent_points",  # 行业内 max-min chg
    "turnover_hkd": "HKD_absolute",
    "price": "HKD_absolute",
    "breadth": "count",
}

INDEX_CODES = ("HK.800000", "HK.800700", "HK.800100")  # 恒生指数 / 恒生科技 / 国企指数
INDEX_NAMES = {"HK.800000": "恒生指数", "HK.800700": "恒生科技指数", "HK.800100": "国企指数"}

# 行业篮子 (curated, 等权; 成分股 2026-11-27 探针全 NORMAL。全市场 113 行业板块 breadth
# 需 get_plate_stock 逐板块拉成分 (配额面未探针), 列为 DEB 后续扩展, 本批不实施)。
SECTOR_BASKETS = {
    "tech_internet": ("HK.00700", "HK.09988", "HK.03690", "HK.01024",
                      "HK.09618", "HK.09888", "HK.09999", "HK.01810"),
    "semis": ("HK.00981", "HK.01347", "HK.00522"),
    "autos_ev": ("HK.01211", "HK.02015", "HK.09866", "HK.09868", "HK.00175"),
    "pharma_biotech": ("HK.02269", "HK.01177", "HK.06160"),
    "banks_financials": ("HK.00939", "HK.01398", "HK.02318", "HK.03988"),
    "energy_resources": ("HK.00857", "HK.00883", "HK.01088"),
    "consumer": ("HK.02020", "HK.02331", "HK.06862"),
    "telecom_utilities": ("HK.00941", "HK.00762", "HK.00002"),
}

# HK→US ADR 映射: 与 app/catalyst/sources/hkex_source.py::HK_STOCK_MAPPING 同源。
# 刻意不 import 该模块 (新闻面重依赖 requests/bs4/news_v2), parity 由契约测试钉死。
HK_ADR_MAPPING = {
    "00700": ("腾讯", "US.TCEHY"),
    "09988": ("阿里巴巴", "US.BABA"),
    "09618": ("京东", "US.JD"),
    "03690": ("美团", "US.MPNGY"),
    "09888": ("百度", "US.BIDU"),
    "01810": ("小米", "US.XIAOMY"),
    "09999": ("网易", "US.NTES"),
}
# 探针实证 (2026-11-27): OpenD 不提供 OTC 美股行情 (TCEHY 报「暂不提供美股 OTC 市场行情」)。
OTC_ADR_TICKERS = frozenset({"US.TCEHY", "US.MPNGY", "US.XIAOMY"})


def _all_codes():
    codes = list(INDEX_CODES)
    for members in SECTOR_BASKETS.values():
        for c in members:
            if c not in codes:
                codes.append(c)
    return codes


def _now_cst():
    return datetime.now(CST)


def session_for(dt):
    """港股时段闸 (CST/HKT): 早盘 09:30-12:00 / 午盘 13:00-16:00 / 两收盘锚窗。

    返回 morning|afternoon|morning_close|close|None。周末/盘前/午休/窗外 → None。
    cron 已限 1-5, 此处再核周末 (防 --force 外的手工误跑)。
    """
    if dt.weekday() >= 5:
        return None
    m = dt.hour * 60 + dt.minute
    if 570 <= m < 720:        # 09:30-12:00
        return "morning"
    if 720 <= m < 730:        # 12:00-12:10 早盘收盘锚
        return "morning_close"
    if 780 <= m < 960:        # 13:00-16:00
        return "afternoon"
    if 960 <= m < 970:        # 16:00-16:10 全日收盘锚
        return "close"
    return None


def chg_pct(last, prev_close):
    """涨跌幅 (percent)。未知/非法 (None/bool/非正 prev_close/非正 last) → None, 不归零。"""
    if isinstance(last, bool) or isinstance(prev_close, bool):
        return None
    try:
        last = float(last)
        prev_close = float(prev_close)
    except (TypeError, ValueError):
        return None
    if prev_close <= 0 or last <= 0:
        return None
    return round((last / prev_close - 1.0) * 100.0, 4)


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN guard


def aggregate_sector(stock_rows):
    """行业等权聚合。stock_rows: {code: {chg_pct, turnover, ...}}。

    breadth: up = chg >= +FLAT_BAND_PP, down = chg <= -FLAT_BAND_PP, 其余 flat。
    全缺失 → chg_pct_eq=None (不归零), n_available=0。
    """
    chgs, turns, missing = [], [], []
    up = flat = down = 0
    for code, r in stock_rows.items():
        c = r.get("chg_pct")
        if c is None:
            missing.append(code)
            continue
        chgs.append(c)
        if c >= FLAT_BAND_PP:
            up += 1
        elif c <= -FLAT_BAND_PP:
            down += 1
        else:
            flat += 1
        t = _num(r.get("turnover"))
        if t is not None:
            turns.append(t)
    return {
        "chg_pct_eq": round(statistics.mean(chgs), 4) if chgs else None,
        "chg_pct_median": round(statistics.median(chgs), 4) if chgs else None,
        "dispersion_pp": round(max(chgs) - min(chgs), 4) if chgs else None,
        "breadth": {"up": up, "flat": flat, "down": down},
        "turnover_hkd": round(sum(turns), 2) if turns else None,
        "n_available": len(chgs),
        "n_missing": len(missing),
        "missing": missing,
    }


def build_tick_row(ts, session, snapshot_rows, stale=False):
    """snapshot_rows: {code: row dict (futu snapshot)} → 紧凑 tick (序列真值源)。"""
    indices, stocks = {}, {}
    for code, r in snapshot_rows.items():
        last = _num(r.get("last_price"))
        prev = _num(r.get("prev_close_price"))
        rec = {
            "last": last,
            "prev_close": prev,
            "chg_pct": chg_pct(last, prev),
            "update_time": str(r.get("update_time") or ""),
            "sec_status": str(r.get("sec_status") or ""),
        }
        if code in INDEX_CODES:
            hi = _num(r.get("high_price"))
            lo = _num(r.get("low_price"))
            rec["high_chg_pct"] = chg_pct(hi, prev)
            rec["low_chg_pct"] = chg_pct(lo, prev)
            rec["turnover"] = _num(r.get("turnover"))
            indices[code] = rec
        else:
            rec["turnover"] = _num(r.get("turnover"))
            stocks[code] = rec
    sectors = {}
    for name, members in SECTOR_BASKETS.items():
        sectors[name] = aggregate_sector({c: stocks.get(c, {"chg_pct": None}) for c in members})
    return {
        "ts": ts,
        "session": session,
        "stale": bool(stale),
        "indices": indices,
        "sectors": sectors,
        "stocks": stocks,
    }


def is_stale_tick(tick, today_str):
    """假日/停市判定: 任一指数 update_time 日期 != 今日 或 sec_status != NORMAL → stale。

    指数全缺 → 无法确认交易日 → stale (宁缺毋滥, 不伪装样本)。
    """
    idx = tick.get("indices") or {}
    if not idx:
        return True
    for r in idx.values():
        ut = r.get("update_time") or ""
        if ut[:10] != today_str or r.get("sec_status") != "NORMAL":
            return True
    return False


def derive_events(target, series):
    """翻红/翻绿事件: 相邻 tick chg 符号穿越 0 (负→非负 = sign_cross_up, 反之 down)。"""
    events = []
    prev = None
    for point in series:
        cur = point.get("chg_pct")
        if cur is None:
            continue
        if prev is not None:
            if prev < 0 <= cur:
                events.append({"ts": point["ts"], "kind": "sign_cross_up",
                               "target": target, "from_pct": prev, "to_pct": cur})
            elif prev >= 0 > cur:
                events.append({"ts": point["ts"], "kind": "sign_cross_down",
                               "target": target, "from_pct": prev, "to_pct": cur})
        prev = cur
    return events


def _rollup(series):
    vals = [p["chg_pct"] for p in series if p.get("chg_pct") is not None]
    if not vals:
        return {"open_chg_pct": None, "high_chg_pct": None,
                "low_chg_pct": None, "latest_chg_pct": None}
    return {"open_chg_pct": vals[0], "high_chg_pct": max(vals),
            "low_chg_pct": min(vals), "latest_chg_pct": vals[-1]}


def derive_day_doc(tick_rows, date_str):
    """从 tick_rows (真值源) 重算全日 rollup — 幂等, 无增量状态漂移。"""
    indices_roll, sectors_roll, events = {}, {}, []
    for code in INDEX_CODES:
        series = [{"ts": t["ts"], "chg_pct": (t.get("indices") or {}).get(code, {}).get("chg_pct")}
                  for t in tick_rows]
        series = [p for p in series if p["chg_pct"] is not None]
        if not series:
            continue
        last_idx = (tick_rows[-1].get("indices") or {}).get(code, {})
        hi = [p["chg_pct"] for p in
              ({"ts": t["ts"], "chg_pct": (t.get("indices") or {}).get(code, {}).get("high_chg_pct")}
               for t in tick_rows) if p["chg_pct"] is not None]
        lo = [p["chg_pct"] for p in
              ({"ts": t["ts"], "chg_pct": (t.get("indices") or {}).get(code, {}).get("low_chg_pct")}
               for t in tick_rows) if p["chg_pct"] is not None]
        roll = _rollup(series)
        roll["high_chg_pct"] = max(hi) if hi else roll["high_chg_pct"]
        roll["low_chg_pct"] = min(lo) if lo else roll["low_chg_pct"]
        roll["name"] = INDEX_NAMES.get(code, code)
        roll["latest_turnover_hkd"] = last_idx.get("turnover")
        roll["series"] = series
        indices_roll[code] = roll
        events.extend(derive_events("index:" + code, series))
    for name in SECTOR_BASKETS:
        series = [{"ts": t["ts"], "chg_pct": (t.get("sectors") or {}).get(name, {}).get("chg_pct_eq")}
                  for t in tick_rows]
        series = [p for p in series if p["chg_pct"] is not None]
        if not series:
            continue
        roll = _rollup(series)
        roll["latest"] = (tick_rows[-1].get("sectors") or {}).get(name, {})
        roll["series"] = series
        sectors_roll[name] = roll
        events.extend(derive_events("sector:" + name, series))
    events.sort(key=lambda e: e["ts"])
    adr = {}
    last_stocks = tick_rows[-1].get("stocks") or {} if tick_rows else {}
    for hk_code, (cname, us_ticker) in HK_ADR_MAPPING.items():
        full = "HK." + hk_code
        if full in last_stocks:
            adr[hk_code] = {
                "name": cname,
                "us_ticker": us_ticker,
                "us_quote_status": ("OTC_UNAVAILABLE" if us_ticker in OTC_ADR_TICKERS
                                    else "EXCHANGE_LISTED_DEFERRED"),
                "chg_pct": last_stocks[full].get("chg_pct"),
            }
    return {
        "date": date_str,
        "market": "HK",
        "schema_version": SCHEMA_VERSION,
        "units": UNITS,
        "source": "OpenD get_market_snapshot (read-only, no subscription, no kline)",
        "trading_day": True,
        "ticks": len(tick_rows),
        "first_ts": tick_rows[0]["ts"] if tick_rows else None,
        "last_ts": tick_rows[-1]["ts"] if tick_rows else None,
        "sessions": sorted({t.get("session") for t in tick_rows if t.get("session")}),
        "constituents": {"indices": dict(INDEX_NAMES), "sectors": {k: list(v) for k, v in SECTOR_BASKETS.items()}},
        "indices": indices_roll,
        "sectors": sectors_roll,
        "adr_hk_legs": adr,
        "events": events,
        "tick_rows": tick_rows,
    }


@contextlib.contextmanager
def _observation_lock(lock_path):
    """观测面互斥 (flock LOCK_EX, 阻塞式短临界区; 复用 §23 executor 范式)。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path, doc):
    """mkstemp 唯一 tmp + os.replace 原子落盘 (防重叠调用互踩, 复用 §23 范式)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, default=str)
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _default_ctx_factory():
    from futu import OpenQuoteContext  # 懒 import: 模块导入零副作用, 测试可注入假 ctx
    return OpenQuoteContext(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )


def _snapshot_rows(ctx):
    from futu import RET_OK
    ret, data = ctx.get_market_snapshot(_all_codes())
    if ret != RET_OK:
        raise RuntimeError(f"get_market_snapshot ret={ret}: {data}")
    cols = [c for c in ("code", "name", "last_price", "prev_close_price", "open_price",
                        "high_price", "low_price", "volume", "turnover", "update_time",
                        "sec_status") if c in data.columns]
    return {r["code"]: r for r in data[cols].to_dict("records")}


def record_tick(now=None, force=False, dry_run=False, ctx_factory=None, out_dir=None):
    """单 tick 采集编排。恒不抛 (fail-safe); 返回 result dict 供日志/测试。

    force=True: 绕过时段/假日闸, 落 ``_verify/`` 子目录 (验证写路径, 不污染样本计数)。
    dry_run=True: 全程零写盘 (含 mkdir)。
    """
    now = now or _now_cst()
    base = Path(out_dir) if out_dir else OUT_DIR
    today = now.strftime("%Y-%m-%d")
    result = {"status": None, "date": today, "ts": now.isoformat(timespec="seconds")}
    try:
        session = session_for(now)
        if session is None and not force:
            result["status"] = "skipped_session"
            return result
        if force:
            session = session or "closed_verify"
        factory = ctx_factory or _default_ctx_factory
        ctx = factory()
        try:
            rows = _snapshot_rows(ctx)
        finally:
            with contextlib.suppress(Exception):
                ctx.close()
        tick = build_tick_row(now.isoformat(timespec="seconds"), session, rows)
        stale = is_stale_tick(tick, today)
        tick["stale"] = stale
        if stale and not force:
            result["status"] = "skipped_stale"
            return result
        if dry_run:
            result["status"] = "dry_run"
            result["would_write"] = str((base / "_verify" if force else base) / (today + ".json"))
            result["tick"] = tick
            return result
        target_dir = base / "_verify" if force else base
        path = target_dir / (today + ".json")
        minute_key = now.strftime("%H:%M")
        with _observation_lock(target_dir.parent / (target_dir.name + ".lock")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
            if not isinstance(doc, dict):
                doc = {}
            tick_rows = doc.get("tick_rows")
            if not isinstance(tick_rows, list):
                tick_rows = []
            if any((t.get("ts") or "")[11:16] == minute_key for t in tick_rows):
                result["status"] = "skipped_dedup"
                return result
            if len(tick_rows) >= MAX_TICK_ROWS:
                result["status"] = "skipped_cap"
                return result
            tick_rows.append(tick)
            new_doc = derive_day_doc(tick_rows, today)
            _atomic_write_json(path, new_doc)
        result["status"] = "written"
        result["path"] = str(path)
        result["ticks"] = len(tick_rows)
        return result
    except Exception as exc:
        # 观测面与决策面解耦: 宁可打 warning, 绝不抛进 cron / 绝不静默假绿。
        print(f"[hk_context] WARNING: tick 采集失败 {type(exc).__name__}: {exc}")
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="HK 行业/趋势影子采集 (record-only, RFC-218 §25)")
    ap.add_argument("--dry-run", action="store_true", help="只算不写 (零写盘)")
    ap.add_argument("--force", action="store_true", help="窗外验证 tick, 落 _verify/ 子目录")
    args = ap.parse_args(argv)
    result = record_tick(force=args.force, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0  # fail-safe: 观测失败不使 cron 非零退出 (error_alert 按日志级别判, 不误报)


if __name__ == "__main__":
    sys.exit(main())
