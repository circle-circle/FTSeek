"""daily_alert_summary 账户权益对账回归测试 (2026-08-29).

背景 (用户裁决"同意"): 富途 App "今日盈亏 +5百多" 与日报 "-338.76" 被当成矛盾人工拷问,
两个数本就不同口径 (App=账户全口径含未平仓当日波动, 且模拟盘 today_pl_val 是**毛额**;
日报=机器人当日已了结净额)。裁决是把差异每晚机器化摊开成一行观测, 残差超容差才标 ⚠️。

因此本测试锁定的不是"残差等于某个数", 而是**这台机器坏了必须自己说出口**:
  1. 桥接恒等式在合成数据上精确闭合 (取数口径没串);
  2. 任一输入缺失/异常/跨账户 → verdict=not_judgeable 并写明原因, 严禁退化成 0
     (2026-08-29 一天内三次踩到"只能报健康的守卫": journalctl 数 ERROR / /proc mtime /
      awk '/ ERROR /' 对上日志里的 "] ERROR:") —— 每个 not_judgeable 用例都要能证伪;
  3. 权益样本必须取收盘**后**首样本 (收盘印), 取到收盘前十几秒的盘中共价会把 $40 级别的
     偏差直接灌进残差 (8/28 首跑实测, 残差 57.06 → 16.68)。

回滚: 删本文件即可, 生产侧只回退日报里的一行观测。
"""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_alert_summary", PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
)
das = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(das)

SESSION = "2026-08-27"      # ET 交易日 (周四)
PREV = "2026-08-26"


def _put_equity(db, last_update, equity, trade_env=1):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO capital_state (current_equity, last_update, trade_env) VALUES (?, ?, ?)",
        (equity, last_update, trade_env),
    )
    conn.commit()
    conn.close()


def _put_trade(db, symbol, exit_time, exit_price, realized_pnl, fees_est, pnl_net=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "realized_pnl, fees_est, pnl_net) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (symbol, (exit_time or "")[:10], exit_time, exit_price, realized_pnl, fees_est, pnl_net),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def recon_db(tmp_path):
    """合成一个可精确闭合的交易日: C=-500 = G(-300) + U(-200).

    权益样本每个边界放两条 —— 收盘前 10 多秒的"盘中共价"诱饵 + 收盘后首样本 (正确值)。
    诱饵与正确值差 $1000/$1500, 任何"取最近样本/取更早样本"的实现都会被抓出来。
    """
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, realized_pnl REAL, fees_est REAL, pnl_net REAL)"
    )
    conn.execute(
        "CREATE TABLE capital_state (id INTEGER PRIMARY KEY, current_equity REAL, "
        "last_update TEXT, trade_env INTEGER DEFAULT 1)"
    )
    conn.commit()
    conn.close()

    # 前收边界 = 2026-08-27T04:00 CST (8/26 16:00 ET)
    _put_equity(db, "2026-08-27T03:59:46.000000", 100000)   # 诱饵 (收盘前共价)
    _put_equity(db, "2026-08-27T04:00:20.000000", 99000)    # 前收印
    # 今收边界 = 2026-08-28T04:00 CST (8/27 16:00 ET)
    _put_equity(db, "2026-08-28T03:59:50.000000", 98000)    # 诱饵
    _put_equity(db, "2026-08-28T04:00:15.000000", 98500)    # 今收印 → C = -500

    _put_trade(db, "US.AAA", f"{SESSION}T10:00:00-04:00", 10.0, -400.0, 30.0, -430.0)
    _put_trade(db, "US.BBB", f"{SESSION}T11:00:00-04:00", 10.0, 100.0, 20.0, 80.0)
    # 未平仓行 (exit_price=0) 带占位盈亏 → 不得混入已了结侧
    _put_trade(db, "US.CCC", None, 0.0, 9999.0, 0.0, None)
    # 前一日出场的行 → 不得混入当日
    _put_trade(db, "US.DDD", f"{PREV}T15:00:00-04:00", 10.0, -777.0, 5.0, -782.0)
    return db


def _recon(db, broker=-200.0, session_date=SESSION, trade_date=SESSION):
    calls = []

    def fetch():
        calls.append(1)
        return (broker, None) if not isinstance(broker, tuple) else broker

    out = das.get_us_equity_reconciliation(trade_date, db_path=db, broker_fetch=fetch,
                                           session_date=session_date)
    out["_broker_calls"] = len(calls)
    return out


# ================== 桥接闭合与取数口径 ==================

def test_bridge_closes_on_closing_prints(recon_db):
    r = _recon(recon_db)
    assert r["verdict"] == "ok"
    assert r["equity_change"] == pytest.approx(-500.0)     # 用收盘印, 不是诱饵 (-1500/-1000)
    assert r["realized_gross"] == pytest.approx(-300.0)    # 未平仓行/前一日行均未混入
    assert r["fees"] == pytest.approx(50.0)
    assert r["realized_net"] == pytest.approx(-350.0)      # 日报净额口径
    assert r["residual"] == pytest.approx(0.0)
    assert r["within_tolerance"] is True


def test_residual_over_tolerance_flagged(recon_db):
    # 今收印再跌 $200 (模拟费用/成交价偏差越过容差) → 必须报 ⚠️, 而不是静默
    conn = sqlite3.connect(str(recon_db))
    conn.execute("UPDATE capital_state SET current_equity=98300 "
                 "WHERE last_update='2026-08-28T04:00:15.000000'")
    conn.commit()
    conn.close()
    r = _recon(recon_db)
    assert r["verdict"] == "ok" and r["within_tolerance"] is False
    assert r["residual"] == pytest.approx(-200.0)
    assert "⚠️" in das._format_equity_recon_line(r)


def test_broker_side_read_once_per_report(recon_db):
    assert _recon(recon_db)["_broker_calls"] == 1


# ================== 不可判: 每个输入缺失都必须自己说出口 ==================

def test_missing_today_post_close_sample_not_judged(recon_db):
    conn = sqlite3.connect(str(recon_db))
    conn.execute("DELETE FROM capital_state WHERE last_update='2026-08-28T04:00:15.000000'")
    conn.commit()
    conn.close()
    r = _recon(recon_db)
    assert r["verdict"] == "not_judgeable"
    assert "今收" in r["reason"]
    assert r["equity_change"] is None and r["residual"] is None  # 严禁拿诱饵凑 / 以 0 冒充


def test_sample_beyond_window_not_judged(recon_db):
    """窗内无样本 → 不判。放宽窗口不是安全边际, 而是把盘中/盘后漂移灌进残差。"""
    conn = sqlite3.connect(str(recon_db))
    conn.execute("DELETE FROM capital_state WHERE last_update='2026-08-28T04:00:15.000000'")
    conn.execute("INSERT INTO capital_state (current_equity, last_update, trade_env) "
                 "VALUES (98500, '2026-08-28T04:30:00.000000', 1)")   # 窗 (15min) 外
    conn.commit()
    conn.close()
    r = _recon(recon_db)
    assert r["verdict"] == "not_judgeable" and "今收" in r["reason"]


def test_missing_prev_close_sample_says_so(recon_db):
    """美股假日后的周一必走此路 (休市日采样停摆) — 必须写明"属预期", 免得被读成探针坏了。"""
    conn = sqlite3.connect(str(recon_db))
    conn.execute("DELETE FROM capital_state WHERE last_update LIKE '2026-08-27T04%'")
    conn.commit()
    conn.close()
    r = _recon(recon_db)
    assert r["verdict"] == "not_judgeable" and "前收" in r["reason"]
    assert "休市" in r["reason"] and "预期" in r["reason"]
    assert r["equity_change"] is None and r["residual"] is None


def test_missing_capital_state_table_not_judged(tmp_path):
    db = tmp_path / "noflow.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "trade_date TEXT, exit_time TEXT, exit_price REAL, realized_pnl REAL, "
                 "fees_est REAL, pnl_net REAL)")
    conn.commit()
    conn.close()
    r = _recon(db, broker=(None, "不该走到这步"))
    assert r["verdict"] == "not_judgeable" and "capital_state" in r["reason"]
    assert r["_broker_calls"] == 0      # 权益侧无载体就不该去麻烦券商


def test_mixed_trade_env_not_judged(recon_db):
    """券商侧按 .env.us 解析账户, 权益侧必须同账户 —— 混入第二套 env 序列时不判 (8/11 同课)。"""
    _put_equity(recon_db, "2026-08-28T04:00:40.000000", 500000, trade_env=2)
    conn = sqlite3.connect(str(recon_db))
    conn.execute("DELETE FROM capital_state WHERE last_update='2026-08-28T04:00:15.000000'")
    conn.commit()
    conn.close()
    r = _recon(recon_db)
    assert r["verdict"] == "not_judgeable" and "trade_env" in r["reason"]


def test_broker_unreachable_not_judged(recon_db):
    r = _recon(recon_db, broker=(None, "OpenD 未启动"))
    assert r["verdict"] == "not_judgeable"
    assert "OpenD 未启动" in r["reason"]
    assert r["open_mv_change"] is None and r["residual"] is None
    line = das._format_equity_recon_line(r)
    assert "不以 0 冒充已对账" in line and "不可判" in line


def test_unreadable_db_not_judged(tmp_path):
    r = _recon(tmp_path / "missing.db", broker=(None, "不该走到这步"))
    assert r["verdict"] == "not_judgeable" and "库不可读" in r["reason"]


def test_historical_date_never_queried(recon_db):
    """券商 today_pl_val 只描述当前会话 → 回填历史日一律不判, 且不去读券商 (会拼出假闭合)。"""
    r = _recon(recon_db, session_date=SESSION, trade_date=PREV)
    assert r["verdict"] == "not_judgeable"
    assert "非当前会话" in r["reason"]
    assert r["_broker_calls"] == 0


def test_exception_inside_is_reported_not_swallowed(recon_db, monkeypatch):
    """探测器自身异常不得读成"已对账/无差异" (§6 规则5 同课)。"""
    monkeypatch.setattr(das, "_equity_at", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _recon(recon_db)
    assert r["verdict"] == "not_judgeable" and "boom" in r["reason"]
    assert r["residual"] is None


# ================== 时间边界 (ET→CST) ==================

def test_close_boundary_edt_and_est():
    # 首版曾多算一天 (16:00 + 12h 本就跨 CST 午夜), 边界落到未来 → 判据立即退化成"样本缺失"
    assert das._cst_close_boundary("2026-08-27") == "2026-08-28T04:00:00"
    assert das._cst_close_boundary("2026-11-27") == "2026-11-28T05:00:00"   # EST: +13h


def test_prev_trade_date_skips_weekend():
    assert das._prev_us_trade_date("2026-08-31") == "2026-08-28"   # 周一 → 上周五
    assert das._prev_us_trade_date("2026-09-01") == "2026-08-31"   # 周二 → 周一


# ================== 券商侧取数 (env 解析 / 双计守卫 / 拒绝部分求和) ==================

class _Ctx:
    def __init__(self, ret, pos, sink):
        self._ret, self._pos, self._sink = ret, pos, sink

    def position_list_query(self, **kw):
        self._sink.append(kw)
        return self._ret, self._pos

    def close(self):
        pass


def _fake_broker(monkeypatch, rows, env_map=None, ret=0):
    """装一个假 futu SDK + 假 .env.us, 只驱动被测函数自身的解析/求和分支。"""
    sink = []
    pos = pd.DataFrame(rows, columns=["code", "qty", "today_pl_val"])
    trd_env = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")

    mod = types.ModuleType("futu")
    mod.TrdEnv = trd_env
    mod.TrdMarket = types.SimpleNamespace(US="US")
    mod.OpenSecTradeContext = lambda **kw: _Ctx(ret, pos, sink)
    monkeypatch.setitem(sys.modules, "futu", mod)

    env = {"AUTO_TRADE_ACCOUNT": "sim", "SIMULATE_ACCOUNT_ID": "19930823",
           "REAL_ACCOUNT_ID": "281756480166019917", "ALLOW_REAL_TRADING": "false",
           "FUTU_OPEND_HOST": "127.0.0.1", "FUTU_OPEND_PORT": "11111"}
    env.update(env_map or {})
    import app.core.trading_mode as tm
    monkeypatch.setattr(tm, "_read_env_file",
                        lambda key, default="": env.get(key, default))
    return sink


def test_qty_zero_rows_excluded_to_prevent_double_count(monkeypatch):
    # qty==0 是"当日已平"的剩余列表项, 其 today_pl_val 已由 trade_records 计入 G
    _fake_broker(monkeypatch, [
        ("US.AAA", 100, -50.0),
        ("US.CCC", 0, -9999.0),      # 已平 → 排除, 否则桥接假闭合
        ("US.DDD", -20, 10.0),       # SHORT 负数量 → 仍计入
    ])
    total, why = das._fetch_broker_open_mv_change()
    assert why is None and total == pytest.approx(-40.0)


def test_non_numeric_today_pl_refuses_partial_sum(monkeypatch):
    _fake_broker(monkeypatch, [("US.AAA", 100, -50.0), ("US.BBB", 20, "N/A")])
    total, why = das._fetch_broker_open_mv_change()
    assert total is None and "非数值" in why        # 拒绝只加一半 (部分求和=假残差)


def test_real_account_without_gate_downgrades_to_sim(monkeypatch):
    sink = _fake_broker(monkeypatch, [("US.AAA", 1, 0.0)],
                        env_map={"AUTO_TRADE_ACCOUNT": "real", "ALLOW_REAL_TRADING": "false"})
    das._fetch_broker_open_mv_change()
    assert sink[0]["trd_env"] == "SIMULATE" and sink[0]["acc_id"] == 19930823


def test_real_account_with_gate_reads_real(monkeypatch):
    sink = _fake_broker(monkeypatch, [("US.AAA", 1, 0.0)],
                        env_map={"AUTO_TRADE_ACCOUNT": "real", "ALLOW_REAL_TRADING": "true"})
    das._fetch_broker_open_mv_change()
    assert sink[0]["trd_env"] == "REAL" and sink[0]["acc_id"] == 281756480166019917


def test_query_failure_returns_reason(monkeypatch):
    _fake_broker(monkeypatch, [("US.AAA", 1, 0.0)], ret=-1)
    total, why = das._fetch_broker_open_mv_change()
    assert total is None and "position_list_query 失败" in why


def test_missing_account_id_refused(monkeypatch):
    _fake_broker(monkeypatch, [("US.AAA", 1, 0.0)], env_map={"SIMULATE_ACCOUNT_ID": "0"})
    total, why = das._fetch_broker_open_mv_change()
    assert total is None and "账户 ID" in why


# ================== 报告渲染 ==================

def test_report_renders_recon_line(recon_db):
    r = _recon(recon_db)
    # 2026-09-14 同步 34e4a608a: 港股段退役删掉 hk_alerts/hk_summary 两个位置形参,
    # 本文件当时未跟改 ⇒ 位置实参整体左移一位 (report_date 吃到 summary 字典) →
    # "got multiple values for argument 'us_equity_recon'"。新签名
    # (us_alerts, us_summary, report_date, us_equity_recon=None, log_coverage=None)。
    body = das.build_summary_report([], {"trades": 2, "pnl": -350.0, "positions": 0, "errors": 0},
                                    "2026-08-27", us_equity_recon=r)
    assert "权益对账" in body and "观测, 不进判据" in body


def test_report_without_recon_is_unchanged():
    body = das.build_summary_report([], {"trades": 0, "pnl": 0.0, "positions": 0, "errors": 0},
                                    "2026-08-27")
    assert "权益对账" not in body
