"""[DEB-20260902-stale-bridge] 陈旧 bridge / 陈旧 decision_id 双守卫契约钉 (2026-09-02)。

病根回归钉 — 9/1 实锤事故 (logs/ftseek_us.log-20260902):
  服务 11:42CST 启动载入 8/31 bridge (7 候选) → 9/1 bridge 19:27:54 落盘 (13 候选,
  DA skill 同时直写 13 条当日指令) → P0-2 守卫①"已是 BridgeVotingAdapter 就不重载"
  直接 return → 21:04 服务侧生成批 5 条 vote→instruction 全带 DA-202608311*
  (BABA/LLY/BWXT/META/EIX) → 4/14 成交溯源 T-1 决策 (净 +157.17 = 当日净利 72%),
  其中 BABA/BWXT 当日 DA 门已显式 BLOCK (破位硬买拦截), EIX 当日 DA 为 SKIP/BLOCK
  (8/31 SHORT strength=80 全天重试 67 次, 仅被单轮上限→单日 notional 上限挡住;
  54.00→收盘 58.94, 若成交会在 5.5% SL 被扫 ≈ -$240)。22:01 冻结 heal 重启才换血。

任何把"Bridge 无需重载"或"同 prio 无条件同源类替换"加回来的改动必须先打碎这些钉子。

覆盖:
  S1-S10 pipeline._reload_bridge_if_landed (陈旧重载/当日不重载/开关回滚/P0-2 原路径/
        文件未就绪不消耗额度/_trade_date 缺失 fail-soft/同日重写/启动基线/幂等/非法基线)
  D1-D5 db.save_instruction 覆盖仲裁新鲜度 tiebreaker (拒陈旧/同日照替换/更新照放行/
        开关回滚/既有阶梯语义不破)
  W1-W4 布线与配置面 (yaml 双键在位 + 开关函数读的是声明键 + 启动基线布线)
  I1-I2 I15 尺子在位 + _did_trade_date 口径 (非 DA/畸形 → '' = 守卫不介入)
回滚: 删本文件 + 两个 yaml 键置 false (行为逐字节回到 P0-2 / RFC-066 原状)。
"""
import inspect
import sqlite3
from unittest.mock import MagicMock

import pytest
import yaml

from app.playbook import db as pdb
from app.playbook import pipeline as pl
from app.playbook.db import PlaybookDB, _did_trade_date
from app.playbook.pipeline import BridgeVotingAdapter, _reload_bridge_if_landed
from tests.helpers.invariant_harness import isolate_invariant_check

TODAY = "2026-09-01"
YESTERDAY = "2026-08-31"
PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
UNIFIED_CONFIG = PROJECT_ROOT / "config" / "unified_config.yaml"


def _cand(symbol, did, direction="LONG"):
    return {"symbol": symbol, "direction": direction, "score": 6.0,
            "confidence": 0.7, "decision_id": did}


@pytest.fixture
def fresh_bridge(monkeypatch):
    """当日 bridge 文件就绪 + _try_load_bridge 返回当日候选 (13 候选形态收敛为 2 只)。"""
    adapter = BridgeVotingAdapter(
        {"US.SAIC": _cand("US.SAIC", "DA-20260901191627-US.SAIC-1ea2"),
         "US.LLY": _cand("US.LLY", "DA-20260901192313-US.LLY-2038")}, TODAY)
    monkeypatch.setattr(pl, "_today_et_str", lambda: TODAY)
    monkeypatch.setattr(pl, "_bridge_file_state", lambda _d: "ok")
    monkeypatch.setattr(pl, "_try_load_bridge", lambda: adapter)
    monkeypatch.setattr(pl, "_bridge_reloaded_date", None)
    monkeypatch.setattr(pl, "_bridge_reloaded_mtime", None)
    monkeypatch.setattr(pl, "_bridge_reloaded_sha", None)
    return adapter


@pytest.fixture
def scheduler_with_stale_bridge():
    """内存里挂着 T-1 bridge 的调度器 (9/1 11:42CST 启动形态)。"""
    sched = MagicMock()
    sched.voting_coordinator = BridgeVotingAdapter(
        {"US.LLY": _cand("US.LLY", "DA-20260831191607-US.LLY-cd0c"),
         "US.BABA": _cand("US.BABA", "DA-20260831191642-US.BABA-cd2f")}, YESTERDAY)
    return sched


class TestStaleBridgeReload:
    """S 组: pipeline._reload_bridge_if_landed 交易日滚动重载。"""

    def test_s1_stale_bridge_is_reloaded(self, fresh_bridge, scheduler_with_stale_bridge):
        """S1 病根钉: 内存 T-1 bridge + 当日文件 ok → 必须换成当日 adapter。"""
        stale = scheduler_with_stale_bridge.voting_coordinator
        _reload_bridge_if_landed(scheduler_with_stale_bridge)
        assert scheduler_with_stale_bridge.voting_coordinator is fresh_bridge
        assert scheduler_with_stale_bridge.voting_coordinator is not stale
        assert pl._bridge_reloaded_date == TODAY, "重载成功必须消耗当日额度 (幂等)"

    def test_s2_same_day_bridge_not_reloaded(self, fresh_bridge, monkeypatch):
        """S2: 内存 bridge 已是当日 → 不重载 (P0-2 原语义: Bridge 无需重载)。"""
        sched = MagicMock()
        sched.voting_coordinator = BridgeVotingAdapter(
            {"US.SAIC": _cand("US.SAIC", "DA-20260901191627-US.SAIC-1ea2")}, TODAY)
        same = sched.voting_coordinator
        called = []
        monkeypatch.setattr(pl, "_try_load_bridge", lambda: called.append(1) or fresh_bridge)
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is same
        assert not called, "当日 bridge 在内存时不应触发文件重读"
        assert pl._bridge_reloaded_date is None

    def test_s3_switch_off_is_rollback_to_p0_2(self, fresh_bridge, scheduler_with_stale_bridge,
                                                monkeypatch, caplog):
        """S3 回滚钉: 开关 false → 逐字节回到 P0-2 行为 (陈旧候选照消费) 且 WARNING 留痕。"""
        monkeypatch.setattr(pl, "_stale_bridge_reload_enabled", lambda: False)
        stale = scheduler_with_stale_bridge.voting_coordinator
        with caplog.at_level("WARNING"):
            _reload_bridge_if_landed(scheduler_with_stale_bridge)
        assert scheduler_with_stale_bridge.voting_coordinator is stale
        assert pl._bridge_reloaded_date is None
        assert "陈旧bridge在内存" in caplog.text, "回滚路径必须显性留痕, 不得静默消费 T-1 候选"

    def test_s4_non_bridge_adapter_keeps_p0_2_path(self, fresh_bridge, monkeypatch):
        """S4: TechnicalFallback 形态仍走 P0-2 原路径 (本修复不得 regress 8/26 那半边)。"""
        sched = MagicMock()

        class TechnicalFallbackAdapter:  # 同名判定靠 type().__name__, 本地替身即可
            pass

        sched.voting_coordinator = TechnicalFallbackAdapter()
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge
        assert pl._bridge_reloaded_date == TODAY

    def test_s5_file_not_ready_does_not_burn_daily_quota(self, scheduler_with_stale_bridge,
                                                         monkeypatch):
        """S5: 当日文件未就绪 → 不重载且不消耗额度 (下一轮 run_generate 还能再试)。"""
        monkeypatch.setattr(pl, "_today_et_str", lambda: TODAY)
        monkeypatch.setattr(pl, "_bridge_file_state", lambda _d: "missing")
        stale = scheduler_with_stale_bridge.voting_coordinator
        _reload_bridge_if_landed(scheduler_with_stale_bridge)
        assert scheduler_with_stale_bridge.voting_coordinator is stale
        assert pl._bridge_reloaded_date is None

    def test_s6_missing_trade_date_fail_soft(self, fresh_bridge, monkeypatch):
        """S6: 内存 adapter 无 _trade_date → 不判陈旧 (fail-soft 保持 P0-2 原行为)。"""
        sched = MagicMock()
        legacy = BridgeVotingAdapter({"US.LLY": _cand("US.LLY", "DA-x")}, "")
        sched.voting_coordinator = legacy
        called = []
        monkeypatch.setattr(pl, "_try_load_bridge", lambda: called.append(1) or fresh_bridge)
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is legacy
        assert not called

    def test_s7_same_day_rewritten_bridge_refreshes(self, fresh_bridge, monkeypatch):
        """S7: 同日 bridge 被重跑/finalize 重写 (mtime 变) → 刷新内存 adapter。"""
        sched = MagicMock()
        old = BridgeVotingAdapter(
            {"US.SAIC": _cand("US.SAIC", "DA-20260901191627-US.SAIC-1ea2")}, TODAY)
        sched.voting_coordinator = old
        monkeypatch.setattr(pl, "_bridge_reloaded_date", TODAY)
        monkeypatch.setattr(pl, "_bridge_reloaded_mtime", 100.0)
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 200.0)
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge
        assert pl._bridge_reloaded_date == TODAY
        assert pl._bridge_reloaded_mtime == 200.0

    def test_s8_startup_baseline_enables_same_day_refresh(self, fresh_bridge, monkeypatch):
        """S8: 启动已载当日 bridge 也必须记版本基线，同日重写后能换进内存。"""
        sched = MagicMock()
        loaded = BridgeVotingAdapter(
            {"US.SAIC": _cand("US.SAIC", "DA-20260901191627-US.SAIC-1ea2")}, TODAY)
        sched.voting_coordinator = loaded
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 100.0)
        pl._record_bridge_loaded_baseline(loaded, pl._bridge_mtime(TODAY))
        assert pl._bridge_reloaded_date == TODAY
        assert pl._bridge_reloaded_mtime == 100.0
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 200.0)
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge
        assert pl._bridge_reloaded_mtime == 200.0

    def test_s9_startup_baseline_unchanged_mtime_keeps_adapter(self, fresh_bridge, monkeypatch):
        """S9: 启动基线建立后文件未变，不重复读盘，保持一次加载语义。"""
        sched = MagicMock()
        loaded = BridgeVotingAdapter(
            {"US.SAIC": _cand("US.SAIC", "DA-20260901191627-US.SAIC-1ea2")}, TODAY)
        sched.voting_coordinator = loaded
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 100.0)
        pl._record_bridge_loaded_baseline(loaded, 100.0)
        called = []
        monkeypatch.setattr(pl, "_try_load_bridge", lambda: called.append(1) or fresh_bridge)
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is loaded
        assert not called
        assert pl._bridge_reloaded_mtime == 100.0

    def test_s10_baseline_helper_ignores_non_bridge_or_missing_date(self, fresh_bridge):
        """S10: 非 Bridge 或缺交易日不得伪造刷新基线。"""
        pl._record_bridge_loaded_baseline(object(), 100.0)
        pl._record_bridge_loaded_baseline(None, 100.0)
        pl._record_bridge_loaded_baseline(BridgeVotingAdapter({}, ""), 100.0)
        assert pl._bridge_reloaded_date is None
        assert pl._bridge_reloaded_mtime is None


class TestBridgeContentBoundVersion:
    """S11-S17 (A2 内容绑定版本契约, 2026-11-27): 版本须绑定实际读入内容,
    而非探测期 mtime。覆盖同 mtime 重写/探测-读取竞态/旧 adapter 迁移/
    一致读取稳定性校验/坏文件不消耗基线。现有 S1-S10 mtime 钉零改动。"""

    def test_s11_same_mtime_content_rewrite_refreshes(self, fresh_bridge, monkeypatch):
        """S11 根因钉: mtime 不变但内容重写 → sha 变化仍须刷新 (mtime 代理漏检)。"""
        sched = MagicMock()
        old = BridgeVotingAdapter({"US.SAIC": _cand("US.SAIC", "DA-old")}, TODAY)
        old._source_sha256 = "sha-old"
        old._source_mtime = 100.0
        sched.voting_coordinator = old
        pl._record_bridge_loaded_baseline(old, 100.0)
        fresh_bridge._source_sha256 = "sha-new"
        fresh_bridge._source_mtime = 100.0  # mtime 不变, 仅内容变
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 100.0)
        monkeypatch.setattr(pl, "_bridge_content_sha", lambda _d: "sha-new")
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge, "同 mtime 内容重写必须刷新"
        assert pl._bridge_reloaded_sha == "sha-new"

    def test_s12_probe_read_race_records_read_content(self, fresh_bridge, monkeypatch):
        """S12 (A2 要求4): 探测 sha ≠ 实际读入内容 → 基线记实际读入版本, 不错误归属。"""
        sched = MagicMock()
        old = BridgeVotingAdapter({"US.SAIC": _cand("US.SAIC", "DA-old")}, TODAY)
        old._source_sha256 = "sha-old"
        old._source_mtime = 100.0
        sched.voting_coordinator = old
        pl._record_bridge_loaded_baseline(old, 100.0)
        fresh_bridge._source_sha256 = "sha-new"
        fresh_bridge._source_mtime = 300.0
        monkeypatch.setattr(pl, "_bridge_content_sha", lambda _d: "sha-mid")  # 探测期版本
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 200.0)  # 探测期 mtime
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge
        assert pl._bridge_reloaded_sha == "sha-new", "基线须绑定实际读入内容, 非探测 sha-mid"
        assert pl._bridge_reloaded_mtime == 300.0, "mtime 须取实际读入版本, 非探测 200"

    def test_s13_legacy_adapter_without_sha_migrates(self, fresh_bridge, monkeypatch):
        """S13 (A2 迁移): 同日旧 adapter 缺 sha → mtime 变化即刷新并建立 sha 绑定, 不永远返回。"""
        sched = MagicMock()
        legacy = BridgeVotingAdapter({"US.SAIC": _cand("US.SAIC", "DA-old")}, TODAY)
        sched.voting_coordinator = legacy  # 无 _source_sha256
        monkeypatch.setattr(pl, "_bridge_reloaded_date", TODAY)
        monkeypatch.setattr(pl, "_bridge_reloaded_mtime", 100.0)
        monkeypatch.setattr(pl, "_bridge_reloaded_sha", None)
        fresh_bridge._source_sha256 = "sha-new"
        fresh_bridge._source_mtime = 200.0
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 200.0)  # mtime 变化触发迁移
        monkeypatch.setattr(pl, "_bridge_content_sha", lambda _d: "sha-new")
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is fresh_bridge, "缺版本记录的旧 adapter 不得永远返回"
        assert pl._bridge_reloaded_sha == "sha-new"

    def test_s14_content_unchanged_no_reload(self, fresh_bridge, monkeypatch):
        """S14 幂等: 内容 sha 未变 (即便 mtime 被 touch) → 不重复读盘/注入。"""
        sched = MagicMock()
        old = BridgeVotingAdapter({"US.SAIC": _cand("US.SAIC", "DA-old")}, TODAY)
        old._source_sha256 = "sha-same"
        old._source_mtime = 100.0
        sched.voting_coordinator = old
        pl._record_bridge_loaded_baseline(old, 100.0)
        called = []
        monkeypatch.setattr(pl, "_try_load_bridge", lambda: called.append(1) or fresh_bridge)
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 999.0)  # mtime 被 touch
        monkeypatch.setattr(pl, "_bridge_content_sha", lambda _d: "sha-same")  # 内容未变
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is old
        assert not called, "内容未变不得触发重读 (sha 权威, 不被 mtime touch 误触发)"

    def test_s15_failed_load_keeps_adapter_and_baseline(self, fresh_bridge, monkeypatch):
        """S15 (A2 要求7): 坏文件/不稳定读取 → loader None → 保留旧 adapter, 不消耗成功基线。"""
        sched = MagicMock()
        old = BridgeVotingAdapter({"US.SAIC": _cand("US.SAIC", "DA-old")}, TODAY)
        old._source_sha256 = "sha-old"
        old._source_mtime = 100.0
        sched.voting_coordinator = old
        pl._record_bridge_loaded_baseline(old, 100.0)
        monkeypatch.setattr(pl, "_bridge_content_sha", lambda _d: "sha-bad")
        monkeypatch.setattr(pl, "_bridge_mtime", lambda _d: 200.0)
        monkeypatch.setattr(pl, "_try_load_bridge", lambda: None)  # 坏 JSON/不稳定 → None
        _reload_bridge_if_landed(sched)
        assert sched.voting_coordinator is old
        assert pl._bridge_reloaded_sha == "sha-old", "失败不得消耗成功基线"
        assert pl._bridge_reloaded_mtime == 100.0


class TestBridgeConsistentRead:
    """S16-S18 (A2 要求5/6): _read_bridge_raw 一致读取 + 注入前稳定性校验。
    直接测临时文件, 不调真实 loader (避免写生产动态池)。"""

    def test_s16_stable_read_returns_bytes_and_mtime(self, tmp_path):
        p = tmp_path / "bridge.json"
        p.write_text('{"candidates": []}', encoding="utf-8")
        res = pl._read_bridge_raw(str(p))
        assert res is not None
        raw, mtime = res
        assert raw == b'{"candidates": []}'
        assert mtime == p.stat().st_mtime

    def test_s17_missing_file_returns_none(self, tmp_path):
        assert pl._read_bridge_raw(str(tmp_path / "absent.json")) is None

    def test_s18_mid_read_rewrite_aborts(self, tmp_path, monkeypatch):
        """读取期间文件被改写 → 稳定性校验失败 → None (不解析/不注入/不消耗基线)。"""
        import builtins
        p = tmp_path / "bridge.json"
        p.write_text('{"candidates": []}', encoding="utf-8")
        real_open = builtins.open

        class RacingFile:
            def __init__(self, *a, **k):
                self._f = real_open(*a, **k)

            def read(self, *a):
                data = self._f.read(*a)
                p.write_text('{"candidates": [], "extra": 1}', encoding="utf-8")
                return data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._f.close()
                return False

        def fake_open(file, *a, **k):
            if str(file) == str(p):
                return RacingFile(file, *a, **k)
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert pl._read_bridge_raw(str(p)) is None


def _mkdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    return db, conn


def _inst(did, direction="BUY", model="qwen3.7-max", **over):
    row = {"date": TODAY, "symbol": "US.LLY", "direction": direction,
           "entry_price": 1179.99, "stop_loss": 1126.89, "take_profit": 1275.57,
           "confidence": 70, "decision_id": did, "model_used": model}
    row.update(over)
    return row


class TestStaleDidOverwriteGuard:
    """D 组: RFC-066 覆盖仲裁的"新鲜度" tiebreaker (同源类同 prio 时)。"""

    def test_d1_stale_da_cannot_overwrite_same_day_da(self):
        """D1 病根钉: 9/1 LLY/META 形态 — T-1 DA 行不得原地覆盖当日 DA 行。"""
        db, conn = _mkdb()
        fresh_id = db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        stale_id = db.save_instruction(_inst("DA-20260831191607-US.LLY-cd0c",
                                             direction="SELL_SHORT", model="deterministic",
                                             entry_price=1142.0))
        assert stale_id == fresh_id, "拒绝覆盖时必须返回现存行 id (不插新行)"
        rows = conn.execute("SELECT direction, decision_id, model_used, entry_price "
                            "FROM playbook_instructions WHERE date=? AND symbol='US.LLY'",
                            (TODAY,)).fetchall()
        assert len(rows) == 1, f"必须保持单行, 实际 {len(rows)}"
        assert rows[0]["decision_id"] == "DA-20260901192313-US.LLY-2038"
        assert rows[0]["direction"] == "BUY", "当日方向不得被 T-1 决策改写"
        assert rows[0]["model_used"] == "qwen3.7-max", "归因不得退化为 deterministic"
        assert rows[0]["entry_price"] == 1179.99

    def test_d2_same_day_replacement_still_allowed(self):
        """D2: 同日 DA 行之间的"同源类替换"语义不变 (幂等重跑/DA 重产)。"""
        db, conn = _mkdb()
        db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        db.save_instruction(_inst("DA-20260901210500-US.LLY-9999", entry_price=1170.0))
        rows = conn.execute("SELECT decision_id, entry_price FROM playbook_instructions "
                            "WHERE date=? AND symbol='US.LLY'", (TODAY,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["decision_id"] == "DA-20260901210500-US.LLY-9999"
        assert rows[0]["entry_price"] == 1170.0

    def test_d3_newer_da_over_older_row_allowed(self):
        """D3: 守卫只挡"更旧", 不挡"更新" — 昨日行被当日决策替换照放行。"""
        db, conn = _mkdb()
        db.save_instruction(_inst("DA-20260831191607-US.LLY-cd0c", model="deterministic"))
        db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        rows = conn.execute("SELECT decision_id, model_used FROM playbook_instructions "
                            "WHERE date=? AND symbol='US.LLY'", (TODAY,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["decision_id"] == "DA-20260901192313-US.LLY-2038"
        assert rows[0]["model_used"] == "qwen3.7-max"

    def test_d4_switch_off_restores_unconditional_replacement(self, monkeypatch, caplog):
        """D4 回滚钉: 开关 false → 回到 RFC-066 无条件同源类替换 (陈旧覆盖复现)。"""
        monkeypatch.setattr(pdb, "_stale_did_guard_enabled", lambda: False)
        db, conn = _mkdb()
        db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        db.save_instruction(_inst("DA-20260831191607-US.LLY-cd0c", model="deterministic"))
        row = conn.execute("SELECT decision_id, model_used FROM playbook_instructions "
                           "WHERE date=? AND symbol='US.LLY'", (TODAY,)).fetchone()
        assert row["decision_id"] == "DA-20260831191607-US.LLY-cd0c"
        assert row["model_used"] == "deterministic"

    def test_d4b_reject_path_is_loud(self, caplog):
        """D4b: 拒绝必须 WARNING 留痕 (静默拒绝 = 供给归零查不到根因, R5 纪律)。"""
        db, _conn = _mkdb()
        db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        with caplog.at_level("WARNING"):
            db.save_instruction(_inst("DA-20260831191607-US.LLY-cd0c"))
        assert "陈旧did覆盖拒绝" in caplog.text

    def test_d5_existing_ladder_semantics_intact(self):
        """D5: 既有阶梯不破 — UE 不覆盖 DA / 空 did 不覆盖有源行 / reentry 另起轮次行。"""
        db, conn = _mkdb()
        db.save_instruction(_inst("DA-20260901192313-US.LLY-2038"))
        db.save_instruction(_inst("universe_expansion_2026-09-01_LLY", direction="SELL_SHORT",
                                  model="glm-5.2"))
        db.save_instruction(_inst("", direction="SKIP", model=None))
        rows = conn.execute("SELECT decision_id, direction FROM playbook_instructions "
                            "WHERE date=? AND symbol='US.LLY' ORDER BY id", (TODAY,)).fetchall()
        assert len(rows) == 1, "UE/空票均不得覆盖或并存 DA 深度行"
        assert rows[0]["decision_id"] == "DA-20260901192313-US.LLY-2038"
        db.save_instruction(_inst("intraday_reentry-1413-1003", model="intraday_reentry_v1"))
        rows = conn.execute("SELECT decision_id FROM playbook_instructions "
                            "WHERE date=? AND symbol='US.LLY' ORDER BY id", (TODAY,)).fetchall()
        assert len(rows) == 2, "reentry 重武装必须新起轮次行 (RFC-066 根因语义)"


class TestWiringAndConfigSurface:
    """W 组: 开关布线与配置面 (键不在 yaml = 行为扛在代码默认值上, 反向普查病灶)。"""

    def test_w1_yaml_declares_both_switches(self):
        cfg = yaml.safe_load(UNIFIED_CONFIG.read_text(encoding="utf-8"))
        pb = cfg["playbook"]
        assert pb["bridge_stale_reload_enabled"] is True
        assert pb["stale_did_overwrite_guard_enabled"] is True

    def test_w2_pipeline_switch_reads_declared_key(self):
        src = inspect.getsource(pl._stale_bridge_reload_enabled)
        assert "playbook.bridge_stale_reload_enabled" in src
        assert pl._stale_bridge_reload_enabled() is True, "默认必须为开 (守卫优先)"

    def test_w3_db_switch_reads_declared_key(self):
        src = inspect.getsource(pdb._stale_did_guard_enabled)
        assert "playbook.stale_did_overwrite_guard_enabled" in src
        assert pdb._stale_did_guard_enabled() is True

    def test_w4_scheduler_records_startup_baseline_after_load(self):
        """W4: 启动路径先取 mtime、加载并挂载成功后再记基线，避免加载期重写被漏掉。"""
        src = (PROJECT_ROOT / "app" / "core" / "scheduler_core.py").read_text(encoding="utf-8")
        start = src.index("# bridge覆盖: 尝试加载今日bridge文件")
        end = src.index("# bridge不存在 → 按配置 fallback", start)
        block = src[start:end]
        assert block.index("_bridge_mtime_before =") < block.index("_try_load_bridge()")
        assert block.index("self.voting_coordinator = _bridge") < block.index(
            "_record_bridge_loaded_baseline(_bridge, _bridge_mtime_before)")


class TestInvariantRulerAndDidCaliber:
    """I 组: I15 次日晨兜底尺子 + did 日期解析口径 (非 DA/畸形 → '' = 不介入)。"""

    def test_i1_invariant_i15_is_mounted(self):
        src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
        assert "I15_stale_decision_id" in src
        assert "I15_detector_error" in src, "尺子自身异常必须显性化 (不得读成零违反)"
        assert "decision_id LIKE 'DA-%'" in src

    @pytest.mark.parametrize("did,expect", [
        ("DA-20260901192313-US.LLY-2038", "2026-09-01"),
        ("DA-20260831191607-US.LLY-cd0c", "2026-08-31"),
        ("DA-20260731-PRIO-x1", "2026-07-31"),
        ("universe_expansion_2026-09-01_LLY", ""),
        ("intraday_reentry-1413-1003", ""),
        ("TFB-US.LLY-1788261288", ""),
        ("DA-notadate", ""),
        ("", ""),
        (None, ""),
    ])
    def test_i2_did_trade_date_caliber(self, did, expect):
        assert _did_trade_date(did) == expect


# ═══════════════════════════════════════════════════════════════
# I 组功能面: I15 尺子实跑 (harness 同 tests/test_invariant_i10_exit_time.py)
# ═══════════════════════════════════════════════════════════════
import importlib.util  # noqa: E402
import json  # noqa: E402
from datetime import datetime  # noqa: E402

_dic_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check_i15", PROJECT_ROOT / "scripts" / "daily_invariant_check.py")

# I15 组统一冻结时钟 (2026-09-10, DEB-20260910-logging-observability §10.7):
#   尺子窗口 = now_et()-7d 的滑动窗, 而本组夹具日期是硬编码常量 (09-01/09-02)。
#   不冻结 → 2026-09-10 起窗口滑过夹具日: 正例天天恒红 ("陈旧 did" 假阳), 三个负例
#   恒真空转 (行根本不在窗内, "不告"是假绿)。冻结在 09-05 ⇒ window_since=08-29,
#   夹具日全部在窗内, 判定只由 _I15_SINCE floor 与 did 日期决定 = 尺子本意。
_I15_NOW = datetime(2026, 9, 5, 12, 0)

_TRADE_COLS = (
    "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, source_type TEXT, "
    "trade_date TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, "
    "exit_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL, "
    "fees_est REAL, pnl_pct REAL, mfe_pct REAL, mae_pct REAL, "
    "signal_confidence REAL, metadata_json TEXT"
)
_INST_COLS = (
    "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, decision_id TEXT, "
    "executed INTEGER, outcome TEXT, pnl REAL, confidence REAL"
)


_I15_FLOOR = None


def _i15_floor():
    """尺子口径起点 _I15_SINCE — 供"豁免来自 floor 而非窗口滑过"的断言用。"""
    global _I15_FLOOR
    if _I15_FLOOR is None:
        _dic = importlib.util.module_from_spec(_dic_spec)
        _dic_spec.loader.exec_module(_dic)
        _I15_FLOOR = _dic._I15_SINCE
    return _I15_FLOOR


def _assert_window_covers(res, *dates):
    """防空转钉: 断言时钟冻结确已生效, 且被测行真的落在尺子窗口内。

    少了这层, "不告"可能只是行滑出 7 天窗 (假绿), 而不是判据放行。"""
    assert res["checked_at"].startswith(_I15_NOW.date().isoformat()), res["checked_at"]
    for d in dates:
        assert res["window_since"] <= d, (
            f"夹具日 {d} 滑出窗口 (window_since={res['window_since']}) → 本例空转")


def _run_invariant(tmp_path, monkeypatch, inst_rows, frozen_now=None):
    dic = importlib.util.module_from_spec(_dic_spec)
    _dic_spec.loader.exec_module(dic)
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_TRADE_COLS})")
    conn.execute(f"CREATE TABLE playbook_instructions ({_INST_COLS})")
    conn.executemany(
        "INSERT INTO playbook_instructions (date, symbol, direction, decision_id, "
        "executed, outcome, pnl, confidence) VALUES (?,?,?,?,?,?,?,?)", inst_rows)
    conn.commit()
    conn.close()
    out = tmp_path / "invariant_out.json"
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "OUT", out)
    if frozen_now is not None:
        monkeypatch.setattr(dic, "now_et", lambda: frozen_now)
    # 2026-09-14: 隔离生产读面 + 关外发。本组只判 I15 键 (返回码受其他不变量影响),
    # 但 main() 默认 push_enabled=True ⇒ W1_* 命中时会**从单测直推钉钉**; 且 W4/W5/W6
    # 扫描面按 REPO 全仓 AST 解析 (~900 文件/例)。两者都由 harness 收口。
    isolate_invariant_check(dic, tmp_path, monkeypatch)
    dic.main(push_enabled=False)
    return json.loads(out.read_text())


class TestInvariantI15Functional:
    def test_i15_flags_stale_did_go_forward(self, tmp_path, monkeypatch):
        """I15 病根钉: 修复部署日起, did 决策日 ≠ 指令日 → 必须报违反。"""
        res = _run_invariant(tmp_path, monkeypatch, [
            ("2026-09-02", "US.LLY", "BUY", "DA-20260901192313-US.LLY-2038", 1, "win", 10.0, 70),
        ], frozen_now=_I15_NOW)
        _assert_window_covers(res, "2026-09-02")
        assert res["violations"].get("I15_stale_decision_id") == 1
        assert "did 决策日=2026-09-01" in res["examples"]["I15_stale_decision_id"]

    def test_i15_clean_when_did_matches_date(self, tmp_path, monkeypatch):
        res = _run_invariant(tmp_path, monkeypatch, [
            ("2026-09-02", "US.LLY", "BUY", "DA-20260902192313-US.LLY-2038", 1, "win", 10.0, 70),
        ], frozen_now=_I15_NOW)
        _assert_window_covers(res, "2026-09-02")
        assert "I15_stale_decision_id" not in res["violations"]

    def test_i15_exempts_founding_evidence_rows(self, tmp_path, monkeypatch):
        """I15 口径起点: 9/1 立档证据行不重复告警 (历史行不自愈 → 连告 7 天=不可动作噪声)。"""
        res = _run_invariant(tmp_path, monkeypatch, [
            ("2026-09-01", "US.BWXT", "BUY", "DA-20260831191607-US.BWXT-cd0c", 1, "win", 105.63, 70),
        ], frozen_now=_I15_NOW)
        _assert_window_covers(res, "2026-09-01")
        # 豁免必须来自 _I15_SINCE floor, 不能来自"行滑出 7 天窗"(后者随日历变假绿)
        assert "2026-09-01" < _i15_floor(), "本例已被窗口放行 → floor 判据未被覆盖"
        assert "I15_stale_decision_id" not in res["violations"]

    def test_i15_non_da_did_not_judgeable(self, tmp_path, monkeypatch):
        """I15 fail-soft: UE/reentry/畸形 did 不可判日期 → 不告 (尺子自身不得造假阳)。"""
        res = _run_invariant(tmp_path, monkeypatch, [
            ("2026-09-02", "US.MSTR", "SELL_SHORT", "universe_expansion:2026-09-02:MSTR",
             1, "win", 5.0, 70),
            ("2026-09-02", "US.BABA", "BUY", "intraday_reentry-1420-1223", 0, "missed", 0.0, 70),
            ("2026-09-02", "US.EIX", "SELL_SHORT", "DA-notadate", 0, "missed", 0.0, 70),
        ], frozen_now=_I15_NOW)
        _assert_window_covers(res, "2026-09-02")
        assert "I15_stale_decision_id" not in res["violations"]


class TestInvariantI15ClockFrozen:
    """结构钉 (2026-09-10): 本组每例都必须冻结时钟 + 断言窗口覆盖。

    9/10 实锤的日历炸弹是"夹具日期硬编码 vs 尺子窗口滑动"的耦合, 修一次不够 —
    新增用例只要忘传 frozen_now, 就会在 7 天后重新退化成恒红或恒真空转。"""

    def test_every_i15_case_freezes_the_clock(self):
        src = inspect.getsource(TestInvariantI15Functional)
        calls = src.count("_run_invariant(")
        assert calls >= 4, calls
        assert src.count("frozen_now=_I15_NOW") == calls, (
            "有 I15 用例未冻结时钟 → 日历炸弹 (窗口滑过夹具日)")

    def test_every_i15_case_asserts_window_coverage(self):
        src = inspect.getsource(TestInvariantI15Functional)
        assert src.count("_assert_window_covers(") >= src.count("_run_invariant("), (
            "有 I15 用例未断言窗口覆盖 → 负例可能只是空转 (假绿)")
