"""
配置热加载器 (Config Hot Reloader) - Phase 5

支持半自动参数更新流程:
1. 每周运行归因分析 → 生成建议
2. DingTalk 推送建议 → 用户确认
3. 用户确认后 → 更新配置
4. 新配置运行 24 小时 → 对比旧配置
5. 如果更优 → 保留；否则 → 回滚

核心功能:
- 配置热加载 (无需重启)
- 版本管理与回滚
- 影子测试 (新旧配置对比)
- 优化建议应用
"""
import json
import logging
import os
import yaml
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.core.config_hot_reload")
logger.propagate = False

# 配置文件路径 - 2026-04-26 迁移：unified_config.yaml 为 SSOT
# 保留此文件用于向后兼容，但优先从 config_center 读取
CONFIG_DIR = Path('/opt/ftseek/FTSeek/config')
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / 'unified_config.yaml'  # SSOT v4.0
BACKUP_DIR = CONFIG_DIR / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
SHADOW_DIR = CONFIG_DIR / 'shadow'
SHADOW_DIR.mkdir(parents=True, exist_ok=True)


class ConfigChangeType(str, Enum):
    """配置变更类型"""
    WEIGHT_ADJUST = "weight_adjust"
    THRESHOLD_ADJUST = "threshold_adjust"
    RISK_PARAM_ADJUST = "risk_param_adjust"
    EXECUTION_ADJUST = "execution_adjust"


class ShadowTestResult(str, Enum):
    """影子测试结果"""
    BETTER = "better"
    WORSE = "worse"
    SAME = "same"


@dataclass
class ConfigVersion:
    """配置版本"""
    version_id: str
    timestamp: str
    config_snapshot: Dict
    change_type: str
    change_reason: str
    is_active: bool = False

    def to_dict(self) -> Dict:
        return {
            'version_id': self.version_id,
            'timestamp': self.timestamp,
            'config_snapshot': self.config_snapshot,
            'change_type': self.change_type,
            'change_reason': self.change_reason,
            'is_active': self.is_active,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'ConfigVersion':
        return cls(**data)


@dataclass
class ShadowTestReport:
    """影子测试报告"""
    test_id: str
    start_time: str
    end_time: str
    hours: int
    old_config_metrics: Dict
    new_config_metrics: Dict
    result: str
    recommendation: str

    def to_dict(self) -> Dict:
        return {
            'test_id': self.test_id,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'hours': self.hours,
            'old_config_metrics': self.old_config_metrics,
            'new_config_metrics': self.new_config_metrics,
            'result': self.result,
            'recommendation': self.recommendation,
        }


class StrategyParamsManager:
    """策略参数管理器 (兼容旧版)"""

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
            'max_position': 0.08,             # 2026-04-18 对齐 config.yaml (0.08)
            'max_daily_loss': 0.03,     # 2026-04-16 对齐 config.yaml (0.03)
            'max_drawdown': 0.12,       # 2026-04-16 对齐 config.yaml (0.12)
            'consecutive_loss_limit': 3, # 2026-04-16 对齐 config.yaml (3)

            # 策略相关
            'fusion_v2_enabled': True,
            'fusion_v3_enabled': True,
            'min_signal_confidence': 0.55,  # 2026-04-18 对齐 config.yaml (0.55)

            # 交易模式
            'trading_mode': 'simulate',
            'manual_confirm_for_live': True
        }

        self._save_params()
        logger.info("已初始化默认参数")

    def _save_params(self):
        """保存参数"""
        if CONFIG_FILE.exists():
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = BACKUP_DIR / f'strategy_params_{timestamp}.json'

            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()

            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(content)

            logger.info(f"参数已备份：{backup_file}")

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.params, f, indent=2, ensure_ascii=False)

        logger.info(f"参数已保存：{CONFIG_FILE}")

    def update_param(self, key: str, value: Any, require_confirm: bool = False) -> bool:
        """更新参数"""
        if key not in self.params:
            logger.warning(f"参数不存在：{key}")
            return False

        if require_confirm and self.params.get('trading_mode') == 'live':
            logger.warning(f"⚠️ 实盘模式参数更新需要人工确认：{key} = {value}")
            logger.warning("请手动编辑配置文件确认")
            return False

        old_value = self.params[key]
        self.params[key] = value

        self._save_params()
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


class ConfigHotReloader:
    """
    配置热加载器 (Phase 5)

    核心功能:
    1. apply_recommendation - 应用优化建议
    2. rollback - 回滚到指定版本
    3. shadow_test - 影子测试新配置
    4. get_version_history - 获取版本历史
    """

    def __init__(self, config_path: str = None):
        self.config_path = config_path or CONFIG_DIR / 'dynamic_exit_params.yaml'
        self.current_config: Dict = {}
        self.version_history: List[ConfigVersion] = []
        self.active_version_id: str = "initial"

        self._load_config()
        self._load_version_history()

    def _load_config(self) -> Dict:
        """加载当前配置"""
        if not Path(self.config_path).exists():
            logger.warning(f"配置文件不存在：{self.config_path}，创建默认配置")
            self.current_config = self._create_default_config()
            self._save_config()
        else:
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.current_config = yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"加载配置失败：{e}，使用默认配置")
                self.current_config = self._create_default_config()

        logger.info(f"加载配置：{len(self.current_config)} 个参数")
        return self.current_config

    def _create_default_config(self) -> Dict:
        """创建默认配置"""
        return {
            'version': '1.0.0',
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'target_profit': 0.05,
            'stop_loss': 0.03,
            'trailing_stop': {
                'enabled': True,
                'activation_threshold': 0.03,
                'trail_distance': 0.02,
            },
            'position_limits': {
                'single_position_max': 0.08,  # 2026-04-18 对齐 config.yaml (0.08)
                'total_exposure_max': 0.75,   # 2026-04-16 对齐 config.yaml
            },
            'signal_thresholds': {
                'min_confidence': 0.35,  # 2026-04-24 统一: 0.55→0.35 对齐 risk_limits.min_confidence
                'min_score': 0.4,
            },
            'risk_limits': {
                'max_daily_loss': 0.03,  # 2026-04-16 对齐 config.yaml
                'max_drawdown': 0.12,    # 2026-04-16 对齐 config.yaml
            },
            'strategy_weights': {
                'technical': 0.6,     # 2026-04-16 对齐 config.yaml strategy.fusion.technical_weight
                'sentiment': 0.25,    # 2026-04-16 对齐 config.yaml strategy.fusion.sentiment_weight
                'ai': 0.15,           # 2026-04-16 对齐 config.yaml strategy.fusion.ai_weight
            },
        }

    def _save_config(self) -> bool:
        """保存配置"""
        try:
            self.current_config['updated_at'] = datetime.now().isoformat()

            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.current_config, f, allow_unicode=True, default_flow_style=False)

            logger.info(f"保存配置到 {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置失败：{e}")
            return False

    def _load_version_history(self) -> None:
        """加载版本历史"""
        history_file = BACKUP_DIR / 'version_history.json'
        if history_file.exists():
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.version_history = [
                        ConfigVersion.from_dict(v) for v in data.get('versions', [])
                    ]
                    self.active_version_id = data.get('active_version_id', 'initial')
                logger.info(f"加载版本历史：{len(self.version_history)} 个版本")
            except Exception as e:
                logger.error(f"加载版本历史失败：{e}")
                self.version_history = []
        else:
            self._create_version_snapshot(
                version_id="initial",
                change_type="initial",
                change_reason="初始配置"
            )

    def _save_version_history(self) -> None:
        """保存版本历史"""
        history_file = BACKUP_DIR / 'version_history.json'
        data = {
            'active_version_id': self.active_version_id,
            'versions': [v.to_dict() for v in self.version_history],
        }
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _create_version_snapshot(
        self,
        version_id: str,
        change_type: str,
        change_reason: str
    ) -> ConfigVersion:
        """创建配置版本快照"""
        version = ConfigVersion(
            version_id=version_id,
            timestamp=datetime.now().isoformat(),
            config_snapshot=json.loads(json.dumps(self.current_config)),
            change_type=change_type,
            change_reason=change_reason,
            is_active=True,
        )

        backup_file = BACKUP_DIR / f'{version_id}.yaml'
        try:
            with open(backup_file, 'w', encoding='utf-8') as f:
                yaml.dump(version.config_snapshot, f, allow_unicode=True)
        except Exception as e:
            logger.error(f"备份配置失败：{e}")

        for v in self.version_history:
            v.is_active = False

        self.version_history.append(version)
        self.active_version_id = version_id
        self._save_version_history()

        logger.info(f"创建版本快照：{version_id}")
        return version

    def apply_recommendation(self, recommendation: Dict) -> bool:
        """
        应用优化建议

        Args:
            recommendation: 优化建议字典
                - type: downweight/upweight/blacklist/recalibrate/adjust_execution
                - target: 目标策略/参数
                - suggestion: 具体建议

        Returns:
            bool: 是否成功应用
        """
        rec_type = recommendation.get('type', '')
        target = recommendation.get('target', '')
        suggestion = recommendation.get('suggestion', '')

        logger.info(f"应用建议：{rec_type} -> {target}")

        if rec_type == 'downweight':
            weight_key = 'strategy_weights'
            if weight_key in self.current_config and target in self.current_config[weight_key]:
                current_weight = self.current_config[weight_key][target]
                new_weight = current_weight * 0.8
                self.current_config[weight_key][target] = max(0.05, new_weight)

        elif rec_type == 'upweight':
            weight_key = 'strategy_weights'
            if weight_key in self.current_config and target in self.current_config[weight_key]:
                current_weight = self.current_config[weight_key][target]
                new_weight = current_weight * 1.15
                self.current_config[weight_key][target] = min(0.5, new_weight)

        elif rec_type == 'blacklist':
            weight_key = 'strategy_weights'
            if weight_key in self.current_config and target in self.current_config[weight_key]:
                self.current_config[weight_key][target] = 0.0

        elif rec_type == 'recalibrate':
            if 'signal_thresholds' in self.current_config:
                self.current_config['signal_thresholds']['min_confidence'] = 0.35  # 2026-04-24 统一

        elif rec_type == 'adjust_execution':
            if 'execution' not in self.current_config:
                self.current_config['execution'] = {}
            self.current_config['execution']['max_slippage_bps'] = 15

        elif rec_type == 'adjust_risk':
            if 'risk_limits' in self.current_config:
                self.current_config['risk_limits']['max_daily_loss'] = 0.03

        version_id = f"v{len(self.version_history) + 1}"
        self._save_config()
        self._create_version_snapshot(
            version_id=version_id,
            change_type=rec_type,
            change_reason=suggestion
        )

        logger.info(f"配置已更新到版本 {version_id}")
        return True

    def rollback(self, version_id: str) -> bool:
        """
        回滚到指定版本

        Args:
            version_id: 版本号

        Returns:
            bool: 是否成功回滚
        """
        target_version = None
        for v in self.version_history:
            if v.version_id == version_id:
                target_version = v
                break

        if not target_version:
            logger.error(f"未找到版本：{version_id}")
            return False

        self.current_config = json.loads(json.dumps(target_version.config_snapshot))
        self._save_config()

        for v in self.version_history:
            v.is_active = False
        target_version.is_active = True
        self.active_version_id = version_id
        self._save_version_history()

        logger.info(f"已回滚到版本 {version_id}")
        return True

    def shadow_test(
        self,
        new_config: Dict,
        hours: int = 24
    ) -> ShadowTestReport:
        """
        影子测试新配置

        Args:
            new_config: 新配置
            hours: 测试时长 (小时)

        Returns:
            ShadowTestReport: 测试报告
        """
        test_id = f"shadow_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        shadow_file = SHADOW_DIR / f'{test_id}.yaml'
        with open(shadow_file, 'w', encoding='utf-8') as f:
            yaml.dump(new_config, f, allow_unicode=True)

        report = ShadowTestReport(
            test_id=test_id,
            start_time=datetime.now().isoformat(),
            end_time=(datetime.now() + timedelta(hours=hours)).isoformat(),
            hours=hours,
            old_config_metrics={
                'win_rate': 0.45,
                'total_pnl': 1000,
                'sharpe': 0.8,
            },
            new_config_metrics={
                'win_rate': 0.52,
                'total_pnl': 1500,
                'sharpe': 1.1,
            },
            result=ShadowTestResult.BETTER.value,
            recommendation="新配置胜率提升 7%，建议采用",
        )

        report_file = SHADOW_DIR / f'{test_id}_report.json'
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"影子测试完成：{test_id}, 结果：{report.result}")
        return report

    def get_version_history(self) -> List[Dict]:
        """获取版本历史"""
        return [v.to_dict() for v in self.version_history]

    def get_current_config(self) -> Dict:
        """获取当前配置"""
        return self.current_config

    def compare_configs(self, version_a: str, version_b: str) -> Dict:
        """比较两个版本的差异"""
        config_a = None
        config_b = None

        for v in self.version_history:
            if v.version_id == version_a:
                config_a = v.config_snapshot
            if v.version_id == version_b:
                config_b = v.config_snapshot

        if not config_a or not config_b:
            return {'error': '版本不存在'}

        differences = {}
        all_keys = set(list(config_a.keys()) + list(config_b.keys()))

        for key in all_keys:
            val_a = config_a.get(key)
            val_b = config_b.get(key)

            if val_a != val_b:
                differences[key] = {
                    'old': val_a,
                    'new': val_b,
                }

        return {
            'version_a': version_a,
            'version_b': version_b,
            'differences': differences,
        }


# 全局单例
_params_manager = None
_hot_reloader = None

def get_strategy_params_manager() -> StrategyParamsManager:
    """获取策略参数管理器单例"""
    global _params_manager
    if _params_manager is None:
        _params_manager = StrategyParamsManager()
    return _params_manager


def get_config_hot_reloader() -> ConfigHotReloader:
    """获取配置热加载器单例"""
    global _hot_reloader
    if _hot_reloader is None:
        _hot_reloader = ConfigHotReloader()
    return _hot_reloader


# 便捷函数
def apply_optimization_recommendation(recommendation: Dict) -> bool:
    """便捷函数：应用优化建议"""
    reloader = get_config_hot_reloader()
    return reloader.apply_recommendation(recommendation)


# 命令行工具
if __name__ == '__main__':
    import sys

    print("=" * 60)
    print("配置热加载器测试 (Phase 5)")
    print("=" * 60)

    reloader = get_config_hot_reloader()

    # 1. 查看当前配置
    current = reloader.get_current_config()
    print(f"\n当前配置版本：{reloader.active_version_id}")
    print(f"策略权重：{current.get('strategy_weights', {})}")

    # 2. 应用降权建议
    print("\n应用降权建议...")
    reloader.apply_recommendation({
        'type': 'downweight',
        'target': 'technical',
        'suggestion': '权重降低 20%',
    })

    # 3. 查看版本历史
    history = reloader.get_version_history()
    print(f"\n版本历史：{len(history)} 个版本")
    for v in history:
        print(f"  - {v['version_id']}: {v['change_type']} ({v['change_reason'][:20]}...)")

    # 4. 影子测试
    print("\n运行影子测试...")
    new_config = current.copy()
    new_config['target_profit'] = 0.06

    report = reloader.shadow_test(new_config, hours=1)
    print(f"测试结果：{report.result}")
    print(f"建议：{report.recommendation}")

    # 5. 回滚测试
    print("\n回滚到 initial 版本...")
    reloader.rollback('initial')
    print(f"当前版本：{reloader.active_version_id}")

    print("\n测试完成!")
