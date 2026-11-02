"""LLM 逐笔归因死门拆除契约钉 (2026-11-01).

拆除对象: `app/core/scheduler_triggers.py` 盘后复盘里的「LLM 逐笔归因」块。

死因 (实测):
    today_trades = recorder.get_trades_by_date(today_str) if hasattr(recorder, 'get_trades_by_date') else []
`TradeRecorder` 的公开方法只有
    get_record / get_records_by_account / get_records_by_date / get_records_with_pnl /
    get_review_statistics / record / record_close / update_review
**没有** `get_trades_by_date` ⇒ hasattr 恒 False ⇒ today_trades 恒 `[]` ⇒ 循环体一次没跑过。
整块还包在 `except Exception → logger.debug` 里 (LOG_LEVEL=INFO 不可见) ⇒ 零调用 + 零告警。

数据取证 (拆除时):
    trade_attribution 223 行, llm_model / key_decision_factor / root_cause_category 填充均为 0
    (表内行全部来自 order_executor 下单期阶段性归因, 与本块无关) ⇒ 本块从未写入一行。

本文件钉三件事: ①死门不得复活 (也不得换名 sneaky 复活) ②真实方法名仍存在 (前提实证)
③attribution_engine 本身仍有活调用方 (说明只删了调用点, 没把引擎删成孤岛)。
"""
import inspect
import sqlite3
from pathlib import Path

import pytest

from app.core import scheduler_triggers as st
from app.storage.trade_recorder import TradeRecorder

PROD_DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
REPO = Path(__file__).resolve().parents[1]   # 扫"当前这棵树", 不用绝对路径 (否则 worktree 里会误扫主仓)


def _code_only(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


class TestDeadGateRemoved:
    def test_scheduler_triggers_no_longer_mentions_phantom_method(self):
        src = _code_only(inspect.getsource(st))
        assert "get_trades_by_date" not in src, (
            "scheduler_triggers 又在按不存在的方法名取当日交易 (TradeRecorder 无该方法) ⇒ "
            "hasattr 恒 False, 归因静默空转。真名是 get_records_by_date")

    def test_no_module_uses_phantom_method(self):
        hits = []
        for p in (REPO / "app").rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            src = _code_only(p.read_text(encoding="utf-8", errors="ignore"))
            if "get_trades_by_date" in src:   # 只看代码行, tombstone 注释里的说明不算引用
                hits.append(str(p.relative_to(REPO)))
        assert not hits, f"以下文件仍引用不存在的方法 get_trades_by_date: {hits}"

    def test_block_tombstone_present(self):
        """删除必须留恢复条件, 否则下个 agent 会照着旧注释把死门重建一遍。"""
        src = inspect.getsource(st)
        assert "LLM 逐笔归因 — 已下线" in src
        assert "get_records_by_date" in src, "tombstone 必须写明真实方法名"


class TestPremiseStillHolds:
    """前提实证: 真方法存在、假方法不存在、LLM 归因列仍为 0 填充。"""

    def test_real_method_exists(self):
        assert hasattr(TradeRecorder, "get_records_by_date")

    def test_phantom_method_absent(self):
        assert not hasattr(TradeRecorder, "get_trades_by_date"), (
            "get_trades_by_date 已被实现 ⇒ 拆除前提变了, 需重评: 此时应改为"
            "显式接线 + LLM 费用预注册, 而不是保留删除")

    def test_llm_attribution_columns_still_empty(self):
        con = sqlite3.connect(PROD_DB)
        row = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN llm_model IS NOT NULL AND llm_model<>'' THEN 1 ELSE 0 END) "
            "FROM trade_attribution").fetchone()
        con.close()
        total, filled = row
        assert total and total > 50, f"trade_attribution 样本异常 ({total}), 前提未实证"
        assert (filled or 0) == 0, (
            f"trade_attribution.llm_model 已有 {filled} 行非空 ⇒ 有别的路径在做逐笔 LLM 归因, "
            "本块的拆除理由 (从未写入过) 已变, 需重评")


class TestEngineNotOrphaned:
    def test_attribution_engine_importable(self):
        from app.llm.attribution_engine import get_attribution_engine
        assert callable(get_attribution_engine)

    def test_weekly_proposer_still_calls_engine(self):
        """只删调用点, 不得把引擎删成孤岛 (weekly_proposer 仍用 get_systemic_issues)。"""
        src = _code_only((REPO / "app/llm/weekly_proposer.py").read_text(encoding="utf-8"))
        assert "get_attribution_engine" in src