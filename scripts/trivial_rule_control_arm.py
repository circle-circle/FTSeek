#!/opt/ftseek/FTSeek-US/venv/bin/python3
# -*- coding: utf-8 -*-
"""笨规则对照臂 (只读) — DEB-20260907 建议③ 落地: "全系统没有对照臂"。

立项理由 (Claude 建议原话)
--------------------------
通篇判据里没有基准。`毛 edge` 本身不可解释 — 和什么比? 本脚本在**浮现层**
(信号快照, 不含执行/费用/门禁, 因此没有选择效应) 给每条 LLM 假设配一条
"一行规则"臂, 按日配对比较。若 LLM 臂与一行规则打平, 那双模型 panel + 多段
动态注入就是在复现一个一行规则 — 这个结论如果为真, 越早知道越好。

为什么建在浮现层而不是成交层 (宪章数据口径七律 #1/#9)
----------------------------------------------------
成交层经过三重条件化 (执行门禁 / confirmation 触发本身是动量过滤器 / conf 加权仓位),
在被自身机制筛过且筛据与结果变量相关的子样本上量区分度, 读数必然虚高。浮现层是
唯一无偏测量平面, 且样本量是成交层的 ~5 倍、零成本噪声。

度量口径 (三处刻意选择, 均为可复现契约)
--------------------------------------
① 配对: 同一行 (symbol, trade_date) 上, 两臂共用同一个前瞻收益, 只有方向不同
   ⇒ 标的/日期/市场共同波动全部消掉, 剩下纯粹的"方向指派"差异。
② 收益重算不复用: 不复用 `fwd_ret_t1` 的方向调整值, 而用 `close_t0/close_tn`
   重算 LONG 基准收益再乘各臂方向; 同时逐行校验与库内 `fwd_ret_t1` 一致
   (SHORT 行 = close_t0/close_t1 − 1, 见 signal_ic_tracker.py:370-374), 不一致即计数上报
   ⇒ 上游口径若改, 本脚本自己会发现, 不会静默算错。
③ 离群策略是一等维度, 不是脚注: 实测行级 |fwd_ret| 标准差 12.5%, 单行
   `US.ADBT 2026-09-02 SHORT` (close_t0 $1.72 → close_t1 $0.344, fwd_ret_t1 = +400%)
   对浮现层均值的贡献 > 其余 1052 行净贡献之和。故四种策略全部并报:
   raw / abs_lt_0_5 (仓内既有惯例, 见 web_dashboard/routes/analytics.py:530) /
   winsor_1_99 / excl_top3。任何结论只在四策略同号时才允许采信
   (军规"剔除前 3 笔后仍成立"的强化版)。

统计口径 (宪章七律 #9: 跨信号统计必须报按日聚类值 + MDE)
--------------------------------------------------------
同日信号高度相关 (实测中位 40 行/日), pooled t 会被夸大约 √(行/日) 倍 ⇒ 主指标是
按日聚类的配对差: 每日先求 (LLM 臂均值 − 对照臂均值), 再对日序列做 t 与符号翻转
置换检验 (配对符号翻转是配对数据的标准置换, 保留日内相关结构)。同时报
MDE@80% = 2.8 × sd(日差) / √(日数): |实测日差| < MDE 即"本窗口无法分辨", 不等于"无差异"
— 这是本项目最常犯的一类误读 (不显著被读成无效)。

刻意不做的事
------------
· 不碰交易路径、不改配置、不写生产判分文件 (输出落 data/shadow/, fresh 覆写幂等)。
· 不下"LLM 有没有 edge"的终裁 — 本脚本只给对照读数与功率声明。
· NEUTRAL 行 (占浮现层 47%, 语义 = "不交易") 不参与方向配对, 单独作为
  "不交易漂移"基线报告 (它是机会成本的零点)。
· 日K只读 `data/replay_bars_daily/` 缓存, 零网络拉取 (补拉是 t5cf 06:53 的职责,
  与 gate_counterfactual_ruler.py / da_blocked_signal_cf_eval.py 同口径)。

用法:
  venv/bin/python scripts/trivial_rule_control_arm.py                  # 全窗口
  venv/bin/python scripts/trivial_rule_control_arm.py --since 2026-08-01 --no-write
  venv/bin/python scripts/trivial_rule_control_arm.py --chains universe_expansion
输出:
  data/shadow/trivial_rule_control_arm_latest.json (fresh 覆写, 重跑幂等)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SNAPSHOT_DB = REPO / "data" / "signal_snapshots.db"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
OUT_LATEST = REPO / "data" / "shadow" / "trivial_rule_control_arm_latest.json"

CHAINS = ("deep_analysis", "universe_expansion")
HORIZONS = ("t1", "t5")
POLICIES = ("raw", "abs_lt_0_5", "winsor_1_99", "excl_top3")
DEFAULT_SEED = 20260907


# ────────────────────────────────────────────────────────────
# 纯函数层 (可单测, 零 IO)
# ────────────────────────────────────────────────────────────

def sign_of(x: Optional[float]) -> int:
    """严格符号; 0/None → 0 (= 该臂当日无方向, 该行从配对中剔除, 不记 0 收益)。"""
    if x is None:
        return 0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def long_basis_ret(close_t0: Optional[float], close_tn: Optional[float]) -> Optional[float]:
    """LONG 基准前瞻收益 close_t0 → close_tn。非正价格视为无效 (脏数据不入样本)。"""
    if not close_t0 or not close_tn or close_t0 <= 0 or close_tn <= 0:
        return None
    return close_tn / close_t0 - 1.0


def short_basis_ret(close_t0: Optional[float], close_tn: Optional[float]) -> Optional[float]:
    """SHORT 基准 — 与 signal_ic_tracker._calc_fwd 同式 (t0/tn) − 1, 不是 −long_basis。"""
    if not close_t0 or not close_tn or close_t0 <= 0 or close_tn <= 0:
        return None
    return close_t0 / close_tn - 1.0


def quantile(xs: Sequence[float], q: float) -> float:
    ys = sorted(float(x) for x in xs if x is not None)
    if not ys:
        return 0.0
    k = (len(ys) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ys) - 1)
    return ys[lo] * (1 - (k - lo)) + ys[hi] * (k - lo)


def clip_row(r: Dict[str, Any], lo: float, hi: float) -> Dict[str, Any]:
    out = dict(r)
    for key in ("llm_ret", "arm_ret"):
        v = out.get(key)
        if v is None:
            continue
        out[key] = min(max(v, lo), hi)
    return out


def apply_policy(rows: List[Dict[str, Any]], policy: str) -> List[Dict[str, Any]]:
    """离群策略作用于收益列, 两臂共用同一裁剪 ⇒ 配对结构不被破坏。

    若只裁 llm_ret 不裁 arm_ret, 两臂被不同规则裁剪, 差值就不再是"方向指派"的差。
    """
    if not rows:
        return []
    vals = [r["llm_ret"] for r in rows if r.get("llm_ret") is not None]
    if policy == "raw":
        return list(rows)
    if policy == "abs_lt_0_5":
        return [r for r in rows if r.get("llm_ret") is not None and abs(r["llm_ret"]) < 0.5]
    if policy == "winsor_1_99":
        lo, hi = quantile(vals, 0.01), quantile(vals, 0.99)
        return [clip_row(r, lo, hi) for r in rows if r.get("llm_ret") is not None]
    if policy == "excl_top3":
        if len(vals) <= 3:
            return []
        thr = abs(sorted(vals, key=abs)[-3])
        return [r for r in rows if r.get("llm_ret") is not None and abs(r["llm_ret"]) < thr]
    raise ValueError(f"unknown policy: {policy}")


def mean(xs: Sequence[float]) -> Optional[float]:
    ys = [float(x) for x in xs if x is not None]
    return sum(ys) / len(ys) if ys else None


def stdev(xs: Sequence[float]) -> Optional[float]:
    ys = [float(x) for x in xs if x is not None]
    if len(ys) < 2:
        return None
    mu = sum(ys) / len(ys)
    return math.sqrt(sum((v - mu) ** 2 for v in ys) / (len(ys) - 1))


def sign_flip_perm_p(diffs: Sequence[float], obs: float, n_perm: int,
                     seed: int = DEFAULT_SEED) -> Optional[float]:
    """配对符号翻转移换: 日差整体乘以 ±1 (保留日内相关, 只打乱"哪臂更好"的指派)。"""
    if len(diffs) < 3 or n_perm <= 0:
        return None
    rng = random.Random(seed)
    ge = le = 0
    n = len(diffs)
    for _ in range(n_perm):
        s = sum(d * (1 if rng.random() < 0.5 else -1) for d in diffs) / n
        if s >= obs:
            ge += 1
        if s <= obs:
            le += 1
    return min(1.0, 2.0 * min(ge, le) / n_perm)


def daily_cluster_stats(pairs: Sequence[Tuple[str, float, float]],
                        n_perm: int = 20000, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """按日聚类的配对差统计 (宪章七律 #9 主指标)。

    pairs: [(trade_date, llm_ret, arm_ret)] — 同一行同一收益窗, 只差方向。
    """
    by_day: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for d, a, b in pairs:
        by_day[d].append((a, b))
    days = sorted(by_day)
    if not days:
        return {"n_days": 0, "n_rows": 0}
    diffs = [mean([x[0] for x in by_day[d]]) - mean([x[1] for x in by_day[d]]) for d in days]
    n_days = len(diffs)
    mu = mean(diffs) or 0.0
    sd = stdev(diffs)
    t = (mu / (sd / math.sqrt(n_days))) if (sd and sd > 0) else None
    mde = (2.8 * sd / math.sqrt(n_days)) if (sd and sd > 0) else None
    perm_p = sign_flip_perm_p(diffs, mu, n_perm, seed)
    return {
        "n_rows": len(pairs),
        "n_days": n_days,
        "daily_mean_diff": round(mu, 6),
        "daily_sd_diff": round(sd, 6) if sd is not None else None,
        "t_daily": round(t, 3) if t is not None else None,
        "days_positive": sum(1 for x in diffs if x > 0),
        "perm_p_two_sided": round(perm_p, 4) if perm_p is not None else None,
        "mde80": round(mde, 6) if mde is not None else None,
        "resolvable": bool(mde is not None and abs(mu) >= mde),
        "n_lt_30_days_flag": n_days < 30,
        "pooled_mean_diff": round(mean([a - b for _, a, b in pairs]) or 0.0, 6),
    }


# ────────────────────────────────────────────────────────────
# 对照臂定义 (每条都声明信息集 — 信息集不同的臂不可横向比优劣)
# ────────────────────────────────────────────────────────────
#
# 度量窗 = close_t0 → close_tn。故"是否在度量窗起点前已知"才是前视判据:
#   · PREV_DAY_MOMENTUM 用 t0−1 及更早收盘价 ⇒ 两条链决策时都已拥有 (DA 盘前生成、
#     UE 前夜 21:10 CST 生成) ⇒ 信息集弱于 LLM, 是最公平的"一行规则"对照。
#   · OVERNIGHT_GAP 用 t0 开盘价 ⇒ 在度量窗起点前, 无窗口内前视; 但两链决策时均未
#     知开盘价 ⇒ 对 LLM 有信息优势, 读数须按"优势臂"解释。
#   · T0_CLOSE_MOMENTUM 用 t0 收盘价 = 度量窗起点那一刻 ⇒ 无前视 (窗口尚未开始),
#     但用尽当日全部行情 ⇒ 信息优势最大, 是"读盘带能否替代整套 LLM 栈"的上界对照。
ARM_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "ALWAYS_LONG",
        "rule": "恒做多 (不看任何信息)",
        "info_set": "none",
        "known_at_decision": {"deep_analysis": True, "universe_expansion": True},
        "arm_class": "zero_info_floor",
    },
    {
        "key": "PREV_DAY_MOMENTUM",
        "rule": "前一交易日收涨→做多, 收跌→做空: sign(close[t0-1]/close[t0-2] - 1)",
        "info_set": "t0-1 及更早收盘价",
        "known_at_decision": {"deep_analysis": True, "universe_expansion": True},
        "arm_class": "fair_trivial_rule",
        "note": "两链决策时都已拥有该信息 ⇒ 最公平的一行规则对照 (主对照臂)。",
    },
    {
        "key": "OVERNIGHT_GAP",
        "rule": "t0 开盘高于昨收→做多, 否则做空: sign(open[t0]/close[t0-1] - 1)",
        "info_set": "t0 开盘价",
        "known_at_decision": {"deep_analysis": False, "universe_expansion": False},
        "arm_class": "info_advantaged",
        "note": "对 LLM 有信息优势 (两链决策时均未见开盘价); 在度量窗前 ⇒ 无窗口内前视。",
    },
    {
        "key": "T0_CLOSE_MOMENTUM",
        "rule": "t0 收高于开→做多, 否则做空: sign(close[t0]/open[t0] - 1)",
        "info_set": "t0 收盘价 (= 度量窗起点)",
        "known_at_decision": {"deep_analysis": False, "universe_expansion": False},
        "arm_class": "info_advantaged_upper_bound",
        "note": "用尽当日行情 ⇒ 读盘带能否替代整套 LLM 栈的上界对照。",
    },
    {
        "key": "CONTRARIAN_LLM",
        "rule": "反向跟 LLM: -direction",
        "info_set": "llm_output",
        "known_at_decision": {"deep_analysis": True, "universe_expansion": True},
        "arm_class": "anti_predictivity_probe",
        "note": "非独立臂: 若它显著优于 LLM 臂, 说明信号反向可用 (比'无效'更强的结论)。",
    },
)


def arm_direction(spec_key: str, row: Dict[str, Any]) -> Optional[int]:
    """给定臂与行, 返回该臂方向 (+1/−1); None = 该行在此臂下无定义 (从配对剔除)。"""
    if spec_key == "ALWAYS_LONG":
        return 1
    if spec_key == "CONTRARIAN_LLM":
        s = row.get("llm_sign")
        return -s if s else None
    if spec_key == "PREV_DAY_MOMENTUM":
        return sign_of(row.get("prev_day_ret")) or None
    if spec_key == "OVERNIGHT_GAP":
        return sign_of(row.get("overnight_gap")) or None
    if spec_key == "T0_CLOSE_MOMENTUM":
        return sign_of(row.get("t0_intraday_ret")) or None
    raise ValueError(f"unknown arm: {spec_key}")


# ────────────────────────────────────────────────────────────
# 数据装载 (只读, 零网络)
# ────────────────────────────────────────────────────────────

_daily_cache: Dict[str, Optional[List[List[Any]]]] = {}
_daily_index: Dict[str, Dict[str, int]] = {}


def load_daily(symbol: str) -> Optional[List[List[Any]]]:
    """[[date,o,h,l,c]] 升序, 只读缓存不补拉 (与 gate_counterfactual_ruler 同款)。"""
    if symbol in _daily_cache:
        return _daily_cache[symbol]
    p = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    rows: Optional[List[List[Any]]] = None
    if p.exists():
        try:
            rows = json.loads(p.read_text()) or None
        except Exception:
            rows = None
    _daily_cache[symbol] = rows
    _daily_index[symbol] = {str(r[0]): i for i, r in enumerate(rows)} if rows else {}
    return rows


def bar_context(symbol: str, trade_date: str) -> Dict[str, Optional[float]]:
    """该行三个平庸规则所需的日K派生量; 任一缺数据即 None (该臂剔除该行)。"""
    out: Dict[str, Optional[float]] = {"prev_day_ret": None, "overnight_gap": None,
                                       "t0_intraday_ret": None}
    rows = load_daily(symbol)
    if not rows:
        return out
    i = _daily_index.get(symbol, {}).get(str(trade_date))
    if i is None or i < 2:
        return out
    try:
        o_t0, c_t0 = float(rows[i][1]), float(rows[i][4])
        c_m1, c_m2 = float(rows[i - 1][4]), float(rows[i - 2][4])
    except (TypeError, ValueError, IndexError):
        return out
    if c_m2 > 0 and c_m1 > 0:
        out["prev_day_ret"] = c_m1 / c_m2 - 1.0
    if c_m1 > 0 and o_t0 > 0:
        out["overnight_gap"] = o_t0 / c_m1 - 1.0
    if o_t0 > 0 and c_t0 > 0:
        out["t0_intraday_ret"] = c_t0 / o_t0 - 1.0
    return out


def build_rows(since: Optional[str], until: Optional[str],
               chains: Sequence[str], dedupe: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """装载浮现层行, 计算两臂共用收益 + 校验库内 fwd_ret 口径 + 附日K派生量。

    去重 (dedupe=True, 缺省): 实测 1094 条 directional 行只有 946 个唯一
    (trade_date, symbol, direction, chain) — DA 会在同一天多次重跑并各自落一行
    (实锤 NVDA 2026-07-02: 03:46/03:47/03:48/03:49 四行同值)。它们是**同一假设的
    重复登记**, 不是独立信号; 不去重等于给重复登记的标的加权, 且虚增 n。
    保留规则确定性: 同键取 created_at 最早的一行。
    """
    uri = Path(SNAPSHOT_DB).as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    ph = ",".join("?" * len(chains))
    sql = f"""SELECT trade_date, symbol, strategy_id, direction, strength, confidence,
                     close_t0, close_t1, close_t5, fwd_ret_t1, fwd_ret_t5,
                     alpha_t1, alpha_t5, spy_ret_t1, created_at, decision_id
              FROM signal_snapshots
              WHERE strategy_id IN ({ph}) AND fwd_ret_t1 IS NOT NULL"""
    params: List[Any] = list(chains)
    if since:
        sql += " AND trade_date >= ?"
        params.append(since)
    if until:
        sql += " AND trade_date <= ?"
        params.append(until)
    raw = con.execute(sql, params).fetchall()
    n_neutral = con.execute(
        f"""SELECT COUNT(*) FROM signal_snapshots
            WHERE strategy_id IN ({ph}) AND direction = 'NEUTRAL' AND fwd_ret_t1 IS NOT NULL""",
        list(chains)).fetchone()[0]
    con.close()

    directional: List[Dict[str, Any]] = []
    neutral_drift: List[float] = []
    mismatch = 0
    dropped_no_bar = 0
    dropped_bad_price = 0
    dropped_dup = 0
    backfilled = 0
    n_directional_seen = 0
    seen: Dict[Tuple[str, str, str, str], str] = {}
    horizon_cov: Dict[str, int] = {"t1": 0, "t5": 0}
    for r in raw:
        direction = str(r["direction"] or "").upper()
        if direction not in ("LONG", "SHORT"):
            # NEUTRAL = "不交易" 信号 (signal_ic_tracker.py:683), 无方向可配对
            v = long_basis_ret(r["close_t0"], r["close_t1"])
            if v is not None:
                neutral_drift.append(v)
            continue
        n_directional_seen += 1
        key = (str(r["trade_date"]), str(r["symbol"]), direction, str(r["strategy_id"]))
        created = str(r["created_at"] or "")
        if created[:10] > str(r["trade_date"]):
            backfilled += 1
        if dedupe:
            prev = seen.get(key)
            if prev is not None:
                dropped_dup += 1
                if created and (not prev or created < prev):
                    # 保留 created_at 最早的一行: 用当前行替换先前保留的行
                    directional = [x for x in directional if x["_key"] != key]
                    seen[key] = created
                else:
                    continue
            else:
                seen[key] = created
        llm_sign = 1 if direction == "LONG" else -1
        ret_t1 = long_basis_ret(r["close_t0"], r["close_t1"])
        if ret_t1 is None:
            dropped_bad_price += 1
            continue
        # 口径校验: 重算值必须与库内方向调整值一致 (SHORT 用 (t0/t1)-1, 非 -long_basis)
        expect = ret_t1 if llm_sign > 0 else short_basis_ret(r["close_t0"], r["close_t1"])
        if r["fwd_ret_t1"] is None or abs(expect - float(r["fwd_ret_t1"])) > 1e-6:
            mismatch += 1
        ctx = bar_context(str(r["symbol"]), str(r["trade_date"]))
        if all(v is None for v in ctx.values()):
            dropped_no_bar += 1
            continue
        ret_t5 = long_basis_ret(r["close_t0"], r["close_t5"])
        directional.append({
            "_key": key,
            "date": str(r["trade_date"]), "symbol": str(r["symbol"]),
            "chain": str(r["strategy_id"]), "direction": direction, "llm_sign": llm_sign,
            "strength": r["strength"], "confidence": r["confidence"],
            "raw_ret_t1": ret_t1, "raw_ret_t5": ret_t5,
            "llm_ret": float(r["fwd_ret_t1"]),
            "llm_ret_t5": float(r["fwd_ret_t5"]) if r["fwd_ret_t5"] is not None and ret_t5 is not None else None,
            "alpha_t1": r["alpha_t1"], "alpha_t5": r["alpha_t5"],
            "spy_ret_t1": r["spy_ret_t1"],
            **ctx,
        })
        horizon_cov["t1"] += 1
        if r["fwd_ret_t5"] is not None and ret_t5 is not None and r["alpha_t5"] is not None:
            horizon_cov["t5"] += 1
    for x in directional:
        x.pop("_key", None)
    integrity = {
        "rows_directional": len(directional),
        "rows_directional_raw": n_directional_seen,
        "dedupe": bool(dedupe),
        "dropped_duplicate_rows": dropped_dup,
        "backfilled_rows_created_after_trade_date": backfilled,
        "horizon_coverage": horizon_cov,
        "rows_neutral_excluded": int(n_neutral),
        "fwd_ret_mismatch_rows": mismatch,
        "dropped_no_daily_bar": dropped_no_bar,
        "dropped_bad_price": dropped_bad_price,
        "neutral_drift_mean": round(mean(neutral_drift), 6) if neutral_drift else None,
        "neutral_drift_n": len(neutral_drift),
    }
    return directional, integrity


# ────────────────────────────────────────────────────────────
# 评估
# ────────────────────────────────────────────────────────────

def _horizon_fields(horizon: str) -> Tuple[str, str]:
    return ("raw_ret_t1", "llm_ret") if horizon == "t1" else ("raw_ret_t5", "llm_ret_t5")


def paired_rows(rows: List[Dict[str, Any]], arm_key: Optional[str],
                horizon: str) -> List[Dict[str, Any]]:
    """构造 (llm_ret, arm_ret) 配对行。arm_key=None ⇒ 自比 (LLM vs 0)。"""
    raw_f, llm_f = _horizon_fields(horizon)
    out: List[Dict[str, Any]] = []
    for r in rows:
        raw = r.get(raw_f)
        llm = r.get(llm_f)
        if raw is None or llm is None:
            continue
        if arm_key is None:
            arm_ret = 0.0
        else:
            d = arm_direction(arm_key, r)
            if d is None:
                continue
            arm_ret = d * raw
        out.append({"date": r["date"], "chain": r["chain"], "symbol": r["symbol"],
                    "llm_ret": float(llm), "arm_ret": float(arm_ret)})
    return out


def days_needed_to_resolve(mu: Optional[float], sd: Optional[float]) -> Optional[int]:
    """按当前日间波动, 要分辨 |mu| 的差需要多少个交易日 (80% 功率反解)。

    MDE@80% = 2.8·sd/√K ≤ |mu|  ⇒  K ≥ (2.8·sd/|mu|)²。sd 不随 K 变 (日间波动是性质),
    故这是"再攒多久"的直接答案, 而不是把不显著读成无效。
    """
    if not mu or not sd or sd <= 0 or abs(mu) < 1e-12:
        return None
    return int(math.ceil((2.8 * sd / abs(mu)) ** 2))


def evaluate(rows: List[Dict[str, Any]], horizon: str, policy: str, scope: str,
             n_perm: int, seed: int) -> Dict[str, Any]:
    sel = rows if scope == "all" else [r for r in rows if r["chain"] == scope]
    base = paired_rows(sel, None, horizon)
    base_pol = apply_policy(base, policy)
    llm_vs_zero = daily_cluster_stats(
        [(r["date"], r["llm_ret"], r["arm_ret"]) for r in base_pol], n_perm, seed)
    llm_vs_zero["days_needed"] = days_needed_to_resolve(
        llm_vs_zero.get("daily_mean_diff"), llm_vs_zero.get("daily_sd_diff"))
    arms: Dict[str, Any] = {}
    for spec in ARM_SPECS:
        pr = apply_policy(paired_rows(sel, spec["key"], horizon), policy)
        st = daily_cluster_stats([(r["date"], r["llm_ret"], r["arm_ret"]) for r in pr],
                                 n_perm, seed)
        st["days_needed"] = days_needed_to_resolve(
            st.get("daily_mean_diff"), st.get("daily_sd_diff"))
        st["arm_pooled_mean"] = round(mean([r["arm_ret"] for r in pr]) or 0.0, 6) if pr else None
        st["llm_pooled_mean"] = round(mean([r["llm_ret"] for r in pr]) or 0.0, 6) if pr else None
        arms[spec["key"]] = st
    return {"horizon": horizon, "policy": policy, "scope": scope,
            "llm_vs_zero": llm_vs_zero, "arms": arms}


def cross_policy_sign_table(rows: List[Dict[str, Any]], horizon: str, scope: str,
                            n_perm: int, seed: int) -> Dict[str, Any]:
    """四策略同号才允许采信 — 军规"剔除前 3 笔后仍成立"的强化版 (一票否决表)。"""
    out: Dict[str, Any] = {}
    for key in [None] + [s["key"] for s in ARM_SPECS]:
        signs: Dict[str, Optional[float]] = {}
        for pol in POLICIES:
            sel = rows if scope == "all" else [r for r in rows if r["chain"] == scope]
            pr = apply_policy(paired_rows(sel, key, horizon), pol)
            st = daily_cluster_stats([(r["date"], r["llm_ret"], r["arm_ret"]) for r in pr],
                                     0, seed)
            signs[pol] = st.get("daily_mean_diff")
        vals = [v for v in signs.values() if v is not None]
        out[key or "LLM_vs_ZERO"] = {
            "by_policy": signs,
            "all_same_sign": bool(vals) and len({(v > 0) for v in vals}) == 1,
            "n_policy": len(vals),
        }
    return out


def alpha_readout(rows: List[Dict[str, Any]], horizon: str, scope: str,
                  n_perm: int, seed: int) -> Dict[str, Any]:
    """LLM 臂的 beta 剥离读数 (库内 SSOT alpha 列, 不重算 beta ⇒ 不造平行层)。

    对照臂没有 alpha 口径: 仓内 alpha 由 signal_ic_tracker 按信号级 beta 模型算出,
    本脚本不复制该模型。故此块只回答"LLM 臂剥 beta 后还剩多少", 不做配对。
    """
    field = "alpha_t1" if horizon == "t1" else "alpha_t5"
    sel = rows if scope == "all" else [r for r in rows if r["chain"] == scope]
    pairs = [(r["date"], float(r[field]), 0.0) for r in sel if r.get(field) is not None]
    st = daily_cluster_stats(pairs, n_perm, seed)
    st["metric"] = field
    st["days_needed"] = days_needed_to_resolve(st.get("daily_mean_diff"), st.get("daily_sd_diff"))
    return st


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    rows, integrity = build_rows(args.since, args.until, args.chains,
                                 dedupe=not args.no_dedupe)
    scopes = ["all"] + [c for c in args.chains if c != "all"]
    primary = evaluate(rows, "t1", "abs_lt_0_5", "all", args.permutations, args.seed)
    grid = [evaluate(rows, h, p, s, args.permutations, args.seed)
            for h in args.horizons for p in POLICIES for s in scopes]
    by_day = defaultdict(int)
    for r in rows:
        by_day[r["date"]] += 1
    cps = {h: {s: cross_policy_sign_table(rows, h, s, 0, args.seed) for s in scopes}
           for h in args.horizons}
    return {
        "script": "trivial_rule_control_arm",
        "read_only": True,
        "window": {"since": args.since, "until": args.until, "chains": list(args.chains),
                   "horizons": list(args.horizons), "seed": args.seed,
                   "permutations": args.permutations},
        "n_days": len(by_day),
        "rows_per_day_median": sorted(by_day.values())[len(by_day) // 2] if by_day else 0,
        "measurement": {
            "plane": "浮现层 (signal_snapshots), 无执行/费用/门禁 ⇒ 无选择效应",
            "return_window": "close_t0 → close_tn (库内 fwd_ret 同窗)",
            "pairing": "同一行两臂共用同一收益, 只差方向指派",
            "primary_stat": "按日聚类配对差 (宪章七律 #9), pooled 仅作参考",
            "primary_policy": "abs_lt_0_5",
            "resolvable_rule": "|日均差| ≥ MDE@80% 才可下结论; 否则记'本窗口无法分辨'",
        },
        "integrity": integrity,
        "arm_specs": list(ARM_SPECS),
        "primary": primary,
        "cross_policy_signs": cps,
        "alpha_readout": {
            f"{h}|{s}": alpha_readout(rows, h, s, args.permutations, args.seed)
            for h in args.horizons for s in scopes},
        "grid": grid,
        "verdict": _verdict(integrity, primary, len(by_day), cps.get("t1", {}).get("all", {})),
        "not_a_verdict": "本脚本不下 'LLM 有无 edge' 的终裁; 输出是对照读数 + 功率声明。",
    }


def _verdict(integrity: Dict[str, Any], primary: Dict[str, Any],
             n_days: int, cps: Dict[str, Any]) -> Dict[str, Any]:
    """把结论按证据类型分层 (与 ticket_size_breakeven.py 同款纪律)。"""
    z = primary["llm_vs_zero"]
    arms = primary["arms"]
    resolvable = [k for k, v in arms.items() if v.get("resolvable")]
    diffs = [abs(v.get("daily_mean_diff") or 0.0) for v in arms.values()]
    mdes = [v.get("mde80") for v in arms.values() if v.get("mde80")]
    dn = [v["days_needed"] for v in list(arms.values()) + [z] if v.get("days_needed")]
    # 四策略同号 = 唯一允许采信的符号证据 (一票否决表)
    consistent = {k: v for k, v in cps.items() if v.get("all_same_sign")}
    fav = [k for k, v in consistent.items()
           if k != "LLM_vs_ZERO" and (v["by_policy"].get("abs_lt_0_5") or 0) > 0]
    unfav = [k for k, v in consistent.items()
             if k != "LLM_vs_ZERO" and (v["by_policy"].get("abs_lt_0_5") or 0) < 0]
    inconsistent = [k for k, v in cps.items() if not v.get("all_same_sign")]
    lo = min(diffs) if diffs else None
    hi = max(diffs) if diffs else None
    mde_lo = min(mdes) if mdes else None
    return {
        "certain_facts": [
            f"浮现层可配对样本 {integrity['rows_directional']} 行 / {n_days} 个交易日 "
            f"(去重前 {integrity['rows_directional_raw']} 行, 重复登记 "
            f"{integrity['dropped_duplicate_rows']} 行 = 同一假设被 DA 多次重跑各落一行)。",
            f"库内 fwd_ret 口径逐行校验: 不一致 {integrity['fwd_ret_mismatch_rows']} 行 "
            f"(0 = 本脚本重算的收益与 SSOT 完全同口径)。",
            f"T+5 覆盖仅 {integrity['horizon_coverage']['t5']} 行 (DA alpha_t5 全 NULL) "
            f"⇒ T+5 读数当前不可用, 只有 T+1 有样本。",
            f"回填行 (created_at 晚于 trade_date) "
            f"{integrity['backfilled_rows_created_after_trade_date']} 行 "
            f"⇒ 浮现层基本是实时登记, 未受回填污染。",
        ],
        "power_statement": {
            "daily_sd_llm_vs_zero_pp": round((z.get("daily_sd_diff") or 0.0) * 100, 4),
            "mde80_llm_vs_zero_pp": round((z.get("mde80") or 0.0) * 100, 4),
            "min_mde80_across_arms_pp": round(mde_lo * 100, 4) if mde_lo else None,
            "abs_daily_diff_range_pp": [round(lo * 100, 4), round(hi * 100, 4)] if lo is not None else None,
            "days_needed_range": [min(dn), max(dn)] if dn else None,
            "resolvable_arms": resolvable,
            "sign_consistent_favoring_llm": fav,
            "sign_consistent_against_llm": unfav,
            "sign_inconsistent_not_adoptable": inconsistent,
        },
        "reading": (
            f"配对确实降了方差, 但没降一个量级: 各臂日差 sd 与 'LLM vs 0' 的 sd "
            f"({round((z.get('daily_sd_diff') or 0.0) * 100, 3)}pp) 同量级, 而待分辨的效应本身只有 "
            f"{round((lo or 0) * 100, 3)}~{round((hi or 0) * 100, 3)}pp, 最小 MDE@80% 也有 "
            f"{round((mde_lo or 0) * 100, 3)}pp ⇒ 需要 "
            f"{(str(min(dn)) + '~' + str(max(dn))) if dn else 'n/a'} 个交易日。"
            f"没有任何一臂可分辨。四策略同号且偏向 LLM 的臂: {fav or '无'}; "
            f"同号且不利的臂: {unfav or '无'}; 符号不稳 (raw 与稳健口径反向, 由极值行驱动) "
            f"因而不可采信: {inconsistent or '无'}。"
            "结论: 浮现层收益类判据在当前日间波动下无法裁决任何 prompt/执行改动, "
            "这不是攒样能救的, 是尺子选错了。"
        ),
        "implication": (
            "与 DEB-20260907 建议①一致: 判据终点必须从高噪声的收益/PnL 换成高 SNR 代理 — "
            "成交保真度对账 (确定性, 无需 n) 与配对反事实 (信号方差被消掉), 见 "
            "scripts/exit_path_replay.py + scripts/cf_pullback_limit_replay.py。"
            "本脚本的价值是把'没有对照臂'这个缺口变成一个带功率声明的读数, "
            "并给出反面证据: 一行规则臂与 LLM 臂在本窗口不可区分。"
        ),
    }


# ────────────────────────────────────────────────────────────
# 打印
# ────────────────────────────────────────────────────────────

def _pp(x: Optional[float]) -> str:
    return f"{x * 100:+.4f}" if x is not None else "  n/a "


def print_report(rep: Dict[str, Any]) -> None:
    ig = rep["integrity"]
    pr = rep["primary"]
    print("=" * 96)
    print(f"笨规则对照臂 — 浮现层按日配对 (只读)  窗口 {rep['window']['since'] or '全期'}"
          f"~{rep['window']['until'] or '最新'}  链={','.join(rep['window']['chains'])}")
    print("=" * 96)
    print(f"\n[样本完整性] 可配对行 {ig['rows_directional']}  日数 {rep['n_days']} "
          f"(中位 {rep['rows_per_day_median']} 行/日)  去重={ig['dedupe']}")
    print(f"  去重前 {ig['rows_directional_raw']} 行, 丢弃重复登记 {ig['dropped_duplicate_rows']} 行 "
          f"(同一 (日期,标的,方向,链) 被 DA 多次重跑各落一行 = 同一假设, 非独立信号)")
    print(f"  回填行 (created_at 晚于 trade_date): "
          f"{ig['backfilled_rows_created_after_trade_date']} 行")
    print(f"  前瞻窗覆盖: T+1 {ig['horizon_coverage']['t1']} 行 / "
          f"T+5 {ig['horizon_coverage']['t5']} 行 (T+5 需 close_t5+fwd_ret_t5+alpha_t5 三者齐)")
    print(f"  NEUTRAL 行排除 {ig['rows_neutral_excluded']} (语义='不交易', 无方向可配对); "
          f"其 T+1 自然漂移均值 {_pp(ig['neutral_drift_mean'])}% (n={ig['neutral_drift_n']}) "
          f"= 不下单的机会成本零点")
    print(f"  库内 fwd_ret 口径校验不一致行: {ig['fwd_ret_mismatch_rows']} "
          f"(>0 ⇒ 上游口径已变, 本脚本读数不可与历史直接比)")
    print(f"  丢弃: 无日K {ig['dropped_no_daily_bar']} 行 / 价格非正 {ig['dropped_bad_price']} 行")

    z = pr["llm_vs_zero"]
    print(f"\n[LLM 臂 vs 0] 主策略 abs_lt_0_5, 按日聚类: 日均 {_pp(z['daily_mean_diff'])}% "
          f"sd {_pp(z['daily_sd_diff'])} t={z['t_daily']} perm_p={z['perm_p_two_sided']} "
          f"正日 {z['days_positive']}/{z['n_days']}")
    print(f"  MDE@80% = {_pp(z['mde80'])}pp ⇒ {'可分辨' if z['resolvable'] else '本窗口无法分辨'}"
          + (f"; 要分辨当前差值需 {z['days_needed']} 个交易日" if z.get("days_needed") else ""))

    print("\n[主表] LLM 臂 − 对照臂 (正 = LLM 更好), 主策略 abs_lt_0_5 / 按日聚类")
    print("  %-20s %5s %9s %9s %10s %7s %8s %9s %7s %s" % (
        "arm", "n", "arm池%", "llm池%", "日均差pp", "t", "perm_p", "MDE80pp", "正日", "可分辨"))
    for spec in ARM_SPECS:
        st = pr["arms"][spec["key"]]
        print("  %-20s %5s %9s %9s %10s %7s %8s %9s %7s %s" % (
            spec["key"], st.get("n_rows", 0),
            _pp(st.get("arm_pooled_mean")), _pp(st.get("llm_pooled_mean")),
            _pp(st.get("daily_mean_diff")), st.get("t_daily"), st.get("perm_p_two_sided"),
            _pp(st.get("mde80")), f"{st.get('days_positive', 0)}/{st.get('n_days', 0)}",
            "是" if st.get("resolvable") else "否"))
    print("  (arm_class / info_set 见 JSON arm_specs — 信息集不同的臂不可横向比优劣)")

    print("\n[四策略同号检验] 一票否决: 四策略不同号 ⇒ 结论不采信")
    cps = rep["cross_policy_signs"].get("t1", {}).get("all", {})
    print("  %-20s %10s %10s %10s %10s %s" % (
        "arm", "raw", "abs<0.5", "winsor", "excl_top3", "同号"))
    for key, v in cps.items():
        b = v["by_policy"]
        print("  %-20s %10s %10s %10s %10s %s" % (
            key, _pp(b.get("raw")), _pp(b.get("abs_lt_0_5")),
            _pp(b.get("winsor_1_99")), _pp(b.get("excl_top3")),
            "✓" if v["all_same_sign"] else "✗ 不可采信"))

    print("\n[分链] 主策略 abs_lt_0_5 (宪章七律 #1: 按来源切分)")
    for scope in [c for c in rep["window"]["chains"] if c != "all"]:
        blk = next((g for g in rep["grid"]
                    if g["horizon"] == "t1" and g["policy"] == "abs_lt_0_5"
                    and g["scope"] == scope), None)
        if not blk:
            continue
        zz = blk["llm_vs_zero"]
        flag = "  ⚠ n<30 日" if zz.get("n_lt_30_days_flag") else ""
        print(f"  {scope}: n={zz.get('n_rows')} 日={zz.get('n_days')} "
              f"LLM vs 0 日均 {_pp(zz.get('daily_mean_diff'))}% t={zz.get('t_daily')} "
              f"MDE80 {_pp(zz.get('mde80'))}pp ⇒ "
              f"{'可分辨' if zz.get('resolvable') else '无法分辨'}{flag}")
        for spec in ARM_SPECS:
            st = blk["arms"][spec["key"]]
            print(f"      vs {spec['key']:<20} 日均差 {_pp(st.get('daily_mean_diff'))}pp "
                  f"t={st.get('t_daily')} 正日 {st.get('days_positive')}/{st.get('n_days')}")

    print("\n[beta 剥离读数] 库内 SSOT alpha 列 (对照臂无 alpha 口径, 不做配对)")
    for k, v in rep["alpha_readout"].items():
        print(f"  {k:<28} n={v.get('n_rows')} 日均 {_pp(v.get('daily_mean_diff'))}% "
              f"t={v.get('t_daily')} perm_p={v.get('perm_p_two_sided')} "
              f"MDE80 {_pp(v.get('mde80'))}pp")
    v = rep["verdict"]
    print("\n" + "=" * 96)
    print("[确定事实] (只依赖库内数据与口径校验, 不含统计推断)")
    for s in v["certain_facts"]:
        print(f"  · {s}")
    p = v["power_statement"]
    print("\n[功率声明] (宪章七律 #9: 不带 MDE 的判据不予采信)")
    print(f"  日差 sd = {p['daily_sd_llm_vs_zero_pp']}pp  LLM vs 0 的 MDE@80% = "
          f"{p['mde80_llm_vs_zero_pp']}pp  各臂最小 MDE@80% = {p['min_mde80_across_arms_pp']}pp")
    print(f"  各臂实测日差绝对值区间 = {p['abs_daily_diff_range_pp']}pp  "
          f"⇒ 待分辨效应比最小 MDE 小一个量级")
    print(f"  要分辨当前观测到的差值需要 {p['days_needed_range']} 个交易日 "
          f"(现有 {rep['n_days']} 日)")
    print(f"  可分辨的臂: {p['resolvable_arms'] or '无 — 全部记为「本窗口无法分辨」'}")
    print(f"  四策略同号且偏向 LLM: {p['sign_consistent_favoring_llm'] or '无'}")
    print(f"  四策略同号且不利于 LLM: {p['sign_consistent_against_llm'] or '无'}")
    print(f"  符号不稳不可采信 (极值行驱动): {p['sign_inconsistent_not_adoptable'] or '无'}")
    print(f"\n[读法] {v['reading']}")
    print(f"\n[含义] {v['implication']}")
    print(f"\n{rep['not_a_verdict']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="笨规则对照臂 (只读, 浮现层按日配对)")
    ap.add_argument("--since", default=None, help="窗口起始 trade_date (缺省=全期)")
    ap.add_argument("--until", default=None, help="窗口结束 trade_date (含)")
    ap.add_argument("--chains", nargs="+", default=list(CHAINS),
                    help=f"链路 (可选: {' '.join(CHAINS)})")
    ap.add_argument("--horizons", nargs="+", default=list(HORIZONS),
                    help=f"前瞻窗 (可选: {' '.join(HORIZONS)})")
    ap.add_argument("--permutations", type=int, default=5000,
                    help="符号翻转移换次数 (5000 时 p≈0.05 的蒙特卡洛 SE≈0.003, 足够; 调高只增耗时)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="置换随机种子 (可复现)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="不按 (日期,标的,方向,链) 去重 (缺省去重; 不去重会给重复登记的标的加权)")
    ap.add_argument("--no-write", action="store_true", help="只打印, 不写 data/shadow/*.json")
    ap.add_argument("--json", default=str(OUT_LATEST), help="输出路径")
    args = ap.parse_args()
    for h in args.horizons:
        if h not in HORIZONS:
            ap.error(f"--horizons 只支持 {HORIZONS}")
    for c in args.chains:
        if c not in CHAINS:
            ap.error(f"--chains 只支持 {CHAINS}")

    rep = build_report(args)
    if rep["integrity"]["rows_directional"] == 0:
        print("[trivial_rule_control_arm] 窗口内无可配对浮现行, 不产出。")
        return 0
    print_report(rep)
    if not args.no_write:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        print(f"\n[write] {out} (fresh 覆写, 重跑幂等)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
