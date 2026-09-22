#!/usr/bin/env python3
"""候选源汇合口的 ETF 过滤 (2026-09-22 零成交归因)。

背景(实测): 当日盘前异动榜 50 只里 **6 只是 ETF** (IMVP 印度ETF / GRAG 2倍做多GRAB ETF /
WRTH Options Income ETF / ARGT 阿根廷ETF / KSA 沙特ETF / RKX 2倍做多RKLB ETF)。而 earnings
门要求"当日同相位财报事件" —— ETF 永远没有财报事件 ⇒ 它们是**结构性死票**: 每轮走完四闸,
必然计入 earnings 台账的 calendar_missing, 污染 coverage 归因, 还占 12% 候选位。
判据 = 榜单自带 name 字段含 "ETF" (futu 美股 ETF 命名惯例; 实测 6/6 命中 / 个股 0 误伤)。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_etf", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"symbol": "US.TOPS", "name": "Top Ships", "px": 3.0, "chg": 9.0, "vol": 9e6},
    {"symbol": "US.IMVP", "name": "印度ETF-Invesco", "px": 40.0, "chg": 3.0, "vol": 9e5},
    {"symbol": "US.IMCC", "name": "IM Cannabis", "px": 5.77, "chg": 87.0, "vol": 1.05e6},
    {"symbol": "US.ARGT", "name": "Global X MSCI阿根廷ETF", "px": 60.0, "chg": 2.5, "vol": 3e5},
    {"symbol": "US.GRML", "name": "Greenland Mines", "px": 10.84, "chg": 15.1, "vol": 2.13e5},
    {"symbol": "US.RKX", "name": "2倍做多RKLB ETF-Corgi", "px": 12.0, "chg": 5.0, "vol": 4e5},
]


def _arm(mod, monkeypatch, rows):
    monkeypatch.setattr(mod, "SOURCE_RANK_BOARD", True)
    monkeypatch.setattr(mod, "SOURCE_BEAT_RANK", False)
    monkeypatch.setattr(mod, "SOURCE_WATCHLIST", False)
    monkeypatch.setattr(mod, "_scan_movers", lambda session, qctx: list(rows))


def test_etf_dropped_at_merge_point_stocks_kept(monkeypatch, capsys):
    mod = _load()
    _arm(mod, monkeypatch, ROWS)
    merged = mod._scan_candidates("premarket", None)
    syms = [m["symbol"] for m in merged]
    assert syms == ["US.TOPS", "US.IMCC", "US.GRML"], "ETF 必须被挡, 个股保序保留"
    out = capsys.readouterr().out
    assert "忽略 ETF: 3 只" in out
    assert "候选源合并: 3 只" in out


def test_missing_name_must_not_drop_a_stock(monkeypatch):
    """defensive: 无 name 字段的来源 (旧式 dict) 不得因过滤而误杀个股。"""
    mod = _load()
    _arm(mod, monkeypatch, [{"symbol": "US.NEW1", "px": 6.0, "chg": 8.0, "vol": 5e5}])
    merged = mod._scan_candidates("premarket", None)
    assert [m["symbol"] for m in merged] == ["US.NEW1"]


def test_etf_filter_is_insensitive_to_case_and_etf_suffix(monkeypatch):
    mod = _load()
    _arm(mod, monkeypatch, [
        {"symbol": "US.AA1", "name": "Some etf trust", "px": 6.0, "chg": 8.0, "vol": 5e5},
        {"symbol": "US.AA2", "name": "ETF", "px": 6.0, "chg": 8.0, "vol": 5e5},
        {"symbol": "US.AA3", "name": "Ethereum Fund", "px": 6.0, "chg": 8.0, "vol": 5e5},
    ])
    syms = [m["symbol"] for m in mod._scan_candidates("premarket", None)]
    assert syms == ["US.AA3"], "大小写不敏感; 不含 ETF 的基金名 (Ethereum Fund) 不误杀"


def test_both_sources_carry_name_field():
    """源码契约: 两个候选构建器都要带 name, 否则过滤对它们静默失效 (假绿)。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert src.count('"name": str(r.get("name", ""))') == 2, (
        "_scan_movers 与 _snapshot_candidates 都必须带 name 字段"
    )