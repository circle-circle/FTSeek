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
}

# app/playbook/exit_shadow.py → parents[2] = 仓库根
_SHADOW_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "exit_param_shadow.jsonl"


def _variant_effective_sl(direction: str, entry: float, initial_sl: float,
                          peak_profit_pct: float, be_pct: float, dd_pct: float) -> float:
    """纯函数: 忠实复刻 pipeline._compute_dynamic_effective_sl (trailing_enabled=True 分支)。

    保持与实盘退出同一数学, 只替换 breakeven_at_pct / drawdown_from_peak_pct 两个参数。
    """
    eff = initial_sl
    if peak_profit_pct < be_pct:
        return eff
    # 先保本
    if direction == "LONG":
        eff = max(eff, entry)
    else:
        eff = min(eff if eff > 0 else entry, entry)
    # 回撤锁利
    if dd_pct > 0 and peak_profit_pct > be_pct:
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
                            peak_profit_pct: float, held_minutes: float) -> bool:
    """每轮巡检: 回放各变体, 记录首次触发到 meta["_exit_shadow"]。record-only, 永不抛异常。

    返回 True 表示本轮有【新】变体触发 (供调用方决定是否写回 metadata)。
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
        for name, p in EXIT_VARIANTS.items():
            if name in sh:  # 只记首次触发, 已触发的不覆盖
                continue
            eff = _variant_effective_sl(direction, entry_price, float(initial_sl or 0),
                                        peak_profit_pct, p["be"], p["dd"])
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
        }
        _append_jsonl(rec)
        return rec
    except Exception:
        return None
