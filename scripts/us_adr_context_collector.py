"""RFC-218 §25 C 批 · HK→US 时间差研究之美股对应腿 (record-only 影子).

用户裁决链 (2026-11-27/28): 港股定位=候选发现与背景输入 → 采集器三处硬化 B1/B2/B3
完成 → 本批=美股对应腿第一步: **先同公司可交易 ADR** (交易所上市 4 只), 行业映射另批。

硬边界 (零判定消费, 与 HK 采集器同):
  - 不接 DA prompt / sizing / gates / 方向; 不下单; 不落库; 不推送。
  - 只用 get_market_snapshot (探针 2026-11-28 ret_code=0 实证, /tmp/rfc218_c_us_probe2.log);
    **OTC 票绝不入请求**——探针实证 TCEHY 混入批次致整请求 ret=-1 失败。
  - 写盘失败 / OpenD 不可达 → warning, 恒 exit 0 (fail-safe)。
  - flock 锁内 RMW + mkstemp/os.replace 原子写 (复用 HK 采集器/§23 范式)。
  - 落 ``data/shadow/hk_us_linkage/us_leg/<ET交易日>.json``; 注册表 glob 非递归
    (shadow_registry._count_samples ``*.json``) ⇒ 不污染 HK 样本计数。注册表条目更新
    属 app/ 改动, 排队治理批 (本批无 app/ 改动无重启)。

采样窗 (ET 时区闸, DST 自动适应; cron CST 宽窗 + 闸拒窗外):
  premarket 04:00-09:30 ET / regular_open 09:30-10:15 ET (含 09:45 入场参考锚)。
  半日市 13:00 ET 收盘不影响本窗。周末/假日 → 无产物 (update_time 日期闸跳过)。

配对合同 (研究层 D 消费): date=ET 交易日; HK 同 ET 日期 day-doc 配对 (HK 收盘→美股
盘前/开盘)。感恩节次日型前收区间差 (HK 相对前一日 vs US 相对前一交易日) 由研究层统一,
采集层只如实记录双方 prev_close 原始值。chg_pct=US 日窗口, 与 HK 窗口不同, 不得直接相减。

回滚 = 删 crontab 两行 (7,22,37,52 16-23 * * 1-5 / 45 21,22 * * 1-5); 产物留档。
"""

import argparse
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 复用 HK 采集器稳定原语 (B1 有限值守卫/原子写/互斥/穿零事件单一真源; 模块导入零副作用)。
from scripts.hk_market_context_collector import (
    _atomic_write_json,
    _num,
    _observation_lock,
    chg_pct,
    derive_events,
)

CST = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")

OUT_DIR = Path("data/shadow/hk_us_linkage/us_leg")
SCHEMA_VERSION = 1
MAX_TICK_ROWS = 40          # 预期 ~26 tick/日 (盘前 22 + 开盘窗 4), 40 留余量

# 同公司可交易 ADR (交易所上市, OpenD 探针 ret_code=0)。shares_per_ads 凭据双源:
# ① 公开 ADS 条款 (BABA 1:8 自 2019 / JD 1:2 / BIDU 1:8 自 2021 / NTES 1:5);
# ② 2026-11-27 真实港美股平价交叉验证: HK 收盘 × ratio / US last 隐含 FX
#    7.766-7.894 ≈ 联系汇率 7.8 中枢 (JD/NTES 带内, BABA/BIDU 微超带=美股盘后漂移,
#    比例识别唯一: 其余比例隐含 FX 远离 7.8)。探针日志 /tmp/rfc218_c_us_probe2.log。
ADR_META = {
    "US.BABA": {"hk_code": "HK.09988", "name": "阿里巴巴", "shares_per_ads": 8},
    "US.JD": {"hk_code": "HK.09618", "name": "京东", "shares_per_ads": 2},
    "US.BIDU": {"hk_code": "HK.09888", "name": "百度", "shares_per_ads": 8},
    "US.NTES": {"hk_code": "HK.09999", "name": "网易", "shares_per_ads": 5},
}
# OTC 不可用 (OpenD 探针实证; 混入批次会毒化整请求) —— 只标状态, 绝不请求。
OTC_UNAVAILABLE = {
    "US.TCEHY": {"hk_code": "HK.00700", "name": "腾讯"},
    "US.MPNGY": {"hk_code": "HK.03690", "name": "美团"},
    "US.XIAOMY": {"hk_code": "HK.01810", "name": "小米"},
}
# 声明近似 (联系汇率中枢), 仅供研究粗平价; 精确平价/转换成本研究 (D) 须用实时 FX。
FX_HKD_PER_USD_DECLARED = 7.8

UNITS = {
    "chg_pct": "percent",            # (last_usd/prev_close_usd - 1)*100, US 日窗口
    "first_obs_chg_pct": "percent",  # 首个采样观测 (非开盘; 开盘价未接入)
    "obs_high_chg_pct": "percent",   # 采样观测 chg 极大值 (非真实盘中高)
    "obs_low_chg_pct": "percent",    # 采样观测 chg 极小值 (非真实盘中低)
    "latest_chg_pct": "percent",
    "last_usd": "USD_absolute",
    "prev_close_usd": "USD_absolute",
    "shares_per_ads": "count",
    "fx_hkd_per_usd_declared": "HKD_per_USD_approx",
}


def _now_cst():
    return datetime.now(CST)


def session_for(dt_et):
    """美股采样窗 (ET): premarket 04:00-09:30 / regular_open 09:30-10:15。周末→None。

    用 ET 时区闸而非固定 CST 小时 → 冬夏令时自动适应 (DST 偏移 13h/12h)。
    半日市 13:00 ET 收盘在本窗之后, 不影响。
    """
    if dt_et.weekday() >= 5:
        return None
    m = dt_et.hour * 60 + dt_et.minute
    if 240 <= m < 570:        # 04:00-09:30 盘前
        return "premarket"
    if 570 <= m < 615:        # 09:30-10:15 开盘窗 (含 09:45 入场参考)
        return "regular_open"
    return None


def build_tick_row(ts_cst, ts_et, session, snapshot_rows, today_et):
    """snapshot_rows: {code: row dict} → 紧凑 tick。成分级时点/状态守卫 (B2 范式):
    update_time 日期==今日 ET ∧ NORMAL, 否则当日 chg 标 None + stale_reason (不归零,
    原始 last/prev_close/update_time/sec_status 保留可复算)。"""
    tickers = {}
    for code in ADR_META:
        r = snapshot_rows.get(code)
        if r is None:
            tickers[code] = {"last_usd": None, "prev_close_usd": None, "chg_pct": None,
                             "update_time": "", "sec_status": "",
                             "stale_reason": "missing_from_snapshot"}
            continue
        last = _num(r.get("last_price"))
        prev = _num(r.get("prev_close_price"))
        rec = {
            "last_usd": last,
            "prev_close_usd": prev,
            "chg_pct": chg_pct(last, prev),
            "update_time": str(r.get("update_time") or ""),
            "sec_status": str(r.get("sec_status") or ""),
        }
        reasons = []
        if rec["update_time"][:10] != today_et:
            reasons.append("quote_not_today")
        if rec["sec_status"] != "NORMAL":
            reasons.append("non_normal_status")
        if reasons:
            rec["chg_pct"] = None
            rec["stale_reason"] = "+".join(reasons)
        tickers[code] = rec
    return {"ts": ts_cst, "ts_et": ts_et, "session": session, "tickers": tickers}


def is_stale_tick(tick):
    """全部 ADR 无当日新鲜报价 → 无法确认美股交易日 → stale (宁缺毋滥, 镜像 HK 指数闸)。
    部分新鲜 → 不 stale (新鲜子集照常记录, 缺失成分如实标 None)。"""
    tk = tick.get("tickers") or {}
    if not tk:
        return True
    return all(r.get("chg_pct") is None and r.get("stale_reason") for r in tk.values())


def _rollup(series):
    """采样观测 rollup (B3 诚实命名): first_obs/obs_high/obs_low/latest。
    真实盘中 OHLC 未接入 (盘前 high/low 语义歧义, 另批); 稀疏采样不得当完整日内路径。"""
    vals = [p["chg_pct"] for p in series if p.get("chg_pct") is not None]
    if not vals:
        return {"first_obs_chg_pct": None, "obs_high_chg_pct": None,
                "obs_low_chg_pct": None, "latest_chg_pct": None}
    return {"first_obs_chg_pct": vals[0], "obs_high_chg_pct": max(vals),
            "obs_low_chg_pct": min(vals), "latest_chg_pct": vals[-1]}


def derive_day_doc(tick_rows, date_str):
    """从 tick_rows (真值源) 重算全日 rollup — 幂等。date_str=ET 交易日。"""
    tickers_roll, events = {}, []
    for code, meta in ADR_META.items():
        series = [{"ts": t["ts"], "chg_pct": (t.get("tickers") or {}).get(code, {}).get("chg_pct")}
                  for t in tick_rows]
        series = [p for p in series if p["chg_pct"] is not None]
        if not series:
            continue
        roll = _rollup(series)
        roll["hk_code"] = meta["hk_code"]
        roll["name"] = meta["name"]
        roll["shares_per_ads"] = meta["shares_per_ads"]
        last_rec = (tick_rows[-1].get("tickers") or {}).get(code, {})
        roll["latest_last_usd"] = last_rec.get("last_usd")
        roll["latest_prev_close_usd"] = last_rec.get("prev_close_usd")
        roll["series"] = series
        tickers_roll[code] = roll
        events.extend(derive_events("adr:" + code, series))
    events.sort(key=lambda e: e["ts"])
    return {
        "date": date_str,
        "market": "US_ADR",
        "schema_version": SCHEMA_VERSION,
        "units": UNITS,
        "source": "OpenD get_market_snapshot (read-only, exchange-listed ADR only, no OTC)",
        "adr_meta": {c: dict(m) for c, m in ADR_META.items()},
        "fx_hkd_per_usd_declared": FX_HKD_PER_USD_DECLARED,
        "otc_unavailable": {c: dict(m) for c, m in OTC_UNAVAILABLE.items()},
        "pairing_note": "date=ET 交易日; 与 HK 同 ET 日期 day-doc 配对; chg=US 日窗口, "
                        "与 HK 窗口不同 (感恩节次日前收差由研究层统一), 不得直接相减",
        "trading_day": True,
        "ticks": len(tick_rows),
        "first_ts": tick_rows[0]["ts"] if tick_rows else None,
        "last_ts": tick_rows[-1]["ts"] if tick_rows else None,
        "sessions": sorted({t.get("session") for t in tick_rows if t.get("session")}),
        "tickers": tickers_roll,
        "events": events,
        "tick_rows": tick_rows,
    }


def _default_ctx_factory():
    from futu import OpenQuoteContext  # 懒 import: 模块导入零副作用, 测试可注入假 ctx
    import os
    return OpenQuoteContext(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )


def _snapshot_rows(ctx):
    from futu import RET_OK
    # 只请求交易所上市 4 只 —— OTC 混入会毒化整批 (探针实证 ret=-1)。
    ret, data = ctx.get_market_snapshot(list(ADR_META))
    if ret != RET_OK:
        raise RuntimeError(f"get_market_snapshot ret={ret}: {data}")
    cols = [c for c in ("code", "last_price", "prev_close_price", "update_time",
                        "sec_status") if c in data.columns]
    return {r["code"]: r for r in data[cols].to_dict("records")}


def record_tick(now=None, force=False, dry_run=False, ctx_factory=None, out_dir=None):
    """单 tick 采集编排。恒不抛 (fail-safe); 返回 result dict。

    force=True: 绕过时段闸, 落 ``_verify/`` (不污染样本计数)。dry_run=True: 零写盘。
    """
    now = now or _now_cst()
    now_et = now.astimezone(ET)
    base = Path(out_dir) if out_dir else OUT_DIR
    today_et = now_et.strftime("%Y-%m-%d")
    result = {"status": None, "date": today_et, "ts": now.isoformat(timespec="seconds")}
    try:
        session = session_for(now_et)
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
        tick = build_tick_row(now.isoformat(timespec="seconds"),
                              now_et.isoformat(timespec="seconds"), session, rows, today_et)
        if is_stale_tick(tick) and not force:
            result["status"] = "skipped_stale"
            return result
        if dry_run:
            result["status"] = "dry_run"
            result["would_write"] = str((base / "_verify" if force else base) / (today_et + ".json"))
            result["tick"] = tick
            return result
        target_dir = base / "_verify" if force else base
        path = target_dir / (today_et + ".json")
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
            new_doc = derive_day_doc(tick_rows, today_et)
            _atomic_write_json(path, new_doc)
        result["status"] = "written"
        result["path"] = str(path)
        result["ticks"] = len(tick_rows)
        return result
    except Exception as exc:
        print(f"[us_adr_context] WARNING: tick 采集失败 {type(exc).__name__}: {exc}")
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="美股 ADR 对应腿影子采集 (record-only, RFC-218 §25 C)")
    ap.add_argument("--dry-run", action="store_true", help="只算不写 (零写盘)")
    ap.add_argument("--force", action="store_true", help="窗外验证 tick, 落 _verify/ 子目录")
    args = ap.parse_args(argv)
    result = record_tick(force=args.force, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0  # fail-safe: 观测失败不使 cron 非零退出


if __name__ == "__main__":
    sys.exit(main())
