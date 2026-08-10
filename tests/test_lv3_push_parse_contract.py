"""LV3 push parse_rsp_pb 契约测试 (2026-08-10).

背景: push handler 从 super().on_recv_rsp() (SDK 内部把 dict 包成 pd.DataFrame,
占 push 线程 CPU ~75%) 改为直接调用各基类的 parse_rsp_pb() 拿 dict 列表.
本文件用真实 pb2 消息锁定 SDK 契约, 防 SDK 升级静默改变返回结构:

- StockQuoteHandlerBase.parse_rsp_pb  → list[dict] (code/last_price/open_price/...)
- CurKlineHandlerBase.parse_rsp_pb    → list[dict] (code/time_key/open/close/...)
- TickerHandlerBase.parse_rsp_pb      → list[dict] (code/price/ticker_direction/...)
- OrderBookHandlerBase.parse_rsp_pb   → dict (code/Bid/Ask, 档位为 (price, vol, cnt, detail) 元组)
"""
from futu import (
    StockQuoteHandlerBase,
    CurKlineHandlerBase,
    TickerHandlerBase,
    OrderBookHandlerBase,
)
from futu.common.pb import (
    Qot_UpdateBasicQot_pb2,
    Qot_UpdateKL_pb2,
    Qot_UpdateTicker_pb2,
    Qot_UpdateOrderBook_pb2,
)


def _mk_rsp(pb2_mod, ret_type=0):
    rsp = pb2_mod.Response()
    rsp.retType = ret_type
    rsp.retMsg = ""
    return rsp


class TestQuoteParseContract:
    def test_quote_returns_dict_list_with_handler_keys(self):
        rsp = _mk_rsp(Qot_UpdateBasicQot_pb2)
        qot = rsp.s2c.basicQotList.add()
        qot.security.market = 11  # US
        qot.security.code = "AAPL"
        qot.curPrice = 313.33
        qot.openPrice = 310.0
        qot.highPrice = 315.0
        qot.lowPrice = 309.0
        qot.lastClosePrice = 308.5
        qot.volume = 12345678
        qot.turnover = 3.9e9
        qot.updateTime = "2026-08-10 10:30:00"

        ret, rows = StockQuoteHandlerBase.parse_rsp_pb(rsp)

        assert ret == 0
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert isinstance(row, dict)
        for key in ("code", "last_price", "open_price", "high_price",
                    "low_price", "prev_close_price", "volume", "turnover"):
            assert key in row, f"quote dict 缺 handler 依赖键: {key}"
        assert row["last_price"] == 313.33
        assert row["prev_close_price"] == 308.5

    def test_error_ret_passthrough(self):
        rsp = _mk_rsp(Qot_UpdateBasicQot_pb2, ret_type=-1)
        rsp.retMsg = "boom"
        ret, _ = StockQuoteHandlerBase.parse_rsp_pb(rsp)
        assert ret != 0


class TestKlineParseContract:
    def test_kline_returns_dict_list_with_handler_keys(self):
        rsp = _mk_rsp(Qot_UpdateKL_pb2)
        rsp.s2c.security.market = 11
        rsp.s2c.security.code = "TSM"
        rsp.s2c.klType = 1  # K_1M
        kl = rsp.s2c.klList.add()
        kl.time = "2026-08-10 10:31:00"
        kl.openPrice = 420.0
        kl.highPrice = 421.0
        kl.lowPrice = 419.5
        kl.closePrice = 420.8
        kl.volume = 9999

        ret, rows = CurKlineHandlerBase.parse_rsp_pb(rsp)

        assert ret == 0
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        for key in ("code", "time_key", "open", "high", "low", "close", "volume"):
            assert key in row, f"kline dict 缺 handler 依赖键: {key}"
        assert row["close"] == 420.8


class TestTickerParseContract:
    def test_ticker_returns_dict_list_with_handler_keys(self):
        rsp = _mk_rsp(Qot_UpdateTicker_pb2)
        rsp.s2c.security.market = 11
        rsp.s2c.security.code = "CRM"
        tk = rsp.s2c.tickerList.add()
        tk.time = "2026-08-10 10:31:05"
        tk.price = 193.9
        tk.volume = 100
        tk.turnover = 19390.0
        tk.sequence = 777

        ret, rows = TickerHandlerBase.parse_rsp_pb(rsp)

        assert ret == 0
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        for key in ("code", "time", "price", "volume", "turnover",
                    "ticker_direction", "sequence"):
            assert key in row, f"ticker dict 缺 handler 依赖键: {key}"
        # 已知: ticker_direction 为字符串枚举 ("BUY"/"SELL"/"NEUTRAL"), handler 侧
        # int 比较恒 NEUTRAL — 现存语义, 本测试锁定契约防无意改变
        assert isinstance(row["ticker_direction"], str)


class TestOrderBookParseContract:
    def test_orderbook_returns_dict_with_bid_ask_tuples(self):
        rsp = _mk_rsp(Qot_UpdateOrderBook_pb2)
        rsp.s2c.security.market = 11
        rsp.s2c.security.code = "LLY"
        bid = rsp.s2c.orderBookBidList.add()
        bid.price = 1130.0
        bid.volume = 200
        ask = rsp.s2c.orderBookAskList.add()
        ask.price = 1131.5
        ask.volume = 150

        ret, ob = OrderBookHandlerBase.parse_rsp_pb(rsp)

        assert ret == 0
        assert isinstance(ob, dict)
        assert ob["code"].endswith("LLY")
        assert isinstance(ob["Bid"], list) and isinstance(ob["Ask"], list)
        price, vol, _cnt, _detail = ob["Bid"][0]
        assert price == 1130.0 and vol == 200
        price, vol, _cnt, _detail = ob["Ask"][0]
        assert price == 1131.5 and vol == 150


class TestTickerDirectionMapping:
    """2026-08-10: handler 曾将 SDK 字符串方向与 int 1/2 比较 → 恒 NEUTRAL.

    影响面: 当前 TICKER 未订阅 (ADR-013), 生产零影响 (休眠);
    一旦开启订阅, buy/sell 量全 0 → buy_sell_ratio 兜底 2.0 →
    order_flow_imbalance 假多头 +0.333 注入择时上下文。
    """

    def test_map_string_enums(self):
        from app.data.futu_lv3_provider import _map_ticker_direction
        assert _map_ticker_direction("BUY") == "BUY"
        assert _map_ticker_direction("SELL") == "SELL"
        assert _map_ticker_direction("NEUTRAL") == "NEUTRAL"

    def test_map_legacy_ints_and_unknown(self):
        from app.data.futu_lv3_provider import _map_ticker_direction
        assert _map_ticker_direction(1) == "BUY"
        assert _map_ticker_direction(2) == "SELL"
        assert _map_ticker_direction(4) == "NEUTRAL"
        assert _map_ticker_direction("N/A") == "NEUTRAL"
        assert _map_ticker_direction(None) == "NEUTRAL"
        assert _map_ticker_direction("") == "NEUTRAL"

    def test_analyzer_gets_true_ratio_with_mapped_directions(self):
        """修复后: 字符串方向正确分类 → ratio 反映真实买卖量, 而非 2.0 假多头兜底."""
        from app.data.futu_lv3_provider import _map_ticker_direction
        from app.data.ticker_analyzer import analyze_tickers

        tickers = []
        for i in range(15):  # 15 笔 BUY × 100 股
            tickers.append({
                "price": 100.0, "volume": 100.0, "turnover": 10000.0,
                "direction": _map_ticker_direction("BUY"),
                "time": "2026-08-10 10:31:00", "sequence": i,
            })
        for i in range(15, 20):  # 5 笔 SELL × 100 股
            tickers.append({
                "price": 100.0, "volume": 100.0, "turnover": 10000.0,
                "direction": _map_ticker_direction("SELL"),
                "time": "2026-08-10 10:31:01", "sequence": i,
            })

        factors = analyze_tickers(tickers, current_price=100.0)

        assert factors is not None
        # 短窗(最近50笔): buy=1500 / sell=500 = 3.0
        assert factors["buy_sell_ratio_short"] == 3.0

    def test_analyzer_all_neutral_documents_old_bug(self):
        """旧 bug 场景存档: 全 NEUTRAL 时 sell_vol=0 → ratio=2.0 假多头兜底."""
        from app.data.ticker_analyzer import analyze_tickers

        tickers = [{
            "price": 100.0, "volume": 100.0, "turnover": 10000.0,
            "direction": "NEUTRAL",
            "time": "2026-08-10 10:31:00", "sequence": i,
        } for i in range(20)]

        factors = analyze_tickers(tickers, current_price=100.0)
        assert factors["buy_sell_ratio_short"] == 2.0  # 兜底值, 即旧 bug 的恒定输出
