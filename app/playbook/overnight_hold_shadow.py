#!/usr/bin/env python3
"""隔夜持仓 shadow (2026-08-10, record-only, 用户批准).

问题 (DEB-20260808 隔夜讨论): benign 退出 (SL_BREAKEVEN/SL_TRAIL_LOCK/PRE_CLOSE_EXIT)
若不平仓持到次日, 是否有正期望? 历史反事实 (7/15-8/7, 73笔) 显示:
  隔夜跳空胜率 60% +0.36%, 持到次日收盘胜率 59% +0.22% (+$2,112)
  但 COIN 单笔占 78% — 剔除后 ≈打平; regime 未剥离; 样本内结论.

本 emitter: 每次 PRE_CLOSE_EXIT / SL_BREAKEVEN / SL_TRAIL_LOCK 触发时落一行决策点
(零行为变更), 次日 settle_overnight_shadow.py 结算反事实 (持仓过夜 vs 已退出).
跨 regime 攒样本, 判定隔夜是否真有 edge (标的层面).

配置键: playbook.overnight_hold_shadow_enabled (默认 true)
回滚: 置 false + 重启.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# DEB-20260903 #83: 相对路径 Path("data/...") 依赖进程 CWD — 改为基于 __file__
# 的仓库根绝对路径 (app/playbook/file → parents[2]=仓库根), 与 pipeline.py 一致
_OUTPUT = Path(__file__).resolve().parents[2] / "data" / "overnight_hold_shadow.jsonl"
_BENIGN = frozenset({"SL_BREAKEVEN", "SL_TRAIL_LOCK", "PRE_CLOSE_EXIT"})


def _enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.overnight_hold_shadow_enabled", True))
    except Exception:
        return True  # fail-open: 影子记录不应因配置故障丢失


def emit(symbol: str, direction: str, exit_reason: str, exit_price: float,
         entry_price: float, pnl_net: float, notional: float = None,
         decision_id: str = None) -> bool:
    """PRE_CLOSE/保本/锁利退出时调用, 落一行隔夜决策点. 返回是否落盘."""
    # 测试卫生守卫 (DEB-20260823 X3, 对齐 pipeline 同款约定): 单测 TEST 标记不落生产影子
    if "TEST" in str(symbol or "").upper():
        return False
    if not _enabled():
        return False
    if exit_reason not in _BENIGN:
        return False
    try:
        from app.utils.timezone import now_et
        _now = now_et()
    except Exception:
        # 8/27 时钟统一: 兜底也走 DST 真实时区 (zoneinfo 为 3.9+ 标准库);
        # 极端环境再落固定偏移 — 至少夏令时正确
        try:
            from zoneinfo import ZoneInfo
            _now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timezone, timedelta
            _now = datetime.now(timezone(timedelta(hours=-4)))
    row = {
        "ts": _now.isoformat(),
        "trade_date": _now.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "direction": direction,
        "exit_reason": exit_reason,
        "exit_price": exit_price,
        "entry_price": entry_price,
        "pnl_net": pnl_net,
        "notional": notional,
        "decision_id": decision_id,
        "settled": False,
    }
    try:
        _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with open(_OUTPUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("[隔夜shadow] %s %s %s exit=%.2f pnl=%.2f",
                    symbol, direction, exit_reason, exit_price, pnl_net)
        return True
    except Exception as e:
        logger.warning("[隔夜shadow] 写入失败: %s", e)
        return False
