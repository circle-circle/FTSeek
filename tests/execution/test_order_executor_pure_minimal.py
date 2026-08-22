"""order_executor.py 最小纯函数测试集 — DEB-20260822 任务F (P1-3 前置子项提前)。

背景: 双维审计 (DEB-20260820 E1/E2) 指出 order_executor.py (5.4K行) 无专属测试,
近期事故集中于该文件。本文件只覆盖**可离线测试的纯函数** (零 broker/DB/网络依赖),
作为 P1-3 拆分前置的最小安全网; 完整测试矩阵仍归 P1-3 排队项。

覆盖: _is_option_contract / _build_close_metadata_json / _derive_entry_reason_for_close /
      _close_retry_key / _is_backoff_error 边界。
回滚: 删本文件即可, 零生产代码改动。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.order_executor import (
    _build_close_metadata_json,
    _close_retry_key,
    _derive_entry_reason_for_close,
    _is_backoff_error,
    _is_option_contract,
)
from app.core.models import Direction


# ── _is_option_contract: OCC 格式识别 ────────────────────────────────────

def test_option_put_and_call_detected():
    # DEB-20260822 修复回归: root≤3字符 与 root≥4字符 都必须识别
    assert _is_option_contract("US.CRM260515P180000") is True      # root=3 (旧实现唯一能命中的形态)
    assert _is_option_contract("US.AAPL260515C180000") is True     # root=4 (旧实现误判股票→乘数错100倍)
    assert _is_option_contract("US.GOOGL260121P00180000") is True  # root=5 + 8位行权价


def test_stock_symbols_not_option():
    assert _is_option_contract("US.AAPL") is False
    assert _is_option_contract("US.BABA") is False
    assert _is_option_contract("HK.00700") is False
    assert _is_option_contract("US.BRK.B") is False  # 多段代码=股票, 非期权


def test_option_malformed_inputs():
    # 无市场前缀 / 多余段 → 非期权
    assert _is_option_contract("AAPL260515P180000") is False
    assert _is_option_contract("") is False
    # 缺日期或 P/C 结构不完整 → 非期权
    assert _is_option_contract("US.AAPL26051C180000") is False   # 日期仅5位
    assert _is_option_contract("US.AAPL260515180000") is False   # 无 P/C
    # 行权价段含非数字 → 非期权
    assert _is_option_contract("US.AAPL260515P1800AB") is False


# ── _close_retry_key: direction 枚举/字符串双态归一 ──────────────────────

def test_retry_key_normalizes_direction():
    assert _close_retry_key("US.MU", Direction.LONG) == ("US.MU", "LONG")
    assert _close_retry_key("US.MU", "short") == ("US.MU", "SHORT")
    assert _close_retry_key(123, "long") == ("123", "LONG")


# ── _is_backoff_error: 退避类别识别 ─────────────────────────────────────

def test_backoff_empty_msg_is_false():
    assert _is_backoff_error("") is False
    assert _is_backoff_error(None) is False


def test_backoff_patterns_hit_case_insensitive():
    assert _is_backoff_error("持仓不足") is True
    assert _is_backoff_error("订单被拒: 待成交订单存在") is True
    # 英文模式大小写不敏感
    assert _is_backoff_error("Insufficient shares to close") is True
    assert _is_backoff_error("PENDING ORDER exists") is True


def test_backoff_unrelated_error_is_false():
    assert _is_backoff_error("network timeout") is False
    assert _is_backoff_error("行情订阅失败") is False


# ── _build_close_metadata_json: 平仓归因全量保留 ─────────────────────────

def _pos(mfe=0.0, mae=0.0):
    return SimpleNamespace(max_favorable_excursion=mfe, max_adverse_excursion=mae)


def test_close_metadata_preserves_attribution_fields():
    meta = {"trace_id": "tr-1", "factor_detail": {"f1": 0.2}, "signal_tags": ["a"]}
    out = _build_close_metadata_json(meta, _pos())
    parsed = json.loads(out)
    assert parsed["trace_id"] == "tr-1"
    assert parsed["factor_detail"] == {"f1": 0.2}
    assert parsed["signal_tags"] == ["a"]


def test_close_metadata_merges_execution_and_excursions():
    meta = {"execution": {"slippage_bps": 3.1}, "entry_reason": "main_signal"}
    out = json.loads(_build_close_metadata_json(meta, _pos(mfe=0.8, mae=-1.2)))
    assert out["execution"] == {"slippage_bps": 3.1}
    assert out["max_favorable_excursion"] == 0.8
    assert out["max_adverse_excursion"] == -1.2


def test_close_metadata_none_when_nothing_to_record():
    # 空 meta (dict) + 零 MFE/MAE → None (不写空 JSON 行)
    assert _build_close_metadata_json({}, _pos()) is None
    # ⚠️ 契约记录 (DEB-20260822): 生产调用方 (:5014) 恒传 dict — meta=None/非dict 会在
    # :3834 `meta.get("execution",...)` 抛 AttributeError。函数首行的 isinstance 容错分支
    # 是半死代码, 属 P2 卫生排队项, 冻结期内不改平仓路径 → 本测试钉死现契约而非理想契约。


# ── _derive_entry_reason_for_close: 三级优先级 ───────────────────────────

def test_existing_entry_reason_wins():
    meta = {"entry_reason": "news_driven", "source_type": "MAIN_PIPELINE"}
    assert _derive_entry_reason_for_close(meta) == "news_driven"


def test_literal_none_string_ignored_falls_through():
    # entry_reason='None' 字符串视为缺失 → 走 source_type 推导
    meta = {"entry_reason": "None", "source_type": "CROSS_MARKET"}
    out = _derive_entry_reason_for_close(meta)
    assert out and out != "None"


def test_fallback_by_strategy_id_then_unknown():
    # 无 source: 从 strategy_id 关键字推导 (orb → orb_breakout)
    out = _derive_entry_reason_for_close({"strategy_id": "ORB_ENGINE_v2"})
    assert "orb" in out.lower()
    # 全空 → unknown 兜底 (不抛异常)
    out2 = _derive_entry_reason_for_close({})
    assert isinstance(out2, str) and out2
