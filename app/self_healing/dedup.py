#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 自愈防重引擎 (Self-Healing Dedup & Anti-Repeat) v1.0

解决多会话并发执行时的重复问题:
- 全局去重锁 (文件锁)
- 问题实例 Hash 去重 (相同问题不重复分析)
- 分类冷却机制 (同类问题在一定时间内不再处理)
- 跨会话状态共享 (SQLite 持久化)

设计原则:
- 相同问题实例 30min 内只处理一次
- 同类问题 5min 内只处理一次  
- 成功修复的问题 24h 内不再触发
- 所有状态跨会话持久化
"""

import os
import sys
import re
import time
import json
import fcntl
import hashlib
import sqlite3
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.self_healing.dedup")


# ============================================================
# 防重状态存储 (SQLite)
# ============================================================

class DedupStore:
    """SQLite 持久化的防重状态存储"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = "/opt/ftseek/FTSeek/data/self_healing_dedup.db"
        
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._conn = None
        self._init_db()
    
    def _get_conn(self) -> sqlite3.Connection:
        # B3-13：自愈模块单例长连接，不适合改造为连接池（worker 线程持续持有）。
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn
    
    def _init_db(self):
        conn = self._get_conn()
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS issue_instances (
                issue_hash TEXT PRIMARY KEY,
                issue_name TEXT NOT NULL,
                issue_detail TEXT,
                category TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_processed TEXT,
                process_count INTEGER DEFAULT 0,
                last_fix_success INTEGER DEFAULT 0,
                last_escalated INTEGER DEFAULT 0,
                suppressed_until TEXT  -- 下次可处理时间
            );
            
            CREATE TABLE IF NOT EXISTS category_cooldowns (
                category TEXT PRIMARY KEY,
                cooldown_until TEXT NOT NULL,
                reason TEXT
            );
            
            CREATE TABLE IF NOT EXISTS session_locks (
                lock_name TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_issue_suppressed ON issue_instances(suppressed_until);
            CREATE INDEX IF NOT EXISTS idx_category_cooldown ON category_cooldowns(cooldown_until);
        ''')
        conn.commit()
    
    def should_process_issue(
        self,
        issue_hash: str,
        issue_name: str,
        category: str,
        issue_detail: str = "",
    ) -> Tuple[bool, str]:
        """判断一个问题实例是否应该被处理
        
        Returns:
            (should_process, reason)
        """
        conn = self._get_conn()
        now = datetime.now()
        now_iso = now.isoformat()
        
        cursor = conn.execute(
            "SELECT last_processed, process_count, last_fix_success, suppressed_until "
            "FROM issue_instances WHERE issue_hash = ?",
            (issue_hash,)
        )
        row = cursor.fetchone()
        
        if row:
            last_processed_str, process_count, last_fix_success, suppressed_until = row
            
            # 检查抑制时间
            if suppressed_until and suppressed_until > now_iso:
                return False, f"抑制中 (直到 {suppressed_until})"
            
            # 30 分钟内相同实例不重复处理
            if last_processed_str:
                last_processed = datetime.fromisoformat(last_processed_str)
                if (now - last_processed).total_seconds() < 1800:  # 30min
                    return False, f"30min 内已处理过 ({process_count} 次)"
            
            # 如果上次修复成功，24h 内不再处理
            if last_fix_success and process_count > 0:
                last_processed = datetime.fromisoformat(last_processed_str)
                if (now - last_processed).total_seconds() < 86400:  # 24h
                    return False, "上次修复成功，24h 内不再触发"
            
            # 更新记录
            conn.execute(
                "UPDATE issue_instances SET last_seen = ?, process_count = process_count + 1, last_processed = ? "
                "WHERE issue_hash = ?",
                (now_iso, now_iso, issue_hash)
            )
        else:
            # 首次出现
            conn.execute(
                "INSERT INTO issue_instances (issue_hash, issue_name, issue_detail, category, first_seen, last_seen, last_processed, process_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (issue_hash, issue_name, issue_detail, category, now_iso, now_iso, now_iso)
            )
        
        conn.commit()
        return True, "可以处理"
    
    def record_fix_result(self, issue_hash: str, success: bool, escalated: bool = False):
        """记录修复结果"""
        conn = self._get_conn()
        now_iso = datetime.now().isoformat()
        
        suppressed_until = None
        if success:
            # 修复成功，24h 抑制
            suppressed_until = (datetime.now() + timedelta(hours=24)).isoformat()
        elif escalated:
            # 升级人工，6h 抑制
            suppressed_until = (datetime.now() + timedelta(hours=6)).isoformat()
        else:
            # 修复失败，2h 后重试
            suppressed_until = (datetime.now() + timedelta(hours=2)).isoformat()
        
        conn.execute(
            "UPDATE issue_instances SET last_fix_success = ?, last_escalated = ?, suppressed_until = ? "
            "WHERE issue_hash = ?",
            (1 if success else 0, 1 if escalated else 0, suppressed_until, issue_hash)
        )
        conn.commit()
    
    def should_process_category(self, category: str) -> Tuple[bool, str]:
        """判断某分类是否在冷却中"""
        conn = self._get_conn()
        now = datetime.now()
        now_iso = now.isoformat()
        
        cursor = conn.execute(
            "SELECT cooldown_until FROM category_cooldowns WHERE category = ?",
            (category,)
        )
        row = cursor.fetchone()
        
        if row:
            cooldown_until = row[0]
            if cooldown_until > now_iso:
                return False, f"{category} 冷却中 (直到 {cooldown_until})"
            else:
                # 冷却已过，删除记录
                conn.execute("DELETE FROM category_cooldowns WHERE category = ?", (category,))
                conn.commit()
        
        return True, "可以处理"
    
    def set_category_cooldown(self, category: str, minutes: int = 5, reason: str = ""):
        """设置分类冷却"""
        conn = self._get_conn()
        cooldown_until = (datetime.now() + timedelta(minutes=minutes)).isoformat()
        
        conn.execute(
            "INSERT OR REPLACE INTO category_cooldowns (category, cooldown_until, reason) "
            "VALUES (?, ?, ?)",
            (category, cooldown_until, reason)
        )
        conn.commit()
    
    def acquire_session_lock(self, lock_name: str, session_id: str, timeout_minutes: int = 10) -> bool:
        """获取会话锁"""
        conn = self._get_conn()
        now = datetime.now()
        now_iso = now.isoformat()
        expires_at = (now + timedelta(minutes=timeout_minutes)).isoformat()
        
        # 检查锁是否存在且未过期
        cursor = conn.execute(
            "SELECT session_id, expires_at FROM session_locks WHERE lock_name = ?",
            (lock_name,)
        )
        row = cursor.fetchone()
        
        if row:
            existing_session, expires = row
            if expires > now_iso and existing_session != session_id:
                return False  # 锁被其他会话持有
            
            # 锁已过期或属于当前会话，可以获取
            conn.execute(
                "UPDATE session_locks SET session_id = ?, acquired_at = ?, expires_at = ? "
                "WHERE lock_name = ?",
                (session_id, now_iso, expires_at, lock_name)
            )
        else:
            conn.execute(
                "INSERT INTO session_locks (lock_name, session_id, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (lock_name, session_id, now_iso, expires_at)
            )
        
        conn.commit()
        return True
    
    def release_session_lock(self, lock_name: str, session_id: str):
        """释放会话锁"""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM session_locks WHERE lock_name = ? AND session_id = ?",
            (lock_name, session_id)
        )
        conn.commit()
    
    def get_stats(self) -> Dict:
        """获取防重统计"""
        conn = self._get_conn()
        
        cursor = conn.execute("SELECT COUNT(*) FROM issue_instances")
        total_issues = cursor.fetchone()[0]
        
        cursor = conn.execute("SELECT COUNT(*) FROM issue_instances WHERE suppressed_until > datetime('now')")
        suppressed = cursor.fetchone()[0]
        
        cursor = conn.execute("SELECT COUNT(*) FROM category_cooldowns WHERE cooldown_until > datetime('now')")
        cooldowns = cursor.fetchone()[0]
        
        cursor = conn.execute("SELECT issue_name, process_count FROM issue_instances ORDER BY process_count DESC LIMIT 5")
        top_issues = [{"name": r[0], "count": r[1]} for r in cursor.fetchall()]
        
        return {
            "total_tracked_issues": total_issues,
            "currently_suppressed": suppressed,
            "active_cooldowns": cooldowns,
            "top_issues": top_issues,
        }
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ============================================================
# Issue Hash 生成器
# ============================================================

class IssueHasher:
    """为问题实例生成唯一 Hash"""
    
    # 忽略的变量部分 (避免同一问题因变量不同而产生不同 hash)
    VARIABLE_PATTERNS = [
        (r'\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.,]?\d*', ''),  # 时间戳
        (r'PID\s*=?\s*\d+', ''),  # PID
        (r'\[\d+\]', ''),  # [数字]
        (r'0x[0-9a-fA-F]+', ''),  # 内存地址
        (r'\d+ms', 'Xms'),  # 耗时统一替换
        (r'[\d.]+%', 'X%'),  # 百分比统一替换
        (r'HK\.\d+', 'HK.X'),  # 股票代码统一替换
        (r'US\.\w+', 'US.X'),  # 美股代码统一替换
        (r'价格\s*[\d.]+', '价格 X'),  # 价格数值统一替换
        (r'高点\s*[\d.]+', '高点 X'),  # 高点数值统一替换
        (r'距离\s*[\d.]+', '距离 X'),  # 距离数值统一替换
    ]
    
    @classmethod
    def normalize(cls, text: str) -> str:
        """归一化文本，去除变量部分"""
        for pattern, replacement in cls.VARIABLE_PATTERNS:
            text = re.sub(pattern, replacement, text)
        return text.strip()
    
    @classmethod
    def hash_issue(cls, issue_text: str, check_name: str) -> str:
        """生成问题实例的 Hash"""
        # 使用归一化后的文本
        normalized = cls.normalize(issue_text)
        combined = f"{check_name}:{normalized}"
        return hashlib.md5(combined.encode()).hexdigest()[:12]



# ============================================================
# 防重包装器 (Decorator for handle_issue)
# ============================================================

class DedupWrapper:
    """为 SelfHealingEngine.handle_issue 添加防重功能"""
    
    def __init__(self, store: DedupStore):
        self.store = store
        self.hasher = IssueHasher
    
    def wrap_handle_issue(self, engine, issue_text: str, check_name: str, severity: str = "warning"):
        """包装 handle_issue 方法，添加防重逻辑"""
        issue_hash = self.hasher.hash_issue(issue_text, check_name)
        
        # 1. 检查实例级去重
        should_process, reason = self.store.should_process_issue(
            issue_hash, check_name, "unknown", issue_text
        )
        
        if not should_process:
            logger.debug(f"[防重] 跳过 {check_name}: {reason}")
            return None
        
        # 2. 检查分类级冷却
        # 从 issue_text 推断分类 (简单匹配)
        category = self._infer_category(issue_text, check_name)
        cat_ok, cat_reason = self.store.should_process_category(category)
        
        if not cat_ok:
            logger.debug(f"[防重/分类冷却] 跳过 {check_name}: {cat_reason}")
            return None
        
        # 3. 调用原始 handle_issue
        try:
            # 获取原始方法
            original_method = engine.__class__.handle_issue
            
            # 临时修改 handler 的 category
            for h in engine.handlers:
                if h.matches(issue_text, check_name):
                    h.category = category  # 更新分类
                    break
            
            report = original_method(engine, issue_text, check_name, severity)
            
            # 4. 记录修复结果
            if report:
                self.store.record_fix_result(
                    issue_hash,
                    success=report.fix_success and report.verified,
                    escalated=report.escalated
                )
                
                # 设置分类冷却
                if report.fix_attempted:
                    cooldown_minutes = 5 if report.fix_success else 15
                    self.store.set_category_cooldown(
                        category,
                        minutes=cooldown_minutes,
                        reason=f"处理了 {check_name}"
                    )
            
            return report
            
        except Exception as e:
            logger.error(f"[防重] 处理异常: {e}")
            return None
    
    def _infer_category(self, issue_text: str, check_name: str) -> str:
        """从问题文本推断分类"""
        text_lower = (issue_text + " " + check_name).lower()
        
        if any(k in text_lower for k in ["碎股", "lot", "整手"]):
            return "config"
        if any(k in text_lower for k in ["熔断", "circuit"]):
            return "risk"
        if any(k in text_lower for k in ["adx", "指标"]):
            return "code"
        if any(k in text_lower for k in ["error", "exception", "traceback", "not defined"]):
            return "code"
        if any(k in text_lower for k in ["追高", "价格检查", "entry"]):
            return "risk"
        if any(k in text_lower for k in ["性能", "perf", "ms", "阈值"]):
            return "performance"
        if any(k in text_lower for k in ["新闻", "news", "rsshub", "flash"]):
            return "data"
        if any(k in text_lower for k in ["配置", "config", "硬编码", "drift"]):
            return "config"
        
        return "unknown"


# ============================================================
# 集成到 SelfHealingEngine
# ============================================================

def create_engine_with_dedup(base_dir: str = "/opt/ftseek/FTSeek"):
    """创建带防重功能的自愈引擎"""
    from app.self_healing.engine import create_engine
    
    engine = create_engine(base_dir)
    
    # 添加防重存储
    engine.dedup_store = DedupStore(
        str(Path(base_dir) / "data" / "self_healing_dedup.db")
    )
    
    # 包装 handle_issue 方法
    wrapper = DedupWrapper(engine.dedup_store)
    original_handle_issue = engine.handle_issue
    
    def dedup_handle_issue(issue_text: str, check_name: str, severity: str = "warning"):
        return wrapper.wrap_handle_issue(engine, issue_text, check_name, severity)
    
    engine.handle_issue = dedup_handle_issue
    engine.dedup_wrapper = wrapper
    
    logger.info("自愈引擎已启用防重功能")
    
    return engine


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    from app.self_healing.engine import create_engine
    
    engine = create_engine()
    store = DedupStore()
    wrapper = DedupWrapper(store)
    
    # 测试 Hash 生成
    print("=== Hash 测试 ===")
    h1 = IssueHasher.hash_issue("价格检查: 价格 8.62 接近日内高点 8.63 (距离 0.12% < 2.0%)", "追高买入")
    h2 = IssueHasher.hash_issue("价格检查: 价格 59.70 接近日内高点 60.25 (距离 0.91% < 2.0%)", "追高买入")
    print(f"Hash1: {h1}")
    print(f"Hash2: {h2}")
    print(f"相同: {h1 == h2}")  # 应该为 True，因为归一化后相同
    
    # 测试防重
    print("\n=== 防重测试 ===")
    for i in range(3):
        ok, reason = store.should_process_issue(h1, "追高买入", "risk", "价格检查")
        print(f"第{i+1}次: {ok} - {reason}")
        if ok:
            store.record_fix_result(h1, success=(i == 0))
    
    # 测试统计
    print("\n=== 防重统计 ===")
    stats = store.get_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
