#!/usr/bin/env python3
"""
FTSeek 策略信号监控脚本

功能:
- 定时从富途 API 获取股票行情
- 运行多个策略生成买卖信号
- 推送到钉钉群
- 记录信号到数据库
- 跟踪收益率

使用方式:
    python3 scripts/strategy_monitor.py

定时任务 (crontab):
    */15 9-16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/strategy_monitor.py >> logs/strategy_monitor.log 2>&1
"""
import sys
import os
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage.database import Database
from app.notifier.dingtalk import DingTalkNotifier
from app import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("ftseek.scripts.strategy_monitor")

# 关注的港股列表 (富途格式)
WATCHLIST = [
    'HK.00700',  # 腾讯控股
    'HK.09988',  # 阿里巴巴
    'HK.09992',  # 泡泡玛特
    'HK.02390',  # 智谱 AI
    'HK.03690',  # 美团
]

# 策略配置
STRATEGIES_CONFIG = {
    'ma_cross': {
        'enabled': True,
        'fast_period': 5,
        'slow_period': 20,
    },
    'rsi_rebound': {
        'enabled': True,
        'rsi_period': 14,
        'oversold_threshold': 30,
        'overbought_threshold': 70,
    },
    'macd': {
        'enabled': True,
        'fast_period': 12,
        'slow_period': 26,
        'signal_period': 9,
    },
}


def get_kline_data(code: str, kline_type: str = '1M', count: int = 100) -> List[Dict]:
    """
    从富途 API 获取 K 线数据
    
    Args:
        code: 股票代码 (富途格式)
        kline_type: K 线类型 (1M/5M/15M/30M/60M/D/W/M)
        count: 获取数量
        
    Returns:
        K 线数据列表
    """
    try:
        from futu import OpenQuoteContext, KLType
        
        # 创建行情上下文
        quote_ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        
        # 转换 K 线类型
        kl_type_map = {
            '1M': KLType.K_1M,
            '5M': KLType.K_5M,
            '15M': KLType.K_15M,
            '30M': KLType.K_30M,
            '60M': KLType.K_60M,
            'D': KLType.K_DAY,
            'W': KLType.K_WEEK,
            'M': KLType.K_MON,
        }
        kl_type = kl_type_map.get(kline_type, KLType.K_DAY)
        
        # 获取历史 K 线
        ret, data, page_req_key = quote_ctx.request_history_klines(
            code=code,
            ktype=kl_type,
            autype='AQF',  # 前复权
            start='',
            end='',
            max_count=count,
        )
        
        quote_ctx.close()
        
        if ret == 0 and data is not None and not data.empty:
            klines = []
            for _, row in data.iterrows():
                klines.append({
                    'time': row['time_key'],
                    'open': row['open'],
                    'close': row['close'],
                    'high': row['high'],
                    'low': row['low'],
                    'volume': row['volume'],
                })
            return klines
        else:
            logger.warning(f"获取 {code} K 线失败：{data}")
            return []
            
    except Exception as e:
        logger.error(f"获取 {code} K 线异常：{e}")
        return []


def run_strategy(strategy_name: str, code: str, klines: List[Dict]) -> Optional[Dict]:
    """
    运行单个策略
    
    Args:
        strategy_name: 策略名称
        code: 股票代码
        klines: K 线数据
        
    Returns:
        信号字典或 None
    """
    try:
        if strategy_name == 'ma_cross':
            from app.strategy.ma_cross import MACrossStrategy
            cfg = STRATEGIES_CONFIG.get('ma_cross', {})
            strategy = MACrossStrategy(cfg)
            
            # 按时间顺序处理 K 线
            for kline in klines:
                signal = strategy.on_kline(code, kline, 'D')
                if signal:
                    return {
                        'code': signal.code,
                        'name': signal.name,
                        'action': signal.action.value,
                        'price': signal.price,
                        'quantity': signal.quantity,
                        'strategy': signal.strategy,
                        'reason': signal.reason,
                        'created_at': datetime.now().isoformat(),
                    }
                    
        elif strategy_name == 'rsi_rebound':
            from app.strategy.rsi_strategy import RSIStrategy
            cfg = STRATEGIES_CONFIG.get('rsi_rebound', {})
            strategy = RSIStrategy(cfg)
            
            for kline in klines:
                signal = strategy.on_kline(code, kline, 'D')
                if signal:
                    return {
                        'code': signal.code,
                        'name': signal.name,
                        'action': signal.action.value,
                        'price': signal.price,
                        'quantity': signal.quantity,
                        'strategy': signal.strategy,
                        'reason': signal.reason,
                        'created_at': datetime.now().isoformat(),
                    }
        
        elif strategy_name == 'macd':
            from app.strategy.macd_strategy import MACDStrategy
            cfg = STRATEGIES_CONFIG.get('macd', {})
            strategy = MACDStrategy(cfg)
            
            for kline in klines:
                signal = strategy.on_kline(code, kline, 'D')
                if signal:
                    return {
                        'code': signal.code,
                        'name': signal.name,
                        'action': signal.action.value,
                        'price': signal.price,
                        'quantity': signal.quantity,
                        'strategy': signal.strategy,
                        'reason': signal.reason,
                        'created_at': datetime.now().isoformat(),
                    }
        
    except Exception as e:
        logger.error(f"运行策略 {strategy_name} 失败：{e}")
    
    return None


def save_signal_to_db(signal: Dict, db: Database):
    """保存信号到数据库"""
    import hashlib
    
    signal_id = hashlib.md5(
        f"{signal['code']}_{signal['action']}_{signal['created_at']}".encode()
    ).hexdigest()[:16]
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO signals 
            (id, code, name, action, price, quantity, strategy, reason, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_id,
            signal['code'],
            signal.get('name', signal['code']),
            signal['action'],
            signal['price'],
            signal.get('quantity', 100),
            signal.get('strategy', 'manual'),
            signal.get('reason', ''),
            signal['created_at'],
            'pending',
        ))


def send_signal_to_dingtalk(signal: Dict, notifier: DingTalkNotifier):
    """发送信号到钉钉"""
    try:
        # 构建分析数据
        analysis = {
            'sentiment': 'bullish' if signal['action'] == 'BUY' else 'bearish',
            'impact_score': 7,
            'suggestion': 'buy' if signal['action'] == 'BUY' else 'sell',
            'reason': signal.get('reason', '')[:100],
        }
        
        # 使用 DingTalkNotifier 的 send_signal 方法推送
        notifier.send_signal(signal)
        logger.info(f"钉钉推送成功：{signal['code']} {signal['action']}")
        
    except Exception as e:
        logger.error(f"钉钉推送失败：{e}")


def calculate_returns(db: Database) -> Dict:
    """
    计算策略收益率
    
    Returns:
        收益率统计字典
    """
    with db.get_connection() as conn:
        cursor = conn.cursor()
        
        # 查询已执行信号
        cursor.execute("""
            SELECT action, price, created_at, executed_at
            FROM signals
            WHERE status = 'executed'
            ORDER BY created_at DESC
            LIMIT 100
        """)
        
        signals = cursor.fetchall()
        
        if not signals:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_return': 0,
                'avg_return': 0,
            }
        
        # 简化计算 (实际应该配对买卖信号)
        buys = [s for s in signals if s['action'] == 'BUY']
        sells = [s for s in signals if s['action'] == 'SELL']
        
        total_trades = min(len(buys), len(sells))
        
        if total_trades == 0:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_return': 0,
                'avg_return': 0,
            }
        
        # 简化收益率计算
        wins = 0
        total_return = 0
        
        for i in range(total_trades):
            buy_price = buys[i]['price']
            sell_price = sells[i]['price']
            
            if sell_price > buy_price:
                wins += 1
            
            ret = (sell_price - buy_price) / buy_price
            total_return += ret
        
        return {
            'total_trades': total_trades,
            'win_rate': wins / total_trades * 100 if total_trades > 0 else 0,
            'total_return': total_return * 100,
            'avg_return': (total_return / total_trades * 100) if total_trades > 0 else 0,
        }


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("🎯 FTSeek 策略信号监控开始")
    logger.info("=" * 60)
    logger.info(f"关注股票：{len(WATCHLIST)} 只")
    logger.info(f"启用策略：{[k for k, v in STRATEGIES_CONFIG.items() if v.get('enabled')]}")
    logger.info(f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 初始化组件
    try:
        db = Database()
        notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET,
        )
    except Exception as e:
        logger.error(f"初始化组件失败：{e}")
        return 1
    
    # 统计
    total_signals = 0
    signals_by_action = {'BUY': 0, 'SELL': 0}
    
    # 遍历股票和策略
    for code in WATCHLIST:
        logger.info(f"\n分析股票：{code}")
        
        # 获取 K 线数据 (日线)
        klines = get_kline_data(code, kline_type='D', count=100)
        
        if not klines:
            logger.warning(f"{code} 无 K 线数据，跳过")
            continue
        
        logger.info(f"  获取 K 线：{len(klines)} 条")
        
        # 运行每个策略
        for strategy_name, cfg in STRATEGIES_CONFIG.items():
            if not cfg.get('enabled', False):
                continue
            
            logger.info(f"  运行策略：{strategy_name}")
            
            signal = run_strategy(strategy_name, code, klines)
            
            if signal:
                logger.info(f"    ✓ 生成信号：{signal['action']} @ {signal['price']}")
                
                # 保存信号
                save_signal_to_db(signal, db)
                
                # 推送钉钉
                send_signal_to_dingtalk(signal, notifier)
                
                total_signals += 1
                signals_by_action[signal['action']] += 1
                
                # 避免推送太快
                time.sleep(2)
        
        # 避免请求太快
        time.sleep(1)
    
    # 发送汇总报告
    if total_signals > 0:
        logger.info(f"\n✅ 本周期生成 {total_signals} 条信号")
        logger.info(f"   买入：{signals_by_action['BUY']} 条")
        logger.info(f"   卖出：{signals_by_action['SELL']} 条")
        
        # 发送汇总 (简化为日志)
        logger.info(f"📊 本周期监控 {len(WATCHLIST)} 只股票，生成 {total_signals} 条信号")
    else:
        logger.info("\nℹ️  本周期无新信号")
    
    # 收益率统计
    try:
        returns = calculate_returns(db)
        logger.info(f"\n📈 策略收益率统计:")
        logger.info(f"   总交易：{returns['total_trades']} 笔")
        logger.info(f"   胜率：{returns['win_rate']:.1f}%")
        logger.info(f"   总收益：{returns['total_return']:.2f}%")
        logger.info(f"   平均收益：{returns['avg_return']:.2f}%")
    except Exception as e:
        logger.error(f"计算收益率失败：{e}")
    
    logger.info("=" * 60)
    logger.info("✅ 策略信号监控完成")
    logger.info("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
