"""[RFC-195] 盘前生成 `trade_date` NameError 回归 (11-06 RFC-186b 引入)。

病灶 (2026-11-09 实测): instruction_generator.py:583 传了 `trade_date=trade_date` 实参,
但 `compute_instruction_values` 没有该形参 ⇒ 每轮盘前生成跑到最后一步恒定 NameError:
    playbook.generate 每轮 ~295s 后崩 (今日 16 次), playbook_generate_state 永远无完成标记
    ⇒ 09:15 只能「兜底放行」, 且主循环每轮被这条死步占住 ~5min, 首次 auto_execute 拖到 09:40。
"""
import types

import app.playbook.instruction_generator as ig


def _data(price=100.0, atr=3.0):
    tech = types.SimpleNamespace(
        price=price, bid=price - 0.02, ask=price + 0.02, atr_14=atr
    )
    return types.SimpleNamespace(technical=tech)


def _decision(symbol="US.TEST", direction="BUY"):
    return {
        "symbol": symbol,
        "direction": direction,
        "selected_conditions": [],
        "thesis_template": "unit-test",
    }


def _spy_sizing(monkeypatch):
    """把 sizing 换成记录器再调真实现 — 断言 trade_date 真的被透传给费用模型。"""
    captured = {}
    real = ig.compute_position_size

    def _rec(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ig, "compute_position_size", _rec)
    return captured


def test_compute_instruction_values_no_name_error(monkeypatch):
    """修前: NameError: name 'trade_date' is not defined (整段生成必崩)。"""
    captured = _spy_sizing(monkeypatch)
    values = ig.compute_instruction_values(_decision(), _data(), 100000.0)

    assert values.get("entry_price"), "入口价必须算出 (说明没被 NameError 中断)"
    assert values.get("stop_loss")
    assert captured.get("trade_date"), "trade_date 必须传给 compute_position_size"


def test_trade_date_defaults_to_et_today(monkeypatch):
    from app.utils.timezone import now_et

    captured = _spy_sizing(monkeypatch)
    ig.compute_instruction_values(_decision(), _data(), 100000.0)

    assert str(captured["trade_date"])[:10] == now_et().date().isoformat()


def test_trade_date_explicit_passthrough(monkeypatch):
    captured = _spy_sizing(monkeypatch)
    ig.compute_instruction_values(
        _decision(), _data(), 100000.0, trade_date="2026-11-09"
    )

    assert captured["trade_date"] == "2026-11-09"