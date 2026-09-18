"""持仓极值 (peak/trough) 的**唯一实现** — 2026-09-18 (SSOT 合并).

为什么有这个模块
----------------
`app/playbook/pipeline.py::_include_observed_prices_in_extremes` 与
`scripts/repair_trade_exit_invariants.py::_include_observed_prices` 曾各自实现同一件事,
且语义在"极值字段**存在但不可解析**"这一类输入上**相反**:

    pipeline 版: float() 失败 ⇒ 按 0/缺失处理 ⇒ 用观测端点**覆盖**该字段
    repair   版: float() 失败 ⇒ `continue` ⇒ **跳过**该字段, 保留原值

同一类输入两种行为, 谁先改谁silently alter 另一条链的口径。合并成一份是这里的唯一目的;
管线与修补脚本必须共用同一函数 —— 换口径时一起换, 不靠人记。

口径 (逐字取 pipeline 原实现, 禁在此"顺手优化")
-----------------------------------------------
* LONG  : peak = max(已有峰, 观测高) ; trough = min(已有谷, 观测低)
* SHORT : peak = min(已有峰, 观测低) ; trough = max(已有谷, 观测高)
          (空头最有利=最低价, 与 app/risk/atr_stop_engine 的 peak 注释一致)
* 0/负值视为"缺失"(观测值直接落位); 只在真的变了才写回; 返回是否发生改写。
* 覆盖 `peak_price/trough_price` 与 `peak_price_bar/trough_price_bar` 两套键。

副作用边界: 只改传入的 dict (调用方传的是 `json.loads(...)` 出来的**新副本**),
不碰活仓对象 —— atr_stop_engine 的 chandelier 用的是在仓内存态, 不受影响。
"""
from __future__ import annotations


def include_observed_prices(metadata, direction, entry_price, exit_price) -> bool:
    """把观测到的入场/出场价并入极值。返回是否发生改写 (供调用方决定要不要落库)。"""
    if not isinstance(metadata, dict):
        return False
    try:
        entry_value = float(entry_price)
        exit_value = float(exit_price)
    except (TypeError, ValueError):
        return False
    if entry_value <= 0 or exit_value <= 0:
        return False

    short = str(direction or "LONG").upper() in {"SHORT", "SELL_SHORT"}
    changed = False
    for peak_key, trough_key in (
        ("peak_price", "trough_price"),
        ("peak_price_bar", "trough_price_bar"),
    ):
        try:
            peak = float(metadata.get(peak_key, 0) or 0)
            trough = float(metadata.get(trough_key, 0) or 0)
        except (TypeError, ValueError):
            peak = 0.0
            trough = 0.0

        observed_low = min(entry_value, exit_value)
        observed_high = max(entry_value, exit_value)
        if short:
            new_peak = min(peak, observed_low) if peak > 0 else observed_low
            new_trough = max(trough, observed_high)
        else:
            new_peak = max(peak, observed_high)
            new_trough = min(trough, observed_low) if trough > 0 else observed_low

        new_peak = round(new_peak, 4)
        new_trough = round(new_trough, 4)
        if peak != new_peak or trough != new_trough:
            metadata[peak_key] = new_peak
            metadata[trough_key] = new_trough
            changed = True
    return changed