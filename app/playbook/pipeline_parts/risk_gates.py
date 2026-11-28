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


def no_trade_short_sim_experiment_active() -> bool:
    """[DEB-20261125 §8] NO_TRADE SHORT 直接验证实验是否生效 (SIM-only)。

    用户裁 (2026-11-25): SIM 无真实资金, 直接验证高 VIX 恐惧日 DA SHORT 的
    成本后期望, 覆盖影子先行默认。LIVE-capable 或 live-arm 结构上可接受
    SHORT real-target 镜像时恒 False; 配置/能力读取失败 = False
    (回落 NO_TRADE 全禁旧门)。
    """
    try:
        from app.core.live_readiness import is_live_capable_env
        if is_live_capable_env():
            return False
        import os
        allow_real_trading = (
            os.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true"
        )
        if allow_real_trading:
            from app.core.config_container import get_config
            from app.playbook.db import route_playbook_db
            from app.playbook.live_arm_gate import evaluate_live_arm_gate
            arm = get_config().get_dotted("playbook.live_manual_arm", {}) or {}
            if bool(arm.get("enabled", False)):
                live_db = route_playbook_db("real")
                decision = evaluate_live_arm_gate(
                    arm,
                    allow_real_trading=allow_real_trading,
                    real_account_id=os.getenv("REAL_ACCOUNT_ID", ""),
                    lane_db_name=live_db.db_name,
                    instruction_direction="SELL_SHORT",
                    market_phase="TRADING",
                    open_live_positions=0,
                    halted=False,
                )
                if decision.get("allowed"):
                    return False
        from app.playbook.config import get_playbook_config
        return bool(get_playbook_config().get(
            "no_trade_short_sim_experiment_enabled", False))
    except Exception:
        return False


def no_trade_short_regime_mult(direction, exp_active: bool) -> float:
    """NO_TRADE 档仓位乘数: 实验生效且 SHORT → risk_off 同款 0.3, 否则 0.0。"""
    if exp_active and str(direction or "").upper() in ("SELL_SHORT", "SHORT"):
        return 0.3
    return 0.0


def _record_regime_snapshot(sink, stage, context, logger):
    if sink is not None:
        try:
            sink[stage] = context.to_dict()
        except Exception as error:
            logger.warning(f"[playbook] regime 审计快照失败，不影响原门禁: {error}", exc_info=True)


def _auto_execute_preflight(
    today: str,
    *,
    check_daily_loss_circuit: Callable[[str], Optional[Dict[str, Any]]],
    logger,
    entry_data_owner=None,
    regime_audit=None,
) -> Optional[Dict[str, Any]]:
    """auto_execute 前置闸；只有入口 owner 路径有界化只读 regime 输入。"""
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
        if entry_data_owner is None:
            # Existing non-entry/direct callers retain their original behavior.
            from app.playbook.market_regime import MarketRegimeGate
            regime_ctx = MarketRegimeGate().assess()
        else:
            from app.playbook.entry_data_fetch import (
                EntryDataUnavailable, fetch_entry_market_regime)
            try:
                regime_ctx = fetch_entry_market_regime(entry_data_owner)
            except EntryDataUnavailable as error:
                # Never feed this through SIM's generic risk fail-open branch.
                # Kill switch, risk-state mutation and daily-loss remain caller work.
                logger.warning(f"[playbook] 入场 regime 取数暂不可用，本轮暂缓: {error}", exc_info=True)
                return {"date": today, "executed": 0, "skipped": 0,
                        "reason": "entry_data_unavailable",
                        "skipped_reason": "entry_data_unavailable",
                        "query_label": error.query_label}
        _record_regime_snapshot(regime_audit, "preflight", regime_ctx, logger)
        from app.playbook.market_regime import MarketRegime
        from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
        if regime_ctx.regime == MarketRegime.NO_TRADE:
            if no_trade_short_sim_experiment_active():
                # [DEB-20261125 §8] SIM-only 实验: 放行本轮, 下游 auto_execute_flow
                #   按方向过滤 (仅 SELL_SHORT) + sizing 用 0.3; LONG 双层拦截。
                logger.warning(
                    f"[playbook] NO_TRADE SHORT实验 (SIM-only, VIX={regime_ctx.vix:.2f}): "
                    "仅放行 SELL_SHORT (risk_off 仓位参数), LONG 仍禁"
                )
            else:
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
        # 2026-10-17 审计: 配置不可读时按环境回落 — LIVE-capable → closed, SIM → open
        from app.core.live_readiness import preflight_fail_mode
        fail_mode = preflight_fail_mode()
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
                # DA 直写已绕开旧生成侧打点；执行漏斗成为 edge 影子的常驻写方。
                # EdgeShadowGate 内按 (ET日, decision_id) 去重，避免与旧路径双写。
                log=True,
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
        # C13/C14 (DEB-20261116 §10 第 7 步): 臂归因单源 —— mode 读 live_manual_arm
        # (原硬编码 engineering_probe ⇒ 签署日翻 strategy_probe 后标记仍是旧值,
        # 真钱单事后无法按臂归因); 附臂闸状态快照, 事后可辨"镜像时限额是否生效"。
        # 审计元数据读失败只降级标签, 不影响镜像写入。
        try:
            from app.core.config_container import get_config
            _arm_mode = str(get_config().get_dotted("playbook.live_manual_arm.mode", "") or "")
        except Exception:
            _arm_mode = ""
        try:
            from app.playbook import s1_arm as _s1
            _s1_on = bool(_s1.enabled())
        except Exception:
            _s1_on = None
        metadata["dual_arm"] = {
            "target_env": "real",
            "source_instruction_id": instruction.get("id"),
            "decision_id": instruction.get("decision_id"),
            "mode": _arm_mode or "config_unreadable",
            "s1_arm_enabled": _s1_on,
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
    decision = evaluate_live_arm_gate(
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
    # C14: 拒绝归因上下文 —— 运维可辨"哪个臂模式的限额拒了单" (附加键, 旧消费方不受影响)
    decision["arm_mode"] = str(arm.get("mode") or "")
    return decision


# ── RFC-218 §23: NO_TRADE SHORT 实验指令 LIVE 镜像隔离 (§22.9 #5) ─────────────────
# 病灶: 镜像入口未按 no_trade_short_exp 排除实验指令。当前 live 闸只收 LONG/BUY
#   (live_arm_gate LIVE_PROBE_DIRECTION_REJECTED) ⇒ 本路径今日零流量、零行为变化;
#   但未来 SHORT 真实目标门获允许时, 跨 regime 遗留的 SIM-only 实验指令仍可能被镜像。
# 合同 (纵深防御, SHORT 实盘授权前的必备隔离):
#   - no_trade_short_exp 标记 (内存 ∨ 持久化源 metadata) → 排除 (标记=cohort SSOT,
#     §10.3 实验身份以指令身份确定, 不以成交时 regime 重划);
#   - SHORT ∧ reprice_audit.regime_reads.sizing.regime==no_trade → 排除 (实验执行
#     次级证据, 覆盖标记写失败但 reprice 审计在库的情形);
#   - SHORT ∧ 两源 metadata 均不可读 (缺失/坏 blob/行缺失/读失败/无 id) → fail-closed
#     排除 (无法排除实验身份的 SHORT 不镜像; SHORT 实盘授权裁决时须复核本谓词);
#   - LONG/BUY 无标记 → 永不被本谓词排除 (实验仅 SHORT)。
# 持久化源 = SIM/research lane (镜像恒 SIM→LIVE); 内存 metadata 可能滞后于 sizing
#   期的 merge_instruction_metadata ⇒ 持久化读为权威, 内存只能追加排除不能撤销。
# 回滚: revert 本批 commit (无开关; 今日零流量 ⇒ 无运行时差异)。


def _parse_metadata_blob(blob: Any) -> Optional[Dict[str, Any]]:
    """dict → 原样; JSON str → 解析; 其余/不可解析/非 dict → None (不可核验)。"""
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _no_trade_short_exp_marker(metadata: Optional[Dict[str, Any]]) -> bool:
    # 存在性判定 (非 truthy): 空 dict 标记也是标记; 仅 None/缺失 = 无标记。
    return bool(isinstance(metadata, dict)
                and metadata.get("no_trade_short_exp") is not None)


def _no_trade_sizing_regime(metadata: Optional[Dict[str, Any]]) -> bool:
    """reprice_audit.regime_reads.sizing.regime == no_trade (实验执行次级证据)。"""
    if not isinstance(metadata, dict):
        return False
    audit = metadata.get("reprice_audit")
    if not isinstance(audit, dict):
        return False
    reads = audit.get("regime_reads")
    if not isinstance(reads, dict):
        return False
    sizing = reads.get("sizing")
    if not isinstance(sizing, dict):
        return False
    return str(sizing.get("regime") or "").upper() == "NO_TRADE"


def _is_no_trade_short_experiment_instruction(
    instruction: Dict[str, Any], *, source_db=None
) -> tuple:
    """Return (is_experiment, reason)。合同见上方注释块。

    source_db 可注入 (测试); 默认 route_playbook_db("sim")。读失败对 SHORT
    fail-closed (持久化不可读 + 内存不可读 ⇒ 排除), 对 LONG 无影响。
    """
    direction = str(instruction.get("direction") or "").upper()
    is_short = direction in ("SELL_SHORT", "SHORT")

    mem_md = _parse_metadata_blob(instruction.get("metadata"))
    if _no_trade_short_exp_marker(mem_md):
        return True, "no_trade_short_exp_marker_in_memory"
    if not is_short:
        return False, ""

    persisted_md = None
    persisted_readable = False
    inst_id = instruction.get("id")
    if inst_id is not None:
        try:
            if source_db is None:
                from app.playbook.db import route_playbook_db
                source_db = route_playbook_db("sim")
            row = source_db.get_instruction_by_id(int(inst_id))
            if row is not None:
                blob = row.get("metadata_json")
                if blob is None:
                    # 行存在且 metadata 为空 = 一次成功的持久化读 (库内无标记)。
                    persisted_readable = True
                    persisted_md = {}
                else:
                    persisted_md = _parse_metadata_blob(blob)
                    persisted_readable = persisted_md is not None
        except Exception as exc:
            logger.warning(
                f"[playbook] 实验隔离持久化 metadata 读取失败 "
                f"(SHORT fail-closed): id={inst_id} err={exc}",
                exc_info=True,
            )
    if _no_trade_short_exp_marker(persisted_md):
        return True, "no_trade_short_exp_marker_persisted"
    if _no_trade_sizing_regime(persisted_md) or _no_trade_sizing_regime(mem_md):
        return True, "no_trade_sizing_regime_audit"
    if persisted_readable or mem_md is not None:
        return False, ""
    return True, "short_metadata_unverifiable_fail_closed"


def _mirror_instruction_to_live_if_allowed(
    instruction: Dict[str, Any],
    live_db,
    *,
    mirror: Optional[Callable] = None,
    gate_decision: Optional[Callable] = None,
    experiment_check: Optional[Callable] = None,
):
    """Return (mirror_id, gate); a rejected gate never writes LIVE.

    RFC-218 §23: 实验隔离先于臂闸 —— NO_TRADE SHORT 实验指令 (SIM-only cohort)
    绝不进入 LIVE 镜像, 独立于臂闸状态。隔离拒绝走与臂闸拒绝相同的 (None, gate)
    合同, 调用方日志/推送路径不变。
    """
    mirror = mirror or _mirror_instruction_to_live
    gate_decision = gate_decision or _live_mirror_gate_decision
    experiment_check = experiment_check or _is_no_trade_short_experiment_instruction
    try:
        _is_exp, _exp_reason = experiment_check(instruction)
    except Exception as _exp_err:
        # 谓词自身异常: SHORT fail-closed / LONG fail-open (实验仅 SHORT)。
        logger.warning(
            f"[playbook] 实验隔离谓词异常: {instruction.get('symbol')} err={_exp_err}",
            exc_info=True,
        )
        _is_short = str(instruction.get("direction") or "").upper() in ("SELL_SHORT", "SHORT")
        _is_exp = _is_short
        _exp_reason = "experiment_check_error_fail_closed" if _is_short else ""
    if _is_exp:
        return None, {
            "allowed": False,
            "reasons": [{
                "code": "NO_TRADE_SHORT_EXP_ISOLATION",
                "message": (
                    "NO_TRADE SHORT experiment instruction excluded from LIVE mirror "
                    f"({_exp_reason})"
                ),
            }],
        }
    gate = gate_decision(live_db, instruction)
    if not gate.get("allowed"):
        return None, gate
    return mirror(instruction, live_db), gate
