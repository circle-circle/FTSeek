"""盘后复盘"最大回撤"口径钉子 (2026-09-03 复盘批, REVIEW-20260903 §四.5).

钉住两个病灶不得复发:
  ① 常量退化 — 原 `obsidian_writer` 读 `data['max_drawdown']`, 而唯一调用方
     `daily_review._save_report` 取 `summary.get('max_drawdown', 0)`, 但
     `_generate_summary` 从不产该键 → 盘后文档天天印 "最大回撤: 0.00%",
     与同日 "-49.30 净亏 + 水下过夜仓" 并列 (AGENT.md 军规"基数自检"同类)。
  ② 量纲说谎 — 源字段 trade_records.max_drawdown 是**百分点**口径
     (pipeline._max_drawdown_from_mfe_mae = 100*(mfe+mae)/(100+mfe)), 而渲染用 `.2%`
     → 一旦接线, GTLB 9/2 的 3.6464pp 会印成 "364.64%" (军规"单位/量纲声明"同类)。

回滚: 删本文件 + revert 对应提交即可。
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.core.obsidian_writer import ObsidianWriter  # noqa: E402
from app.review.daily_review import DailyReviewGenerator  # noqa: E402

_STUB = types.SimpleNamespace()   # _generate_summary 非空分支不触 self


def _trade(symbol="US.GTLB", *, dd=3.6464, pnl=16.39):
    return {"symbol": symbol, "realized_pnl": pnl, "pnl_net": pnl - 4.7, "fees_est": 4.7,
            "pnl_pct": 0.0028, "mfe_pct": 1.213, "mae_pct": 2.478, "max_drawdown": dd,
            "holding_hours": 0.2}


def test_summary_emits_drawdown_with_unit_and_basis():
    s = DailyReviewGenerator._generate_summary(
        _STUB, [_trade(), _trade("US.DELL", dd=2.0769, pnl=-45.12)],
        [_trade(), _trade("US.DELL", dd=2.0769, pnl=-45.12)])
    assert s["max_position_drawdown_pp"] == 3.65, "取当日最差单笔 (峰→谷, 百分点)"
    assert s["max_position_drawdown_symbol"] == "US.GTLB"
    assert s["max_position_drawdown_n"] == 2, "非零样本数=基数自检, 全零日必须显式为 0"
    assert "max_drawdown" not in s, "不带量纲的旧键名不得复活 (下游会再套 .2%)"


def test_summary_handles_zero_drawdown_rows():
    rows = [_trade("US.MDB", dd=0.0), _trade("US.ASTS", dd=0.0)]
    s = DailyReviewGenerator._generate_summary(_STUB, rows, rows)
    assert s["max_position_drawdown_pp"] == 0.0
    assert s["max_position_drawdown_n"] == 0, "mfe/mae 缺失 → 0.0 (源语义), 计数必须如实为 0"


def _write(tmp_path, **over):
    w = ObsidianWriter(vault_path=str(tmp_path))
    data = {"date": "2026-09-02", "market": "US", "total_trades": 2, "win_rate": 0.5,
            "total_pnl": 43.82, "max_drawdown_pp": 3.65,
            "max_drawdown_symbol": "US.GTLB", "max_drawdown_n": 2,
            "trades": [{"code": "US.GTLB", "pnl": 11.59, "id": "1424"},
                       {"code": "US.DELL", "pnl": -49.30, "id": "1425"}]}
    data.update(over)
    path = w.write_daily_postmarket(data)
    return Path(path).read_text(encoding="utf-8")


def test_doc_line_declares_unit_and_sample_basis(tmp_path):
    text = _write(tmp_path)
    assert "最大单笔持仓回撤" in text
    assert "3.65 pp (峰→谷)" in text, "单位必须写在标签里 (百分点, 非小数)"
    assert "US.GTLB" in text and "2/2 笔有非零回撤" in text
    assert "364.6" not in text, "百分点值被 .2% 渲染 = 量纲说谎, 不得复发"
    assert "最大回撤**: 0.00%" not in text


def test_doc_line_is_honest_on_all_zero_day(tmp_path):
    text = _write(tmp_path, max_drawdown_pp=0.0, max_drawdown_symbol="US.MDB",
                  max_drawdown_n=0)
    assert "0.00 pp (峰→谷)" in text and "0/2 笔有非零回撤" in text, (
        "全零日要显式说'0 笔有非零回撤', 不能伪装成'回撤 0.00%'")


def test_doc_line_missing_key_renders_na(tmp_path):
    text = _write(tmp_path, max_drawdown_pp=None, max_drawdown_symbol=None,
                  max_drawdown_n=0)
    assert "n/a | 当日 0/2 笔有非零回撤" in text


def test_source_contract_no_percent_format_on_pp_field():
    src = (REPO / "app" / "core" / "obsidian_writer.py").read_text(encoding="utf-8")
    # 只判代码位 (剥掉整行注释) — 修复说明里逐字引用旧渲染串是留痕需要, 不算"旧代码回来了"
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "- **最大回撤**: {data.get('max_drawdown', 0):.2%}" not in body, (
        "旧渲染 (恒 0.00% + 百分点值套 .2%) 不得回来")
    assert "_dd_line" in body and "max_drawdown_pp" in body
    rev = (REPO / "app" / "review" / "daily_review.py").read_text(encoding="utf-8")
    assert "'max_drawdown_pp': summary.get('max_position_drawdown_pp')" in rev, (
        "调用方必须传带量纲后缀的键 (键名即单位声明)")
