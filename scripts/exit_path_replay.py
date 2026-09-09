#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线退出路径重放器 — 用与实盘同一套退出数学, 在 1min bar 上重放一笔仓位。

为什么需要 (2026-09-07, 配对反事实口径修正)
------------------------------------------
`scripts/cf_pullback_limit_replay.py` v1 的反事实臂只近似「原始 SL 触及 / 否则 EOD
收盘」两种出场, 而实盘臂走完整退出引擎 (保本 ratchet / 回撤锁利 / 早期紧 SL / TP /
PRE_CLOSE)。实测 9/1–9/4: 17 笔「限价挂不到」的 expired 单里 15 笔实盘以
SL_BREAKEVEN 小赢出场 (合计 +115.76), v1 给它们的反事实记 0 ⇒ **系统性低估反事实臂**。
同时 v1 拿 `cf_pnl`(毛) 比 `actual_net`(= trade_records.pnl_net, 净) ⇒ 又**系统性高估
反事实臂**。两处偏差方向相反、量级未知 ⇒ v1 的差值不可判读, 而
`playbook.entry_archetype_routing_enabled` 这个活体开关正等它裁决 (ADR-035)。

三条设计不变量
--------------
① **两臂同尺** — 实盘入场价与反事实入场价走同一个重放器; 实盘真值
   (`trade_records.pnl_net` / `exit_reason`) 另列, 只用于保真度校验, 不进配对差。
   重放器未复刻的规则因此成为**共模缺失**, 在配对差里抵消。
② **复用 SSOT 数学, 不造平行层** (AGENT.md 军规) — 保本/锁利有效止损直接 import
   `app.playbook.exit_engine._compute_dynamic_effective_sl` 与 `_get_factor_exit_config`;
   费用直接 import `app.execution.fee_model.estimate_round_trip`; 阈值全部读
   `get_playbook_config()`。本模块只实现「bar 循环 + 规则次序」这层离线专属逻辑。
③ **零副作用** — 刻意**不**调用 `_check_playbook_exit_rules`: 该函数体内写生产影子
   文件 (`data/breakeven_shadow.jsonl` / `early_dead_signal_shadow.jsonl` /
   winner_overnight arm 点) 并依赖墙钟 (`_is_overnight_position` 用 `datetime.now`,
   会把历史重放日误判成隔夜仓)。离线重放若调它 = 污染尺子 + 假隔夜分支。
   本模块不写任何文件、不读时钟、只读 config。

已复刻 (与 exit_engine 同序同公式; 方括号内 = 实盘规则号, 次序逐条对齐)
  [0]   开盘冷却放宽 (minutes_from_open<15 且保本未武装 → SL 放宽到 cooldown_sl_pct;
        已武装豁免放宽, 8/20 裁决)
  [0.5] 早期紧 SL (early_tight_sl_enabled / minutes / pct / mfe_gate / ATR 比例 clamp)
  [1]   SL 三分类 SL_INITIAL | SL_BREAKEVEN | SL_TRAIL_LOCK (分类线=费用垫+滑点垫, 1bp 容差)
  [1.5] TAKE_PROFIT (含 min_tp_rr 门禁; RR 不足则忽略指令 TP)
  [3]   PRE_CLOSE_EXIT (minutes_to_close∈[0,8]; cohort B/long horizon 盈利仓跳过)
  [3.3] DEAD_SIGNAL_EXIT (held≥dead_signal_exit_minutes ∧ 生命周期 peak<dead_signal_exit_mfe_pct
        → 收盘价离场; Phase0 reentry 呼吸窗 = decision_id 前缀 "intraday_reentry" 时窗宽
        +dead_signal_reentry_extension_min。2026-09-07 保真度专项步骤③ 补)
  [3.4] MFE_STALL_EXIT (held≥stall_minutes ∧ peak<stall_mfe_pct ∧ 当前水下; 8/04「浅亏转持有」
        分支同式: 亏损浅于 shallow_pct 时不出场, 交 SL/PRE_CLOSE 收尾。现网 enabled=False,
        仍配置驱动实现 ⇒ 任意参数纪年的窗都可重放)
  [4]   TIME_EXIT (held>time_exit_hours*60 ∧ (only_if_loss=False ∨ 毛口径水下); time_exit_hours
        取自 factor_exit_rules 命中的那一档 — 与实盘同一个 _get_factor_exit_config, 不另立常量)
  兜底  EOD_REPLAY (全日无规则触发 → 最后一根收盘价; 非实盘标签, 便于区分)

未复刻 (两臂同时缺失 ⇒ 共模; 由 fidelity 报告按 reason 逐条披露)
  [2] OVERNIGHT_GAP_STOP / [5] MAX_HOLD / 耐心出场等待态 (PATIENT_EXIT_*) / 隔夜日重置
  (RFC-064 TIME_EXIT 与 RFC-073 DEAD_SIGNAL 的 minutes_from_open 口径) / W_WEEK SIM 水下仓
  豁免 (_loser_overnight_exempt) —— 均需跨日 bar 或墙钟, 单日重放物理上不可复刻

参数纪年错位 (2026-09-07 步骤③ 实测; 它决定保真度门**怎么读**, 不决定门槛高低)
  重放器只读**现行** config, 而 trade_records 横跨多个参数纪年: MFE_STALL_EXIT 末次触发
  2026-08-11 (现 mfe_stall_exit_enabled=False)、TIME_EXIT 末次 08-14 (现 default
  time_exit_hours=6.0 ⇒ 6.5h 会话内 held>360min 几乎不可达)。全窗 (7/11 起 n=395) 含这两轨
  共 62 笔 ⇒ **全窗保真度数学上限 0.843**, 0.8 门在全窗不可达 —— 这是纪年错位, 不是重放器缺陷。
  宪章「禁跨窗拼接」⇒ 保真度门必须**按单纪年窗**评估 (8/15 起 = TIME_EXIT 末次触发之后),
  全窗读数只用于披露共模缺失规模, 不得当过门依据; 两口径 (arm_lag 0/1) 仍须双报。

bar 内歧义 (声明, 同样作用于两臂)
  1min bar 内 high/low 先后不可知。次序固定为: 有利极值更新 peak → 不利极值判 SL →
  有利极值判 TP → 收盘价判时间类。**入场那根 bar 整根不评估** (bar 内 high/low 可能
  发生在 fill 之前, 据其武装保本是假浮盈)。SL 成交价: 保本/锁利已武装取 active_sl
  (实盘挂限价单@净保本线, 订单类型保证地板), 未武装取 bar 不利极值 (真止损无保护,
  对齐 exit_engine 7/16 P0 语义)。两处口径均对两臂同尺。

武装生效滞后 arm_lag_bars (2026-09-07 保真度专项步骤①, DEB-20260907 §4.5/§4.6)
  **订单因果性**: 净保本限价单是在保本**武装之后**才挂出的, 故它不可能被"武装之前
  已经发生过"的价格成交。旧实现 (arm_lag_bars=0) 让同一根 1min bar 先用有利极值把
  peak 抬过 breakeven_at_pct 完成武装、再用**同一根**的不利极值打掉刚武装的保本线
  ⇒ "武装即死", peak 被系统性截断在武装线附近。实测后果 (395 笔逐笔重放):
  实盘 SL_TRAIL_LOCK 101 笔中 **91 笔被误分类** (84 笔→SL_BREAKEVEN), 且这些笔的
  重放 MFE **0%** 达到锁利所需最小 peak (中位 1.119%), 而实盘 MFE 达标率 **81%**
  (重放 MFE 中位 0.477% vs 实盘 1.512%) ⇒ 锁利分支在数学上永不可达。
  现默认 arm_lag_bars=1: 本根 bar 观测到的 peak **从下一根起**才成为在场止损的依据;
  本根内仍在场的止损 = 用上一根结束时的 peak 算 (初始 SL / 冷却放宽 / 早期紧 SL 照常,
  它们本就自入场起存在)。实盘 10s 轮询可在 bar 内先武装后触发, 故真值介于两口径之间:
  arm_lag_bars=0 是**乐观武装上界**, =1 是**保守武装下界**。两者必须**双报**, 不得只报
  通过保真度门的那一个 (宪章: 阈值预注册 + 禁挑口径)。mfe/mae/peak 的**观测**口径
  不受本参数影响 (仍取全 bar 极值) — 它约束的是"哪张止损单在场", 不是"价格到过哪"。

采样口径 sample_source (2026-09-07 保真度专项步骤①, DEB-20260907 §4.6-1)
  实盘退出快环每 **10s** 轮询一次 `current_price`, peak 与 SL/TP 判定都只用轮询到的价
  (exit_engine.py:329-333 `peak_price = max(peak_price, current_price)`); 而 1min bar 的
  high/low 是**整根极值**, 包含轮询可能整个错过的瞬时插针。故 bar-极值口径系统性**高估**
  实盘观测到的 peak, 也系统性**多判** SL/TP 触发。补完三轨后 (步骤③) 8/15+ 窗当日域
  剩余 43 笔失配里, `SL_TRAIL_LOCK→SL_BREAKEVEN` 16 笔 + `SL_INITIAL→SL_BREAKEVEN` 6 笔
  + `DEAD_SIGNAL→{SL_BREAKEVEN,TIME_EXIT}` 8 笔 全部指向这一条 (逐笔取证: 这些笔实盘
  `trade_records.mfe_pct` 低于重放 mfe, 且重放平均在入场后 4min 就假武装保本出场, 实盘
  却跑到 60min)。
  10s 价格路径无法从 OHLC 反演 ⇒ 不猜中间值, 而是**双报一个可证区间**:
    `sample_source="bar_extreme"` (默认): fav/adv 取 bar 有利/不利极值 = **实盘观测上界**
    `sample_source="bar_close"`:          fav/adv 均取 bar 收盘价     = **实盘观测下界**
  实盘 (每 bar ~6 次轮询) 的真值必落在两者之间。保真度门必须**两口径并报**, 不得只报
  过门的那一个 (与 arm_lag_bars 同一条纪律)。本参数只改"重放器认为实盘看到了什么",
  对反事实两臂同尺 ⇒ 配对差里共模抵消。

单日重放的结构性域 (2026-09-07 步骤③ 实测, 与参数纪年上限并列的第二道天花板)
  实盘出场日 ≠ 入场日的仓 (隔夜持有), 其真值路径需要跨日 bar + 墙钟, 单日重放物理上
  不可复刻。8/15+ 窗 n=178 中 **20 笔跨日** (match 仅 0.150) ⇒ 该窗保真度**数学上限
  0.888**。故保真度门须在**同纪年 ∧ 当日出场**的域上评估, 两条排除都必须预声明并披露
  样本数, 不得事后按"哪个域过门"挑选 (宪章: 禁挑口径)。

用法 (作为库):
    from scripts.exit_path_replay import replay_exit, cfg_snapshot
    rep = replay_exit(direction="LONG", entry_price=610.0, entry_hm="09:37",
                      initial_sl=582.55, take_profit=659.41, bars=bars, qty=9)
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

MARKET_OPEN_MINUTES_ET = 9 * 60 + 30
DEFAULT_CLOSE_MINUTES_ET = 16 * 60

#: 本重放器复刻的规则 (供输出自描述, 判读者不必读源码即知口径边界)
RULES_REPLICATED = ("cooldown_sl", "early_tight_sl", "sl_three_way", "take_profit_rr_gate",
                    "pre_close_exit", "dead_signal_exit", "mfe_stall_exit", "time_exit",
                    "eod_fallback")
#: 刻意未复刻的规则 — 两臂共模缺失, 由 fidelity 报告披露其影响
RULES_OMITTED = ("overnight_gap_stop", "max_hold_t5", "patient_exit_wait",
                 "overnight_daily_reset", "loser_overnight_exemptions")

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _hm_to_minutes(hm: str) -> int:
    """'09:37' / '2026-09-04 09:37:00' → ET 当日分钟数。解析失败抛 ValueError。"""
    s = str(hm or "")
    tok = s[11:16] if len(s) > 16 else s[:5]
    hh, mm = tok.split(":")[:2]
    return int(hh) * 60 + int(mm)


def load_cfg(refresh: bool = False) -> Dict[str, Any]:
    """读取重放所需的全部阈值 (单点快照, 使输出可复现/可审计)。

    与 exit_engine 同一批键; 读不到 config 时回退到 exit_engine 的同名默认值,
    保证重放器不因配置模块异常而静默改口径。
    """
    global _CFG_CACHE
    if _CFG_CACHE is not None and not refresh:
        return _CFG_CACHE
    from app.playbook.config import get_playbook_config
    from app.playbook.exit_engine import overnight_master_switch_on

    pb = get_playbook_config()
    cfg = {
        "factor_exit_rules": pb.get_horizon_top("factor_exit_rules", {}) or {},
        "breakeven_fee_cushion_pct": float(pb.get("breakeven_fee_cushion_pct", 0.4)),
        "breakeven_slip_cushion_pct": float(pb.get("breakeven_slip_cushion_pct", 0.0)),
        "breakeven_fee_aware_enabled": bool(pb.get("breakeven_fee_aware_enabled", False)),
        "cooldown_sl_pct": float(pb.get("cooldown_sl_pct", 4.0)),
        "early_tight_sl_enabled": bool(pb.get("early_tight_sl_enabled", False)),
        "early_tight_sl_minutes": float(pb.get("early_tight_sl_minutes", 30)),
        "early_tight_sl_pct": float(pb.get("early_tight_sl_pct", 1.0)),
        "early_tight_sl_mfe_gate": float(pb.get("early_tight_sl_mfe_gate", 0.0) or 0.0),
        "early_tight_sl_atr_enabled": bool(pb.get("early_tight_sl_atr_enabled", False)),
        "early_tight_sl_atr_mult": float(pb.get("early_tight_sl_atr_mult", 0.5)),
        "early_tight_sl_pct_max": float(pb.get("early_tight_sl_pct_max", 2.5)),
        "min_tp_rr": float(pb.get("min_tp_rr", 1.5)),
        # 步骤③ 三轨 (默认值 = exit_engine 同名 .get 的默认, 保证 config 缺键时口径不分叉)
        "dead_signal_exit_enabled": bool(pb.get("dead_signal_exit_enabled", False)),
        "dead_signal_exit_minutes": float(pb.get("dead_signal_exit_minutes", 60)),
        "dead_signal_exit_mfe_pct": float(pb.get("dead_signal_exit_mfe_pct", 0.2)),
        "dead_signal_reentry_extension_min": float(pb.get("dead_signal_reentry_extension_min", 60)),
        "mfe_stall_exit_enabled": bool(pb.get("mfe_stall_exit_enabled", False)),
        "mfe_stall_exit_minutes": float(pb.get("mfe_stall_exit_minutes", 150)),
        "mfe_stall_exit_mfe_pct": float(pb.get("mfe_stall_exit_mfe_pct", 0.5)),
        "mfe_stall_exit_shallow_hold_enabled": bool(
            pb.get("mfe_stall_exit_shallow_hold_enabled", False)),
        "mfe_stall_exit_shallow_pct": float(pb.get("mfe_stall_exit_shallow_pct", 1.0)),
        "overnight_master_switch": bool(overnight_master_switch_on()),
    }
    _CFG_CACHE = cfg
    return cfg


def cfg_snapshot() -> Dict[str, Any]:
    """配置快照的可落库副本 (去掉大字典, 只留标量阈值 + default 退出参数)。"""
    cfg = load_cfg()
    out = {k: v for k, v in cfg.items() if k != "factor_exit_rules"}
    out["default_exit_cfg"] = dict((cfg.get("factor_exit_rules") or {}).get("default", {}))
    out["rules_replicated"] = list(RULES_REPLICATED)
    out["rules_omitted"] = list(RULES_OMITTED)
    return out


def _fee_cushion_pct(cfg: Dict[str, Any], direction: str, entry_price: float,
                     qty: float) -> float:
    """费用感知保本垫 (对齐 exit_engine D4: 真实往返费/名义额, 算不出回退 yaml 值)。"""
    cushion = cfg["breakeven_fee_cushion_pct"]
    if not cfg["breakeven_fee_aware_enabled"] or qty <= 0 or entry_price <= 0:
        return cushion
    try:
        from app.execution.fee_model import estimate_round_trip
        fee = estimate_round_trip(qty, entry_price, entry_price, direction)["total"]
        return fee / (qty * entry_price) * 100.0
    except Exception:
        return cushion  # 静默回退 (与 exit_engine 同口径: 记账增强不得阻断)


def _round_trip_fee(direction: str, entry_price: float, exit_price: float,
                    qty: float) -> float:
    if qty <= 0 or entry_price <= 0 or exit_price <= 0:
        return 0.0
    try:
        from app.execution.fee_model import estimate_round_trip
        return float(estimate_round_trip(qty, entry_price, exit_price, direction)["total"])
    except Exception:
        return 0.0


def replay_exit(*, direction: str, entry_price: float, entry_hm: str,
                initial_sl: Optional[float], take_profit: Optional[float],
                bars: Sequence[Sequence[Any]], qty: float,
                conditions: Optional[Sequence[str]] = None,
                cohort: str = "A", horizon: str = "short",
                atr_abs: float = 0.0, atr_basis: str = "?",
                decision_id: str = "",
                close_minutes: int = DEFAULT_CLOSE_MINUTES_ET,
                arm_lag_bars: int = 1, sample_source: str = "bar_extreme",
                cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在 bars 上重放一笔仓位, 返回出场与净盈亏。

    Args:
        direction:   "LONG" / "SHORT"
        entry_price: 入场价 (实盘臂=真实成交价; 反事实臂=确认位限价成交价)
        entry_hm:    入场时刻 "HH:MM" (ET)
        initial_sl:  指令初始止损 (绝对价; 两臂同值 — 差值只归因入场方式)
        take_profit: 指令止盈 (绝对价, 可为 0/None)
        bars:        [[time_str, open, high, low, close], ...] 升序
        qty:         股数 (费用与 fee-aware 保本垫的输入)
        conditions/cohort/horizon/atr_abs/atr_basis: 取自 trade_records.metadata_json,
                     决定因子退出参数与早期紧 SL 的 ATR 比例
        decision_id: trade_records.decision_id — 前缀 "intraday_reentry" 时 DEAD_SIGNAL
                     呼吸窗按 Phase0 (8/23) 加宽 dead_signal_reentry_extension_min
        arm_lag_bars: 武装生效滞后根数。1 (默认) = 本根观测到的 peak 从下一根起才武装
                     止损 (订单因果性); 0 = 旧行为, 同根内武装即触发 (乐观上界)。
                     两口径须双报, 见模块头"武装生效滞后"段。
        sample_source: "bar_extreme" (默认, 实盘观测上界) | "bar_close" (下界)。
                     决定 fav/adv 取 bar 极值还是收盘价, 两口径须双报,
                     见模块头"采样口径"段。
    Returns:
        dict: status / exit_price / exit_hm / exit_reason / gross_pnl / fee / net_pnl /
              peak_profit_pct / mfe_pct / mae_pct / held_minutes / bars_used /
              bars_skipped_bad_row / fee_cushion_pct / exit_cfg_name
    """
    cfg = cfg or load_cfg()
    base = {"status": "replayed", "direction": direction, "entry_price": entry_price,
            "entry_hm": entry_hm, "qty": qty, "rules_replicated": list(RULES_REPLICATED),
            "rules_omitted": list(RULES_OMITTED)}
    if not bars:
        return {**base, "status": "no_bars"}
    try:
        entry_min = _hm_to_minutes(entry_hm)
    except Exception:
        return {**base, "status": "bad_entry_hm"}
    if not entry_price or entry_price <= 0 or qty <= 0:
        return {**base, "status": "bad_input"}
    if sample_source not in ("bar_extreme", "bar_close"):
        # 口径名写错必须炸, 不能静默回落 bar_extreme — 否则"双报"会退化成两次同一口径
        raise ValueError(f"sample_source 只允许 bar_extreme/bar_close, 收到 {sample_source!r}")

    from app.playbook.exit_engine import _compute_dynamic_effective_sl, _get_factor_exit_config

    sign = -1.0 if direction == "SHORT" else 1.0
    exit_cfg = _get_factor_exit_config(list(conditions or []), cfg["factor_exit_rules"],
                                       direction=direction)
    be_pct = float(exit_cfg.get("breakeven_at_pct", 0.5))
    fee_cushion = _fee_cushion_pct(cfg, direction, entry_price, qty)
    slip = cfg["breakeven_slip_cushion_pct"]
    # 对齐 exit_engine: _skip_allowed = meta 显式值, 否则 cohort B / long horizon;
    #   隔夜总开关关闭时强制 False (极简版B)。离线无 meta._skip_allowed, 走默认式。
    skip_allowed = (str(cohort) == "B" or str(horizon) == "long") and cfg["overnight_master_switch"]

    sl = float(initial_sl) if initial_sl else 0.0
    tp = float(take_profit) if take_profit else 0.0
    tp_risk = abs(entry_price - sl) if sl > 0 else 0.0
    tp_rr = (abs(tp - entry_price) / tp_risk) if (tp > 0 and tp_risk > 0) else 999.0
    tp_armed = bool(tp > 0 and tp_rr >= cfg["min_tp_rr"])

    # 步骤③ 三轨阈值 (循环外算一次; 全部来自 cfg / exit_cfg, 无平行常量)
    ds_minutes = float(cfg["dead_signal_exit_minutes"])
    ds_ext = float(cfg["dead_signal_reentry_extension_min"])
    if ds_ext > 0 and str(decision_id or "").startswith("intraday_reentry"):
        ds_minutes += ds_ext                      # Phase0 (8/23): reentry 呼吸窗 60→120min
    ds_mfe_pct = float(cfg["dead_signal_exit_mfe_pct"])
    stall_minutes = float(cfg["mfe_stall_exit_minutes"])
    stall_mfe_pct = float(cfg["mfe_stall_exit_mfe_pct"])
    shallow_hold = bool(cfg["mfe_stall_exit_shallow_hold_enabled"])
    shallow_pct = float(cfg["mfe_stall_exit_shallow_pct"])
    time_exit_minutes = float(exit_cfg.get("time_exit_hours", 2.0)) * 60.0
    time_exit_only_if_loss = bool(exit_cfg.get("time_exit_only_if_loss", True))

    peak_price = 0.0
    peak_prev = 0.0        # 上一根**已评估** bar 结束时的 peak_profit_pct = 本根在场止损的依据
    mfe = mae = 0.0
    exit_price = exit_reason = None
    exit_hm = ""
    held = 0
    used = 0
    bad_bars = 0

    for bar in bars:
        try:
            bmin = _hm_to_minutes(bar[0])
            # 开盘价在本重放器里不参与任何判定 (次序=有利极值→SL→TP→收盘), 故不取。
            h, l, c = float(bar[2]), float(bar[3]), float(bar[4])
        except Exception:
            # R5 纪律 (except:pass 棘轮): 坏行不得静默跳过 — 缓存损坏会悄悄缩短重放窗口
            # 并改变出场价, 故计数并随报告透出 (bars_skipped_bad_row>0 = 该笔读数不可信)。
            bad_bars += 1
            continue
        if bmin <= entry_min:
            # 入场那根 1min bar 整根跳过: 成交价发生在 bar 内某一刻, 该 bar 的 high/low
            # 可能全部发生在 fill **之前** — 若据它更新 peak 会把成交前的价格算成本仓
            # 浮盈, 从而假武装保本 (实测 USAR 9/4: db=SL_INITIAL −158.05 被误判成
            # SL_TRAIL_LOCK +11.98)。从下一根起评估 = 保守且对两臂同尺。
            continue
        used += 1
        held = bmin - entry_min
        mfo = bmin - MARKET_OPEN_MINUTES_ET
        mtc = close_minutes - bmin
        # 采样口径 (模块头"采样口径"段): bar_close 下 fav/adv 同为收盘价 = 假设实盘
        #   轮询只看到收盘那一档, 是 10s 轮询真值的**下界**; bar_extreme 是上界。
        if sample_source == "bar_close":
            fav = adv = c
        else:
            fav, adv = (h, l) if direction == "LONG" else (l, h)

        # ① peak tracking (有利极值) — 与 exit_engine 同式
        if direction == "LONG":
            if fav > peak_price:
                peak_price = fav
        else:
            if peak_price == 0 or fav < peak_price:
                peak_price = fav
        peak_profit_pct = (peak_price - entry_price) / entry_price * 100.0 * sign
        mfe = max(mfe, peak_profit_pct)
        cur_pct = (adv - entry_price) / entry_price * 100.0 * sign
        mae = min(mae, cur_pct)

        # ② effective SL (SSOT 纯函数: 保本 ratchet + 回撤锁利 + tighten_sl_by_beta)
        #   peak_inforce = 本根 bar 期间"已在场"的武装依据 (订单因果性, 见模块头)。
        #   arm_lag_bars=0 时退化为旧行为 (peak_inforce == peak_profit_pct)。
        peak_inforce = peak_prev if arm_lag_bars >= 1 else peak_profit_pct
        eff_sl = _compute_dynamic_effective_sl(direction, entry_price, sl, peak_inforce,
                                               exit_cfg, fee_cushion, slip)
        be_triggered = peak_inforce >= be_pct

        # ③ 规则0 开盘冷却放宽 (已武装保本豁免放宽)
        in_cooldown = mfo < 15
        if in_cooldown and not be_triggered:
            cool_sl = entry_price * (1 - cfg["cooldown_sl_pct"] / 100.0) if direction == "LONG" \
                else entry_price * (1 + cfg["cooldown_sl_pct"] / 100.0)
            active_sl = min(eff_sl, cool_sl) if direction == "LONG" else max(eff_sl, cool_sl)
        else:
            active_sl = eff_sl

        # ④ 规则0.5 早期紧 SL (含 ATR 比例 clamp 与 mfe_gate)
        if (cfg["early_tight_sl_enabled"] and not in_cooldown and not be_triggered
                and held <= cfg["early_tight_sl_minutes"]
                and (cfg["early_tight_sl_mfe_gate"] <= 0
                     or peak_inforce < cfg["early_tight_sl_mfe_gate"])):
            et_pct = cfg["early_tight_sl_pct"]
            if cfg["early_tight_sl_atr_enabled"] and atr_abs > 0 and entry_price > 0:
                atr_pct = atr_abs / entry_price * 100.0
                cand = max(et_pct, min(cfg["early_tight_sl_pct_max"],
                                       cfg["early_tight_sl_atr_mult"] * atr_pct))
                if cand > et_pct:
                    et_pct = cand
            et_sl = entry_price * (1 - et_pct / 100.0) if direction == "LONG" \
                else entry_price * (1 + et_pct / 100.0)
            active_sl = max(active_sl, et_sl) if direction == "LONG" else min(active_sl, et_sl)

        # ⑤ 规则1 SL (最高优先级) — 成交价取 bar 不利极值 (对齐 exit_engine current_price 语义)
        if active_sl > 0 and ((direction == "LONG" and adv <= active_sl)
                              or (direction == "SHORT" and adv >= active_sl)):
            cls_pad = fee_cushion + slip
            be_line = entry_price * (1 + cls_pad / 100.0) if direction == "LONG" \
                else entry_price * (1 - cls_pad / 100.0)
            be_tol = entry_price * 1e-4
            if not be_triggered:
                exit_reason = "SL_INITIAL"
            elif (direction == "LONG" and eff_sl > be_line + be_tol) or \
                 (direction == "SHORT" and 0 < eff_sl < be_line - be_tol):
                exit_reason = "SL_TRAIL_LOCK"
            else:
                exit_reason = "SL_BREAKEVEN"
            # 成交价口径 (对齐实盘订单类型, 8/20 方案A):
            #   保本/锁利已武装 → monitor 挂**限价单@净保本线**, "成交价物理上 ≥(空:≤)
            #     此线 = 净保本由订单类型保证" ⇒ 填充价取 active_sl (限价地板), 不取 bar
            #     极值 — 否则 1min bar 的下影线会把有限价保护的出场打成无保护的市价滑点
            #     (实测该口径修正前 SL_TRAIL_LOCK 复刻率仅 16.7%, 净偏差 −65.82/笔)。
            #   保本未武装 (SL_INITIAL, 真止损) → 无限价保护, 取 bar 不利极值 (保守,
            #     对齐 exit_engine 7/16 P0 "实际成交价可能远低于 SL" 的语义)。
            exit_price = active_sl if be_triggered else adv
            exit_hm = str(bar[0])[11:16]
            break

        # ⑥ 规则1.5 TAKE_PROFIT (RR 门禁)
        if tp_armed and ((direction == "LONG" and fav >= tp)
                         or (direction == "SHORT" and fav <= tp)):
            exit_reason, exit_price, exit_hm = "TAKE_PROFIT", fav, str(bar[0])[11:16]
            break

        # ⑦ 规则3 PRE_CLOSE_EXIT (cohort B / long horizon 盈利仓跳过)
        #   cur_close_pct = 本根收盘价口径的浮动盈亏 — 时间类规则 (⑦⑧⑨⑩) 一律用它判
        #   "当前水下", 因为实盘那几条的 current_price 就是巡检时刻的市价, 不是 bar 极值。
        cur_close_pct = (c - entry_price) / entry_price * 100.0 * sign
        if 0 <= mtc <= 8 and mfo > 0:
            if not (skip_allowed and cur_close_pct > 0):
                exit_reason, exit_price, exit_hm = "PRE_CLOSE_EXIT", c, str(bar[0])[11:16]
                break

        # ⑧ 规则3.3 DEAD_SIGNAL_EXIT (8/08 毕业; 实盘 exit_engine.py:891, 序在 PRE_CLOSE 之后)
        #   语义: 到点仍未兑现方向 (生命周期 peak 从未达 ds_mfe_pct) → 承认信号失效, 收盘价离场。
        #   peak 用**观测**口径 (含本根有利极值), 不用 peak_inforce — arm_lag 约束的是"哪张止损
        #   单在场", 而本条不下止损单, 它是巡检时刻的一次状态判定 (见模块头"武装生效滞后"段)。
        #   未复刻: W_WEEK SIM 水下仓豁免 (_loser_overnight_exempt 需跨日 bar + 墙钟) ⇒ 共模缺失。
        if (cfg["dead_signal_exit_enabled"] and held >= ds_minutes
                and peak_profit_pct < ds_mfe_pct):
            exit_reason, exit_price, exit_hm = "DEAD_SIGNAL_EXIT", c, str(bar[0])[11:16]
            break

        # ⑨ 规则3.4 MFE_STALL_EXIT (7/24; 实盘 exit_engine.py:932) — 三重条件防误杀:
        #   时间 × 峰值从未达标 × 当前水下。浅亏转持有分支 (8/04 裁) 与实盘同式: 亏损浅于
        #   shallow_pct 时**不出场**, 后续 bar 继续走 SL/PRE_CLOSE 链 (实盘靠 meta 标记只
        #   免重复打日志, 行为上等价于每轮巡检重新浅亏持有, 故离线无需状态位)。
        if (cfg["mfe_stall_exit_enabled"] and held >= stall_minutes
                and peak_profit_pct < stall_mfe_pct and cur_close_pct < 0):
            if not (shallow_hold and cur_close_pct > -shallow_pct):
                exit_reason, exit_price, exit_hm = "MFE_STALL_EXIT", c, str(bar[0])[11:16]
                break

        # ⑩ 规则4 TIME_EXIT (7/7 恢复; 实盘 exit_engine.py:972) — 严格大于, 且亏损门用**毛**
        #   口径 (与实盘同式: 切净口径会把微盈仓市价卖=白跑)。实盘的隔夜日重置 (RFC-064)
        #   与 W_WEEK 豁免需跨日 bar ⇒ 共模缺失, 单日重放 held 即日内分钟数。
        if held > time_exit_minutes and (not time_exit_only_if_loss or cur_close_pct < 0):
            exit_reason, exit_price, exit_hm = "TIME_EXIT", c, str(bar[0])[11:16]
            break

        # 本根评估完毕 ⇒ 它观测到的 peak 从**下一根**起才成为在场止损的武装依据
        peak_prev = peak_profit_pct

    if exit_reason is None:
        try:
            last = bars[-1]
            exit_price, exit_hm = float(last[4]), str(last[0])[11:16]
        except Exception:
            return {**base, "status": "bad_bars"}
        exit_reason = "EOD_REPLAY"

    gross = (exit_price - entry_price) * qty * sign
    fee = _round_trip_fee(direction, entry_price, exit_price, qty)
    return {**base, "exit_price": round(exit_price, 4), "exit_hm": exit_hm,
            "exit_reason": exit_reason, "gross_pnl": round(gross, 2),
            "fee": round(fee, 2), "net_pnl": round(gross - fee, 2),
            "peak_profit_pct": round(peak_price and (peak_price - entry_price) / entry_price * 100.0 * sign or 0.0, 4),
            "mfe_pct": round(mfe, 4), "mae_pct": round(mae, 4),
            "held_minutes": held, "bars_used": used,
            "bars_skipped_bad_row": bad_bars,
            "arm_lag_bars": int(arm_lag_bars),
            "sample_source": sample_source,
            "fee_cushion_pct": round(fee_cushion, 4),
            "tp_rr": round(tp_rr, 3), "tp_armed": tp_armed,
            "exit_cfg": {k: exit_cfg.get(k) for k in
                         ("breakeven_at_pct", "drawdown_from_peak_pct", "trailing_enabled",
                          "time_exit_hours", "time_exit_only_if_loss", "max_hold_days")},
            "dead_signal_minutes_effective": round(ds_minutes, 1),
            "skip_allowed": skip_allowed}


def fidelity_report(pairs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """保真度报告: 重放器跑**实盘入场**的结果 vs trade_records 真值。

    这是重放器可信度的唯一凭据 — 不报保真度的反事实数字不可判读 (承 ADR-035
    「价格条件下界」自认口径 + da_score_overwrite_counterfactual 的保真校验先例)。

    Args:
        pairs: [{"replay_reason","replay_net","db_reason","db_net","same_day",
                 "arm_lag_bars","sample_source"}, ...]  (后两项为口径标签, 可缺省)
    Returns:
        dict: n / reason_match_rate / net_bias (重放−真值均值) / net_mae /
              by_db_reason (逐个实盘出场原因的可复刻率与偏差) / n_cross_day /
              **当日域切分** (n_same_day / match_rate_same_day / match_rate_cross_day /
              ceiling_cross_day) — 单日重放对"实盘次日才出场"的仓物理上不可复刻,
              故当日域才是本重放器的**可判读域**; ceiling_cross_day = 全窗数学上限 /
              **钱口径三指标** (sign_agreement_rate / bias_to_mae_ratio /
              mae_over_median_abs_net) — 与 reason_match_rate 并列, 各回答不同问题,
              不得互相替换 (判别力实证见下方注释)。
    """
    rows = [p for p in pairs if p.get("db_net") is not None]
    if not rows:
        return {"n": 0, "note": "无可校验样本 (当日零成交或 trade_records 无净额)"}
    n = len(rows)
    match = sum(1 for p in rows if p.get("replay_reason") == p.get("db_reason"))
    diffs = [float(p["replay_net"]) - float(p["db_net"]) for p in rows
             if p.get("replay_net") is not None]
    by: Dict[str, Dict[str, Any]] = {}
    for p in rows:
        key = str(p.get("db_reason") or "?")
        b = by.setdefault(key, {"n": 0, "match": 0, "net_bias_sum": 0.0, "cnt_bias": 0})
        b["n"] += 1
        b["match"] += int(p.get("replay_reason") == p.get("db_reason"))
        if p.get("replay_net") is not None:
            b["net_bias_sum"] += float(p["replay_net"]) - float(p["db_net"])
            b["cnt_bias"] += 1
    for b in by.values():
        b["match_rate"] = round(b["match"] / b["n"], 3) if b["n"] else None
        b["net_bias"] = round(b["net_bias_sum"] / b["cnt_bias"], 2) if b["cnt_bias"] else None
        b.pop("net_bias_sum", None)
        b.pop("cnt_bias", None)
    sd = [p for p in rows if p.get("same_day") is not False]
    xd = [p for p in rows if p.get("same_day") is False]

    def _mr(rs):
        return (round(sum(1 for p in rs if p.get("replay_reason") == p.get("db_reason"))
                      / len(rs), 3) if rs else None)

    net_sd = [float(p["replay_net"]) - float(p["db_net"]) for p in sd
              if p.get("replay_net") is not None]

    # ── 钱口径三指标 (2026-09-07 判据换轨; 为什么必须与标签一致率**并列**而不是替换) ──
    #   实测判别力: 烂窗 7/11–8/14 (跨参数纪年) 标签一致率仅 0.384/0.444, 但钱口径
    #   |bias|/MAE=0.054/0.009、符号一致 0.852/0.875 ⇒ **钱口径在烂窗也过门 = 无判别力**。
    #   反过来标签门对"经济等价标签"(SL_BREAKEVEN vs SL_TRAIL_LOCK, 钱差中位仅 $2.47) 过严。
    #   ⇒ 两个都要, 各自回答不同问题: 钱口径回答"聚合/配对差能不能用", 标签口径回答
    #   "出场路径归因能不能用"。分级见 cf_pullback_limit_replay.summarize_v2 的 ruler_license。
    money = [p for p in rows if p.get("replay_net") is not None]
    m_diffs = [float(p["replay_net"]) - float(p["db_net"]) for p in money]
    m_mae = (sum(abs(d) for d in m_diffs) / len(m_diffs)) if m_diffs else None
    m_bias = (sum(m_diffs) / len(m_diffs)) if m_diffs else None
    abs_nets = sorted(abs(float(p["db_net"])) for p in money)
    med_abs_net = abs_nets[len(abs_nets) // 2] if abs_nets else None
    sign_ok = sum(1 for p in money if (float(p["replay_net"]) > 0) == (float(p["db_net"]) > 0))
    # 零误差不得因除零退化成 None: bias_to_mae_ratio 的 None 在许可卡里是 fail-closed
    #   (=不许可), 于是"完美复刻"反而被判成不可用。MAE=0 ⇒ 偏差必为 0 ⇒ 比值定义为 0。
    if m_mae:
        m_ratio = round(abs(m_bias) / m_mae, 3) if m_bias is not None else None
    else:
        m_ratio = 0.0 if m_diffs else None
    return {
        "n": n,
        "reason_match_rate": round(match / n, 3),
        "net_bias": round(sum(diffs) / len(diffs), 2) if diffs else None,
        "net_mae": round(sum(abs(d) for d in diffs) / len(diffs), 2) if diffs else None,
        # ── 当日域切分 (2026-09-07 步骤③): 跨日仓是真值路径不可得, 不是重放器不努力 ──
        # 钱口径 (聚合/配对可用性)
        "sign_agreement_rate": round(sign_ok / len(money), 3) if money else None,
        "bias_to_mae_ratio": m_ratio,
        "median_abs_db_net": round(med_abs_net, 2) if med_abs_net is not None else None,
        # 单笔可判读性: >1 ⇒ 尺子的单笔误差**大于**该笔自身的盈亏量级 ⇒ 逐笔归因不可用
        "mae_over_median_abs_net": (round(m_mae / med_abs_net, 2)
                                    if (m_mae is not None and med_abs_net) else None),
        "n_same_day": len(sd),
        "match_rate_same_day": _mr(sd),
        "net_bias_same_day": round(sum(net_sd) / len(net_sd), 2) if net_sd else None,
        "net_mae_same_day": round(sum(abs(d) for d in net_sd) / len(net_sd), 2) if net_sd else None,
        "match_rate_cross_day": _mr(xd),
        "ceiling_cross_day": round(len(sd) / n, 3),
        "n_cross_day": len(xd),
        "by_db_reason": dict(sorted(by.items(), key=lambda kv: -kv[1]["n"])),
        "note": ("net_bias<0 = 重放比真值保守 (SL_INITIAL 取 bar 不利极值 + 未复刻 "
                 "OVERNIGHT_GAP/MAX_HOLD/耐心出场/隔夜豁免); 该偏差同时作用于两臂, "
                 "在配对差里共模抵消。DEAD_SIGNAL/MFE_STALL/TIME_EXIT 已于步骤③ 复刻, "
                 "但只读**现行** config ⇒ 跨参数纪年的窗存在数学上限, 见模块头"
                 "「参数纪年错位」段"),
    }
