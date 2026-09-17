#!/usr/bin/env python3
"""[P1.4 2026-09-12] DA 候选选择契约测试的**子进程驱动器** (非 pytest 模块).

为什么跑子进程 (而不是像 test_da_output_contract 那样在 pytest 进程内 import skill):
  被测对象在仓外 skill (`/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py`)。
  在 pytest 进程内导入它会:
    ① 把 skill 目录插到 sys.path[0] 并让 skill 的 `config.py` 顶掉本仓 `config` **包**
       (`app.playbook.config` 随后降级到 ConfigContainer 兜底路径, 影响同会话其他测试);
    ② 触达 `data_collector` / futu SDK —— conftest 顶部专段记录过 futu 非 daemon 线程
       导致解释器退出挂死 1h+ 的实证。
  子进程 + 伪造 `data_collector` / `market_scanner` ⇒ 零网络、零 futu、零 sys.modules 污染,
  且完整复现生产 DA 进程的模块解析语义。

用法: `python da_selection_driver.py --workdir DIR --out RESULT.json`
所有行情由 `KLINE` 表伪造 (未登记的 symbol 取数即抛, 防"静默走真实数据源")。
"""
import argparse
import json
import os
import re
import sys
import types
from datetime import datetime, timedelta, timezone

SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
REPO = "/opt/ftseek/FTSeek-US"
ET = timezone(timedelta(hours=-4))
NOW = datetime.now(ET)
TODAY = NOW.date()

# ── 伪造 data_collector (只被 _fetch_screening_atr 用) ────────────────────
KLINE = {}


def _bars(n=20, vol=0.02, base=100.0, end_offset=2):
    """n 根**已完成**日线 (末根 = 今天-end_offset, 严格早于 asof)。"""
    out, close, day = [], base, TODAY - timedelta(days=end_offset)
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    for d in reversed(days):
        out.append({"time": d.isoformat(), "open": close, "high": close * (1 + vol),
                    "low": close * (1 - vol), "close": close, "complete": True})
        close *= 1.001
    return out


_fake_dc = types.ModuleType("data_collector")


def _collect_kline(symbol, period="1d", count=40):
    key = str(symbol)
    if key not in KLINE:
        raise AssertionError(f"unexpected kline fetch (未登记伪造数据): {key}")
    spec = KLINE[key]
    if spec is None:
        return []
    return _bars(vol=spec.get("vol", 0.02), base=spec.get("base", 100.0),
                 end_offset=spec.get("end_offset", 2))


_fake_dc.collect_kline = _collect_kline
sys.modules["data_collector"] = _fake_dc

# ── 伪造 market_scanner (只被 _ckpt_policy_version 读 BUCKET_ORDER_VERSION) ──
_real_bucket_order = ""
try:
    with open(os.path.join(SKILL, "market_scanner.py"), encoding="utf-8") as _fh:
        _m = re.search(r'^BUCKET_ORDER_VERSION\s*=\s*"([^"]+)"', _fh.read(), re.M)
        _real_bucket_order = _m.group(1) if _m else ""
except OSError:
    pass
_fake_ms = types.ModuleType("market_scanner")
_fake_ms.BUCKET_ORDER_VERSION = _real_bucket_order or "bearish_conviction_v1"
sys.modules["market_scanner"] = _fake_ms

sys.path.insert(0, REPO)
sys.path.insert(0, SKILL)
import pipeline as PL   # noqa: E402  (skill 侧模块, 非本仓 app.playbook.pipeline)

RESULTS = {"skill_pipeline": PL.__file__, "bucket_order_version": _real_bucket_order}
CASES = {}          # name -> callable (main() 设好 WORKDIR 后按登记顺序执行)
WORKDIR = ""


def register(name):
    def deco(fn):
        assert name not in CASES, f"duplicate case: {name}"
        CASES[name] = fn
        return fn
    return deco


def sym(name, score, price=100.0, **kw):
    row = {"symbol": f"US.{name}", "score": score, "price": price}
    row.update(kw)
    return row


def arm(symbols, vol=0.02):
    for s in symbols:
        KLINE[f"US.{s}" if not str(s).startswith("US.") else s] = {"vol": vol}


def scan_of(bullish, bearish, *, all_scored=None, top_k=2):
    return {
        "candidates_total": len(bullish) + len(bearish),
        "market_condition": "driver",
        "top_bullish": bullish[:top_k], "top_bearish": bearish[:top_k],
        "bullish_all": bullish, "bearish_all": bearish, "watchlist_all": [],
        "bucket_order_version": _fake_ms.BUCKET_ORDER_VERSION,
        "all_scored": all_scored if all_scored is not None else bullish + bearish,
        "avoid": [], "sector_rotation": {},
    }


# ══ 阈值与配置真源 ═══════════════════════════════════════════════════════
@register("thresholds")
def _thresholds():
    deps = PL._ftseek_screening_deps()
    thr = PL._da_screening_thresholds()
    clean = None
    if deps:
        try:
            clean = deps["load_candidate_policy"]()
        except Exception as exc:                       # pragma: no cover
            clean = {"error": str(exc)}
    return {"deps_available": bool(deps),
            "deps_keys": sorted(deps) if deps else [],
            "thresholds": {k: thr.get(k) for k in
                           ("version", "min_price", "min_atr_ratio", "low_atr_enabled",
                            "quote_gate_enabled", "borrow_gate_enabled",
                            "max_scan_age_hours", "config_source")},
            "repo_policy": clean}


# ══ 筛选 ATR 口径 ════════════════════════════════════════════════════════
@register("atr")
def _atr():
    KLINE.clear()
    KLINE["US.HIVOL"] = {"vol": 0.02}
    KLINE["US.LOWVOL"] = {"vol": 0.004}
    KLINE["US.NODATA"] = None
    hi = PL._fetch_screening_atr("US.HIVOL", ref_price=100.0)
    hi_noref = PL._fetch_screening_atr("HIVOL")
    lo = PL._fetch_screening_atr("US.LOWVOL", ref_price=100.0)
    nod = PL._fetch_screening_atr("US.NODATA", ref_price=100.0)
    return {
        "high_vol": {k: hi.get(k) for k in ("status", "reason", "ratio_pct",
                                            "denominator", "bars_asof", "basis",
                                            "bars_used", "source")},
        "high_vol_no_ref_price": {k: hi_noref.get(k) for k in
                                  ("status", "denominator", "ratio_pct")},
        "low_vol": {k: lo.get(k) for k in ("status", "ratio_pct")},
        "no_data": {k: nod.get(k) for k in ("status", "reason", "atr", "ratio")},
        "compat_fetch_atr_pct": PL._fetch_atr_pct("HIVOL", ref_price=100.0),
        "compat_fetch_atr_pct_no_data": PL._fetch_atr_pct("NODATA", 100.0),
        "bars_asof_before_today": str(hi.get("bars_asof")) < TODAY.isoformat(),
        # 同一 ATR 被不同参考价的候选复用 (分母在缓存外决定) — 轮内缓存生效证据
        "cache_reused_with_other_denominator": PL._fetch_screening_atr(
            "US.HIVOL", ref_price=50.0).get("denominator"),
        "cache_size_after_run": len(PL._ATR_CACHE),
    }


# ══ 主池: 完整队列 + 最强空头不丢 + 预算 ═════════════════════════════════
@register("main_pool")
def _main_pool():
    KLINE.clear()
    bullish = [sym(n, s) for n, s in (("BAA", 9.0), ("BAB", 8.0), ("BAC", 7.0),
                                      ("BAD", 6.5), ("BAE", 6.0))]
    bearish = [sym(n, s) for n, s in (("SAA", 1.0), ("SAB", 1.5), ("SAC", 2.0),
                                      ("SAD", 3.0), ("SAE", 3.9))]
    arm([c["symbol"] for c in bullish + bearish])
    PL._reset_screening_cache()
    sel = PL._select_deep_candidates(scan_of(bullish, bearish), [], [], top_n=5,
                                     output_dir=WORKDIR, batch_id="DA:driver-main")
    return {"selected": sel["selected_symbols"],
            "filter_selected": sel["filter"]["selected_symbols"],
            "filter": {k: v for k, v in sel["filter"].items() if k != "screening"},
            "screening": sel["filter"]["screening"],
            "funnel_exists": os.path.exists(
                os.path.join(WORKDIR, "shadow", "candidate_funnel.jsonl")),
            "funnel_head": _read_funnel(1)}


def _read_funnel(limit=None):
    path = os.path.join(WORKDIR, "shadow", "candidate_funnel.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    return rows[:limit] if limit else rows


# ══ 淘汰补位 ═════════════════════════════════════════════════════════════
@register("backfill")
def _backfill():
    KLINE.clear()
    bull = [sym(n, s) for n, s in (("HAA", 9.5), ("HAB", 9.0), ("HAC", 8.5),
                                   ("HAD", 8.0), ("HAE", 7.5), ("HAF", 7.0),
                                   ("HAG", 6.5), ("HAH", 6.0), ("HAI", 5.5),
                                   ("HAJ", 5.0))]
    arm([c["symbol"] for c in bull])
    KLINE["US.HAA"] = {"vol": 0.004}          # 最高信念但低 ATR → 拒
    bull[3]["price"] = 2.5                     # HAD 低价 → price_floor 拒
    PL._reset_screening_cache()
    sel = PL._select_deep_candidates(scan_of(bull, []), [], [], top_n=5,
                                     output_dir=WORKDIR, batch_id="DA:driver-backfill")
    return {"selected": sel["selected_symbols"],
            "eligibility": sel["filter"]["screening"]["eligibility"],
            "reason_codes": sel["filter"]["screening"]["reason_codes"],
            "pool_size": sel["filter"]["pool_size"]}


# ══ 跨来源去重 + extras/UE/异动预算 ══════════════════════════════════════
@register("dedup_budgets")
def _dedup_budgets():
    KLINE.clear()
    main = [sym(n, s) for n, s in (("MAA", 9.0), ("MAB", 8.0), ("MAC", 7.0))]
    arm([c["symbol"] for c in main])
    arm(["US.MAB", "US.XAA"])                  # extras: MAB 与主池重复
    ue = [{"symbol": "US.UAA", "ue_direction": "SHORT", "ue_causal_chain": "c"},
          {"symbol": "US.MAA", "ue_direction": "SHORT"},   # 与主池重复
          {"symbol": "US.UAB", "ue_direction": "SHORT"},
          {"symbol": "US.UAC", "ue_direction": "SHORT"},
          {"symbol": "US.UAD", "ue_direction": "SHORT"},   # 超 cap=3
          {"symbol": "US.UEE", "ue_direction": "SHORT"}]
    arm([u["symbol"] for u in ue])
    KLINE["US.UAA"] = {"vol": 0.004}           # UE 首个低 ATR → 拒 → 由后续补位
    movers = [dict(sym("PAA", 4.0), channels=["premarket_movers"], premarket_chg_pct=-12.0),
              dict(sym("PAB", 4.0), channels=["premarket_movers"], premarket_chg_pct=9.5),
              dict(sym("PAC", 4.0), channels=["premarket_movers"], premarket_chg_pct=-8.5),
              dict(sym("MAC", 4.0), channels=["premarket_movers"], premarket_chg_pct=20.0)]
    arm([m["symbol"] for m in movers])
    KLINE["US.PAA"] = {"vol": 0.004}           # 异动首个低 ATR → 拒 → 补位
    sr = scan_of(main, [], all_scored=main + movers)
    PL._reset_screening_cache()
    sel = PL._select_deep_candidates(sr, ["US.MAB", "US.XAA"], ue, top_n=5,
                                     output_dir=WORKDIR, batch_id="DA:driver-dedup")
    by_stage = sel["filter"]["screening"]["by_stage"]
    return {"selected": sel["selected_symbols"],
            "duplicates": len(sel["selected_symbols"]) != len(set(sel["selected_symbols"])),
            "selection": sel["filter"]["screening"]["selection"],
            "by_stage": {k: v["selection"] for k, v in by_stage.items()},
            "ue_injected": sel["filter"]["ue_candidate_injected"],
            "mover_injected": sel["filter"]["premarket_mover_injected"],
            "sources": [c.get("_source") for c in sel["deep_candidates"]]}


# ══ 全空 / 旧 scanner 回退 ═══════════════════════════════════════════════
@register("empty_and_legacy")
def _empty_and_legacy():
    KLINE.clear()
    PL._reset_screening_cache()
    empty = PL._select_deep_candidates(
        {"candidates_total": 0, "market_condition": "t", "top_bullish": [],
         "top_bearish": [], "all_scored": []}, [], [], top_n=5,
        output_dir=WORKDIR, batch_id="DA:driver-empty")
    legacy_rows = [sym("OAA", 9.0), sym("OAB", 8.0), sym("OAC", 2.0)]
    arm([c["symbol"] for c in legacy_rows])
    legacy_sr = {"candidates_total": 3, "market_condition": "t",
                 "top_bullish": legacy_rows[:2], "top_bearish": legacy_rows[2:],
                 "all_scored": legacy_rows}          # 无 *_all 完整队列
    PL._reset_screening_cache()
    legacy = PL._select_deep_candidates(legacy_sr, [], [], top_n=5,
                                        output_dir=WORKDIR, batch_id="DA:driver-legacy")
    return {"empty_selected": empty["selected_symbols"],
            "empty_screening_total": empty["filter"]["screening"]["total"],
            "legacy_selected": legacy["selected_symbols"]}


# ══ checkpoint 版本 ══════════════════════════════════════════════════════
@register("checkpoint")
def _checkpoint():
    version = PL._ckpt_policy_version()
    original = PL._da_screening_thresholds
    variants = {}
    for field, value in (("min_atr_ratio", 0.029), ("min_price", 4.0),
                         ("low_atr_enabled", False), ("version", "other")):
        PL._da_screening_thresholds = lambda _f=field, _v=value: dict(original(), **{_f: _v})
        variants[field] = PL._ckpt_policy_version()
    PL._da_screening_thresholds = original
    os.environ["DA_CANDIDATE_SCREEN"] = "0"
    screen_off = PL._ckpt_policy_version()
    os.environ.pop("DA_CANDIDATE_SCREEN", None)
    real_bucket = _fake_ms.BUCKET_ORDER_VERSION
    _fake_ms.BUCKET_ORDER_VERSION = "other_order_v9"
    bucket_changed = PL._ckpt_policy_version()
    _fake_ms.BUCKET_ORDER_VERSION = real_bucket

    analysis = {"symbol": "US.CKP", "_ckpt_model": "m", "rating": "看多"}
    ck_dir = os.path.join(WORKDIR, "ckpt")
    PL._ckpt_save(ck_dir, "2026-09-11", dict(analysis))
    loaded = {
        "no_expect": PL._ckpt_load(ck_dir, "2026-09-11", "US.CKP", "m") is not None,
        "match": PL._ckpt_load(ck_dir, "2026-09-11", "US.CKP", "m",
                               expect_policy=version) is not None,
        "mismatch": PL._ckpt_load(ck_dir, "2026-09-11", "US.CKP", "m",
                                  expect_policy="deadbeefdeadbeef") is None,
        "model_mismatch": PL._ckpt_load(ck_dir, "2026-09-11", "US.CKP", "other",
                                        expect_policy=version) is None,
        "caller_dict_unpolluted": "_ckpt_policy" not in analysis,
    }
    return {"version": version, "stable": version == PL._ckpt_policy_version(),
            "variants_differ": {k: v != version for k, v in variants.items()},
            "screen_off_differs": screen_off != version,
            "bucket_order_differs": bucket_changed != version,
            "load": loaded}


# ══ UE feed 批次纪律 ═════════════════════════════════════════════════════
@register("ue_feed")
def _ue_feed():
    feed = os.path.join(WORKDIR, "ue_scan.jsonl")

    def rec(window, age_min, surfaced, status="complete", rtype="scan"):
        stamp = (NOW - timedelta(minutes=age_min)).isoformat()
        return {"type": rtype, "date": (NOW - timedelta(minutes=age_min)).strftime("%Y-%m-%d"),
                "scanned_at": stamp, "started_at": stamp, "scan_window": window,
                "batch_id": f"UE:{window}:{age_min}", "batch_status": status,
                "surfaced": surfaced}

    def s(name, direction="SHORT", **kw):
        row = {"symbol": name, "direction": direction, "confidence": 0.8,
               "thesis": "t", "causal_chain": "c"}
        row.update(kw)
        return row

    def write(rows):
        with open(feed, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    original_path = PL._UE_SCAN_JSONL
    PL._UE_SCAN_JSONL = feed
    out = {}
    try:
        write([rec("da_feed", 20, [s("AAA")]), rec("premarket", 5, [s("BBB")])])
        out["premarket_does_not_override"] = [o["symbol"] for o in PL._load_ue_short_candidates()]
        write([rec("da_feed", 20, [s("AAA")]), rec("midday", 1, [s("MMM")])])
        out["midday_does_not_override"] = [o["symbol"] for o in PL._load_ue_short_candidates()]
        write([rec("da_feed", 20, [s("AAA")]), rec("da_feed", 5, [s("CCC")])])
        out["latest_da_feed_wins"] = [o["symbol"] for o in PL._load_ue_short_candidates()]
        write([rec("da_feed", 20, [s("AAA")]), rec("da_feed", 5, [])])
        out["empty_batch_not_reused"] = PL._load_ue_short_candidates()
        write([rec("da_feed", 20, [s("AAA")]),
               rec("da_feed", 5, [], "failed", rtype="batch_end")])
        out["failed_batch_invalidates"] = PL._load_ue_short_candidates()
        write([rec("da_feed", 400, [s("AAA")])])
        out["stale_rejected"] = PL._load_ue_short_candidates()
        future = dict(rec("da_feed", 20, [s("AAA")]))
        future["started_at"] = (NOW + timedelta(hours=2)).isoformat()
        future["scanned_at"] = future["started_at"]
        write([future])
        out["future_rejected"] = PL._load_ue_short_candidates()
        write([rec("premarket", 5, [s("BBB")])])
        out["only_premarket"] = PL._load_ue_short_candidates()
        write([])
        out["no_records"] = PL._load_ue_short_candidates()
        write([rec("da_feed", 10, [s("DDD", direction="LONG"), s("EEE")])])
        out["only_short_kept"] = [o["symbol"] for o in PL._load_ue_short_candidates()]
        write([rec("da_feed", 10, [s("FFF", short_check={"provided": True,
                                                          "borrow_feasible": False})])])
        out["borrow_infeasible_filtered"] = PL._load_ue_short_candidates()
        write([rec("da_feed", 10, [s("GGG", emergence_streak_days=999)])])
        out["streak_filtered"] = PL._load_ue_short_candidates()
        write([rec("da_feed", 10, [s("HHH")])])
        row = PL._load_ue_short_candidates()[0]
        out["fields_passthrough"] = sorted(row)
        out["symbol_normalized"] = row["symbol"]
        # 时间戳无时区 (naive) → 拒, 不猜时区
        naive = dict(rec("da_feed", 10, [s("III")]))
        naive["started_at"] = naive["scanned_at"] = "2026-09-11T07:40:00"
        write([naive])
        out["naive_timestamp_rejected"] = PL._load_ue_short_candidates()
    finally:
        PL._UE_SCAN_JSONL = original_path
    return out


# ══ 兼容包装 / fail-open ═════════════════════════════════════════════════
@register("compat_and_failopen")
def _compat_and_failopen():
    KLINE.clear()
    arm(["US.PFA", "US.PFB", "US.PFC"])
    KLINE["US.PFB"] = {"vol": 0.004}
    cands = [{"symbol": "US.PFA", "price": 50.0}, {"symbol": "US.PFB", "price": 50.0},
             {"symbol": "US.PFC", "price": 1.0}]
    PL._reset_screening_cache()
    kept = [c["symbol"] for c in PL._prefilter_candidates(cands)]

    os.environ["DA_CANDIDATE_SCREEN"] = "0"
    PL._reset_screening_cache()
    off = PL._screen_da_candidates(cands, stage="off")
    off_rows = [{"status": c["candidate_screening"]["status"],
                 "missing": c["candidate_screening"]["missing_fields"],
                 "policy_version": c["candidate_screening"]["policy_version"]}
                for c in off]
    main = [sym("ZAA", 9.0), sym("ZAB", 8.0)]
    arm([c["symbol"] for c in main])
    sel = PL._select_deep_candidates(scan_of(main, []), [], [], top_n=5,
                                     output_dir=WORKDIR, batch_id="DA:driver-off")
    os.environ.pop("DA_CANDIDATE_SCREEN", None)

    # deps 不可导入 → fail-open (只标注不拒绝), 且不抛
    saved = PL._DEPS_STATE["deps"]
    PL._DEPS_STATE["deps"] = None
    PL._reset_screening_cache()
    no_deps = [{"status": c["candidate_screening"]["status"],
                "missing": c["candidate_screening"]["missing_fields"]}
               for c in PL._screen_da_candidates(cands, stage="nodeps")]
    PL._DEPS_STATE["deps"] = saved
    return {"prefilter_kept": kept, "screen_off": off_rows,
            "screen_off_selected": sel["selected_symbols"],
            "no_deps": no_deps}


# ══ DailyActivePool 一致性 (pool 不从 scanner Top 重拼) ═══════════════════
@register("pool_write")
def _pool_write():
    KLINE.clear()
    pool_dir = os.path.join(WORKDIR, "pool")
    os.makedirs(pool_dir, exist_ok=True)
    eligible = [sym("EAA", 9.0), sym("EAB", 8.0)]
    rejected = [sym("RJC", 7.5), sym("RJD", 7.0)]        # 被资格判定拒绝的 scanner Top
    arm([c["symbol"] for c in eligible + rejected])
    KLINE["US.RJC"] = {"vol": 0.004}
    rejected[1]["price"] = 1.0
    sr = scan_of(eligible + rejected, [])
    PL._reset_screening_cache()
    sel = PL._select_deep_candidates(sr, [], [], top_n=1, output_dir=pool_dir,
                                     batch_id="DA:driver-pool")
    analyses = [{"symbol": "US.EAA", "action": "BUY", "score": 8,
                 "strategy_type": "quality_breakout"},
                {"symbol": "US.NEW", "action": "SELL_SHORT", "score": 6,
                 "strategy_type": "trend_short"},
                {"symbol": "US.SKIP", "action": "SKIP", "score": 9,
                 "strategy_type": "watch"}]
    pipe = PL.Pipeline("US", 10, 5, extra_symbols=[], output_dir=pool_dir,
                       db_write=False, pool_write=True)
    pipe._write_ftseek_data_channels(sr, analyses, sel["selected_symbols"])
    with open(os.path.join(pool_dir, "daily_active_pool.json"), encoding="utf-8") as fh:
        pool = json.load(fh)

    # 未传 selected_symbols (兼容旧调用点) → 只按 analyses, 绝不回退 scanner Top
    none_dir = os.path.join(WORKDIR, "pool_none")
    os.makedirs(none_dir, exist_ok=True)
    PL.Pipeline("US", 10, 5, output_dir=none_dir, db_write=False,
                pool_write=True)._write_ftseek_data_channels(sr, analyses, None)
    with open(os.path.join(none_dir, "daily_active_pool.json"), encoding="utf-8") as fh:
        pool_none = json.load(fh)
    return {"selected": sel["selected_symbols"],
            "pool_codes": [e["code"] for e in pool["entries"]],
            "pool_summary": pool.get("summary"),
            "pool_none_codes": [e["code"] for e in pool_none["entries"]],
            "pool_metadata": {e["code"]: e["metadata"] for e in pool["entries"]}}


def main():
    global WORKDIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    WORKDIR = os.path.abspath(args.workdir)
    os.makedirs(WORKDIR, exist_ok=True)
    # 生产 data/ 不得被驱动写入 (只允许 WORKDIR); 候选漏斗路径随 output_dir 走。
    assert os.path.realpath(WORKDIR) != os.path.realpath(os.path.join(REPO, "data"))
    failed = {}
    for name, fn in CASES.items():
        try:
            RESULTS[name] = fn()
        except Exception as exc:                     # 单例失败不吞掉其余结果
            import traceback
            failed[name] = f"{type(exc).__name__}: {exc}"
            RESULTS[name] = {"error": failed[name],
                             "traceback": traceback.format_exc()}
    RESULTS["_failed_cases"] = failed
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, ensure_ascii=False, default=str, indent=1)
    print(json.dumps({"cases": list(CASES), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
