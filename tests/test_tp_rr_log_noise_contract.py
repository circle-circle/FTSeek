"""TP-RR 门禁日志降噪契约 (DEB-20261028).

同一持仓每 ~10 秒重算同一 TP/SL 几何时，INFO 只允许在首次或几何变化时出现；
重复轮询必须是 DEBUG。meta['_tp_rr_gate'] 的内容与写入语义保持不变。
"""

from datetime import date
import logging

import pytest

import app.playbook.config as pb_config_mod
from app.playbook.exit_engine import _check_playbook_exit_rules


class _CfgWithMinTpRr:
    """委托真实配置，只固定 min_tp_rr，隔离生产 yaml 漂移。"""

    def __init__(self, real):
        self._real = real

    def get(self, key, default=None):
        if key == "min_tp_rr":
            return 1.5
        return self._real.get(key, default)

    def get_horizon_top(self, *args, **kwargs):
        return self._real.get_horizon_top(*args, **kwargs)

    def get_horizon_params(self, *args, **kwargs):
        return self._real.get_horizon_params(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(meta):
    return _check_playbook_exit_rules(
        direction="LONG",
        entry_price=100.0,
        current_price=100.1,
        initial_sl=95.0,
        peak_price=100.1,
        meta=meta,
        held_minutes=10,
        minutes_to_close=300,
        minutes_from_open=100,
        entry_time_str=f"{date.today().isoformat()}T10:00:00",
    )


@pytest.fixture()
def real_cfg(monkeypatch):
    real = pb_config_mod.get_playbook_config()
    monkeypatch.setattr(
        pb_config_mod, "get_playbook_config", lambda: _CfgWithMinTpRr(real)
    )
    return real


def test_tp_rr_info_only_on_first_or_geometry_change(real_cfg, caplog):
    meta = {
        "_symbol": "US.TEST",
        "_log_tag": "test",
        "horizon": "short",
        "take_profit": 107.0,  # (107-100)/(100-95)=1.4 < 1.5 ⇒ 吞掉
    }

    with caplog.at_level(logging.INFO, logger="app.playbook.exit_engine"):
        _run(meta)
        first = [r for r in caplog.records
                 if r.name == "app.playbook.exit_engine" and "TP-RR门禁" in r.getMessage()]
        gate_first = dict(meta["_tp_rr_gate"])

        _run(meta)  # 同一几何，不应再 INFO
        same = [r for r in caplog.records
                if r.name == "app.playbook.exit_engine" and "TP-RR门禁" in r.getMessage()]

        meta["take_profit"] = 106.0  # RR=1.2，几何变化 ⇒ 重新 INFO
        _run(meta)
        changed = [r for r in caplog.records
                   if r.name == "app.playbook.exit_engine" and "TP-RR门禁" in r.getMessage()]

    assert len(first) == 1
    assert len(same) == 1, "重复轮询仍在 INFO 刷屏"
    assert len(changed) == 2, "TP/SL/RR 变化后应重新可见"
    assert gate_first["swallowed"] is True
    assert gate_first["tp"] == pytest.approx(107.0)
    assert meta["_tp_rr_gate"]["tp"] == pytest.approx(106.0)
