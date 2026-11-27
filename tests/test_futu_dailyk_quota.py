"""FutuLV3 日K配额门闸回归测试 (2026-08-27 ret=-1 根因).

病灶复盘: EOD 批量 (ic_tracker/shadow_tracker/factor_analyzer) 恰在 24h 整点撞日K缓存
TTL 集体过期, 45 symbol 在 6s 内连发 request_history_kline 打穿 OpenD 账户级频控
(60次/30s), 其后全部 ret=-1 (retMsg: "获取历史K线频率太高") → TwelveData fallback 风暴
→ 免费版 8次/分钟 429 → 10分钟熔断 → 部分标的"所有数据源均失败".

修复三层: ① get_daily_kline 接 _wait_kline_quota 全局窗口 (有界等待)
② 失败日志透出 OpenD retMsg (原 `_` 吞掉) ③ ic_tracker 按 symbol 去重.
"""
import time
import threading
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

    def test_same_symbol_check_and_reservation_are_atomic(self, provider):
        """两个同标的并发调用只能一个穿过冷却并占用配额。"""
        barrier = threading.Barrier(2)
        results = []

        def reserve():
            barrier.wait(timeout=2)
            results.append(provider._wait_kline_quota("US.ATOMIC"))

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert sorted(results) == [False, True]
        assert len(provider._kline_req_times) == 1


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

    def test_phase_fresh_cache_skips_request_and_quota(self, provider, monkeypatch):
        """[RFC-171] 相位新鲜 (末端=上一交易日) 的缓存: 既不打 OpenD 也不耗配额。

        这是盘前 09:0x 62 只候选批次的零点: 缓存落盘后重启进程仍命中 ⇒ 配额窗口
        不被 62 连发打穿 ⇒ TwelveData 回退与 10-30s 等待从路径上消失。
        """
        from app.utils import timezone as _timezone
        phase_now = _timezone.now_et().replace(hour=9, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(_timezone, "now_et", lambda: phase_now)
        from datetime import timedelta as _td
        from app.utils.timezone import now_et as _now_et_test
        # [RFC-171 补] 必须用 ET 日期而非服务器 CST 日期: CST 00:00-12:00 期间 CST 日期
        #   已比 ET 日期早一天 (对应 ET 12:00-24:00), date.today() 会把「ET 今日」当成
        #   上一交易日 ⇒ 盘中半截 bar 被 _session_final_bar 拒收, 测试每天有半天窗口
        #   假红 (2026-10-28 CST 00:05 实测 1 failed)。
        prev = _now_et_test().date() - _td(days=1)
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

    def test_daily_disk_save_merges_multi_instance(self, provider, tmp_path, monkeypatch):
        """[RFC-171] 多实例共享同一落盘文件: B 落盘不得丢掉 A 的条目。

        实测背景: MarketDataRouter 每个实例自建 FutuLV3Provider (非单例), 若落盘用
        覆盖写, 后写者会把别的实例的条目清空 ⇒ 次日盘前重新全量拉取, 配额再次被打穿。
        """
        import app.data.futu_lv3_provider as _fp
        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        bar = [{"time": "2026-10-24 00:00:00", "open": 1, "high": 1, "low": 1,
                "close": 1, "volume": 1, "turnover": 1}]

        provider._daily_kline_cache = {"US.A": bar}
        provider._daily_kline_cache_time = {"US.A": 100.0}
        provider._daily_disk_dirty = True
        provider._daily_disk_save(force=True)

        p2 = FutuLV3Provider(host="127.0.0.1", port=1)
        p2._daily_kline_cache = {"US.B": bar}
        p2._daily_kline_cache_time = {"US.B": 200.0}
        p2._daily_disk_dirty = True
        p2._daily_disk_save(force=True)

        import json as _json
        syms = _json.load(open(disk))["symbols"]
        assert set(syms) == {"US.A", "US.B"}          # 合并, 不互相清空

        # 旧数据不覆盖新数据: A 实例再写一次 (fetched_at 更旧) 不得回退 B
        p2._daily_kline_cache = {"US.A": bar, "US.B": bar}
        p2._daily_kline_cache_time = {"US.A": 50.0, "US.B": 200.0}
        provider._daily_kline_cache = {"US.A": bar}
        provider._daily_kline_cache_time = {"US.A": 50.0}
        provider._daily_disk_dirty = True
        provider._daily_disk_save(force=True)
        syms = _json.load(open(disk))["symbols"]
        assert syms["US.A"]["fetched_at"] == 100.0   # 保留较新者

    def test_daily_disk_concurrent_writers_preserve_all_symbols(self, provider, tmp_path, monkeypatch):
        """跨实例并发“读—合并—替换”不得丢条目或留下混写临时文件。"""
        import json
        import app.data.futu_lv3_provider as _fp

        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        bar = [{"time": "2026-10-24 00:00:00", "open": 1, "high": 1, "low": 1,
                "close": 1, "volume": 1, "turnover": 1}]
        second = FutuLV3Provider(host="127.0.0.1", port=1)
        for offset, instance in enumerate((provider, second)):
            instance._daily_kline_cache = {
                f"US.{side}{i}": bar
                for side in (("A" if offset == 0 else "B"),) for i in range(30)
            }
            instance._daily_kline_cache_time = {
                sym: 100.0 + offset + i for i, sym in enumerate(instance._daily_kline_cache)
            }
            instance._daily_disk_dirty = True

        barrier = threading.Barrier(2)
        errors = []

        def save(instance):
            try:
                barrier.wait(timeout=2)
                instance._daily_disk_save(force=True)
            except Exception as exc:  # 实现内应自行留痕，不得把并发异常抛给调用方
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(instance,))
                   for instance in (provider, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert errors == []
        payload = json.loads(disk.read_text(encoding="utf-8"))
        assert len(payload["symbols"]) == 60
        assert not list(tmp_path.glob("*.tmp"))

    def test_daily_disk_producer_during_flush_keeps_dirty(self, provider, tmp_path, monkeypatch):
        """落盘快照写出的同时生产者新增标的，新 dirty 不得被旧快照清掉。"""
        import json
        import app.data.futu_lv3_provider as _fp

        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        monkeypatch.setattr(_fp, "_session_final_bar", lambda *_args: True)
        bar = [{"time": f"{date.today().isoformat()} 00:00:00", "open": 1, "high": 1,
                "low": 1, "close": 1, "volume": 1, "turnover": 1}]
        provider._daily_kline_cache = {"US.OLD": bar}
        provider._daily_kline_cache_time = {"US.OLD": 100.0}
        provider._daily_disk_dirty = True
        provider._daily_disk_loaded = True
        provider._connected = True
        provider._verify_connection_alive = lambda: True
        monkeypatch.setattr(provider, "_daily_disk_load", lambda: None)
        monkeypatch.setattr(provider, "_acquire_daily_kline_quota",
                            lambda *_args, **_kwargs: True)

        dump_started = threading.Event()
        producer_ready = threading.Event()
        release_dump = threading.Event()
        original_dump = json.dump

        def paused_dump(payload, fh, **kwargs):
            dump_started.set()
            producer_ready.wait(timeout=2)
            release_dump.wait(timeout=2)
            return original_dump(payload, fh, **kwargs)

        class Ctx:
            def request_history_kline(self, code, **kwargs):
                producer_ready.set()
                return 0, pd.DataFrame([{
                    "time_key": f"{date.today().isoformat()} 00:00:00",
                    "open": 2, "high": 2, "low": 2, "close": 2,
                    "volume": 2, "turnover": 2,
                }]), None

        provider._quote_ctx = Ctx()
        monkeypatch.setattr(json, "dump", paused_dump)
        errors = []

        def flush():
            try:
                provider._daily_disk_save(force=True)
            except Exception as exc:
                errors.append(exc)

        def produce():
            try:
                provider.get_daily_kline("US.NEW", count=1)
            except Exception as exc:
                errors.append(exc)

        flush_thread = threading.Thread(target=flush)
        flush_thread.start()
        assert dump_started.wait(timeout=2)
        produce_thread = threading.Thread(target=produce)
        produce_thread.start()
        assert producer_ready.wait(timeout=2)
        time.sleep(0.1)
        release_dump.set()
        flush_thread.join(timeout=2)
        produce_thread.join(timeout=2)
        assert not flush_thread.is_alive()
        assert not produce_thread.is_alive()
        assert errors == []
        assert provider._daily_disk_dirty is True
        provider._daily_disk_save(force=True)
        payload = json.loads(disk.read_text(encoding="utf-8"))
        assert set(payload["symbols"]) == {"US.OLD", "US.NEW"}

    def test_daily_disk_write_failure_keeps_dirty_and_flush_baseline(self, provider, tmp_path, monkeypatch):
        import app.data.futu_lv3_provider as _fp

        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        provider._daily_kline_cache = {
            "US.FAIL": [{"time": "2026-10-24 00:00:00", "open": 1, "high": 1,
                         "low": 1, "close": 1, "volume": 1, "turnover": 1}]
        }
        provider._daily_kline_cache_time = {"US.FAIL": 100.0}
        provider._daily_disk_dirty = True
        provider._daily_disk_last_flush = 123.0
        monkeypatch.setattr(_fp.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk busy")))

        provider._daily_disk_save(force=True)
        assert provider._daily_disk_dirty is True
        assert provider._daily_disk_last_flush == 123.0
        assert not list(tmp_path.glob("*.tmp"))

    def test_daily_disk_roundtrip_survives_restart(self, provider, tmp_path, monkeypatch):
        """[RFC-171] 落盘 → 清内存 → 重新装载: 跨进程重启后仍零请求。"""
        import app.data.futu_lv3_provider as _fp
        from datetime import timedelta as _td
        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        from app.utils import timezone as _timezone
        phase_now = _timezone.now_et().replace(hour=9, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(_timezone, "now_et", lambda: phase_now)
        from app.utils.timezone import now_et as _now_et_test
        # [RFC-171 补] 必须用 ET 日期而非服务器 CST 日期: CST 00:00-12:00 期间 CST 日期
        #   已比 ET 日期早一天 (对应 ET 12:00-24:00), date.today() 会把「ET 今日」当成
        #   上一交易日 ⇒ 盘中半截 bar 被 _session_final_bar 拒收, 测试每天有半天窗口
        #   假红 (2026-10-28 CST 00:05 实测 1 failed)。
        prev = _now_et_test().date() - _td(days=1)
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

    def test_daily_disk_hot_reload_accepts_only_newer_snapshot(self, provider, tmp_path, monkeypatch):
        """盘后任务更新落盘缓存后，长寿进程无需重启即可读到；旧快照不得回写。"""
        import json
        import os
        import app.data.futu_lv3_provider as _fp
        from datetime import timedelta as _td
        from app.utils import timezone as _timezone
        from app.utils.timezone import now_et as _now_et_test

        disk = tmp_path / "daily_kline_v1.json"
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(disk))
        phase_now = _now_et_test().replace(hour=9, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(_timezone, "now_et", lambda: phase_now)
        prev = phase_now.date() - _td(days=1)
        while prev.weekday() >= 5:
            prev -= _td(days=1)

        def bars(close):
            return [{"time": f"{prev.isoformat()} 00:00:00", "open": close,
                     "high": close + 1, "low": close - 1, "close": close,
                     "volume": 1000, "turnover": close * 1000} for _ in range(40)]

        fetched = phase_now.timestamp() - 3600
        disk.write_text(json.dumps({"version": 1, "symbols": {
            "US.PAL": {"bars": bars(10.0), "fetched_at": fetched}}}))
        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return 0, None, None

        self._make_live(provider, 0, Ctx())
        first = provider.get_daily_kline("US.PAL", count=30)
        assert first[-1]["close"] == 10.0

        # 模拟另一进程在收盘后写入更新快照；文件 mtime 与 fetched_at 均推进。
        disk.write_text(json.dumps({"version": 1, "symbols": {
            "US.PAL": {"bars": bars(20.0), "fetched_at": fetched + 60}}}))
        newer_mtime = disk.stat().st_mtime_ns + 1_000_000_000
        os.utime(disk, ns=(newer_mtime, newer_mtime))
        provider._daily_disk_next_probe = 0.0
        second = provider.get_daily_kline("US.PAL", count=30)
        assert second[-1]["close"] == 20.0

        # 即便文件 mtime 再变，较旧 fetched_at 也不得把新值覆盖回去。
        disk.write_text(json.dumps({"version": 1, "symbols": {
            "US.PAL": {"bars": bars(5.0), "fetched_at": fetched - 60}}}))
        older_mtime = newer_mtime + 1_000_000_000
        os.utime(disk, ns=(older_mtime, older_mtime))
        provider._daily_disk_next_probe = 0.0
        third = provider.get_daily_kline("US.PAL", count=30)
        assert third[-1]["close"] == 20.0
        assert calls == []

    def test_quota_free_path_fetches_and_caches(self, provider, tmp_path, monkeypatch):
        # [RFC-171 补] 生产落盘缓存 data/cache/daily_kline_v1.json 已含 US.GOOG 上一交易日
        #   定稿 bar ⇒ _daily_disk_load 命中后会直接返回缓存, OpenD 一次都不打 (非产品 bug,
        #   而是测试非封闭)。这里把落盘路径钉到 tmp ⇒ 装载为空, 才真正测到"取数"分支。
        import app.data.futu_lv3_provider as _fp
        monkeypatch.setattr(_fp, "_DAILY_DISK_PATH", str(tmp_path / "daily_kline_v1.json"))
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

    def test_daily_retry_reserves_quota_for_each_physical_request(self, provider, monkeypatch):
        """首次与退避重试各对应一次配额预占，不能两次请求只记一次账。"""
        provider._daily_disk_loaded = True
        provider._daily_disk_next_probe = float("inf")
        provider._daily_disk_save = lambda *args, **kwargs: None
        today = date.today().isoformat()
        df = pd.DataFrame({
            "time_key": [today] * 10, "open": [10.0] * 10, "high": [11.0] * 10,
            "low": [9.0] * 10, "close": [10.5] * 10,
            "volume": [1000] * 10, "turnover": [10000] * 10,
        })
        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return ((-1, None, "rate limited") if len(calls) == 1 else (0, df, None))

        self._make_live(provider, time.time(), Ctx())
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        out = provider.get_daily_kline("US.RETRY", count=5)
        assert out is not None
        assert len(calls) == 2
        assert len(provider._kline_req_times) == 2

    def test_daily_retry_not_sent_when_second_reservation_fails(self, provider, monkeypatch):
        from unittest.mock import Mock

        provider._daily_disk_loaded = True
        provider._daily_disk_next_probe = float("inf")
        calls = []

        class Ctx:
            def request_history_kline(self, **kw):
                calls.append(kw)
                return -1, None, "rate limited"

        self._make_live(provider, time.time(), Ctx())
        provider._wait_kline_quota = Mock(side_effect=[True, False])
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        assert provider.get_daily_kline("US.NORETRY", count=5) is None
        assert len(calls) == 1
        assert provider._wait_kline_quota.call_count == 2
        assert provider._wait_kline_quota.call_args_list[1].args == ()


class TestSessionFinalBar:
    """[RFC-171] 半截当日 bar 不得落盘/装载 (否则次日整天沿用错误收盘价)。"""

    def test_intraday_today_bar_not_final(self):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from app.data.futu_lv3_provider import _session_final_bar
        ET = ZoneInfo("America/New_York")
        now = _dt(2026, 10, 26, 14, 0, tzinfo=ET)          # 盘中
        fetched = now.timestamp()
        assert _session_final_bar("2026-10-26", fetched, now) is False   # 当日半截 bar
        assert _session_final_bar("2026-10-23", fetched, now) is True    # 历史日已定稿

    def test_postclose_today_bar_is_final_only_if_fetched_after_close(self):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from app.data.futu_lv3_provider import _session_final_bar
        ET = ZoneInfo("America/New_York")
        now = _dt(2026, 10, 26, 18, 0, tzinfo=ET)          # 收盘后
        close = now.replace(hour=16, minute=15).timestamp()
        assert _session_final_bar("2026-10-26", close + 60, now) is True   # 收盘后拉 → 定稿
        assert _session_final_bar("2026-10-26", close - 3600, now) is False  # 盘中拉 → 半截

    def test_future_bar_not_final(self):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from app.data.futu_lv3_provider import _session_final_bar
        ET = ZoneInfo("America/New_York")
        now = _dt(2026, 10, 26, 12, 0, tzinfo=ET)
        assert _session_final_bar("2026-10-27", now.timestamp(), now) is False


class TestDailyCacheHolidayAware:
    """[RFC-171 §6 hotfix 2026-11-27] 上一交易日跳假日 (感恩节次日零成交根因).

    病灶: daily_cache_acceptable 算 prev 只跳周末 → 11-27(感恩节次日) prev=11-26(休市)
    恒无 bar → 含 11-25 定稿 bar 的缓存被误判过期 → 每轮重拉日K撞配额等满 30s → 超 3s
    入场取数预算 → 单飞槽被占、其余 busy、迟到结果不回写 ⇒ 入场死循环 0 成交。
    修复: prev 复用交易日历 SSOT 离线判定 is_us_holiday (跳周末+假日, 不发网络)。
    """

    @pytest.fixture(autouse=True)
    def _offline_calendar(self, monkeypatch):
        # 强制日历走硬编码降级层 (不读 API 热缓存、不发网络) ⇒ 测试确定性。
        from app.utils import holiday_calendar as _hc
        monkeypatch.setattr(_hc.TradingHolidayCalendar, "_cached_trading_day",
                            lambda self, market, d: None)

    def _acceptable(self, last_bar, age_h, now_et):
        from app.data.futu_lv3_provider import daily_cache_acceptable
        fetched = 1_000_000.0
        return daily_cache_acceptable(last_bar, fetched, now_et, fetched + age_h * 3600.0)

    def test_thanksgiving_next_day_accepts_last_trading_day_bar(self):
        # 回归钉子: 11-27 盘中, 末端=11-25(真实上一交易日), age 41.7h>20h。
        # 旧逻辑 prev=11-26(休市) → 拒; 新逻辑 prev=11-25 → 命中。
        from datetime import datetime
        assert self._acceptable("2026-11-25", 41.7, datetime(2026, 11, 27, 9, 0)) is True

    def test_thanksgiving_next_day_genuinely_stale_still_rejected(self):
        from datetime import datetime
        # 末端 11-20 落后上一交易日 11-25 且 age>20h → 仍拒 (10 天硬底未触)。
        assert self._acceptable("2026-11-20", 41.7, datetime(2026, 11, 27, 9, 0)) is False

    def test_holiday_trust_window_still_works(self):
        from datetime import datetime
        # 末端 11-24 落后 prev=11-25, 但 2h<20h 信任窗 → 命中 (信任窗语义不变)。
        assert self._acceptable("2026-11-24", 2.0, datetime(2026, 11, 27, 9, 0)) is True

    def test_normal_monday_weekend_skip_preserved(self):
        from datetime import datetime
        # 周一 11-23: 上一交易日=11-20(周五)跳周末; age 旧但末端=prev → 命中。
        assert self._acceptable("2026-11-20", 70.0, datetime(2026, 11, 23, 9, 0)) is True

    def test_hard_floor_unchanged(self):
        from datetime import datetime
        # 末端 11-10 (>10 天) → 一律拒 (反幻觉硬底不变, 即便 age 新鲜)。
        assert self._acceptable("2026-11-10", 1.0, datetime(2026, 11, 27, 9, 0)) is False

    def test_postclose_branch_unchanged(self):
        from datetime import datetime
        # 收盘后要求当日 bar 且收盘后拉取; 末端 11-25 + age 旧 → 拒 (分支不动)。
        assert self._acceptable("2026-11-25", 41.7, datetime(2026, 11, 27, 17, 0)) is False


class TestMinuteKlineHolidayAware:
    """[RFC-171 §7 hotfix 2026-11-27] 日内K线上一交易日假日感知 (与 §6 同源).

    病灶: get_kline 内联 `Mon→-3 / Sun→-2 / 其余→-1` 只跳周末 → 感恩节次日(11-27)
    prev=11-26(休市)恒无 bar → 日内K start 落在无 bar 日, 盘前/盘初 K_1M 空返 ret=0
    (今日 11 条警告实锤), 且 2026-06-17 P0 修复本意的"前交易日尾盘缓冲"在假日次日失效。
    修复: 抽模块级纯函数 _kline_prev_trading_day, 复用 timezone.get_last_trade_date
    (DEB-20260904 #135 已假日感知, 离线 is_us_holiday, 不触网); 传 today-1 得严格上一交易日。
    关联: gate_liveness_probe._most_recent_et_trading_day 同源 (工作日假日误报 stale) 同批修复。
    """

    @pytest.fixture(autouse=True)
    def _offline_calendar(self, monkeypatch):
        # 强制日历走硬编码降级层 (不读 API 热缓存、不发网络) ⇒ 测试确定性 (同 §6)。
        from app.utils import holiday_calendar as _hc
        monkeypatch.setattr(_hc.TradingHolidayCalendar, "_cached_trading_day",
                            lambda self, market, d: None)

    def _prev(self, y, m, d, hh=8, mm=41):
        from datetime import datetime
        from app.data.futu_lv3_provider import _kline_prev_trading_day
        return _kline_prev_trading_day(datetime(y, m, d, hh, mm))

    def test_thanksgiving_next_day_prev_wednesday(self):
        # 回归钉子: 11-27(感恩节次日, 半日市) 上一交易日=11-25, 跳 11-26 休市。
        # 旧逻辑 prev=11-26(无 bar) → 盘前空返; 新逻辑 prev=11-25。
        assert self._prev(2026, 11, 27) == "2026-11-25"

    def test_normal_tuesday_prev_monday(self):
        assert self._prev(2026, 11, 24, 10, 0) == "2026-11-23"

    def test_monday_prev_friday(self):
        # 周一 11-30 → 上周五 11-27 (半日市仍是交易日, 有 bar)。
        assert self._prev(2026, 11, 30, 10, 0) == "2026-11-27"

    def test_sunday_prev_friday(self):
        assert self._prev(2026, 11, 29, 10, 0) == "2026-11-27"

    def test_saturday_prev_friday(self):
        assert self._prev(2026, 11, 28, 10, 0) == "2026-11-27"

    def test_presidents_day_tuesday_prev_friday(self):
        # 02-16(周一) 总统日休市 → 02-17(周二) 上一交易日=02-13(周五), 跳假日+周末。
        assert self._prev(2026, 2, 17, 10, 0) == "2026-02-13"

    def test_independence_observed_monday_prev_thursday(self):
        # 07-03(周五) 独立日观察休市 (07-04 周六) → 07-06(周一) prev=07-02(周四)。
        assert self._prev(2026, 7, 6, 10, 0) == "2026-07-02"

    def test_get_kline_start_wires_holiday_aware_prev(self, provider, monkeypatch):
        # 钉死调用点: get_kline 把 helper 输出用作日内K start (不依赖真实运行日期)。
        import app.data.futu_lv3_provider as fp
        captured = {}
        monkeypatch.setattr(fp, "_kline_prev_trading_day",
                            lambda now_et: captured.setdefault("helper_out", "2026-11-25"))
        monkeypatch.setattr(provider, "_ensure_connected", lambda: True)
        monkeypatch.setattr(provider, "_get_cached_kline", lambda *a, **k: None)
        monkeypatch.setattr(provider, "_wait_kline_quota", lambda *a, **k: True)
        monkeypatch.setattr(provider, "_set_cached_kline", lambda *a, **k: None)

        class Ctx:
            def request_history_kline(self, **kw):
                captured["start"] = kw.get("start")
                return 0, pd.DataFrame(), None  # 空返 → 只验 start, 不走成交分支

        provider._quote_ctx = Ctx()
        provider.get_kline("US.MSFT", ktype="K_1M", count=100)
        assert captured["helper_out"] == "2026-11-25"
        assert captured["start"] == "2026-11-25"  # = helper 输出 (假日感知 prev)

    def test_gate_liveness_probe_holiday_not_false_stale(self, monkeypatch):
        # 同源关联: 工作日假日 (感恩节 11-26) 应回退 11-25, 而非返回当日无 bar 误报 stale。
        from datetime import datetime
        import app.utils.timezone as tz
        import scripts.gate_liveness_probe as glp
        monkeypatch.setattr(tz, "now_et", lambda: datetime(2026, 11, 26, 10, 0))
        assert glp._most_recent_et_trading_day() == "2026-11-25"

    def test_gate_liveness_probe_trading_day_returns_today(self, monkeypatch):
        from datetime import datetime
        import app.utils.timezone as tz
        import scripts.gate_liveness_probe as glp
        monkeypatch.setattr(tz, "now_et", lambda: datetime(2026, 11, 27, 10, 0))
        assert glp._most_recent_et_trading_day() == "2026-11-27"
