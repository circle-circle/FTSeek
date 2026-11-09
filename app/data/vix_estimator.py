# -*- coding: utf-8 -*-
"""VIX 水平/变化分离估计器 (2026-11-09, RFC-190 第六轮)。

问题 (2026-11-05 实证):
  - **水平**: 只有 T-1 真值 (FRED/CBOE 收盘) —— 准, 但慢一天;
  - **变化**: 只有 VIXY×0.85 实时代理 —— 快, 但口径偏差大 (正常情况下高估 ~7 点,
    backwardation 时又低估 ~10 点, 见 DEB §58/§63)。
  把两者混成一个数用, 就会在崩盘当天看到"平静"、在崩盘次日看到"高波动"。

本模块只做一件事: 用 **T-1 真值做锚**, 用 **VIXY 的当日收益率** 做变化量, 得到当日
水平的实时估计。不设阈值、不写库、不判交易 —— 由调用方决定怎么用。

标定 (2026-11-09, 359 个交易日, FRED VIXCLS vs Yahoo VIXY 收盘):
    ΔVIX% ≈ beta × ΔVIXY%      beta=2.07,  R²=0.85,  corr=0.90
    滚动 60 日 beta 中位 2.15, 区间 1.48–2.60; 对数收益 beta=1.99
    (beta>1 来自 VIX 对短期期货的凸性; 不是杠杆 ETF 的 2× 结构)

    预测误差 (同日收盘):
      仅用昨收真值        MAE 1.04
      VIXY×0.85 (旧盘中)  MAE 7.68  (bias +7.06)
      真值锚定 × beta=2   MAE 0.43  ← 采用
      真值锚定 × beta=1   MAE 0.64

失效边界:
  - beta 会漂移 (滚动区间 1.48–2.60) ⇒ 必须定期重标: `scripts/vixy_beta_calibration.py`;
  - 极端日 (VIXY 单日 ±30%) 非线性放大, 对 ret 做 [-60%, +150%] 截断;
  - 盘前若 VIXY 无 pre_price, 不能用 last_price 假装"今日变化"(那是昨收) —— 返回 None。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 标定值; 重标脚本会打印新值, 改动须同步本常量并在 DEB 记录
BETA_DEFAULT = 2.0
RET_FLOOR = -0.60      # 单日代理收益下限 (截断, 防 -100% 类异常)
RET_CAP = 1.50         # 单日代理收益上限
JUMP_TRIGGER = 0.25    # 估计水平较 T-1 真值上跳 >=25% ⇒ 跳变告警 (11-05 当日 +60%)


def estimate_level(prev_truth: Optional[float],
                   vixy_prev_close: Optional[float],
                   vixy_live: Optional[float],
                   beta: float = BETA_DEFAULT) -> Optional[float]:
    """T-1 真值 × (1 + beta × VIXY 当日收益率)。任一输入不可用 ⇒ None (不猜)。"""
    try:
        prev_truth = float(prev_truth) if prev_truth is not None else None
        vixy_prev_close = float(vixy_prev_close) if vixy_prev_close is not None else None
        vixy_live = float(vixy_live) if vixy_live is not None else None
    except (TypeError, ValueError):
        return None
    if not prev_truth or prev_truth <= 0:
        return None
    if not vixy_prev_close or vixy_prev_close <= 0:
        return None
    if not vixy_live or vixy_live <= 0:
        return None
    ret = vixy_live / vixy_prev_close - 1.0
    ret = max(RET_FLOOR, min(RET_CAP, ret))
    est = prev_truth * (1.0 + beta * ret)
    return round(max(1.0, est), 2)


def change_pct(prev_truth: Optional[float], est: Optional[float]) -> Optional[float]:
    if not prev_truth or not est or prev_truth <= 0:
        return None
    return round((est / float(prev_truth) - 1.0) * 100, 2)


def is_jump(prev_truth: Optional[float], est: Optional[float],
            trigger: float = JUMP_TRIGGER) -> bool:
    """仅上跳 (波动率飙升) 触发; VIX 快速回落不告警 (不会因此加仓)。"""
    pct = change_pct(prev_truth, est)
    return pct is not None and pct >= trigger * 100


def describe(prev_truth: Optional[float], prev_as_of: Optional[str],
             vixy_prev_close: Optional[float], vixy_live: Optional[float],
             est: Optional[float], beta: float = BETA_DEFAULT) -> str:
    if est is None:
        return "VIX 实时估计: N/A (缺 T-1 真值或 VIXY 报价)"
    pct = change_pct(prev_truth, est)
    ret = (vixy_live / vixy_prev_close - 1.0) * 100 if vixy_prev_close else 0.0
    return (f"VIX 实时估计 {est:.2f} (T-1 真值 {float(prev_truth):.2f}@{prev_as_of or '?'} "
            f"× (1+{beta:.1f}×VIXY {ret:+.1f}%) = {pct:+.0f}%; 非今日成交真值)")