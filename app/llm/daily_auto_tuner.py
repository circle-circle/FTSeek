"""
日级 AutoTuner (L8, 2026-05-14)

收盘后 LLM 看 daily_review JSON 输出"明日参数微调":
- 每日提议 ≤ 3 项
- 必走 ConfigWriter PARAM_MAP 白名单 (16 项)
- 默认 trigger_mode=shadow_only
- 7 天 shadow 优于 baseline → 升 canary
- 30 天 canary 优 → 升 full

可控:
- 默认 disabled
- 输入: daily_review.json (含 exit_reason×pnl, 信号 funnel, owner_strategy 分布)
- 输出 → ConfigWriter 走 proposals 流程, 不直接改主线
"""
from __future__ import annotations
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import clamp_numeric
# 2026-05-17: 迁移到 model_call 统一入口 (盘后, 非盘中, 安全)

logger = logging.getLogger(__name__)

ALLOWED_TRIGGER_MODE = ["shadow_only", "canary", "full"]


@dataclass
class TuningProposal:
    proposal_id: str
    param_key: str           # ConfigWriter PARAM_MAP key (e.g. "MIN_CONFIDENCE")
    current_value: Any
    proposed_value: Any
    rationale: str
    expected_improvement: str
    confidence: float
    trigger_mode: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending / applied / rejected / shadow / canary / full


def _db_path() -> str:
    from app.core.paths import get_data_dir
    return str(get_data_dir() / "auto_tuner_proposals.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tuning_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT UNIQUE,
            param_key TEXT NOT NULL,
            current_value TEXT,
            proposed_value TEXT,
            rationale TEXT,
            expected_improvement TEXT,
            confidence REAL,
            trigger_mode TEXT,
            status TEXT DEFAULT 'pending',
            shadow_pnl REAL,
            canary_pnl REAL,
            applied_at TEXT,
            created_at REAL NOT NULL,
            ts_iso TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atp_param ON tuning_proposals(param_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atp_status ON tuning_proposals(status)")


def propose_from_daily_review(daily_review: Dict[str, Any]) -> List[TuningProposal]:
    """
    Args:
        daily_review: app/review/daily_review.py 生成的 report dict

    Returns: list of proposals (≤ 3)
    """
    if not is_enabled("daily_auto_tuner"):
        return []

    cfg = get_feature_config("daily_auto_tuner")

    # 取 ConfigWriter PARAM_MAP 当前值
    try:
        from app.core.config_writer import ConfigWriter
        param_map = ConfigWriter.PARAM_MAP
    except Exception as e:
        logger.warning(f"[DailyAutoTuner] PARAM_MAP 不可用: {e}")
        return []
    allowed_keys = list(param_map.keys())

    # Build prompt
    summary = daily_review.get("summary", {})
    exit_reason_pnl = daily_review.get("exit_reason_pnl", {})
    review_score = daily_review.get("review_score", {})

    system = (
        "You are FTSeek's daily auto-tuner. Given today's review, propose <= 3 parameter changes "
        "from the allowed PARAM_MAP. Output strict JSON only."
    )
    prompt = f"""Today's review summary:
- Total trades: {summary.get('total_trades', 0)}
- Win rate: {summary.get('win_rate', 0)*100:.1f}%
- Total PnL: ${summary.get('total_pnl', 0):.2f}
- Avg PnL: {summary.get('avg_pnl_pct', 0)*100:+.2f}%
- Review score: {review_score.get('overall_score', 0):.1f}/10

Exit reason PnL distribution: {json.dumps(exit_reason_pnl, default=str)[:600]}

Allowed PARAM_MAP keys (with min/max/max_delta):
{json.dumps({k: {**v, "yaml_path": v.get("yaml_path","")} for k, v in param_map.items()}, default=str)[:1500]}

Propose tuning:
{{
  "proposals": [
    {{
      "param_key": "<key from PARAM_MAP>",
      "proposed_value": <number>,
      "rationale": "<= 100 chars",
      "expected_improvement": "<= 80 chars (e.g. +2% win rate)",
      "confidence": 0.0-1.0,
      "trigger_mode": "shadow_only|canary|full"
    }}
  ]
}}

Rules:
- At most 3 proposals
- Each proposed_value must respect min/max/max_delta from PARAM_MAP
- Default trigger_mode=shadow_only unless evidence is overwhelming
- Skip proposal if today's data insufficient (< 5 trades)"""

    # 盘后调用 (非盘中), 走 deepseek-pro → qwen-plus fallback
    from app.llm.model_call import call_llm as _call_llm
    res = _call_llm(
        "daily_auto_tuner",
        prompt,
        system=system,
        temperature=0.1,
        max_tokens=800,
        schema_required={"proposals": list},
    )
    if not res or not res.success or not isinstance(res.response, dict):
        return []
    obj = res.response

    proposals: List[TuningProposal] = []
    for p in obj["proposals"][:3]:
        if not isinstance(p, dict):
            continue
        param_key = p.get("param_key")
        if param_key not in allowed_keys:
            logger.warning(f"[DailyAutoTuner] 拒绝白名单外的 param: {param_key}")
            continue
        pm = param_map[param_key]
        try:
            proposed_value = float(p.get("proposed_value"))
        except Exception:
            continue
        # 验证 min/max
        if not (pm["min"] <= proposed_value <= pm["max"]):
            logger.warning(
                f"[DailyAutoTuner] {param_key} 超界: {proposed_value} 不在 [{pm['min']},{pm['max']}]"
            )
            continue
        trigger_mode = p.get("trigger_mode", "shadow_only")
        if trigger_mode not in ALLOWED_TRIGGER_MODE:
            trigger_mode = "shadow_only"
        prop = TuningProposal(
            proposal_id=f"tune-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            param_key=param_key,
            current_value=None,  # 由 ConfigWriter 在应用时填实际当前值
            proposed_value=proposed_value,
            rationale=str(p.get("rationale", ""))[:200],
            expected_improvement=str(p.get("expected_improvement", ""))[:160],
            confidence=clamp_numeric(p.get("confidence", 0.5), 0.0, 1.0),
            trigger_mode=trigger_mode,
        )
        proposals.append(prop)
        _persist(prop)
        logger.info(
            f"[DailyAutoTuner] 提议 {prop.proposal_id} {prop.param_key}={prop.proposed_value} "
            f"mode={prop.trigger_mode} conf={prop.confidence:.2f} | {prop.rationale[:80]}"
        )

    return proposals


def _persist(p: TuningProposal):
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO tuning_proposals "
            "(proposal_id, param_key, current_value, proposed_value, rationale, "
            " expected_improvement, confidence, trigger_mode, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.proposal_id, p.param_key,
                str(p.current_value), str(p.proposed_value),
                p.rationale, p.expected_improvement,
                p.confidence, p.trigger_mode, p.status, p.created_at,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[DailyAutoTuner·persist] 失败: {e}")


def get_pending_proposals() -> List[Dict[str, Any]]:
    """供 ConfigWriter / canary loop 读"""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT proposal_id, param_key, proposed_value, trigger_mode, confidence "
            "FROM tuning_proposals WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "proposal_id": r[0],
                "param_key": r[1],
                "proposed_value": r[2],
                "trigger_mode": r[3],
                "confidence": r[4],
            }
            for r in rows
        ]
    except Exception:
        return []
