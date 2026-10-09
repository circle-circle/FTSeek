"""Shadow/遥测/防重域 (P1a 迁自 app/playbook/pipeline.py, DEB-20261008 §2 职责地图)。

本模块只承接"纯几何/台账判定"最小件; A/B 开关判定与台账写入仍在 pipeline
(与 _VWAP_AB_LEDGER_PATH 缝/写侧同批再迁, 避免读写分家)。
"""
from __future__ import annotations


def _vwap_ab_row_exists(date_str: str, symbol: str, direction: str) -> bool:
    """台账文件级去重 (2026-08-07 主动审计 P1-4 修复): _VWAP_AB_LOGGED 是进程内
    set, 进程重启(systemd 重启/cron 重跑)后清空 → 同 (symbol,date,direction) 重复
    append, 破坏"每 symbol-day-direction 一行"的台账唯一性承诺, A/B 统计重复计数。
    写前查文件历史行, 与进程 set 双保险。异常 → False (允许写, 审计降级优于丢行)."""
    try:
        # P1a 过渡缝: 台账路径仍读 pipeline 模块常量 — tests/conftest.py 的沙箱
        # 重定向打的是 app.playbook.pipeline._VWAP_AB_LEDGER_PATH, 搬家不换缝。
        from app.playbook import pipeline as _pipeline
        _f = _pipeline._VWAP_AB_LEDGER_PATH
        if not _f.exists():
            return False
        import json as _jab
        with open(_f, "r", encoding="utf-8") as _fh:
            for _line in _fh:
                try:
                    _j = _jab.loads(_line)
                except Exception:
                    continue
                if (_j.get("date") == date_str and _j.get("symbol") == symbol
                        and _j.get("direction") == direction):
                    return True
    except Exception:
        return False
    return False


def _vwap_ab_arm(symbol: str, date_str: str, direction: str) -> str:
    """确定性 50/50 分臂 (纯函数, 可单测): sha1 奇偶 → 'bypass' | 'gated'."""
    import hashlib
    h = hashlib.sha1(f"{date_str}|{symbol}|{direction}".encode()).hexdigest()
    return "bypass" if int(h, 16) % 2 == 0 else "gated"
