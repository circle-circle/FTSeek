from config.config_center import get_config_center
config_center = get_config_center()
#!/usr/bin/env python3
"""
美股隔夜监控器 (轻量版)

功能:
1. 监控美股持仓相关股票
2. 监控跨市场映射股票 (港股对应的美股)
3. 分析美股走势
4. 生成次日港股影响预测
5. 重大事件实时推送

运行时间: 21:00 - 06:00 HKT

设计理念:
- 轻量级，低资源占用
- 仅监控+预测，不执行交易
- 重大事件实时推送
- 生成结构化JSON供次日盘前分析使用
"""

import json
import logging
import os
import threading
import time
from datetime import datetime  # NOTE: don't import `time` from datetime — shadows stdlib `time` module
from typing import Dict, List, Optional

from app.notifier.dingtalk import DingTalkNotifier

logger = logging.getLogger("ftseek.us_overnight_monitor")


class USOvernightMonitor:
    """
    美股隔夜监控器
    
    职责:
    - 每5分钟检查一次美股数据
    - 识别重大涨跌幅 (>2%)
    - 生成次日预测JSON
    - 重大事件推送钉钉
    """
    
    # 跨市场映射 (港股 -> 美股)
    HK_US_MAPPING = {
        # 互联网/电商
        "HK.00700": "TCEHY",   # 腾讯
        "HK.09988": "BABA",    # 阿里
        "HK.09618": "JD",      # 京东
        "HK.01024": "KWEB",    # 快手 (中概互联)
        # 新能源车/汽车
        "HK.01211": "BYDDY",   # 比亚迪
        "HK.02015": "LI",      # 理想
        "HK.09868": "NIO",     # 小鹏
        "HK.01810": "XIACY",   # 小米
        "HK.00708": "EGREY",   # 恒大汽车
        # 医药/生物
        "HK.02269": None,       # 药明生物 (无活跃 US ADR, 跳过)
        "HK.06618": None,       # 京东健康 (无活跃 US ADR, 跳过)
        # 半导体
        "HK.00981": None,       # 中芯国际 (US ADR 已退市, 跳过)
        # 能源
        "HK.00883": "CNOOY",   # 中海油
        "HK.00857": "PTR",     # 中石油
        # AI/游戏
        "HK.00772": "CHLXY",   # 阅文
        # 旅游
        "HK.09961": "TCOM",    # 携程
    }
    
    def __init__(self, notifier: DingTalkNotifier):
        self.notifier = notifier
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_prediction: Optional[Dict] = None
        
        logger.info("美股隔夜监控器已初始化")
    
    def start(self):
        """启动监控"""
        if self._running:
            logger.warning("美股隔夜监控器已在运行")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("美股隔夜监控器已启动")
    
    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("美股隔夜监控器已停止")
    
    def _run(self):
        """监控主循环"""
        logger.info("[美股监控] 主循环启动")
        
        while self._running:
            try:
                now = datetime.now()
                
                # 仅在21:00-06:00运行
                if now.hour >= 21 or now.hour < 6:
                    self._check_us_market()
                else:
                    logger.debug(f"[美股监控] 非运行时段 ({now.hour}:00)，跳过")
                
                time.sleep(config_center.get('markets.us.night_interval', 900))  # 5分钟检查一次
                
            except Exception as e:
                logger.error(f"[美股监控] 异常: {e}", exc_info=True)
                time.sleep(config_center.get('markets.us.check_interval', 300))
    
    def _check_us_market(self):
        """检查美股数据"""
        try:
            # 1. 获取美股数据 (通过FTSeek-US或外部API)
            us_data = self._fetch_us_data()
            
            if not us_data:
                logger.debug("[美股监控] 无数据，跳过")
                return
            
            # 2. 分析涨跌幅
            significant_moves = self._analyze_moves(us_data)
            
            # 3. 生成次日预测
            prediction = self._generate_next_day_prediction(us_data)
            self._last_prediction = prediction
            
            # 4. 保存预测JSON
            self._save_prediction(prediction)
            
            # 5. 重大事件推送
            if significant_moves:
                logger.info(f"[美股监控] 发现 {len(significant_moves)} 个重大波动")
                self._push_urgent_alert(significant_moves)
            else:
                logger.debug("[美股监控] 无重大波动")
        
        except Exception as e:
            logger.error(f"[美股监控] 检查失败: {e}", exc_info=True)
    
    def _fetch_us_data(self) -> Optional[Dict]:
        """获取美股数据 (简化版)"""
        try:
            # TODO: 集成FTSeek-US API或Alpha Vantage
            # 临时方案：读取缓存文件
            cache_file = "data/us_overnight_cache.json"
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    logger.debug(f"[美股监控] 从缓存读取 {len(data)} 个股票")
                    return data
            
            logger.debug("[美股监控] 缓存文件不存在")
            return None
            
        except Exception as e:
            logger.error(f"[美股监控] 获取数据失败: {e}")
            return None
    
    def _analyze_moves(self, us_data: Dict) -> List[Dict]:
        """分析重大涨跌幅"""
        significant = []
        
        for code, data in us_data.items():
            change_pct = data.get('change_pct', 0)
            
            if abs(change_pct) > 2.0:  # 涨跌幅>2%
                significant.append({
                    "code": code,
                    "change_pct": change_pct,
                    "price": data.get('price', 0),
                    "volume": data.get('volume', 0)
                })
                logger.info(f"[美股监控] 重大波动: {code} {change_pct:+.2f}%")
        
        return significant
    
    def _generate_next_day_prediction(self, us_data: Dict) -> Dict:
        """生成次日预测"""
        try:
            # 简化版：基于美股整体表现判断
            changes = [d.get('change_pct', 0) for d in us_data.values()]
            avg_change = sum(changes) / len(changes) if changes else 0
            
            if avg_change > 2.0:
                bias = "strong_long"
            elif avg_change > 1.0:
                bias = "moderate_long"
            elif avg_change < -2.0:
                bias = "strong_short"
            elif avg_change < -1.0:
                bias = "moderate_short"
            else:
                bias = "neutral"
            
            # 统计涨跌数量
            up_count = sum(1 for c in changes if c > 0)
            down_count = sum(1 for c in changes if c < 0)
            
            return {
                "session": "overnight",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "next_day_bias": bias,
                "us_market_summary": {
                    "avg_change": round(avg_change, 2),
                    "up_count": up_count,
                    "down_count": down_count,
                    "stocks_monitored": list(us_data.keys())
                },
                "key_events": [],  # TODO: 从新闻中提取
                "focus_symbols": list(self.HK_US_MAPPING.keys()),
                "risk_flags": [],
                "simulation_notes": [
                    f"美股平均涨跌{avg_change:.2f}%，次日港股可能{'高开' if avg_change > 0 else '低开'}"
                ],
                "generated_at": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"[美股监控] 预测生成失败: {e}")
            return {
                "session": "overnight",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "next_day_bias": "neutral",
                "error": str(e),
                "generated_at": datetime.now().isoformat()
            }
    
    def _save_prediction(self, prediction: Dict):
        """保存预测JSON"""
        try:
            date = datetime.now().strftime("%Y-%m-%d")
            filepath = f"data/overnight_predictions/{date}.json"
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(prediction, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"[美股监控] 预测已保存至 {filepath}")
            
        except Exception as e:
            logger.error(f"[美股监控] 保存预测失败: {e}", exc_info=True)
    
    def _push_urgent_alert(self, significant_moves: List[Dict]):
        """推送重大事件告警"""
        try:
            markdown_text = "## 🚨 美股重大波动\n\n"
            
            for move in significant_moves:
                direction = "📈" if move['change_pct'] > 0 else "📉"
                markdown_text += (
                    f"{direction} **{move['code']}**: "
                    f"{move['change_pct']:+.2f}%\n\n"
                )
            
            markdown_text += f"\n_更新时间: {datetime.now().strftime('%H:%M')}_"
            self.notifier.send_text(markdown_text)
            logger.info("[美股监控] 已推送重大波动告警")
            
        except Exception as e:
            logger.error(f"[美股监控] 推送告警失败: {e}", exc_info=True)
    
    def get_latest_prediction(self) -> Optional[Dict]:
        """获取最新预测"""
        return self._last_prediction


# 全局单例
_monitor: Optional[USOvernightMonitor] = None


def get_monitor() -> Optional[USOvernightMonitor]:
    """获取监控器单例"""
    return _monitor


def init_monitor(notifier: DingTalkNotifier) -> USOvernightMonitor:
    """初始化监控器"""
    global _monitor
    _monitor = USOvernightMonitor(notifier)
    return _monitor
