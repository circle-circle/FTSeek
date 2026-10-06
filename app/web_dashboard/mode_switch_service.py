"""
交易模式切换服务

包含两层职责:
  1. .env.us 文件层模式切换 (simulate / live_conservative / live_normal)
     —— 模块级函数 get_current_mode / switch_mode 等, 需要重启交易引擎生效
  2. 运行时 auto/manual 双链路并行控制 (ModeSwitchService 类)
     —— Dashboard 一键开关自动交易 + 激进程度切换, 不影响手动链路
"""

import os
import re
import shutil
import fcntl
import tempfile
import subprocess
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek_us.mode_switch")

ENV_FILE = Path(__file__).parent.parent.parent / ".env.us"
LEGACY_GLOBAL_REAL_SWITCH_RETIRED = "LEGACY_GLOBAL_REAL_SWITCH_RETIRED"

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


def write_env_keys(updates: Dict[str, str]) -> None:
    """在一次文件锁和原子替换中写入一组 .env.us 键值。"""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f".env.us 不存在: {ENV_FILE}")
    if not updates:
        return

    normalized = {str(key): str(value) for key, value in updates.items()}
    lock_fd = _acquire_env_lock()
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()

        remaining = dict(normalized)
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in normalized:
                    if key in remaining:
                        new_lines.append(f"{key}={remaining.pop(key)}\n")
                    continue
            new_lines.append(line)

        if remaining:
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.extend(f"{key}={value}\n" for key, value in remaining.items())

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


def write_env_key(key: str, value: str) -> None:
    """原子写入或更新 .env.us 中的单个键值。"""
    write_env_keys({key: value})


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
    """获取当前模式配置 (2026-06-24 重构: 从 AUTO_TRADE_ACCOUNT + live_trial 推导,
    不再读 deprecated 的 TRADE_ENV/FUTU_TRADE_ENV)."""
    env = read_env_file()

    auto_account = (env.get("AUTO_TRADE_ACCOUNT") or "sim").strip().lower()
    allow_real = env.get("ALLOW_REAL_TRADING", "false").lower()
    live_conservative = env.get("LIVE_CONSERVATIVE", "1")

    if auto_account == "real" and allow_real == "true":
        # real + live_trial=true → 保守(LIVE_TRIAL); live_trial=false → 正常(LIVE_FULL)
        _live_trial = (env.get("playbook.live_trial") or "true").strip().lower()
        if _live_trial == "false" or live_conservative == "0":
            mode = "live_normal"
            mode_label = "实盘(正常/LIVE_FULL)"
        else:
            mode = "live_conservative"
            mode_label = "实盘(保守/LIVE_TRIAL)"
    else:
        mode = "simulate"
        mode_label = "模拟盘"

    return {
        "mode": mode,
        "mode_label": mode_label,
        "TRADE_ENV": env.get("TRADE_ENV", "1"),  # DB 路由，须与自动账户成套切换
        "AUTO_TRADE_ACCOUNT": auto_account,      # broker 路由真实状态
        "ALLOW_REAL_TRADING": allow_real,
        "LIVE_CONSERVATIVE": live_conservative,
        "REAL_ACCOUNT_ID": env.get("REAL_ACCOUNT_ID", ""),
        "SIMULATE_ACCOUNT_ID": env.get("SIMULATE_ACCOUNT_ID", ""),
        "FUTU_UNLOCK_PWD_MD5": "已设置" if env.get("FUTU_UNLOCK_PWD_MD5") else "未设置",
    }


def switch_mode(target_mode: str, confirm: str = "") -> Dict[str, Any]:
    """
    切换交易模式

    target_mode: "simulate"（修复旧全局快照）；live_* 已退役
    confirm: 兼容旧请求体，live_* 一律拒绝

    返回: {"success": bool, "message": str, "mode": dict, "restart_needed": bool}
    """
    if not target_mode or target_mode not in ("simulate", "live_conservative", "live_normal"):
        return {"success": False, "message": f"无效模式: '{target_mode}'，可选: simulate / live_conservative / live_normal"}

    if target_mode.startswith("live_"):
        return {
            "success": False,
            "code": LEGACY_GLOBAL_REAL_SWITCH_RETIRED,
            "http_status": 410,
            "message": (
                "旧全局实盘切换已退役：请使用 playbook.live_manual_arm 显式 LIVE 臂，"
                "不得翻转 AUTO_TRADE_ACCOUNT/TRADE_ENV"
            ),
        }

    current = get_current_mode()
    env = read_env_file()
    if target_mode == "simulate":
        if (
            (env.get("AUTO_TRADE_ACCOUNT") or "sim").strip().lower() == "sim"
            and (env.get("TRADE_ENV") or "1").strip() == "1"
        ):
            return {
                "success": True,
                "message": "SIM 研究臂常驻，无需全局切换",
                "mode": current,
                "restart_needed": False,
                "no_op": True,
            }
        updates = {
            "AUTO_TRADE_ACCOUNT": "sim",
            "TRADE_ENV": "1",
            "ALLOW_REAL_TRADING": "false",
            "LIVE_CONSERVATIVE": "1",
        }
        msg = "已修复为 SIM_ONLY 全局快照 (自动交易→模拟账户)"

    if current["mode"] == target_mode and all(env.get(key) == value for key, value in updates.items()):
        logger.info(f"[模式切换] 拒绝重复切换: 当前已是 {current['mode_label']}")
        return {
            "success": True,
            "message": f"已经是 {current['mode_label']} 模式 (无操作)",
            "mode": current,
            "restart_needed": False,
            "no_op": True,
        }

    bak = _backup_env_file()
    if bak:
        logger.info(f"[模式切换] 已备份 .env.us → {bak.name}")

    try:
        write_env_keys(updates)
        new_mode = get_current_mode()
        logger.warning(f"[模式切换] {current['mode_label']} → {new_mode['mode_label']}")

        try:
            from app.web_dashboard.routes.dashboard import _is_live
            if hasattr(_is_live, '_cache_time'):
                _is_live._cache_time = 0
                _is_live._cache = None
        except Exception:
            pass
        try:
            from app.web_dashboard.data_service import get_data_service
            get_data_service().invalidate_cache()
        except Exception:
            pass

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


# ============================================================
#  ModeSwitchService — 双链路并行模式管理 (auto + manual)
# ============================================================
#
# 旧逻辑: auto/manual 互斥切换
# 新逻辑: 两者独立运行, 各自有开关控制
#   - 自动交易: 可通过 Dashboard 随时开启/关闭, 默认以谨慎模式运行
#   - 手动交易: 始终可用 (Dashboard 随时可操作)
# ============================================================


class ModeSwitchService:
    """双链路并行模式管理服务 (运行时态, 内存级开关)."""

    VALID_LEVELS = ("conservative", "normal", "aggressive")

    def __init__(self, scheduler=None):
        self._lock = threading.RLock()
        self._auto_enabled: bool = self._read_yaml_auto_trading()  # 从 YAML 恢复, 重启后状态一致
        self._auto_aggression: str = self._read_yaml_auto_aggression()  # 从 YAML 恢复, 重启后状态一致
        self._scheduler = scheduler
        self._last_toggle_time: Optional[datetime] = None
        self._toggle_history: list = []
        logger.info(
            f"[ModeSwitchService] Initialized: auto={self._auto_enabled} "
            f"(from YAML), level={self._auto_aggression}"
        )

    @staticmethod
    def _read_yaml_auto_trading() -> bool:
        """从 unified_config.yaml 读取 auto_trading_enabled (启动时恢复状态)"""
        import re as _re
        try:
            _yaml_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config", "unified_config.yaml"
            )
            with open(_yaml_path, "r") as _f:
                for _line in _f:
                    _m = _re.match(r'\s+auto_trading_enabled:\s*(true|false)', _line)
                    if _m:
                        return _m.group(1) == 'true'
        except Exception:
            pass
        return False  # 读取失败 → 默认关闭 (安全优先)

    @staticmethod
    def _read_yaml_auto_aggression() -> str:
        """从 unified_config.yaml 读取 feature_flags.auto_aggression (启动时恢复; 默认 conservative)"""
        import re as _re
        try:
            _yaml_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config", "unified_config.yaml"
            )
            with open(_yaml_path, "r") as _f:
                for _line in _f:
                    _m = _re.match(r'\s+auto_aggression:\s*(conservative|normal|aggressive)', _line)
                    if _m:
                        return _m.group(1)
        except Exception:
            pass
        return "conservative"  # 读取失败 → 保守 (安全优先)

    # ---- Dashboard 控制接口 -------------------------------------------------

    def set_auto_enabled(self, enabled: bool) -> dict:
        """Dashboard 一键开启/关闭自动交易 (不影响手动).

        关闭时: 停止新信号扫描, 但现有自动持仓的退出规则继续生效
        开启时: 以当前设定的激进程度恢复扫描
        """
        enabled = bool(enabled)
        with self._lock:
            old_state = self._auto_enabled
            self._auto_enabled = enabled
            self._last_toggle_time = datetime.now()
            self._toggle_history.append({
                "time": self._last_toggle_time,
                "from": old_state,
                "to": enabled,
            })
            # 仅保留最近 20 条
            if len(self._toggle_history) > 20:
                self._toggle_history = self._toggle_history[-20:]

            logger.info(
                f"[ModeSwitchService] Auto trading "
                f"{'ENABLED' if enabled else 'DISABLED'} "
                f"(level={self._auto_aggression})"
            )

            return {
                "success": True,
                "auto_enabled": enabled,
                "auto_level": self._auto_aggression,
                "toggled_at": self._last_toggle_time.isoformat(),
            }

    def set_auto_aggression(self, level: str) -> dict:
        """设置自动交易激进程度.

        - conservative (默认): 仓位缩减 50%, 资金上限 30%, 仅高置信度信号 (70/0.70)
        - normal: 标准参数运行
        - aggressive: 满配参数 (仅模拟盘建议使用)
        """
        if level not in self.VALID_LEVELS:
            return {
                "success": False,
                "error": f"Invalid level: {level}. Must be one of {self.VALID_LEVELS}",
            }

        with self._lock:
            old_level = self._auto_aggression
            self._auto_aggression = level
            logger.info(
                f"[ModeSwitchService] Auto aggression changed: {old_level} -> {level}"
            )
            return {
                "success": True,
                "level": level,
                "previous_level": old_level,
            }

    # ---- Pipeline / 查询接口 ------------------------------------------------

    def is_auto_enabled(self) -> bool:
        """自动交易 pipeline 调用: 检查是否允许扫描新信号."""
        with self._lock:
            return self._auto_enabled

    def get_auto_aggression(self) -> str:
        """获取当前自动交易激进程度."""
        with self._lock:
            return self._auto_aggression

    def get_mode_status(self) -> dict:
        """获取完整模式状态 (供 Dashboard 展示)."""
        with self._lock:
            # graduation 状态 (Phase 2): 实盘开关需毕业才可点
            graduation = None
            try:
                from app.playbook.pipeline import _check_graduation_gate
                graduation = _check_graduation_gate()
            except Exception as _e:
                logger.debug(f"[ModeSwitch] graduation 查询失败: {_e}")
            return {
                "auto_enabled": self._auto_enabled,
                "auto_level": self._auto_aggression,
                "auto_account": self._read_auto_account(),   # sim | real
                "manual_enabled": True,  # 手动始终可用 (实盘)
                "manual_account": "real",
                "graduation": graduation,
                "last_toggle_time": (
                    self._last_toggle_time.isoformat()
                    if self._last_toggle_time else None
                ),
            }

    def _read_auto_account(self) -> str:
        """从 .env.us 读 AUTO_TRADE_ACCOUNT (sim|real)."""
        return read_env_file().get(
            "AUTO_TRADE_ACCOUNT",
            os.getenv("AUTO_TRADE_ACCOUNT", "sim"),
        ).strip().lower()

    def set_auto_account(self, account: str) -> dict:
        """Repair the legacy global snapshot to SIM; REAL global flips are retired."""
        account = (account or "").strip().lower()
        if account not in ("sim", "real"):
            return {"success": False, "error": f"invalid account: {account}"}

        if account == "real":
            return {
                "success": False,
                "error": (
                    "旧全局 REAL 账户切换已退役：请使用 playbook.live_manual_arm "
                    "显式 LIVE 臂"
                ),
                "code": LEGACY_GLOBAL_REAL_SWITCH_RETIRED,
                "http_status": 410,
            }

        env = read_env_file()
        if (
            (env.get("AUTO_TRADE_ACCOUNT") or "sim").strip().lower() == "sim"
            and (env.get("TRADE_ENV") or "1").strip() == "1"
        ):
            return {
                "success": True,
                "auto_account": "sim",
                "restart_required": False,
                "message": "SIM 研究臂常驻，无需全局切换",
                "graduation": self.get_mode_status().get("graduation"),
            }

        updates = {
            "AUTO_TRADE_ACCOUNT": "sim",
            "TRADE_ENV": "1",
            "ALLOW_REAL_TRADING": "false",
        }

        try:
            write_env_keys(updates)
        except Exception as _e:
            logger.error(f"[ModeSwitch] 写 .env.us 模式快照失败: {_e}")
            return {"success": False, "error": "写 .env.us 失败"}
        logger.warning(f"[ModeSwitch] auto_account → {account} (需重启 ftseek-us 生效)")
        return {
            "success": True,
            "auto_account": account,
            "restart_required": True,
            "graduation": self.get_mode_status().get("graduation"),
        }

    def get_toggle_history(self, limit: int = 20) -> list:
        """获取自动交易开关历史 (最近 limit 条)."""
        with self._lock:
            history = self._toggle_history[-limit:]
            return [
                {
                    "time": h["time"].isoformat() if hasattr(h["time"], "isoformat") else str(h["time"]),
                    "from": h["from"],
                    "to": h["to"],
                }
                for h in history
            ]

# ---- 进程内单例 ---------------------------------------------------------

_mode_switch_service_singleton: Optional[ModeSwitchService] = None
_mode_switch_service_lock = threading.Lock()


def get_mode_switch_service(scheduler=None) -> ModeSwitchService:
    """获取 ModeSwitchService 进程内单例.

    首次调用允许传入 scheduler 注入, 后续调用忽略参数返回已有实例.
    """
    global _mode_switch_service_singleton
    if _mode_switch_service_singleton is None:
        with _mode_switch_service_lock:
            if _mode_switch_service_singleton is None:
                _mode_switch_service_singleton = ModeSwitchService(scheduler=scheduler)
    return _mode_switch_service_singleton
