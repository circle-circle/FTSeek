"""Tests for app.playbook.feishu_push — 飞书推送"""
import pytest
import sqlite3
from unittest.mock import patch, MagicMock

from app.playbook.db import PlaybookDB
from app.playbook.feishu_push import PlaybookPusher


@pytest.fixture(autouse=True)
def _admission_pass(monkeypatch):
    """本文件测推送格式, 不测 admission 门 ⇒ 落库门放行。

    2026-11-05 (J4 清账): fixture 名义额 100×50 = $5,000 低于现役地板 $5,500,
    `admission.apply` 把 BUY 行改写成 SKIP ⇒ 盘前/盘后卡片里该标的整段消失,
    7 例长期红 (先例 tests/test_playbook_db.py:765)。真门由 tests/test_admission_gate.py
    等覆盖。
    """
    monkeypatch.setattr(
        "app.playbook.admission.apply",
        lambda inst: (inst, {"verdict": "PASS", "reasons": [], "enforce": False}))
    monkeypatch.setattr(
        "app.playbook.admission.evaluate",
        lambda inst: {"verdict": "PASS", "reasons": [], "metrics": {}})


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def pusher(db):
    return PlaybookPusher(playbook_db=db)


def _save_inst(db, **kwargs):
    defaults = {
        "date": "2026-06-18",
        "symbol": "US.NTES",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 50,
        "confidence": 72,
        "risk_amount": 250.0,
        "risk_reward": 2.0,
        "conditions": ["above_ma20", "insider_cluster_buy"],
    }
    defaults.update(kwargs)
    return db.save_instruction(defaults)


class TestMorningPush:
    def test_format_morning_message(self, pusher, db):
        """盘前消息格式正确"""
        _save_inst(db, symbol="US.NTES", confidence=72)
        _save_inst(db, symbol="US.META", direction="SKIP", skip_reason="财报3天内")

        instructions = db.get_instructions_by_date("2026-06-18")
        regime_info = {"regime": "RISK_ON", "vix": 18.5, "spy_price": 542}

        message = pusher._format_morning_message("2026-06-18", instructions, regime_info)

        assert "2026-06-18" in message
        assert "RISK_ON" in message
        assert "US.NTES" in message
        assert "BUY LONG" in message
        # 2026-08-21 (A方案): conf 伪概率数字已移除 (回放实锤零残余信号+零决策权重),
        # 只展示辩论语义标签. 本 fixture 无 debate_outcome → 无语义标签 🎚.
        assert "72%" not in message
        assert "conf" not in message
        assert "🎚" in message
        assert "META" in message
        assert "财报" in message

    def test_morning_skip_grouped_by_reason(self, pusher, db):
        """2026-10-20 信噪比: 跳过的按原因聚合 — 批量同类折叠为计数, 小批门拦截保留 symbol"""
        _save_inst(db, symbol="US.NTES", confidence=72)
        # 10 只同类占位候选 (bulk: 应折叠为 ×10, 不逐只列明细)
        for i in range(10):
            _save_inst(db, symbol=f"US.BULK{i}", direction="SKIP",
                       skip_reason="非当前通道候选(bridge未提名/无方向)")
        # 1 只真实门拦截 (小批: 应列 symbol)
        _save_inst(db, symbol="US.AVGO", direction="SKIP",
                   skip_reason="entry_confirm_reversed")

        instructions = db.get_instructions_by_date("2026-06-18")
        regime_info = {"regime": "CAUTIOUS", "vix": 14.5, "spy_price": 766}
        message = pusher._format_morning_message("2026-06-18", instructions, regime_info)

        assert "跳过 11 只" in message
        # bulk 折叠: 取 "(" 前缀归并, 只给计数, 不逐只列
        assert "非当前通道候选 ×10" in message
        assert "US.BULK0" not in message
        # 小批门拦截: 保留 symbol
        assert "entry_confirm_reversed ×1" in message
        assert "US.AVGO" in message

    def test_morning_skip_holiday(self, pusher):
        """假日不推送"""
        result = pusher.push_morning_instructions("2026-06-19")  # Juneteenth
        assert result is False

    def test_morning_skip_no_instructions(self, pusher):
        """无指令不推送"""
        result = pusher.push_morning_instructions("2020-01-02")
        assert result is False

    def test_morning_push_sends(self, pusher, db):
        """有指令时推送"""
        _save_inst(db)

        with patch.object(pusher, '_send', return_value=True) as mock_send:
            result = pusher.push_morning_instructions("2026-06-18")
            assert result is True
            mock_send.assert_called_once()


class TestEveningPush:
    def test_format_evening_message(self, pusher, db):
        """盘后消息格式正确"""
        inst_id = _save_inst(db, symbol="US.NTES")
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.5,
            "exit_price": 103.5,
            "pnl": 150.0,
            "outcome": "win",
            "max_favorable_excursion": 200.0,
            "max_adverse_excursion": -50.0,
        })

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "2026-06-18" in message
        assert "US.NTES" in message
        assert "✅ 执行" in message
        assert "MFE" in message
        assert "净 +$150 | 毛 +$150 | 费 $0" in message
        assert "执行 1/1 (SKIP 0)" in message

    def test_evening_not_executed(self, pusher, db):
        """未触发的指令显示 '⏸ 未触发' (2026-07: 未执行拆分为 未触发/触发未执行 两态)"""
        _save_inst(db)

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "⏸ 未触发(未满足触发条件)" in message
        assert "未满足盘中触发条件" in message

    def test_evening_not_executed_shows_invalidation_reason(self, pusher, db):
        """未触发且已作废时显示具体拒因, 不再误写成“价格未达”"""
        inst_id = _save_inst(db)
        db.update_instruction_tracking(inst_id, {
            "skip_reason": "thesis_drift_invalidate",
        })

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "thesis_drift_invalidate" in message
        assert "未达入场价" not in message

    def test_evening_triggered_not_executed(self, pusher, db):
        """已触发但未执行的指令显示 '❌ 触发未执行' (真正错失)"""
        inst_id = _save_inst(db)
        db.update_instruction_tracking(inst_id, {"triggered": 1})

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "❌ 触发未执行" in message

    def test_evening_capture_separates_gross_net_and_loss(self, pusher):
        """退出质量拆毛/净捕获; 亏损单不再输出负百分比"""
        instructions = [
            {
                "symbol": "US.WIN", "direction": "BUY", "executed": 1,
                "outcome": "win", "execution_price": 100.0, "exit_price": 110.0,
                "position_size": 10, "pnl": 80.0,
                "max_favorable_excursion": 100.0, "horizon": "short",
            },
            {
                "symbol": "US.LOSS", "direction": "BUY", "executed": 1,
                "outcome": "loss", "execution_price": 100.0, "exit_price": 90.0,
                "position_size": 10, "pnl": -120.0,
                "max_favorable_excursion": 20.0,
                "max_adverse_excursion": 100.0, "horizon": "short",
            },
        ]
        message = pusher._format_evening_message("2026-08-30", instructions)

        assert "US.WIN: 毛100% / 净80%" in message
        assert "US.LOSS: N/A(亏损单)" in message
        assert "-600%" not in message
        assert "中位捕获率: 毛100% / 净80%" in message

    def test_evening_summary_separates_closed_and_holding_pnl(self, pusher):
        """总结分母只算可执行指令, 已平/持仓 PnL 分开口径"""
        instructions = [
            {
                "symbol": "US.CLOSED", "direction": "BUY", "executed": 1,
                "outcome": "win", "execution_price": 100.0, "exit_price": 110.0,
                "position_size": 10, "pnl": 25.0, "horizon": "short",
            },
            {
                "symbol": "US.HOLDING", "direction": "BUY", "executed": 1,
                "outcome": "holding", "execution_price": 100.0,
                "position_size": 10, "pnl": -10.0, "horizon": "short",
            },
            {"symbol": "US.SKIP", "direction": "SKIP", "outcome": "skipped"},
        ]
        message = pusher._format_evening_message("2026-08-30", instructions)

        assert "执行 2/2 (SKIP 1)" in message
        assert "已平净利 +$25" in message
        assert "持仓浮盈亏 -$10(未实现)" in message
        assert "合计 +$15" in message


class TestTrappedEntryCushionDefault:
    """TZ-3 (DEB-20260830-timezone-duplication-audit): cushion 兜底必须对齐 yaml 真值。

    病根: get 默认 / or 兜底 / except 兜底是 yaml 旧值的过期拷贝 → 降级路径阈值漂移。
    2026-09-09 (DEB-20260909 行动项C, 用户裁"同意"): 真值 0.4→0.15 并收敛共享常量
    BE_FALLBACK_FEE_CUSHION_PCT (0.4+slip0.2>be0.4 破几何); 对齐纪律不变。
    判别式: notional $5000 的单 — 新线 0.15%=$7.5: MFE=$5 拦截, MFE=$15 放行
    ($15<$20 旧线, 同时钉死"回退到 0.4"变异)。
    """

    @staticmethod
    def _closed_inst(mfe=15.0):
        return {
            "symbol": "US.TEST", "direction": "BUY", "executed": 1, "outcome": "loss",
            "execution_price": 100.0, "position_size": 50,
            "max_favorable_excursion": mfe, "max_adverse_excursion": -30.0,
            "pnl": -50.0, "horizon": "short",
        }

    def test_missing_key_falls_back_to_yaml_truth(self, pusher):
        """yaml 缺键/配置读成空 → 阈值 0.15 (共享常量), MFE=$5(<$7.5线) 被拦截"""
        with patch("app.playbook.config.get_playbook_config", return_value={}):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=5.0)])
        assert "入场即套牢" in msg
        assert "阈值 0.15%" in msg

    def test_config_exception_falls_back_to_constant(self, pusher):
        """get_playbook_config 抛异常 → except 兜底同样 0.15 (三处默认一致)"""
        with patch("app.playbook.config.get_playbook_config", side_effect=RuntimeError("boom")):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=5.0)])
        assert "入场即套牢" in msg
        assert "阈值 0.15%" in msg

    def test_above_floor_not_trapped(self, pusher):
        """MFE=$15 > 新线 $7.5 → 不报套牢 (防'永远拦截'反向变异; $15<$20旧线兼防回退0.4)"""
        with patch("app.playbook.config.get_playbook_config", return_value={}):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=15.0)])
        assert "入场即套牢" not in msg

    def test_yaml_truth_equals_shared_constant(self):
        """生产口径自证: 活配置读到 0.15 == yaml 真值 == 共享常量 → 生产/降级同阈值"""
        from app.playbook.config import BE_FALLBACK_FEE_CUSHION_PCT, get_playbook_config
        import yaml as _yaml
        from pathlib import Path as _Path
        raw = _yaml.safe_load(open(_Path(__file__).resolve().parent.parent / "config" / "unified_config.yaml"))
        assert raw["playbook"]["execution_parameters"]["breakeven_fee_cushion_pct"] == 0.15
        assert get_playbook_config().get("breakeven_fee_cushion_pct") == 0.15
        assert BE_FALLBACK_FEE_CUSHION_PCT == 0.15


class TestHelpers:
    def test_parse_conditions_list(self):
        result = PlaybookPusher._parse_conditions(["a", "b"])
        assert result == ["a", "b"]

    def test_parse_conditions_json_string(self):
        result = PlaybookPusher._parse_conditions('["a", "b"]')
        assert result == ["a", "b"]

    def test_parse_conditions_none(self):
        result = PlaybookPusher._parse_conditions(None)
        assert result == []

    def test_parse_conditions_invalid(self):
        result = PlaybookPusher._parse_conditions("invalid")
        assert result == []
