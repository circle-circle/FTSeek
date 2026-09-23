#!/usr/bin/env python3
"""ETH 开空探针的守卫测试 (预注册判据的机械校验 + 静态纪律)。

这些守卫的意义: 探针是全项目**唯一**会主动下券商单的手工脚本, 所以"不许在非 SIM 下跑 /
不许窗外跑 / 不许用 ext_entry 备注 / 不许无 --execute 下单"必须是**代码里的事实**, 不是文档里的承诺。
"""
import ast
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "probe_eth_short_open.py").read_text()


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("eth_short_probe", ROOT / "scripts" / "probe_eth_short_open.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load()


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=-4)))


# ── 时段窗 ──────────────────────────────────────────────────────────────
def test_premarket_window_edges():
    assert probe._in_eth_window(_et(2026, 9, 23, 4, 0))[0] is True
    assert probe._in_eth_window(_et(2026, 9, 23, 9, 29))[0] is True
    assert probe._in_eth_window(_et(2026, 9, 23, 9, 30))[0] is False   # RTH 起, 探针不跑
    assert probe._in_eth_window(_et(2026, 9, 23, 12, 0))[0] is False


def test_afterhours_window_edges():
    assert probe._in_eth_window(_et(2026, 9, 23, 16, 0)) == (True, "afterhours")
    assert probe._in_eth_window(_et(2026, 9, 23, 19, 59))[0] is True
    assert probe._in_eth_window(_et(2026, 9, 23, 20, 0))[0] is False


def test_weekend_and_none_rejected():
    assert probe._in_eth_window(_et(2026, 9, 26, 5, 0))[0] is False   # 周六盘前
    assert probe._in_eth_window(None)[0] is False


# ── 模式守卫 (SIM-only) ─────────────────────────────────────────────────
def test_mode_guard_only_sim():
    assert probe._mode_ok(False, "SIMULATE") is True
    assert probe._mode_ok(True, "SIMULATE") is False    # 实盘模式 ⇒ 拒跑
    assert probe._mode_ok(False, "REAL") is False


def test_resolve_trd_env_handles_method_and_attribute():
    """TradingMode.futu_trd_env 是方法 — 漏调括号会拿到 '<bound method ...>' 而误判拒跑。"""
    class _M:
        def futu_trd_env(self):
            return "SIMULATE"

    class _A:
        futu_trd_env = "REAL"

    assert probe._resolve_trd_env(_M()) == "SIMULATE"
    assert probe._resolve_trd_env(_A()) == "REAL"
    assert probe._mode_ok(False, probe._resolve_trd_env(_M())) is True


# ── 价格守卫 ────────────────────────────────────────────────────────────
def test_price_guard():
    assert probe._price_ok(45.0) is True
    assert probe._price_ok(probe.PRICE_CAP_USD) is True
    assert probe._price_ok(probe.PRICE_CAP_USD + 0.01) is False
    assert probe._price_ok(0) is False
    assert probe._price_ok(None) is False


# ── 当日次数上限 (防试探性重试) ─────────────────────────────────────────
def test_attempts_counted_by_et_date(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"et": "2026-09-23 04:10:00-04:00", "verdict": "eth_short_fills"}\n'
        '{"et": "2026-09-22 16:20:00-04:00", "verdict": "eth_short_unsupported"}\n'
        'not-json\n'
    )
    monkeypatch.setattr(probe, "LEDGER", str(ledger))
    assert probe._attempts_today("2026-09-23") == 1
    assert probe._attempts_today("2026-09-22") == 1
    assert probe._attempts_today("2026-09-24") == 0


# ── 选标 (券源/权限/价格 三态) ──────────────────────────────────────────
class _FakeTctx:
    def __init__(self, info, ratio):
        self.info, self.ratio = info, ratio

    def acctradinginfo_query(self, **kw):
        row = self.info.get(kw["code"])
        return (0, pd.DataFrame([row])) if row is not None else (1, pd.DataFrame())

    def get_margin_ratio(self, codes):
        row = self.ratio.get(codes[0])
        return (0, pd.DataFrame([row])) if row is not None else (1, pd.DataFrame())


class _FakeQctx:
    def __init__(self, snaps):
        self.snaps = snaps

    def get_market_snapshot(self, codes):
        row = self.snaps.get(codes[0])
        return (0, pd.DataFrame([row])) if row is not None else (1, pd.DataFrame())


class _FakeEnv:
    SIMULATE = "SIMULATE"


class _FakeOrderType:
    NORMAL = "NORMAL"


def _pick(info, ratio, snaps, monkeypatch, candidates=("US.AAA", "US.BBB")):
    monkeypatch.setattr(probe, "CANDIDATES", list(candidates))
    return probe._select_candidate(_FakeTctx(info, ratio), _FakeQctx(snaps), _FakeEnv, _FakeOrderType, 0,
                                   log=lambda *_: None)


_SNAP_OK = {"last_price": 45.0, "bid_price": 44.9, "ask_price": 45.1}


def test_select_takes_first_eligible(monkeypatch):
    pick = _pick({"US.AAA": {"max_sell_short": 3535}}, {"US.AAA": {"is_short_permit": True}},
                 {"US.AAA": _SNAP_OK}, monkeypatch)
    assert pick == {"code": "US.AAA", "shortable": 3535, "last": 45.0, "bid": 44.9, "ask": 45.1,
                    "spread_pct": 0.444}   # spread_pct: 预注册修订 #3 (成交验证选标用)


def test_select_skips_no_borrow_and_no_permit(monkeypatch):
    pick = _pick({"US.AAA": {"max_sell_short": 0}, "US.BBB": {"max_sell_short": 100}},
                 {"US.AAA": {"is_short_permit": True}, "US.BBB": {"is_short_permit": False}},
                 {"US.AAA": _SNAP_OK, "US.BBB": _SNAP_OK}, monkeypatch)
    assert pick is None


def test_select_skips_over_price_cap(monkeypatch):
    pick = _pick({"US.AAA": {"max_sell_short": 10}}, {"US.AAA": {"is_short_permit": True}},
                 {"US.AAA": {"last_price": probe.PRICE_CAP_USD + 50, "bid_price": 1, "ask_price": 1}},
                 monkeypatch)
    assert pick is None


# ── 静态纪律 (防止探针与生产链相互污染) ─────────────────────────────────
def _code_without_docstring(src):
    """剥 docstring 再断言 (注释/文档里提 ext_entry 是说明, 代码里用才是事故)。"""
    tree = ast.parse(src)
    first = tree.body[0] if tree.body else None
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
            and isinstance(first.value.value, str):
        src = src.replace(first.value.value, "", 1)
    return "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))


CODE = _code_without_docstring(SRC)


def test_probe_remark_never_ext_entry():
    """P1 事故机制: remark=ext_entry 会被入场执行器当成自己在途单 ⇒ 探针绝不能用该备注。"""
    assert 'REMARK = "eth_probe"' in CODE
    assert "ext_entry" not in CODE


def test_probe_orders_gated_by_execute_flag():
    """无 --execute 必须一次券商写调用都不发。"""
    assert "if not args.execute:" in CODE
    assert "args.execute" in CODE.split("place_order")[0]
    assert "Session.ETH" in CODE and "TrdSide.SELL" in CODE
    assert "TrdSide.SELL" not in CODE.split("if not args.execute:")[0]

# ── 预注册修订 #3 (2026-09-23): 成交验证选标 —— 价差最窄优先 + 价差闸 ────────────
def test_fill_check_prefers_tightest_spread(monkeypatch):
    """fill-check 模式: 不取第一个合格票, 取**价差最窄**的 —— 否则空簿挂单永远 dealt=0,
    把"标的没对手盘"误判成"盘前不能成交" (2026-09-23 实测 CBRL 价差 12.4% ⇒ dealt=0;
    CTAS 价差 0.49% ⇒ FILLED_ALL)。"""
    info = {"US.AAA": {"max_sell_short": 100}, "US.BBB": {"max_sell_short": 100}}
    ratio = {"US.AAA": {"is_short_permit": True}, "US.BBB": {"is_short_permit": True}}
    snaps = {"US.AAA": {"last_price": 45.0, "bid_price": 44.9, "ask_price": 45.1},        # 0.44%
             "US.BBB": {"last_price": 100.0, "bid_price": 99.0, "ask_price": 101.0}}      # 2.00%
    monkeypatch.setattr(probe, "CANDIDATES", ["US.AAA", "US.BBB"])
    pick = probe._select_candidate(_FakeTctx(info, ratio), _FakeQctx(snaps), _FakeEnv,
                                   _FakeOrderType, 0, log=lambda *_: None,
                                   prefer_liquid=True, spread_max_pct=1.0)
    assert pick["code"] == "US.AAA" and pick["spread_pct"] < 1.0


def test_fill_check_returns_none_when_no_liquid_candidate(monkeypatch):
    """全部候选价差 > 闸 ⇒ None (不下一张注定不成交的单), 且不影响默认口径。"""
    info = {"US.BBB": {"max_sell_short": 100}}
    ratio = {"US.BBB": {"is_short_permit": True}}
    snaps = {"US.BBB": {"last_price": 100.0, "bid_price": 98.0, "ask_price": 102.0}}      # 4.00%
    monkeypatch.setattr(probe, "CANDIDATES", ["US.BBB"])
    t, q = _FakeTctx(info, ratio), _FakeQctx(snaps)
    assert probe._select_candidate(t, q, _FakeEnv, _FakeOrderType, 0, log=lambda *_: None,
                                   prefer_liquid=True, spread_max_pct=1.0) is None
    assert probe._select_candidate(t, q, _FakeEnv, _FakeOrderType, 0,
                                   log=lambda *_: None)["code"] == "US.BBB"   # 默认口径不受影响
