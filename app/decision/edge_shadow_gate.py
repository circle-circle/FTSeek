"""
EdgeShadowGate — P0 (2026-07-11): 数据驱动闸门 (影子模式)

背景 (数据驱动结论):
- signal_quality 实测: 置信度与真实胜率/风险归一期望脱钩 (confidence 桶非单调)
- 现行闸门 min_confidence 用未校准置信度做门槛 → 无法可靠选出正期望信号
- 本模块改用「风险归一期望 (avg_pnl_pct)」按 confidence 桶给出 edge 判定

安全设计 (与 threshold_recommendations 一致的安全约定):
- 纯影子: 只记录 would_pass / would_block, 绝不修改实盘指令
- 数据源: data/signal_quality/latest.json 的 expectancy_by_bucket (P2 产出)
- 失败静默: 任何异常都返回 PASS, 不抛入主流程
- 实盘切换需人工 / 灰度审批, 待影子数据验证 edge 闸门优于置信度闸门后再落地

用法:
    from app.decision.edge_shadow_gate import get_edge_shadow_gate
    gate = get_edge_shadow_gate()
    gate.evaluate(symbol, direction, confidence_0_100, live_pass=True)
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_LATEST = _ROOT / "data" / "signal_quality" / "latest.json"
_SHADOW_DIR = _ROOT / "data" / "shadow"
_SHADOW_LOG = _SHADOW_DIR / "edge_gate_shadow.jsonl"

# 与 signal_quality_tracker.CONFIDENCE_BUCKETS 对齐 (0-1 尺度)
_BUCKETS: Tuple[Tuple[float, float, str], ...] = (
    (0.0, 0.3, "0-0.3"),
    (0.3, 0.5, "0.3-0.5"),
    (0.5, 0.7, "0.5-0.7"),
    (0.7, 1.0001, "0.7-1.0"),
)

# 判定所需最小依据: 期望值 <= 该阈值视为负 edge (留极小容差避免浮点噪声)
_NEG_EDGE_EPS = 0.0


def _bucket_for(conf_0_1: float) -> str:
    for lo, hi, label in _BUCKETS:
        if lo <= conf_0_1 < hi:
            return label
    return _BUCKETS[-1][2]


class EdgeShadowGate:
    """数据驱动 edge 闸门 (影子). 无状态判定 + mtime 缓存 expectancy 表."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exp_by_bucket: Dict[str, Any] = {}
        self._loaded_mtime: float = -1.0

    def _refresh(self) -> None:
        """按 latest.json 的 mtime 惰性刷新 expectancy_by_bucket (变更才重载)."""
        try:
            if not _LATEST.exists():
                return
            mtime = _LATEST.stat().st_mtime
            if mtime == self._loaded_mtime and self._exp_by_bucket:
                return
            with open(_LATEST, "r", encoding="utf-8") as f:
                data = json.load(f)
            rec = data.get("threshold_recommendations", {}) or {}
            exp = rec.get("expectancy_by_bucket") or {}
            with self._lock:
                self._exp_by_bucket = exp
                self._loaded_mtime = mtime
        except Exception as e:
            logger.debug(f"[EdgeShadowGate] 刷新 expectancy 表失败 (忽略): {e}")

    def evaluate(
        self,
        symbol: str,
        direction: str,
        confidence_0_100: float,
        live_pass: bool,
        log: bool = True,
    ) -> Dict[str, Any]:
        """
        返回 edge 判定 (影子), 并落 jsonl 供事后与实盘判定对照.

        Args:
            confidence_0_100: 信号置信度 (0-100 尺度, 与 pipeline 一致)
            live_pass: 实盘闸门此刻是否放行 (confidence >= 门槛)
            log: 是否写 edge_gate_shadow.jsonl. 统一收口审计 (exec_gate_audit) 已落库
                 edge 判定时传 False, 避免同一场景双写 (2026-07-17).

        Returns dict:
            edge_verdict: "PASS" / "BLOCK"
            bucket, expectancy, reason, agree_with_live
        """
        result: Dict[str, Any] = {
            "edge_verdict": "PASS",
            "bucket": None,
            "expectancy": None,
            "reason": "no_data",
        }
        try:
            self._refresh()
            conf = max(0.0, min(1.0, float(confidence_0_100) / 100.0))
            bucket = _bucket_for(conf)
            exp = self._exp_by_bucket.get(bucket)
            result["bucket"] = bucket
            result["expectancy"] = exp

            if exp is None:
                result["edge_verdict"] = "PASS"
                result["reason"] = "no_data"  # 无依据不拦
            elif exp <= _NEG_EDGE_EPS:
                result["edge_verdict"] = "BLOCK"
                result["reason"] = "neg_expectancy"
            else:
                result["edge_verdict"] = "PASS"
                result["reason"] = "pos_expectancy"

            result["agree_with_live"] = (
                (result["edge_verdict"] == "PASS") == bool(live_pass)
            )
            if log:
                self._log(symbol, direction, confidence_0_100, live_pass, result)
        except Exception as e:
            logger.debug(f"[EdgeShadowGate] 判定失败 (忽略, 返回 PASS): {e}")
        return result

    def _log(
        self,
        symbol: str,
        direction: str,
        confidence: float,
        live_pass: bool,
        result: Dict[str, Any],
    ) -> None:
        try:
            _SHADOW_DIR.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now().isoformat(),
                "symbol": symbol,
                "direction": direction,
                "confidence": round(float(confidence), 2),
                "bucket": result.get("bucket"),
                "expectancy": result.get("expectancy"),
                "edge_verdict": result.get("edge_verdict"),
                "live_verdict": "PASS" if live_pass else "BLOCK",
                "reason": result.get("reason"),
                "agree_with_live": result.get("agree_with_live"),
            }
            with open(_SHADOW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[EdgeShadowGate] 写影子日志失败 (忽略): {e}")


_gate: Optional[EdgeShadowGate] = None
_gate_lock = threading.Lock()


def get_edge_shadow_gate() -> EdgeShadowGate:
    """进程级单例."""
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = EdgeShadowGate()
    return _gate


__all__ = ["EdgeShadowGate", "get_edge_shadow_gate"]
