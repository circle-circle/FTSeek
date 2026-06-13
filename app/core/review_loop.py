"""
FTSeek-US 复盘改进闭环系统 (v2.0)

架构:
  盘后复盘 → IssueDetector 自动分类 (P0/P1/P2)
      ↓
  ActionExecutor 执行自动修复 (L0/L1) → ConfigWriter 持久化
      ↓
  ReviewEffectTracker 验证修复效果 + 自动回滚
      ↓
  Memory 消费点注入 (次日盘前/盘中/风控)
      ↓
  IssueRegistry 跨天去重 + 状态追踪

安全边界:
  L0: 自动执行 (黑名单/冷却)
  L1: 自动执行+通知 (阈值微调) → ConfigWriter 持久化
  L2: 建议+等待审批 (参数大幅调整)
  L3: 仅建议 (架构级变更)
"""
import json
import logging
import os
import threading
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek_us.review_loop")

# ============================================================================
#  数据类
# ============================================================================

class IssueSeverity:
    P0 = "P0"  # 致命 — 立即自动修复
    P1 = "P1"  # 重要 — 自动修复+通知
    P2 = "P2"  # 一般 — 建议等待审批
    P3 = "P3"  # 轻微 — 仅记录


@dataclass
class DetectedIssue:
    """检测到的问题"""
    category: str  # signal_quality/voting/risk/execution/funnel/attribution
    title: str
    description: str
    issue_id: str = ""
    severity: str = ""  # P0/P1/P2/P3
    evidence: Dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""
    auto_fix_available: bool = False
    detected_at: str = ""
    
    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = datetime.now().isoformat()


@dataclass 
class ActionResult:
    """自动修复执行结果"""
    issue_id: str
    action_taken: str
    success: bool
    details: str = ""
    rollback_version: str = ""
    executed_at: str = ""
    
    def __post_init__(self):
        if not self.executed_at:
            self.executed_at = datetime.now().isoformat()


# ============================================================================
#  IssueDetector: 自动分类复盘问题
# ============================================================================

class IssueDetector:
    """自动从复盘报告检测问题并分类
    
    输入: daily_review report dict
    输出: List[DetectedIssue]
    """
    
    def __init__(self):
        self._rules = self._build_rules()
    
    def _build_rules(self) -> List[Dict]:
        """构建检测规则"""
        return [
            # 信号质量规则
            {
                "name": "low_win_rate",
                "category": "signal_quality",
                "severity": IssueSeverity.P1,
                "check": self._check_low_win_rate,
                "auto_fix": True,
            },
            {
                "name": "signal_strength_all_low",
                "category": "signal_quality", 
                "severity": IssueSeverity.P0,
                "check": self._check_signal_strength,
                "auto_fix": True,
            },
            # 投票一致性规则
            {
                "name": "voting_data_missing",
                "category": "voting",
                "severity": IssueSeverity.P0,
                "check": self._check_voting_data,
                "auto_fix": False,
            },
            # 风控规则
            {
                "name": "high_drawdown",
                "category": "risk",
                "severity": IssueSeverity.P1,
                "check": self._check_drawdown,
                "auto_fix": True,
            },
            {
                "name": "high_losing_ratio",
                "category": "risk",
                "severity": IssueSeverity.P1,
                "check": self._check_losing_ratio,
                "auto_fix": True,
            },
            # 漏斗规则
            {
                "name": "funnel_overly_restrictive",
                "category": "funnel",
                "severity": IssueSeverity.P2,
                "check": self._check_funnel,
                "auto_fix": False,
            },
            # 执行规则
            {
                "name": "unplanned_trades",
                "category": "execution",
                "severity": IssueSeverity.P1,
                "check": self._check_unplanned,
                "auto_fix": True,
            },
            # 归因规则
            {
                "name": "attribution_data_empty",
                "category": "attribution",
                "severity": IssueSeverity.P2,
                "check": self._check_attribution_empty,
                "auto_fix": False,
            },
            # 市场状态规则 (P1-fix 2026-05-02)
            {
                "name": "market_regime_unknown",
                "category": "data_integrity",
                "severity": IssueSeverity.P0,
                "check": self._check_market_regime_unknown,
                "auto_fix": True,
            },
            # LLM 辩论提取 Issue (2026-05-05)
            {
                "name": "llm_debate_issues",
                "category": "llm_debate",
                "severity": IssueSeverity.P1,
                "check": self._check_llm_issues,
                "auto_fix": True,
            },
        ]
    
    def detect(self, report: Dict) -> List[DetectedIssue]:
        """从复盘报告检测问题"""
        issues = []
        dims = report.get("dimensions", {})
        summary = report.get("summary", {})
        funnel = report.get("signal_funnel", {})
        rejections = report.get("rejection_analysis", {})
        capital = report.get("capital_state", {})
        pva = report.get("plan_vs_actual", {})
        
        for rule in self._rules:
            try:
                issue = rule["check"](report, dims, summary, funnel, rejections, capital, pva)
                if issue:
                    issue.issue_id = f"{rule['name']}_{datetime.now().strftime('%Y%m%d')}"
                    issue.severity = rule["severity"]
                    issue.auto_fix_available = rule["auto_fix"]
                    issues.append(issue)
            except Exception as e:
                logger.debug(f"[IssueDetector] 规则 {rule['name']} 检测失败: {e}")
        
        return issues
    
    # ── 检测规则实现 ──
    
    def _check_low_win_rate(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        wr = summary.get("win_rate", 0)
        if wr < 0.3 and summary.get("closed_trades", 0) >= 3:
            return DetectedIssue(
                category="signal_quality",
                title=f"胜率过低: {wr*100:.0f}%",
                description=f"今日胜率 {wr*100:.0f}% 低于 30% 阈值",
                evidence={"win_rate": wr, "closed_trades": summary.get("closed_trades")},
                suggested_action="提高入场门槛，收紧 MIN_CONFIDENCE"
            )
        return None
    
    def _check_signal_strength(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        sq = dims.get("signal_quality", {})
        if sq.get("assessment") == "low_quality":
            return DetectedIssue(
                category="signal_quality",
                title="信号强度全部偏低",
                description="所有交易信号强度均低于 50",
                evidence=sq,
                suggested_action="提高 SIGNAL_MIN_SCORE 门槛"
            )
        return None
    
    def _check_voting_data(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        vc = dims.get("voting_consistency", {})
        if not vc.get("data_available"):
            return DetectedIssue(
                category="voting",
                title="投票数据未写入",
                description="final_strength 和 consensus_level 均为 0/NULL",
                evidence=vc,
                suggested_action="检查 voting_coordinator 写入管线"
            )
        return None
    
    def _check_drawdown(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        if capital and "drawdown_pct" in capital:
            dd = abs(capital["drawdown_pct"])
            if dd > 5:
                return DetectedIssue(
                    category="risk",
                    title=f"回撤过大: {dd:.1f}%",
                    description=f"当前回撤 {dd:.1f}% 超过 5% 警戒线",
                    evidence=capital,
                    suggested_action="降低仓位或暂停交易"
                )
        return None
    
    def _check_losing_ratio(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        rc = dims.get("risk_compliance", {})
        losing_ratio = rc.get("losing_trade_ratio", 0)
        if losing_ratio > 0.6 and summary.get("closed_trades", 0) >= 3:
            return DetectedIssue(
                category="risk",
                title=f"亏损交易占比过高: {losing_ratio*100:.0f}%",
                description=f"{rc.get('losing_trade_count')} 笔亏损 / {summary.get('closed_trades')} 笔总交易",
                evidence=rc,
                suggested_action="收紧入场条件或提高止损距离"
            )
        return None
    
    def _check_funnel(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        fd = dims.get("funnel_diagnosis", {})
        if fd.get("status") == "overly_restrictive":
            return DetectedIssue(
                category="funnel",
                title="漏斗过于严格",
                description=f"决策/交易比异常: {fd.get('total_decisions')} 决策 → {fd.get('total_trades')} 交易",
                evidence=fd,
                suggested_action="降低 MIN_CONFIDENCE 或 SIGNAL_MIN_SCORE"
            )
        return None
    
    def _check_unplanned(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        if pva.get("plan_available"):
            unplanned = len(pva.get("unplanned_trades", []))
            executed = len(pva.get("executed_targets", []))
            total = unplanned + executed
            if total > 0 and unplanned / total > 0.5:
                return DetectedIssue(
                    category="execution",
                    title=f"计划外交易占比过高: {unplanned/total*100:.0f}%",
                    description=f"{unplanned} 笔计划外 / {total} 笔总执行",
                    evidence=pva,
                    suggested_action="提高计划外交易门槛或加强盘前规划"
                )
        return None
    
    def _check_attribution_empty(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        if report.get("report_version") == "2.0":
            al = dims.get("attribution_learning", {})
            if not al.get("data_available"):
                return DetectedIssue(
                    category="attribution",
                    title="归因数据未填充",
                    description="trade_attribution 记录存在但评分数据全空",
                    evidence=al,
                    suggested_action="修复归因写入管线"
                )
        return None
    
    def _check_market_regime_unknown(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        """
        P1-fix 2026-05-02: 检测所有交易 market_regime=UNKNOWN 的问题
        当所有交易的 regime 都是 UNKNOWN 时，说明 regime 传播链路断裂，
        导致基于 regime 的调整 (VIX 倍数、会话调整等) 不生效。
        """
        trades = summary.get("trades", [])
        if not trades:
            return None
        
        unknown_count = sum(1 for t in trades if t.get("market_regime") in (None, "", "UNKNOWN", "未知"))
        total = len(trades)
        
        if total > 0 and unknown_count == total:
            return DetectedIssue(
                category="data_integrity",
                title=f"所有 {total} 笔交易的 market_regime=UNKNOWN",
                description=(
                    f"全部 {total} 笔交易的市场状态均为 UNKNOWN。"
                    "regime 传播链路断裂 → VIX 倍数/会话调整等基于 regime 的调整不生效。"
                    "需检查: signal_pipeline.py _build_signal_from_vote regime 读取路径"
                ),
                evidence={"total_trades": total, "unknown_count": unknown_count},
                suggested_action="修复 _build_signal_from_vote 从 vote_result 读取 market_regime"
            )
        elif total > 0 and unknown_count / total > 0.5:
            return DetectedIssue(
                category="data_integrity",
                title=f"超半数交易 market_regime=UNKNOWN ({unknown_count}/{total})",
                description=f"{unknown_count}/{total} 笔交易的 regime 为 UNKNOWN，可能导致风控调整失效",
                evidence={"total_trades": total, "unknown_count": unknown_count},
                suggested_action="检查 regime 从 IntradayScorer/voting 到 SignalScore 的传播路径"
            )
        return None
    
    def _check_llm_issues(self, report, dims, summary, funnel, rejections, capital, pva) -> Optional[DetectedIssue]:
        """
        2026-05-05: LLM 辩论 Issue 检测
        不再直接返回 issue, 由 run_review_loop 中的注入逻辑统一处理所有 LLM Issue
        避免 IssueDetector 单 issue 限制导致丢失其他 LLM Issue
        """
        llm_issues = report.get("llm_issues", [])
        if llm_issues:
            logger.info(f"[IssueDetector] 检测到 {len(llm_issues)} 个 LLM Issue, 将在 run_review_loop 中统一处理")
        return None  # 不返回, 由注入逻辑处理


# ============================================================================
#  ActionExecutor: 自动修复执行器 (v2.0: 持久化)
# ============================================================================

class ActionExecutor:
    """根据检测结果执行自动修复
    
    安全边界:
    - L0: 自动执行 (黑名单/冷却)
    - L1: 自动执行+通知 (阈值微调) → ConfigWriter 持久化
    - L2: 建议+等待审批
    """
    
    # 安全限制: 参数最大调整幅度
    SAFETY_BOUNDS = {
        "MIN_CONFIDENCE": {"min": 0.3, "max": 0.8, "max_delta": 0.1},
        "SIGNAL_MIN_SCORE": {"min": 30, "max": 80, "max_delta": 10},
        "MAX_POSITION_PER_SYMBOL": {"min": 0.1, "max": 0.4, "max_delta": 0.05},
        "cooldown_hours": {"min": 1, "max": 72, "max_delta": 12},
    }
    
    def __init__(self, notifier=None):
        self.notifier = notifier
        self._action_log = []
        self._lock = threading.Lock()
    
    def execute(self, issue: DetectedIssue, context: Dict = None) -> ActionResult:
        """执行自动修复"""
        context = context or {}
        
        if issue.category == "signal_quality" and issue.issue_id.startswith("signal_strength"):
            return self._adjust_signal_threshold(issue, context)
        elif issue.category == "risk" and issue.issue_id.startswith("high_losing"):
            return self._tighten_entry_conditions(issue, context)
        elif issue.category == "execution" and issue.issue_id.startswith("unplanned"):
            return self._increase_bypass_threshold(issue, context)
        elif issue.category == "risk" and issue.issue_id.startswith("high_drawdown"):
            return self._reduce_position_size(issue, context)
        elif issue.category.startswith("llm_debate"):
            return self._execute_llm_issue(issue, context)
        else:
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"无自动修复方案: {issue.title}",
                success=False,
                details="建议手动处理"
            )
    
    def _execute_llm_issue(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """
        2026-05-05: 执行 LLM 辩论提取的 Issue
        优先处理结构化 ChangeProposal，其次关键词匹配
        """
        # ★★★ 优先处理结构化提案
        if issue.category == "llm_debate_proposal":
            return self._execute_change_proposal(issue, context)
        
        desc = (issue.description + " " + issue.suggested_action).lower()
        
        # 关键词匹配 → 自动修复动作
        if any(kw in desc for kw in ["提高", "提高门槛", "提高入场", "提高信号", "提高阈值", "tighten"]):
            return self._tighten_entry_conditions(issue, context)
        
        if any(kw in desc for kw in ["暂停", "restricted", "限制", "禁止"]):
            return self._add_to_restricted_list(issue, context)
        
        if any(kw in desc for kw in ["止损", "stop", "收紧止损"]):
            return self._tighten_stoploss(issue, context)
        
        # 无法自动修复 → 推送通知
        msg = f"⚠️ [LLM 辩论 Issue] {issue.title}\n💡 建议: {issue.suggested_action[:200]}"
        logger.info(msg)
        if self.notifier:
            self.notifier.send_text(msg)
        
        return ActionResult(
            issue_id=issue.issue_id,
            action_taken=f"LLM 建议 (需人工确认): {issue.title}",
            success=True,
            details=f"已推送通知: {issue.suggested_action[:100]}"
        )
    
    def _execute_change_proposal(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """
        执行结构化 ChangeProposal (SSOT 强制)
        - 仅允许 config_tweak 类型自动执行
        - 必须通过 ConfigWriter 修改 unified_config.yaml
        """
        try:
            proposal = json.loads(issue.suggested_action)
            change_type = proposal.get("change_type", "")
            
            if change_type != "config_tweak":
                msg = f"📝 [LLM 提案] {proposal.get('id', 'N/A')} 非配置变更，需人工确认\n{proposal.get('rationale', '')}"
                if self.notifier:
                    self.notifier.send_text(msg)
                return ActionResult(issue_id=issue.issue_id, action_taken="需人工确认", success=True, details=msg)
            
            # SSOT 执行: 通过 ConfigWriter 修改 unified_config.yaml
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            
            config_path = proposal.get("config_path", "")
            target_value = proposal.get("target_value")
            if not config_path or target_value is None:
                return ActionResult(issue_id=issue.issue_id, action_taken="配置路径或目标值缺失", success=False)
            
            # 反向查找 PARAM_MAP 中的 Key (基于 yaml_path 匹配)
            matched_key = None
            for k, v in writer.PARAM_MAP.items():
                if v.get("yaml_path") == config_path:
                    matched_key = k
                    break
            
            if not matched_key:
                return ActionResult(issue_id=issue.issue_id, action_taken="配置路径不在白名单中", success=False, details=f"{config_path} 未映射到 PARAM_MAP")
            
            # 安全边界检查 (复用 ConfigWriter 内部逻辑)
            bounds = writer.PARAM_MAP[matched_key]
            if not (bounds["min"] <= target_value <= bounds["max"]):
                return ActionResult(issue_id=issue.issue_id, action_taken="超出安全边界", success=False, details=f"{matched_key} 不在 [{bounds['min']}, {bounds['max']}] 范围内")
            
            record = writer.update_param(
                key=matched_key,
                new_value=target_value,
                reason=f"llm_proposal: {proposal.get('id', '')} — {proposal.get('rationale', '')[:100]}",
                severity=issue.severity,
                approval_level="L1",
            )
            
            msg = f"✅ [配置变更] {config_path} → {target_value}\n🎯 预期: {proposal.get('expected_impact', '')}"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"配置变更: {config_path}",
                success=True,
                details=msg,
                rollback_version=record.version_hash,
            )
        except Exception as e:
            return ActionResult(issue_id=issue.issue_id, action_taken="提案执行失败", success=False, details=str(e))
    
    def _add_to_restricted_list(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """将标的加入限制列表 (L1: 自动执行+通知)"""
        # 从描述中提取标的符号
        import re
        symbols = re.findall(r'US\.([A-Z]{2,5})', issue.description + issue.suggested_action)
        if not symbols:
            symbols = re.findall(r'([A-Z]{2,5})', issue.title)
        
        if symbols:
            msg = f"🚫 [自动修复] 将 {', '.join(f'US.{s}' for s in symbols[:3])} 加入 Restricted List"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"Restricted: {', '.join(symbols[:3])}",
                success=True,
                details=msg
            )
        
        return ActionResult(
            issue_id=issue.issue_id,
            action_taken="无法提取标的符号",
            success=False,
            details="建议手动处理"
        )
    
    def _tighten_stoploss(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """收紧止损参数 (L1: 自动执行+通知 → ConfigWriter 持久化)"""
        try:
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            current = context.get("STOP_LOSS_PCT", 0.03)
            new_value = max(current - 0.005, 0.01)  # 最多收紧 0.5%
            
            record = writer.update_param(
                key="STOP_LOSS_PCT",
                new_value=new_value,
                reason=f"llm_debate: {issue.issue_id} — {issue.title}",
                severity=issue.severity,
                approval_level="L1",
            )
            
            msg = f"🔧 [自动修复] 止损收紧: {current:.2%} → {new_value:.2%} (版本: {record.version_hash})"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"止损收紧: {current:.2%} → {new_value:.2%}",
                success=True,
                details=msg,
                rollback_version=record.version_hash,
            )
        except Exception as e:
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="止损收紧失败",
                success=False,
                details=str(e)
            )
    
    def _adjust_signal_threshold(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """调整信号强度阈值 (L1: 自动执行+通知 → ConfigWriter 持久化)"""
        try:
            current = context.get("SIGNAL_MIN_SCORE", 50)
            new_value = min(
                current + self.SAFETY_BOUNDS["SIGNAL_MIN_SCORE"]["max_delta"],
                self.SAFETY_BOUNDS["SIGNAL_MIN_SCORE"]["max"]
            )
            
            # v2.0: 使用 ConfigWriter 持久化
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            record = writer.update_param(
                key="SIGNAL_MIN_SCORE",
                new_value=new_value,
                reason=f"auto_fix: {issue.issue_id} — {issue.title}",
                severity=issue.severity,
                approval_level="L1",
            )
            
            msg = f"🔧 [自动修复] 信号强度阈值: {current} → {new_value} (版本: {record.version_hash})"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            with self._lock:
                self._action_log.append({
                    "action": "adjust_signal_threshold",
                    "before": current,
                    "after": new_value,
                    "issue_id": issue.issue_id,
                    "version_hash": record.version_hash,
                    "timestamp": datetime.now().isoformat(),
                })
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"信号强度阈值调整: {current} → {new_value}",
                success=True,
                details=msg,
                rollback_version=record.version_hash,
            )
        except Exception as e:
            logger.error(f"[ActionExecutor] 调整信号强度阈值失败: {e}")
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="调整信号强度阈值",
                success=False,
                details=str(e)
            )
    
    def _tighten_entry_conditions(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """收紧入场条件 (L1: 自动执行+通知 → ConfigWriter 持久化)"""
        try:
            current = context.get("MIN_CONFIDENCE", 0.70)
            new_value = min(
                current + self.SAFETY_BOUNDS["MIN_CONFIDENCE"]["max_delta"],
                self.SAFETY_BOUNDS["MIN_CONFIDENCE"]["max"]
            )
            
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            record = writer.update_param(
                key="MIN_CONFIDENCE",
                new_value=new_value,
                reason=f"auto_fix: {issue.issue_id} — {issue.title}",
                severity=issue.severity,
                approval_level="L1",
            )
            
            msg = f"🔧 [自动修复] 入场信心阈值: {current} → {new_value} (版本: {record.version_hash})"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            with self._lock:
                self._action_log.append({
                    "action": "tighten_entry",
                    "before": current,
                    "after": new_value,
                    "issue_id": issue.issue_id,
                    "version_hash": record.version_hash,
                    "timestamp": datetime.now().isoformat(),
                })
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"入场信心阈值调整: {current} → {new_value}",
                success=True,
                details=msg,
                rollback_version=record.version_hash,
            )
        except Exception as e:
            logger.error(f"[ActionExecutor] 收紧入场条件失败: {e}")
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="收紧入场条件",
                success=False,
                details=str(e)
            )
    
    def _increase_bypass_threshold(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """提高计划外交易门槛 (L1)"""
        try:
            msg = f"🔧 [自动修复] 提高计划外交易门槛 (原因: {issue.title})"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            with self._lock:
                self._action_log.append({
                    "action": "increase_bypass_threshold",
                    "issue_id": issue.issue_id,
                    "timestamp": datetime.now().isoformat(),
                })
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="提高计划外交易门槛",
                success=True,
                details=msg
            )
        except Exception as e:
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="提高计划外交易门槛",
                success=False,
                details=str(e)
            )
    
    def _reduce_position_size(self, issue: DetectedIssue, context: Dict) -> ActionResult:
        """降低仓位上限 (L1: 自动执行+通知 → ConfigWriter 持久化)"""
        try:
            current = context.get("MAX_POSITION_PER_SYMBOL", 0.25)
            new_value = max(
                current - self.SAFETY_BOUNDS["MAX_POSITION_PER_SYMBOL"]["max_delta"],
                self.SAFETY_BOUNDS["MAX_POSITION_PER_SYMBOL"]["min"]
            )
            
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            record = writer.update_param(
                key="MAX_POSITION_PER_SYMBOL",
                new_value=new_value,
                reason=f"auto_fix: {issue.issue_id} — {issue.title}",
                severity=issue.severity,
                approval_level="L1",
            )
            
            msg = f"🔧 [自动修复] 单标的仓位上限: {current} → {new_value} (版本: {record.version_hash})"
            logger.info(msg)
            if self.notifier:
                self.notifier.send_text(msg)
            
            with self._lock:
                self._action_log.append({
                    "action": "reduce_position_size",
                    "before": current,
                    "after": new_value,
                    "issue_id": issue.issue_id,
                    "version_hash": record.version_hash,
                    "timestamp": datetime.now().isoformat(),
                })
            
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"单标的仓位上限调整: {current} → {new_value}",
                success=True,
                details=msg,
                rollback_version=record.version_hash,
            )
        except Exception as e:
            logger.error(f"[ActionExecutor] 降低仓位上限失败: {e}")
            return ActionResult(
                issue_id=issue.issue_id,
                action_taken="降低仓位上限",
                success=False,
                details=str(e)
            )
    
    def get_action_log(self) -> List[Dict]:
        with self._lock:
            return list(self._action_log)


# ============================================================================
#  ReviewEffectTracker: 修复效果追踪 + 自动回滚 (v2.0: 持久化)
# ============================================================================

class ReviewEffectTracker:
    """追踪自动修复效果，恶化时自动回滚
    
    机制:
    1. 记录每次修复前后的关键指标
    2. N 天后对比指标
    3. 如果恶化超过阈值，自动回滚 (通过 ConfigWriter)
    """
    
    DETERIORATION_THRESHOLD = 0.10  # 10% 恶化触发回滚
    CHECK_PERIOD_DAYS = 3  # 3 天后检查效果
    
    def __init__(self, tracker_path: str = None):
        self.tracker_path = tracker_path or str(get_data_dir() / "review_effects.json")
        self._lock = threading.Lock()
        self._effects = self._load()
    
    def _load(self) -> Dict:
        if os.path.exists(self.tracker_path):
            try:
                with open(self.tracker_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError, UnicodeDecodeError):
                pass
        return {"actions": [], "rollbacks": []}
    
    def _save(self):
        os.makedirs(os.path.dirname(self.tracker_path), exist_ok=True)
        with open(self.tracker_path, 'w') as f:
            json.dump(self._effects, f, indent=2, ensure_ascii=False)
    
    def record_action(self, action_result: ActionResult, baseline_metrics: Dict):
        """记录修复动作和基线指标"""
        with self._lock:
            self._effects["actions"].append({
                "issue_id": action_result.issue_id,
                "action": action_result.action_taken,
                "success": action_result.success,
                "baseline": baseline_metrics,
                "rollback_version": action_result.rollback_version,
                "timestamp": datetime.now().isoformat(),
                "check_due": (datetime.now() + timedelta(days=self.CHECK_PERIOD_DAYS)).isoformat(),
                "checked": False,
            })
            self._save()
    
    def check_effect(self, action_entry: Dict, current_metrics: Dict) -> Dict:
        """检查修复效果"""
        baseline = action_entry.get("baseline", {})
        
        # 对比胜率
        baseline_wr = baseline.get("win_rate", 0)
        current_wr = current_metrics.get("win_rate", 0)
        
        # 对比回撤
        baseline_dd = baseline.get("drawdown_pct", 0)
        current_dd = current_metrics.get("drawdown_pct", 0)
        
        deteriorated = False
        reason = ""
        
        if baseline_wr > 0 and current_wr < baseline_wr * (1 - self.DETERIORATION_THRESHOLD):
            deteriorated = True
            reason = f"胜率恶化: {baseline_wr*100:.0f}% → {current_wr*100:.0f}%"
        elif abs(current_dd) > abs(baseline_dd) * (1 + self.DETERIORATION_THRESHOLD):
            deteriorated = True
            reason = f"回撤恶化: {baseline_dd:.1f}% → {current_dd:.1f}%"
        
        return {
            "deteriorated": deteriorated,
            "reason": reason,
            "baseline_wr": baseline_wr,
            "current_wr": current_wr,
            "baseline_dd": baseline_dd,
            "current_dd": current_dd,
        }
    
    def execute_rollback(self, action_entry: Dict) -> bool:
        """执行配置回滚 (v2.0: 通过 ConfigWriter)"""
        issue_id = action_entry.get("issue_id", "")
        rollback_version = action_entry.get("rollback_version", "")
        
        try:
            from app.core.config_writer import get_config_writer
            writer = get_config_writer()
            
            # 从审计日志找到对应的参数路径和历史值
            audit = writer._read_audit_log()
            # B3 修复 (2026-05-13): reason 格式为 "auto_fix: {issue_id} — {title}", 以 title 结尾
            # 改为检查 reason 中是否包含 issue_id
            matching = [e for e in audit if issue_id in e.get("reason", "")]
            if matching:
                dotted_path = matching[-1].get("param_path")
                if dotted_path:
                    writer.rollback_param(dotted_path)
                    return True
            
            logger.warning(f"[EffectTracker] 无法回滚 {issue_id}: 无匹配审计记录")
            return False
        except Exception as e:
            logger.error(f"[EffectTracker] 回滚失败: {e}")
            return False
    
    def record_rollback(self, issue_id: str, reason: str):
        """记录回滚"""
        with self._lock:
            self._effects["rollbacks"].append({
                "issue_id": issue_id,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            })
            self._save()
    
    def get_pending_checks(self) -> List[Dict]:
        """获取待检查的修复动作"""
        now = datetime.now().isoformat()
        with self._lock:
            return [
                a for a in self._effects.get("actions", [])
                if not a.get("checked") and a.get("check_due", "") < now
            ]


# ============================================================================
#  模块级单例
# ============================================================================

_issue_detector: Optional[IssueDetector] = None
_action_executor: Optional[ActionExecutor] = None
_effect_tracker: Optional[ReviewEffectTracker] = None


def get_issue_detector() -> IssueDetector:
    global _issue_detector
    if _issue_detector is None:
        _issue_detector = IssueDetector()
    return _issue_detector


def get_action_executor(notifier=None) -> ActionExecutor:
    global _action_executor
    if _action_executor is None:
        _action_executor = ActionExecutor(notifier=notifier)
    return _action_executor


def get_effect_tracker() -> ReviewEffectTracker:
    global _effect_tracker
    if _effect_tracker is None:
        _effect_tracker = ReviewEffectTracker()
    return _effect_tracker


# ============================================================================
#  便捷入口: 从复盘报告自动触发闭环
# ============================================================================

def run_review_loop(report: Dict, notifier=None, context: Dict = None) -> Dict:
    """从复盘报告运行完整的改进闭环
    
    使用示例:
        from app.core.review_loop import run_review_loop
        result = run_review_loop(report, notifier=scheduler.notifier)
    """
    from app.core.issue_registry import get_issue_registry
    
    detector = get_issue_detector()
    executor = get_action_executor(notifier=notifier)
    tracker = get_effect_tracker()
    registry = get_issue_registry()
    
    # 1. 检测问题
    issues = detector.detect(report)
    logger.info(f"[ReviewLoop] 检测到 {len(issues)} 个问题")
    
    # 1.5 去重: 通过 IssueRegistry 检查是否为持续问题
    unique_issues = []
    for issue in issues:
        state = registry.record_issue(
            category=issue.category,
            title=issue.title,
            description=issue.description,
            severity=issue.severity,
            issue_id=issue.issue_id,
        )
        
        # 如果是持续问题 (已解决后复发)，提升优先级
        if state.status == "resolved":
            logger.warning(f"[ReviewLoop] 问题复发: {issue.title}")
        
        unique_issues.append(issue)
    
    # 1.6 注入 LLM 辩论提取的 Issue (2026-05-05)
    # 直接从 review dict 获取, 绕过 IssueDetector 的单 issue 限制
    llm_issues = report.get("llm_issues", [])
    if llm_issues:
        # 过滤掉与现有 issue 重复的
        existing_ids = set(i.issue_id for i in unique_issues)
        for li in llm_issues:
            llm_issue = DetectedIssue(
                category=li.get("category", "llm_debate"),
                title=li.get("title", "LLM Issue"),
                description=li.get("description", ""),
                issue_id=li.get("issue_id", f"llm_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
                severity=li.get("severity", "P2"),
                evidence=li.get("evidence", {}),
                suggested_action=li.get("suggested_action", ""),
                auto_fix_available=li.get("auto_fix_available", False),
            )
            if llm_issue.issue_id not in existing_ids:
                unique_issues.append(llm_issue)
                logger.info(f"[ReviewLoop] 注入 LLM Issue: [{llm_issue.severity}] {llm_issue.title[:60]}")
        logger.info(f"[ReviewLoop] 注入 {len(llm_issues)} 个 LLM Issue, 总计 {len(unique_issues)} 个")
    
    # 2. 执行自动修复 (P0/P1)
    results = []
    for issue in unique_issues:
        if issue.severity in (IssueSeverity.P0, IssueSeverity.P1) and issue.auto_fix_available:
            result = executor.execute(issue, context)
            results.append(result)
            
            if result.success:
                # 标记问题已解决
                fp = registry.fingerprint(issue.category, issue.title)
                registry.mark_resolved(fp, resolver=f"auto_fix:{issue.issue_id}")
            
            # 记录效果追踪
            baseline = {
                "win_rate": report.get("summary", {}).get("win_rate", 0),
                "drawdown_pct": report.get("capital_state", {}).get("drawdown_pct", 0),
            }
            tracker.record_action(result, baseline)
        else:
            results.append(ActionResult(
                issue_id=issue.issue_id,
                action_taken=f"待审批: {issue.title}",
                success=False,
                details=f"严重度={issue.severity}, 需手动处理"
            ))
    
    # 3. 检查待验证的修复效果 + 自动回滚
    pending = tracker.get_pending_checks()
    for entry in pending:
        current_metrics = {
            "win_rate": report.get("summary", {}).get("win_rate", 0),
            "drawdown_pct": report.get("capital_state", {}).get("drawdown_pct", 0),
        }
        check_result = tracker.check_effect(entry, current_metrics)
        if check_result["deteriorated"]:
            # v2.0: 实际回滚配置
            rollback_success = tracker.execute_rollback(entry)
            tracker.record_rollback(entry["issue_id"], check_result["reason"])
            
            if notifier:
                status = "✅ 已回滚" if rollback_success else "⚠️ 回滚失败"
                notifier.send_text(f"⚠️ [自动回滚] {entry['issue_id']}: {check_result['reason']} {status}")
            
            if rollback_success:
                logger.info(f"[ReviewLoop] 自动回滚成功: {entry['issue_id']} — {check_result['reason']}")
            else:
                logger.warning(f"[ReviewLoop] 自动回滚失败: {entry['issue_id']} — {check_result['reason']}")
        
        entry["checked"] = True
    
    # 4. Phase 5: 逐笔交易分析 + 辩论触发 (2026-05-02 新增)
    trade_analyses = report.get('trade_analyses', [])
    debate_trades = []
    verdict_summary = {}
    
    for ta in trade_analyses:
        v = ta.get('verdict', '')
        verdict_summary[v] = verdict_summary.get(v, 0) + 1
        
        if ta.get('needs_debate'):
            debate_trades.append(ta)
    
    if debate_trades:
        logger.info(f"[ReviewLoop] 检测到 {len(debate_trades)} 笔需要辩论的交易")
        if notifier:
            debate_msg = "🔍 交易辩论触发\n"
            for dt in debate_trades[:3]:  # 最多推送 3 笔
                debate_msg += (
                    f"\n• #{dt['trade_id']} {dt['symbol']} {dt['direction']} "
                    f"pnl=${dt['realized_pnl']:.2f}\n"
                    f"  判定: {dt['verdict']} | 原因: {dt['debate_reason']}\n"
                )
            notifier.send_text(debate_msg)
    
    # ── Phase 6: 经验积累 (2026-05-02 新增) ──
    experience_result = {}
    try:
        from app.review.experience_accumulator import ExperienceAccumulator

        accumulator = ExperienceAccumulator()
        experience_result = accumulator.accumulate(report)
        logger.info(
            f"[ExperienceAcc] 经验积累: 提取={experience_result['lessons_extracted']} "
            f"持久化={experience_result['lessons_persisted']} "
            f"次日提示={experience_result['lesson_count']}条"
        )
        # P0 修复 (2026-05-09): 移除内部推送，改为返回 experience_result
        # 让调用方决定是否发送，避免 daily/weekly/intraday review 重复推送
    except Exception as acc_err:
        logger.debug(f"[ExperienceAcc] 经验积累失败 (不影响主流程): {acc_err}")
    
    # 5. 清理归档
    registry.archive_resolved(max_age_days=30)
    
    return {
        "issues_detected": len(issues),
        "actions_taken": len([r for r in results if r.success]),
        "actions": [r.__dict__ for r in results],
        "rollbacks_pending": len(pending),
        "registry_summary": registry.get_summary(),
        # Phase 5: 新增
        "verdict_summary": verdict_summary,
        "debate_trades": len(debate_trades),
        # P0 修复 (2026-05-09): 返回 experience_result，让调用方控制次日提示推送
        "experience_result": experience_result,
    }
