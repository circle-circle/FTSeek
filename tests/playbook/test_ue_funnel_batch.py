"""[P1.5 / P4.1 2026-09-12] UE 批次纪律 · 额度幂等 · 固定终点评分 (proud-slope-falcon).

被测对象:
  `app/playbook/ue_batch.py`  — latest_valid_batch / latest_premarket / injection_lock /
                                effective_ue_symbols / confirm_saved / next_close /
                                candidate_score
  `scripts/llm_universe_scan.py` — cmd_inject 的批次消费纪律与写后确认接线 (源级契约)

修前的三个洞 (DEB-20260910 问题②③⑧), 本文件逐条钉死:
  ① inject 取"当日最后一条 type==scan" ⇒ da_feed (07:40 ET) / midday 与 premarket
     (09:10 ET) 同写主 jsonl, 可能误消费**异窗**批次;
  ② 空扫描不落批次 ⇒ 静默回用早先 (可能隔夜/过期) 候选, 且"当日无机会"与"批次失败"
     在账上无法区分;
  ③ `save_instruction()` 返回 ID 不代表入围成功 (db.py 覆盖仲裁拒绝时同样返回现存行
     ID) ⇒ 名额被"假成功"吃掉; 重跑/cron 与手动并发会双花额度;
  ④ cmd_score 用**评分运行时现价**当前瞻终点 ⇒ 同一批次今天跑与下周跑得到不同
     "前瞻收益", 尺子本身会漂。

DST/假日口径: 终点用 ET 墙上时间 (16:00, 提前收盘日 13:00) 构造, 不做固定 UTC 偏移;
naive datetime 一律拒绝 (app.utils.timezone.to_et() 把 naive 当北京时间, 会移半天)。
"""
import inspect
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.playbook import ue_batch as ub  # noqa: E402
from app.playbook.ue_batch import (  # noqa: E402
    SCORE_VERSION, candidate_score, confirm_saved, effective_ue_symbols,
    injection_lock, latest_premarket, latest_valid_batch, next_close, positive,
)
from scripts import llm_universe_scan as ue  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 11, 9, 30, tzinfo=ET)
TODAY = "2026-09-11"
MAX_AGE = 3.0


def rec(window="premarket", minutes_ago=20, surfaced=None, status="complete",
        rtype="scan", day=TODAY, stamp=None, batch_id=None, **extra):
    at = stamp if stamp is not None else (NOW - timedelta(minutes=minutes_ago)).isoformat()
    row = {"type": rtype, "date": day, "scanned_at": at, "started_at": at,
           "scan_window": window, "batch_status": status,
           "batch_id": batch_id or f"UE:{window}:{minutes_ago}",
           "surfaced": surfaced if surfaced is not None else []}
    row.update(extra)
    return row


def surfaced(name, direction="LONG", **kw):
    row = {"symbol": name, "direction": direction, "confidence": 0.8,
           "scan_price": 100.0, "thesis": "t"}
    row.update(kw)
    return row


# ══ ① 窗口隔离 + 批次时效 ════════════════════════════════════════════════
class TestWindowIsolation:
    def test_premarket_only_from_premarket_window(self):
        rows = [rec("da_feed", 20, [surfaced("AAA")]),
                rec("midday", 10, [surfaced("MMM")]),
                rec("premarket", 5, [surfaced("PPP")])]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert reason == "complete"
        assert [s["symbol"] for s in got["surfaced"]] == ["PPP"]

    def test_da_feed_window_is_separate(self):
        rows = [rec("premarket", 5, [surfaced("PPP")]),
                rec("da_feed", 20, [surfaced("AAA")])]
        got, reason = latest_valid_batch(rows, TODAY, NOW, MAX_AGE, window="da_feed")
        assert reason == "complete" and got["surfaced"][0]["symbol"] == "AAA"

    def test_missing_window_is_not_an_error_state(self):
        rows = [rec("premarket", 5, [surfaced("PPP")])]
        got, reason = latest_valid_batch(rows, TODAY, NOW, MAX_AGE, window="da_feed")
        assert got is None and reason == "no_da_feed_attempt"

    def test_other_day_records_ignored(self):
        """跨日不得消费: 昨日批次即使很"新"也不是今日候选。"""
        rows = [rec("premarket", 20, [surfaced("OLD")], day="2026-09-10")]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert got is None and reason == "no_premarket_attempt"

    def test_legacy_rows_without_scan_window_default_to_premarket(self):
        rows = [{"type": "scan", "date": TODAY,
                 "scanned_at": (NOW - timedelta(minutes=9)).isoformat(),
                 "surfaced": [surfaced("LEG")]}]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert reason == "complete" and got["surfaced"][0]["symbol"] == "LEG"

    def test_non_batch_records_ignored(self):
        rows = [{"type": "score", "date": TODAY, "scan_window": "premarket"},
                {"type": "inject", "date": TODAY, "scan_window": "premarket"},
                rec("premarket", 5, [surfaced("PPP")])]
        got, _ = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert got["surfaced"][0]["symbol"] == "PPP"


class TestBatchFreshness:
    def test_stale_batch_rejected(self):
        got, reason = latest_premarket([rec(minutes_ago=400)], TODAY, NOW, MAX_AGE)
        assert got is None and reason == "stale_or_future"

    def test_future_batch_rejected(self):
        got, reason = latest_premarket([rec(minutes_ago=-120)], TODAY, NOW, MAX_AGE)
        assert got is None and reason == "stale_or_future"

    def test_age_boundary_is_inclusive(self):
        exact = NOW - timedelta(hours=MAX_AGE)
        got, reason = latest_premarket([rec(stamp=exact.isoformat())], TODAY, NOW, MAX_AGE)
        assert reason == "complete" and got is not None
        over = exact - timedelta(seconds=1)
        assert latest_premarket([rec(stamp=over.isoformat())], TODAY, NOW, MAX_AGE)[1] \
            == "stale_or_future"

    def test_naive_timestamp_rejected_not_guessed(self):
        got, reason = latest_premarket(
            [rec(stamp="2026-09-11T09:10:00")], TODAY, NOW, MAX_AGE)
        assert got is None and reason == "missing_or_invalid_timestamp"

    def test_missing_and_garbage_timestamps_rejected(self):
        for bad in (None, "", "not-a-time"):
            row = rec()
            row["started_at"] = row["scanned_at"] = bad
            assert latest_premarket([row], TODAY, NOW, MAX_AGE)[1] == \
                "missing_or_invalid_timestamp", bad

    def test_naive_now_rejected_not_guessed(self):
        """naive `now` ⇒ 年龄无法计算 ⇒ 拒绝消费 (不当 UTC、不当本地时区)。"""
        got, reason = latest_premarket(
            [rec()], TODAY, datetime(2026, 9, 11, 9, 30), MAX_AGE)
        assert got is None and reason == "missing_or_invalid_timestamp"

    def test_max_age_is_caller_supplied_not_hardcoded(self):
        rows = [rec(minutes_ago=200)]              # 3.33h
        assert latest_premarket(rows, TODAY, NOW, 2.0)[0] is None
        assert latest_premarket(rows, TODAY, NOW, 4.0)[0] is not None


# ══ ② 空批 / 失败批 / 更晚尝试使旧成功批失效 ═════════════════════════════
class TestBatchTerminalStates:
    def test_latest_empty_batch_is_complete_not_failure(self):
        """空批次 = 当日无机会的正常结束; 调用方按空处理, 不回用旧候选。"""
        rows = [rec(minutes_ago=60, surfaced=[surfaced("OLD")]),
                rec(minutes_ago=5, surfaced=[])]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert reason == "complete" and got["surfaced"] == []

    def test_earlier_success_not_reused_after_empty_batch(self):
        rows = [rec(minutes_ago=60, surfaced=[surfaced("OLD")]),
                rec(minutes_ago=5, surfaced=[])]
        got, _ = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert [s["symbol"] for s in got["surfaced"]] == []

    def test_failed_batch_end_invalidates_earlier_success(self):
        rows = [rec(minutes_ago=60, surfaced=[surfaced("OLD")]),
                rec(minutes_ago=5, surfaced=[], status="failed", rtype="batch_end")]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert got is None and reason == "failed"

    def test_complete_no_scan_invalidates(self):
        """rc=0 但未落 scan 记录 (如影子关闭) ⇒ 显式作废, 不静默回用旧批。"""
        rows = [rec(minutes_ago=60, surfaced=[surfaced("OLD")]),
                rec(minutes_ago=5, status="complete_no_scan", rtype="batch_end")]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert got is None and reason == "complete_no_scan"

    def test_pending_batch_start_without_scan_is_not_consumable(self):
        rows = [rec(minutes_ago=60, surfaced=[surfaced("OLD")]),
                rec(minutes_ago=5, status="pending", rtype="batch_start",
                    batch_id="UE:new")]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert got is None and reason == "pending"

    def test_latest_batch_id_wins_even_if_written_earlier(self):
        """完成顺序 != 开始顺序 (两跑重叠): 以最新 batch_id 的终态为准。"""
        rows = [
            rec(minutes_ago=30, surfaced=[surfaced("FIRST")], batch_id="UE:b1"),
            rec(minutes_ago=25, status="pending", rtype="batch_start", batch_id="UE:b2"),
            rec(minutes_ago=20, surfaced=[surfaced("SECOND")], batch_id="UE:b1"),
            rec(minutes_ago=5, surfaced=[surfaced("THIRD")], batch_id="UE:b2"),
        ]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert reason == "complete"
        assert [s["symbol"] for s in got["surfaced"]] == ["THIRD"]

    def test_mixed_windows_do_not_invalidate_each_other(self):
        """da_feed 的失败批次不得作废 premarket 的成功批次 (反之亦然)。"""
        rows = [rec("premarket", 20, [surfaced("PPP")]),
                rec("da_feed", 5, status="failed", rtype="batch_end")]
        got, reason = latest_premarket(rows, TODAY, NOW, MAX_AGE)
        assert reason == "complete" and got["surfaced"][0]["symbol"] == "PPP"


# ══ ③ 额度: 唯一有效入围累计, 重跑不重占 ═════════════════════════════════
class TestEffectiveUeSymbols:
    def row(self, symbol="AAA", direction="BUY", decision_id="UE:x", source="UE",
            skip_reason=None, **extra):
        r = {"symbol": symbol, "direction": direction, "decision_id": decision_id,
            "skip_reason": skip_reason,
            "raw_instruction": json.dumps({"source": source})}
        r.update(extra)
        return r

    def test_counts_unique_symbol_once_across_rounds(self):
        """RFC-066 多行台账: 同标的多轮终局执行只占一个名额。"""
        rows = [self.row(round_id=1), self.row(round_id=2), self.row(round_id=3)]
        assert effective_ue_symbols(rows, "UE") == {"AAA"}

    def test_skip_rows_do_not_consume_quota(self):
        rows = [self.row(symbol="AAA", direction="SKIP", skip_reason="low_atr"),
                self.row(symbol="BBB")]
        assert effective_ue_symbols(rows, "UE") == {"BBB"}

    def test_other_source_rows_do_not_consume_quota(self):
        rows = [self.row(symbol="CCC", decision_id="DA-2026-09-11:CCC", source="DA"),
                self.row(symbol="DDD")]
        assert effective_ue_symbols(rows, "UE") == {"DDD"}

    def test_decision_id_prefix_or_raw_source_both_qualify(self):
        rows = [self.row(symbol="EEE", decision_id="OTHER", source="UE"),
                self.row(symbol="FFF", decision_id="UE:y", source="DA")]
        assert effective_ue_symbols(rows, "UE") == {"EEE", "FFF"}

    @pytest.mark.parametrize("direction", ["BUY", "SELL_SHORT", "LONG", "SHORT"])
    def test_short_and_long_vocabulary_accepted(self, direction):
        assert effective_ue_symbols([self.row(direction=direction)], "UE") == {"AAA"}

    @pytest.mark.parametrize("direction", ["SKIP", "CANCEL", "", None])
    def test_non_position_directions_excluded(self, direction):
        assert effective_ue_symbols([self.row(direction=direction)], "UE") == set()

    def test_raw_instruction_dict_and_string_both_parsed(self):
        rows = [{"symbol": "GGG", "direction": "BUY", "decision_id": "",
                 "skip_reason": None, "raw_instruction": {"source": "UE"}},
                self.row(symbol="HHH")]
        assert effective_ue_symbols(rows, "UE") == {"GGG", "HHH"}

    def test_malformed_raw_instruction_does_not_crash(self):
        rows = [{"symbol": "III", "direction": "BUY", "decision_id": "",
                 "skip_reason": None, "raw_instruction": "{not json"}]
        assert effective_ue_symbols(rows, "UE") == set()


# ══ ③ 写后确认: 返回 ID 不是入围凭证 ═════════════════════════════════════
class FakeDB:
    def __init__(self, row=None, raises=False):
        self.row, self.raises = row, raises

    def get_instruction_by_id(self, rid):
        if self.raises:
            raise RuntimeError("db down")
        return self.row


def instruction(**over):
    base = {"decision_id": "UE:2026-09-11:AAA", "symbol": "US.AAA",
            "direction": "BUY", "skip_reason": None, "status": "PENDING",
            "raw_instruction": {"source": "UE",
                                "candidate_screening": {"candidate_id": "UE:b1:US.AAA:LONG"}}}
    base.update(over)
    return base


def saved_row(**over):
    row = dict(instruction())
    row.update(over)
    return row


class TestConfirmSaved:
    def test_saved(self):
        assert confirm_saved(FakeDB(saved_row()), instruction(), 7) == "saved"

    def test_none_id_is_unknown_not_success(self):
        assert confirm_saved(FakeDB(saved_row()), instruction(), None) == "unknown"

    def test_missing_row_is_unknown(self):
        assert confirm_saved(FakeDB(None), instruction(), 7) == "unknown"
        assert confirm_saved(FakeDB({}), instruction(), 7) == "unknown"

    def test_db_error_is_unknown(self):
        assert confirm_saved(FakeDB(raises=True), instruction(), 7) == "unknown"

    @pytest.mark.parametrize("field,value", [
        ("decision_id", "DA-2026-09-11:AAA"),
        ("symbol", "US.OTHER"),
        ("direction", "SELL_SHORT"),
        ("skip_reason", "da_priority_skip"),
        ("status", "REJECTED"),
        ("status", "SKIP"),
        ("status", "CANCELLED"),
    ])
    def test_mismatch_is_rejected(self, field, value):
        assert confirm_saved(FakeDB(saved_row(**{field: value})), instruction(), 7) \
            == "rejected"

    def test_executed_or_settled_row_is_rejected(self):
        assert confirm_saved(FakeDB(saved_row(executed=1)), instruction(), 7) == "rejected"
        assert confirm_saved(FakeDB(saved_row(outcome="WIN")), instruction(), 7) == "rejected"
        assert confirm_saved(FakeDB(saved_row(outcome="PENDING")), instruction(), 7) == "saved"

    def test_candidate_id_mismatch_is_rejected(self):
        """覆盖仲裁保留旧行时 candidate_screening 是**别人的** ⇒ 不是本轮入围。"""
        row = saved_row()
        row["raw_instruction"] = {"source": "UE",
                                  "candidate_screening": {"candidate_id": "UE:b0:US.AAA:LONG"}}
        assert confirm_saved(FakeDB(row), instruction(), 7) == "rejected"

    def test_intended_skip_row_with_own_reason_is_saved(self):
        """资格拒绝的审计行本身就是 SKIP + skip_reason ⇒ 那是**预期内容**, 不是仲裁拒绝。

        修前 skip_reason 一律按 truthy 判 rejected: 离线全链 (2026-09-12) 把成功写入的
        stale_quote / price_floor 两行误报成"写库仲裁拒绝 → 补位", 归因与额度计数双错。
        """
        want = instruction(direction="SKIP", skip_reason="candidate_ineligible(stale_quote)")
        assert confirm_saved(FakeDB(saved_row(**want)), want, 1) == "saved"

    def test_intended_skip_row_with_rewritten_reason_is_rejected(self):
        want = instruction(direction="SKIP", skip_reason="price_floor(3.00<5)")
        row = saved_row(**want)
        row["skip_reason"] = "da_priority_skip"
        assert confirm_saved(FakeDB(row), want, 1) == "rejected"

    def test_eligible_row_downgraded_to_skip_is_rejected(self):
        """反向仍严格: 合格指令被落成 SKIP (admission 降级等) = 确定拒绝, 要补位。"""
        want = instruction(direction="BUY", skip_reason=None)
        assert confirm_saved(FakeDB(saved_row(skip_reason="admission_min_notional")),
                             want, 7) == "rejected"

    def test_source_mismatch_is_rejected(self):
        row = saved_row()
        row["raw_instruction"] = dict(instruction()["raw_instruction"], source="DA")
        assert confirm_saved(FakeDB(row), instruction(), 7) == "rejected"

    def test_raw_instruction_as_json_string(self):
        row = saved_row(raw_instruction=json.dumps(instruction()["raw_instruction"]))
        assert confirm_saved(FakeDB(row), instruction(), 7) == "saved"

    def test_missing_candidate_identity_is_unknown_not_rejected(self):
        """无候选身份 ⇒ 无法核对 ⇒ "结果不明"(停止补位先核对), 不是"确定拒绝"。

        初筛整体 fail-open 时 (screening_error 路径) 指令不带 candidate_id,
        若判成 rejected 会让成功写入被当拒绝 → 名额统计与实际入库分叉。
        """
        inst = instruction(raw_instruction={"source": "UE"})
        assert confirm_saved(FakeDB(saved_row(raw_instruction={"source": "UE"})),
                             inst, 7) == "unknown"


# ══ ③ 并发: 读额度→筛选→写后确认 全程持锁 ════════════════════════════════
class TestInjectionLock:
    def test_lock_serializes_read_modify_write(self, tmp_path):
        """cron 与手动重跑并发不得双花名额: 无丢失更新。"""
        target = tmp_path / "ue_inject_quota.lock"
        counter = tmp_path / "counter.json"
        counter.write_text("0", encoding="utf-8")
        inside, overlap = [], []
        lock = threading.Lock()

        def worker():
            for _ in range(25):
                with injection_lock(target):
                    with lock:
                        inside.append(1)
                        if len(inside) > 1:
                            overlap.append(len(inside))
                    value = int(counter.read_text(encoding="utf-8"))
                    counter.write_text(str(value + 1), encoding="utf-8")
                    with lock:
                        inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert int(counter.read_text(encoding="utf-8")) == 200
        assert overlap == []

    def test_lock_file_created_if_absent(self, tmp_path):
        target = tmp_path / "nested" / "ue.lock"
        with injection_lock(target):
            assert target.exists()

    def test_lock_released_on_exception(self, tmp_path):
        target = tmp_path / "ue.lock"
        with pytest.raises(RuntimeError):
            with injection_lock(target):
                raise RuntimeError("boom")
        with injection_lock(target):        # 不死锁
            pass


# ══ ④ 固定终点评分 ═══════════════════════════════════════════════════════
class FakeCal:
    """注入日历: 只实现 next_trading_day(market, from_date) 这一 SSOT 接口。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def next_trading_day(self, market, from_date):
        self.calls.append((market, from_date))
        return self.mapping.get(str(from_date))


CAL = FakeCal({"2026-09-11": date(2026, 9, 14)})        # 周五 → 下周一
ENDPOINT = datetime(2026, 9, 14, 16, 0, tzinfo=ET)
AFTER = ENDPOINT + timedelta(minutes=1)


def scan(**over):
    base = {"date": TODAY, "batch_id": "UE:b1", "policy_version": "da_ue_v1"}
    base.update(over)
    return base


def cand(symbol="AAA", direction="LONG", price=100.0, cid="UE:b1:US.AAA:LONG", **over):
    row = {"symbol": symbol, "direction": direction, "scan_price": price,
           "candidate_screening": {"candidate_id": cid}, "self_tier": "T1",
           "emergence_streak_days": 1}
    row.update(over)
    return row


def bars(close, day="2026-09-14", key="time", complete=True, extra=()):
    out = [{key: day, "open": close, "high": close, "low": close,
            "close": close, "complete": complete}]
    out.extend(extra)
    return out


class TestNextClose:
    def test_accepts_string_date_and_aware_datetime(self):
        cal = FakeCal({"2026-09-11": date(2026, 9, 14)})
        assert next_close("2026-09-11", cal) == ENDPOINT
        assert next_close(date(2026, 9, 11), cal) == ENDPOINT
        assert next_close(NOW, cal) == ENDPOINT

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError):
            next_close(datetime(2026, 9, 11, 9, 30), CAL)

    def test_rejects_bad_type_and_string(self):
        with pytest.raises(TypeError):
            next_close(12345, CAL)
        with pytest.raises(ValueError):
            next_close("nope", CAL)

    def test_holiday_and_weekend_skip(self):
        """劳动节长周末: 周五 9/4 的下一交易日是周二 9/8 (9/7 休市)。"""
        cal = FakeCal({"2026-09-04": date(2026, 9, 8)})
        got = next_close("2026-09-04", cal)
        assert got.date() == date(2026, 9, 8) and got.hour == 16

    def test_dst_transition_uses_et_wall_clock(self):
        """跨 DST 不做固定 UTC 偏移: 11/6 (EDT) → 11/9 (EST) 收盘仍是 16:00 ET。"""
        cal = FakeCal({"2026-11-06": date(2026, 11, 9)})
        got = next_close("2026-11-06", cal)
        assert (got.hour, got.utcoffset()) == (16, timedelta(hours=-5))
        before = next_close("2026-09-11", CAL)
        assert before.utcoffset() == timedelta(hours=-4)

    def test_early_close_day_is_13_00(self):
        """提前收盘日 (2026-11-27 感恩节次日) 终点 = 13:00 ET, 不是 16:00。"""
        cal = FakeCal({"2026-11-26": date(2026, 11, 27)})
        got = next_close("2026-11-26", cal)
        assert (got.date(), got.hour) == (date(2026, 11, 27), 13)

    def test_market_argument_is_us(self):
        cal = FakeCal({"2026-09-11": date(2026, 9, 14)})
        next_close("2026-09-11", cal)
        assert cal.calls == [("US", date(2026, 9, 11))]

    def test_unavailable_next_session_raises(self):
        with pytest.raises(LookupError):
            next_close("2026-09-11", FakeCal({}))

    def test_default_calendar_is_repo_ssot_not_exchange_calendars(self, monkeypatch):
        """反死门钉: 默认日历必须走仓内 SSOT。

        本环境未安装 exchange_calendars ⇒ 用它就是恒抛 ImportError 的死路径
        (被 candidate_score 的 except 吞成 calendar_unavailable, 评分永不产出)。
        """
        import app.utils.holiday_calendar as hc
        seen = []

        class Spy:
            def next_trading_day(self, market, from_date):
                seen.append((market, from_date))
                return date(2026, 9, 14)

        monkeypatch.setattr(hc, "get_holiday_calendar", lambda: Spy())
        assert next_close("2026-09-11").date() == date(2026, 9, 14)
        assert seen == [("US", date(2026, 9, 11))]
        # 查字节码引用的名字, 不查源码文本 (docstring 会提到被弃用的依赖)
        names = set(ub._default_calendar.__code__.co_names) | set(ub.next_close.__code__.co_names)
        assert "exchange_calendars" not in names, sorted(names)
        assert "get_holiday_calendar" in names

    def test_datetime_input_normalized_to_et_day(self):
        """UTC 深夜 = ET 同日晚间: 不得因时区错位把终点算成后一天。"""
        cal = FakeCal({"2026-09-11": date(2026, 9, 14)})
        late_utc = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)   # 22:00 ET 9/11
        assert next_close(late_utc, cal).date() == date(2026, 9, 14)


class TestCandidateScore:
    def test_pending_before_endpoint(self):
        got = candidate_score(scan(), cand(), NOW, bars(110.0), calendar=CAL)
        assert got["status"] == "pending"
        assert got["reason"] == "endpoint_not_complete"
        assert got["endpoint_at"] == ENDPOINT.isoformat()
        assert "fwd_pct" not in got and "scored_price" not in got

    def test_complete_uses_only_t1_close(self):
        got = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=CAL)
        assert got["status"] == "complete"
        assert got["scan_price"] == 100.0 and got["scored_price"] == 110.0
        assert got["raw_fwd_pct"] == pytest.approx(10.0)
        assert got["fwd_pct"] == pytest.approx(10.0)
        assert got["hit"] is True
        assert got["endpoint_at"] == ENDPOINT.isoformat()

    def test_short_sign_flipped(self):
        c = cand(direction="SHORT", cid="UE:b1:US.AAA:SHORT")
        got = candidate_score(scan(), c, AFTER, bars(110.0), calendar=CAL)
        assert got["raw_fwd_pct"] == pytest.approx(10.0)     # 价格本身涨了 10%
        assert got["fwd_pct"] == pytest.approx(-10.0)        # SHORT 方向收益为负
        assert got["hit"] is False

    def test_long_price_drop_is_negative(self):
        got = candidate_score(scan(), cand(), AFTER, bars(90.0), calendar=CAL)
        assert got["fwd_pct"] == pytest.approx(-10.0) and got["hit"] is False

    def test_hit_threshold_is_one_percent(self):
        assert candidate_score(scan(), cand(), AFTER, bars(101.0), CAL)["hit"] is True
        assert candidate_score(scan(), cand(), AFTER, bars(100.5), CAL)["hit"] is False

    def test_measurement_is_price_return_not_net_pnl(self):
        """量纲声明军规: 这是价格收益, 不是扣费净损益 — 不得被当成交损益读。"""
        got = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=CAL)
        assert got["measurement"] == "price_return_not_net_execution_pnl"
        assert got["score_version"] == SCORE_VERSION == "ue_t1_close_v1"

    def test_no_runtime_quote_is_used(self):
        """终点只认已完成日 K: 传入的 bars 是唯一价格来源, 结果里没有实时报价字段。"""
        got = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=CAL)
        for banned in ("quote", "last_price", "price_now", "cur"):
            assert banned not in got, banned

    def test_censored_without_candidate_identity(self):
        got = candidate_score(scan(), cand(cid=None), AFTER, bars(110.0), calendar=CAL)
        assert got["status"] == "censored"
        assert got["reason"] == "missing_candidate_identity"
        assert "fwd_pct" not in got

    def test_top_level_candidate_id_also_accepted(self):
        c = {"symbol": "AAA", "direction": "LONG", "scan_price": 100.0,
             "candidate_id": "UE:b1:US.AAA:LONG"}
        assert candidate_score(scan(), c, AFTER, bars(110.0), CAL)["status"] == "complete"

    def test_calendar_unavailable_stays_pending_with_reason(self):
        got = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=FakeCal({}))
        assert got["status"] == "pending"
        assert got["reason"].startswith("calendar_unavailable:")

    @pytest.mark.parametrize("bar_set,price", [
        ([], 100.0),                                       # 无当日 K
        ([{"time": "2026-09-13", "close": 110.0}], 100.0),  # 日期不匹配
        ([{"time": "2026-09-14", "close": 110.0, "complete": False}], 100.0),
        ([{"time": "2026-09-14", "close": 110.0},
          {"time": "2026-09-14", "close": 120.0}], 100.0),  # 收盘歧义
    ])
    def test_missing_or_ambiguous_price_is_recoverable_not_guessed(self, bar_set, price):
        """缺价/歧义 → 显式待补算, 绝不外推、不取均值、不用运行时现价冒充。"""
        got = candidate_score(scan(), cand(), AFTER, bar_set, calendar=CAL)
        assert got["status"] == "pending"
        assert got["reason"] == "missing_or_ambiguous_price"
        assert "scored_price" not in got

    def test_missing_scan_price_is_recoverable(self):
        got = candidate_score(scan(), cand(price=None), AFTER, bars(110.0), calendar=CAL)
        assert got["status"] == "pending" and got["reason"] == "missing_or_ambiguous_price"

    def test_date_key_and_duplicate_same_close_accepted(self):
        """`date` 键与 `time` 键同义; 同日重复但收盘一致 ⇒ 不算歧义。"""
        by_date = candidate_score(scan(), cand(), AFTER,
                                  bars(110.0, key="date"), calendar=CAL)
        assert by_date["status"] == "complete"
        dup = candidate_score(scan(), cand(), AFTER,
                              bars(110.0, extra=[{"time": "2026-09-14", "close": 110.0}]),
                              calendar=CAL)
        assert dup["status"] == "complete" and dup["scored_price"] == 110.0

    def test_context_only_row_never_acquires_hit_rate(self):
        """盘前初筛淘汰为 context_only 的行 (无 LLM 方向) 不得获得模型命中率。"""
        c = cand(direction=None, cid="UE:b1:US.AAA:UNDECIDED", llm_evaluated=False,
                 direction_hint="LONG")
        got = candidate_score(scan(), c, AFTER, bars(110.0), calendar=CAL)
        assert got["status"] == "complete"
        assert "hit" not in got and "fwd_pct" not in got
        assert got["direction_hint"] == "LONG"
        assert got["raw_fwd_pct"] == pytest.approx(10.0)

    def test_identity_and_provenance_echoed(self):
        got = candidate_score(scan(batch_id="UE:b7"), cand(), AFTER, bars(110.0), CAL)
        assert got["type"] == "score_candidate"
        assert got["batch_id"] == "UE:b7"
        assert got["candidate_id"] == "UE:b1:US.AAA:LONG"
        assert got["scan_date"] == TODAY
        assert got["policy_version"] == "da_ue_v1"
        assert got["self_tier"] == "T1" and got["emergence_streak_days"] == 1
        assert got["scored_at"]

    def test_idempotent_rerun(self):
        """幂等键 = 批次/候选/评分口径 ⇒ 重复补算得到逐字相同结果。"""
        first = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=CAL)
        second = candidate_score(scan(), cand(), AFTER, bars(110.0), calendar=CAL)
        first.pop("scored_at"), second.pop("scored_at")
        assert first == second

    def test_non_finite_close_is_not_a_price(self):
        assert positive(float("nan")) is None and positive(float("inf")) is None
        assert positive(0) is None and positive(-1) is None
        assert positive("3.5") == 3.5
        assert positive(None) is None and positive("x") is None

    def test_bool_is_not_a_price(self):
        """True 当 1.0 会让"缺价补算"用 1 美元算前瞻收益 (与 positive_number 同口径)。"""
        assert positive(True) is None and positive(False) is None
        got = candidate_score(scan(), cand(price=True), AFTER, bars(110.0), calendar=CAL)
        assert got["status"] == "pending" and got["reason"] == "missing_or_ambiguous_price"


# ══ cmd_inject / cmd_scan 接线契约 (源级反分叉钉) ════════════════════════
class TestInjectWiring:
    def test_inject_consumes_only_latest_premarket_batch(self):
        src = inspect.getsource(ue.cmd_inject)
        assert "latest_premarket(" in src
        assert "injection_lock(" in src
        assert "require_auto_sim(" in src
        assert "_cmd_inject_impl(" in src

    def test_inject_quota_and_write_confirmation_wired(self):
        src = inspect.getsource(ue._cmd_inject_impl)
        assert "effective_ue_symbols(" in src        # 额度按唯一有效入围累计
        assert "select_candidates(" in src           # 先过滤后预算
        assert "confirm_saved(" in src               # 写后确认, ID 不当成功凭证
        assert "record_candidate_batch(" in src      # 全漏斗审计
        assert "halted_after_unknown_write" in src   # 结果不明 → 停止补位

    def test_scan_writes_batch_on_empty_and_failure(self):
        """空扫描必须落批次; 失败批次与"无机会"必须可区分。"""
        src_scan = inspect.getsource(ue.cmd_scan)
        assert '"type": "batch_start"' in src_scan
        assert '"batch_status": "failed"' in src_scan
        assert '"batch_status": "complete_no_scan"' in src_scan
        src_impl = inspect.getsource(ue._cmd_scan_impl)
        assert "empty_batch" in src_impl or "surfaced 空" in src_impl
        assert '"batch_status": "complete"' in src_impl

    def test_scan_window_label_single_source(self):
        """记录里的 scan_window 与包装器选路径必须同源 (否则窗口隔离形同虚设)。"""
        window_expr = 'window = "midday" if midday else ("da_feed" if da_feed else "premarket")'
        impl_expr = '_window = "midday" if midday else ("da_feed" if da_feed else "premarket")'
        assert window_expr in inspect.getsource(ue.cmd_scan)
        assert impl_expr in inspect.getsource(ue._cmd_scan_impl)

    def test_scan_et_window_guards_survive_dst_dual_cron(self):
        """双季双挂后, off-season 那一发必须在写批次前被 ET 窗口拒绝。"""
        src = inspect.getsource(ue.cmd_scan)
        assert "730 <= _hm <= 805" in src
        assert "900 <= _hm <= 929" in src
        assert "force_window" in src
        assert "--force-window" in inspect.getsource(ue.main)

    def test_score_and_report_slice_by_window(self):
        """da_feed 早跑不入评分/报表 (否则同一交易日前瞻收益被计两次)。"""
        for fn in (ue.cmd_score, ue.cmd_report):
            assert 'scan_window", "premarket") == "premarket"' in inspect.getsource(fn)

    def test_inject_lock_path_is_registered_test_seam(self):
        assert "production_data_sandbox" in str(ue._INJECT_LOCK_PATH), \
            str(ue._INJECT_LOCK_PATH)

    def test_batch_age_config_key_is_shared_with_da(self):
        """批次时效走同一显式键 candidate_scan_max_age_hours (DA feed 与 UE inject 共用),
        且默认值与活配置一致 —— 键被搬走/改名时本例立红 (不是静默回退默认)。"""
        import app.playbook.candidate_eligibility as ce
        import app.playbook.config as pbc
        assert ce.load_candidate_policy()["max_scan_age_hours"] == pytest.approx(MAX_AGE)
        assert "candidate_scan_max_age_hours" in inspect.getsource(ce.load_candidate_policy)
        assert "candidate_scan_max_age_hours" in inspect.getsource(pbc)
        with open(Path(pbc.__file__).parents[2] / "config" / "unified_config.yaml",
                  encoding="utf-8") as fh:
            assert "candidate_scan_max_age_hours:" in fh.read()
