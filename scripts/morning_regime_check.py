#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘前市场状态检查脚本

功能:
1. 获取恒指数据
2. 判断市场状态
3. 加载对应参数集
4. 推送钉钉通知

运行时间：每个交易日 09:00
"""

import sys
import os
import yaml
import logging
from datetime import datetime

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.strategy.market_regime import MarketRegimeDetector, MarketRegimeType
from app.notifier.dingtalk import DingTalkNotifier

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.morning_check")

# 参数配置文件
PARAMS_CONFIG_PATH = '/opt/ftseek/FTSeek/config/market_regime_params.yaml'


class MarketRegimeParamsLoader:
    """市场状态参数加载器"""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = {}
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            logger.info(f"市场状态参数配置已加载：{self.config_path}")
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
            self.config = {}
    
    def get_params(self, regime_type: MarketRegimeType) -> dict:
        """根据市场状态获取参数"""
        regime_key = regime_type.value
        
        # 优先使用特定状态参数，否则使用默认
        params = self.config.get(regime_key, self.config.get('default', {}))
        
        return {
            'signal_cooldown': params.get('signal_cooldown', 120),
            'min_confidence': params.get('min_confidence', 0.50),
            'max_single_position': params.get('max_single_position', 0.20),
            'max_total_position': params.get('max_total_position', 0.80),
            'max_daily_trades': params.get('max_daily_trades', 10),
            'description': params.get('description', '默认参数'),
        }


def get_hsi_regime() -> dict:
    """
    获取恒指市场状态
    
    实际应从富途 API 获取恒指数据
    这里使用简化版本
    """
    # TODO: 集成富途 API 获取恒指数据
    # 目前返回示例数据
    
    return {
        'regime_type': MarketRegimeType.WEAK_TREND_UP,
        'adx_value': 22.5,
        'trend': 'weak_up',
        'volatility': 'normal',
        'description': '弱上涨趋势',
    }


def format_regime_message(regime: dict, params: dict) -> str:
    """格式化钉钉推送消息"""
    
    # 状态图标
    regime_icons = {
        'strong_trend_up': '🚀',
        'weak_trend_up': '📈',
        'range_bound': '➡️',
        'weak_trend_down': '📉',
        'strong_trend_down': '🔻',
        'high_volatility': '⚡',
        'low_volatility': '🔍',
    }
    
    icon = regime_icons.get(regime['regime_type'].value, '📊')
    
    message = f"""
{icon} **FTSeek 市场状态日报** {icon}

📅 日期：{datetime.now().strftime('%Y-%m-%d %A')}
⏰ 时间：{datetime.now().strftime('%H:%M')}

━━━━━━━━━━━━━━━━━━━━

📊 **市场状态判断**

状态：{regime['description']}
ADX: {regime['adx_value']:.1f}
趋势：{regime['trend']}
波动率：{regime['volatility']}

━━━━━━━━━━━━━━━━━━━━

⚙️ **今日交易参数**

冷却时间：{params['signal_cooldown']}秒
置信度阈值：{params['min_confidence']}
单一仓位：{params['max_single_position']*100:.0f}%
总仓位：{params['max_total_position']*100:.0f}%
日交易上限：{params['max_daily_trades']}笔

说明：{params['description']}

━━━━━━━━━━━━━━━━━━━━

💡 **策略建议**

"""
    
    # 根据状态添加策略建议
    if regime['regime_type'] in (MarketRegimeType.STRONG_TREND_UP, MarketRegimeType.WEAK_TREND_UP):
        message += """• ✅ 趋势良好，可积极交易
• 优先使用趋势策略 (Supertrend, MACD)
• 适当提高仓位"""
    elif regime['regime_type'] == MarketRegimeType.RANGE_BOUND:
        message += """• ⚠️ 震荡行情，谨慎交易
• 优先使用震荡策略 (RSI, 布林带)
• 降低仓位，等待突破"""
    elif regime['regime_type'] in (MarketRegimeType.STRONG_TREND_DOWN, MarketRegimeType.WEAK_TREND_DOWN):
        message += """• 🛡️ 下跌趋势，防守为主
• 降低仓位和交易频率
• 考虑做空机会 (如支持)"""
    elif regime['regime_type'] == MarketRegimeType.HIGH_VOLATILITY:
        message += """• ⚡ 高波动，风险控制优先
• 大幅降低仓位
• 延长冷却时间，等待稳定"""
    else:
        message += """• 🔍 低波动，等待方向
• 关注突破信号
• 正常仓位"""
    
    message += """

━━━━━━━━━━━━━━━━━━━━

🤖 FTSeek 智能交易系统
"""
    
    return message


def apply_params(params: dict):
    """
    应用参数到运行时配置
    
    TODO: 需要修改 data_subscriber.py 支持动态参数加载
    """
    logger.info(f"应用参数：冷却={params['signal_cooldown']}s, "
                f"置信度={params['min_confidence']}, "
                f"单仓位={params['max_single_position']*100:.0f}%")
    
    # 实际应用中需要:
    # 1. 更新 config 模块的全局变量
    # 2. 通知 RiskManager 重新加载配置
    # 3. 通知 DataSubscriber 更新冷却时间
    
    # 示例：写入临时配置文件
    temp_config = {
        'runtime_params': params,
        'updated_at': datetime.now().isoformat(),
    }
    
    import json
    temp_path = '/opt/ftseek/FTSeek/config/runtime_params.json'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(temp_config, f, indent=2, ensure_ascii=False)
    
    logger.info(f"运行时参数已保存：{temp_path}")


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("开盘前市场状态检查开始")
    logger.info("=" * 60)
    
    try:
        # 1. 获取恒指市场状态
        logger.info("获取恒指市场状态...")
        regime = get_hsi_regime()
        logger.info(f"市场状态：{regime['description']}")
        
        # 2. 加载参数配置
        logger.info("加载参数配置...")
        params_loader = MarketRegimeParamsLoader(PARAMS_CONFIG_PATH)
        params = params_loader.get_params(regime['regime_type'])
        
        # 3. 应用参数
        logger.info("应用交易参数...")
        apply_params(params)
        
        # 4. 推送钉钉通知
        logger.info("推送钉钉通知...")
        try:
            notifier = DingTalkNotifier()
            message = format_regime_message(regime, params)
            # notifier.send_text(message)  # 实际推送
            logger.info("钉钉消息内容已生成")
            print(message)  # 输出到控制台
        except Exception as e:
            logger.warning(f"钉钉推送失败：{e}")
        
        logger.info("=" * 60)
        logger.info("✅ 市场状态检查完成")
        logger.info("=" * 60)
        
        return 0
        
    except Exception as e:
        logger.error(f"❌ 检查失败：{e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
