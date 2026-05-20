"""
Prompt A/B 测试框架 (Prompt AB Testing)

P2-2: 对比不同 prompt 版本的效果，自动选择最优版本。

核心功能:
1. 管理 prompt 版本 (prompt_versions.json)
2. 按流量比例分配 A/B 测试
3. 收集效果数据 (胜率、信号质量、归因准确率)
4. 自动选择最优版本

设计原则:
- 非侵入式: 通过 prompt_id 标识版本
- 统计显著: 至少 30 次调用才计算效果
- 安全回滚: 新版本效果差自动切回旧版本

作者: FTSeek Team
日期: 2026-05-21
"""

import json
import logging
import random
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.llm.ab_testing")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
VERSIONS_FILE = DATA_DIR / "prompt_versions.json"
RESULTS_FILE = DATA_DIR / "prompt_ab_results.jsonl"


class PromptVersion:
    """Prompt 版本定义"""
    def __init__(self, version_id: str, content: str, metadata: Dict = None):
        self.version_id = version_id
        self.content = content
        self.metadata = metadata or {}
        self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return {
            "version_id": self.version_id,
            "content": self.content[:200],  # 截断存储
            "content_hash": hash(self.content) & 0xFFFFFFFF,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class ABTestManager:
    """A/B 测试管理器"""

    # 最小样本量 (统计显著性要求)
    MIN_SAMPLE_SIZE = 30

    def __init__(self):
        self._versions: Dict[str, Dict[str, PromptVersion]] = {}  # {task_type: {version_id: PromptVersion}}
        self._traffic_split: Dict[str, Dict[str, float]] = {}  # {task_type: {version_id: ratio}}
        self._load_versions()

    def _load_versions(self):
        """加载 prompt 版本配置"""
        if VERSIONS_FILE.exists():
            try:
                with open(VERSIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for task_type, versions in data.items():
                    self._versions[task_type] = {}
                    for vid, vdata in versions.items():
                        self._versions[task_type][vid] = PromptVersion(
                            version_id=vid,
                            content=vdata.get("content", ""),
                            metadata=vdata.get("metadata", {}),
                        )
                        self._versions[task_type][vid].created_at = vdata.get("created_at", "")
                    self._traffic_split[task_type] = vdata.get("traffic_split", {})
            except Exception as e:
                logger.warning(f"[ABTesting] 加载版本配置失败: {e}")

    def register_version(self, task_type: str, version_id: str, content: str,
                         traffic_ratio: float = 0.0, metadata: Dict = None):
        """注册新的 prompt 版本"""
        if task_type not in self._versions:
            self._versions[task_type] = {}
            self._traffic_split[task_type] = {}

        version = PromptVersion(version_id=version_id, content=content, metadata=metadata)
        self._versions[task_type][version_id] = version
        self._traffic_split[task_type][version_id] = traffic_ratio

        self._save_versions()
        logger.info(f"[ABTesting] 注册版本: {task_type}/{version_id} ratio={traffic_ratio:.1%}")

    def get_version_for_call(self, task_type: str) -> Optional[str]:
        """
        根据流量分配选择版本

        Returns:
            version_id 或 None (使用默认版本)
        """
        if task_type not in self._versions or not self._versions[task_type]:
            return None

        versions = list(self._versions[task_type].keys())
        ratios = [self._traffic_split.get(task_type, {}).get(v, 0) for v in versions]

        # 如果没有配置流量分配，返回第一个版本
        if sum(ratios) == 0:
            return versions[0] if versions else None

        # 按概率选择
        rand = random.random()
        cumulative = 0
        for v, r in zip(versions, ratios):
            cumulative += r
            if rand <= cumulative:
                return v

        return versions[-1]

    def get_version_content(self, task_type: str, version_id: str) -> Optional[str]:
        """获取版本内容"""
        if task_type in self._versions and version_id in self._versions[task_type]:
            return self._versions[task_type][version_id].content
        return None

    def record_result(self, task_type: str, version_id: str, metrics: Dict):
        """记录 A/B 测试结果"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": int(time.time()),
                "datetime": datetime.now().isoformat(),
                "task_type": task_type,
                "version_id": version_id,
                **metrics,
            }
            with open(RESULTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[ABTesting] 记录结果失败: {e}")

    def get_version_stats(self, task_type: str) -> Dict[str, Any]:
        """获取各版本统计"""
        if not RESULTS_FILE.exists():
            return {}

        stats = defaultdict(lambda: {
            "calls": 0, "wins": 0, "errors": 0,
            "signal_quality_sum": 0, "format_ok": 0,
        })

        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("task_type") != task_type:
                            continue
                        vid = r.get("version_id", "unknown")
                        stats[vid]["calls"] += 1
                        if r.get("is_win"):
                            stats[vid]["wins"] += 1
                        if r.get("is_error"):
                            stats[vid]["errors"] += 1
                        if r.get("signal_quality"):
                            stats[vid]["signal_quality_sum"] += r["signal_quality"]
                        if r.get("format_ok"):
                            stats[vid]["format_ok"] += 1
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        result = {}
        for vid, s in stats.items():
            calls = s["calls"]
            result[vid] = {
                "calls": calls,
                "win_rate": round(s["wins"] / calls, 3) if calls > 0 else 0,
                "error_rate": round(s["errors"] / calls, 3) if calls > 0 else 0,
                "signal_quality_avg": round(s["signal_quality_sum"] / calls, 3) if calls > 0 else 0,
                "format_compliance": round(s["format_ok"] / calls, 3) if calls > 0 else 0,
                "is_significant": calls >= self.MIN_SAMPLE_SIZE,
            }

        return result

    def auto_select_best(self, task_type: str) -> Optional[str]:
        """
        自动选择最优版本 (基于胜率)

        Returns:
            最优 version_id 或 None
        """
        stats = self.get_version_stats(task_type)
        if not stats:
            return None

        # 过滤有统计显著性的版本
        significant = {vid: s for vid, s in stats.items() if s["is_significant"]}
        if not significant:
            return None

        # 按胜率排序
        best = max(significant.items(), key=lambda x: x[1]["win_rate"])
        best_vid = best[0]

        logger.info(
            f"[ABTesting] {task_type} 自动选择最优版本: {best_vid} "
            f"(win_rate={best[1]['win_rate']:.1%}, calls={best[1]['calls']})"
        )

        # 更新流量分配: 100% 给最优版本
        if task_type in self._traffic_split:
            for vid in self._traffic_split[task_type]:
                self._traffic_split[task_type][vid] = 1.0 if vid == best_vid else 0.0
            self._save_versions()

        return best_vid

    def _save_versions(self):
        """保存版本配置"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {}
            for task_type, versions in self._versions.items():
                data[task_type] = {}
                for vid, v in versions.items():
                    data[task_type][vid] = v.to_dict()
                data[task_type]["traffic_split"] = self._traffic_split.get(task_type, {})
            with open(VERSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[ABTesting] 保存版本配置失败: {e}")

    def get_all_stats(self) -> Dict[str, Dict]:
        """获取所有 task_type 的统计"""
        result = {}
        for task_type in self._versions:
            result[task_type] = self.get_version_stats(task_type)
        return result


# ── 全局单例 ─────────────────────────────────────────────────

_ab_manager: Optional[ABTestManager] = None


def get_ab_manager() -> ABTestManager:
    """获取全局 A/B 测试管理器"""
    global _ab_manager
    if _ab_manager is None:
        _ab_manager = ABTestManager()
    return _ab_manager


# ── CLI 入口 ─────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    manager = get_ab_manager()
    stats = manager.get_all_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
