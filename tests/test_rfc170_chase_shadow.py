"""RFC-170 回归: 确认侧追价影子 must be record-only.

背景 (2026-10-26, n=53 已结算确认单):
  - 确认单追价 opp 均 +0.350% / 中 +0.299% (mid-mid 尺 +0.365%), 越过预注册 c3 天花板 0.30%;
  - 3 笔 SL_INITIAL 全部落在追价最大的三笔 (opp +0.41/+1.07/+1.41%);
  - Spearman(opp, 净额) = -0.264, p=0.056。
⇒ 本批只**记录** opp≥0.5% 的 would_abandon, 不动判定 (改判据要等裁决日, 防阈值购物)。

本测试钉三件事: ①阈值语义 ②写入 forward 行 ③**不在判定链里被消费**。
"""
import re
from pathlib import Path

import app.playbook.entry_confirm_gate as gate

_SRC = Path(gate.__file__).read_text(encoding="utf-8")


def test_chase_shadow_flag_threshold_semantics():
    assert gate.CHASE_SHADOW_PCT == 0.5
    assert gate.chase_shadow_flag(0.5001) is True
    assert gate.chase_shadow_flag(0.5) is True          # ≥ 边界含等号
    assert gate.chase_shadow_flag(0.4999) is False
    assert gate.chase_shadow_flag(-0.0307) is False     # 今日 CAT: 顺价确认
    # 脏输入不得抛 (影子路径异常不许影响交易)
    assert gate.chase_shadow_flag(None) is False
    assert gate.chase_shadow_flag("") is False
    assert gate.chase_shadow_flag("0.7") is True


def test_shadow_fields_written_to_forward_row():
    assert '"chase_shadow_pct": CHASE_SHADOW_PCT' in _SRC
    assert '"chase_shadow_would_abandon": _chase_shadow' in _SRC
    # 标记必须在两臂都落 (forward 行是唯一出口)
    assert "_chase_shadow = bool(still and chase_shadow_flag(opp))" in _SRC


def test_shadow_is_not_consumed_by_the_judgment():
    """影子若进了判定分支就不是影子 —— 判定块内不得出现 _chase_shadow。"""
    start = _SRC.index("_fs_cap_hit = bool(")
    # 判定块终点 = 该判定之后的那次 _PENDING.pop (EXPIRED 分支里还有一个更早的)
    end = _SRC.index("_PENDING.pop(inst_id, None)", start)
    judgment_block = _SRC[start:end]
    assert judgment_block, "切片为空 = 锚点选错, 契约测试失效"
    assert "_chase_shadow" not in judgment_block, (
        "追价影子被判定链消费 = 交易行为变更, 必须走预注册裁决, 不能夹带")
    # 判定入口保持原样: FADE_SHORT cap + grace 两条, 没多第三条
    assert "_fs_cap_hit = bool(" in judgment_block
    assert "grace_outcome = \"persisted\"" in judgment_block


def test_no_yaml_key_required():
    """阈值走代码常量, 不开 YAML 键 (避免 playbook.* 热载盲区 + 配置漂移)。"""
    yml = (Path(gate.__file__).resolve().parents[2] / "config" / "unified_config.yaml")
    if yml.exists():
        text = yml.read_text(encoding="utf-8")
        assert "chase_shadow_pct" not in text