"""Playbook 盘中证据·板块脉搏新鲜度契约测试 (2026-09-14, 用户裁「全部处理」P1-2).

被测对象: `app/playbook/pipeline.py::_gather_intraday_evidence` (topup 落库时并排记录
盘中方向证据, record-only, 供 S1 记分牌按"证据一致度"切片)。

病根 (盘前数据有效性审计 2026-09-14):
  该函数读 `data/sector_pulse.json` 的 `changes` 记 `sector_pulse_chg`, 并只记
  `asof_et` (如 "15:30") —— **不记日期, 且无任何新鲜度检查**。而该文件由
  `scripts/sector_pulse.py` 在盘中每 30min 覆写 (cron: ET 10:00-15:30) ⇒
  ① topup **盘前窗 (ET 08:45-09:30)** 读到的必然是上一交易日收盘读数 (周一跨周末
     = 3 天前), 却被记成"盘中脉搏 asof 15:30";
  ② 脉搏 cron 若挂掉, `sector_pulse.py` 自身的"成功<6 块保留旧文件"逻辑会让旧文件
     长期留存 → 可以是任意久的读数。
  后果: 陈旧读数会被当成"当日盘中读数"参与 S1 证据一致度切片 (胜出者才升确认/否决门),
  污染候选证据的归因。

本测试钉住的不变量:
  A. 非当日 ET 的脉搏 → `sector_pulse_chg` 如实 null, 并记 `pulse_stale_reason`
     (不冒充日内读数, 也不静默);
  B. 当日脉搏 → 正常记录, 且补记 `pulse_date` / `pulse_age_min` (按陈旧度可切片);
  C. 陈旧脉搏**不参与** `agree`/`avail` 计数 (只按 vwap/订单流计), 即不污染一致性;
  D. record-only 语义不变: 任何异常仍返 None, 不影响指令落库 (主链不被本函数拖死)。

实现注: 生产路径读 `data/sector_pulse.json`, 而 tests/conftest.py 硬闸禁止测试写
生产 data/ ⇒ 本次把数据根参数化为 `data_root` (默认仍为生产 data/), 测试传 tmp_path
(conftest 提示的修法 (a): "把写入路径参数化, 测试传 tmp_path")。
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

from app.playbook import pipeline as PL


class _Router:
    """最小 data_router 桩: 只提供 _gather_intraday_evidence 用到的四个字段。"""

    def __init__(self, price=100.0, vwap=98.0, ofi=0.3):
        self._m = {"price": price, "vwap": vwap, "order_flow_imbalance": ofi,
                   "ticker_buy_sell_ratio": 1.2, "volume_ratio": 0.9}

    def get_full_market_context(self, symbol):
        return dict(self._m)


def _write_data(root, pulse_date, asof="15:30", sector="Semis"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "deep_sector_rotation.json").write_text(json.dumps(
        {"symbol_sectors": {"US.NVDA": sector}}), encoding="utf-8")
    (root / "sector_pulse.json").write_text(json.dumps({
        "date": pulse_date, "asof_et": asof,
        "changes": {sector: 1.5},
        "ranked": [f"{sector}(+1.5%)"],
    }), encoding="utf-8")


@pytest.fixture
def _freeze_now_et(monkeypatch):
    """冻结"当前 ET 时刻"到 2026-09-14 11:00, 使 pulse_age_min 可确定断言。

    注: 被测代码走 `datetime.now(_now_et())` —— `_now_et()` 只返回 **tzinfo**,
    单 patch 它冻不住时钟 (取到的仍是真实当前时刻); 故同时把模块级 `datetime`
    换成带冻结 `now()` 的替身。这是测试侧替身, 不改生产语义。
    """
    et = timezone(timedelta(hours=-4))
    frozen = datetime(2026, 9, 14, 11, 0, tzinfo=et)

    class _FrozenDT:
        @staticmethod
        def now(tz=None):
            return frozen.astimezone(tz) if tz is not None else frozen

    monkeypatch.setattr(PL, "_now_et", lambda: et)
    monkeypatch.setattr(PL, "_today_et_str", lambda: "2026-09-14")
    monkeypatch.setattr(PL, "datetime", _FrozenDT)
    return frozen


class TestPulseFreshness:
    def test_stale_pulse_is_nulled_with_reason(self, tmp_path):
        """A: 上一交易日 (盘前窗必然如此) → 不冒充日内读数, 且写明原因。"""
        _write_data(tmp_path, "2026-09-11")
        ev = PL._gather_intraday_evidence(_Router(), "US.NVDA", "BUY", data_root=tmp_path)
        assert ev["sector_pulse_chg"] is None, "非当日脉搏不得当成日内涨跌"
        assert ev["pulse_asof_et"] is None
        assert ev["pulse_date"] is None
        assert ev["pulse_stale_reason"], "陈旧必须留痕, 不许静默"
        assert "2026-09-11" in ev["pulse_stale_reason"]

    def test_stale_pulse_excluded_from_agreement_count(self, tmp_path):
        """C: 陈旧脉搏不进 agree/avail ⇒ 一致性切片不被污染。"""
        _write_data(tmp_path, "2026-09-11")
        ev = PL._gather_intraday_evidence(_Router(), "US.NVDA", "BUY", data_root=tmp_path)
        # vwap: 100>98 → 同向; ofi 0.3 → 同向; 脉搏缺席 → avail 只能是 2
        assert ev["avail"] == 2
        assert ev["agree"] == 2

    def test_today_pulse_recorded_with_age(self, tmp_path, _freeze_now_et):
        """B: 当日脉搏正常记录, 并补 pulse_date / pulse_age_min (11:00 - 10:30 = 30min)。"""
        _write_data(tmp_path, "2026-09-14", asof="10:30")
        ev = PL._gather_intraday_evidence(_Router(), "US.NVDA", "BUY", data_root=tmp_path)
        assert ev["sector_pulse_chg"] == 1.5
        assert ev["pulse_date"] == "2026-09-14"
        assert ev["pulse_asof_et"] == "10:30"
        assert ev["pulse_age_min"] == 30
        assert ev["pulse_stale_reason"] is None
        assert ev["avail"] == 3 and ev["agree"] == 3

    def test_future_asof_records_null_age(self, tmp_path, _freeze_now_et):
        """B 的边界: asof 晚于当前时刻 (时钟偏差) → age 如实 null, 不记负数。"""
        _write_data(tmp_path, "2026-09-14", asof="12:30")
        ev = PL._gather_intraday_evidence(_Router(), "US.NVDA", "BUY", data_root=tmp_path)
        assert ev["pulse_age_min"] is None
        assert ev["sector_pulse_chg"] == 1.5, "日期正确就该记录, 不因 age 不可算而丢弃"

    def test_sector_mapping_miss_still_null(self, tmp_path, _freeze_now_et):
        """既有语义不变: symbol→sector 映射不上时 chg 仍如实 null (不猜)。"""
        _write_data(tmp_path, "2026-09-14", asof="10:30", sector="Semis")
        ev = PL._gather_intraday_evidence(_Router(), "US.UNKNOWN_SYM", "BUY",
                                          data_root=tmp_path)
        assert ev["sector"] is None
        assert ev["sector_pulse_chg"] is None

    def test_missing_files_do_not_raise(self, tmp_path):
        """D: record-only — 数据文件缺失时返回结构完整的 evidence, 不抛异常。"""
        ev = PL._gather_intraday_evidence(_Router(), "US.NVDA", "BUY",
                                          data_root=tmp_path / "empty")
        assert ev["sector"] is None and ev["pulse_stale_reason"] is None
        assert ev["avail"] == 2

    def test_router_failure_returns_none(self):
        """D: 上游取数失败仍返 None (调用方按"无证据"处理, 不阻断落库)。"""
        assert PL._gather_intraday_evidence(None, "US.NVDA", "BUY") is None