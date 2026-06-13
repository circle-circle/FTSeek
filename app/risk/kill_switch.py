"""
紧急停止开关 - Kill Switch

用途:
  - Dashboard 一键停止所有交易（手动 + 自动）
  - 通过文件锁实现，所有下单路径检查此文件

机制:
  - 激活: 创建 .kill_switch 文件
  - 解除: 删除 .kill_switch 文件
  - 检查: 下单前检查文件是否存在

位置: data/.kill_switch
"""
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# 文件路径
KILL_SWITCH_FILE = Path(__file__).parent.parent.parent / "data" / ".kill_switch"


def activate_kill_switch(reason: str = "手动触发") -> bool:
    """激活紧急停止开关
    
    Args:
        reason: 触发原因（记录到文件）
        
    Returns:
        True if activated successfully
    """
    try:
        KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(KILL_SWITCH_FILE, "w") as f:
            f.write(f"{datetime.now().isoformat()}\n{reason}\n")
        return True
    except Exception as e:
        print(f"[KillSwitch] 激活失败: {e}")
        return False


def deactivate_kill_switch() -> bool:
    """解除紧急停止开关
    
    Returns:
        True if deactivated successfully
    """
    try:
        if KILL_SWITCH_FILE.exists():
            KILL_SWITCH_FILE.unlink()
        return True
    except Exception as e:
        print(f"[KillSwitch] 解除失败: {e}")
        return False


def is_kill_switch_active() -> bool:
    """检查紧急停止开关是否激活
    
    Returns:
        True if kill switch is active (trading blocked)
    """
    return KILL_SWITCH_FILE.exists()


def get_kill_switch_info() -> Optional[Tuple[str, str]]:
    """获取 Kill Switch 信息
    
    Returns:
        (timestamp, reason) or None if not active
    """
    if not is_kill_switch_active():
        return None
    try:
        with open(KILL_SWITCH_FILE, "r") as f:
            lines = f.readlines()
            timestamp = lines[0].strip() if len(lines) > 0 else "unknown"
            reason = lines[1].strip() if len(lines) > 1 else "unknown"
            return (timestamp, reason)
    except Exception:
        return None
