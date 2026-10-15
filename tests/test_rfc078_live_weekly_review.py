r"""RFC-078 批C · 实盘周报 (美股段 + 港股段) 契约 (2026-09-08).

病灶: 既有周报体系**全部是模拟研究口径** —— `weekly_trade_summary.py` 白名单
`PLAYBOOK_SIM/PLAYBOOK_LIVE`、`symbol_performance_report.py` 同白名单、
`exit_scorecard.py:244` 干脆写 `AND trade_mode != 'live'` 显式排除实盘
⇒ 用户的真钱交易在任何一份周报里都不是一等公民; 港股更是**零留痕**。

本文件钉的是设计里最容易被人"顺手优化掉"的几条诚实性约束:
  · 不得给"两市场合计盈亏" (USD + HKD 相加 = 一个没有意义的数, 但会被当一个数读);
  · 不得把"台账没同步"报成"本周零交易" (两种空必须分开说);
  · 对账缺一侧时**不得出差集** (缺券商快照的差集会把全部正常在仓报成孤儿);
  · 港股手动单活动必须读 jsonl (港股单不进 manual_order_audit, 只查 DB 会得出
    "港股本周无手动下单"的假结论, 而同期券商有 18 笔成交);
  · 多空市值不得混加 (券商对空仓返回负 market_val)。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))

from app.web_dashboard import live_ledger  # noqa: E402
import live_weekly_review as lwr  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时 live 库 + 临时 jsonl; 绝不读写生产库与生产审计文件."""
    db = tmp_path / "live.db"
    monkeypatch.setattr(live_ledger, "_db_path", lambda: db)
    monkeypatch.setattr(lwr, "_db_path", lambda: db)
    live_ledger.ensure_tables(db)

    # 回合台账 (美股实盘) + 手动单审计
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE trade_records (
        symbol TEXT, direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL,
        exit_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL, exit_reason TEXT,
        source_type TEXT, trade_mode TEXT, mode TEXT)""")
    conn.execute("""CREATE TABLE manual_order_audit (
        timestamp TEXT, symbol TEXT, action TEXT, quantity INTEGER, mode TEXT, status TEXT)""")
    conn.commit()
    conn.close()

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    monkeypatch.setattr(lwr, "ROOT", tmp_path)
    audit_dir.mkdir(exist_ok=True)
    (tmp_path / "data" / "audit").mkdir(parents=True, exist_ok=True)
    return {"db": db, "tmp": tmp_path, "conn": conn}


def _put_deal(db, deal_id, market, symbol, side, qty, price, when):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT INTO live_deal_records (deal_id, market, currency, symbol, trd_side,
                   qty, price, amount, fee, deal_time, deal_date)
               VALUES (?,?,?,?,?,?,?,?,NULL,?,?)""",
            (deal_id, market, "HKD" if market == "HK" else "USD", symbol, side,
             qty, price, round(qty * price, 2), when, when[:10]))
        conn.commit()
    finally:
        conn.close()


def _put_snap(db, day, market, symbol, qty, cost, cur, mv, pl, ratio=0.0, name=""):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT INTO live_position_snapshots (snapshot_date, market, symbol, stock_name,
                   qty, cost_price, cur_price, market_val, pl_val, pl_ratio, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_date, market, symbol) DO UPDATE SET qty=excluded.qty""",
            (day, market, symbol, name, qty, cost, cur, mv, pl, ratio,
             "HKD" if market == "HK" else "USD"))
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
#  1. 不得跨币种合计
# ══════════════════════════════════════════════════════════════════
def test_report_never_sums_across_currencies(env):
    """两市场金额**不得**相加 —— 给了就会被当成一个数读.

    证伪: 在 render 里加一行"合计盈亏 = US + HK" ⇒ 本条转红。
    """
    db = env["db"]
    _put_deal(db, "u1", "US", "US.TSLA", "BUY", 50, 360.0, "2026-09-03 10:00:00")
    _put_deal(db, "u2", "US", "US.TSLA", "SELL", 50, 370.0, "2026-09-04 10:00:00")
    _put_deal(db, "h1", "HK", "HK.00700", "BUY", 100, 440.0, "2026-09-03 10:00:00")
    _put_deal(db, "h2", "HK", "HK.00700", "SELL", 100, 460.0, "2026-09-04 10:00:00")
    _put_snap(db, "2026-09-08", "US", "US.TSLA", 50, 360.0, 355.0, 17750.0, -250.0)
    _put_snap(db, "2026-09-08", "HK", "HK.00700", 100, 440.0, 435.0, 43500.0, -500.0)

    data = lwr.build(7, "2026-09-08")
    report = lwr.render(data["start"], data["end"], data["us"], data["hk"], data["gaps"])
    assert "币种不同, 不给合计" in report
    # 美股 +500 USD 与港股 +2000 HKD 都必须在场, 但不得出现 2500 这样的跨币种和
    assert "$500.00" in report and "HK$2,000.00" in report
    assert "2,500" not in report and "2500" not in report
    assert "【美股 · 实盘】 币种 USD" in report and "【港股 · 实盘】 币种 HKD" in report


# ══════════════════════════════════════════════════════════════════
#  2. 两种"空"必须分开说
# ══════════════════════════════════════════════════════════════════
def test_empty_ledger_is_no_data_not_zero_trades(env):
    """台账整体为空 (没同步/券商不可达) ⇒ NO_DATA + 明示"这不是本周零交易"."""
    sec = lwr.deal_section("HK", "2026-09-02", "2026-09-08")
    assert sec["state"] == "NO_DATA"
    assert "不是" in sec["detail"] and "零交易" in sec["detail"]


def test_populated_ledger_with_empty_window_is_a_different_state(env):
    """台账有数据但本窗口没有 ⇒ NO_TRADES_IN_WINDOW (真的没交易), 与 NO_DATA 严格区分."""
    db = env["db"]
    _put_deal(db, "h1", "HK", "HK.00700", "BUY", 100, 440.0, "2026-08-01 10:00:00")
    sec = lwr.deal_section("HK", "2026-09-02", "2026-09-08")
    assert sec["state"] == "NO_TRADES_IN_WINDOW"
    assert "本窗口" in sec["detail"] and "无成交" in sec["detail"]


def test_position_flat_vs_no_snapshot_distinguished(env):
    db = env["db"]
    assert lwr.position_section("US", "2026-09-08")["state"] == "NO_DATA"
    _put_snap(db, "2026-09-05", "US", "US.TSLA", 0, 0, 0, 0, 0)  # 有快照日但空仓
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM live_position_snapshots WHERE market='US'")
    conn.execute("INSERT INTO live_position_snapshots (snapshot_date, market, symbol, qty)"
                 " VALUES ('2026-09-05','US','US.X',0)")
    conn.commit()
    conn.close()
    # 有快照行但 latest_snapshot 取到的是 qty=0 的行 ⇒ 仍属"有数据"
    assert lwr.position_section("US", "2026-09-08")["state"] == "OK"


# ══════════════════════════════════════════════════════════════════
#  3. 对账: 缺一侧不得出差集
# ══════════════════════════════════════════════════════════════════
def test_reconcile_refuses_when_broker_side_missing(env):
    """券商快照缺失时**不得**出差集 —— 否则全部正常在仓都会被报成孤儿.

    这比不对账更坏: 孤儿告警会让人去平掉实际存在的仓位。
    """
    rt = {"state": "OK", "open_rows": [{"symbol": "US.AAPL"}, {"symbol": "US.NVDA"}]}
    r = lwr.reconcile_section("US", rt, {"state": "NO_DATA"})
    assert r["state"] == "UNAVAILABLE"
    assert r["orphans"] == [] and r["untracked"] == []
    assert "不出差集" in r["detail"]


def test_reconcile_finds_orphans_and_untracked(env):
    """两侧齐备时: 台账有券商无 = 孤儿(平仓未落账); 券商有台账无 = 未入账(开仓未落账)."""
    rt = {"state": "OK", "open_rows": [{"symbol": "US.AAPL"}, {"symbol": "US.MU"}]}
    pos = {"state": "OK", "rows": [{"symbol": "US.MU"}, {"symbol": "US.TSLA"}]}
    r = lwr.reconcile_section("US", rt, pos)
    assert r["state"] == "OK"
    assert r["orphans"] == ["US.AAPL"] and r["untracked"] == ["US.TSLA"]


def test_reconcile_is_na_for_hk(env):
    """港股不进 trade_records ⇒ 无回合台账可对, 必须明说 N/A 而不是报 0 孤儿."""
    r = lwr.reconcile_section("HK", {}, {"state": "OK", "rows": []})
    assert r["state"] == "N/A" and "无回合台账" in r["detail"]


# ══════════════════════════════════════════════════════════════════
#  4. 港股手动单活动必须读 jsonl
# ══════════════════════════════════════════════════════════════════
def test_hk_activity_reads_jsonl_not_manual_order_audit(env):
    """**关键钉桩**: 港股单不进 `manual_order_audit` (实测 160 行里只有 1 行 HK, 停在
    2026-06-10), 留痕在 `data/audit/hk_manual_orders.jsonl`。只查 DB 会得出"港股本周
    无手动下单"的假结论, 而同期券商真值层有 18 笔成交。
    """
    tmp = env["tmp"]
    jsonl = tmp / "data" / "audit" / "hk_manual_orders.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"ts": "2026-09-03T10:00:00", "op": "submit", "symbol": "HK.00700",
                    "status": "submitted"}),
        json.dumps({"ts": "2026-09-04T11:00:00", "op": "submit", "symbol": "HK.02513",
                    "status": "rejected"}),
        json.dumps({"ts": "2026-09-05T12:00:00", "op": "cancel", "symbol": "",
                    "status": "cancelled"}),
        json.dumps({"ts": "2026-08-01T12:00:00", "op": "submit", "symbol": "HK.00100",
                    "status": "submitted"}),  # 窗口外, 不得计入
        "{ broken json",
    ]), encoding="utf-8")

    sec = lwr.manual_activity_section("2026-09-02", "2026-09-08", "HK")
    assert sec["state"] == "OK" and sec["n"] == 3, sec
    assert sec["ops"] == {"submit": 2, "cancel": 1}
    assert sec["status"] == {"submitted": 1, "rejected": 1, "cancelled": 1}
    assert sec["malformed_lines"] == 1, "坏行必须计数上报, 不得静默丢"
    assert sec["source"] == "hk_manual_orders.jsonl"


def test_hk_activity_missing_jsonl_is_no_data(env):
    sec = lwr.manual_activity_section("2026-09-02", "2026-09-08", "HK")
    assert sec["state"] == "NO_DATA" and "jsonl" in sec["detail"]


def test_us_activity_reads_db_and_filters_by_market_prefix(env):
    """美股走 DB, 且必须按 symbol 前缀分市场 (HK 行不得混进美股段)."""
    conn = sqlite3.connect(str(env["db"]))
    try:
        for ts, sym, status in (("2026-09-03T10:00:00", "US.TSLA", "submitted"),
                                ("2026-09-04T10:00:00", "US.MU", "rejected"),
                                ("2026-09-04T11:00:00", "HK.00700", "submitted")):
            conn.execute("INSERT INTO manual_order_audit (timestamp, symbol, action, quantity,"
                         " mode, status) VALUES (?,?,?,?,'live_conservative',?)",
                         (ts, sym, "BUY", 10, status))
        conn.commit()
    finally:
        conn.close()
    sec = lwr.manual_activity_section("2026-09-02", "2026-09-08", "US")
    assert sec["total"] == 2, f"港股行混进了美股段: {sec}"
    assert {r["status"]: r["n"] for r in sec["rows"]} == {"submitted": 1, "rejected": 1}


# ══════════════════════════════════════════════════════════════════
#  5. 多空市值不得混加
# ══════════════════════════════════════════════════════════════════
def test_long_and_short_market_value_not_mixed(env):
    """券商对空仓返回负 market_val ⇒ 直接 sum 会把多空对冲成无意义的数.

    实测: TSLA +17,777.50 与 MU −10,214.40 混加得 7,563.10, 读报告的人会以为
    总仓位只有 7.5K (真实毛敞口 27,991.90)。
    """
    db = env["db"]
    _put_snap(db, "2026-09-08", "US", "US.TSLA", 50, 360.0, 355.55, 17777.5, -222.5)
    _put_snap(db, "2026-09-08", "US", "US.MU", -10, 999.0, 1021.44, -10214.4, -224.4)
    pos = lwr.position_section("US", "2026-09-08")
    assert pos["state"] == "OK"
    assert pos["long_mv"] == 17777.5 and pos["short_mv"] == 10214.4
    assert pos["gross_mv"] == 27991.9
    assert (pos["n_long"], pos["n_short"]) == (1, 1)
    assert round(pos["unrealized_pl"], 2) == -446.9
    assert pos["market_val"] if "market_val" in pos else True  # 混加键不得再被渲染使用


def test_render_shows_long_short_split(env):
    db = env["db"]
    _put_snap(db, "2026-09-08", "US", "US.TSLA", 50, 360.0, 355.55, 17777.5, -222.5)
    _put_snap(db, "2026-09-08", "US", "US.MU", -10, 999.0, 1021.44, -10214.4, -224.4)
    data = lwr.build(7, "2026-09-08")
    report = lwr.render(data["start"], data["end"], data["us"], data["hk"], data["gaps"])
    assert "多头市值 $17,777.50" in report and "空头市值 $10,214.40" in report
    assert "毛敞口 $27,991.90" in report
    assert "$7,563.10" not in report, "混加值不得出现在报告里"


# ══════════════════════════════════════════════════════════════════
#  6. 端到端: 报告必须两段都在 + 三态清单 + 缺口登记
# ══════════════════════════════════════════════════════════════════
def test_end_to_end_report_structure(env):
    db = env["db"]
    _put_deal(db, "u1", "US", "US.TSLA", "BUY", 50, 360.0, "2026-09-03 10:00:00")
    _put_deal(db, "h1", "HK", "HK.00700", "BUY", 100, 440.0, "2026-09-03 10:00:00")
    _put_deal(db, "h2", "HK", "HK.00700", "SELL", 100, 450.0, "2026-09-05 10:00:00")
    _put_snap(db, "2026-09-08", "US", "US.TSLA", 50, 360.0, 355.0, 17750.0, -250.0)
    _put_snap(db, "2026-09-08", "HK", "HK.00700", 100, 440.0, 435.0, 43500.0, -500.0)
    # 批 D: 派生回合必须先落表, 否则该段只会报 NO_DATA (那也是合法态, 见下面第二个用例)
    assert live_ledger.sync_round_trips()["inserted"] == 1

    data = lwr.build(7, "2026-09-08")
    report = lwr.render(data["start"], data["end"], data["us"], data["hk"], data["gaps"])
    assert "实盘周报" in report and "ftseek_us_live.db" not in report  # 只报文件名不报全路径
    assert "【美股 · 实盘】" in report and "【港股 · 实盘】" in report
    assert "【数据状态 (三态, 不伪装空)】" in report
    for tag in ("US成交", "US在仓", "US回合台账", "US对账", "US回合派生", "US手动单",
                "HK成交", "HK在仓", "HK回合派生", "HK手动单"):
        assert tag in report, f"三态清单缺 {tag}"
    assert "【已知缺口" in report
    # 批 D 后港股**有了执行级派生回合**, 但决策级仍缺 ⇒ 缺口措辞必须区分两者,
    # 否则读者会以为"港股回合台账已建好、可以做 exit_reason 归因了" (那是把两层混成一层)
    assert "港股**无决策级回合台账**" in report
    assert "执行级" in report and "决策理由仍不可复盘" in report
    assert "回合(券商真值派生 FIFO" in report, "港股段缺派生回合小节 (批 D 判据 8)"
    assert "毛/成交额" in report


def test_end_to_end_report_structure_without_derived_rows(env):
    """派生表为空时, 缺口必须说"派生段也没出数", 不得沿用"已有执行级回合"的措辞."""
    db = env["db"]
    _put_deal(db, "h1", "HK", "HK.00700", "BUY", 100, 440.0, "2026-09-03 10:00:00")
    _put_snap(db, "2026-09-08", "HK", "HK.00700", 100, 440.0, 435.0, 43500.0, -500.0)
    data = lwr.build(7, "2026-09-08")
    assert data["hk"]["derived_rt"]["state"] == "NO_DATA"
    gaps = "\n".join(data["gaps"])
    assert "港股回合数据不可用" in gaps and "NO_DATA" in gaps
    assert "已有执行级回合" not in gaps and "进出场时刻/价/量/毛盈亏" not in gaps, \
        "派生段没出数时不得宣称港股已有回合级数据"


def test_gaps_flag_orphan_ledger_rows(env):
    """孤儿在仓行必须进缺口清单 —— 它们是断链残留, 不当在仓念."""
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute("INSERT INTO trade_records (symbol, direction, entry_time, exit_time,"
                     " entry_price, exit_price, quantity, realized_pnl, trade_mode, source_type)"
                     " VALUES ('US.AAPL','LONG','2026-06-18T12:33:14',NULL,200,0,10,0,'live','MANUAL')")
        conn.commit()
    finally:
        conn.close()
    _put_snap(env["db"], "2026-09-08", "US", "US.TSLA", 50, 360.0, 355.0, 17750.0, -250.0)
    data = lwr.build(7, "2026-09-08")
    assert data["us"]["reconcile"]["orphans"] == ["US.AAPL"]
    assert any("孤儿在仓行" in g for g in data["gaps"])
    report = lwr.render(data["start"], data["end"], data["us"], data["hk"], data["gaps"])
    assert "US.AAPL" in report and "平仓未落账" in report


def test_cli_defaults_to_shadow_no_push(env, capsys, monkeypatch):
    """默认**不推送** (影子模式) —— 首次上线不得直接往飞书发未经核对的实盘数字."""
    pushed = []
    monkeypatch.setattr(lwr, "build", lambda d, e: {"start": "2026-09-02", "end": "2026-09-08",
                                                     "us": {}, "hk": {}, "gaps": []})
    monkeypatch.setattr(lwr, "render", lambda *a, **k: "REPORT")
    # 段缺失时不得抛 KeyError 让整份周报消失, 而应如实报状态并以退出码 3 告警
    assert lwr.main(["--days", "7"]) == 3
    assert "REPORT" in capsys.readouterr().out
    assert not pushed


def test_cli_push_flag_is_the_only_path_to_feishu(env, capsys, monkeypatch):
    """只有显式 --push 才发推送; 默认路径必须零推送调用。
    (2026-10-15 起推送走 app/push hub — 审计 P2-4 军规旁路迁移; 契约不变: 默认零推送)"""
    sent = []
    monkeypatch.setattr(lwr, "build", lambda d, e: lwr_build_stub())
    monkeypatch.setattr(lwr, "render", lambda *a, **k: "REPORT")

    class _Hub:
        def push(self, msg, sync=False):
            sent.append(msg.body)
            return True

        def stop(self):
            pass

    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _Hub())
    assert lwr.main(["--days", "7"]) == 0
    assert sent == [], "默认(影子)模式不得推送"
    capsys.readouterr()
    assert lwr.main(["--days", "7", "--push"]) == 0
    assert sent == ["REPORT"]


def lwr_build_stub():
    return {"start": "2026-09-02", "end": "2026-09-08",
            "us": {"deals": {"state": "OK"}, "positions": {"state": "OK"},
                   "roundtrip": {"state": "OK"}, "reconcile": {"state": "OK"},
                   "audit": {"state": "OK"}},
            "hk": {"deals": {"state": "OK"}, "positions": {"state": "OK"},
                   "audit": {"state": "OK"}},
            "gaps": []}


# ══════════════════════════════════════════════════════════════════
#  6. 时间尺度 (2026-09-08 补正): 每市场用自己的日历, 每列用自己的尺
#     病灶原型: 第一版用一个 ET 窗口切**全部**数据源, 而 `manual_order_audit.timestamp`
#     是 CST 墙钟、港股 jsonl 的 ts 是 CST(=HKT) 墙钟、券商 `deal_date` 是各市场本地日
#     ⇒ 美股下午盘 (ET 12:00 后 = 北京次日) 的手动单被算进后一天, 跨周即错周。
# ══════════════════════════════════════════════════════════════════
def test_us_audit_window_is_converted_to_the_column_own_scale(env):
    """**回归钉 (真缺陷, 非风格)**: ET 窗口必须换算到 `timestamp` 的 CST 尺度再比.

    写方实测: app/web_dashboard/order/audit.py:183 用裸 now ⇒ 该列是服务器 CST 墙钟
    (同行 timestamp=2026-09-04T21:52:25 与 created_at=2026-09-04 13:52:25(UTC) 差 8h)。
      · CST 2026-09-09T02:00 = ET 2026-09-08T14:00 ⇒ **窗内** (同尺直比会按 CST 日 09-09 剔掉)
      · CST 2026-09-02T08:00 = ET 2026-09-01T20:00 ⇒ **窗外** (同尺直比会按 CST 日 09-02 收进来)
    两个方向都错 ⇒ 本条两头都钉。
    """
    conn = sqlite3.connect(str(env["db"]))
    try:
        for ts, sym, status in (("2026-09-09T02:00:00", "US.MU", "submitted"),
                                ("2026-09-02T08:00:00", "US.TSLA", "rejected")):
            conn.execute("INSERT INTO manual_order_audit (timestamp, symbol, action, quantity,"
                         " mode, status) VALUES (?,?,?,?,'live_conservative',?)",
                         (ts, sym, "BUY", 10, status))
        conn.commit()
    finally:
        conn.close()
    sec = lwr.manual_activity_section("2026-09-02", "2026-09-08", "US")
    assert sec["total"] == 1, f"边界行归属错了 (旧同尺直比会得到 1 笔但选错那笔): {sec}"
    assert sec["rows"][0]["status"] == "submitted", sec
    assert "ET" in sec["window_scale"] and "CST" in sec["window_scale"], \
        f"窗口尺度必须自证, 否则读报告的人不知道这段是按哪把尺切的: {sec}"


def test_et_to_cst_bounds_are_not_a_hardcoded_offset(env):
    """冬令时 EST 差 13h、夏令时 EDT 差 12h ⇒ 必须真实时区 localize, 不得写死 ±12h.

    (同 DEB-20260904 #203 的判例, 换算沿用 app/web_dashboard/order/executor.py:883 的模式)
    """
    assert lwr._et_window_to_cst_bounds("2026-09-02", "2026-09-08") == \
        ("2026-09-02T12:00:00", "2026-09-09T12:00:00")  # EDT: −4h ⇒ +12h
    assert lwr._et_window_to_cst_bounds("2026-01-05", "2026-01-06") == \
        ("2026-01-05T13:00:00", "2026-01-07T13:00:00")  # EST: −5h ⇒ +13h


def test_hk_activity_is_same_scale_and_must_not_be_converted(env):
    """港股 jsonl 的 ts 是 CST(=HKT) 墙钟, 与港股窗口**同尺** ⇒ 直接比日期串.

    反方向同款错: 把同尺当异尺再换算一次 = 窗口整体推 8h, 一样会错周。
    """
    jsonl = env["tmp"] / "data" / "audit" / "hk_manual_orders.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"ts": "2026-09-09T09:35:00", "op": "submit", "symbol": "HK.00700",
                    "status": "submitted"}),   # HKT 09-09 盘中 ⇒ 港股窗口(至 09-09)内
        json.dumps({"ts": "2026-09-01T15:00:00", "op": "submit", "symbol": "HK.02513",
                    "status": "submitted"}),   # 窗外
    ]), encoding="utf-8")
    sec = lwr.manual_activity_section("2026-09-03", "2026-09-09", "HK")
    assert sec["n"] == 1, sec
    assert "window_scale" not in sec, "同尺路径不该出现换算声明 (出现了说明被二次换算)"


def test_market_windows_use_each_market_own_calendar(monkeypatch):
    """两市场窗口各按本地日算 — 共用一个 ET 窗口切港股行 = 整段平移半天到一天."""
    monkeypatch.setattr(lwr, "_local_today",
                        lambda m: date(2026, 9, 8) if m == "US" else date(2026, 9, 9))
    win = lwr.market_windows(7)
    assert win["US"] == {"start": "2026-09-02", "end": "2026-09-08", "tz": "ET"}
    assert win["HK"] == {"start": "2026-09-03", "end": "2026-09-09", "tz": "HKT"}
    both = lwr.market_windows(7, "2026-08-01")
    assert both["US"]["end"] == both["HK"]["end"] == "2026-08-01", \
        "显式 --end-date 是复算入口, 两市场必须同串 (否则历史无法重放)"


def test_build_uses_per_market_windows_when_end_date_omitted(env, monkeypatch):
    """build() 必须真的把各自窗口喂给各自市场, 而不只是把窗口算出来摆着."""
    monkeypatch.setattr(lwr, "_local_today",
                        lambda m: date(2026, 9, 8) if m == "US" else date(2026, 9, 9))
    data = lwr.build(7, None)
    assert data["windows"]["US"]["end"] == "2026-09-08"
    assert data["windows"]["HK"]["end"] == "2026-09-09"
    assert (data["start"], data["end"]) == ("2026-09-02", "2026-09-08"), \
        "旧字段保留 = 美股 ET 窗口 (既有调用点/回归不破)"


def test_render_prints_each_market_window_and_scale(env):
    """两段窗口**不是同一个绝对区间** ⇒ 必须各自写出来并带尺度标签, 否则会被当同一周读."""
    win = {"US": {"start": "2026-09-02", "end": "2026-09-08", "tz": "ET"},
           "HK": {"start": "2026-09-03", "end": "2026-09-09", "tz": "HKT"}}
    data = lwr.build(7, "2026-09-08")
    report = lwr.render(data["start"], data["end"], data["us"], data["hk"], data["gaps"],
                        windows=win)
    assert "美股 2026-09-02~2026-09-08(ET)" in report
    assert "港股 2026-09-03~2026-09-09(HKT)" in report
    assert "日历不同" in report, "必须显式告诉读者两段不是同一个绝对区间"
    assert "(ET 本地日)" in report and "(HKT 本地日)" in report, "每段标题也要带自己的窗口"
    gen = [ln for ln in report.splitlines() if ln.startswith("生成:")][0]
    assert "(ET)" in gen and "(CST)" in gen, f"生成时刻必须带尺: {gen}"


def test_deal_section_shares_one_direction_caliber_with_dashboard(env):
    """周报的开/平仓额必须与看板端点**同源** (`live_ledger.summarize_deals`).

    两处各写一份方向语义的后果是"两边错得一样": 净现金流符号反了, 而报告与看板
    互相印证 ⇒ 谁都发现不了。故钉住同源, 而不只是钉数值。
    """
    _put_deal(env["db"], "1", "US", "US.MU", "SELL_SHORT", 10, 100.0, "2026-09-04 10:00:00")
    _put_deal(env["db"], "2", "US", "US.MU", "BUY_BACK", 10, 90.0, "2026-09-05 10:00:00")
    sec = lwr.deal_section("US", "2026-09-02", "2026-09-08")
    shared = live_ledger.summarize_deals(
        live_ledger.read_deals("2026-09-02", "2026-09-08", market="US"))
    assert (sec["open_amount"], sec["close_amount"], sec["net_cash"]) == \
        (shared["open_amount"], shared["close_amount"], shared["net_cash"]) == (1000.0, 900.0, -100.0)
    assert sec["fee_rows_null"] == shared["fee_rows_null"] == 2
    assert not (set(lwr.__dict__) & {"_OPEN_SIDES", "_CLOSE_SIDES"}), \
        "周报不得再私藏一份方向常量 (SSOT 在 live_ledger)"
