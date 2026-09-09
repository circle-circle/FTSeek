"""[DEB-20260903 议题二方案A] 被动侧净保本限价 → marketable limit 契约测试.

2026-09-02 DELL 案 (净 -$49.30): 20s 轮询间隙价格击穿保本地板
(检测价 473.1199 < 地板 474.6533), 被动侧限价结构性不可成交 — 触发后 bar
窗口 high 472.36 永不及限价, 唯一结局 = 3s 死等→撤单→市价 (~12s 往返,
该分量 $30.90/63%)。家族 5 案 (SNDK 8/18 / MU 8/20 / WPM 8/20 / AU 8/20 /
DELL 9/2) 0 回摆成交, 全负 (净均值 ≈ -$19.7/案)。

修复 (用户裁"按建议执行"): 被动侧改挂 marketable limit = 检测价∓垫
(breakeven_passive_marketable_pad_bps, 默认 15bps) — 立即成交且带地板;
主动侧 (8/20 方案A 贴线几何) 行为完全不变。回滚: 键改 0 = 回旧保本限价行为。
"""
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "app" / "playbook" / "pipeline.py"
_CFG_SRC = Path(__file__).resolve().parents[1] / "app" / "playbook" / "config.py"
_YAML = Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml"


def _exit_block() -> str:
    s = _SRC.read_text(encoding="utf-8")
    idx = s.find('if exit_reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "PATIENT_EXIT_FILL"):')
    assert idx > 0, "净保本限价保护块必须存在"
    return s[idx:idx + 4600]


def test_passive_branch_gated_and_mirror_formulas():
    """marketable 分支仅被动侧进入; LONG=检测价×(1-垫), SHORT 镜像 ×(1+垫)."""
    b = _exit_block()
    g = b.find('if _be_side == "passive"')
    assert g > 0, "marketable 分支必须由被动侧判定把守"
    seg = b[g:g + 1800]
    assert "1 - _mkt_pad_bps / 10000.0" in seg, "LONG 被动侧: 限价=检测价×(1-垫bps)"
    assert "1 + _mkt_pad_bps / 10000.0" in seg, "SHORT 被动侧镜像: 限价=检测价×(1+垫bps)"
    assert 'breakeven_passive_marketable_pad_bps' in seg, "垫宽必须走配置键"
    assert '"marketable_limit"' in seg, "分支必须打 path=marketable_limit 标记"


def test_active_side_keeps_breakeven_floor():
    """回归红线: 主动侧 (8/20 方案A 贴线几何) 行为完全不变 — 限价恒=保本地板,
    改价语句只允许出现在被动侧分支内。"""
    b = _exit_block()
    g = b.find('if _be_side == "passive"')
    assert g > 0
    before = b[:g]
    assert "_close_limit_price = round(current_price" not in before, (
        "主动侧路径不得改价 — 改价只能发生在被动侧分支 (回归红线)")


def test_rollback_pad_zero_keeps_floor():
    """回滚语义: 垫宽 ≤0 → 不进入 marketable 分支, 保持旧保本限价行为."""
    b = _exit_block()
    g = b.find('if _be_side == "passive"')
    seg = b[g:g + 1800]
    assert "if _mkt_pad_bps > 0:" in seg, (
        "垫宽 0 必须短路回旧保本限价行为 (配置级回滚, 免 revert)")


def test_telemetry_records_path_and_floor():
    """毕业判分基建: _be_close_telemetry 按侧分组之外增记 path/floor_px/pad_bps."""
    b = _exit_block()
    t = b.find('meta["_be_close_telemetry"]')
    assert t > 0
    seg = b[t:t + 400]
    for k in ('"path"', '"floor_px"', '"pad_bps"', '"limit_side"'):
        assert k in seg, f"遥测必须含 {k} (议题二毕业判分: 被动侧笔数与净结局按路径分组)"


def test_config_key_default_15_in_yaml():
    """活性值 SSOT: unified_config.yaml 带键且默认 15bps."""
    y = _YAML.read_text(encoding="utf-8")
    assert "breakeven_passive_marketable_pad_bps: 15" in y, (
        "yaml 必须带 breakeven_passive_marketable_pad_bps: 15 (活性值 SSOT)")


def test_pad_key_participates_in_exit_hash(monkeypatch):
    """防漏键假绿 (R-4b 纪律): 垫宽改变已实现 PnL, 必须入退出参数哈希 —
    改键值 → exit config version 必须变。"""
    from app.playbook.config import get_playbook_config
    c = get_playbook_config()
    orig_get = c.get
    v0 = c.get_exit_config_version()
    try:
        monkeypatch.setattr(
            c, "get",
            lambda k, d=None: 0.0 if k == "breakeven_passive_marketable_pad_bps"
            else orig_get(k, d))
        if "_exit_config_version" in c.__dict__:
            del c.__dict__["_exit_config_version"]
        v1 = c.get_exit_config_version()
    finally:
        c.__dict__.pop("_exit_config_version", None)  # 防污染同进程后续用例
    assert v0 != v1, (
        "breakeven_passive_marketable_pad_bps 必须入退出参数哈希 "
        "(漏键 = 改垫宽不动版本号 = KR2 判分假绿)")


def test_new_exit_version_registered_in_freeze_baseline():
    """KR2 豁免通道闭环: 新哈希必须已登记 config_freeze_baseline.json 且带裁决说明."""
    bl = json.loads((Path(__file__).resolve().parents[1]
                     / "data" / "config_freeze_baseline.json").read_text(encoding="utf-8"))
    from app.playbook.config import get_playbook_config
    v = get_playbook_config().get_exit_config_version()
    note = bl.get("exit_versions", {}).get(v, "")
    assert note, f"当前退出参数版本 {v} 未登记冻结基线豁免清单 (KR2 周检将告警)"
    assert "DEB-20260909" in note, "豁免说明必须携带 DEB 裁决依据"
