"""富途批量快照 'N/A' 容错 + 逐行隔离契约 (2026-11-18).

病灶 (2026-11-18 21:00 CST 告警实锤, logs/rsshub_us.log:111/130/179):
权证 US.TACOW($0.32)/US.FACTW($0.15) 的 snapshot 行含字符串 'N/A' ⇒
`float('N/A')` ValueError 被 `get_batch_quote` 的**整批** except 捕获, 而
`results.update(snapshot_results)` 在 row 循环之后 ⇒ 同批已解析的主板票一起
被丢弃, 全部退化到逐只 get_quote。这正是 2026-08-25 (DEB-ue-audit r5) 记录过的
「单票毒死整批 → 主板票失去 LV3 pre_price → 盘前锚定门全判无现价 → 0 条注入」
机制, 只是那次修的是 ret=-1 分支, 没修行解析异常分支。

两层修复各自被钉住:
  1. `_safe_float`  — 'N/A'/None/''/NaN/inf 一律落 default, 不再抛;
  2. 逐行 try/except — 万一某字段抛出 TypeError/ValueError 之外的异常, 也只跳该行。

健康行输出必须逐值不变 (含 open_price=0 保持 0.0 而非回落 last_price), 见
test_healthy_row_output_is_unchanged —— 这条是防止容错改写正常口径的钉子。
"""
import logging

import pandas as pd
import pytest

from app.data import futu_lv3_provider as mod
from app.data.futu_lv3_provider import FutuLV3Provider, _safe_float


class _ExplodingFloat:
    """`float()` 抛 TypeError/ValueError **之外**的异常 ⇒ 只能靠逐行隔离兜住。"""

    def __float__(self):
        raise RuntimeError("boom")


@pytest.fixture
def log_records():
    """直接挂在模块 logger 上收 record, 不依赖 propagate 配置。"""
    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.DEBUG)
    mod.logger.addHandler(handler)
    old_level = mod.logger.level
    mod.logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        mod.logger.removeHandler(handler)
        mod.logger.setLevel(old_level)


def _mk_provider(rows, snapshot_exc=None, ret=0):
    """构造一个不连 OpenD 的 provider; snapshot 返回给定 rows 或抛异常。"""
    p = FutuLV3Provider.__new__(FutuLV3Provider)
    p._success_count = 0
    p._fail_count = 0
    p._last_error = ""
    p.get_push_quote = lambda symbol: None
    p._ensure_connected = lambda: True

    degraded = []

    def _fake_get_quote(symbol):
        degraded.append(symbol)
        return None

    p.get_quote = _fake_get_quote

    class _Ctx:
        def get_market_snapshot(self, symbols):
            if snapshot_exc is not None:
                raise snapshot_exc
            return ret, pd.DataFrame(rows)

    p._quote_ctx = _Ctx()
    return p, degraded


_CLEAN_AAPL = {
    "code": "US.AAPL",
    "last_price": 351.91,
    "open_price": 348.0,
    "high_price": 353.0,
    "low_price": 347.5,
    "prev_close_price": 349.0,
    "volume": 1234567,
    "turnover": 4.3e8,
    "pre_price": 352.5,
    "pre_change_rate": 0.17,
    "update_time": "2026-11-18 08:00:01",
}

# 21:00:28 现场形态: 权证行 last_price 正常, 盘前字段是 'N/A' 字符串
_DIRTY_TACOW = {
    "code": "US.TACOW",
    "last_price": 0.32,
    "open_price": "N/A",
    "high_price": "N/A",
    "low_price": "N/A",
    "prev_close_price": 0.3201,
    "volume": "N/A",
    "turnover": "N/A",
    "pre_price": "N/A",
    "pre_change_rate": "N/A",
    "update_time": "2026-11-18 08:00:02",
}


class TestSafeFloat:
    @pytest.mark.parametrize("value", ["N/A", "n/a", "NA", "", None, "abc", "N/A "])
    def test_non_numeric_falls_to_default(self, value):
        assert _safe_float(value, 0.0) == 0.0
        assert _safe_float(value, 7.5) == 7.5

    def test_nan_and_inf_are_not_finite(self):
        assert _safe_float(float("nan"), -1.0) == -1.0
        assert _safe_float(float("inf"), -1.0) == -1.0
        assert _safe_float(float("-inf"), -1.0) == -1.0

    def test_numeric_values_pass_through(self):
        assert _safe_float(3.5) == 3.5
        assert _safe_float("3.5") == 3.5
        assert _safe_float(0) == 0.0
        assert _safe_float(-1.25) == -1.25

    def test_or_zero_idiom_does_not_protect_against_na(self):
        """原实现 `float(row.get('pre_price', 0) or 0)` 挡不住 'N/A' 的根因。"""
        assert bool("N/A") is True  # 'N/A' 是 truthy ⇒ `or 0` 不生效
        with pytest.raises(ValueError):
            float("N/A" or 0)
        assert _safe_float("N/A" or 0) == 0.0


class TestDirtyRowIsolation:
    def test_na_warrant_row_does_not_poison_clean_rows(self, log_records):
        """现场复现: TACOW 脏行在批内, AAPL/MSFT 必须活着, 且不退化逐只取价。"""
        clean_msft = dict(_CLEAN_AAPL, code="US.MSFT", last_price=510.0)
        p, degraded = _mk_provider([_CLEAN_AAPL, _DIRTY_TACOW, clean_msft])

        out = p.get_batch_quote(["US.AAPL", "US.TACOW", "US.MSFT"])

        assert "US.AAPL" in out and "US.MSFT" in out, "脏行毒死了同批主板票"
        assert out["US.AAPL"]["price"] == 351.91
        assert out["US.MSFT"]["price"] == 510.0
        assert out["US.AAPL"]["pre_price"] == 352.5
        # 权证行本身也活下来: 价是真的, 缺字段落 0.0 (与"字段缺失"同语义)
        assert out["US.TACOW"]["price"] == 0.32
        assert out["US.TACOW"]["pre_price"] == 0.0
        assert out["US.TACOW"]["volume"] == 0.0
        # 批成功了 ⇒ 不得走逐只降级 (那才是 8/25 事故的实损路径)
        assert degraded == []
        assert p._fail_count == 0 and p._success_count == 1
        errors = [r for r in log_records if r.levelno >= logging.ERROR]
        assert errors == [], f"整批 ERROR 不该再出现: {[r.getMessage() for r in errors]}"

    def test_dirty_row_keeps_already_parsed_rows_when_float_raises_other_error(
        self, log_records
    ):
        """第二层隔离: 抛 RuntimeError 的字段也只杀自己那一行。"""
        rows = [
            _CLEAN_AAPL,
            dict(_DIRTY_TACOW, code="US.FACTW", last_price=_ExplodingFloat()),
        ]
        p, degraded = _mk_provider(rows)

        out = p.get_batch_quote(["US.AAPL", "US.TACOW", "US.FACTW"])

        assert "US.AAPL" in out, "逐行隔离失效: 一行异常丢弃了整批"
        assert out["US.AAPL"]["price"] == 351.91
        assert "US.FACTW" not in out
        assert degraded == []
        assert p._fail_count == 0, "行级失败不该记成批级失败"
        warns = [r.getMessage() for r in log_records if r.levelno == logging.WARNING]
        assert any("US.FACTW" in m and "只跳过" in m for m in warns), warns

    def test_last_price_na_skips_only_that_row(self):
        rows = [dict(_DIRTY_TACOW, last_price="N/A"), _CLEAN_AAPL]
        p, degraded = _mk_provider(rows)

        out = p.get_batch_quote(["US.TACOW", "US.AAPL"])

        assert "US.TACOW" not in out, "last_price='N/A' 必须当作无价跳过"
        assert out["US.AAPL"]["price"] == 351.91
        assert degraded == []

    def test_last_price_nan_skips_row(self):
        rows = [dict(_DIRTY_TACOW, last_price=float("nan")), _CLEAN_AAPL]
        p, _ = _mk_provider(rows)

        out = p.get_batch_quote(["US.TACOW", "US.AAPL"])

        assert "US.TACOW" not in out
        assert "US.AAPL" in out

    @pytest.mark.parametrize(
        "field,default_expected",
        [
            ("open_price", 351.91),
            ("high_price", 351.91),
            ("low_price", 351.91),
            ("prev_close_price", 351.91),
            ("volume", 0.0),
            ("turnover", 0.0),
            ("pre_price", 0.0),
            ("pre_change_rate", 0.0),
        ],
    )
    def test_each_field_tolerates_na_independently(self, field, default_expected):
        row = dict(_CLEAN_AAPL, **{field: "N/A"})
        p, _ = _mk_provider([row])

        out = p.get_batch_quote(["US.AAPL"])

        assert "US.AAPL" in out
        key = {
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "prev_close_price": "prev_close",
            "pre_change_rate": "pre_change_rate",
        }.get(field, field)
        assert out["US.AAPL"][key] == default_expected
        assert out["US.AAPL"]["price"] == 351.91


class TestHealthyRowUnchanged:
    def test_healthy_row_output_is_unchanged(self):
        """容错不得改写健康行口径: 14 个键逐值钉死。"""
        p, _ = _mk_provider([_CLEAN_AAPL])

        out = p.get_batch_quote(["US.AAPL"])["US.AAPL"]

        expected_keys = {
            "price", "open", "high", "low", "prev_close", "volume", "turnover",
            "change_percent", "pre_price", "pre_change_rate", "data_source",
            "update_time", "timestamp",
        }
        assert set(out) == expected_keys
        assert out["price"] == 351.91
        assert out["open"] == 348.0
        assert out["high"] == 353.0
        assert out["low"] == 347.5
        assert out["prev_close"] == 349.0
        assert out["volume"] == 1234567
        assert out["turnover"] == 4.3e8
        assert out["change_percent"] == pytest.approx((351.91 - 349.0) / 349.0 * 100)
        assert out["pre_price"] == 352.5
        assert out["pre_change_rate"] == 0.17
        assert out["data_source"] == "futu_lv3"
        assert out["update_time"] == "2026-11-18 08:00:01"
        assert isinstance(out["timestamp"], float)

    def test_zero_valued_ohlc_is_kept_as_zero_not_replaced_by_last_price(self):
        """真 0 与缺字段是两件事: open_price=0 必须保持 0.0。"""
        row = dict(_CLEAN_AAPL, open_price=0, high_price=0, low_price=0, volume=0)
        p, _ = _mk_provider([row])

        out = p.get_batch_quote(["US.AAPL"])["US.AAPL"]

        assert out["open"] == 0.0
        assert out["high"] == 0.0
        assert out["low"] == 0.0
        assert out["volume"] == 0.0

    def test_missing_optional_fields_default_like_before(self):
        """键缺失时沿用原默认: OHLC/prev_close → last_price, 其余 → 0。"""
        row = {"code": "US.AAPL", "last_price": 100.0, "update_time": ""}
        p, _ = _mk_provider([row])

        out = p.get_batch_quote(["US.AAPL"])["US.AAPL"]

        assert out["open"] == out["high"] == out["low"] == 100.0
        assert out["prev_close"] == 100.0
        assert out["change_percent"] == 0.0
        assert out["volume"] == 0.0 and out["turnover"] == 0.0
        assert out["pre_price"] == 0.0 and out["pre_change_rate"] == 0.0

    def test_prev_close_zero_keeps_change_percent_zero(self):
        row = dict(_CLEAN_AAPL, prev_close_price=0)
        p, _ = _mk_provider([row])

        out = p.get_batch_quote(["US.AAPL"])["US.AAPL"]

        assert out["prev_close"] == 0.0
        assert out["change_percent"] == 0.0


class TestBatchLevelPathsUnchanged:
    def test_ret_minus_one_still_warns_and_degrades_per_symbol(self, log_records):
        """8/25 修复的 ret=-1 分支不得被本批改动破坏。"""
        p, degraded = _mk_provider([_CLEAN_AAPL], ret=-1)

        out = p.get_batch_quote(["US.AAPL", "US.MSFT"])

        assert out == {}
        assert degraded == ["US.AAPL", "US.MSFT"]
        warns = [r.getMessage() for r in log_records if r.levelno == logging.WARNING]
        assert any("批量快照失败" in m and "逐只降级" in m for m in warns), warns

    def test_snapshot_exception_still_logs_batch_error(self, log_records):
        p, degraded = _mk_provider([], snapshot_exc=ConnectionError("OpenD gone"))

        p.get_batch_quote(["US.AAPL"])

        assert degraded == ["US.AAPL"]
        assert p._fail_count == 1 and p._last_error == "OpenD gone"
        errors = [r.getMessage() for r in log_records if r.levelno >= logging.ERROR]
        assert any("批量快照异常" in m for m in errors), errors

    def test_all_push_cached_skips_snapshot_entirely(self):
        p, degraded = _mk_provider([_CLEAN_AAPL])
        p.get_push_quote = lambda symbol: {
            "price": 1.0, "data_source": "futu_lv3_push", "timestamp": 0.0
        }

        out = p.get_batch_quote(["US.AAPL"])

        assert out["US.AAPL"]["data_source"] == "futu_lv3_push"
        assert degraded == [] and p._success_count == 0
