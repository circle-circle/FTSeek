"""退出侧交易时段影子 (2026-11-04, record-only) — 不改任何交易动作。

## 地面真值 (用户观察, 已复核)
2026-11-04 US.LMT LONG 9 股 @651.535: `OVERNIGHT_GAP_STOP` 在 **09:30:20 ET**
(开盘后 20 秒) 成交 603.33。当日 1m: 09:31 分钟最低 **602.25** / 收 613.81,
09:35 **616.85**, 09:37 619.32 —— 成交几乎贴着开盘插针低点, 延后 5 分钟本可少亏 ≈$121。
同日另有 2 笔**盘前**平仓 (LLY 05:02 / AMD 08:54)。

## 为什么只记不改 (历史回放先验)
35 笔"隔夜仓且在 09:30–09:44 内平仓"样本, 把成交价换成延后档位 (Δ=延后平仓净差, 正=延后更好):
09:35 Σ **−$510.65** (11 正 24 负) | 09:40 −219 | 09:45 −285 | 10:00 −471 | 10:30 −636 | 15:55 −262。
按族拆: `OVERNIGHT_GAP_STOP` (n=10) 延后 5 分钟 Σ **−$324** (2 正 8 负) —— 正是本族最差。
⇒ 单笔观察与统计证据相反, 不能据此改行为。故本模块只留痕, 由离线报表结算。

## 记录什么
每次"隔夜仓 + exit_reason 已定 + 即将真实平仓"时追加一行 JSONL:
- `window`: `premarket`(<09:30) / `rth_open`(09:30–09:35) —— 即"入场侧 09:35 门若同样管退出,
  这条退出会被挡在窗外"
- `price`: 判定时的市价 (真实成交价另在 trade_records, 由报表 join)
- 记完**原流程继续平仓**, 行为零变更。

## 预注册判据 (不得边跑边改)
影子期 ≥10 个交易日 且 ≥20 条记录; 09:35 档 ΣΔ>0 且日聚类 t≥2 且正日占比≥60%
⇒ 才提请上线 (行为变更须用户二次裁决); 否则留档否掉。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ftseek.playbook.exit_session_window_shadow")

_SHADOW_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "exit_session_window_shadow.jsonl"

# 与入场侧 entry_confirm_gate 的开盘放行点一致 (09:35 ET)
_RTH_OPEN = (9, 30)
_WINDOW_END = (9, 35)

_SEEN: dict[str, float] = {}
_SEEN_TTL_SEC = 600.0


def classify_exit_window(now_et: datetime) -> Optional[str]:
    """按 ET 时刻判窗: premarket / rth_open / None (窗外, 无需留痕)。

    只用于**退出侧**留痕分类; 不参与任何判据, 不改退出行为。
    """
    hm = (now_et.hour, now_et.minute)
    if hm < _RTH_OPEN:
        return "premarket"
    if hm < _WINDOW_END:
        return "rth_open"
    return None


def _is_overnight(entry_time: str, now_et: datetime) -> bool:
    """入场日 < 今日 ⇒ 隔夜仓。解析失败按非隔夜处理 (宁少记不错记)。"""
    try:
        d = str(entry_time)[:10]
        return bool(d) and d < now_et.strftime("%Y-%m-%d")
    except Exception:
        return False


def _enabled() -> bool:
    """开关真门控写方: playbook.exit_session_window_shadow_enabled (默认 true)。

    注意: 不走 get_playbook_config() —— 该包装器只合并 risk_parameters /
    execution_parameters 两个子段 (config.py:120-126), 顶层键它读不到 (实测返回 None)。
    """
    try:
        from app.core.config_container import ConfigContainer
        v = ConfigContainer.get_instance().get_dotted(
            "playbook.exit_session_window_shadow_enabled", None)
        return True if v is None else bool(v)
    except Exception:
        return True


def record_exit_session_window_shadow(
    *,
    now_et: datetime,
    symbol: str,
    direction: str,
    qty,
    exit_reason: str,
    price,
    entry_time: str,
    source_type: str = "",
) -> bool:
    """记录一条"若退出侧套用 09:35 时段门会被推迟"的留痕。**绝不抛异常、绝不改行为**。

    返回 True = 写了一行; False = 不满足条件/被关闭/写失败 (均属正常)。
    """
    try:
        if not _enabled():
            return False
        window = classify_exit_window(now_et)
        if window is None:
            return False
        if not _is_overnight(str(entry_time or ""), now_et):
            return False
        if not exit_reason:
            return False

        date_et = now_et.strftime("%Y-%m-%d")
        key = f"{date_et}|{symbol}|{exit_reason}"
        now_mono = time.monotonic()
        if now_mono - _SEEN.get(key, -1e9) < _SEEN_TTL_SEC:
            return False
        _SEEN[key] = now_mono

        row = {
            "ts_et": now_et.isoformat(),
            "date_et": date_et,
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "exit_reason": exit_reason,
            "price_at_decision": price,
            "entry_time": entry_time,
            "overnight": True,
            "window": window,
            "window_end_et": "%02d:%02d" % _WINDOW_END,
            "source_type": source_type,
        }
        _SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        logger.info(
            f"[ExitSessionWindowShadow] {symbol} {direction} {exit_reason} "
            f"{window} @{date_et} {now_et.strftime('%H:%M:%S')} 价={price} "
            f"(record-only, 平仓照常执行)"
        )
        return True
    except Exception as e:  # pragma: no cover - 保险丝
        logger.debug(f"[ExitSessionWindowShadow] 记录失败 (已隔离): {e}")
        return False