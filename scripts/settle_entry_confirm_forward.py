#!/usr/bin/env python3
"""RFC-141 入场延迟确认门结算器 — 裁决源 = SIM 真实成交盈亏 (富途 app 可见那条)。

只读: data/shadow/entry_confirm_forward.jsonl (机制留痕) + trade_records (mode=ro)。
写: data/shadow/entry_confirm_forward_latest.json (原子写)。

═══ 2026-10-10 口径变更 (用户裁) ═══
用户原话: 「富途 app 上目前的链路交易, 盈利和亏损是可见的, 我的本意是直接走 sim
交易链路, 不用影子攒样看 sim 交易结果」
⇒ 裁决源从「影子产物配对净差」改为「开门后 SIM 真实盈亏 vs 开门前预注册基线」。
  jsonl 降级为**机制留痕**: 只回答「门真在拦吗 / 拦的是反转单吗 / 机会成本多少」,
  不再独立裁决。holdout_pct 已归零 ⇒ 无同期对照臂, 代价=市况混淆 (RFC §5.4 披露)。

判据 (RFC-141 §4, 预注册, 读数前写死; 基线 2026-10-10 冻结):
  1 病灶族收敛: 开门后 SL_INITIAL 族 ≥ −$40/笔 (v2 基线 −$162.3363, −$40 取严), 或该族 n≤1
  2 开盘窗改善: 09:35–09:45 每笔净均 > −$2.2632 且日聚类 t > −0.3218
  3 机制量达标: 避免率 ≥30% ∧ CONFIRMED opp_cost 均值 <0.3%
  4 不伤赢家:   非 SL_INITIAL 族全量每笔净均 ≥ +$7.79 (= v2 基线 +$9.7353 的 80%);
                裁决口径为剔费每笔净均 ≥ +$8.79 (= v2 基线 +$10.9854 的 80%)
  样本门: n≥60 ∨ ≥10 交易日 (先到为准); 未达 = INSUFFICIENT (不是 FAIL, 也不许当 PASS)
  分层 R1(SHORT×开盘窗)/LONG×开盘窗: 只印不判。

开门日不硬编码: 取 forward 产物首行 date (门真 arm 过的那天) ⇒ 重启延后不会让
基线窗口错位, 也不会把开门前的成交混进「开门后」人群。
"""
from __future__ import annotations
import datetime
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
FWD = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
OUT = ROOT / "data" / "shadow" / "entry_confirm_forward_latest.json"
DB = ROOT / "data" / "ftseek_us.db"

# ── 预注册基线 (RFC-141 §4, 裁决日禁挪) ──
# 口径: source_type=PLAYBOOK_SIM ∧ exit_price>0 ∧ COALESCE(pnl_net,realized_pnl) NOT NULL
#      ∧ trade_date ∈ [BASELINE_SINCE, BASELINE_UNTIL)
#
# 📌 v2 窗口变更 (2026-10-10 22:37, 用户裁「从8月份开始统计, 早期的策略和逻辑不够成熟」):
#   v1 = [2026-06-24, 2026-10-10) n=632  →  v2 = [2026-08-01, 2026-10-10) n=441
#   **不构成挪门柱**: 门 21:36 部署后 forward 产物文件从未生成 (0 行)、verdict=TOO_EARLY、
#   playbook_instructions 含 entry_confirm 旗标 0 条 ⇒ **零读数**。挪门柱的定义是拿已结算
#   案例反推阈值; 零读数下按用户口径修正窗口是修口径错误, 且判据④ 方向是**收紧**
#   (地板 +2.79→+7.79 / 剔费 +3.47→+8.79, 对门更不利)。
#   生成方式: `scripts/deb20261009_rfc141_baseline_gen.py --since 2026-08-01`
#   (用本文件自己的 _pnl_block/_day_clustered_t 算 ⇒ 生成尺子=裁决尺子, 非手抄)
# ⚠️ t 一律用 statistics.stdev (样本标准差 ddof=1), 与 _day_clustered_t 同尺 ——
#   v1 首版误用 pstdev ⇒ t 偏小 (sl_initial −10.6924 应为 −10.4217), 已统一。
BASELINE_SINCE = "2026-08-01"    # v2: 用户裁「早期策略不成熟」, 8 月起统计
BASELINE_UNTIL = "2026-10-10"    # 开门前 (右开界)
BASELINE = {
    "all": {"n": 441, "net_sum": -352.67, "net_mean": -0.7997,
            "days": 49, "t": -0.1499, "win_rate": 0.6644},
    "open_win": {"n": 214, "net_sum": -484.33, "net_mean": -2.2632,
                 "days": 44, "t": -0.3849, "win_rate": 0.6682},
    # 门靶向病灶族: 27 笔亏 4383.08 = 全样本净亏的 12.4283×, 胜率 0/27
    # (v1 是 1.8482×/29 笔 ⇒ v2 窗口下病灶集中度大幅上升, 更支持门靶向正确)
    "sl_initial": {"n": 27, "net_sum": -4383.08, "net_mean": -162.3363,
                   "days": 18, "t": -10.2418, "win_rate": 0.0},
    # 剔除 SL_INITIAL 后的正贡献面 (判据 4 的基准)
    # v2: +9.7353/笔 t=+2.4673 (显著正) — 系统其余部分是赚钱的, 亏损全在 SL_INITIAL 族
    "rest": {"n": 414, "net_sum": 4030.41, "net_mean": 9.7353,
             "days": 49, "t": 2.4673, "win_rate": 0.7077},
    # 判据④ 的两端同尺基准 (RFC-141 §5.6): rest 剔 fee_bps>100 的 2 笔
    # (CTNT 915.1 / GIPR 203.6, 均 9/17) 后的干净面。裁决以此为准。
    "rest_clean": {"n": 412, "net_sum": 4526.00, "net_mean": 10.9854,
                   "fee_bps_anomaly_dropped": 2, "fee_bps_threshold": 100.0},
}
# 判据② 的 t 门槛**单独钉住 v1 值** (取严不放松):
#   v2 基线 open_win.t = −0.3849, 比 v1 的 −0.3218 更负 ⇒ 若跟着基线走, t 门槛会**放松**
#   (更容易通过)。净均门槛随 v2 收紧 (−4.1115 → −2.2632), 但 t 门槛按挪门柱禁令
#   「改窗口只准收紧不准放松」保持 v1 严值。两侧取严 ⇒ 判据② 整体只紧不松。
OPEN_WIN_T_FLOOR = -0.3218       # v1 值, 刻意不随 v2 基线放松
# 判据阈值 (RFC-141 §4 逐字)
MIN_SIM_N = 60                 # 开门后 SIM 成交笔数
MIN_SIM_DAYS = 10              # 或交易日数 (先到为准)
SL_INIT_MAX_LOSS_PER_TRADE = -40.0   # 判据1: v2 25%线 −40.58; −40 取严
SL_INIT_MAX_N = 1              # 判据1 或: 族 n ≤ 1
MIN_AVOID = 0.30               # 判据3
MAX_OPP = 0.3                  # 判据3
# WINNER_MEAN_FLOOR / _CLEAN 由 winner_floor() 从 BASELINE 派生 (见下方), 单一真源
# 判据4 两端同尺 (RFC-141 §5.6 / DEB §33.3): v2 基线 (8/1–10/9) rest +$9.7353 含 2 笔
# fee_bps>100 的费用异常单 (CTNT 915.1 / GIPR 203.6, 均 9/17), 剔后 +$10.9854 ⇒ 剔费地板
# +$8.79 (全量地板被低估 +$1.00/笔 = 12.8%)。裁决以**剔后地板**为准。
# v1 留档 (窗口 6/24–10/9): rest +$3.4852 含 3 笔 (多 NVDA 198.6), 剔后 +$4.3340
# ⇒ 全量地板 +$2.79 / 剔费地板 +$3.47。v2 使门槛抬高 2.8× (对门更严 = 收紧方向)。
# 阈值 100 与 §27 缺陷3 同 (915/204 与第三名断层不明显, 故取 §30 已实装的
# W8_fee_bps_anomaly 同一 100bps 口径, 不另造阈值 = 防阈值购物)。
FEE_BPS_ANOMALY = 100.0
OPEN_WIN_START, OPEN_WIN_END = "09:35", "09:45"


def winner_floor(clean: bool = False) -> float:
    """判据④ 地板 = 基准净均 × 80% (从 BASELINE 派生, 单一真源不重复硬编码)。

    ⚠️ round 到 **2 位** = 与 RFC-141 §4 预注册文本逐字一致 (v2: +$7.79 / 剔费 +$8.79)。
    4 位精确值是 7.7882/8.7883, 若直接用 4 位 ⇒ 地板低于预注册文本 =
    **放松门槛方向**, 属挪门柱 (2026-10-10 v1 自查抓出同一问题); 预注册门槛值不得因
    「派生更精确」而漂移, 精度对齐到注册文本。

    clean=False → 全量地板 (基准含费用异常单, 与开门后全量样本同尺)
    clean=True  → 剔费地板 (基准与开门后剔费样本同尺) ⇒ **裁决以此为准** (§5.6)
    """
    key = "rest_clean" if clean else "rest"
    return round(BASELINE[key]["net_mean"] * 0.8, 2)


# 地板常量: 由 winner_floor 从 BASELINE 派生 (单一真源)。两端同尺的理由与 v1/v2 数值
# 见上方 FEE_BPS_ANOMALY 注释块 + RFC-141 §4 判据④ + §5.6, 此处不重复 (防两处注释漂移)。
WINNER_MEAN_FLOOR = winner_floor(False)        # v2: 7.79 (4位 7.7882)
WINNER_MEAN_FLOOR_CLEAN = winner_floor(True)   # v2: 8.79 (4位 8.7883)


def _day_clustered_t(daily_values):
    """宪章第9律: 按日等权取均值再单样本 t; n_days<3 或零方差 → None (不可判 ≠ 0)。"""
    n_days = len(daily_values)
    if n_days < 3:
        return None
    day_mean = statistics.mean(daily_values)
    sd = statistics.stdev(daily_values)
    if sd == 0:
        return None
    return round(day_mean / (sd / (n_days ** 0.5)), 4)


def load_rows():
    if not FWD.exists():
        return []
    rows, bad = [], 0
    for line in open(FWD, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception as _e:
            # fail-loud: 结算器最坏的失效是「坏行静默跳过」与「没有坏行」输出同形 ⇒
            # 集体假绿 (except:pass 棘轮纪律, DEB-20260914 §十)。计数并进汇总披露。
            bad += 1
            print(f"  ⚠️ 坏行跳过 ({_e}): {line[:120]}", file=sys.stderr)
            continue
        if r.get("schema") == "entry_confirm_forward_v1":
            rows.append(r)
        else:
            bad += 1
            print(f"  ⚠️ schema 不符跳过: {str(r.get('schema'))[:60]}", file=sys.stderr)
    if bad:
        print(f"  ⚠️ 共跳过 {bad} 行 (坏行/schema 不符) — 判据人群已缩小, 见 stderr",
              file=sys.stderr)
    load_rows.bad_lines = bad
    return rows


def load_sim_pnl(since_date: str):
    """开门后 SIM 真实成交盈亏 — 裁决源 (富途 app 可见的那条链路)。

    WHERE 逐字照抄基线口径 (NULL 下等价重写不成立, memory 纪律):
      source_type='PLAYBOOK_SIM' AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL
    fee_bps 相关列一并带出 (判据④ 两端同尺需要, 见 §5.6 / DEB §33.3)。
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute(
            "SELECT symbol, trade_date, entry_time, exit_reason, fees_est, "
            "       quantity AS qty, entry_price, "
            "       COALESCE(pnl_net, realized_pnl) AS net "
            "FROM trade_records "
            "WHERE source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0) > 0 "
            "  AND COALESCE(pnl_net, realized_pnl) IS NOT NULL "
            "  AND trade_date >= ?", (since_date,)):
        out.append(dict(r))
    con.close()
    return out


def fee_bps(r) -> Optional[float]:
    """fee_bps = fees_est / (entry_price × quantity) × 10000 — **事前可算** (非事后标签)。

    为什么需要: DEB-20261009 §27 缺陷3 实测少数仙股/最小佣金单的 fee_bps 可达
    915bps (CTNT)/204bps (GIPR), 单笔即可决定样本净符号。RFC-141 判据④ 的基准
    (非 SL_INITIAL 族净均 +$3.4852) 含 3 笔 fee_bps>100 ⇒ 若开门后样本不含同类单,
    比较就变成「干净值 vs 被 3 笔拖低的基准」= 判据偏松, 门可能白过。
    处置 (§5.6): 两端同尺 — 同时输出全量地板与剔费用地板, 裁决以剔后为准。
    不可算 (缺 fees_est/qty/px) ⇒ None, 调用方按「保留」处理 (不因数据缺失误剔)。
    """
    fee, qty, px = r.get("fees_est"), r.get("qty"), r.get("entry_price")
    if fee is None or not qty or not px:
        return None
    try:
        return float(fee) / (float(qty) * float(px)) * 10000
    except Exception:
        return None


def is_fee_anomaly(r) -> bool:
    """fee_bps > FEE_BPS_ANOMALY 视为费用异常单 (两端同尺的剔除判据)。"""
    b = fee_bps(r)
    return b is not None and b > FEE_BPS_ANOMALY


def _in_open_win(entry_time) -> bool:
    if not entry_time or len(str(entry_time)) < 16:
        return False
    return OPEN_WIN_START <= str(entry_time)[11:16] < OPEN_WIN_END


def _pnl_block(sim_rows, pred=None):
    """一组 SIM 成交的读数 (净均/日聚类 t/胜率)。pred=None 取全部。"""
    sel = [r for r in sim_rows if (pred(r) if pred else True)]
    nets = [float(r["net"]) for r in sel]
    if not nets:
        return {"n": 0, "net_sum": None, "net_mean": None, "days": 0,
                "t": None, "win_rate": None}
    by_day = defaultdict(list)
    for r in sel:
        by_day[r["trade_date"]].append(float(r["net"]))
    return {
        "n": len(nets),
        "net_sum": round(sum(nets), 2),
        "net_mean": round(statistics.mean(nets), 4),
        "days": len(by_day),
        "t": _day_clustered_t([statistics.mean(v) for v in by_day.values()]),
        "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 4),
    }


def _agg(rows, fills):
    """一组 forward 行的机制留痕聚合 (契约对齐影子 _agg_block)。"""
    ok = [r for r in rows if r.get("status") == "ok"]
    ab = [r for r in ok if not r.get("still_triggers")]
    cf = [r for r in ok if r.get("still_triggers")]
    opp = [float(r.get("opp_cost_pct") or 0) for r in cf if r.get("opp_cost_pct") is not None]

    def _join_net(r):
        v = fills.get((r.get("symbol"), r.get("date")))
        return (sum(v) / len(v)) if v else None

    treat_fills = [x for x in (_join_net(r) for r in cf) if x is not None]
    by_day = defaultdict(list)
    for r in cf:
        x = _join_net(r)
        if x is not None:
            by_day[r.get("date")].append(x)
    day_t = _day_clustered_t([statistics.mean(v) for v in by_day.values()] if by_day else [])

    return {
        "n": len(ok),
        "n_abandoned": len(ab),
        "avoidance_rate": round(len(ab) / len(ok), 4) if ok else None,
        "n_confirmed": len(cf),
        "opp_cost_mean_pct": round(statistics.mean(opp), 4) if opp else None,
        "opp_cost_median_pct": round(statistics.median(opp), 4) if opp else None,
        "confirmed_fill_net_mean": round(statistics.mean(treat_fills), 2) if treat_fills else None,
        "n_confirmed_joined": len(treat_fills),
        "day_clustered_t_confirmed_fills": day_t,
    }


def load_fills(since_date: str):
    """(symbol, date) → [pnl_net,...] 开门后成交 (供机制留痕 join, 不作裁决)。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fills = defaultdict(list)
    for r in con.execute(
            "SELECT symbol, trade_date, COALESCE(pnl_net, realized_pnl) AS net "
            "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 "
            "AND trade_date>=?", (since_date,)):
        fills[(r["symbol"], r["trade_date"])].append(float(r["net"] or 0))
    con.close()
    return fills


def main():
    rows = load_rows()

    # 开门日 = forward 产物首行 date (门真 arm 过那天); 无产物 ⇒ 门尚未上线
    gate_live_date = min((str(r.get("date")) for r in rows if r.get("date")), default=None)
    if not gate_live_date:
        print("RFC-141: 无 forward 产物 ⇒ 门尚未上线 (TOO_EARLY)。"
              " 首次 arm 后本结算器才有裁决窗口。")
        out = {"generated_at": datetime.datetime.now().astimezone().isoformat(),
               "rfc": "RFC-141", "verdict": "TOO_EARLY", "gate_live_date": None,
               "reason": "无 forward 产物 (门未 arm 过任何指令)",
               "caliber": "裁决源=开门后 SIM 真实盈亏 vs 预注册基线 (RFC-141 §4)"}
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT)
        return 0

    sim = load_sim_pnl(gate_live_date)
    fills = load_fills(gate_live_date)

    # ── 裁决源: 开门后 SIM 真实盈亏, 分四个人群 ──
    p_all = _pnl_block(sim)
    p_sl = _pnl_block(sim, lambda r: (r.get("exit_reason") or "") == "SL_INITIAL")
    p_rest = _pnl_block(sim, lambda r: (r.get("exit_reason") or "") != "SL_INITIAL")
    p_win = _pnl_block(sim, lambda r: _in_open_win(r.get("entry_time")))

    # ── 机制留痕 (不裁决) ──
    a_all = _agg(rows, fills)
    treat = [r for r in rows if r.get("arm") == "treatment"]
    hold = [r for r in rows if r.get("arm") == "holdout"]
    a_t = _agg(treat, fills)

    # ── 判据 1–4 (RFC-141 §4) ──
    sl_mean = p_sl["net_mean"]
    c1 = (p_sl["n"] <= SL_INIT_MAX_N) or (
        sl_mean is not None and sl_mean >= SL_INIT_MAX_LOSS_PER_TRADE)
    c1_why = (f"SL_INITIAL 族 n={p_sl['n']} ≤{SL_INIT_MAX_N} (病灶近乎消失)"
              if p_sl["n"] <= SL_INIT_MAX_N else
              f"SL_INITIAL 族净均 {sl_mean} ≥ {SL_INIT_MAX_LOSS_PER_TRADE} "
              f"(基线 {BASELINE['sl_initial']['net_mean']})")

    # 判据② 的 t 门槛取**两侧更严**者: v2 基线 t=−0.3849 比 v1 的 −0.3218 更负,
    # 直接跟基线走会**放松**门槛 (挪门柱禁令: 改窗口只准收紧) ⇒ max() 取严。
    # 净均门槛随 v2 收紧 (−4.1115 → −2.2632), 故判据② 整体只紧不松。
    _ow_t_floor = max(BASELINE["open_win"]["t"], OPEN_WIN_T_FLOOR)
    c2 = (p_win["net_mean"] is not None
          and p_win["net_mean"] > BASELINE["open_win"]["net_mean"]
          and p_win["t"] is not None and p_win["t"] > _ow_t_floor)
    c3 = (a_t["avoidance_rate"] is not None and a_t["avoidance_rate"] >= MIN_AVOID
          and a_t["opp_cost_mean_pct"] is not None and a_t["opp_cost_mean_pct"] < MAX_OPP)
    # 判据④ 两端同尺 (RFC-141 §5.6 / DEB §33.3): 基线含 fee_bps>100 的费用异常单
    # (v2 窗口 2 笔: CTNT 915.1 / GIPR 203.6), 若开门后样本不含同类单而用全量地板比
    # ⇒ 判据偏松, 门可能白过。故:
    #   · 裁决口径 (c4) = 剔费样本 vs 剔费地板 (WINNER_MEAN_FLOOR_CLEAN, 两端同尺)
    #   · 并陈口径 (c4_full) = 全量样本 vs 全量地板 (供对照, 不单独裁决)
    p_rest_clean = _pnl_block(
        sim, lambda r: (r.get("exit_reason") or "") != "SL_INITIAL" and not is_fee_anomaly(r))
    c4 = (p_rest_clean["net_mean"] is not None
          and p_rest_clean["net_mean"] >= WINNER_MEAN_FLOOR_CLEAN)
    c4_full = (p_rest["net_mean"] is not None
               and p_rest["net_mean"] >= WINNER_MEAN_FLOOR)

    sample_ok = (p_all["n"] >= MIN_SIM_N) or (p_all["days"] >= MIN_SIM_DAYS)

    if not sample_ok:
        verdict = "INSUFFICIENT"
        verdict_why = (f"样本未达门: n={p_all['n']}<{MIN_SIM_N} 且 "
                       f"交易日={p_all['days']}<{MIN_SIM_DAYS} — 不作 PASS 也不作 FAIL")
    elif c1 and c2 and c3 and c4:
        verdict = "PASS"
        verdict_why = "判据 1∧2∧3∧4 全过 → 呈用户裁 SIM 常态化 (REAL 另须签字)"
    elif c1 and not (c2 and c4):
        verdict = "OBSERVE"
        verdict_why = ("判据1 过 (病灶对了) 但 2 或 4 不过 (总量没转好) → 不行动, "
                       "轴维持 OBSERVING")
    else:
        verdict = "FAIL"
        verdict_why = (f"判据1 明确不过 (SL_INITIAL 族强度未收敛: n={p_sl['n']} "
                       f"净均={sl_mean}) → 轴 KILLED, enabled=false, 产物留档")

    # R1 分层只印不判
    r1 = [r for r in treat if r.get("direction") == "SHORT"
          and r.get("session_min") is not None and 0 <= r["session_min"] < 15]
    r1_long = [r for r in treat if r.get("direction") == "LONG"
               and r.get("session_min") is not None and 0 <= r["session_min"] < 15]

    out = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "rfc": "RFC-141",
        "gate_live_date": gate_live_date,
        "verdict": verdict,
        "verdict_why": verdict_why,
        "adjudication_source": {
            "kind": "SIM 真实成交盈亏 (富途 app 可见链路)",
            "table": "trade_records",
            "where": ("source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0)>0 AND "
                      "COALESCE(pnl_net, realized_pnl) IS NOT NULL AND trade_date>='"
                      + gate_live_date + "'"),
            "all": p_all, "sl_initial": p_sl, "non_sl_initial": p_rest,
            "open_window_0935_0945": p_win,
        },
        "baseline_frozen_20261010": BASELINE,
        "criteria": {
            "min_sim_n": MIN_SIM_N, "min_sim_days": MIN_SIM_DAYS,
            "c1_sl_initial_converged": c1, "c1_why": c1_why,
            "c1_floor_per_trade": SL_INIT_MAX_LOSS_PER_TRADE,
            "c1_base_mean": BASELINE["sl_initial"]["net_mean"],
            "c2_open_window_improved": c2,
            "c2_base_mean": BASELINE["open_win"]["net_mean"],
            "c2_base_t": BASELINE["open_win"]["t"],
            "c3_mechanism_ok": c3, "min_avoidance": MIN_AVOID, "max_opp_cost_pct": MAX_OPP,
            # 判据④ 两端同尺 (RFC-141 §5.6 / DEB §33.3): c4=裁决口径 (剔费样本 vs
            # 剔费地板), c4_full=并陈对照 (全量样本 vs 全量地板)。基线含 3 笔
            # fee_bps>100 ⇒ 全量地板被低估 24.4%, 只比全量会让门白过。
            "c4_winners_not_hurt": c4,
            "c4_floor_clean": WINNER_MEAN_FLOOR_CLEAN,
            "c4_base_mean_clean": BASELINE["rest_clean"]["net_mean"],
            "c4_full_caliber_also_printed": c4_full,
            "c4_floor_full": WINNER_MEAN_FLOOR,
            "c4_base_mean_full": BASELINE["rest"]["net_mean"],
            "c4_fee_bps_anomaly_threshold": FEE_BPS_ANOMALY,
            "c4_caliber": ("裁决用剔费口径 (两端同尺); 全量口径只并陈不裁决"),
            "sample_ok": sample_ok,
        },
        "mechanism_trace": {
            "n_rows": len(rows), "all": a_all, "treatment": a_t,
            "holdout": _agg(hold, fills),
            "note": ("jsonl 降级为机制留痕 (2026-10-10 用户裁): 只证明门真在拦 + 拦的是"
                     "反转单 + 机会成本; 不独立裁决"),
        },
        "strata_print_only": {
            "R1_short_open_window": _agg(r1, fills),
            "long_open_window": _agg(r1_long, fills),
        },
        "caliber": ("裁决源=开门后 SIM 真实盈亏 vs 开门前预注册基线; holdout_pct=0 ⇒ "
                    "无同期对照臂, 受市况变化混淆 (披露 RFC §5.4); 判据1/3 为门自身机制量"
                    "不随市况漂, 故列为最强判据。开门日取 forward 首行 date (不硬编码, "
                    "重启延后不错位)。"),
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT)

    print(f"RFC-141 结算: 开门日={gate_live_date} rows={len(rows)} verdict={verdict}")
    print(f"  {verdict_why}")
    print(f"  SIM 真实盈亏 (裁决源):")
    print(f"    ALL        n={p_all['n']} 净均={p_all['net_mean']} t={p_all['t']} "
          f"(基线 {BASELINE['all']['net_mean']} / t={BASELINE['all']['t']})")
    print(f"    SL_INITIAL n={p_sl['n']} 净均={p_sl['net_mean']} "
          f"(基线 {BASELINE['sl_initial']['net_mean']}, 判据线 ≥{SL_INIT_MAX_LOSS_PER_TRADE})")
    print(f"    非SL_INIT  n={p_rest['n']} 净均={p_rest['net_mean']} (全量地板 ≥{WINNER_MEAN_FLOOR})")
    print(f"    非SL剔费   n={p_rest_clean['n']} 净均={p_rest_clean['net_mean']} "
          f"(剔费地板 ≥{WINNER_MEAN_FLOOR_CLEAN} = 裁决口径, 两端同尺)")
    print(f"    开盘窗     n={p_win['n']} 净均={p_win['net_mean']} t={p_win['t']} "
          f"(基线 {BASELINE['open_win']['net_mean']} / t={BASELINE['open_win']['t']})")
    print(f"  机制留痕: 避免率={a_t['avoidance_rate']} opp={a_t['opp_cost_mean_pct']}% "
          f"(n={a_t['n']})")
    print(f"  判据: 1={c1} 2={c2} 3={c3} 4={c4}(剔费裁决) 4_full={c4_full}(并陈) 样本门={sample_ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
