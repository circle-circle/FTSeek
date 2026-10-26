"""FutuLV3 日K配额门闸回归测试 (2026-08-27 ret=-1 根因).

病灶复盘: EOD 批量 (ic_tracker/shadow_tracker/factor_analyzer) 恰在 24h 整点撞日K缓存
TTL 集体过期, 45 symbol 在 6s 内连发 request_history_kline 打穿 OpenD 账户级频控
(60次/30s), 其后全部 ret=-1 (retMsg: "获取历史K线频率太高") → TwelveData fallback 风暴
→ 免费版 8次/分钟 429 → 10分钟熔断 → 部分标的"所有数据源均失败".

修复三层: ① get_daily_kline 接 _wait_kline_quota 全局窗口 (有界等待)
② 失败日志透出 OpenD retMsg (原 `_` 吞掉) ③ ic_tracker 按 symbol 去重.
"""
import time
from datetime import date

import pandas as pd
import pytest

from app.data.futu_lv3_provider import FutuLV3Provider


class _FakeClock:
    """推进式假钟: 每次 time.time() 调用 +step 秒, 让 max_wait 等待循环瞬时耗尽.

    _acquire_daily_kline_quota 每轮循环至少调 2 次 time.time() (_wait_kline_quota 1 次
    + deadline 比较 1 次), step=0.5 时 10s 窗口 <50 轮, 测试毫秒级完成.
    """

    def __init__(self, t0: float, step: float = 0.5):
        self.t = t0
        self.step = step

    def now(self) -> float:
        self.t += self.step
        return self.t


@pytest.fixture
def provider():
    return FutuLV3Provider(host="127.0.0.1", port=1)  # 惰性连接, 构造不碰网络


def _fill_window(provider, t0, n=60):
    """把全局 30s 窗口灌满 n 个新鲜请求戳."""
    provider._kline_req_times = [t0 - 1.0] * n


class TestAcquireDailyKlineQuota:
    def test_acquire_when_window_empty(self, provider):
        assert provider._acquire_daily_kline_quota("D::US.GOOG", max_wait=0.0) is True
        # 请求被记账
        assert len(provider._kline_req_times) == 1

    def test_acquire_fails_when_window_full(self, provider):
        _fill_window(provider, time.time())
        assert provider._acquire_daily_kline_quota("D::US.GOOG", max_wait=0.0) is False
        # 被拒不追加记账 (窗口不因此更长)
        assert len(provider._kline_req_times) == 60

    def test_daily_key_not_collide_with_minute_cooldown(self, provider):
        # 日K请求不应把同标的分钟K挡进单标的冷却 (分键空间 "D::")
        assert provider._acquire_daily_kline_quota("D::US.NVDA", max_wait=0.0) is True
        assert provider._wait_kline_quota("US.NVDA") is True


class TestGetDailyKlineGate:
    def _make_live(self, provider, clock_t0, ctx):
        provider._connected = True
        provider._verify_connection_alive = lambda: True
        provider._quote_ctx = ctx

    def test_quota_full_degrades_to_cache_without_request(self, provider, monkeypatch):
        clock = _FakeClock(1_000_000.0)
        monkeypatch.setattr(time, "time", clock.now)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        # 缓存必须"相位过期"才会走到门闸: 末端 bar 落后于上一交易日 (today-6) 且
        #   拉取时间很旧 (age 远超 20h 假日信任窗) ⇒ daily_cache_acceptable=False。
        from datetime import timedelta as _td
        stale_day = (date.today() - _td(days=6)).isoformat()
        bars = [{"time": f"{stale_day} 00:00:00", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "turnover": 1}] * 40
        provider._daily_kline_cache = {"US.PAL": bars}
        provider._daily_kline_cache_time = {"US.PAL": 1.0}
        _fill_window(provider, clock.t)
        # [RFC-171] 有界等待上限 10s→30s: 假钟每轮 +0.5s ⇒ 60 个新鲜戳会在 30s
        #   假时间窗内自然过期, 门闸会被"放行"(测试假象, 非真实饱和)。这里让窗口
        #   在每个 tick 重新灌满 = 模拟"其它消费方持续占用配额"的真实饱和场景。
        _orig_now = clock.now

        def _now_keep_full():
            t = _orig_now()
            _fill_window(provider, t)
            return t
        monkeypatch.setattr(time, "time", _now_keep_full)

        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return 0, None, None

        self._make_live(provider, clock.t, Ctx())
        out = provider.get_daily_kline("US.PAL", count=30)
        # 门闸挡住: OpenD 一次都没被打, 走缓存降级
        assert calls == []
        assert out == bars[-30:]

    def test_phase_fresh_cache_skips_request_and_quota(self, provider):
        """[RFC-171] 相位新鲜 (末端=上一交易日) 的缓存: 既不打 OpenD 也不耗配额。

        这是盘前 09:0x 62 只候选批次的零点: 缓存落盘后重启进程仍命中 ⇒ 配额窗口
        不被 62 连发打穿 ⇒ TwelveData 回退与 10-30s 等待从路径上消失。
        """
        from datetime import timedelta as _td
        prev = date.today() - _td(days=1)
        while prev.weekday() >= 5:
            prev -= _td(days=1)
        bars = [{"time": f"{prev.isoformat()} 00:00:00", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "turnover": 1}] * 40
        provider._daily_kline_cache = {"US.PAL": bars}
        provider._daily_kline_cache_time = {"US.PAL": time.time() - 3600}
        provider._daily_disk_loaded = True      # 不读真实落盘文件

        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return 0, None, None

        self._make_live(provider, 0, Ctx())
        provider._kline_req_times = []
        out = provider.get_daily_kline("US.PAL", count=30)
        assert calls == []                       # 零 OpenD 请求
        assert provider._kline_req_times == []   # 零配额消耗
        assert out == bars[-30:]

    def test_daily_disk_roundtrip_survives_restart(self, provider, tmp_path, monkeypatch):
        """[RFC-171] 落盘 → 清内存 → 重新装载: 跨进程重启后仍零请求。"""
        import app.data.futu_lv3_provider as _fp
        from datetime import timedelta as _td
        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        prev = date.today() - _td(days=1)
        while prev.weekday() >= 5:
            prev -= _td(days=1)
        bars = [{"time": f"{prev.isoformat()} 00:00:00", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "turnover": 1}] * 40
        provider._daily_kline_cache = {"US.PAL": bars}
        provider._daily_kline_cache_time = {"US.PAL": time.time() - 3600}
        provider._daily_disk_dirty = True
        provider._daily_disk_save(force=True)
        assert disk.exists()

        p2 = FutuLV3Provider(host="127.0.0.1", port=1)
        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return 0, None, None
        self._make_live(p2, 0, Ctx())
        out = p2.get_daily_kline("US.PAL", count=30)
        assert calls == []
        assert out == bars[-30:]

    def test_quota_free_path_fetches_and_caches(self, provider):
        real_today = date.today().isoformat()
        df = pd.DataFrame({
            "time_key": [f"{real_today} 00:00:00"] * 100,
            "open": [10.0] * 100, "high": [11.0] * 100, "low": [9.0] * 100,
            "close": [10.5] * 100, "volume": [1000] * 100, "turnover": [10500] * 100,
        })
        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return 0, df, None

        self._make_live(provider, time.time(), Ctx())
        out = provider.get_daily_kline("US.GOOG", count=30)
        assert len(calls) == 1
        assert out is not None and len(out) == 30
        assert "US.GOOG" in provider._daily_kline_cache

    def test_freq_limit_failure_message_surfaced(self, provider, caplog):
        # retMsg 不再被 `_` 吞掉: warning 必须带 OpenD 原文
        self._make_live(provider, time.time(), None)

        class Ctx:
            def request_history_kline(self, **kw):
                return -1, None, "获取历史K线频率太高，请求失败，每30秒最多60次。"

        provider._quote_ctx = Ctx()
        import logging
        with caplog.at_level(logging.WARNING):
            out = provider.get_daily_kline("US.MSI", count=30)
        assert out is None
        assert "频率太高" in caplog.text
