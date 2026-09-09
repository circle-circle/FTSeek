# -*- coding: utf-8 -*-
"""RFC-080 S2': playbook 半翻互锁单测.

钉锁 (docs/PROPOSAL-20260909-cutover-dual-arm-routing.md §三·五):
  半翻态 = target_env=real 但 (ALLOW_REAL_TRADING!=true 或 TRADE_ENV 路由仍指 sim 库)
  → 必须强制降级 sim (fail-close), 禁止真钱单进错误账本。
  完整前提 (ALLOW_REAL_TRADING=true + is_live_mode()=True) → 放行到毕业门禁 (既有链)。

单库翻转模型证据 (影响面分析实锤, S2 物理分库方案作废的根据):
  · 退出监控单 conn 扫两臂: pipeline.py WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
  · _close_trade_record(conn,...) 单 conn 原子写 trade_records+playbook_instructions+open_positions
  · broker 臂按行: _row_target = "real" if source_type=='PLAYBOOK_LIVE' else "sim"
"""
import pytest


def _run_interlock(monkeypatch, allow_real: str, db_live: bool):
    """复刻 playbook_service._execute_via_dual_broker_inner 的互锁段 (RFC-080 S2').

    直接调私有方法需构造 15+ 参数的 inst dict, 成本高且脆; 本测试钉**判定逻辑本身**
    (与实现同构), 另由 test_interlock_present_in_source 钉实现存在性防漂移。
    """
    import os
    target_env = "real"
    _is_live = (target_env == "real")
    monkeypatch.setenv("ALLOW_REAL_TRADING", allow_real)
    if _is_live:
        _allow = os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true"
        _db_is_live = db_live
        if not _allow or not _db_is_live:
            target_env = "sim"
            _is_live = False
    return target_env, _is_live


class TestHalfFlipInterlock:
    def test_halfflip_allow_false_db_sim_downgrades(self, monkeypatch):
        """半翻 A: broker 想 real 但 ALLOW_REAL_TRADING=false → 降级 sim."""
        assert _run_interlock(monkeypatch, "false", False) == ("sim", False)

    def test_halfflip_allow_true_db_sim_downgrades(self, monkeypatch):
        """半翻 B (最危险): ALLOW=true 但 TRADE_ENV=1 → 真钱单会记进 sim 研究库 → 降级."""
        assert _run_interlock(monkeypatch, "true", False) == ("sim", False)

    def test_halfflip_allow_false_db_live_downgrades(self, monkeypatch):
        """半翻 C: TRADE_ENV=0 但 ALLOW=false (di_container 已降级 AUTO, 此为纵深防御) → 降级."""
        assert _run_interlock(monkeypatch, "false", True) == ("sim", False)

    def test_full_flip_passes_through(self, monkeypatch):
        """完整前提: ALLOW=true + TRADE_ENV=0 → 放行 (交由既有毕业门禁, 双向审计放行侧)."""
        assert _run_interlock(monkeypatch, "true", True) == ("real", True)


class TestInterlockWiredInSource:
    """防漂移钉桩: 互锁必须真实存在于 _execute_via_dual_broker_inner 的 real 分支."""

    def test_interlock_present_in_source(self):
        import inspect
        import app.playbook.playbook_service as ps
        src = inspect.getsource(ps.PlaybookService._execute_via_dual_broker_inner)
        assert "半翻互锁" in src, "互锁段被删除/漂移"
        assert "ALLOW_REAL_TRADING" in src
        assert "is_live_mode" in src
        # 降级方向: 互锁命中后 target_env 必须回 sim
        idx = src.find("半翻互锁")
        assert 'target_env = "sim"' in src[idx:idx + 2000]

    def test_interlock_before_graduation_gate(self):
        """互锁必须在毕业门禁之前 (先锁账本一致性, 再谈毕业)."""
        import inspect
        import app.playbook.playbook_service as ps
        src = inspect.getsource(ps.PlaybookService._execute_via_dual_broker_inner)
        assert src.find("半翻互锁") < src.find("fail-closed guard")

    def test_os_import_fixed_no_bare_os_getenv(self):
        """回归钉桩: 本文件无模块级 import os — 互锁段必须用局部 import
        (曾有 os.getenv NameError 隐患, 2026-09-09 py_compile 不报但运行时炸)."""
        import inspect
        import app.playbook.playbook_service as ps
        src = inspect.getsource(ps.PlaybookService._execute_via_dual_broker_inner)
        idx = src.find("半翻互锁")
        seg = src[idx:idx + 2000]
        assert "import os as _os_il" in seg
        # 段内不得出现裸 os.getenv (NameError 源)
        assert "str(os.getenv" not in seg
