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
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["app", "scripts", "config", "tests"]  # tests/ 2026-09-06 起纳入 (批F 曾藏活 import)
SELF = Path(__file__).resolve()
PATTERN = re.compile(r"(from\s+scripts\.archive|import\s+scripts\.archive|scripts/archive/)")
# 真加载形态: 豁免只对散文生效, 命中此形态一律红 (防有人把 import 写在含豁免子串的行里)
IMPORT_SHAPE = re.compile(r"^\s*(from|import)\s+[\w.]*archive")

PROSE_ALLOWLIST = {
    ("app/pipeline/observers.py", "run_metrics_collector.py 已归档"):
        "文件头退役证据注释 (2026-09-10 §7.7): 说明 MetricsObserver 为何保留 —— 唯一调用方已归档。非加载。",
    ("scripts/error_alert.py", "crontab 2 行指回"):
        "文件头**回滚指引** (2026-09-10 §7.5): 运维需要可复制的旧脚本准确路径。本文件零 import/零调用它。",
    ("tests/test_w456_log_hygiene_ratchet.py", "scripts/archive/dead.py"):
        "W4/W5/W6 棘轮的**测试夹具路径** —— 用例断言的正是 archive/ 被扫描面排除。非加载。",
    ("tests/test_error_alert.py", "应移入 scripts/archive/"):
        "断言失败提示文案: 告诉下一个改动人旧 .sh 该归档到哪。非加载。",
}


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
        for lineno, line in enumerate(text.splitlines(), 1):
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
    assert not _is_offender("scripts/error_alert.py",
                            '回滚: crontab 2 行指回 scripts/archive/error_alert.sh + 删本文件。')
