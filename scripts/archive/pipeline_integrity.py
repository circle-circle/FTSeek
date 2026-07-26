#!/usr/bin/env python3
"""
FTSeek-US 管道完整性与静态审计脚本 (Pipeline Integrity Auditor)

目标：
1. 验证代码链路是否与设计一致（静态分析）
2. 发现隐蔽异常 (Silent Failures)
3. 检测配置僵尸 (Unused Configs)

使用方式：
    python scripts/audit/pipeline_integrity.py
"""

import os
import re
import ast
import sys
import json
from pathlib import Path
from typing import Dict, List, Set, Any

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
APP_DIR = ROOT_DIR / "app"
CONFIG_FILE = ROOT_DIR / "config" / "unified_config.yaml"

class AuditResult:
    def __init__(self):
        self.findings = []

    def add_finding(self, category: str, severity: str, file: str, line: int, message: str, suggestion: str):
        self.findings.append({
            "category": category,
            "severity": severity,  # CRITICAL, HIGH, MEDIUM, LOW
            "file": str(Path(file).relative_to(ROOT_DIR)) if Path(file).is_absolute() else file,
            "line": line,
            "message": message,
            "suggestion": suggestion
        })

    def print_report(self):
        print(f"\n{'='*80}")
        print(f"FTSeek-US 管道完整性审计报告")
        print(f"{'='*80}\n")
        
        if not self.findings:
            print("✅ 恭喜！未发现严重代码风险。")
            return

        by_severity = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
        for f in self.findings:
            by_severity.setdefault(f["severity"], []).append(f)

        for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
            issues = by_severity.get(sev, [])
            if issues:
                print(f"🔴 {sev} 风险 ({len(issues)} 个):")
                for i in issues:
                    print(f"  📍 {i['file']}:{i['line']}")
                    print(f"     💬 {i['message']}")
                    print(f"     💡 建议: {i['suggestion']}\n")

    def save_json(self):
        out_path = ROOT_DIR / "logs" / "audit" / "pipeline_integrity_report.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(self.findings, f, indent=2, ensure_ascii=False)
        print(f"📄 报告已保存至: {out_path}")


class PipelineAuditor(ast.NodeVisitor):
    """静态分析访问者"""

    def __init__(self, source: str, filename: str, result: AuditResult):
        self.source = source
        self.filename = filename
        self.result = result
        self.lines = source.split('\n')

    def visit_Try(self, node):
        # 检查 try...except 块
        for handler in node.handlers:
            # 1. 检查是否有 pass (隐蔽异常)
            if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                self.result.add_finding(
                    category="SilentFailure", severity="CRITICAL",
                    file=self.filename, line=handler.lineno,
                    message="发现空的 except 块 (except: pass)。异常将被静默吞掉，极难排查。",
                    suggestion="至少添加 logger.warning 记录异常堆栈。"
                )
            
            # 2. 检查是否只记录了 debug (隐蔽异常)
            if len(handler.body) == 1 and isinstance(handler.body[0], ast.Expr):
                call = handler.body[0].value
                if isinstance(call, ast.Call):
                    func = call.func
                    # 检查是否是 logger.debug
                    if (isinstance(func, ast.Attribute) and 
                        isinstance(func.value, ast.Name) and 
                        func.value.id == 'logger' and func.attr == 'debug'):
                         self.result.add_finding(
                            category="SilentFailure", severity="MEDIUM",
                            file=self.filename, line=handler.lineno,
                            message="异常仅被 logger.debug 记录。在生产环境中，这等同于没有日志。",
                            suggestion="改为 logger.warning 或 logger.error。"
                        )
        
        self.generic_visit(node)

    def visit_If(self, node):
        # 检查 if ...: continue 后的逻辑
        # 这里可以扩展更复杂的流分析，例如检查 if gate_passed: ... else: ...
        self.generic_visit(node)

    def visit_Call(self, node):
        # 检查特定函数的调用模式
        self.generic_visit(node)


class ConfigAuditor:
    """配置审计器"""
    def __init__(self, result: AuditResult):
        self.result = result
        self.config_content = ""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                self.config_content = f.read()
                self.config_keys = self._extract_keys(self.config_content)
        else:
            self.result.add_finding("Config", "CRITICAL", "config/unified_config.yaml", 0, "配置文件不存在", "检查路径")

    def _extract_keys(self, content: str) -> Set[str]:
        # 简单提取 YAML 键值（不包含注释）
        keys = set()
        for line in content.split('\n'):
            if line.strip() and not line.strip().startswith('#'):
                match = re.match(r'^(\s*)(\w+):', line)
                if match:
                    keys.add(match.group(2))
        return keys

    def check_zombie_configs(self):
        """检查代码中未使用的配置键"""
        if not self.config_keys:
            return

        # 扫描所有 Python 文件
        py_files = list(APP_DIR.rglob("*.py"))
        used_keys = set()
        
        # 简单启发式：搜索 'config.get("KEY")' 或 '.KEY'
        for py_file in py_files:
            try:
                content = py_file.read_text(encoding='utf-8')
                for key in self.config_keys:
                    if re.search(rf'["\']{key}["\']|\\.{key}\\b|get\\(\\s*[\'"]{key}[\'"]', content):
                        used_keys.add(key)
            except:
                pass

        unused = self.config_keys - used_keys
        # 过滤掉通用键
        ignore = {'enabled', 'name', 'description', 'version', 'updated', 'strategy_enhancements', 'quality_gate', 'weights'}
        
        for key in (unused - ignore):
            if len(key) > 4: # 忽略太短的键，可能是误报
                 self.result.add_finding(
                    category="ZombieConfig", severity="LOW",
                    file="config/unified_config.yaml", line=0,
                    message=f"配置键 '{key}' 在代码中未被引用。",
                    suggestion="确认是否已废弃，若是则从 YAML 中删除以保持整洁。"
                )


def run_audit():
    print("🔍 开始执行 FTSeek-US 管道完整性审计...")
    result = AuditResult()

    # 1. 静态代码分析
    critical_files = [
        "app/core/scheduler_tasks.py",
        "app/factors/trade_quality_gate.py",
        "app/factors/pivotal_detector.py",
        "app/factors/capital_flow.py",
        "app/factors/regime_filter.py",
        "app/factors/leader_pool.py",
        "app/factors/execution_assessor.py",
    ]

    for rel_path in critical_files:
        full_path = ROOT_DIR / rel_path
        if full_path.exists():
            print(f"  📄 扫描: {rel_path}")
            with open(full_path, 'r', encoding='utf-8') as f:
                source = f.read()
            try:
                tree = ast.parse(source)
                visitor = PipelineAuditor(source, rel_path, result)
                visitor.visit(tree)
            except SyntaxError as e:
                result.add_finding("Syntax", "CRITICAL", rel_path, e.lineno, f"语法错误: {e.msg}", "修复语法")

    # 2. 配置审计
    print("  ⚙️ 扫描配置文件...")
    cfg_auditor = ConfigAuditor(result)
    cfg_auditor.check_zombie_configs()

    # 3. 逻辑一致性检查 (Heuristic)
    # 检查 scheduler_tasks.py 中 TradeQualityGate 的调用是否正确使用了结果
    scheduler_path = ROOT_DIR / "app/core/scheduler_tasks.py"
    if scheduler_path.exists():
        content = scheduler_path.read_text(encoding='utf-8')
        if "TradeQualityGate" in content:
            # 检查是否检查了 passed_gate
            if "passed_gate" not in content and "passed" not in content:
                result.add_finding(
                    "LogicGap", "CRITICAL", "app/core/scheduler_tasks.py", 0,
                    "使用了 TradeQualityGate 但未检查 passed_gate 或 passed 字段。",
                    "这会导致拦截失效，所有信号都会放行！"
                )
            
            # 检查是否检查了 opportunity_score
            if "opportunity_score" not in content:
                result.add_finding(
                    "LogicGap", "MEDIUM", "app/core/scheduler_tasks.py", 0,
                    "使用了 TradeQualityGate 但未读取 opportunity_score。",
                    "无法根据分数进行分级下单 (试盘 vs 重仓)。"
                )

    # 4. 输出报告
    result.print_report()
    result.save_json()

    return len([f for f in result.findings if f['severity'] in ['CRITICAL', 'HIGH']]) == 0


if __name__ == "__main__":
    success = run_audit()
    sys.exit(0 if success else 1)
