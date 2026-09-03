"""保本线穿透影子 (2026-09-03, DEB-20260903 议题二后续, 用户裁"A"全量执行) — record-only.

背景: 被动侧击穿 (检测价跌穿净保本地板) 现有证据 = 家族5案0回摆全负,
marketable limit 立即成交保持为默认执行路径 (不动)。但 DELL 9/2 案提示另一类
样本: 穿透若是"缓慢临时回撤" (V型), 立即离场把回摆让给市场。"快跌 vs V回"
的判别特征今天无人知道 (DELL n=1), 拍脑袋写阈值 = 引入混乱 → 先攒样:

  ① 本模块: 穿透触发瞬间采集判别特征 → data/shadow/penetration_shadow.jsonl
  ② scripts/penetration_outcome_backfill.py: 每晚用离场后 1 分 bar 回填结局标签
     (离场后 1/3/5/10min 是否回摆过地板 = V-recovery vs 续跌 = correct_exit)
  ③ scripts/penetration_llm_analysis.py: 周频 LLM 提假设 + 确定性 replay 判分

为什么不在持仓内评估"多持有一次等回摆": exit_shadow 铁律 — 更松变体会骑过实盘
退出点, 路径不可观测, 只能离场后回填 (同 cf_pullback_limit_replay 口径)。

安全不变量 (同 exit_shadow): 纯记录。所有入口自带 try/except, 任何异常吞掉,
绝不影响实盘退出。feature flag `penetration_shadow` 默认 on, 单键全关。
KR2 红线: 影子键一律不进 exit config 哈希 (record-only 不影响已实现 PnL)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from app.llm.features.feature_flags import is_enabled
except Exception:  # pragma: no cover - flag 模块不可用时默认开启记录
    def is_enabled(_feature: str) -> bool:
        return True

# app/playbook/penetration_shadow.py → parents[2] = 仓库根
_PEN_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "penetration_shadow.jsonl"


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None  # NaN 守卫
    except (TypeError, ValueError):
        return None


def _atr_pct(symbol: str, price: float) -> Optional[float]:
    """日线 ATR% (与 UE 低ATR预检同源 provider), fail-open 三层: 任何失败返回 None."""
    try:
        from app.core.di_container import get_di_container
        provider = get_di_container().atr_provider()
        sym = str(symbol or "")
        code = sym if sym.startswith("US.") else f"US.{sym}"
        v = provider.get_atr_pct(code, float(price))
        return round(float(v), 4) if v else None
    except Exception:
        return None


def record_penetration(*, symbol: str, direction: str, exit_reason: str,
                       side: str, path: str, trigger_price: float,
                       floor_px: float, entry_price: float, peak_price: float,
                       trough_price: float, held_minutes: float,
                       quote_src: str, inst_id: Any = None,
                       telemetry: Optional[Dict[str, Any]] = None) -> bool:
    """穿透触发瞬间采集判别特征, append 一行 type=penetration。record-only, 永不抛异常。

    特征口径 (全 direction 镜像为"有利方向为正"):
      depth_bps        穿透深度 = (地板-检测价)/地板, 正=已被击穿 (被动侧), 负=未破 (主动侧)
      mfe_pct          峰值浮盈% (peak_price 语义对齐 exit_shadow: 最有利极值价)
      giveback_bps     自峰值回吐 = (峰值-检测价)/入场, 正=在回吐
      fresh_extreme    检测价是否仍在刷本轮最劣极值 (≈仍在下跌/上涨途中)
      minutes_since_open / et_hm  时间带
      atr_pct / depth_atr_ratio   日线ATR归一 (fail-open, 可为 None)
    """
    try:
        if not is_enabled("penetration_shadow"):
            return False
        # 测试卫生守卫 (同 exit_shadow/finalize 约定): TEST 标的不落生产影子
        if "TEST" in str(symbol or "").upper():
            return False
        trig = _f(trigger_price)
        floor = _f(floor_px)
        entry = _f(entry_price)
        if not trig or not floor or not entry or trig <= 0 or floor <= 0 or entry <= 0:
            return False
        direction = (str(direction or "LONG")).upper()
        is_long = direction == "LONG"

        depth_bps = ((floor - trig) if is_long else (trig - floor)) / floor * 10000.0
        peak = _f(peak_price) or entry
        mfe_pct = (((peak - entry) if is_long else (entry - peak)) / entry * 100.0)
        giveback_bps = (((peak - trig) if is_long else (trig - peak)) / entry * 10000.0)
        trough = _f(trough_price)
        fresh_extreme = None
        if trough and trough > 0:
            fresh_extreme = bool(trig <= trough * 1.0002) if is_long else bool(trig >= trough * 0.9998)

        from app.utils.timezone import now_et, now_utc
        et = now_et()
        minutes_since_open = (et.hour * 60 + et.minute) - (9 * 60 + 30)
        atr = _atr_pct(str(symbol), trig)
        depth_pct = abs(depth_bps) / 100.0
        row = {
            "type": "penetration",
            "event_id": f"{symbol}-{et.strftime('%Y%m%d%H%M%S')}",
            "ts_utc": now_utc().isoformat(),
            "et_date": et.strftime("%Y-%m-%d"),
            "et_hm": et.strftime("%H:%M"),
            "symbol": symbol,
            "direction": direction,
            "inst_id": inst_id,
            "exit_reason": exit_reason,
            "side": side,
            "path": path,
            "quote_src": quote_src,
            "entry": round(entry, 4),
            "trigger_price": round(trig, 4),
            "floor_px": round(floor, 4),
            "depth_bps": round(depth_bps, 1),
            "mfe_pct": round(mfe_pct, 4),
            "giveback_bps": round(giveback_bps, 1),
            "fresh_extreme": fresh_extreme,
            "held_minutes": round(_f(held_minutes) or 0.0, 1),
            "minutes_since_open": minutes_since_open,
            "atr_pct": atr,
            "depth_atr_ratio": (round(depth_pct / atr, 3) if atr else None),
        }
        if telemetry:
            row["limit_px"] = telemetry.get("limit_px")
            row["pad_bps"] = telemetry.get("pad_bps")
        _PEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PEN_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
