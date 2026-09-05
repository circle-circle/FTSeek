"""风控分级复权 (REDUCE_50 + 观察期半仓) 接线契约 (2026-09-05, 用户裁"按照建议执行").

被测: app/playbook/playbook_service.py `execute_instruction` 的 `_risk_scale` 通道,
与 app/risk/account_circuit_breaker.py `AccountCircuitBreaker.check` 的四档判定。

立起的失效模式 (两条, 都是"防线在纸面"):
  ① OKR 账户熔断定义了 NORMAL/REDUCE_50/HALT/LIQUIDATE 四档, 而 playbook 此前只拦
     HALT/LIQUIDATE → 日亏 -1%~-2% 区间**什么都不做**, 从"正常"直接跳"停新开仓",
     中间那级零消费方 (全仓 grep REDUCE_50 唯一产出点即 account_circuit_breaker.py)。
  ② 绝对熔断的连亏暂停到期后原本直接全放行 — 没有"先小仓试水"的过渡档。

现语义: 两个半仓来源 (OKR REDUCE_50 / ACB 观察期 get_position_scale) 取 min, 只缩
**新开仓名义**, 不触碰已持仓的 SL/TP/退出。缩放点刻意放在 admission 二次复核**之后**
(与 LIVE_TRIAL/gap_dive 两处半仓先例同序), 以守住"不减少成交笔数"这条红线。

本文件锁什么:
  - 行为: check() 四档边界 (含"恰好 -1%/-2%/-3%"的闭区间方向) 与便捷函数同路径;
  - 结构 (AST, 不用 substring — 注释措辞改动不该误红, 控制流改动必须红):
    _risk_scale 初始化在两闸之前 / REDUCE_50 分支是**非拒单**路径且写 min(...,0.5) /
    ACB 闸内调 get_position_scale("playbook") 并取 min / 缩放应用在 admission 复核之后
    且 max(1, int(...)) 保底 1 股 / 缩放分支本身不含 return。

回滚: 删本文件 + 摘掉 playbook_service 中 _risk_scale 三处 (init / REDUCE_50 / ACB)。
"""
import ast
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.risk.account_circuit_breaker import AccountCircuitBreaker

_SVC = PROJECT_ROOT / "app" / "playbook" / "playbook_service.py"

EQUITY = 100_000.0
TODAY = "2026-09-08"


# ───────────────────────── 行为: OKR 四档边界 ─────────────────────────

@pytest.fixture
def okr(tmp_path, monkeypatch):
    """返回 set_daily_pnl(amount) → equity 100k 口径下的当日已实现净亏."""
    db = tmp_path / "trade_records.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT,
            entry_price REAL, exit_price REAL, quantity INTEGER,
            realized_pnl REAL, pnl_net REAL, fees_est REAL
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "app.risk.account_circuit_breaker.get_trade_records_db", lambda: db)
    monkeypatch.setattr("app.risk.account_circuit_breaker.now_et", lambda: TODAY)
    monkeypatch.setattr(
        "app.risk.account_circuit_breaker._et_trade_date", lambda _dt: TODAY)

    class _State:
        current_equity = EQUITY
        peak_equity = EQUITY          # 回撤 0 → 只由日亏驱动档位

    class _CS:
        _state = _State()

    import app.risk.capital_safety as cs_mod
    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS())

    def set_daily_pnl(amount: float) -> None:
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM trade_records")
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, "
            "exit_time, entry_price, exit_price, quantity, realized_pnl, pnl_net) "
            "VALUES (?, 'US.UBER', 'LONG', ?, ?, 78.0, 77.0, 10, ?, ?)",
            (TODAY, f"{TODAY}T09:40:00-04:00", f"{TODAY}T10:10:00-04:00",
             amount, amount))
        conn.commit()
        conn.close()

    return set_daily_pnl, AccountCircuitBreaker()


def test_normal_tier_when_loss_below_one_pct(okr):
    set_pnl, br = okr
    set_pnl(-500.0)                                  # -0.5%
    assert br.check(EQUITY).action == "NORMAL"


def test_reduce_50_tier_between_one_and_two_pct(okr):
    """核心: -1%~-2% 区间必须落 REDUCE_50 (此前该档零消费方 = 什么都不做)."""
    set_pnl, br = okr
    set_pnl(-1500.0)                                 # -1.5%
    r = br.check(EQUITY)
    assert r.action == "REDUCE_50"
    assert r.daily_pnl_pct == pytest.approx(-0.015)


def test_tier_boundaries_are_closed(okr):
    set_pnl, br = okr
    for amount, expected in ((-1000.0, "REDUCE_50"), (-2000.0, "HALT"),
                             (-3000.0, "LIQUIDATE")):
        set_pnl(amount)
        assert br.check(EQUITY).action == expected, f"{amount} 应落 {expected}"


def test_halt_tier_between_two_and_three_pct(okr):
    set_pnl, br = okr
    set_pnl(-2500.0)                                 # -2.5%
    assert br.check(EQUITY).action == "HALT"


def test_convenience_fn_shares_tier_logic(okr, monkeypatch):
    """playbook_service 走的是 check_circuit_breaker() 便捷函数 (模块级单例)."""
    set_pnl, _ = okr
    set_pnl(-1500.0)
    import app.risk.account_circuit_breaker as acb_mod
    monkeypatch.setattr(acb_mod, "_circuit_breaker_instance", None)
    assert acb_mod.check_circuit_breaker(EQUITY).action == "REDUCE_50"


def test_drawdown_alone_can_reach_liquidate(okr, monkeypatch):
    """回撤档独立于日亏: 日亏 0 但峰值回撤 ≥3% 仍 LIQUIDATE (原有语义不被本批改动)."""
    set_pnl, _ = okr
    set_pnl(0.0)

    class _State:
        current_equity = EQUITY
        peak_equity = EQUITY / 0.965                 # 回撤 3.5%

    class _CS:
        _state = _State()

    import app.risk.capital_safety as cs_mod
    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS())
    assert AccountCircuitBreaker().check(EQUITY).action == "LIQUIDATE"


# ───────────────────────── 结构: AST 锁接线 ─────────────────────────

def _tree() -> ast.Module:
    return ast.parse(_SVC.read_text(encoding="utf-8"), filename=str(_SVC))


def _exec_fn(tree: ast.Module) -> ast.FunctionDef:
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "execute_instruction"]
    assert len(hits) == 1, f"期望唯一 execute_instruction, 实得 {len(hits)}"
    return hits[0]


def _calls(node, func_name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == func_name]


def _attrs_called(node, attr_name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr_name]


def _assigns_to(node, name):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            out.append(n)
    return out


def _consts(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant)}


def _ifs(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.If)]


@pytest.fixture(scope="module")
def fn():
    return _exec_fn(_tree())


def test_risk_scale_initialized_before_both_gates(fn):
    """init 必须先于 OKR 闸与 ACB 闸, 否则两闸写的是未定义名 (NameError→fail-close 拒单)."""
    inits = [a for a in _assigns_to(fn, "_risk_scale")
             if isinstance(a.value, ast.Constant) and a.value.value == 1.0]
    assert len(inits) == 1, "期望唯一 `_risk_scale = 1.0` 初始化"
    init_line = inits[0].lineno
    okr_gate = _calls(fn, "check_circuit_breaker")
    acb_gate = _calls(fn, "get_absolute_circuit_breaker")
    assert okr_gate and acb_gate, "两道熔断闸必须都在 execute_instruction 内"
    assert init_line < min(n.lineno for n in okr_gate)
    assert init_line < min(n.lineno for n in acb_gate)


def test_reduce_50_branch_scales_and_is_not_a_reject(fn):
    """REDUCE_50 分支: 写 min(_risk_scale, 0.5), 且分支内**不得有 return** (半仓≠拒单)."""
    hits = [i for i in _ifs(fn)
            if "REDUCE_50" in _consts(i.test)]
    assert len(hits) == 1, "期望唯一 REDUCE_50 分支"
    br = hits[0]
    writes = [a for a in _assigns_to(br, "_risk_scale")
              if isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name)
              and a.value.func.id == "min" and 0.5 in _consts(a.value)]
    assert writes, "REDUCE_50 必须把 _risk_scale 取 min(..., 0.5)"
    assert not [n for n in ast.walk(br) if isinstance(n, ast.Return)], \
        "REDUCE_50 是半仓档, 不是停新开档 — 分支内出现 return 即回到'纸面档'"


def test_acb_gate_consumes_position_scale_with_min(fn):
    """ACB 闸过闸后必须读观察期缩放 get_position_scale("playbook") 并取 min."""
    hits = [n for n in _attrs_called(fn, "get_position_scale")]
    assert len(hits) == 1, "期望唯一 get_position_scale 消费点"
    call = hits[0]
    assert "playbook" in _consts(call), 'strategy_id 须为 "playbook" (与 can_open_new_position 同键)'
    gate_try = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "get_position_scale"
                        for n in ast.walk(t))]
    assert gate_try, "get_position_scale 必须在 ACB 闸的 try 内 (异常仍走 acb_fail_mode)"
    acb_scale_writes = [a for a in _assigns_to(fn, "_risk_scale")
                        if isinstance(a.value, ast.Call)
                        and isinstance(a.value.func, ast.Name)
                        and a.value.func.id == "min" and 0.5 not in _consts(a.value)]
    assert acb_scale_writes, "ACB 观察期缩放必须以 min() 并入 _risk_scale (两源取更严)"


def test_scaling_applied_after_admission_recheck(fn):
    """缩放点必须在 admission 二次复核之后 — 守住“不因缩名义而新触发拒单”红线."""
    scale_ifs = [i for i in _ifs(fn)
                 if "_risk_scale" in {n.id for n in ast.walk(i.test)
                                      if isinstance(n, ast.Name)}
                 and _assigns_to(i, "position_size")]
    assert len(scale_ifs) == 1, f"期望唯一缩放应用点, 实得 {len(scale_ifs)}"
    scale_if = scale_ifs[0]

    adm = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
           and n.module == "app.playbook.admission"]
    assert adm, "admission 二次复核必须在场"
    assert max(n.lineno for n in adm) < scale_if.lineno, \
        "缩放必须在 admission 复核之后 (先例: LIVE_TRIAL / gap_dive 半仓同序)"

    # 保底 1 股: max(1, int(position_size * _risk_scale))
    writes = [a for a in _assigns_to(scale_if, "position_size")
              if isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name)
              and a.value.func.id == "max"]
    assert writes, "缩放必须经 max(...) 保底"
    assert 1 in _consts(writes[0].value), "保底值必须是 1 股 (不得缩到 0 → 空单/异常)"
    assert _calls(writes[0].value, "int"), "股数必须取整 (int) 后下 max"
    assert not [n for n in ast.walk(scale_if) if isinstance(n, ast.Return)], \
        "缩放分支不得 return — 半仓仍要成交 (数据采集目标)"


def test_scaling_guard_skips_one_share_orders(fn):
    """position_size > 1 守卫: 1 股单不参与缩放 (缩了也是 1 股, 徒增日志噪声)."""
    scale_ifs = [i for i in _ifs(fn) if _assigns_to(i, "position_size")
                 and "_risk_scale" in {n.id for n in ast.walk(i.test)
                                        if isinstance(n, ast.Name)}]
    assert len(scale_ifs) == 1
    assert 1 in _consts(scale_ifs[0].test), "守卫须含 position_size > 1"
