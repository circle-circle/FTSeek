"""
FTSeek-US 问题注册表 — 跨天问题去重 + 状态追踪

机制:
- 问题按 category+title 去重 (而非 issue_id)
- 状态流转: new → active → resolved → archived
- 持续问题自动升级严重度 (同一问题连续出现 3 天 → P0)
- 已解决超过 30 天的问题 → archive
"""
import json
import logging
import os
import threading
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek_us.issue_registry")


@dataclass
class IssueState:
    """问题状态"""
    fingerprint: str       # category:title 的 hash (去重用)
    category: str
    title: str
    description: str
    status: str = "new"    # new/active/resolved/archived
    severity: str = "P2"   # 当前严重度
    first_seen: str = ""
    last_seen: str = ""
    seen_count: int = 0
    resolved_at: str = ""
    resolved_by: str = ""
    related_actions: List[str] = None
    
    def __post_init__(self):
        if not self.first_seen:
            self.first_seen = datetime.now().isoformat()
        if self.related_actions is None:
            self.related_actions = []
        self.last_seen = datetime.now().isoformat()


class IssueRegistry:
    """问题注册表 — 跨天去重 + 状态追踪"""
    
    REGISTRY_PATH = str(get_data_dir() / "issue_registry.json")
    
    def __init__(self, registry_path: str = None):
        self.registry_path = registry_path or self.REGISTRY_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        self._issues = self._load()
    
    def _load(self) -> Dict:
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError, UnicodeDecodeError):
                pass
        return {}
    
    def _save(self):
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            json.dump(self._issues, f, indent=2, ensure_ascii=False)
    
    @staticmethod
    def fingerprint(category: str, title: str) -> str:
        """生成问题指纹 (去重 key)"""
        raw = f"{category}:{title}"
        import hashlib

        return hashlib.md5(raw.encode()).hexdigest()[:16]
    
    def record_issue(
        self,
        category: str,
        title: str,
        description: str,
        severity: str,
        issue_id: str = "",
    ) -> IssueState:
        """记录问题 (自动去重 + 状态更新)"""
        fp = self.fingerprint(category, title)
        
        with self._lock:
            if fp in self._issues:
                state = self._issues[fp]
                # 更新: 增加出现次数
                state["seen_count"] += 1
                state["last_seen"] = datetime.now().isoformat()
                state["severity"] = severity
                
                # 持续问题自动升级
                if state["seen_count"] >= 3 and state["severity"] != "P0":
                    state["severity"] = "P0"
                    logger.warning(
                        f"[IssueRegistry] 问题持续出现，自动升级为 P0: {title}"
                    )
                
                if state["status"] == "new":
                    state["status"] = "active"
                elif state["status"] == "resolved":
                    state["status"] = "active"  # 复发
                    state["resolved_at"] = ""
                    state["resolved_by"] = ""
                
                self._issues[fp] = state
                self._save()
                
                return IssueState(**state)
            else:
                # 新问题
                state_data = {
                    "fingerprint": fp,
                    "category": category,
                    "title": title,
                    "description": description,
                    "status": "new",
                    "severity": severity,
                    "first_seen": datetime.now().isoformat(),
                    "last_seen": datetime.now().isoformat(),
                    "seen_count": 1,
                    "resolved_at": "",
                    "resolved_by": "",
                    "related_actions": [],
                }
                self._issues[fp] = state_data
                self._save()
                return IssueState(**state_data)
    
    def mark_resolved(self, fp: str, resolver: str = "auto_fix"):
        """标记问题已解决"""
        with self._lock:
            if fp in self._issues:
                self._issues[fp]["status"] = "resolved"
                self._issues[fp]["resolved_at"] = datetime.now().isoformat()
                self._issues[fp]["resolved_by"] = resolver
                self._save()
    
    def is_duplicate(self, category: str, title: str) -> Optional[IssueState]:
        """检查是否为重复问题"""
        fp = self.fingerprint(category, title)
        with self._lock:
            if fp in self._issues:
                return IssueState(**self._issues[fp])
        return None
    
    def get_active_issues(self) -> List[Dict]:
        """获取所有活跃问题"""
        with self._lock:
            return [
                v for v in self._issues.values()
                if v.get("status") in ("new", "active")
            ]
    
    def get_persistent_issues(self, min_days: int = 3) -> List[Dict]:
        """获取持续出现的问题"""
        cutoff = datetime.now() - timedelta(days=min_days)
        with self._lock:
            result = []
            for v in self._issues.values():
                if v.get("seen_count", 0) >= min_days:
                    first = datetime.fromisoformat(v.get("first_seen", ""))
                    if first >= cutoff:
                        result.append(v)
            return result
    
    def archive_resolved(self, max_age_days: int = 30) -> int:
        """归档已解决超过 N 天的问题"""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        archived = 0
        
        with self._lock:
            to_remove = []
            for fp, state in self._issues.items():
                if state.get("status") == "resolved":
                    resolved_at = state.get("resolved_at", "")
                    if resolved_at:
                        try:
                            resolved_dt = datetime.fromisoformat(resolved_at)
                            if resolved_dt <= cutoff:
                                to_remove.append(fp)
                                archived += 1
                        except (ValueError, TypeError):
                            pass
            
            for fp in to_remove:
                del self._issues[fp]
            
            if archived > 0:
                self._save()
        
        return archived
    
    def get_summary(self) -> Dict:
        """获取注册表摘要"""
        with self._lock:
            total = len(self._issues)
            by_status = {}
            by_severity = {}
            by_category = {}
            
            for v in self._issues.values():
                s = v.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1
                
                sev = v.get("severity", "unknown")
                by_severity[sev] = by_severity.get(sev, 0) + 1
                
                cat = v.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1
            
            return {
                "total": total,
                "by_status": by_status,
                "by_severity": by_severity,
                "by_category": by_category,
            }


# ============================================================================
#  模块级单例
# ============================================================================

_issue_registry: Optional[IssueRegistry] = None


def get_issue_registry() -> IssueRegistry:
    global _issue_registry
    if _issue_registry is None:
        _issue_registry = IssueRegistry()
    return _issue_registry
