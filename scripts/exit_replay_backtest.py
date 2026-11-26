#!/usr/bin/env python3
"""离线退出规则回放 (路径1, 2026-07-25) — 只读历史交易 + 1min bar 反事实评估退出参数.

定位: 修尺子/攒样本类工具. 只读 DB (ro-mode), 不改任何 live 文件/配置,
bar 缓存写 data/replay_bars/ (新目录), 结果写 data/exit_replay_latest.json (新文件).

数据源: data/ftseek_us.db trade_records (source_type=PLAYBOOK_SIM, 已平仓).
bar:   Futu request_history_kline K_1M (常规时段, 深度实测 ≥2026-01).
数学:  直接复用 app.playbook.exit_engine._compute_dynamic_effective_sl /
       _get_factor_exit_config — 与实盘退出同一函数, 杜绝两份数学漂移.
       (2026-09-07 修: 原从 app.playbook.pipeline 取; P2a 提交 14e9a544 把本体逐字
        迁入 exit_engine 并**撤掉了 pipeline 侧 `_compute_dynamic_effective_sl` 转发器**
        ⇒ 本引擎 ImportError, 并级联打穿 6 个复用方 (含 3 个 cron 活体的懒 import)。
        修法=改指向 SSOT 本体, 不改数学; 由 tests/test_replay_engine_import_smoke.py 钉住。)

保真度边界 (如实报告):
  1. 1min bar 内 high/low 先后不可知 → 双序假设 (pess=不利先触, opt=有利先触) 各跑, 报区间.
  2. SL/TP/BE 成交按**生效线** eff ± slippage_bps (见 replay_trade L388 exit_price=eff*(1∓slip),
     eff 复用生产 SSOT _compute_dynamic_effective_sl); 时间类退出按 bar close.
     ⚠️ (2026-09-27 勘误) 旧文写"按触发价±滑点(实盘=市价单)"**不准**: 生产 SL_BREAKEVEN
     走**限价单@净保本线**(成交价物理≥线, net-breakeven-caliber-switch-20260817), 引擎按 eff 线
     结算是近似, 且**不模拟限价被动侧有利成交** ⇒ BE 桶偏悲观. MU id=1561 校准单: 引擎 +2.40
     vs 实际 +15.67. 另一侧引擎用**固定 fee_cushion**(params) > 生产 fee-aware 实弹(avg 0.109%)
     ⇒ BE 线偏高偏乐观. 两侧**反向**, 净方向见 scripts/replay_engine_be_fidelity.py 聚合对账.
  3. 回放跳过入场 bar. ⚠️ (2026-09-27 勘误) 旧文写"实盘 ~60s 轮询"**错**: 退出快循环实测
     playbook_exit_fast_loop_interval_sec=**10** (unified_config, 2026-07-22 a4c1207e9 引入).
     ⇒ 本引擎按 1min bar 粒度采样, 比实盘 10s 轮询**粗 6 倍** ⇒ 峰值/触发时点保真度受限,
     与 RFC-116 §7 的 peak 口径分叉同源 (实盘 quote 峰 vs 引擎 bar 极值抬峰).
  4. 入场选择偏差: 结论只外推到"同入场分布"; 三门/入场门不可回放 (属路径2).
  5. sl_floor 网格不在 v1 (需重建 ATR, 误差大); dd/tiered/early_tight/mfe_stall 可回放.
     ⛔ **be/dd 阈值变体受「参数时代混算」污染, 读数不可直接裁** (2026-09-27 实锤):
     live_baseline 读**现行 config** 回放整窗, 若窗口跨该参数落地日 (如 be_0.4 于 8/17),
     则窗口主体的实盘成交是在**旧参数时代**(be_0.5)发生的 ⇒ 引擎把旧时代本不该武装的笔也
     武装了. 聚合对账实测 (窗 6/24~8/17): 引擎 BE 桶 103笔/净+519 vs 实盘 59笔/净−130,
     笔数虚增 +74.6%, BE占比 37.7% vs 20.3%; validation.reason_match_pct=0.542 主因也是它.
     ⇒ 违反 skill「参数 era 分层」纪律: **裁 be/dd 须 baseline 按入场日取当时参数值, 或
     只取现行参数时代样本**; 跨参数落地日的窗口回放只能看方向不能定值.
  6. ⛔ **reason_match_pct < 0.80 时, 本引擎的变体裁决一律冻结** (项目自定门, 与影子尺子同源):
     baseline 回放与实盘出场码一致率 < 0.80 ⇒ 触发面本身没对齐, 变体间相对比较也失去意义.
     跑完先看 validation.reason_match_pct, < 0.80 就停, 别读 variants.

用法:
  ./venv/bin/python scripts/exit_replay_backtest.py [--since 2026-06-24] [--until DATE]
      [--slippage-bps 3] [--refresh-bars] [--out data/exit_replay_latest.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from app.utils.timezone import ET, to_et  # noqa: E402
from app.playbook.exit_engine import (  # noqa: E402
    _compute_dynamic_effective_sl,
    _get_factor_exit_config,
)

DB_PATH = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
HORIZON_YAML = REPO / "config" / "playbook_horizon.yaml"
UNIFIED_YAML = REPO / "config" / "unified_config.yaml"

MFE_STALL_LIVE_SINCE = "2026-07-24"  # 三门上线日 (entry ET date >= 此日 → baseline 启用)


# ────────────────────────────────────────────────────────────
# 配置装载
# ────────────────────────────────────────────────────────────

def _load_configs() -> tuple[dict, dict]:
    hz = yaml.safe_load(HORIZON_YAML.read_text())
    factor_rules = hz.get("factor_exit_rules", {})
    short_hz = hz.get("short_horizon", {}) or {}
    uni = yaml.safe_load(UNIFIED_YAML.read_text())
    pb = (uni or {}).get("playbook", {}).get("execution_parameters", {}) or {}
    params = {
        "fee_cushion_pct": float(pb.get("breakeven_fee_cushion_pct", 0.15)),
        "cooldown_sl_pct": float(pb.get("cooldown_sl_pct", 4.0)),
        "min_tp_rr": float(pb.get("min_tp_rr", 1.5)),
        "stall_minutes": float(pb.get("mfe_stall_exit_minutes", 150)),
        "stall_mfe_pct": float(pb.get("mfe_stall_exit_mfe_pct", 0.5)),
        "profit_override_threshold": float(pb.get("profit_override_threshold_pct", 1.5)),
        "profit_override_dd": float(pb.get("profit_override_drawdown_pct", 0.5)),
        # 方案 E: 隔夜跳空止损线 (生产 pipeline.py:5106 同款, short_horizon 未配置时默认 3.0)
        "overnight_gap_stop_pct": float(short_hz.get("overnight_gap_stop_pct", 3.0)),
    }
    return factor_rules, params


# ────────────────────────────────────────────────────────────
# 变体网格 (命名变体; be/dd=None → 沿用因子配置值)
# ────────────────────────────────────────────────────────────

def _variants() -> list[dict]:
    v = [{"name": "live_baseline"}]
    for dd in (0.4, 0.5, 0.6, 1.0, 1.2):
        v.append({"name": f"dd_{dd}", "dd": dd})
    for be in (0.3, 0.4, 0.8):
        v.append({"name": f"be_{be}", "be": be})
    v.append({"name": "tiered_dd0.4_tier1.2", "tier_dd_low": 0.4, "tier_at": 1.2})
    v.append({"name": "tiered_dd0.5_tier1.5", "tier_dd_low": 0.5, "tier_at": 1.5})
    for win in (30, 60):
        for gate in (0.3, 0.5):
            v.append({"name": f"early_tight_{win}m_mfe{gate}",
                      "early_tight": {"window": win, "sl_pct": 1.0, "mfe_gate": gate}})
    v.append({"name": "mfe_stall_off", "mfe_stall": False})
    # ── 方案 E (DEB-20260802 §六 用户终裁 2026-08-02: 执行, max_hold=2, 周五平, 财报先接受) ──
    # e_step1: 赢家隔夜 (生产 :5318 winners-only 语义), 输家 EOD 照砍, TIME_EXIT/MFE_STALL 保留
    # e_full:  SHORT 时间退出全退役 (PRE_CLOSE/TIME_EXIT/MFE_STALL), 只留价格退出 + MAX_HOLD + 周五平
    # 共同: MAX_HOLD deadline = 第 (1+max_hold_days) 个交易日盘初 (生产 nth_trading_day_after 语义),
    #       隔夜 gap_stop 3%, 周末/长假不过夜 (下一交易日 >2 日历日 → 当周最后收盘平)
    v.append({"name": "e_step1_winners_overnight", "e_mode": "step1", "max_hold_days": 2})
    v.append({"name": "e_full_price_exits", "e_mode": "full", "max_hold_days": 2})
    # ── DEB-20261016 L1 (隔夜 gap × SL 宽度假设, RFC-124 §2.3 同源, 用户裁「全批」):
    #   初始 SL 宽度上限 — 若入场 SL 距离 >cap%, 收窄到 cap% (隔夜跳空 −5.6~−6.4% 族
    #   的 SL 距离实测 4.5-5%, 被击穿后亏损超宽); 含赢家误杀面 (被提前扫出的恢复单)。
    v.append({"name": "slcap_4", "cap_sl_pct": 4.0})
    v.append({"name": "slcap_5", "cap_sl_pct": 5.0})
    return v


# ────────────────────────────────────────────────────────────
# 数据装载
# ────────────────────────────────────────────────────────────

def _parse_et(ts: str) -> datetime | None:
    """entry/exit_time → aware ET. aware→astimezone; naive→to_et (SSOT 契约: naive=CST)."""
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(ET)
    return to_et(dt)


def load_trades(since: str, until: str | None) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price, "
           "quantity, realized_pnl, pnl_net, fees_est, fees_source, "
           "exit_reason, mfe_pct, mae_pct, config_version, "
           "decision_id, metadata_json "
           "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL "
           "AND entry_price > 0 AND quantity > 0 ORDER BY entry_time")
    out = []
    out_skipped_corrupt: list[str] = []
    for r in conn.execute(sql):
        entry_et = _parse_et(r["entry_time"])
        exit_et = _parse_et(r["exit_time"])
        if not entry_et or not exit_et:
            continue
        if entry_et.strftime("%Y-%m-%d") < since:
            continue
        if until and entry_et.strftime("%Y-%m-%d") > until:
            continue
        meta = json.loads(r["metadata_json"] or "{}")
        # 排除损坏时间戳 (时区修复前遗留: CST wall 误标 ET, 如 01:00-04:00"ET"入场)
        if not (9 <= entry_et.hour < 16):
            out_skipped_corrupt.append(r["symbol"])
            continue
        out.append({
            "id": r["id"], "symbol": r["symbol"], "direction": r["direction"].upper(),
            "entry_et": entry_et, "exit_et": exit_et,
            "entry": float(r["entry_price"]), "live_exit": float(r["exit_price"] or 0),
            "qty": float(r["quantity"]),
            # live_pnl 是历史字段名，实际一直装毛利；保留以免旧消费方静默改义。
            # 新代码必须分别读 live_gross/live_net/live_fee_est。
            "live_pnl": float(r["realized_pnl"] or 0),
            "live_gross": float(r["realized_pnl"] or 0),
            "live_net": (
                float(r["pnl_net"]) if r["pnl_net"] is not None
                else (float(r["realized_pnl"] or 0) - float(r["fees_est"])
                      if r["fees_est"] is not None else None)
            ),
            "live_fee_est": float(r["fees_est"]) if r["fees_est"] is not None else None,
            "fees_source": r["fees_source"],
            "live_reason": r["exit_reason"] or "", "live_mfe": float(r["mfe_pct"] or 0),
            "config_version": r["config_version"] or "",
            # 重放器 DEAD_SIGNAL 的 Phase0 reentry 呼吸窗判据 (decision_id 前缀),
            #   全窗 19 笔 reentry ⇒ 不带上就会把 120min 窗误算成 60min 窗。
            "decision_id": r["decision_id"] or "",
            "initial_sl": float(meta.get("stop_loss") or 0),
            "take_profit": float(meta.get("take_profit") or 0),
            "conditions": meta.get("conditions") or [],
            "meta_config_version": meta.get("config_version", ""),
        })
    conn.close()
    if out_skipped_corrupt:
        print(f"  排除损坏时间戳 {len(out_skipped_corrupt)} 笔: {out_skipped_corrupt}")
    return out


# ────────────────────────────────────────────────────────────
# 1min bar 获取 (直连 OpenD, 只读; 磁盘缓存)
# ────────────────────────────────────────────────────────────

def _bar_cache_path(symbol: str, day: str) -> Path:
    return BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"


def expected_last_hhmm(day: str) -> str:
    """该交易日 1min bar 应覆盖到的末根时刻 (ET, "HH:MM").

    提前收盘日 (13:00 ET) → "12:55", 常规 → "15:55"。日历不可用 → 兜底 "15:55"
    (收严: 宁可把残 bar 当缺失重拉, 也不把半日当完整)。
    """
    try:
        from datetime import date as _date
        from app.utils.holiday_calendar import get_us_close_minutes_et
        close = int(get_us_close_minutes_et(_date.fromisoformat(str(day))))
        return "%02d:%02d" % ((close - 5) // 60, (close - 5) % 60)
    except Exception:
        return "15:55"


def bars_truncated(raw, day: str) -> bool:
    """raw=[[ts,o,h,l,c],…] 是否残缺 (盘中拉取/半日落盘). 空列表也算残缺。

    2026-10-23 (RFC-161): 盘中拉取会把半日 bar 落盘并被后续「缓存=完整」路径复用
    (数据面实测 17 个: 如 US_SPY_2026-08-21.json 106 根止于 11:16) ⇒ 用 11:16 价当
    收盘价 = 静默错值。残 bar 一律视同缺失, 由调用方重拉。
    """
    if not raw:
        return True
    try:
        if not isinstance(raw[0], (list, tuple)):
            return True
        if len(raw) < 30:
            return True
        return str(raw[-1][0])[11:16] < expected_last_hhmm(day)
    except Exception:
        return True


def fetch_bars_with_status(ctx, symbol: str, day: str, refresh: bool = False):
    """返回 (status, bars); status ∈ {cache, fetched, empty, error}。

    与 fetch_bars 的唯一差别是**区分「无数据」与「拉取失败」**: 额度耗尽/网络/OpenD
    不可用都是 error, 而不是「这只票没有 bar」。需要据此判「排除样本 vs 继续等」的
    消费方 (赢家隔夜结算器 no_bar 判定) 必须用本函数, 否则一次额度耗尽就会把真样本
    永久踢出判据。缓存命中但残缺 (bars_truncated) 视同未命中, 走重拉。
    """
    cp = _bar_cache_path(symbol, day)
    if cp.exists() and not refresh:
        try:
            raw = json.loads(cp.read_text())
            if not bars_truncated(raw, day):
                return "cache", [{"t": ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
                                  "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in raw]
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    data = None
    last_ret = None
    for attempt in (1, 2):  # 空结果重试一次 (区分限流失败 vs 休市合法空)
        # 2026-07-28 (DEB-bar-ruler F2): autype=NONE — 回放的 entry/exit 是原始成交价,
        #   缺省 QFQ 前复权在分红股跨除息日会错位 ~0.3-0.5%
        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=day, end=day, max_count=1000,
            autype=AuType.NONE)
        last_ret = ret
        if ret == RET_OK and data is not None and len(data) > 0:
            break
        if attempt == 1:
            time.sleep(3.0)
    if last_ret != RET_OK or data is None:
        time.sleep(1.2)  # kline 配额保护 (≤25次/30s)
        return "error", None
    if len(data) == 0:
        time.sleep(1.2)
        return "empty", None
    bars = []
    for _, row in data.iterrows():
        t = str(row.get("time_key", ""))[:19]
        bars.append([t, float(row["open"]), float(row["high"]),
                     float(row["low"]), float(row["close"])])
    bars.sort(key=lambda b: b[0])
    if bars_truncated(bars, day):
        # 拉到了但非全 RTH (半日市/盘中中断/停牌): 不写缓存污染后续「缓存=完整」路径
        time.sleep(1.2)
        return "truncated", [{"t": ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
                              "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in bars]
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(bars))
    time.sleep(1.2)  # kline 配额保护 (≤25次/30s)
    return "fetched", [{"t": ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
                        "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in bars]


def fetch_bars(ctx, symbol: str, day: str, refresh: bool = False) -> list[dict] | None:
    """返回 [{t: aware ET, o,h,l,c}], 按时间升序; 无数据/拉取失败返回 None。

    2026-10-23 (RFC-161): 委托 fetch_bars_with_status — 缓存残缺自动重拉 (修半日
    bar 污染), 拉到的残日不落盘。返回口径 (有/无) 未变, 既有调用方无需改。
    """
    _status, bars = fetch_bars_with_status(ctx, symbol, day, refresh=refresh)
    return bars


def collect_bars(trades: list[dict], refresh: bool = False,
                 e_days: int = 0) -> dict[tuple[str, str], list[dict]]:
    """按需抓取 (symbol, day) → bars; 覆盖 entry→exit 全部工作日.
    e_days>0: SHORT 加抓 entry 后 e_days 个日历日 (方案 E 隔夜回放需要 T+1/T+2 bars)."""
    need: set[tuple[str, str]] = set()
    for tr in trades:
        d = tr["entry_et"].date()
        end = tr["exit_et"].date()
        while d <= end and (d - tr["entry_et"].date()).days <= 6:
            if d.weekday() < 5:
                need.add((tr["symbol"], d.strftime("%Y-%m-%d")))
            d += timedelta(days=1)
        if e_days > 0 and tr["direction"] == "SHORT":
            d2 = tr["entry_et"].date()
            fwd = d2 + timedelta(days=e_days)
            while d2 <= fwd:
                if d2.weekday() < 5:
                    need.add((tr["symbol"], d2.strftime("%Y-%m-%d")))
                d2 += timedelta(days=1)
    from futu import OpenQuoteContext
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    ctx = OpenQuoteContext(host=host, port=port)
    out: dict[tuple[str, str], list[dict]] = {}
    missing = 0
    try:
        for i, (sym, day) in enumerate(sorted(need)):
            bars = fetch_bars(ctx, sym, day, refresh)
            if bars:
                out[(sym, day)] = bars
            else:
                missing += 1
            if (i + 1) % 20 == 0:
                print(f"  bars: {i + 1}/{len(need)} (missing {missing})")
    finally:
        ctx.close()
    print(f"  bars 就绪: {len(out)}/{len(need)} symbol-days (missing {missing})")
    return out


# ────────────────────────────────────────────────────────────
# 回放引擎
# ────────────────────────────────────────────────────────────

def _effective_sl(direction: str, entry: float, initial_sl: float, peak_profit_pct: float,
                  exit_cfg: dict, var: dict, fee_cushion: float,
                  po_threshold: float, po_dd: float,
                  adaptive_cfg: dict | None = None) -> float:
    """变体参数覆盖后调用实盘同函数. trailing=true 因子覆盖 be/dd; tiered 用 dd_low.
    ponytail: be override 也应用到 trailing=false 因子 (原脚本只覆盖 trailing=true,
    导致 noBE 对照对快速证伪类因子失效).

    adaptive_cfg (RFC-154 T 臂, 2026-10-20): 默认 None = 原静态 dd, 现有调用零变化;
    给定时按实盘 adaptive_trailing 公式算 dd 并传给 SSOT 函数 —— 生产 SIM 走的正是
    这条路径, 但本引擎此前未建模 (live_baseline 用静态 dd). 只读回放, 不改生产.
    """
    cfg = dict(exit_cfg)
    if var.get("be") is not None:
        cfg["breakeven_at_pct"] = var["be"]
    if cfg.get("trailing_enabled", False):
        if var.get("be") is not None:
            cfg["breakeven_at_pct"] = var["be"]
        dd_eff = cfg.get("drawdown_from_peak_pct", 0)
        if var.get("dd") is not None:
            dd_eff = var["dd"]
        tier_at = var.get("tier_at")
        if tier_at is not None and peak_profit_pct < tier_at:
            dd_eff = var.get("tier_dd_low", dd_eff)
        cfg["drawdown_from_peak_pct"] = dd_eff
    # trailing=false 因子走 profit_override 分支 (函数内部读 live config, 语义与 shadow 一致)
    adaptive_dd = None
    if adaptive_cfg is not None:
        from app.playbook.adaptive_trailing import adaptive_trailing_drawdown_pct
        adaptive_dd = adaptive_trailing_drawdown_pct(
            peak_profit_pct, adaptive_cfg, "PLAYBOOK_SIM")
    return _compute_dynamic_effective_sl(
        direction, entry, initial_sl, peak_profit_pct, cfg, fee_cushion,
        adaptive_drawdown_pct=adaptive_dd)


def replay_trade(tr: dict, bars: list[dict], exit_cfg: dict, var: dict,
                 params: dict, ordering: str, slippage_bps: float,
                 anchor: dict | None = None,
                 adaptive_cfg: dict | None = None) -> dict:
    """单笔回放. ordering: 'pess' (不利极值先触) / 'opt' (有利极值先触).

    返回 {exit_price, reason, held_min, mfe_pct} — 数学与 _check_playbook_exit_rules 对齐.

    anchor (DEB-20261014, 可选, 默认 None=全路径回放不变): 状态锚定续放 —
    {"t": aware ET 退出时刻, "peak_pct": 实盘录得的峰值%, "px": 实盘出场价}.
    从锚定 bar (含) 起扫, peak 状态用实盘实测值初始化, 线数学不变 (SSOT).
    用途: 1m 全路径重放在触发时序上失真 (实测 reason_match 0.588), 而对
    「BE/trail 已实际触发的笔」从实盘记录状态向后续放不依赖前段路径重建.
    """
    direction, entry, qty = tr["direction"], tr["entry"], tr["qty"]
    initial_sl = tr["initial_sl"] or (entry * 0.975 if direction == "LONG" else entry * 1.025)
    # 变体 (DEB-20261016 L1): 初始 SL 宽度上限 cap_sl_pct — 收窄过宽的初始止损
    _cap_sl = var.get("cap_sl_pct")
    if _cap_sl:
        if direction == "LONG":
            initial_sl = max(initial_sl, entry * (1 - _cap_sl / 100))
        else:
            initial_sl = min(initial_sl, entry * (1 + _cap_sl / 100))
    tp = tr["take_profit"]
    be_pct = float(exit_cfg.get("breakeven_at_pct", 0.5))
    fee = params["fee_cushion_pct"]
    slip = slippage_bps / 10000.0
    time_exit_min = float(exit_cfg.get("time_exit_hours", 2.0)) * 60
    time_exit_only_loss = bool(exit_cfg.get("time_exit_only_if_loss", True))
    stall_on = var.get("mfe_stall")
    if stall_on is None:  # baseline: 按交易日期规则
        stall_on = tr["entry_et"].strftime("%Y-%m-%d") >= MFE_STALL_LIVE_SINCE
    et_cfg = var.get("early_tight")  # None=baseline (live 关闭中)
    # 方案 E (DEB-20260802): 仅 SHORT 生效 (S12: BUY 显式不变); e_mode None=不改行为
    e_mode = var.get("e_mode") if direction == "SHORT" else None
    max_hold_days = int(var.get("max_hold_days", 2))
    gap_stop_pct = float(var.get("gap_stop_pct") or params["overnight_gap_stop_pct"])

    # TP RR 门禁 (与实盘一致: RR<min_tp_rr 忽略 TP)
    tp_active = False
    if tp > 0 and initial_sl > 0:
        risk = abs(entry - initial_sl)
        tp_active = risk <= 0 or (abs(tp - entry) / risk) >= params["min_tp_rr"]
    elif tp > 0:
        tp_active = True

    peak_profit_pct = 0.0
    if anchor:  # 状态锚定续放 (DEB-20261014): 实盘退出状态起扫, 默认路径不受影响
        peak_profit_pct = float(anchor.get("peak_pct") or 0.0)
        bars = [b for b in bars if b["t"] >= anchor["t"]]
        if not bars:
            return {"exit_price": float(anchor.get("px") or tr["live_exit"]),
                    "reason": "ANCHOR_NO_BARS", "held_min": 0.0,
                    "mfe_pct": peak_profit_pct}
    sign = 1.0 if direction == "LONG" else -1.0

    def _pct(px):
        return (px - entry) * sign / entry * 100

    def _active_sl(peak_pct, held_min, mins_from_open):
        eff = _effective_sl(direction, entry, initial_sl, peak_pct, exit_cfg, var,
                            fee, params["profit_override_threshold"],
                            params["profit_override_dd"], adaptive_cfg=adaptive_cfg)
        # 规则0: 开盘15min冷却 → SL放宽到 cooldown_sl_pct
        if mins_from_open < 15:
            cool = entry * (1 - params["cooldown_sl_pct"] / 100 * sign)
            eff = min(eff, cool) if direction == "LONG" else max(eff, cool)
        # 变体: 早期紧SL (live 关闭; window内 + 峰值未达 mfe_gate + breakeven未激活)
        if et_cfg and mins_from_open >= 15 and held_min <= et_cfg["window"] \
                and peak_pct < et_cfg["mfe_gate"] and peak_pct < be_pct:
            tight = entry * (1 - et_cfg["sl_pct"] / 100 * sign)
            eff = max(eff, tight) if direction == "LONG" else min(eff, tight)
        return eff

    def _classify_sl(eff):
        be_line = entry * (1 + fee / 100 * sign)
        tol = entry * 1e-4
        if peak_profit_pct < be_pct:
            return "SL_INITIAL"
        if (eff - be_line) * sign > tol:
            return "SL_TRAIL_LOCK"
        return "SL_BREAKEVEN"

    prev_day = None
    day_idx = 0  # 1-based 交易日序 (entry=1); 生产 MAX_HOLD deadline=T+max_hold → day_idx>max_hold 盘初起强平
    n_bars = len(bars)
    for i_bar, bar in enumerate(bars):
        t = bar["t"]
        held_min = (t - tr["entry_et"]).total_seconds() / 60
        mins_from_open = (t - t.replace(hour=9, minute=30, second=0)).total_seconds() / 60

        if e_mode:
            cur_day = t.date()
            if prev_day is None:
                day_idx = 1
            elif cur_day != prev_day:
                day_idx += 1
                # ── 新交易日盘初链 (生产规则序: SL→TP→gap_stop; gap穿越 SL/TP 按开盘价成交,
                #    轮询类退出按首 bar close ≈ 9:31 首次轮询市价单) ──
                eff0 = _active_sl(peak_profit_pct, held_min, 0)
                if eff0 > 0 and (bar["o"] - eff0) * sign <= 0:
                    return {"exit_price": bar["o"] * (1 - slip * sign), "reason": _classify_sl(eff0),
                            "held_min": held_min, "mfe_pct": peak_profit_pct}
                if tp_active and (bar["o"] - tp) * sign >= 0:
                    return {"exit_price": bar["o"] * (1 - slip * sign), "reason": "TAKE_PROFIT",
                            "held_min": held_min, "mfe_pct": peak_profit_pct}
                if _pct(bar["c"]) <= -gap_stop_pct:
                    return {"exit_price": bar["c"] * (1 - slip * sign), "reason": "OVERNIGHT_GAP_STOP",
                            "held_min": held_min, "mfe_pct": peak_profit_pct}
            prev_day = cur_day

        fav_px = bar["h"] if direction == "LONG" else bar["l"]
        adv_px = bar["l"] if direction == "LONG" else bar["h"]
        seq = [(adv_px, False), (fav_px, True)] if ordering == "pess" else [(fav_px, True), (adv_px, False)]

        exited = None
        for px, is_fav in seq:
            if is_fav:
                new_peak = max(peak_profit_pct, _pct(px))
                peak_profit_pct = new_peak
                # 规则1.5: TP (favorable extreme 触及)
                if tp_active and ((direction == "LONG" and px >= tp) or
                                  (direction == "SHORT" and px <= tp)):
                    exited = {"exit_price": tp * (1 - slip * sign), "reason": "TAKE_PROFIT"}
                    break
            else:
                eff = _active_sl(peak_profit_pct, held_min, mins_from_open)
                hit = (px - eff) * sign <= 0 if eff > 0 else False
                if hit:
                    exited = {"exit_price": eff * (1 - slip * sign),
                              "reason": _classify_sl(eff)}
                    break
                # 方案 E: 隔夜仓 gap_stop 每轮询生效 (生产规则2, 序在 SL 后);
                #   宽SL桶(SL>3%)次日起此线先于 SL 收敛
                if e_mode and day_idx >= 2 and _pct(px) <= -gap_stop_pct:
                    gap_line = entry * (1 - gap_stop_pct / 100 * sign)
                    exited = {"exit_price": gap_line * (1 - slip * sign),
                              "reason": "OVERNIGHT_GAP_STOP"}
                    break
        if exited:
            exited["held_min"] = held_min
            exited["mfe_pct"] = peak_profit_pct
            return exited

        # ── bar close 时间类退出 (优先级: PRE_CLOSE > MFE_STALL > TIME_EXIT) ──
        # 方案 E: e_full 对 SHORT 全退役; e_step1 保留但 PRE_CLOSE 赢家跳过 (生产 :5318)
        close_pct = _pct(bar["c"])
        e_time_retired = (e_mode == "full")
        if not e_time_retired and (t.hour > 15 or (t.hour == 15 and t.minute >= 52)):
            if e_mode and close_pct > 0:
                pass  # e_step1: 赢家持夜 (生产 winners-only 语义)
            else:
                return {"exit_price": bar["c"], "reason": "PRE_CLOSE_EXIT",
                        "held_min": held_min, "mfe_pct": peak_profit_pct}
        if not e_time_retired and stall_on and held_min >= params["stall_minutes"] \
                and peak_profit_pct < params["stall_mfe_pct"] and close_pct < 0:
            return {"exit_price": bar["c"], "reason": "MFE_STALL_EXIT",
                    "held_min": held_min, "mfe_pct": peak_profit_pct}
        if not e_time_retired and held_min > time_exit_min \
                and (not time_exit_only_loss or close_pct < 0):
            return {"exit_price": bar["c"], "reason": "TIME_EXIT",
                    "held_min": held_min, "mfe_pct": peak_profit_pct}
        # 方案 E: MAX_HOLD 兜底 — deadline 日首个 bar close 强平 (生产盘初轮询语义,
        #   序在 TIME_EXIT 后: e_step1 隔夜输家在 T+2 盘初先被 TIME_EXIT 收编)
        if e_mode and day_idx > max_hold_days:
            return {"exit_price": bar["c"], "reason": "MAX_HOLD",
                    "held_min": held_min, "mfe_pct": peak_profit_pct}
        # 方案 E: 周末/长假护栏 (S5) — 下一交易日距今 >2 日历日 → 当周最后收盘平, 不过周末
        if e_mode and i_bar + 1 < n_bars \
                and (bars[i_bar + 1]["t"].date() - t.date()).days > 2:
            return {"exit_price": bar["c"], "reason": "FRIDAY_FLAT",
                    "held_min": held_min, "mfe_pct": peak_profit_pct}

    last = bars[-1]
    return {"exit_price": last["c"], "reason": "REPLAY_EOD",
            "held_min": (last["t"] - tr["entry_et"]).total_seconds() / 60,
            "mfe_pct": peak_profit_pct}


# ────────────────────────────────────────────────────────────
# 聚合
# ────────────────────────────────────────────────────────────

def _fee_of(tr: dict) -> float | None:
    """返回单笔费用；费用证据缺失或异常时返回 None，绝不静默当零。

    当前账本约定：realized_pnl=毛、fees_est=费、pnl_net=净。优先使用明确费用列，
    其次用毛−净反推。无新口径字段的旧外部字典仍按历史 live_pnl=净兼容；
    load_trades 总会提供新字段，因此缺失费用不会被该兼容分支掩盖。
    """
    sign = 1.0 if tr["direction"] == "LONG" else -1.0
    gross = tr["qty"] * sign * (tr["live_exit"] - tr["entry"])
    if tr.get("live_fee_est") is not None:
        fee = float(tr["live_fee_est"])
    else:
        if "live_net" in tr:
            net = tr.get("live_net")
        else:
            net = tr.get("live_pnl")  # 旧外部调用方的历史约定：live_pnl=净
        if net is None:
            return None
        fee = gross - float(net)
    notional = tr["qty"] * tr["entry"]
    return fee if 0 <= fee <= 0.03 * notional else None


SPLIT_DATE = "2026-07-20"  # 当前退出配置时代 (dd=0.8 07-19 起) — 宪章口径2: 新旧窗口分层


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}
    pnls = [r["net"] for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    by_reason: dict = {}
    for r in results:
        d = by_reason.setdefault(r["reason"], {"n": 0, "sum_net": 0.0})
        d["n"] += 1
        d["sum_net"] = round(d["sum_net"] + r["net"], 2)
    return {
        "n": n,
        "sum_net": round(sum(pnls), 2),
        "expectancy": round(sum(pnls) / n, 2),
        "win_rate": round(len(wins) / n, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr": round(avg_win / avg_loss, 2) if avg_loss > 0 else None,
        "by_reason": by_reason,
        "killed_winners": sum(1 for r in results if r["live_net"] > 0 and r["net"] <= 0),
        "rescued_losers": sum(1 for r in results if r["live_net"] <= 0 and r["net"] > 0),
        "small_n_warning": n < 30,
    }


# ────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-24")
    ap.add_argument("--until", default=None)
    ap.add_argument("--slippage-bps", type=float, default=3.0)
    ap.add_argument("--refresh-bars", action="store_true")
    ap.add_argument("--e-days", type=int, default=6,
                    help="方案E: SHORT 加抓 entry 后 N 个日历日 bars (隔夜回放)")
    ap.add_argument("--out", default=str(REPO / "data" / "exit_replay_latest.json"))
    args = ap.parse_args()

    factor_rules, params = _load_configs()
    trades = load_trades(args.since, args.until)
    print(f"交易: {len(trades)} 笔 PLAYBOOK_SIM 已平仓 (since {args.since})")

    # 零候选窗口也不得连接 OpenD 或保留旧产物；后续统一落诊断报告。
    bars_map = collect_bars(trades, args.refresh_bars, e_days=args.e_days) if trades else {}

    # 组装每笔的 bar 序列 + 因子退出配置
    # _bars: entry→live exit (baseline 保真, 口径与 07-25 版一致);
    # _bars_ext: entry→entry+e_days (方案E隔夜回放用; baseline 不受 ext 影响 — PRE_CLOSE 日0先收编)
    runnable, skipped, skipped_fee_unknown = [], 0, 0
    for tr in trades:
        seq_ext: list[dict] = []
        end_ext = max(tr["exit_et"].date(), tr["entry_et"].date() + timedelta(days=args.e_days))
        d = tr["entry_et"].date()
        while d <= end_ext and (d - tr["entry_et"].date()).days <= max(6, args.e_days):
            day_bars = bars_map.get((tr["symbol"], d.strftime("%Y-%m-%d")))
            if day_bars:
                seq_ext.extend(day_bars)
            d += timedelta(days=1)
        # 跳过入场 bar (含入场分钟): 从下一分钟开始 (实盘 ~60s 轮询等效)
        seq_ext = [b for b in seq_ext if b["t"] > tr["entry_et"].replace(second=0, microsecond=0)]
        exit_day = tr["exit_et"].date()
        seq = [b for b in seq_ext if b["t"].date() <= exit_day]
        if len(seq) < 5:
            skipped += 1
            continue
        tr["_bars"] = seq
        tr["_bars_ext"] = seq_ext if len(seq_ext) >= 5 else seq
        tr["_exit_cfg"] = _get_factor_exit_config(tr["conditions"], factor_rules,
                                                  direction=tr["direction"])
        fee = _fee_of(tr)
        if fee is None:
            skipped_fee_unknown += 1
            continue
        tr["_fee"] = fee
        runnable.append(tr)
    print(f"可回放: {len(runnable)} 笔 (缺 bar 跳过 {skipped}; 费用未知跳过 {skipped_fee_unknown})")

    live_sum_gross = sum(t["live_gross"] for t in runnable)
    live_sum_net = sum(t["live_net"] for t in runnable)
    live_sum_fees = sum(t["_fee"] for t in runnable)
    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": args.since, "until": args.until,
        "trades_total": len(trades), "trades_replayed": len(runnable),
        "live_sum_gross": round(live_sum_gross, 2),
        "live_sum_fees": round(live_sum_fees, 2),
        "live_sum_net": round(live_sum_net, 2),
        "exclusions": {"no_bars": skipped, "fee_unknown": skipped_fee_unknown},
        "fee_coverage": {
            "known_n": len(runnable), "unknown_n": skipped_fee_unknown,
            "sources": sorted({str(t.get("fees_source") or "unknown") for t in runnable}),
            "policy": "费用未知即跳过，不静默当零；净值优先 pnl_net，缺失时毛利−fees_est",
        },
        "slippage_bps": args.slippage_bps,
        "params": params,
        "validation": {},
        "variants": {},
    }
    if not runnable:
        # 必须覆盖旧产物：全样本不可回放时，下游不得继续读到上一轮的变体结论。
        report["status"] = "NO_CANDIDATE_TRADES" if not trades else "NO_REPLAYABLE_TRADES"
        report["frozen_by_fidelity"] = True
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print("⛔ 无可回放样本：仅输出诊断，variants 为空；旧产物已被覆盖。")
        print(f"产出: {args.out}")
        return

    variants = _variants()

    # ── 验证: live_baseline vs 实盘记录 ──
    base_var = variants[0]
    deltas, reason_match, mfe_deltas = [], 0, []
    base_details = []
    for tr in runnable:
        rp = replay_trade(tr, tr["_bars"], tr["_exit_cfg"], base_var, params, "pess",
                          args.slippage_bps)
        sign = 1.0 if tr["direction"] == "LONG" else -1.0
        replay_pct = (rp["exit_price"] - tr["entry"]) * sign / tr["entry"] * 100
        live_pct = (tr["live_exit"] - tr["entry"]) * sign / tr["entry"] * 100
        deltas.append(abs(replay_pct - live_pct))
        mfe_deltas.append(abs(rp["mfe_pct"] - tr["live_mfe"]))
        if rp["reason"] == tr["live_reason"]:
            reason_match += 1
        base_details.append({
            "id": tr["id"], "symbol": tr["symbol"], "dir": tr["direction"],
            "entry_day": tr["entry_et"].strftime("%Y-%m-%d"),
            "live_reason": tr["live_reason"], "replay_reason": rp["reason"],
            "live_pct": round(live_pct, 3), "replay_pct": round(replay_pct, 3),
            "replay_held": round(rp["held_min"], 1),
        })
    deltas.sort()
    mfe_deltas.sort()
    n = len(deltas)
    report["validation"] = {
        "reason_match_pct": round(reason_match / n, 3),
        "exit_pct_delta_median": round(deltas[n // 2], 4),
        "exit_pct_delta_p90": round(deltas[int(n * 0.9)], 4),
        "mfe_delta_median": round(mfe_deltas[n // 2], 4),
        "note": "baseline 回放 vs 实盘记录; 差异来源=bar粒度/轮询时点/成交滑点",
        "per_trade": base_details,
    }

    report["validation"]["fidelity_ok"] = report["validation"]["reason_match_pct"] >= 0.80
    report["frozen_by_fidelity"] = not report["validation"]["fidelity_ok"]
    if report["frozen_by_fidelity"]:
        # 文档既定纪律必须成为机器门：失败时只留 baseline 诊断，不生成可裁决变体。
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        v = report["validation"]
        print(f"\n[验证] baseline vs 实盘: reason一致 {v['reason_match_pct']:.0%}, "
              f"退出价差 median {v['exit_pct_delta_median']:.3f}% "
              f"p90 {v['exit_pct_delta_p90']:.3f}%, MFE差 median {v['mfe_delta_median']:.3f}%")
        print("⛔ 保真门未过 (reason_match < 0.80)：冻结全部变体，仅输出诊断。")
        print(f"产出: {args.out}")
        return

    # ── 变体网格: 双序各跑 (含 07-20 分层 + pess 逐笔明细) ──
    baseline_rows: dict = {}
    e_rows: dict = {}
    for var in variants:
        use_ext = bool(var.get("e_mode"))
        for ordering in ("pess", "opt"):
            results = []
            for tr in runnable:
                bars = tr["_bars_ext"] if use_ext else tr["_bars"]
                rp = replay_trade(tr, bars, tr["_exit_cfg"], var, params,
                                  ordering, args.slippage_bps)
                sign = 1.0 if tr["direction"] == "LONG" else -1.0
                net = tr["qty"] * sign * (rp["exit_price"] - tr["entry"]) - tr["_fee"]
                # 右删失: E变体SHORT仓 REPLAY_EOD = 前向 bars 尚未存在 (近2日入场),
                #   非真实退出 — MAX_HOLD/FRIDAY_FLAT 本会在未来交易日收编
                censored = use_ext and tr["direction"] == "SHORT" and rp["reason"] == "REPLAY_EOD"
                results.append({"id": tr["id"], "day": tr["entry_et"].strftime("%Y-%m-%d"),
                                "symbol": tr["symbol"], "dir": tr["direction"], "net": round(net, 2),
                                "live_net": tr["live_net"], "reason": rp["reason"],
                                "held_min": round(rp["held_min"], 1), "censored": censored})
            entry = report["variants"].setdefault(var["name"], {})
            entry[ordering] = aggregate(results)
            entry[f"{ordering}_recent"] = aggregate(
                [r for r in results if r["day"] >= SPLIT_DATE])
            entry[f"{ordering}_short"] = aggregate(
                [r for r in results if r["dir"] == "SHORT" and not r["censored"]])
            n_cens = sum(1 for r in results if r["censored"])
            if n_cens:
                entry["n_censored"] = n_cens
            if var["name"] == "live_baseline":
                baseline_rows[ordering] = results
            if use_ext:
                e_rows.setdefault(var["name"], {})[ordering] = results
            if ordering == "pess":
                entry["per_trade"] = [
                    {"id": r["id"], "sym": r["symbol"], "day": r["day"], "dir": r["dir"],
                     "net": r["net"], "live": r["live_net"], "reason": r["reason"],
                     "held": r["held_min"]} for r in results]

    # ── 方案 E 闸门读数 (DEB-20260802 三道闸之门1: 期望>0 且非单尾驱动) ──
    # 成对Δ: 同一批未删失 SHORT 上 E vs baseline 逐笔差 (删失交易两端同剔, 口径干净)
    e_names = [v["name"] for v in variants if v.get("e_mode")]
    if e_names:
        gate: dict = {}
        for nm in e_names:
            row: dict = {}
            for o in ("pess", "opt"):
                bmap = {r["id"]: r for r in baseline_rows.get(o, [])}
                pairs = [(r, bmap[r["id"]]) for r in e_rows[nm][o]
                         if r["dir"] == "SHORT" and not r["censored"] and r["id"] in bmap]
                n_cens = sum(1 for r in e_rows[nm][o] if r["dir"] == "SHORT" and r["censored"])
                if not pairs:
                    continue
                n = len(pairs)
                e_sum = sum(r["net"] for r, _ in pairs)
                b_sum = sum(b["net"] for _, b in pairs)
                e_wr = sum(1 for r, _ in pairs if r["net"] > 0) / n
                b_wr = sum(1 for _, b in pairs if b["net"] > 0) / n
                by_reason: dict = {}
                for r, _ in pairs:
                    d = by_reason.setdefault(r["reason"], {"n": 0, "sum_net": 0.0})
                    d["n"] += 1
                    d["sum_net"] = round(d["sum_net"] + r["net"], 2)
                row[o] = {"n": n, "n_censored": n_cens,
                          "sum_net": round(e_sum, 2), "base_sum": round(b_sum, 2),
                          "d_sum": round(e_sum - b_sum, 2),
                          "exp": round(e_sum / n, 2),
                          "d_exp": round((e_sum - b_sum) / n, 2),
                          "wr": round(e_wr, 3),
                          "d_wr_pp": round((e_wr - b_wr) * 100, 1),
                          "by_reason": by_reason}
            gate[nm] = row
        report["e_gate"] = gate

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n产出: {args.out}")

    # ── 打印对比表 (Δbaseline = 同机制变体间增量, 诚实口径; Δlive 混入回放偏差仅参考) ──
    v = report["validation"]
    print(f"\n[验证] baseline vs 实盘: reason一致 {v['reason_match_pct']:.0%}, "
          f"退出价差 median {v['exit_pct_delta_median']:.3f}% p90 {v['exit_pct_delta_p90']:.3f}%, "
          f"MFE差 median {v['mfe_delta_median']:.3f}%")
    vb = report["variants"]["live_baseline"]
    base_lo = min(vb["pess"]["sum_net"], vb["opt"]["sum_net"])
    base_hi = max(vb["pess"]["sum_net"], vb["opt"]["sum_net"])
    print(f"[校准] 实盘 live_sum_net={live_sum_net:.0f} (毛 {live_sum_gross:.0f} − 费 {live_sum_fees:.0f}) "
          f"vs 回放 baseline={base_lo:.0f}~{base_hi:.0f} "
          f"→ 回放固有偏差 {(base_lo - live_sum_net):+.0f}~{(base_hi - live_sum_net):+.0f} "
          f"(bar影线>轮询采样, 变体排序不受影响)")
    print(f"\n{'variant':<24} {'n':>4} {'sum$区间':>13} {'Δbaseline$':>11} "
          f"{'exp$':>9} {'WR':>5} {'RR':>5} {'杀赢':>5} {'救亏':>5} {'≥07-20Δ':>8}")
    print(f"{'live_实盘记录':<24} {len(runnable):>4} {live_sum_net:>13.0f}")
    for var in variants:
        name = var["name"]
        vd = report["variants"][name]
        p, o = vd["pess"], vd["opt"]
        sum_lo, sum_hi = sorted([p["sum_net"], o["sum_net"]])
        d_lo, d_hi = sum_lo - base_hi, sum_hi - base_lo
        exp_lo, exp_hi = sorted([p["expectancy"], o["expectancy"]])
        rp_, ro_ = vd.get("pess_recent", {}), vd.get("opt_recent", {})
        rb_lo = min(vb["pess_recent"]["sum_net"], vb["opt_recent"]["sum_net"])
        rb_hi = max(vb["pess_recent"]["sum_net"], vb["opt_recent"]["sum_net"])
        r_d = (min(rp_.get("sum_net", 0), ro_.get("sum_net", 0)) - rb_hi,
               max(rp_.get("sum_net", 0), ro_.get("sum_net", 0)) - rb_lo)
        flag = " ⚠n<30" if p.get("small_n_warning") else ""
        print(f"{name:<24} {p['n']:>4} {sum_lo:>6.0f}~{sum_hi:<6.0f} "
              f"{d_lo:>+5.0f}~{d_hi:<+5.0f} {exp_lo:>4.1f}~{exp_hi:<4.1f} "
              f"{p['win_rate']:>5.2f} {p['rr'] or 0:>5.2f} "
              f"{p['killed_winners']:>3}/{o['killed_winners']:<2}"
              f"{p['rescued_losers']:>3}/{o['rescued_losers']:<2} "
              f"{r_d[0]:>+4.0f}~{r_d[1]:<+4.0f}{flag}")

    if e_names:
        print(f"\n[方案E·闸门1读数] SHORT-only 成对Δ (同批未删失交易上 E vs baseline, 两端同剔)")
        for nm in e_names:
            for o in ("pess", "opt"):
                g = report["e_gate"][nm].get(o)
                if not g:
                    continue
                rs = ", ".join(f"{k}×{v2['n']}(${v2['sum_net']:+.0f})"
                               for k, v2 in sorted(g["by_reason"].items()))
                cens = f" 剔删失×{g['n_censored']}" if g.get("n_censored") else ""
                print(f"  {nm}[{o}] n={g['n']}{cens} net=${g['sum_net']:+.0f} (Δ{g['d_sum']:+.0f}) "
                      f"exp=${g['exp']:+.2f}/笔 (Δ{g['d_exp']:+.2f}) "
                      f"WR={g['wr']:.1%} (Δ{g['d_wr_pp']:+.1f}pp)\n    {rs}")


if __name__ == "__main__":
    main()
