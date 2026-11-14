"""源码契约: Event Intelligence 离线原型不得进入生产进程.

背景 (DEB-20261106 §17.1/§17.7): 三个离线原型模块被放在**生产包** `app/event_stream/` 里
(`intelligence.py` / `market_brief.py` / `instructions.py`), 当前零生产消费者 —— 这是
"今天安全、下次重启就静默加载"的经典口子。本测试把"零消费者"从口头声明变成可执行契约。

三条不变量:
  1. `app/event_stream/__init__.py` 不得 re-export 这三个模块 (否则任一 `import app.event_stream`
     的活模块都会连带加载它们 —— 包级 __init__ 连带)。
  2. `app/` 下不得出现指向这三个模块的 import 字符串 (生产代码面)。
  3. 检测器本身必须有牙: 对一段人为植入的 import 必须判 True (防"改哑成恒绿")。

允许的消费面: `scripts/da_event_*.py`(离线回放) 与 `tests/`。若将来要转生产, 必须
先在本文件显式登记真实消费者与开关, 而不是删掉断言。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "app" / "event_stream"
OFFLINE_MODULES = ("intelligence", "market_brief", "instructions")

# RFC-208 用户批准的唯一生产消费面；仍由 DA_EVENT_CONTEXT_INJECT 缺省关闭隔离。
# 其它 app 模块继续零消费者，防止离线原型被静默加载。
APPROVED_PRODUCTION_CONSUMERS = {
    "app/event_stream/da_prompt_injection.py": "DA_EVENT_CONTEXT_INJECT",
}

# 生产代码面上"引用离线原型"的形态 (import / from-import / 字符串动态导入)
_IMPORT_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"from\s+app\.event_stream\.(?:%s)\b" % "|".join(OFFLINE_MODULES),
        r"import\s+app\.event_stream\.(?:%s)\b" % "|".join(OFFLINE_MODULES),
        r"from\s+\.(?:%s)\s+import\b" % "|".join(OFFLINE_MODULES),
        r"event_stream\.(?:%s)\b" % "|".join(OFFLINE_MODULES),
    )
)


def _strip_comments(text: str) -> str:
    """去掉整行注释与行尾注释, 避免 tombstone 注释把守卫打红。"""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def references_offline_module(text: str) -> bool:
    body = _strip_comments(text)
    return any(p.search(body) for p in _IMPORT_PATTERNS)


def test_init_does_not_reexport_offline_modules():
    init_py = PKG / "__init__.py"
    assert init_py.exists(), "app/event_stream/__init__.py 缺失"
    assert not references_offline_module(init_py.read_text(encoding="utf-8")), (
        "app/event_stream/__init__.py 不得 import 离线原型模块: 任何 import app.event_stream 的"
        "活模块都会被连带加载 (包级 __init__ 连带)。"
    )


def test_no_production_importer_under_app():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.parent == PKG and path.stem in OFFLINE_MODULES:
            continue  # 模块自身不算消费者
        relative = str(path.relative_to(ROOT))
        if relative in APPROVED_PRODUCTION_CONSUMERS:
            continue  # RFC-208 显式批准且独立开关隔离的唯一生产注入口
        if references_offline_module(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(relative)
    assert not offenders, (
        "app/ 下出现离线原型的生产消费者 (会随重启进入生产进程): %s\n"
        "若确要转生产: 先在 tests/test_offline_prototype_isolation.py 登记消费者与开关, "
        "不要删断言。" % offenders
    )


def test_rfc208_consumer_is_explicitly_switch_gated():
    assert APPROVED_PRODUCTION_CONSUMERS == {
        "app/event_stream/da_prompt_injection.py": "DA_EVENT_CONTEXT_INJECT",
    }


def test_detector_has_teeth():
    """负向用例: 植入真实 import 形态必须被判 True, 否则守卫是恒绿的死门。"""
    assert references_offline_module("from app.event_stream.intelligence import build_snapshot\n")
    assert references_offline_module("    from .market_brief import build_brief\n")
    assert references_offline_module("mod = importlib.import_module('app.event_stream.instructions')\n")
    # 不误报: 同包内的正常模块与注释
    assert not references_offline_module("from app.event_stream.store import EventStore\n")
    assert not references_offline_module("# DEB-20261106: 离线原型见 app/event_stream/intelligence.py\n")