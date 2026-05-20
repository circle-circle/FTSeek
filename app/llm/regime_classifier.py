"""
场景分类器 (L6, 2026-05-14)

每 30 分钟 LLM 给市场打"场景标签":
- morning_drive_chop: 开盘震荡, 噪声大
- trend_continuation: 单边趋势延续
- reversal: 反转中
- news_driven: 新闻主导
- liquidity_thin: 流动性差
- mean_reversion_friendly: 均值回归友好
- breakout_friendly: 突破友好

写 market_scenario.db, IntradayScorer 取场景调整因子权重 (上限 ±10%).

可控:
- 默认 disabled
- 30 分钟 1 次, 日累计 ~13 次, 成本 ~$0.20
- 标签必须白名单, 自由文本 drop
- 调权严格上限 (单次 ±10%)
"""
from __future__ import annotations
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled
from app.llm.features.json_schema_guard import clamp_numeric
# 2026-05-17: 迁移到 model_call 统一入口

logger = logging.getLogger(__name__)

ALLOWED_REGIME = [
    "morning_drive_chop",
    "trend_continuation",
    "reversal",
    "news_driven",
    "liquidity_thin",
    "mean_reversion_friendly",
    "breakout_friendly",
    "high_uncertainty",
]

ALLOWED_OUTLOOK = ["stable", "spike_likely", "compress_likely", "unknown"]

# 各场景对因子权重的微调建议 (上限 ±0.10)
REGIME_FACTOR_HINTS = {
    "morning_drive_chop":      {"momentum": -0.05, "trend": -0.05, "volume": +0.10},
    "trend_continuation":      {"trend": +0.10, "momentum": +0.05},
    "reversal":                {"momentum": -0.10, "lifecycle": +0.10},
    "news_driven":             {"momentum": -0.05, "orderflow": +0.10},
    "liquidity_thin":          {"volume": +0.10, "orderflow": -0.05},
    "mean_reversion_friendly": {"momentum": -0.05, "lifecycle": +0.05},
    "breakout_friendly":       {"trend": +0.10, "momentum": +0.05},
    "high_uncertainty":        {},  # 不调权
}


@dataclass
class RegimeClassification:
    regime: str
    key_drivers: List[str]
    best_strategies: List[str]
    avoid_strategies: List[str]
    vix_outlook: str
    horizon_hours: float
    classified_at: float = 0.0


def _db_path() -> str:
    from app.core.paths import get_data_dir
    return str(get_data_dir() / "market_scenario.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime TEXT,
            key_drivers TEXT,
            best_strategies TEXT,
            avoid_strategies TEXT,
            vix_outlook TEXT,
            horizon_hours REAL,
            classified_at REAL NOT NULL,
            spy_change_pct REAL,
            vix REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_time ON market_scenarios(classified_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_regime ON market_scenarios(regime)")


def classify(market_context: Dict[str, Any]) -> Optional[RegimeClassification]:
    """
    Args:
        market_context: {
            "spy_price", "spy_change_pct", "spy_vol_ratio",
            "vix", "vix_change_5m",
            "qqq_change_pct",
            "advance_decline_ratio",
            "session": "opening_drive|morning|midday|afternoon|closing_drive",
            "recent_news_summary": "..."
        }
    """
    if not is_enabled("regime_classifier"):
        return None

    # ⚠ 实盘铁律: 此函数可能被 30min 间隔的盘中调度触发.
    # 调用方必须在异步线程包装 (不阻塞主线程). 例:
    #   threading.Thread(target=lambda: classify(ctx), daemon=True).start()
    # 当前函数本身是同步, 单次 LLM 8-20s. 主调度路径绝不能直接 await.

    spy_chg = market_context.get("spy_change_pct", 0)
    vix = market_context.get("vix", 20)
    vix_chg = market_context.get("vix_change_5m", 0)
    session = market_context.get("session", "midday")
    news = (market_context.get("recent_news_summary") or "")[:300]
    ad_ratio = market_context.get("advance_decline_ratio", 1.0)

    system = (
        "You are FTSeek's market regime classifier. Tag the current market state "
        "with one of the predefined regimes. Output strict JSON only."
    )
    prompt = f"""Market snapshot:
SPY: {spy_chg:+.2%} change today
VIX: {vix:.1f} (5m change: {vix_chg:+.1%})
QQQ: {market_context.get('qqq_change_pct', 0):+.2%}
Session: {session}
Advance/Decline ratio: {ad_ratio:.2f}
Recent news: {news}

Classify regime:
{{
  "regime": "morning_drive_chop|trend_continuation|reversal|news_driven|liquidity_thin|mean_reversion_friendly|breakout_friendly|high_uncertainty",
  "key_drivers": ["..."],
  "best_strategies": ["momentum|breakout|mean_reversion|wait|hedge"],
  "avoid_strategies": ["..."],
  "vix_outlook": "stable|spike_likely|compress_likely|unknown",
  "horizon_hours": 0.5-4.0
}}

Rules:
- morning_drive_chop: opening 30min, low directional conviction
- trend_continuation: clear directional move with breadth
- reversal: against prior trend with confirmation
- news_driven: single news dominating
- liquidity_thin: midday lull or holiday/expiration"""

    # 2026-05-17: 走 model_call 统一入口 (yaml 路由: qwen-plus → deepseek-pro)
    # schema 强校验 — regime/vix_outlook 必须白名单, 否则返回 None
    from app.llm.model_call import call_llm as _call_llm
    res = _call_llm(
        "regime_classifier",
        prompt,
        system=system,
        temperature=0.0,
        max_tokens=300,
        schema_required={
            "regime": str,
            "key_drivers": list,
            "best_strategies": list,
            "avoid_strategies": list,
            "vix_outlook": str,
            "horizon_hours": float,
        },
        schema_enums={
            "regime": ALLOWED_REGIME,
            "vix_outlook": ALLOWED_OUTLOOK,
        },
    )
    if not res or not res.success or not isinstance(res.response, dict):
        return None
    obj = res.response

    cls = RegimeClassification(
        regime=obj["regime"],
        key_drivers=[str(d)[:50] for d in obj["key_drivers"][:5]],
        best_strategies=[str(s)[:30] for s in obj["best_strategies"][:5]],
        avoid_strategies=[str(s)[:30] for s in obj["avoid_strategies"][:5]],
        vix_outlook=obj["vix_outlook"],
        horizon_hours=clamp_numeric(obj["horizon_hours"], 0.25, 6.0),
        classified_at=time.time(),
    )

    _persist(cls, spy_chg, vix)
    logger.info(
        f"[RegimeClassifier] regime={cls.regime} drivers={cls.key_drivers[:2]} "
        f"vix_outlook={cls.vix_outlook} horizon={cls.horizon_hours}h"
    )
    return cls


def get_factor_weight_adjustments(regime: str) -> Dict[str, float]:
    """获取 regime 对应的因子权重调整 (上限 ±0.10)"""
    raw = REGIME_FACTOR_HINTS.get(regime, {})
    return {k: max(-0.10, min(0.10, v)) for k, v in raw.items()}


def get_latest_regime(within_minutes: int = 60) -> Optional[str]:
    """读最近一次 regime, 超过 within_minutes 视为过期返回 None"""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT regime, classified_at FROM market_scenarios ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        regime, ts = row
        if time.time() - ts > within_minutes * 60:
            return None
        return regime
    except Exception:
        return None


def _persist(cls: RegimeClassification, spy_chg: float, vix: float):
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO market_scenarios "
            "(regime, key_drivers, best_strategies, avoid_strategies, vix_outlook, "
            " horizon_hours, classified_at, spy_change_pct, vix) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cls.regime, ",".join(cls.key_drivers),
                ",".join(cls.best_strategies), ",".join(cls.avoid_strategies),
                cls.vix_outlook, cls.horizon_hours, cls.classified_at,
                spy_chg, vix,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[RegimeClassifier·persist] 失败: {e}")
