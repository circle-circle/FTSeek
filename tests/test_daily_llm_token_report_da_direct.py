"""scripts/daily_llm_token_report.py DA 直连段契约测试 (2026-09-01, PE 审阅执行批).

背景: DA 走 skill urllib 直连 DashScope, 不落 llm_audit.db → 日报/预算告警此前对
全链最大 LLM 消费方 (~96% 成本, 8/31 实测 $2.97/$3.10) 完全失明。本批把
da_checkpoint/{day}/*.json 的 _llm_output._usage 归集进日报 (总计/预算/环比全链口径)。

本测试只验归集与展示逻辑 (tmp 目录打桩), 不碰生产 checkpoint / 不发飞书。
回滚: 删本文件 + revert scripts/daily_llm_token_report.py 即可。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_llm_token_report", PROJECT_ROOT / "scripts" / "daily_llm_token_report.py"
)
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)


def _mk_ckpt(day_dir: Path, symbol: str, tok_in: int, tok_out: int,
             cached: int = 0, finish: str = "stop", with_debate: bool = True,
             raw: bool = False):
    day_dir.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}.raw.json" if raw else f"{symbol}.json"
    doc = {}
    if not raw:
        doc["_llm_output"] = {
            "_usage": {"prompt_tokens": tok_in, "completion_tokens": tok_out,
                        "prompt_tokens_details": {"cached_tokens": cached}},
            "_finish_reason": finish,
            "_model_reported": "qwen3.7-max",
        }
        if with_debate:
            doc["_llm_output"]["_debate"] = {"challenger_verdict": "AGREE"}
    else:
        doc = {"schema": "da_raw_inputs/v1"}
    (day_dir / name).write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def tmp_data(monkeypatch, tmp_path):
    monkeypatch.setattr(rep, "DATA", tmp_path)
    return tmp_path


def test_da_direct_empty_day(tmp_data):
    out = rep._query_da_direct("2026-01-01")
    assert out["calls"] == 0 and out["cost"] == 0.0 and out["challenger_calls"] == 0


def test_da_direct_aggregates_usage_and_cost(tmp_data):
    day = tmp_data / "da_checkpoint" / "2026-08-31"
    _mk_ckpt(day, "US.AAA", 8000, 2000, cached=1800)
    _mk_ckpt(day, "US.BBB", 9000, 2500, finish="length")
    _mk_ckpt(day, "US.CCC", 100, 50, raw=True)  # 生料档必须被排除
    out = rep._query_da_direct("2026-08-31")
    assert out["calls"] == 2  # raw.json 不计
    assert out["tok_in"] == 17000 and out["tok_out"] == 4500
    assert out["cached"] == 1800
    assert out["trunc"] == 1  # finish_reason=length 计数
    assert out["challenger_calls"] == 2
    # 价格随 config/unified_config.yaml llm.models.qwen 变动 (2026-08-27 qwen-max→flash 降档
    # 后成本口径已迁; 硬编码 0.008/0.024 是旧价, 读 SSOT 对齐避免下次调价又红).
    costs = rep._llm_model_costs()
    cin, cout = costs.get("qwen3.7-max", (0.0, 0.0))
    assert out["cost"] == pytest.approx(17.0 * cin + 4.5 * cout, rel=1e-6)


def test_da_direct_tolerates_corrupt_file(tmp_data):
    day = tmp_data / "da_checkpoint" / "2026-08-31"
    day.mkdir(parents=True, exist_ok=True)
    (day / "BAD.json").write_text("{not json", encoding="utf-8")
    _mk_ckpt(day, "US.OKK", 1000, 500)
    out = rep._query_da_direct("2026-08-31")
    assert out["calls"] == 1


def _gw_day(cost=0.1, calls=2):
    return {"date": "2026-08-31",
            "total": {"calls": calls, "ok": calls, "tok_in": 5000, "tok_out": 1000,
                       "cost": cost, "lat_sum": 60000.0, "trunc": 0},
            "by_feature": {"ue": {"calls": calls, "ok": calls, "tok_in": 5000,
                                    "tok_out": 1000, "cost": cost, "lat_sum": 60000.0,
                                    "trunc": 0}},
            "by_model": {"glm-5.2": {"calls": calls, "ok": calls, "tok_in": 5000,
                                       "tok_out": 1000, "cost": cost, "trunc": 0}}}


def test_build_message_includes_da_section_and_full_cost():
    da = {"calls": 26, "ok": 26, "tok_in": 220000, "tok_out": 46000, "cached": 27000,
          "cost": 2.97, "trunc": 0, "challenger_calls": 26}
    msg = rep._build_message(_gw_day(), _gw_day(cost=0.05), None, da, {"cost": 0.0})
    assert "DA 直连" in msg
    assert "分析师: 26次" in msg
    assert "挑战者: 26次" in msg
    assert "含 DA 直连 26 次" in msg  # 总计行合并口径
    assert "缓存命中27.0ktok" in msg


def test_build_message_full_cost_drives_budget_alert():
    # 网关 $0.1 远低于预算, 但加 DA $4.95 → 全链路 $5.05 超 $5 上限 → 必须告警
    da = {"calls": 30, "ok": 30, "tok_in": 1, "tok_out": 1, "cached": 0,
          "cost": 4.95, "trunc": 0, "challenger_calls": 0}
    msg = rep._build_message(_gw_day(cost=0.1), None, None, da, None)
    assert "超预算" in msg


def test_build_message_no_calls_no_da_unchanged():
    empty = _gw_day(cost=0.0, calls=0)
    empty["total"]["calls"] = 0
    msg = rep._build_message(empty, None, None, None, None)
    assert "无 LLM 调用记录" in msg


def test_model_costs_contains_da_model():
    costs = rep._llm_model_costs()
    assert "qwen3.7-max" in costs  # DA 生产模型必须可定价, 否则成本又归零失明
    assert costs["qwen3.7-max"][0] > 0
