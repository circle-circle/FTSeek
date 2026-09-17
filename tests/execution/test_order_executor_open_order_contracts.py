import sys
from pathlib import Path
from types import SimpleNamespace

from futu import RET_OK, TrdEnv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.order_executor import _query_broker_open_orders


class FakeDataFrame:
    def __init__(self, empty=True):
        self.empty = empty


class FakeTradeContext:
    def __init__(self, ret, data):
        self.ret = ret
        self.data = data
        self.captured = None

    def order_list_query(self, **kwargs):
        self.captured = kwargs
        return self.ret, self.data


def _scheduler_with_adapter(adapter):
    return SimpleNamespace(
        execution_engine=SimpleNamespace(broker_adapter=adapter)
    )


def test_open_order_query_failure_returns_unknown():
    trade_context = FakeTradeContext(-1, None)
    adapter = SimpleNamespace(trade_ctx=trade_context, trade_env=1, account_id=123)

    result = _query_broker_open_orders(_scheduler_with_adapter(adapter), "US.AAPL")

    assert result is None


def test_open_order_query_success_empty_returns_empty_list():
    trade_context = FakeTradeContext(RET_OK, FakeDataFrame(empty=True))
    adapter = SimpleNamespace(trade_ctx=trade_context, trade_env=1, account_id=123)

    result = _query_broker_open_orders(_scheduler_with_adapter(adapter), "US.AAPL")

    assert result == []


def test_open_order_query_uses_shared_environment_resolver():
    trade_context = FakeTradeContext(RET_OK, FakeDataFrame(empty=True))
    adapter = SimpleNamespace(
        trade_ctx=trade_context, trade_env="1", account_id="123"
    )

    result = _query_broker_open_orders(_scheduler_with_adapter(adapter), "US.AAPL")

    assert result == []
    assert trade_context.captured["trd_env"] == TrdEnv.SIMULATE
