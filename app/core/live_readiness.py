"""LIVE 路径统一启动/执行断言 (2026-10-17).

原先同一组 fail-mode 断言只在 `AUTO_TRADE_ACCOUNT=real` 的 DI 启动分支执行；
`live_manual_arm` 可在 AUTO=sim 下用显式 target_env=real 下真单，完全绕过该分支。
本模块把断言抽成单一实现，DI 启动与 `evaluate_live_arm_gate` 共用，避免第三份拷贝。
"""
from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

_REQUIRED_CLOSED = ("risk.acb_fail_mode", "risk.preflight_fail_mode")
_DEFAULTS = {"risk.acb_fail_mode": "closed", "risk.preflight_fail_mode": "open"}


def live_fail_mode_issues() -> List[str]:
    """返回 LIVE 路径不允许的 fail-mode 列表；配置不可读时返回哨兵项 (fail-close)。"""
    try:
        from app.core.config_container import ConfigContainer

        cfg = ConfigContainer.get_instance()
        values = {
            key: str(cfg.get_dotted(key, _DEFAULTS[key])).strip().lower()
            for key in _REQUIRED_CLOSED
        }
    except Exception as error:
        logger.critical(f"[LIVE就绪断言] 读取 fail_mode 配置失败: {error}", exc_info=True)
        return [f"config_unreadable: {error}"]

    issues = []
    for key in _REQUIRED_CLOSED:
        if values[key] != "closed":
            issues.append(f"{key}={values[key]}")
    return issues


def assert_live_fail_modes() -> None:
    """DI 启动时调用：任何一项不是 closed 都拒绝以 LIVE 启动。"""
    issues = live_fail_mode_issues()
    if issues:
        logger.critical(
            "[LIVE启动断言] 实盘要求 acb_fail_mode/preflight_fail_mode 均为 'closed' "
            f"(当前 {', '.join(issues)}); fail-open 在真账户会放行风控异常开仓 → 拒绝启动"
        )
        raise RuntimeError(f"LIVE 启动断言失败: {issues} 必须为 closed (实盘禁止 fail-open)")


def is_live_capable_env() -> bool:
    """进程是否被授权走真钱 — 与 di_container broker 路由同源。"""
    import os

    return (
        os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "real"
        and os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true"
    )


def preflight_fail_mode() -> str:
    """运行时读取 risk.preflight_fail_mode；配置不可读时按环境回落。

    2026-10-17 审计修复：`auto_execute_flow` / `risk_gates` 两处异常分支原先直接
    回落 `"open"`。LIVE 启动断言要求该值为 closed —— 启动后配置读取失败却回落
    open，等于把启动时锁上的 fail-close 又悄悄放开。SIM 面默认 open 是
    DEB-round2 Ω.3-3 的既定设计，保持不变。
    """
    try:
        from app.core.config_container import ConfigContainer

        value = ConfigContainer.get_instance().get_dotted(
            "risk.preflight_fail_mode", "open"
        )
        return str(value).strip().lower()
    except Exception as error:
        fallback = "closed" if is_live_capable_env() else "open"
        logger.critical(
            "[fail-mode] risk.preflight_fail_mode 读取失败 (%s) → 按环境回落 %s",
            error, fallback, exc_info=True,
        )
        return fallback
