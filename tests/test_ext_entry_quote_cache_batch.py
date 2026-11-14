"""执行前第二轮报价的批量预取/缓存 (`_QuoteCache`) 专属用例 (2026-11-13, 429 收口)。

病灶: 第一波 `_snapshot_candidates` 批量快照之后, `_liquidity_quote` 在 3 个调用点逐票
`get_market_snapshot([symbol])` —— 候选 50 只 ⇒ 第二轮 50 次单票请求, 撞 OpenD 限频。

本文件钉住:
  ① 第二轮合并为**批量**请求 (N 只 → 1 次), 且与第一波 `_batch_quote` 是**独立时间样本**;
  ② 批量失败/缺行 ⇒ fail-closed (None), 同一轮**不逐票重试**; 限频/网络类整批放弃
     (不二分、不逐票重发), 个别标的错误按第一波同款策略二分隔离;
  ③ 迟登记的标的并入同一批次, 不产生逐票风暴;
  ④ 非标准帧形状退回单票解析时次数有界 (`_LEGACY_FALLBACK_MAX`);
  ⑤ 研究臂下游闸与严格臂共用同一缓存 (两候选一请求), 反事实仍读第一波 `_batch_quote`。

全部本地 fake, 不触 OpenD/券商/DB。
"""
import pandas as pd

import scripts.extended_hours_entry_executor as executor


# ── 桩件 ─────────────────────────────────────────────────────────────────────

class RecordingQuoteContext:
    """记录每次批量请求的 symbol 列表; 返回由 responder 决定。"""

    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def get_market_snapshot(self, symbols):
        self.calls.append(list(symbols))
        return self._responder(list(symbols))


def _snapshot_frame(symbols, bid=9.99, ask=10.01):
    """futu 批量快照形状: DataFrame + code 列 (供 `_QuoteCache._parse_snapshot`)。"""
    return pd.DataFrame([{
        "code": symbol,
        "bid_price": bid,
        "ask_price": ask,
        "bid_vol": 1_000,
        "ask_vol": 1_000,
        "total_market_val": 5_000_000_000,
    } for symbol in symbols])


def _first_wave_frame(symbols):
    """第一波 `_snapshot_candidates` 的形状 (盘前字段 + 更紧的盘口)。"""
    return pd.DataFrame([{
        "code": symbol,
        "name": symbol,
        "prev_close_price": 100.0,
        "pre_price": 102.0,
        "last_price": 101.0,
        "pre_volume": 20_000,
        "bid_price": 101.99,
        "ask_price": 102.01,
        "bid_vol": 1_000,
        "ask_vol": 1_000,
        "total_market_val": 5_000_000_000,
    } for symbol in symbols])


class _LegacyRow:
    def get(self, key, default=0):
        return {
            "bid_price": 9.99,
            "ask_price": 10.01,
            "bid_vol": 1_000,
            "ask_vol": 1_000,
            "total_market_val": 5_000_000_000,
        }.get(key, default)


class _LegacySnapshot:
    """旧单票形状: 只有 iloc (无 columns/iterrows), 用于钉有界回退路径。"""

    empty = False

    def __init__(self):
        self.iloc = [_LegacyRow()]


def _first_wave_rows(symbols):
    qctx = RecordingQuoteContext(lambda syms: (0, _first_wave_frame(syms)))
    return qctx, executor._snapshot_candidates("premarket", qctx, symbols)


# ── ① 批量 + 独立样本 ────────────────────────────────────────────────────────

def test_second_pass_is_one_batch_and_independent_of_batch_quote():
    """N 只候选的第二轮报价 = 1 次批量请求; 报价取自新样本, 不覆盖第一波 `_batch_quote`。"""
    _, rows = _first_wave_rows(["US.AAA", "US.BBB"])
    assert [row["_batch_quote"]["bid"] for row in rows] == [101.99, 101.99]

    ctx = RecordingQuoteContext(lambda syms: (0, _snapshot_frame(syms, 201.99, 202.01)))
    cache = executor._QuoteCache(ctx, [row["symbol"] for row in rows])

    first = executor._liquidity_quote(cache, "US.AAA")
    second = executor._liquidity_quote(cache, "US.BBB")

    assert ctx.calls == [["US.AAA", "US.BBB"]], "第二轮仍未合并成一次批量请求"
    assert first["bid"] == 201.99 and second["ask"] == 202.01
    # 第一波样本保持原值 ⇒ 两个时间样本独立 (反事实对照不被污染)
    assert [row["_batch_quote"]["bid"] for row in rows] == [101.99, 101.99]
    assert first != rows[0]["_batch_quote"]


def test_cached_quote_is_not_requested_twice():
    ctx = RecordingQuoteContext(lambda syms: (0, _snapshot_frame(syms)))
    cache = executor._QuoteCache(ctx, ["US.AAA", "US.BBB"])

    first = cache.quote("US.AAA")
    assert cache.quote("US.AAA") is first
    cache.quote("US.BBB")
    assert ctx.calls == [["US.AAA", "US.BBB"]]


def test_late_registered_symbols_join_the_same_batch():
    """迟登记 (研究臂新增) 的标的在首次取价时并入同一批, 不各自单票请求。"""
    ctx = RecordingQuoteContext(lambda syms: (0, _snapshot_frame(syms)))
    cache = executor._QuoteCache(ctx, ["US.AAA"])
    cache.register(["US.BBB", "US.CCC"])

    assert cache.quote("US.AAA")["bid"] == 9.99
    assert cache.quote("US.BBB")["bid"] == 9.99
    assert ctx.calls == [["US.AAA", "US.BBB", "US.CCC"]]


def test_batches_are_chunked_by_snapshot_batch(monkeypatch):
    monkeypatch.setattr(executor, "SNAPSHOT_BATCH", 2)
    ctx = RecordingQuoteContext(lambda syms: (0, _snapshot_frame(syms)))
    cache = executor._QuoteCache(ctx, ["US.A", "US.B", "US.C"])

    cache.quote("US.A")

    assert ctx.calls == [["US.A", "US.B"], ["US.C"]]


# ── ② 失败 fail-closed / 不逐票重试 ─────────────────────────────────────────

def test_batch_failure_marks_all_symbols_unavailable_without_retry():
    """RET≠OK (限频) ⇒ 整批 None + 一次请求; 后续取价不得再发单票请求。"""
    ctx = RecordingQuoteContext(lambda syms: (-1, "获取市场快照频率太高"))
    cache = executor._QuoteCache(ctx, ["US.AAA", "US.BBB", "US.CCC"])

    assert [cache.quote(s) for s in ("US.AAA", "US.BBB", "US.CCC")] == [None, None, None]
    assert cache.quote("US.AAA") is None
    assert ctx.calls == [["US.AAA", "US.BBB", "US.CCC"]], "失败后出现逐票重试 (429 风暴)"


def test_batch_exception_marks_all_symbols_unavailable_without_retry():
    def _boom(_symbols):
        raise TimeoutError("snapshot timeout")

    ctx = RecordingQuoteContext(_boom)
    cache = executor._QuoteCache(ctx, ["US.AAA", "US.BBB"])

    assert cache.quote("US.AAA") is None
    assert cache.quote("US.BBB") is None
    assert ctx.calls == [["US.AAA", "US.BBB"]]


def test_symbol_specific_error_is_split_isolated_without_per_symbol_storm():
    """非限频类错误 (个别标的行情不可用) 二分隔离坏票; 坏票只被请求一次。"""
    def responder(symbols):
        if "US.BAD" in symbols:
            return -1, "暂不提供美股 OTC 市场行情"
        return 0, _snapshot_frame(symbols)

    ctx = RecordingQuoteContext(responder)
    cache = executor._QuoteCache(ctx, ["US.GOOD", "US.BAD"])

    assert cache.quote("US.GOOD")["bid"] == 9.99
    assert cache.quote("US.BAD") is None
    assert ctx.calls == [["US.GOOD", "US.BAD"], ["US.GOOD"], ["US.BAD"]]
    assert cache.quote("US.BAD") is None
    assert len(ctx.calls) == 3, "坏票被重复请求 (风暴风险)"


def test_missing_row_in_successful_batch_is_fail_closed():
    ctx = RecordingQuoteContext(
        lambda syms: (0, _snapshot_frame(["US.AAA"]))
    )
    cache = executor._QuoteCache(ctx, ["US.AAA", "US.BBB"])

    assert cache.quote("US.AAA")["bid"] == 9.99
    assert cache.quote("US.BBB") is None
    assert ctx.calls == [["US.AAA", "US.BBB"]]


def test_empty_frame_marks_all_symbols_unavailable():
    ctx = RecordingQuoteContext(lambda syms: (0, pd.DataFrame()))
    cache = executor._QuoteCache(ctx, ["US.AAA"])

    assert cache.quote("US.AAA") is None
    assert ctx.calls == [["US.AAA"]]


# ── ④ 形状回退有界 ──────────────────────────────────────────────────────────

def test_unparseable_frame_falls_back_to_single_symbol_but_is_bounded():
    """非标准帧 (无 columns) ⇒ 退回单票解析, 但最多 `_LEGACY_FALLBACK_MAX` 次。"""
    ctx = RecordingQuoteContext(lambda syms: (0, _LegacySnapshot()))
    symbols = [f"US.S{i}" for i in range(8)]
    cache = executor._QuoteCache(ctx, symbols)

    cache.quote("US.S0")

    # 1 次批量 + 最多 5 次单票回退; 其余 fail-closed
    assert len(ctx.calls) == 1 + executor._QuoteCache._LEGACY_FALLBACK_MAX
    assert sum(1 for s in symbols if cache._quotes.get(s) is not None) == 5
    assert ctx.calls[0] == symbols
    assert all(call == [symbol] for call, symbol in zip(ctx.calls[1:], symbols[:5]))


# ── ③⑤ 研究臂共享缓存 + 反事实读第一波样本 ─────────────────────────────────

def test_research_downstream_gates_share_one_batch_and_fail_closed():
    ctx = RecordingQuoteContext(lambda syms: (-1, "请求频率超过限制"))
    cache = executor._QuoteCache(ctx, ["US.AAA", "US.BBB"])
    counts, rejects = {}, []

    for symbol in ("US.AAA", "US.BBB"):
        item = {"symbol": symbol, "px": 10.0, "chg": 3.0, "vol": 2_000_000,
                "_source": "rank"}
        assert executor._research_downstream_gates(
            item, "premarket", "2026-11-11", cache, counts, rejects
        ) is None

    assert ctx.calls == [["US.AAA", "US.BBB"]]
    assert [r["reason"] for r in rejects] == ["quote_unavailable", "quote_unavailable"]
    assert counts["research_quote_unavailable"] == 2


def test_research_gate_uses_cache_quote_while_counterfactual_keeps_batch_quote(monkeypatch):
    """实盘闸读第二轮缓存报价; `_batch_quote_cf` 仍记录第一波样本 (两份独立)。"""
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        executor, "_entry_structure_gate",
        lambda qctx, symbol, session, price: ({"vwap": 9.0}, None, {"vwap": 9.0}),
    )
    ctx = RecordingQuoteContext(
        lambda syms: (0, _snapshot_frame(syms, 9.99, 10.01))
    )
    cache = executor._QuoteCache(ctx, ["US.TSLA"])
    batch_quote = {"bid": 9.0, "ask": 11.0, "bid_volume": 1_000, "ask_volume": 1_000,
                   "market_cap": 5_000_000_000, "spread_pct": 20.0}
    item = {"symbol": "US.TSLA", "px": 10.0, "chg": 3.0, "vol": 2_000_000,
            "_source": "research_watchlist", "_batch_quote": batch_quote}
    counts, rejects = {}, []

    passed = executor._research_downstream_gates(
        item, "premarket", "2026-11-11", cache, counts, rejects
    )

    assert passed is not None and rejects == []
    assert passed[1] == 10.01, "实盘闸没用第二轮缓存报价"
    assert ctx.calls == [["US.TSLA"]]
    cf = item["_batch_quote_cf"]
    assert cf["batch_quote"]["bid"] == 9.0 and cf["batch_quote"]["spread_pct"] == 20.0
    assert cf["batch_quote"] is batch_quote
    assert cf["live_passed"] is True
    assert cf["batch_pretrade_passed"] is False
    assert cf["would_change"] is True


# ── 源码契约: 调用点不得回退到逐票 ──────────────────────────────────────────

def test_collect_call_sites_use_the_quote_cache():
    src = (executor.ROOT / "scripts" / "extended_hours_entry_executor.py").read_text(
        encoding="utf-8")
    assert src.count("_liquidity_quote(quote_cache, symbol)") == 2
    assert src.count("quote_cache = _QuoteCache(") == 2
    assert "_liquidity_quote(qctx, symbol)" in src  # 研究臂助手经参数接收缓存