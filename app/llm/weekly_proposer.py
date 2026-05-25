#!/usr/bin/env python3
"""
FTSeek-US 周度假设生成器 — LLM 角色重定位核心组件

职责:
1. 每周汇总所有 trade_attribution + lessons
2. 调 LLM 生成 1-3 个参数调整提议 (ImprovementProposal)
3. 提议写入 proposals.db，等待回测验证

2026-05-13: 初始实现
"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger("ftseek_us.llm.weekly_proposer")


@dataclass
class ImprovementProposal:
    """参数改进提议"""
    proposal_id: str = ""              # "prop-YYYYMMDD-HHMM"
    proposer: str = "llm:weekly"       # "llm:weekly" / "rule:low_win_rate" / "human"
    target_param: str = ""             # "day_trade_mode.take_profit.tier_1.profit_pct"
    strategy_type: str = "main"        # "main" or "scalping"
    current_value: Any = None
    proposed_value: Any = None
    rationale: str = ""                # 为什么这么改
    evidence: List[str] = field(default_factory=list)  # trade_ids 作证据
    expected_improvement: str = ""     # 预期改善 (量化)
    failure_hypothesis: str = ""       # 失败假设
    confidence: float = 0.0            # 提议者自评置信度
    status: str = "pending"            # pending / validated / rejected / canary / promoted
    created_at: str = ""
    validated_at: str = ""
    backtest_result: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.proposal_id:
            self.proposal_id = f"prop-{datetime.now().strftime('%Y%m%d-%H%M')}"
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


# ─────────────────────────────────────────────────────────────
#  Prompt 模板
# ─────────────────────────────────────────────────────────────

from app.llm.prompts.weekly import WEEKLY_PROPOSER_SYSTEM_PROMPT, WEEKLY_PROPOSER_USER_PROMPT



class WeeklyProposer:
    """
    周度假设生成器
    
    用法:
        proposer = WeeklyProposer(llm_client)
        proposals = proposer.generate_weekly_proposals()
        for p in proposals:
            pipeline.submit(p)
    """

    def __init__(self, llm_client=None, model="deepseek"):
        self.llm_client = llm_client
        self.proposal_model = model  # 默认 deepseek (推理强)

    def generate_weekly_proposals(
        self,
        days: int = 7,
    ) -> List[ImprovementProposal]:
        """
        生成本周的参数调整提议
        
        Args:
            days: 回看天数 (默认 7)
        """
        start = time.time()
        
        # 收集数据
        trade_summary = self._get_trade_summary(days)
        systemic_issues = self._get_systemic_issues(days)
        current_params = self._get_current_params()
        
        if not trade_summary or trade_summary.get("n_trades", 0) < 3:
            logger.info(f"[周度假设] 本周交易不足 3 笔，跳过提议生成")
            return []
        
        # 构建 prompt
        exit_breakdown = "\n".join(
            f"  {r['exit_reason']}: {r['count']} 笔, 胜率 {r['win_rate']:.0%}, 平均 PnL {r['avg_pnl']:+.1%}"
            for r in trade_summary.get("exit_reasons", [])
        )
        
        issue_text = "\n".join(
            f"  [{issue['count']}x] {issue['systemic_pattern'] or issue['key_decision_factor']}"
            for issue in systemic_issues[:5]
        ) if systemic_issues else "  无"
        
        user_prompt = WEEKLY_PROPOSER_USER_PROMPT.format(
            n_trades=trade_summary.get("n_trades", 0),
            win_rate=trade_summary.get("win_rate", 0),
            total_pnl=trade_summary.get("total_pnl", 0),
            avg_hold_hours=trade_summary.get("avg_hold_hours", 0),
            exit_reason_breakdown=exit_breakdown,
            systemic_issues=issue_text,
            atr_mult=current_params.get("atr_mult", 1.5),
            max_loss=current_params.get("max_loss", 3.0),
            tp1=current_params.get("tp1", 4.0),
            tp2=current_params.get("tp2", 8.0),
            chandelier_atr=current_params.get("chandelier_atr", 1.2),
            min_conf=current_params.get("min_conf", 0.6),
        )
        
        proposals = []

        # 2026-05-17: 迁移到 model_call 统一入口 (盘后, 不影响盘中)
        # 路由: yaml `llm.features.weekly_proposer` = {primary: deepseek-pro, fallback: qwen-plus}
        # llm_client 旧参数已不再使用 (model_call 自走 yaml 路由)
        try:
            from app.llm.model_call import call_llm as _call_llm
            res = _call_llm(
                "weekly_proposer",
                user_prompt,
                system=WEEKLY_PROPOSER_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=1000,
            )
            response = res.response if (res and res.success and isinstance(res.response, str)) else None
            proposals = self._parse_proposals(response)

            if res and res.success:
                logger.info(
                    f"[周度假设] 生成 {len(proposals)} 个提议 "
                    f"(model={res.model_used}, latency={int(res.latency_ms)}ms, "
                    f"fallback={res.fallback_used})"
                )
            else:
                logger.warning(f"[周度假设] LLM 失败: {res.error if res else 'unknown'}")
        except Exception as e:
            logger.error(f"[周度假设] LLM 调用异常: {e}")

        return proposals

    def _parse_proposals(self, response: str) -> List[ImprovementProposal]:
        """解析 LLM 输出为 ImprovementProposal 列表"""
        if not response:
            return []
        
        # 尝试直接解析
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [ImprovementProposal(**{k: v for k, v in item.items() if k in ImprovementProposal.__dataclass_fields__})
                        for item in data]
        except (json.JSONDecodeError, TypeError):
            pass
        
        # 尝试提取 JSON 块
        import re
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, list):
                    return [ImprovementProposal(**{k: v for k, v in item.items() if k in ImprovementProposal.__dataclass_fields__})
                            for item in data]
            except json.JSONDecodeError:
                pass
        
        return []

    def _get_trade_summary(self, days: int) -> Dict:
        """获取最近 N 天交易汇总"""
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            
            db_path = get_trade_records_db()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 总体统计
            cursor.execute(
                """SELECT COUNT(*) as n,
                          AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_rate,
                          SUM(pnl) as total_pnl,
                          AVG(holding_hours) as avg_hold_hours
                   FROM trade_attribution 
                   WHERE entry_time > date('now', '-{} days')""".format(days)
            )
            row = cursor.fetchone()
            if not row or row["n"] == 0:
                conn.close()
                return {}
            
            summary = dict(row)
            summary["win_rate"] = summary["win_rate"] or 0
            
            # 按退出原因分布
            cursor.execute(
                """SELECT exit_reason,
                          COUNT(*) as count,
                          AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_rate,
                          AVG(pnl) as avg_pnl
                   FROM trade_attribution 
                   WHERE entry_time > date('now', '-{} days')
                     AND exit_reason IS NOT NULL
                   GROUP BY exit_reason
                   ORDER BY count DESC""".format(days)
            )
            summary["exit_reasons"] = [dict(r) for r in cursor.fetchall()]
            
            conn.close()
            return summary
            
        except Exception as e:
            logger.error(f"[周度假设] 获取交易汇总失败: {e}")
            return {}

    def _get_systemic_issues(self, days: int) -> List[Dict]:
        """获取系统性问题"""
        try:
            from app.llm.attribution_engine import get_attribution_engine
            engine = get_attribution_engine()
            return engine.get_systemic_issues(days)
        except Exception as e:
            logger.error(f"[周度假设] 获取系统性问题失败: {e}")
            return []

    def _get_current_params(self) -> Dict:
        """获取当前关键参数值"""
        try:
            import yaml
            from pathlib import Path
            from app.core.config_container import get_config
            cfg = get_config()
            risk = cfg.get_section("risk", {}).get("signal_quality", {})
            return {
                "min_conf": risk.get("min_confidence", 0.6),
            }
        except Exception:
            pass
        
        try:
            # 从 exit_policy.yaml 读
            ep_path = get_exit_policy_path()
            if ep_path.exists():
                with open(ep_path) as f:
                    ep = yaml.safe_load(f)
                dtm = ep.get("day_trade_mode", {})
                return {
                    "atr_mult": dtm.get("initial_stop", {}).get("atr_multiplier", 1.5),
                    "max_loss": dtm.get("initial_stop", {}).get("max_loss_pct", 3.0),
                    "tp1": dtm.get("take_profit", {}).get("tier_1", {}).get("profit_pct", 4.0),
                    "tp2": dtm.get("take_profit", {}).get("tier_2", {}).get("profit_pct", 8.0),
                    "chandelier_atr": ep.get("exit_rules", {}).get("chandelier_trail", {}).get("activation_profit_atr", 1.2),
                    "min_conf": 0.6,
                }
        except Exception:
            pass
        
        return {
            "atr_mult": 1.5, "max_loss": 3.0, "tp1": 4.0, "tp2": 8.0,
            "chandelier_atr": 1.2, "min_conf": 0.6,
        }


# ─────────────────────────────────────────────────────────────
#  全局单例
# ─────────────────────────────────────────────────────────────

_weekly_proposer: Optional[WeeklyProposer] = None

def get_weekly_proposer(llm_client=None, model="deepseek") -> WeeklyProposer:
    global _weekly_proposer
    if _weekly_proposer is None:
        _weekly_proposer = WeeklyProposer(llm_client=llm_client, model=model)
    return _weekly_proposer
