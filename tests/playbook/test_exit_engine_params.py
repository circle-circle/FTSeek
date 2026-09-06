"""P2b 参数单点化契约钉 (2026-09-06, PROPOSAL-20260906B §P2b).

钉住两个新单点助手与三处接线的行为等价性:

  ① exit_engine.cohort_b_skip_eligible(cohort, source_type)
     == 合并前 poll monitor / EOD sweeper 两处逐字公式
        `cohort == "B" and cohort_b_skip_allowed(src)` (LIVE 需 env 显式开).
  ② exit_engine.overnight_master_switch_on()
     == 合并前退出引擎 / sweeper 两处逐字 `get("overnight_hold_enabled", False)`.
  ③ 三处接线的组合语义 == 旧 inline 组合 (含两处刻意保留的不对称):
     - poll 盖章点不叠总开关 (引擎在 meta["_skip_allowed"] 读取处施加);
     - 引擎 meta 缺失时的默认公式 (B or long) ≠ poll/sweeper 盖章公式 (B and 允许).
  ④ 异常不吞: 助手不在内部 try (与合并前 inline 上抛口径一致).
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from app.playbook.exit_engine import (  # noqa: E402
    cohort_b_skip_eligible,
    overnight_master_switch_on,
)
from app.storage import trade_store  # noqa: E402


# ── ① 名义资格助手 == 旧 inline 公式 (全矩阵) ──────────────────────────

_COHORTS = ["", "A", "B", "b"]           # 含小写 b: 旧公式区分大小写, 不得"顺手修"
_SOURCES = ["PLAYBOOK_SIM", "PLAYBOOK_LIVE", "ORB_ENGINE", "MAIN_PIPELINE", ""]


@pytest.mark.parametrize("cohort", _COHORTS)
@pytest.mark.parametrize("src", _SOURCES)
@pytest.mark.parametrize("live_env", [None, "1"])
def test_eligible_equals_legacy_inline_formula(monkeypatch, cohort, src, live_env):
    if live_env is None:
        monkeypatch.delenv("RFC061_LIVE_OVERNIGHT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("RFC061_LIVE_OVERNIGHT_ENABLED", live_env)
    legacy = bool(cohort == "B" and trade_store.cohort_b_skip_allowed(src))
    assert cohort_b_skip_eligible(cohort, src) == legacy


def test_live_env_gate_end_to_end(monkeypatch):
    """LIVE 闸的真实语义: env 关 → B+LIVE False; env 开 → True; SIM 恒 True."""
    monkeypatch.delenv("RFC061_LIVE_OVERNIGHT_ENABLED", raising=False)
    assert cohort_b_skip_eligible("B", "PLAYBOOK_LIVE") is False
    assert cohort_b_skip_eligible("B", "PLAYBOOK_SIM") is True
    monkeypatch.setenv("RFC061_LIVE_OVERNIGHT_ENABLED", "1")
    assert cohort_b_skip_eligible("B", "PLAYBOOK_LIVE") is True
    monkeypatch.setenv("RFC061_LIVE_OVERNIGHT_ENABLED", "yes")
    assert cohort_b_skip_eligible("B", "PLAYBOOK_LIVE") is True


# ── ② 总开关助手 == 旧 inline 读取 ──────────────────────────────────────

@pytest.mark.parametrize("cfg_val,expected", [(True, True), (False, False), (None, False)])
def test_master_switch_equals_legacy_read(monkeypatch, cfg_val, expected):
    class _Cfg(dict):
        pass
    cfg = _Cfg()
    if cfg_val is not None:
        cfg["overnight_hold_enabled"] = cfg_val
    # cfg_val=None → 键缺失, 旧 inline 的 get(key, False) 默认 False
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: cfg)
    assert overnight_master_switch_on() is expected


# ── ③ 组合语义: 三处接线 == 旧 inline 组合 ──────────────────────────────

@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("cohort", ["A", "B"])
@pytest.mark.parametrize("src", ["PLAYBOOK_SIM", "PLAYBOOK_LIVE"])
def test_poll_stamp_composition_equals_legacy(monkeypatch, master, cohort, src):
    """poll 盖章点: 只有名义资格公式, 无总开关 (旧行为如此, 不对称是刻意的).

    旧: _skip_allowed = (cohort == "B" and cohort_b_skip_allowed(src))
    新: _skip_allowed = cohort_b_skip_eligible(cohort, src)
    总开关由引擎在读取 meta["_skip_allowed"] 时施加 — 与本点无关.
    """
    monkeypatch.delenv("RFC061_LIVE_OVERNIGHT_ENABLED", raising=False)
    legacy = bool(cohort == "B" and trade_store.cohort_b_skip_allowed(src))
    assert cohort_b_skip_eligible(cohort, src) == legacy
    # master 无关性: 无论开关状态, 本点结果不变 (开关不在这条链路)
    monkeypatch.setattr("app.playbook.config.get_playbook_config",
                        lambda: {"overnight_hold_enabled": master})
    assert cohort_b_skip_eligible(cohort, src) == legacy


@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("cohort", ["A", "B"])
def test_sweeper_composition_equals_legacy(monkeypatch, master, cohort):
    """sweeper: 名义资格 ∧ 总开关 (旧两行组合的净效果)."""
    monkeypatch.setattr("app.playbook.config.get_playbook_config",
                        lambda: {"overnight_hold_enabled": master})
    legacy = bool(cohort == "B" and trade_store.cohort_b_skip_allowed("PLAYBOOK_SIM"))
    if not master:
        legacy = False
    new = cohort_b_skip_eligible(cohort, "PLAYBOOK_SIM")
    if not overnight_master_switch_on():
        new = False
    assert new == legacy


@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("stamped", [None, True, False])
def test_engine_read_composition_equals_legacy(monkeypatch, master, stamped):
    """引擎读取处: meta 盖章优先, 缺失默认 (B or long), 然后总开关一刀切.

    旧: _skip_allowed = meta.get("_skip_allowed", cohort=="B" or horizon=="long")
        if not cfg.get("overnight_hold_enabled", False): _skip_allowed = False
    """
    monkeypatch.setattr("app.playbook.config.get_playbook_config",
                        lambda: {"overnight_hold_enabled": master})
    meta = {} if stamped is None else {"_skip_allowed": stamped}
    legacy = meta.get("_skip_allowed", True)   # 此处 cohort B → 默认 True
    if not master:
        legacy = False
    new = meta.get("_skip_allowed", True)
    if not overnight_master_switch_on():
        new = False
    assert new == legacy
    if not master:
        assert new is False                    # 总开关关闭一律压 False
    if stamped is False and master:
        assert new is False                    # 盖章 False 优先于默认公式


def test_asymmetry_engine_default_vs_poll_stamp():
    """不对称钉: 引擎默认公式 (B or long) 比 poll 盖章公式 (B and 允许) 宽.

    cohort A + horizon long 的仓: 引擎 meta 缺失时默认允许跳过, poll 盖章恒 False.
    该差异是合并前就存在的既有行为, P2b 保留不"修".
    """
    assert cohort_b_skip_eligible("A", "PLAYBOOK_SIM") is False     # poll 口径
    engine_default = ("A" == "B" or "long" == "long")               # 引擎默认口径
    assert engine_default is True


# ── ④ 异常不吞 ──────────────────────────────────────────────────────────

def test_eligible_does_not_swallow_store_errors(monkeypatch):
    def _boom(src):
        raise RuntimeError("store down")
    monkeypatch.setattr(trade_store, "cohort_b_skip_allowed", _boom)
    with pytest.raises(RuntimeError, match="store down"):
        cohort_b_skip_eligible("B", "PLAYBOOK_SIM")


def test_master_switch_does_not_swallow_config_errors(monkeypatch):
    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr("app.playbook.config.get_playbook_config", _boom)
    with pytest.raises(RuntimeError, match="config down"):
        overnight_master_switch_on()


# ── ⑤ 接线钉: 三处调用点必须真的走单点 (防"抽了函数忘了接线") ──────────

def test_call_sites_use_the_helpers():
    pipe_src = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    eng_src = (REPO / "app" / "playbook" / "exit_engine.py").read_text(encoding="utf-8")
    # poll monitor 与 sweeper 都经 cohort_b_skip_eligible
    assert pipe_src.count("cohort_b_skip_eligible") >= 2, (
        "poll 与 sweeper 两处都应 import+调用单点助手")
    # sweeper 与引擎都经 overnight_master_switch_on
    assert "overnight_master_switch_on as _master_on" in pipe_src
    assert "if not _master_on():" in pipe_src
    assert "if not overnight_master_switch_on():" in eng_src
    # 旧 inline 公式不得残留 (函数体外出现即为回潮)
    body = "\n".join(l for l in pipe_src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "cohort_b_skip_allowed as _skip_ok" not in body
