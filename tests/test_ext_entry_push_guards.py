#!/usr/bin/env python3
"""延长时段开仓链路的**推送出口守卫** (2026-09-19 推送污染收口).

事故实证 (用户收到的富卡 → 逐项等于单测 fixture):
  `scripts/extended_hours_entry_executor.py::_push_filled` 的建仓成交富卡, 内容与
  `tests/test_extended_hours_order_execution.py:136-141` 的 `US.FAKE 100@10.0 / 盘前 +3.00% /
  EXT 量 300,000 / 财报超预期 +10.0% / SL $9.50` 一字不差 —— 生产链不可能产生 (trade_records /
  open_positions / logs 三处 US.FAKE 均 0 次), 即**出口被测试/演练标的污染**。

两个真缺口与对应修法 (本文件逐个钉住):
  ① `_push_filled` 不走 `_push_once` 去重 ⇒ 同一笔成交被反复演练即可反复外发;
  ② 静默只看 `FUTSEEK_PROBE=1`, 而 pytest 跑法**绕过 conftest 出口封锁**时 (`--noconftest` /
     裸 import 测试模块) 没有任何静默 —— 现在 `_probe_mode()` 直接把"进程内在跑 pytest"算作静默。
"""
import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name="ext_entry_push_guards_mod"):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── ① pytest 进程内默认静默 (且生产进程行为零变化) ────────────────────────────

def test_probe_mode_true_within_pytest_itself():
    """本用例本身就在 pytest 进程内 ⇒ 静默判据必须为真 (否则 --noconftest 跑法会真发)。"""
    mod = _load()
    assert mod._probe_mode() is True


def test_probe_mode_unchanged_in_production_process(tmp_path):
    """生产路径零变化: 既无 FUTSEEK_PROBE、也没有 pytest 的独立进程中, 判据必须为假。

    这是本改动的**放行方向**验证 (只验拦截方向 = 可能把生产告警永久静默掉): 用子进程
    起干净解释器 import 本脚本, 断言 False; 再带 FUTSEEK_PROBE=1 断言 True。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.pop("FUTSEEK_PROBE", None)
    code = ("import importlib.util,pathlib,sys;"
            "sys.path.insert(0, r'%s');"
            "spec=importlib.util.spec_from_file_location('m', r'%s/scripts/extended_hours_entry_executor.py');"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "print(m._probe_mode())" % (ROOT, ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, timeout=90, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "False", f"生产进程判据被改动: {out.stdout!r}"
    env["FUTSEEK_PROBE"] = "1"
    out2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=90, cwd=str(ROOT))
    assert out2.stdout.strip() == "True", f"显式探针开关失效: {out2.stdout!r}"


# ── ② 成交富卡: 探针静默 + 每 ET 日一次 ───────────────────────────────────────

def test_push_filled_silent_under_probe_and_writes_no_key(tmp_path, monkeypatch):
    """探针态下富卡不外发, 且**不写去重键** (否则演练会吃掉生产当日的真告警名额)。"""
    mod = _load("m_probe_filled")
    dedup = tmp_path / "dedup.json"
    monkeypatch.setattr(mod, "_PUSH_DEDUP", dedup)
    monkeypatch.setattr(mod, "_probe_mode", lambda: True)
    assert mod._push_filled("US.FAKE", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000,
                            {"initial_sl": 9.5}, "pos-x") is False
    assert not dedup.exists(), "探针态不得写去重键"


def test_push_filled_deduped_per_et_day_but_reentry_not_blocked(tmp_path, monkeypatch):
    """同一持仓 (`symbol:pos_id`) 每 ET 日只发一次; **同标的再入场 (新 pos_id) 照发**。"""
    mod = _load("m_dedup_filled")
    monkeypatch.setattr(mod, "_PUSH_DEDUP", tmp_path / "dedup.json")
    monkeypatch.setattr(mod, "_probe_mode", lambda: False)
    sent = []

    class _Hub:
        def push(self, msg):
            sent.append(msg)
            return True

    import app.push.message as _msg
    monkeypatch.setattr(_msg, "trade_entry", lambda **kw: kw)      # 富卡构造与被测去重无关
    import app.push as _ap
    monkeypatch.setattr(_ap, "get_push_hub", lambda: _Hub(), raising=False)

    args = ("US.NVDA", 10, 100.0, 100.0, "premarket", {}, 4.0, 500000,
            {"initial_sl": 95.0})
    first = mod._push_filled(*args, "pos-1")
    second = mod._push_filled(*args, "pos-1")          # 同一笔成交重放 → 收敛
    reentry = mod._push_filled(*args, "pos-2")         # 同日再入场 (新持仓) → 必须照发
    assert (first, second, reentry) == (True, False, True), (first, second, reentry)
    assert len(sent) == 2, f"实发 {len(sent)} 条, 期望 2 (pos-1 一次 + pos-2 一次)"
    keys = __import__("json").loads((tmp_path / "dedup.json").read_text())
    assert keys.get("ext_entry_filled:US.NVDA:pos-1") == mod._today_et()


def test_push_filled_key_survives_hub_exception(tmp_path, monkeypatch):
    """出口异常不得变成"无限重试刷屏": 占位在建仓侧先落, 且函数永不抛 (通知不阻断建仓)。"""
    mod = _load("m_hub_raise")
    monkeypatch.setattr(mod, "_PUSH_DEDUP", tmp_path / "dedup.json")
    monkeypatch.setattr(mod, "_probe_mode", lambda: False)

    class _Bad:
        def push(self, msg):
            raise RuntimeError("hub down")

    import app.push as _ap
    monkeypatch.setattr(_ap, "get_push_hub", lambda: _Bad(), raising=False)
    assert mod._push_filled("US.AMD", 5, 50.0, 50.0, "afterhours", {}, 2.0, 100000,
                            {}, "pos-z") is False
    assert mod._push_filled("US.AMD", 5, 50.0, 50.0, "afterhours", {}, 2.0, 100000,
                            {}, "pos-z") is False      # 第二次: 已被去重挡住, 不再打 hub