"""RFC-127 stage-0 contract: ETH-held exit → RTH escalation shadow (record-only)."""

import importlib.util
import json
import logging
from pathlib import Path

import pytest

import app.playbook.pipeline as pipeline
import app.playbook.eth_held_exit_shadow as _shadow_mod
from app.playbook.eth_held_exit_shadow import (
    append_event,
    build_eth_held_rth_event,
    emit_eth_held_exit_shadow,
)

# 守卫真身必须在 autouse fixture 替换它之前抓住引用 (TestPositionGuard 要还原)
_REAL_POSITION_STILL_OPEN = _shadow_mod._position_still_open

ROOT = Path(__file__).resolve().parents[1]
_PIPE_SRC = (ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")


def _load_observer():
    spec = importlib.util.spec_from_file_location(
        "observe_nightly_supply_rfc127", ROOT / "scripts" / "observe_nightly_supply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent(**overrides):
    payload = {
        "intent_id": "7b149410fb274548b209659b822b9ba7",
        "symbol": "US.ACN",
        "direction": "SHORT",
        "target_env": "sim",
        "session": "ETH",
        "quantity": 23.0,
        "created_at": 1790861119.21669,
    }
    payload.update(overrides)
    return payload


def _result(**overrides):
    payload = {
        "success": False,
        "exit_state": "HELD",
        "error": "exit_eth_partial_fill_held",
        "dealt_qty": 0.0,
        "futu_order_id": "8219159",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _hermetic_position_guard(monkeypatch):
    """钉住幽灵样本守卫: 既有用例不得真查生产 open_positions。

    守卫默认查 `data/ftseek_us.db` 的 `US.ACN` 行 — 该仓一旦平掉, 未注入
    `position_open` 的用例会因"本地无持仓"而静默变红 (假回归)。默认注入 True
    (= 持仓仍在, 与用例原意一致); 守卫本身的判定另由 TestPositionGuard 覆盖。
    """
    monkeypatch.setattr(
        "app.playbook.eth_held_exit_shadow._position_still_open",
        lambda symbol, direction: True)


class TestEligibility:
    def test_eth_held_during_rth_builds_event(self):
        event = build_eth_held_rth_event(_intent(), _result(), rth_open=True)
        assert event is not None
        assert event["symbol"] == "US.ACN"
        assert event["remaining_qty"] == 23.0
        assert event["rth_open"] is True
        assert event["proposed_resolution"] == "escalate_to_rth_market_leg_for_remaining_qty"
        assert event["policy"].startswith("record-only")
        assert event["event_id"].startswith("ETH-HELD-RTH:")

    def test_other_error_is_not_eligible(self):
        assert build_eth_held_rth_event(
            _intent(), _result(error="exit_owner_conflict"), rth_open=True) is None

    def test_non_eth_session_is_not_eligible(self):
        assert build_eth_held_rth_event(
            _intent(session="RTH"), _result(), rth_open=True) is None

    def test_closed_market_is_not_eligible(self):
        assert build_eth_held_rth_event(_intent(), _result(), rth_open=False) is None

    def test_zero_remaining_is_not_eligible(self):
        assert build_eth_held_rth_event(
            _intent(quantity=23.0), _result(dealt_qty=23.0), rth_open=True) is None


class TestPositionGuard:
    """幽灵样本守卫 (10/1 US.ACN: external_close 平掉仓但不释放 intent)."""

    @pytest.fixture(autouse=True)
    def _restore_real_guard(self, monkeypatch):
        """本类要测守卫本体 ⇒ 还原被模块级 autouse fixture 替换掉的真函数。"""
        monkeypatch.setattr(_shadow_mod, "_position_still_open",
                            _REAL_POSITION_STILL_OPEN)

    def test_no_local_position_suppresses_ghost_event(self, caplog):
        """本地无持仓 ⇒ 不落样本 (否则 RFC-127 判据2 被幽灵顶成假 PASS)。"""
        with caplog.at_level(logging.WARNING,
                             logger="ftseek.playbook.eth_held_exit_shadow"):
            event = build_eth_held_rth_event(
                _intent(), _result(), rth_open=True, position_open=False)
        assert event is None
        assert "幽灵样本守卫" in caplog.text

    def test_position_open_keeps_event_and_marks_guard(self):
        event = build_eth_held_rth_event(
            _intent(), _result(), rth_open=True, position_open=True)
        assert event is not None and event["remaining_qty"] == 23.0
        assert event["position_open_guard"] == "injected"

    def test_query_failure_fails_open(self, caplog):
        """守卫自身故障 (None) ⇒ 照常落样本, 绝不因守卫丢真样本。"""
        with caplog.at_level(logging.WARNING,
                             logger="ftseek.playbook.eth_held_exit_shadow"):
            event = build_eth_held_rth_event(
                _intent(), _result(), rth_open=True, position_open=None)
        assert event is not None
        assert "幽灵样本守卫" not in caplog.text

    def test_default_path_queries_ro_db(self, tmp_path, monkeypatch):
        """默认路径真查 open_positions (只读): 有仓 True / 无仓 False / 方向不符 False。"""
        import sqlite3

        import app.playbook.eth_held_exit_shadow as shadow

        db = tmp_path / "ftseek_us.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL)")
        conn.execute("INSERT INTO open_positions VALUES ('US.ACN','SHORT',23.0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(shadow, "_FTSEEK_DB_PATH", db)

        assert shadow._position_still_open("US.ACN", "SHORT") is True
        assert shadow._position_still_open("US.ACN", "LONG") is False   # 方向不符
        assert shadow._position_still_open("US.MU", "LONG") is False    # 无此仓

        # 仓位被 external_close DELETE 后 ⇒ False (幽灵样本的真实成因)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM open_positions WHERE symbol='US.ACN'")
        conn.commit()
        conn.close()
        assert shadow._position_still_open("US.ACN", "SHORT") is False

    def test_missing_db_returns_none_not_false(self, tmp_path, monkeypatch):
        """DB 不可达 ⇒ None (fail-open), 不得退化成 False 而吞样本。"""
        import app.playbook.eth_held_exit_shadow as shadow

        monkeypatch.setattr(shadow, "_FTSEEK_DB_PATH", tmp_path / "nope.db")
        assert shadow._position_still_open("US.ACN", "SHORT") is None

    def test_zero_size_position_is_not_open(self, tmp_path, monkeypatch):
        import sqlite3

        import app.playbook.eth_held_exit_shadow as shadow

        db = tmp_path / "ftseek_us.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL)")
        conn.execute("INSERT INTO open_positions VALUES ('US.ACN','SHORT',0.0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(shadow, "_FTSEEK_DB_PATH", db)
        assert shadow._position_still_open("US.ACN", "SHORT") is False

    @pytest.mark.parametrize("raw,canon", [
        ("SHORT", "SHORT"), ("SELL_SHORT", "SHORT"), ("sell_short", "SHORT"),
        ("SELL", "SHORT"), ("LONG", "LONG"), ("BUY", "LONG"),
        ("", ""), (None, ""), ("WEIRD", ""),
    ])
    def test_direction_aliases_normalize(self, raw, canon):
        """枚举归一: 防 SHORT/SELL_SHORT 值域漂移把'有仓'误判成'无仓'而丢真样本。"""
        from app.playbook.eth_held_exit_shadow import _canon_direction

        assert _canon_direction(raw) == canon

    def test_guard_reads_direction_aliases_from_db(self, tmp_path, monkeypatch):
        """intent 写 SELL_SHORT / open_positions 写 SHORT ⇒ 必须判为同一方向。"""
        import sqlite3

        import app.playbook.eth_held_exit_shadow as shadow

        db = tmp_path / "ftseek_us.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL)")
        conn.execute("INSERT INTO open_positions VALUES ('US.ACN','SHORT',23.0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(shadow, "_FTSEEK_DB_PATH", db)
        assert shadow._position_still_open("US.ACN", "SELL_SHORT") is True


class TestAppendAndEmit:
    def test_append_is_idempotent_per_intent(self, tmp_path):
        path = tmp_path / "eth_held_exit_escalation.jsonl"
        event = build_eth_held_rth_event(_intent(), _result(), rth_open=True)
        assert append_event(event, path) is True
        assert append_event(event, path) is False
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["event_id"] == event["event_id"]

    def test_emit_failure_never_raises_and_warns_with_stack(
            self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(
            "app.playbook.eth_held_exit_shadow._ETH_HELD_EXIT_SHADOW_PATH",
            tmp_path / "boom.jsonl")
        # 该用例不注入 rth_open ⇒ build 会真查生产时钟, 盘后 (RTH 未开) 时
        # 判定返回 None 而 append_event 永不被调用 ⇒ "盘中绿 / 盘后红" 的脆弱测试。
        # 钉住时钟判定, 让它与用例语义 (测 append 抛错时的兜底) 解耦。
        monkeypatch.setattr(
            "app.playbook.eth_held_exit_shadow._regular_market_open", lambda: True)
        monkeypatch.setattr(
            "app.playbook.eth_held_exit_shadow._position_still_open",
            lambda symbol, direction: True)

        def boom(event, path):
            raise OSError("disk full")

        monkeypatch.setattr(
            "app.playbook.eth_held_exit_shadow.append_event", boom)
        with caplog.at_level(logging.WARNING,
                             logger="ftseek.playbook.eth_held_exit_shadow"):
            emit_eth_held_exit_shadow(_intent(), _result())
        assert "影子写入失败" in caplog.text
        assert caplog.records[0].exc_info is not None


class TestWiring:
    def test_pipeline_resumes_then_emits_shadow_with_fuse(self):
        resume_pos = _PIPE_SRC.index("result = dual.resume_close(")
        emit_pos = _PIPE_SRC.index("emit_eth_held_exit_shadow(intent, result)")
        alert_pos = _PIPE_SRC.index('_alert_close_failure(intent["symbol"]')
        assert resume_pos < emit_pos < alert_pos
        context = _PIPE_SRC[emit_pos - 400:emit_pos]
        assert "try:" in context and "eth_held_exit_shadow" in context

    def test_shadow_failure_is_bested_effort_in_source(self):
        emit_pos = _PIPE_SRC.index("emit_eth_held_exit_shadow(intent, result)")
        tail = _PIPE_SRC[emit_pos:_PIPE_SRC.index("if not result.get(\"success\")", emit_pos)]
        assert "except Exception as _eth_held_shadow_error" in tail
        assert "exc_info=True" in tail


class TestObserver:
    def test_today_row_is_p1(self, tmp_path, monkeypatch):
        observer = _load_observer()
        monkeypatch.setattr(observer, "SHADOW", tmp_path)
        event = build_eth_held_rth_event(_intent(), _result(), rth_open=True)
        (tmp_path / "eth_held_exit_escalation.jsonl").write_text(
            json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        findings = []
        observer.section_shadows(event["date"], findings)
        assert any(
            level == "P1" and "ETH出场HELD在RTH仍阻塞" in message
            and "US.ACN" in message
            for level, message in findings
        )

    def test_no_rows_no_finding(self, tmp_path, monkeypatch):
        observer = _load_observer()
        monkeypatch.setattr(observer, "SHADOW", tmp_path)
        findings = []
        observer.section_shadows("2026-10-02", findings)
        assert not any(
            "ETH出场HELD" in message for _, message in findings)
