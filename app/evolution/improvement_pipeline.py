#!/usr/bin/env python3
"""
FTSeek-US 改进流水线 — 提议 → 验证 → 部署

设计原则:
1. 任何参数调整都必须经过流水线
2. LLM 提议 → 影子验证 → 全量部署 (Phase 1)
3. 提议者可来自 LLM、规则触发、或人工

2026-05-13: 增强版
- 引入 PromotionEngine 进行统计学决策
- 限制并发影子测试数量 (Bonferroni 校正)
- 支持 Rollback Shadow (切换后旧参数继续观察)
"""
import json
import logging
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

from app.evolution.promotion_engine import PromotionEngine

logger = logging.getLogger("ftseek_us.evolution.pipeline")


@dataclass
class ProposalRecord:
    """提议记录 (持久化到 proposals.db)"""
    proposal_id: str
    proposer: str                  # "llm:eod_review" / "rule:low_win_rate" / "human"
    target_param: str
    current_value: Any
    proposed_value: Any
    rationale: str
    evidence: List[str]
    expected_improvement: str
    failure_hypothesis: str
    confidence: float
    status: str                    # pending / testing / promoted / rejected / rollback_shadow
    created_at: str
    approved_at: str = ""
    approved_by: str = ""
    rejected_reason: str = ""
    backtest_result: str = ""      # JSON
    
    # 影子测试字段
    test_start_date: str = ""
    test_days_count: int = 0
    shadow_pnl_total: float = 0.0
    shadow_win_count: float = 0.0
    shadow_total_count: float = 0.0
    test_mode: str = "level3"      # level2 (exit) or level3 (full)
    rollback_until: str = ""       # 观察截止日期
    strategy_type: str = "main"
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class ImprovementPipeline:
    """
    改进流水线
    
    用法:
        pipeline = ImprovementPipeline()
        pipeline.submit(proposal)
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(
            Path(__file__).resolve().parent.parent.parent / "data" / "proposals.db"
        )
        self._ensure_schema()

    def _ensure_schema(self):
        """确保 proposals 表存在，包含影子测试字段"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    proposer TEXT,
                    target_param TEXT,
                    current_value TEXT,
                    proposed_value TEXT,
                    rationale TEXT,
                    evidence TEXT,
                    expected_improvement TEXT,
                    failure_hypothesis TEXT,
                    confidence REAL,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    approved_at TEXT,
                    approved_by TEXT,
                    rejected_reason TEXT,
                    backtest_result TEXT,
                    -- 影子测试字段 (2026-05-13 新增)
                    test_start_date TEXT,
                    test_days_count INTEGER DEFAULT 0,
                    shadow_pnl_total REAL DEFAULT 0.0,
                    shadow_win_count REAL DEFAULT 0.0,
                    shadow_total_count REAL DEFAULT 0.0,
                    test_mode TEXT DEFAULT 'level3',
                    rollback_until TEXT,
                    strategy_type TEXT DEFAULT 'main'
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[Pipeline] schema 创建失败: {e}")

    def submit(self, proposal: Any) -> str:
        """提交提议到流水线"""
        try:
            # 检查并发影子数量 (Trap 4: 多重检验校正)
            active_shadows = self._count_active_shadows()
            if active_shadows >= PromotionEngine.MAX_ACTIVE_SHADOWS:
                logger.warning(f"[Pipeline] 拒绝提议: 并发影子已达上限 ({active_shadows})")
                return ""

            conn = sqlite3.connect(self.db_path)
            
            # 支持 ImprovementProposal 和 dict
            if hasattr(proposal, 'proposal_id'):
                from app.llm.weekly_proposer import ImprovementProposal
                if isinstance(proposal, ImprovementProposal):
                    proposal = ProposalRecord(
                        proposal_id=proposal.proposal_id,
                        proposer=proposal.proposer,
                        target_param=proposal.target_param,
                        strategy_type=getattr(proposal, 'strategy_type', 'main'),
                        current_value=str(proposal.current_value),
                        proposed_value=str(proposal.proposed_value),
                        rationale=proposal.rationale,
                        evidence=json.dumps(proposal.evidence),
                        expected_improvement=proposal.expected_improvement,
                        failure_hypothesis=proposal.failure_hypothesis,
                        confidence=proposal.confidence,
                        status="pending",
                    )
            elif isinstance(proposal, dict):
                proposal = ProposalRecord(**{
                    k: v for k, v in proposal.items()
                    if k in ProposalRecord.__dataclass_fields__
                })
            
            if not isinstance(proposal, ProposalRecord):
                raise ValueError(f"不支持的提议类型: {type(proposal)}")
            
            conn.execute(
                """INSERT OR REPLACE INTO proposals
                   (proposal_id, proposer, target_param, current_value, proposed_value,
                    rationale, evidence, expected_improvement, failure_hypothesis,
                    confidence, status, created_at, strategy_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal.proposal_id,
                    proposal.proposer,
                    proposal.target_param,
                    json.dumps(proposal.current_value, ensure_ascii=False) if not isinstance(proposal.current_value, str) else proposal.current_value,
                    json.dumps(proposal.proposed_value, ensure_ascii=False) if not isinstance(proposal.proposed_value, str) else proposal.proposed_value,
                    proposal.rationale,
                    json.dumps(proposal.evidence) if isinstance(proposal.evidence, list) else proposal.evidence,
                    proposal.expected_improvement,
                    proposal.failure_hypothesis,
                    proposal.confidence,
                    proposal.status,
                    proposal.created_at,
                    proposal.strategy_type,
                )
            )
            conn.commit()
            conn.close()
            
            logger.info(f"[Pipeline] 提议已提交: {proposal.proposal_id} ({proposal.target_param})")
            
            # REMOVED (2026-06-19): ShadowTracker 变体测试不再使用
            # 改用 Playbook shadow 模式做策略验证
            
            return proposal.proposal_id
            
        except Exception as e:
            logger.error(f"[Pipeline] 提交失败: {e}")
            return ""

    def _count_active_shadows(self) -> int:
        """获取当前正在测试的影子数量"""
        try:
            conn = sqlite3.connect(self.db_path)
            count = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='testing'").fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0
