"""scripts/daily_llm_token_report.py UE 思考预算常驻行契约测试 (2026-09-24, RFC-105).

背景: UE 主模型换 glm-5.3 后强制思考链烧穿输出预算 → `finish_reason=length` →
schema 失败 → 静默兜底 deepseek。修复 (9/17 抬 max_tokens 至 24000 + 9/18
`reasoning_effort: high`) 的后验读数原本挂在**一次性** Hermes job `96c8e404f55a`
(载体 `ops/hermes/ue_truncation_readout.py`) 上, 该 job 9/18 21:45 跑完即消耗,
且 `delivery_outcome=failed` ⇒ 结论从未送达 (executions.db 实证)。用户裁
「并入每日 LLM 日报常驻一行, 不再依赖一次性 job」。

本测试只验归集与判定逻辑 (内存库打桩), 不碰生产库 / 不发飞书。
判据与锚点必须与独立读数脚本**逐字一致** (改判据＝改尺子须用户裁)。
回滚: 删本文件 + revert scripts/daily_llm_token_report.py 即可。
"""
import importlib.util
import re
import sqlite3
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

STANDALONE = PROJECT_ROOT / "ops" / "hermes" / "ue_truncation_readout.py"
GLM = "glm-5.3"
FB = "deepseek-v4.1-flash"


def _db(rows):
    """rows: (model, tokens_out, latency_ms, success, error_msg, created_at, is_test)"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, feature TEXT, model TEXT, "
        "timestamp INTEGER, success INTEGER, tokens_out INTEGER, latency_ms REAL, "
        "error_msg TEXT, created_at TEXT, is_test INTEGER)")
    for i, (model, tok, lat, ok, err, created, is_test) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO llm_calls (id, feature, model, success, tokens_out, latency_ms, "
            "error_msg, created_at, is_test) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, rep.UE_FEATURE, model, ok, tok, lat, err, created, is_test))
    conn.commit()
    return conn


def _glm(err="", tok=5000, lat=60000, created="2026-09-21 12:00:00", ok=1):
    return (GLM, tok, lat, ok, err, created, 0)


# ── 口径: 排除测试流量 / 降档重试臂 / 兜底臂 ─────────────────────────────
def test_excludes_test_traffic():
    conn = _db([_glm(), (GLM, 16000, 600000, 1, "schema_failed finish_reason=length",
                         "2026-09-22 12:00:00", 1)])
    b = rep._query_ue_budget(conn)
    assert b["n"] == 1 and b["trunc"] == 0


def test_excludes_downshift_retry_arm_from_denominator():
    """一次触顶会落两行 (主调用 + 降档重试), 重试臂不入分母否则截断率翻倍虚增。"""
    conn = _db([
        _glm(err="schema_failed finish_reason=length", tok=24000),
        _glm(err="downshift_retry_schema_failed finish_reason=length", tok=12000),
    ])
    b = rep._query_ue_budget(conn)
    assert b["n"] == 1 and b["trunc"] == 1 and b["retry"] == 1


def test_counts_fallback_arm_separately():
    """主模型失败后由 fallback 作答: 兜底行单独计数, 不进 glm 分母。"""
    conn = _db([_glm(), (FB, 6000, 60000, 1, "", "2026-09-21 13:00:00", 0)])
    b = rep._query_ue_budget(conn)
    assert b["n"] == 1 and b["fallback"] == 1


def test_pre_anchor_rows_excluded():
    conn = _db([_glm(created="2026-09-17 13:00:00"),
                (GLM, 16000, 160000, 1, "finish_reason=length", "2026-09-17 13:00:00", 0)])
    b = rep._query_ue_budget(conn)
    assert b["n"] == 0 and b["trunc"] == 0


def test_p50_latency_and_max_out():
    conn = _db([_glm(lat=10000, tok=1000), _glm(lat=50000, tok=9000),
                _glm(lat=30000, tok=5000)])
    b = rep._query_ue_budget(conn)
    assert b["max_out"] == 9000 and b["p50_lat"] == 30.0


# ── 判定: 与独立读数脚本预注册判据同轴 ─────────────────────────────────
def test_verdict_insufficient_below_three():
    conn = _db([_glm(), _glm()])
    assert rep._query_ue_budget(conn)["verdict"] == "样本不足"


def test_verdict_improved_below_one_third():
    conn = _db([_glm() for _ in range(10)])
    assert rep._query_ue_budget(conn)["verdict"] == "改善"


def test_verdict_boundary_one_third_is_critical_not_improved():
    """率恰 =1/3 属临界 (改善要求严格 <1/3)。"""
    conn = _db([_glm()] * 4 + [_glm(err="finish_reason=length", tok=24000)] * 2)
    b = rep._query_ue_budget(conn)
    assert b["n"] == 6 and abs(b["rate"] - 1 / 3) < 1e-9 and b["verdict"] == "临界"


def test_verdict_falsified_at_or_above_half():
    conn = _db([_glm()] * 3 + [_glm(err="finish_reason=length", tok=24000)] * 3)
    assert rep._query_ue_budget(conn)["verdict"] == "未改善/证伪"


def test_no_rows_at_all():
    b = rep._query_ue_budget(_db([]))
    assert b["n"] == 0 and b["rate"] is None and b["verdict"] == "样本不足"


# ── 渲染 ────────────────────────────────────────────────────────────────
def test_build_message_prints_ue_line_with_n():
    conn = _db([_glm() for _ in range(10)])
    ub = rep._query_ue_budget(conn)
    msg = rep._build_message({"date": "2026-09-24", "total": {
        "calls": 1, "ok": 1, "tok_in": 10, "tok_out": 10, "cost": 0.01,
        "lat_sum": 1000.0, "trunc": 0}, "by_feature": {
        rep.UE_FEATURE: {"calls": 1, "ok": 1, "tok_in": 10, "tok_out": 10, "cost": 0.01,
                         "lat_sum": 1000.0, "trunc": 0}}, "by_model": {}},
        None, ue_budget=ub)
    assert "UE 思考预算" in msg
    assert "主调用 10 次" in msg and "判定: 改善" in msg


def test_build_message_ue_line_absent_when_not_passed():
    empty = {"date": "2026-09-24", "total": {"calls": 1, "ok": 1, "tok_in": 10,
             "tok_out": 10, "cost": 0.01, "lat_sum": 1000.0, "trunc": 0},
             "by_feature": {"x": {"calls": 1, "ok": 1, "tok_in": 10, "tok_out": 10,
                                  "cost": 0.01, "lat_sum": 1000.0, "trunc": 0}},
             "by_model": {}}
    assert "UE 思考预算" not in rep._build_message(empty, None)


# ── 跨文件口径不漂移守卫 (改一处不改另一处 = 两把尺子) ──────────────────
def test_anchor_matches_standalone_readout():
    assert STANDALONE.exists(), f"独立读数脚本缺失: {STANDALONE}"
    src = STANDALONE.read_text(encoding="utf-8")
    anchor = re.search(r'"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"', src)
    assert anchor, "独立脚本解析不到锚点常量"
    assert anchor.group(1) == rep.UE_BUDGET_ANCHOR


def test_thresholds_match_standalone_readout():
    src = STANDALONE.read_text(encoding="utf-8")
    assert re.search(r"cap / gl < 1 / 3", src), "独立脚本改善阈值已改"
    assert re.search(r"cap / gl >= 1 / 2", src), "独立脚本证伪阈值已改"
    # 本侧行为须与之一致: <1/3 改善 / >=1/2 未改善 / 其余临界
    assert rep._query_ue_budget(_db([_glm() for _ in range(9)]))["verdict"] == "改善"
    assert rep._query_ue_budget(
        _db([_glm()] + [_glm(err="finish_reason=length")] * 2))["verdict"] == "未改善/证伪"
    assert rep._query_ue_budget(_db([_glm()] * 4))["verdict"] == "改善"
