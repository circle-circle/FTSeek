"""守卫: 影子注册表 graduation_criteria 的出处可回源 (DEB-20261007 方案B, 2026-10-07).

立起理由 (实测危害, 非假想):
  `entry_band_widen` 的 graduation_criteria 曾抄成 DEB-20260806 锚限价影子的主门
  (sign-flip B=10000), min_n 同批被拉到 60 — 而 RFC-065 原文是 n≥30 + 单侧安全门。
  后果: n=38 时「按文档口径已过门、按注册表口径差 22 行」, 该影子永远等不到裁决日
  (与台账「攒着永远没有裁决日 = 用无限攒样规避裁决」同族)。串台的直接证据是注册表
  同一文件内 :901 (错标 RFC-065) 与 :931 (正标 DEB-20260806) 出现同一判据文本。

三层守卫 (由窄到宽):
  1. entry_band_widen 专项: 判据文本中「预注册见 RFC-065: 」之后的部分必须是
     RFC-065 文档的**逐字子串** (修复后的锚定, 再被改回错轴文本 ⇒ 红)。
  2. 负向测试: 把该条目判据注入 DEB-20260806 的错轴文本 ⇒ 专项校验必须 FAIL
     (证明守卫有牙, 不是恒绿)。
  3. 通用死引用防御: 所有条目 graduation_criteria 里引用的 RFC-\\d+ / DEB-… 文档
     必须在 docs/decisions 或 docs/debates 里存在 (2026-10-07 实测全量通过)。

限度 (显式声明, 防被读成语义保证):
  - 逐字校验只对 entry_band_widen 生效, 不推广到全注册表: 既有条目存在格式化差异
    (如 limit_entry 判据写 B=10000 而 DEB-20260806 原文是 B=10,000), 全量逐字
    匹配会产生假红; 推广前须先做数字格式归一, 属另案。
  - 只测「文本可回源」, 不测「判据语义与该影子匹配」— 后者靠复核日人工抽查
    (同 test_adjudicated_note_anchor 的限度声明)。

回滚: 删本文件即可, 零生产依赖。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.playbook.shadow_registry import REGISTRY

REPO = Path(__file__).resolve().parents[1]
RFC_065 = REPO / "docs" / "decisions" / "RFC-065-entry-band-vol-widen.md"
PREFIX = "预注册见 RFC-065: "


def _get_item(item_id: str):
    return next((s for s in REGISTRY if s.id == item_id), None)


def _verbatim_in_doc(criteria: str, doc_text: str, prefix: str) -> bool:
    """criteria 中 prefix 之后的判据文本是否为 doc_text 的逐字子串。可独立单测。"""
    if prefix not in criteria:
        return False
    quoted = criteria.split(prefix, 1)[1].strip()
    return bool(quoted) and quoted in doc_text


def test_rfc065_doc_exists():
    assert RFC_065.exists(), f"RFC-065 文档不在盘: {RFC_065}"


def test_entry_band_widen_criteria_verbatim_in_rfc065():
    """修复后的判据文本必须能在 RFC-065 原文里逐字命中 (DEB-20261007 方案B)。"""
    item = _get_item("entry_band_widen")
    assert item is not None, "注册表缺 entry_band_widen 条目"
    doc = RFC_065.read_text(encoding="utf-8")
    assert _verbatim_in_doc(item.graduation_criteria, doc, PREFIX), (
        f"entry_band_widen 判据无法在 RFC-065 逐字回源 (串台复发?): "
        f"{item.graduation_criteria!r}"
    )
    # min_n 与 RFC-065 样本门同轴 (文档原文 n≥30; 60 是 DEB-20260806 的错轴锚)
    assert item.min_n == 30, f"entry_band_widen min_n={item.min_n}, RFC-065 原文是 30"
    assert "sign-flip" not in item.graduation_criteria, (
        "sign-flip B=10000 是 DEB-20260806 锚限价影子的门, 出现在 RFC-065 条目 = 串台复发"
    )


def test_wrong_axis_injection_must_fail():
    """负向: 注入 DEB-20260806 错轴判据文本 ⇒ 逐字校验必须 FAIL (证明守卫有牙)。"""
    wrong = "预注册见 RFC-065: sign-flip B=10000 单侧α=0.025 + 双条件 + 符号共门"
    doc = RFC_065.read_text(encoding="utf-8")
    assert not _verbatim_in_doc(wrong, doc, PREFIX), (
        "错轴文本竟然在 RFC-065 里逐字命中 — 要么 RFC-065 被污染, 要么本守卫失效"
    )


def test_all_referenced_rfc_deb_docs_exist():
    """通用: 全注册表 graduation_criteria 引用的 RFC/DEB 文档必须在盘 (死引用防御)。"""
    missing = []
    for item in REGISTRY:
        gc = item.graduation_criteria or ""
        for tag in set(re.findall(r"RFC-\d+|DEB-\d{8}", gc)):
            hits = (list((REPO / "docs" / "decisions").glob(tag + "*.md"))
                    + list((REPO / "docs" / "debates").glob(tag + "*.md")))
            if not hits:
                missing.append((item.id, tag))
    assert not missing, f"判据引用了不存在的文档: {missing}"
