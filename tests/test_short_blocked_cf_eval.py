"""short_blocked_cf_eval 契约测试 (DEB-20261031).

钉住三件事 (口径错了会直接误导「要不要为被掐空单开门」的裁决):
  1. 桶分类: bridge / entry_confirm_reversed / admission / sector / 其他 的字面量映射不变;
  2. 判据门: n<30 一律 INSUFFICIENT_N (不许小样本出结论), ±0.15% 费用线边界方向正确;
  3. 方向带符号: SHORT 盈利记正 (signed = -(exit/entry-1)) —— 符号反了会把「该开门」读成「拦得好」。
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "short_blocked_cf_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("short_blocked_cf_eval", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_bucket_classification_literals():
    m = _load()
    assert m.classify("SKIP: 非当前通道候选(bridge未提名/无方向)") == "bridge/通道候选未提名"
    assert m.classify("entry_confirm_reversed") == "entry_confirm 2min门弃单"
    assert m.classify("admission: PRICE_TOO_LOW(3.69<5)") == "admission 价量门"
    assert m.classify("sector 集中度 guardrail 降级 (Technology)") == "sector 集中度"
    assert m.classify("") == "无指令(skip_reason 空)"
    assert m.classify("thesis_drift_invalidate").startswith("其他: ")


def test_verdict_respects_min_n_and_fee_line():
    m = _load()
    # 小样本一律不下结论 (即使均值很漂亮)
    assert m._verdict(29, 5.0) == "INSUFFICIENT_N"
    # 过门 + 超费用线 → 误杀嫌疑; 反向 → 拦对; 线内 → 不可分辨
    assert m._verdict(30, 0.15) == "OVER_BLOCKING_SUSPECT"
    assert m._verdict(30, -0.15) == "BLOCK_CORRECT"
    assert m._verdict(30, 0.1499) == "UNRESOLVED"


def test_short_sign_is_profit_positive():
    """价格跌 → 做空赚 → signed 必须为正。"""
    m = _load()
    samples = [{"date": "2026-10-01", "symbol": "US.X", "bucket": "b",
                "bars": {"2026-10-01": (100.0, 100.0), "2026-10-02": (100.0, 95.0)},
                "raw_reasons": []}]
    stats = m.cf_stats(samples)
    assert stats["T+1"]["b"]["mean_pct"] > 0, "做空方向符号反了 (会把该开门读成拦得好)"