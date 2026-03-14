"""
策略参数热更新模块

无需重启服务即可更新策略参数
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger("ftseek.core.config_hot_reload")

# 配置文件路径
CONFIG_FILE = Path('/opt/ftseek/FTSeek/config/strategy_params.json')
BACKUP_DIR = Path('/opt/ftseek/FTSeek/config/backups')
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


class StrategyParamsManager:
    """策略参数管理器"""
    
    def __init__(self):
        self.params: Dict[str, Any] = {}
        self.callbacks = []
        self._load_params()
        
        logger.info("策略参数管理器已初始化")
    
    def _load_params(self):
        """加载参数"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    self.params = json.load(f)
                logger.info(f"已加载策略参数：{len(self.params)} 个")
            except Exception as e:
                logger.error(f"加载参数失败：{e}")
                self._init_default_params()
        else:
            self._init_default_params()
    
    def _init_default_params(self):
        """初始化默认参数"""
        self.params = {
            # AI 相关
            'ai_weight': 0.3,
            'ai_min_accuracy': 0.45,
            'ai_disable_threshold': 0.35,
            
            # 风控相关
            'base_position': 0.10,
            'min_position': 0.05,
            'max_position': 0.30,
            'max_daily_loss': 0.05,
            'max_drawdown': 0.15,
            'consecutive_loss_limit': 5,
            
            # 策略相关
            'fusion_v2_enabled': True,
            'fusion_v3_enabled': True,
            'min_signal_confidence': 0.4,
            
            # 交易模式
            'trading_mode': 'simulate',  # simulate/paper/live
            'manual_confirm_for_live': True  # 实盘必须人工确认
        }
        
        self._save_params()
        logger.info("已初始化默认参数")
    
    def _save_params(self):
        """保存参数"""
        # 备份当前参数
        if CONFIG_FILE.exists():
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = BACKUP_DIR / f'strategy_params_{timestamp}.json'
            
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
            
            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"参数已备份：{backup_file}")
        
        # 保存新参数
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.params, f, indent=2, ensure_ascii=False)
        
        logger.info(f"参数已保存：{CONFIG_FILE}")
    
    def update_param(self, key: str, value: Any, require_confirm: bool = False) -> bool:
        """
        更新参数
        
        Args:
            key: 参数名
            value: 参数值
            require_confirm: 是否需要确认 (实盘相关参数必须确认)
        
        Returns:
            bool: 是否更新成功
        """
        if key not in self.params:
            logger.warning(f"参数不存在：{key}")
            return False
        
        # 检查是否需要人工确认
        if require_confirm and self.params.get('trading_mode') == 'live':
            logger.warning(f"⚠️ 实盘模式参数更新需要人工确认：{key} = {value}")
            logger.warning("请手动编辑配置文件确认")
            return False
        
        old_value = self.params[key]
        self.params[key] = value
        
        self._save_params()
        
        # 通知回调
        self._notify_callbacks(key, old_value, value)
        
        logger.info(f"参数已更新：{key} = {old_value} → {value}")
        return True
    
    def update_params_batch(self, updates: Dict[str, Any], require_confirm: bool = False) -> bool:
        """批量更新参数"""
        success = True
        
        for key, value in updates.items():
            if not self.update_param(key, value, require_confirm):
                success = False
        
        return success
    
    def get_param(self, key: str, default: Any = None) -> Any:
        """获取参数"""
        return self.params.get(key, default)
    
    def get_all_params(self) -> Dict[str, Any]:
        """获取所有参数"""
        return self.params.copy()
    
    def register_callback(self, callback):
        """注册回调函数"""
        self.callbacks.append(callback)
    
    def _notify_callbacks(self, key: str, old_value: Any, new_value: Any):
        """通知回调"""
        for callback in self.callbacks:
            try:
                callback(key, old_value, new_value)
            except Exception as e:
                logger.error(f"回调执行失败：{e}")
    
    def reset_to_default(self):
        """重置为默认参数"""
        logger.warning("重置为默认参数")
        self._init_default_params()
    
    def get_trading_mode(self) -> str:
        """获取交易模式"""
        return self.params.get('trading_mode', 'simulate')
    
    def is_simulate_mode(self) -> bool:
        """是否模拟模式"""
        return self.get_trading_mode() == 'simulate'
    
    def is_live_mode(self) -> bool:
        """是否实盘模式"""
        return self.get_trading_mode() == 'live'
    
    def require_manual_confirm(self) -> bool:
        """是否需要人工确认"""
        return self.params.get('manual_confirm_for_live', True)


# 全局单例
_params_manager = None

def get_strategy_params_manager() -> StrategyParamsManager:
    """获取策略参数管理器单例"""
    global _params_manager
    if _params_manager is None:
        _params_manager = StrategyParamsManager()
    return _params_manager


# 命令行工具
if __name__ == '__main__':
    import sys
    
    manager = get_strategy_params_manager()
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 config_hot_reload.py list              # 列出所有参数")
        print("  python3 config_hot_reload.py get <key>         # 获取参数")
        print("  python3 config_hot_reload.py set <key> <value> # 设置参数")
        print("  python3 config_hot_reload.py reset             # 重置为默认")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == 'list':
        print("当前参数:")
        for key, value in manager.get_all_params().items():
            print(f"  {key}: {value}")
    
    elif command == 'get':
        if len(sys.argv) < 3:
            print("请指定参数名")
            sys.exit(1)
        key = sys.argv[2]
        value = manager.get_param(key)
        print(f"{key}: {value}")
    
    elif command == 'set':
        if len(sys.argv) < 4:
            print("请指定参数名和值")
            sys.exit(1)
        key = sys.argv[2]
        value = sys.argv[3]
        
        # 尝试转换类型
        try:
            if '.' in value:
                value = float(value)
            elif value.isdigit():
                value = int(value)
            elif value.lower() == 'true':
                value = True
            elif value.lower() == 'false':
                value = False
        except:
            pass
        
        # 实盘相关参数需要确认
        require_confirm = key in ['trading_mode', 'ai_weight', 'max_position']
        
        if manager.update_param(key, value, require_confirm):
            print(f"✅ 参数已更新：{key} = {value}")
        else:
            print(f"❌ 参数更新失败：{key}")
    
    elif command == 'reset':
        manager.reset_to_default()
        print("✅ 已重置为默认参数")
    
    else:
        print(f"未知命令：{command}")
        sys.exit(1)
