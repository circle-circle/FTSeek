"""UE 扩池影子的 horizon/批次标记契约 (2026-09-23)

背景: `data/shadow/universe_expansion_shadow.jsonl` 里 8/26 首启 score 时一次性回填了
26 个 capture 日 (horizon 1..43 天, 207 只 = 全样本 70%), 与日常批 (1-3 天) 混存。
下游按 scored_on 聚合会把长窗走势当 1 日读数 —— 实测把扩池 LONG/SHORT 的方向结论
整个翻转 (DEB-20260923 §2.4)。本测试钉住分类口径, 防止回填批再被静默混入日常读数。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import universe_expansion_shadow as ue  # noqa: E402


def test_horizon_days_basic_and_unparsable():
    assert ue._horizon_days("2026-08-26", "2026-07-14") == 43
    assert ue._horizon_days("2026-08-27", "2026-08-26") == 1
    assert ue._horizon_days("2026-09-21T12:00:00-04:00", "2026-09-18") == 3
    assert ue._horizon_days("", "2026-07-14") is None
    assert ue._horizon_days("2026-08-26", None) is None


def test_batch_boundary_is_three_days():
    """≤3 天 = 日常批; >3 天或不可解析 = 回填/长窗批。"""
    assert ue.BACKFILL_HORIZON_DAYS == 3
    assert ue._batch_of(0) == "daily"
    assert ue._batch_of(1) == "daily"
    assert ue._batch_of(3) == "daily"
    assert ue._batch_of(4) == "backfill"
    assert ue._batch_of(43) == "backfill"
    assert ue._batch_of(None) == "backfill"  # 解析失败不静默当日常读


def test_score_row_carries_batch_fields():
    """cmd_score 写盘行必须带 horizon_days/batch (下游可切片, 不靠现算)。"""
    src = (REPO / "scripts" / "universe_expansion_shadow.py").read_text(encoding="utf-8")
    block = src[src.index("def cmd_score("):]
    assert '"horizon_days": _h' in block
    assert '"batch": _batch_of(_h)' in block


def test_report_labels_backfill_rows():
    """控制台 report 对回填批要打 ⚠, 不能默认成 daily。"""
    src = (REPO / "scripts" / "universe_expansion_shadow.py").read_text(encoding="utf-8")
    assert "⚠回填批(混合horizon, 勿与日常批混算)" in src
    assert "sm.get(\"batch\") or _batch_of(_hd)" in src