"""
原子文件写入工具 (P0-6 2026-05-14)

避免读写竞态: tempfile 写满后 os.replace 原子覆盖.
带 mtime 双重校验防 phantom-empty (写到一半被读)。
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional, Tuple


def atomic_write_text(path: str, content: str, encoding: str = "utf-8"):
    """原子写入文本: tmp + os.replace"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_", suffix=p.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def atomic_write_json(path: str, obj: Any, indent: int = 2):
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False, default=str))


def atomic_read_with_mtime(path: str, encoding: str = "utf-8") -> Tuple[Optional[str], Optional[float]]:
    """
    读 + 返回 mtime. 适合幂等检查 (调用方记 mtime, 下次发现没变就跳过).

    防 phantom-empty: 读 2 次, mtime 不变才信任内容.
    """
    p = Path(path)
    if not p.exists():
        return None, None
    try:
        mtime1 = p.stat().st_mtime
        content = p.read_text(encoding=encoding)
        mtime2 = p.stat().st_mtime
        if mtime1 != mtime2:
            return None, None  # 文件正在被改, 视为不可信
        return content, mtime1
    except Exception:
        return None, None


def atomic_touch(path: str):
    """原子 touch (创建或更新 mtime), 不影响内容"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
