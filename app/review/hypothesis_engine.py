"""
假设驱动进化引擎 (Hypothesis-Driven Evolution Engine)

借鉴 RD-Agent 的核心架构：
  Hypothesis → Experiment → Execution → Feedback → Confirm/Refute → Knowledge

核心概念：
1. Hypothesis (假设): 从历史交易中生成的可测试假设
   例: "HK.02269 午后(RSI>70)追高买入胜率 < 30%"
2. Evidence (证据): 实盘中支持或反驳假设的交易记录
3. Status: pending → verifying (需要≥3次证据) → confirmed / refuted
4. Knowledge: confirmed 的假设 → 转化为盘前门禁规则

价值公式: 假设的价值 = 证据数量 × 影响幅度 × 置信度
"""

import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from collections import defaultdict

logger = logging.getLogger("ftseek.review.hypothesis_engine")

DB_PATH = '/opt/ftseek/FTSeek/data/hypothesis.db'


class HypothesisStatus:
    PENDING = "pending"       # 新生成，等待验证
    VERIFYING = "verifying"   # 正在收集证据
    CONFIRMED = "confirmed"   # 证据充分确认 → 转化为规则
    REFUTED = "refuted"       # 证据反驳 → 废弃


@dataclass
class Hypothesis:
    """可测试的交易假设（对标 RD-Agent 的 Hypothesis）"""
    id: str = ""
    statement: str = ""          # 假设陈述: "HK.02269 午后 RSI>70 追高胜率<30%"
    source: str = ""             # 来源: "pattern_mining" / "eod_review" / "llm_analysis"
    category: str = ""           # 分类: "stock_time" / "signal_quality" / "stop_loss" / "strategy" / "market_regime"
    trigger_conditions: Dict = field(default_factory=dict)  # 触发条件: {"code": "HK.02269", "time": "afternoon", "RSI": ">70"}
    predicted_outcome: str = ""  # 预测结果: "胜率<30%, 平均亏损>2%"
    evidence_count: int = 0      # 当前证据数
    confirm_count: int = 0       # 支持次数
    refute_count: int = 0        # 反驳次数
    confidence: float = 0.5      # 置信度 0-1
    status: str = HypothesisStatus.PENDING
    created_at: str = ""
    updated_at: str = ""
    confirmed_at: Optional[str] = None
    refuted_at: Optional[str] = None
    # 转化为规则后的字段
    actionable_rule: str = ""    # confirmed 后的可执行规则
    rule_type: str = ""          # "block" / "warn" / "adjust_param"
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'Hypothesis':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class HypothesisDB:
    """假设持久化层"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS hypotheses (
            id TEXT PRIMARY KEY,
            statement TEXT,
            source TEXT,
            category TEXT,
            trigger_conditions TEXT,
            predicted_outcome TEXT,
            evidence_count INTEGER DEFAULT 0,
            confirm_count INTEGER DEFAULT 0,
            refute_count INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0.5,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            updated_at TEXT,
            confirmed_at TEXT,
            refuted_at TEXT,
            actionable_rule TEXT,
            rule_type TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_status ON hypotheses(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_category ON hypotheses(category)")
        conn.commit()
        conn.close()
    
    def save(self, h: Hypothesis):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO hypotheses 
            (id, statement, source, category, trigger_conditions, predicted_outcome,
             evidence_count, confirm_count, refute_count, confidence, status,
             created_at, updated_at, confirmed_at, refuted_at, actionable_rule, rule_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            h.id, h.statement, h.source, h.category,
            json.dumps(h.trigger_conditions, ensure_ascii=False),
            h.predicted_outcome, h.evidence_count, h.confirm_count, h.refute_count,
            h.confidence, h.status, h.created_at, h.updated_at,
            h.confirmed_at, h.refuted_at, h.actionable_rule, h.rule_type
        ))
        conn.commit()
        conn.close()
    
    def load_all(self, status: Optional[str] = None) -> List[Hypothesis]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if status:
            c.execute("SELECT * FROM hypotheses WHERE status = ?", (status,))
        else:
            c.execute("SELECT * FROM hypotheses")
        rows = c.fetchall()
        conn.close()
        return [Hypothesis(
            id=r[0], statement=r[1], source=r[2], category=r[3],
            trigger_conditions=json.loads(r[4]) if r[4] else {},
            predicted_outcome=r[5], evidence_count=r[6], confirm_count=r[7],
            refute_count=r[8], confidence=r[9], status=r[10],
            created_at=r[11], updated_at=r[12], confirmed_at=r[13], refuted_at=r[14],
            actionable_rule=r[15], rule_type=r[16]
        ) for r in rows]
    
    def load_active(self) -> List[Hypothesis]:
        """加载 pending + verifying 的假设（需要继续验证的）"""
        return self.load_all(status=HypothesisStatus.PENDING) + \
               self.load_all(status=HypothesisStatus.VERIFYING)
    
    def load_confirmed(self) -> List[Hypothesis]:
        return self.load_all(status=HypothesisStatus.CONFIRMED)


class HypothesisEngine:
    """假设驱动进化引擎 — 核心进化循环
    
    对标 RD-Agent:
      Hypothesis → Experiment → Execution → Feedback → Confirm/Refute → Knowledge
    
    FTSeek 映射:
      Hypothesis: 从 PatternMiner/EOD 生成的交易假设
      Experiment: 次日实盘中观察该假设场景
      Execution: 实际交易发生（或门禁拦截）
      Feedback: 交易结果 vs 预测
      Confirm/Refute: 更新证据计数 → 状态流转
      Knowledge: confirmed → 转化为盘中门禁规则
    """
    
    CONFIRM_THRESHOLD = 5   # 需要 5 次证据确认
    REFUTE_THRESHOLD = 3    # 3 次反驳即推翻
    VERIFYING_THRESHOLD = 3 # 3 次证据进入 verifying
    
    def __init__(self):
        self.db = HypothesisDB()
    
    def generate_from_patterns(self, patterns: List[Dict]) -> List[Hypothesis]:
        """从 PatternMiner 的模式生成可测试假设（对标 RD-Agent Hypothesis Generation）"""
        hypotheses = []
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        for i, p in enumerate(patterns):
            h = Hypothesis(
                id=f"hypo_{datetime.now().strftime('%Y%m%d')}_{i}",
                statement=p.get('pattern', ''),
                source='pattern_mining',
                category=p.get('category', 'general'),
                trigger_conditions={
                    'conditions': p.get('trigger_conditions', []),
                },
                predicted_outcome=f"胜率{p.get('win_rate_when_triggered', 0):.0%}, 平均PnL={p.get('avg_pnl', 0):.0f}",
                evidence_count=p.get('evidence_count', 0),
                confidence=1.0 - p.get('win_rate_when_triggered', 0.5),
                status=HypothesisStatus.VERIFYING if p.get('evidence_count', 0) >= self.VERIFYING_THRESHOLD else HypothesisStatus.PENDING,
                created_at=now,
                updated_at=now,
                actionable_rule=p.get('actionable_rule', ''),
                rule_type=self._infer_rule_type(p),
            )
            hypotheses.append(h)
            self.db.save(h)
        
        logger.info(f"[HypothesisEngine] 从 {len(patterns)} 个模式生成 {len(hypotheses)} 个假设")
        return hypotheses
    
    def evaluate_trade(self, trade: Dict) -> Dict[str, Any]:
        """评估一笔交易对活跃假设的影响（对标 RD-Agent Feedback Collection）
        
        返回: {matched_hypotheses: [...], updates: [...]}
        """
        code = trade.get('code', '')
        pnl = trade.get('pnl', 0) or 0
        exit_reason = trade.get('exit_reason', '')
        is_loss = pnl < 0
        
        active = self.db.load_active()
        if not active:
            return {'matched': 0, 'updates': []}
        
        updates = []
        for h in active:
            # 检查交易是否匹配假设的触发条件
            if not self._matches_hypothesis(trade, h):
                continue
            
            # 证据更新
            h.evidence_count += 1
            h.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 判断支持还是反驳
            if self._supports_hypothesis(trade, h):
                h.confirm_count += 1
                evidence = "支持"
            else:
                h.refute_count += 1
                evidence = "反驳"
            
            # 状态流转
            old_status = h.status
            if h.refute_count >= self.REFUTE_THRESHOLD:
                h.status = HypothesisStatus.REFUTED
                h.refuted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"[HypothesisEngine] 假设被推翻 [{h.id[:12]}]: {h.statement[:60]}... "
                           f"(支持{h.confirm_count}/反驳{h.refute_count})")
            elif h.confirm_count >= self.CONFIRM_THRESHOLD and h.status != HypothesisStatus.CONFIRMED:
                h.status = HypothesisStatus.CONFIRMED
                h.confirmed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                h.confidence = min(0.95, h.confidence + 0.1)
                logger.info(f"[HypothesisEngine] ✅ 假设已确认 [{h.id[:12]}]: {h.statement[:60]}... "
                           f"(支持{h.confirm_count}/反驳{h.refute_count}) → 规则: {h.actionable_rule}")
            elif h.evidence_count >= self.VERIFYING_THRESHOLD and h.status == HypothesisStatus.PENDING:
                h.status = HypothesisStatus.VERIFYING
                logger.info(f"[HypothesisEngine] 假设进入验证 [{h.id[:12]}]: {h.statement[:60]}...")
            
            self.db.save(h)
            updates.append({
                'hypothesis_id': h.id,
                'old_status': old_status,
                'new_status': h.status,
                'evidence': evidence,
                'confirm_count': h.confirm_count,
                'refute_count': h.refute_count,
            })
        
        return {'matched': len(updates), 'updates': updates}
    
    def get_confirmed_rules(self) -> List[Dict]:
        """获取已确认的规则（用于盘中门禁）"""
        confirmed = self.db.load_confirmed()
        return [
            {
                'rule': h.actionable_rule,
                'type': h.rule_type,
                'trigger': h.trigger_conditions,
                'confidence': h.confidence,
                'evidence': f"支持{h.confirm_count}/反驳{h.refute_count} (总{h.evidence_count}次)",
                'statement': h.statement,
            }
            for h in confirmed
        ]
    
    def get_evolution_summary(self) -> Dict:
        """获取进化状态摘要"""
        all_h = self.db.load_all()
        return {
            'total': len(all_h),
            'pending': len([h for h in all_h if h.status == HypothesisStatus.PENDING]),
            'verifying': len([h for h in all_h if h.status == HypothesisStatus.VERIFYING]),
            'confirmed': len([h for h in all_h if h.status == HypothesisStatus.CONFIRMED]),
            'refuted': len([h for h in all_h if h.status == HypothesisStatus.REFUTED]),
            'confirmed_rules': self.get_confirmed_rules(),
        }
    
    # ── 内部方法 ──
    
    def _matches_hypothesis(self, trade: Dict, h: Hypothesis) -> bool:
        """检查交易是否匹配假设的触发条件"""
        trigger = h.trigger_conditions.get('conditions', [])
        if not trigger:
            return False
        
        code = trade.get('code', '')
        strategy = trade.get('strategy', '').lower()
        exit_reason = (trade.get('exit_reason', '') or '').lower()
        pnl = trade.get('pnl', 0) or 0
        statement = h.statement.lower()
        category = h.category.lower()
        
        # 1. 股票匹配：假设 statement 中包含股票代码
        if code and code in statement:
            return True
        
        # 2. 策略匹配：假设 statement 中包含策略名
        if strategy and strategy in statement:
            return True
        
        # 3. 止损类假设：匹配中英文 exit_reason
        #    中文: 止损 | 英文: stop_loss, stop loss, trailing_stop, risk_control
        if '止损' in statement or category == 'stop_loss':
            stop_reasons = ['止损', 'stop_loss', 'stop loss', 'trailing_stop', 'risk_control', 'sl']
            if any(sr in exit_reason for sr in stop_reasons):
                return True
        
        # 4. 市场状态类假设（恒指/大盘/宏观）
        if category == 'market_regime' or any(w in statement for w in ['恒指', '大盘', 'hsi', '市场', 'macro']):
            return True
        
        return False
    
    def _supports_hypothesis(self, trade: Dict, h: Hypothesis) -> bool:
        """判断交易结果是支持还是反驳假设"""
        pnl = trade.get('pnl', 0) or 0
        is_loss = pnl < 0
        statement = h.statement.lower()
        category = h.category.lower()
        exit_reason = (trade.get('exit_reason', '') or '').lower()
        
        # 止损类：任何止损触发都支持"止损太紧"假设
        #    匹配中英文: 止损, stop_loss, trailing_stop, risk_control
        if category == 'stop_loss' or '止损' in statement:
            stop_reasons = ['止损', 'stop_loss', 'stop loss', 'trailing_stop', 'risk_control', 'sl']
            if any(sr in exit_reason for sr in stop_reasons):
                return True
        
        # 市场状态类：恒指跌+亏损 → 支持；恒指跌+盈利 → 反驳
        if category == 'market_regime' or any(w in statement for w in ['恒指', '大盘', 'hsi']):
            return is_loss
        
        # 假设预测"会亏"/"胜率低"，实际亏了 → 支持
        if any(w in statement for w in ['亏损', '亏', '输', 'loss', '低胜', '失败']):
            return is_loss
        
        # 假设预测"会赚"/"有效"/"高胜"，实际赚了 → 支持
        if any(w in statement for w in ['盈利', '赚', 'win', 'profit', '高胜', '有效']):
            return not is_loss
        
        # 默认：亏损支持亏损类假设
        return is_loss
    
    def _infer_rule_type(self, pattern: Dict) -> str:
        """从模式推断规则类型"""
        action = pattern.get('suggested_action', '')
        severity = pattern.get('severity', '')
        
        if action == 'avoid' or severity == 'HIGH':
            return 'block'
        elif action == 'reduce_size':
            return 'warn'
        elif action == 'adjust_sl':
            return 'adjust_param'
        else:
            return 'warn'


def get_hypothesis_engine() -> HypothesisEngine:
    return HypothesisEngine()
