"""开盘禁入窗的时间口径单一真源。

``auto_execute`` 外层早退闸与 ``check_intraday_trigger`` 内层禁触闸必须
使用同一解析、非法值和边界语义；否则配置改成 ``"09:45"`` 等合法展示
格式时，两层会对同一时刻给出不同答案。
"""
from typing import Any


_LEGACY_OPEN_FLOOR_MIN = 9 * 60 + 35


def open_floor_minutes(logger: Any = None) -> tuple[int, int]:
    """返回 ``(floor_min, floor_hhmm)``；关闸/非法值保留 09:35 噪声底线。"""
    try:
        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()
        if not bool(pb_cfg.get("entry_open_blackout_enabled", False)):
            return _LEGACY_OPEN_FLOOR_MIN, 935
        hhmm = int(str(pb_cfg.get("entry_open_blackout_hhmm", 935)).strip()
                   .replace(":", "") or 0)
        if not (0 < hhmm <= 2359 and hhmm % 100 < 60):
            if logger is not None:
                logger.warning(
                    f"[playbook] entry_open_blackout_hhmm={hhmm!r} 非法 "
                    "(需 HHMM, 分<60) → 回落 09:35")
            return _LEGACY_OPEN_FLOOR_MIN, 935
        return (hhmm // 100) * 60 + (hhmm % 100), hhmm
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[playbook] 开盘禁入窗配置不可读 (回落 09:35): {exc}",
                           exc_info=True)
        return _LEGACY_OPEN_FLOOR_MIN, 935


def is_open_blackout(now_dt, logger: Any = None) -> bool:
    """返回当前 ET 时刻是否处于配置禁触窗；边界为 ``now < floor``。

    开关关闭时内层禁触闸保持历史 fail-open 语义；外层 ``auto_execute``
    仍通过 :func:`open_floor_minutes` 保留 09:35 底线。配置读取异常时，
    内层与外层一致采用 09:35 保守底线，不再静默放开整个开盘窗。
    """
    now_min = now_dt.hour * 60 + now_dt.minute
    try:
        from app.playbook.config import get_playbook_config
        if not bool(get_playbook_config().get("entry_open_blackout_enabled", False)):
            return False
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[playbook] 开盘禁触开关不可读 (按 09:35 底线): {exc}",
                           exc_info=True)
        return now_min < _LEGACY_OPEN_FLOOR_MIN
    floor_min, _ = open_floor_minutes(logger=logger)
    return now_min < floor_min
