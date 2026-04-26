#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 自愈优化器 (Self-Healing Optimizer) v1.0

功能:
- 从自愈历史中学习，自动调整策略
- 识别重复出现的问题模式，建议永久性修复
- 生成优化报告，推动系统自我演进

运行方式:
  1. 作为 cron 任务定期运行 (每天 22:00)
  2. 作为 harness 全链路审计的一部分

输出:
  - 自愈优化报告 (推送钉钉)
  - 策略调整建议
  - 代码修复 PR 建议
"""

import os
import sys
import json
import logging
import re
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path

try:
    from app.dingtalk_alert import send_dingtalk_alert
    _HAS_DINGTALK = True
except ImportError:
    _HAS_DINGTALK = False

logger = logging.getLogger("ftseek.self_healing.optimizer")


class SelfHealingOptimizer:
    """自愈优化器 — 从历史中学习，推动系统演进"""
    
    def __init__(self, base_dir: str = "/opt/ftseek/FTSeek"):
        self.base_dir = Path(base_dir)
        self.history_file = self.base_dir / "data" / "self_healing_history.json"
        self.patterns_file = self.base_dir / "data" / "self_healing_patterns.json"
        self.optimization_log = self.base_dir / "data" / "self_healing_optimization.json"
    
    def load_history(self) -> List[Dict]:
        """加载自愈历史"""
        if self.history_file.exists():
            try:
                with open(self.history_file) as f:
                    data = json.load(f)
                return data.get("reports", [])
            except Exception:
                pass
        return []
    
    def load_patterns(self) -> List[Dict]:
        """加载日志模式"""
        if self.patterns_file.exists():
            try:
                with open(self.patterns_file) as f:
                    data = json.load(f)
                return data.get("patterns", [])
            except Exception:
                pass
        return []
    
    def load_optimization_log(self) -> List[Dict]:
        """加载优化日志"""
        if self.optimization_log.exists():
            try:
                with open(self.optimization_log) as f:
                    data = json.load(f)
                return data.get("optimizations", [])
            except Exception:
                pass
        return []
    
    def save_optimization(self, optimization: Dict):
        """保存优化记录"""
        existing = self.load_optimization_log()
        existing.append(optimization)
        # 只保留最近 100 条
        existing = existing[-100:]
        try:
            self.optimization_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.optimization_log, 'w') as f:
                json.dump({"optimizations": existing, "updated_at": datetime.now().isoformat()}, f, indent=2)
        except Exception as e:
            logger.warning(f"保存优化记录失败: {e}")
    
    def analyze(self, window_hours: int = 168) -> Dict:
        """分析最近 window_hours 的自愈历史，生成优化建议"""
        history = self.load_history()
        patterns = self.load_patterns()
        
        cutoff = datetime.now() - timedelta(hours=window_hours)
        recent = [
            r for r in history
            if r.get("diagnosed_at", "") >= cutoff.isoformat()
        ]
        
        if not recent:
            return {"status": "no_data", "message": "无自愈历史数据"}
        
        # === 1. 问题分类统计 ===
        category_counter = Counter(r.get("issue_category", "unknown") for r in recent)
        issue_counter = Counter(r.get("issue_name", "unknown") for r in recent)
        
        # === 2. 修复成功率分析 ===
        fix_success_rate = sum(1 for r in recent if r.get("fix_success")) / max(1, len(recent))
        escalation_rate = sum(1 for r in recent if r.get("escalated")) / max(1, len(recent))
        
        # === 3. 重复问题识别 ===
        recurring_issues = []
        for issue_name, count in issue_counter.most_common(10):
            if count >= 3:
                issue_reports = [r for r in recent if r.get("issue_name") == issue_name]
                fixed = sum(1 for r in issue_reports if r.get("fix_success"))
                recurring_issues.append({
                    "name": issue_name,
                    "count": count,
                    "fixed": fixed,
                    "fix_rate": fixed / count if count > 0 else 0,
                })
        
        # === 4. 趋势分析 ===
        # 按天统计
        daily_counts = Counter()
        for r in recent:
            ts = r.get("diagnosed_at", "")[:10]
            if ts:
                daily_counts[ts] += 1
        
        dates = sorted(daily_counts.keys())
        trend = "stable"
        if len(dates) >= 3:
            first_half = sum(daily_counts[d] for d in dates[:len(dates)//2])
            second_half = sum(daily_counts[d] for d in dates[len(dates)//2:])
            if second_half > first_half * 1.5:
                trend = "worsening"
            elif second_half < first_half * 0.5:
                trend = "improving"
        
        # === 5. 生成优化建议 ===
        suggestions = self._generate_suggestions(recent, recurring_issues, patterns)
        
        # === 6. 构建报告 ===
        report = {
            "generated_at": datetime.now().isoformat(),
            "window_hours": window_hours,
            "total_issues": len(recent),
            "category_distribution": dict(category_counter),
            "top_issues": [{"name": n, "count": c} for n, c in issue_counter.most_common(5)],
            "fix_success_rate": round(fix_success_rate * 100, 1),
            "escalation_rate": round(escalation_rate * 100, 1),
            "recurring_issues": recurring_issues,
            "daily_trend": dict(daily_counts),
            "trend_direction": trend,
            "suggestions": suggestions,
            "new_log_patterns": len(patterns),
        }
        
        return report
    
    def _generate_suggestions(self, recent: List[Dict], recurring: List[Dict], patterns: List[Dict]) -> List[Dict]:
        """生成优化建议"""
        suggestions = []
        
        # 1. 针对重复出现的问题，建议永久修复
        for issue in recurring:
            if issue["fix_rate"] < 0.5:
                suggestions.append({
                    "type": "permanent_fix",
                    "priority": "high",
                    "issue": issue["name"],
                    "reason": f"24h 内发生 {issue['count']} 次，修复成功率仅 {issue['fix_rate']*100:.0f}%",
                    "action": "建议进行代码级永久修复，而非依赖自愈",
                })
        
        # 2. 针对新发现的日志模式
        if patterns:
            suggestions.append({
                "type": "investigate",
                "priority": "medium",
                "reason": f"发现 {len(patterns)} 个新的错误模式",
                "action": "审查日志模式库，将高频模式纳入自愈策略",
            })
        
        # 3. 针对修复率低的类别
        category_fix = {}
        for r in recent:
            cat = r.get("issue_category", "unknown")
            if cat not in category_fix:
                category_fix[cat] = {"total": 0, "fixed": 0}
            category_fix[cat]["total"] += 1
            if r.get("fix_success"):
                category_fix[cat]["fixed"] += 1
        
        for cat, stats in category_fix.items():
            rate = stats["fixed"] / stats["total"] if stats["total"] > 0 else 0
            if rate < 0.3 and stats["total"] >= 3:
                suggestions.append({
                    "type": "category_improvement",
                    "priority": "medium",
                    "category": cat,
                    "reason": f"{cat} 类别修复成功率仅 {rate*100:.0f}% ({stats['fixed']}/{stats['total']})",
                    "action": f"增强 {cat} 类别的自愈策略",
                })
        
        return suggestions
    
    def generate_report_markdown(self, report: Dict) -> str:
        """生成钉钉 Markdown 报告"""
        lines = [
            "### 🧠 自愈优化报告",
            "",
            f"**统计周期**: 最近 {report.get('window_hours', 168)} 小时",
            f"**总问题数**: {report.get('total_issues', 0)}",
            f"**修复成功率**: {report.get('fix_success_rate', 0)}%",
            f"**升级率**: {report.get('escalation_rate', 0)}%",
            f"**趋势**: {report.get('trend_direction', 'stable')}",
            "",
            "**分类分布**:",
        ]
        
        for cat, count in report.get("category_distribution", {}).items():
            lines.append(f"  - {cat}: {count}")
        
        lines.append("")
        lines.append("**TOP 5 问题**:")
        for issue in report.get("top_issues", [])[:5]:
            lines.append(f"  - {issue['name']}: {issue['count']}次")
        
        if report.get("recurring_issues"):
            lines.append("")
            lines.append("**⚠️ 重复问题** (需永久修复):")
            for issue in report["recurring_issues"]:
                lines.append(
                    f"  - {issue['name']}: {issue['count']}次, "
                    f"修复率 {issue['fix_rate']*100:.0f}%"
                )
        
        if report.get("suggestions"):
            lines.append("")
            lines.append("**📋 优化建议**:")
            for s in report["suggestions"][:5]:
                priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s.get("priority", "low"), "⚪")
                lines.append(f"  {priority_emoji} [{s['type']}] {s.get('reason', '')}")
                if s.get("action"):
                    lines.append(f"    └ {s['action']}")
        
        return "\n".join(lines)
    
    def run(self, send_alert: bool = True) -> Dict:
        """执行优化分析"""
        logger.info("开始执行自愈优化分析...")
        report = self.analyze(window_hours=168)  # 最近 7 天
        
        if report.get("status") == "no_data":
            logger.info("无自愈历史数据，跳过优化分析")
            return report
        
        # 保存优化记录
        self.save_optimization({
            "timestamp": datetime.now().isoformat(),
            "report_summary": {
                "total_issues": report.get("total_issues"),
                "fix_success_rate": report.get("fix_success_rate"),
                "trend": report.get("trend_direction"),
            },
            "suggestions_count": len(report.get("suggestions", [])),
        })
        
        # 推送报告
        if send_alert and _HAS_DINGTALK and report.get("total_issues", 0) > 0:
            try:
                markdown = self.generate_report_markdown(report)
                send_dingtalk_alert(
                    f"🧠 自愈优化报告 ({report.get('total_issues', 0)} 个问题)",
                    markdown
                )
                logger.info("自愈优化报告已推送")
            except Exception as e:
                logger.error(f"推送优化报告失败: {e}")
        
        return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    optimizer = SelfHealingOptimizer()
    report = optimizer.run(send_alert=False)
    
    if report.get("status") != "no_data":
        print("\n" + "=" * 60)
        print("自愈优化报告")
        print("=" * 60)
        print(f"总问题数: {report.get('total_issues')}")
        print(f"修复成功率: {report.get('fix_success_rate')}%")
        print(f"趋势: {report.get('trend_direction')}")
        print(f"\n优化建议: {len(report.get('suggestions', []))}")
        for s in report.get("suggestions", []):
            print(f"  [{s.get('priority')}] {s.get('reason')}")
    else:
        print("无自愈历史数据")
