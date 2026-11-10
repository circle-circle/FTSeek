# -*- coding: utf-8 -*-
"""美国经济事件日历 (未来 7 天, Futu get_economic_calendar)。

为什么在 app/ 而不在 scripts/:
  `app/data/premarket_enrichment.py` 的盘前 prompt 需要"未来日程表", 但原实现是
  **按星期几猜** CPI/NFP/FOMC 并硬编码日期 (2026-11-09 复核发现 FOMC 日期与联储
  官方日程不符), 查不到时还输出 "No major scheduled events this week" —— 把
  "本函数没覆盖" 谎成 "本周无事件"。

  修法是把 `scripts/macro_calendar_shadow_collect.py` 里已实测可用的动态日历
  提升为生产模块: 生产 prompt 不应依赖实验性影子脚本的下划线私有函数 (该脚本一旦
  重构, 盘前日历会静默降级)。

契约:
  - 返回 `{"status": "ok"|"disabled"|"opend_unreachable"|"empty..."|"error...", "events": [...]}`;
  - 每条事件必须含 `title / ts_et / star`, 并已被过滤为: 美国 + HIGH/MEDIUM + 尚未发生;
  - **失败绝不返回空列表冒充"无事件"** —— 由调用方决定如何表达不可用;
  - 只读, 不写库/不写文件/不改任何交易状态。
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOOKAHEAD_DAYS = 7
_CONNECT_TIMEOUT_S = 2.0
_COUNTRIES = ("美国",)
_STARS = ("HIGH", "MEDIUM")


def _config_enabled() -> bool:
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted("macro_calendar.event_schedule_enabled", True))
    except Exception:
        return True  # 配置读不到 → 默认启用 (与既有影子行为一致)


def _opend_reachable(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT_S)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        try:
            sock.close()
        except Exception as exc:
            logger.warning("socket close 失败(忽略): %s", exc, exc_info=True)


def fetch_us_event_schedule(now: datetime) -> Dict[str, Any]:
    """未来 7 天美国 HIGH/MEDIUM 经济事件。失败时 status 非 ok, events 为空。"""
    out: Dict[str, Any] = {"status": "skipped", "events": []}
    if not _config_enabled():
        out["status"] = "disabled"
        return out

    try:
        from futu import OpenQuoteContext, RET_OK
        from app.data.futu_lv3_provider import silence_futu_sdk_logs
    except Exception as exc:
        out["status"] = f"error: {type(exc).__name__}: {str(exc)[:120]}"
        return out

    silence_futu_sdk_logs()
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    except ValueError:
        port = 11111
    if not _opend_reachable(host, port):
        out["status"] = "opend_unreachable"
        return out

    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        begin = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
        ret, df = ctx.get_economic_calendar(begin, end)[:2]
        if ret != RET_OK or df is None or not len(df):
            out["status"] = f"empty ret={ret}"
            return out

        events: List[Dict[str, str]] = []
        for _, row in df.iterrows():
            country = str(row.get("country", ""))
            star = str(row.get("star", ""))
            if country not in _COUNTRIES or star not in _STARS:
                continue
            try:
                ts = datetime.fromtimestamp(float(row.get("timestamp", 0)), tz=timezone.utc)
                ts_et = ts.astimezone(_et())
            except Exception as exc:
                logger.warning("事件时间戳坏行跳过: %s", exc, exc_info=True)
                continue
            # 过滤已发生事件: Futu 日期边界按北京时间解释, 窗口会含 ET 前一日的
            # 已发布事件 (带 actual 值); 消费侧需要的是"未来日程"。
            if ts_et.replace(tzinfo=None) < now.replace(tzinfo=None):
                continue
            events.append({
                "title": str(row.get("title", "")),
                "ts_et": ts_et.strftime("%Y-%m-%d %H:%M"),
                "star": star,
                "previous": str(row.get("previous", "")),
                "consensus": str(row.get("consensus", "")),
                "actual": str(row.get("actual", "")),
            })
        out["status"] = "ok"
        out["events"] = events
        out["n_us_high_medium"] = len(events)
    except Exception as exc:
        out["status"] = f"error: {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception as exc:
                logger.warning("futu ctx close 失败(忽略): %s", exc, exc_info=True)
    return out


def _et():
    from app.utils.timezone import ET
    return ET