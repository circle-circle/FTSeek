"""退出参数影子 (2026-07-13) — record-only, 不改任何交易动作。

背景 (数据驱动): capture_ratio 分布实测中位仅 0.17、40% 盈利单浮盈变亏损,
而 MFE 中位 0.92% —— 当前 default breakeven_at_pct=1.0 高于 MFE 中位, 过半交易
"保本永不触发" 就把浮盈全数回吐。假设: 更早保本 + 更紧回撤锁利能提升 capture。

做法: 在盘中每轮巡检时, 用与 _compute_dynamic_effective_sl 完全一致的纯逻辑,
回放几组【比现行更紧】的 breakeven/drawdown 变体 (更紧变体必在实盘退出前触发,
故其反事实退出价落在【已观测到的价格路径】内, 可忠实评估)。记录各变体首次触发
(仿 invalidation_shadow), close 时对比各变体 vs 实盘的 capture_ratio 落 JSONL。

安全不变量: 纯记录。所有入口自带 try/except, 任何异常都吞掉, 绝不影响实盘退出。
由 feature flag `exit_param_shadow` 控制 (默认 on, 可随时关)。

⚠️ 只能评估【比现行更紧】的变体 —— 更松的变体会骑过实盘退出点, 路径不可观测。

2026-07-18 新增 (三轮辩论裁决):
  - 提案 A: profit_override 变体 — 浮盈≥1.5% 时强制锁利 (dd=0.5%),
    针对 trailing_enabled=false 因子的利润回吐问题
  - 提案 B: dead_signal_exit 变体 — 60min 时 MFE<0.2%/0.3% 则标记信号失效,
    仅对 time_exit_hours≥4 的因子 + default(空 conditions) 路径
  - 60min-MFE 打点: 每笔仓位在 60min 时点显式记录当前 MFE, 供 B 阈值定稿

2026-07-19 新增 (DEB-round3 执行清单):
  - 30min-abort 变体 — 持仓 30min 时 MFE<0.1% 且浮亏 → would_abort 标记,
    close 时反事实 counterfactual_saved_pct (正=abort更优). 全路径适用.
    背景: TIME_EXIT 11笔0%WR 根因=方向未兑现, 但实单 abort 反事实 -$160
    (杀 EOD 慢热赢家), 故仅 shadow 验证, 2 周数据定是否实单化.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from app.llm.features.feature_flags import is_enabled
except Exception:  # pragma: no cover - flag 模块不可用时默认开启记录
    def is_enabled(_feature: str) -> bool:
        return True

# 待测变体 (均比现行 default be=1.0/dd=0.5 更紧, 保证反事实退出可观测)。
#   baseline 为对照组 = 现行 default, 用于同口径比对。
EXIT_VARIANTS: Dict[str, Dict[str, float]] = {
    "baseline_be1.0_dd0.5": {"be": 1.0, "dd": 0.5},   # 对照: 现行 default
    "be0.5_dd0.4":          {"be": 0.5, "dd": 0.4},   # 更早保本 + 更紧锁利
    "be0.3_dd0.3":          {"be": 0.3, "dd": 0.3},   # 激进锁利 (贴合 MFE 中位 ~0.9%)
    # ── 2026-07-18 提案 A: profit_override ──
    # 浮盈≥1.5% 时强制启用回撤锁利 (dd=0.5%), 针对 trailing_enabled=false 因子.
    # be=0.5 保持现行保本触发, dd_threshold=1.5 表示回撤锁利仅在浮盈≥1.5%后激活.
    "profit_override_1.5_dd0.5": {"be": 0.5, "dd": 0.5, "dd_threshold": 1.5},
}

# ── 2026-07-18 提案 B: dead_signal_exit 变体 ──
# 60min 检查点, MFE < 阈值则标记信号失效 (仅 record, 不触发真实退出).
# 双档阈值并行: 0.2% / 0.3%, 用真实采集数据定稿.
DEAD_SIGNAL_VARIANTS: Dict[str, Dict[str, float]] = {
    "dead_signal_60m_mfe0.2": {"check_minutes": 60, "mfe_threshold": 0.2},
    "dead_signal_60m_mfe0.3": {"check_minutes": 60, "mfe_threshold": 0.3},
}

# ── 2026-07-19 (DEB-round3): TIME_EXIT 30min-abort 变体 ──
# 背景: TIME_EXIT 11 笔 0%WR -$312, 根因=方向未兑现 (MFE 全<0.5%) — 持仓 30min
#   仍 MFE<0.1% 且浮亏的单, 后续大概率 TIME_EXIT 亏损.
# 但实单 30min abort 反事实净 -$160 (杀掉 EOD 慢热赢家) → 只能 shadow 验证, 不实单.
# 全路径适用 (不限 time_exit_hours 因子), record-only.
ABORT_30M_VARIANTS: Dict[str, Dict[str, float]] = {
    "abort_30m_mfe0.1_loss": {"check_minutes": 30, "mfe_threshold": 0.1, "require_loss": 1},
}

# app/playbook/exit_shadow.py → parents[2] = 仓库根
_SHADOW_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "exit_param_shadow.jsonl"


def _variant_effective_sl(direction: str, entry: float, initial_sl: float,
                          peak_profit_pct: float, be_pct: float, dd_pct: float,
                          dd_threshold: float = 0.0) -> float:
    """纯函数: 忠实复刻 pipeline._compute_dynamic_effective_sl (trailing_enabled=True 分支)。

    保持与实盘退出同一数学, 只替换 breakeven_at_pct / drawdown_from_peak_pct 两个参数。

    2026-07-18: 新增 dd_threshold 参数 (提案 A profit_override):
      dd_threshold > 0 时, 回撤锁利仅在 peak_profit_pct >= dd_threshold 后激活,
      而非 peak > be_pct 即激活。模拟「浮盈≥1.5% 才强制锁利」的行为。
    """
    eff = initial_sl
    if peak_profit_pct < be_pct:
        return eff
    # 先保本
    if direction == "LONG":
        eff = max(eff, entry)
    else:
        eff = min(eff if eff > 0 else entry, entry)
    # 回撤锁利 (dd_threshold: 浮盈达到此值后才启用回撤锁利)
    _dd_active_threshold = dd_threshold if dd_threshold > 0 else be_pct
    if dd_pct > 0 and peak_profit_pct > _dd_active_threshold:
        if direction == "LONG":
            peak = entry * (1 + peak_profit_pct / 100)
            dd_sl = peak * (1 - dd_pct / 100)
            eff = max(eff, dd_sl)
        else:
            peak = entry * (1 - peak_profit_pct / 100)
            dd_sl = peak * (1 + dd_pct / 100)
            eff = min(eff if eff > 0 else entry, dd_sl)
    return eff


def record_exit_shadow_poll(meta: Dict[str, Any], *, direction: str, entry_price: float,
                            initial_sl: float, current_price: float,
                            peak_profit_pct: float, held_minutes: float,
                            time_exit_hours: float = 0.0) -> bool:
    """每轮巡检: 回放各变体, 记录首次触发到 meta["_exit_shadow"]。record-only, 永不抛异常。

    2026-07-18 新增:
      - 60min-MFE 打点: held_minutes 跨越 60 时记录当前 peak_profit_pct
      - dead_signal_exit 变体: 60min 时 MFE < 阈值则标记
      - time_exit_hours: 调用方传入当前因子的 time_exit_hours, 用于 B 的作用域判断

    返回 True 表示本轮有【新】变体触发或新打点 (供调用方决定是否写回 metadata)。
    """
    try:
        if not is_enabled("exit_param_shadow"):
            return False
        entry_price = float(entry_price or 0)
        current_price = float(current_price or 0)
        if entry_price <= 0 or current_price <= 0:
            return False
        sh = meta.get("_exit_shadow") or {}
        new_trigger = False

        # ── 原有: breakeven/drawdown 变体回放 ──
        for name, p in EXIT_VARIANTS.items():
            if name in sh:  # 只记首次触发, 已触发的不覆盖
                continue
            eff = _variant_effective_sl(direction, entry_price, float(initial_sl or 0),
                                        peak_profit_pct, p["be"], p["dd"],
                                        p.get("dd_threshold", 0.0))
            if eff <= 0:
                continue
            hit = ((direction == "LONG" and current_price <= eff) or
                   (direction != "LONG" and current_price >= eff))
            if hit:
                exit_pct = (((eff - entry_price) if direction == "LONG"
                             else (entry_price - eff)) / entry_price * 100)
                sh[name] = {
                    "exit_price": round(eff, 4),
                    "held_minutes": round(float(held_minutes or 0), 1),
                    "profit_pct": round(exit_pct, 4),
                }
                new_trigger = True

        # ── 2026-07-18: 60min-MFE 打点 (所有仓位, 不分因子) ──
        _held = float(held_minutes or 0)
        if "_mfe_at_60min" not in sh and _held >= 60:
            sh["_mfe_at_60min"] = round(float(peak_profit_pct or 0), 4)
            new_trigger = True

        # ── 2026-07-18 提案 B: dead_signal_exit 变体 ──
        # 作用域: 仅 time_exit_hours >= 4 的因子 + default (time_exit_hours=0 表示未传入/空 conditions)
        _te_h = float(time_exit_hours or 0)
        _b_eligible = (_te_h >= 4.0 or _te_h == 0.0)  # 0 = default/空 conditions 路径
        if _b_eligible and _held >= 60:
            for name, bp in DEAD_SIGNAL_VARIANTS.items():
                if name in sh:
                    continue
                _check_min = float(bp["check_minutes"])
                if _held >= _check_min:
                    _mfe_now = float(peak_profit_pct or 0)
                    _threshold = float(bp["mfe_threshold"])
                    # 标记: 在检查点时 MFE 是否低于阈值 (信号未兑现)
                    sh[name] = {
                        "triggered": _mfe_now < _threshold,
                        "mfe_at_check": round(_mfe_now, 4),
                        "held_minutes": round(_held, 1),
                        "threshold": _threshold,
                    }
                    new_trigger = True

        # ── 2026-07-19 (DEB-round3): 30min-abort 变体 (全路径, 不限 time_exit_hours) ──
        # held>=30min 时点记录: would_abort = MFE<阈值 且 当前浮亏.
        # pnl_at_check 供 finalize 算反事实收益 (abort 在检查点价平 vs 实盘持有到底).
        for name, ap in ABORT_30M_VARIANTS.items():
            if name in sh:
                continue
            if _held >= float(ap["check_minutes"]):
                _mfe_now = float(peak_profit_pct or 0)
                _cur_pct = (((current_price - entry_price) if direction == "LONG"
                             else (entry_price - current_price)) / entry_price * 100)
                _would = (_mfe_now < float(ap["mfe_threshold"])
                          and (not ap.get("require_loss") or _cur_pct < 0))
                sh[name] = {
                    "would_abort": _would,
                    "mfe_at_check": round(_mfe_now, 4),
                    "pnl_at_check": round(_cur_pct, 4),
                    "held_minutes": round(_held, 1),
                }
                new_trigger = True

        if new_trigger:
            meta["_exit_shadow"] = sh
        return new_trigger
    except Exception:
        return False


def _append_jsonl(record: Dict[str, Any]) -> None:
    _SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SHADOW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def finalize_exit_shadow(*, symbol: str, direction: str, entry_price: float,
                         live_exit_price: float, peak_price: float, trough_price: float,
                         live_exit_reason: str, held_minutes: float,
                         meta: Dict[str, Any], inst_id=None) -> Optional[Dict[str, Any]]:
    """close 时: 对比各变体反事实 capture vs 实盘 capture, 落 JSONL。record-only, 永不抛异常。

    变体未触发 → 视同随实盘退出 (capture 与实盘一致)。capture = 已实现有利% / 最大浮盈%。

    2026-07-18: 新增 dead_signal 变体结果 + 60min-MFE 打点写入 JSONL。
    """
    try:
        if not is_enabled("exit_param_shadow"):
            return None
        direction = (direction or "LONG").upper()
        entry_price = float(entry_price or 0)
        peak_price = float(peak_price or 0)
        if entry_price <= 0:
            return None

        if direction == "LONG":
            max_fav_pct = (peak_price - entry_price) / entry_price * 100 if peak_price > 0 else 0.0
        else:
            max_fav_pct = (entry_price - peak_price) / entry_price * 100 if peak_price > 0 else 0.0

        def _realized_pct(px: float) -> float:
            px = float(px or 0)
            return (((px - entry_price) if direction == "LONG"
                     else (entry_price - px)) / entry_price * 100)

        live_pct = _realized_pct(live_exit_price)
        live_cap = round(live_pct / max_fav_pct, 3) if max_fav_pct > 0 else None

        sh = meta.get("_exit_shadow") or {}
        variants_out: Dict[str, Any] = {}
        for name in EXIT_VARIANTS:
            if name in sh:
                v_exit = float(sh[name].get("exit_price", 0) or 0)
                v_pct = float(sh[name].get("profit_pct", 0) or 0)
                triggered = True
            else:
                v_exit = float(live_exit_price or 0)  # 未触发 → 随实盘退出
                v_pct = live_pct
                triggered = False
            v_cap = round(v_pct / max_fav_pct, 3) if max_fav_pct > 0 else None
            variants_out[name] = {
                "triggered": triggered,
                "exit_price": round(v_exit, 4),
                "realized_pct": round(v_pct, 4),
                "capture": v_cap,
            }

        # ── 2026-07-18: dead_signal 变体结果 ──
        dead_signal_out: Dict[str, Any] = {}
        for name in DEAD_SIGNAL_VARIANTS:
            if name in sh:
                dead_signal_out[name] = sh[name]
            else:
                dead_signal_out[name] = {"triggered": False, "reason": "not_evaluated"}

        # ── 2026-07-19 (DEB-round3): 30min-abort 反事实 ──
        # counterfactual_saved_pct = pnl_at_check - live_pct
        #   正 = abort 更优 (实盘继续持有回吐/扩大亏损); 负 = 实盘更优 (abort 杀了慢热赢家).
        #   2 周样本后若 mean(saved) > 0 且would_abort命中率稳定, 才评估实单化.
        abort_out: Dict[str, Any] = {}
        for name in ABORT_30M_VARIANTS:
            if name in sh:
                _pnl_chk = float(sh[name].get("pnl_at_check", 0) or 0)
                abort_out[name] = {
                    **sh[name],
                    "counterfactual_saved_pct": round(_pnl_chk - live_pct, 4),
                }
            else:
                abort_out[name] = {"would_abort": False, "reason": "not_evaluated"}

        rec = {
            "ts": datetime.utcnow().isoformat(),
            "inst_id": inst_id,
            "symbol": symbol,
            "direction": direction,
            "entry": round(entry_price, 4),
            "peak": round(peak_price, 4),
            "trough": round(float(trough_price or 0), 4),
            "max_fav_pct": round(max_fav_pct, 4),
            "held_minutes": round(float(held_minutes or 0), 1),
            "live": {
                "exit_reason": live_exit_reason,
                "exit_price": round(float(live_exit_price or 0), 4),
                "realized_pct": round(live_pct, 4),
                "capture": live_cap,
            },
            "variants": variants_out,
            # 2026-07-18 新增字段
            "mfe_at_60min": sh.get("_mfe_at_60min"),  # 60min-MFE 打点 (可能 None = 持仓<60min)
            "dead_signal": dead_signal_out,
            # 2026-07-19 (DEB-round3) 新增字段
            "abort_30m": abort_out,
        }
        _append_jsonl(rec)
        return rec
    except Exception:
        return None
