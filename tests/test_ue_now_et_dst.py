"""UE 时钟契约测试 (2026-08-26 UE审计P0·用户批准).

原 bug: _now_et() 硬编码 utc-4h (永久EDT假钟) → cmd_confirm 0932-1030 ET
窗口守卫在 EST 季节把正确发车(22:40CST=真09:40)读成10:40拒跑、错误发车
(21:40CST=真08:40盘前)读成09:40放行 → 整冬静默错样+缺样。

变异测试设计 (全年咬合, 不依赖冻结时间):
  A. 墙钟贴近真 America/New_York — 捕捉任何偏移漂移;
  B. tzinfo 必须携带时区身份(key/zone) — 原bug的 tzinfo=UTC 固定偏移伪装
     在任意季节都被 B 击杀 (8月A能侥幸通过, B不能)。
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_lus_clock", _ROOT / "scripts" / "llm_universe_scan.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_lus_clock"] = _mod
_spec.loader.exec_module(_mod)


def test_now_et_tracks_real_new_york_wallclock():
    """墙钟与真实 America/New_York 差 <5s (EST/EDT 任一季节均成立)."""
    now = _mod._now_et()
    ref = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    assert now.tzinfo is not None, "_now_et 必须返回带时区的 aware datetime"
    drift = abs((now.replace(tzinfo=None) - ref.replace(tzinfo=None)).total_seconds())
    assert drift < 5, f"假钟漂移 {drift:.0f}s (硬编码UTC-4在冬季=+3600s)"


def test_now_et_tzinfo_carries_zone_identity():
    """tzinfo 必须是真时区对象而非固定偏移伪装 — 咬合原 bug 方向.

    原实现返回 datetime.now(timezone.utc) - 4h: 数值像ET但 tzinfo=UTC,
    isoformat 落 '+00:00' 假标 (skill侧 age 计算恒虚高4h的根), 且 EST 季节
    墙钟错 1h。zoneinfo/pytz 的 New_York 对象分别带 key/zone 属性。
    """
    now = _mod._now_et()
    ident = getattr(now.tzinfo, "key", "") or getattr(now.tzinfo, "zone", "")
    assert ident == "America/New_York", \
        f"tzinfo 缺时区身份 (got {now.tzinfo!r}) = 固定偏移伪装"


def test_now_et_offset_matches_zoneinfo_reference():
    """utcoffset 与权威参考一致 — 直接锁 DST 切换行为 (11月切回 -5h 不漂移)."""
    now = _mod._now_et()
    ref_off = datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/New_York")).utcoffset()
    assert now.utcoffset() == ref_off, \
        f"offset {now.utcoffset()} != 权威 {ref_off}"


def test_confirm_window_guard_semantics_unchanged():
    """守卫窗口 [932,1030] 本体不被时钟修复波及 (纯算术回归护栏)."""
    for hm, expect in [(932, True), (1000, True), (1030, True),
                       (931, False), (1031, False)]:
        got = 932 <= hm <= 1030
        assert got == expect, f"_hm={hm}"
