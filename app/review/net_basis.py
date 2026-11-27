"""未知净额统一合同 (RFC-218 §22 P1-A) — 日报/教训验证与官方评分器同源。

背景 (独立审计 2026-11-27 隔离合成检查): 同一笔交易三套读数分叉 —
  毛10/费未知/净未知: 官方评分=不可测, 日报=10, 教训helper=10 (后两者把毛当净);
  毛10/费4/净缺失:    官方评分=6,    日报=10, 教训helper=6  (日报缺费不扣)。
教训验证 (validate_lessons) 会用该读数更新经验分值/有效期 ⇒ 未知收益不得自证经验正确。

合同 (与 scripts/okr_2026_11_scoring._row_net/_num 逐字同语义,  parity 有契约测试钉死):
  1) pnl_net 已知 (有限数值; None/"" 视为缺失) → 原样保留;
  2) pnl_net 缺失 → 仅当 realized_pnl(毛) 与 fees_est(费) 均已知才回算 毛-费;
  3) 其余 → None (NOT_MEASURABLE)。**未知绝不归零, 绝不用毛额顶替。**

消费方: app/review/daily_review._generate_summary / _calculate_review_score,
        app/review/lesson_injector._trade_net_pnl (validate_lessons 剔除 None 样本)。
完整旧数据 (pnl_net 在场) 的结果与修复前逐字一致。
"""
import math
from typing import Any, Dict, Optional


def _num(value: Any) -> Optional[float]:
    """有限数值解析 (与官方评分器 _num 同语义): None/""/非数/NaN/inf → None。"""
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def resolve_net_pnl(trade: Dict[str, Any]) -> Optional[float]:
    """单笔净额单一真源: 已知净保留 | 毛费双已知才回算 | 否则 None (不可测)。"""
    net = _num(trade.get("pnl_net"))
    if net is not None:
        return net
    gross = _num(trade.get("realized_pnl"))
    fees = _num(trade.get("fees_est"))
    if gross is None or fees is None:
        return None
    return gross - fees
