"""
FTSeek Unified Configuration Module
版本：v5.0.0
功能：统一港股和美股配置结构，提供类型安全的配置访问，支持配置热重载
"""

import os
import yaml
import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field
from datetime import datetime
import threading
import time


# ============================================================================
# 配置数据类定义 (类型安全)
# ============================================================================

@dataclass
class TradingConfig:
    """交易配置"""
    mode: str = "paper"
    max_daily_trades: int = 10
    max_single_position: float = 0.20
    max_total_position: float = 0.60
    max_daily_loss: float = 0.03
    default_position_size: float = 0.10


@dataclass
class RiskLimits:
    """风控限制"""
    min_confidence: float = 0.45
    max_single_position: float = 0.18
    max_single_loss: float = 0.05
    trailing_percent: float = 0.03
    max_holding_hours: int = 72
    atr_multiplier: float = 2.0
    max_total_position: float = 0.75
    max_daily_trades: int = 5
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.08
    max_drawdown: float = 0.12
    consecutive_loss_limit: int = 3


@dataclass
class NotificationConfig:
    """通知配置"""
    enabled: bool = True
    mode: str = "enterprise"
    webhook_url: str = ""
    secret: str = ""
    app_key: str = ""
    app_secret: str = ""
    agent_id: str = ""
    timeout: int = 600


@dataclass
class FutuConfig:
    """富途 OpenD 配置"""
    host: str = "127.0.0.1"
    port: int = 11111
    trade_env: int = 1
    simulate_account_id: str = "8246818"
    login_account: str = ""
    trade_password_md5: str = ""
    force_simulate: bool = True


@dataclass
class MarketConfig:
    """市场特定配置"""
    market_type: str = "hk"  # hk | us
    currency: str = "HKD"
    trading_hours: Dict[str, str] = field(default_factory=dict)
    check_interval: int = 300
    night_interval: int = 900


@dataclass
class VersionInfo:
    """版本信息"""
    version: str = "5.0.0"
    updated: str = ""
    description: str = ""


# ============================================================================
# 统一配置类
# ============================================================================

class UnifiedConfig:
    """
    统一配置管理器
    
    功能:
    - 统一港股和美股配置结构
    - 类型安全的配置访问
    - 配置热重载支持
    - 配置变更监控
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, config_dir: Optional[str] = None):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config_dir: Optional[str] = None):
        if self._initialized:
            return
        
        self.config_dir = Path(config_dir) if config_dir else Path(__file__).parent
        self.configs: Dict[str, Any] = {}
        self.file_hashes: Dict[str, str] = {}
        self.last_loaded: datetime = datetime.now()
        self.hot_reload_enabled = True
        self._reload_thread: Optional[threading.Thread] = None
        self._stop_reload = False
        self.callbacks: List[callable] = []
        
        self._initialized = True
        self.load_all_configs()
    
    def load_all_configs(self) -> None:
        """加载所有配置文件"""
        config_files = {
            'unified_config': self.config_dir / 'unified_config.yaml',
            'version': self.config_dir / 'version.yaml',
            'accounts': self.config_dir / 'accounts.yaml',
            'strategy_params': self.config_dir / 'strategy_params.json',
        }
        
        for name, path in config_files.items():
            if path.exists():
                self._load_config_file(name, path)
        
        self.last_loaded = datetime.now()
    
    def _load_config_file(self, name: str, path: Path) -> None:
        """加载单个配置文件"""
        try:
            content = path.read_text(encoding='utf-8')
            current_hash = hashlib.md5(content.encode()).hexdigest()
            
            if path.suffix in ['.yaml', '.yml']:
                self.configs[name] = yaml.safe_load(content)
            elif path.suffix == '.json':
                self.configs[name] = json.loads(content)
            
            self.file_hashes[name] = current_hash
            
        except Exception as e:
            print(f"⚠️  加载配置 {name} 失败：{e}")
            self.configs[name] = {}
    
    def check_for_changes(self) -> List[str]:
        """检查配置文件变更"""
        changed_files = []
        
        for name, path in {
            'unified_config': self.config_dir / 'unified_config.yaml',
            'version': self.config_dir / 'version.yaml',
        }.items():
            if path.exists():
                try:
                    content = path.read_text(encoding='utf-8')
                    current_hash = hashlib.md5(content.encode()).hexdigest()
                    
                    if self.file_hashes.get(name) != current_hash:
                        changed_files.append(name)
                        self.file_hashes[name] = current_hash
                except Exception:
                    pass
        
        return changed_files
    
    def reload_config(self, config_name: Optional[str] = None) -> None:
        """重新加载配置 (热重载)"""
        if config_name:
            path = self.config_dir / f"{config_name}.yaml"
            if not path.exists():
                path = self.config_dir / f"{config_name}.json"
            if path.exists():
                self._load_config_file(config_name, path)
        else:
            self.load_all_configs()
        
        self.last_loaded = datetime.now()
        
        # 通知回调
        for callback in self.callbacks:
            try:
                callback(config_name)
            except Exception as e:
                print(f"⚠️  配置变更回调失败：{e}")
    
    def start_hot_reload_monitor(self, interval: int = 30) -> None:
        """启动热重载监控"""
        if not self.hot_reload_enabled:
            return
        
        self._stop_reload = False
        
        def monitor_loop():
            while not self._stop_reload:
                changed = self.check_for_changes()
                if changed:
                    print(f"🔄 检测到配置变更：{changed}")
                    for config_name in changed:
                        self.reload_config(config_name)
                time.sleep(interval)
        
        self._reload_thread = threading.Thread(target=monitor_loop, daemon=True)
        self._reload_thread.start()
    
    def stop_hot_reload_monitor(self) -> None:
        """停止热重载监控"""
        self._stop_reload = True
        if self._reload_thread:
            self._reload_thread.join(timeout=5)
    
    def register_change_callback(self, callback: callable) -> None:
        """注册配置变更回调"""
        self.callbacks.append(callback)
    
    # ========================================================================
    # 类型安全的配置访问方法
    # ========================================================================
    
    def get_trading_config(self, market: str = "hk") -> TradingConfig:
        """获取交易配置"""
        config = self.configs.get('unified_config', {})
        trading = config.get('trading', {})
        
        return TradingConfig(
            mode=trading.get('mode', 'paper'),
            max_daily_trades=trading.get('max_daily_trades', 10),
            max_single_position=trading.get('max_single_position', 0.20),
            max_total_position=trading.get('max_total_position', 0.60),
            max_daily_loss=trading.get('max_daily_loss', 0.03),
            default_position_size=trading.get('default_position_size', 0.10),
        )
    
    def get_risk_limits(self) -> RiskLimits:
        """获取风控限制"""
        config = self.configs.get('unified_config', {})
        risk = config.get('risk_limits', {})
        
        return RiskLimits(
            min_confidence=risk.get('min_confidence', 0.45),
            max_single_position=risk.get('max_single_position', 0.18),
            max_single_loss=risk.get('max_single_loss', 0.05),
            trailing_percent=risk.get('trailing_percent', 0.03),
            max_holding_hours=risk.get('max_holding_hours', 72),
            atr_multiplier=risk.get('atr_multiplier', 2.0),
            max_total_position=risk.get('max_total_position', 0.75),
            max_daily_trades=risk.get('max_daily_trades', 5),
            max_daily_loss=risk.get('max_daily_loss', 0.03),
            max_weekly_loss=risk.get('max_weekly_loss', 0.08),
            max_drawdown=risk.get('max_drawdown', 0.12),
            consecutive_loss_limit=risk.get('consecutive_loss_limit', 3),
        )
    
    def get_notification_config(self) -> NotificationConfig:
        """获取通知配置"""
        config = self.configs.get('unified_config', {})
        notify = config.get('notification', {}).get('dingtalk', {})
        
        return NotificationConfig(
            enabled=notify.get('enabled', True),
            mode=notify.get('mode', 'enterprise'),
            webhook_url=os.getenv('DINGTALK_WEBHOOK_URL', notify.get('webhook_url', '')),
            secret=os.getenv('DINGTALK_SECRET', notify.get('secret', '')),
            app_key=os.getenv('DINGTALK_APP_KEY', notify.get('app_key', '')),
            app_secret=os.getenv('DINGTALK_APP_SECRET', notify.get('app_secret', '')),
            agent_id=os.getenv('DINGTALK_AGENT_ID', notify.get('agent_id', '')),
            timeout=notify.get('timeout', 600),
        )
    
    def get_futu_config(self) -> FutuConfig:
        """获取富途配置"""
        config = self.configs.get('unified_config', {})
        futu = config.get('futu', {})
        
        return FutuConfig(
            host=futu.get('host', '127.0.0.1'),
            port=futu.get('port', 11111),
            trade_env=futu.get('trade_env', 1),
            simulate_account_id=futu.get('simulate_account_id', '8246818'),
            login_account=os.getenv('FUTU_LOGIN_ACCOUNT', futu.get('login_account', '')),
            trade_password_md5=os.getenv('FUTU_TRD_PWD_MD5', futu.get('trade_password_md5', '')),
            force_simulate=futu.get('force_simulate', True),
        )
    
    def get_market_config(self, market: str = "hk") -> MarketConfig:
        """获取市场配置"""
        config = self.configs.get('unified_config', {})
        markets = config.get('markets', {})
        market_config = markets.get(market, {})
        
        return MarketConfig(
            market_type=market,
            currency=market_config.get('currency', 'HKD' if market == 'hk' else 'USD'),
            trading_hours=market_config.get('trading_hours', {}),
            check_interval=market_config.get('check_interval', 300),
            night_interval=market_config.get('night_interval', 900),
        )
    
    def get_version_info(self) -> VersionInfo:
        """获取版本信息"""
        version_config = self.configs.get('version', {})
        
        return VersionInfo(
            version=version_config.get('version', '5.0.0'),
            updated=version_config.get('updated', ''),
            description=version_config.get('description', ''),
        )
    
    def get(self, key: str, default: Any = None) -> Any:
        """通用配置访问"""
        parts = key.split('.')
        config = self.configs.get(parts[0], {})
        
        for part in parts[1:]:
            if isinstance(config, dict):
                config = config.get(part, {})
            else:
                return default
        
        return config if config else default
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self.configs.copy()
    
    def get_config_path(self, config_name: str) -> Path:
        """获取配置文件路径"""
        return self.config_dir / config_name


# ============================================================================
# 便捷函数
# ============================================================================

def get_config(config_dir: Optional[str] = None) -> UnifiedConfig:
    """获取配置单例"""
    return UnifiedConfig(config_dir)


def load_config(config_dir: Optional[str] = None) -> UnifiedConfig:
    """加载配置 (别名)"""
    return get_config(config_dir)


# ============================================================================
# 主函数 (测试用)
# ============================================================================

if __name__ == "__main__":
    print("Testing UnifiedConfig...")
    
    config = get_config()
    
    print(f"\n📋 版本信息:")
    version = config.get_version_info()
    print(f"  版本：{version.version}")
    print(f"  更新时间：{version.updated}")
    
    print(f"\n📈 交易配置:")
    trading = config.get_trading_config()
    print(f"  模式：{trading.mode}")
    print(f"  最大日交易：{trading.max_daily_trades}")
    print(f"  单股最大仓位：{trading.max_single_position}")
    
    print(f"\n🛡️  风控限制:")
    risk = config.get_risk_limits()
    print(f"  最小置信度：{risk.min_confidence}")
    print(f"  最大回撤：{risk.max_drawdown}")
    
    print(f"\n✅ 配置加载成功!")
