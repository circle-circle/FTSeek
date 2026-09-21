# tests/execution/test_exit_lifecycle_us_guard.py
"""ExitLifecycle.preflight 跨市场守卫契约.

2026-09-21 全链路复查 P1-4 回归防护: 美股退出意图不得匹配综合账户下的
非 US. 持仓行 — 若 intent.symbol 被污染为非美股代码, 宁可
exit_position_ownership_conflict 拒退, 也不对他市场仓位动手。离线测试。
"""
import threading

import pandas as pd
import pytest

from app.execution.exit_lifecycle import ExitLifecycle


class _FakeCtx:
    def __init__(self, rows):
        self._rows = rows

    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame(self._rows)


def _lifecycle():
    # preflight 守卫段不触碰实例属性, 绕过 __init__ 重依赖
    return object.__new__(ExitLifecycle)


def test_preflight_rejects_non_us_exact_match():
    """修复前: HK 行按 code 精确匹配成功 → 继续走退出流程 (误碰他市场仓位)。"""
    lc = _lifecycle()
    ctx = _FakeCtx([{"code": "HK.00700", "qty": 300}])
    intent = {"symbol": "HK.00700", "quantity": 300}
    with pytest.raises(ValueError, match="exit_position_ownership_conflict"):
        lc.preflight(intent, ctx, {}, threading.Lock())


def test_preflight_us_match_still_proceeds_past_guard():
    """US. 正常匹配不受守卫影响 — 走过 matches 守卫后因证据不全停在后续校验."""
    lc = _lifecycle()
    ctx = _FakeCtx([{"code": "US.AAPL", "qty": 10}])
    intent = {"symbol": "US.AAPL", "quantity": 10}
    # qty 可解析 → 守卫与 ownership 校验通过; 后续账本逻辑缺依赖抛其他异常,
    # 只要不是 ownership_conflict 即证明守卫未误伤。
    try:
        lc.preflight(intent, ctx, {}, threading.Lock())
    except ValueError as e:
        assert "ownership_conflict" not in str(e)
    except Exception:
        pass
