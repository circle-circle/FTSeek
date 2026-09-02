"""[P1 2026-09-02] UE inject 低ATR源头预检测试 (用户裁"同意").

背景: 执行门 (pipeline low_atr_gate) 已判定低波票永不可能触发, UE 注入即"结构性
死指令" (9/2 实测 XLY/XLI/XLU/AAPL/LLYVK 5 条全天 12 轮 low_atr_block; 7/24 以来
5,240 条拦截记录; 全历史 ETF 仅 1 笔成交)。预检在 inject 阶段源头拦截, 与执行门
同键同阈值同 provider (零交易行为变更, 只省死指令开销)。

锁住四件事:
  1. 低ATR候选 → SKIP 行落库对象 (skip_reason 自带数值) + 循环拒绝归因;
  2. 高ATR候选照常生成正常指令 (预检不误杀);
  3. 开关关 → provider 不构建、预检整体旁路;
  4. ATR 缺失/provider 异常 → fail-open 放行 (执行门兜底);
  5. 源级契约: 读执行门同键 playbook.low_atr_gate_* 且回退默认 0.025 对齐。
"""
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import llm_universe_scan as ue  # noqa: E402

_SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "llm_universe_scan.py")
_PIPE = os.path.join(os.path.dirname(__file__), "..", "app", "playbook", "pipeline.py")


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _has_dir_line(out: str, direction: str, symbol: str) -> bool:
    """指令打印行格式: '{direction:<10} {symbol}' (direction 列宽 10)."""
    return any(
        ln.strip().startswith(direction) and symbol in ln
        for ln in out.splitlines()
    )


def _cand(sym: str, direction: str = "LONG", conf: float = 0.8):
    return {
        "symbol": sym,
        "direction": direction,
        "confidence": conf,
        "scan_price": 100.0,
        "thesis": "test thesis",
        "invalidation": "",
        "sector": "ETF",
        "emergence_streak_days": 1,
    }


def _run_inject(tmp_cfg=None, atr_by_symbol=None, provider_error=False):
    """驱动 cmd_inject (dry-run, 不写库), 返回 (stdout, mocks)."""
    cfg = {
        "universe_expansion.entry_enabled": False,  # dry-run
        "universe_expansion.min_confidence": 0.5,
        "universe_expansion.max_targets_per_day": 30,
        "universe_expansion.max_emergence_streak": 99,
        "universe_expansion.min_price_floor": 5,
        "universe_expansion.premarket_anchor": True,
        "playbook.low_atr_gate_enabled": True,
        "playbook.low_atr_gate_min_atr_pct": 0.03,
    }
    if tmp_cfg:
        cfg.update(tmp_cfg)
    candidates = [_cand("XLY"), _cand("TEST")]

    fake_provider = mock.MagicMock()
    if provider_error:
        fake_provider.get_atr_pct.side_effect = RuntimeError("boom")
    else:
        fake_provider.get_atr_pct.side_effect = (
            lambda sym, px: (atr_by_symbol or {}).get(sym)
        )
    fake_container = mock.MagicMock()
    fake_container.atr_provider.return_value = fake_provider
    get_di = mock.MagicMock(return_value=fake_container)

    regime = SimpleNamespace(
        regime=SimpleNamespace(value="neutral"), vix=15.0,
        spy_ma20=1.0, spy_ma50=1.0,
    )
    fake_mrg = mock.MagicMock()
    fake_mrg.return_value.assess.return_value = regime

    scan_rec = {
        "type": "scan", "date": ue._today(),
        "surfaced": candidates, "model_used": "test-model",
    }
    quotes = {"XLY": {"price": 250.0}, "TEST": {"price": 100.0}}

    mocks = {}
    loop_reject_mock = mock.MagicMock()
    patches = [
        mock.patch.object(ue, "_cfg", side_effect=lambda k, d=None: cfg.get(k, d)),
        mock.patch.object(ue, "_is_us_trading_day_today", return_value=True),
        mock.patch.object(ue, "_is_us_premarket", return_value=False),
        mock.patch.object(ue, "_read_jsonl", return_value=[scan_rec]),
        mock.patch.object(ue, "_position_symbols", return_value=set()),
        mock.patch.object(ue, "_build_router", return_value=mock.MagicMock()),
        mock.patch.object(ue, "_batch_quotes", return_value=quotes),
        mock.patch.object(ue, "_log_inject_reject"),
        mock.patch.object(ue, "_log_direction_structure"),
        mock.patch.object(ue, "_log_streak_counterfactual"),
        mock.patch.object(ue, "_log_inject_loop_reject", loop_reject_mock),
        mock.patch("app.playbook.db.PlaybookDB"),
        mock.patch("app.playbook.market_regime.MarketRegimeGate", fake_mrg),
        mock.patch("app.core.di_container.get_di_container", get_di),
    ]
    from io import StringIO
    out = StringIO()
    with mock.patch("sys.stdout", out):
        for p in patches:
            p.start()
        try:
            rc = ue.cmd_inject()
        finally:
            for p in patches:
                p.stop()
            mocks["get_di"] = get_di
            mocks["loop_reject"] = loop_reject_mock
            mocks["provider"] = fake_provider
    return out.getvalue(), rc, mocks


class TestLowAtrPrecheck:
    def test_low_atr_blocked_to_skip_row(self, capsys=None):
        """XLY atr=1.31% < 3% → SKIP 行 (skip_reason 带数值) + 拒绝归因."""
        out, rc, mocks = _run_inject(atr_by_symbol={"US.XLY": 0.0131, "US.TEST": 0.06})
        assert rc == 0
        assert "低ATR预检 (0.0131 < 0.03) → SKIP行" in out
        assert "low_atr(0.0131<0.03" in out
        # XLY 不应再走正常指令打印 (BUY 行无 XLY)
        assert not _has_dir_line(out, "BUY", "US.XLY")
        # 循环拒绝归因 jsonl 记录
        reasons = [str(c.args[1]) for c in mocks["loop_reject"].call_args_list]
        assert any("low_atr(0.0131<0.03" in r for r in reasons)

    def test_high_atr_candidate_proceeds_normally(self):
        """高ATR候选 (TEST atr=6%) 照常生成正常指令 — 预检不误杀."""
        out, rc, mocks = _run_inject(atr_by_symbol={"US.XLY": 0.0131, "US.TEST": 0.06})
        assert _has_dir_line(out, "BUY", "US.TEST")
        assert "zone=[" in out

    def test_gate_disabled_bypasses_provider(self):
        """开关关 → get_di_container 不被调用, 低ATR标的照常生成."""
        out, rc, mocks = _run_inject(
            tmp_cfg={"playbook.low_atr_gate_enabled": False},
            atr_by_symbol={"US.XLY": 0.0131, "US.TEST": 0.06},
        )
        assert mocks["get_di"].call_count == 0
        assert "低ATR预检" not in out
        assert _has_dir_line(out, "BUY", "US.XLY")  # 预检旁路, 交执行门兜底

    def test_atr_missing_fail_open(self):
        """ATR 取不到 (None) → fail-open 放行."""
        out, rc, mocks = _run_inject(atr_by_symbol={"US.XLY": None, "US.TEST": None})
        assert _has_dir_line(out, "BUY", "US.XLY")
        assert _has_dir_line(out, "BUY", "US.TEST")

    def test_atr_provider_error_fail_open(self):
        """provider 抛异常 → warn + fail-open 放行 (不炸整个 inject)."""
        out, rc, mocks = _run_inject(provider_error=True)
        assert "低ATR预检异常 fail-open" in out
        assert _has_dir_line(out, "BUY", "US.XLY")
        assert _has_dir_line(out, "BUY", "US.TEST")


class TestSourceContract:
    """源级契约: 与执行门同键同阈值同纪律 (防未来键名漂移导致双门失联)."""

    def test_reads_exec_gate_keys(self):
        s = _src(_SRC)
        assert '"playbook.low_atr_gate_enabled"' in s
        assert '"playbook.low_atr_gate_min_atr_pct"' in s

    def test_default_threshold_matches_pipeline(self):
        s = _src(_SRC)
        assert '"playbook.low_atr_gate_min_atr_pct", 0.025' in s
        # pipeline.py 执行门回退默认同为 0.025 (活性值由 yaml SSOT 0.03 覆盖)
        p = _src(_PIPE)
        assert 'low_atr_gate_min_atr_pct", 0.025' in p

    def test_uses_di_container_atr_provider(self):
        """provider 与盘前定桶任务同源 (get_di_container().atr_provider())."""
        s = _src(_SRC)
        assert "get_di_container().atr_provider()" in s

    def test_fail_open_discipline(self):
        """ATR 异常必须 fail-open (与执行门 'atr 解析异常 → fail-open' 同纪律)."""
        s = _src(_SRC)
        assert "低ATR预检异常 fail-open" in s
