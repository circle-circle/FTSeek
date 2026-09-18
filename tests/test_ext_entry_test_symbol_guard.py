#!/usr/bin/env python3
"""占位标的守卫 + REAL 拦截推送去重 (2026-09-18 用户反馈"一直持续推送 US.TEST").

背景: 券商 SIM 账户里存有**报废测试单** (US.TEST order 8185070, remark ext_entry, 9/17 建单已 CANCELLED),
对账路径会反复碰到它; 而 `_push` 无去重 ⇒ 同一句"🚫 延长时段开仓拦截: US.TEST REAL未授权"每轮都推。
修复: ① 占位标的 (TEST/US.TEST/*.TEST) 在 `_execute` 源头挡掉(不推告警) ② 该拦截推送一日一次。
拦截行为本身不变 (仍返 real_not_authorized)。
"""
import importlib.util
import pathlib
import sys

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
    calls = []
    monkeypatch.setattr(mod, "_push", lambda t: calls.append(t))
    return calls


def test_test_symbol_skipped_and_silent_even_in_real(tmp_path, monkeypatch):
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")          # 即便 REAL 通道, 占位标的也不该推
    for sym in ("US.TEST", "TEST", "US.FAKE.TEST"):
        r = mod._execute(sym, 1, 1.0, "premarket", 1.0, 1_000_000, None, False)
        assert r == {"ok": False, "skip": "test_symbol"}, r
    assert calls == [], f"占位标的不得触发告警, 实推 {calls}"


def test_real_block_push_deduped_once_per_et_day(tmp_path, monkeypatch):
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    r1 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    r2 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    r3 = mod._execute("US.AMD", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    assert r1["skip"] == r2["skip"] == r3["skip"] == "real_not_authorized"   # 拦截行为不变
    assert len(calls) == 2, f"同标的一日只推一次 + 不同标的各一次, 实推 {len(calls)}"
    assert "US.NVDA" in calls[0] and "US.AMD" in calls[1]


def test_push_once_is_per_day(tmp_path, monkeypatch):
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    assert mod._push_once("k1", "a") is True
    assert mod._push_once("k1", "a") is False            # 同日再推被抑制
    assert mod._push_once("k2", "b") is True
    assert calls == ["a", "b"]

def test_probe_mode_silences_push_and_keeps_dedup_slot(tmp_path, monkeypatch):
    """`FUTSEEK_PROBE=1` ⇒ 不推、也**不写去重键**（2026-09-18 §23.5）。

    病灶: 用户看到的"连续推送虚拟标的"有一类来自**人工/探针以 REAL 环境跑生产脚本**
    （生产 cron 未设 FUTU_TRD_ENV = 默认 SIMULATE, 审计里也没有占位行）。
    探针若占掉当日去重名额, 反而会把生产的真告警静默掉 —— 所以探针模式连键都不写。
    """
    mod = _load()
    calls = _arm(mod, tmp_path, monkeypatch)
    monkeypatch.setenv("FUTSEEK_PROBE", "1")
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")

    r1 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    r2 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)

    assert r1["skip"] == r2["skip"] == "real_not_authorized"   # 拦截行为不变
    assert calls == [], f"探针模式不得推送, 实推 {calls}"
    assert not (tmp_path / "dedup.json").exists(), "探针不得占生产去重名额"

    # 摘掉探针 ⇒ 生产路径行为立刻恢复（同日同标的仍能推一次）
    monkeypatch.delenv("FUTSEEK_PROBE")
    r3 = mod._execute("US.NVDA", 1, 100.0, "premarket", 1.0, 1_000_000, None, False)
    assert r3["skip"] == "real_not_authorized"
    assert len(calls) == 1 and "US.NVDA" in calls[0]
