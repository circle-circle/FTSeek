#!/usr/bin/env python3
"""
FTSeek-US LLM 归因模块 — LLM 角色重定位核心实现

设计原则:
1. LLM 做归因 (attribution)，不做决策 (decision)
2. 每笔已平仓交易单独归因，输出结构化 JSON
3. 归因结果写入 trade_attribution.llm_insight
4. 为后续 ImprovementProposal 提供输入

LLM 在新架构中的角色:
- 核心: 归因 + 假设生成
- 不做: 直接生成 BattlePlan / 直接修改配置
- 辅助: 解读非结构化信息

2026-05-13: 初始实现
"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger("ftseek_us.llm.attribution")


@dataclass
class TradeAttribution:
    """单笔交易的 LLM 归因结果"""
    trace_id: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    entry_time: str
    exit_time: str
    # LLM 输出
    key_decision_factor: str = ""        # 入场时机/退出时机/仓位大小/市场环境
    should_have_done_differently: bool = False
    specific_suggestion: str = ""        # 可量化的具体建议
    is_systemic_issue: bool = False
    systemic_pattern: str = ""           # 系统性问题模式描述
    confidence: float = 0.0
    # 元数据
    llm_model: str = ""
    attribution_time: float = 0.0
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


# ─────────────────────────────────────────────────────────────
#  Prompt 模板
# ─────────────────────────────────────────────────────────────

from app.llm.prompts.attribution import ATTRIBUTION_SYSTEM_PROMPT, ATTRIBUTION_USER_PROMPT



class LLMTAttributionEngine:
    """
    LLM 逐笔归因引擎
    
    用法:
        engine = LLMTAttributionEngine()
        result = engine.attribute_trade(trade_data, context)
        engine.save(result)
    """

    def __init__(self, llm_client=None, model="qwen"):
        # 2026-05-13: 支持 MultiModelClient
        self.llm_client = llm_client
        self.attribution_model = model  # 默认 qwen (综合归因)
        self._db_path = None

    def attribute_trade(
        self,
        trade: Dict[str, Any],
        market_context: Optional[Dict] = None,
    ) -> TradeAttribution:
        """
        对单笔已平仓交易进行 LLM 归因
        
        Args:
            trade: {symbol, direction, entry_price, exit_price, pnl_pct, 
                    exit_reason, entry_time, exit_time, factor_detail, trace_id}
            market_context: {vix, spy_chg, session, regime}
        
        RFC-050 (P1-3): 改用 call_llm 统一接口，不再依赖 llm_client 实例
        """
        start = time.time()
        
        # 构建 prompt
        factor_detail = json.dumps(
            trade.get("factor_detail", {}), 
            ensure_ascii=False
        )
        ctx = market_context or {}
        
        user_prompt = ATTRIBUTION_USER_PROMPT.format(
            symbol=trade.get("symbol", "N/A"),
            direction=trade.get("direction", "LONG"),
            entry_price=trade.get("entry_price", 0),
            exit_price=trade.get("exit_price", 0),
            pnl_pct=trade.get("pnl_pct", 0),
            exit_reason=trade.get("exit_reason", "unknown"),
            entry_time=trade.get("entry_time", "N/A"),
            exit_time=trade.get("exit_time", "N/A"),
            factor_detail=factor_detail,
            vix=ctx.get("vix", "N/A"),
            spy_chg=ctx.get("spy_chg", 0),
            session=ctx.get("session", "N/A"),
            regime=ctx.get("regime", "N/A"),
        )

        # 构建归因结果骨架
        attribution = TradeAttribution(
            trace_id=trade.get("trace_id", ""),
            symbol=trade.get("symbol", ""),
            direction=trade.get("direction", ""),
            entry_price=trade.get("entry_price", 0),
            exit_price=trade.get("exit_price", 0),
            pnl_pct=trade.get("pnl_pct", 0),
            exit_reason=trade.get("exit_reason", ""),
            entry_time=trade.get("entry_time", ""),
            exit_time=trade.get("exit_time", ""),
        )

        # RFC-050: 使用 call_llm 统一接口
        try:
            from app.llm.model_call import call_llm
            res = call_llm(
                "attribution_engine",
                user_prompt,
                system=ATTRIBUTION_SYSTEM_PROMPT,
                temperature=0.1,
                max_tokens=500,
            )
            response = res.response if res and res.success else None
            
            # 解析 JSON 输出
            llm_output = self._parse_llm_json(response) if response else None
            if llm_output:
                attribution.key_decision_factor = llm_output.get("key_decision_factor", "")
                attribution.should_have_done_differently = llm_output.get("should_have_done_differently", False)
                attribution.specific_suggestion = llm_output.get("specific_suggestion", "")
                attribution.is_systemic_issue = llm_output.get("is_systemic_issue", False)
                attribution.systemic_pattern = llm_output.get("systemic_pattern", "")
                attribution.confidence = float(llm_output.get("confidence", 0.0))
                attribution.llm_model = res.model_used if res else "unknown"
                
                logger.info(
                    f"[LLM归因] {trade.get('symbol')} pnl={trade.get('pnl_pct'):+.1%} "
                    f"factor={attribution.key_decision_factor} "
                    f"systemic={attribution.is_systemic_issue}"
                )
            else:
                logger.warning(f"[LLM归因] {trade.get('symbol')} JSON 解析失败")
                
        except Exception as e:
            logger.error(f"[LLM归因] {trade.get('symbol')} 调用失败: {e}")

        attribution.attribution_time = time.time() - start
        return attribution

    def _parse_llm_json(self, response: str) -> Optional[Dict]:
        """从 LLM 输出中提取 JSON"""
        if not response:
            return None
        
        # 尝试直接解析
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        
        # 尝试提取 JSON 块
        import re
        match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        
        # 尝试提取代码块中的 JSON
        match = re.search(r'```(?:json)?\s*\n(.*?)\n```', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        
        return None

    def save(self, attribution: TradeAttribution) -> bool:
        """保存归因结果到 trade_attribution 表"""
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            
            db_path = get_trade_records_db()
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            # 确保列存在
            try:
                cursor.execute("ALTER TABLE trade_attribution ADD COLUMN llm_insight TEXT")
                conn.commit()
            except Exception:
                pass  # 列已存在
            
            cursor.execute(
                """UPDATE trade_attribution 
                   SET llm_insight = ?, 
                       key_decision_factor = ?,
                       should_have_done_differently = ?,
                       specific_suggestion = ?,
                       is_systemic_issue = ?,
                       systemic_pattern = ?,
                       attribution_confidence = ?
                   WHERE trace_id = ?""",
                (
                    json.dumps(asdict(attribution), ensure_ascii=False),
                    attribution.key_decision_factor,
                    int(attribution.should_have_done_differently),
                    attribution.specific_suggestion,
                    int(attribution.is_systemic_issue),
                    attribution.systemic_pattern,
                    attribution.confidence,
                    attribution.trace_id,
                )
            )
            
            if cursor.rowcount == 0:
                # trace_id 不存在，INSERT
                try:
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN key_decision_factor TEXT")
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN should_have_done_differently INTEGER")
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN specific_suggestion TEXT")
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN is_systemic_issue INTEGER")
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN systemic_pattern TEXT")
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN attribution_confidence REAL")
                    conn.commit()
                except Exception:
                    pass
                
                cursor.execute(
                    """INSERT OR IGNORE INTO trade_attribution 
                       (trace_id, symbol, direction, pnl, exit_reason, 
                        llm_insight, key_decision_factor, should_have_done_differently,
                        specific_suggestion, is_systemic_issue, systemic_pattern, attribution_confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attribution.trace_id,
                        attribution.symbol,
                        attribution.direction,
                        attribution.pnl_pct,
                        attribution.exit_reason,
                        json.dumps(asdict(attribution), ensure_ascii=False),
                        attribution.key_decision_factor,
                        int(attribution.should_have_done_differently),
                        attribution.specific_suggestion,
                        int(attribution.is_systemic_issue),
                        attribution.systemic_pattern,
                        attribution.confidence,
                    )
                )
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"[LLM归因] 保存失败: {e}")
            return False

    def get_systemic_issues(self, days: int = 7) -> List[Dict]:
        """获取最近 N 天的系统性问题汇总"""
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            
            db_path = get_trade_records_db()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute(
                """SELECT symbol, exit_reason, key_decision_factor, 
                          specific_suggestion, systemic_pattern, pnl,
                          COUNT(*) as count
                   FROM trade_attribution 
                   WHERE is_systemic_issue = 1 
                     AND entry_time > date('now', '-{} days')
                   GROUP BY systemic_pattern, symbol
                   ORDER BY count DESC""",
                days,
            )
            
            results = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return results
            
        except Exception as e:
            logger.error(f"[LLM归因] 查询系统性问题失败: {e}")
            return []


# ─────────────────────────────────────────────────────────────
#  全局单例
# ─────────────────────────────────────────────────────────────

_attribution_engine: Optional[LLMTAttributionEngine] = None

def get_attribution_engine(llm_client=None, model="qwen") -> LLMTAttributionEngine:
    global _attribution_engine
    if _attribution_engine is None:
        _attribution_engine = LLMTAttributionEngine(llm_client=llm_client, model=model)
    return _attribution_engine
