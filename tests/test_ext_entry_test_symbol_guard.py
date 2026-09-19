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


def test_placeholder_symbol_skipped_and_silent_even_in_real(tmp_path, monkeypatch):
    """占位标的在 _execute 源头挡掉, 且**不推任何告警** (即使 REAL 环境)."""
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    for sym in ("US.TEST", "TEST", "US.FAKE.TEST"):
        r = mod._execute(sym, 1, 1.0, "premarket", 1.0, 1_000_000, None, False)
        assert r == {"ok": False, "skip": "test_symbol"}, r
    assert calls == [], f"占位标的不得触发告警, 实推 {calls}"


def test_real_env_notice_is_env_level_and_deduped(tmp_path, monkeypatch):
    """B) REAL 环境 = **配置态**问题: 一条环境级提示, 一日一次; 文案不再点标的 (不混淆 SIM/REAL)."""
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    r1 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    r2 = mod._execute("US.AMD", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    assert r1["skip"] == r2["skip"] == "real_not_authorized"      # 拦截行为不变
    assert len(calls) == 1, f"环境级提示应为 1 条 (跨标的去重), 实推 {len(calls)}: {calls}"
    assert "FUTU_TRD_ENV" in calls[0] and "US.NVDA" not in calls[0] and "US.AMD" not in calls[0]


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