"""E1 前瞻影子臂采集器 交易日口径回归测试 (2026-08-28).

病症 (DEB-20260815 §16.4 W0 基建, 建线以来 13 天空转): 阶段2 用
  datetime.now().strftime("%Y-%m-%d") = 服务器 CST 墙钟日期查 signal_snapshots,
而 trade_date 列是 ET 交易日。本 job 排 04:45 CST = 16:45 ET, 那一刻 CST 日历日
已比 ET 大 1 天 → WHERE trade_date='CST今日' 恒 0 行。实证: data/
e1_shadow_signals.jsonl 自 2026-08-15 建线起 0 字节 (Hermes 9 次运行全
last_status=ok — "日志正常但逻辑失效"同族), 而同期 signal_snapshots 里有
184 条唯一 DA/UE LONG|SHORT 信号 (9 个交易日, 判据 n≥30 的 6 倍)。
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import app.utils.timezone as tzmod
import scripts.e1_shadow_collector as e1

ET_DAY = "2026-08-27"          # 被采集的交易日 (job 跑于 08-28 04:45 CST = 08-27 16:45 ET)
CST_TODAY = "2026-08-28"       # 同一时刻的服务器墙钟日期 — 旧口径用它查询


def _mk_db(path: Path):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE signal_snapshots (
        symbol TEXT, trade_date TEXT, direction TEXT,
        strength TEXT, strategy_id TEXT)""")
    con.executemany(
        "INSERT INTO signal_snapshots VALUES (?,?,?,?,?)",
        [("US.FOO", ET_DAY, "LONG", "4", "deep_analysis"),
         ("US.BAR", ET_DAY, "SHORT", "3", "universe_expansion"),
         ("US.FOO", ET_DAY, "LONG", "4", "deep_analysis"),      # 重复 → 去重
         ("US.IGN", ET_DAY, "NEUTRAL", "5", "deep_analysis"),   # 非 LONG/SHORT → 排除
         ("US.OLD", "2026-08-26", "LONG", "5", "deep_analysis"),  # 非今日 → 排除
         ("US.VOT", ET_DAY, "LONG", "5", "voting_llm")])          # 非 DA/UE → 排除
    con.commit()
    con.close()


def _patch(monkeypatch, tmp_path):
    db = tmp_path / "signal_snapshots.db"
    _mk_db(db)
    out = tmp_path / "e1.jsonl"
    monkeypatch.setattr(e1, "SNAP_DB", db)
    monkeypatch.setattr(e1, "OUT", out)
    monkeypatch.setattr(e1, "CACHE_DIR", tmp_path / "bars")
    # 免实盘 Futu: 合成日线 (08-27 open=100, 次日 open=103)
    bars = {"2026-08-27": ["2026-08-27", 100.0, 101.0, 99.0, 100.5],
            "2026-08-28": ["2026-08-28", 103.0, 104.0, 102.0, 103.5]}
    monkeypatch.setattr(e1, "get_daily",
                        lambda sym, date: (bars, list(bars.keys())))
    return out


def _run_as(monkeypatch, et_day):
    monkeypatch.setattr(tzmod, "get_trade_date", lambda *a, **k: et_day)


def test_collects_et_trade_day_not_server_date(monkeypatch, tmp_path):
    """04:45 CST 运行必须采到 ET 08-27 的信号 (去重后 2 条)."""
    out = _patch(monkeypatch, tmp_path)
    _run_as(monkeypatch, ET_DAY)
    e1.main()
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert {r["symbol"] for r in rows} == {"US.FOO", "US.BAR"}
    assert len(rows) == 2, "同 (symbol,trade_date,direction) 必须去重"
    by = {r["symbol"]: r for r in rows}
    assert by["US.FOO"]["signal_day_open"] == 100.0
    assert by["US.BAR"]["borrow_fee_annual_pct"] == 0.05   # SHORT 保守借券费
    assert by["US.FOO"]["borrow_fee_annual_pct"] == 0.0    # LONG 无
    assert datetime.fromisoformat(by["US.FOO"]["collected_at"]).tzinfo is not None, \
        "采集戳须 tz-aware (naive CST 墙钟混进 ET 文件 = id=1320 同类陷阱)"


def test_old_server_date_caliber_would_collect_nothing(monkeypatch, tmp_path):
    """旧口径对照 (钉住病症): 同一时刻的服务器日期查不到任何行 → 文件恒空."""
    out = _patch(monkeypatch, tmp_path)
    _run_as(monkeypatch, CST_TODAY)
    e1.main()
    assert out.read_text().strip() == "", "CST 今日口径若采到数据 = 口径又漂移了"


def test_backfill_fills_next_open(monkeypatch, tmp_path):
    """阶段1 回填: 已记录信号补齐次一交易日开盘价 (GATE 1 结算口径)."""
    out = _patch(monkeypatch, tmp_path)
    _run_as(monkeypatch, ET_DAY)
    e1.main()
    # 次日 08-28 开盘 103 落进记录 (幂等重写不丢字段)
    e1.main()
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 2, "重跑必须幂等 (不重复追加)"
    assert all(r["next_open_price"] == 103.0 and r["next_open_date"] == "2026-08-28"
               for r in rows)
