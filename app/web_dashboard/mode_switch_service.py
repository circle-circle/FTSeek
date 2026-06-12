"""
交易模式切换服务 — 安全地修改 .env.us 中的交易模式参数

支持:
  - 读取当前模式配置
  - 切换到 simulate / live_conservative / live_normal
  - 修改后提示重启服务生效
"""

import os
import re
import shutil
import fcntl
import tempfile
import subprocess
import logging
from datetime import datetime
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


_ENV_LOCK_FILE = ENV_FILE.parent / ".env.us.lock"


def _acquire_env_lock(timeout: float = 5.0):
    """获取 .env.us 进程级文件锁 (P1-3 2026-06-12)"""
    import time as _t
    fd = open(_ENV_LOCK_FILE, "w")
    deadline = _t.time() + timeout
    while True:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if _t.time() >= deadline:
                fd.close()
                raise TimeoutError(f"获取 .env.us 锁超时 ({timeout}s)")
            _t.sleep(0.1)


def _release_env_lock(fd) -> None:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fd.close()
    except Exception:
        pass


def write_env_key(key: str, value: str) -> None:
    """
    写入或更新 .env.us 中的单个键值对 (P1-3 原子写 2026-06-12)
    保证: 进程级文件锁防并发 + tmp+fsync+os.replace 原子替换
    """
    if not ENV_FILE.exists():
        raise FileNotFoundError(f".env.us 不存在: {ENV_FILE}")

    lock_fd = _acquire_env_lock()
    try:
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

        env_dir = ENV_FILE.parent
        fd, tmp_path = tempfile.mkstemp(prefix=".env.us.", suffix=".tmp", dir=str(env_dir))
        try:
            with os.fdopen(fd, "w") as tf:
                tf.writelines(new_lines)
                tf.flush()
                os.fsync(tf.fileno())
            try:
                st = os.stat(ENV_FILE)
                os.chmod(tmp_path, st.st_mode)
            except Exception:
                pass
            os.replace(tmp_path, ENV_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    finally:
        _release_env_lock(lock_fd)


def _backup_env_file():
    """切换前备份 .env.us (P1-4 2026-06-12), 仅保留最近 10 份"""
    if not ENV_FILE.exists():
        return None
    ts2 = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = ENV_FILE.parent / f".env.us.bak.{ts2}"
    try:
        shutil.copy2(ENV_FILE, bak)
        try:
            backups = sorted(ENV_FILE.parent.glob(".env.us.bak.*"), reverse=True)
            for old in backups[10:]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        return bak
    except Exception as e:
        logger.warning(f"[模式切换] 备份 .env.us 失败: {e}")
        return None


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
        # P1-4 (2026-06-12): 同模式直接返回, 不触发 _clear_live_db / 重启 / 写文件
        logger.info(f"[模式切换] 拒绝重复切换: 当前已是 {current['mode_label']}")
        return {
            "success": True,
            "message": f"已经是 {current['mode_label']} 模式 (无操作)",
            "mode": current,
            "restart_needed": False,
            "no_op": True,
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

    # 执行切换 (P1-4 2026-06-12: 切换前自动备份)
    bak = _backup_env_file()
    if bak:
        logger.info(f"[模式切换] 已备份 .env.us → {bak.name}")

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

        # 清除 dashboard._is_live 30s 缓存 (2026-06-11)
        # 否则切换后 30s 内页面仍显示旧模式标签
        try:
            from app.web_dashboard.routes.dashboard import _is_live
            if hasattr(_is_live, '_cache_time'):
                _is_live._cache_time = 0
                _is_live._cache = None
        except Exception:
            pass
        # 清除 DataService 缓存
        try:
            from app.web_dashboard.data_service import get_data_service
            get_data_service().invalidate_cache()
        except Exception:
            pass

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
    """切换到实盘时清空 live DB 的 open_positions, 避免显示旧模拟数据 (C4: 先备份)"""
    try:
        live_db = ENV_FILE.parent / "data" / "ftseek_us_live.db"
        if live_db.exists():
            import sqlite3
            from datetime import datetime
            conn = sqlite3.connect(str(live_db))
            conn.row_factory = sqlite3.Row

            # C4: 备份到 archive 表
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            for table in ("open_positions", "trade_records"):
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if count > 0:
                        archive_table = f"_archive_{table}_{ts}"
                        conn.execute(f"CREATE TABLE {archive_table} AS SELECT * FROM {table}")
                        logger.info(f"[模式切换] 备份 {count} 行到 {archive_table}")
                except Exception as be:
                    logger.warning(f"[模式切换] 备份 {table} 失败: {be}")

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

