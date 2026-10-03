"""退出链取价有界化 (2026-09-22 开盘 OpenD 行情瞬断 121s 停摆修复) 的回归钉子.

缺陷: `monitor_playbook_sim_exits` 的取价直调 `data_router.get_batch_quotes` /
  `data_router.get_quote` (批量 miss 后逐仓再走一次), 在 OpenD 行情连接瞬断时
  **无上界阻塞** — 实测 ET 2026-09-22 09:30:53→09:32:54 watchdog 报
  `playbook_exit_loop 已 121s 无心跳` (阈值 120s)。退出快循环是 SL/TP 的执行者,
  停摆 = 止损不生效。
修法: `_pb_exit_bounded_fetch` (有界线程取价, 超时即放弃本轮) +
  `_pb_exit_price_cache_update/_view` (最近一次成功价兜底, 未超龄才可用)。

本组锁四条: ①超时不阻塞调用方; ②超时后回落到兜底价, 兜底价超龄按无价 (旧行为);
  ③单飞 — 在途时不重复发起 (防连接故障期线程堆积); ④超时线程自行结束后其结果
  在下一轮被收编 (不白丢一次成功取价)。

回滚: 删 `_pb_exit_*` 助手 + 调用点还原为直调 data_router.get_batch_quotes/get_quote,
  本文件随之删除。
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))))

from app.playbook import pipeline as pb  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pb_exit_state():
    """模块级兜底缓存/在途表逐测试清空 (否则跨测试串状态)。"""
    pb._PB_EXIT_PRICE_CACHE.clear()
    pb._PB_EXIT_PRICE_CACHE_TS.clear()
    pb._PB_EXIT_FETCH_INFLIGHT.clear()
    pb._PB_EXIT_FETCH_BOX.clear()
    yield
    pb._PB_EXIT_PRICE_CACHE.clear()
    pb._PB_EXIT_PRICE_CACHE_TS.clear()
    pb._PB_EXIT_FETCH_INFLIGHT.clear()
    pb._PB_EXIT_FETCH_BOX.clear()


class TestBoundedFetch:
    def test_success_path_returns_value_and_clears_inflight(self):
        val, timed_out, err = pb._pb_exit_bounded_fetch(
            "t-ok", lambda: {"US.AAA": {"price": 12.5}}, 1.0)
        assert val == {"US.AAA": {"price": 12.5}}
        assert (timed_out, err) == (False, None)
        assert "t-ok" not in pb._PB_EXIT_FETCH_INFLIGHT

    def test_timeout_does_not_block_caller(self):
        """阻塞的取价必须让出控制权 (调用方在 timeout 量级返回, 不停摆)。"""
        t0 = time.time()
        val, timed_out, err = pb._pb_exit_bounded_fetch(
            "t-slow", lambda: (time.sleep(3.0), {"US.BBB": {"price": 1.0}})[1], 0.2)
        elapsed = time.time() - t0
        assert timed_out is True and val is None and err is None
        assert elapsed < 1.0, f"超时未被上界约束, 实际 {elapsed:.2f}s"

    def test_error_is_surfaced_not_swallowed(self):
        """取价异常必须回传 (由调用方决定日志级别), 不静默。"""
        def _boom():
            raise ValueError("quote source down")

        val, timed_out, err = pb._pb_exit_bounded_fetch("t-err", _boom, 1.0)
        assert val is None and timed_out is False
        assert isinstance(err, ValueError)

    def test_single_flight_skips_duplicate_spawn(self):
        """在途取价未返回时, 同名 label 再调用立即返回且不新起线程。"""
        pb._pb_exit_bounded_fetch(
            "t-flight", lambda: (time.sleep(2.0), {})[1], 0.2)
        threads_before = threading.active_count()
        t0 = time.time()
        val, timed_out, err = pb._pb_exit_bounded_fetch(
            "t-flight", lambda: {"US.CCC": {"price": 9.0}}, 1.0)
        assert timed_out is True and val is None
        assert time.time() - t0 < 0.2, "在途时应当立即返回, 不等待新线程"
        assert threading.active_count() <= threads_before

    def test_timed_out_thread_result_harvested_next_round(self):
        """上一轮超时线程自行结束后, 其结果在下一轮收编进兜底缓存 (不白丢)。"""

        def _slow_good():
            time.sleep(0.4)
            return {"US.DDD": {"price": 7.5}}

        pb._pb_exit_bounded_fetch("t-harvest", _slow_good, 0.05)
        assert pb._pb_exit_price_cache_view(["US.DDD"]) == {}
        time.sleep(0.6)  # 让被放弃的线程跑完
        pb._pb_exit_bounded_fetch("t-harvest", lambda: {}, 1.0)
        assert pb._pb_exit_price_cache_view(["US.DDD"]) == {"US.DDD": {"price": 7.5}}


class TestPriceCacheFallback:
    def test_view_returns_recent_and_drops_expired(self):
        pb._pb_exit_price_cache_update({"US.EEE": {"price": 3.0}})
        assert set(pb._pb_exit_price_cache_view(["US.EEE"])) == {"US.EEE"}
        # 超龄 → 视为无价 (与"无价跳过"旧行为一致)
        pb._PB_EXIT_PRICE_CACHE_TS["US.EEE"] = time.time() - (pb._PB_EXIT_PRICE_CACHE_MAX_AGE + 1)
        assert pb._pb_exit_price_cache_view(["US.EEE"]) == {}

    def test_update_ignores_priceless_quotes(self):
        pb._pb_exit_price_cache_update({
            "US.FFF": {"price": 0},
            "US.GGG": {"last": 4.25},
            "US.HHH": "not-a-dict",
        })
        assert set(pb._pb_exit_price_cache_view(["US.FFF", "US.GGG", "US.HHH"])) == {"US.GGG"}

    def test_unknown_symbol_is_no_price(self):
        assert pb._pb_exit_price_cache_view(["US.NOPE"]) == {}


class TestExitCallSiteIsBounded:
    """接线契约: 退出监控的取价路径不得退回无上界直调 (源码级钉)。"""

    @staticmethod
    def _monitor_body() -> str:
        src = pb.__file__
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("def monitor_playbook_exits(")
        return text[start:text.index("\ndef ", start + 10)]

    def test_batch_and_single_fetches_go_through_bounded_helper(self):
        # 去掉注释行再断言 (注释里保留了历史直调写法的说明)
        code = "\n".join(line for line in self._monitor_body().splitlines()
                         if not line.lstrip().startswith("#"))
        assert "data_router.get_batch_quotes(_pb_syms)" in code
        assert "_pb_exit_bounded_fetch(" in code
        assert "quote = data_router.get_quote(symbol)" not in code, \
            "逐仓取价退回无上界直调 = 121s 停摆可复发"
        assert "batch\"" in code and "single:" in code, "批量与逐仓都要走有界助手"


class TestExitMonitorDoesNotStallOnHungPriceSource:
    """集成: 取价源挂起时, 退出监控**一轮必须在上界内返回**。

    这是本次修复的原始诉求 (规定行为 = 快循环心跳恒 ≤ interval): 若取价源在半死连接上
    无限阻塞, 调用方不许陪着一起等 — 放弃本轮取价、按无价跳过, 让 loop 继续转。
    """

    def test_monitor_round_returns_within_bounds(self, tmp_path, monkeypatch):
        import sqlite3
        import types
        from datetime import timezone

        db = tmp_path / "trade_records.db"
        with sqlite3.connect(str(db)) as conn:
            conn.executescript("""
                CREATE TABLE trade_records (
                    id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
                    entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
                    quantity INTEGER, exit_reason TEXT, futu_order_id TEXT, source_type TEXT,
                    metadata_json TEXT, decision_id TEXT, trade_mode TEXT);
            """)
            conn.execute(
                "INSERT INTO trade_records (id, trade_date, symbol, direction, entry_price,"
                " entry_time, quantity, source_type, metadata_json, futu_order_id, decision_id,"
                " trade_mode)"
                " VALUES (1,'2026-09-22','US.TESTX','LONG',100.0,'2026-09-22T09:00:00-04:00',10,"
                "'PLAYBOOK_SIM','{\"stop_loss\": 95.0}','','','simulate')")
            conn.commit()

        fake_paths = types.ModuleType("app.core.paths")
        fake_paths.get_trade_records_db = lambda: str(db)
        fake_paths.get_trade_records_db_for_arm = lambda target_env: str(db)
        fake_paths.get_exit_ledger_db = lambda: str(tmp_path / "ledger.db")
        monkeypatch.setitem(sys.modules, "app.core.paths", fake_paths)

        fake_trig = types.ModuleType("app.core.scheduler_triggers")
        fake_trig.ET = timezone.utc
        fake_trig.MarketPhase = types.SimpleNamespace(TRADING="TRADING", CLOSED="CLOSED")
        fake_trig.get_market_phase = lambda: "TRADING"
        monkeypatch.setitem(sys.modules, "app.core.scheduler_triggers", fake_trig)

        # 跳过 broker 侧恢复 (本测试只考取价路径)
        monkeypatch.setattr(pb, "_resume_playbook_exits", lambda **kw: (0, set(), True))
        monkeypatch.setattr(pb, "_PB_EXIT_FETCH_TIMEOUT", 0.5)
        monkeypatch.setattr(pb, "_PB_EXIT_FETCH_SINGLE_TIMEOUT", 0.2)

        class HungRouter:
            def get_batch_quotes(self, symbols):
                time.sleep(30)          # 模拟 SDK 在"半死"连接上无上界阻塞

            def get_quote(self, symbol):
                time.sleep(30)

        scheduler = types.SimpleNamespace(data_router=HungRouter())
        t0 = time.time()
        pb.monitor_playbook_exits(scheduler)
        elapsed = time.time() - t0
        # 上界证据 (非假绿): 批量取价确实发起过且被放弃 (仍在途), 说明阻塞的是取价线程
        assert "batch" in pb._PB_EXIT_FETCH_INFLIGHT, "未走到批量取价 (测试前置失败, 非绿)"
        assert elapsed < 3.0, f"退出监控被挂起的取价源拖住 {elapsed:.2f}s (上界失效)"
