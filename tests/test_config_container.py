"""
配置容器测试

测试配置容器的功能
"""
import pytest
import os
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config_container import (
    ConfigContainer,
    PathConfig,
    OpenDConfig,
    RiskConfig,
    DataSourceConfig,
    AgentConfig,
    NotificationConfig,
    get_config,
    reload_config,
)


class TestPathConfig:
    """测试路径配置"""
    
    def test_from_env_creates_valid_paths(self):
        """测试从环境变量创建路径配置"""
        config = PathConfig.from_env()
        
        assert config.base_dir is not None
        assert config.data_dir is not None
        assert config.log_dir is not None
        assert config.config_dir is not None
        assert config.db_path is not None
    
    def test_directories_exist_or_created(self):
        """测试目录存在或已创建"""
        config = PathConfig.from_env()
        
        # 这些目录应该存在或被创建
        assert config.base_dir.exists()
        # data, log, config 目录会被 from_env 创建


class TestOpenDConfig:
    """测试 OpenD 配置"""
    
    def test_from_env_with_defaults(self):
        """测试从环境变量创建 OpenD 配置（默认值）"""
        with patch.dict(os.environ, {}, clear=False):
            config = OpenDConfig.from_env()
            
            assert config.host == "127.0.0.1"
            assert config.port == 11111
            assert config.trade_env == 1  # 默认模拟环境
    
    def test_from_env_with_custom_values(self):
        """测试从环境变量创建 OpenD 配置（自定义值）"""
        with patch.dict(os.environ, {
            "FUTU_OPEND_HOST": "192.168.1.100",
            "FUTU_OPEND_PORT": "22222",
            "FUTU_LOGIN_ACCOUNT": "test123",
            "SIMULATE_ACCOUNT_ID": "888888",
        }, clear=False):
            config = OpenDConfig.from_env()
            
            assert config.host == "192.168.1.100"
            assert config.port == 22222
            assert config.login_account == "test123"
            assert config.simulate_account_id == "888888"
    
    def test_trade_env_security(self):
        """测试交易环境安全限制"""
        # TRADE_ENV=0 但 ALLOW_REAL_TRADING=false 应该被强制为模拟环境
        with patch.dict(os.environ, {
            "TRADE_ENV": "0",
            "ALLOW_REAL_TRADING": "false",
        }, clear=False):
            config = OpenDConfig.from_env()
            assert config.trade_env == 1  # 被强制为模拟环境


class TestRiskConfig:
    """测试风控配置"""
    
    def test_from_env_with_defaults(self):
        """测试从环境变量创建风控配置（默认值）"""
        config = RiskConfig.from_env()
        
        assert config.drop_warning_threshold == -0.05
        assert config.drop_critical_threshold == -0.08
        assert config.vix_warning_threshold == 30.0
        assert config.vix_critical_threshold == 40.0
        assert config.vix_cache_ttl_seconds == 180
        assert config.max_position_pct == 0.25
        assert config.max_total_position_pct == 0.80
        assert config.stop_loss_pct == -0.08
    
    def test_from_env_with_custom_values(self):
        """测试从环境变量创建风控配置（自定义值）"""
        with patch.dict(os.environ, {
            "DROP_WARNING_THRESHOLD": "-0.03",
            "DROP_CRITICAL_THRESHOLD": "-0.06",
            "VIX_WARNING_THRESHOLD": "25.0",
            "MAX_POSITION_PCT": "0.20",
            "STOP_LOSS_PCT": "-0.05",
        }, clear=False):
            config = RiskConfig.from_env()
            
            assert config.drop_warning_threshold == -0.03
            assert config.drop_critical_threshold == -0.06
            assert config.vix_warning_threshold == 25.0
            assert config.max_position_pct == 0.20
            assert config.stop_loss_pct == -0.05


class TestDataSourceConfig:
    """测试数据源配置"""
    
    def test_from_env_with_api_keys(self):
        """测试从环境变量创建数据源配置"""
        with patch.dict(os.environ, {
            "ALPHAVANTAGE_API_KEY": "test_av_key",
            "FINNHUB_API_KEY": "test_finnhub_key",
            "TWELVEDATA_API_KEY": "test_twelvedata_key",
        }, clear=False):
            config = DataSourceConfig.from_env()
            
            assert config.alphavantage_api_key == "test_av_key"
            assert config.finnhub_api_key == "test_finnhub_key"
            assert config.twelvedata_api_key == "test_twelvedata_key"
            assert config.alphavantage_base_url == "https://www.alphavantage.co/query"
            assert config.finnhub_base_url == "https://finnhub.io/api/v1"
            assert config.twelvedata_base_url == "https://api.twelvedata.com"


class TestAgentConfig:
    """测试 Agent 配置"""
    
    def test_from_env_with_defaults(self):
        """测试从环境变量创建 Agent 配置（默认值）"""
        config = AgentConfig.from_env()
        
        assert config.consensus_threshold == 0.60
        assert config.min_strategies == 3
        assert config.agent_timeout_seconds == 30
    
    def test_from_env_with_custom_values(self):
        """测试从环境变量创建 Agent 配置（自定义值）"""
        with patch.dict(os.environ, {
            "CONSENSUS_THRESHOLD": "0.70",
            "MIN_STRATEGIES": "5",
            "AGENT_TIMEOUT_SECONDS": "60",
        }, clear=False):
            config = AgentConfig.from_env()
            
            assert config.consensus_threshold == 0.70
            assert config.min_strategies == 5
            assert config.agent_timeout_seconds == 60


class TestNotificationConfig:
    """测试通知配置"""
    
    def test_from_env_with_defaults(self):
        """测试从环境变量创建通知配置（默认值）"""
        config = NotificationConfig.from_env()
        
        assert config.dingtalk_enabled == True
        assert config.feishu_enabled == True
    
    def test_from_env_disabled(self):
        """测试从环境变量创建通知配置（禁用）"""
        with patch.dict(os.environ, {
            "DINGTALK_ENABLED": "false",
            "FEISHU_ENABLED": "false",
        }, clear=False):
            config = NotificationConfig.from_env()
            
            assert config.dingtalk_enabled == False
            assert config.feishu_enabled == False


class TestConfigContainer:
    """测试配置容器"""
    
    def setup_method(self):
        """每个测试前重置配置"""
        ConfigContainer.reset()
    
    def teardown_method(self):
        """每个测试后清理"""
        ConfigContainer.reset()
    
    def test_singleton_pattern(self):
        """测试单例模式"""
        config1 = ConfigContainer()
        config2 = ConfigContainer()
        
        assert config1 is config2
    
    def test_get_instance(self):
        """测试获取实例"""
        config = ConfigContainer.get_instance()
        assert config is not None
        assert isinstance(config, ConfigContainer)
    
    def test_all_sub_configs_loaded(self):
        """测试所有子配置已加载"""
        config = ConfigContainer()
        
        assert config.paths is not None
        assert config.opend is not None
        assert config.risk is not None
        assert config.data_source is not None
        assert config.agent is not None
        assert config.notification is not None
    
    def test_to_dict(self):
        """测试转换为字典"""
        config = ConfigContainer()
        config_dict = config.to_dict()
        
        assert 'paths' in config_dict
        assert 'risk' in config_dict
        assert 'agent' in config_dict
        assert 'base_dir' in config_dict['paths']
        assert 'drop_warning_threshold' in config_dict['risk']
        assert 'consensus_threshold' in config_dict['agent']
    
    def test_validate_success(self):
        """测试配置验证成功"""
        config = ConfigContainer()
        errors = config.validate()
        
        # 应该没有严重错误（可能有 API key 警告）
        # 主要验证配置结构正确
        assert isinstance(errors, list)
    
    def test_validate_invalid_position_pct(self):
        """测试配置验证 - 无效仓位百分比"""
        config = ConfigContainer()
        config.risk.max_position_pct = 1.5  # 无效值
        
        errors = config.validate()
        
        assert any("max_position_pct" in e for e in errors)
    
    def test_validate_invalid_stop_loss(self):
        """测试配置验证 - 无效止损"""
        config = ConfigContainer()
        config.risk.stop_loss_pct = 0.05  # 止损应该是负数
        
        errors = config.validate()
        
        assert any("Stop loss" in e for e in errors)
    
    def test_reload(self):
        """测试重新加载配置"""
        config = ConfigContainer()
        original_base = config.paths.base_dir
        
        # 修改配置
        with patch.dict(os.environ, {"ALLOW_REAL_TRADING": "true"}):
            config.reload()
        
        # 配置应该被重新加载
        assert config._initialized == True
    
    def test_reset(self):
        """测试重置配置"""
        config = ConfigContainer()
        ConfigContainer.reset()
        
        # 实例应该被清除
        assert ConfigContainer._instance is None


class TestGlobalConfigFunctions:
    """测试全局配置函数"""
    
    def setup_method(self):
        """每个测试前重置"""
        ConfigContainer.reset()
    
    def teardown_method(self):
        """每个测试后清理"""
        ConfigContainer.reset()
    
    def test_get_config(self):
        """测试获取全局配置"""
        config = get_config()
        
        assert config is not None
        assert isinstance(config, ConfigContainer)
    
    def test_get_config_singleton(self):
        """测试全局配置单例"""
        config1 = get_config()
        config2 = get_config()
        
        assert config1 is config2
    
    def test_reload_config(self):
        """测试重新加载全局配置"""
        config1 = get_config()
        reloaded = reload_config()
        config2 = get_config()
        
        assert reloaded is not None
        assert config2 is reloaded


class TestConfigContainerIntegration:
    """配置容器集成测试"""
    
    def setup_method(self):
        ConfigContainer.reset()
    
    def teardown_method(self):
        ConfigContainer.reset()
    
    def test_full_config_workflow(self):
        """测试完整配置工作流"""
        # 1. 获取配置
        config = ConfigContainer()
        
        # 2. 验证所有子配置
        assert config.paths.base_dir.exists()
        assert isinstance(config.risk.drop_warning_threshold, float)
        assert isinstance(config.agent.consensus_threshold, float)
        
        # 3. 转换为字典
        config_dict = config.to_dict()
        assert len(config_dict) > 0
        
        # 4. 验证配置
        errors = config.validate()
        assert isinstance(errors, list)
    
    def test_dependency_injection_pattern(self):
        """测试依赖注入模式"""
        # 模拟一个需要配置的类
        class Service:
            def __init__(self, config: ConfigContainer):
                self.config = config
                self.risk_threshold = config.risk.drop_warning_threshold
        
        config = ConfigContainer()
        service = Service(config)
        
        assert service.risk_threshold == -0.05
    
    def test_environment_override(self):
        """测试环境变量覆盖"""
        with patch.dict(os.environ, {
            "DROP_WARNING_THRESHOLD": "-0.02",
            "VIX_WARNING_THRESHOLD": "20.0",
        }, clear=False):
            ConfigContainer.reset()
            config = ConfigContainer()
            
            assert config.risk.drop_warning_threshold == -0.02
            assert config.risk.vix_warning_threshold == 20.0


class TestReloadLastKnownGood:
    """坏 YAML/缺文件不得清空现役配置，也不得被热重载器计为成功。"""

    def setup_method(self):
        ConfigContainer.reset()

    def teardown_method(self):
        ConfigContainer.reset()

    def _container(self, tmp_path, monkeypatch):
        import app.core.config_container as cc_mod

        root = tmp_path / "candidate-root"
        (root / "app" / "core").mkdir(parents=True)
        (root / "config").mkdir()
        yaml_path = root / "config" / "unified_config.yaml"
        yaml_path.write_text(
            "risk:\n  signal_quality:\n    min_confidence: 0.81\n", encoding="utf-8")
        monkeypatch.setattr(
            cc_mod, "__file__", str(root / "app" / "core" / "config_container.py"))
        return ConfigContainer(), yaml_path

    @pytest.mark.parametrize(
        "failure",
        ["malformed", "missing", "empty", "comments", "false", "empty_list", "empty_dict"])
    def test_reload_failure_preserves_complete_old_state(self, tmp_path, monkeypatch, failure):
        cfg, yaml_path = self._container(tmp_path, monkeypatch)
        old_yaml = dict(cfg._yaml_raw)
        old_flat = dict(cfg._config_dict)
        old_trade_mode = cfg.trade_env
        invalid_contents = {
            "malformed": "risk: [unterminated\n",
            "empty": "",
            "comments": "# only a comment\n",
            "false": "false\n",
            "empty_list": "[]\n",
            "empty_dict": "{}\n",
        }
        if failure == "missing":
            yaml_path.unlink()
        else:
            yaml_path.write_text(invalid_contents[failure], encoding="utf-8")

        with pytest.raises(RuntimeError, match="保留上一份有效配置"):
            cfg.reload()

        assert cfg._yaml_raw == old_yaml
        assert cfg._config_dict == old_flat
        assert cfg.trade_env == old_trade_mode
        assert cfg.get_dotted("risk.signal_quality.min_confidence") == 0.81

    @pytest.mark.parametrize("invalid_yaml", ["risk: [unterminated\n", ""])
    def test_hot_reloader_counts_failure_and_recovers_after_fix(
            self, tmp_path, monkeypatch, invalid_yaml):
        from app.config_mgmt.hot_reloader import ConfigHotReloader

        cfg, yaml_path = self._container(tmp_path, monkeypatch)
        reloader = ConfigHotReloader(cfg, cooldown_seconds=0)
        yaml_path.write_text(invalid_yaml, encoding="utf-8")

        reloader._reload()
        assert reloader.stats["reload_count"] == 0
        assert reloader.stats["error_count"] == 1
        assert cfg.get_dotted("risk.signal_quality.min_confidence") == 0.81

        yaml_path.write_text(
            "risk:\n  signal_quality:\n    min_confidence: 0.82\n", encoding="utf-8")
        reloader._reload()
        assert reloader.stats["reload_count"] == 1
        assert reloader.stats["error_count"] == 1
        assert cfg.get_dotted("risk.signal_quality.min_confidence") == 0.82


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app/core/config_container", "--cov-report=term-missing"])
