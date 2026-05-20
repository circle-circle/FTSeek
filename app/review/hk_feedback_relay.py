"""
港股反馈传递层 (HK Feedback Relay)

职责: 连接港股交易全链路的数据传递，确保盘前→午间→盘后→次日盘前的经验闭环。

数据流:
  EOD 复盘 → save_eod_feedback() → data/hk_feedback/eod_review.json
  午间刷新 → save_midday_update() → data/hk_feedback/midday_update.json
  次日盘前 → load_yesterday_feedback() → 加载 eod_review.json
  EOD 复盘 → load_midday_context() → 加载 midday_update.json

设计原则:
- 纯 overlay，不修改现有模块核心逻辑
- 文件协议 (JSON)，atomic write (temp + rename)
- TTL 过期 (默认 48h，防止加载过期数据)
- 零副作用: 文件不存在时返回 None，不报错
"""

import json
import logging
import os
import tempfile
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger("ftseek.review.hk_feedback_relay")

# 反馈数据目录
FEEDBACK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "hk_feedback"

# 文件名常量
EOD_REVIEW_FILE = "eod_review.json"
MIDDAY_UPDATE_FILE = "midday_update.json"

# TTL 配置 (小时) — 2026-05-10: EOD TTL 48h→72h (多模型评审共识: 防止周末/节假日过期)
EOD_TTL_HOURS = 72       # EOD 复盘数据保留 72 小时 (覆盖周末)
MIDDAY_TTL_HOURS = 12    # 午间更新保留 12 小时 (仅当日有效)

# Schema 版本 — 用于向后兼容检测
SCHEMA_VERSION = 1


def _ensure_feedback_dir():
    """确保反馈目录存在"""
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(filepath: Path, data: Dict, retries: int = 3, retry_delay: float = 0.5):
    """
    Atomic write: 先写临时文件，再 rename (防止半写文件)
    增加文件锁重试逻辑 (多模型评审共识: 并发安全)
    """
    _ensure_feedback_dir()

    # 先计算校验和 (不含 _meta)
    data_for_checksum = {k: v for k, v in data.items() if k != "_meta"}
    checksum = hashlib.md5(
        json.dumps(data_for_checksum, indent=2, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:8]

    # 添加 meta (含 schema 版本 + 校验和 + 写入者信息)
    data["_meta"] = {
        "schema_version": SCHEMA_VERSION,
        "checksum": checksum,
        "writer_pid": os.getpid(),
        "written_at": datetime.now().isoformat(),
    }

    for attempt in range(1, retries + 1):
        fd, tmp_path = tempfile.mkstemp(dir=str(FEEDBACK_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_path, str(filepath))
            return  # 成功
        except (OSError, PermissionError) as e:
            logger.warning(f"[HKFeedbackRelay] atomic write 失败 (尝试 {attempt}/{retries}): {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            if attempt < retries:
                import time
                time.sleep(retry_delay * attempt)  # 指数退避
            else:
                raise


def _safe_load(filepath: Path, ttl_hours: int) -> Optional[Dict]:
    """
    安全加载 JSON 文件，带 TTL 过期检查 + schema 验证 + 校验和验证
    (多模型评审共识: 数据完整性)
    """
    if not filepath.exists():
        return None

    try:
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
        if datetime.now() - mtime > timedelta(hours=ttl_hours):
            logger.debug(f"[HKFeedbackRelay] 文件过期: {filepath.name} (>{ttl_hours}h)")
            return None

        with open(filepath, 'r', encoding='utf-8') as f:
            raw = f.read()
            data = json.loads(raw)

        # Schema 版本检查
        meta = data.get("_meta", {})
        schema_ver = meta.get("schema_version", 0)
        if schema_ver > SCHEMA_VERSION:
            logger.warning(
                f"[HKFeedbackRelay] {filepath.name} schema v{schema_ver} > 当前 v{SCHEMA_VERSION}, "
                f"可能来自未来版本的数据"
            )

        # 校验和验证
        if "checksum" in meta and "_meta" in data:
            data_for_check = {k: v for k, v in data.items() if k != "_meta"}
            expected_checksum = meta["checksum"]
            actual_checksum = hashlib.md5(
                json.dumps(data_for_check, indent=2, ensure_ascii=False, default=str).encode()
            ).hexdigest()[:8]
            if actual_checksum != expected_checksum:
                logger.warning(
                    f"[HKFeedbackRelay] {filepath.name} 校验和不匹配 "
                    f"(预期 {expected_checksum}, 实际 {actual_checksum}), 数据可能损坏"
                )
                return None

        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[HKFeedbackRelay] 加载失败 {filepath.name}: {e}")
        return None


# =====================================================================
#  EOD 复盘反馈
# =====================================================================

def save_eod_feedback(
    trade_date: str,
    summary: Dict,
    next_day_preview: str = "",
    lessons: list = None,
    battle_plan_execution: Dict = None,
    trades: list = None,
    execution_quality: Dict = None,
):
    """
    EOD 复盘完成后调用 — 保存完整反馈到文件。

    Args:
        trade_date: 交易日 (YYYY-MM-DD)
        summary: EOD 复盘摘要 (盈亏、胜率等)
        next_day_preview: 次日预判文本
        lessons: 经验教训列表
        battle_plan_execution: 作战计划执行情况
        trades: 今日交易明细
        execution_quality: 执行质量数据 (滑点、成交率、延迟等)
    """
    data = {
        "trade_date": trade_date,
        "saved_at": datetime.now().isoformat(),
        "summary": summary or {},
        "next_day_preview": next_day_preview or "",
        "lessons": lessons or [],
        "battle_plan_execution": battle_plan_execution or {},
        "trades": trades or [],
        # 实战表现 — 次日盘前加载后用于校准计划
        "performance": {
            "total_trades": len(trades) if trades else 0,
            "win_count": sum(1 for t in (trades or []) if _calc_pnl(t) > 0) if trades else 0,
            "total_pnl": sum(_calc_pnl(t) for t in (trades or [])) if trades else 0,
        },
        # 执行质量 (多模型评审共识: 补充实盘成交/滑点数据)
        "execution_quality": execution_quality or {},
    }

    filepath = FEEDBACK_DIR / EOD_REVIEW_FILE
    _atomic_write(filepath, data)
    logger.info(f"[HKFeedbackRelay] EOD 反馈已保存: {trade_date} ({len(lessons or [])} 条教训)")
    return filepath


def load_yesterday_feedback() -> Optional[Dict]:
    """
    次日盘前调用 — 加载昨日的 EOD 反馈。
    包含: 次日预判 + 经验教训 + 实战表现 + 作战计划执行

    Returns:
        Dict 或 None (文件不存在/过期)
    """
    data = _safe_load(FEEDBACK_DIR / EOD_REVIEW_FILE, EOD_TTL_HOURS)
    if data:
        logger.info(
            f"[HKFeedbackRelay] 已加载昨日反馈: {data.get('trade_date', 'unknown')} "
            f"(预判: {'有' if data.get('next_day_preview') else '无'}, "
            f"教训: {len(data.get('lessons', []))} 条)"
        )
    return data


# =====================================================================
#  午间更新
# =====================================================================

def save_midday_update(
    new_targets: list = None,
    market_context: Dict = None,
    a_share_close: Dict = None,
    southbound_flow: Dict = None,
):
    """
    午间刷新完成后调用 — 保存午间增量信息。

    Args:
        new_targets: 午间新增标的列表
        market_context: 午间市场上下文
        a_share_close: A 股收盘数据
        southbound_flow: 南向资金流向
    """
    data = {
        "saved_at": datetime.now().isoformat(),
        "new_targets": new_targets or [],
        "market_context": market_context or {},
        "a_share_close": a_share_close or {},
        "southbound_flow": southbound_flow or {},
    }

    filepath = FEEDBACK_DIR / MIDDAY_UPDATE_FILE
    _atomic_write(filepath, data)
    logger.info(f"[HKFeedbackRelay] 午间更新已保存: {len(new_targets or [])} 只新标的")
    return filepath


def load_midday_context() -> Optional[Dict]:
    """
    EOD 复盘调用 — 加载当日午间更新。

    Returns:
        Dict 或 None (文件不存在/过期)
    """
    data = _safe_load(FEEDBACK_DIR / MIDDAY_UPDATE_FILE, MIDDAY_TTL_HOURS)
    if data:
        logger.info(
            f"[HKFeedbackRelay] 已加载午间上下文: {len(data.get('new_targets', []))} 只标的"
        )
    return data


# =====================================================================
#  辅助函数
# =====================================================================

def load_plan_history(plan_path: str = None) -> Optional[Dict]:
    """
    EOD 复盘调用 — 读取 daily_battle_plan.json 的全天版本历史和阶段摘要。

    替代方案: 不再需要分别读 eod_review.json + midday_update.json,
    一个调用即可获取"盘前辩论→午间规则→午间LLM→最终计划"全链路。

    Args:
        plan_path: daily_battle_plan.json 路径, 默认自动推导

    Returns:
        {
            "date": "2026-05-12",
            "version_history": [{"version": 1, "phase": "premarket", ...}, ...],
            "phase_summary": {"premarket": {"count": 5}, "midday_llm": {"count": 2}},
            "total_targets": 8,
            "restricted_count": 2,
            "debate_consensus": {"direction": "BUY", "conviction": 0.65},
        }
        或 None (文件不存在/过期)
    """
    if plan_path is None:
        plan_path = str(FEEDBACK_DIR.parent / "daily_battle_plan.json")

    filepath = Path(plan_path)
    if not filepath.exists():
        logger.debug(f"[HKFeedbackRelay] 作战计划文件不存在: {plan_path}")
        return None

    try:
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
        age = (datetime.now() - mtime).total_seconds() / 3600
        if age > 24:
            logger.debug(f"[HKFeedbackRelay] 作战计划文件过期: {age:.1f}h > 24h")
            return None

        with open(filepath, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        from app.pipeline.battle_plan_schema import BattlePlan
        plan = BattlePlan.from_json(raw)
        history = plan.get_version_history()
        phases = plan.get_phase_summary()

        result = {
            "date": plan.date,
            "version_history": [h.to_dict() for h in history],
            "phase_summary": phases,
            "total_targets": len(plan.targets),
            "restricted_count": len(plan.restricted),
            "debate_consensus": plan.debate_consensus.to_dict(),
            "stop_trading": plan.stop_trading,
            "market_regime": plan.market_regime,
        }

        logger.info(
            f"[HKFeedbackRelay] 已加载计划历史: {plan.date} "
            f"({len(history)} 个版本, {len(plan.targets)} 只标的)"
        )
        return result

    except Exception as e:
        logger.warning(f"[HKFeedbackRelay] 加载计划历史失败: {e}")
        return None


def _calc_pnl(trade: Dict) -> float:
    """计算单笔交易 PnL (兼容多种字段名)"""
    pnl = trade.get('pnl') or trade.get('realized_pnl') or trade.get('pnl_amount')
    if pnl is not None:
        try:
            return float(pnl)
        except (ValueError, TypeError):
            return 0.0

    # 从 entry_price / exit_price 计算
    try:
        entry = float(trade.get('entry_price', 0))
        exit_p = float(trade.get('exit_price', 0))
        qty = float(trade.get('quantity', trade.get('qty', 0)))
        action = trade.get('action', 'BUY').upper()
        if action in ('BUY', 'LONG'):
            return (exit_p - entry) * qty
        else:
            return (entry - exit_p) * qty
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def get_feedback_status() -> Dict:
    """
    获取反馈系统状态 (用于 health check / 诊断)

    Returns:
        {eod_review: {exists, age_hours, trade_date}, midday_update: {...}}
    """
    status = {}

    for name, filepath, ttl in [
        ("eod_review", FEEDBACK_DIR / EOD_REVIEW_FILE, EOD_TTL_HOURS),
        ("midday_update", FEEDBACK_DIR / MIDDAY_UPDATE_FILE, MIDDAY_TTL_HOURS),
    ]:
        if filepath.exists():
            mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds() / 3600
            try:
                with open(filepath) as f:
                    data = json.load(f)
                status[name] = {
                    "exists": True,
                    "age_hours": round(age, 1),
                    "expired": age > ttl,
                    "trade_date": data.get("trade_date", "N/A"),
                }
            except Exception:
                status[name] = {"exists": True, "age_hours": round(age, 1), "expired": age > ttl, "error": "parse_failed"}
        else:
            status[name] = {"exists": False}

    return status
