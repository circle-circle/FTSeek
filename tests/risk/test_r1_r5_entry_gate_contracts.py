"""R-1~R-5 契约测试 (2026-08-23, DEB-20260823).

背景: 全链路复盘发现 Playbook 下单路径 (唯一 LIVE 管线) 缺失多道本应存在的硬门,
且 `grep -rln "_submit_order" tests/` = 0 —— 1445 个通过的测试对真实下单路径零覆盖。
本文件锁住补丁后的 fail-close 语义, 防止回归时被"优化"掉。

覆盖:
  R-1a  VIX 数据不可用 → 拒单 (fail-close)
  R-1b  VIX 超阈值     → 拒单
  R-1c  VIX 取数抛异常 → 拒单 (fail-close)
  R-1d  per-symbol 互斥: 同标的并发下单只放行一笔
  R-2   孤儿仓默认不自动伪造平仓 (开关默认 false)
  R-3   is_live_mode 进程内固化 + DB/broker 路由一致性断言
  R-5a  capital_safety RED/BLACKOUT → 拒单
  R-5b  capital_safety 查询抛异常   → 拒单 (fail-close)
"""
import sqlite3
import threading
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


def _passthrough_upstream():
    """放行 R-5 之前的既有门 (账户熔断 / 绝对熔断), 只留待测门."""
    return [
        patch("app.risk.account_circuit_breaker.check_circuit_breaker",
              return_value=MagicMock(action="OK", reason="")),
        patch("app.risk.absolute_circuit_breaker.get_absolute_circuit_breaker",
              return_value=MagicMock(can_open_new_position=lambda _s: (True, ""))),
    ]


def _capital_safety_ok():
    return patch("app.risk.capital_safety.get_capital_safety_system",
                 return_value=MagicMock(
                     can_open_position=lambda: (True, ""),
                     _state=MagicMock(current_equity=97000.0)))


class TestR1VixGate:
    """R-1: Playbook 路径此前零 VIX 拦截 (dual_broker 不经 order_executor)."""

    def test_vix_unavailable_rejects(self, svc, db):
        """VIX 返回 None → 必须拒单. 宁可错过, 不可放错."""
        iid = _save_inst(db)
        with _passthrough_upstream()[0], _passthrough_upstream()[1], _capital_safety_ok(), \
             patch("app.analysis.market_state.get_checker",
                   return_value=MagicMock(get_vix_cached=lambda: None)):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "VIX" in res["message"]

    def test_vix_fetch_exception_rejects(self, svc, db):
        """VIX 取数抛异常 → fail-close 拒单, 不得静默放行."""
        iid = _save_inst(db)
        with _passthrough_upstream()[0], _passthrough_upstream()[1], _capital_safety_ok(), \
             patch("app.analysis.market_state.get_checker",
                   side_effect=RuntimeError("FRED down")):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "VIX" in res["message"]

    def test_vix_above_threshold_rejects(self, svc, db):
        """VIXGuard 判定 blocked → 拒单."""
        iid = _save_inst(db)
        with _passthrough_upstream()[0], _passthrough_upstream()[1], _capital_safety_ok(), \
             patch("app.analysis.market_state.get_checker",
                   return_value=MagicMock(get_vix_cached=lambda: 35.0)), \
             patch("app.risk.vix_guard.VIXGuard") as _vg:
            _vg.return_value.should_block.return_value = (True, "VIX 35.0 > 28")
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "VIX" in res["message"]

    def test_vix_gate_can_be_disabled(self, svc, db):
        """开关关闭时 VIX 门不生效 (保证可回滚)."""
        iid = _save_inst(db)

        def _cfg(key, default=None):
            if key == "risk.playbook_vix_gate_enabled":
                return False
            return default

        with _passthrough_upstream()[0], _passthrough_upstream()[1], _capital_safety_ok(), \
             patch("app.core.config_container.get_config",
                   return_value=MagicMock(get_dotted=_cfg)), \
             patch("app.analysis.market_state.get_checker",
                   return_value=MagicMock(get_vix_cached=lambda: None)), \
             patch.object(PlaybookService, "_execute_via_dual_broker",
                          return_value={"success": True, "message": "ok"}):
            res = svc.execute_instruction(iid)
        # VIX 为 None 也没被 VIX 门拦下 → 说明开关生效
        assert "VIX" not in res.get("message", "")


class TestR1SymbolLock:
    """R-1: per-symbol 互斥, 修 duplicate_position_guard 的 TOCTOU 双开."""

    def test_same_symbol_concurrent_only_one_passes(self, svc):
        entered = []
        release = threading.Event()

        def _slow_inner(*a, **kw):
            entered.append(1)
            release.wait(timeout=5)
            return {"success": True, "message": "filled"}

        args = ({}, 1, "US.NVDA", "BUY", 100.0, 97.5, 105.0, 10, "BUY", "", "sim")
        with patch.object(PlaybookService, "_execute_via_dual_broker_inner", _slow_inner):
            t = threading.Thread(target=lambda: svc._execute_via_dual_broker(*args))
            t.start()
            while not entered:
                pass
            # 持锁期间第二笔同标的必须被拒
            second = svc._execute_via_dual_broker(*args)
            release.set()
            t.join(timeout=5)

        assert second["success"] is False
        assert "SymbolLock" in second["message"]

    def test_different_symbols_not_blocked(self, svc):
        args_a = ({}, 1, "US.NVDA", "BUY", 100.0, 97.5, 105.0, 10, "BUY", "", "sim")
        args_b = ({}, 2, "US.AMD", "BUY", 100.0, 97.5, 105.0, 10, "BUY", "", "sim")
        with patch.object(PlaybookService, "_execute_via_dual_broker_inner",
                          return_value={"success": True, "message": "ok"}):
            assert svc._execute_via_dual_broker(*args_a)["success"] is True
            assert svc._execute_via_dual_broker(*args_b)["success"] is True

    def test_lock_released_on_exception(self, svc):
        """inner 抛异常也必须释放锁, 否则该标的永久无法下单."""
        args = ({}, 1, "US.TSLA", "BUY", 100.0, 97.5, 105.0, 10, "BUY", "", "sim")
        with patch.object(PlaybookService, "_execute_via_dual_broker_inner",
                          side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                svc._execute_via_dual_broker(*args)
        assert svc._get_symbol_order_lock("US.TSLA").acquire(blocking=False)
        svc._get_symbol_order_lock("US.TSLA").release()


class TestR5CapitalSafetyGate:
    """R-5: can_open_position() 此前从未在 Playbook 路径调用."""

    @pytest.mark.parametrize("reason", [
        "危险水位: 禁止开新仓, 请减仓",
        "熔断: 全部平仓, 禁止一切交易",
        "fail-safe: 资本状态 DB 保存失败，禁止新开仓直到下次成功更新",
    ])
    def test_blocked_water_level_rejects(self, svc, db, reason):
        iid = _save_inst(db)
        with _passthrough_upstream()[0], _passthrough_upstream()[1], \
             patch("app.risk.capital_safety.get_capital_safety_system",
                   return_value=MagicMock(
                       can_open_position=lambda: (False, reason),
                       _state=MagicMock(current_equity=97000.0))):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "资本安全水位" in res["message"]

    def test_capital_safety_exception_rejects(self, svc, db):
        """水位查不到 = 不知道自己在哪个回撤档 → fail-close.

        门序说明: 上游 OKR 账户熔断门 (playbook_service.py:721) 也调
        get_capital_safety_system() 取 equity, 故它会先 fail-close 拦下, message 为
        "OKR熔断检查异常"。本契约锁的是"取不到资本状态必拒单", 不锁由哪道门拒 ——
        两道门任一失效, 另一道仍兜住。
        """
        iid = _save_inst(db)
        with _passthrough_upstream()[0], _passthrough_upstream()[1], \
             patch("app.risk.capital_safety.get_capital_safety_system",
                   side_effect=RuntimeError("state db locked")):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "fail-close" in res["message"]

    def test_capital_safety_gate_is_reached_when_upstream_ok(self, svc, db):
        """上游熔断门拿到 equity 正常放行后, R-5 水位门必须仍能独立拦下."""
        iid = _save_inst(db)
        _cs = MagicMock(_state=MagicMock(current_equity=97000.0))
        _cs.can_open_position.side_effect = RuntimeError("water level unreadable")
        with _passthrough_upstream()[0], _passthrough_upstream()[1], \
             patch("app.risk.capital_safety.get_capital_safety_system", return_value=_cs):
            res = svc.execute_instruction(iid)
        assert res["success"] is False
        assert "资本安全水位" in res["message"]


class TestR2OrphanAutocloseDefaultOff:
    """R-2: 默认不再自动伪造 exit_price=entry_price / pnl=0 的平仓记录."""

    def test_default_is_off(self):
        from app.core.config_container import get_config
        assert get_config().get_dotted("reconcile.orphan_autoclose_enabled", False) is False

    def test_yaml_key_present_and_false(self):
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert cfg["reconcile"]["orphan_autoclose_enabled"] is False


class TestR3EnvRoutingPinned:
    """R-3: is_live_mode 进程内固化, 防运行期改 .env.us 静默切库."""

    def test_is_live_mode_cached(self):
        import app.core.db_utils as du
        du._LIVE_MODE_CACHE = None
        with patch.object(du, "_parse_trade_env_from_file", return_value=False) as _p:
            assert du.is_live_mode() is False
            assert du.is_live_mode() is False
            assert du.is_live_mode() is False
        # 固化后只解析一次
        assert _p.call_count == 1

    def test_runtime_flip_ignored_without_escape_hatch(self):
        """固化后即使文件改成 live, 也不切库."""
        import app.core.db_utils as du
        du._LIVE_MODE_CACHE = None
        with patch.object(du, "_parse_trade_env_from_file", return_value=False):
            du.is_live_mode()
        with patch.object(du, "_parse_trade_env_from_file", return_value=True):
            assert du.is_live_mode() is False
            assert du.get_main_db().name == "ftseek_us.db"

    def test_mismatch_logs_critical(self, caplog):
        """TRADE_ENV→live 库 但 AUTO_TRADE_ACCOUNT=sim → 必须 CRITICAL 告警."""
        import logging
        import app.core.db_utils as du
        with patch.dict("os.environ", {"AUTO_TRADE_ACCOUNT": "sim"}):
            with caplog.at_level(logging.CRITICAL):
                du._assert_env_routing_consistent(True)
        assert any("ENV_ROUTING_MISMATCH" in r.message for r in caplog.records)

    def test_consistent_routing_no_alarm(self, caplog):
        import logging
        import app.core.db_utils as du
        with patch.dict("os.environ", {"AUTO_TRADE_ACCOUNT": "sim"}):
            with caplog.at_level(logging.CRITICAL):
                du._assert_env_routing_consistent(False)
        assert not any("ENV_ROUTING_MISMATCH" in r.message for r in caplog.records)


class TestR4ExitConfigInvalidation:
    """R-4: playbook_horizon.yaml 变更时单例必须失效 (否则 exit_config_version 冻结)."""

    def test_reset_rebuilds_singleton(self):
        import app.playbook.config as pc
        c1 = pc.get_playbook_config()
        assert pc.get_playbook_config() is c1
        pc.reset_playbook_config()
        assert pc.get_playbook_config() is not c1

    def test_mtime_change_triggers_reload(self):
        import app.playbook.config as pc
        pc.reset_playbook_config()
        c1 = pc.get_playbook_config()
        # 伪造 mtime 变化 + 绕过节流
        pc._horizon_mtime_checked_at = 0.0
        with patch.object(pc, "_current_horizon_mtime",
                          return_value=(pc._horizon_mtime or 0) + 999):
            c2 = pc.get_playbook_config()
        assert c2 is not c1

    def test_exit_config_version_is_stable_within_version(self):
        import app.playbook.config as pc
        pc.reset_playbook_config()
        c = pc.get_playbook_config()
        assert c.get_exit_config_version() == c.get_exit_config_version()
        assert c.get_exit_config_version().startswith("exit-")
