"""空头近失带影子 (RFC-167): 记录器 + 结算器 的契约钉子.

守两件事:
  1. **判定语义零变化** —— 记录器只在判完之后追加观测; 写盘失败/band 关闭时方向必须与
     RFC-099 内联式逐字等价 (SHORT_ALLOW and chg >= SHORT_GAP_MIN)。
  2. **结算口径是预注册的** —— mfe/mae/close 三列按 (px, 当日 RTH high/low/close) 算,
     verdict 阈值 0.50 固定; 幂等 (重跑不重复落行); 单笔主导不得判 READY。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import scripts.extended_hours_entry_executor as EXEC      # noqa: E402
import scripts.settle_ext_short_nearmiss as SET           # noqa: E402


# ── 记录器 ────────────────────────────────────────────────────────────────
def _cfg(monkeypatch, tmp_path, band="2.0", short_allow=True, gap_min=3.0):
    monkeypatch.setattr(EXEC, "SHORT_ALLOW", short_allow)
    monkeypatch.setattr(EXEC, "SHORT_GAP_MIN", gap_min)
    monkeypatch.setattr(EXEC, "SHORT_NEARMISS_BAND", band)
    monkeypatch.setattr(EXEC, "SHORT_NEARMISS_DIR", tmp_path / "nm")
    return tmp_path / "nm"


def test_direction_semantics_unchanged(monkeypatch, tmp_path):
    """带关/带开都不改方向: <gap_min ⇒ LONG, >=gap_min ⇒ SHORT。"""
    _cfg(monkeypatch, tmp_path, band="")
    assert EXEC._classify_ext_direction("US.A", "premarket", 2.5, 100.0, 10 ** 6) == "LONG"
    assert EXEC._classify_ext_direction("US.A", "premarket", 3.0, 100.0, 10 ** 6) == "SHORT"
    assert EXEC._classify_ext_direction("US.A", "premarket", 9.9, 100.0, 10 ** 6) == "SHORT"
    monkeypatch.setattr(EXEC, "SHORT_ALLOW", False)
    assert EXEC._classify_ext_direction("US.A", "premarket", 9.9, 100.0, 10 ** 6) == "LONG"


def test_band_records_only_inside_window(monkeypatch, tmp_path):
    """[2.0, 3.0) 落行; 1.9% 与 3.1% 都不落 (后者由 SHORT 分支接管)。"""
    d = _cfg(monkeypatch, tmp_path, band="2.0")
    assert EXEC._classify_ext_direction("US.A", "premarket", 1.90, 50.0, 900000) == "LONG"
    assert EXEC._classify_ext_direction("US.B", "premarket", 2.40, 51.0, 900000) == "LONG"
    assert EXEC._classify_ext_direction("US.C", "premarket", 3.10, 52.0, 900000) == "SHORT"
    rows = json.loads((d / (sorted(p.name for p in d.glob("*.json"))[0])).read_text())["rows"]
    assert list(rows) == ["premarket:US.B"]
    assert rows["premarket:US.B"]["chg_pct"] == 2.4
    assert rows["premarket:US.B"]["px"] == 51.0


def test_band_disabled_writes_nothing(monkeypatch, tmp_path):
    """band 未配置 ⇒ 目录都不该建 (默认零足迹, 部署当天不影响生产盘)。"""
    d = _cfg(monkeypatch, tmp_path, band="")
    EXEC._classify_ext_direction("US.B", "premarket", 2.40, 51.0, 900000)
    assert not d.exists()


def test_dedupe_per_symbol_per_session(monkeypatch, tmp_path):
    """同票同场次一天看到 N 次 ⇒ 1 行 + ticks=N (不能把票次当票数)。"""
    d = _cfg(monkeypatch, tmp_path, band="2.0")
    for _ in range(3):
        EXEC._classify_ext_direction("US.B", "premarket", 2.40, 51.0, 900000)
    doc = json.loads((d / (sorted(p.name for p in d.glob("*.json"))[0])).read_text())
    assert len(doc["rows"]) == 1
    assert doc["rows"]["premarket:US.B"]["ticks"] == 3
    assert doc["rows"]["premarket:US.B"]["chg_pct"] == 2.4      # 首 tick 值不被覆盖


def test_record_failure_never_changes_direction(monkeypatch, tmp_path):
    """写盘炸了也必须返回原判定 (埋点异常不得影响开不开/开哪边)。"""
    _cfg(monkeypatch, tmp_path, band="2.0")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(EXEC, "_record_short_nearmiss", _boom)
    assert EXEC._classify_ext_direction("US.B", "premarket", 2.40, 51.0, 900000) == "LONG"
    assert EXEC._classify_ext_direction("US.B", "premarket", 3.40, 51.0, 900000) == "SHORT"


def test_inline_form_is_gone():
    """源码契约: 内联式不得再出现 (两条扫描臂必须共用 SSOT, 防漂移)。"""
    src = (REPO / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert '"SHORT" if (SHORT_ALLOW and change_pct >= SHORT_GAP_MIN) else "LONG"' not in src
    # 1 个 def + 2 个调用点 (dry 观测臂 + live 臂), 多一处就是又有人内联了第三份
    assert src.count("= _classify_ext_direction(") == 2
    assert src.count("def _classify_ext_direction(") == 1


# ── 结算器 ────────────────────────────────────────────────────────────────
def _bars(tmp_path, sym, date, high, low, close):
    (tmp_path / ("US_%s_%s.json" % (sym, date))).write_text(json.dumps([
        [date + " 09:31:00", close, high, low, close],
        [date + " 16:00:00", close, close, low, close],
    ]))


def _day_file(tmp_path, date, rows):
    (tmp_path / (date + ".json")).write_text(json.dumps({"et_date": date, "rows": rows}))


@pytest.fixture()
def settler(tmp_path, monkeypatch):
    d = tmp_path / "nm"
    d.mkdir()
    monkeypatch.setattr(SET, "SHADOW_DIR", d)
    monkeypatch.setattr(SET, "BARS_DIR", tmp_path)
    monkeypatch.setattr(SET, "SETTLED", tmp_path / "settled.jsonl")
    monkeypatch.setattr(SET, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(SET, "_open_ctx", lambda: None)   # 离线: 只走 replay_bars
    monkeypatch.setattr(sys, "argv", ["settle"])
    return d


def test_settle_math_and_verdict(settler, tmp_path):
    """px=100 / high=102 / low=98.6 / close=99 ⇒ mfe=1.4 mae=2.0 close=1.0 → FADE。"""
    _day_file(settler, "2026-10-20", {"premarket:US.X": {
        "symbol": "US.X", "session": "premarket", "chg_pct": 2.4, "px": 100.0,
        "volume": 1, "first_ts": "2026-10-20T05:00:00-04:00", "ticks": 2}})
    _bars(tmp_path, "X", "2026-10-20", high=102.0, low=98.6, close=99.0)
    SET.main()
    rec = json.loads((tmp_path / "settled.jsonl").read_text().splitlines()[0])
    assert rec["settled"] is True
    assert rec["mfe_pct"] == 1.4 and rec["mae_pct"] == 2.0 and rec["close_pct"] == 1.0
    assert rec["verdict"] == "FADE"
    assert rec["source"].startswith("replay_bars:")


def test_no_bars_stays_unsettled(settler, monkeypatch):
    """replay_bars 与富途日K 都没数据 ⇒ settled=false/reason=no_bars (绝不静默当 0 收益)。"""
    import scripts.settle_extended_hours as SEH
    monkeypatch.setattr(SET, "_open_ctx", lambda: object())
    monkeypatch.setattr(SEH, "_day_bars", lambda *a, **k: [])
    _day_file(settler, "2026-10-20", {"premarket:US.NOB": {
        "symbol": "US.NOB", "session": "premarket", "chg_pct": 2.4, "px": 10.0,
        "volume": 1, "first_ts": "2026-10-20T05:00:00-04:00", "ticks": 1}})
    SET.main()
    rec = json.loads((SET.SETTLED).read_text().splitlines()[0])
    assert rec["settled"] is False and rec["reason"] == "no_bars"


def test_ctx_unavailable_is_distinct_from_no_bars(settler):
    """连不上 OpenD 必须记 ctx_unavailable —— 否则"接口挂了"会被读成"这只票没数据"。"""
    _day_file(settler, "2026-10-20", {"premarket:US.NOB": {
        "symbol": "US.NOB", "session": "premarket", "chg_pct": 2.4, "px": 10.0,
        "volume": 1, "first_ts": "2026-10-20T05:00:00-04:00", "ticks": 1}})
    SET.main()      # fixture 的 _open_ctx lambda: None
    rec = json.loads((SET.SETTLED).read_text().splitlines()[0])
    assert rec["settled"] is False and rec["reason"] == "ctx_unavailable"


def test_settle_is_idempotent(settler, tmp_path):
    """重跑不重复落行 (settled 集合按 date|session|symbol)。"""
    _day_file(settler, "2026-10-20", {"premarket:US.X": {
        "symbol": "US.X", "session": "premarket", "chg_pct": 2.4, "px": 100.0,
        "volume": 1, "first_ts": "2026-10-20T05:00:00-04:00", "ticks": 1}})
    _bars(tmp_path, "X", "2026-10-20", high=101.0, low=99.0, close=100.0)
    SET.main()
    SET.main()
    assert len(SET.SETTLED.read_text().strip().splitlines()) == 1


def test_today_not_settled(settler, tmp_path):
    """当日未收盘不结 (避免用半天 bar 冒充整日读数)。"""
    today = SET._now_et().strftime("%Y-%m-%d")
    _day_file(settler, today, {"premarket:US.X": {
        "symbol": "US.X", "session": "premarket", "chg_pct": 2.4, "px": 100.0,
        "volume": 1, "first_ts": today + "T05:00:00-04:00", "ticks": 1}})
    _bars(tmp_path, "X", today, high=101.0, low=99.0, close=100.0)
    SET.main()
    assert not SET.SETTLED.exists()


def test_decidable_blocks_single_trade_domination():
    """单笔主导 (剔最大 mfe 后 edge 掉到线下) 不得判 READY —— PEP 式误读的挡板。"""
    rows = [{"mfe_pct": 9.0, "mae_pct": 0.2, "verdict": "FADE"}] + \
           [{"mfe_pct": 0.6, "mae_pct": 0.5, "verdict": "FADE"} for _ in range(29)]
    st = SET._stats(rows)
    ok, why = SET._decidable(st)
    assert st["edge"] >= SET.NET_EDGE_GATE       # 全样本看着"过线"
    assert st["ex_top1_edge"] < SET.NET_EDGE_GATE
    assert ok is False and why.startswith("SINGLE_TRADE")


def test_decidable_sample_gate():
    st = SET._stats([{"mfe_pct": 1.0, "mae_pct": 0.1, "verdict": "FADE"} for _ in range(5)])
    ok, why = SET._decidable(st)
    assert ok is False and why.startswith("SAMPLE_5/")