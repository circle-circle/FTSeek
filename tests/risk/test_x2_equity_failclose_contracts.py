"""X2 契约测试 (2026-08-23, DEB-20260823-system-health-check).

背景: 三处账户级熔断 guard 原均为 `if _equity > 0:` 无 else —— capital_safety 的
current_equity 为 0/过期(启动首查前/行情断供窗口)时熔断被静默跳过.
本文件锁住补丁后的 fail-close 语义: **权益不可用 = 拒绝新仓**, 防回归时被"优化"掉.

覆盖:
  X2-a playbook_service.execute_instruction: equity=0 → 拒单 (行为测试, LIVE 管线)
  X2-b/c order_executor + orb.engine: 各自熔断块含 else fail-close 分支 (源码契约,
        两处内联在巨型函数中, 行为测试成本不成比例; playbook 路径已行为级锁定,
        同款语义源码锚点防删).
"""
import pathlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def svc(db):
    return PlaybookService(playbook_db=db)


def _save_inst(db, **kwargs):
    payload = {
        "date": "2026-08-23",
        "symbol": "US.NVDA",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 97.5,
        "take_profit": 105.0,
        "position_size": 50,
        "confidence": 70,
        "conditions": ["above_ma20"],
    }
    payload.update(kwargs)
    return db.save_instruction(payload)


class TestX2EquityUnavailableFailClose:
    def test_playbook_execute_rejects_when_equity_zero(self, svc, db):
        """equity=0(启动首查前/断供) → 必须拒单, 不得静默跳过熔断."""
        iid = _save_inst(db)
        with patch(
            "app.risk.capital_safety.get_capital_safety_system",
            return_value=MagicMock(_state=MagicMock(current_equity=0.0)),
        ):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "权益不可用" in res["message"]

    def test_playbook_execute_still_checks_breaker_when_equity_positive(self, svc, db):
        """对照: equity>0 时 HALT 判定照常拒单 (确认未把正常门改坏)."""
        iid = _save_inst(db)
        with patch(
            "app.risk.capital_safety.get_capital_safety_system",
            return_value=MagicMock(_state=MagicMock(current_equity=97000.0)),
        ), patch(
            "app.risk.account_circuit_breaker.check_circuit_breaker",
            return_value=MagicMock(action="HALT", reason="当日亏损≥2%"),
        ):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "OKR熔断" in res["message"]


class TestX2SourceContract:
    """order_executor 一处为巨型函数内联块, 用源码锚点锁语义.

    [2026-09-20 ORB 退役] 原 `_SITES` 含 `app/orb/engine.py`; 用户裁「Playbook 成为唯一
    自动交易下单管线, 其他管线都是历史」⇒ `app/orb/`(2735 行) 整体退役, 该站点已不存在,
    故从 _SITES 移除 (X2 语义本身不变: 剩下站点的 fail-close 分支仍被本测试钉住)。
    """

    _SITES = (
        "app/pipeline/order_executor.py",
    )

    def test_all_sites_have_failclose_branch(self):
        repo = pathlib.Path(__file__).resolve().parents[2]
        for rel in self._SITES:
            src = (repo / rel).read_text(encoding="utf-8")
            assert "DEB-20260823 X2" in src, f"{rel} 缺 X2 else 分支注释"
            assert "账户权益不可用" in src, f"{rel} 缺 equity≤0 拒单日志"

    def test_no_bare_equity_guard_without_else_remains(self):
        """全仓不得再有 `if _equity > 0:` 无 else 的熔断守卫 (X2 后应仅剩带分支的三处)."""
        repo = pathlib.Path(__file__).resolve().parents[2]
        hits = []
        for rel in ("app/pipeline/order_executor.py", "app/playbook/playbook_service.py"):
            for i, line in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1):
                if "if _equity > 0:" in line:
                    hits.append(f"{rel}:{i}")
        assert len(hits) == 2, f"equity 守卫站点数量异常: {hits}"
