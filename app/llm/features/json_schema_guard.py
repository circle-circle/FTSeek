"""
JSON Schema Guard (L0 2026-05-14)

LLM 输出统一通过 schema 校验, 不通过 → drop + log + 返回 None.
约束: 拒绝自由文本影响仓位决策.

无外部依赖 (不引入 jsonschema 包), 用纯 Python 实现最小子集.
"""
from __future__ import annotations
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_json_strict(text: str) -> Optional[Dict[str, Any]]:
    """
    从 LLM 输出抽取 JSON (容忍 ```json fenced code block / 前后说明文字).
    失败 → None
    """
    if not text or not text.strip():
        return None
    # 1. 直接 parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 抽 ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3. 抽第一个 { ... } 平衡块
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i+1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        return None
    return None


def validate_schema(
    obj: Any,
    required: Dict[str, type],
    optional: Optional[Dict[str, type]] = None,
    enums: Optional[Dict[str, List[Any]]] = None,
) -> Tuple[bool, str]:
    """
    最小 schema 校验:
    - required: 字段必须存在 + 类型匹配
    - optional: 若存在则类型匹配
    - enums: 字段值必须在白名单

    Returns: (ok, error_msg)
    """
    if not isinstance(obj, dict):
        return False, f"not_a_dict (got {type(obj).__name__})"

    # required
    for k, t in required.items():
        if k not in obj:
            return False, f"missing_required: {k}"
        if not _type_check(obj[k], t):
            return False, f"type_error: {k} expected {t.__name__}, got {type(obj[k]).__name__}"

    # optional
    if optional:
        for k, t in optional.items():
            if k in obj and obj[k] is not None and not _type_check(obj[k], t):
                return False, f"type_error_optional: {k}"

    # enums
    if enums:
        for k, allowed in enums.items():
            if k in obj and obj[k] not in allowed:
                return False, f"enum_violation: {k}={obj[k]!r} not in {allowed}"

    return True, ""


def _type_check(v: Any, t: type) -> bool:
    if t is float:
        return isinstance(v, (int, float))
    if t is str:
        return isinstance(v, str)
    if t is int:
        return isinstance(v, int) and not isinstance(v, bool)
    if t is bool:
        return isinstance(v, bool)
    if t is list:
        return isinstance(v, list)
    if t is dict:
        return isinstance(v, dict)
    return isinstance(v, t)


def parse_and_validate(
    text: str,
    required: Dict[str, type],
    optional: Optional[Dict[str, type]] = None,
    enums: Optional[Dict[str, List[Any]]] = None,
    feature: str = "unknown",
) -> Optional[Dict[str, Any]]:
    """一站式: parse + validate. 失败返回 None + log warning."""
    obj = parse_json_strict(text)
    if obj is None:
        logger.warning(f"[SchemaGuard·{feature}] JSON parse 失败, raw_len={len(text or '')}, raw_head={(text or '')[:200]}")
        return None
    ok, err = validate_schema(obj, required, optional, enums)
    if not ok:
        logger.warning(f"[SchemaGuard·{feature}] schema 校验失败: {err}")
        return None
    return obj


def clamp_numeric(value: float, min_val: float, max_val: float) -> float:
    """安全钳位 — LLM 输出数字越界时收敛到合法范围."""
    try:
        v = float(value)
    except Exception:
        return min_val
    return max(min_val, min(max_val, v))
