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

═══ 2026-10-15 v2 判尺修订 (RFC-141 §5.10, 用户裁「直接优化落地」) ═══
判尺改 mid-mid + grace 2-of-3 持续性 ⇒ 两代判据不同尺, 两源不混算 (§5.9 同纪律):
  · 裁决窗 = v2 首行 date 起 (判据①②③④ 阈值逐字不变, 样本门自 v2 窗重计);
  · v1 窗 [v1 开门日, v2 开门日) 单出 legacy_v1_window 块, 留痕不裁决;
  · 判据③ 机制量只用现行代行; type=entry_confirm_post (弃单后观测) 不进 _agg。
"""
from __future__ import annotations
import datetime
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
FWD = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
OUT = ROOT / "data" / "shadow" / "entry_confirm_forward_latest.json"
DB = ROOT / "data" / "ftseek_us.db"

# ── RFC-153 FADE_SHORT 开盘快跌禁追门 进度读数 (只印不判) ──
#   窗口自门生效首日 (2026-10-21 ET) 起; 人群 = FADE_SHORT ∧ 开盘 0–window 分钟。
#   样本门/主判据见 RFC-153 §3 (读数前写死, 门未达只报进度)。
RFC153_SINCE = "2026-10-21"
RFC153_WINDOW_MIN = 15
RFC153_NEED_N = 30
RFC153_NEED_DAYS = 10
RFC153_BASELINE = {"window": "2026-09-23~10-20", "n": 23, "net": -446.69,
                   "sl_initial_n": 4}

# ── 预注册基线 (RFC-141 §4, 裁决日禁挪) ──
# 口径: source_type=PLAYBOOK_SIM ∧ exit_price>0 ∧ COALESCE(pnl_net,realized_pnl) NOT NULL
#      ∧ 排除 gate_mode=research_momentum 研究臂 (2026-10-20 P0, 见 EXCLUDE_RESEARCH_ARM_SQL)
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

# ── RFC-158 (2026-11-05 重开预注册) 阈值 —— 读数前写死, 窗内禁改 ──────────────
RFC158_M3_MAX_GAP = 0.15       # M3: |SHORT放弃率 − LONG放弃率| ≤ 15pp (防砍唯一赚钱侧)
RFC158_M4_MIN_RATIO = 1.2      # M4: 净经济价值 = (避免额 + 省下往返费) / 机会成本 ≥ 1.2
RFC158_M2_MAX_OPP_MEAN = 0.35  # M2: opp_cost **均值** ≤ 0.35% (上一段实测 0.313%; 中位仅披露)
RFC158_E1_RELAX_RATIO = 0.5    # E1: 族净均 ≥ 基线 × 50% (= −$81.17), 且 族 n ≥ 10
RFC158_E1_MIN_N = 10
RFC158_COOLDOWN_AFTER_REOPEN = "2026-11-05"  # 新 cohort 起点 (宪章第2律: 开门日重起 cohort)
RFC158_HOLDOUT_MIN_N = 20      # 对照臂样本门 (20% holdout 必须够, 否则读数不可归因)
RFC158_TAIL_MIN_TREATMENT_N = 60
RFC158_TAIL_MIN_HOLDOUT_N = 20
RFC158_TAIL_MIN_PAIRED_DAYS = 10
RFC158_TAIL_MIN_COVERAGE = 0.95
Z80 = 1.2815515655446004
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
# schema 双代 (RFC-141 §5.10): v1=混尺单点 (10-10~10-15), v2=mid-mid+grace (10-15 起)。
# 裁决窗 = 现行代开门日 (v2 存在 ⇒ v2 首行 date); v1 窗单出 legacy_v1_window 留痕不裁决。
SCHEMA_V1 = "entry_confirm_forward_v1"
SCHEMA_V2 = "entry_confirm_forward_v2"

# 2026-10-20 P0 口径修复: 研究臂 (RFC-149, gate_mode=research_momentum) 自 10-19
# 起写 PLAYBOOK_SIM, 但它不经过入场确认门, 也不在 8/1–10/9 冻结基线人群内
# (研究源 10-18 才启用)。混入裁决队列会把 EXT 结果算成门的效果 (10-19 实证:
# NVDA -75.50 成为唯一 SL_INITIAL, PEP +106.64 抬高非 SL 族)。只排除研究臂,
# 保留 strict EXT — strict EXT 在基线窗内已存在, 排除会破坏基线同尺。
RESEARCH_ARM_GATE_MODE = "research_momentum"
EXCLUDE_RESEARCH_ARM_SQL = (
    "COALESCE(json_extract(metadata_json, '$.gate_mode'), '') != 'research_momentum'"
)


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
    """载入判据人群行: schema ∈ {v1, v2} 且 type=entry_confirm。

    v2 (RFC-141 §5.10, 2026-10-15): 判尺改 mid-mid + grace 持续性 ⇒ 行尾打 _gen 标签
    (v1=混尺单点代 / v2=mid同尺+grace代) 供分代聚合, 两源不混算 (与 §5.9 同纪律)。
    type=entry_confirm_post (弃单后 +5m/+10m 补记) 是纯观测行, 不进 _agg ——
    单独计数披露 (它不是坏行, 打了也不进判据人群)。
    """
    if not FWD.exists():
        return []
    rows, bad, post, post_list = [], 0, 0, []
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
        if r.get("type") == "entry_confirm_post":
            post += 1                      # 观测行: 计数披露, 不进判据人群
            post_list.append(r)            # RFC-158 M4 用它估「被弃笔避免的亏损额」(短窗估计)
            continue
        if r.get("schema") in (SCHEMA_V1, SCHEMA_V2) and r.get("type") == "entry_confirm":
            r["_gen"] = "v2" if r.get("schema") == SCHEMA_V2 else "v1"
            rows.append(r)
        else:
            bad += 1
            print(f"  ⚠️ schema 不符跳过: {str(r.get('schema'))[:60]}", file=sys.stderr)
    if bad:
        print(f"  ⚠️ 共跳过 {bad} 行 (坏行/schema 不符) — 判据人群已缩小, 见 stderr",
              file=sys.stderr)
    load_rows.bad_lines = bad
    load_rows.post_rows = post
    load_rows.post_rows_list = post_list
    return rows


def verdict_window(rows):
    """(gate_live_date, verdict_since, gen) — 开窗/裁决窗推导 (RFC-141 §5.10)。

    gate_live_date = 任意代首行 date (v1 开门日, 留痕锚); verdict_since = 现行代
    (v2 存在 ⇒ v2 首行 date, 否则 v1 开门日)。纯函数, 供契约测试直钉。
    """
    gate = min((str(r.get("date")) for r in rows if r.get("date")), default=None)
    v2 = min((str(r.get("date")) for r in rows
              if r.get("_gen") == "v2" and r.get("date")), default=None)
    return gate, (v2 or gate), ("v2" if v2 else "v1")


def _mom_exclude_sql(con) -> str:
    columns = {row[1] for row in con.execute("PRAGMA table_info(trade_records)")}
    return (
        "  AND COALESCE(decision_id, '') NOT LIKE 'MOM-ETF-V1-%' "
        if "decision_id" in columns else ""
    )


def load_sim_pnl(since_date: str):
    """开门后 SIM 真实成交盈亏 — 裁决源 (富途 app 可见的那条链路)。

    WHERE 逐字照抄基线口径 (NULL 下等价重写不成立, memory 纪律):
      source_type='PLAYBOOK_SIM' AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL
    2026-10-20 P0: 再排除 gate_mode=research_momentum 的研究臂 — 它是 10-19 才启用的
    新通道, 不在冻结基线人群内, 且不经过本门。strict EXT 保留 (基线窗内已存在)。
    2026-11-12 P0: 排除 MOM-ETF-V1 试点桶 — 它绕过 RFC-141 确认门, 不得混入 DA-only 裁决。
    fee_bps 相关列一并带出 (判据④ 两端同尺需要, 见 §5.6 / DEB §33.3)。
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    mom_exclude_sql = _mom_exclude_sql(con)
    out = []
    for r in con.execute(
            "SELECT symbol, trade_date, entry_time, exit_time, exit_reason, fees_est, "
            "       quantity AS qty, entry_price, "
            "       COALESCE(pnl_net, realized_pnl) AS net "
            "FROM trade_records "
            "WHERE source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0) > 0 "
            "  AND COALESCE(pnl_net, realized_pnl) IS NOT NULL "
            f"  AND {EXCLUDE_RESEARCH_ARM_SQL} "
            f"{mom_exclude_sql}"
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
    mom_exclude_sql = _mom_exclude_sql(con)
    fills = defaultdict(list)
    for r in con.execute(
            "SELECT symbol, trade_date, COALESCE(pnl_net, realized_pnl) AS net "
            "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 "
            f"AND {EXCLUDE_RESEARCH_ARM_SQL} "
            f"{mom_exclude_sql}"
            "AND trade_date>=?", (since_date,)):
        fills[(r["symbol"], r["trade_date"])].append(float(r["net"] or 0))
    con.close()
    return fills


def load_trade_by_decision(decision_ids):
    """decision_id → {net, exit_reason} (已平 PLAYBOOK_SIM; 只读)。

    用于 RFC-153 进度读数的 cohort join: forward 行带 decision_id, 而 (symbol,date)
    的 _agg 口径会把同日同标的的其它单混进来 ⇒ 该 cohort 用 decision_id 逐笔对齐。
    """
    ids = [str(d) for d in decision_ids if d]
    if not ids:
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            for r in con.execute(
                    "SELECT decision_id, COALESCE(pnl_net, realized_pnl) AS net, exit_reason "
                    f"FROM trade_records WHERE decision_id IN ({q}) "
                    "AND source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0)>0 "
                    "AND COALESCE(pnl_net, realized_pnl) IS NOT NULL", chunk):
                out[str(r["decision_id"])] = {"net": float(r["net"] or 0),
                                              "exit_reason": r["exit_reason"]}
    finally:
        con.close()
    return out


def _tail_arm_stats(assigned, resolved, unresolved_n):
    nets = [float(r["net"]) for r in resolved]
    sl = [r for r in resolved if (r.get("exit_reason") or "") == "SL_INITIAL"]
    sl_loss = sum(max(0.0, -float(r["net"])) for r in sl)
    n = len(resolved)
    tail_n = max(1, math.ceil(n * 0.1)) if n else 0
    worst = sorted(nets)[:tail_n]
    return {
        "assigned_n": len(assigned),
        "resolved_n": n,
        "unresolved_n": unresolved_n,
        "resolution_coverage": round(n / len(assigned), 4) if assigned else None,
        "closed_trade_n": sum(r.get("resolved_from") == "closed_trade" for r in resolved),
        "abandoned_n": sum(r.get("resolved_from") == "abandoned" for r in resolved),
        "net_sum": round(sum(nets), 4),
        "net_per_resolved_assignment": round(statistics.mean(nets), 4) if nets else None,
        "sl_initial_n": len(sl),
        "sl_initial_loss_sum": round(sl_loss, 4),
        "sl_initial_loss_per_resolved_assignment": round(sl_loss / n, 4) if n else None,
        "sl_initial_events_per_100_resolved_assignments": (
            round(len(sl) / n * 100.0, 4) if n else None),
        "bottom_decile_mean_net": round(statistics.mean(worst), 4) if worst else None,
        "worst_net": round(min(nets), 4) if nets else None,
    }


def _paired_day_ci(values):
    n = len(values)
    if not n:
        return {"n_days": 0, "mean": None, "mde80": None, "ci80": None}
    mean = statistics.mean(values)
    mde = Z80 * statistics.stdev(values) / math.sqrt(n) if n >= 2 else None
    return {
        "n_days": n,
        "mean": round(mean, 4),
        "mde80": round(mde, 4) if mde is not None else None,
        "ci80": [round(mean - mde, 4), round(mean + mde, 4)] if mde is not None else None,
    }


def tail_objective(rows, joined):
    """Compare resolved assignments; abandoned treatment decisions have zero realized PnL."""
    arms = {}
    resolved_by_arm_day = {"treatment": defaultdict(list), "holdout": defaultdict(list)}
    for arm in ("treatment", "holdout"):
        assigned = [r for r in rows if r.get("arm") == arm and r.get("status") == "ok"]
        resolved = []
        unresolved_n = 0
        for row in assigned:
            did = str(row.get("decision_id") or "")
            if arm == "treatment" and not row.get("still_triggers"):
                item = {"date": str(row.get("date") or ""), "net": 0.0,
                        "exit_reason": None, "resolved_from": "abandoned"}
            elif did and did in joined:
                trade = joined[did]
                item = {"date": str(row.get("date") or ""), "net": float(trade["net"]),
                        "exit_reason": trade.get("exit_reason"),
                        "resolved_from": "closed_trade"}
            else:
                unresolved_n += 1
                continue
            resolved.append(item)
            resolved_by_arm_day[arm][item["date"]].append(item)
        arms[arm] = _tail_arm_stats(assigned, resolved, unresolved_n)

    treatment = arms["treatment"]
    holdout = arms["holdout"]
    paired_dates = sorted(set(resolved_by_arm_day["treatment"])
                          & set(resolved_by_arm_day["holdout"]))
    daily_sl_reduction = []
    daily_net_delta = []
    for date in paired_dates:
        t_rows = resolved_by_arm_day["treatment"][date]
        h_rows = resolved_by_arm_day["holdout"][date]
        t_sl = sum(max(0.0, -float(r["net"])) for r in t_rows
                   if (r.get("exit_reason") or "") == "SL_INITIAL") / len(t_rows)
        h_sl = sum(max(0.0, -float(r["net"])) for r in h_rows
                   if (r.get("exit_reason") or "") == "SL_INITIAL") / len(h_rows)
        daily_sl_reduction.append(h_sl - t_sl)
        daily_net_delta.append(
            statistics.mean(float(r["net"]) for r in t_rows)
            - statistics.mean(float(r["net"]) for r in h_rows))

    def _delta(key, reverse=False):
        t_value, h_value = treatment.get(key), holdout.get(key)
        if t_value is None or h_value is None:
            return None
        return round((h_value - t_value) if reverse else (t_value - h_value), 4)

    comparison = {
        "sl_initial_loss_burden_reduction": _delta(
            "sl_initial_loss_per_resolved_assignment", reverse=True),
        "sl_initial_event_rate_reduction_per_100": _delta(
            "sl_initial_events_per_100_resolved_assignments", reverse=True),
        "bottom_decile_mean_improvement": _delta("bottom_decile_mean_net"),
        "total_net_per_assignment_delta": _delta("net_per_resolved_assignment"),
    }
    daily = {
        "sl_initial_loss_burden_reduction": _paired_day_ci(daily_sl_reduction),
        "total_net_per_assignment_delta": _paired_day_ci(daily_net_delta),
    }
    coverage_ok = all(
        arms[arm]["resolution_coverage"] is not None
        and arms[arm]["resolution_coverage"] >= RFC158_TAIL_MIN_COVERAGE
        for arm in ("treatment", "holdout"))
    sample_gate = {
        "treatment_resolved_n": treatment["resolved_n"],
        "treatment_min_n": RFC158_TAIL_MIN_TREATMENT_N,
        "holdout_resolved_n": holdout["resolved_n"],
        "holdout_min_n": RFC158_TAIL_MIN_HOLDOUT_N,
        "paired_days": len(paired_dates),
        "min_paired_days": RFC158_TAIL_MIN_PAIRED_DAYS,
        "min_coverage": RFC158_TAIL_MIN_COVERAGE,
        "coverage_ok": coverage_ok,
    }
    sample_gate["ok"] = (
        treatment["resolved_n"] >= RFC158_TAIL_MIN_TREATMENT_N
        and holdout["resolved_n"] >= RFC158_TAIL_MIN_HOLDOUT_N
        and len(paired_dates) >= RFC158_TAIL_MIN_PAIRED_DAYS
        and coverage_ok)
    sl_reduction = comparison["sl_initial_loss_burden_reduction"]
    net_delta = comparison["total_net_per_assignment_delta"]
    sl_daily = daily["sl_initial_loss_burden_reduction"]
    net_daily = daily["total_net_per_assignment_delta"]
    if not rows:
        verdict = "TOO_EARLY"
        why = "当前 cohort 尚无分配样本"
    elif not sample_gate["ok"]:
        verdict = "INSUFFICIENT"
        why = "分配样本、同期配对日或结局覆盖率未达门；不作 PASS/FAIL"
    else:
        sl_lower = (sl_daily.get("ci80") or [None, None])[0]
        sl_upper = (sl_daily.get("ci80") or [None, None])[1]
        net_upper = (net_daily.get("ci80") or [None, None])[1]
        if (sl_reduction is not None and sl_reduction > 0
                and net_delta is not None and net_delta >= 0
                and sl_lower is not None and sl_lower > 0):
            verdict = "PASS"
            why = "SL_INITIAL每分配指令亏损负担下降且日聚类80%CI>0，总净额未恶化"
        elif ((sl_reduction is not None and sl_reduction < 0
               and sl_upper is not None and sl_upper < 0)
              or (net_delta is not None and net_delta < 0
                  and net_upper is not None and net_upper < 0)):
            verdict = "FAIL"
            why = "大亏损负担或总净额恶化且日聚类80%CI排除零；只提请复核，不自动关门"
        else:
            verdict = "OBSERVE"
            why = "方向或显著性尚未同时满足；胜率、机会成本和M1~M4不作自动关门理由"
    return {
        "objective": "reduce_large_losses_without_worse_total_net_per_resolved_assignment",
        "arms": arms,
        "comparison": comparison,
        "daily_paired": daily,
        "sample_gate": sample_gate,
        "verdict": verdict,
        "verdict_why": why,
        "resolution_contract": (
            "treatment abandoned=0 realized PnL; confirmed/holdout require closed PLAYBOOK_SIM "
            "trade by decision_id; unresolved rows never converted to zero"),
        "diagnostics_only": ["win_rate", "M1_avoidance", "M2_opp_cost", "M3_direction_gap",
                             "M4_short_window_value", "E2_small_winner_floor"],
    }


def rfc153_progress(rows, joined=None):
    """RFC-153 FADE_SHORT 开盘快跌禁追 进度块 (只印不判; 判据见 RFC-153 §3)。

    人群 = entry_archetype=='FADE_SHORT' ∧ 0 ≤ session_min < window ∧ date ≥ 生效首日。
    entry_archetype / abandon_cause / chase_cap_hit / session_min 由 gate 直落 forward 行
    (2026-10-20 起); 更早的行没有这些字段 ⇒ 天然不进本窗。
    joined: decision_id → {net, exit_reason}; None 时自行查库 (测试可注入)。
    """
    _fs_rows = [r for r in rows
                if r.get("entry_archetype") == "FADE_SHORT"
                and isinstance(r.get("session_min"), int)
                and 0 <= r["session_min"] < RFC153_WINDOW_MIN
                and str(r.get("date") or "") >= RFC153_SINCE]
    _fs_cap = [r for r in _fs_rows if r.get("chase_cap_hit")]
    _fs_conf = [r for r in _fs_rows if r.get("still_triggers") is True]
    _fs_days = sorted({str(r.get("date")) for r in _fs_rows})
    if joined is None:
        joined = load_trade_by_decision([r.get("decision_id") for r in _fs_conf])
    nets = [v["net"] for v in joined.values()]
    return {
        "since": RFC153_SINCE, "window_min": RFC153_WINDOW_MIN,
        "n_samples": len(_fs_rows), "n_days": len(_fs_days),
        "n_chase_cap_abandoned": len(_fs_cap),
        "n_confirmed": len(_fs_conf), "n_confirmed_joined": len(nets),
        "confirmed_net_sum": round(sum(nets), 2) if nets else None,
        "confirmed_net_mean": round(statistics.mean(nets), 2) if nets else None,
        "confirmed_sl_initial_n": sum(1 for v in joined.values()
                                      if (v.get("exit_reason") or "") == "SL_INITIAL"),
        "cap_hit_opp_median_pct": (
            round(statistics.median([float(r.get("opp_cost_pct") or 0) for r in _fs_cap]), 4)
            if _fs_cap else None),
        "baseline": RFC153_BASELINE,
        "gate": {"need_n": RFC153_NEED_N, "need_days": RFC153_NEED_DAYS,
                 "n_ok": len(_fs_rows) >= RFC153_NEED_N,
                 "days_ok": len(_fs_days) >= RFC153_NEED_DAYS,
                 "pass": len(_fs_rows) >= RFC153_NEED_N and len(_fs_days) >= RFC153_NEED_DAYS},
        "note": ("只印不判: 样本门 ≥10 ET 日 ∧ ≥30 笔 (treatment+abandoned 均计入机制样本; "
                 "PnL 只对有成交的 treatment 计, decision_id 对齐); 主判据见 RFC-153 §3 "
                 "(SL_INITIAL 笔数下降 ≥50% / 扣费净 bps 不劣 / 误杀净额 < 避免净额)。"),
    }


def avoided_bps_from_post(decision_row, posts):
    """Return (avoided adverse bps, anchor basis) from confirm/abandon to post-watch.

    New post fields are direction-normalized percentages for both LONG and SHORT.
    Legacy arm-anchored mid_move_pct is intentionally not a fallback: it includes
    pre-abandon movement and cannot measure the value of the abandon decision.
    """
    if not posts:
        return None, "missing_post_watch"
    pick = next((p for p in posts if p.get("horizon") == "+10m"), posts[0])
    for field, basis in (
        ("mid_from_confirm_pct", "confirm_mid_to_post_mid"),
        ("last_from_confirm_pct", "confirm_last_to_post_last"),
    ):
        raw = pick.get(field)
        if raw is None:
            continue
        try:
            move = float(raw)
        except (TypeError, ValueError) as exc:
            print(f"[settle_entry_confirm_forward] {field} 非数值跳过 {raw!r}: {type(exc).__name__}", file=sys.stderr)
            continue
        if not math.isfinite(move):
            continue
        return max(0.0, -move * 100.0), basis
    return None, "legacy_arm_anchor_excluded"


def path_shadow_progress(rows):
    paths = [r.get("path_shadow") for r in rows if isinstance(r.get("path_shadow"), dict)]
    evaluable = [p for p in paths if p.get("evaluable") is True]
    reasons = Counter(reason for p in paths for reason in p.get("unevaluable_reasons") or [])
    days = {r.get("date") for r in rows
            if isinstance(r.get("path_shadow"), dict) and r["path_shadow"].get("evaluable") is True}
    return {
        "n": len(paths),
        "evaluable_n": len(evaluable),
        "evaluable_days": len(days),
        "r1_would_confirm_n": sum(p.get("r1_would_confirm") is True for p in evaluable),
        "r1_would_abandon_n": sum(p.get("r1_would_confirm") is False for p in evaluable),
        "r1_agree_live_n": sum(p.get("r1_agrees_with_live") is True for p in evaluable),
        "r1_disagree_live_n": sum(p.get("r1_agrees_with_live") is False for p in evaluable),
        "unevaluable_reason_counts": dict(reasons),
        "sample_gate": {"min_evaluable_n": 30, "min_evaluable_days": 10,
                        "ok": len(evaluable) >= 30 and len(days) >= 10},
        "note": "R1=60/90/120s valid-mid true 2-of-3；record-only，不进入RFC-158现役PASS/FAIL",
    }


def holdout_strata(rows, fills):
    groups = {
        "LONG": [r for r in rows if r.get("direction") == "LONG"],
        "SHORT": [r for r in rows if r.get("direction") == "SHORT"],
        "open_lt_15m": [r for r in rows if isinstance(r.get("session_min"), int)
                        and 0 <= r["session_min"] < 15],
        "later_ge_15m": [r for r in rows if isinstance(r.get("session_min"), int)
                         and r["session_min"] >= 15],
    }
    for archetype in sorted({str(r.get("entry_archetype") or "UNKNOWN") for r in rows}):
        groups[f"archetype:{archetype}"] = [
            r for r in rows if str(r.get("entry_archetype") or "UNKNOWN") == archetype]
    out = {}
    for name, subset in groups.items():
        agg = _agg(subset, fills)
        out[name] = {"n": agg["n"], "confirmed_fill_net_mean": agg["confirmed_fill_net_mean"],
                     "n_confirmed_joined": agg["n_confirmed_joined"]}
    return out


def main():
    all_rows = load_rows()
    rows = [r for r in all_rows
            if str(r.get("date") or "") >= RFC158_COOLDOWN_AFTER_REOPEN]
    historical_rows = len(all_rows) - len(rows)
    post_rows_list = [r for r in (getattr(load_rows, "post_rows_list", []) or [])
                      if str(r.get("date") or "") >= RFC158_COOLDOWN_AFTER_REOPEN]
    load_rows.post_rows_list = post_rows_list
    load_rows.post_rows = len(post_rows_list)

    # 开门日 = forward 产物首行 date (门真 arm 过那天); 无产物 ⇒ 门尚未上线
    gate_live_date = min((str(r.get("date")) for r in rows if r.get("date")), default=None)
    if not gate_live_date:
        print("RFC-141: 无 forward 产物 ⇒ 门尚未上线 (TOO_EARLY)。"
              " 首次 arm 后本结算器才有裁决窗口。")
        empty_sim = _pnl_block([])
        empty_tail = tail_objective([], {})
        out = {"generated_at": datetime.datetime.now().astimezone().isoformat(),
               "rfc": "RFC-141", "verdict": "TOO_EARLY", "gate_live_date": None,
               "reason": "无 forward 产物 (门未 arm 过任何指令)",
               "current_cohort": {
                   "cohort_since": RFC158_COOLDOWN_AFTER_REOPEN,
                   "sim": empty_sim,
                   "mechanism": _agg([], {}),
                   "holdout": _agg([], {}),
                   "path_shadow": path_shadow_progress([]),
                   "holdout_strata": {},
                   "tail_objective": empty_tail,
               },
               "adjudication_source": {"all": empty_sim},
               "historical_cohort": {
                   "excluded_rows": historical_rows,
                   "note": "RFC-158 开始前及已归档行仅作历史证据, 不进入当前 cohort 计数。",
               },
               "caliber": "裁决源=开门后 SIM 真实盈亏 vs 预注册基线 (RFC-141 §4)"}
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT)
        return 0

    # v2 双窗 (RFC-141 §5.10): 裁决窗 = 现行代开门日 (v2 首行 date; 无 v2 行 = 仍是 v1 代)。
    # v1 窗 [gate_live_date, v2_live_date) 的 SIM 盈亏+机制量单出 legacy_v1_window 块,
    # 留痕不裁决 — 两代判据不同尺, 混算 = §5.9 明令禁止的「两源混算」。
    gate_live_date, verdict_since, verdict_gen = verdict_window(rows)
    v2_live_date = verdict_since if verdict_gen == "v2" else None

    sim = load_sim_pnl(verdict_since)
    fills = load_fills(verdict_since)

    legacy_v1_window = None
    if v2_live_date and v2_live_date > gate_live_date:
        _sim_v1 = [r for r in load_sim_pnl(gate_live_date) if r["trade_date"] < v2_live_date]
        _fills_v1 = defaultdict(list)
        for _k, _v in load_fills(gate_live_date).items():
            if _k[1] < v2_live_date:
                _fills_v1[_k] = _v
        _v1_rows = [r for r in rows if r.get("_gen") == "v1"]
        legacy_v1_window = {
            "window": [gate_live_date, v2_live_date],
            "note": ("v1 混尺单点代 (2026-10-10~10-15) — 留痕不裁决 (RFC-141 §5.10: "
                     "两源不混算); v1 实测弃单率 61%、CONFIRMED opp_cost 中位 +0.36% "
                     "已超判据③ 轨迹 ⇒ 触发了 v2 判尺修订"),
            "sim_pnl": {"all": _pnl_block(_sim_v1),
                        "sl_initial": _pnl_block(
                            _sim_v1, lambda r: (r.get("exit_reason") or "") == "SL_INITIAL")},
            "mechanism_v1": _agg(_v1_rows, _fills_v1),
        }

    # ── 裁决源: 开门后 SIM 真实盈亏, 分四个人群 ──
    p_all = _pnl_block(sim)
    p_sl = _pnl_block(sim, lambda r: (r.get("exit_reason") or "") == "SL_INITIAL")
    p_rest = _pnl_block(sim, lambda r: (r.get("exit_reason") or "") != "SL_INITIAL")
    p_win = _pnl_block(sim, lambda r: _in_open_win(r.get("entry_time")))

    # ── 机制留痕 (不裁决) — v2 起分代: 判据③ 只用现行代行 (两代判据不同尺, 不混算) ──
    a_all = _agg(rows, fills)
    treat_v1 = [r for r in rows if r.get("arm") == "treatment" and r.get("_gen") == "v1"]
    treat_v2 = [r for r in rows if r.get("arm") == "treatment" and r.get("_gen") == "v2"]
    treat_cur = treat_v2 if verdict_gen == "v2" else treat_v1
    a_t = _agg(treat_cur, fills)
    hold = [r for r in rows if r.get("arm") == "holdout"]

    # ── RFC-153 FADE_SHORT 开盘快跌禁追 进度 (只印不判, 预注册见 RFC-153 §3) ──
    rfc153 = rfc153_progress(rows)

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

    # ── RFC-158 (2026-11-05 重开预注册) 新判据 M3 / M4 ─────────────────────────
    # M3 分方向放弃率差: |SHORT放弃率 − LONG放弃率| ≤ 15pp
    #   目的: 防门系统性砍掉唯一赚钱侧 (上一段空头放弃率 57.9% vs 多头 44%)。
    # M4 净经济价值 = (避免的亏损额 + 被弃笔省下的往返费) / 确认笔机会成本 ≥ 1.2
    #   目的: 上一段关门依据①「净经济价值≈0」只算两栏, 漏了"被弃笔省下的往返费"
    #         ⇒ 本条补齐三栏。三栏全用 bps ⇒ 不依赖名义额。
    #   「避免的亏损额」用 entry_confirm_post 补记行 (弃单后 +5m/+10m 的价格变动) 做
    #   **短窗估计** (不是全程结局; 保守方向: 只计朝不利方向的变动)。
    _post_by_did = defaultdict(list)
    for _r in getattr(load_rows, "post_rows_list", []) or []:
        _post_by_did[_r.get("decision_id")].append(_r)

    _short_rows = [r for r in treat_cur if str(r.get("direction") or "").upper()
                   in ("SHORT", "SELL_SHORT", "SELL")]
    _long_rows = [r for r in treat_cur if str(r.get("direction") or "").upper() == "LONG"]
    a_short = _agg(_short_rows, fills)
    a_long = _agg(_long_rows, fills)
    _ab_rate_s = a_short.get("avoidance_rate")
    _ab_rate_l = a_long.get("avoidance_rate")
    _m3_gap = (round(abs(_ab_rate_s - _ab_rate_l), 4)
               if (_ab_rate_s is not None and _ab_rate_l is not None) else None)
    m3 = _m3_gap is not None and _m3_gap <= RFC158_M3_MAX_GAP

    _ab_rows = [r for r in treat_cur
                if r.get("status") == "ok" and not r.get("still_triggers")]
    _avoid_results = [avoided_bps_from_post(r, _post_by_did.get(r.get("decision_id")) or [])
                      for r in _ab_rows]
    _avoid_list = [value for value, _basis in _avoid_results if value is not None]
    _avoid_basis_counts = Counter(basis for value, basis in _avoid_results if value is not None)
    _avoid_missing_n = sum(value is None for value, _basis in _avoid_results)
    _avoid_coverage_ok = len(_avoid_list) == len(_ab_rows)
    _avoid_sum = sum(_avoid_list)
    _fee_bps_med = None
    try:
        _fb = [fee_bps(r) for r in sim]
        _fb = [x for x in _fb if x is not None]
        if _fb:
            _fee_bps_med = round(statistics.median(_fb), 2)
    except Exception:
        _fee_bps_med = None
    _opp_bps_sum = sum(float(r.get("opp_cost_pct") or 0) * 100.0
                        for r in treat_cur
                        if r.get("status") == "ok" and r.get("still_triggers")
                        and r.get("opp_cost_pct") is not None)
    _saved_fee_bps = (len(_ab_rows) * _fee_bps_med) if _fee_bps_med is not None else 0.0
    m4_ratio = (round((_avoid_sum + _saved_fee_bps) / _opp_bps_sum, 4)
                if _opp_bps_sum > 0 else None)
    m4 = (m4_ratio is not None and m4_ratio >= RFC158_M4_MIN_RATIO
          and _avoid_coverage_ok)

    sample_ok = (p_all["n"] >= MIN_SIM_N) or (p_all["days"] >= MIN_SIM_DAYS)
    # RFC-158: 对照臂样本门 (20% holdout 必须够, 否则读数不可归因)
    a_hold = _agg(hold, fills)
    holdout_ok = a_hold["n"] >= RFC158_HOLDOUT_MIN_N
    hold_cur = [r for r in hold if r.get("_gen") == verdict_gen]
    tail_rows = treat_cur + hold_cur
    tail_joined = load_trade_by_decision(
        [r.get("decision_id") for r in tail_rows if r.get("still_triggers")])
    tail = tail_objective(tail_rows, tail_joined)

    _m1 = a_t.get("avoidance_rate") is not None and a_t["avoidance_rate"] >= MIN_AVOID
    _m2 = (a_t.get("opp_cost_mean_pct") is not None
           and a_t["opp_cost_mean_pct"] <= RFC158_M2_MAX_OPP_MEAN)
    _e1_floor = round(BASELINE["sl_initial"]["net_mean"] * RFC158_E1_RELAX_RATIO, 2)
    _e1 = (sl_mean is not None and sl_mean >= _e1_floor
           and p_sl["n"] >= RFC158_E1_MIN_N)
    _e2 = c4  # 非 SL_INITIAL 族净均 ≥ 剔费地板 +$8.79 (沿用 RFC-141 判据④, 机制未改)

    verdict = tail["verdict"]
    verdict_why = tail["verdict_why"]

    # R1 分层只印不判 (现行代行, 与判据③ 同人群)
    r1 = [r for r in treat_cur if r.get("direction") == "SHORT"
          and r.get("session_min") is not None and 0 <= r["session_min"] < 15]
    r1_long = [r for r in treat_cur if r.get("direction") == "LONG"
               and r.get("session_min") is not None and 0 <= r["session_min"] < 15]
    path_progress = path_shadow_progress(treat_cur)
    holdout_groups = holdout_strata(hold, fills)

    out = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "rfc": "RFC-141",
        "gate_live_date": gate_live_date,
        # v2 双窗 (§5.10): 裁决窗自现行代开门日起算; gate_live_date 恒为 v1 开门日 (留痕锚)
        "verdict_window_since": verdict_since,
        "verdict_generation": verdict_gen,
        "verdict": verdict,
        "verdict_why": verdict_why,
        "current_cohort": {
            "cohort_since": RFC158_COOLDOWN_AFTER_REOPEN,
            "sim": p_all,
            "mechanism": a_t,
            "holdout": a_hold,
            "path_shadow": path_progress,
            "holdout_strata": holdout_groups,
            "tail_objective": tail,
        },
        "adjudication_source": {
            "kind": "SIM 真实成交盈亏 (富途 app 可见链路)",
            "table": "trade_records",
            "where": ("source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0)>0 AND "
                      "COALESCE(pnl_net, realized_pnl) IS NOT NULL AND "
                      + EXCLUDE_RESEARCH_ARM_SQL + " AND trade_date>='"
                      + verdict_since + "'"),
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
            # ── RFC-158 (2026-11-05 重开预注册) 新判据 —— **裁决用这一组** ──
            # RFC-141 §4 的判据已被 10-31 INSUFFICIENT 裁决消耗 (族绝对严线 −$40 在族样本
            # n 小时统计上不可达), 故重开另立判据; 上面 c1~c4 保留为历史口径留档。
            "rfc158": {
                "rfc": "RFC-158",
                "cohort_since": RFC158_COOLDOWN_AFTER_REOPEN,
                "role": "diagnostics_only_not_automatic_close_gates",
                "M1_avoidance_rate": {"ok": _m1, "value": a_t.get("avoidance_rate"),
                                      "floor": MIN_AVOID},
                "M2_opp_cost_mean_pct": {"ok": _m2, "value": a_t.get("opp_cost_mean_pct"),
                                         "max": RFC158_M2_MAX_OPP_MEAN,
                                         "median_disclosure_only": a_t.get("opp_cost_median_pct")},
                "M3_direction_abandon_gap": {"ok": m3, "gap": _m3_gap,
                                             "max": RFC158_M3_MAX_GAP,
                                             "short_avoidance": _ab_rate_s,
                                             "long_avoidance": _ab_rate_l},
                "M4_net_economic_value": {"ok": m4, "ratio": m4_ratio,
                                         "min": RFC158_M4_MIN_RATIO,
                                         "avoided_bps_sum": round(_avoid_sum, 2),
                                         "saved_fee_bps_sum": round(_saved_fee_bps, 2),
                                         "opp_cost_bps_sum": round(_opp_bps_sum, 2),
                                         "n_abandoned": len(_ab_rows),
                                         "avoided_coverage_n": len(_avoid_list),
                                         "avoided_missing_n": _avoid_missing_n,
                                         "avoided_anchor_basis_counts": dict(_avoid_basis_counts),
                                         "avoided_direction_rule": "LONG/SHORT unified: max(0, -direction_normalized_move_pct*100)",
                                         "legacy_arm_anchor_rows_excluded": _avoid_missing_n,
                                         "fee_bps_median": _fee_bps_med,
                                         "note": "避免额=弃单mid/last→+5m/+10m同尺变动，非arm锚；短窗估计非全程结局，缺新锚字段的旧行不进M4"},
                "E1_family_convergence": {"ok": _e1, "sl_net_mean": sl_mean,
                                          "floor_relative": _e1_floor,
                                          "base_mean": BASELINE["sl_initial"]["net_mean"],
                                          "family_n": p_sl["n"],
                                          "min_n": RFC158_E1_MIN_N},
                "E2_winners_not_hurt": {"ok": _e2,
                                        "rest_net_mean": p_rest_clean.get("net_mean"),
                                        "floor_clean": WINNER_MEAN_FLOOR_CLEAN},
                "holdout_sample_gate": {"ok": holdout_ok, "n": a_hold["n"],
                                        "min_n": RFC158_HOLDOUT_MIN_N},
                "tail_objective": tail,
                "verdict": verdict, "verdict_why": verdict_why,
            },
        },
        "mechanism_trace": {
            "n_rows": len(rows), "all": a_all, "treatment": a_t,
            "treatment_v1_legacy": _agg(treat_v1, fills),
            "treatment_v2": _agg(treat_v2, fills),
            "post_observation_rows": getattr(load_rows, "post_rows", 0),
            "holdout": _agg(hold, fills),
            "holdout_assignment": "sha1(entry_confirm_holdout:{inst_id}) v1; allocation unchanged",
            "holdout_strata": holdout_groups,
            "path_shadow": path_progress,
            "note": ("jsonl 降级为机制留痕 (2026-10-10 用户裁): 只证明门真在拦 + 拦的是"
                     "反转单 + 机会成本; 不独立裁决。v2 (§5.10): treatment=现行代 "
                     f"({verdict_gen}), 判据③只用它; 分代块并列, 两源不混算; "
                     "post_observation_rows=弃单后 +5m/+10m 补记行数 (纯观测不进聚合)"),
        },
        "legacy_v1_window": legacy_v1_window,
        "historical_cohort": {
            "excluded_rows": historical_rows,
            "note": "RFC-158 开始前及已归档行仅作历史证据, 不进入当前 cohort 计数。",
        },
        "strata_print_only": {
            "R1_short_open_window": _agg(r1, fills),
            "long_open_window": _agg(r1_long, fills),
        },
        "rfc153_chase_cap": rfc153,
        "caliber": ("裁决源=开门后 SIM 真实盈亏 vs 开门前预注册基线; holdout_pct=0 ⇒ "
                    "无同期对照臂, 受市况变化混淆 (披露 RFC §5.4); 判据1/3 为门自身机制量"
                    "不随市况漂, 故列为最强判据。开门日取现行代 forward 首行 date (不硬编码, "
                    "重启延后不错位)。v2 (§5.10): 判尺 mid-mid+grace 2-of-3, 裁决窗自 v2 "
                    "首行 date 重计, v1 窗单出 legacy_v1_window 留痕不裁决。"),
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT)

    print(f"RFC-158 结算: 现行代={verdict_gen} 裁决窗自={verdict_since} "
          f"(v1 开门={gate_live_date}) rows={len(rows)} verdict={verdict}")
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
    print(f"  尾部目标: SL负担减少={tail['comparison']['sl_initial_loss_burden_reduction']} "
          f"总净差={tail['comparison']['total_net_per_assignment_delta']} "
          f"样本门={tail['sample_gate']['ok']}")
    print(f"  判据: 1={c1} 2={c2} 3={c3} 4={c4}(剔费裁决) 4_full={c4_full}(并陈) 样本门={sample_ok}")
    print(f"  RFC-153 FADE_SHORT 开盘禁追: 样本 n={rfc153['n_samples']}"
          f"/{RFC153_NEED_N} days={rfc153['n_days']}/{RFC153_NEED_DAYS}"
          f" | 门拦 {rfc153['n_chase_cap_abandoned']} 笔"
          f" (拦时 opp 中位 {rfc153['cap_hit_opp_median_pct']}%)"
          f" | 确认 {rfc153['n_confirmed']} 笔 净均 {rfc153['confirmed_net_mean']}"
          f" SL_INITIAL {rfc153['confirmed_sl_initial_n']} 笔"
          f" (基线 n={RFC153_BASELINE['n']} 净 {RFC153_BASELINE['net']}"
          f" SL {RFC153_BASELINE['sl_initial_n']})"
          f" → {'达样本门(可呈裁)' if rfc153['gate']['pass'] else '攒样中'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
