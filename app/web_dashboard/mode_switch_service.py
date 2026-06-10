"""
交易模式切换服务 — 安全地修改 .env.us 中的交易模式参数

支持:
  - 读取当前模式配置
  - 切换到 simulate / live_conservative / live_normal
  - 修改后提示重启服务生效
"""

import os
import re
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger("ftseek_us.mode_switch")

ENV_FILE = Path(__file__).parent.parent.parent / ".env.us"

# 模式相关的环境变量
MODE_KEYS = {
    "TRADE_ENV",
    "LIVE_CONSERVATIVE",
    "ALLOW_REAL_TRADING",
    "REAL_ACCOUNT_ID",
    "SIMULATE_ACCOUNT_ID",
    "FUTU_UNLOCK_PWD_MD5",
}


def read_env_file() -> Dict[str, str]:
    """读取 .env.us 文件，返回所有键值对"""
    env = {}
    if not ENV_FILE.exists():
        return env
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env_key(key: str, value: str) -> None:
    """写入或更新 .env.us 中的单个键值对"""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f".env.us 不存在: {ENV_FILE}")

    with open(ENV_FILE) as f:
        lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k == key:
                new_lines.append(f"{key}={value}\n")
                found = True
                continue
        new_lines.append(line)

    if not found:
        new_lines.append(f"\n{key}={value}\n")

    with open(ENV_FILE, "w") as f:
        f.writelines(new_lines)


def get_current_mode() -> Dict[str, Any]:
    """获取当前模式配置"""
    env = read_env_file()

    trade_env = env.get("TRADE_ENV", "1")
    allow_real = env.get("ALLOW_REAL_TRADING", "false").lower()
    live_conservative = env.get("LIVE_CONSERVATIVE", "1")

    if trade_env == "0" and allow_real == "true":
        if live_conservative == "1":
            mode = "live_conservative"
            mode_label = "实盘(保守)"
        else:
            mode = "live_normal"
            mode_label = "实盘(正常)"
    else:
        mode = "simulate"
        mode_label = "模拟盘"

    return {
        "mode": mode,
        "mode_label": mode_label,
        "TRADE_ENV": trade_env,
        "ALLOW_REAL_TRADING": allow_real,
        "LIVE_CONSERVATIVE": live_conservative,
        "REAL_ACCOUNT_ID": env.get("REAL_ACCOUNT_ID", ""),
        "SIMULATE_ACCOUNT_ID": env.get("SIMULATE_ACCOUNT_ID", ""),
        "FUTU_UNLOCK_PWD_MD5": "已设置" if env.get("FUTU_UNLOCK_PWD_MD5") else "未设置",
    }


def switch_mode(target_mode: str, confirm: str = "") -> Dict[str, Any]:
    """
    切换交易模式

    target_mode: "simulate" | "live_conservative" | "live_normal"
    confirm: 实盘切换需输入 "CONFIRM"

    返回: {"success": bool, "message": str, "mode": dict, "restart_needed": bool}
    """
    if not target_mode or target_mode not in ("simulate", "live_conservative", "live_normal"):
        return {"success": False, "message": f"无效模式: '{target_mode}'，可选: simulate / live_conservative / live_normal"}

    current = get_current_mode()

    if current["mode"] == target_mode:
        return {
            "success": True,
            "message": f"已经是 {current['mode_label']} 模式",
            "mode": current,
            "restart_needed": False,
        }

    # 实盘切换需要确认
    if target_mode.startswith("live_") and confirm != "CONFIRM":
        return {
            "success": False,
            "message": "切换到实盘需要输入 CONFIRM 确认",
        }

    # 检查实盘前置条件
    if target_mode.startswith("live_"):
        env = read_env_file()
        if not env.get("REAL_ACCOUNT_ID"):
            return {"success": False, "message": "REAL_ACCOUNT_ID 未设置，无法切实盘"}
        if not env.get("FUTU_UNLOCK_PWD_MD5"):
            return {"success": False, "message": "FUTU_UNLOCK_PWD_MD5 未设置，无法切实盘"}

    # 执行切换
    try:
        if target_mode == "simulate":
            write_env_key("TRADE_ENV", "1")
            write_env_key("FUTU_TRADE_ENV", "1")
            write_env_key("ALLOW_REAL_TRADING", "false")
            write_env_key("LIVE_CONSERVATIVE", "1")  # 安全默认值：下次切实盘时默认保守
            msg = "已切换到模拟盘"

        elif target_mode == "live_conservative":
            write_env_key("TRADE_ENV", "0")
            write_env_key("FUTU_TRADE_ENV", "0")
            write_env_key("ALLOW_REAL_TRADING", "true")
            write_env_key("LIVE_CONSERVATIVE", "1")
            msg = "已切换到实盘(保守)模式 — 仓位减半，风控更严"
            _clear_live_db()

        elif target_mode == "live_normal":
            write_env_key("TRADE_ENV", "0")
            write_env_key("FUTU_TRADE_ENV", "0")
            write_env_key("ALLOW_REAL_TRADING", "true")
            write_env_key("LIVE_CONSERVATIVE", "0")
            msg = "已切换到实盘(正常)模式"
            _clear_live_db()

        new_mode = get_current_mode()
        logger.warning(f"[模式切换] {current['mode_label']} → {new_mode['mode_label']}")

        # ftseek-us 交易引擎必须重启才能切换 broker/config/DB 连接
        # Dashboard 本身热加载，不需要重启
        restart_result = _restart_ftseek_us()

        return {
            "success": True,
            "message": msg + restart_result["message"],
            "mode": new_mode,
            "restart_needed": True,
            "restart_done": restart_result["success"],
            "hot_reload_dashboard": True,
        }

    except Exception as e:
        return {"success": False, "message": f"切换失败: {e}"}


def _clear_live_db() -> None:
    """切换到实盘时清空 live DB 的 open_positions, 避免显示旧模拟数据"""
    try:
        live_db = ENV_FILE.parent / "data" / "ftseek_us_live.db"
        if live_db.exists():
            import sqlite3
            conn = sqlite3.connect(str(live_db))
            conn.execute("DELETE FROM open_positions")
            conn.execute("DELETE FROM trade_records")
            conn.commit()
            conn.close()
            logger.info("[模式切换] 已清空 ftseek_us_live.db (open_positions + trade_records)")
    except Exception as e:
        logger.error(f"[模式切换] 清空 live DB 失败: {e}")


def _restart_ftseek_us() -> Dict[str, Any]:
    """重启 ftseek-us 交易引擎并等待健康就绪"""
    import time as _time

    # 1. 触发重启
    try:
        proc = subprocess.run(
            ["systemctl", "restart", "ftseek-us"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()[:80] if proc.stderr else "未知错误"
            logger.error(f"[模式切换] ftseek-us 重启命令失败: {err}")
            return {"success": False, "message": f"  ⚠️ 重启命令失败 ({err})，请手动: systemctl restart ftseek-us"}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "  ⚠️ 重启命令超时，请手动检查"}
    except Exception as e:
        return {"success": False, "message": f"  ⚠️ {e}，请手动: systemctl restart ftseek-us"}

    # 2. 轮询等待服务就绪 (最多 30 秒)
    max_wait = 60
    poll_interval = 3
    waited = 0
    while waited < max_wait:
        _time.sleep(poll_interval)
        waited += poll_interval
        try:
            check = subprocess.run(
                ["systemctl", "is-active", "ftseek-us"],
                capture_output=True, text=True, timeout=5,
            )
            status = check.stdout.strip()
            if status == "active":
                logger.info(f"[模式切换] ftseek-us 已就绪 (等待 {waited}s)")
                return {"success": True, "message": f"  交易引擎已重启 ({waited}s)，Dashboard 即时生效。"}
            elif status == "failed":
                logger.error("[模式切换] ftseek-us 启动失败")
                return {"success": False, "message": "  ⚠️ 交易引擎启动失败，请检查: journalctl -u ftseek-us -n 50"}
        except Exception:
            pass

    logger.warning(f"[模式切换] ftseek-us 等待超时 ({max_wait}s)")
    return {"success": False, "message": f"  ⚠️ 交易引擎启动超时 ({max_wait}s)，请手动检查: systemctl status ftseek-us"}

