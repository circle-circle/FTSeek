# -*- coding: utf-8 -*-
"""RFC-080 S1: 毕业门禁研究库 pin 单测.

钉锁 (缺口1, docs/PROPOSAL-20260909-cutover-dual-arm-routing.md):
  1. db_pool "ftseek_research" 恒映射 data/ftseek_us.db — 与 TRADE_ENV 全解耦
  2. 门禁两读点 (_check_graduation_gate / _has_consecutive_losses) 用 pin, 不用默认池名
  3. pin 路径 = sim 研究证据物理位置 (playbook_instructions 1918 行)
"""
from pathlib import Path

import pytest


class TestResearchPin:
    def test_pool_pin_is_hard_path(self):
        """ftseek_research 恒 ftseek_us.db, 不随 TRADE_ENV/_ftseek_us_db 漂."""
        from app.storage.db_pool import _DB_PATHS, PROJECT_ROOT
        assert _DB_PATHS["ftseek_research"] == PROJECT_ROOT / "data" / "ftseek_us.db"

    def test_pin_differs_from_live_db(self):
        from app.storage.db_pool import _DB_PATHS
        assert _DB_PATHS["ftseek_research"].name == "ftseek_us.db"
        assert _DB_PATHS["ftseek_research"] != _DB_PATHS["ftseek_research"].parent / "ftseek_us_live.db"

    def test_pin_survives_trade_env_flip(self, monkeypatch):
        """模拟 cutover 日 TRADE_ENV=0: 默认池名漂到 live, pin 不动."""
        import importlib
        import app.storage.db_pool as dp
        monkeypatch.setenv("TRADE_ENV", "0")
        dp2 = importlib.reload(dp)
        try:
            assert dp2._DB_PATHS["ftseek_us"].name == "ftseek_us_live.db"   # 默认名漂了
            assert dp2._DB_PATHS["ftseek_research"].name == "ftseek_us.db"  # pin 不漂
        finally:
            monkeypatch.setenv("TRADE_ENV", "1")
            importlib.reload(dp)

    def test_research_db_has_graduation_evidence(self):
        """pin 指向的库确有门禁证据 (活体数据: 样本量 >> live 库 16 行)."""
        import sqlite3
        from app.storage.db_pool import _DB_PATHS
        conn = sqlite3.connect(f"file:{_DB_PATHS['ftseek_research']}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
        finally:
            conn.close()
        assert n > 500, f"研究库样本 {n} — 门禁证据应在研究臂 (实证 1918 行)"


class TestGateUsesPin:
    def test_graduation_gate_reads_research_pool(self, monkeypatch):
        """_check_graduation_gate 的 PlaybookDB 必须用 db_name='ftseek_research'."""
        import app.playbook.pipeline as pl
        captured = {}

        class FakeDB:
            def __init__(self, db_name="ftseek_us", conn=None):
                captured["db_name"] = db_name

        import app.playbook.db as pdb
        monkeypatch.setattr(pdb, "PlaybookDB", FakeDB)
        monkeypatch.setattr(pl, "compute_playbook_ic", lambda db, days: {}, raising=False)
        # ic_tracker import inside function — patch at source module
        import app.playbook.ic_tracker as ic
        monkeypatch.setattr(ic, "compute_playbook_ic", lambda db, days: {"overall_ic_t3": 0.1, "sample_t3": 50})
        monkeypatch.setattr(ic, "_check_graduation", lambda rep: {"graduated": True, "checks": {"ic": True}, "ic_t3": 0.1, "sample_count": 50})
        monkeypatch.setattr(pl, "_has_consecutive_losses", lambda n: False)
        res = pl._check_graduation_gate()
        assert captured.get("db_name") == "ftseek_research", f"门禁未用研究库 pin: {captured}"
        assert res.get("graduated") is True  # 放行方向也要验 (双向审计)

    def test_consecutive_losses_reads_research_pool(self, monkeypatch):
        import app.playbook.pipeline as pl
        captured = {}

        class FakeDB:
            def __init__(self, db_name="ftseek_us", conn=None):
                captured["db_name"] = db_name

            def get_recent_instructions(self, limit=200):
                return []

        import app.playbook.db as pdb
        monkeypatch.setattr(pdb, "PlaybookDB", FakeDB)
        assert pl._has_consecutive_losses(5) is False
        assert captured.get("db_name") == "ftseek_research"
