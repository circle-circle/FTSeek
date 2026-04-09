#!/usr/bin/env python3
"""
FTSeek 美股模拟交易系统

功能：
1. 不需要行情订阅（模拟数据）
2. 基于港股同类型股票趋势进行买卖
3. 模拟交易，独立于港股系统

港股-美股对应关系：
- 港股科技股 (腾讯/阿里/美团) → 美股科技股 (AAPL/GOOGL/MSFT)
- 港股消费股 (泡泡玛特) → 美股消费股 (AMZN/META)
- 港股半导体 (中芯国际) → 美股半导体 (NVDA/AMD)

使用方式：
    python3 scripts/us_market_simulator.py
"""
import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/us_simulator.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.us_simulator")


# 港股-美股映射关系
HK_US_MAPPING = {
    # 港股科技 → 美股科技
    "HK.00700": ["US.AAPL", "US.MSFT"],      # 腾讯 → 苹果/微软
    "HK.09988": ["US.BABA", "US.AMZN"],       # 阿里 → 阿里巴巴/亚马逊
    "HK.03690": ["US.GOOGL", "US.META"],      # 美团 → 谷歌/Meta
    "HK.01810": ["US.AAPL", "US.TSLA"],       # 小米 → 苹果/特斯拉
    
    # 港股消费 → 美股消费
    "HK.09992": ["US.AMZN", "US.NFLX"],       # 泡泡玛特 → 亚马逊/奈飞
    
    # 港股半导体 → 美股半导体
    "HK.00981": ["US.NVDA", "US.AMD"],        # 中芯国际 → 英伟达/AMD
}

# 美股默认配置
US_STOCKS = {
    "US.AAPL": {"name": "苹果", "sector": "科技", "base_price": 245.0},
    "US.MSFT": {"name": "微软", "sector": "科技", "base_price": 380.0},
    "US.GOOGL": {"name": "谷歌", "sector": "科技", "base_price": 160.0},
    "US.AMZN": {"name": "亚马逊", "sector": "消费", "base_price": 185.0},
    "US.NVDA": {"name": "英伟达", "sector": "半导体", "base_price": 125.0},
    "US.TSLA": {"name": "特斯拉", "sector": "汽车", "base_price": 280.0},
    "US.META": {"name": "Meta", "sector": "科技", "base_price": 500.0},
    "US.AMD": {"name": "AMD", "sector": "半导体", "base_price": 150.0},
}

# 美股交易时间 (北京时间)
US_MARKET_HOURS = {
    "open": (21, 30),   # 21:30 开盘
    "close": (4, 0),    # 04:00 收盘
}


@dataclass
class USSimulatedPosition:
    """美股模拟持仓"""
    code: str
    name: str
    action: str  # BUY/SELL
    quantity: int
    entry_price: float
    current_price: float
    pnl_pct: float
    entry_time: str
    signal_source: str  # 触发信号的港股代码


@dataclass
class USSimulatedSignal:
    """美股模拟信号"""
    code: str
    name: str
    action: str
    price: float
    quantity: int
    confidence: float
    signal_source: str
    reason: str
    timestamp: str


class HKTrendAnalyzer:
    """港股趋势分析器"""
    
    def __init__(self):
        self.db_path = "/opt/ftseek/FTSeek/data/ftseek.db"
    
    def get_hk_trend(self, code: str) -> Dict:
        """
        获取港股趋势
        
        Returns:
            {
                'trend': 'up'/'down'/'neutral',
                'change_pct': 0.02,
                'signal_count': 5,
                'buy_signals': 3,
                'sell_signals': 2
            }
        """
        result = {
            'trend': 'neutral',
            'change_pct': 0.0,
            'signal_count': 0,
            'buy_signals': 0,
            'sell_signals': 0
        }
        
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            
            # 获取今日信号
            cursor = conn.execute("""
                SELECT action, COUNT(*) 
                FROM signals 
                WHERE code = ? AND date(created_at) = date('now')
                GROUP BY action
            """, (code,))
            
            for row in cursor.fetchall():
                result['signal_count'] += row[1]
                if row[0] == 'BUY':
                    result['buy_signals'] = row[1]
                elif row[0] == 'SELL':
                    result['sell_signals'] = row[1]
            
            # 计算趋势
            if result['buy_signals'] > result['sell_signals'] * 1.5:
                result['trend'] = 'up'
            elif result['sell_signals'] > result['buy_signals'] * 1.5:
                result['trend'] = 'down'
            
            conn.close()
            
        except Exception as e:
            logger.warning(f"获取港股趋势失败 {code}: {e}")
        
        return result
    
    def get_hk_capital_flow(self, code: str) -> float:
        """获取港股资金流向情绪"""
        try:
            from app.news_collector import FutuMarketData
            futu = FutuMarketData()
            flow = futu.get_capital_flow(code)
            return flow.get('sentiment', 0)
        except Exception as e:
            logger.debug(f"获取资金流向失败: {e}")
            return 0


class USMarketSimulator:
    """美股模拟交易器"""
    
    def __init__(self, initial_capital: float = 50000.0):
        self.capital = initial_capital
        self.positions: Dict[str, USSimulatedPosition] = {}
        self.signals: List[USSimulatedSignal] = []
        self.hk_analyzer = HKTrendAnalyzer()
        self.notifier = DingTalkNotifier()
        
        # 加载状态
        self._load_state()
        
        logger.info(f"美股模拟交易器已初始化 | 初始资金: ${initial_capital:,.0f}")
    
    def _load_state(self):
        """加载模拟状态"""
        state_file = "/opt/ftseek/FTSeek/data/us_simulator_state.json"
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r') as f:
                    data = json.load(f)
                    self.capital = data.get('capital', 50000)
                    self.positions = {
                        k: USSimulatedPosition(**v) 
                        for k, v in data.get('positions', {}).items()
                    }
                logger.info(f"已加载模拟状态 | 资金: ${self.capital:,.0f} | 持仓: {len(self.positions)}")
            except Exception as e:
                logger.warning(f"加载状态失败: {e}")
    
    def _save_state(self):
        """保存模拟状态"""
        state_file = "/opt/ftseek/FTSeek/data/us_simulator_state.json"
        try:
            data = {
                'capital': self.capital,
                'positions': {k: asdict(v) for k, v in self.positions.items()},
                'updated': datetime.now().isoformat()
            }
            with open(state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")
    
    def is_market_open(self) -> bool:
        """检查美股是否开盘"""
        now = datetime.now()
        hour = now.hour
        
        # 美股交易时间: 北京时间 21:30 - 04:00
        if hour >= 21 or hour < 4:
            return True
        return False
    
    def generate_signals_from_hk(self) -> List[USSimulatedSignal]:
        """
        基于港股趋势生成美股信号
        
        逻辑:
        1. 遍历港股-美股映射
        2. 获取港股趋势和资金流向
        3. 如果港股趋势 + 资金流向一致，生成美股信号
        """
        signals = []
        
        for hk_code, us_codes in HK_US_MAPPING.items():
            # 获取港股趋势
            trend = self.hk_analyzer.get_hk_trend(hk_code)
            sentiment = self.hk_analyzer.get_hk_capital_flow(hk_code)
            
            if trend['signal_count'] == 0:
                continue
            
            # 计算信号置信度
            confidence = 0.5
            
            if trend['trend'] == 'up' and sentiment > 0.3:
                # 港股上涨 + 资金流入 → 美股买入
                action = 'BUY'
                confidence = 0.5 + min(0.3, sentiment * 0.3)
                
            elif trend['trend'] == 'down' and sentiment < -0.3:
                # 港股下跌 + 资金流出 → 美股卖出
                action = 'SELL'
                confidence = 0.5 + min(0.3, abs(sentiment) * 0.3)
                
            else:
                continue
            
            # 为对应的每个美股生成信号
            for us_code in us_codes:
                if us_code not in US_STOCKS:
                    continue
                
                us_info = US_STOCKS[us_code]
                base_price = us_info['base_price']
                
                # 模拟价格波动 (±1%)
                simulated_price = base_price * (1 + random.uniform(-0.01, 0.01))
                
                # 生成信号
                signal = USSimulatedSignal(
                    code=us_code,
                    name=us_info['name'],
                    action=action,
                    price=round(simulated_price, 2),
                    quantity=10,  # 美股默认 10 股
                    confidence=round(confidence, 2),
                    signal_source=hk_code,
                    reason=f"港股 {hk_code} {trend['trend']} 趋势 | 资金情绪 {sentiment:+.2f}",
                    timestamp=datetime.now().isoformat()
                )
                
                signals.append(signal)
                self.signals.append(signal)
                
                logger.info(
                    f"[美股信号] {us_code} {action} @ ${simulated_price:.2f} | "
                    f"来源: {hk_code} | 置信度: {confidence:.2f}"
                )
        
        return signals
    
    def execute_trade(self, signal: USSimulatedSignal) -> bool:
        """执行模拟交易"""
        if signal.code in self.positions:
            logger.info(f"{signal.code} 已有持仓，跳过")
            return False
        
        # 计算交易金额
        trade_amount = signal.price * signal.quantity
        
        if signal.action == 'BUY':
            if trade_amount > self.capital:
                logger.warning(f"资金不足: 需要 ${trade_amount:,.0f}, 可用 ${self.capital:,.0f}")
                return False
            
            # 买入
            self.capital -= trade_amount
            position = USSimulatedPosition(
                code=signal.code,
                name=signal.name,
                action='BUY',
                quantity=signal.quantity,
                entry_price=signal.price,
                current_price=signal.price,
                pnl_pct=0.0,
                entry_time=signal.timestamp,
                signal_source=signal.signal_source
            )
            self.positions[signal.code] = position
            
            logger.info(f"✅ 买入 {signal.code} {signal.quantity}股 @ ${signal.price:.2f}")
            
        elif signal.action == 'SELL':
            # 模拟做空 (记录但不扣资金)
            position = USSimulatedPosition(
                code=signal.code,
                name=signal.name,
                action='SELL',
                quantity=signal.quantity,
                entry_price=signal.price,
                current_price=signal.price,
                pnl_pct=0.0,
                entry_time=signal.timestamp,
                signal_source=signal.signal_source
            )
            self.positions[signal.code] = position
            
            logger.info(f"✅ 做空 {signal.code} {signal.quantity}股 @ ${signal.price:.2f}")
        
        self._save_state()
        return True
    
    def update_prices(self):
        """更新持仓价格和盈亏"""
        for code, pos in self.positions.items():
            if code in US_STOCKS:
                # 模拟价格波动
                base_price = US_STOCKS[code]['base_price']
                change_pct = random.uniform(-0.02, 0.02)  # ±2%
                pos.current_price = round(base_price * (1 + change_pct), 2)
                
                # 计算盈亏
                if pos.action == 'BUY':
                    pos.pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
                else:  # SELL
                    pos.pnl_pct = (pos.entry_price - pos.current_price) / pos.entry_price
    
    def check_take_profit_stop_loss(self):
        """检查止盈止损"""
        closed = []
        
        for code, pos in list(self.positions.items()):
            # 止盈: +5%
            if pos.pnl_pct >= 0.05:
                logger.info(f"🎯 止盈触发: {code} +{pos.pnl_pct:.1%}")
                self._close_position(code, "止盈")
                closed.append(code)
            
            # 止损: -3%
            elif pos.pnl_pct <= -0.03:
                logger.info(f"🛑 止损触发: {code} {pos.pnl_pct:.1%}")
                self._close_position(code, "止损")
                closed.append(code)
        
        return closed
    
    def _close_position(self, code: str, reason: str):
        """平仓"""
        if code not in self.positions:
            return
        
        pos = self.positions[code]
        
        # 计算盈亏
        pnl_amount = pos.entry_price * pos.quantity * pos.pnl_pct
        
        if pos.action == 'BUY':
            self.capital += pos.current_price * pos.quantity
        
        logger.info(
            f"💼 平仓 {code} | {reason} | "
            f"盈亏: ${pnl_amount:+,.0f} ({pos.pnl_pct:+.1%})"
        )
        
        del self.positions[code]
        self._save_state()
    
    def send_report(self):
        """发送报告到钉钉"""
        total_pnl = sum(
            pos.entry_price * pos.quantity * pos.pnl_pct 
            for pos in self.positions.values()
        )
        
        message = f"""
📊 **美股模拟交易报告**

🕐 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}

---

### 💰 资金状况
- 可用资金: ${self.capital:,.0f}
- 持仓数量: {len(self.positions)} 笔
- 浮动盈亏: ${total_pnl:+,.0f}

---

### 📈 持仓明细
"""
        
        if self.positions:
            for code, pos in self.positions.items():
                pnl_emoji = "🟢" if pos.pnl_pct >= 0 else "🔴"
                message += f"- {pos.name} ({code}): {pos.action} {pos.quantity}股\n"
                message += f"  入场: ${pos.entry_price:.2f} → 现价: ${pos.current_price:.2f}\n"
                message += f"  {pnl_emoji} {pos.pnl_pct:+.1%} | 来源: {pos.signal_source}\n"
        else:
            message += "- 无持仓\n"
        
        message += """
---

### 📝 说明
- 美股模拟交易，基于港股趋势生成信号
- 不订阅实时行情，使用模拟价格
- 止盈: +5% | 止损: -3%
"""
        
        try:
            self.notifier.send_text(message)
            logger.info("报告已发送到钉钉")
        except Exception as e:
            logger.error(f"发送报告失败: {e}")
    
    def run_cycle(self):
        """运行一个周期"""
        logger.info("=" * 50)
        logger.info("美股模拟交易周期开始")
        
        # 1. 更新价格
        self.update_prices()
        
        # 2. 检查止盈止损
        closed = self.check_take_profit_stop_loss()
        
        # 3. 生成新信号 (基于港股)
        signals = self.generate_signals_from_hk()
        
        # 4. 执行交易
        for signal in signals:
            # 随机执行 (模拟过滤)
            if signal.confidence >= 0.6 and random.random() > 0.3:
                self.execute_trade(signal)
        
        # 5. 保存状态
        self._save_state()
        
        logger.info(f"周期结束 | 信号: {len(signals)} | 持仓: {len(self.positions)}")
        
        return {
            'signals': len(signals),
            'positions': len(self.positions),
            'capital': self.capital
        }


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="FTSeek 美股模拟交易")
    parser.add_argument("--once", action="store_true", help="运行一次后退出")
    parser.add_argument("--interval", type=int, default=300, help="运行间隔 (秒)")
    parser.add_argument("--report", action="store_true", help="发送报告到钉钉")
    args = parser.parse_args()
    
    simulator = USMarketSimulator()
    
    if args.report:
        simulator.send_report()
        return
    
    if args.once:
        simulator.run_cycle()
        simulator.send_report()
    else:
        logger.info(f"启动美股模拟交易 | 间隔: {args.interval}秒")
        while True:
            try:
                simulator.run_cycle()
                
                # 每 30 分钟发送报告
                if datetime.now().minute % 30 == 0:
                    simulator.send_report()
                
            except Exception as e:
                logger.error(f"运行错误: {e}")
            
            time.sleep(args.interval)


if __name__ == "__main__":
    main()