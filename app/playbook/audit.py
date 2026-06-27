"""Playbook 审计日志 — 记录人工/自动的 reconciliation 操作.

目标:
- 为 phantom void、manual reconcile 等 DB 修正操作留痕
- 支持可审计、可回滚、可追踪
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _default_audit_path() -> Path:
    """默认审计日志路径."""
    base = Path(__file__).resolve().parent.parent.parent
    audit_dir = base / "data" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "playbook_reconciliation.jsonl"


def log_reconciliation(
    action: str,
    reason: str,
    operator: str = "system",
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    audit_path: Optional[Path] = None,
) -> None:
    """写入一条 reconciliation 审计日志.

    Args:
        action: 操作类型, 如 "phantom_void", "manual_reconcile", "bulk_void"
        reason: 操作原因 (人类可读)
        operator: 操作主体, 默认 "system", 人工操作建议传用户名
        before: 操作前的数据快照 (可选)
        after: 操作后的数据快照 (可选)
        metadata: 额外元数据 (可选)
        audit_path: 审计日志文件路径, 默认 data/audit/playbook_reconciliation.jsonl
    """
    if audit_path is None:
        audit_path = _default_audit_path()

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "reason": reason,
        "operator": operator,
        "before": before or {},
        "after": after or {},
        "metadata": metadata or {},
        "hostname": os.environ.get("HOSTNAME", ""),
        "pid": os.getpid(),
    }

    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        # 审计日志不可影响主流程, 失败只记录 warning
        logger.warning(f"[audit] 写入 reconciliation 日志失败: {e}")


def get_recent_reconciliation_logs(
    action: Optional[str] = None,
    limit: int = 100,
    audit_path: Optional[Path] = None,
) -> list:
    """读取最近 reconciliation 审计日志 (用于回查/调试)."""
    if audit_path is None:
        audit_path = _default_audit_path()

    if not audit_path.exists():
        return []

    logs = []
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if action is None or rec.get("action") == action:
                        logs.append(rec)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"[audit] 读取 reconciliation 日志失败: {e}")

    return logs[-limit:] if limit else logs
