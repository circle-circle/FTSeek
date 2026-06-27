#!/usr/bin/env python3
"""
FTSeek Playbook 全链路验证: Screener → Voting → Instruction → DB → Execute
"""
import sys, os, json, sqlite3, tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

os.environ["TRADE_ENV"] = "1"  # simulate
os.environ["FUTU_TRADE_ENV"] = "1"

print("=" * 70)
print("FTSeek Playbook 全链路验证: Screener → Voting → Instruction → DB")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════
# Step 1: Screener — 验证不截断, 返回全量
# ══════════════════════════════════════════════════════════════════
print("\n[Step 1] Screener — 验证不截断, 返回全量通过门槛的候选")

from app.playbook.screener import PlaybookScreener, ScreenResult
from app.playbook.market_regime import MarketRegime
from app.playbook.data_collector import (
    PlaybookData, FundamentalData, TechnicalData, InsiderData,
    NewsData, EarningsData, SentimentData
)

def make_test_data(symbol, price=100.0, ma20=90.0, rsi=45.0, insider_cluster=0.0, insider_net=0, sentiment=0.0):
    return PlaybookData(
        symbol=symbol,
        fundamental=FundamentalData(pe=22.0, industry="Technology", market_cap=5000, quality="ok"),
        technical=TechnicalData(
            price=price, ma20=ma20, ma50=85.0, rsi_14=rsi, macd_signal="golden_cross",
            atr_14=3.0, volume_5d_avg_ratio=1.2, quality="ok"
        ),
        insider=InsiderData(
            cluster_score=insider_cluster, net_shares_30d=insider_net, quality="ok"
        ),
        news=NewsData(sentiment_score=sentiment, quality="ok"),
        earnings=EarningsData(beat_rate=0.6, days_until=30, quality="ok"),
        sentiment=SentimentData(quality="ok"),
    )

mock_collector = MagicMock()
watchlist = ["US.AAPL", "US.TSLA", "US.MSFT", "US.GOOG", "US.AMZN",
             "US.NVDA", "US.META", "US.NFLX", "US.AMD", "US.INTC"]

def mock_collect(symbol):
    sym = symbol.split(".")[-1]
    if sym in ("TSLA", "AMD", "NFLX", "INTC"):
        # SHORT 候选: 低RSI + 负sentiment + insider net sell
        return make_test_data(symbol, price=100.0, ma20=110.0, rsi=28.0, sentiment=-0.5, insider_net=-2000)
    elif sym in ("NVDA", "META"):
        return make_test_data(symbol, price=100.0, ma20=90.0, rsi=35.0, sentiment=0.3, insider_cluster=0.8)
    else:
        return make_test_data(symbol, price=100.0, ma20=90.0, rsi=35.0, sentiment=0.3)

mock_collector.collect.side_effect = mock_collect
screener = PlaybookScreener(mock_collector, config_path="config/playbook_screening.yaml")

results = screener.screen(
    watchlist=watchlist,
    regime=MarketRegime.CAUTIOUS,
    positions=[],
)
print(f"  watchlist: {len(watchlist)} symbols")
print(f"  screener 返回: {len(results)} candidates (不截断)")
assert len(results) == 10, f"Expected 10, got {len(results)}"
for r in results:
    print(f"    {r.symbol}: score={r.score}, factors={list(r.factors.keys())}")
print("  [PASS] Step 1 — 全量返回, 无 top_n 截断")


# ══════════════════════════════════════════════════════════════════
# Step 2: VotingCoordinator → Instruction 转换器 (mock voting)
# ══════════════════════════════════════════════════════════════════
print("\n[Step 2] _build_instructions_from_votes — 投票结果转 Instruction")

from app.playbook.pipeline import (
    _build_instructions_from_votes,
    _select_thesis_template,
)
from app.playbook.instruction_generator import Instruction

# Mock VotingCoordinator — 模拟多模型投票结果
class MockVotingCoordinator:
    def __init__(self):
        self.vote_results = {
            "US.AAPL": {"direction": "LONG", "final_strength": 72, "consensus_level": 0.85, "is_neutral": False, "decision_id": "dec-aapl-001"},
            "US.TSLA": {"direction": "SHORT", "final_strength": 28, "consensus_level": 0.80, "is_neutral": False, "decision_id": "dec-tsla-002"},
            "US.NVDA": {"direction": "LONG", "final_strength": 68, "consensus_level": 0.75, "is_neutral": False, "decision_id": "dec-nvda-003"},
            "US.AMD": {"direction": "SHORT", "final_strength": 35, "consensus_level": 0.70, "is_neutral": False, "decision_id": "dec-amd-004"},
            "US.MSFT": {"direction": "NEUTRAL", "final_strength": 50, "consensus_level": 0.3, "is_neutral": True, "decision_id": "dec-msft-005"},
        }

    def vote(self, symbol, ctx):
        sym = symbol if isinstance(symbol, str) else str(symbol)
        result = self.vote_results.get(sym)
        if result is None:
            # 未预设的symbol → 返回LONG
            return {"direction": "LONG", "final_strength": 60, "consensus_level": 0.6, "is_neutral": False, "decision_id": f"dec-{sym}-auto"}
        return result

mock_vc = MockVotingCoordinator()

# 构造 RegimeContext mock
class MockRegimeContext:
    regime = MarketRegime.CAUTIOUS
    vix = 18.5
    spy_price = 540.0
    spy_ma20 = 535.0
    spy_ma50 = 530.0
    top_n = 5  # 保留但不用
    position_multiplier = 0.5

instructions = _build_instructions_from_votes(
    screen_results=results,
    voting_coordinator=mock_vc,
    regime_context=MockRegimeContext(),
    total_equity=82000.0,
)

print(f"  screen_results: {len(results)} candidates")
print(f"  instructions: {len(instructions)} instructions")

buy_count = sum(1 for i in instructions if i.direction == "BUY")
sell_short_count = sum(1 for i in instructions if i.direction == "SELL_SHORT")
skip_count = sum(1 for i in instructions if i.direction == "SKIP")
print(f"  BUY: {buy_count}, SELL_SHORT: {sell_short_count}, SKIP: {skip_count}")

assert sell_short_count > 0, "SHORT 指令应为 >0 (TSLA, AMD, NFLX, INTC 投票为 SHORT)"
print(f"  [PASS] Step 2 — SHORT 指令正常生成 ({sell_short_count} 个 SELL_SHORT)")

# 验证 decision_id 传递
for inst in instructions:
    if inst.direction != "SKIP":
        assert inst.decision_id, f"{inst.symbol} decision_id 为空!"
print("  [PASS] Step 2 — decision_id 全部传递")

# 验证数值计算
for inst in instructions:
    if inst.direction == "BUY":
        assert inst.entry_price > 0, f"{inst.symbol} entry_price should > 0"
        assert inst.stop_loss > 0 and inst.stop_loss < inst.entry_price, f"{inst.symbol} BUY stop_loss should < entry"
        assert inst.take_profit > inst.entry_price, f"{inst.symbol} BUY take_profit should > entry"
    elif inst.direction == "SELL_SHORT":
        assert inst.entry_price > 0, f"{inst.symbol} entry_price should > 0"
        assert inst.stop_loss > inst.entry_price, f"{inst.symbol} SHORT stop_loss should > entry"
        assert inst.take_profit < inst.entry_price, f"{inst.symbol} SHORT take_profit should < entry"
print("  [PASS] Step 2 — 数值计算正确 (entry/SL/TP 方向一致)")


# ══════════════════════════════════════════════════════════════════
# Step 3: DB 存储 — 验证 decision_id 写入 + 读取
# ══════════════════════════════════════════════════════════════════
print("\n[Step 3] DB 存储 — save_instruction + decision_id 读写")

from app.playbook.db import PlaybookDB

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
db = PlaybookDB(conn=conn)
db.ensure_tables()

from app.playbook.pipeline import _instruction_to_db_dict

today = "2026-06-23"
saved = 0
for inst in instructions:
    inst_dict = _instruction_to_db_dict(inst, MockRegimeContext(), today)
    try:
        db.save_instruction(inst_dict)
        saved += 1
    except Exception as e:
        print(f"  [FAIL] save_instruction {inst.symbol}: {e}")

print(f"  入库: {saved}/{len(instructions)} 条指令")

# 读回验证
rows = db.get_instructions_by_date(today)
print(f"  读回: {len(rows)} 条")

for row in rows:
    direction = row["direction"]
    decision_id = row["decision_id"]
    symbol = row["symbol"]
    print(f"    {symbol}: direction={direction}, decision_id={decision_id}, entry={row['entry_price']}, SL={row['stop_loss']}")

# 验证 SHORT 在 DB 中
short_rows = [r for r in rows if r["direction"] == "SELL_SHORT"]
assert len(short_rows) > 0, "DB 中应有 SELL_SHORT 指令!"
print(f"  [PASS] Step 3 — DB 中 SELL_SHORT 指令: {len(short_rows)} 条")

# 验证 decision_id
for row in rows:
    if row["direction"] != "SKIP":
        assert row["decision_id"], f"{row['symbol']} decision_id 为空!"
print("  [PASS] Step 3 — decision_id 全部写入 DB")


# ══════════════════════════════════════════════════════════════════
# Step 4: playbook_service 执行路径 — decision_id 优先从 inst 读
# ══════════════════════════════════════════════════════════════════
print("\n[Step 4] playbook_service — decision_id 优先从 instruction 读取")

from app.playbook.playbook_service import PlaybookService
import inspect

src = inspect.getsource(PlaybookService.execute_instruction)
assert 'inst.get("decision_id"' in src, "should read decision_id from inst first"
assert "signal_snapshots" in src, "should have fallback to signal_snapshots"
print("  [PASS] Step 4 — execute_instruction 优先 inst.decision_id → fallback signal_snapshots")


# ══════════════════════════════════════════════════════════════════
# Step 5: 端到端方向覆盖验证 — SHORT 不再被过滤
# ══════════════════════════════════════════════════════════════════
print("\n[Step 5] 端到端方向覆盖 — SHORT 不再被过滤")

# 统计 Screener 返回的候选 vs 最终 DB 中的指令方向
screener_symbols = {r.symbol for r in results}
db_symbols = {row["symbol"] for row in rows}
assert screener_symbols == db_symbols, f"Screener和DB的symbol集不匹配!"

db_directions = {}
for row in rows:
    db_directions[row["symbol"]] = row["direction"]

print(f"  Screener 返回: {len(screener_symbols)} symbols")
print(f"  DB 存储: {len(db_symbols)} symbols")
print(f"  方向分布:")
for sym, direction in sorted(db_directions.items()):
    print(f"    {sym}: {direction}")

# 关键验证: SHORT 候选进入了 DB
short_symbols = [sym for sym, d in db_directions.items() if d == "SELL_SHORT"]
long_symbols = [sym for sym, d in db_directions.items() if d == "BUY"]
skip_symbols = [sym for sym, d in db_directions.items() if d == "SKIP"]

print(f"\n  BUY: {len(long_symbols)} → {long_symbols}")
print(f"  SELL_SHORT: {len(short_symbols)} → {short_symbols}")
print(f"  SKIP: {len(skip_symbols)} → {skip_symbols}")

assert len(short_symbols) > 0, "SHORT 指令应为 >0 — 这是本次修复的核心目标!"
print(f"\n  [PASS] Step 5 — SHORT 指令成功进入 Playbook 管线 ({len(short_symbols)} 个)")


# ══════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("全链路验证结果")
print("=" * 70)
print(f"  Step 1: Screener 不截断              [PASS]")
print(f"  Step 2: Voting → Instruction 转换      [PASS]")
print(f"  Step 3: DB 存储 + decision_id          [PASS]")
print(f"  Step 4: playbook_service decision_id   [PASS]")
print(f"  Step 5: SHORT 不再被过滤               [PASS]")
print()
print(f"  方向覆盖: BUY={len(long_symbols)}, SHORT={len(short_symbols)}, SKIP={len(skip_symbols)}")
print(f"  归因链路: VotingCoordinator.decision_id → Instruction → DB → trade_records")
print()
print("  核心问题已修复: SHORT 候选不再被 Screener top_n 隐性过滤")
print("  信号源统一: Playbook 消费 VotingCoordinator, 不跑独立 LLM")
print("  归因贯通: decision_id 从投票到DB到执行全链路可追溯")
print("=" * 70)
