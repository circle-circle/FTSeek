"""Tests for app.playbook.feishu_push — 飞书推送"""
import pytest
import sqlite3
from unittest.mock import patch, MagicMock

from app.playbook.db import PlaybookDB
from app.playbook.feishu_push import PlaybookPusher


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

    def test_evening_not_executed(self, pusher, db):
        """未触发的指令显示 '⏸ 未触发' (2026-07: 未执行拆分为 未触发/触发未执行 两态)"""
        _save_inst(db)

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "⏸ 未触发(未达入场价)" in message

    def test_evening_triggered_not_executed(self, pusher, db):
        """已触发但未执行的指令显示 '❌ 触发未执行' (真正错失)"""
        inst_id = _save_inst(db)
        db.update_instruction_tracking(inst_id, {"triggered": 1})

        instructions = db.get_instructions_by_date("2026-06-18")
        message = pusher._format_evening_message("2026-06-18", instructions)

        assert "❌ 触发未执行" in message


class TestTrappedEntryCushionDefault:
    """TZ-3 (DEB-20260830-timezone-duplication-audit): cushion 三处兜底 0.15→0.4 对齐 yaml 真值。

    病根: get 默认 / or 兜底 / except 兜底三处 0.15 是 2026-08 前 yaml 旧值的过期拷贝,
    yaml 真值已 0.4 (playbook.execution_parameters.breakeven_fee_cushion_pct) → 降级路径
    阈值漂移。判别式: notional $5000 的单, MFE=$15 — 旧线 0.15%=$7.5 放行, 新线 0.4%=$20 拦截。
    """

    @staticmethod
    def _closed_inst(mfe=15.0):
        return {
            "symbol": "US.TEST", "direction": "BUY", "executed": 1, "outcome": "loss",
            "execution_price": 100.0, "position_size": 50,
            "max_favorable_excursion": mfe, "max_adverse_excursion": -30.0,
            "pnl": -50.0, "horizon": "short",
        }

    def test_missing_key_falls_back_to_yaml_truth_04(self, pusher):
        """yaml 缺键/配置读成空 → 阈值 0.4, MFE=$15 被拦截 (旧默认 0.15 时放行 → 钉死旧值)"""
        with patch("app.playbook.config.get_playbook_config", return_value={}):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=15.0)])
        assert "入场即套牢" in msg
        assert "阈值 0.40%" in msg

    def test_config_exception_falls_back_to_04(self, pusher):
        """get_playbook_config 抛异常 → except 兜底同样 0.4 (三处默认一致)"""
        with patch("app.playbook.config.get_playbook_config", side_effect=RuntimeError("boom")):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=15.0)])
        assert "入场即套牢" in msg
        assert "阈值 0.40%" in msg

    def test_above_floor_not_trapped(self, pusher):
        """MFE=$25 > 新线 $20 → 不报套牢 (防'永远拦截'反向变异)"""
        with patch("app.playbook.config.get_playbook_config", return_value={}):
            msg = pusher._format_evening_message("2026-08-30", [self._closed_inst(mfe=25.0)])
        assert "入场即套牢" not in msg

    def test_yaml_truth_unchanged_04(self):
        """生产口径自证: 活配置读到 0.4 == yaml 真值 → 生产/降级同阈值 (零行为变化的前提)"""
        from app.playbook.config import get_playbook_config
        import yaml as _yaml
        from pathlib import Path as _Path
        raw = _yaml.safe_load(open(_Path(__file__).resolve().parent.parent / "config" / "unified_config.yaml"))
        assert raw["playbook"]["execution_parameters"]["breakeven_fee_cushion_pct"] == 0.4
        assert get_playbook_config().get("breakeven_fee_cushion_pct", 0.4) == 0.4


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
