"""[DEB-long-expectancy 2026-08-17 DA覆盖事故] generate/topup 侧 DA 优先保护契约测试。

8/17 实锤: DA 17 条 gate-PASS 指令被 generate 的 64 条 SKIP 票 UPSERT 覆盖。
修复: _generate_da_priority_check 在 generate/topup 写入前查现存行 decision_id,
DA-/universe_expansion: 前缀受保护不覆盖; TFB-/SNAP-/空 不保护(幂等重跑不受影响)。
"""
import sys
import pytest
from unittest import mock

sys.path.insert(0, "/opt/ftseek/FTSeek-US")


class _FakeDB:
    def __init__(self, existing=None):
        self._existing = existing or {}

    def get_instruction(self, date, symbol):
        return self._existing.get((date, symbol))


from app.playbook.pipeline import _generate_da_priority_check


class TestGenerateDaPriorityCheck:
    def test_da_row_protected(self):
        """现存 DA- 前缀指令 → 返回 True(跳过不覆盖)"""
        db = _FakeDB({("2026-08-17", "US.SNDK"): {"decision_id": "DA-20260817-US.SNDK-5844"}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.SNDK") is True

    def test_ue_row_protected(self):
        """现存 universe_expansion: 前缀指令 → 返回 True"""
        db = _FakeDB({("2026-08-17", "US.NVDA"): {"decision_id": "universe_expansion:2026-08-17:NVDA"}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.NVDA") is True

    def test_tfb_row_not_protected(self):
        """现存 TFB- 前缀(generate自身技术兜底) → 返回 False(允许幂等重跑覆盖)"""
        db = _FakeDB({("2026-08-17", "US.ANET"): {"decision_id": "TFB-US.ANET-123"}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.ANET") is False

    def test_empty_decision_id_not_protected(self):
        """现存空 decision_id(SKIP票) → 返回 False(允许覆盖)"""
        db = _FakeDB({("2026-08-17", "US.MU"): {"decision_id": ""}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.MU") is False

    def test_none_row_not_protected(self):
        """无现存行 → 返回 False(首次写入放行)"""
        db = _FakeDB({})
        assert _generate_da_priority_check(db, "2026-08-17", "US.NEW") is False

    def test_decision_id_none_not_protected(self):
        """现存行 decision_id=None → 返回 False"""
        db = _FakeDB({("2026-08-17", "US.X"): {"decision_id": None}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.X") is False

    def test_db_error_degrades_open(self):
        """DB 查询异常 → 降级放行(不阻断生成, fail-open)"""
        class _BrokenDB:
            def get_instruction(self, date, symbol):
                raise RuntimeError("db locked")
        assert _generate_da_priority_check(_BrokenDB(), "2026-08-17", "US.Y") is False

    def test_da_prefix_not_substring(self):
        """前缀匹配是 startswith, 非包含 — 'XDA-' 不受保护"""
        db = _FakeDB({("2026-08-17", "US.Z"): {"decision_id": "XDA-123"}})
        assert _generate_da_priority_check(db, "2026-08-17", "US.Z") is False

    def test_structure_generate_loop_wired(self):
        """结构契约: generate 入库循环调用保护检查(防再次遗漏)"""
        import inspect
        from app.playbook import pipeline
        src = inspect.getsource(pipeline.run_playbook_generate)
        assert "_generate_da_priority_check" in src, "generate 入库循环必须调用 DA 优先检查"

    def test_structure_topup_wired(self):
        """结构契约: topup 补量路径同样接线"""
        import inspect
        from app.playbook import pipeline
        src = inspect.getsource(pipeline.run_playbook_topup_supplement)
        assert "_generate_da_priority_check" in src, "topup 路径必须调用 DA 优先检查"
