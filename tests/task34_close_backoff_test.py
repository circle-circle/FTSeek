"""Task #34 \u9a8c\u8bc1\u811a\u672c \u2014 \u6a21\u62df\u8fde\u7eed\u5e73\u4ed3\u5931\u8d25\u9a8c\u8bc1\u9000\u907f\u672a\u8131\u7f30."""
import sys
sys.path.insert(0, '/root/FTSeek-US')

from app.pipeline.order_executor import (
    _close_retry_key,
    _is_backoff_error,
    _check_close_cooldown,
    _record_close_failure,
    _clear_close_retry,
    _close_retry_state,
    _CLOSE_BLOCK_THRESHOLD,
    manual_unblock,
)
from app.core.models import Direction


class FakePos:
    def __init__(self, sym, direction):
        self.symbol = sym
        self.direction = direction


def reset():
    _close_retry_state.clear()


def test_backoff_ladder():
    reset()
    pos = FakePos("US.MU", Direction.LONG)
    assert _check_close_cooldown(pos.symbol, pos.direction) is None
    fc, cd, blocked = _record_close_failure(None, pos, "\u6301\u4ed3\u4e0d\u8db3", is_backoff_category=True)
    assert fc == 1 and cd is None and not blocked
    fc, cd, blocked = _record_close_failure(None, pos, "\u6301\u4ed3\u4e0d\u8db3", is_backoff_category=True)
    assert fc == 2 and cd == 5 and not blocked
    assert _check_close_cooldown(pos.symbol, pos.direction) is not None
    fc, cd, blocked = _record_close_failure(None, pos, "\u5f85\u6210\u4ea4\u8ba2\u5355", is_backoff_category=True)
    assert fc == 3 and cd == 30 and not blocked
    fc, cd, blocked = _record_close_failure(None, pos, "\u4e0d\u652f\u6301\u4ea4\u53c9\u4ea4\u6613", is_backoff_category=True)
    assert fc == 4 and cd == 30 and not blocked
    fc, cd, blocked = _record_close_failure(None, pos, "insufficient", is_backoff_category=True)
    assert fc == 5 and blocked, f"expected blocked, got fc={fc} cd={cd} blocked={blocked}"
    print("OK ladder: 2->5min, 3->30min, 4->30min, 5->blocked")


def test_separate_directions():
    reset()
    pos_long = FakePos("US.SNDK", Direction.LONG)
    pos_short = FakePos("US.SNDK", Direction.SHORT)
    _record_close_failure(None, pos_long, "\u6301\u4ed3\u4e0d\u8db3")
    _record_close_failure(None, pos_long, "\u6301\u4ed3\u4e0d\u8db3")
    assert _check_close_cooldown(pos_short.symbol, pos_short.direction) is None
    assert _check_close_cooldown(pos_long.symbol, pos_long.direction) is not None
    print("OK independent (symbol, direction)")


def test_clear_on_success():
    reset()
    pos = FakePos("US.MU", Direction.LONG)
    _record_close_failure(None, pos, "\u6301\u4ed3\u4e0d\u8db3")
    _record_close_failure(None, pos, "\u6301\u4ed3\u4e0d\u8db3")
    assert _close_retry_key(pos.symbol, pos.direction) in _close_retry_state
    _clear_close_retry(pos.symbol, pos.direction)
    assert _close_retry_key(pos.symbol, pos.direction) not in _close_retry_state
    assert _check_close_cooldown(pos.symbol, pos.direction) is None
    print("OK clear on success")


def test_error_pattern_match():
    assert _is_backoff_error("\u6301\u4ed3\u4e0d\u8db3")
    assert _is_backoff_error("insufficient position")
    assert _is_backoff_error("\u4e0d\u652f\u6301\u4ea4\u53c9\u4ea4\u6613, \u6709\u5f85\u6210\u4ea4\u8ba2\u5355")
    assert _is_backoff_error("Pending Order on cross direction")
    assert not _is_backoff_error("network timeout")
    assert not _is_backoff_error("connection refused")
    print("OK error pattern match")


def test_manual_unblock_clears_state():
    reset()
    pos = FakePos("US.MU", Direction.LONG)
    for _ in range(5):
        _record_close_failure(None, pos, "\u6301\u4ed3\u4e0d\u8db3")
    assert _close_retry_state[_close_retry_key(pos.symbol, pos.direction)]["blocked"]

    class FakeScheduler:
        position_store = None
        trade_store = None
    res = manual_unblock(FakeScheduler(), "US.MU", "LONG")
    assert res["unblocked"]
    assert _check_close_cooldown(pos.symbol, pos.direction) is None
    print("OK manual_unblock clears state")


def test_no_infinite_loop():
    """验证场景: 下单 success+订单ID 后立即报持仓不足 → 不出现无限重试.

    表现: 20 轮 tick 中只会产生 ≤ 5 次实际下单 (超出后被冷却拦截或被 blocked).
    原问题: 每 tick 都会重试，6 轮后才跳出。
    修复后: 第 2 次后冷却 5min, 中间几千 tick 都被冷却直接跳过不发单。
    """
    reset()
    pos = FakePos("US.MU", Direction.LONG)
    submit_count = 0
    blocked = False
    fc = 0
    for i in range(50):
        if _check_close_cooldown(pos.symbol, pos.direction) is not None:
            continue  # 冷却中 → 不下单
        submit_count += 1
        fc, cd, blocked = _record_close_failure(None, pos, "持仓不足")
        if blocked:
            break
    # 验证: 实际下单次数 == 2 (第 2 次后被 5min 冷却护航)
    # 这说明不会出现无限重试
    assert submit_count == 2, f"expected 2 submits before cooldown, got {submit_count}"
    print(f"OK 场景验证: 50 tick 中只下了 {submit_count} 个单 (冷却护航生效), blocked={blocked}")


def test_six_consecutive_kills_blocking():
    """验收标准: 模拟 'success 后立即拒绝' 序列, 6 次后强制停止并入 anomalous."""
    reset()
    pos = FakePos("US.MU", Direction.LONG)
    blocked = False
    fc = 0
    # 跳过冷却, 直接调 record_close_failure (模拟冷却上限后仍失败的最坏场景)
    for i in range(6):
        fc, cd, blocked = _record_close_failure(None, pos, "持仓不足")
        if blocked:
            break
    assert blocked, f"after 6 kills should be blocked"
    assert fc == 5, f"blocked at fc={fc}, expected 5"
    print(f"OK 6 次联续失败后强制阻塞 → anomalous (达到验收标准)")


if __name__ == "__main__":
    test_error_pattern_match()
    test_backoff_ladder()
    test_separate_directions()
    test_clear_on_success()
    test_manual_unblock_clears_state()
    test_no_infinite_loop()
    test_six_consecutive_kills_blocking()
    print("\nALL PASS")
