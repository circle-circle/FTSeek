"""死代码坟场守卫 (R-2a, 2026-11-15)

减法批删掉的东西不允许悄悄长回来 —— 静态坟场 + 导入坟场双闸:
  1. 文件不得重现 (pathlib 检查, 防 git revert 手滑带回来而未重新论证消费方)
  2. 模块不得可导入
  3. 代码里不得再出现对它们的 import 语句 (**用 AST 看 import 节点**,
     所以本文件/生产代码的 tombstone 注释里出现模块名不会被误判 ——
     那是"散文命中代码"的老坑, 反过来也不该让注释救活死代码)

背景: 这四个模块零消费方 (唯一引用是包 __init__ re-export 与
di_container 的死工厂); 现役策略真源是 app/core/strategy_registry.py。
"""
from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

GONE_FILES = [
    "app/strategy/base.py",
    "app/strategy/registry.py",
    "app/decision/strategies/__init__.py",
    "app/decision/strategies/base.py",
    "app/decision/strategies/ma_cross.py",
    "app/decision/strategies/price_break.py",
    "app/decision/strategies/rsi_rebound.py",
    "app/analysis/fundamental_filter.py",
]

GONE_MODULES = [
    "app.strategy.base",
    "app.strategy.registry",
    "app.decision.strategies",
    "app.analysis.fundamental_filter",
]


@pytest.mark.parametrize("rel", GONE_FILES)
def test_gone_files_stay_gone(rel: str) -> None:
    assert not (ROOT / rel).exists(), f"{rel} 又长回来了 —— 先证明消费方再恢复"


@pytest.mark.parametrize("mod", GONE_MODULES)
def test_gone_modules_are_unimportable(mod: str) -> None:
    with pytest.raises(ImportError):
        importlib.import_module(mod)


def _iter_py_roots():
    yield ROOT / "main.py"
    yield from (ROOT / "app").rglob("*.py")
    yield from (ROOT / "scripts").rglob("*.py")


def test_no_import_statements_for_gone_modules() -> None:
    """AST 级扫描: 任何 import/from-import 指向坟场模块即红。"""
    needles = {m.rsplit(".", 1)[0] for m in GONE_MODULES} | set(GONE_MODULES)
    hits: list[str] = []
    for path in _iter_py_roots():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not any(n.split(".")[0] in text for n in needles):
            continue  # 便宜预筛: 连包名都没有就不用解析
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if name in needles:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {name}")
    assert not hits, "仍有 import 指向已删模块:\n" + "\n".join(hits)