"""结算器 限频/记忆化 回归测试 (2026-09-17).

病灶: 富途 OpenD 历史K线有请求预算(约 60 次/30 秒), 超限后**整体拒绝且错误文本为 None**;
原实现 `ret, kl, _ = ...; if ret != RET_OK: return []` 把"拒绝"当成"无数据" ⇒ 结算循环跑到
约第 30 行(≈60 次调用)之后, **后续所有行静默返回 None 永不结算**。
实测证据: 颠倒 SOURCES 顺序后故障随之转移(变成"盘后 30 行 OK、其后 119 盘后 + 138 盘前全 None")
⇒ 与数据源无关, 随**累计调用数**走。修复后一次跑完 257 行补齐, api_unavailable=0。

本测试钉住两条不许回退的性质:
  1. 记忆化: 同 (标的, 起始, 长度) 只请求一次 (376 行 → 15 个唯一对, 调用量 752→~30)
  2. 持续拒绝必须抛 _KlineUnavailable, **不得**静默返回 [] (否则台账再次静默缺口)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.settle_extended_hours as settle  # noqa: E402


class _FakeKL:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        return iter(enumerate(self.rows))


class _FakeCtx:
    """记录每次请求; fail=True 时永远返回拒绝 (模拟限频超预算)."""

    def __init__(self, fail=False, rows_per_call=3):
        self.calls = []
        self.fail = fail
        self.rows_per_call = rows_per_call

    def request_history_kline(self, code, ktype=None, start=None, end=None, max_count=None):
        self.calls.append((code, start))
        if self.fail:
            return -1, None, None  # 实测: 超限时 ret != RET_OK 且错误文本为 None
        rows = [
            {"time_key": f"{start} 09:3{i}", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}
            for i in range(self.rows_per_call)
        ]
        return 0, _FakeKL(rows), None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    settle._day_bars_cache.clear()
    settle._intraday_cache.clear()
    monkeypatch.setattr(settle.time, "sleep", lambda *_a, **_k: None)
    yield


def test_day_bars_is_memoized_per_symbol_and_start():
    ctx = _FakeCtx()
    settle._day_bars(ctx, "US.UBER", "2026-09-16", 6)
    settle._day_bars(ctx, "US.UBER", "2026-09-16", 6)
    assert len(ctx.calls) == 1  # 第二次走缓存, 不再打接口

    settle._day_bars(ctx, "US.UBER", "2026-09-17", 6)
    assert len(ctx.calls) == 2


def test_intraday_path_is_memoized_per_symbol_and_date():
    ctx = _FakeCtx()
    settle._intraday_path(ctx, "US.UBER", "2026-09-16")
    settle._intraday_path(ctx, "US.UBER", "2026-09-16")
    assert len(ctx.calls) == 1


def test_persistent_api_rejection_raises_instead_of_silent_empty():
    ctx = _FakeCtx(fail=True)
    with pytest.raises(settle._KlineUnavailable):
        settle._day_bars(ctx, "US.UBER", "2026-09-16", 6)
    assert len(ctx.calls) == settle._KLINE_RETRIES  # 重试用满才抛


def test_genuine_empty_kline_is_not_an_api_error():
    ctx = _FakeCtx(rows_per_call=0)
    assert settle._day_bars(ctx, "US.NODATA", "2026-09-16", 6) == []
    assert len(ctx.calls) == 1  # 真"无数据"不重试、不抛错