"""风险闸域 (阶段 4 迁自 app/playbook/pipeline.py)。

迁移纪律: fail-open/fail-close 语义、研究库 pin、滞回阈值与日志口径不变。
pipeline 转发器显式注入 baseline 路径和连亏回调, 保 monkeypatch 缝。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DAILY_LOSS_BASELINE_DEFAULT = (
    Path(__file__).resolve().parents[3] / "data" / "daily_loss_baseline.json"
)


def _live_arm_currently_armed() -> bool:
    """LIVE 臂当前是否处于武装态 (配置 enabled ∧ ALLOW_REAL_TRADING ∧ waiver 活跃)。

    探测失败 → False (不额外加 REAL 面 = 维持现状行为; REAL 臂自身每指令还有
    live_execution_gate 把守, 此处只是日亏熔断的覆盖面)。
    """
    try:
        import os
        from app.core.config_container import get_config
        from app.playbook.live_arm_gate import read_live_probe_waiver
        arm = get_config().get_dotted("playbook.live_manual_arm", {}) or {}
        if not bool(arm.get("enabled", False)):
            return False
        if os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() != "true":
            return False
        return bool(read_live_probe_waiver().get("active"))
    except Exception:
        return False


def _daily_loss_circuit_one_arm(
    today: str,
    get_capital_safety,
    baseline_path: Path,
    arm_label: str,
) -> Optional[Dict[str, Any]]:
    """单臂日亏熔断评估 (滞回 latch 落 baseline_path)。None = 本臂 fail-open 跳过。"""
    from app.utils.timezone import now_et

    try:
        capital_safety = get_capital_safety()
        equity = (
            float(capital_safety._state.current_equity or 0)
            if capital_safety._state else 0.0
        )
    except Exception as error:
        logger.warning(
            f"[playbook] 日亏熔断({arm_label}): 净值读取失败, 本轮跳过 (fail-open): {error}"
        )
        return None
    if equity <= 0:
        return None

    try:
        base = (
            json.loads(baseline_path.read_text())
            if baseline_path.exists() else {}
        )
        if not isinstance(base, dict):
            base = {}
    except Exception:
        base = {}

    if base.get("date") != today:
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps({
                "date": today,
                "equity": equity,
                "ts": now_et().isoformat(),
                "latched": False,
            }))
        except Exception as error:
            logger.warning(f"[playbook] 日亏熔断({arm_label}): 基线落盘失败: {error}")
        return None

    baseline_equity = float(base.get("equity") or 0)
    if baseline_equity <= 0:
        return None

    try:
        from app.core.config_container import get_config
        threshold = float(get_config().get_dotted(
            "risk.circuit_breaker.strategy.daily_loss_threshold", -0.03
        ))
        release_threshold = float(get_config().get_dotted(
            "risk.circuit_breaker.strategy.daily_loss_release_threshold", -0.02
        ))
    except Exception:
        threshold = -0.03
        release_threshold = -0.02
    if release_threshold <= threshold:
        release_threshold = threshold

    loss_pct = equity / baseline_equity - 1
    epsilon = 1e-9
    breached = loss_pct <= threshold + epsilon
    was_latched = bool(base.get("latched"))
    if breached:
        latched = True
    elif was_latched and loss_pct < release_threshold - epsilon:
        latched = True
    else:
        latched = False

    if latched != was_latched:
        if latched:
            logger.critical(
                f"🚨 [playbook] 日亏熔断触发({arm_label}): 净值 {loss_pct * 100:.2f}% "
                f"≤ {threshold * 100:.2f}% "
                f"(基线 ${baseline_equity:,.0f} → 现值 ${equity:,.0f}) "
                f"| 停新开, 平仓照常; "
                f"解除需回升过 {release_threshold * 100:.2f}% (滞回带)"
            )
        else:
            logger.warning(
                f"[playbook] 日亏熔断解除({arm_label}): 净值回升至 {loss_pct * 100:.2f}% ≥ "
                f"{release_threshold * 100:.2f}% 放行线 → 恢复新开仓"
            )
        try:
            baseline_path.write_text(json.dumps({
                "date": base.get("date"),
                "equity": baseline_equity,
                "ts": base.get("ts"),
                "latched": latched,
                "flip_equity": round(equity, 2),
                "flip_pct": round(loss_pct * 100, 2),
                "flip_at": now_et().isoformat(),
            }))
        except Exception as error:
            logger.warning(
                f"[playbook] 日亏熔断({arm_label}): latch 落盘失败 (跨重启会丢失触发态): {error}"
            )

    return {
        "triggered": latched,
        "raw_breach": breached,
        "equity": round(equity, 2),
        "baseline": round(baseline_equity, 2),
        "loss_pct": round(loss_pct * 100, 2),
        "threshold_pct": round(threshold * 100, 2),
        "release_threshold_pct": round(release_threshold * 100, 2),
    }


def _check_daily_loss_circuit(
    today: str,
    *,
    baseline_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """账户级日亏熔断, 带滞回 latch。

    2026-10-15 双臂化 (审计 P2-2): 原实现恒用 `get_capital_safety_system()` 默认单例
    (=AUTO_TRADE_ACCOUNT 臂, 现为 SIM), 不随执行臂切换 — 与历史「双路径查不同账户」
    熔断事故同族。现: 默认臂恒评; LIVE 臂武装时 (配置 enabled ∧ ALLOW_REAL_TRADING ∧
    waiver 活跃) 加评 REAL 臂 (独立 latch 文件 `<stem>_real.json`), 任一臂触发即熔断。
    """
    baseline_path = Path(baseline_path or _DAILY_LOSS_BASELINE_DEFAULT)

    from app.risk.capital_safety import get_capital_safety_system
    default_verdict = _daily_loss_circuit_one_arm(
        today, get_capital_safety_system, baseline_path, "默认臂")

    arms: Dict[str, Any] = {"default": default_verdict}
    real_verdict = None
    if _live_arm_currently_armed():
        from app.risk.capital_safety import get_capital_safety_system_for_arm
        real_baseline = baseline_path.with_name(baseline_path.stem + "_real.json")
        real_verdict = _daily_loss_circuit_one_arm(
            today, lambda: get_capital_safety_system_for_arm("real"),
            real_baseline, "REAL臂")
        arms["real"] = real_verdict

    triggered = any(v and v.get("triggered") for v in arms.values())
    if default_verdict is None and real_verdict is None:
        return None
    base = dict(default_verdict or real_verdict or {})
    base["triggered"] = triggered
    base["arms"] = arms
    return base


def _auto_execute_preflight(
    today: str,
    *,
    check_daily_loss_circuit: Callable[[str], Optional[Dict[str, Any]]],
    logger,
) -> Optional[Dict[str, Any]]:
    """auto_execute 前置 kill/regime/risk/日亏闸; 返回跳口结果或 None 放行。"""
    try:
        from app.us_pipeline.emergency_interceptor import check_kill_switch
        if check_kill_switch():
            logger.warning("🚨 [playbook] Kill Switch 已激活, 跳过 auto_execute")
            return {"date": today, "executed": 0, "skipped": 0, "reason": "kill_switch"}
    except Exception as error:
        logger.warning(f"🚨 [playbook] Kill Switch 检查异常, fail-close 跳过本轮: {error}")
        return {
            "date": today,
            "executed": 0,
            "skipped": 0,
            "reason": "kill_switch_check_error",
        }

    try:
        from app.playbook.market_regime import MarketRegime, MarketRegimeGate
        from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
        regime_ctx = MarketRegimeGate().assess()
        if regime_ctx.regime == MarketRegime.NO_TRADE:
            logger.warning(
                f"[playbook] 当前 regime=NO_TRADE (VIX={regime_ctx.vix:.2f}), 跳过 auto_execute"
            )
            return {
                "date": today,
                "executed": 0,
                "skipped": 0,
                "reason": "regime_no_trade",
            }
        risk_state = RiskStateMachine().get_state()
        if risk_state in (RiskState.RISK_OFF, RiskState.HALT):
            logger.warning(
                f"[playbook] 当前 risk_state={risk_state}, 跳过 auto_execute"
            )
            return {
                "date": today,
                "executed": 0,
                "skipped": 0,
                "reason": f"risk_state_{risk_state}",
            }
    except Exception as error:
        try:
            from app.core.config_container import get_config
            fail_mode = get_config().get_dotted(
                "risk.preflight_fail_mode", "open"
            )
        except Exception:
            fail_mode = "open"
        if fail_mode == "closed":
            logger.error(f"[playbook] 风险状态检查异常 (fail-close 跳过本轮): {error}")
            return {
                "date": today,
                "executed": 0,
                "skipped": 0,
                "reason": "preflight_fail_close",
            }
        logger.warning(f"[playbook] 风险状态检查异常 (放行): {error}")

    try:
        daily_loss = check_daily_loss_circuit(today)
    except Exception as error:
        logger.warning(f"[playbook] 日亏熔断检查异常 (fail-open): {error}")
        daily_loss = None
    if daily_loss and daily_loss.get("triggered"):
        logger.warning(f"[playbook] 日亏熔断触发, 本轮停新开: {daily_loss}")
        return {
            "date": today,
            "executed": 0,
            "skipped": 0,
            "reason": "daily_loss_circuit",
            "detail": daily_loss,
        }
    return None


def _has_consecutive_losses(n: int = 5) -> bool:
    """最近是否连续 n 笔亏损; 异常 fail-close。"""
    try:
        from app.playbook.db import PlaybookDB
        database = PlaybookDB(db_name="ftseek_research")
        instructions = database.get_recent_instructions(limit=200)
        consecutive = 0
        for instruction in instructions:
            outcome = instruction.get("outcome")
            if outcome == "loss":
                consecutive += 1
                if consecutive >= n:
                    return True
            elif outcome == "win":
                consecutive = 0
        return False
    except Exception as error:
        logger.warning(f"[playbook] 连续亏损检查失败 (fail-close): {error}")
        return True


def _check_graduation_gate(
    consecutive_losses: Optional[Callable[[int], bool]] = None,
) -> Dict[str, Any]:
    """实盘毕业门禁; 证据恒来自 ftseek_research。"""
    consecutive_losses = consecutive_losses or _has_consecutive_losses
    try:
        from app.playbook.ic_tracker import (
            _check_graduation,
            compute_playbook_ic,
        )
        from app.playbook.db import PlaybookDB

        database = PlaybookDB(db_name="ftseek_research")
        ic_report = compute_playbook_ic(database, days=30)
        graduation = _check_graduation(ic_report)
        checks = dict(graduation.get("checks", {}))
        checks["no_consecutive_5_losses"] = not consecutive_losses(5)
        graduated = all(checks.values()) and graduation.get("graduated", False)
        failed = [key for key, value in checks.items() if not value]
        return {
            "graduated": graduated,
            "checks": checks,
            "ic_t3": graduation.get("ic_t3"),
            "sample_count": graduation.get("sample_count"),
            "ic_t1": ic_report.get("overall_ic_t1"),
            "ic_t5": ic_report.get("overall_ic_t5"),
            "sample_t1": ic_report.get("sample_t1", 0),
            "sample_t3": ic_report.get("sample_t3", 0),
            "sample_t5": ic_report.get("sample_t5", 0),
            "reason": "" if graduated else f"未毕业: {failed}",
        }
    except Exception as error:
        logger.error(f"[playbook] graduation gate 异常 (fail-close): {error}")
        return {"graduated": False, "checks": {}, "reason": f"门禁异常: {error}"}


def _check_same_day_hard_stop_cooldown(
    symbol: str, direction: str, today: str
) -> Optional[Dict[str, Any]]:
    """同日同向硬止损冷却闸; 查询异常 fail-open。"""
    try:
        from app.playbook.intraday_reentry import _HARD_STOP_REASONS
        from app.core.db_utils import safe_sqlite_connect
        from app.core.paths import get_trade_records_db

        normalized_direction = (
            "LONG" if str(direction).upper() in ("BUY", "LONG") else "SHORT"
        )
        connection = safe_sqlite_connect(get_trade_records_db(), timeout=5)
        try:
            rows = connection.execute(
                "SELECT exit_reason, exit_time FROM trade_records "
                "WHERE symbol = ? AND substr(exit_time,1,10) = ? "
                "AND UPPER(direction) = ? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND exit_time IS NOT NULL AND exit_time != ''",
                (symbol, today, normalized_direction),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            reason = str(row[0] or "").upper()
            if reason in _HARD_STOP_REASONS:
                return {
                    "prior_exit_reason": reason,
                    "prior_exit_time": str(row[1] or ""),
                }
    except Exception as error:
        logger.warning(
            f"[playbook] 同日硬止损冷却闸查询异常 (fail-open 放行): {error}"
        )
    return None


def _emit_exec_gate_audit(
    instruction: Dict[str, Any],
    *,
    action: str,
    entry: float,
    size: float,
    notional: float,
    extra: Optional[Dict[str, Any]] = None,
    now_et_provider: Optional[Callable] = None,
    module_file: Optional[str] = None,
) -> None:
    """执行漏斗统一审计; 纯影子, 异常不影响交易主流程。"""
    try:
        from app.decision.edge_shadow_gate import get_edge_shadow_gate

        decision_id = str(instruction.get("decision_id", "") or "")
        if decision_id.startswith("DA-"):
            source = "da_bridge"
        elif decision_id.startswith("universe_expansion"):
            source = "universe_expansion"
        elif decision_id:
            source = "internal"
        else:
            source = "unknown"

        direction = instruction.get("direction", "")
        confidence = float(instruction.get("confidence", 0) or 0)
        try:
            edge = get_edge_shadow_gate().evaluate(
                instruction.get("symbol", ""),
                direction,
                confidence,
                live_pass=True,
                log=False,
                decision_id=str(instruction.get("decision_id", "") or ""),
            ) or {}
        except Exception:
            edge = {}

        now_et = datetime.now(now_et_provider() if now_et_provider else None)
        row = {
            "ts": now_et.isoformat(timespec="seconds"),
            "date": now_et.strftime("%Y-%m-%d"),
            "ts_cst": datetime.now().isoformat(timespec="seconds"),
            "tz": "ET",
            "inst_id": instruction.get("id"),
            "symbol": instruction.get("symbol", ""),
            "direction": direction,
            "confidence": round(confidence, 2),
            "source": source,
            "decision_id": decision_id,
            "action": action,
            "entry": round(float(entry or 0), 4),
            "size": int(size or 0),
            "notional": round(float(notional or 0), 2),
            "edge_bucket": edge.get("bucket"),
            "edge_expectancy": edge.get("expectancy"),
            "edge_verdict": edge.get("edge_verdict"),
            "edge_reason": edge.get("reason"),
        }
        knife_audit = instruction.get("_knife_audit")
        if isinstance(knife_audit, dict):
            row["knife_gate"] = dict(knife_audit)
        if extra:
            row.update(extra)

        output_path = (
            Path(module_file or __file__).resolve().parents[2]
            / "data" / "shadow" / "exec_gate_audit.jsonl"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _live_engineering_probe_context() -> Optional[Dict[str, Any]]:
    """Build the LIVE C-lite context only after the shared base gate passes."""
    try:
        import os
        from app.core.config_container import get_config
        from app.playbook.db import route_playbook_db
        from app.playbook.live_arm_gate import (
            evaluate_live_arm_gate,
            read_live_arm_halt,
        )
        from app.playbook.playbook_service import PlaybookService
        from app.core.scheduler_triggers import get_market_phase

        arm = get_config().get_dotted("playbook.live_manual_arm", {}) or {}
        live_db = route_playbook_db("real")
        base_gate = evaluate_live_arm_gate(
            arm,
            allow_real_trading=(
                os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true"
            ),
            real_account_id=os.getenv("REAL_ACCOUNT_ID", ""),
            lane_db_name=live_db.db_name,
            instruction_direction="BUY",
            market_phase=get_market_phase().value,
            open_live_positions=0,
            halted=bool(read_live_arm_halt().get("halted")),
        )
        if not base_gate.get("allowed"):
            return None
        return {"db": live_db, "service": PlaybookService(live_db)}
    except Exception as error:
        logger.warning(
            f"[playbook] LIVE C-lite context unavailable: {error}", exc_info=True
        )
        return None


def _mirror_instruction_to_live(
    instruction: Dict[str, Any], live_db
) -> Optional[int]:
    """Mirror one SIM instruction into the LIVE lane with the same decision_id."""
    try:
        mirror = dict(instruction)
        metadata = mirror.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, json.JSONDecodeError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["dual_arm"] = {
            "target_env": "real",
            "source_instruction_id": instruction.get("id"),
            "decision_id": instruction.get("decision_id"),
            "mode": "engineering_probe",
        }
        mirror["metadata"] = metadata
        mirror["position_size"] = 1
        return int(live_db.save_instruction(mirror))
    except Exception as error:
        logger.error(
            f"[playbook] LIVE instruction mirror failed "
            f"{instruction.get('symbol')}: {error}",
            exc_info=True,
        )
        return None


def _live_mirror_gate_decision(
    live_db, instruction: Dict[str, Any]
) -> Dict[str, Any]:
    """Re-run the shared LIVE gate immediately before writing a mirror row."""
    import os
    from app.core.paths import get_trade_records_db_for_arm
    from app.core.config_container import get_config
    from app.core.scheduler_triggers import get_market_phase
    from app.playbook.live_arm_gate import (
        count_open_live_positions,
        evaluate_live_arm_gate,
        read_live_arm_halt,
    )

    arm = get_config().get_dotted("playbook.live_manual_arm", {}) or {}
    return evaluate_live_arm_gate(
        arm,
        allow_real_trading=(
            os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true"
        ),
        real_account_id=os.getenv("REAL_ACCOUNT_ID", ""),
        lane_db_name=getattr(live_db, "db_name", ""),
        instruction_direction=str(instruction.get("direction") or ""),
        market_phase=get_market_phase().value,
        open_live_positions=count_open_live_positions(
            get_trade_records_db_for_arm("real")
        ),
        halted=bool(read_live_arm_halt().get("halted")),
    )


def _mirror_instruction_to_live_if_allowed(
    instruction: Dict[str, Any],
    live_db,
    *,
    mirror: Optional[Callable] = None,
    gate_decision: Optional[Callable] = None,
):
    """Return (mirror_id, gate); a rejected gate never writes LIVE."""
    mirror = mirror or _mirror_instruction_to_live
    gate_decision = gate_decision or _live_mirror_gate_decision
    gate = gate_decision(live_db, instruction)
    if not gate.get("allowed"):
        return None, gate
    return mirror(instruction, live_db), gate
