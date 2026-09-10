"""settle_overnight_shadow 配置门契约 (2026-09-10, DEB-20260910 §2 议题二 A 案第②步「配置停」).

病灶 (日志实测, 非推断): 结算器是 AFTER_HOURS 段 12 个 in-process 钩子里**唯一无任何门**的
一个。写入方 `playbook.overnight_hold_shadow_enabled` 2026-08-28 已置 false, 结算器却仍每
~7m47s 重算重印 74 笔反事实全表, 一晚 32 轮 (`本次新结算 0 笔` × 31)。
其挂载面是 scheduler_core.py 的 `_load_script_module("settle_overnight_shadow")` —— importlib
按**文件路径**加载, 无 `import` 语句 ⇒ 仓内 import / crontab / Hermes prompt 三面全查不到
(8/28 的 yaml 注释因此误判「无排班」)。这是本批新发现的**第六挂载面**。

本文件钉住四条不可退让的语义 —— 任何一条被"优化"掉都必须红:
  ① 默认 (键缺失 / false) → **零结算**: run_check 返回 0 且不触碰 _settle_all;
  ② 显式 true → 恢复原行为 (回滚 = yaml 一行 + 重启, 不留代码债);
  ③ 配置读取异常 → **fail-closed 跳过 + WARNING** (与另 10 个钩子的 fail-open 刻意相反:
     本门默认 false = "本就不该跑", fail-open 会让一次 ConfigContainer 抖动把已停作业复活;
     跳过零代价 —— 结算是全量幂等重算, 漏一夜下夜自动补齐);
  ④ 挂载面必须在 yaml 注释与 shadow_registry 双处登记 (哨兵可核, 防第六面再次失踪)。
"""
from pathlib import Path

import pytest
import yaml

import scripts.settle_overnight_shadow as sos

REPO = Path(__file__).resolve().parents[1]
GATE_KEY = "playbook.overnight_hold_settler_enabled"


class _StubContainer:
    """ConfigContainer 替身: 只暴露本门用到的 get_dotted。"""

    def __init__(self, value, raises=False):
        self._value = value
        self._raises = raises
        self.asked = []

    def get_dotted(self, path, default=None):
        self.asked.append((path, default))
        if self._raises:
            raise RuntimeError("ConfigContainer 抖动 (测试注入)")
        return self._value


@pytest.fixture
def gate(monkeypatch):
    """接管配置读取与结算实体, 返回探针 (settle 调用次数 + 容器)。"""
    from app.core.config_container import ConfigContainer

    calls = []
    box = {"value": False, "raises": False, "n": 0}

    def _stub():
        return _StubContainer(box["value"], box["raises"])

    monkeypatch.setattr(ConfigContainer, "get_instance", classmethod(lambda cls: _stub()))

    def fake_settle(dry_run=False):
        calls.append({"dry_run": dry_run})
        return box["n"]

    monkeypatch.setattr(sos, "_settle_all", fake_settle)
    return {"calls": calls, "box": box}


def test_default_off_does_not_settle(gate):
    """①默认 false: 模拟盘后窗 31 轮 ⇒ _settle_all 零调用 (原病灶 = 32 轮重算重印)。"""
    for _ in range(31):
        assert sos.run_check(None) == 0
    assert gate["calls"] == [], f"配置门失效: false 仍结算了 {len(gate['calls'])} 轮"


def test_key_absent_falls_back_to_off(gate, monkeypatch):
    """①键缺失 (default=False 生效) 同样必须停 — 防"删键即复活"。"""
    from app.core.config_container import ConfigContainer

    monkeypatch.setattr(
        ConfigContainer, "get_instance",
        classmethod(lambda cls: _StubContainer(None)))   # get_dotted 返回 default
    gate["box"]["value"] = None
    assert sos.run_check(None) == 0
    assert gate["calls"] == []


def test_enabled_true_restores_settlement(gate):
    """②回滚路径: true → 正常结算并原样返回笔数 (行为与改前一致)。"""
    gate["box"]["value"] = True
    gate["box"]["n"] = 3
    assert sos.run_check(None) == 3
    assert len(gate["calls"]) == 1, "true 时必须真结算一次"


def test_config_read_failure_fails_closed_with_warning(gate, caplog):
    """③配置读异常 → fail-closed 跳过, 且必须吼一声 (静默跳过 = 守卫只能报健康)。"""
    gate["box"]["raises"] = True
    assert sos.run_check(None) == 0
    assert gate["calls"] == [], "配置读不到就复活已停作业 = fail-open (本门禁止)"
    assert any(r.levelname == "WARNING" and "fail-closed" in r.message
               for r in caplog.records), "fail-closed 必须留 WARNING 证据"


def test_settle_exception_still_isolated(gate, monkeypatch, caplog):
    """门开了之后, 结算异常仍须被隔离 (不得冒泡打断 AFTER_HOURS 段其余 11 个钩子)。"""
    gate["box"]["value"] = True

    def boom(dry_run=False):
        raise RuntimeError("结算炸了 (测试注入)")

    monkeypatch.setattr(sos, "_settle_all", boom)
    assert sos.run_check(None) == 0
    assert any("结算异常" in r.message and r.exc_info for r in caplog.records), \
        "隔离可以, 但必须带栈留证 (②批 19 处补 exc_info 的同款纪律)"


def test_yaml_pins_key_false_with_mount_face_documented():
    """④yaml 侧: 键存在且为 false, 注释必须写清第六挂载面 (否则下次核对又会漏)。"""
    raw = (REPO / "config" / "unified_config.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert data["playbook"]["overnight_hold_settler_enabled"] is False, \
        "默认必须 false (减法三步第②步配置停); 改回 true 须先在 DEB 落档"
    assert "_load_script_module" in raw, \
        "yaml 注释必须登记 importlib 挂载面 — 这是 crontab/import 搜索查不到的第六面"


def test_registry_notes_register_both_mount_faces():
    """④注册表侧 (DEB §2.4 判据 4): overnight_hold 条目 notes 须登记两个挂载面。"""
    from app.playbook.shadow_registry import REGISTRY

    item = next(i for i in REGISTRY if i.id == "overnight_hold")
    for needle in ("settle_overnight_shadow", "overnight_hold_settler_enabled",
                   "_load_script_module"):
        assert needle in item.notes, f"注册表 notes 缺登记: {needle}"
    assert item.config_key == "playbook.overnight_hold_shadow_enabled", \
        "写入方开关键不得被改动 (结算器门是独立新键, 两者联动但不同名)"
