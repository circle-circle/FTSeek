"""
减法审计 R2 方法论固化 (2026-07-26):
archive/ 目录必须零活 import — F1 教训: api_limit_monitor 被误归档后
finnhub/twelvedata provider 仍从 scripts.archive import, 主进程带病运行 3 个月。

2026-09-10 (DEB-20260910 §8.9): 加 PROSE_ALLOWLIST。PATTERN 的第三条 `scripts/archive/`
是**字面量**探针 (为抓按路径动态加载 = 本批新登记的"零引用核对第六面"), 它连散文一起咬:
文件头退役证据注释、运维**回滚指引**、以及"验证 archive/ 被排除"的测试夹具路径。这些行
不是加载, 逐条人工核实后登记豁免。房型照 tests/test_dead_db_literal_gate.py: 双向 + 免行号。
  方向1: 未登记的命中 → 红 (新站点必须先人工核实再登记, 不许静默通过);
  方向2: 登记项已不再命中 → 红 (防名单腐烂成永久盲区)。
豁免键 = (文件, 稳定子串) 而非行号 (行号随任何编辑漂移 ⇒ 天天假告警, 假告警一多守卫就被无视)。
豁免**永不覆盖真 import 形态** (IMPORT_SHAPE 优先判定), 见 test_allowlist_does_not_mask_real_import。
"""
import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["app", "scripts", "config", "tests"]  # tests/ 2026-09-06 起纳入 (批F 曾藏活 import)
SELF = Path(__file__).resolve()
PATTERN = re.compile(r"(from\s+scripts\.archive|import\s+scripts\.archive|scripts/archive/)")
# 真加载形态: 豁免只对散文生效, 命中此形态一律红 (防有人把 import 写在含豁免子串的行里)
IMPORT_SHAPE = re.compile(r"^\s*(from|import)\s+[\w.]*archive")

PROSE_ALLOWLIST = {
    # 2026-11-15: 原 4 条里 2 条(observers.py 退役注释 / error_alert.py 回滚指引)是**散文**,
    #   已由 _code_only_lines 抹白后自然消失, 条目随之删除 (否则 direction2 腐烂检查红)。
    #   余下 2 条是**代码里的字符串字面量**(非 docstring/注释), 抹白不会也不能覆盖:
    ("tests/test_w456_log_hygiene_ratchet.py", "scripts/archive/dead.py"):
        "W4/W5/W6 棘轮的**测试夹具路径** —— 用例断言的正是 archive/ 被扫描面排除。非加载。",
    ("tests/test_error_alert.py", "应移入 scripts/archive/"):
        "断言失败提示文案: 告诉下一个改动人旧 .sh 该归档到哪。非加载。",
}


def _code_only_lines(text: str):
    """把**散文**抹白后的逐行文本: 注释 + 模块/类/函数 docstring。

    2026-11-15 根因修复: 守卫原按原文匹配, 于是"文件头退役说明/回滚指引/文档串里
    提到 archive 路径"一律当命中 (scripts/orderflow_forward_capture.py:6 的模块
    docstring 就是活例) ⇒ 只能靠 PROSE_ALLOWLIST 一条条登记, 名单只会腐烂。
    散文永远不是加载, 在匹配前抹掉才是根因解法。
    **刻意不抹代码里的字符串字面量** —— `scripts/archive/` 路径探针正是为了抓
    importlib/字符串动态加载, 抹掉它等于把那条防线删掉。
    """
    lines = list(text.splitlines())
    out = list(lines)
    # 1) docstring 区间 = 模块/类/函数体的首个字符串表达式
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            value = getattr(first, "value", None)
            if (isinstance(first, ast.Expr) and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)):
                end = getattr(first, "end_lineno", None) or first.lineno
                for lineno in range(first.lineno, end + 1):
                    if 1 <= lineno <= len(out):
                        out[lineno - 1] = ""
    except Exception:
        pass  # 语法异常交给 py_compile/其他守卫, 这里退回原文匹配 (更严而非更松)
    # 2) 行尾与整行注释 (代码里的 # 在字符串内时 tokenize 不会误判)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                if 1 <= row <= len(out) and out[row - 1]:
                    out[row - 1] = out[row - 1][:col]
    except Exception:
        pass
    return out


def _all_raw_hits() -> list:
    """全仓 (含 main.py) 命中 PATTERN 的原始行, 不做任何豁免 —— 供方向2 腐烂检查复用。"""
    hits = []
    targets = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(ROOT)
            if "archive" in rel.parts or "__pycache__" in rel.parts:
                continue
            if py.resolve() == SELF:
                continue  # 守卫自身含模式字面量与豁免名单, 不扫
            targets.append((str(rel), py))
    if (ROOT / "main.py").exists():
        targets.append(("main.py", ROOT / "main.py"))
    for rel, py in targets:
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 便宜的预筛: 原文不提及 PATTERN 就不必解析/分词
        if not PATTERN.search(text):
            continue
        for lineno, line in enumerate(_code_only_lines(text), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if PATTERN.search(stripped):
                hits.append((rel, lineno, stripped))
    return hits


def _is_offender(rel: str, stripped: str) -> bool:
    """一行是否算违反。单独成函数 ⇒ 可用合成行做防假绿直测, 不必真往仓里写违规文件。"""
    if not PATTERN.search(stripped):
        return False
    if IMPORT_SHAPE.search(stripped):
        return True  # 真加载形态永不豁免
    return not any(rel == f and sub in stripped for (f, sub) in PROSE_ALLOWLIST)


def test_no_live_imports_from_archive():
    offenders = [f"{rel}:{lineno}: {stripped[:100]}"
                 for rel, lineno, stripped in _all_raw_hits()
                 if _is_offender(rel, stripped)]
    assert not offenders, (
        "archive/ 必须零活 import (活模块请移回 scripts/, 见 DEB-20260726 F1)。\n"
        "若命中的是散文/回滚指引/测试夹具而非加载, 人工核实后登记进 PROSE_ALLOWLIST 并写明理由:\n"
        + "\n".join(offenders)
    )


def test_allowlist_not_rotten():
    """方向2: 登记项必须仍命中 —— 对应行被改写/删除后要同步删条目, 否则名单腐烂成永久盲区。"""
    raw = [(rel, stripped) for rel, _lineno, stripped in _all_raw_hits()]
    stale = [f"{f} :: {sub}  ({why})"
             for (f, sub), why in PROSE_ALLOWLIST.items()
             if not any(rf == f and sub in rs for rf, rs in raw)]
    assert not stale, (
        "PROSE_ALLOWLIST 陈旧豁免 (仓内已无对应行) → 删掉该条:\n" + "\n".join(stale))


def test_allowlist_does_not_mask_real_import():
    """防假绿: 豁免只针对散文子串; 真 import 形态即使写在豁免文件里、即使带豁免子串, 也必须红。"""
    for (f, sub) in PROSE_ALLOWLIST:
        for line in (f"from scripts.archive import x  # {sub}",
                     f"import scripts.archive.foo  # {sub}",
                     f"from scripts.archive.error_alert import push  # {sub}"):
            assert _is_offender(f, line), f"豁免掩盖了真加载: {f} / {line}"
    # 反向: 豁免自身确实生效 (否则方向1 会退化成"任何提及都红" = 守卫被无视)
    assert not _is_offender("tests/test_error_alert.py",
                            'assert ok, "旧 .sh 应移入 scripts/archive/ 后重跑"')


def test_prose_is_stripped_before_matching():
    """承重测试 (2026-11-15): 散文(注释/docstring)不得触发守卫, 代码字符串必须仍触发。

    没有这条, _code_only_lines 被改坏也不会有任何用例变红 (原缺陷 = 散文误报, 而误报
    只会让 PROSE_ALLOWLIST 越堆越长, 没人会去查抹白逻辑还在不在)。
    """
    src = (
        '"""模块说明: 旧实现见 scripts/archive/orderbook_s2_capture.py (非加载)。"""\n'
        "def f():\n"
        '    """函数说明: 也可 from scripts.archive import x —— 只是散文。"""\n'
        "    # 行尾/整行注释: scripts/archive/dead.sh\n"
        '    msg = "应移入 scripts/archive/ 才算归档"  # 代码里的字符串字面量\n'
        "    return msg\n"
    )
    code_lines = _code_only_lines(src)
    joined = "\n".join(code_lines)
    assert "orderbook_s2_capture" not in joined          # 模块 docstring 抹白
    assert "只是散文" not in joined                        # 函数 docstring 抹白
    assert "dead.sh" not in joined                        # 注释抹白
    assert "scripts/archive/" in joined                   # 代码字符串保留 (动态加载探针)


def test_real_import_in_code_is_caught_after_stripping():
    """抹白后真加载形态仍必须红 (防'抹白'把 import 一起吃掉)。"""
    src = "from scripts.archive import dead  # 注释\nimport scripts.archive.foo\n"
    code_lines = _code_only_lines(src)
    assert any(IMPORT_SHAPE.search(ln) for ln in code_lines)
