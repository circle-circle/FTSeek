#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settle_premarket_invalidation 契约钉 (2026-09-11, 断头传感器修复批).

钉住:
  1. 判据边界: n<20 → KEEP_SAMPLING; 净≥0 → KEEP_SAMPLING; 误杀>40% → KEEP_SAMPLING;
     滚动窗不同向 → KEEP_SAMPLING; 四关全过 → READY_TO_ADJUDICATE (不自动生效)
  5. 退役闸 (2026-09-11 DEB-20260911): A/C 恒 RETIRED_NO_PROMOTION, 无论判据数值
  6. 转正前冲突核对: 相关组件未互引或注册表/台账不可读 → READY 降级 KEEP_SAMPLING
     且 stderr 留痕
  2. 结算数学: ban_value = −Σnet; miskill = wins/executed; not_executed 不进价值口径
  3. 只读纪律: DB mode=ro; 不写影子 jsonl
  4. 坏行留痕不静默 (stderr), 好行照常结算
"""
import sqlite3
from pathlib import Path

import pytest

from scripts import settle_premarket_invalidation as spmi


class TestVerdict:
    def _a(self, executed=25, net=-100.0, wins=5, roll_executed=8, roll_net=-40.0, roll_wins=2):
        return {"flagged": executed + 5, "executed": executed, "net": net, "wins": wins,
                "not_executed": 5, "roll_flagged": roll_executed, "roll_executed": roll_executed,
                "roll_net": roll_net, "roll_wins": roll_wins}

    def test_all_gates_pass(self, monkeypatch):
        # n=25≥20, net<0, 误杀 20%≤40%, 滚动窗同向, 冲突核对过 → 提请裁决
        monkeypatch.setattr(spmi, "_conflict_check", lambda rule: (True, "ok"))
        assert spmi._verdict("B_long_knife", self._a()) == "READY_TO_ADJUDICATE"

    def test_n_insufficient_keeps_sampling(self):
        assert spmi._verdict("B_long_knife", self._a(executed=19)) == "KEEP_SAMPLING"

    def test_net_positive_keeps_sampling(self):
        # 被拦组实际净赚 = 禁入会丢钱 → 不提请
        assert spmi._verdict("B_long_knife", self._a(net=+50.0, wins=15)) == "KEEP_SAMPLING"

    def test_miskill_over_40pct_keeps_sampling(self):
        # C_long_chase 首读实测形态: 净负但误杀 68% → 不许提请 (预注册门)
        a = self._a(net=-152.0, wins=15, executed=22)
        assert spmi._verdict("B_long_knife", a) == "KEEP_SAMPLING"

    def test_roll_window_disagree_keeps_sampling(self):
        # 全窗负但滚动窗正 → 两窗不同向
        assert spmi._verdict("B_long_knife", self._a(roll_net=+30.0, roll_wins=5)) == "KEEP_SAMPLING"

    def test_roll_window_too_small_keeps_sampling(self):
        assert spmi._verdict("B_long_knife", self._a(roll_executed=4, roll_net=-20.0)) == "KEEP_SAMPLING"


class TestRetiredGate:
    """DEB-20260911: A/C 恒 RETIRED_NO_PROMOTION, 历史行仍进 _settle 结算呈报."""

    def _passing(self):
        return {"flagged": 30, "executed": 25, "net": -100.0, "wins": 5,
                "not_executed": 5, "roll_flagged": 8, "roll_executed": 8,
                "roll_net": -40.0, "roll_wins": 2}

    def test_retired_rules_never_promote(self):
        for r in ("A_short_chase", "C_long_chase"):
            assert spmi._verdict(r, self._passing()) == "RETIRED_NO_PROMOTION"

    def test_retired_gate_precedes_all_gates(self):
        # 即使样本为 0 也判 RETIRED (闸在 n 检查之前 — 读数语义先于判据语义)
        zero = {"flagged": 0, "executed": 0, "net": 0.0, "wins": 0,
                "not_executed": 0, "roll_flagged": 0, "roll_executed": 0,
                "roll_net": 0.0, "roll_wins": 0}
        assert spmi._verdict("A_short_chase", zero) == "RETIRED_NO_PROMOTION"

    def test_active_rule_unaffected(self, monkeypatch):
        monkeypatch.setattr(spmi, "_conflict_check", lambda rule: (True, "ok"))
        assert spmi._verdict("B_long_knife", self._passing()) == "READY_TO_ADJUDICATE"


class TestConflictCheck:
    """转正前冲突核对: 未互引 / 注册表不可读 → READY 降级 KEEP_SAMPLING + stderr."""

    def _passing(self):
        return {"flagged": 30, "executed": 25, "net": -100.0, "wins": 5,
                "not_executed": 5, "roll_flagged": 8, "roll_executed": 8,
                "roll_net": -40.0, "roll_wins": 2}

    def test_missing_crossref_downgrades(self, monkeypatch, capsys):
        monkeypatch.setitem(spmi._CONFLICT_REFS, "B_long_knife", ("__definitely_not_in_registry__",))
        assert spmi._verdict("B_long_knife", self._passing()) == "KEEP_SAMPLING"
        err = capsys.readouterr().err
        assert "conflict_check 降级" in err and "未互引" in err

    def test_registry_unreadable_downgrades(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(spmi, "ROOT", tmp_path)  # 空目录 → 注册表读不到
        assert spmi._verdict("B_long_knife", self._passing()) == "KEEP_SAMPLING"
        err = capsys.readouterr().err
        assert "conflict_check 降级" in err and "不可读" in err

    def test_real_corpus_has_b_refs(self):
        # 环境钉: 真实注册表/台账必须含 B 的互引锚, 否则 B 永远无法提请 (设计意图)
        ok, detail = spmi._conflict_check("B_long_knife")
        assert ok, detail


class TestSettleMath:
    def _mk_db(self, tmp_path):
        db = tmp_path / "t.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trade_records (decision_id TEXT, realized_pnl REAL, "
                     "pnl_net REAL, exit_time TEXT)")
        conn.execute("INSERT INTO trade_records VALUES ('D1', -100, -104, '2026-09-05T10:00')")
        conn.execute("INSERT INTO trade_records VALUES ('D2', 50, 46, '2026-09-05T11:00')")
        conn.commit()
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        return ro

    def test_ban_value_and_miskill(self, tmp_path):
        conn = self._mk_db(tmp_path)
        flags = [
            {"decision_id": "D1", "date": "2026-09-05", "would_invalidate": ["C_long_chase"]},
            {"decision_id": "D2", "date": "2026-09-05", "would_invalidate": ["C_long_chase"]},
            {"decision_id": "D9", "date": "2026-09-05", "would_invalidate": ["C_long_chase"]},
        ]
        out = spmi._settle(flags, conn, roll_since="2026-08-01")
        c = out["C_long_chase"]
        assert c["flagged"] == 3 and c["executed"] == 2 and c["not_executed"] == 1
        assert c["net"] == pytest.approx(-58.0)      # -104 + 46
        assert c["wins"] == 1                         # D2 是误杀赢家
        assert c["roll_executed"] == 2

    def test_multi_rule_row_counts_each(self, tmp_path):
        conn = self._mk_db(tmp_path)
        flags = [{"decision_id": "D1", "date": "2026-09-05",
                  "would_invalidate": ["A_short_chase", "C_long_chase"]}]
        out = spmi._settle(flags, conn, roll_since="2026-08-01")
        assert out["A_short_chase"]["executed"] == 1
        assert out["C_long_chase"]["executed"] == 1

    def test_unknown_rule_ignored(self, tmp_path):
        conn = self._mk_db(tmp_path)
        flags = [{"decision_id": "D1", "date": "2026-09-05", "would_invalidate": ["Z_new"]}]
        out = spmi._settle(flags, conn, roll_since="2026-08-01")
        assert all(out[k]["flagged"] == 0 for k in spmi.RULES)


class TestReadOnlyDiscipline:
    def test_db_is_opened_ro(self):
        src = Path(spmi.__file__).read_text()
        assert "mode=ro" in src

    def test_does_not_write_shadow_jsonl(self):
        src = Path(spmi.__file__).read_text()
        # 只允许读 SHADOW; 写只许 OUT (atomic)
        assert "SHADOW.write_text" not in src
        assert "open(SHADOW" not in src or "write" not in src.split("SHADOW")[1][:200]
