"""Playbook 退出引擎 — 自管退出规则链 (P2a 纯搬家, 2026-09-06).

本模块全部代码逐字迁自 app/playbook/pipeline.py (9453 行单体), 零行为变更:
  - _check_playbook_exit_rules  (711 行, 规则[0]冷却/[0.5]早期紧SL/[1]SL/[1.5]TP/
    [2]OVERNIGHT_GAP_STOP/[3]PRE_CLOSE/[3.4]MFE_STALL/[4]TIME_EXIT/[5]MAX_HOLD)
  - 其私有 helper: _is_overnight_position / _get_factor_exit_config (+_factor_exit_warned)
    / _compute_dynamic_effective_sl / _load_horizon_config
  - 进程级日志去重集 _EARLY_TIGHT_SL_LOGGED / _LOSER_DS_EXEMPT_LOGGED
  - P7 埋点缝 _EARLY_DEAD_SHADOW_PATH (tests/conftest.py 登记已同步迁至本模块)

隔夜豁免判定不在此复制: EOD 单点走 app.playbook.overnight_decision (P1),
模块顶部以同名别名引入, 使下方迁入函数体保持逐字不变。
pipeline.py 侧保留同名转发器 (外部调用点/既有测试 import 路径不变)。

回滚: git revert 本提交即可 (pipeline 转发器与本体并存, 无双写)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict

from app.playbook.overnight_decision import (
    horizon_max_hold_hit as _horizon_max_hold_hit,
    overnight_eod_exempt as _overnight_eod_exempt,
    w_week_loser_exempt as _loser_overnight_exempt,
)

logger = logging.getLogger(__name__)

# P7 埋点缝 (值与解析语义逐字不变; 隔离登记在 tests/conftest.py _PRODUCTION_DATA_SEAMS)
_EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl")

# 惰性 ET 时区访问 (与 pipeline._now_et 同模式; SSOT = app.core.scheduler_triggers.ET)
_ET = None


def _now_et():
    global _ET
    if _ET is None:
        from app.core.scheduler_triggers import ET as _et
        _ET = _et
    return _ET


def _is_overnight_position(entry_time_str: str) -> bool:
    """RFC-061 #6: 仓位是否隔夜 (entry 在之前的交易日, 非今日入场).

    用于隔夜深亏快平 (OVERNIGHT_GAP_STOP): 持仓过夜 且 亏损 ≥ 阈值 即快平。
    2026-09-24 勘误: 名称里的 "GAP" 是历史命名 — 判据里没有 gap 校验, 开盘跳空只是其中一种触发。
    用 ET 日期判断. 异常返回 False (不触发, 保守).
    """
    if not entry_time_str:
        return False
    try:
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        return _entry_date < datetime.now(_et).date()
    except Exception:
        return False



# 2026-07-19 (DEB-round3 #6-A): factor_exit 缺规则 WARN 去重 (进程级, 防刷屏)
_factor_exit_warned = set()


def _get_factor_exit_config(conditions: list, factor_rules: dict, direction: str = None) -> dict:
    """根据触发因子选择对应的退出规则配置 (Phase 3 2026-06-30).

    7/7 fix: 方向感知匹配。BUY 优先匹配 BULLISH_CONDITIONS, SHORT 优先匹配 BEARISH_CONDITIONS。
    避免 SHORT 指令因第一个 condition 是趋势类而配上 trailing=true 的退出策略。

    Args:
        conditions: 触发条件列表 (e.g. ['insider_cluster_buy', 'above_ma20'])
        factor_rules: playbook_horizon.yaml 顶层 factor_exit_rules 字典
        direction: "LONG"/"SHORT" — 方向感知匹配 (None 时退回原始逻辑)

    Returns:
        匹配到的因子退出规则, 未命中返回 default
    """
    default = factor_rules.get("default", {})
    if not conditions:
        return default

    # 7/7 fix: 方向感知 — 优先匹配与方向一致的 condition
    if direction:
        from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS
        _preferred = BULLISH_CONDITIONS if direction == "LONG" else BEARISH_CONDITIONS
        # 第一轮: 方向一致 + 有 exit_rule 的 condition
        for cond in conditions:
            if cond in factor_rules and cond in _preferred:
                return factor_rules[cond]

    # 第二轮: 方向一致的没匹配到, 退回第一个有 exit_rule 的 condition
    # P0-2 fix (2026-07-20): 跳过方向相反的 condition — 原逻辑第二轮无方向过滤,
    #   SHORT 指令的 bullish conditions (如 macd_golden_cross) 会配上 trailing=true 规则,
    #   与 7/7 fix 注释目标矛盾. 修复: 第二轮仍排除方向相反因子, 无命中则落 default.
    for cond in conditions:
        if cond in factor_rules:
            if direction:
                from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS
                _opposite = BEARISH_CONDITIONS if direction == "LONG" else BULLISH_CONDITIONS
                if cond in _opposite:
                    continue
            return factor_rules[cond]

    # 2026-07-19 (DEB-round3 #6-A): conditions 非空但零命中 → WARN (去重防刷屏).
    #   根因: 新因子进了 conditions 但未配 factor_exit_rules → 静默落 default,
    #   退出行为与设计意图漂移而无人察觉. 只告警不改行为 (default 兜底不变).
    _unregistered = [c for c in conditions if c not in factor_rules]
    if _unregistered:
        _wkey = tuple(sorted(str(c) for c in _unregistered))
        if _wkey not in _factor_exit_warned:
            _factor_exit_warned.add(_wkey)
            logger.warning(
                f"[playbook] #6-A factor_exit 缺规则: conditions={_unregistered} 未注册 "
                f"factor_exit_rules, 落 default 退出 (direction={direction}) — "
                f"请在 playbook_horizon.yaml 补配或确认有意为之"
            )

    return default



def _compute_dynamic_effective_sl(
    direction: str,
    entry_price: float,
    initial_sl: float,
    peak_profit_pct: float,
    exit_cfg: dict,
    fee_cushion_pct: float = 0.0,
    slip_cushion_pct: float = 0.0,
    adaptive_drawdown_pct: float | None = None,
) -> float:
    """根据因子退出规则计算动态 effective stop loss.

    逻辑:
    - breakeven_at_pct: 浮盈达到该阈值, SL 提到 entry (保本)
    - drawdown_from_peak_pct: 从峰值回撤超过该百分比时, SL锁定到 peak×(1-drawdown)
      2026-07-09: 替代原步进trailing. 原trailing_step=2%但MFE中位数仅1.5%, 永远触发不了.
      新逻辑: 浮盈2%→峰值, 回撤0.6%→SL锁在peak-0.6%=1.4%利润. 高波动美股也能落袋.
    - 兼容: 若无drawdown_from_peak_pct配置, 回退原步进逻辑(最多3步).
    """
    breakeven_pct = float(exit_cfg.get("breakeven_at_pct", 0.5))
    # 2026-08-08 (DEB-20260808 §17.3-2 + §22 审计修正): SL 收紧系数 sl_tighten_beta
    #   β=0.20 被审计否决: 收紧后 SL 0.6-1.2% 全落 7/9 证伪的 <2% 噪声扫损区 (WR47% 亏$1352).
    #   修正 β=0.50 + 下限 1.5%: 收紧后 SL 1.5-2.5%, 贴近 7/9 最优区 (2-3% WR59%) 下沿,
    #   保留 ~1/3 模拟改善 (双臂对照 β0.5 +$596 vs β0.2 +$1555), 风险大幅下降.
    #   回滚: sl_tighten_beta 改 1.0.
    # 2026-08-14 (止损可见性修复): 公式收归 config.tighten_sl_by_beta 单一真源,
    #   成交推送展示同一公式算有效止损 (此前展示价≠生效价, UCTT 案).
    from app.playbook.config import tighten_sl_by_beta as _tighten_sl_by_beta
    initial_sl = _tighten_sl_by_beta(direction, entry_price, initial_sl)
    effective_sl = initial_sl

    if peak_profit_pct < breakeven_pct:
        return effective_sl

    # 浮盈超过 breakeven: 先保本
    # 2026-07-17: 净保本 — 保本线含手续费缓冲 (成本价 ± fee_cushion), 确保被保本SL打掉时不亏双边费用.
    # 2026-08-20 (方案A, 用户裁"同意先方案A"): + slip_cushion 滑点垫 — 触发线在费用线之上再垫一截.
    #   8/20 开盘实弹 6 笔 BE 出场 3:3: 限价成交 3 笔全净正 / 8s 超时降级市价 3 笔全净负
    #   (MU −24.42 / WPM −12.14 / AU −3.80) — cushion 只垫费用(~7.5bps), 降级滑点 21-43bps 兜不住.
    #   几何: 触发线抬高后, 触发时现价 > 限价单(纯费用线, 见 _be_net_line_price) →
    #   限价卖单从被动等回摆翻转为主动穿越立即成交, 成交地板仍=费用线(订单类型保证).
    #   代价: 提前 0.2% 出场 (换确定性). 约束: slip < breakeven_at_pct 否则武装即触发.
    #   回滚: breakeven_slip_cushion_pct 改 0.
    _be_pad = fee_cushion_pct + slip_cushion_pct
    if direction == "LONG":
        _be_line = entry_price * (1 + _be_pad / 100.0)
        effective_sl = max(effective_sl, _be_line)
    else:
        _be_line = entry_price * (1 - _be_pad / 100.0)
        effective_sl = min(effective_sl if effective_sl > 0 else _be_line, _be_line)

    if not exit_cfg.get("trailing_enabled", False):
        # 2026-07-18 profit_override (三轮辩论裁决): 浮盈超过阈值时强制启用锁利.
        #   根因: trailing_enabled=false 的因子(17个"快速证伪"类)只有保本没有锁利,
        #   浮盈 3.89% 的 COIN SHORT 最终保本出场, 白送 3.9% 利润.
        #   修复: 浮盈 >= 1.5% (3×breakeven) 时, 不管因子配置, 强制启用回撤锁利.
        #   安全: 0-1.5% 证伪期行为不变, 设计意图兼容.
        #   回滚: 设 profit_override_threshold_pct=999 即禁用.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_override
            _po_cfg = _get_pb_cfg_override()
            _po_threshold = float(_po_cfg.get("profit_override_threshold_pct", 1.5))
            _po_dd = float(_po_cfg.get("profit_override_drawdown_pct", 0.5))
        except Exception:
            _po_threshold = 1.5
            _po_dd = 0.5
        if peak_profit_pct < _po_threshold:
            return effective_sl
        # 浮盈已大, 强制锁利: SL = peak × (1 - dd%), 但不低于保本线
        if direction == "LONG":
            _po_peak = entry_price * (1 + peak_profit_pct / 100)
            _po_sl = _po_peak * (1 - _po_dd / 100)
            effective_sl = max(effective_sl, _po_sl)
        else:
            _po_peak = entry_price * (1 - peak_profit_pct / 100)
            _po_sl = _po_peak * (1 + _po_dd / 100)
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, _po_sl)
        return effective_sl

    # ── 回撤止损 (新逻辑, 2026-07-09) ──
    drawdown_pct = (
        float(adaptive_drawdown_pct)
        if adaptive_drawdown_pct is not None
        else float(exit_cfg.get("drawdown_from_peak_pct", 0))
    )
    if drawdown_pct > 0 and peak_profit_pct > breakeven_pct:
        # SL = peak_price × (1 - drawdown_pct/100), 但不低于保本
        if direction == "LONG":
            peak_price = entry_price * (1 + peak_profit_pct / 100)
            dd_sl = peak_price * (1 - drawdown_pct / 100)
            effective_sl = max(effective_sl, dd_sl)
        else:
            peak_price = entry_price * (1 - peak_profit_pct / 100)
            dd_sl = peak_price * (1 + drawdown_pct / 100)
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, dd_sl)
        return effective_sl

    # ── 回退: 原步进 trailing (兼容旧配置) ──
    trailing_step = float(exit_cfg.get("trailing_step_pct", 2.0))
    if trailing_step > 0:
        for step in range(1, 4):  # 最多 3 步
            threshold = breakeven_pct + trailing_step * step
            if peak_profit_pct < threshold:
                break
            locked_pct = breakeven_pct + trailing_step * (step - 1) + trailing_step * 0.5
            if direction == "LONG":
                effective_sl = max(effective_sl, entry_price * (1 + locked_pct / 100))
            else:
                effective_sl = min(effective_sl if effective_sl > 0 else entry_price,
                                 entry_price * (1 - locked_pct / 100))

    return effective_sl



# 规则0.5 审计日志去重集 (key: "symbol:date") — 见 _check_playbook_exit_rules 规则0.5 段.
# 进程级, 每 symbol每日 只记一次紧SL生效事件, 防止 monitor 轮询刷屏.
_EARLY_TIGHT_SL_LOGGED: set = set()

# 规则3.3 W_WEEK 豁免日志去重集 (key: "symbol:date") — 见 DEAD_SIGNAL 块 (2026-08-16 方案B).
# 进程级, 每 symbol每日 只记一次豁免事件, 防止 monitor 轮询刷屏.
_LOSER_DS_EXEMPT_LOGGED: set = set()



def _load_horizon_config(horizon: str) -> Dict[str, Any]:
    """读取指定 horizon 的配置参数"""
    try:
        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()
        return pb_cfg.get_horizon_params(horizon)
    except Exception as e:
        logger.debug(f"[playbook] 读取 horizon 配置失败: {e}, 使用默认")
        return {}



def cohort_b_skip_eligible(cohort: str, source_type: str) -> bool:
    """名义隔夜资格单点 (P2b, 2026-09-06): cohort B 且 source 允许 (LIVE 需显式开).

    合并前 poll monitor 与 EOD 兜底 sweeper 两处逐字相同的公式
    `cohort == "B" and cohort_b_skip_allowed(src)`。异常不吞 (与合并前 inline 一致:
    env/store 读错沿调用栈上抛, 各自既有的 except 链兜底口径不变)。
    """
    from app.storage.trade_store import cohort_b_skip_allowed
    return bool(cohort == "B" and cohort_b_skip_allowed(source_type))


def overnight_master_switch_on() -> bool:
    """隔夜总开关 (极简版B 2026-07-17): 关闭时 EOD 盈利跳过与隔夜豁免全链失效.

    合并前退出引擎与 EOD sweeper 两处逐字相同的
    `get_playbook_config().get("overnight_hold_enabled", False)`。异常不吞。
    """
    from app.playbook.config import get_playbook_config
    return bool(get_playbook_config().get("overnight_hold_enabled", False))


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str):
    """共用退出规则判断 — 被 monitor_playbook_sim_exits 调用.

    计算盈利百分比 → peak tracking → breakeven ratchet / trailing stop (factor-specific) → 6 条退出规则.
    退出规则 (按优先级):
      [0] 开盘冷却期: 前15min SL放宽到4% (防开盘噪声秒杀)
      [0.5] 早期紧SL: 入场后30min内未浮盈达标 → SL收紧到1% (防方向全错慢性失血)
      [1] SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK: effective_sl 触及 (按初始止损/保本/锁利区分)
      [1.5] TAKE_PROFIT: 触及止盈价 (RR≥1.5 才生效)
      [2] OVERNIGHT_GAP_STOP (名不副实, 2026-09-24 勘误): 持仓过夜 且 亏损 ≥
          overnight_gap_stop_pct 的快平 —— **无 gap 校验**, 开盘跳空只是其中一种触发方式
          (metadata 也不落 gap 字段, 复盘不要去查 gap%)
      [3] PRE_CLOSE_EXIT: 收盘前 8min (cohort B 盈利可跳过)
      [3.4] MFE_STALL_EXIT: 持仓≥150min 且 峰值浮盈<0.5% 且 当前水下 → 提前离场 (2026-07-24)
      [4] TIME_EXIT: 持仓 > factor_time_exit_hours 且 盈利<0 (factor-specific;
          隔夜仓按"今日开盘以来"计, 每日重置 — RFC-064)
      [5] MAX_HOLD_T5: cohort B 持仓≥5 交易日

    Args:
        direction: "LONG"/"SHORT"
        entry_price: 入场价
        current_price: 当前报价
        initial_sl: 初始 stop_loss (meta["stop_loss"])
        peak_price: 历史峰值价 (从 meta 恢复, 函数内更新后返回)
        meta: metadata dict (读取 cohort/_skip_allowed/_log_tag/_symbol/conditions)
        held_minutes: 持仓分钟数
        minutes_to_close: 距收盘剩余分钟 (ET)
        minutes_from_open: 开盘后分钟数 (ET)
        entry_time_str: entry_time ISO 字符串

    Returns: (exit_reason, exit_price, peak_price, breakeven_triggered)
        exit_reason 为 "" 表示不退出; 调用方负责 peak_price metadata 写回.
    """
    _log_tag = meta.get("_log_tag", "playbook")
    _symbol = meta.get("_symbol", "")

    # ── 盈利百分比计算 ──
    if direction == "LONG":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    # ── peak tracking (从参数恢复, 更新后返回) ──
    if direction == "LONG":
        if current_price > peak_price:
            peak_price = current_price
    else:
        if peak_price == 0 or current_price < peak_price:
            peak_price = current_price

    if peak_price > 0:
        if direction == "LONG":
            peak_profit_pct = (peak_price - entry_price) / entry_price * 100
        else:
            peak_profit_pct = (entry_price - peak_price) / entry_price * 100
    else:
        peak_profit_pct = 0

    # ── 读取 horizon 配置 + 因子退出规则 (Phase 3 2026-06-30) ──
    _cohort = meta.get("cohort", "A")
    _horizon = meta.get("horizon", "short")
    # _skip_allowed: 盈利仓跳过 EOD 的条件
    _skip_allowed = meta.get("_skip_allowed", _cohort == "B" or _horizon == "long")
    # 极简版B (2026-07-17): 隔夜总开关关闭时强制不跳过 EOD/PRE_CLOSE → 全部当日平仓.
    #   一处收口: 失效 OVERNIGHT_GAP_STOP / PRE_CLOSE_EXIT 盈利仓 pass / MAX_HOLD 三个隔夜分支.
    if not overnight_master_switch_on():
        _skip_allowed = False
    _horizon_cfg = _load_horizon_config(_horizon)
    _overnight_gap_stop_pct = _horizon_cfg.get("overnight_gap_stop_pct", 3.0)

    from app.playbook.config import get_playbook_config
    from app.playbook.adaptive_trailing import (
        adaptive_trailing_drawdown_pct,
        adaptive_trailing_snapshot,
    )
    _pb_cfg = get_playbook_config()
    _factor_exit_rules = _pb_cfg.get_horizon_top("factor_exit_rules", {})
    _default_exit_cfg = _factor_exit_rules.get("default", {})

    # 改动9 [已退役 2026-07-11]: exit_param_override.json 合并 — 测量优先重构下移除.
    #   根因: 消费端只匹配 TIME_EXIT, 但 Hermes feedback_loop 生成端按 direction+exit_reason
    #   分组【混合 v1+v2】全样本, 可能产出 LONG+TIME_EXIT 从而【静默篡改】factor_exit_rules —
    #   典型"打影子"(用旧策略数据硬调新策略退出参数). 此处移除消费端后影响链已切断,
    #   生成的 json 现无人读取(惰性). 生成端在 Hermes skill (工作区外, feedback_loop.py:348/439),
    #   需手动注掉那两行调用才能彻底停写. 参数重设留到 Section 5 启用门槛满足后、在 v2 数据上另起执行阶段.
    #   (原合并逻辑见 git 历史; 此处保持 no-op, factor_exit_rules 仅由 yaml 决定.)

    # 根据触发因子选择退出规则 (7/7: 传 direction 做方向感知匹配)
    _conditions = meta.get("conditions", [])
    _exit_cfg = _get_factor_exit_config(_conditions, _factor_exit_rules, direction=direction)

    # 最大持有天数: 优先因子级, 其次 horizon 级
    _max_hold_days = int(
        _exit_cfg.get(
            "max_hold_days",
            _horizon_cfg.get("max_hold_days", 5 if _horizon == "long" else 1),
        )
    )

    # ── effective SL (factor-specific breakeven ratchet + trailing stop) ──
    # Phase 3 (2026-06-30): 按因子类型动态调整利润锁定/跟踪止损
    # 2026-09-09 (行动项C): 回退默认值收敛 BE_FALLBACK_FEE_CUSHION_PCT (原 0.0,
    #   与 yaml 旧值 0.4 叠加 slip 0.2 = 0.6 > be 0.4 破几何同族; 单一真源防再漂移)。
    from app.playbook.config import BE_FALLBACK_FEE_CUSHION_PCT as _BE_FEE_FB
    _fee_cushion_pct = float(_pb_cfg.get("breakeven_fee_cushion_pct", _BE_FEE_FB))
    # 2026-07-30 (DEB-fee-net-pnl D4, 用户裁"直接实现"): 费用感知保本线 — 拍的 0.15%
    #   换真实往返费用/名义额 (真成本由 $/股固定佣金主导: 小仓 cushion 低估→假保本净亏,
    #   大仓高估→锁利线虚高)。同一 _fee_cushion_pct 变量下游三处 (ratchet/退出归类/
    #   be_shadow B 锚) 自动同口径。失败/无股数 → 回退旧 cushion; 回滚=关 yaml 开关。
    if _pb_cfg.get("breakeven_fee_aware_enabled", False):
        try:
            _d4_qty = float(meta.get("_qty", 0) or 0)
            if _d4_qty > 0 and entry_price > 0:
                from app.execution.fee_model import estimate_round_trip as _d4_rt
                _d4_fee = _d4_rt(_d4_qty, entry_price, entry_price, direction)["total"]
                _fee_cushion_pct = _d4_fee / (_d4_qty * entry_price) * 100.0
                meta["_be_cushion_fee_aware"] = round(_fee_cushion_pct, 4)  # 验收可观测
        except Exception:
            pass  # 回退旧 cushion (静默容错: 保本链路不得因记账增强而阻断)

    # ── 净口径盈亏 (2026-08-17, 用户裁"保本单和豁免条件真正净保本, 包含费用") ──
    # 旧口径全部用毛盈亏: ① 豁免条件 (水下判定 profit<0) 把 0<毛<费用 的"微盈仓"当盈利
    #   → 失去 W_WEEK 豁免被 DEAD_SIGNAL/PRE_CLOSE 出场, 落袋变白跑 (NVDA 8/17 实弹:
    #   毛+0.013% 失去豁免 → DEAD_SIGNAL 出场 净-$3.51)。② 保本单市价平仓 fill 漂移。
    # 净口径 = 毛 − 真实往返费率 (仅当 D4 fee-aware cushion 算出时启用; 算不出 → 毛口径
    #   原行为, 保守不回退)。豁免/落袋/保本线全部消费同一 _profit_net_pct, 单一口径。
    _has_real_cushion = meta.get("_be_cushion_fee_aware") is not None
    _profit_net_pct = profit_pct - _fee_cushion_pct if _has_real_cushion else profit_pct
    # 净保本线价格 (monitor 平仓时挂限价单用): 成交价物理上 ≥(空:≤) 此线 = 净保本由订单类型保证
    # 2026-08-20 (方案A): 此线刻意只含 fee 不含 slip 垫 — 限价单是"地板", 触发线才垫 slip:
    #   触发线(费用+滑点) > 限价线(仅费用) → 触发时限价单大概率在主动侧立即成交, 地板不动.
    #   误把 slip 垫进此线会把限价单推得更被动(降低成交率), 与本意相反.
    if _has_real_cushion and entry_price > 0:
        meta["_be_net_line_price"] = round(
            entry_price * (1 + _fee_cushion_pct / 100.0) if direction == "LONG"
            else entry_price * (1 - _fee_cushion_pct / 100.0), 4)

    # ── 滑点垫 (2026-08-20 方案A): 触发线 = 费用线 + slip_cushion. 见 _compute_dynamic_effective_sl.
    #   校准: 8/20 实弹降级市价滑点 21/6.6/43bps → 0.2% 覆盖; 约束 < breakeven_at_pct(0.4).
    _slip_cushion_pct = float(_pb_cfg.get("breakeven_slip_cushion_pct", 0.0))
    _source_type = str(meta.get("_source_type", "") or "").strip().upper()
    _adaptive_drawdown_pct = adaptive_trailing_drawdown_pct(
        peak_profit_pct, _pb_cfg, _source_type
    )
    if _adaptive_drawdown_pct is not None:
        meta["_adaptive_trailing"] = adaptive_trailing_snapshot(
            peak_profit_pct, _pb_cfg, _source_type
        )

    effective_sl = _compute_dynamic_effective_sl(
        direction, entry_price, initial_sl, peak_profit_pct, _exit_cfg, _fee_cushion_pct,
        _slip_cushion_pct, _adaptive_drawdown_pct
    )
    breakeven_triggered = peak_profit_pct >= float(_exit_cfg.get("breakeven_at_pct", 0.5))

    # ── 退出判断 (按优先级, if/elif/elif 链) ──
    # 优先级: SL > TAKE_PROFIT > OVERNIGHT_GAP > PRE_CLOSE > TIME_EXIT > MAX_HOLD
    exit_reason = ""
    exit_price = current_price

    # 规则0: 开盘冷却期 — 前15分钟SL放宽到4% (不改SL参数本身, 只加时间窗口保护)
    # 原因: 3/5笔在0-6min秒杀, MFE=0%, 价格从未浮盈. 开盘噪声大, 2%SL被瞬间打掉.
    # 诊断: 修后MFE>0%=时机问题, MFE仍=0%=信号问题
    _cooldown_pct = float(_pb_cfg.get("cooldown_sl_pct", 4.0))  # 2026-07-16: 从 config 读取, 默认 4%
    _cooldown_sl = entry_price * (1 - _cooldown_pct / 100.0) if direction == "LONG" else entry_price * (1 + _cooldown_pct / 100.0)
    _in_cooldown = minutes_from_open is not None and minutes_from_open < 15
    # 2026-08-20 (DEB §三十九, 用户裁"要"): 已武装保本仓豁免冷却放宽.
    #   TSM 8/19 实弹: 隔夜仓开盘 09:31 冲高武装 (be线416.46), 09:32 价格穿线,
    #   但冷却窗 min(be线, 冷却SL399.5) 把保本线锁进保险柜 → 压到 09:45 冷却到期
    #   才出场, 期间价格 416→413, 净亏 -$25.5 (1分K回放: 09:32 出场可净$0~+$11, 挽回$25-37).
    #   冷却窗原始保护对象 = MFE=0% 未武装新仓 (防开盘秒杀), 它们本就不触发本豁免.
    #   已武装 = 价格已证明过浮盈, 保本线是"防赚变亏"的承诺, 开盘瞬间假跌破也应尊重.
    #   回滚: 删 `and not breakeven_triggered` (恢复无差别放宽). 判分: 开盘15min内
    #   SL_BREAKEVEN 出场笔数 + 其净结局 (回放基准 09:32 出场).
    if _in_cooldown and not breakeven_triggered:
        _active_sl = min(effective_sl, _cooldown_sl) if direction == "LONG" else max(effective_sl, _cooldown_sl)
    else:
        _active_sl = effective_sl

    # 规则0.5 (2026-07-19): 早期紧SL — 入场后 N 分钟内未浮盈达标 → SL收紧到 early_tight_sl_pct.
    # 数据依据: TIME_EXIT 11笔 0%WR, MFE 全部<0.5%, 方向从未兑现但等 4-6h 才触发 TIME_EXIT.
    # 逻辑: 如果方向对, 价格很快向有利方向移动, breakeven 激活后本规则自动失效;
    #        如果方向错, 1% 紧SL 在 ~15-30min 内触发 SL_INITIAL → 快速脱身, 只亏1%而非等4h.
    # 与 cooldown 互补: cooldown 防开盘瞬间秒杀(放宽到4%); 本规则防方向全错慢性失血(收紧到1%).
    #        cooldown 优先(开盘噪声>方向信号); cooldown 过期后本规则接管.
    # 退出原因: 仍为 SL_INITIAL (breakeven 未激活), 无需新标签, 复用现有基建.
    # 回滚: early_tight_sl_enabled 改 false. 判分: TIME_EXIT 笔数下降 + SL_INITIAL 早期笔数/损失.
    _early_tight_enabled = bool(_pb_cfg.get("early_tight_sl_enabled", False))
    if _early_tight_enabled and not _in_cooldown and not breakeven_triggered:
        _et_minutes = float(_pb_cfg.get("early_tight_sl_minutes", 30))
        _et_sl_pct = float(_pb_cfg.get("early_tight_sl_pct", 1.0))
        # 2026-08-29 影子毕业提案: mfe_gate>0 时仅"方向全错"仓收紧 (窗口内峰值浮盈
        #   从未达 gate 才收紧), 对齐影子胜出变体 early_tight_30m_mfe0.3 ——
        #   290样本/5.5周净反事实+27.58pp, 误杀最少(20/47)。默认0=现行行为不变。
        _et_mfe_gate = float(_pb_cfg.get("early_tight_sl_mfe_gate", 0.0) or 0.0)
        # [2026-09-01 用户裁"A"·C1 冻结显式豁免] ATR 比例紧 SL:
        #   tight = clamp(0.5×ATR%, 下限=early_tight_sl_pct, 上限=pct_max)。
        #   缘起 9/1 V 型日: AMD/LLY/SAIC 三笔 MFE=0 + ATR 3-5%, 固定 1% ≈ 0.25×ATR
        #   低于噪声 → 回踩即扫 (SAIC 场后 +1.96% 误杀 $186)。修噪声错配不放宽纪律:
        #   高 ATR 桶收 2-2.5% 仍 < 代码 SL(5%) 一半, 慢性失血单 (跌穿-3~-5%) 照杀。
        #   meta.atr = 入场时持久化的 ATR 绝对价 (playbook_service._extract_atr_from_instruction);
        #   缺失/0 → 回退固定 1% (=毕业行为, fail-safe)。回滚: atr_enabled=false 单键。
        _et_basis = "fixed"
        _et_atr_abs = float(meta.get("atr") or 0)
        if (bool(_pb_cfg.get("early_tight_sl_atr_enabled", False))
                and _et_atr_abs > 0 and entry_price > 0):
            _et_atr_pct = _et_atr_abs / entry_price * 100.0
            _et_mult = float(_pb_cfg.get("early_tight_sl_atr_mult", 0.5))
            _et_ceiling = float(_pb_cfg.get("early_tight_sl_pct_max", 2.5))
            _et_cand = max(_et_sl_pct, min(_et_ceiling, _et_mult * _et_atr_pct))
            if _et_cand > _et_sl_pct:
                _et_sl_pct = _et_cand
                # [2026-09-02 DEB-20260831 §8.7] basis 尾缀透传 atr 来源 (real|proxy|missing),
                #   切片时区分真 ATR 与 SL 距离代理; 历史仓无此键 → '?'。零行为变更。
                _et_basis = f"atr={_et_atr_pct:.1f}%×{_et_mult:g}[{meta.get('atr_basis', '?')}]"
        if held_minutes <= _et_minutes and (_et_mfe_gate <= 0 or peak_profit_pct < _et_mfe_gate):
            _et_sl = entry_price * (1 - _et_sl_pct / 100.0) if direction == "LONG" \
                else entry_price * (1 + _et_sl_pct / 100.0)
            # 紧SL = 更靠近入场价 = LONG取max, SHORT取min
            _sl_before_tight = _active_sl
            _active_sl = max(_active_sl, _et_sl) if direction == "LONG" else min(_active_sl, _et_sl)
            # [2026-09-16 用户裁"同意按建议执行"第1条, 纯观测零行为变更] 紧SL线落 meta —
            #   ARM 9/15 案: 真杀仓的是本规则 (248.84), 但标签/推送只有初始SL 255.94,
            #   对账要翻日志。触发轮由规则1 写 _effective_sl_at_exit/_tight_sl_killed,
            #   随 _playbook_exit_decision 快照 → exit_metadata → metadata_json + 推送。
            if _active_sl != _sl_before_tight:
                meta["_early_tight_sl_px"] = round(_et_sl, 4)
            # 审计日志 (2026-07-19 辩论收敛#2): 紧SL实际生效(改变了SL)时每 symbol每日 只记一次,
            #   供 exit_scorecard 判分用: "SL_INITIAL 早期出场" ← 本日志记录, 可区分紧SL触发 vs 正常2%SL触发.
            #   防刷屏: monitor 轮询期间同一仓位每轮都命中本分支, 模块级 set 去重 (进程重启后重记一次, 可接受).
            if _active_sl != _sl_before_tight:
                _et_log_key = f"{_symbol}:{date.today().isoformat()}"
                if _et_log_key not in _EARLY_TIGHT_SL_LOGGED:
                    _EARLY_TIGHT_SL_LOGGED.add(_et_log_key)
                    logger.info(
                        f"[规则0.5-早期紧SL] {_symbol} {direction} held={held_minutes:.1f}min "
                        f"SL {_sl_before_tight:.4f}→{_active_sl:.4f} "
                        f"(entry={entry_price:.4f}, tight={_et_sl_pct:.2f}% basis={_et_basis})"
                    )

    # 规则1: SL止损 (最高优先级 — 亏损仓位必须先检查止损)
    # 2026-07-14: 拆分原 SL_FIXED_PCT 复合标签. 同一标签曾混合三种机制(近30天9笔仅1笔
    #   真止损, 其余为保本+锁利), 污染退出侧诊断. 按 effective_sl 相对 entry 的位置区分,
    #   便于 exit_shadow / 报表按真实语义归因. 保留 SL_ 前缀以兼容 LIKE 'SL%' 过滤.
    if _active_sl > 0 and ((direction == "LONG" and current_price <= _active_sl) or
                           (direction == "SHORT" and current_price >= _active_sl)):
        # [2026-09-16 用户裁"同意按建议执行"第1条, record-only] 生效SL观测性 — ARM 9/15 案:
        #   真触发线是早期紧SL 248.84, 但标签 SL_INITIAL + 推送只有初始SL 255.94,
        #   对账要翻日志。触发瞬间把生效线/是否紧SL所杀写 meta, 随 decision 快照 →
        #   exit_metadata → metadata_json + exit_telemetry + 平仓推送。零行为变更。
        meta["_effective_sl_at_exit"] = round(_active_sl, 4)
        _et_px = meta.get("_early_tight_sl_px")
        meta["_tight_sl_killed"] = bool(
            _et_px is not None
            and abs(float(_et_px) - _active_sl) < entry_price * 1e-4)
        # 2026-07-16 P0 修复: exit_price 用 current_price 而非 _active_sl.
        #   _active_sl 是止损线价格, 实际市价单成交价可能远低于 SL (滑点/gap).
        #   旧逻辑 exit_price = _active_sl 系统性高估退出价 → PnL 偏高.
        #   修复: 用 current_price (本轮报价) 作为退出价, 与 broker 实际成交更接近.
        #   最终退出价在调用方 (monitor_playbook_sim_exits) 用 broker filled_price 覆盖.
        exit_price = current_price
        # 2026-07-17: 分类阈值用"净保本线"(成本±fee_cushion), 否则纯净保本会被误标 SL_TRAIL_LOCK.
        # 2026-08-20 (方案A): 分类线 = 费用线 + 滑点垫, 与 ratchet 触发线同口径 —
        #   否则纯保本出场 (effective_sl 恰在垫线) 会被误标 SL_TRAIL_LOCK, 污染退出账本归因.
        _cls_pad = _fee_cushion_pct + _slip_cushion_pct
        _be_line = entry_price * (1 + _cls_pad / 100.0) if direction == "LONG" \
            else entry_price * (1 - _cls_pad / 100.0)
        _be_tol = entry_price * 1e-4  # 1bp 容差, 吸收浮点/取整
        if not breakeven_triggered:
            exit_reason = "SL_INITIAL"        # 浮盈未达 breakeven → 初始固定止损被打掉
        elif (direction == "LONG" and effective_sl > _be_line + _be_tol) or \
             (direction == "SHORT" and 0 < effective_sl < _be_line - _be_tol):
            exit_reason = "SL_TRAIL_LOCK"     # 回撤锁利 → SL 已锁在净保本线之上(空:之下)
        else:
            exit_reason = "SL_BREAKEVEN"      # 净保本 ratchet → SL 提到成本价+手续费缓冲附近
            # ── 死亡谷 shadow 方案A (2026-07-28, record-only): breakeven 触发线 0.5%→0.8%
            #   反事实 — 近两周 26 笔均浮盈 0.96% 只捕获 11% (保本锚上被回踩杀)。
            #   触发时 MFE<0.8% 的仓在 A 方案下尚无保本锚→不会在此出场; 记快照,
            #   最终反事实出场由离线回放补 (出场后走势超出仓位生命周期)。回滚: 删本块。
            try:
                if peak_profit_pct < 0.8 and not meta.get("_be_shadow_a"):
                    meta["_be_shadow_a"] = True
                    import json as _bj
                    from app.utils.timezone import now_et as _be_now
                    with open("data/breakeven_shadow.jsonl", "a") as _bf:
                        _bf.write(_bj.dumps({
                            "variant": "A_no_be_yet", "ts": _be_now().isoformat(),
                            "symbol": _symbol, "direction": direction,
                            "entry_price": entry_price, "real_exit_price": current_price,
                            "mfe_at_trigger": round(peak_profit_pct, 4),
                            "initial_sl": initial_sl, "held_minutes": round(held_minutes, 1),
                        }, ensure_ascii=False) + "\n")
                    logger.info(f"[影子·保本谷A] {_symbol} MFE={peak_profit_pct:.2f}%<0.8% "
                                f"真实保本出@{current_price} (A方案不会在此出场, record-only)")
            except Exception:
                pass

    # ── 死亡谷 shadow 方案B (2026-07-28, record-only): 保本锚 +0.15% 缓冲的反事实 —
    #   B 锚更高→更早触发: 真实仓未出场但 B 锚已被碰到时记录反事实出场点。
    #   真实仓继续持有, 后续真实结局与 B 出场点可直接对比 (无需离线回放)。
    #   仅当 B 锚严于当前生效 SL (真实不会同点出) 才是真差异点。回滚: 删本块。
    # 2026-08-20 (方案A) 取代标注: 真实保本触发线已抬至 fee+slip(0.2%) = 100.607,
    #   高于 B 锚 (fee+0.15% = 100.5575) → 生产上真实 SL_BREAKEVEN 先于 B 锚触发,
    #   _b_differs 恒 False → 生产不再产生 B/C 记录。方案 A 即本影子验证的"缓冲出场"
    #   的实弹落地 (0.2% > 0.15%, 且更早)。测试用 slip=0 保留本契约语义。
    # ── shadow 方案C (2026-08-19, DEB-20260819 §三十七, record-only): 时间门控缓冲出场 —
    #   B 结算(n=49): 触碰≥60min 组 14例13胜 +$76 (死单特征), <60min 组被7笔错过赢单
    #   拖成 -$88 (动量未走完)。C = 只对 held≥60min 的首次锚触碰记录反事实出场。
    #   预注册翻开关判据 (§三十七): C 专属事件 n≥30 + 单侧符号检验 p<0.05 +
    #   均值>0 (B 教训: 胜率高≠均值正) ; gate=60 为事后选定切点, 结算时按
    #   held_minutes 协变量做稳健性检查。回滚: 删 C 分支。
    _c_gate_min = 60
    # 测试卫生守卫: _symbol=="TEST" 是单测约定标记 (实证: 8/18-19 生产 jsonl 已被
    # test_net_breakeven_contract 调用写入 4×N 条 TEST 记录) — 影子写盘跳过.
    if (_symbol != "TEST" and not exit_reason and breakeven_triggered and _active_sl > 0
            and (not meta.get("_be_shadow_b")
                 or (held_minutes >= _c_gate_min and not meta.get("_be_shadow_c")))):
        try:
            _b_buffer = 0.15
            # 块内自算净保本线 (规则1 内的 _be_line/_be_tol 在本路径未定义 — NameError
            # 会被 try 吞成永久静默, shadow now_et 教训同款, 自查揭出)
            _b_be_line = entry_price * (1 + _fee_cushion_pct / 100.0) if direction == "LONG" \
                else entry_price * (1 - _fee_cushion_pct / 100.0)
            _b_be_tol = entry_price * 1e-4
            _b_anchor = _b_be_line * (1 + _b_buffer / 100.0) if direction == "LONG" \
                else _b_be_line * (1 - _b_buffer / 100.0)
            _b_hit = (direction == "LONG" and current_price <= _b_anchor) or \
                     (direction == "SHORT" and current_price >= _b_anchor)
            _b_differs = (direction == "LONG" and _b_anchor > _active_sl + _b_be_tol) or \
                         (direction == "SHORT" and _b_anchor < _active_sl - _b_be_tol)
            if _b_hit and _b_differs:
                import json as _bj
                from app.utils.timezone import now_et as _be_now
                if not meta.get("_be_shadow_b"):
                    meta["_be_shadow_b"] = True
                    with open("data/breakeven_shadow.jsonl", "a") as _bf:
                        _bf.write(_bj.dumps({
                            "variant": "B_buffer_exit", "ts": _be_now().isoformat(),
                            "symbol": _symbol, "direction": direction,
                            "entry_price": entry_price, "shadow_exit_price": current_price,
                            "b_anchor": round(_b_anchor, 4),
                            "mfe_pct": round(peak_profit_pct, 4),
                            "held_minutes": round(held_minutes, 1),
                        }, ensure_ascii=False) + "\n")
                    logger.info(f"[影子·保本谷B] {_symbol} B锚({_b_anchor:.4f})被碰到 "
                                f"反事实出@{current_price}, 真实仓继续 (record-only)")
                if (held_minutes >= _c_gate_min and not meta.get("_be_shadow_c")):
                    meta["_be_shadow_c"] = True
                    with open("data/breakeven_shadow.jsonl", "a") as _bf:
                        _bf.write(_bj.dumps({
                            "variant": "C_late_buffer_touch", "ts": _be_now().isoformat(),
                            "symbol": _symbol, "direction": direction,
                            "entry_price": entry_price, "shadow_exit_price": current_price,
                            "b_anchor": round(_b_anchor, 4),
                            "mfe_pct": round(peak_profit_pct, 4),
                            "held_minutes": round(held_minutes, 1),
                            "gate_min": _c_gate_min,
                        }, ensure_ascii=False) + "\n")
                    logger.info(f"[影子·保本谷C] {_symbol} 锚触碰@held{held_minutes:.0f}min≥{_c_gate_min} "
                                f"反事实出@{current_price}, 真实仓继续 (record-only)")
        except Exception:
            pass

    # 规则1.5 (2026-06-28): 止盈退出 — 价格触及 take_profit 时主动锁定利润.
    # 智能跳过: rr_ratio=999 / take_profit=0 的指令不触发 (用户决策).
    # 2026-07-10: 增加 RR 底线 (MIN_TP_RR=1.5). 根因: short_horizon rr_ratio=999
    #   (设计上不设TP, 让 trailing/EOD 捕获利润) 但指令级 analyst TP 仍会触发,
    #   出现 NVDA SL-4.5%/TP+1.7%(RR=0.38) 的畸形几何 — 冒大风险博小利润.
    #   修法: TP 相对【初始SL】的 RR < 1.5 时忽略该 TP, 交给 trailing/EOD;
    #   RR>=1.5 的好几何(如 NU RR=2.26)仍正常止盈. 无初始SL时不做门禁(退回原行为).
    _take_profit = meta.get("take_profit", 0)
    _MIN_TP_RR = float(_pb_cfg.get("min_tp_rr", 1.5))  # 2026-07-17: config 化 (SSOT), 原硬编码 1.5
    if not exit_reason and _take_profit and float(_take_profit) > 0:
        _tp = float(_take_profit)
        _tp_risk = abs(entry_price - initial_sl) if (initial_sl and initial_sl > 0) else 0
        _tp_reward = abs(_tp - entry_price)
        _tp_rr = (_tp_reward / _tp_risk) if _tp_risk > 0 else 999  # 无SL→不门禁
        if _tp_rr < _MIN_TP_RR:
            # [DEB-20260910 §15.5 2026-09-12 用户裁"按建议执行"] DEBUG→INFO + meta 留痕:
            #   CTRN 9/11 实锤 RR=1.499261 显示取整成 1.50 — "固定TP是否启用及原因"
            #   常规日志不可见 (DEBUG 级), metadata 无记录 → 事后归因只能翻源码重算。
            #   近30日 82/223 笔 (36.8%) 被本门禁吞掉固定TP, 其中临界带 [1.4,1.5) 61 笔。
            #   本块零行为变更: 只升级日志可见性 + meta 记账 (随 trade_records.metadata_json
            #   持久化, 供复盘按三时点RR切片)。回滚: 还原 DEBUG + 删 meta 写入。
            logger.info(f"[TP-RR门禁] {_symbol} {direction} TP RR={_tp_rr:.6f}<{_MIN_TP_RR}, "
                        f"忽略指令TP(交给trailing/EOD锁利)")
            meta["_tp_rr_gate"] = {
                "rr_at_execution": round(_tp_rr, 6),   # 实际成交价几何 (本函数口径)
                "min_tp_rr": _MIN_TP_RR,
                "swallowed": True,
                "tp": _tp, "initial_sl": initial_sl,
            }
            # ── 再入场快TP门禁吞掉影子 (2026-08-31, DEB-20260831-reentry-quick-tp-gate-conflict, record-only) ──
            # 8/7 裁决的再入场快收 TP (几何 RR≈0.58) 恰被本门禁吞掉 — 13 笔实弹 0 笔
            # TAKE_PROFIT 出场, 两次裁决互杀。本块只在价格触及被吞 TP 时把反事实触达
            # 记入 meta (跨轮持久); 结算对比在 _close_trade_record → settle_on_close。
            # 零退出行为变更。回滚: playbook.reentry_quick_tp_shadow_enabled=false 或删本块。
            try:
                from app.playbook.reentry_tp_shadow import note_gate_swallowed_touch as _rqtp_note
                _rqtp_note(meta, direction, current_price, _tp, peak_profit_pct, held_minutes)
            except Exception as _rqtp_e:
                logger.debug(f"[shadow·reentry_tp] 触达记录跳过: {_rqtp_e}")
        elif direction == "LONG" and current_price >= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price
        elif direction == "SHORT" and current_price <= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price

    # 规则2 (RFC-061 #6): 隔夜跳空止损
    if not exit_reason and _skip_allowed and _is_overnight_position(entry_time_str) and profit_pct <= -_overnight_gap_stop_pct:
        exit_reason = "OVERNIGHT_GAP_STOP"
        exit_price = current_price

    # 规则4: 盘末退出 — 7/7 fix: 恢复此规则
    # 原因: 7/3 fix 让 ExitMonitor 不再管 PLAYBOOK_SIM/LIVE 仓位 (reconciler 拦截→PROTECTED),
    #        6/30 DEB 把 pre_close 交给 ExitMonitor 的决策前提已不成立。
    #        PLAYBOOK 仓位的 pre_close 必须由 Pipeline 自管, 否则收盘前8min 盈利落袋无人执行。
    if not exit_reason and 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
        if _skip_allowed and profit_pct > 0:
            pass  # cohort B 盈利仓跳过, 持过夜 (毛口径: 此处若切净口径会把微盈仓市价卖=白跑)
        elif _overnight_eod_exempt(_symbol, meta.get("_source_type", ""), entry_time_str, _profit_net_pct):
            pass  # P1 单点 (2026-09-06): 轨③ reentry 首夜 (8/6 裁决) → W_WEEK SIM 水下多夜
                  # (8/15 三问Q3), 顺序/8/17 净口径与合并前逐字一致
            # ── RFC-079 (2026-09-09): EOD 深水闸影子打点 (record-only, 豁免行为零变更) ──
            #   豁免成立后记录决策时刻净水深; would_revoke 行由次日结算脚本算反事实 Δ。
            #   开关 eod_depth_gate_shadow_enabled (回滚 false + 重启); 异常全收口不影响主链。
            try:
                from app.playbook.overnight_decision import emit_eod_depth_gate_shadow as _edg_emit
                _edg_emit("poll_pre_close", _symbol, direction,
                          meta.get("_source_type", ""), entry_price, meta.get("_qty"),
                          entry_time_str, current_price, _profit_net_pct,
                          trade_id=meta.get("_trade_id"))  # RFC-095 身份锚
            except Exception as _edg_err:
                # R5: 打点内层已全量收口, 本层只兜 import 级故障 — 必须留痕 (2026-09-09 棘轮清剿)
                logger.warning(f"[exit_engine] EOD深水闸影子打点兜底异常 {_symbol}: {_edg_err}")
        else:
            exit_reason = "PRE_CLOSE_EXIT"
            exit_price = current_price
            # ── RFC-074 (2026-09-02, 用户裁"方案C同意"): 赢家隔夜影子 arm 点 (record-only) ──
            #   结构缺口: cohort A 盈利仓 15:52 一律强平 → "赢家隔夜"臂事实死亡 (long
            #   horizon 8月以来 0.5% 分配率)。影子记"带 SL 持一夜"反事实臂, 次日 1min
            #   bar 结算 (scripts/settle_winner_overnight_shadow.py)。零行为变更。
            #   回滚: winner_overnight_shadow_enabled 改 false (无需重启, 每轮读取)。
            #   2026-09-02 核查批补: pytest 环境隔离 (先例 audit_db.py) — 埋点写死生产
            #   路径, 单测触发 PRE_CLOSE_EXIT 会污染生产账本 (实测一次全量跑 8 行 TEST
            #   符号落账)。生产判定不读此环境变量, 零生产影响。
            import os as _wos_os
            if bool(_pb_cfg.get("winner_overnight_shadow_enabled", True)) \
                    and not _wos_os.environ.get("PYTEST_CURRENT_TEST"):
                try:
                    import json as _wos_j
                    from pathlib import Path as _WOS_P
                    from app.utils.timezone import now_et as _wos_now
                    _wos_et = _wos_now()
                    _wos_sl = float(meta.get("stop_loss", 0) or 0)
                    _wos_row = {
                        "shadow": "winner_overnight",
                        "type": "arm",
                        "ts": _wos_et.isoformat(),
                        "symbol": _symbol, "direction": direction,
                        "entry_time": entry_time_str, "entry_price": entry_price,
                        "quantity": meta.get("_qty"),
                        "decision_id": str(meta.get("decision_id", "") or ""),
                        "source_type": meta.get("_source_type", ""),
                        "arm_date": _wos_et.strftime("%Y-%m-%d"),
                        "arm_price": round(current_price, 4),
                        "arm_profit_pct": round(profit_pct, 4),
                        "arm_net_profit_pct": round(_profit_net_pct, 4),
                        "stop_loss_at_arm": round(_wos_sl, 4) if _wos_sl > 0 else None,
                        "settled": False,
                    }
                    with open(_WOS_P("data/shadow/winner_overnight_shadow.jsonl"), "a") as _wos_f:
                        _wos_f.write(_wos_j.dumps(_wos_row, ensure_ascii=False) + "\n")
                except Exception:
                    pass  # 影子埋点不阻断退出链 (静默死亡族纪律: 结算脚本侧有覆盖率对账)

    # ── 高 ATR 退出档 shadow 探针 (2026-07-28 首夜复盘 #1, record-only 零行为影响) ──
    #   处方源: L2 回测 hi4.0 翻案 (高 ATR 不拦入场→转退出档) + 07-27 实证 (TSM/GS ATR 5%
    #   享 -5% 宽 SL + 150min 慢停损, 两笔 -$176)。shadow 假设: ATR≥4% 且 60min 内 MFE<0.3%
    #   且水下 → 反事实提前离场。只记录触发点价格, 不动真实退出; 与真实 exit 对比出
    #   反事实收益差, n≥5 后 08-07 裁决点定是否转 live。回滚: 删本块。
    try:
        _atr_val = float(meta.get("atr", 0) or 0)
        _atr_pct_live = (_atr_val / entry_price * 100.0) if (entry_price and _atr_val) else 0.0
        # 2026-08-05 (DEB-20260805 门B 终裁): 单一 60min 记录点扩为 45/60/75/90min 多时点 —
        #   原硬编码 held≥60 只记首次越线点, 时点选择零敏感性无法检验; 多时点同臂内对比.
        #   寄生特征 slope_5m/vwap_dev (复用 K_1M 300s 缓存, 零新采集器); record-only,
        #   见数前不得升级为判据 (预注册规范§3). 回滚: 删多时点/字段即恢复原行为.
        if (_atr_pct_live >= 4.0 and peak_profit_pct < 0.3 and profit_pct < 0):
            # 2026-08-05 修复: set 不可序列化且 meta 每轮 DB 重读 → 去重失效每 10s 重复发射.
            #   改 list 落 meta (JSON-safe, 随 metadata_json 写回跨轮持久) — 水下单 mae>0 必触发写回.
            _shadow_pts = meta.setdefault("_high_atr_stall_shadow_pts", [])
            if not isinstance(_shadow_pts, list):
                _shadow_pts = meta["_high_atr_stall_shadow_pts"] = []
            if meta.get("_high_atr_stall_shadow") and 60 not in _shadow_pts:
                _shadow_pts.append(60)  # 旧标志已在 60min 记录过, 防重复
            _hit = None
            for _pt in (45, 60, 75, 90):
                if held_minutes >= _pt and _pt not in _shadow_pts:
                    _hit = _pt
                    break
            if _hit is not None:
                _shadow_pts.append(_hit)
                if _hit == 60:
                    meta["_high_atr_stall_shadow"] = True  # 保留旧标志语义 (会话内 60 点已记)
                import json as _sj
                from pathlib import Path as _SP
                from app.utils.timezone import now_et as _shadow_now_et  # pipeline 无模块级 now_et
                _slope_5m = None
                _vwap_dev = None
                try:
                    from app.data.market_data_router import get_market_data_router as _get_rtr
                    _futu = getattr(_get_rtr(), "futu", None)
                    if _futu is not None and hasattr(_futu, "get_kline"):
                        try:
                            _sbars = _futu.get_kline(_symbol, ktype="K_1M", count=400, autype="NONE") or []
                        except TypeError:
                            _sbars = _futu.get_kline(_symbol, ktype="K_1M", count=400) or []
                        _today = _shadow_now_et().strftime("%Y-%m-%d")
                        _rows = []
                        for _b in _sbars:
                            _t = str(_b.get("time") or _b.get("time_key") or "")
                            if not _t.startswith(_today):
                                continue
                            _c = float(_b.get("close", 0) or 0)
                            _v = float(_b.get("volume", 0) or 0)
                            _tv = float(_b.get("turnover", 0) or 0)
                            if _c > 0:
                                _rows.append((_c, _v, _tv))
                        if _rows:
                            _tv_sum = sum(r[2] for r in _rows)
                            _v_sum = sum(r[1] for r in _rows)
                            if _v_sum > 0 and _tv_sum > 0:
                                _vwap_v = _tv_sum / _v_sum
                                if _vwap_v > 0:
                                    _vwap_dev = round((current_price - _vwap_v) / _vwap_v, 6)
                            if len(_rows) >= 6:
                                _c5 = _rows[-1][0]
                                _c0 = _rows[-6][0]
                                if _c0 > 0:
                                    _slope_5m = round((_c5 - _c0) / _c0 * 100.0, 4)
                except Exception:
                    _slope_5m, _vwap_dev = None, None  # 寄生特征缺失不阻断影子行
                _srow = {
                    "ts": _shadow_now_et().isoformat(), "symbol": _symbol, "direction": direction,
                    "entry_price": entry_price, "shadow_exit_price": current_price,
                    "held_minutes": round(held_minutes, 1), "mfe_pct": round(peak_profit_pct, 4),
                    "cur_pct": round(profit_pct, 4), "atr_pct": round(_atr_pct_live, 2),
                    "rule": "high_atr_stall_60m_mfe0.3",
                    "checkpoint": _hit, "slope_5m": _slope_5m, "vwap_dev": _vwap_dev,
                }
                with open(_SP("data/high_atr_stall_shadow.jsonl"), "a") as _sf:
                    _sf.write(_sj.dumps(_srow, ensure_ascii=False) + "\n")
                logger.info(
                    f"[影子·高ATR停滞] {_symbol} {direction} atr={_atr_pct_live:.1f}% "
                    f"checkpoint={_hit}min held={held_minutes:.0f}min mfe={peak_profit_pct:.2f}% "
                    f"cur={profit_pct:.2f}% 反事实退出点=${current_price} (record-only)"
                )
    except Exception:
        pass

    # 影子·死单早期识别 (2026-08-11, DEB-20260811-dead-order-early-detection, record-only):
    #   回放实锤: 入场后 30min MFE<0.2% 双窗口死单率 69%/64% (W1 n=35 / W2 n=14,
    #   命中组实际净 PnL 全负 -1218/-366, 误杀代价近零)。与已毕业 DEAD_SIGNAL_EXIT
    #   (60min, MFE<0.2%) 同源不同参数 — 本影子验证"提前到 30min"的独立价值:
    #   逐笔配对 30min 命中 vs 60min 命中的差集结局。15min 为辅助观测点 (回放
    #   MFE<0.15 死单率 61-62%)。零行为变更; 回滚: early_dead_signal_shadow_enabled=false。
    #   裁决预注册 (攒样≥30 命中): ①双窗口死单率均≥55% ②命中组实际净PnL<0
    #   ③差集(30min命中但60min未命中)结局为负。scripts/early_dead_signal_report.py 结算。
    try:
        if bool(_pb_cfg.get("early_dead_signal_shadow_enabled", True)):
            _ed_pts = meta.setdefault("_early_dead_shadow_pts", [])
            if not isinstance(_ed_pts, list):
                _ed_pts = []
            for _ed_pt in (15, 30):
                # 新鲜度守卫 (2026-08-11): 只在检查点窗口 [pt, pt+5min] 内穿越才记录。
                #   MFE 是持仓期累计最大值 — 服务重启后对已远超检查点的存量仓
                #   (如隔夜仓 held=796min) 若仍触发, 会把"整个持仓期的 MFE"误标为
                #   30min 检查点, 系统性高估 MFE/漏计死单, 污染裁决数据。
                #   快循环 10s 节奏下正常穿越落在 pt+~0.02min; +5min 容差覆盖轮询间隙,
                #   同时拒绝重启后的穿越 (held≫pt+5)。错过窗口即不记录该检查点 (不伪造)。
                if held_minutes >= _ed_pt and held_minutes <= _ed_pt + 5 and _ed_pt not in _ed_pts:
                    _ed_pts.append(_ed_pt)
                    import json as _edj
                    from app.utils.timezone import now_et as _ed_now_et
                    _ed_row = {
                        "ts": _ed_now_et().isoformat(),
                        "symbol": _symbol, "direction": direction,
                        "entry_time": entry_time_str, "entry_price": entry_price,
                        "checkpoint": _ed_pt,
                        "held_minutes": round(held_minutes, 1),
                        "mfe_pct": round(peak_profit_pct, 4),
                        "cur_pct": round(profit_pct, 4),
                        "rule": "early_dead_signal_shadow",
                        "settled": False,
                    }
                    with open(_EARLY_DEAD_SHADOW_PATH, "a") as _edf:
                        _edf.write(_edj.dumps(_ed_row, ensure_ascii=False) + "\n")
                    logger.info(
                        f"[影子·死单早期] {_symbol} {direction} checkpoint={_ed_pt}min "
                        f"mfe={peak_profit_pct:.2f}% cur={profit_pct:.2f}% (record-only)"
                    )
    except Exception:
        pass  # 影子埋点不阻断退出判断

    # 规则3.3 (2026-08-08, exit_shadow dead_signal_60m_mfe0.2 毕业): 死信号退出 —
    #   shadow 判分 PASS (全历史 171 笔: 触发组 mean -1.12% vs 未触发组 +0.37%,
    #   分离度 1.49pp) → 60min 时峰值浮盈从未达 0.2% = 方向从未兑现 → 立即离场.
    #   与规则3.4 区别: 不要求当前水下 (0%~-0.2% 也走), 更早承认信号失效.
    #   MFE_STALL(150min) 保留兜底. 回滚: dead_signal_exit_enabled 改 false.
    # 2026-08-16 (用户裁"方案B", DEB§34 后续审计): SIM水下仓豁免 DEAD_SIGNAL_EXIT.
    #   根因: W_WEEK 水下隔夜仓的 MFE 是生命周期累计 (metadata 恢复, 只增不减) —
    #   入场日 MFE<0.2% 的仓, 次日开盘 held(墙钟)≫60min 且 MFE 恒<0.2% → 第一轮巡检
    #   必触发 DEAD_SIGNAL → patient_exit eod 腿/立即卖 → 第2天必出局, W_WEEK 的
    #   5日修复空间被截断成2天。回放实锤 (overnight_loser_multiday_replay, n=15):
    #   W_WEEK +$629 中 MFE<0.2% 子集 n=6 贡献 +$698 (100%修复), 全部会被本规则杀掉。
    #   豁免范围 = _loser_overnight_exempt (仅 PLAYBOOK_SIM + 水下 + ≤5交易日);
    #   LIVE 零影响; SL 仍最高优先级兜底; 豁免仓转入 SL/TIME_EXIT(已豁免)/EOD转正/5日上限链。
    #   回滚: 删本 if 包裹 (恢复无条件触发) 或 loser_overnight_enabled=false。
    if not exit_reason and bool(_pb_cfg.get("dead_signal_exit_enabled", False)):
        _ds_minutes = float(_pb_cfg.get("dead_signal_exit_minutes", 60))
        _ds_mfe_pct = float(_pb_cfg.get("dead_signal_exit_mfe_pct", 0.2))
        # ── Phase 0 (2026-08-23): Reentry 仓延长 DEAD_SIGNAL 呼吸窗 ──
        # VWAP 回归策略需 1-3 小时发酵, 而 DEAD_SIGNAL 在 60min+MFE<0.2% 即出场 →
        # 实测 reentry 前 6 笔中 4 笔被 DEAD_SIGNAL 杀掉 (CRM/TSM/AVGO/CSCO, 全部
        # 在入场后 1-2h 内)。reentry 不满足既有 W_WEEK 水下隔夜豁免 (日内新入场非隔夜)。
        # 修法: reentry 仓额外加宽 extension_min (默认 60, 即 120min 呼吸空间);
        # 120min 后仍 MFE<0.2% 一样出场 — 不是无限豁免, 只是让时间尺度匹配。
        # 回滚: dead_signal_reentry_extension_min 改 0。
        _ds_reentry_ext = float(_pb_cfg.get("dead_signal_reentry_extension_min", 60))
        if _ds_reentry_ext > 0:
            _did_str = str(meta.get("decision_id", "") or "")
            if _did_str.startswith("intraday_reentry"):
                _ds_minutes += _ds_reentry_ext
        # ── RFC-073 (2026-09-02): DEAD_SIGNAL 隔夜仓时间口径对齐 (与 RFC-064 TIME_EXIT 同款) ──
        # 隔夜仓 held_minutes 是入场以来墙钟 (数千分钟), 次日第一轮巡检恒 ≥60min →
        # 规则退化为"次日开盘第一眼, 生命周期MFE<0.2%即杀", 时间维度完全消失.
        # 修复: 隔夜仓用"今日开盘以来分钟数", 获得真实60min观察窗 —
        #   窗内价格兑现(峰值≥0.2%) → 条件不满足, 存活; 真死信号照杀.
        # 回放验证 (6笔1min bar, 悲观序): +$97 正效应; AMZN(基线0.027%)照杀.
        # 回滚: dead_signal_overnight_daily_reset_enabled 改 false + 重启.
        _ds_held_minutes = held_minutes
        if bool(_pb_cfg.get("dead_signal_overnight_daily_reset_enabled", True)) \
                and _is_overnight_position(entry_time_str):
            _ds_held_minutes = float(minutes_from_open or 0)
        if _ds_held_minutes >= _ds_minutes and peak_profit_pct < _ds_mfe_pct:
            # 8/17 净口径: 豁免的"水下"判定用净盈亏 — 毛 0<毛<费用 的微盈仓是净水下,
            #   同样在修复窗口内, 不该因"毛利>0"被误判失去豁免而出场白跑 (NVDA 8/17 实弹).
            if _loser_overnight_exempt(meta.get("_source_type", ""), entry_time_str, _profit_net_pct):
                # W_WEEK 豁免: 水下仓正在多日修复窗口内, 不因死信号提前出局.
                _dsx_log_key = f"{_symbol}:{date.today().isoformat()}"
                if _dsx_log_key not in _LOSER_DS_EXEMPT_LOGGED:
                    _LOSER_DS_EXEMPT_LOGGED.add(_dsx_log_key)
                    logger.info(
                        f"[规则3.3-死信号·W_WEEK豁免] {_symbol} {direction} held={held_minutes:.0f}min "
                        f"peak={peak_profit_pct:.2f}%<{_ds_mfe_pct}% — SIM水下仓多日持有豁免, "
                        f"转 SL/EOD转正/{_pb_cfg.get('loser_overnight_max_hold_days', 5)}日上限链"
                    )
            else:
                exit_reason = "DEAD_SIGNAL_EXIT"
                exit_price = current_price
                meta["dead_signal_exit"] = {
                    # check_minutes = 生效口径 (隔夜仓=今日开盘以来分钟, RFC-073); 保留
                    # wall_minutes 供历史对照 (旧口径 7183min 类数字即病灶自证样本).
                    "check_minutes": round(_ds_held_minutes, 1),
                    "wall_minutes": round(held_minutes, 1),
                    "mfe_at_trigger": round(peak_profit_pct, 4),
                    "cur_at_trigger": round(profit_pct, 4),
                    "threshold": _ds_mfe_pct,
                }
                logger.info(
                    f"[规则3.3-死信号] {_symbol} {direction} held={_ds_held_minutes:.0f}min "
                    f"(wall={held_minutes:.0f}min) "
                    f"peak={peak_profit_pct:.2f}%<{_ds_mfe_pct}% → 离场"
                )

    # 规则3.4 (2026-07-24, DEB-20260723 crash-day + DEB-20260724 rootcause): MFE停滞退出 —
    #   持仓≥stall_minutes 且 峰值浮盈从未达标 且 当前水下 → 立即离场, 不等 TIME_EXIT 磨满时间窗.
    #   数据依据 (PLAYBOOK 全历史): MFE<0.5% 且持有≥2.5h 共47笔仅3胜(+$18.7),
    #   TIME_EXIT 子集 0/33 -$1666; 赢家 MFE≥0.5% 平均 2.1h 内达成 → 2.5h 检查点几乎碰不到
    #   未来赢家 (历史误杀上限 $6.7: ORCL+1.0/COF+5.7).
    #   三重条件防误杀: 时间 × 峰值(从未≥0.5%) × 现状(水下); 盈利/曾浮盈达标的仓不受影响
    #   (零质量损失约束: EOD/TRAIL_LOCK 盈利路径完整保留). 回滚: mfe_stall_exit_enabled 改 false.
    if not exit_reason and bool(_pb_cfg.get("mfe_stall_exit_enabled", False)):
        _stall_minutes = float(_pb_cfg.get("mfe_stall_exit_minutes", 150))
        _stall_mfe_pct = float(_pb_cfg.get("mfe_stall_exit_mfe_pct", 0.5))
        if held_minutes >= _stall_minutes and peak_profit_pct < _stall_mfe_pct and profit_pct < 0:
            # 2026-08-04 (21笔反事实, 用户裁"同意 先调整再观察"): 检查点恒落午餐低谷,
            #   浅亏单市价卖系统性劣于持收 (+$158/21笔) — 浅亏转持有, 深亏维持立即退。
            _shallow_hold = bool(_pb_cfg.get("mfe_stall_exit_shallow_hold_enabled", False))
            _shallow_pct = float(_pb_cfg.get("mfe_stall_exit_shallow_pct", 1.0))
            if _shallow_hold and profit_pct > -_shallow_pct:
                if not meta.get("_stall_shallow_held"):
                    meta["_stall_shallow_held"] = True
                    logger.info(
                        f"[规则3.4-浅亏持有] {_symbol} {direction} held={held_minutes:.0f}min "
                        f"peak={peak_profit_pct:.2f}% cur={profit_pct:.2f}%>-{_shallow_pct}% "
                        f"— 不市价卖, 转 SL/PRE_CLOSE 收尾"
                    )
            else:
                exit_reason = "MFE_STALL_EXIT"
                exit_price = current_price
                # 2026-07-25 (首夜验证遗留): 触发现场字段入 meta — 零附近边界单 (如 MRK
                #   cur=-0.03% 触发、成交后落库 +$0.12) 事后归因不再翻日志。
                #   调用方退出落盘处从内存 meta 搬运 (主路径 _meta_out 用的是开环旧行).
                meta["stall_exit"] = {
                    "stall_minutes": round(held_minutes, 1),
                    "mfe_at_trigger": round(peak_profit_pct, 4),
                    "cur_at_trigger": round(profit_pct, 4),
                    "threshold": _stall_mfe_pct,
                }
                logger.info(
                    f"[规则3.4-MFE停滞] {_symbol} {direction} held={held_minutes:.0f}min "
                    f"peak={peak_profit_pct:.2f}%<{_stall_mfe_pct}% cur={profit_pct:.2f}% → 提前离场"
                )

    # 规则3: 时间退出 — 7/7 fix: 恢复此规则, 由 factor_exit_rules.time_exit_hours 驱动
    # 原因: 6/30 DEB 禁用时把 time_exit 交给 exit_policy.yaml, 但 7/3 fix 后 ExitMonitor 不再管
    #        PLAYBOOK 仓位。PLAYBOOK 仓位的 time_exit 必须由 Pipeline 自管。
    # 安全保障: time_exit_only_if_loss=true → 只砍亏损仓, 盈利仓不受影响
    #           factor 差异化: volume_expansion 0.5h / default 2h / macd_golden_cross 6h
    _time_exit_enabled = True
    _time_exit_hours = float(_exit_cfg.get("time_exit_hours", 2.0))
    _time_exit_only_if_loss = bool(_exit_cfg.get("time_exit_only_if_loss", True))
    # RFC-064 (2026-08-11, 语义B): 隔夜仓 TIME_EXIT 每日重置 — entry 在先前交易日时
    #   用"今日开盘以来分钟数"(minutes_from_open) 替代 wall-clock held_minutes,
    #   隔夜 ~17.5h 非交易时间不再计入, 防开盘第一轮检查超龄秒杀 (8/11 TSM 实锤).
    #   盘前 minutes_from_open<0 → 天然不触发. 日内单 _is_overnight_position=False, 不变.
    _te_held_minutes = held_minutes
    if bool(_pb_cfg.get("time_exit_overnight_daily_reset_enabled", True)) \
            and _is_overnight_position(entry_time_str):
        _te_held_minutes = float(minutes_from_open or 0)
    if _time_exit_enabled and not exit_reason and _te_held_minutes > (_time_exit_hours * 60):
        if not _time_exit_only_if_loss or profit_pct < 0:
            # (毛口径退出门保留: 切净口径会把微盈仓市价卖=白跑)
            # 2026-08-15 (§34): SIM多夜水下仓豁免 TIME_EXIT — 与 EOD 豁免同语义:
            #   W_WEEK 回放要求修复空间, 次日水下2h被砍则多夜持有名存实亡.
            #   SL 仍最高优先级兜底; 到期由 loser_overnight_max_hold_days 强平.
            # 8/17 净口径: 豁免水下判定用净盈亏 (微盈仓=净水下→豁免, 同 DEAD_SIGNAL/EOD).
            if not _loser_overnight_exempt(meta.get("_source_type", ""), entry_time_str, _profit_net_pct):
                exit_reason = "TIME_EXIT"
                exit_price = current_price

    # 规则5: MAX_HOLD — long horizon 最大持有期检查 (short horizon 默认不持夜, _skip_allowed 控制)
    # 2026-08-16 (用户裁"方案B", DEB§34 后续审计): W_WEEK 水下仓豁免 MAX_HOLD.
    #   根因: short_horizon max_hold_days=1, 若将来 overnight_hold_enabled 打开 →
    #   _skip_allowed=True → 水下仓第2天被 1日 cap 强平, W_WEEK 的 5 日窗口作废。
    #   豁免范围与 DEAD_SIGNAL/TIME_EXIT/EOD 同款 (_loser_overnight_exempt):
    #   仅 PLAYBOOK_SIM + 水下 + ≤loser_overnight_max_hold_days(5)。回滚: 删本 if 包裹。
    if not exit_reason and _skip_allowed and held_minutes > 0:
        if _horizon_max_hold_hit(entry_time_str, _max_hold_days):
            # 8/17 净口径: 豁免水下判定用净盈亏 (微盈仓=净水下→豁免, 同 DEAD_SIGNAL/EOD).
            if _loser_overnight_exempt(meta.get("_source_type", ""), entry_time_str, _profit_net_pct):
                logger.info(
                    f"[规则5-MAX_HOLD·W_WEEK豁免] {_symbol} {direction} 达 horizon max_hold="
                    f"{_max_hold_days}日 但 SIM水下仓在 "
                    f"{_pb_cfg.get('loser_overnight_max_hold_days', 5)}日修复窗口内, 豁免"
                )
            else:
                exit_reason = "MAX_HOLD"
                exit_price = current_price

    return exit_reason, exit_price, peak_price, breakeven_triggered
