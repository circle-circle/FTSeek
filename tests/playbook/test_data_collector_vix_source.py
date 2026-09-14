"""Playbook 数据采集侧 VIX 取数口径契约测试 (2026-09-14, 用户裁「全部处理」P2).

被测对象: `app/playbook/data_collector.py` 的 `_get_vix_cached()` / `SentimentData`.

病根 (盘前数据有效性审计 2026-09-14):
  ① `_get_vix_cached()` 走 `AlphaVantageProvider.get_vix()` —— 该函数失败时
     `return 20.0` 兜底假值 (见其 docstring "向后兼容: 失败时返回默认值 20.0");
  ② 调用方用 `vix_quality = "ok" if vix != 20.0 else "missing"` —— 拿**值等于哨兵**
     来猜"缺失": 真值恰为 20.0 时反被判缺失, 而 20.0 又正好卡在 overlay 档位边界
     (>=20 → "medium", 触发止损加宽);
  ③ 盘前/盘后 AV 走 FRED 日频 (T-1/T-2), 与执行门/regime 侧的 VIXY 实时代理**不同
     口径** —— 而盘前恰恰是 Playbook 生成链的运行时段。

本测试钉住的不变量:
  A. `SentimentData.vix` 默认值不再是数值哨兵 20.0 (缺失即 None);
  B. `_get_vix_cached()` 与**执行门同源** (market_state 检查器), 取不到如实 None,
     绝不回落到 20.0;
  C. None 结果**也进 5min 缓存** (否则逐标的重复取数 = OpenD 连接风暴);
  D. `vix_quality` 按 `is not None` 判定 —— 真值 20.0 必须算 "ok" (回归闸)。

消费方语义 (未变): `instruction_generator` 的 market_state_overlay 用
`float(getattr(data.sentiment, "vix", 20.0) or 20.0)` 显式兜底 (fail-open),
故 None 在本改动前后都是 "medium" 档 —— 本改动只让"缺失"变得可区分, 不改档位行为。
"""
from unittest.mock import MagicMock

import pytest

from app.playbook import data_collector as DC


def _collector(**kv):
    """只装配单测所需字段 (绕开 provider 装配)。"""
    c = DC.PlaybookDataCollector.__new__(DC.PlaybookDataCollector)
    c._vix_cache = {"value": None, "ts": 0}
    for k, v in kv.items():
        setattr(c, k, v)
    return c


class TestNoNumericSentinel:
    def test_sentiment_vix_default_is_none(self):
        """A: 默认值不是哨兵数值 —— "缺失"与"真值 20.0"必须可区分。"""
        assert DC.SentimentData().vix is None
        assert DC.PlaybookData(symbol="US.NVDA").sentiment.vix is None


class TestUnifiedSource:
    def test_reads_from_market_state_checker(self, monkeypatch):
        """B: 走执行门同源的检查器, 而不是 AV 的 get_vix() 兜底口径。"""
        calls = []

        class _Checker:
            def get_vix_cached(self):
                calls.append(1)
                return 27.5

        monkeypatch.setattr("app.analysis.market_state.get_checker", lambda: _Checker())
        assert _collector()._get_vix_cached() == 27.5
        assert calls == [1]

    def test_missing_data_returns_none_not_20(self, monkeypatch):
        """B: 上游取不到 → None; 严禁回落 20.0 (否则 fail-close 又纸面化)。"""
        class _Checker:
            def get_vix_cached(self):
                return None

        monkeypatch.setattr("app.analysis.market_state.get_checker", lambda: _Checker())
        assert _collector()._get_vix_cached() is None

    def test_checker_exception_returns_none(self, monkeypatch):
        """B: 检查器抛异常也必须 None, 不得回落 20.0。"""
        def _boom():
            raise RuntimeError("opend down")

        monkeypatch.setattr("app.analysis.market_state.get_checker", _boom)
        assert _collector()._get_vix_cached() is None

    def test_none_result_is_cached(self, monkeypatch):
        """C: None 也缓存 5min —— 否则每只标的重连一次 OpenD (连接风暴)。"""
        n = []

        class _Checker:
            def get_vix_cached(self):
                n.append(1)
                return None

        monkeypatch.setattr("app.analysis.market_state.get_checker", lambda: _Checker())
        c = _collector()
        c._get_vix_cached()
        c._get_vix_cached()
        c._get_vix_cached()
        assert len(n) == 1, "None 结果必须进缓存, 不能逐标的重复取数"

    def test_real_value_is_cached(self, monkeypatch):
        """C: 正常值同样 5min 内只取一次。"""
        n = []

        class _Checker:
            def get_vix_cached(self):
                n.append(1)
                return 17.84

        monkeypatch.setattr("app.analysis.market_state.get_checker", lambda: _Checker())
        c = _collector()
        assert c._get_vix_cached() == c._get_vix_cached() == 17.84
        assert len(n) == 1


class TestVixQualityFlag:
    def _quality(self, monkeypatch, vix):
        c = _collector(finnhub=MagicMock(), futu_ext=None)
        c.finnhub.get_sentiment_score = MagicMock(side_effect=RuntimeError("no news"))
        monkeypatch.setattr(DC.PlaybookDataCollector, "_get_vix_cached", lambda self: vix)
        return c._collect_sentiment("US.NVDA")

    def test_true_20_is_ok_not_missing(self, monkeypatch):
        """D 回归闸: 真值恰为 20.0 必须算 "ok" —— 旧口径 (vix != 20.0) 会误判缺失。"""
        d = self._quality(monkeypatch, 20.0)
        assert d.vix == 20.0
        assert d.quality == "partial", "vix 有效即不该是 missing"

    def test_none_is_missing(self, monkeypatch):
        """D: 取不到 VIX → 降级为 missing (而不是让 20.0 冒充有效值)。"""
        d = self._quality(monkeypatch, None)
        assert d.vix is None
        assert d.quality == "missing"

    def test_high_vix_recorded(self, monkeypatch):
        d = self._quality(monkeypatch, 31.2)
        assert d.vix == 31.2 and d.quality == "partial"