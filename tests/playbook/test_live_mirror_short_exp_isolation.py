"""RFC-218 §23 (§22.9 #5): NO_TRADE SHORT 实验指令 LIVE 镜像隔离契约。

钉子:
  - no_trade_short_exp 标记 (内存 dict/JSON str ∨ 持久化源 metadata) → 排除, 先于臂闸;
  - 内存 metadata 滞后 (sizing 期 merge 只写库) → 持久化读为权威;
  - SHORT ∧ reprice_audit.regime_reads.sizing.regime==no_trade → 排除 (次级证据);
  - SHORT ∧ 两源均不可读 (缺失/坏 blob/行缺失/读失败/无 id) → fail-closed 排除;
  - SHORT ∧ 行存在 metadata 为空 (一次成功读) → 放行到臂闸;
  - LONG/BUY 无标记 → 永不被本谓词排除 (实验仅 SHORT);
  - 隔离拒绝走 (None, gate) 合同, code=NO_TRADE_SHORT_EXP_ISOLATION, 臂闸/镜像零调用;
  - 谓词自身异常: SHORT fail-closed / LONG fail-open;
  - 默认持久化源 = SIM/research lane (route_playbook_db("sim"))。

当前 live 闸只收 LONG/BUY ⇒ 本隔离今日零流量零行为变化 (纵深防御, SHORT 实盘授权前必备)。
回滚: revert 本批 commit + 删本文件。
"""
import json

import pytest

from app.playbook.pipeline_parts import risk_gates as rg


class FakeSourceDB:
    def __init__(self, row=None, raise_err=False):
        self._row = row
        self._raise = raise_err
        self.calls = []

    def get_instruction_by_id(self, iid):
        self.calls.append(iid)
        if self._raise:
            raise RuntimeError("db down")
        return self._row


def _inst(direction="SELL_SHORT", metadata=None, inst_id=7):
    d = {"symbol": "US.TEST", "direction": direction}
    if inst_id is not None:
        d["id"] = inst_id
    if metadata is not None:
        d["metadata"] = metadata
    return d


def _row(metadata_json):
    return {"id": 7, "metadata_json": metadata_json}


class TestPredicate:
    def test_long_no_metadata_no_id_allowed(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst("BUY", inst_id=None))
        assert ok is False and reason == ""

    def test_short_marker_in_memory_dict_excluded(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(metadata={"no_trade_short_exp": {"vix": 30}}))
        assert ok is True and "in_memory" in reason

    def test_short_marker_in_memory_json_str_excluded(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(metadata=json.dumps({"no_trade_short_exp": {"vix": 30}})))
        assert ok is True and "in_memory" in reason

    def test_short_marker_persisted_only_excluded(self):
        # 内存滞后 (sizing merge 只写库): 持久化读为权威
        db = FakeSourceDB(_row(json.dumps({"no_trade_short_exp": {"vix": 30}})))
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(metadata={"other": 1}), source_db=db)
        assert ok is True and "persisted" in reason
        assert db.calls == [7]

    def test_short_persisted_null_metadata_row_exists_allowed(self):
        db = FakeSourceDB(_row(None))
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(), source_db=db)
        assert ok is False and reason == ""

    def test_short_no_id_no_metadata_fail_closed(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(inst_id=None))
        assert ok is True and "fail_closed" in reason

    def test_short_no_id_readable_memory_allowed(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(metadata={"other": 1}, inst_id=None))
        assert ok is False and reason == ""

    def test_short_corrupt_both_sources_fail_closed(self):
        db = FakeSourceDB(_row("{not json"))
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(metadata="{bad"), source_db=db)
        assert ok is True and "fail_closed" in reason

    def test_short_db_read_error_fail_closed(self):
        db = FakeSourceDB(raise_err=True)
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(), source_db=db)
        assert ok is True and "fail_closed" in reason

    def test_short_row_missing_fail_closed(self):
        db = FakeSourceDB(None)
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(), source_db=db)
        assert ok is True and "fail_closed" in reason

    def test_short_no_trade_sizing_regime_audit_excluded(self):
        md = {"reprice_audit": {"regime_reads": {"sizing": {"regime": "no_trade"}}}}
        db = FakeSourceDB(_row(json.dumps(md)))
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(), source_db=db)
        assert ok is True and "sizing_regime" in reason

    def test_short_risk_off_sizing_regime_allowed(self):
        md = {"reprice_audit": {"regime_reads": {"sizing": {"regime": "risk_off"}}}}
        db = FakeSourceDB(_row(json.dumps(md)))
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst(), source_db=db)
        assert ok is False and reason == ""

    def test_long_with_marker_still_excluded(self):
        # 标记=cohort SSOT, 方向异常也不镜像
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst("BUY", metadata={"no_trade_short_exp": {}}))
        assert ok is True

    def test_short_direction_alias_short(self):
        ok, reason = rg._is_no_trade_short_experiment_instruction(
            _inst("SHORT", inst_id=None))
        assert ok is True and "fail_closed" in reason

    def test_default_source_routes_sim(self, monkeypatch):
        import app.playbook.db as pdb
        seen = []

        def _route(target):
            seen.append(target)
            return FakeSourceDB(_row(None))

        monkeypatch.setattr(pdb, "route_playbook_db", _route)
        ok, _ = rg._is_no_trade_short_experiment_instruction(_inst())
        assert ok is False
        assert seen == ["sim"]


class TestMirrorIntegration:
    def _spies(self):
        calls = {"gate": 0, "mirror": 0}

        def gate(live_db, inst):
            calls["gate"] += 1
            return {"allowed": True, "reasons": []}

        def mirror(inst, live_db):
            calls["mirror"] += 1
            return 42

        return calls, gate, mirror

    def test_experiment_excluded_before_gate(self):
        calls, gate, mirror = self._spies()
        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst(metadata={"no_trade_short_exp": {"vix": 30}}), object(),
            mirror=mirror, gate_decision=gate)
        assert mid is None
        assert dec["allowed"] is False
        assert dec["reasons"][0]["code"] == "NO_TRADE_SHORT_EXP_ISOLATION"
        assert calls == {"gate": 0, "mirror": 0}

    def test_clean_short_passes_to_gate_and_mirrors(self):
        calls, gate, mirror = self._spies()
        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst(metadata={"other": 1}, inst_id=None), object(),
            mirror=mirror, gate_decision=gate)
        assert mid == 42 and dec["allowed"] is True
        assert calls == {"gate": 1, "mirror": 1}

    def test_gate_rejection_contract_unchanged(self):
        calls, _, mirror = self._spies()

        def gate(live_db, inst):
            calls["gate"] += 1
            return {"allowed": False,
                    "reasons": [{"code": "LIVE_PROBE_DIRECTION_REJECTED", "message": "x"}]}

        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst(metadata={"other": 1}, inst_id=None), object(),
            mirror=mirror, gate_decision=gate)
        assert mid is None
        assert dec["reasons"][0]["code"] == "LIVE_PROBE_DIRECTION_REJECTED"
        assert calls["mirror"] == 0

    def test_predicate_error_short_fail_closed(self):
        calls, gate, mirror = self._spies()

        def boom(inst):
            raise RuntimeError("predicate bug")

        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst(), object(), mirror=mirror, gate_decision=gate,
            experiment_check=boom)
        assert mid is None and dec["allowed"] is False
        assert dec["reasons"][0]["code"] == "NO_TRADE_SHORT_EXP_ISOLATION"
        assert calls == {"gate": 0, "mirror": 0}

    def test_predicate_error_long_fail_open(self):
        calls, gate, mirror = self._spies()

        def boom(inst):
            raise RuntimeError("predicate bug")

        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst("BUY"), object(), mirror=mirror, gate_decision=gate,
            experiment_check=boom)
        assert mid == 42 and dec["allowed"] is True
        assert calls == {"gate": 1, "mirror": 1}

    def test_injected_experiment_check_seam(self):
        calls, gate, mirror = self._spies()
        mid, dec = rg._mirror_instruction_to_live_if_allowed(
            _inst("BUY"), object(), mirror=mirror, gate_decision=gate,
            experiment_check=lambda inst: (True, "injected"))
        assert mid is None and dec["allowed"] is False
        assert calls == {"gate": 0, "mirror": 0}


class TestPipelineForwarder:
    def test_forwarder_keeps_seam_and_isolation(self, monkeypatch):
        from app.playbook import pipeline as pl
        seen = {"gate": 0, "mirror": 0}
        monkeypatch.setattr(
            pl, "_live_mirror_gate_decision",
            lambda live_db, inst: seen.__setitem__("gate", seen["gate"] + 1)
            or {"allowed": True, "reasons": []})
        monkeypatch.setattr(
            pl, "_mirror_instruction_to_live",
            lambda inst, live_db: seen.__setitem__("mirror", seen["mirror"] + 1) or 9)
        mid, dec = pl._mirror_instruction_to_live_if_allowed(
            _inst(metadata={"no_trade_short_exp": {}}), object())
        assert mid is None and dec["allowed"] is False
        assert seen == {"gate": 0, "mirror": 0}
