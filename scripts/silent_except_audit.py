#!/usr/bin/env python3
"""静默 except 普查: 找出"吞异常且无任何日志"的处理器.

病灶模型 (2026-08-26·用户令深度检查): subscription_manager._auto_loop 的
TypeError 被 except 吞掉后周末特性静默死亡数月。此类 handler 运行期零痕迹,
是"日志显示正常但其实坏了"的最大温床。

分级:
  SILENT   : handler 体只有 pass/continue/break, 无日志无常量 → 最高危
  NO_LOG   : handler 有处理逻辑但全程无 logging 调用 → 中危
只扫关键链路目录; tests/web_dashboard 噪声大默认排除。

用法: ./venv/bin/python scripts/silent_except_audit.py [--all] [--json OUT]
"""
import ast
import json
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
# 关键链路: 调度/管线/数据/风控/执行/LLM
CRITICAL_DIRS = [
    "app/core", "app/us_pipeline", "app/data", "app/risk",
    "app/execution", "app/llm", "app/coordinator", "app/playbook",
    "app/shared", "scripts",
]
LOG_HINTS = ("logger.", "logging.", "_log(", "print(")


def _has_log_call(body) -> bool:
    """handler 体内是否出现任何日志/输出调用."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                src = ast.dump(node.func)[:0]  # 占位, 用名字判断
                f = node.func
                name = (getattr(f, "attr", None) or getattr(f, "id", ""))
                if any(h.rstrip("(") in f"{name}" or h in ast.unparse(node)
                       for h in LOG_HINTS):
                    return True
    return False


def analyze(path: Path, rel: Path) -> List[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # 裸 except 或 except Exception → 吞一切
        t = node.type
        broad = t is None or (
            isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))
        if not broad:
            continue
        body_types = {type(s).__name__ for s in node.body}
        if body_types <= {"Pass", "Continue", "Break"}:
            level = "SILENT"
        elif not _has_log_call(node.body):
            level = "NO_LOG"
        else:
            continue
        # 上下文: 所在函数名
        out.append({"level": level, "at": f"{rel}:{node.lineno}",
                    "ctx": _enclosing(tree, node.lineno)})
    return out


def _enclosing(tree, lineno: int) -> str:
    best = "<module>"
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.lineno <= lineno <= max(getattr(n, "end_lineno", n.lineno), n.lineno):
                if getattr(n, "end_lineno", 0):
                    best = n.name
    return best


def main():
    dirs = CRITICAL_DIRS
    if "--all" in sys.argv:
        dirs = ["app"]
    findings = []
    for d in dirs:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "venv" in p.parts or "__pycache__" in p.parts:
                continue
            findings.extend(analyze(p, p.relative_to(ROOT)))
    sil = [f for f in findings if f["level"] == "SILENT"]
    nol = [f for f in findings if f["level"] == "NO_LOG"]
    print(f"# SILENT(纯pass/continue): {len(sil)} 处 | NO_LOG(有逻辑但零日志): {len(nol)} 处\n")
    print("== SILENT ==")
    for f in sil:
        print(f"  {f['at']}   ({f['ctx']})")
    print("\n== NO_LOG ==")
    for f in nol:
        print(f"  {f['at']}   ({f['ctx']})")
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        Path(out).write_text(json.dumps(findings, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
