#!/usr/bin/env python3
"""DEB-20261128-ext-premarket-revival-application §6/§10 · M1 数据覆盖审计 (只读, record-only).

用途 (用户「继续」→ 兑现「M1 数据覆盖审计先行」):
  复活申请 §6 冻结了 M1 C10 两对照设计, 但明令「覆盖审计先行, 覆盖不足转前瞻, 不得用残缺
  便利样本下结论」。本脚本就是那个「执行第一步」: 只读盘点既有价格/财报缓存对 EXT-盘前
  人群 (29 票 × 事件日 + 匹配非事件日所需) 的覆盖率, 给出「能否在现有数据上跑 M1」的裁决。

  量的是 **数据可得性**, 不是收益。覆盖不足 ⇒ M1 历史不可执行, 须前瞻采集 (富途日历零配额
  历史段 + 盘前 1m 回补受日K配额墙 + SPY5m 延展); 即便补齐, strict 财报 n12<30 ⇒ 历史只能
  给支持性证据, 结论须前瞻攒样。本脚本不下「复活成立/盈利」结论。

只读承诺: sqlite mode=ro; 不写库/不下单/不改文件/不 import app.*; 仅 import
  scripts.utils.da_sample (纯 stdlib 零写)。读 JSON 缓存均为只读。默认仅控制台打印。
  不连接 OpenD / 不拉新数据 (前瞻采集须用户批 RFC 后另批执行)。

口径: 人群 = M2/§16 同款 SSOT (PLAYBOOK_SIM 已平仓 ∧ clean-era signal ∧ artifact 经
  split_signal_artifact 单列 ∧ decision_id 前缀 EXT ∧ entry_session='premarket')。
  事件日 = trade_date (入场日, entry_time 落在该日盘前 07:30–09:15 ET)。

复现: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python \
        scripts/deb20261129_ext_premarket_m1_coverage_audit.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from scripts.utils.da_sample import split_signal_artifact

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
DATA = ROOT / "data"

ERA = (
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')"
)
A9 = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''"


def _meta(r, key):
    try:
        return json.loads(r.get("metadata_json") or "{}").get(key)
    except Exception:
        return None


def _bare(sym):
    return str(sym).replace("US.", "").replace("US_", "")


def _load_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _bars_ts(d):
    """从两种缓存 schema 抽 timestamp 列表: list[[ts,...]] 或 {bars:[{ts,...}]} 或 list[{ts,...}]。"""
    if d is None:
        return []
    if isinstance(d, dict):
        d = d.get("bars") or []
    out = []
    for r in d:
        if isinstance(r, list) and r:
            out.append(str(r[0]))
        elif isinstance(r, dict) and "ts" in r:
            out.append(str(r["ts"]))
    return out


def _premarket_dates(ts_list):
    """有盘前 bar (04:00–09:30 ET, 即 HH:MM < 09:31) 的日期集合。"""
    days = set()
    for ts in ts_list:
        if len(ts) >= 16 and ts[11:16] < "09:31" and ts[11:13] >= "04":
            days.add(ts[:10])
    return days


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = (
        "SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL "
        f"{ERA} {A9}"
    )
    rows = [dict(r) for r in conn.execute(q)]
    # earnings_calendar 表行数 (C10 数据基建是否落库)
    try:
        ec_n = conn.execute("SELECT COUNT(*) FROM earnings_calendar").fetchone()[0]
    except Exception:
        ec_n = "TABLE_ABSENT"
    conn.close()

    signal, artifact = split_signal_artifact(rows)
    ext = sorted(
        [r for r in signal
         if str(r.get("decision_id") or "").startswith("EXT")
         and (r.get("entry_session") or "") == "premarket"],
        key=lambda r: (r["trade_date"], r["symbol"]),
    )
    strict_earn = [r for r in ext if _meta(r, "earnings_report_date") is not None]
    strict_noearn = [r for r in ext
                     if str(_meta(r, "gate_mode")) == "strict"
                     and _meta(r, "earnings_report_date") is None]
    research = [r for r in ext if str(_meta(r, "gate_mode")) == "research_momentum"]

    syms = sorted({_bare(r["symbol"]) for r in ext})
    event_days = sorted({r["trade_date"] for r in ext})
    strict_days = sorted({r["trade_date"] for r in strict_earn})
    strict_syms = sorted({_bare(r["symbol"]) for r in strict_earn})
    # (symbol, trade_date) 事件键
    event_keys = {(_bare(r["symbol"]), r["trade_date"]) for r in ext}
    strict_keys = {(_bare(r["symbol"]), r["trade_date"]) for r in strict_earn}

    print("== M1 数据覆盖审计 | EXT-盘前复活申请 §6 执行第一步 (只读, 数据可得性非收益) ==")
    print(f"人群: n={len(ext)} (strict财报 {len(strict_earn)} + strict无财报 {len(strict_noearn)}"
          f" + research {len(research)}); artifact 单列 n={len(artifact)}")
    print(f"票池: {len(syms)} 票; 事件日 {len(event_days)} 天 ({event_days[0]}→{event_days[-1]});"
          f" strict财报事件日 {len(strict_days)} 天")
    print(f"earnings_calendar DB 表行数 = {ec_n}  (0/空 ⇒ 财报事件须 trade metadata + 富途日历)\n")

    # ---- (1) 盘前入场窗 1m bar 覆盖 (M1 对照①/② 的绑定缺口) ----
    print("-- (1) 盘前入场窗 1m bar (07:30–09:15 ET) 覆盖 = M1 绑定缺口 --")
    ah_dir = DATA / "replay_1m_ah"
    ah_pre = set()          # (sym, date) 有盘前 bar
    ah_sym_dates = defaultdict(set)
    ah_files = sorted(ah_dir.glob("*.json")) if ah_dir.exists() else []
    ah_empty = 0
    for p in ah_files:
        m = re.match(r"US_([A-Z]+)_", p.name)
        if not m:
            continue
        s = m.group(1)
        ts = _bars_ts(_load_json(p))
        if not ts:
            ah_empty += 1
            continue
        pd = _premarket_dates(ts)
        for d in pd:
            ah_pre.add((s, d))
            ah_sym_dates[s].add(d)
    ah_all_dates = sorted({d for _, d in ah_pre})
    print(f"  replay_1m_ah: {len(ah_files)} 文件 (空 {ah_empty}); 盘前 bar 日期范围"
          f" {ah_all_dates[0] if ah_all_dates else '—'}→{ah_all_dates[-1] if ah_all_dates else '—'}")
    ah_sym_overlap = sorted(set(ah_sym_dates) & set(syms))
    print(f"  与 EXT 票池交集: {len(ah_sym_overlap)}/{len(syms)} 票 {ah_sym_overlap}")
    hit = event_keys & ah_pre
    strict_hit = strict_keys & ah_pre
    print(f"  ★ EXT 事件日盘前 bar 覆盖: {len(hit)}/{len(event_keys)} 笔-key;"
          f" strict财报 {len(strict_hit)}/{len(strict_keys)}")
    print(f"  ⇒ 盘前入场腿覆盖 {'不足' if len(hit) < len(event_keys) else '足'};"
          f" 缓存日期 ({ah_all_dates[-1] if ah_all_dates else '—'}) vs 事件日 ({event_days[-1]})")

    # 常规段 1m 缓存是否含盘前 (应 0)
    print("  常规段 1m 缓存盘前 bar 抽查 (schema 09:31 起, 应无盘前):")
    for sub, s in [("replay_bars_1m", "AAPL"), ("replay_bars_1m_extra", "AMD"),
                   ("replay_bars_1m_extra", "ACN")]:
        p = DATA / sub / f"US_{s}.json"
        if p.exists():
            ts = _bars_ts(_load_json(p))
            pre = sum(1 for t in ts if len(t) >= 16 and t[11:16] < "09:31")
            print(f"    {sub}/US_{s}: 盘前 bar={pre}/{len(ts)}; 首 bar={ts[0] if ts else '—'}")

    # ---- (2) SPY5m 市场调整覆盖 ----
    print("\n-- (2) SPY5m 市场调整 (replay_cache/spy5m) 事件日覆盖 --")
    spy_dir = DATA / "replay_cache" / "spy5m"
    spy_days = {f[:-5] for f in os.listdir(spy_dir)} if spy_dir.exists() else set()
    spy_range = (min(spy_days), max(spy_days)) if spy_days else ("—", "—")
    print(f"  SPY5m 日期范围 {spy_range[0]}→{spy_range[1]} ({len(spy_days)} 天)")
    spy_hit = [d for d in event_days if d in spy_days]
    spy_miss = [d for d in event_days if d not in spy_days]
    print(f"  事件日覆盖 {len(spy_hit)}/{len(event_days)}; 缺 {spy_miss}")
    strict_spy_miss = [d for d in strict_days if d not in spy_days]
    print(f"  strict财报事件日缺: {strict_spy_miss}")

    # ---- (3) 财报日历 (对照② 非事件日匹配所需) ----
    print("\n-- (3) 财报日历覆盖 (对照② 非事件日匹配 = 须知道哪些日子非财报) --")
    av_dir = DATA / "replay_bars" / "earnings_dir_cache"
    av_syms = {}
    if av_dir.exists():
        for p in av_dir.glob("av_earnings_*.json"):
            s = p.stem.replace("av_earnings_", "")
            d = _load_json(p) or []
            dates = [x.get("reported_date") for x in d if isinstance(x, dict)]
            av_syms[s] = max(dates) if dates else None
    av_overlap = sorted(set(av_syms) & set(strict_syms))
    print(f"  av_earnings (AlphaVantage C10 旧产物, Sep30 快照): {len(av_syms)} 票")
    print(f"  与 strict财报 {len(strict_syms)} 票交集: {len(av_overlap)}/{len(strict_syms)} {av_overlap}")
    for s in av_overlap:
        print(f"    {s}: 缓存最新 reported_date={av_syms[s]} (须覆盖事件日 {strict_days})")
    print(f"  ⇒ DB 日历空 + av 缓存仅覆盖 {len(av_overlap)}/{len(strict_syms)} strict 票且陈旧"
          f" ⇒ 非事件日匹配不可从现有缓存构造")
    # 富途日历 API 路径是否存在 (读源码文本, 不 import app.*)
    ec_src = ROOT / "app" / "data" / "earnings_calendar.py"
    has_futu = False
    if ec_src.exists():
        txt = ec_src.read_text()
        has_futu = "get_earnings_calendar" in txt and "OpenQuoteContext" in txt
    print(f"  富途 get_earnings_calendar API 路径存在 = {has_futu}"
          f" (OpenD 本地零配额, 单次≤7天含端点须分段; 历史段可补 Oct–Nov)")

    # ---- (4) 常规段 1m + 日K (出场/开盘侧, 非绑定缺口) ----
    print("\n-- (4) 常规段 1m + 日K (开盘价/出场侧, 非盘前绑定缺口) --")
    reg = {f[3:-5] for f in os.listdir(DATA / "replay_bars_1m")} if (DATA / "replay_bars_1m").exists() else set()
    extra = {f[3:-5] for f in os.listdir(DATA / "replay_bars_1m_extra")} if (DATA / "replay_bars_1m_extra").exists() else set()
    reg_union = reg | extra
    reg_absent = [s for s in syms if s not in reg_union]
    print(f"  常规 1m (replay_bars_1m ∪ extra) EXT 票覆盖 {len(syms)-len(reg_absent)}/{len(syms)}; 缺 {reg_absent}")
    daily = {f[3:-5] for f in os.listdir(DATA / "replay_bars_daily")} if (DATA / "replay_bars_daily").exists() else set()
    daily_absent = [s for s in syms if s not in daily]
    print(f"  日K (replay_bars_daily) EXT 票覆盖 {len(syms)-len(daily_absent)}/{len(syms)}; 缺 {daily_absent}")
    print("  ⇒ 开盘价/常规出场侧大部分可得, 但盘前入场参考价位移 (绑定缺口在 (1))")

    # ---- 裁决 ----
    print("\n== M1 覆盖审计裁决 (数据可得性, 非复活/盈利裁决) ==")
    binding = len(hit) < len(event_keys)
    cal_gap = len(av_overlap) < len(strict_syms) or ec_n in (0, "TABLE_ABSENT")
    spy_gap = bool(spy_miss)
    print(f"1. 盘前入场腿 (绑定): 事件日覆盖 {len(hit)}/{len(event_keys)} ⇒ {'缺口' if binding else '足'}。"
          f" 唯一盘前缓存 replay_1m_ah 止于 {ah_all_dates[-1] if ah_all_dates else '—'},"
          f" EXT 事件日 {event_days[0]}→{event_days[-1]}; 常规缓存 0 盘前 bar。")
    print(f"2. 财报日历 (对照②): DB={ec_n}, av 覆盖 strict {len(av_overlap)}/{len(strict_syms)} 且陈旧"
          f" ⇒ {'缺口' if cal_gap else '足'}; 须富途 get_earnings_calendar 历史段 (零配额)。")
    print(f"3. SPY5m 市场调整: 缺 {spy_miss} ⇒ {'缺口' if spy_gap else '足'}; 须延展至 {event_days[-1]}+。")
    verdict = "INSUFFICIENT" if (binding or cal_gap or spy_gap) else "SUFFICIENT"
    print(f"\n★ 总理决 = {verdict}: M1 C10 两对照 {'不可' if verdict=='INSUFFICIENT' else '可'}"
          f" 在现有缓存上执行。")
    if verdict == "INSUFFICIENT":
        print("  前瞻采集计划 (须用户批 RFC-149 新窗 + KR4 腾位后另批执行, 本脚本不拉数据):")
        print("   (a) 富途 get_earnings_calendar 历史段 (Oct1–Nov12, ≤7天/段, 零配额) → 落库补 earnings_calendar;")
        print("   (b) 盘前 1m bar 回补 29 票 × 事件日 + 匹配非事件日 (受富途日K 1000只/7天配额墙, 须分批);")
        print("   (c) SPY5m 延展至 Nov12+ (市场调整);")
        print("   (d) 即便补齐: strict 财报 n12<30 ⇒ 历史 M1 仅支持性证据, 结论须前瞻攒样 (n≥120 目标)。")
    print("\n局限: 只读盘点既有缓存, 未连接 OpenD/未拉新数据; 覆盖足≠复活成立; SIM-only fees_est≠live。")


if __name__ == "__main__":
    main()
