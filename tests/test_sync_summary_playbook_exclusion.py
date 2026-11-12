"""US Sync Done 汇总计数不得把 Playbook 内存残留仓重复计入主线侧。"""

from app.core import position_sync as position_sync_module
from app.core.position_sync import USPositionSync


class _FakeStore:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def all(self):
        positions = []
        for symbol in self._symbols:
            position = type("Position", (), {})()
            position.symbol = symbol
            positions.append(position)
        return positions

    def count(self):
        return len(self._symbols)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql):
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def _make_sync(monkeypatch, mem_syms, pb_rows):
    sync = USPositionSync(position_store=_FakeStore(mem_syms))
    monkeypatch.setattr(position_sync_module, "_get_db_path", lambda: ":memory:")
    monkeypatch.setattr(
        position_sync_module, "safe_sqlite_connect", lambda *args, **kwargs: _FakeConn(pb_rows)
    )
    return sync


def _futu(symbols):
    return {symbol: {"qty": 1.0, "cost": 100.0, "direction": "LONG"} for symbol in symbols}


def test_residual_playbook_memory_positions_are_not_double_counted(monkeypatch):
    sync = _make_sync(
        monkeypatch,
        mem_syms={"US.LLY", "US.BWXT", "US.MSFT"},
        pb_rows=[("US.LLY",), ("US.BWXT",), ("US.QCOM",)],
    )

    futu_n, main_count, pb_count = sync._sync_done_counts(
        _futu({"US.LLY", "US.BWXT", "US.QCOM", "US.MSFT"})
    )

    assert (futu_n, main_count, pb_count) == (4, 1, 3)
    assert futu_n == main_count + pb_count


def test_non_playbook_memory_positions_still_counted(monkeypatch):
    sync = _make_sync(
        monkeypatch,
        mem_syms={"US.MSFT", "US.OLD"},
        pb_rows=[("US.LLY",)],
    )

    futu_n, main_count, pb_count = sync._sync_done_counts(_futu({"US.MSFT", "US.LLY"}))

    assert (futu_n, main_count, pb_count) == (2, 2, 1)
    assert futu_n != main_count + pb_count


def test_playbook_query_failure_keeps_legacy_fallback(monkeypatch):
    sync = USPositionSync(position_store=_FakeStore({"US.LLY", "US.MSFT"}))
    monkeypatch.setattr(position_sync_module, "_get_db_path", lambda: ":memory:")

    def _raise(*args, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(position_sync_module, "safe_sqlite_connect", _raise)

    futu_n, main_count, pb_count = sync._sync_done_counts(_futu({"US.LLY", "US.MSFT"}))

    assert (futu_n, main_count, pb_count) == (2, 2, -1)


def test_missing_position_store_falls_back_to_broker_count(monkeypatch):
    sync = USPositionSync(position_store=None)
    monkeypatch.setattr(position_sync_module, "_get_db_path", lambda: ":memory:")
    monkeypatch.setattr(
        position_sync_module, "safe_sqlite_connect", lambda *args, **kwargs: _FakeConn([])
    )

    futu_n, main_count, pb_count = sync._sync_done_counts(_futu({"US.MSFT", "US.LLY"}))

    assert (futu_n, main_count, pb_count) == (2, 2, 0)
