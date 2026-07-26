#!/usr/bin/env python3
"""FTSeek-US 端到端冒烟测试

覆盖完整链路:
1. Deepulse注入 → playbook_signal_context
2. 数据采集 → 6维度数据
3. 信号生成 → VotingCoordinator 投票
4. 入场 → DualBroker.submit_order (SIM)
5. 止损 → monitor_playbook_sim_exits SL_FIXED_PCT
6. EOD平仓 → _force_close_orphaned_sim_positions
7. 归因 → _close_trade_record → compute_attribution
8. IC回填 → signal_lifecycle 同步

不真正下单 — 用已有的AMD/BABA持仓模拟平仓+归因链路。
用真实数据采集和Deepulse注入验证信号生成链路。
"""
import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

# Bootstrap
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# Use venv
os.environ.setdefault("AUTO_TRADE_ACCOUNT", "sim")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("e2e_smoke")

PASS = 0
FAIL = 0
WARN = 0
ERRORS = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def warn(name, detail=""):
    global WARN
    WARN += 1
    print(f"  ⚠️ {name} — {detail}")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# T1: Deepulse 注入链路
# ============================================================
def test_deepulse_injection():
    section("T1: Deepulse 注入链路")
    from app.playbook.pipeline import _persist_signal_context, _build_data_collector

    # 用 NVDA (在 theme_context.json 中)
    collector = _build_data_collector()
    data = collector.collect("US.NVDA")
    today = datetime.now().strftime("%Y-%m-%d")
    _persist_signal_context(today, "US.NVDA", data)

    db = sqlite3.connect("data/ftseek_us.db")
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT * FROM playbook_signal_context WHERE date=? AND symbol='US.NVDA' ORDER BY created_at DESC LIMIT 1",
        (today,)
    ).fetchone()

    check("Deepulse theme_momentum 注入", row and row["theme_momentum"] is not None, f"momentum={row['theme_momentum'] if row else 'N/A'}")
    check("Deepulse theme_wind 注入", row and row["theme_wind"] is not None, f"wind={row['theme_wind'] if row else 'N/A'}")
    check("Deepulse theme_narratives 注入", row and row["theme_narratives_json"] is not None, f"narratives={str(row['theme_narratives_json'])[:60] if row else 'N/A'}")
    check("News headlines 注入", row and row["news_headlines_json"] is not None, "headlines为NULL")
    check("Insider signal 注入", row and row["insider_signal"] is not None, f"signal={row['insider_signal'] if row else 'N/A'}")
    check("PE 注入", row and row["pe"] is not None, f"pe={row['pe'] if row else 'N/A'}")
    check("Earnings 注入", row and row["earnings_next_date"] is not None, f"next={row['earnings_next_date'] if row else 'N/A'}")

    db.close()
    return row


# ============================================================
# T2: 信号生成 → VotingCoordinator
# ============================================================
def test_signal_generation():
    # 2026-07-14: VotingCoordinator 死投票脑已清理; 双脑架构入场走 DA bridge → BridgeVotingAdapter.
    section("T2: 信号生成 (旧 3 模型投票已退役)")
    check("T2 跳过", True, "voting 脑已删除; 入场经 DA bridge, 见 shadow_board / da_chain_health")


# ============================================================
# T3: 入场 → DualBroker (查持仓, 不下单)
# ============================================================
def test_entry_mechanism():
    section("T3: 入场机制 → DualBroker")
    from app.execution.dual_broker import get_dual_broker

    dual = get_dual_broker()
    check("DualBroker连接", dual._connected, "未连接")
    check("SIM ctx可用", dual._sim_ctx is not None, "sim_ctx为None")
    check("SIM account有值", bool(dual.sim_account_id), f"acc={dual.sim_account_id}")

    # 查持仓
    positions = dual.get_positions(target_env="sim")
    check("查询SIM持仓", len(positions) > 0, "无持仓")

    # 验证已知持仓
    symbols = [p.get("code", "") for p in positions if "US." in p.get("code", "")]
    check("AMD在持仓中", "US.AMD" in symbols, f"持仓: {symbols}")
    check("BABA在持仓中", "US.BABA" in symbols, f"持仓: {symbols}")
    # 注意: 不调 disconnect — T4 需要复用同一个 DualBroker 单例


# ============================================================
# T4: close_position 平仓前/后验证 (用已有AMD持仓模拟)
# ============================================================
def test_close_position_mechanism():
    section("T4: close_position 平仓前/后验证 (不真正平仓)")
    from app.execution.dual_broker import get_dual_broker

    dual = get_dual_broker()

    # 测试1: 不存在的标的 → 应返回 broker_no_position
    result = dual.close_position("US.FAKE", 100, "LONG", target_env="sim")
    check("不存在的标的返回broker_no_position",
          "broker_no_position" in result.get("error", "") or result.get("price_source") == "broker_empty",
          f"error={result.get('error')} src={result.get('price_source')}")

    # 测试2: AMD 有持仓 → 验证平仓前查询逻辑
    ctx, acc_id, trd_env, lock = dual._ctx_and_acc("sim")
    from futu import RET_OK
    with lock:
        ret, pos_data = ctx.position_list_query(trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0)
    amd_qty = 0
    if ret == RET_OK and pos_data is not None and not pos_data.empty:
        matches = pos_data[pos_data["code"] == "US.AMD"]
        if not matches.empty:
            amd_qty = abs(float(matches.iloc[0].get("qty", 0) or 0))
    check("AMD broker持仓查询", amd_qty > 0, f"qty={amd_qty}")

    # 测试3: history_deal_list_query 可调用 (不报错)
    now = datetime.now()
    try:
        with lock:
            ret, data = ctx.history_deal_list_query(
                start=now.strftime("%Y-%m-%d"),
                end=now.strftime("%Y-%m-%d"),
                trd_env=trd_env,
            )
        check("history_deal_list_query 不报错", ret is not None, f"ret={ret}")
    except Exception as e:
        check("history_deal_list_query 不报错", False, str(e))

    dual.disconnect()


# ============================================================
# T5: _close_trade_record + 归因写入
# ============================================================
def test_attribution_write():
    section("T5: _close_trade_record + 归因写入 (用虚拟trade)")
    from app.playbook.pipeline import _close_trade_record
    from app.storage.trade_store import TradeStore
    from app.core.paths import get_trade_records_db

    # 创建一条虚拟trade_record
    ts = TradeStore(str(get_trade_records_db()))
    trade_id = ts.save_trade({
        "trace_id": "e2e-smoke-test",
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": "US.SMOKE",
        "direction": "LONG",
        "entry_time": datetime.now().isoformat(),
        "exit_time": "",
        "entry_price": 100.0,
        "exit_price": 0,
        "quantity": 10,
        "realized_pnl": 0,
        "pnl_pct": 0,
        "exit_reason": "",
        "signal_strength": 50,
        "holding_hours": 0,
        "source_type": "PLAYBOOK_SIM",
        "owner_strategy": "playbook_sim",
        "metadata_json": json.dumps({"test": True}),
    })
    check("虚拟trade_record创建", trade_id > 0, f"trade_id={trade_id}")

    if trade_id > 0:
        # 调用 _close_trade_record (会触发归因)
        conn = sqlite3.connect(str(get_trade_records_db()))
        _close_trade_record(
            conn, trade_id,
            exit_price=105.0,
            exit_reason="EOD_FORCE_CLOSE",
            pnl=50.0,
            pnl_pct=0.05,
            holding_hours=6.5,
            inst_id=None,
            symbol="US.SMOKE",
        )
        conn.close()

        # 验证: trade_records 更新了
        conn = sqlite3.connect(str(get_trade_records_db()))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trade_records WHERE id=?", (trade_id,)).fetchone()
        check("exit_price写入", row["exit_price"] == 105.0, f"exit={row['exit_price']}")
        check("exit_reason写入", row["exit_reason"] == "EOD_FORCE_CLOSE", f"reason={row['exit_reason']}")

        # 验证: metadata中有attribution
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        check("归因写入metadata", "attribution" in meta, f"meta_keys={list(meta.keys())}")
        if "attribution" in meta:
            attr = meta["attribution"]
            check("归因有summary", bool(attr.get("summary")), "summary为空")
            check("归因有overall_score", "overall_score" in attr, f"keys={list(attr.keys())}")
            check("归因有dimensions", len(attr.get("dimensions", [])) > 0, "dimensions为空")
            print(f"    → summary: {attr.get('summary')}")
            print(f"    → score: {attr.get('overall_score')}")

        # 清理: 删除虚拟trade
        conn.execute("DELETE FROM trade_records WHERE id=?", (trade_id,))
        conn.execute("DELETE FROM open_positions WHERE symbol='US.SMOKE'")
        conn.commit()
        conn.close()
        print("    → 虚拟trade已清理")


# ============================================================
# T6: EOD兜底逻辑 (用DB模拟, 不真正平仓)
# ============================================================
def test_eod_logic():
    section("T6: EOD兜底逻辑 (DB模拟)")
    from app.core.paths import get_trade_records_db

    # 验证: _force_close_orphaned_sim_positions 能查到未平仓的PLAYBOOK_SIM仓位
    conn = sqlite3.connect(str(get_trade_records_db()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, symbol, direction, entry_price, quantity, metadata_json
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
        ORDER BY id DESC
    """).fetchall()
    check("有未平仓PLAYBOOK_SIM仓位", len(rows) > 0, "无未平仓仓位")
    for r in rows:
        meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        fail_count = meta.get("eod_close_fail_count", 0)
        print(f"    → {r['symbol']:12s} qty={r['quantity']:>4} entry=${r['entry_price']:>8.2f} eod_fail={fail_count}")

    # 验证: close_position的新返回字段在EOD代码中被正确处理
    # 检查 EOD 代码是否匹配 "broker_no_position"
    with open(PROJECT_ROOT / "app" / "playbook" / "pipeline.py") as f:
        content = f.read()
    eod_section = content[content.find("def _force_close_orphaned_sim_positions"):]
    check("EOD匹配broker_no_position", "broker_no_position" in eod_section, "缺少broker_no_position匹配")
    check("EOD检查broker_qty_after", "broker_qty_after" in eod_section, "缺少平仓后验证")
    check("EOD写exit_price_source", "exit_price_source" in eod_section, "缺少price_source写入")

    conn.close()


# ============================================================
# T7: SL止损逻辑
# ============================================================
def test_sl_logic():
    section("T7: SL止损逻辑 (代码验证)")
    with open(PROJECT_ROOT / "app" / "playbook" / "pipeline.py") as f:
        content = f.read()

    # SL逻辑在 _check_playbook_exit_rules + monitor_playbook_sim_exits 中
    # 截取从 _check_playbook_exit_rules 到 _force_close_orphaned_sim_positions
    sl_section = content[content.find("def _check_playbook_exit_rules"):content.find("def _force_close_orphaned_sim_positions")]

    check("SL止损标签活跃", "SL_INITIAL" in sl_section, "未找到 SL_INITIAL(拆分后的止损标签)")
    check("15min开盘冷却", "15" in sl_section and "冷却" in sl_section, "未找到开盘冷却")
    check("breakeven ratchet", "breakeven" in sl_section.lower(), "未找到breakeven")
    check("trailing stop", "trail" in sl_section.lower(), "未找到trailing")
    check("5min grace", "5" in sl_section and "grace" in sl_section.lower(), "未找到grace")
    check("SL匹配broker_no_position", "broker_no_position" in sl_section, "SL路径缺少broker_no_position匹配")
    check("SL写exit_price_source", "exit_price_source" in sl_section, "SL路径缺少price_source写入")


# ============================================================
# T8: signal_lifecycle 同步
# ============================================================
def test_signal_lifecycle():
    section("T8: signal_lifecycle 同步")
    db = sqlite3.connect("data/ftseek_us.db")
    db.row_factory = sqlite3.Row

    stats = db.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN fwd_ret_t1 IS NOT NULL THEN 1 ELSE 0 END) as t1,
            SUM(CASE WHEN fwd_ret_t5 IS NOT NULL THEN 1 ELSE 0 END) as t5,
            SUM(CASE WHEN fwd_ret_t20 IS NOT NULL THEN 1 ELSE 0 END) as t20
        FROM signal_lifecycle
    """).fetchone()
    print(f"    → total={stats['total']} t1={stats['t1']} t5={stats['t5']} t20={stats['t20']}")
    check("signal_lifecycle有数据", stats["total"] > 0, "表空")
    check("T1有填充", stats["t1"] > 0, "T1全空")
    check("T20有填充", stats["t20"] > 0, "T20全空")

    # 验证: 自动同步代码存在
    with open(PROJECT_ROOT / "app" / "playbook" / "pipeline.py") as f:
        content = f.read()
    check("盘后自动同步signal_lifecycle", "update_signal_lifecycle" in content, "未找到自动同步")

    db.close()


# ============================================================
# T9: 信号采集器 (data_collector) 各维度
# ============================================================
def test_data_collector():
    section("T9: 数据采集器6维度")
    from app.playbook.pipeline import _build_data_collector

    collector = _build_data_collector()
    data = collector.collect("US.NVDA")

    check("Fundamental采集", data.fundamental is not None and data.fundamental.pe is not None, f"pe={data.fundamental.pe if data.fundamental else 'N/A'}")
    check("News采集", data.news is not None and len(data.news.headlines) > 0, f"headlines={len(data.news.headlines) if data.news else 0}")
    check("Insider采集", data.insider is not None and data.insider.signal, f"signal={data.insider.signal if data.insider else 'N/A'}")
    check("Earnings采集", data.earnings is not None and data.earnings.next_date, f"next={data.earnings.next_date if data.earnings else 'N/A'}")
    check("Technical采集", data.technical is not None, "technical为None")
    check("Sentiment采集", data.sentiment is not None, "sentiment为None")


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  FTSeek-US 端到端冒烟测试")
    print(f"  时间: {datetime.now().isoformat()}")
    print("=" * 60)

    tests = [
        ("T1: Deepulse注入", test_deepulse_injection),
        ("T2: 信号生成", test_signal_generation),
        ("T3: 入场机制", test_entry_mechanism),
        ("T4: 平仓机制", test_close_position_mechanism),
        ("T5: 归因写入", test_attribution_write),
        ("T6: EOD兜底", test_eod_logic),
        ("T7: SL止损", test_sl_logic),
        ("T8: signal_lifecycle", test_signal_lifecycle),
        ("T9: 数据采集器", test_data_collector),
    ]

    for name, func in tests:
        try:
            func()
        except Exception as e:
            FAIL_count = 1
            ERRORS.append(f"{name}: 异常 {e}")
            import traceback
            print(f"  💥 {name} 异常: {e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"  结果: ✅ {PASS}  ❌ {FAIL}  ⚠️ {WARN}")
    print("=" * 60)

    if ERRORS:
        print("\n失败详情:")
        for e in ERRORS:
            print(f"  - {e}")

    sys.exit(0 if FAIL == 0 else 1)
