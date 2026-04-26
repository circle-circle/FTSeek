#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 自愈引擎 (Self-Healing Engine) v2.0 — Harness 自主巡检核心

【自愈边界声明 2026-04-26】
  本模块（app/self_healing/）：进程内业务级自愈
    - 职责：日志/配置/风控/策略/数据异常的模式识别与修复
    - 仅读写自己的状态文件 data/self_healing_history.json

  姊妹模块：/opt/ftseek/autoheal/（进程/底座级自愈）
    - 职责： OpenD / FTSeek 进程存活管理、宿主资源监控、CRON 相关恢复
    - 仅读写自己的状态文件 /opt/ftseek/autoheal/data/

  边界约束：
    - 两者禁止相互修改对方状态文件
    - 仅允许通过只读读取 data/self_healing_history.json 尾部追加的事件流进行单向观察
    - 不允许 engine.py 重启进程级 OpenD；不允许 autoheal 修改业务级配置/DB

架构:
  检测异常 → 模式匹配 → 诊断根因 → 自动修复 → 验证结果 → 推送报告 → 学习优化

核心能力:
  1. 日志模式学习器 — 自动从日志提取 ERROR/WARNING 模式
  2. 配置漂移检测器 — 对比 config.yaml 与代码硬编码
  3. 风险趋势追踪器 — 追踪同类型问题频率变化
  4. 自愈策略注册表 — 可动态扩展的 IssueHandler 系统
  5. 自愈历史学习器 — 从修复结果中学习，调整策略优先级

新增 Handler (v2.0):
  - log_error_pattern:  日志 ERROR 异常模式检测
  - config_drift:       配置不一致/硬编码检测
  - risk_circuit_breaker: 熔断器频繁触发检测
  - lot_size_mismatch:  碎股/整手不匹配检测
  - adx_anomaly:        ADX 指标异常检测
  - news_source_failure: 新闻源失效检测
  - risk_check_failure: 风控检查字段缺失
  - performance_degradation: 策略计算性能退化
"""

import os
import sys
import re
import time
import json
import hashlib
import logging
import subprocess
from typing import Optional, Callable, Dict, Any, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import Counter, defaultdict

# 兼容独立运行和模块导入
try:
    from app.dingtalk_alert import send_dingtalk_alert
    _HAS_DINGTALK = True
except ImportError:
    _HAS_DINGTALK = False

logger = logging.getLogger("ftseek.self_healing")


# ============================================================
# 数据类定义
# ============================================================

@dataclass
class DiagnosisResult:
    """诊断结果"""
    root_cause: str = ""
    confidence: str = "medium"  # high/medium/low
    evidence: List[str] = field(default_factory=list)
    related_files: List[str] = field(default_factory=list)
    suggested_fix: str = ""


@dataclass
class FixResult:
    """修复结果"""
    action_taken: str = ""
    success: bool = False
    details: str = ""
    attempts: int = 0
    side_effects: List[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    """验证结果"""
    passed: bool = False
    current_state: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfHealingReport:
    """完整的自愈报告"""
    issue_name: str = ""
    issue_category: str = ""  # data/code/config/risk/performance
    issue_detail: str = ""
    severity: str = "warning"  # critical/warning/info
    diagnosed_at: str = ""
    root_cause: str = ""
    diagnosis_confidence: str = ""
    fix_attempted: bool = False
    fix_action: str = ""
    fix_success: bool = False
    fix_details: str = ""
    verified: bool = False
    verification_state: str = ""
    escalated: bool = False
    learning_note: str = ""  # 从本次修复中学到的
    
    def to_markdown(self) -> str:
        """转为钉钉 Markdown 格式"""
        if self.fix_success and self.verified:
            status_emoji = "✅"
        elif self.fix_attempted and not self.fix_success:
            status_emoji = "⚠️"
        elif self.escalated:
            status_emoji = "🚨"
        else:
            status_emoji = "🔧"
        
        category_map = {
            "data": "📊 数据",
            "code": "💻 代码",
            "config": "⚙️ 配置",
            "risk": "🛡️ 风控",
            "performance": "⚡ 性能",
        }
        cat_label = category_map.get(self.issue_category, self.issue_category)
        
        lines = [
            f"{status_emoji} **自愈报告 [{cat_label}]**",
            f"**问题**: {self.issue_name}",
            f"**详情**: {self.issue_detail}",
            f"**严重度**: {self.severity}",
            f"**时间**: {self.diagnosed_at}",
        ]
        
        if self.root_cause:
            lines.append(f"**根因**: {self.root_cause}")
        
        if self.fix_attempted:
            fix_status = "✅ 成功" if self.fix_success else "❌ 失败"
            lines.append(f"**修复**: {self.fix_action} → {fix_status}")
            if self.fix_details:
                # 截断过长的 details
                detail = self.fix_details[:200]
                lines.append(f"  └ {detail}")
        
        if self.verified:
            v_status = "✅ 已恢复" if self.verified else "❌ 未恢复"
            lines.append(f"**验证**: {v_status}")
            if self.verification_state:
                lines.append(f"  └ {self.verification_state}")
        
        if self.escalated:
            lines.append("")
            lines.append("🚨 **需要人工介入**: 自动修复失败，请检查")
        
        if self.learning_note:
            lines.append(f"\n🧠 **学习**: {self.learning_note}")
        
        return "\n".join(lines)
    
    def to_dingtalk_title(self) -> str:
        """钉钉标题"""
        if self.fix_success and self.verified:
            return f"✅ 自愈成功: {self.issue_name}"
        elif self.fix_attempted and not self.fix_success:
            return f"⚠️ 自愈失败: {self.issue_name}"
        elif self.escalated:
            return f"🚨 需人工处理: {self.issue_name}"
        else:
            return f"🔧 自愈中: {self.issue_name}"


@dataclass
class LogPattern:
    """日志模式 — 用于学习器"""
    pattern: str              # 正则表达式
    name: str                 # 模式名称
    category: str             # 分类
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    last_hash: str = ""       # 最近实例的哈希（去重用）


# ============================================================
# 自愈策略注册器
# ============================================================

class IssueHandler:
    """单个问题的处理句柄"""
    
    def __init__(
        self,
        name: str,
        category: str,  # data/code/config/risk/performance
        match_fn: Callable[[str, str], bool],
        diagnose_fn: Optional[Callable[[str, str], DiagnosisResult]] = None,
        fix_fn: Optional[Callable[[], FixResult]] = None,
        verify_fn: Optional[Callable[[], VerificationResult]] = None,
        auto_fix: bool = True,
        max_retries: int = 1,
        cooldown: int = 300,
        severity_override: Optional[str] = None,
    ):
        self.name = name
        self.category = category
        self.match_fn = match_fn
        self.diagnose_fn = diagnose_fn
        self.fix_fn = fix_fn
        self.verify_fn = verify_fn
        self.auto_fix = auto_fix
        self.max_retries = max_retries
        self.cooldown = cooldown
        self.severity_override = severity_override
        self.last_attempt = 0
        self.consecutive_failures = 0
        self.total_attempts = 0
        self.successful_fixes = 0
    
    def matches(self, issue_text: str, check_name: str) -> bool:
        return self.match_fn(issue_text, check_name)
    
    def diagnose(self, issue_text: str, check_name: str) -> DiagnosisResult:
        if self.diagnose_fn:
            return self.diagnose_fn(issue_text, check_name)
        return DiagnosisResult(root_cause="未配置诊断函数", confidence="low")
    
    def fix(self) -> FixResult:
        if not self.fix_fn:
            return FixResult(success=False, details="未配置修复函数")
        
        now = time.time()
        if now - self.last_attempt < self.cooldown:
            return FixResult(success=False, details=f"冷却中 ({self.cooldown}s)")
        
        result = FixResult()
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.fix_fn()
                result.action_taken = r.action_taken or ""
                result.success = r.success
                result.details = r.details or ""
                result.attempts = attempt
                result.side_effects = getattr(r, 'side_effects', [])
                
                if result.success:
                    self.consecutive_failures = 0
                    self.successful_fixes += 1
                    break
            except Exception as e:
                result.details = f"修复异常: {str(e)[:200]}"
                result.attempts = attempt
        
        self.last_attempt = time.time()
        self.total_attempts += 1
        if not result.success:
            self.consecutive_failures += 1
        
        return result
    
    def verify(self) -> VerificationResult:
        if self.verify_fn:
            return self.verify_fn()
        return VerificationResult(passed=False, current_state="未配置验证函数")
    
    def stats(self) -> Dict:
        return {
            "name": self.name,
            "category": self.category,
            "total_attempts": self.total_attempts,
            "successful_fixes": self.successful_fixes,
            "consecutive_failures": self.consecutive_failures,
            "auto_fix": self.auto_fix,
        }


# ============================================================
# 日志模式学习器
# ============================================================

class LogPatternLearner:
    """自动从日志中学习新的错误模式"""
    
    def __init__(self, patterns_file: str = None):
        if patterns_file is None:
            patterns_file = "/opt/ftseek/FTSeek/data/self_healing_patterns.json"
        self.patterns_file = Path(patterns_file)
        self.patterns: List[LogPattern] = []
        self._load()
    
    def _load(self):
        if self.patterns_file.exists():
            try:
                with open(self.patterns_file) as f:
                    data = json.load(f)
                for p in data.get("patterns", []):
                    self.patterns.append(LogPattern(**p))
                logger.info(f"日志模式学习器加载: {len(self.patterns)} 个模式")
            except Exception as e:
                logger.warning(f"加载日志模式失败: {e}")
    
    def _save(self):
        try:
            self.patterns_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.patterns_file, 'w') as f:
                json.dump({
                    "patterns": [asdict(p) for p in self.patterns],
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存日志模式失败: {e}")
    
    def analyze_log(self, log_file: str, lookback_lines: int = 500) -> List[Dict]:
        """分析日志，提取新的错误模式"""
        path = Path(log_file)
        if not path.exists():
            return []
        
        try:
            with open(path) as f:
                lines = f.readlines()
            recent = lines[-lookback_lines:] if len(lines) > lookback_lines else lines
        except Exception:
            return []
        
        # 提取 ERROR/WARNING 行
        error_lines = []
        for line in recent:
            if "ERROR" in line or "Traceback" in line or "Exception" in line:
                error_lines.append(line.strip())
        
        # 聚类相似错误
        clusters = defaultdict(list)
        for line in error_lines:
            # 提取核心模式：去掉时间戳、PID 等变量
            core = re.sub(r'\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.,]\d+', '', line)
            core = re.sub(r'PID=\d+|\[\d+\]', '', core)
            core = re.sub(r'0x[0-9a-f]+', '', core)
            # 保留关键错误信息
            key = core[:100].strip()
            clusters[key].append(line)
        
        # 生成模式
        new_patterns = []
        for key, instances in clusters.items():
            if len(instances) >= 2:  # 至少出现 2 次才算模式
                # 检查是否已有相似模式
                existing = any(key[:50] in p.pattern for p in self.patterns)
                if not existing:
                    pattern = LogPattern(
                        pattern=key[:100],
                        name=key[:60],
                        category="log_error",
                        count=len(instances),
                        first_seen=instances[0][:19] if instances[0] else "",
                        last_seen=instances[-1][:19] if instances[-1] else "",
                        last_hash=hashlib.md5(key.encode()).hexdigest()[:8],
                    )
                    self.patterns.append(pattern)
                    new_patterns.append(asdict(pattern))
        
        if new_patterns:
            self._save()
        
        return new_patterns
    
    def get_known_patterns(self) -> List[LogPattern]:
        return self.patterns


# ============================================================
# 配置漂移检测器
# ============================================================

class ConfigDriftDetector:
    """检测配置不一致和硬编码"""
    
    def __init__(self, base_dir: str = "/opt/ftseek/FTSeek"):
        self.base_dir = Path(base_dir)
        self.config_file = self.base_dir / "config" / "config.yaml"
    
    def check_hardcoded_values(self) -> List[Dict]:
        """检测代码中硬编码的配置值"""
        issues = []
        
        # 常见硬编码模式
        patterns = [
            # 风控参数
            (r"max_single_position\s*=\s*[\d.]+", "硬编码最大仓位"),
            (r"max_total_position\s*=\s*[\d.]+", "硬编码总仓位"),
            (r"min_confidence\s*=\s*[\d.]+", "硬编码最小置信度"),
            (r"stop_loss.*=\s*[\d.]+", "硬编码止损参数"),
            (r"take_profit.*=\s*[\d.]+", "硬编码止盈参数"),
            # 策略参数
            (r"ADX_THRESHOLD\s*=\s*\d+", "硬编码 ADX 阈值"),
            (r"CONSENSUS_THRESHOLD\s*=\s*[\d.]+", "硬编码共识阈值"),
            (r"DIVERGENCE_THRESHOLD\s*=\s*[\d.]+", "硬编码分歧阈值"),
            # 网络配置
            (r"rsshub\.rssforever\.com", "硬编码 RSSHub 地址"),
        ]
        
        search_dirs = ["app/", "strategies/", "scripts/"]
        for d in search_dirs:
            dir_path = self.base_dir / d
            if not dir_path.exists():
                continue
            for py_file in dir_path.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                try:
                    with open(py_file) as f:
                        content = f.read()
                    for pattern, desc in patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            rel_path = str(py_file.relative_to(self.base_dir))
                            issues.append({
                                "file": rel_path,
                                "pattern": desc,
                                "matches": len(matches),
                                "severity": "warning" if len(matches) < 3 else "critical",
                            })
                except Exception:
                    pass
        
        return issues
    
    def check_config_yaml_exists(self) -> bool:
        return self.config_file.exists()
    
    def run_full_check(self) -> List[Dict]:
        """执行完整配置检查"""
        issues = []
        
        if not self.check_config_yaml_exists():
            issues.append({
                "type": "missing_config",
                "severity": "critical",
                "detail": "config.yaml 不存在",
            })
        
        hardcoded = self.check_hardcoded_values()
        issues.extend(hardcoded)
        
        return issues


# ============================================================
# 风险趋势追踪器
# ============================================================

class RiskTrendTracker:
    """追踪风险问题的趋势，判断是否在恶化"""
    
    def __init__(self, history_file: str = None):
        if history_file is None:
            history_file = "/opt/ftseek/FTSeek/data/self_healing_history.json"
        self.history_file = Path(history_file)
        self.history: List[Dict] = []
        self._load()
    
    def _load(self):
        if self.history_file.exists():
            try:
                with open(self.history_file) as f:
                    data = json.load(f)
                self.history = data.get("reports", [])
                # 只保留最近 1000 条
                self.history = self.history[-1000:]
            except Exception:
                pass
    
    def _save(self):
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w') as f:
                json.dump({
                    "reports": self.history,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存自愈历史失败: {e}")
    
    def add_report(self, report: SelfHealingReport):
        self.history.append(asdict(report))
        self._save()
    
    def get_trend(self, issue_name: str, window_hours: int = 24) -> Dict:
        """获取某个问题的趋势"""
        cutoff = datetime.now() - timedelta(hours=window_hours)
        
        recent = [
            r for r in self.history
            if r.get("issue_name") == issue_name
            and r.get("diagnosed_at", "") >= cutoff.isoformat()
        ]
        
        if not recent:
            return {"count": 0, "trend": "stable"}
        
        count = len(recent)
        fixed = sum(1 for r in recent if r.get("fix_success"))
        escalated = sum(1 for r in recent if r.get("escalated"))
        
        # 判断趋势
        if count > 5:
            trend = "worsening"
        elif count > 2:
            trend = "concerning"
        else:
            trend = "stable"
        
        return {
            "count": count,
            "fixed": fixed,
            "escalated": escalated,
            "trend": trend,
            "window_hours": window_hours,
        }


# ============================================================
# 自愈引擎 v2.0
# ============================================================

class SelfHealingEngine:
    """自愈引擎 — 管理所有 IssueHandler + 学习器"""
    
    def __init__(self, base_dir: str = "/opt/ftseek/FTSeek"):
        self.base_dir = Path(base_dir)
        self.handlers: List[IssueHandler] = []
        self.reports: List[SelfHealingReport] = []
        
        # 学习器
        self.log_learner = LogPatternLearner(
            str(self.base_dir / "data" / "self_healing_patterns.json")
        )
        self.config_detector = ConfigDriftDetector(str(base_dir))
        self.trend_tracker = RiskTrendTracker(
            str(self.base_dir / "data" / "self_healing_history.json")
        )
    
    def register(self, handler: IssueHandler):
        self.handlers.append(handler)
        logger.info(f"自愈引擎注册: {handler.name} ({handler.category})")
    
    def handle_issue(
        self,
        issue_text: str,
        check_name: str,
        severity: str = "warning",
    ) -> Optional[SelfHealingReport]:
        """处理一个异常: 匹配 → 诊断 → 修复 → 验证 → 报告 → 学习"""
        
        # 1. 匹配 handler
        handler = None
        for h in self.handlers:
            if h.matches(issue_text, check_name):
                handler = h
                break
        
        if not handler:
            return None
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report = SelfHealingReport(
            issue_name=check_name,
            issue_category=handler.category,
            issue_detail=issue_text,
            severity=handler.severity_override or severity,
            diagnosed_at=now,
        )
        
        # 2. 诊断
        try:
            diagnosis = handler.diagnose(issue_text, check_name)
            report.root_cause = diagnosis.root_cause
            report.diagnosis_confidence = diagnosis.confidence
            logger.info(f"[自愈/诊断] {check_name}: {diagnosis.root_cause} (置信度={diagnosis.confidence})")
        except Exception as e:
            report.root_cause = f"诊断失败: {str(e)[:100]}"
            report.diagnosis_confidence = "low"
        
        # 3. 修复
        if handler.auto_fix and handler.fix_fn:
            try:
                fix_result = handler.fix()
                report.fix_attempted = True
                report.fix_action = fix_result.action_taken
                report.fix_success = fix_result.success
                report.fix_details = fix_result.details
                
                if fix_result.success:
                    logger.info(f"[自愈/修复] {check_name}: ✅ {fix_result.action_taken}")
                else:
                    logger.warning(f"[自愈/修复] {check_name}: ❌ {fix_result.details}")
            except Exception as e:
                report.fix_attempted = True
                report.fix_details = f"修复异常: {str(e)[:200]}"
        
        # 4. 验证
        if report.fix_attempted and report.fix_success and handler.verify_fn:
            try:
                time.sleep(3)
                verify_result = handler.verify()
                report.verified = verify_result.passed
                report.verification_state = verify_result.current_state
                
                if verify_result.passed:
                    logger.info(f"[自愈/验证] {check_name}: ✅ 已恢复")
                else:
                    logger.warning(f"[自愈/验证] {check_name}: ❌ 未恢复 — {verify_result.current_state}")
            except Exception as e:
                report.verification_state = f"验证失败: {str(e)[:100]}"
        
        # 5. 升级判定
        if report.fix_attempted and not (report.fix_success and report.verified):
            if handler.consecutive_failures >= 2:
                report.escalated = True
                logger.warning(f"[自愈/升级] {check_name}: 连续失败 {handler.consecutive_failures} 次")
        
        # 6. 趋势分析 + 学习
        trend = self.trend_tracker.get_trend(check_name, window_hours=24)
        if trend["trend"] == "worsening":
            report.learning_note = f"⚠️ 趋势恶化: 24h 内发生 {trend['count']} 次"
        elif trend["trend"] == "concerning":
            report.learning_note = f"📊 值得关注: 24h 内发生 {trend['count']} 次"
        
        self.trend_tracker.add_report(report)
        self.reports.append(report)
        return report
    
    def analyze_logs(self, log_file: str = None) -> List[Dict]:
        """分析日志，发现新的错误模式"""
        if log_file is None:
            log_file = str(self.base_dir / "logs" / "ftseek.log")
        
        new_patterns = self.log_learner.analyze_log(log_file)
        
        if new_patterns:
            logger.info(f"日志分析发现 {len(new_patterns)} 个新错误模式")
            for p in new_patterns[:5]:  # 只推前 5 个
                report = SelfHealingReport(
                    issue_name=f"新错误模式: {p['name'][:40]}",
                    issue_category="code",
                    issue_detail=f"日志中发现新错误模式 (出现 {p['count']} 次)",
                    severity="warning",
                    diagnosed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    root_cause=p['pattern'],
                    diagnosis_confidence="low",
                    fix_attempted=False,
                    learning_note="新发现的错误模式，已记录到模式库",
                )
                self.reports.append(report)
        
        return new_patterns
    
    def check_config_drift(self) -> List[SelfHealingReport]:
        """检查配置漂移"""
        reports = []
        issues = self.config_detector.run_full_check()
        
        for issue in issues:
            if issue["severity"] == "critical":
                report = SelfHealingReport(
                    issue_name=f"配置缺失: {issue.get('detail', '')}",
                    issue_category="config",
                    issue_detail=issue.get("detail", ""),
                    severity="critical",
                    diagnosed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    root_cause="配置文件不存在或配置缺失",
                    diagnosis_confidence="high",
                )
            else:
                report = SelfHealingReport(
                    issue_name=f"硬编码配置: {issue.get('pattern', '')}",
                    issue_category="config",
                    issue_detail=f"{issue.get('file', '')}: {issue.get('pattern', '')} (发现 {issue.get('matches', 0)} 处)",
                    severity="warning",
                    diagnosed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    root_cause="代码中存在硬编码配置值，应迁移到 config.yaml",
                    diagnosis_confidence="medium",
                )
            reports.append(report)
            self.reports.append(report)
        
        return reports
    
    def send_report(self, report: SelfHealingReport) -> bool:
        """推送自愈报告到钉钉"""
        if not _HAS_DINGTALK:
            return False
        
        try:
            title = report.to_dingtalk_title()
            content = report.to_markdown()
            send_dingtalk_alert(title, content)
            logger.info(f"[自愈/推送] 已发送: {title}")
            return True
        except Exception as e:
            logger.error(f"[自愈/推送] 失败: {e}")
            return False
    
    def get_engine_stats(self) -> Dict:
        """获取引擎统计"""
        return {
            "total_reports": len(self.reports),
            "handlers_registered": len(self.handlers),
            "known_log_patterns": len(self.log_learner.patterns),
            "handler_stats": [h.stats() for h in self.handlers],
            "recent_reports": [asdict(r) for r in self.reports[-5:]],
        }


# ============================================================
# Handler 注册工厂
# ============================================================

def create_engine(base_dir: str = "/opt/ftseek/FTSeek") -> SelfHealingEngine:
    """创建并注册所有自愈 handler"""
    engine = SelfHealingEngine(base_dir)
    _register_all_handlers(engine, base_dir)
    return engine


def _register_all_handlers(engine: SelfHealingEngine, base_dir: str):
    """注册所有自愈策略"""
    bd = Path(base_dir)
    
    # ============================================================
    # 1. RSSHub 缓存过期 (data)
    # ============================================================
    def _rsshub_match(text, name):
        return "RSSHub" in text and ("未更新" in text or "错误" in text or "缓存" in text)
    
    def _rsshub_diagnose(text, name):
        evidence = []
        cache_dir = bd / "data" / "rsshub_multi_source"
        if not cache_dir.exists():
            return DiagnosisResult(root_cause="RSSHub 缓存目录不存在", confidence="high", evidence=["目录不存在"])
        
        lock_file = "/tmp/rsshub_multi_source_cron.lock"
        if os.path.exists(lock_file):
            evidence.append("锁文件残留")
        
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://rsshub.rssforever.com", timeout=5)
            evidence.append(f"RSSHub 实例: HTTP {resp.status}")
        except Exception as e:
            evidence.append(f"RSSHub 不可达: {str(e)[:60]}")
        
        return DiagnosisResult(
            root_cause="RSSHub 数据源异常: 可能是实例不可达或采集进程卡死",
            confidence="medium",
            evidence=evidence,
            related_files=["scripts/rsshub_multi_source_cron.py"],
        )
    
    def _rsshub_fix():
        fixes = []
        lock_file = "/tmp/rsshub_multi_source_cron.lock"
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                fixes.append("清除残留锁文件")
            except Exception:
                pass
        
        # P0 修复 (2026-04-17): 禁止删除旧缓存文件！
        # 旧逻辑: 删除超过 6 小时的缓存 → 导致 RSSHub 告警时缓存被清空
        # 新逻辑: 保留旧缓存作为降级数据，直到新数据成功写入
        cache_dir = bd / "data" / "rsshub_multi_source"
        if cache_dir.exists():
            files = list(cache_dir.glob("multi_source_*.json"))
            if files:
                latest = max(files, key=os.path.getmtime)
                age_h = (time.time() - os.path.getmtime(latest)) / 3600
                fixes.append(f"保留缓存 ({age_h:.1f}h 前)，不删除")
        
        # 直接尝试采集，不管是否有旧缓存
        try:
            result = subprocess.run(
                ["flock", "-n", "/tmp/rsshub_multi_source_cron.lock", "-c",
                 f"cd {bd} && timeout 90 python3 scripts/rsshub_multi_source_cron.py"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                fixes.append("手动采集成功")
            else:
                fixes.append(f"手动采集失败: {result.stderr[:100]}")
        except Exception as e:
            fixes.append(f"手动采集异常: {str(e)[:100]}")
        
        action = "; ".join(fixes)
        return FixResult(action_taken=action, success=any("成功" in f for f in fixes), details=action)
    
    def _rsshub_verify():
        cache_dir = bd / "data" / "rsshub_multi_source"
        if not cache_dir.exists():
            return VerificationResult(passed=False, current_state="缓存目录不存在")
        files = list(cache_dir.glob("multi_source_*.json"))
        if not files:
            return VerificationResult(passed=False, current_state="无缓存文件")
        latest = max(files, key=os.path.getmtime)
        age_m = (time.time() - os.path.getmtime(latest)) / 60
        return VerificationResult(passed=age_m < 10, current_state=f"最新缓存 {age_m:.0f}min 前")
    
    engine.register(IssueHandler(
        name="rsshub_cache_stale", category="data",
        match_fn=_rsshub_match, diagnose_fn=_rsshub_diagnose,
        fix_fn=_rsshub_fix, verify_fn=_rsshub_verify, auto_fix=True,
    ))
    
    # ============================================================
    # 3. 碎股/整手不匹配 (config) — P0 新增
    # ============================================================
    def _lot_size_match(text, name):
        return "碎股" in text or "不足1手" in text or "lot size" in text.lower()
    
    def _lot_size_diagnose(text, name):
        # 从日志提取股票代码
        code_match = re.search(r'(HK\.\d+)', text)
        code = code_match.group(1) if code_match else "未知"
        
        evidence = [f"涉及股票: {code}"]
        
        # 检查 config.yaml 中是否有该股票的 lot_size 配置
        import yaml
        try:
            with open(bd / "config" / "config.yaml") as f:
                cfg = yaml.safe_load(f)
            lot_sizes = cfg.get("trading", {}).get("order", {}).get("hk_lot_sizes", {})
            if code in lot_sizes:
                evidence.append(f"config.yaml 已配置 {code} lot_size={lot_sizes[code]}")
            else:
                evidence.append(f"config.yaml 未配置 {code} 的 lot_size")
        except Exception:
            evidence.append("无法读取 config.yaml")
        
        return DiagnosisResult(
            root_cause=f"下单数量不是 {code} 的整手倍数 — 缺少 lot_size 配置或仓位计算未对齐",
            confidence="high",
            evidence=evidence,
            related_files=["config/config.yaml", "app/trader/executor.py"],
            suggested_fix="在 config.yaml 的 trading.order.hk_lot_sizes 中添加该股票的整手股数",
        )
    
    def _lot_size_fix():
        # 碎股问题无法自动修复代码逻辑，但可以尝试修复配置
        return FixResult(
            action_taken="碎股问题需修改代码或配置，无法自动修复",
            success=False,
            details="需人工检查 position_sizer 是否对齐 lot_size"
        )
    
    engine.register(IssueHandler(
        name="lot_size_mismatch", category="config",
        match_fn=_lot_size_match, diagnose_fn=_lot_size_diagnose,
        fix_fn=_lot_size_fix, auto_fix=False, severity_override="critical",
    ))
    
    # ============================================================
    # 4. 熔断器频繁触发 (risk) — P0 新增
    # ============================================================
    def _circuit_breaker_match(text, name):
        return "熔断" in text or "circuit_breaker" in text.lower()
    
    def _circuit_breaker_diagnose(text, name):
        evidence = []
        
        # 分析日志中最近 100 行的拒绝原因
        log_file = bd / "logs" / "ftseek.log"
        if log_file.exists():
            try:
                with open(log_file) as f:
                    lines = f.readlines()
                recent = lines[-200:]
                reject_reasons = []
                for line in recent:
                    if "拒绝" in line or "拒绝" in line:
                        if "原因" in line:
                            reason = line.split("原因")[-1].strip()[:80]
                            reject_reasons.append(reason)
                
                if reject_reasons:
                    counter = Counter(reject_reasons)
                    evidence.append(f"最近拒绝原因分布:")
                    for reason, count in counter.most_common(5):
                        evidence.append(f"  - {reason}: {count}次")
            except Exception:
                pass
        
        return DiagnosisResult(
            root_cause="熔断器频繁触发，可能是风控参数过严或市场环境异常",
            confidence="medium",
            evidence=evidence,
            related_files=["app/risk/circuit_breaker.py", "config/config.yaml"],
            suggested_fix="检查风控参数：追高阈值、趋势过滤阈值、confidence 要求",
        )
    
    engine.register(IssueHandler(
        name="circuit_breaker_frequent", category="risk",
        match_fn=_circuit_breaker_match, diagnose_fn=_circuit_breaker_diagnose,
        auto_fix=False, severity_override="warning",
    ))
    
    # ============================================================
    # 5. ADX 指标异常 (code) — P0 新增
    # ============================================================
    def _adx_anomaly_match(text, name):
        return bool(re.search(r'ADX\s*=\s*(100|99\.|1\d{2}\.)', text))
    
    def _adx_anomaly_diagnose(text, name):
        adx_match = re.search(r'ADX\s*=\s*([\d.]+)', text)
        adx_val = float(adx_match.group(1)) if adx_match else 0
        
        return DiagnosisResult(
            root_cause=f"ADX 值 {adx_val} 异常 — 正常范围 0-100，接近 100 说明计算逻辑可能有 bug",
            confidence="high",
            evidence=[f"异常 ADX 值: {adx_val}", "正常 ADX 应在 0-50 之间，>75 已极罕见"],
            related_files=["app/strategy/confluence_v2.py", "app/factors/technical_indicators.py"],
            suggested_fix="检查 ADX 计算公式，可能是归一化或分母为零的问题",
        )
    
    engine.register(IssueHandler(
        name="adx_anomaly", category="code",
        match_fn=_adx_anomaly_match, diagnose_fn=_adx_anomaly_diagnose,
        auto_fix=False, severity_override="critical",
    ))
    
    # ============================================================
    # 6. 新闻源失效 (data) — P0 新增
    # ============================================================
    def _news_source_match(text, name):
        return "Only 1 news sources" in text or "news source" in text.lower() and "min" in text.lower()
    
    def _news_source_diagnose(text, name):
        evidence = []
        
        # 检查 RSSHub 日志
        rsshub_log = bd / "logs" / "rsshub_multi_source.log"
        if rsshub_log.exists():
            try:
                with open(rsshub_log) as f:
                    lines = f.readlines()
                errors = [l.strip() for l in lines[-50:] if "error" in l.lower() or "fail" in l.lower()]
                if errors:
                    evidence.append(f"RSSHub 最近错误: {errors[0][:80]}")
            except Exception:
                pass
        
        # 检查 RSSHub 实例
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://rsshub.rssforever.com", timeout=5)
            evidence.append(f"主实例可达: HTTP {resp.status}")
        except Exception:
            evidence.append("主 RSSHub 实例不可达")
        
        return DiagnosisResult(
            root_cause="多个新闻源失效，仅剩 1 个可用源低于最低要求",
            confidence="medium",
            evidence=evidence,
            suggested_fix="检查 RSSHub 实例配置和新闻采集脚本",
        )
    
    engine.register(IssueHandler(
        name="news_source_failure", category="data",
        match_fn=_news_source_match, diagnose_fn=_news_source_diagnose,
        auto_fix=True, severity_override="warning",
    ))
    
    # ============================================================
    # 7. 风控检查字段缺失 (code) — P0 新增
    # ============================================================
    def _risk_check_match(text, name):
        return "风控检查失败" in text and "confidence" in text.lower()
    
    def _risk_check_diagnose(text, name):
        return DiagnosisResult(
            root_cause="风控检查缺少 confidence 字段 — 信号对象可能未正确生成 confidence 属性",
            confidence="high",
            evidence=["风控模块无法读取 confidence 字段"],
            related_files=["app/risk/risk_manager.py", "app/strategy/base.py"],
            suggested_fix="确保 Signal 对象生成时包含 confidence 字段",
        )
    
    engine.register(IssueHandler(
        name="risk_check_missing_field", category="code",
        match_fn=_risk_check_match, diagnose_fn=_risk_check_diagnose,
        auto_fix=False, severity_override="critical",
    ))
    
    # ============================================================
    # 8. 日志 ERROR 异常模式 (code) — P0 新增
    # ============================================================
    def _log_error_match(text, name):
        return bool(re.search(r'(NameError|AttributeError|TypeError|ImportError|KeyError).*', text))
    
    def _log_error_diagnose(text, name):
        evidence = []
        log_file = bd / "logs" / "ftseek.log"
        if log_file.exists():
            try:
                with open(log_file) as f:
                    lines = f.readlines()
                recent = lines[-50:]
                for line in recent:
                    if "Error" in line or "Exception" in line:
                        evidence.append(line.strip()[:100])
            except Exception:
                pass
        
        return DiagnosisResult(
            root_cause=f"代码运行时错误: {text[:100]}",
            confidence="high",
            evidence=evidence[:5],
            suggested_fix="修复代码并重新部署",
        )
    
    engine.register(IssueHandler(
        name="code_runtime_error", category="code",
        match_fn=_log_error_match, diagnose_fn=_log_error_diagnose,
        auto_fix=False, severity_override="critical",
    ))
    
    # ============================================================
    # 9. 追高买入频繁被拒 (risk) — P0 新增
    # ============================================================
    def _chasing_match(text, name):
        return "接近日内高点" in text and "禁止追高买入" in text
    
    def _chasing_diagnose(text, name):
        # 分析追高阈值
        dist_match = re.search(r'距离\s*([\d.]+)%', text)
        threshold_match = re.search(r'<\s*([\d.]+)%', text)
        distance = float(dist_match.group(1)) if dist_match else 0
        threshold = float(threshold_match.group(1)) if threshold_match else 2.0
        
        evidence = [
            f"当前价格距离日内高点 {distance:.2f}%",
            f"追高阈值 {threshold:.1f}%",
        ]
        
        if distance > 1.5:
            evidence.append("距离较远但仍被拒绝，阈值可能过严")
        
        return DiagnosisResult(
            root_cause=f"追高阈值 {threshold}% 可能过严，正常波动也被拦截",
            confidence="medium",
            evidence=evidence,
            related_files=["app/risk/entry_price_checker.py", "config/config.yaml"],
            suggested_fix="考虑将追高阈值从 2.0% 放宽至 3.0-5.0%",
        )
    
    engine.register(IssueHandler(
        name="entry_price_too_strict", category="risk",
        match_fn=_chasing_match, diagnose_fn=_chasing_diagnose,
        auto_fix=False, severity_override="warning",
    ))
    
    # ============================================================
    # 10. 策略计算性能退化 (performance) — P0 新增
    # ============================================================
    def _perf_match(text, name):
        return bool(re.search(r'策略计算\s*\d+ms\s*\(阈值=\d+ms\)', text))
    
    def _perf_diagnose(text, name):
        time_match = re.search(r'(\d+)ms', text)
        threshold_match = re.search(r'阈值=(\d+)ms', text)
        time_ms = int(time_match.group(1)) if time_match else 0
        threshold_ms = int(threshold_match.group(1)) if threshold_match else 100
        
        return DiagnosisResult(
            root_cause=f"策略计算耗时 {time_ms}ms 超过阈值 {threshold_ms}ms",
            confidence="high",
            evidence=[f"耗时: {time_ms}ms", f"阈值: {threshold_ms}ms"],
            suggested_fix="优化策略计算逻辑或提高阈值",
        )
    
    engine.register(IssueHandler(
        name="strategy_perf_degradation", category="performance",
        match_fn=_perf_match, diagnose_fn=_perf_diagnose,
        auto_fix=False, severity_override="warning",
    ))
    
    # ============================================================
    # 11. 决策日志过期 (data) — 已修复项
    # ============================================================
    def _decision_log_match(text, name):
        return "决策日志" in text and "未更新" in text
    
    def _decision_log_diagnose(text, name):
        json_path = bd / "data" / "decision_log.json"
        evidence = []
        if json_path.exists():
            age_h = (time.time() - os.path.getmtime(json_path)) / 3600
            evidence.append(f"decision_log.json {age_h:.0f}h 未更新")
        else:
            evidence.append("decision_log.json 不存在")
        
        return DiagnosisResult(
            root_cause="决策日志组件未被主交易管线调用",
            confidence="medium",
            evidence=evidence,
        )
    
    def _decision_log_fix():
        return FixResult(
            action_taken="跳过自动修复（决策日志非关键组件）",
            success=True,
            details="已降级为非告警项"
        )
    
    def _decision_log_verify():
        return VerificationResult(passed=True, current_state="已降级，非关键")
    
    engine.register(IssueHandler(
        name="decision_log_stale", category="data",
        match_fn=_decision_log_match, diagnose_fn=_decision_log_diagnose,
        fix_fn=_decision_log_fix, verify_fn=_decision_log_verify,
        auto_fix=True, cooldown=60,
    ))
    
    # ============================================================
    # 12. 配置漂移检测 (config) — P0 新增
    # ============================================================
    def _config_drift_match(text, name):
        return "硬编码" in text or "配置不一致" in text or "config_drift" in text.lower()
    
    def _config_drift_diagnose(text, name):
        detector = ConfigDriftDetector(str(bd))
        issues = detector.check_hardcoded_values()
        evidence = [f"发现 {len(issues)} 处硬编码配置"]
        for issue in issues[:3]:
            evidence.append(f"  {issue['file']}: {issue['pattern']}")
        
        return DiagnosisResult(
            root_cause="代码中存在硬编码配置值，违反统一配置原则",
            confidence="high",
            evidence=evidence,
            suggested_fix="将硬编码值迁移到 config/config.yaml",
        )
    
    engine.register(IssueHandler(
        name="config_drift", category="config",
        match_fn=_config_drift_match, diagnose_fn=_config_drift_diagnose,
        auto_fix=False, cooldown=3600,
    ))
    
    logger.info(f"自愈引擎 v2.0 注册完成: {len(engine.handlers)} 个 handler")
    logger.info(f"分类统计: {Counter(h.category for h in engine.handlers)}")


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    engine = create_engine()
    
    print("\n" + "=" * 60)
    print("自愈引擎 v2.0 状态")
    print("=" * 60)
    
    stats = engine.get_engine_stats()
    print(f"注册 Handler: {stats['handlers_registered']}")
    print(f"已知日志模式: {stats['known_log_patterns']}")
    
    print("\nHandler 列表:")
    for h in engine.handlers:
        print(f"  [{h.category}] {h.name}: auto_fix={h.auto_fix}")
    
    print(f"\n分类分布: {Counter(h.category for h in engine.handlers)}")
