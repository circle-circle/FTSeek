"""_build_signal_quality_context 回归 — DEB-20260822 §九 反馈注入修复。

旧版三处坏损 (2026-08-22 修复, 用户裁"prompt修改不影响交易链路逻辑"):
  ① 读 confidence_map 但文件键为 confidence_winrate_map → 置信度桶段恒空;
  ② 依赖已删除的 ic_audit_report_2026-06-19.md → fallback 硬编码 6/19 旧引擎数字;
  ③ 硬编码结论"优先推荐manual方向"每日注入辩论引擎 (误导)。
本测试钉死: 真键渲染 / 缺数据不编造 / 硬编码谎言永不复发。
回滚: 删本文件即可 (零生产 schema 依赖)。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.llm_pipeline import _build_signal_quality_context

FORBIDDEN_LEGACY = ["IC均为负", "优先推荐manual", "EARNINGS_REVISION_MOMENTUM",
                    "manual_sync", "标注AVOID"]


def _fixture(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _base_payload() -> dict:
    return {
        "as_of": "2026-08-21", "history_days": 90, "total_closed_trades": 409,
        "confidence_winrate_map": {"buckets": {
            "0-0.3": {"total": 33, "win_rate": 0.6364, "avg_pnl_pct": 0.0085},
            "0.3-0.5": {"total": 143, "win_rate": 0.5315, "avg_pnl_pct": -0.0002},
        }},
        "strategy_performance": {"rolling": {"30d": {}},
                                 "all_time": {"playbook_sim": {"total": 10,
                                                               "win_rate": 0.4,
                                                               "total_pnl": -120}}},
        "threshold_recommendations": {"anomalies": ["confidence 与 edge 脱钩"]},
    }


# ── 修复①: 读真键 confidence_winrate_map ────────────────────────────────

def test_renders_buckets_from_real_key(tmp_path):
    out = _build_signal_quality_context(path=_fixture(tmp_path, _base_payload()))
    assert "conf 0-0.3" in out and "胜率=63.6%" in out
    assert "期望=+0.85%" in out          # 期望值列必须与胜率并列
    assert "confidence_map" not in out   # 旧错键不得再出现


def test_expectancy_flag_negative_bucket(tmp_path):
    out = _build_signal_quality_context(path=_fixture(tmp_path, _base_payload()))
    assert "🔴 conf 0.3-0.5" in out      # 负期望桶标红 (非按胜率标色)


def test_anomalies_pass_through_verbatim(tmp_path):
    out = _build_signal_quality_context(path=_fixture(tmp_path, _base_payload()))
    assert "confidence 与 edge 脱钩" in out


# ── 修复②③: 缺数据不编造, 硬编码谎言永不复发 ────────────────────────────

def test_missing_file_reports_unavailable_not_fabricated():
    out = _build_signal_quality_context(path="/tmp/no_such_sq_file.json")
    assert "反馈不可用" in out
    for legacy in FORBIDDEN_LEGACY:
        assert legacy not in out


def test_real_repo_data_has_no_hardcoded_conclusions():
    """对仓库真实 latest.json 渲染 — 防硬编码结论回归的活体哨兵。"""
    real = PROJECT_ROOT / "data" / "signal_quality" / "latest.json"
    if not real.exists():
        return  # 数据文件缺失时跳过 (CI/裸环境)
    out = _build_signal_quality_context()
    for legacy in FORBIDDEN_LEGACY:
        assert legacy not in out
    assert "使用要求" in out              # 收尾必须是使用指令而非编造结论


def test_corrupt_json_reports_unavailable(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text("{not valid json", encoding="utf-8")
    out = _build_signal_quality_context(path=p)
    assert "反馈不可用" in out
