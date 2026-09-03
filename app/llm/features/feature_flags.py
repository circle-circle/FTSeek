"""
LLM Feature Flags (L0 2026-05-14)

每个 LLM 增强模块默认关闭, 通过 yaml 单点开启.

约定的 feature key 列表:
- news_classifier (L1)
- news_signal_gen (L9, 依赖 L1)
- news_multilang (L12, 依赖 L1)
- emergency_review (L2) — DEPRECATED
- thesis_inferer (L3)
- regime_classifier (L6)
- daily_auto_tuner (L8)
- replay_dialog (L10)
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 默认全部 disabled
_DEFAULT_FLAGS = {
    "news_classifier": False,
    "news_signal_gen": False,
    "news_multilang": False,
    "emergency_review": False,         # DEPRECATED 2026-05-19
    "thesis_inferer": False,
    "regime_classifier": False,
    "daily_auto_tuner": False,
    "replay_dialog": True,
    # 2026-05-19 新增 — 真正影响交易决策的 LLM features
    "pre_entry_gate": False,            # 默认 off, 等观察 audit raw 数据后启用
    "failure_mode_miner": False,        # 周日 cron 触发, 默认 off 等数据
    "adaptive_tuner": False,            # 每日 04:30 cron, 默认 off
    "thesis_recheck": False,            # 持仓期 thesis/invalidation LLM 复核 (轨道B), 默认 off — 只收紧不放松, shadow→sim 后启用
    "sltp_verify_shadow": True,         # L1: reprice 时记录 LLM vs 代码 SL/TP 分歧 (record-only, 零改动作), 默认 on 采集校准数据
    "exit_param_shadow": True,          # 2026-07-13: 盘中回放更紧 breakeven/drawdown 变体首触发 (record-only, 零改动作), 默认 on 采集 capture 对照
    "penetration_shadow": True,         # 2026-09-03 (DEB-20260903 议题二后续, 用户裁"A"): 保本线穿透瞬间判别特征采集 (record-only, 零改动作), 默认 on; 结局由夜间回填
}


def is_enabled(feature: str) -> bool:
    """检查 feature 是否启用"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        v = cfg.get_dotted(f"llm_features.{feature}.enabled", None)
        if v is None:
            return _DEFAULT_FLAGS.get(feature, False)
        return bool(v)
    except Exception:
        return _DEFAULT_FLAGS.get(feature, False)


def get_feature_config(feature: str) -> dict:
    """读 feature 全部配置"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        return cfg.get_dotted(f"llm_features.{feature}", {}) or {}
    except Exception:
        return {}


def get_per_feature_budget(feature: str) -> Optional[float]:
    """读 feature 单独预算 (USD/日). None 表示走全局预算."""
    cfg = get_feature_config(feature)
    return cfg.get("daily_max_usd")
