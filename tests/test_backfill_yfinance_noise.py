import logging

from scripts.backfill_forward_returns import _YFinanceDelistedNoiseFilter


def _record(message):
    return logging.LogRecord(
        "yfinance", logging.ERROR, __file__, 1, message, None, None
    )


def test_filter_drops_only_delisted_noise():
    noise_filter = _YFinanceDelistedNoiseFilter()

    assert noise_filter.filter(_record("$HELP: possibly delisted; no price data found")) is False
    assert noise_filter.filter(_record("Failed to get ticker 'AAPL'")) is True
