"""
Shadow Mirror — 同源 1:1 对照 (Z1, 2026-05-15)

基于 5/14 复盘发现 shadow vs real 不可比 (不同批信号, 样本不重叠).

设计:
- 每笔实盘信号通过时, 立即在 shadow_trades 写一条镜像
- 镜像可以应用"假设的不同参数" (例如更紧止损, 不同退出策略)
- T+EOD 用真实价格回放 shadow 交易, 计算虚拟 PnL
- 对比同一信号在不同参数下的表现 → 真实 A/B

可控:
- 默认 enabled=true (零侵入: shadow 写表失败不影响实盘)
- 启动 banner 显示当前镜像变体数
- 失败降级 (写日志, 不阻塞)
"""
from __future__ import annotations
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ShadowVariant:
    """Shadow 变体配置"""
    variant_id: str               # 'tighter_stop' / 'wider_tp' / 'no_chandelier' 等
    description: str
    # 参数差异 (覆盖实盘参数)
    stop_loss_pct: Optional[float] = None       # 止损百分比 (None = 用实盘)
    tp_pct: Optional[float] = None              # 止盈百分比
    max_holding_minutes: Optional[int] = None
    min_holding_minutes: Optional[int] = None   # 2026-05-19: 最小持仓 (P1 验证)
    use_chandelier: bool = True
    chandelier_activation_atr: Optional[float] = None  # 2026-05-19: chandelier 激活阈值
    block_directions: Optional[List[str]] = None  # 2026-05-19: 拒绝某方向 (e.g., ['LONG'] 测 short_only)


# 启动时载入变体 (8 个).
# 前 4 个原版 (Z1 5/15), 后 4 个 NEW 2026-05-19 测试 P3/P5/P7 各项调整.
# 每笔实盘信号 → 8 个 shadow trade (1:8 镜像). 数据驱动 t-test 决定哪个上线.
DEFAULT_VARIANTS = [
    # Z1 原版 (5/15)
    ShadowVariant("baseline", "实盘对照基线 (相同参数)"),
    ShadowVariant("tighter_stop", "止损收紧 50% (相对实盘 0.5x)"),
    ShadowVariant("no_chandelier", "禁用 chandelier 仅用 ATR stop", use_chandelier=False),
    ShadowVariant("quick_exit", "30min 强制平仓", max_holding_minutes=30),

    # NEW 2026-05-19 — 测试策略调整建议
    ShadowVariant(
        "tp_sl_5to1",
        "TP 2.5% / SL 0.5% (P3: 5:1 比例, 配低胜率)",
        tp_pct=0.025, stop_loss_pct=0.005,
    ),
    ShadowVariant(
        "min_hold_6min",
        "强制持仓 ≥ 6min (P1 验证: 防摩擦灾难)",
        min_holding_minutes=6,
    ),
    ShadowVariant(
        "short_only",
        "拒绝所有 LONG (P5 验证: 主线 SHORT 是否真 alpha)",
        block_directions=["LONG"],
    ),
    ShadowVariant(
        "early_chandelier",
        "Chandelier 0.5 ATR 激活 (P7: 早锁利)",
        chandelier_activation_atr=0.5,
    ),
]


class ShadowMirror:
    """同源镜像器 (单例)"""

    def __init__(self, variants: Optional[List[ShadowVariant]] = None):
        self._variants = variants or DEFAULT_VARIANTS
        self._enabled = True

    def set_enabled(self, on: bool):
        self._enabled = on
        logger.info(f"[ShadowMirror] enabled={on}")

    def mirror_entry(
        self,
        real_trace_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: int,
        signal_strength: int = 0,
        signal_confidence: float = 0.0,
        timestamp: Optional[str] = None,
        owner_strategy: str = "main",
    ) -> List[str]:
        """
        每笔实盘开仓时调用. 生成 N 个 shadow 变体的入场记录.

        Returns: list of shadow_trade_ids (每变体 1 个)
        """
        if not self._enabled:
            return []

        if timestamp is None:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        ids = []
        try:
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            for variant in self._variants:
                # 2026-05-19: variant 可拒某方向 (short_only 拒 LONG)
                if variant.block_directions and direction in variant.block_directions:
                    continue
                shadow_id = f"sh_{uuid.uuid4().hex[:12]}"
                # signal_detail 写入参数差异 (供 EOD replay 使用)
                detail = {
                    "real_trace_id": real_trace_id,
                    "variant": variant.variant_id,
                    "owner_strategy": owner_strategy,
                    "stop_loss_override": variant.stop_loss_pct,
                    "tp_override": variant.tp_pct,
                    "max_hold_min": variant.max_holding_minutes,
                    "min_hold_min": variant.min_holding_minutes,
                    "use_chandelier": variant.use_chandelier,
                    "chandelier_activation_atr": variant.chandelier_activation_atr,
                }
                conn.execute("""
                    INSERT OR IGNORE INTO shadow_trades
                    (trade_id, symbol, strategy, direction, signal_score, signal_detail,
                     entry_time, entry_price, quantity, closed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (
                    shadow_id, symbol, variant.variant_id, direction,
                    signal_strength, json.dumps(detail, ensure_ascii=False),
                    timestamp, entry_price, quantity,
                ))
                ids.append(shadow_id)
            conn.commit()
            conn.close()
            logger.debug(
                f"[ShadowMirror] {symbol} {direction} → {len(ids)} 镜像入场 "
                f"(real_trace={real_trace_id[:12]})"
            )
        except Exception as e:
            logger.debug(f"[ShadowMirror·entry] 失败 (不影响实盘): {e}")
        return ids

    def mirror_exit(
        self,
        real_trace_id: str,
        symbol: str,
        exit_price: float,
        exit_reason: str = "REAL_EXIT",
        timestamp: Optional[str] = None,
    ):
        """
        实盘平仓时, 同时关闭所有该 trace_id 对应的 shadow 镜像.

        baseline 变体: 用 real 价格平仓 (1:1 对照)
        其他变体: 仍按 variant 规则评估 (可能未到出场条件继续持仓)
        """
        if not self._enabled:
            return

        if timestamp is None:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            # 找所有 closed=0 且 detail 含此 real_trace_id 的镜像
            rows = conn.execute("""
                SELECT trade_id, strategy, direction, entry_price, quantity, signal_detail
                FROM shadow_trades
                WHERE closed = 0 AND symbol = ? AND signal_detail LIKE ?
            """, (symbol, f'%{real_trace_id}%')).fetchall()

            for trade_id, strategy, direction, entry_price, qty, detail_json in rows:
                # baseline 变体: 同步用 real 价格平仓
                if strategy == "baseline":
                    pnl = self._calc_pnl(direction, entry_price, exit_price, qty)
                    pnl_pct = pnl / (entry_price * qty) if entry_price * qty > 0 else 0
                    conn.execute("""
                        UPDATE shadow_trades SET
                          exit_time = ?, exit_price = ?, realized_pnl = ?,
                          pnl_pct = ?, exit_reason = ?, closed = 1,
                          holding_minutes = (julianday(?) - julianday(entry_time)) * 1440
                        WHERE trade_id = ?
                    """, (timestamp, exit_price, pnl, pnl_pct, exit_reason, timestamp, trade_id))
                # 非 baseline 变体: EOD 由 batch 任务回放, 此处只标记 real 已平
                else:
                    try:
                        detail = json.loads(detail_json or "{}")
                    except Exception:
                        detail = {}
                    detail["real_exit_price"] = exit_price
                    detail["real_exit_time"] = timestamp
                    detail["real_exit_reason"] = exit_reason
                    conn.execute("""
                        UPDATE shadow_trades SET signal_detail = ? WHERE trade_id = ?
                    """, (json.dumps(detail, ensure_ascii=False), trade_id))
            conn.commit()
            conn.close()
            if rows:
                logger.debug(
                    f"[ShadowMirror·exit] {symbol} 同步 {len(rows)} 镜像 (real={exit_price:.2f}, reason={exit_reason})"
                )
        except Exception as e:
            logger.debug(f"[ShadowMirror·exit] 失败 (不影响实盘): {e}")

    def _calc_pnl(self, direction: str, entry: float, exit: float, qty: float) -> float:
        if direction == "LONG":
            return (exit - entry) * qty
        else:
            return (entry - exit) * qty

    def get_stats(self, days: int = 1) -> Dict[str, Any]:
        """对比统计 (供 daily_review 用)"""
        try:
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            cutoff = time.time() - days * 86400
            rows = conn.execute("""
                SELECT strategy, COUNT(*) n,
                       AVG(realized_pnl) avg_pnl,
                       SUM(realized_pnl) total_pnl,
                       AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END) win_rate
                FROM shadow_trades
                WHERE closed = 1 AND strftime('%s', exit_time) > ?
                GROUP BY strategy
            """, (str(int(cutoff)),)).fetchall()
            conn.close()
            return {
                "by_variant": [
                    {"variant": r[0], "n": r[1], "avg_pnl": r[2],
                     "total": r[3], "win_rate": r[4]}
                    for r in rows
                ]
            }
        except Exception as e:
            logger.debug(f"[ShadowMirror·stats] 失败: {e}")
            return {}


_singleton: Optional[ShadowMirror] = None


def get_shadow_mirror() -> ShadowMirror:
    global _singleton
    if _singleton is None:
        _singleton = ShadowMirror()
    return _singleton
