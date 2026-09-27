"""守卫: ADJUDICATED (已裁结论精选表) 每条必须可回源 (RFC-114 阶段0)。

立起理由 (实测危害, 非假想):
  ADJUDICATED 是裁决面的"精选表" — note 承载判据读数与处置, 被推送面
  (pending_adjudication_report / shadow_lexicon) 与未来 meta-audit 引用。
  若 note 无任何回源锚, 读数只存在自由文本转述里 ⇒ 违反 MEMORY 纪律
  「引用结论必 grep 原文, 找不到=自造叙事」, 且该纪律此前只约束人、不约束登记表。
  同族先例: test_criteria_registry_visibility.py (B21 达标一个月无人读的可见性缺口,
  本守卫是它的镜像: 读数被引用但无法回源)。

锚点三选一 (RFC-114 §0 自曝更正后的口径, 满足其一即算可回源):
  1. doc 锚: note 含入库文档引用 (RFC-\\d+ / DEB-… / ADR-\\d+ / docs/ / 判据字典)。
     裸 "§" 不算 — 无文档名的章节号定位不了 (2026-09-27 实测 vwap_gate_ab /
     futu_channel_iv_hv 仅凭 "§8.4"/"§8.1" 过旧口径, 属弱锚, 靠 eval_script 锚兜底)。
  2. script-in-note 锚: note 含 scripts/….py 或 app/….py 路径且文件在盘。
  3. eval_script 锚: 注册表条目 eval_script 非空且文件在盘 (可复跑 = 读数可重现)。

限度 (显式声明, 防被读成语义保证 — RFC-114 §2 Q6-a):
  - 只测"有锚", 不测"锚对": note 写错 RFC 章节号本守卫照样绿;
    锚的语义正确性靠复核日人工抽查。
  - 不涉"活跃项无 eval_script"轴 (归 no_adjudication_reason=NO_EVAL_SCRIPT 挂账, RFC-114 F2)。

豁免表 EXEMPTIONS: 仅当出现"读数已出、锚确实暂缺"的登记时加一行
  {id: (类型, 理由, 期限)}; 类型二值: PENDING_BACKFILL(必须带期限) /
  UNTRACEABLE_BACKFILL(原始实跑记录未入库, 永久豁免+理由)。空表 = 全量有锚。
  ⚠️ 禁止为过守卫补假锚 (回填造假比失锚更糟 — RFC-114 §2 Q6-b: 回填必须贴被引原文,
  找不到原文的进 UNTRACEABLE 留名, 不许硬凑)。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.playbook.shadow_registry import ADJUDICATED, REGISTRY

REPO = Path(__file__).resolve().parents[1]

# 严格文档锚: 必须能定位到入库文档。裸 § 不算 (见 docstring 锚点1)。
_DOC_ANCHOR = re.compile(r"RFC-\d+|DEB-[\dA-Za-z-]+|ADR-\d+|docs/|判据字典")
_SCRIPT_IN_NOTE = re.compile(r"(?:scripts|app)/[\w/.-]+\.py")

# 豁免表: 当前为空 (2026-09-27 实测 27/27 可回源, RFC-114 §1.5)。
# 格式: id -> (类型, 理由, 期限ISO或None)。加行须写理由; 到期未回填 = 本测试红。
EXEMPTIONS: dict = {}


def _script_exists(rel: str) -> bool:
    p = Path(rel.split(" ")[0].strip())
    if not p.is_absolute():
        p = REPO / p
    return p.exists()


def anchor_of(note: str, eval_script) -> str:
    """返回锚类型 ('doc'/'script_in_note'/'eval_script'), 无锚返回 ''。可独立单测。"""
    note = note or ""
    if _DOC_ANCHOR.search(note):
        return "doc"
    for m in _SCRIPT_IN_NOTE.findall(note):
        if _script_exists(m):
            return "script_in_note"
    if eval_script and _script_exists(str(eval_script)):
        return "eval_script"
    return ""


def test_adjudicated_notes_are_traceable():
    reg = {s.id: s for s in REGISTRY}
    unanchored = []
    expired = []
    for k, v in ADJUDICATED.items():
        item = reg.get(k)
        # 幽灵条目 (不在 REGISTRY) 由 test_pending_adjudication_report 的幽灵守卫管;
        # 本守卫按无 eval_script 处理 (item=None ⇒ 锚只剩 note 面), 不重复判轴。
        anchor = anchor_of(v.get("note", ""), item.eval_script if item else None)
        if anchor:
            continue
        ex = EXEMPTIONS.get(k)
        if ex:
            kind, _reason, deadline = ex
            assert kind in ("PENDING_BACKFILL", "UNTRACEABLE_BACKFILL"), f"非法豁免类型: {ex}"
            if kind == "PENDING_BACKFILL":
                assert deadline, f"{k}: PENDING_BACKFILL 必须带期限"
                import datetime
                if deadline < str(datetime.date.today()):
                    expired.append((k, deadline))
            continue
        unanchored.append(k)
    assert not unanchored, (
        f"ADJUDICATED 无回源锚 (doc/script-in-note/eval_script 三选一, 见本文件 docstring): "
        f"{unanchored} — 补锚或登记 EXEMPTIONS (禁补假锚)"
    )
    assert not expired, f"PENDING_BACKFILL 豁免已过期未回填: {expired}"


def test_guard_has_teeth():
    """反向测试: 无锚 note + 无 eval_script 必须判 '' (防守卫恒绿)。"""
    assert anchor_of("n=31 ✓ / 笔均 -16.6 ✓ ⇒ 继续攒样", None) == ""
    assert anchor_of("读数出处: RFC-092 §5", None) == "doc"
    assert anchor_of("见 §8.4 补齐后判定", None) == ""          # 裸 § 不算 doc 锚
    assert anchor_of("实跑 scripts/no_such_file_xyz.py", None) == ""  # 路径不存在不算
    # 在盘脚本路径 (真文件) 算 script_in_note
    assert anchor_of("实跑 scripts/pending_adjudication_report.py", None) == "script_in_note"
    # eval_script 在盘 ⇒ 算锚; 不在盘 ⇒ 不算
    assert anchor_of("n=5", "scripts/pending_adjudication_report.py") == "eval_script"
    assert anchor_of("n=5", "scripts/definitely_missing_9x.py") == ""
