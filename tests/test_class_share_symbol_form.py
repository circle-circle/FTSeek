"""类股符号形式回归测试 (2026-09-18)

背景: 内部 canonical 写作 US.BRK-B, 但各数据源接受的类股写法不同 —
TwelveData / Futu OpenD 只认点号 (BRK.B / US.BRK.B), 连字符写法分别返回
HTTP 404 与 ret=-1 "未知股票" → US.BRK-B 0 条日线 (实测 2026-09-18)。
Yahoo / Finnhub / AlphaVantage 反而只认连字符, 故转换只允许出现在
TwelveData 与 Futu 的出站符号构造处。

本测试为纯离线断言 (无网络调用), 防止有人"顺手"把转换抹掉或扩散到错误的一侧。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── SSOT 转换函数 ─────────────────────────────────────────────
class TestToDottedClassShare:
    def test_class_share_converted(self):
        from app.utils.symbol_utils import to_dotted_class_share as f
        assert f("BRK-B") == "BRK.B"
        assert f("BF-B") == "BF.B"

    def test_plain_ticker_untouched(self):
        from app.utils.symbol_utils import to_dotted_class_share as f
        assert f("AAPL") == "AAPL"
        assert f("US.BRK-B") == "US.BRK-B"  # 带市场前缀不改写 (由调用方拼)
        assert f("brk-b") == "brk-b"        # 不改大小写
        assert f(None) is None
        assert f("") == ""

    def test_option_code_untouched(self):
        """期权代码后缀为数字, 绝不能被写成 BRK.B260119C500000。"""
        from app.utils.symbol_utils import to_dotted_class_share as f
        assert f("BRK-B260119C500000") == "BRK-B260119C500000"
        assert f("US.QQQ260602P710000") == "US.QQQ260602P710000"

    def test_normalize_symbol_unaffected(self):
        from app.utils.symbol_utils import normalize_symbol
        assert normalize_symbol("US.US.MU") == "US.MU"
        assert normalize_symbol("GOOGL") == "GOOG"


# ── Provider 出站符号 ────────────────────────────────────────
class TestProviderSymbolForms:
    def test_twelvedata_dotted(self):
        from app.data.twelvedata_provider import TwelveDataProvider
        p = TwelveDataProvider(api_key="dummy")
        assert p._to_ticker("US.BRK-B") == "BRK.B"
        assert p._to_ticker("US.AAPL") == "AAPL"
        assert p._to_ticker("HK.00700") == "00700"

    def test_futu_dotted_with_prefix(self):
        from app.data.futu_lv3_provider import FutuLV3Provider
        assert FutuLV3Provider._to_futu_symbol(None, "US.BRK-B") == "US.BRK.B"
        assert FutuLV3Provider._to_futu_symbol(None, "BRK-B") == "US.BRK.B"
        assert FutuLV3Provider._to_futu_symbol(None, "US.AAPL") == "US.AAPL"
        assert FutuLV3Provider._to_futu_symbol(None, "AAPL") == "US.AAPL"
        # 期权不得改写
        assert (FutuLV3Provider._to_futu_symbol(None, "US.QQQ260602P710000")
                == "US.QQQ260602P710000")


# ── TwelveData: 无效符号负缓存 + header 携钥 (日志不再含 key) ──
class TestTwelveDataPermanentError:
    def test_invalid_symbol_cached_and_short_circuits(self, monkeypatch):
        import requests
        from app.data.twelvedata_provider import TwelveDataProvider

        p = TwelveDataProvider(api_key="KEY_TEST")
        calls = {"n": 0}

        class _Resp:
            status_code = 404

            def raise_for_status(self):
                err = requests.HTTPError("404 Client Error: Not Found")
                err.response = self
                raise err

        def _fake_get(url, **kwargs):
            calls["n"] += 1
            return _Resp()

        monkeypatch.setattr(requests, "get", _fake_get)

        assert p._get("time_series", {"symbol": "NOSUCH", "interval": "1day"}) is None
        assert calls["n"] == 1
        assert p._is_invalid_symbol("NOSUCH") is True

        # 同符号当日不再发请求 (永久性错误不按次重试)
        assert p._get("time_series", {"symbol": "NOSUCH", "interval": "1day"}) is None
        assert calls["n"] == 1

    def test_apikey_in_header_not_url(self, monkeypatch):
        import requests
        from app.data.twelvedata_provider import TwelveDataProvider

        p = TwelveDataProvider(api_key="KEY_TEST")
        seen = {}

        def _fake_get(url, **kwargs):
            seen["params"] = kwargs.get("params") or {}
            seen["headers"] = kwargs.get("headers") or {}
            raise requests.RequestException("boom")

        monkeypatch.setattr(requests, "get", _fake_get)
        p._get("quote", {"symbol": "AAPL"})

        assert "apikey" not in seen["params"]
        assert seen["headers"].get("Authorization") == "apikey KEY_TEST"


# ── 日志脱敏 ─────────────────────────────────────────────────
class TestLogSanitize:
    def test_scrub_secrets(self):
        from app.utils.log_sanitize import scrub_secrets
        raw = ("Max retries exceeded with url: /query?function=OVERVIEW"
               "&apikey=SECRETVALUE&symbol=BRK-B")
        out = scrub_secrets(raw)
        assert "SECRETVALUE" not in out
        assert "apikey=***" in out
        assert "symbol=BRK-B" in out  # 其余上下文保留


# ── screener 数据可得性门 ────────────────────────────────────
class TestScreenerDataAvailabilityGate:
    class _StubCollector:
        def __init__(self, data):
            self._data = data

        def collect_batch(self, symbols):
            return [self._data[s] for s in symbols if s in self._data]

        def collect(self, symbol):
            return self._data.get(symbol)

    @staticmethod
    def _mk(symbol, quality, ma20, rsi):
        from app.playbook.data_collector import PlaybookData, TechnicalData
        d = PlaybookData(symbol=symbol)
        d.technical = TechnicalData(price=100.0, quality=quality, ma20=ma20, rsi_14=rsi)
        d.fundamental.quality = "ok"
        return d

    def test_hollow_technical_excluded(self):
        from app.playbook.screener import PlaybookScreener
        from app.playbook.market_regime import MarketRegime

        data = {
            "US.HOLLOW": self._mk("US.HOLLOW", "partial", None, None),   # 有报价无日线
            "US.HEALTHY": self._mk("US.HEALTHY", "ok", 99.0, 55.0),
            "US.NOPRICE": self._mk("US.NOPRICE", "missing", None, None),  # 无报价
        }
        sc = PlaybookScreener(self._StubCollector(data))
        passed = [c.symbol for c in sc.screen(
            ["US.HOLLOW", "US.HEALTHY", "US.NOPRICE"], MarketRegime.RISK_ON, [])]

        assert "US.HEALTHY" in passed
        assert "US.HOLLOW" not in passed, "无日线空壳必须剔除 (案例 US.BRK-B)"
        assert "US.NOPRICE" not in passed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))