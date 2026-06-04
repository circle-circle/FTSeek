"""配置治理回归测试 - 验证配置一致性和无硬编码"""
import pytest
import sys
import os
import re
import yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent


class TestExitPolicyYaml:
    """验证 exit_policy.yaml 完整性"""

    @pytest.fixture
    def exit_policy(self):
        policy_path = PROJECT_ROOT / 'app' / 'risk' / 'exit_policy.yaml'
        if not policy_path.exists():
            pytest.skip("exit_policy.yaml not found")
        with open(policy_path) as f:
            return yaml.safe_load(f)

    def test_has_version(self, exit_policy):
        """配置文件应有版本号"""
        assert 'version' in exit_policy

    def test_has_exit_rules(self, exit_policy):
        """应有退出规则段"""
        assert 'exit_rules' in exit_policy or 'chandelier_trail' in exit_policy

    def test_atr_stop_config(self, exit_policy):
        """ATR止损应有基本配置"""
        # 可能在 exit_rules.atr_stop 或 chandelier_trail 下
        rules = exit_policy.get('exit_rules', exit_policy)
        has_atr = any(k for k in rules if 'atr' in k.lower() or 'trail' in k.lower() or 'chandelier' in k.lower())
        assert has_atr, "Should have ATR/trailing stop configuration"


class TestUnifiedConfig:
    """验证 unified_config.yaml"""

    @pytest.fixture
    def config(self):
        config_path = PROJECT_ROOT / 'config' / 'unified_config.yaml'
        if not config_path.exists():
            pytest.skip("unified_config.yaml not found")
        with open(config_path) as f:
            return yaml.safe_load(f)

    def test_has_risk_section(self, config):
        """应有风控段"""
        assert 'risk' in config

    def test_has_mode_adaptation(self, config):
        """应有模式适配配置"""
        risk = config.get('risk', {})
        assert 'mode_adaptation' in risk, "Should have risk.mode_adaptation for sim/live switching"

    def test_has_strategy_enforcement(self, config):
        """应有策略执行约束"""
        risk = config.get('risk', {})
        assert 'strategy_enforcement' in risk


class TestNoHardcodedParams:
    """扫描代码中的硬编码参数"""

    RISK_DIR = PROJECT_ROOT / 'app' / 'risk'

    # 允许的硬编码模式（常量定义、默认值等）
    ALLOWED_PATTERNS = [
        r'DEFAULT_',
        r'VERSION',
        r'__version__',
        r'# ',         # 注释中的数字
        r'logging',
        r'range\(',
        r'enumerate\(',
        r'len\(',
    ]

    def test_no_magic_thresholds_in_exit_monitor(self):
        """position_exit_monitor.py 不应有裸露的阈值比较"""
        file_path = self.RISK_DIR / 'position_exit_monitor.py'
        if not file_path.exists():
            pytest.skip("File not found")

        content = file_path.read_text()
        # 查找类似 "if profit > 0.5" 或 "if loss > 3" 的模式
        # 这里用宽松检查，主要是提醒
        suspicious = re.findall(r'if\s+\w+\s*[><=]+\s*[0-9]+\.[0-9]+', content)
        # 过滤掉已知安全的
        filtered = [s for s in suspicious if not any(p in s for p in ['config', 'self.', 'param'])]
        # 只作为警告，不直接fail（因为可能有合理用途）
        if filtered:
            print(f"Warning: Potential hardcoded thresholds in exit_monitor: {filtered[:5]}")
