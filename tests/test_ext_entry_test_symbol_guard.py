#!/usr/bin/env python3
"""占位标的守卫 + SIM/REAL 不混淆 (2026-09-18 用户令「不要 sim 和 real 有混淆」).

背景(实测): 用户持续收到 "🚫 延长时段开仓拦截: US.FAKE REAL未授权"。逐层定位:
  ① 文案原出口 = extended_hours_entry_executor._execute() 的 REAL 守卫;
  ② `US.FAKE` **不是**券商单、也不是候选股 —— 它只出现在 pytest fixture 里;
  ③ `FUTU_TRD_ENV=REAL` 也只出现在 pytest 里 (tests/test_extended_hours_entry_executor.py:172/182/218),
     且那两个用例**未** mock 推送 ⇒ **每跑一次 pytest 就给生产飞书真发一条**。
处置(两层, 均为治本):
  A) conftest 全局封死测试→生产通知出口 (tests/conftest._block_outbound_notifications) + 默认钉 SIMULATE;
  B) _execute 的 REAL 分支改为**环境级**一次提示 (不再逐标的发"某标的被拦"); 拦截行为不变。
注: 曾试过扩展 is_test_symbol 把 US.FAKE 当占位 (误伤 8 个把 US.FAKE 当正常符号的既有用例) ⇒ 已回退。

2026-09-22 (真下单事故, 券商侧实证): 本文件的 `test_real_env_notice_is_env_level_and_deduped` 曾
**每跑一次就往 SIM 账户真下一单** —— 它用 legacy 键 `FUTU_TRD_ENV=REAL` 驱动 REAL 守卫, 而守卫判据
2026-09-20 已迁到 `TradingMode`(SSOT) ⇒ `is_live=False` ⇒ 守卫被跳过 ⇒ 代码直落 `place_order`。
券商流水: US.NVDA BUY qty=1 @$100 remark=ext_entry, 9/20 三笔 (8191435/8191447/8192065),
9/21 三笔 (8195391/8195420/8195497) —— 全部 CANCELLED_ALL、无成交、无盈亏。
代价不止噪声: 9/21 19:25 ET 那轮生产 afterhours 执行器读到在途单 ⇒ `skip=active_ext_entry_order`
⇒ **压制了该轮真实入场**; 且每轮对账都刷"跨日旧单"日志。
处置 (两层, 均为治本): ①本文件所有用例统一钉 `_no_broker` 桩 (走到券商口即红, 不靠"记得 mock");
②REAL 守卫的**判据**在本文件只用 SSOT (`_live_mode`) 驱动, 并用源码契约钉住"不得读 legacy 键"。
类级: tests/conftest 的 `_block_live_broker_orders` 已在真 `OpenSecTradeContext` 写口装挡板。
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _arm(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_PUSH_DEDUP", tmp_path / "dedup.json")
    # 2026-09-19: 本文件的用例测的是**去重与推送语义**, 必须在"非探针"分支上跑 ——
    #   而 `_probe_mode()` 现在对 pytest 进程默认 True (pytest→静默, 见 executor._probe_mode),
    #   不显式关掉就没有真去重键可断言 (那是静默分支, 不是被测行为)。
    monkeypatch.setattr(mod, "_probe_mode", lambda: False)
    calls = []
    monkeypatch.setattr(mod, "_push", lambda t: calls.append(t))
    return calls


def _no_broker(mod, monkeypatch):
    """类级纪律: 本文件用例**永不**触达券商下单口 (2026-09-22 真下单事故后固化)。

    不靠"记得 mock": 任何走到 `_open_trade_context()` 的用例当场红并点名。需要券商行为的
    用例必须显式钉 `FakeTradeContext` (先例: tests/test_extended_hours_order_execution.py:115),
    让"我确实在打桩"这件事写在用例里。
    """
    def _forbidden():
        raise AssertionError(
            "本文件用例不得触达券商 (_open_trade_context): 需要券商行为请显式钉 FakeTradeContext; "
            "2026-09-22 事故 = 少了这一步, 用例真往 SIM 账户下了 NVDA/AMD 单"
        )

    monkeypatch.setattr(mod, "_open_trade_context", _forbidden)


def _live_mode(mod, monkeypatch):
    """REAL 守卫的**判据** = SSOT (`TradingMode.is_live`), 不是 legacy `FUTU_TRD_ENV`。

    钉住真源而非环境变量: 2026-09-20 SSOT 重构后 `_execute` 只读 `_mode()`, 旧用例 setenv
    legacy 键 = 自以为在测 REAL 分支, 实际守卫完全不看它 ⇒ 直落 place_order。
    """
    class _LiveMode:
        is_live = True

        def account_id(self):
            return 19930823

        def futu_trd_env(self):
            return "REAL"

    monkeypatch.setattr(mod, "_mode", lambda: _LiveMode())
    monkeypatch.setattr(mod, "_ext_real_authorized", lambda: False)


def test_placeholder_symbol_skipped_and_silent_even_in_real(tmp_path, monkeypatch):
    """占位标的在 _execute 源头挡掉, 且**不推任何告警** (即使 SSOT=实盘)."""
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    _live_mode(mod, monkeypatch)
    _no_broker(mod, monkeypatch)
    for sym in ("US.TEST", "TEST", "US.FAKE.TEST"):
        r = mod._execute(sym, 1, 1.0, "premarket", 1.0, 1_000_000, None, False)
        assert r == {"ok": False, "skip": "test_symbol"}, r
    assert calls == [], f"占位标的不得触发告警, 实推 {calls}"


def test_real_env_notice_is_env_level_and_deduped(tmp_path, monkeypatch):
    """B) REAL 环境 = **配置态**问题: 一条环境级提示, 一日一次; 文案不再点标的 (不混淆 SIM/REAL)."""
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    _live_mode(mod, monkeypatch)
    _no_broker(mod, monkeypatch)
    r1 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    r2 = mod._execute("US.AMD", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    assert r1["skip"] == r2["skip"] == "real_not_authorized"      # 拦截行为不变
    assert len(calls) == 1, f"环境级提示应为 1 条 (跨标的去重), 实推 {len(calls)}: {calls}"
    assert "EXT_REAL_AUTHORIZED" in calls[0] and "US.NVDA" not in calls[0] and "US.AMD" not in calls[0]


def test_real_guard_reads_ssot_not_legacy_env_key():
    """源码契约: `_execute` 的 REAL 守卫判据必须是 SSOT, 不得读 legacy `FUTU_TRD_ENV`。

    这是 2026-09-22 真下单事故的**根因钉** —— 判据在 SSOT 重构中被迁走, 而用例仍在驱动 legacy 键,
    于是守卫永不触发、用例静默变成"真下单 + 断言失败"。任何人把 legacy 键加回守卫即红。
    """
    import inspect

    mod = _load()
    src = inspect.getsource(mod._execute)
    assert "_mode().is_live and not _ext_real_authorized()" in src, (
        "REAL 守卫判据不见了 — 延长时段开仓链路在实盘模式下会直落 place_order"
    )
    assert "FUTU_TRD_ENV" not in src, (
        "守卫/下单路径又读起了 legacy FUTU_TRD_ENV: 它与 SSOT(TradingMode) 不同源, "
        "切实盘时会错配 (2026-09-20 已迁移)"
    )


def test_push_once_is_per_day(tmp_path, monkeypatch):
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    assert mod._push_once("k1", "a") is True
    assert mod._push_once("k1", "a") is False
    assert mod._push_once("k2", "b") is True
    assert calls == ["a", "b"]


@pytest.mark.allow_outbound_notification
def test_conftest_blocks_outbound_notification(monkeypatch):
    """C) 测试环境封死出口: FeishuNotifier.send_text 已被替换为记录桩 (不发网络).

    allow_outbound_notification: 本用例**就是**在验封锁本身 —— 它必须打到渠道层才能断言"打的是桩";
    留痕是预期产物, 故豁免 conftest 的出站留痕报红。
    """
    from app.notifier.feishu import FeishuNotifier
    assert FeishuNotifier().send_text("本句不得外发") is True       # 桩返回 True, 无网络副作用
    assert monkeypatch is not None


def test_conftest_pins_broker_write_port():
    """D) 类级纪律: conftest 已在真券商**写口**装挡板 —— 且本用例不用真下单来验。

    用哑 `self` 调类上的 `place_order`: 挡板装好了 ⇒ 先抛 RuntimeError (触不到网络);
    挡板没装 ⇒ 真实现因 `self` 不是合法 context 而在触达券商之前就失败, 用例照样红,
    **绝不产生订单** (2026-09-22 事故的教训: 验证"会不会真下单"的用例本身就是下单器)。
    """
    import futu

    with pytest.raises(RuntimeError, match="券商写口"):
        futu.OpenSecTradeContext.place_order(object(), price=100.0, qty=1, code="US.NVDA")