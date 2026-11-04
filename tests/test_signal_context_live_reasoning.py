"""推理留痕重接现役链路 (2026-11-04)

缺陷: playbook_signal_context.llm_reasoning_json 自 2026-10-01 起 1668 行填充 0 (0.0%)。
根因: 写入方 signal_context_store 按 decision_id 查 decisions.db.model_votes —— 那是
  **3 模型投票**的载体, 最后一行 2026-07-12T05:50 (投票退役日); 现役链路是
  DA→bridge→DB, BridgeVotingAdapter 从不产 model_votes ⇒ 查询恒 0 行。
  同因: raw_data 的 brief_conclusion/brief_reasons/brief_risks 也从 vote_result["reasoning"]
  (退役投票输出格式 "文本|JSON") 里拆 ⇒ 五个 brief_* 字段恒空。

修法: 改接**现役** bridge 返回的 thesis / conditions / risks / entry_strategy / invalidation,
  逐字透传; 值为空则跳过 (不写占位串, 不制造"有留痕"的假象)。

不变量: 交易决策链零改动 (本函数只写 playbook_signal_context, 无返回值, 不参与下单)。
"""
import inspect
from pathlib import Path

import pytest

from app.playbook.pipeline_parts import signal_context_store as sc
from app.playbook.pipeline_parts.bridge_adapters import BridgeVotingAdapter

REPO = Path(__file__).resolve().parents[1]
SRC = inspect.getsource(sc)
CODE = "\n".join(ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#"))


def _code_only(path: Path) -> str:
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))


class _FakeData:
    """最小 PlaybookData 形状 (只测映射, 不测采集)。"""
    news = None
    insider = None
    earnings = None
    fundamental = None


def _live_vote_result(**over):
    """BridgeVotingAdapter.vote() 现役返回形状 (逐字取自 bridge_adapters.py:158-187)。"""
    base = {
        "symbol": "US.COIN", "direction": "SHORT", "final_strength": 65,
        "final_confidence": 0.35, "consensus_level": 0.35, "std_dev": 0.0,
        "model_scores": [], "is_neutral": False,
        "decision_id": "DA-20261103211546-US.COIN-496c",
        "strategy_id": "deep_analysis",
        "thesis": "Q3营收骤降35%且TTM亏损，大摩下调目标价至120，等反弹至143.50首批做空。",
        "conditions": ["反弹至143.50", "成交量放大"],
        "entry_strategy": "分2批限价", "invalidation": "站上148.75",
        "risks": ["66%分析师维持买入且木头姐加仓，可能引发逼空"],
        "entry_price": 143.5, "stop_loss": 148.75, "take_profit": 129.37,
    }
    base.update(over)
    return base


@pytest.fixture()
def captured(monkeypatch):
    """拦 PlaybookDB.save_signal_context, 只捕获 ctx (不落生产库)。"""
    box = {}

    class _DB:
        def __init__(self, *a, **k):
            pass

        def save_signal_context(self, ctx):
            box.update(ctx)

    monkeypatch.setattr("app.playbook.db.PlaybookDB", _DB, raising=True)
    return box


def _run(box, vote_result):
    sc._persist_signal_context(
        "2026-11-03", "US.COIN", _FakeData(), vote_result,
        module_file=str(REPO / "app/playbook/pipeline.py"),
        compute_key_levels=lambda d: {"support": [], "resistance": []},
        load_theme_context=lambda p: None,
        logger=type("L", (), {"warning": staticmethod(lambda *a, **k: None)})(),
    )
    return box


class TestNoRetiredCarrier:
    def test_no_model_votes_query(self):
        assert "model_votes" not in CODE, (
            "model_votes 属 3 模型投票 (2026-07-12 停写) —— 留痕不得挂在退役载体上")

    def test_no_retired_reasoning_key(self):
        assert 'vote_result.get("reasoning"' not in CODE, (
            '现役 BridgeVotingAdapter 不返回 "reasoning" 键; 该读取恒空')

    def test_bridge_really_has_no_reasoning_key(self):
        """前提钉住: 若将来 bridge 真加了 reasoning, 本测试会提醒一并接线。"""
        src = _code_only(REPO / "app/playbook/pipeline_parts/bridge_adapters.py")
        assert '"thesis"' in src, "bridge 返回体应有 thesis (现役留痕来源)"
        assert "reasoning" not in src, "bridge 出现 reasoning 键 ⇒ 留痕映射需一并重评"


class TestLiveChainMapping:
    def test_llm_reasoning_from_thesis(self, captured):
        box = _run(captured, _live_vote_result())
        rows = box.get("llm_reasoning")
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert row["reasoning"].startswith("Q3营收骤降35%")
        assert row["model_id"] == "deep_analysis"
        assert row["direction"] == "SHORT"
        assert row["strength"] == 65
        assert row["confidence"] == 0.35

    def test_brief_fields_from_live_keys(self, captured):
        box = _run(captured, _live_vote_result())
        raw = box.get("raw_data") or {}
        assert raw["brief_conclusion"].startswith("Q3营收骤降35%")
        assert raw["brief_reasons"] == ["反弹至143.50", "成交量放大"]
        assert raw["brief_risks"] and "逼空" in raw["brief_risks"][0]
        assert raw["entry_strategy"] == "分2批限价"
        assert raw["stop_trigger"] == "站上148.75"
        assert "key_levels" in raw

    def test_no_thesis_leaves_empty(self, captured):
        """无 thesis ⇒ 留空 (不写占位串), brief_* 也不写。"""
        box = _run(captured, _live_vote_result(thesis="", conditions=[], risks=[],
                                              entry_strategy="", invalidation=""))
        assert not box.get("llm_reasoning")
        raw = box.get("raw_data") or {}
        for k in ("brief_conclusion", "brief_reasons", "brief_risks", "entry_strategy", "stop_trigger"):
            assert k not in raw

    def test_vote_result_none_still_persists_inputs(self, captured):
        box = _run(captured, None)
        assert box["date"] == "2026-11-03" and box["symbol"] == "US.COIN"
        assert not box.get("llm_reasoning")