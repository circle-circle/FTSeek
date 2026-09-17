"""[P1 Step4 2026-09-12, DEB-20260910 / proud-slope-falcon] 离线全链验证 (SIM 身份 + 冻结材料).

计划要求: "第一批先使用冻结材料和 stub LLM 跑完整 scan→select→generate→inject→execute,
不调用真实账户, 不发送通知"; 且"当前若非交易时段, 只验离线全链, 明确**尚未观察模拟成交**"。

分工 (不重复既有钉子):
  - UE scan → inject: 本文件 (驱动 `tests/playbook/offline_chain_driver.py`, 真实代码路径);
  - DA select: `tests/playbook/test_da_candidate_selection.py` (仓外脚本, 独立进程 64 例);
  - 资格纯规则: `tests/playbook/test_candidate_eligibility.py`;
  - 批次/额度/固定终点: `tests/playbook/test_ue_funnel_batch.py`;
  - inject → **execute** 交棒: 本文件 TestChainExecuteStage (用 inject 真写出的 DB 行喂
    `run_playbook_auto_execute`, 真 `_check_intraday_trigger` + 真低ATR门)。

诚实边界: 全链在**临时库 + 伪造行情 + 伪造 broker/service** 上跑通 ≠ 已观察模拟成交。
真实 SIM 成交需按批准重启服务并在正常交易时段观察, 本批未做。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DRIVER = ROOT / "tests" / "playbook" / "offline_chain_driver.py"
PYTHON = sys.executable

ET = timezone(timedelta(hours=-4))


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """跑一次离线链 (子进程), 全模块共享证据。驱动失败即失败, 不 skip。"""
    workdir = tmp_path_factory.mktemp("offline-chain")
    out = workdir / "chain_evidence.json"
    proc = subprocess.run(
        [PYTHON, str(DRIVER), "--workdir", str(workdir), "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert out.exists(), f"驱动未产出证据 (rc={proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    evidence = json.loads(out.read_text(encoding="utf-8"))
    evidence["_workdir"] = str(workdir)
    evidence["_stdout"] = proc.stdout
    return evidence


# ══ Stage 0: 身份与隔离前置条件 ════════════════════════════════════════════
class TestChainPreconditions:
    def test_automatic_route_is_sim(self, chain):
        """P0.2: 只有自动 broker 与自动账本同为 SIM 才允许注入新策略。"""
        mode = chain["sim_mode"]
        assert mode["valid"] is True and mode["errors"] == []
        assert mode["broker_route"] == "sim" and mode["db_route"] == "sim"
        assert mode["allow_real_trading"] is False

    def test_no_production_write_and_no_network(self, chain):
        """写守卫 (open/Path.open/os.open/sqlite3.connect) + 断网断进程: 零越界。"""
        assert chain["violations"] == []

    def test_no_notification_sent(self, chain):
        """飞书/钉钉一条都没发 (只允许被替身记下 ic_snapshot 这类埋点调用)。"""
        assert [n for n in chain["notifications"] if n[0] == "feishu"] == []

    def test_llm_was_stubbed_exactly_once(self, chain):
        """冻结材料 + stub LLM: 整条链只调一次模型 (预算可数, 不打真实模型)。"""
        assert len(chain["llm_calls"]) == 1
        call = chain["llm_calls"][0]
        assert call["task"] == "universe_expansion_reasoning"
        assert call["json_mode"] is True


# ══ Stage 1: scan — 批次身份/时效/策略版本落盘 ═════════════════════════════
class TestChainScanStage:
    def test_scan_writes_pending_then_complete_batch(self, chain):
        recs = chain["stages"]["ue_scan"]["records"]
        assert chain["stages"]["ue_scan"]["rc"] == 0
        assert [(r["type"], r["batch_status"]) for r in recs] == [
            ("batch_start", "pending"), ("scan", "complete")]

    def test_batch_carries_window_identity_and_policy_version(self, chain):
        scan = chain["stages"]["ue_scan"]["records"][-1]
        assert scan["scan_window"] == "premarket"       # inject 只认这一窗
        assert scan["batch_id"] and scan["batch_id"].startswith("UE:")
        assert scan["policy_version"] == "da_ue_v1"
        assert scan["surfaced_n"] == 7

    def test_scanned_at_is_tz_aware_and_fresh(self, chain):
        """时效判据要吃这个字段: naive/缺失 ⇒ inject 判 missing_or_invalid_timestamp。"""
        stamp = chain["stages"]["ue_scan"]["records"][-1]["scanned_at"]
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert abs((datetime.now(ET) - parsed).total_seconds()) < 3600


# ══ Stage 2: inject — 先过滤后预算 + 写后确认 ══════════════════════════════
class TestChainInjectStage:
    @pytest.fixture(scope="class")
    def rows(self, chain):
        return chain["stages"]["ue_inject"]["rows"]

    @pytest.fixture(scope="class")
    def funnel(self, chain):
        batches = chain["stages"]["ue_inject"]["funnel"]
        assert len(batches) == 1
        return {c["symbol"]: c for c in batches[0]["candidates"]}

    def test_rejected_candidates_become_attributable_skip_rows(self, rows, funnel):
        """资格拒绝 → SKIP 行 (可归因, 不占额度), 三类原因各自成串。"""
        skips = {r["symbol"]: r["skip_reason"] for r in rows if r["direction"] == "SKIP"}
        assert skips == {
            "US.TESTB": "candidate_ineligible(stale_quote)",
            "US.TESTC": "price_floor(3.00<5, OTC/仙股无富途行情)",
            "US.TESTD": "low_atr(0.0050<0.03, 低波票过不了执行门)",
        }
        assert funnel["US.TESTB"]["reasons"] == ["stale_quote"]
        assert funnel["US.TESTC"]["reasons"] == ["price_floor"]
        assert funnel["US.TESTD"]["reasons"] == ["low_atr"]

    def test_highest_confidence_candidates_do_not_buy_a_slot(self, rows, funnel):
        """先过滤后预算的核心证据: conf 0.95/0.90 的两只被资格/缺价挡下,
        名额 (3) 全给了合格队列里 conf 更低的 0.80/0.68/0.66。"""
        written = {r["symbol"] for r in rows if r["direction"] != "SKIP"}
        assert written == {"US.TESTA", "US.TESTE", "US.TESTF"}
        assert funnel["US.TESTG"]["status"] == "UNKNOWN"          # 缺价 ≠ 拒绝
        assert funnel["US.TESTG"]["selection_status"] == "deferred_no_price"
        assert "price" in funnel["US.TESTG"]["missing_fields"]
        assert funnel["US.TESTB"]["selection_status"] == "rejected_ineligible"
        assert all(funnel[s]["selection_status"] == "selected"
                   for s in ("US.TESTA", "US.TESTE", "US.TESTF"))
        assert [funnel[s]["selection_rank"] for s in ("US.TESTA", "US.TESTE", "US.TESTF")] == [1, 2, 3]

    def test_screening_atr_is_raw_daily_ratio_per_symbol(self, funnel):
        """筛选 ATR = 原始日线 TR14/参考价, **逐标的不同** (第一轮离线链曾因地夹具
        按裸符号建键而使七只标的同得 4.03 —— 那条教训由本例守住)。"""
        ratios = {s: c["screening_atr_ratio"] for s, c in funnel.items()
                  if c["screening_atr_ratio"] is not None}
        assert len(set(round(v, 6) for v in ratios.values())) > 1
        assert ratios["US.TESTD"] < 0.01 < ratios["US.TESTA"]
        for value in ratios.values():
            assert 0 < value <= 1.0            # 量纲: 比例, 不是百分数

    def test_written_rows_carry_screening_snapshot_and_chase_cap(self, rows):
        """交棒契约: 执行侧低ATR门与 chase_cap enforce 读的就是这两个字段。"""
        live = [r for r in rows if r["direction"] != "SKIP"]
        assert live
        for row in live:
            raw = json.loads(row["raw_instruction_json"])
            assert raw["source"] == "universe_expansion"
            sc = raw["candidate_screening"]
            assert sc["candidate_id"].endswith(row["symbol"].replace("US.", "") + ":LONG")
            assert sc["status"] == "PASS" and sc["trade_mode"] == "sim"
            assert sc["policy_version"] == "da_ue_v1"
            assert 0 < sc["screening_atr_ratio"] <= 1.0
            cap = raw["chase_cap_price"]
            assert cap == row["entry_price"] or cap > row["entry_price"]   # LONG=带上沿
            assert cap >= row["entry_price"]

    def test_skip_rows_also_carry_candidate_identity(self, rows):
        """写后确认按 candidate_id 回读核对 — SKIP 行同样要有身份, 否则只能判"结果不明"。"""
        for row in (r for r in rows if r["direction"] == "SKIP"):
            raw = json.loads(row["raw_instruction_json"])
            assert raw["candidate_screening"]["candidate_id"]

    def test_quota_accounted_once_and_no_unknown_writes(self, chain):
        stdout = chain["_stdout"]
        assert "本轮名额=3 | 选取=3" in stdout
        assert "deferred_no_price=1" in stdout
        assert "占额度 3/3, 结果不明 0" in stdout
        # confirm_saved 修前会把成功写入的 SKIP 行误报成仲裁拒绝 (见 ue_batch 注释)
        assert "写库仲裁拒绝" not in stdout
        assert "写库结果不明" not in stdout

    def test_batch_decision_audit_written(self, chain):
        """"今天注入依据哪一批材料"必须留痕 (批次纪律的审计面)。"""
        decisions = [r for r in chain["stages"]["ue_inject"]["inject_log"]
                     if r.get("type") == "batch_decision"]
        assert len(decisions) == 1
        row = decisions[0]
        assert row["stage"] == "inject_batch" and row["reason"] == "complete"
        assert row["scan_window"] == "premarket"
        assert row["surfaced_n"] == 7 and row["batch_id"]

    def test_atr_daily_bars_fetched_once_per_symbol(self, chain):
        """轮内 ATR 缓存: 同标的不重复取日线 (预算/限频纪律)。"""
        bars = chain["stages"]["ue_inject"]["router_bar_calls"]
        assert len(bars) == len(set(bars))
        assert "US.TESTD" in bars


# ══ Stage 3: inject → execute 交棒 (真 trigger + 真低ATR门 + 伪造 broker) ═══
class TestChainExecuteStage:
    """用 inject **真写出的 DB 行**驱动 `run_playbook_auto_execute`。

    边界全部是显式替身 (service/broker/capital/regime/DI/paths), 生产 data/ 由
    tests/conftest.py 的缝重定向 + 硬闸保护; futu/LIVE 路由一旦被碰即 AssertionError。
    """

    @pytest.fixture
    def env(self, chain, tmp_path, monkeypatch):
        from app.playbook import pipeline
        from app.playbook.config import get_playbook_config
        from app.playbook.db import PlaybookDB

        db_file = tmp_path / "chain.sqlite"
        shutil.copy(chain["_workdir"] + "/chain.sqlite", db_file)
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        rows = db.get_instructions_by_date(_row_date(conn))
        live_rows = [r for r in rows if r["direction"] in ("BUY", "SELL_SHORT")]
        assert live_rows, "inject 阶段没写出可执行行 — 交棒无从验证"

        submissions, violations = [], []
        real_cfg = get_playbook_config()
        # 只密封 wall-clock / 外部数据依赖的门; 阈值与 sizing 仍走活 yaml (单一真源)。
        overrides = {
            "auto_execute": True,
            "entry_time_cutoff_enabled": False,
            # 2026-09-14: 同类 wall-clock 门, 原漏密封 ⇒ 本文件每天 ET 00:00-09:35
            #   (= CST 12:00-21:35) 恒红 2 例。实测 trace 止于 pipeline.py:5405
            #   `_emit_trigger_miss("before_open_blackout")` 静默 return False:
            #   yaml entry_open_blackout_enabled=true / hhmm=0935, ET 00:23 → 23<935 拦。
            #   生产语义正确 (00:23 ET 确在开盘禁触窗内), 泄漏的是夹具不是门。
            "entry_open_blackout_enabled": False,
            "earnings_blackout_gate_enabled": False,
            "low_atr_gate_enabled": True,
            "low_atr_gate_min_atr_pct": 0.03,
            "symbol_streak_ban_enabled": False,
            "intraday_vol_gate_enabled": False,
            "entry_vwap_gate_enabled": False,
            "falsification_gates_enabled": False,
            "max_new_positions_per_cycle": 10,
        }

        class SealedCfg:
            def get(self, key, default=None):
                return overrides.get(key, real_cfg.get(key, default))

            def get_horizon_params(self, horizon):
                return real_cfg.get_horizon_params(horizon)

            def get_horizon_top(self, key):
                return real_cfg.get_horizon_top(key)

            def __getattr__(self, name):
                return getattr(real_cfg, name)

        sealed = SealedCfg()

        class FakeService:
            def execute_instruction(self, **kwargs):
                blob = json.dumps(kwargs, default=str).lower()
                for token in ("real", "live", "futu_open"):
                    if token in blob:
                        violations.append(f"execute_instruction 出现 {token}: {blob[:200]}")
                submissions.append(kwargs)
                db.update_instruction_tracking(kwargs["instruction_id"], {"executed": 1})
                return {"success": True}

        class FakeBroker:
            def __getattr__(self, name):
                if any(t in name.lower() for t in ("real", "live", "place", "order", "unlock")):
                    violations.append(f"broker.{name} 被调用")
                    raise AssertionError(f"forbidden broker boundary: {name}")
                return Mock(return_value=[])

        quotes = {}
        for row in live_rows:
            px = float(row["entry_price"])
            quotes[row["symbol"]] = {
                "price": round(px * 0.995, 4), "last_price": round(px * 0.995, 4),
                "day_high": round(px * 1.02, 4), "day_low": round(px * 0.98, 4),
                "lv3_bid": round(px * 0.994, 4), "lv3_ask": round(px * 0.996, 4),
                "bid_price": round(px * 0.994, 4), "ask_price": round(px * 0.996, 4),
                "volume_ratio": 2.0, "volume_5d_avg_ratio": 2.0, "atr": px * 0.04,
                "source": "frozen_stub", "source_asof": datetime.now(ET).isoformat(),
                "timestamp": datetime.now(ET).timestamp(),
            }
        scheduler = SimpleNamespace(data_router=SimpleNamespace(
            get_full_market_context=Mock(side_effect=lambda s: dict(quotes.get(s, {}))),
            get_batch_quotes=Mock(side_effect=lambda syms: {s: quotes.get(s, {}) for s in syms}),
            get_daily_bars=Mock(return_value=[])))

        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: sealed)
        monkeypatch.setattr(pipeline, "_playbook_cfg",
                            lambda k, d=None: overrides.get(k, real_cfg.get(k, d)))
        monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda *a, **kw: db)
        monkeypatch.setattr("app.playbook.playbook_service.PlaybookService",
                            lambda database=None: FakeService())
        monkeypatch.setattr("app.execution.dual_broker.get_dual_broker", lambda: FakeBroker())
        monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: db_file)
        monkeypatch.setattr("app.risk.capital_safety.get_capital_safety_system",
                            lambda: SimpleNamespace(
                                get_available_budget_for_auto=lambda: 80000.0,
                                get_position_multiplier=lambda: 1.0,
                                get_risk_multiplier=lambda: 1.0,
                                _state=SimpleNamespace(current_equity=100000.0)))
        monkeypatch.setattr("app.us_pipeline.emergency_interceptor.check_kill_switch",
                            lambda: False)
        monkeypatch.setattr("app.playbook.market_regime.MarketRegimeGate",
                            lambda *a, **kw: SimpleNamespace(assess=lambda: SimpleNamespace(
                                regime=SimpleNamespace(value="RISK_ON"), vix=15.0)))
        monkeypatch.setattr("app.core.di_container.get_di_container",
                            lambda: SimpleNamespace(atr_provider=lambda: SimpleNamespace(
                                get_atr_pct=Mock(return_value=None),
                                get_atr=Mock(return_value=None))))
        monkeypatch.setattr(pipeline, "_today_et_str", lambda: _row_date(conn))
        monkeypatch.setattr(pipeline, "_check_bridge_health_and_alert", Mock())
        monkeypatch.setattr(pipeline, "_check_daily_loss_circuit", lambda today: None)
        monkeypatch.setattr(pipeline, "_resolve_sizing_base", lambda config: 100000.0)
        monkeypatch.setattr(pipeline, "_low_atr_logged", set())
        yield SimpleNamespace(pipeline=pipeline, db=db, conn=conn, scheduler=scheduler,
                              submissions=submissions, violations=violations,
                              rows=rows, live_rows=live_rows, quotes=quotes,
                              db_file=db_file)
        conn.close()

    def test_eligible_rows_reach_sim_broker(self, env):
        """inject 写出的合格行 → 真 trigger → 重定价 → 委托 (伪造 SIM service)。"""
        result = env.pipeline.run_playbook_auto_execute(env.scheduler, force=True)
        assert result is not None and "error" not in result, result
        assert env.violations == []
        assert env.submissions, f"没有任何委托 (result={result})"
        executed_ids = {s["instruction_id"] for s in env.submissions}
        assert executed_ids <= {r["id"] for r in env.live_rows}
        for sub in env.submissions:
            assert sub["acknowledge_risk"] is True
            assert isinstance(sub.get("custom_params"), dict)

    def test_execute_gate_consumes_screening_snapshot(self, env, caplog):
        """P1.2 交棒证据: 行内 atr_pct 为空时, 门读筛选快照同义字段并留下依据。

        派生场景 (显式标注): 把 inject 真写出的行改成"低波快照"(0.01) —— 修前这种行
        因 atr_pct 缺失而 fail-open 恒放行, 修后被拦并记录 basis=candidate_screening。
        """
        import logging
        victim = env.live_rows[0]
        raw = json.loads(victim["raw_instruction_json"]) \
            if isinstance(victim["raw_instruction_json"], str) else dict(victim["raw_instruction_json"])
        raw["candidate_screening"]["screening_atr_ratio"] = 0.01
        # update_instruction_tracking 只认跟踪列白名单 (不含 raw_instruction_json) ⇒
        #   派生场景直接改库, 且显式把行内 atr_pct 清空 (模拟盘前定桶取不到 ATR)。
        env.conn.execute(
            "UPDATE playbook_instructions SET raw_instruction_json=?, atr_pct=NULL WHERE id=?",
            (json.dumps(raw), victim["id"]))
        env.conn.commit()
        blocked = SimpleNamespace(data_router=SimpleNamespace(
            get_full_market_context=Mock(side_effect=lambda s: dict(env.quotes.get(s, {})))))
        with caplog.at_level(logging.INFO):
            assert env.pipeline._check_intraday_trigger(blocked, dict(
                env.db.get_instruction_by_id(victim["id"]))) is False
        assert "basis=candidate_screening" in caplog.text
        assert "US.TEST" in caplog.text

    def test_live_route_never_touched(self, env):
        env.pipeline.run_playbook_auto_execute(env.scheduler, force=True)
        assert env.violations == []


def _row_date(conn) -> str:
    row = conn.execute("SELECT date FROM playbook_instructions ORDER BY id LIMIT 1").fetchone()
    assert row is not None, "临时库里没有 inject 写出的行"
    return str(row[0])
