#!/usr/bin/env python3
"""
配置一致性审计脚本 — 三层防御核心

功能：
1. 扫描所有 .py 文件中的硬编码 fallback 值
2. 与 config.yaml 中的定义比对
3. 发现不一致立即报错（可配置为阻断启动）
4. 生成钉钉告警

使用方式：
    python3 scripts/config_audit.py          # 扫描并报告
    python3 scripts/config_audit.py --block  # 不一致时返回 exit code 1
"""

import os
import re
import sys
import yaml
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('config_audit')

# ============================================================
#  审计规则定义
#  格式: (yaml_path, 描述, 搜索正则模式, 期望值类型, 排除模式)
# ============================================================
AUDIT_RULES = [
    # --- 置信度类 ---
    ('risk_limits.min_confidence', '全局最小置信度',
     r"min_confidence\D*=\s*(0\.\d+)", float,
     r'(_llm_review|llm_review|review_min|_fallback)'),

    # --- 仓位类 ---
    ('risk_limits.max_single_position', '单股最大仓位',
     r"max_single_position\D*[:=]\s*(0\.\d+)", float, None),
    ('risk_limits.max_total_position', '总仓位上限',
     r"max_total_position\D*[:=]\s*(0\.\d+)", float, None),

    # --- 日/周/月亏损 ---
    ('risk_limits.max_daily_loss', '日最大亏损',
     r"max_daily_loss\D*[:=]\s*(0\.\d+)", float,
     r'(pct|PCT|percentage|%|\d+\.\d+.*#.*%)'),
    ('risk_limits.max_weekly_loss', '周最大亏损',
     r"max_weekly_loss\D*[:=]\s*(0\.\d+)", float,
     r'(pct|PCT|percentage|%|\d+\.\d+.*#.*%)'),
    ('risk_limits.max_monthly_loss', '月最大亏损',
     r"max_monthly_loss\D*[:=]\s*(0\.\d+)", float,
     r'(pct|PCT|percentage|%|\d+\.\d+.*#.*%)'),

    # --- 最大回撤 ---
    ('risk_limits.max_drawdown', '最大回撤',
     r"max_drawdown\D*[:=]\s*(0\.\d+)", float,
     r'(daily|DAILY|pct|PCT|= 0\.0\b|_max_drawdown_loss)'),

    # --- 日交易次数 ---
    ('risk_limits.max_daily_trades', '日最大交易次数',
     r"max_daily_trades\D*[:=]\s*(\d+)", int, None),
]


class ConfigAuditor:
    """配置一致性审计器"""

    def __init__(self, project_root: str, config_path: str):
        self.project_root = Path(project_root)
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.issues: List[Dict] = []
        self.scan_stats = {'files_scanned': 0, 'matches_found': 0, 'issues_found': 0}

    def _load_config(self) -> Dict:
        """加载 YAML 配置"""
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def _get_yaml_value(self, path: str) -> any:
        """从 YAML 配置中按路径获取值"""
        keys = path.split('.')
        current = self.config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    def _is_in_comment(self, line: str, match_pos: int) -> bool:
        """判断匹配位置是否在注释中"""
        before_match = line[:match_pos]
        # 简单判断: 如果 # 在匹配位置之前且不在字符串中
        if '#' in before_match:
            # 粗略判断: 如果 # 后面没有引号包裹的内容包含匹配
            hash_pos = before_match.rfind('#')
            after_hash = before_match[hash_pos:]
            # 如果 # 后面没有引号，则整行是注释
            if '"' not in after_hash and "'" not in after_hash:
                return True
        return False

    def scan_file(self, filepath: Path, rules: List[Tuple]) -> List[Dict]:
        """扫描单个文件"""
        issues = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return issues

        self.scan_stats['files_scanned'] += 1

        for rule in rules:
            if len(rule) == 5:
                yaml_path, desc, pattern, expected_type, exclude_pattern = rule
            else:
                yaml_path, desc, pattern, expected_type = rule
                exclude_pattern = None

            yaml_value = self._get_yaml_value(yaml_path)
            if yaml_value is None:
                continue

            for line_num, line in enumerate(lines, 1):
                # 跳过 __pycache__ 和备份文件
                if '__pycache__' in str(filepath) or '.bak' in str(filepath):
                    continue

                # 排除模式
                if exclude_pattern and re.search(exclude_pattern, line):
                    continue

                for match in re.finditer(pattern, line):
                    if self._is_in_comment(line, match.start()):
                        continue

                    try:
                        code_value = expected_type(match.group(1))
                    except (ValueError, IndexError):
                        continue

                    # 容忍度: float 允许 0.01 差异, int 必须完全一致
                    if expected_type == float:
                        is_match = abs(code_value - yaml_value) < 0.01
                    else:
                        is_match = code_value == yaml_value

                    if not is_match:
                        rel_path = filepath.relative_to(self.project_root)
                        issues.append({
                            'file': str(rel_path),
                            'line': line_num,
                            'rule': yaml_path,
                            'description': desc,
                            'yaml_value': yaml_value,
                            'code_value': code_value,
                            'context': line.strip()[:100],
                        })
                        self.scan_stats['issues_found'] += 1

                    self.scan_stats['matches_found'] += 1

        return issues

    def scan_project(self) -> List[Dict]:
        """扫描整个项目"""
        all_issues = []

        # 扫描 app/ 目录下所有 .py 文件
        app_dir = self.project_root / 'app'
        if app_dir.exists():
            for py_file in app_dir.rglob('*.py'):
                issues = self.scan_file(py_file, AUDIT_RULES)
                all_issues.extend(issues)

        # 扫描 config/ 目录下的 .py 文件
        config_dir = self.project_root / 'config'
        if config_dir.exists():
            for py_file in config_dir.rglob('*.py'):
                issues = self.scan_file(py_file, AUDIT_RULES)
                all_issues.extend(issues)

        self.issues = all_issues
        return all_issues

    def print_report(self) -> str:
        """生成审计报告"""
        lines = []
        lines.append("=" * 70)
        lines.append(f"配置一致性审计报告 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"扫描统计:")
        lines.append(f"  文件扫描: {self.scan_stats['files_scanned']}")
        lines.append(f"  匹配项: {self.scan_stats['matches_found']}")
        lines.append(f"  不一致: {self.scan_stats['issues_found']}")
        lines.append("")

        if self.issues:
            lines.append(f"❌ 发现 {len(self.issues)} 处配置不一致:")
            lines.append("")
            for i, issue in enumerate(self.issues, 1):
                lines.append(f"  [{i}] {issue['file']}:{issue['line']}")
                lines.append(f"      参数: {issue['description']} ({issue['rule']})")
                lines.append(f"      config.yaml = {issue['yaml_value']}")
                lines.append(f"      代码中 = {issue['code_value']}")
                lines.append(f"      代码: {issue['context']}")
                lines.append("")
            lines.append("修复建议: 将代码中的值改为与 config.yaml 一致")
        else:
            lines.append("✅ 所有配置参数与 config.yaml 一致")

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)

    def send_dingtalk_alert(self, webhook_url: str = None, secret: str = None):
        """发送钉钉告警"""
        if not self.issues:
            return

        import hashlib
        import base64
        import time
        import requests

        if webhook_url is None:
            webhook_url = os.getenv('DINGTALK_WEBHOOK')
        if secret is None:
            secret = os.getenv('DINGTALK_SECRET')

        if not webhook_url:
            logger.warning("未配置钉钉 Webhook，跳过告警")
            return

        # 构建告警消息
        report = self.print_report()
        # 取前 1000 字符
        content = report[:1000]

        # 钉钉签名
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f'{timestamp}\n{secret}'
        hmac_code = base64.b64encode(
            hashlib.sha256(string_to_sign.encode('utf-8')).digest()
        ).decode('utf-8')

        url = f"{webhook_url}&timestamp={timestamp}&sign={hmac_code}"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "⚠️ FTSeek 配置不一致告警",
                "text": f"### ⚠️ 配置不一致告警\n\n```\n{content}\n```"
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("钉钉告警发送成功")
            else:
                logger.warning(f"钉钉告警发送失败: {resp.status_code}")
        except Exception as e:
            logger.warning(f"钉钉告警发送异常: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='配置一致性审计')
    parser.add_argument('--project', default='/opt/ftseek/FTSeek-US', help='项目根目录')
    parser.add_argument('--config', default='/opt/ftseek/FTSeek-US/config/unified_config.yaml', help='配置文件路径')
    parser.add_argument('--block', action='store_true', help='发现不一致时返回 exit code 1')
    parser.add_argument('--alert', action='store_true', help='发送钉钉告警')
    args = parser.parse_args()

    auditor = ConfigAuditor(args.project, args.config)
    auditor.scan_project()
    report = auditor.print_report()
    print(report)

    if args.alert and auditor.issues:
        auditor.send_dingtalk_alert()

    if args.block and auditor.issues:
        print(f"\n⛔ 配置不一致，拒绝启动。请修复上述 {len(auditor.issues)} 处问题。")
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
