#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 盘中实时诊断器 (Layer 1)

功能:
1. 每30分钟读取 signal_tracker + decision_recorder 数据
2. 执行8条异常检测规则
3. 分级推送 (info/warning/critical) 到钉钉/飞书
4. 诊断结果保存到 data/diagnosis_knowledge/ 知识库

数据源:
- /opt/ftseek/FTSeek/data/signal_records/signals_YYYY-MM-DD.json
- /opt/ftseek/FTSeek/data/decisions_hk.db
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger("ftseek.diagnosis.intraday")

# 知识库存储目录
DIAGNOSIS_KNOWLEDGE_DIR = Path('/opt/ftseek/FTSeek/data/diagnosis_knowledge')
DIAGNOSIS_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DiagnosisAlert:
    """诊断警报"""
    level: str  # "info", "warning", "critical"
    rule_id: str  # 规则 ID
    rule_name: str  # 规则名称
    message: str  # 警报消息
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return {
            "level": self.level,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "message": self.message,
            "details": _sanitize_details(self.details),
            "timestamp": self.timestamp,
        }


def _sanitize_details(details: Dict) -> Dict:
    """清理细节中的不可序列化对象"""
    sanitized = {}
    for k, v in details.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            sanitized[k] = v
        elif isinstance(v, (list, tuple)):
            sanitized[k] = [_sanitize_item(i) for i in v]
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_details(v)
        else:
            sanitized[k] = str(v)
    return sanitized


def _sanitize_item(item):
    if isinstance(item, (str, int, float, bool, type(None))):
        return item
    return str(item)


class IntradayDiagnoser:
    """盘中实时诊断器"""
    
    def __init__(self, notifier=None):
        self.notifier = notifier
        self.signal_tracker = None
        self.decision_recorder = None
        self._alerts_history: List[DiagnosisAlert] = []
        self._last_diagnosis_time = None
    
    def _lazy_init(self):
        """延迟初始化数据源"""
        if self.signal_tracker is None:
            try:
                from app.signal.core import SignalTracker
                self.signal_tracker = SignalTracker()
            except Exception as e:
                logger.warning(f"[诊断] signal_tracker 初始化失败: {e}")
        
        if self.decision_recorder is None:
            try:
                from app.storage.decision_recorder import get_decision_recorder
                self.decision_recorder = get_decision_recorder()
            except Exception as e:
                logger.warning(f"[诊断] decision_recorder 初始化失败: {e}")
    
    def run_diagnosis(self) -> List[DiagnosisAlert]:
        """执行完整诊断流程"""
        self._lazy_init()
        
        now = datetime.now()
        self._last_diagnosis_time = now
        
        logger.info(f"[盘中诊断] 开始执行诊断 ({now.strftime('%H:%M')})...")
        
        alerts = []
        
        # 8条异常检测规则
        alerts.extend(self._check_signal_funnel())
        alerts.extend(self._check_rejection_rate())
        alerts.extend(self._check_confidence_drift())
        alerts.extend(self._check_strategy_consistency())
        alerts.extend(self._check_execution_delay())
        alerts.extend(self._check_pnl_distribution())
        alerts.extend(self._check_market_regime_change())
        alerts.extend(self._check_cooldown_pileup())
        
        # 保存诊断结果
        self._save_diagnosis_result(alerts)
        
        # 分级推送
        self._push_alerts(alerts)
        
        logger.info(f"[盘中诊断] 完成，发现 {len(alerts)} 条警报")
        return alerts
    
    def _get_today_signals(self):
        """获取今日信号（带异常处理）"""
        if self.signal_tracker is None:
            return []
        try:
            return self.signal_tracker.get_today_signals()
        except Exception as e:
            logger.error(f"[诊断] 获取今日信号失败: {e}")
            return []
    
    # ─── 8条异常检测规则 ─────────────────────────────────────
    
    def _check_signal_funnel(self) -> List[DiagnosisAlert]:
        """规则1: 信号漏斗断裂 — 信号生成→决策→执行→平仓的链路完整性"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if not today_signals:
            return alerts
        
        total = len(today_signals)
        executed = sum(1 for s in today_signals if s.executed)
        exited = sum(1 for s in today_signals if s.exit_price is not None)
        
        # 检查断点
        funnel_gaps = []
        if total > 0 and executed == 0:
            funnel_gaps.append("信号生成→执行: 100% 断裂")
        if executed > 0 and exited == 0:
            funnel_gaps.append("执行→平仓: 100% 断裂 (可能因为持仓周期较长)")
        
        execution_rate = executed / total if total > 0 else 0
        
        # 仅当执行率为0且信号数>=3时报警（避免早盘误报）
        if total >= 3 and executed == 0:
            alerts.append(DiagnosisAlert(
                level="warning",
                rule_id="FUNNEL_001",
                rule_name="信号漏斗断裂",
                message=f"信号执行率 0% ({total}个信号全部未执行)，漏斗可能断裂",
                details={
                    "total_signals": total,
                    "executed": executed,
                    "exited": exited,
                    "gaps": funnel_gaps,
                }
            ))
        elif total >= 5 and execution_rate < 0.2:
            alerts.append(DiagnosisAlert(
                level="info",
                rule_id="FUNNEL_002",
                rule_name="信号漏斗低转化",
                message=f"信号执行率仅 {execution_rate:.0%} ({executed}/{total})，需关注",
                details={
                    "total_signals": total,
                    "executed": executed,
                    "exited": exited,
                    "execution_rate": round(execution_rate, 4),
                }
            ))
        
        return alerts
    
    def _check_rejection_rate(self) -> List[DiagnosisAlert]:
        """规则2: 拒绝率飙升 — 短时间内大量信号被拒绝"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if not today_signals:
            return alerts
        
        rejected = [s for s in today_signals if not s.executed and s.reject_reason]
        rejection_rate = len(rejected) / len(today_signals)
        
        # 检查拒绝原因集中度
        reject_reasons = defaultdict(int)
        for s in rejected:
            # 简化拒绝原因
            reason = s.reject_reason or "unknown"
            # 提取主要原因
            if "confidence" in reason.lower() or "置信度" in reason:
                reason = "置信度不足"
            elif "cooldown" in reason.lower() or "冷却" in reason:
                reason = "冷却期"
            elif "position" in reason.lower() or "仓位" in reason:
                reason = "仓位限制"
            reject_reasons[reason] += 1
        
        # 如果拒绝率超过60%或单一原因超过3次
        if rejection_rate > 0.6 or any(v >= 3 for v in reject_reasons.values()):
            top_reason = max(reject_reasons.items(), key=lambda x: x[1]) if reject_reasons else ("unknown", 0)
            alerts.append(DiagnosisAlert(
                level="warning" if rejection_rate < 0.8 else "critical",
                rule_id="REJECT_001",
                rule_name="拒绝率飙升",
                message=f"信号拒绝率 {rejection_rate:.0%}，主因: {top_reason[0]} ({top_reason[1]}次)",
                details={
                    "rejection_rate": round(rejection_rate, 4),
                    "reject_reasons": dict(reject_reasons),
                    "total_signals": len(today_signals),
                    "rejected_count": len(rejected),
                }
            ))
        
        return alerts
    
    def _check_confidence_drift(self) -> List[DiagnosisAlert]:
        """规则3: 置信度漂移 — 信号置信度持续下降"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if len(today_signals) < 6:  # 样本不足
            return alerts
        
        # 按时间排序
        sorted_signals = sorted(today_signals, key=lambda s: s.timestamp)
        
        # 分前50%和后50%对比
        mid = len(sorted_signals) // 2
        first_half = sorted_signals[:mid]
        second_half = sorted_signals[mid:]
        
        avg_conf_first = sum(s.confidence for s in first_half) / len(first_half)
        avg_conf_second = sum(s.confidence for s in second_half) / len(second_half)
        
        drift = avg_conf_second - avg_conf_first
        if drift < -0.15:  # 置信度下降超过15%
            alerts.append(DiagnosisAlert(
                level="warning",
                rule_id="CONF_001",
                rule_name="置信度漂移",
                message=f"信号置信度持续下降: {avg_conf_first:.2f} → {avg_conf_second:.2f} (Δ{drift:.2f})",
                details={
                    "avg_confidence_first_half": round(avg_conf_first, 4),
                    "avg_confidence_second_half": round(avg_conf_second, 4),
                    "drift": round(drift, 4),
                }
            ))
        
        return alerts
    
    def _check_strategy_consistency(self) -> List[DiagnosisAlert]:
        """规则4: 策略一致性破裂 — 多策略信号方向冲突"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if not today_signals:
            return alerts
        
        # 按标的分组
        by_code = defaultdict(list)
        for s in today_signals:
            by_code[s.code].append(s)
        
        # 检查同一标的的多策略冲突
        conflicts = []
        for code, signals in by_code.items():
            buy_count = sum(1 for s in signals if s.action == 'BUY')
            sell_count = sum(1 for s in signals if s.action == 'SELL')
            if buy_count > 0 and sell_count > 0:
                conflicts.append({
                    "code": code,
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                    "strategies": list(set(s.strategy for s in signals)),
                })
        
        if conflicts:
            alerts.append(DiagnosisAlert(
                level="info",
                rule_id="STRAT_001",
                rule_name="策略一致性破裂",
                message=f"发现 {len(conflicts)} 只标的存在策略方向冲突",
                details={"conflicts": conflicts}
            ))
        
        return alerts
    
    def _check_execution_delay(self) -> List[DiagnosisAlert]:
        """规则5: 执行延迟 — 信号生成到执行的延迟过长"""
        alerts = []
        today_signals = self._get_today_signals()
        
        executed_signals = [s for s in today_signals if s.executed and s.execution_time]
        if not executed_signals:
            return alerts
        
        delays = []
        for s in executed_signals:
            try:
                gen_time = datetime.fromisoformat(s.timestamp)
                exec_time = datetime.fromisoformat(s.execution_time)
                delay_minutes = (exec_time - gen_time).total_seconds() / 60
                delays.append({"code": s.code, "delay_minutes": round(delay_minutes, 2)})
            except Exception:
                pass
        
        if delays:
            avg_delay = sum(d["delay_minutes"] for d in delays) / len(delays)
            max_delay = max(d["delay_minutes"] for d in delays)
            
            if avg_delay > 5:  # 平均延迟超过5分钟
                alerts.append(DiagnosisAlert(
                    level="warning",
                    rule_id="EXEC_001",
                    rule_name="执行延迟",
                    message=f"信号执行平均延迟 {avg_delay:.1f} 分钟，最大 {max_delay:.1f} 分钟",
                    details={
                        "avg_delay_minutes": round(avg_delay, 2),
                        "max_delay_minutes": round(max_delay, 2),
                        "sample_delays": delays[:5],
                    }
                ))
        
        return alerts
    
    def _check_pnl_distribution(self) -> List[DiagnosisAlert]:
        """规则6: 盈亏分布异常 — 连续亏损或盈亏比失衡"""
        alerts = []
        today_signals = self._get_today_signals()
        
        exited_signals = [s for s in today_signals if s.pnl is not None]
        if len(exited_signals) < 3:
            return alerts
        
        # 检查连续亏损
        consecutive_losses = 0
        max_consecutive_losses = 0
        for s in sorted(exited_signals, key=lambda x: x.exit_time or ""):
            if s.pnl < 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0
        
        if max_consecutive_losses >= 3:
            alerts.append(DiagnosisAlert(
                level="critical" if max_consecutive_losses >= 5 else "warning",
                rule_id="PNL_001",
                rule_name="盈亏分布异常",
                message=f"发现连续 {max_consecutive_losses} 笔亏损交易",
                details={
                    "max_consecutive_losses": max_consecutive_losses,
                    "total_exited": len(exited_signals),
                    "winning": sum(1 for s in exited_signals if s.pnl > 0),
                    "losing": sum(1 for s in exited_signals if s.pnl <= 0),
                }
            ))
        
        return alerts
    
    def _check_market_regime_change(self) -> List[DiagnosisAlert]:
        """规则7: 市场状态突变 — 趋势市/震荡市切换导致策略失效"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if not today_signals:
            return alerts
        
        regimes = [s.market_regime for s in today_signals if s.market_regime]
        if len(regimes) < 5:
            return alerts
        
        # 检查 regime 切换频率
        regime_changes = 0
        for i in range(1, len(regimes)):
            if regimes[i] != regimes[i-1]:
                regime_changes += 1
        
        regime_change_rate = regime_changes / len(regimes)
        if regime_change_rate > 0.4:  # 40%以上信号处于不同regime
            regime_dist = {}
            for r in set(regimes):
                regime_dist[r] = regimes.count(r)
            
            alerts.append(DiagnosisAlert(
                level="info",
                rule_id="REGIME_001",
                rule_name="市场状态突变",
                message=f"市场状态频繁切换 ({regime_change_rate:.0%} 信号处于不同状态)",
                details={
                    "regime_distribution": regime_dist,
                    "regime_changes": regime_changes,
                    "regime_change_rate": round(regime_change_rate, 4),
                }
            ))
        
        return alerts
    
    def _check_cooldown_pileup(self) -> List[DiagnosisAlert]:
        """规则8: 冷却期堆积 — 大量标的处于冷却期，信号生成受阻"""
        alerts = []
        today_signals = self._get_today_signals()
        
        if not today_signals:
            return alerts
        
        # 按小时分组
        hourly_counts = defaultdict(int)
        for s in today_signals:
            try:
                hour = datetime.fromisoformat(s.timestamp).hour
                hourly_counts[hour] += 1
            except Exception:
                pass
        
        # 检查是否有连续2小时无信号（仅限交易时段）
        now_hour = datetime.now().hour
        trading_hours = list(range(9, 12)) + list(range(13, 17))  # 9-11, 13-16
        # 只看已经过去的小时
        past_trading_hours = [h for h in trading_hours if h < now_hour]
        
        if len(past_trading_hours) >= 2:
            zero_hours = [h for h in past_trading_hours if h not in hourly_counts]
            
            # 检查是否有连续2小时无信号
            has_consecutive_zeros = False
            for i in range(len(zero_hours) - 1):
                if zero_hours[i+1] - zero_hours[i] == 1:
                    has_consecutive_zeros = True
                    break
            
            if has_consecutive_zeros or len(zero_hours) >= 2:
                alerts.append(DiagnosisAlert(
                    level="warning",
                    rule_id="COOLDOWN_001",
                    rule_name="冷却期堆积",
                    message=f"发现 {len(zero_hours)} 个交易小时无信号生成: {zero_hours}",
                    details={
                        "zero_hours": zero_hours,
                        "hourly_distribution": dict(hourly_counts),
                        "current_hour": now_hour,
                    }
                ))
        
        return alerts
    
    # ─── 推送 ─────────────────────────────────────────────────
    
    def _push_alerts(self, alerts: List[DiagnosisAlert]):
        """分级推送警报"""
        if not self.notifier or not alerts:
            return
        
        # 按级别分组
        critical = [a for a in alerts if a.level == "critical"]
        warning = [a for a in alerts if a.level == "warning"]
        info = [a for a in alerts if a.level == "info"]
        
        # Critical: 立即推送
        if critical:
            self._push_critical_alerts(critical)
        
        # Warning: 合并推送
        if warning:
            self._push_warning_alerts(warning)
        
        # Info: 仅记录，不推送（除非有critical/warning）
        if info and (critical or warning):
            logger.info(f"[盘中诊断] {len(info)} 条 info 级警报仅记录")
    
    def _push_critical_alerts(self, alerts: List[DiagnosisAlert]):
        """推送 critical 级警报"""
        now_str = datetime.now().strftime('%H:%M')
        lines = [f"### 🚨 盘中诊断 - 严重警报 | {now_str} HKT\n"]
        
        for alert in alerts:
            lines.extend([
                f"**{alert.rule_name}** ({alert.rule_id})",
                f"{alert.message}",
                "---",
            ])
        
        content = "\n\n".join(lines)
        try:
            self.notifier.send_text(content, at_all=True)
            logger.info(f"[盘中诊断] 已推送 {len(alerts)} 条 critical 警报")
        except Exception as e:
            logger.error(f"[盘中诊断] 推送 critical 警报失败: {e}")
    
    def _push_warning_alerts(self, alerts: List[DiagnosisAlert]):
        """推送 warning 级警报（合并）"""
        now_str = datetime.now().strftime('%H:%M')
        lines = [f"### ⚠️ 盘中诊断 - 警告 | {now_str} HKT\n"]
        lines.append(f"**共 {len(alerts)} 条警告**\n")
        
        for alert in alerts:
            lines.append(f"- **{alert.rule_name}**: {alert.message}")
        
        content = "\n\n".join(lines)
        try:
            self.notifier.send_text(content)
            logger.info(f"[盘中诊断] 已推送 {len(alerts)} 条 warning 警报")
        except Exception as e:
            logger.error(f"[盘中诊断] 推送 warning 警报失败: {e}")
    
    # ─── 知识库保存 ──────────────────────────────────────────
    
    def _save_diagnosis_result(self, alerts: List[DiagnosisAlert]):
        """保存诊断结果到知识库"""
        today = datetime.now().strftime('%Y-%m-%d')
        hour = datetime.now().strftime('%H%M')
        
        result = {
            "timestamp": datetime.now().isoformat(),
            "date": today,
            "time": hour,
            "alerts": [a.to_dict() for a in alerts],
            "summary": {
                "total": len(alerts),
                "critical": sum(1 for a in alerts if a.level == "critical"),
                "warning": sum(1 for a in alerts if a.level == "warning"),
                "info": sum(1 for a in alerts if a.level == "info"),
            }
        }
        
        # 保存到知识库
        knowledge_file = DIAGNOSIS_KNOWLEDGE_DIR / f"diagnosis_{today}_{hour}.json"
        try:
            with open(knowledge_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"[盘中诊断] 诊断结果已保存: {knowledge_file}")
        except Exception as e:
            logger.error(f"[盘中诊断] 保存诊断结果失败: {e}")


# ─── 单例 ──────────────────────────────────────────────────
_instance = None
_lock = None

def get_intraday_diagnoser(notifier=None) -> IntradayDiagnoser:
    global _instance, _lock
    if _lock is None:
        import threading
        _lock = threading.Lock()
    
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = IntradayDiagnoser(notifier=notifier)
    return _instance
