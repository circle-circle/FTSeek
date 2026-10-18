#!/usr/bin/env python3
"""pipeline.py 行数棘轮 (2026-10-08, DEB-20261008-playbook-pipeline-refactor-roadmap §9.5)。

背景: pipeline.py 10,207 行进入受控拆分路线 (facade+逐域迁移, 数周工程)。
迁移期间新代码会持续回流该文件 (10-08 F1 摘闸就 +26 行) — 没有棘轮,
阶段 1 没拆完文件先涨回去。

纪律 (与 except:pass/W456/naive-now 棘轮同型): 只降不升。
新增逻辑一律进新模块; 确需加大 pipeline.py 的例外须在本文件调低 BASELINE 前
先有 DEB 落档 (与改判据同级)。

回滚: 删本文件 (回到无管控状态)。
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
BASELINE = 3_700  # 2026-10-18 减法批: 实际 3,670 行; 原 10,207 基线余量 64% 已失效,
                  # 按棘轮“只降不升”下调 (DEB-20261008-playbook-pipeline-refactor-roadmap §9.5)


def _line_count() -> int:
    return len((REPO / "app" / "playbook" / "pipeline.py")
               .read_text(encoding="utf-8").splitlines())


def test_pipeline_py_not_growing():
    current = _line_count()
    assert current <= BASELINE, (
        f"pipeline.py 行数增长: {BASELINE} → {current}。拆分路线期间新增逻辑"
        f"一律进新模块 (DEB-20261008-playbook-pipeline-refactor-roadmap §9.5);"
        f" 确需例外须先 DEB 落档再调基线, 只降不升。")


def test_baseline_not_tampered_upward():
    """防「调基线过关」: 源码里的 BASELINE 数值不得超过立项值。"""
    src = (REPO / "tests" / "test_pipeline_size_ratchet.py").read_text(encoding="utf-8")
    m = re.search(r"BASELINE = ([\d_]+)", src)
    assert m and int(m.group(1).replace("_", "")) <= 3_700, (
        "BASELINE 被调大 — 棘轮失效; 调低需 DEB 落档, 调大禁止")
