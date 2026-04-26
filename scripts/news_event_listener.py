#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻事件监听器 (旁路进程) - Phase 1
架构: 富途原生新闻 API → LLM 意图识别 → 共享 SQLite 信号表
原则: 零侵入现有代码，独立部署，崩溃不影响主系统
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | [NewsListener] | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/news_listener.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.news_listener")

# 共享信号表路径 (与主系统共享)
PROJECT_ROOT = Path("/opt/ftseek/FTSeek")
SIGNALS_DB = PROJECT_ROOT / "data" / "news_signals.db"
PROCESSED_FILE = PROJECT_ROOT / "data" / "news_processed_ids.json"


class NewsSignalInjector:
    """新闻信号注入器 - 管理共享 SQLite 表"""
    
    def __init__(self):
        self._init_db()
        self._processed_ids = self._load_processed_ids()
        # [P2 修复] 信号去重：记录最近注入的 (code+action) 时间戳，防止同类信号反复注入
        self._signal_cooldown = {}  # (code, action) -> last_inject_timestamp
        self._signal_cooldown_secs = 1800  # 30 分钟冷却
        
    def _init_db(self):
        """初始化共享信号表 (仅创建，不修改现有表)"""
        conn = sqlite3.connect(SIGNALS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_signals (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL,
                priority INTEGER,
                reason TEXT,
                source TEXT DEFAULT 'news_event',
                created_at TEXT,
                consumed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"✅ 信号表已初始化: {SIGNALS_DB}")
    
    def _load_processed_ids(self) -> set:
        """加载已处理新闻 ID (防重复)"""
        if PROCESSED_FILE.exists():
            try:
                with open(PROCESSED_FILE, 'r') as f:
                    data = json.load(f)
                    # 只保留最近 24 小时的 ID
                    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
                    return {nid for nid, ts in data.items() if ts > cutoff}
            except:
                pass
        return set()
    
    def _save_processed_id(self, news_id: str):
        """保存已处理新闻 ID"""
        self._processed_ids.add(news_id)
        try:
            data = {nid: datetime.now().isoformat() for nid in list(self._processed_ids)[-1000:]}
            with open(PROCESSED_FILE, 'w') as f:
                json.dump(data, f)
        except:
            pass
    
    def inject_signal(self, news_id: str, code: str, action: str, 
                     confidence: float, priority: int, reason: str) -> bool:
        """注入新闻信号到共享表"""
        # [P2 修复] 检查信号冷却：同一股票+方向 30 分钟内不重复注入
        cooldown_key = (code, action)
        now_ts = time.time()
        if cooldown_key in self._signal_cooldown:
            elapsed = now_ts - self._signal_cooldown[cooldown_key]
            if elapsed < self._signal_cooldown_secs:
                remaining = int(self._signal_cooldown_secs - elapsed)
                logger.debug(f"🔇 信号冷却中: {code} {action}, 剩余 {remaining}s，跳过")
                return False
        
        try:
            conn = sqlite3.connect(SIGNALS_DB)
            conn.execute("""
                INSERT OR IGNORE INTO news_signals 
                (id, code, action, confidence, priority, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (news_id, code, action, confidence, priority, reason, 
                  datetime.now().isoformat()))
            conn.commit()
            conn.close()
            # [P2 修复] 记录注入时间
            self._signal_cooldown[cooldown_key] = now_ts
            logger.info(f"📡 信号已注入: {code} {action} (conf={confidence:.2f}, pri={priority})")
            return True
        except Exception as e:
            logger.error(f"信号注入失败: {e}")
            return False


class FutuNewsFetcher:
    """新闻获取器 - 复用现有 NewsCollector"""
    
    def __init__(self):
        self.collector = None
        self._init_collector()
    
    def _init_collector(self):
        """初始化新闻收集器"""
        try:
            from app.news_push.collector import get_collector
            self.collector = get_collector()
            logger.info("✅ 已初始化 NewsCollector")
        except Exception as e:
            logger.error(f"初始化 NewsCollector 失败: {e}")
            self.collector = None
    
    def get_latest_news(self, symbols: List[str] = None, count: int = 50) -> List[Dict]:
        """获取最新新闻"""
        if not self.collector:
            self._init_collector()
            if not self.collector:
                return []
        
        try:
            # 使用现有收集器
            news_items = self.collector.fetch_all()
            
            all_news = []
            for item in news_items[:count]:
                all_news.append({
                    'id': getattr(item, 'id', f"news_{hash(item.title)}"),
                    'title': getattr(item, 'title', ''),
                    'content': getattr(item, 'content', '')[:500],
                    'url': getattr(item, 'url', ''),
                    'source': getattr(item, 'source', 'unknown'),
                    'symbol': '',
                    'time': getattr(item, 'published_at', '')
                })
            
            logger.info(f"📰 获取到 {len(all_news)} 条新闻")
            return all_news
            
        except Exception as e:
            logger.error(f"获取新闻失败: {e}")
            return []


class LLMNewsAnalyzer:
    """LLM 新闻意图分析器"""
    
    # 极简 Prompt - 专注交易意图
    ANALYSIS_PROMPT = """分析以下新闻对港股/美股的交易影响。输出严格 JSON。

新闻标题: {title}
新闻内容: {content}

输出格式:
{{
  "tradable": true/false,
  "targets": [
    {{"code": "HK.00700", "action": "BUY/SELL", "confidence": 0.8, "reason": "一句话"}}
  ],
  "urgency": "high/medium/low"
}}

要求:
1. 即使新闻未直接提及股票，也要基于二阶推断列出受益/受损标的
2. confidence > 0.7 才返回 tradable=true
3. 最多列出 3 个目标"""
    
    def __init__(self):
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "")
        self.endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        self.model = "qwen-plus"
        self._cache = {}
    
    def analyze(self, title: str, content: str) -> Optional[Dict]:
        """分析新闻交易意图"""
        cache_key = f"{title[:50]}_{content[:100]}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        if not self.api_key:
            logger.warning("⚠️ DASHSCOPE_API_KEY 未配置，使用规则降级")
            return self._rule_based_analysis(title, content)
        
        try:
            import requests
            
            prompt = self.ANALYSIS_PROMPT.format(title=title, content=content[:300])
            
            resp = requests.post(
                self.endpoint,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是专业交易员，输出严格 JSON。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=15
            )
            
            if resp.status_code == 200:
                result = resp.json()
                content_str = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                # 解析 JSON
                start = content_str.find('{')
                end = content_str.rfind('}') + 1
                if start >= 0 and end > start:
                    data = json.loads(content_str[start:end])
                    self._cache[cache_key] = data
                    return data
            
            logger.warning(f"LLM 分析失败，降级到规则模式")
            return self._rule_based_analysis(title, content)
            
        except Exception as e:
            logger.error(f"LLM 调用异常: {e}")
            return self._rule_based_analysis(title, content)
    
    def _rule_based_analysis(self, title: str, content: str) -> Dict:
        """规则降级分析 (LLM 不可用时)"""
        text = f"{title} {content}".lower()
        targets = []
        
        # 简单映射表
        mappings = {
            'deepseek': [('HK.00981', 'BUY', 0.75, 'AI算力需求增加利好芯片制造')],
            '华虹': [('HK.01347', 'BUY', 0.80, '直接受益标的')],
            '中芯': [('HK.00981', 'BUY', 0.80, '直接受益标的')],
            '半导体': [('HK.00981', 'BUY', 0.70, '板块联动'), ('HK.01347', 'BUY', 0.70, '板块联动')],
            '财报': [('HK.00700', 'BUY', 0.65, '财报季关注')]
        }
        
        for keyword, items in mappings.items():
            if keyword in text:
                targets.extend(items)
        
        return {
            "tradable": len(targets) > 0,
            "targets": [{"code": t[0], "action": t[1], "confidence": t[2], "reason": t[3]} for t in targets],
            "urgency": "high" if any(t[2] >= 0.8 for t in targets) else "medium"
        }


class NewsEventListener:
    """主事件循环"""
    
    def __init__(self):
        self.injector = NewsSignalInjector()
        self.fetcher = FutuNewsFetcher()
        self.analyzer = LLMNewsAnalyzer()
        self._running = True
    
    @staticmethod
    def _is_trading_hours() -> bool:
        """
        检查当前是否为港股或美股交易时间 (2026-04-25 P1 修复)
        
        港股 (CST): 09:30-12:00, 13:00-16:00 (工作日)
        美股 (CST): 21:30-次日04:00 (工作日)
        
        非交易时间: 不采集/分析新闻，节省 API 配额
        """
        from datetime import datetime as _dt
        now = _dt.now()
        weekday = now.weekday()  # 0=周一, 6=周日
        
        # 周六全停
        if weekday == 5:
            return False
        
        hour = now.hour
        minute = now.minute
        current = hour * 60 + minute
        
        if weekday == 6:  # 周日
            # 周日只有 21:30 后是美股 (周一 04:00 CST)
            return current >= 1290  # 21:30 = 1290 分钟
        
        # 周一~周五
        hk_morning = 570 <= current < 720      # 09:30-12:00
        hk_afternoon = 780 <= current < 960    # 13:00-16:00
        us_evening = current >= 1290            # 21:30-23:59
        us_morning = current < 240              # 00:00-04:00
        
        return hk_morning or hk_afternoon or us_evening or us_morning
    
    async def run(self):
        """主循环"""
        logger.info("🚀 新闻事件监听器已启动")
        
        while self._running:
            try:
                # [P1 修复] 非交易时间完全休眠，不采集/不分析/不调用LLM
                if not self._is_trading_hours():
                    await asyncio.sleep(300)  # 5分钟检查一次即可
                    continue
                
                # 1. 获取新闻
                news_list = self.fetcher.get_latest_news()
                
                # 2. 处理每条新闻
                for news in news_list:
                    if news['id'] in self.injector._processed_ids:
                        continue
                    
                    # 3. LLM 分析
                    analysis = self.analyzer.analyze(
                        title=news['title'],
                        content=news['content']
                    )
                    
                    if analysis and analysis.get('tradable'):
                        # 4. 注入信号
                        for target in analysis.get('targets', []):
                            self.injector.inject_signal(
                                news_id=f"{news['id']}_{target['code']}",
                                code=target['code'],
                                action=target['action'],
                                confidence=target['confidence'],
                                priority=4 if target['confidence'] >= 0.85 else 3,
                                reason=f"[旁路] {news['title'][:40]} | {target.get('reason', '')}"
                            )
                    
                    self.injector._save_processed_id(news['id'])
                
                # 5. 等待下一轮
                await asyncio.sleep(30)
                
            except Exception as e:
                logger.error(f"监听循环异常: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    def stop(self):
        self._running = False
        logger.info("🛑 新闻监听器已停止")


# 主程序
if __name__ == "__main__":
    import signal
    
    listener = NewsEventListener()
    
    def handle_signal(sig, frame):
        listener.stop()
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    asyncio.run(listener.run())
