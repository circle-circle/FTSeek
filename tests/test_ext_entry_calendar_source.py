#!/usr/bin/env python3
"""候选源: 当日同相位财报日历名单 (2026-09-23 供给归因)。

背景(实测): 严格臂的准入集合 = "当日同相位财报事件", 而候选池 = 按涨跌幅排序的异动榜 ⇒
交集靠运气。2026-09-22 盘后实测: 榜 42 只与日历交集 0, 而当日盘后财报票 US.KBH
(after $49.39/+3.19%/盘后量 26.1 万/市值 2.98B) 四条地板全过却**不在榜内**(榜被 +20% 仙股
挤掉) ⇒ 合格票从未进闸。本源的契约: 相位判据与 earnings 门**逐字同式**, 只并入候选池,
不放松任何闸。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = "2026-09-22"          # 固定 ET 日, 避免跨日 flake


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_cal_source", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAL_ROWS = [
    # 当日盘后财报 (KBH 原型) —— afterhours 窗应入选
    {"symbol": "KBH", "reportDate": TODAY, "timeOfTheDay": "post-market"},
    # 当日盘前财报 —— 与 afterhours 相位不符, 必须排除
    {"symbol": "AZO", "reportDate": TODAY, "timeOfTheDay": "pre-market"},
    # 支票同日但相位字段为空 —— fail-closed (与判定同式: 空相位不入任何 session)
    {"symbol": "NOEV", "reportDate": TODAY, "timeOfTheDay": ""},
    # 明日财报 —— 不是"当日事件", 必须排除
    {"symbol": "TMRO", "reportDate": "2026-09-23", "timeOfTheDay": "post-market"},
]


def _arm(mod, monkeypatch, rows=CAL_ROWS, calendar_on=True, calendar_error=None):
    monkeypatch.setattr(mod, "SOURCE_RANK_BOARD", False)
    monkeypatch.setattr(mod, "SOURCE_BEAT_RANK", False)
    monkeypatch.setattr(mod, "SOURCE_WATCHLIST", False)
    monkeypatch.setattr(mod, "SOURCE_CALENDAR", calendar_on)
    monkeypatch.setattr(mod, "_scan_movers", lambda session, qctx: [])
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: (list(rows), calendar_error))
    monkeypatch.setattr(mod, "_today_et", lambda: TODAY)
    monkeypatch.setattr(mod, "_snapshot_candidates", lambda session, qctx, syms: [
        {"symbol": s, "name": "", "px": 49.39, "chg": 3.19, "vol": 261107.0, "prev_close": 47.86}
        for s in syms
    ])


def test_same_phase_row_becomes_candidate_others_excluded(monkeypatch, capsys):
    mod = _load()
    _arm(mod, monkeypatch)
    syms = [m["symbol"] for m in mod._scan_candidates("afterhours", None)]
    assert syms == ["US.KBH"], "只并入当日同相位 (post-market) 财报票"
    out = capsys.readouterr().out
    assert "日历候选源 (当日同相位财报): 1 只 US.KBH" in out
    assert "calendar=True" in out, "开关状态必须上读到候选源合并行"


def test_premarket_session_picks_premarket_rows(monkeypatch):
    """同一个源在盘前窗取 pre-market 行 (相位映射随 session, 与判定同式)。"""
    mod = _load()
    _arm(mod, monkeypatch)
    syms = [m["symbol"] for m in mod._scan_candidates("premarket", None)]
    assert syms == ["US.AZO"]


def test_switch_off_adds_nothing(monkeypatch):
    """默认关 (扩容方向的安全侧): 与开着的对照组必须给出不同的候选集。"""
    mod = _load()
    _arm(mod, monkeypatch, calendar_on=False)
    assert mod._scan_candidates("afterhours", None) == []


def test_calendar_source_error_is_fail_open_with_warning(monkeypatch, capsys):
    """日历源故障 ⇒ 本源跳过 + 显式警告, 不得炸掉其他源。"""
    mod = _load()
    _arm(mod, monkeypatch, rows=[], calendar_error="TimeoutError: boom")
    monkeypatch.setattr(mod, "SOURCE_RANK_BOARD", True)
    monkeypatch.setattr(mod, "_scan_movers", lambda session, qctx: [
        {"symbol": "US.MOVER1", "name": "", "px": 6.0, "chg": 5.0, "vol": 5e5},
    ])
    syms = [m["symbol"] for m in mod._scan_candidates("afterhours", None)]
    assert syms == ["US.MOVER1"], "源故障只影响本源, 异动榜候选照常"
    assert "日历候选源不可用" in capsys.readouterr().out


def test_phase_predicate_is_verbatim_same_as_earnings_gate():
    """源码契约: 相位判据必须与 `_earnings_calendar_event` 逐字同式。

    否则"入选者必过 earnings 门第①道"只是注释承诺 —— 两处一旦漂移, 本源就会往里灌
    calendar_phase_mismatch 的票 (白占候选位, 且台账归因被污染)。
    """
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    pred = 'if before == (session == "premarket") and after == (session == "afterhours"):'
    assert src.count(pred) == 2, "候选源与 earnings 门必须用同一条相位判据"