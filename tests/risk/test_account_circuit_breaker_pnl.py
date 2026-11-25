"""AccountCircuitBreaker 当日盈亏口径契约测试 (2026-08-27 修复).

原缺陷: WHERE trade_date = today (入场日) + 毛额 realized_pnl —
隔夜仓跳空亏损按入场日记账, 熔断口径对其失明 (8/27 UBER 实证:
净 -$307 记 8/26, 老口径日盈亏 +0.18% vs 净值口径 -0.18%).
修复: 按出场日 exit_time 归集 + 净额 pnl_net 优先.
"""
import sqlite3

import pytest

from app.risk.account_circuit_breaker import AccountCircuitBreaker


@pytest.fixture
def breaker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.core import config_container

    monkeypatch.setattr(config_container, "get_config", lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: default))
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
        "app.risk.account_circuit_breaker.get_trade_records_db_for_arm",
        lambda target_env: db)
    return AccountCircuitBreaker()


def _insert(db_path, **kw):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, "
        "exit_time, entry_price, exit_price, quantity, realized_pnl, pnl_net) "
        "VALUES (:trade_date, :symbol, :direction, :entry_time, :exit_time, "
        ":entry_price, :exit_price, :quantity, :realized_pnl, :pnl_net)",
        kw)
    conn.commit()
    conn.close()


def _db(breaker, monkeypatch):
    from app.risk.account_circuit_breaker import get_trade_records_db_for_arm
    return get_trade_records_db_for_arm("sim")


def test_overnight_exit_today_counts_toward_today(breaker, monkeypatch):
    """核心回归: 入场日记昨日、今晨平仓的隔夜仓必须计入今日熔断口径."""
    db = _db(breaker, monkeypatch)
    _insert(str(db), trade_date="2026-08-26", symbol="US.UBER", direction="LONG",
            entry_time="2026-08-26T09:48:48-04:00",
            exit_time="2026-08-27T09:30:05-04:00",
            entry_price=81.04, exit_price=76.778, quantity=71,
            realized_pnl=-302.60, pnl_net=-307.13)
    assert breaker._get_daily_pnl("2026-08-27") == pytest.approx(-307.13)


def test_net_preferred_over_gross(breaker, monkeypatch):
    """净额优先: 毛正净负的保本单 (费用吞噬) 按真净亏计."""
    db = _db(breaker, monkeypatch)
    _insert(str(db), trade_date="2026-08-27", symbol="US.RIVN", direction="LONG",
            entry_time="2026-08-27T09:41:00-04:00",
            exit_time="2026-08-27T09:46:45-04:00",
            entry_price=16.79, exit_price=16.81, quantity=353,
            realized_pnl=7.06, pnl_net=-2.31)
    assert breaker._get_daily_pnl("2026-08-27") == pytest.approx(-2.31)


def test_gross_fallback_when_net_null(breaker, monkeypatch):
    """旧行 pnl_net NULL → 回退毛额, 不丢数."""
    db = _db(breaker, monkeypatch)
    _insert(str(db), trade_date="2026-05-01", symbol="US.XYZ", direction="LONG",
            entry_time="2026-05-01T10:00:00-04:00",
            exit_time="2026-08-27T11:00:00-04:00",
            entry_price=10.0, exit_price=11.0, quantity=10,
            realized_pnl=10.0, pnl_net=None)
    assert breaker._get_daily_pnl("2026-08-27") == pytest.approx(10.0)


def test_yesterday_exit_not_counted_today(breaker, monkeypatch):
    db = _db(breaker, monkeypatch)
    _insert(str(db), trade_date="2026-08-25", symbol="US.XYZ", direction="LONG",
            entry_time="2026-08-25T10:00:00-04:00",
            exit_time="2026-08-26T15:00:00-04:00",
            entry_price=10.0, exit_price=11.0, quantity=10,
            realized_pnl=10.0, pnl_net=9.0)
    assert breaker._get_daily_pnl("2026-08-27") == 0.0


def test_open_position_excluded(breaker, monkeypatch):
    """在仓行 (exit 字段空) 不计 — exit_price>0 与 exit_time 前缀双过滤."""
    db = _db(breaker, monkeypatch)
    _insert(str(db), trade_date="2026-08-27", symbol="US.MU", direction="LONG",
            entry_time="2026-08-27T09:38:22-04:00",
            exit_time=None, entry_price=946.87, exit_price=None,
            quantity=6, realized_pnl=0.0, pnl_net=None)
    assert breaker._get_daily_pnl("2026-08-27") == 0.0


def test_mixed_same_day_net_sum(breaker, monkeypatch):
    """8/27 实盘复算: 7 笔净额合计 -22.31 (老口径只看见 +191 的入场日子集)."""
    db = _db(breaker, monkeypatch)
    rows = [
        ("2026-08-26", "2026-08-27T09:30:05-04:00", -302.60, -307.13),
        ("2026-08-26", "2026-08-27T09:31:00-04:00", 95.08, 90.73),
        ("2026-08-26", "2026-08-27T09:45:00-04:00", 7.42, 2.99),
        ("2026-08-27", "2026-08-27T09:46:45-04:00", 7.06, -2.31),
        ("2026-08-27", "2026-08-27T09:48:28-04:00", 105.60, 101.30),
        ("2026-08-27", "2026-08-27T09:53:48-04:00", 65.34, 61.01),
        ("2026-08-27", "2026-08-27T10:12:42-04:00", 9.30, 5.13),
        ("2026-08-27", "2026-08-27T10:16:37-04:00", 13.38, 9.20),
        ("2026-08-27", "2026-08-27T10:22:01-04:00", 17.10, 12.96),
        ("2026-08-27", "2026-08-27T10:26:42-04:00", 8.14, 3.81),
    ]
    for i, (td, xt, pnl, net) in enumerate(rows):
        _insert(str(db), trade_date=td, symbol=f"US.S{i}", direction="LONG",
                entry_time="2026-08-26T09:00:00-04:00", exit_time=xt,
                entry_price=10.0, exit_price=11.0, quantity=10,
                realized_pnl=pnl, pnl_net=net)
    assert breaker._get_daily_pnl("2026-08-27") == pytest.approx(-22.31)


def test_db_failure_fail_close(breaker, monkeypatch):
    """fail-close: 查询异常返回 -inf → check() 判 LIQUIDATE (拒单)."""
    monkeypatch.setattr(
        "app.risk.account_circuit_breaker.get_trade_records_db_for_arm",
        lambda target_env: "/nonexistent_dir_xyz/trade_records.db")
    assert breaker._get_daily_pnl("2026-08-27") == float("-inf")


@pytest.mark.parametrize("equity,action", [(96889.835, "LIQUIDATE"), (97000.0, "NORMAL")])
def test_verified_peak_drawdown_remains_protected(breaker, monkeypatch, equity, action):
    from types import SimpleNamespace
    from app.risk import capital_safety

    state = SimpleNamespace(peak_equity=99999.496)
    monkeypatch.setattr(capital_safety, "get_capital_safety_system",
                        lambda: SimpleNamespace(_state=state))
    monkeypatch.setattr(breaker, "_get_daily_pnl", lambda today, target_env: -260.79)
    result = breaker.check(equity, target_env="sim")
    assert result.action == action
    assert result.peak_equity == 99999.496
    assert result.current_equity == equity
    assert result.daily_pnl_pct == pytest.approx(-260.79 / equity)
    assert state.peak_equity == 99999.496


def test_peak_and_daily_pnl_use_requested_arm(breaker, monkeypatch):
    from types import SimpleNamespace
    from app.risk import capital_safety

    seen = []
    monkeypatch.setattr(capital_safety, "get_capital_safety_system",
                        lambda: SimpleNamespace(_state=SimpleNamespace(peak_equity=99999.496)))

    def real_state(arm):
        seen.append(("peak", arm))
        return SimpleNamespace(_state=SimpleNamespace(peak_equity=79087.01))

    def daily_pnl(today, arm):
        seen.append(("daily", arm))
        return 0.0

    monkeypatch.setattr(capital_safety, "get_capital_safety_system_for_arm", real_state)
    monkeypatch.setattr(breaker, "_get_daily_pnl", daily_pnl)
    result = breaker.check(79000.0, target_env="real")
    assert result.action == "NORMAL"
    assert result.peak_equity == 79087.01
    assert seen == [("daily", "real"), ("peak", "real")]


# ── LIQUIDATE 档声明契约 (2026-08-28 纠偏) ─────────────────────────────── #

def test_liquidate_tier_claims_match_wired_behavior():
    """-3% LIQUIDATE 档自建立起从未接线强制平仓 — 四个消费方全部
    `action in ("HALT", "LIQUIDATE")` 仅拒新仓. 声明必须说真话:

    ① 四文件内"强制全平"字样必须带"从未接线"免责标记 (含 equity<=0 的
       reason 运行时字符串 — 不得裸声明);
    ② 头注必须带"从未接线"免责声明 (防声明悄悄漂回);
    ③ 消费方注释不得再宣称"3% 强制全平" (orb/exits.py 的 15:55 EOD 全平是
       真实行为, 不在此列; 纠偏免责行带"从未接线"标记亦豁免).

    [2026-09-20 ORB 退役] 扫描面原含 `app/orb/engine.py`; 用户裁「Playbook 成为唯一自动
    交易下单管线, 其他管线都是历史」⇒ 该文件随 `app/orb/`(2735 行) 整体退役, 已从扫描面移除
    (其 `orb/exits.py` 15:55 EOD 全平属历史行为, 亦随模块退役)。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    acb = (root / "app" / "risk" / "account_circuit_breaker.py").read_text(encoding="utf-8")
    assert "从未接线" in acb, "ACB 头注缺 LIQUIDATE 未接线免责声明"
    offenders = []
    for rel in ("app/risk/account_circuit_breaker.py",
                "app/pipeline/order_executor.py",
                "app/playbook/playbook_service.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if "强制全平" in s and "从未接线" not in s and "15:55" not in s and "盘末" not in s:
                offenders.append(f"{rel}: {s[:80]}")
    assert not offenders, f"'强制全平'声明但全平从未接线 (实际仅拒新仓): {offenders}"
