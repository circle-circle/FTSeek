"""盘前供给完成判定 (RFC-196, 2026-11-10)。

立项根因 (11-09 实盘, logs/ftseek_us.log-20261110):
  [DEB-fullchain #22] 把幂等依据从"任意行存在"改成"生成完成标记"后, DA 先落库的
  日子 (每日 07:00-08:35 ET 直写 playbook_instructions, 无标记) 每轮都命中
  "已有 N 行但无完成标记 (UE 先写/上次中断), 继续生成" ⇒ 重跑**全量 generate**
  (筛 69 只 + 基本面预取 + horizon LLM, 实测 300-400s/轮, 单步不可中断):
    · 正常日: 白占盘前窗 (11-05 实测 09:00:20→09:06:50, 顶到开盘);
    · 异常日: generate 自身崩溃 (11-09 `trade_date` NameError) → 每轮重来,
      18 轮占满 09:01-10:35, watchdog 判"跨开盘卡死", 首轮入场被拖到 09:40。
  根因不是"轮次慢", 是**已经完整供给的一天仍被判为未生成**。

判据 (可双向证伪, 两态都有动作):
  bridge 文件 (DA 产出权威, 与 pipeline._bridge_file_state 同源) 的候选 symbol
  **全部**已存在于当日 playbook_instructions 的 DA 行 ⇒ complete;
  缺任何一个 ⇒ partial ⇒ 不跳过 (完整保留 generate 的补行/自愈能力)。

为什么不用"文件存在即可": 8/17 事故是 bridge 写成功而落库被覆盖归零,
  那时的自愈路径正是重跑 generate (BridgeVotingAdapter 把 bridge 候选重新落库)。
  所以必须以**落库事实**为准, 且缺行时必须仍然重跑。

边界 (显式登记, 改配置前先读):
  · 组合闸 (`playbook.portfolio_guardrail_enabled`, 方向偏斜/sector 集中度)
    只在**生成侧** `_apply_portfolio_guardrails` 执行。该闸开启时本判定不得
    生效 —— 否则等于静默摘掉一层风控 (见 `guardrail_blocks_skip`)。
  · 跳过生成 ⇒ 当日不再产生 supply_placeholder 行 (非提名候选占位, 纯研究口径
    消费方: scripts/non_nominated_counterfactual.py / observe_nightly_supply.py)
    且不再由生成侧二次改写未执行 DA 行的 horizon/entry 锚点。DA 行本身
    (方向/入场/SL/TP/size) 不受影响 —— 这些由 DA 直写 + save_instruction 仲裁决定。

回滚: 删本文件 + 还原 generation_flow 的供给判定块 (零 schema 变更; 完成标记
  `source='da_supply'` 行留着无害 —— 只是历史留痕)。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BRIDGE_PATH_TMPL = "/opt/ftseek/FTSeek-US/data/opportunity_scan_{date}.json"
DA_DECISION_PREFIX = "DA-"
DEFAULT_LATEST_START_ET_HHMM = 920  # ET 09:20: 再晚启动必跨开盘 (实测单轮 300-400s)


def bridge_candidate_symbols(trade_date: str) -> Optional[List[str]]:
    """当日 bridge 候选 symbol 列表 (缺失/空/schema 非法 → None, 不抛异常)."""
    from app.playbook.pipeline_parts.bridge_adapters import _validate_bridge_schema
    from app.utils.symbol_utils import normalize_symbol

    try:
        path = BRIDGE_PATH_TMPL.format(date=trade_date)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            data = json.load(f)
        # 过期文件视作缺失 (与 pipeline._try_load_bridge 同判据)
        _inner = str(data.get("trade_date") or "")
        if _inner and _inner != trade_date:
            logger.warning(
                f"[supply_gate] bridge.trade_date={_inner} ≠ {trade_date}, 视作缺失"
            )
            return None
        cands = data.get("candidates") or []
        if not _validate_bridge_schema(cands):
            return None
        syms = [normalize_symbol(c.get("symbol")) for c in cands if c.get("symbol")]
        return syms or None
    except Exception as e:
        logger.warning(f"[supply_gate] bridge 读取失败 (按未完成处理): {e}")
        return None


def _cmp_symbol(value: Any) -> str:
    """归一化用于比对的 symbol 键 (去 US. 前缀)。

    bridge 与 DB 两侧现在是同一形态 ('US.BA'), 但历史数据/外部供给可能写裸 'BA'
    (normalize_symbol 只折叠 US.US. 与别名, 不补前缀) ⇒ 前缀差会让"已落库"被
    误判成 partial。误判方向是**多跑一轮生成**(安全侧), 但会白烧 300s, 故显式剥前缀。
    """
    from app.utils.symbol_utils import normalize_symbol

    s = str(normalize_symbol(value) or "").strip().upper()
    return s[3:] if s.startswith("US.") else s


def da_supply_state(trade_date: str) -> Dict[str, Any]:
    """DA 供给落库状态: complete / partial / absent (+ want/have/missing).

    只读: 不改 DB、不写文件、不注入 pool (与 pipeline._try_load_bridge 的副作用隔离)。
    """
    state: Dict[str, Any] = {"state": "absent", "want": 0, "have": 0, "missing": []}
    wants = bridge_candidate_symbols(trade_date)
    if not wants:
        return state

    from app.playbook.db import PlaybookDB

    rows = PlaybookDB().get_instructions_by_date(trade_date)
    have = set()
    for r in rows:
        # 只认真指令行 + DA 决策号: supply_placeholder 是"未提名"占位, 不能算已落库
        if (r.get("row_kind") or "instruction") != "instruction":
            continue
        if not str(r.get("decision_id") or "").startswith(DA_DECISION_PREFIX):
            continue
        if r.get("symbol"):
            have.add(_cmp_symbol(r["symbol"]))

    want = {_cmp_symbol(s) for s in wants}
    missing = sorted(want - have)
    state.update({
        "want": len(want),
        "have": len(want & have),
        "missing": missing[:8],
        "state": "complete" if not missing else "partial",
    })
    return state


def guardrail_blocks_skip() -> bool:
    """组合闸开启时禁止跳过生成 (该闸只在生成侧执行, 跳过=静默摘风控).

    配置读失败 → True (保守: 宁可多跑一轮生成, 不可静默少一层风控)。
    """
    try:
        from app.playbook.pipeline_parts.portfolio_guardrails import (
            portfolio_guardrail_enabled,
        )
        return bool(portfolio_guardrail_enabled())
    except Exception as e:
        logger.warning(f"[supply_gate] 组合闸开关读取失败 (按开启处理, 不跳过): {e}")
        return True


def latest_generate_start_hhmm() -> int:
    """全量生成最晚启动时点 (ET HHMM). 配置读失败 → 09:20 兜底."""
    try:
        from app.playbook.config import PlaybookConfig
        return int(PlaybookConfig().get(
            "generate_latest_start_et_hhmm", DEFAULT_LATEST_START_ET_HHMM
        ))
    except Exception as e:
        logger.warning(
            f"[supply_gate] 生成截止时点读取失败 (按 "
            f"{DEFAULT_LATEST_START_ET_HHMM} 处理): {e}"
        )
        return DEFAULT_LATEST_START_ET_HHMM