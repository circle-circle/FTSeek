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
  5. 源级契约: 与执行门同 accessor get_playbook_config() (app/playbook/config.py,
     pipeline.py:5317-5318 同源), 回退默认 0.025 对齐; 扁平键 playbook.low_atr_gate_*
     禁止回归 (U2 事故根因: get_dotted 严格逐层读不到扁平键 → 预检自 09-02 上线
     从未生效, 而旧版本本测试恰好把错键供了起来 — mock 注入不存在的键使功能测试恒绿,
     源级契约断言把错键冻进 CI);
  6. 反分叉钉 (TestNoForkNail, mock 不可满足): 从活 unified_config.yaml 经执行门
     accessor 与 yaml 真实路径 get_dotted 双路读同一键, 断言取值一致 — 任何人
     重新分叉读取路径或搬键不同步, 立即红。
"""
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import llm_universe_scan as ue  # noqa: E402

_SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "llm_universe_scan.py")
_PIPE = os.path.join(os.path.dirname(__file__), "..", "app", "playbook", "pipeline_parts", "trigger_flow.py")


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


class _FakePbCfg:
    """get_playbook_config() 返回值替身 — 只提供 .get(key, default) 边界."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _run_inject(tmp_cfg=None, atr_by_symbol=None, provider_error=False):
    """驱动 cmd_inject (dry-run, 不写库), 返回 (stdout, mocks)."""
    cfg = {
        "universe_expansion.entry_enabled": False,  # dry-run
        "universe_expansion.min_confidence": 0.5,
        "universe_expansion.max_targets_per_day": 30,
        "universe_expansion.max_emergence_streak": 99,
        "universe_expansion.min_price_floor": 5,
        "universe_expansion.premarket_anchor": True,
    }
    # [U2 根因修 2026-09-11] 低ATR门开走执行门 accessor (get_playbook_config),
    #   不再经 _cfg 扁平键 — mock 打在 accessor 边界, 与生产读取路径同构。
    pb_values = {
        "low_atr_gate_enabled": True,
        "low_atr_gate_min_atr_pct": 0.03,
    }
    if tmp_cfg:
        cfg.update(tmp_cfg)
        for _k in ("low_atr_gate_enabled", "low_atr_gate_min_atr_pct"):
            if _k in tmp_cfg:
                pb_values[_k] = tmp_cfg.pop(_k)
    candidates = [_cand("XLY"), _cand("TEST")]

    # [P1.2 2026-09-12, proud-slope-falcon] 筛选 ATR 换源: get_atr_pct (止损口径,
    #   可能含 VIX 缩放/底限/分钟 ATR) → get_screening_atr (原始已完成日线 TR14
    #   rolling-mean)。atr_by_symbol 语义仍是"比例", 此处按参考价还原绝对 ATR。
    _prices = {"US.XLY": 250.0, "US.TEST": 100.0}

    def _fake_screening_atr(sym, asof=None, **kwargs):
        if provider_error:
            raise RuntimeError("boom")
        ratio = (atr_by_symbol or {}).get(sym)
        if ratio is None:
            return {"status": "UNKNOWN", "reason": "no_completed_daily_bars",
                    "atr": None, "basis": "daily_tr14_rolling_mean", "bars_asof": None}
        return {"status": "PASS", "reason": "ok", "atr": ratio * _prices.get(sym, 100.0),
                "basis": "daily_tr14_rolling_mean", "source": "router.get_daily_bars",
                "adjustment": "split_guarded_source_undeclared",
                "bars_asof": "2026-09-11", "bars_used": 15}

    fake_provider = mock.MagicMock()
    fake_provider.get_screening_atr.side_effect = _fake_screening_atr
    fake_provider.get_atr_pct.side_effect = AssertionError(
        "止损口径 get_atr_pct 不得再被选股初筛调用 (P1.2 口径分离)")
    fake_container = mock.MagicMock()
    fake_container.atr_provider.return_value = fake_provider
    get_di = mock.MagicMock(return_value=fake_container)

    regime = SimpleNamespace(
        regime=SimpleNamespace(value="neutral"), vix=15.0,
        spy_ma20=1.0, spy_ma50=1.0,
    )
    fake_mrg = mock.MagicMock()
    fake_mrg.return_value.assess.return_value = regime

    # [P1.5] 批次身份/时效是 inject 消费前置条件: 无 started_at/scanned_at 的记录
    #   一律视为"时效不可核验" → 不注入 (不静默回用旧候选)。
    _now_iso = ue._now_et().isoformat()
    scan_rec = {
        "type": "scan", "date": ue._today(),
        "surfaced": candidates, "model_used": "test-model",
        "scan_window": "premarket", "batch_id": "UE:test-batch",
        "batch_status": "complete", "started_at": _now_iso, "scanned_at": _now_iso,
        "policy_version": "da_ue_v1",
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
        mock.patch("app.playbook.config.get_playbook_config",
                   return_value=_FakePbCfg(pb_values)),
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
            tmp_cfg={"low_atr_gate_enabled": False},
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
    """源级契约: 与执行门同 accessor 同阈值同纪律 (防路径分叉导致双门失联)."""

    def test_reads_exec_gate_via_same_accessor(self):
        """U2 根因修: 与 pipeline.py:5317-5318 同一 accessor get_playbook_config()."""
        s = _src(_SRC)
        assert "get_playbook_config" in s
        assert '"low_atr_gate_enabled"' in s
        assert '"low_atr_gate_min_atr_pct"' in s

    def test_no_flat_key_fork_regression(self):
        """反分叉 (源级): 扁平键禁止回归 — get_dotted 严格逐层读不到
        playbook.low_atr_gate_* (真路径在 execution_parameters 下), 本次事故根因."""
        s = _src(_SRC)
        assert '"playbook.low_atr_gate_enabled"' not in s
        assert '"playbook.low_atr_gate_min_atr_pct"' not in s

    def test_default_threshold_matches_pipeline(self):
        s = _src(_SRC)
        assert '"low_atr_gate_min_atr_pct", 0.025' in s
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


class TestNoForkNail:
    """反分叉钉 (mock 不可满足): 活 unified_config.yaml 双 accessor 读同一键必须同值.

    A = 执行门 / 修复后 UE 预检: get_playbook_config() — PlaybookConfig._load 把
        playbook.risk_parameters + execution_parameters 扁平合并 (config.py:84-90);
    B = yaml 真实路径直读: ConfigContainer.get_dotted("playbook.execution_parameters.*")
        (严格逐层, config_container.py:382-418)。
    U2 事故: 预检走了第三条路 (扁平键 playbook.low_atr_gate_*) 恒读默认 False,
    执行门读 True/0.03 — 同名门一侧活一侧死, 且旧测试用 mock 注入错键恒绿。
    本钉不 mock 任何配置层: 键被搬走/两路分叉/单侧改值 → 立即红。
    """

    @pytest.fixture(autouse=True)
    def _live_singletons(self):
        from app.playbook.config import reset_playbook_config
        reset_playbook_config()
        yield
        reset_playbook_config()

    def test_both_accessors_agree_on_live_yaml(self):
        from app.core.config_container import ConfigContainer
        from app.playbook.config import get_playbook_config

        pb = get_playbook_config()
        container = ConfigContainer.get_instance()
        for flat_key, dotted in (
            ("low_atr_gate_enabled",
             "playbook.execution_parameters.low_atr_gate_enabled"),
            ("low_atr_gate_min_atr_pct",
             "playbook.execution_parameters.low_atr_gate_min_atr_pct"),
        ):
            via_pb = pb.get(flat_key, None)
            via_yaml = container.get_dotted(dotted, None)
            assert via_yaml is not None, (
                f"{dotted} 在活 yaml 缺失 — 键被搬走? 须同步两 accessor 与本钉"
            )
            assert via_pb == via_yaml, (
                f"accessor 分叉: get_playbook_config().get({flat_key!r})={via_pb!r} "
                f"vs get_dotted({dotted!r})={via_yaml!r}"
            )

    def test_nail_cannot_be_satisfied_by_mocked_flat_keys(self):
        """负例自证: 扁平键路径 (事故根因) 从活 yaml 读不到 — 钉住 get_dotted 严格逐层语义."""
        from app.core.config_container import ConfigContainer
        container = ConfigContainer.get_instance()
        assert container.get_dotted("playbook.low_atr_gate_enabled", None) is None
        assert container.get_dotted("playbook.low_atr_gate_min_atr_pct", None) is None
