"""P1-4 跳空抄底减法门 预注册守卫 (2026-08-29).

预注册的意义 = 在前向数据到达之前钉死判据, 防事后阈值漂移
(DEB-20260823-phase1 P1-4 节; 证据来源 DEB-20260817 第 7 轮 47 笔接飞刀裁决,
用户 2026-08-29 裁"同意"预注册)。本测试钉住判据要素: 阈值 -1.5% / 开关名 /
GATE 1 后激活时序 / 反转判据 n≥30 / gap_semantics 结案链接 / G6 证据登记。
改动任一要素 = 修改预注册, 须新开 DEB 节记录理由, 而非改字符串蒙混。

回滚: 删本文件 + revert 对应提交即可, 零生产依赖 (门本身尚未实施)。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PHASE1 = REPO / "docs" / "debates" / "DEB-20260823-phase1-signal-quality-design.md"
DEB817 = REPO / "docs" / "debates" / "DEB-20260817-symbol-profile-market-sense.md"
LEDGER = REPO / "docs" / "CONTROL-LOOP-LEDGER.md"


def test_p1_4_rule_pinned():
    text = PHASE1.read_text(encoding="utf-8")
    assert "P1-4" in text, "P1-4 预注册节被删 (DEB-20260823-phase1)"
    assert "-1.5%" in text, (
        "P1-4 阈值被改动 — 预注册判据: 开盘缺口 ≤ -1.5% 禁 playbook LONG 新入场; "
        "改判据须新开预注册节, 不是改字符串")
    assert "falsification_gate_gap_fade_enabled" in text, (
        "P1-4 开关名漂移 — 实施必须用预注册键名 falsification_gate_gap_fade_enabled")
    assert "GATE 1" in text, "P1-4 激活时序 (GATE 1 裁决后用户裁决) 被删"


def test_p1_4_reversal_criteria_pinned():
    text = PHASE1.read_text(encoding="utf-8")
    assert "反事实" in text and "≥30" in text, (
        "P1-4 反转判据被删 — 预注册: 前向被拦截 ≥30 笔且反事实中位 ≥0 → 撤门")
    assert "空转" in text, "P1-4 空转报告判据被删 (连续 4 周 0 拦截须报告)"


def test_gap_semantics_round7_closed_and_linked():
    text = DEB817.read_text(encoding="utf-8")
    assert "第 7 轮" in text, "gap_semantics 结案轮 (第 7 轮) 被删"
    assert "P1-4" in text, "结案轮与 P1-4 预注册的链接被删 (问题转移去向)"


def test_g6_rbrk_evidence_registered():
    text = LEDGER.read_text(encoding="utf-8")
    assert "RBRK" in text, "G6 复活前置条件② 证据登记 (RBRK 8/28) 被删"
