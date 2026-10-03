"""P2b 参数单点化契约钉 (2026-09-06, PROPOSAL-20260906B §P2b).

2026-10-03 RFC-130 修订 (用户裁「sim和实盘策略一致」):
  原 LIVE 侧 env 门 (RFC061_LIVE_OVERNIGHT_ENABLED / trade_store.cohort_b_skip_allowed /
  trade_store.live_overnight_enabled) 退役 — cohort B 名义资格不再按 source_type 分叉,
  SIM/LIVE 同语义。本文件随之翻面: ①④ 由「LIVE 需 env 显式开」改为「双臂同语义 +
  退役函数不得复活」墓碑; ②③⑤ (总开关/组合语义/接线钉) 语义未变, 逐字保留。

钉住两个单点助手与三处接线的行为:

  ① exit_engine.cohort_b_skip_eligible(cohort, source_type) == (cohort == "B")
     — source_type 不再参与判定 (签名保留, 调用方/矩阵不变)。
  ② exit_engine.overnight_master_switch_on()
     == 退出引擎 / sweeper 两处逐字 `get("overnight_hold_enabled", False)`.
  ③ 三处接线的组合语义 == inline 组合 (含刻意保留的不对称):
     - poll 盖章点不叠总开关 (引擎在 meta["_skip_allowed"] 读取处施加);
     - 引擎 meta 缺失时的默认公式 (B or long) ≠ poll/sweeper 盖章公式 (B).
  ④ 退役墓碑: trade_store 不得再生 cohort_b_skip_allowed / live_overnight_enabled;
     全仓代码面不得再读 RFC061_LIVE_OVERNIGHT_ENABLED。
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


# ── ① 名义资格助手 == (cohort == "B"), 双臂同语义 (全矩阵) ──────────────

_COHORTS = ["", "A", "B", "b"]           # 含小写 b: 公式区分大小写, 不得"顺手修"
_SOURCES = ["PLAYBOOK_SIM", "PLAYBOOK_LIVE", "ORB_ENGINE", "MAIN_PIPELINE", ""]


@pytest.mark.parametrize("cohort", _COHORTS)
@pytest.mark.parametrize("src", _SOURCES)
def test_eligible_equals_cohort_b_only(cohort, src):
    """RFC-130: source_type 不参与判定 — 任何源下 cohort B 即有名义资格。

    净公式与删除前一致: 旧 `cohort=="B" and cohort_b_skip_allowed(src)`, 而
    cohort_b_skip_allowed 对非 LIVE 源恒 True、对 LIVE 由 env 门控 (默认 False);
    对齐后 env 门删除 ⇒ 恒 `cohort=="B"`。ORB/MAIN_PIPELINE 行 cohort 恒 "A"
    (assign_cohort 只对 playbook 双臂赋值), 落在非 B 分支 = False, 与旧行为同。
    """
    assert cohort_b_skip_eligible(cohort, src) == bool(cohort == "B")


def test_sim_live_arms_identical():
    """一致性正面钉: 同一 cohort 下 SIM 与 LIVE 判定逐字相同 (RFC-130 核心语义)."""
    for cohort in _COHORTS:
        assert cohort_b_skip_eligible(cohort, "PLAYBOOK_SIM") == \
               cohort_b_skip_eligible(cohort, "PLAYBOOK_LIVE"), \
               f"cohort={cohort}: SIM/LIVE 分叉复活"


def test_live_env_no_longer_matters(monkeypatch):
    """旧 env 门已退役: 设不设 RFC061_LIVE_OVERNIGHT_ENABLED 结果不变 (恒 cohort==B)."""
    monkeypatch.delenv("RFC061_LIVE_OVERNIGHT_ENABLED", raising=False)
    off = cohort_b_skip_eligible("B", "PLAYBOOK_LIVE")
    monkeypatch.setenv("RFC061_LIVE_OVERNIGHT_ENABLED", "1")
    on = cohort_b_skip_eligible("B", "PLAYBOOK_LIVE")
    assert off is True and on is True


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


# ── ③ 组合语义: 三处接线 == inline 组合 ─────────────────────────────────

@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("cohort", ["A", "B"])
@pytest.mark.parametrize("src", ["PLAYBOOK_SIM", "PLAYBOOK_LIVE"])
def test_poll_stamp_composition(monkeypatch, master, cohort, src):
    """poll 盖章点: 只有名义资格公式, 无总开关 (既有不对称, 刻意保留).

    _skip_allowed = cohort_b_skip_eligible(cohort, src)
    总开关由引擎在读取 meta["_skip_allowed"] 时施加 — 与本点无关.
    """
    expected = bool(cohort == "B")
    assert cohort_b_skip_eligible(cohort, src) == expected
    # master 无关性: 无论开关状态, 本点结果不变 (开关不在这条链路)
    monkeypatch.setattr("app.playbook.config.get_playbook_config",
                        lambda: {"overnight_hold_enabled": master})
    assert cohort_b_skip_eligible(cohort, src) == expected


@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("cohort", ["A", "B"])
def test_sweeper_composition(monkeypatch, master, cohort):
    """sweeper: 名义资格 ∧ 总开关."""
    monkeypatch.setattr("app.playbook.config.get_playbook_config",
                        lambda: {"overnight_hold_enabled": master})
    expected = bool(cohort == "B") and master
    new = cohort_b_skip_eligible(cohort, "PLAYBOOK_SIM")
    if not overnight_master_switch_on():
        new = False
    assert new == expected


@pytest.mark.parametrize("master", [True, False])
@pytest.mark.parametrize("stamped", [None, True, False])
def test_engine_read_composition(monkeypatch, master, stamped):
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
    """不对称钉: 引擎默认公式 (B or long) 比 poll 盖章公式 (B) 宽.

    cohort A + horizon long 的仓: 引擎 meta 缺失时默认允许跳过, poll 盖章恒 False.
    该差异是合并前就存在的既有行为, P2b/RFC-130 均保留不"修".
    """
    assert cohort_b_skip_eligible("A", "PLAYBOOK_SIM") is False     # poll 口径
    engine_default = ("A" == "B" or "long" == "long")               # 引擎默认口径
    assert engine_default is True


# ── ④ 退役墓碑: LIVE env 门不得复活 ─────────────────────────────────────

def test_retired_functions_not_regenerated():
    """trade_store 不得再生 cohort_b_skip_allowed / live_overnight_enabled.

    这两个函数的唯一分叉 = LIVE 侧 env 门; RFC-130 对齐后恒 True = 死门, 已删。
    复活 = 重新引入 SIM/LIVE 策略分叉, 违用户裁决。
    """
    assert not hasattr(trade_store, "cohort_b_skip_allowed"), \
        "cohort_b_skip_allowed 已退役 (RFC-130), 不得复活"
    assert not hasattr(trade_store, "live_overnight_enabled"), \
        "live_overnight_enabled 已退役 (RFC-130), 不得复活"


def test_env_gate_not_read_anywhere_in_code():
    """全仓代码面 (app/scripts/ops) 不得再读 RFC061_LIVE_OVERNIGHT_ENABLED.

    注释/文档提及允许 (留痕), 代码读取 (os.environ/os.getenv) 禁止。
    """
    hits = []
    for root in ("app", "scripts", "ops"):
        base = REPO / root
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "RFC061_LIVE_OVERNIGHT_ENABLED" in line and ("environ" in line or "getenv" in line):
                    hits.append(f"{p.relative_to(REPO)}:{i}")
    assert not hits, f"env 门读取复活: {hits}"


def test_w_week_exempt_dual_arm_whitelist(monkeypatch):
    """W_WEEK 豁免 source 门 = 双臂白名单 (SIM/LIVE 进, SHADOW/其他不进).

    直接调本体 (overnight_decision.w_week_loser_exempt), 配置开关打桩开启,
    用「今日入场 + 水下」构造其余条件全过的场景, 单钉 source 门语义。
    """
    from app.playbook import overnight_decision as od
    from app.utils.timezone import now_et

    cfg = {"loser_overnight_enabled": True,
           "loser_overnight_max_depth_pct": 99,
           "loser_overnight_max_hold_days": 5}
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: cfg)

    entry = now_et().replace(hour=11, minute=30).isoformat()
    assert od.w_week_loser_exempt("PLAYBOOK_SIM", entry, -0.8) is True
    assert od.w_week_loser_exempt("PLAYBOOK_LIVE", entry, -0.8) is True, \
        "RFC-130: LIVE 臂必须继承 W_WEEK 豁免 (与 SIM 同语义)"
    assert od.w_week_loser_exempt("PLAYBOOK_SHADOW", entry, -0.8) is False
    assert od.w_week_loser_exempt("MAIN_PIPELINE", entry, -0.8) is False
    assert od.w_week_loser_exempt("", entry, -0.8) is False


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


def test_no_live_env_gate_residual_in_exit_chain():
    """退出链四文件代码面不得残留退役函数调用 — AST 只扫真 Call 节点.

    (docstring/注释里的历史公式引文允许留痕; 正则扫行会把引文误报 —
    2026-10-03 本测试首版即误报 exit_engine docstring 一次, 改 AST。)
    """
    import ast
    files = ["app/playbook/exit_engine.py", "app/playbook/overnight_decision.py",
             "app/risk/exit_rules/pre_close.py", "app/playbook/pipeline.py",
             "app/storage/trade_store.py"]
    retired = {"cohort_b_skip_allowed", "live_overnight_enabled"}
    for rel in files:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                assert fname not in retired, \
                    f"{rel}:{node.lineno} 残留退役函数调用 {fname}() (RFC-130)"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    assert alias.name not in retired, \
                        f"{rel}:{node.lineno} 残留退役函数 import (RFC-130)"
