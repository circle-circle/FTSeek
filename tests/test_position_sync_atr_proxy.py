"""恢复/收养仓 ATR 代理口径契约。"""
from app.core.position_sync import _proxy_atr_from_stop
from app.playbook.playbook_service import _extract_atr_with_basis


def test_recovery_atr_matches_playbook_sl_proxy():
    instruction = {"entry_price": 100.0, "stop_loss": 95.0}
    _, playbook_source = _extract_atr_with_basis(instruction)
    assert playbook_source == "proxy"
    assert _proxy_atr_from_stop(100.0, 95.0) == 5.0


def test_recovery_atr_does_not_halve_stop_distance():
    assert _proxy_atr_from_stop(100.0, 95.0) == 5.0
    assert _proxy_atr_from_stop(100.0, 95.0) != 2.5


def test_recovery_atr_invalid_geometry_is_zero():
    assert _proxy_atr_from_stop(0.0, 95.0) == 0.0
    assert _proxy_atr_from_stop(100.0, 0.0) == 0.0
