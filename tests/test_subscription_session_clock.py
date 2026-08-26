"""订阅管理器时段时钟契约测试 (2026-08-26 修复·用户批准).

原 bug: SubscriptionManager._auto_loop 用服务器本地 naive 挂钟喂
should_be_subscribed → MarketHours 契约把 naive 按 ET 解释 → 北京挂钟数字对
ET 时段边界。有效订阅窗口反转为 CST[04:00,20:00], 每晚美股整个常规盘中
(21:30-04:00 CST) 被误退订且拒绝重订 (多日日志实证: 订阅@04:00 / 退订@20:00)。

变异测试设计 (全年咬合):
  A. 行为面: 真·aware ET 的常规盘中时刻必须判活跃 — 旧接线在相同时刻用
     naive 本地挂钟评估得 OVERNIGHT=False, 任意季节被击杀;
     注意假日历覆盖至 2026 年末, 冬季样例取覆盖窗内日期 (远期 fail-closed)。
  B. 接线面: _auto_loop 代码禁现 naive 本地时钟调用, 必须用模块统一 now_et()
     (Task #32 同源); scheduler_triggers 零参 get_market_phase 禁再传参
     (原 TypeError 被 except 吞掉 = 周末静默特性静默死亡)。
"""
import inspect
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.shared.market_hours import MarketHours

_ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_ET)


# ── A. 行为面: 常规盘中必须可订阅 ─────────────────────────────
def test_summer_regular_session_is_active():
    """10:00 EDT 夏季盘中 → 活跃."""
    assert MarketHours.is_us_active(_et(2026, 8, 12, 10, 0)) is True


def test_winter_regular_session_is_active():
    """11:00 EST 冬季盘中 → 活跃 (原bug此窗恒 False)."""
    assert MarketHours.is_us_active(_et(2026, 12, 10, 11, 0)) is True


def test_winter_market_open_boundary_is_active():
    """09:30 EST 开盘边界 (含) → 活跃."""
    assert MarketHours.is_us_active(_et(2026, 12, 10, 9, 30)) is True


def test_overnight_is_not_active():
    """20:30 EDT 隔夜休眠 → 不活跃 (退订语义保留)."""
    assert MarketHours.is_us_active(_et(2026, 8, 12, 20, 30)) is False


def test_pre_market_is_active():
    """05:00 EDT 盘前 → 活跃 (docstring 契约: pre-market+trading+after-hours)."""
    assert MarketHours.is_us_active(_et(2026, 8, 12, 5, 0)) is True


# ── B. 接线面: _auto_loop 源码契约 ────────────────────────────
def _auto_loop_code() -> str:
    from app.data.subscription_manager import SubscriptionManager
    src = inspect.getsource(SubscriptionManager._auto_loop)
    # 剥离整行注释 — 断言针对可执行代码, 不被解释性注释误触
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_auto_loop_bans_naive_server_clock():
    """循环体代码禁本地 naive 时钟调用, 必须用统一 now_et()."""
    code = _auto_loop_code()
    assert "datetime.now()" not in code, \
        "_auto_loop 禁用 naive 本地时钟 — 会绕过 MarketHours 的 8/10 时区修复"
    assert "now_et()" in code, "_auto_loop 必须使用模块统一 aware ET 时钟"


def test_auto_loop_weekend_check_zero_arg_call():
    """scheduler_triggers.get_market_phase 是零参函数 — 传参=TypeError被吞=静默死亡."""
    code = _auto_loop_code()
    assert "get_market_phase(now" not in code, "零参函数禁止传参 (原静默死亡根因)"
    assert "_st_phase()" in code, "周末检查必须走无参调用"
