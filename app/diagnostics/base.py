"""
诊断模块基类 — FTSeek-US 诊断 Overlay 框架

设计原则 (Karpathy):
  - 不侵入交易管线，只读 DB
  - 所有诊断结果写入独立表 (diagnostics_results)
  - 纯函数优先，方便单测
  - 统一调度接口

接入方式:
  - scheduler_tasks.py 新增非交易时段定时任务
  - 或 cronjob 独立触发
"""

import logging
import sqlite3
import numpy as np
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class DiagnosticResult:
    """诊断结果容器 (不可变)"""

    def __init__(
        self,
        module_name: str,
        status: str,  # "success" / "error" / "skipped"
        data: Dict[str, Any],
        summary: str = "",
        timestamp: Optional[datetime] = None,
    ):
        self.module_name = module_name
        self.status = status
        self.data = data
        self.summary = summary
        self.timestamp = timestamp or datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "status": self.status,
            "data": self.data,
            "summary": self.summary,
            "timestamp": self.timestamp.isoformat(),
        }


class BaseDiagnostic(ABC):
    """
    诊断模块基类

    子类必须实现:
      - name: 模块名称
      - run(): 执行诊断，返回 DiagnosticResult

    可选实现:
      - is_applicable(): 是否适用当前市场环境 (默认 True)
      - schedule_hint(): 调度建议 (默认 "post_market")
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """模块名称"""
        ...

    @abstractmethod
    def run(self, **kwargs) -> DiagnosticResult:
        """执行诊断"""
        ...

    def is_applicable(self, **kwargs) -> bool:
        """是否适用当前环境"""
        return True

    @property
    def schedule_hint(self) -> str:
        """调度建议: pre_market / post_market / midday / on_demand"""
        return "post_market"

    def _get_db_path(self, config=None) -> str:
        """获取 DB 路径"""
        if config is None:
            try:
                from app.core.config_container import get_config
                config = get_config()
            except Exception:
                pass

        if config:
            return config.get_dotted("database.path", "data/ftseek_us.db")
        return "data/ftseek_us.db"

    def _save_result(self, result: DiagnosticResult, db_path: str = None):
        """持久化诊断结果到 DB"""
        if db_path is None:
            db_path = self._get_db_path()

        try:
            import json
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS diagnostic_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT,
                    summary TEXT,
                    timestamp TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)

            # 处理 numpy/bool 类型序列化
            def json_serializer(obj):
                if isinstance(obj, (np.bool_, np.integer)):
                    return int(obj)
                if isinstance(obj, np.floating):
                    return float(obj)
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                if isinstance(obj, bool):
                    return obj
                raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

            conn.execute(
                "INSERT INTO diagnostic_results (module_name, status, data, summary, timestamp) VALUES (?, ?, ?, ?, ?)",
                (result.module_name, result.status, json.dumps(result.data, default=json_serializer), result.summary, result.timestamp.isoformat()),
            )
            conn.commit()
            conn.close()
            logger.debug(f"[{self.name}] 诊断结果已保存到 {db_path}")
        except Exception as e:
            logger.error(f"[{self.name}] 保存诊断结果失败: {e}")

    def execute(self, **kwargs) -> DiagnosticResult:
        """
        统一执行入口 (模板方法模式)

        BEFORE 检查 → run() → 保存结果 → AFTER 验证
        """
        # ── BEFORE: 适用性检查 ──
        if not self.is_applicable(**kwargs):
            result = DiagnosticResult(
                module_name=self.name,
                status="skipped",
                data={},
                summary="不适用当前环境",
            )
            logger.info(f"[{self.name}] 跳过 (不适用)")
            return result

        # ── DURING: 执行诊断 ──
        try:
            logger.info(f"[{self.name}] 开始执行...")
            result = self.run(**kwargs)
        except Exception as e:
            logger.error(f"[{self.name}] 执行异常: {e}", exc_info=True)
            result = DiagnosticResult(
                module_name=self.name,
                status="error",
                data={"error": str(e)},
                summary=f"执行失败: {e}",
            )

        # ── AFTER: 持久化 ──
        db_path = kwargs.get("db_path")
        self._save_result(result, db_path)

        return result
