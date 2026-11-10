"""RFC-188 分段落袋线 (staged lock) — record-only 影子, 零交易行为变更.

## 为什么要它
现役 `breakeven_at_pct=0.4` 把最大的人群 (MFE∈[0.4,0.8), A10 实测 288/780 = 36.9%) 全部
剪在保本线上: 该桶 85.8% 以 SL_BREAKEVEN 出场, 均 pnl **+0.074%**, 低于往返费用线 ~11.9bps
⇒ 这一桶结构性为负。而它的峰值本来有 40~80bps 可用。

## 设计 (三态, 按峰值比例给保护, 不退化成"到点平仓")
    S0  peak < dd_min_peak_pct            → SL = 初始 SL (不动)
    S1  dd_min ≤ peak < arm_hi            → SL = entry ± (peak − dd_pct)      # 按自身峰值比例
    S2  peak ≥ arm_hi                     → SL = entry ± max(lock_pct, peak − dd_pct)
不变式:
  ① SL 只朝有利方向移动, 且永不差于初始 SL (RATCHET 地板);
  ② 到 arm_hi **不平仓**, 只把止损抬到 lock_pct — 平仓会砍掉右尾
     (MFE≥0.8 的 289 笔若一律封顶 0.8, A10 4 个月毛额 **−$6,671**);
  ③ 本模块只算线、只落盘, 绝不参与真实出场判定 (record-only)。

## 预注册判据 (graduation_criteria, 落地前不改)
    n_required: 120 笔独立仓 (或 2 周, 以先到为准)
    primary:    该桶 (live 以 SL_BREAKEVEN 出场且 mfe ∈ [dd_min, arm_hi)) 的反事实净 bps
                均值 ≥ +8bps 且 ≥ live 同批 +5bps (paired)
    guardrail:  全样本 MAE≥2% 的笔占比不得上升 >10pp
    kill:       反事实净额 ≤ live (同批 paired), 或 top1 标的贡献 >50% 总差
    ⚠️ 历史回放不可用于本轴裁决: 官方引擎连续三次撞保本门
       (2026-10-14 v1 reason_match 0.588 / v2 0.729; 2026-11-06 pess 53.6% / opt 182.6%)
       ⇒ 只能前向攒样。

## 开关
    config/unified_config.yaml → playbook.exit_staging_shadow.enabled (默认 false)
    读取走 ConfigContainer.get_dotted (顶层 playbook.<key> 不在 get_playbook_config 的合并面内)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "arm_hi_pct": 0.8,      # S2 武装线 (原 breakeven 0.4)
    "lock_pct": 0.4,        # S2 锁利幅度 (0 = 净保本, 即现役行为)
    "dd_pct": 0.4,          # 从峰值回撤多少就锁 (原 drawdown_from_peak 0.8)
    "dd_min_peak_pct": 0.3,  # dd 起算门 — 与 BE 解耦 (现役 dd 被 BE 门锁在 0.4 以上)
    "min_interval_s": 300,  # 同一仓同阶段最小落盘间隔
}

SHADOW_PATH = "data/shadow/exit_staging_shadow.jsonl"

GRADUATION_CRITERIA: Dict[str, Any] = {
    "n_required": 120,
    "window": "2 周 或 n≥120 (先到为准)",
    "primary": "该桶反事实净 bps 均值 ≥ +8 且 ≥ live +5bps (paired)",
    "guardrail": "全样本 MAE≥2% 笔占比升幅 ≤ 10pp",
    "kill": "反事实净额 ≤ live, 或 top1 标的 >50% 总差",
    "instrument_note": "历史回放不可裁决 (三次保真门失败), 只前向",
}


def get_config() -> Dict[str, Any]:
    """读 `playbook.exit_staging_shadow`; 容器不可用 → 内置缺省 (默认关)."""
    cfg = dict(_DEFAULTS)
    try:
        from app.core.config_container import get_config as _gc
        override = _gc().get_dotted("playbook.exit_staging_shadow", None)
    except Exception:
        override = None
    if isinstance(override, dict):
        for k, v in override.items():
            if k in cfg and v is not None:
                cfg[k] = v
    return cfg


def staged_sl(direction: str, entry: float, initial_sl: float, peak_pct: float,
              arm_hi_pct: float = 0.8, lock_pct: float = 0.4,
              dd_pct: float = 0.4, dd_min_peak_pct: float = 0.3,
              slip_pct: float = 0.05) -> Tuple[float, str]:
    """分段落袋线 → (sl_price, stage). 纯函数, 无 IO.

    stage: S0 未武装 / S1 比例保护 / S2 锁利武装.
    """
    direction = (direction or "").upper()
    sign = 1.0 if direction in ("LONG", "BUY") else -1.0
    if peak_pct < dd_min_peak_pct:
        return float(initial_sl), "S0"
    pct = peak_pct - dd_pct
    if peak_pct >= arm_hi_pct and pct < lock_pct:
        pct = float(lock_pct)          # S2 锁利地板
    if pct <= 0:
        # 锁到保本线或以下没有意义 (等于以现价平仓) ⇒ 不武装, 保持初始 SL
        return float(initial_sl), "S0"
    sl = entry * (1.0 + sign * pct / 100.0)
    # 地板: 不得差于初始止损 (ratchet 不变式)
    sl = max(sl, initial_sl) if sign > 0 else min(sl, initial_sl)
    return round(sl, 6), ("S2" if peak_pct >= arm_hi_pct else "S1")


def would_exit(direction: str, cur: float, sl: float) -> bool:
    """该线在现价下会不会触发 (只用于影子记录, 不驱动真实出场)."""
    sign = 1.0 if (direction or "").upper() in ("LONG", "BUY") else -1.0
    return (cur - sl) * sign <= 0.0


def record(direction: str, symbol: str, entry: float, cur: float, peak_pct: float,
           initial_sl: float, live_sl: float, held_minutes: float,
           meta: Optional[Dict[str, Any]] = None,
           path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """落一行影子. 开关关 / 去重命中 / 参数非法 → 返回 None, 绝不抛. """
    cfg = get_config()
    if not cfg.get("enabled"):
        return None
    # 测试卫生: pytest 环境下默认不落盘 (防单测灌进生产 jsonl); 显式传 path 的用例不受影响。
    if path is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        stage_price, stage = staged_sl(
            direction, entry, initial_sl, peak_pct,
            arm_hi_pct=float(cfg["arm_hi_pct"]), lock_pct=float(cfg["lock_pct"]),
            dd_pct=float(cfg["dd_pct"]), dd_min_peak_pct=float(cfg["dd_min_peak_pct"]),
        )
        # 去重: 同仓同阶段 min_interval_s 内只落一行
        if isinstance(meta, dict):
            last_ts = float(meta.get("_stg_shadow_ts") or 0)
            last_stage = meta.get("_stg_shadow_stage")
            now_ts = datetime.now().timestamp()
            if last_stage == stage and (now_ts - last_ts) < float(cfg["min_interval_s"]):
                return None
            meta["_stg_shadow_ts"] = now_ts
            meta["_stg_shadow_stage"] = stage

        row = {
            "ts": datetime.now().astimezone().isoformat(),
            "symbol": symbol or "",
            "direction": (direction or "").upper(),
            "held_minutes": round(float(held_minutes or 0), 1),
            "entry": round(float(entry), 4),
            "cur": round(float(cur), 4),
            "peak_pct": round(float(peak_pct), 4),
            "initial_sl": round(float(initial_sl or 0), 4),
            "live_sl": round(float(live_sl or 0), 4),
            "stage": stage,
            "staged_sl": stage_price,
            "live_would_exit": would_exit(direction, float(cur), float(live_sl or 0)),
            "staged_would_exit": would_exit(direction, float(cur), stage_price),
            "cfg": {k: cfg[k] for k in ("arm_hi_pct", "lock_pct", "dd_pct", "dd_min_peak_pct")},
        }
        target = path or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), SHADOW_PATH)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception as e:  # 影子失败不得影响退出链
        logger.warning("[RFC-188 shadow] 落盘失败: %s", e, exc_info=True)
        return None