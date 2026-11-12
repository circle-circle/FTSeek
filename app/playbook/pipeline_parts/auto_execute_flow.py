"""auto_execute 主流程 (阶段11迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

import json

# 2026-11-03 冻结长轮次: 主线程进度心跳 (与出场快循环心跳分离, 见 progress_beat)
from app.observability.progress_beat import progress_ping  # noqa: E402
from app.playbook.price_ledger import build_price_ledger, calculate_risk_reward
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def _admission_at_execution_enabled() -> bool:
    """2026-10-23: apply the existing admission gate with actual order size.

    The write-time gate is intentional precheck-only (estimated multiplier),
    and run_playbook_generate only covers watchlist survivors. Without this
    check a DA row outside that screen can reach submission without ever being
    evaluated at its real notional. Config rollback: false.
    """
    try:
        from app.core.config_container import get_config as _gc
        return bool(_gc().get_dotted(
            "playbook.admission.enforce_at_execution", True))
    except Exception:
        return True  # fail-safe: keep the fee/notional safety gate on


def execution_admission_evaluate(inst: Dict[str, Any],
                                 reprice_params: Optional[Dict[str, Any]] = None
                                 ) -> Optional[Dict[str, Any]]:
    """Evaluate DA instructions with re-priced entry/SL/TP and actual size.

    Returns None for non-DA rows / disabled switch / evaluation error.
    """
    if not str(inst.get("decision_id") or "").startswith("DA-"):
        return None
    if not _admission_at_execution_enabled():
        return None
    payload = dict(inst)
    if reprice_params:
        for key in ("entry_price", "stop_loss", "take_profit", "position_size"):
            if reprice_params.get(key) is not None:
                payload[key] = reprice_params[key]
    raw = inst.get("raw_instruction_json")
    if isinstance(raw, dict):
        payload["raw_instruction"] = raw
    from app.playbook.admission import evaluate as _adm_eval
    return _adm_eval(payload)


def _reprice_quote_age_gate(
    inst: Dict[str, Any],
    ctx: Optional[Dict[str, Any]],
    pb_cfg: Any,
    db: Any,
    logger: Any,
    _emit_exec_gate_audit: Callable,
) -> bool:
    """RFC-185 报价陈旧度门; DA 与 MOM 共用, 过龄不起价。"""
    if not bool(pb_cfg.get("reprice_quote_age_gate_enabled", True)):
        return True
    _qage_ok, _qage_why = True, ""
    _qage_ms = None
    try:
        from app.risk.data_health_guard import check_reprice_quote_age as _qage_check
        _qage_ok, _qage_ms, _qage_why = _qage_check(
            ctx, pb_cfg.get("reprice_quote_age_max_ms", 30000))
    except Exception as _qage_err:
        _qage_ok, _qage_why = True, f"check_error: {_qage_err}"
    if not _qage_ok:
        _qage_reason = f"stale_quote_gate({_qage_why})"
        db.update_instruction_tracking(inst["id"], {"skip_reason": _qage_reason})
        _emit_exec_gate_audit(
            inst, action="skipped_stale_quote",
            entry=float(inst.get("entry_price") or 0),
            size=float(inst.get("position_size") or 0),
            notional=0.0,
            extra={"gate": "reprice_quote_age",
                   "why": _qage_why,
                   "quote_age_ms": _qage_ms})
        logger.info(
            f"[playbook] {inst['symbol']} 报价过龄 ({_qage_why}) "
            f"→ 本轮不起价 (下轮重试)")
        return False
    return True


def _mom_quote_price(ctx: Optional[Dict[str, Any]]) -> float:
    """提取 MOM 实际用于 reprice 的卖一价；缺失/非有限值一律视为无报价。"""
    if not isinstance(ctx, dict):
        return 0.0
    import math
    for key in ("lv3_ask", "ask", "price", "last"):
        try:
            value = float(ctx.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


_MOM_DEFER_AUDIT_SEEN: set = set()  # {(trade_date, instruction_id, reason)}


def _record_mom_defer(db: Any, inst: Dict[str, Any], today: str, reason: str,
                      action: str, gate: str, why: str, logger: Any,
                      emit_exec_gate_audit: Callable) -> None:
    """记录暂时性 MOM 拦截；审计按指令/日/原因去重。

    2026-11-12 (核查后修正): **不再写 `skip_reason`**。该列是终态字段 ——
    `db.update_instruction_tracking` 只在 `executed=1` 时清空它, 所以把暂时态写进去
    等于当天永久留痕; 而下游至少三处把"非空 skip_reason"直接当"已跳过":
    `premarket_invalidation` (不再评估作废) / `shadow_tracker` (退出影子不取样) /
    `ue_batch` (不参与 UE 打分)。这与本批同时修掉的 MOM `MOM_FEE_SIZE_INFEASIBLE`
    终态跳过是同一个病根 (暂时态写进终态字段)。
    留痕改由 `exec_gate_audit` 承担 (带 transient=True), 一行不丢, 且可离线审计。
    """
    key = (today, inst.get("id"), reason)
    if key in _MOM_DEFER_AUDIT_SEEN:
        return
    emit_exec_gate_audit(
        inst, action=action,
        entry=float(inst.get("entry_price") or 0),
        size=float(inst.get("position_size") or 0),
        notional=0.0,
        extra={"gate": gate, "why": why, "transient": True})
    logger.warning(f"[MOM-ETF-V1] {inst.get('symbol')} {reason} (下轮复检)")
    _MOM_DEFER_AUDIT_SEEN.add(key)


def open_floor_minutes(logger: Any = None):
    """兼容既有调用方；时间口径实现见 ``app.playbook.open_blackout``。"""
    from app.playbook.open_blackout import open_floor_minutes as _open_floor
    return _open_floor(logger=logger)


def _blackout_shadow_path() -> Path:
    """data/entry_blackout_shadow.jsonl — 锚定模块路径 (相对 cwd 会写进生产库旁边)。"""
    return Path(__file__).resolve().parents[3] / "data" / "entry_blackout_shadow.jsonl"


_BLACKOUT_SHADOW_SEEN: set = set()   # {(date, inst_id)} 首次落入可触发带去重 (进程内)


def emit_blackout_shadow(scheduler, today: str, now_dt, logger: Any,
                         invalidated_reasons, floor_hhmm: int = 945) -> None:
    """开盘禁入窗内的**只读**观测 (2026-11-12, 用户裁 A)。

    为什么存在: 黑窗闸在 `run_playbook_auto_execute` 顶部直接 return, 逐条 trigger
    一次都不跑 ⇒ 09:30-09:45 **零遥测** (实测 20 日 `trigger_miss` 09:30-09:34 = 0 行)。
    于是"延后到 09:45 到底切掉了什么"只能用历史回放回答, 前向永远没有对照样本。

    零行为变更 (硬约束): 不写 DB / 不改 skip_reason / 不做永久作废 / 不下单 / 不 reprice,
    只 append 两种行:
      1) 每轮一行汇总 (kind=cycle): 候选数/取到价数/带内可触发数 —— 证明观测本身活着
      2) 每条指令**首次**落入可触发带时一行明细 (kind=hit) —— 供离线对账
         短期去重键 (date, inst_id) 进程内; 重启可能重复, 离线按 (date, inst_id) 去重。

    口径声明 (别把它读成"会成交"): 只算 band 几何 + gap, **不**复算后续门
    (VWAP/低ATR/流动性/借券/thesis漂移/极端gap/admission) ⇒ `band_would_trigger` 是
    **上限**。窗口内量能门处于开盘豁免 (`intraday_vol_gate_grace_min: 15`, 0<=min<15),
    故 vol_ok 恒真且不需要取量; 若有人把 grace 调到 <15, 本函数仍然按 vol_ok=True 记,
    并在行里标 `vol_assumed_ok` 供事后剔除。
    """
    import json as _json
    from app.playbook.config import get_playbook_config
    _pb = get_playbook_config()
    if not bool(_pb.get("entry_blackout_shadow_enabled", True)):
        return
    from app.playbook.entry_band import (CHASE_ZONE_PCT, KNIFE_FLOOR_PCT,
                                         classify_trigger, vol_widen_band_pct)
    _widen = bool(_pb.get("entry_band_vol_widen_enabled", False))
    _grace = float(_pb.get("intraday_vol_gate_grace_min", 15))
    _dr = (getattr(scheduler, "data_router", None)
           or getattr(scheduler, "market_data", None))
    _insts = []
    _status = "NO_ROUTER" if _dr is None else "OK"
    try:
        from app.playbook.db import route_playbook_db
        _insts = route_playbook_db("sim").get_instructions_by_date(today) or []
    except Exception as _db_err:
        _status = "DB_ERROR"
        logger.warning(f"[blackout-shadow] 指令读取失败: {_db_err}", exc_info=True)
    _rows = []
    _pending_seen = set()
    _n_eligible = _n_priced = _n_would = 0
    _n_quote_error = _n_price_invalid = _n_eval_error = 0
    for inst in _insts if _dr is not None else []:
        try:
            _d = str(inst.get("direction") or "").upper()
            if _d in ("", "SKIP"):
                continue
            _is_short = ("SHORT" in _d) or _d in ("SELL", "SELL_SHORT")
            if inst.get("triggered") or inst.get("executed"):
                continue
            if (inst.get("skip_reason") or "") in invalidated_reasons:
                continue
            _sym = str(inst.get("symbol") or "")
            if not _sym or "TEST" in _sym.upper():
                continue
            _e = float(inst.get("entry_price") or 0)
            if _e <= 0:
                continue
            _n_eligible += 1
            _px = 0.0
            _quote_source = None
            try:
                _ctx = _dr.get_full_market_context(_sym)
                if isinstance(_ctx, dict):
                    _px = float(_ctx.get("price") or 0)
                    _quote_source = "full_market_context" if _px > 0 else None
            except Exception:
                _n_quote_error += 1
            # 与生产 check_intraday_trigger 一致：主上下文无价时用 quote 兜底，
            # 否则会把行情字段缺失误判为“黑窗没有机会”。
            if _px <= 0:
                try:
                    _quote = _dr.get_quote(_sym)
                    if isinstance(_quote, dict):
                        _px = float(_quote.get("last", 0) or _quote.get("price", 0) or 0)
                        _quote_source = "quote_fallback" if _px > 0 else None
                except Exception:
                    _n_quote_error += 1
            if _px <= 0:
                _n_price_invalid += 1
                continue
            _n_priced += 1
            if _widen:
                _knife, _chase = vol_widen_band_pct(inst.get("atr_pct"))
            else:
                _knife, _chase = KNIFE_FLOOR_PCT, CHASE_ZONE_PCT
            _trig, _path = classify_trigger(_is_short, _e, _px, _knife, _chase, True)
            if not _trig:
                continue
            _n_would += 1
            _key = (today, inst.get("id"))
            if _key in _BLACKOUT_SHADOW_SEEN or _key in _pending_seen:
                continue
            # 不能在写盘前提交去重状态：append 失败时下一轮必须重试，
            # 重启产生的重复行由 evaluator 取最早 ts 消除。
            _pending_seen.add(_key)
            _rows.append({
                "kind": "hit",
                "ts": now_dt.isoformat(timespec="seconds"),
                "date": today,
                "et_min": now_dt.hour * 60 + now_dt.minute,
                "inst_id": inst.get("id"),
                "decision_id": str(inst.get("decision_id") or ""),
                "symbol": _sym,
                "direction": "SHORT" if _is_short else "BUY",
                "entry_price": round(_e, 4),
                "price": round(_px, 4),
                "gap_pct": round((_px - _e) / _e * 100.0, 4),
                "entry_path": _path,
                "band_would_trigger": True,
                "knife_floor": round(_knife, 4), "chase_zone": round(_chase, 4),
                "vol_assumed_ok": True, "vol_grace_min": _grace,
                "atr_pct": inst.get("atr_pct"),
                "confidence": inst.get("confidence"),
                "quote_source": _quote_source,
                "blackout_hhmm": floor_hhmm,
                "caveat": "band geometry only; later gates unobserved",
            })
        except Exception as _one_err:
            _n_eval_error += 1
            logger.warning(f"[blackout-shadow] 单条评估失败 (不阻断): {_one_err}",
                           exc_info=True)
    if _status == "OK":
        if _n_eval_error:
            _status = "EVAL_PARTIAL"
        elif _n_eligible == 0:
            _status = "NO_CANDIDATES"
        elif _n_priced == 0:
            _status = "QUOTE_UNAVAILABLE"
        elif _n_quote_error or _n_price_invalid:
            _status = "PARTIAL_QUOTE"
    try:
        _p = _blackout_shadow_path()
        _p.parent.mkdir(parents=True, exist_ok=True)
        _sum = {
            "kind": "cycle", "ts": now_dt.isoformat(timespec="seconds"),
            "date": today, "et_min": now_dt.hour * 60 + now_dt.minute,
            "status": _status,
            "n_inst": len(_insts), "n_eligible": _n_eligible,
            "n_priced": _n_priced, "n_quote_error": _n_quote_error,
            "n_price_invalid": _n_price_invalid, "n_eval_error": _n_eval_error,
            "n_would_trigger": _n_would, "n_new_hits": len(_rows),
            "blackout_hhmm": floor_hhmm,
            "caveat": "band geometry only; later gates unobserved",
        }
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_sum, ensure_ascii=False) + "\n")
            for _r in _rows:
                _f.write(_json.dumps(_r, ensure_ascii=False) + "\n")
        _BLACKOUT_SHADOW_SEEN.update(_pending_seen)
    except Exception as _w_err:
        logger.warning(f"[blackout-shadow] 写盘失败 (不阻断交易): {_w_err}", exc_info=True)


def run_playbook_auto_execute(
    scheduler,
    *,
    force: bool = False,
    datetime_cls: Callable,
    _now_et: Callable,
    _today_et_str: Callable,
    module_file: str,
    logger: Any,
    INVALIDATED_SKIP_REASONS,
    _CC_NORM_LOGGED: set,
    _CC_NORM_LOGGED_MAX: int,
    _auto_execute_preflight: Callable,
    _chase_cap_round_bump: Callable,
    _check_bridge_health_and_alert: Callable,
    _check_intraday_trigger: Callable,
    _check_same_day_hard_stop_cooldown: Callable,
    _compute_volatility_based_sl_tp: Callable,
    _emit_exec_gate_audit: Callable,
    _is_trading_day_today: Callable,
    _live_engineering_probe_context: Callable,
    _load_execution_budget: Callable,
    _mirror_instruction_to_live_if_allowed: Callable,
    _normalize_regime_str: Callable,
    _playbook_cfg: Callable,
    _positive_finite_atr: Callable,
    _resolve_sizing_base: Callable,
    _shadow_dedup_claim: Callable,
    _shadow_dedup_commit: Callable,
    _vol_bucket_sl_bounds: Callable,
) -> Optional[Dict[str, Any]]:
    """
    盘中自动执行 triggered 的指令 (Paper Trade 模式)。

    流程:
    1. 检查 playbook.auto_execute 配置开关 (默认 false)
    2. 获取今日指令
    3. 对每条 triggered=1 且 executed=0 且 direction != SKIP 的指令，自动执行
    4. 调用 playbook_service.execute_instruction()

    幂等: 同一指令不会重复执行 (executed=1 后跳过)。
    """
    try:
        from app.playbook.db import route_playbook_db
        from app.playbook.playbook_service import PlaybookService

        # 1. 检查配置开关
        if not _playbook_cfg("auto_execute", False):
            logger.info("[playbook] auto_execute=false, 跳过自动执行")
            return None

        today = _today_et_str()
        if not force and not _is_trading_day_today():
            logger.info(f"[playbook] {today} 非交易日, 跳过自动执行")
            return None

        # 2026-06-24: 开盘后不立即执行 — 避开开盘最大噪声 (gap fill + 做市商调仓)
        # 数据依据: 6/23 shadow 22笔中21笔在 9:31:14 批量执行, 2笔瞬间止损 (78ms/473ms)
        # 2026-11-12 (用户令「开盘后延后15分钟」): 本闸原是硬编码 `+5` (=09:35),
        #   与 config 的 entry_open_blackout_hhmm 是**同一语义的两处真源** — 改一处
        #   另一处仍是旧值, 谁真正 binding 取决于谁更晚 (0945 vs 0935 时本闸形同死码)。
        #   故删掉硬编码, 统一读同一键 (与 check_intraday_trigger 的开盘禁触窗同源同值)。
        #   语义: hhmm=0945 ⇒ 09:30-09:45 零入场, 首单 ≥09:45 ET;
        #         改回 0935 ⇒ 与本闸旧 `+5` 行为逐字等价 (回滚路径)。
        #   关闸 (entry_open_blackout_enabled=false) 或键不可解析 ⇒ 仍保底 0935
        #   (旧的 5 分钟噪声底线不随开关消失, 不会放开成 09:30 裸执行)。
        from datetime import datetime as _dt
        _et_tz = _now_et()
        _now_et_dt = _dt.now(_et_tz)
        _now_min = _now_et_dt.hour * 60 + _now_et_dt.minute
        _open_floor_min, _open_floor_hhmm = open_floor_minutes(logger=logger)
        if not force and _now_min < _open_floor_min:
            logger.info(
                f"[playbook] 开盘禁入窗内不执行 (now={_now_et_dt.strftime('%H:%M')} ET "
                f"< {_open_floor_hhmm // 100:02d}:{_open_floor_hhmm % 100:02d} ET)")
            # 2026-11-12 (用户裁 A): 黑窗内只读观测 — 本闸直接 return 使逐条 trigger
            #   一次不跑, 否则这一整窗零遥测 (改动的收益/代价无法用前向数据裁)。
            #   纯 append, 不写 DB/不改 skip_reason/不下单; 异常只 warn。
            try:
                emit_blackout_shadow(scheduler, today, _now_et_dt, logger,
                                     INVALIDATED_SKIP_REASONS, _open_floor_hhmm)
            except Exception as _bs_err:
                logger.warning(f"[playbook] 黑窗观测异常 (不阻断): {_bs_err}", exc_info=True)
            return None

        # 2026-07-19 (DEB-round3 #1): bridge 三态告警 — auto_execute 每30min入口,
        #   覆盖"启动后文件仍缺失"盲区 (bridge 仅启动时加载一次). 当日去重, 不阻断.
        _check_bridge_health_and_alert("auto_execute")

        # [阶段 7] kill/regime/risk/日亏前置闸本体迁 risk_gates;
        #   fail-open/fail-close 与返回 reason 顺序不变。
        _preflight = _auto_execute_preflight(today, logger=logger)
        if _preflight is not None:
            return _preflight

        db = route_playbook_db("sim")
        service = PlaybookService(db)
        live_context = _live_engineering_probe_context()
        instructions = db.get_instructions_by_date(today)

        logger.info(f"[playbook] auto_execute 开始: {len(instructions)} 条指令 (now={_now_et_dt.strftime('%H:%M')} ET)")

        if not instructions:
            logger.info(f"[playbook] {today} 无指令, 跳过自动执行")
            return {"date": today, "executed": 0, "skipped": 0}

        executed_count = 0
        skipped_count = 0
        # [RFC-169] 受理未确认 (fill=0 在途) 独立计数 — 既不是成功也不是失败
        submitted_pending_count = 0
        live_executed_count = 0
        live_skipped_count = 0
        errors = []

        # [阶段 9] 预算/单日 guardrail 装配本体迁 execution_flow;
        #   R1b/日帽 DB 失败仍按原 fail-close 返回。
        _budget_ctx, _budget_skip = _load_execution_budget(today)
        if _budget_skip is not None:
            return _budget_skip
        _max_deploy = _budget_ctx["max_deploy"]
        pb_cfg = _budget_ctx["playbook_config"]
        _bp_limit = _budget_ctx["budget_limit"]
        _risk_base_capital = _budget_ctx["risk_base_capital"]
        _daily_notional_cap = _budget_ctx["daily_notional_cap"]
        _daily_notional_cap_pct = _budget_ctx["daily_notional_cap_pct"]
        _max_daily_positions = _budget_ctx["max_daily_positions"]
        _max_new_per_cycle = _budget_ctx["max_new_per_cycle"]
        _max_open_positions = _budget_ctx["max_open_positions"]
        _today_opened_notional = _budget_ctx["today_opened_notional"]
        _today_opened_count = _budget_ctx["today_opened_count"]
        _open_positions_count = _budget_ctx["open_positions_count"]
        _cycle_new_count = 0

        # 按来源优先级 + confidence 降序 — 购买力优先分配给高优先来源的最强信号.
        # 漏斗最后一层: 多层过滤后剩N只, BP只够开2-3笔 → 先执行优先级最高的.
        # [ADR-020] 2026-07-17: UE(宇宙扩张) 执行优先级高于 DA — 新机会/新资讯动态标的
        #   优先占预算, DA 静态盘次之. 以 source_priority 为主键、confidence 为次键,
        #   与 confidence 语义解耦 (UE conf 上限50 仍保留在 llm_universe_scan.py, 不为排序而篡改).
        #   (取代 2026-07-09 "bridge候选(DA-)优先" 旧策略.)
        def _src_priority(inst):
            did = (inst.get("decision_id", "") or "")
            if did.startswith("universe_expansion"):
                return 2   # UE 最优先 (用户指定)
            if did.startswith("DA-"):
                return 1   # DA 次之
            return 0       # VotingCoordinator / 其他
        instructions.sort(key=lambda x: (
            _src_priority(x),
            x.get("confidence", 0) or 0
        ), reverse=True)

        # 2026-08-09 性能预审: broker 持仓是轮级全局状态, 循环外查一次即可.
        #   原实现每条未执行指令调 get_positions (OpenD 交易连接, 放量后 40+指令×78轮/日
        #   = 数千次连接调用), 同轮内重复查询结果不变 → 提升到循环外, 一轮一次.
        #   失败时降级为空列表 (与原先每条指令独立失败返回 [] 语义一致, 防误拦).
        try:
            from app.execution.dual_broker import get_dual_broker
            _dual = get_dual_broker()
            _broker_positions = _dual.get_positions(target_env="sim") or []
        except Exception as _bp_err:
            logger.warning(f"[playbook] 轮级 broker 持仓查询失败 (降级空表): {_bp_err}")
            _broker_positions = []

        # 2026-08-22 audit: already_held 对账查询的共享连接 — 原每条命中指令新开
        #   sqlite3.connect (N+1), 现循环级懒建一次、循环尾关闭; 查询失败自动回退逐条建连.
        _ah_conn = None
        from app.playbook.pilot_policy import (
            after_entry_cutoff,
            entries_enabled,
            in_entry_window,
            rebalance_day_status,
            mom_budget_reject_reason,
            mom_budget_snapshot,
            mom_reprice_params,
            policy_for,
        )
        _mom_open_count = 0
        _mom_today_notional = 0.0
        _mom_budget_ok = True
        if any(policy_for(inst.get("decision_id")) is not None for inst in instructions):
            try:
                _mom_open_count, _mom_today_notional = mom_budget_snapshot(_now_et_dt)
            except Exception as _mom_budget_err:
                _mom_budget_ok = False
                logger.error(f"[MOM-ETF-V1] 子帽预算查询失败 fail-close: {_mom_budget_err}",
                             exc_info=True)
        for inst in instructions:
            # 2026-11-03 (冻结长轮次): 主线程每处理一条指令打一次"进度"心跳 (5s 节流)。
            #   与出场快循环的 /run/ftseek-heartbeat 分开 (语义=有进展, 非"活着"),
            #   供 freeze_sentinel 在有界窗口内区分"慢轮次"与"真冻结"。
            #   progress_ping 自身绝不抛异常 ⇒ 调用点无需 try (except:pass 棘轮)。
            progress_ping()
            # 跳过已执行、SKIP 方向的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue
            # 2026-07-19 (DEB-round3 #2): 漂移作废指令不再参与触发 (特异性匹配, 防误杀)
            #   P1-2 (2026-08-23): 取值集合化 — 含 tripwire 核销 (见 INVALIDATED_SKIP_REASONS)
            if (inst.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
                continue

            _mom_policy = policy_for(inst.get("decision_id"))
            if _mom_policy is not None:
                if not entries_enabled():
                    _record_mom_defer(
                        db, inst, today, "MOM_ENTRIES_DISABLED",
                        action="skipped_mom_entries_disabled",
                        gate="mom_entries_enabled", why="entries_disabled_or_config_error",
                        logger=logger, emit_exec_gate_audit=_emit_exec_gate_audit)
                    skipped_count += 1
                    continue
                if not _mom_budget_ok:
                    _record_mom_defer(
                        db, inst, today, "MOM_BUDGET_UNAVAILABLE",
                        action="skipped_mom_budget_unavailable",
                        gate="mom_budget", why="budget_snapshot_failed",
                        logger=logger, emit_exec_gate_audit=_emit_exec_gate_audit)
                    skipped_count += 1
                    continue
                _rebalance = rebalance_day_status(_now_et_dt.date(), _mom_policy)
                if _rebalance.reason == "calendar_unavailable":
                    _record_mom_defer(
                        db, inst, today, "MOM_CALENDAR_UNAVAILABLE",
                        action="skipped_mom_calendar_unavailable",
                        gate="mom_rebalance_calendar",
                        why=str(_rebalance.error)[:160],
                        logger=logger, emit_exec_gate_audit=_emit_exec_gate_audit)
                    skipped_count += 1
                    continue
                if not _rebalance.is_rebalance_day:
                    db.mark_instruction_guardrail_skip(inst["id"], "MOM_NOT_REBALANCE_DAY")
                    skipped_count += 1
                    continue
                if after_entry_cutoff(_now_et_dt, _mom_policy):
                    db.mark_instruction_guardrail_skip(inst["id"], "MOM_SAME_DAY_EXPIRED")
                    skipped_count += 1
                    continue
                if not in_entry_window(_now_et_dt, _mom_policy):
                    continue

            # ── [RFC-141] 入场延迟确认门 (D2' forward) — pending 拦截 ──
            #   必须在 reprice **之前**: pending 期间若让 reprice 覆盖 DB entry_price,
            #   锚会逐轮漂移 ⇒ 120s 后判定的就不是 arm 时刻那把尺子 (语义不可审计)。
            #   终态 ABANDONED/EXPIRED 也在此拦 (跨重启有效, 靠 metadata 旗标)。
            #   门关闭时 read_state 恒空 ⇒ 恒 False, 零行为变更。
            if _mom_policy is None:
                try:
                    from app.playbook.entry_confirm_gate import defer_pending as _ec_defer
                    from app.utils.timezone import now_et as _ec_now_et_fn
                    if _ec_defer(inst, _ec_now_et_fn()):
                        skipped_count += 1
                        continue
                except Exception as _ec_defer_err:
                    # fail-open: 门自身异常不得阻断正常执行 (拦普通单不是安全方向)
                    logger.warning(f"[EntryConfirm] pending 复检异常 (放行): {_ec_defer_err}",
                                       exc_info=True)

            # 2026-06-29 P0: 执行前查broker持仓 — 有仓则跳过，防重复下单+孤儿仓循环
            # 根因: fill=0时playbook拒绝记账但broker已成交, 下次auto_execute又下单 → 仓位叠加
            # 2026-08-09 性能预审: 持仓已提到循环外查一次 (_broker_positions), 此处仅复用.
            _symbol = inst.get("symbol", "")
            try:
                # F1 (2026-07-21): 方向感知 — 持有 SHORT 不应拦截新 BUY (反之亦然),
                #   否则合法反向再入被误标 already_held. qty>0=LONG, qty<0=SHORT.
                _want_long = inst.get("direction", "").upper() == "BUY"
                _already_held = any(
                    p.get("code") == _symbol
                    and abs(float(p.get("qty", 0) or 0)) > 0
                    and ((float(p.get("qty", 0) or 0) > 0) == _want_long)
                    for p in _broker_positions
                )
                if _already_held:
                    # P0 (2026-07-21): already_held 不再一律标 $0 —
                    #   broker 已有仓往往是 playbook 自己当日的成交 (fill=0 拒记账后 broker 已成交,
                    #   或先前批次已开). 旧逻辑把自成交标 execution_price=0/pnl=0/outcome=already_held,
                    #   导致日报盈亏低报、归因失真. 先对账 playbook 自己的当日 trade_record 回填真实价/盈亏,
                    #   仅当确无 playbook 记录 (外部/手动/ORB 持仓) 才退回旧 already_held 标记.
                    _pb_rec = None
                    try:
                        import sqlite3 as _sql3_ah
                        from app.core.paths import get_trade_records_db_for_arm
                        _dir_norm = "LONG" if inst.get("direction", "").upper() == "BUY" else "SHORT"
                        if _ah_conn is None:
                            _ah_conn = _sql3_ah.connect(
                                str(get_trade_records_db_for_arm("sim")), timeout=10.0)
                            _ah_conn.row_factory = _sql3_ah.Row
                        _pb_rec = _ah_conn.execute(
                            "SELECT entry_price, exit_price, exit_time, exit_reason, realized_pnl, pnl_pct, quantity, "
                            "pnl_net "
                            "FROM trade_records WHERE symbol = ? AND substr(entry_time,1,10) = ? "
                            "AND source_type = 'PLAYBOOK_SIM' "
                            "AND UPPER(direction) = ? ORDER BY id DESC LIMIT 1",
                            (_symbol, today, _dir_norm)
                        ).fetchone()
                    except Exception as _ah_q_err:
                        logger.debug(f"[playbook] {_symbol} already_held 对账查询失败: {_ah_q_err}")
                        # 连接级故障 (锁忙/schema) → 弃用共享连接, 下次重试重建
                        _ah_conn = None

                    _now_et_iso = datetime_cls.now(_now_et()).isoformat()
                    if _pb_rec and float(_pb_rec["entry_price"] or 0) > 0:
                        _entry = float(_pb_rec["entry_price"] or 0)
                        _exit = float(_pb_rec["exit_price"] or 0)
                        _upd = {
                            "executed": 1,
                            "execution_price": _entry,
                            "execution_time": _now_et_iso,
                        }
                        if _exit > 0:
                            _pnl = float(_pb_rec["realized_pnl"] or 0)
                            # 2026-08-20 净口径切换 (用户裁"切"): 对账回填同样用净PnL/净判定,
                            #   与 _close_trade_record 的 outcome 契约一致. pnl_net 缺→回退毛.
                            _pnl_net_ah = _pb_rec["pnl_net"]
                            _inst_pnl_ah = round(float(_pnl_net_ah), 2) if _pnl_net_ah is not None else round(_pnl, 2)
                            _upd.update({
                                "exit_price": _exit,
                                "exit_time": _pb_rec["exit_time"] or "",
                                "exit_reason": _pb_rec["exit_reason"] or "unknown",
                                "pnl": _inst_pnl_ah,
                                "pnl_pct": round(float(_pb_rec["pnl_pct"] or 0), 2),
                                "outcome": "win" if _inst_pnl_ah > 0 else "loss",
                            })
                        else:
                            _upd["outcome"] = "holding"
                        db.update_instruction_tracking(inst["id"], _upd)
                        logger.info(
                            f"[playbook] {_symbol} broker已有持仓 (playbook自成交), "
                            f"回填 entry=${_entry:.2f} outcome={_upd['outcome']}, 跳过重复下单"
                        )
                    else:
                        logger.info(f"[playbook] {_symbol} broker已有持仓 (外部/手动/ORB), 跳过 (防重复下单)")
                        # 2026-11-03 台账口径: 直写非 BOOKED 终态 (幻影行), 见
                        #   db.py EXISTING_POSITION_STATE 注记。executed=1 仍为
                        #   "已认领/不得重写" 闩; 读数面用 is_phantom_execution() 剔除。
                        db.mark_instruction_existing_position(inst["id"], _now_et_iso)
                    skipped_count += 1
                    continue
            except Exception as _bh_err:
                # R1c (2026-07-19, DEB-round2 Ω.3-3): 预检 fail 模式由 risk.preflight_fail_mode 驱动.
                #   closed → 跳过该仓 (broker 持仓未知时不重复下单). SIM 默认 open.
                #   2026-10-17 审计: 配置不可读时按环境回落 — LIVE-capable 回落 closed
                #   (否则启动断言锁上的 fail-close 会在运行期异常时被悄悄放开), SIM 仍 open.
                from app.core.live_readiness import preflight_fail_mode as _preflight_fail_mode
                _r1c_mode = _preflight_fail_mode()
                if _r1c_mode == "closed":
                    logger.error(f"[playbook] broker持仓检查失败 (fail-close 跳过该仓): {_bh_err}")
                    skipped_count += 1
                    continue
                logger.debug(f"[playbook] broker持仓检查失败 (放行): {_bh_err}")

            # triggered 只记录历史首次触发，不能代替本轮量能、VWAP、时间窗复验。
            triggered = True if _mom_policy is not None else _check_intraday_trigger(scheduler, inst)
            if not triggered:
                continue
            if not inst.get("triggered"):
                db.update_instruction_tracking(inst["id"], {"triggered": 1})
                logger.info(f"[playbook] 盘中触发: {inst['symbol']} {inst['direction']}")

            # ── RFC-111 (2026-09-26 用户批准): 同日硬止损冷却闸 — 当日已按
            #    _HARD_STOP_REASONS 出场的标的, 拒绝同向重开 (9/24 ORCL 教训:
            #    09:30 隔夜跳空割肉 -$400 → 09:38 同向重开 → 09:51 再割 -$84).
            #    复用再入场通道现成常量 (SSOT), 主通道执行前首次挂门。
            #    回滚: playbook.same_day_hard_stop_cooldown 置 false + restart ftseek-us.
            if _playbook_cfg("same_day_hard_stop_cooldown", True):
                _cd_hit = _check_same_day_hard_stop_cooldown(
                    inst.get("symbol", ""), inst.get("direction", ""), today)
                if _cd_hit:
                    _cd_reason = (
                        f"same_day_hard_stop_cooldown({_cd_hit['prior_exit_reason']},"
                        f"禁同向重开,当日有效)")
                    logger.info(
                        f"[playbook] 🚫 {inst['symbol']} 同日硬止损冷却闸拦截: 当日已 "
                        f"{_cd_hit['prior_exit_reason']} 出场 "
                        f"({_cd_hit['prior_exit_time']}), 拒绝同向重开 {inst.get('direction')}")
                    try:
                        db.update_instruction_tracking(inst["id"], {"skip_reason": _cd_reason})
                    except Exception as _cd_sr_err:
                        logger.warning(
                            f"[playbook] cooldown skip_reason 回写失败 "
                            f"{inst.get('symbol')} (不影响拦截): {_cd_sr_err}")
                    _emit_exec_gate_audit(inst, action="skipped_hard_stop_cooldown",
                                          entry=float(inst.get("entry_price") or 0),
                                          size=float(inst.get("position_size") or 0),
                                          notional=0.0,
                                          extra={"gate": "same_day_hard_stop_cooldown",
                                                 "prior_exit_reason": _cd_hit["prior_exit_reason"],
                                                 "prior_exit_time": _cd_hit["prior_exit_time"]})
                    skipped_count += 1
                    continue

            # ── RFC-068 (2026-08-29, 用户批准): B门硬门 — 接飞刀拒入场 ──
            # 证据 (8/10-8/28 影子×成交对账, RFC-068文档+8/29勘误节为准):
            #   飞刀区成交 4 笔净 -$439:
            #   UCTT pm-6.0%→SL -$145, BABA pm-5.6%→SL -$138, LRCX pm-5.5%→+$3,
            #   RBRK pm-3.4%→SL(26分钟) -$159.
            #   本门执行时刻口径(实时价/真昨收)反事实独立重算(139笔全窗口, 8/29):
            #   拦 4 笔 = BABA -6.20%/LRCX -5.58%/RBRK -5.41%/SNDK -5.48%,
            #   避损 $305.48, 误杀 LRCX +$2.51 → 净收益 +$302.97.
            #   注: UCTT 执行时刻已回升至 -2.85% 放行(其后盘中跌穿止损, 非缺口风险);
            #   RFC 初版表用盘前快照口径记"拦3省283", 与执行时刻口径不同, 见勘误节.
            # 语义: 执行时刻实时价 vs 昨收 < -5% → 拒入场; 若价格回升出飞刀区, 下轮自然复评.
            #   规则判定复用 premarket_invalidation.evaluate_rules (SSOT, 阈值唯一),
            #   影子版 record-only 继续采集对照, 本门只取 B_long_knife 命中转正为硬拦.
            # 数据缺失 (价/昨收≤0, 取数异常) → 放行 (质量门非安全门, 下游仍有 SL 兜底).
            # 回滚: playbook.premarket_invalidation_hard_gate 置 false + restart ftseek-us.
            if str(inst.get("direction", "")).upper() in ("BUY", "LONG") and _playbook_cfg(
                    "premarket_invalidation_hard_gate", True):
                _kg_block = False
                _chg_kg = None
                # [DEB-20260924-indexcaliber R4-D] 门判定值显式留痕 — 原缺陷: 审计行无
                # 飞刀门字段, 「未触门」只能由 action=executed 间接反推 (DEB §三.2 复查附注①,
                # E16 的间接性)。本 dict 挂到 inst (内存态, 不落库), 由 _emit_exec_gate_audit
                # 统一并入当轮所有 action 行 ⇒ executed/skipped_chase_cap 等也带门读数。
                _kg_audit: Dict[str, Any] = {
                    "evaluated": True, "fired": False, "rt_price": None,
                    "prev_close": None, "chg_pct": None, "threshold_pct": -5.0,
                }
                inst["_knife_audit"] = _kg_audit
                try:
                    _dr_kg = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
                    _ctx_kg = _dr_kg.get_full_market_context(inst["symbol"]) if _dr_kg is not None else None
                    if _ctx_kg:
                        _px_kg = float(_ctx_kg.get("price") or 0)
                        _pc_kg = float(_ctx_kg.get("prev_close") or 0)
                        if _px_kg > 0 and _pc_kg > 0:
                            _chg_kg = (_px_kg - _pc_kg) / _pc_kg * 100.0
                            _kg_audit.update(rt_price=round(_px_kg, 4),
                                             prev_close=round(_pc_kg, 4),
                                             chg_pct=round(_chg_kg, 2))
                            from app.playbook.premarket_invalidation import evaluate_rules as _kg_eval
                            if "B_long_knife" in _kg_eval(inst.get("direction"), _chg_kg):
                                _kg_block = True
                                _kg_audit["fired"] = True
                                logger.info(
                                    f"[playbook] 🚫 {inst['symbol']} B门硬门拦截: 现价{_px_kg:.2f} vs "
                                    f"昨收{_pc_kg:.2f} ({_chg_kg:+.1f}% < -5%) 接飞刀拒入场 (下轮复评)"
                                )
                except Exception as _kg_err:
                    _kg_audit["error"] = str(_kg_err)[:120]
                    logger.warning(f"[playbook] {inst['symbol']} B门硬门评估异常 (放行): {_kg_err}")
                if _kg_block:
                    # 2026-09-15 (用户裁「都做」批②): skip_reason 精确回写 — 照抄 chase_cap
                    # enforce 块模式 (留痕纪律 REVIEW-20260909 F-2: auto_execute 重评循环
                    # 不读它, 纯审计留痕)。原缺陷: 本门只写 exec_gate_audit jsonl 就 continue,
                    # 收盘 shadow_tracker 兜底盖误导标签"已触发未执行(资金护栏/时段/执行失败)"
                    # — 三个猜测全错 (真因=knife_gate), 且 no_trigger_counterfactual_replay
                    # 按 LIKE '已触发未执行%' 分桶会把飞刀拦截误归"资金护栏"桶污染归因。
                    # 9/14 实锤: BE(37拦)/SKHY(38拦)/NOK(8拦) 全部被盖错标签。
                    try:
                        db.update_instruction_tracking(inst["id"], {
                            "skip_reason": (
                                f"knife_gate_blocked(B_long_knife,chg="
                                f"{round(_chg_kg, 2) if _chg_kg is not None else '?'}%"
                                f"<-5%,下轮复评)")})
                    except Exception as _kg_sr_err:
                        logger.warning(
                            f"[playbook] knife_gate skip_reason 回写失败 "
                            f"{inst.get('symbol')} (不影响拦截): {_kg_sr_err}")
                    _emit_exec_gate_audit(inst, action="skipped_knife_gate",
                                          entry=float(inst.get("entry_price") or 0),
                                          size=float(inst.get("position_size") or 0),
                                          notional=0.0,
                                          extra={"gate": "B_long_knife",
                                                 "chg_pct": (round(_chg_kg, 2) if _chg_kg is not None else None)})
                    skipped_count += 1
                    continue

            # ── 2026-06-29: 触发后重新定价 (re-price at trigger) ──
            # 根因: 盘前生成的 entry_price 用的是盘前 bid/ask, 开盘后 gap 导致 entry/stop 失准
            # 修复: 触发瞬间取实时 bid/ask, 重算 entry_price + stop_loss, 传 custom_params 给 executor
            # DB 原值保留不动 (审计可追溯盘前价), custom_params 覆盖执行价
            _reprice_params = None
            _sl_clamp_meta = {}
            _atr_required = _mom_policy is None
            _atr_validated = _mom_policy is not None
            _ctx = None
            atr_pct = None
            try:
                _hp = pb_cfg.get_horizon_params(inst.get("horizon", "short"))
                if not _hp and _mom_policy is None:
                    # [RFC-185] 未知 horizon (如 "medium": playbook_horizon.yaml 只定义
                    #   short_horizon/long_horizon) 会让 get_horizon_params 返回 {} ⇒
                    #   re-price 静默回退固定 SL%(2.5/2.0) 且不落 vol_bucket。
                    #   实测当前生产 horizon 只有 short(151)/long(1) ⇒ 本分支 0 次;
                    #   加日志是让"日后 selector 产出新 horizon"不再无声降级 (零行为变更)。
                    logger.warning(
                        f"[playbook] {inst['symbol']} horizon={inst.get('horizon')!r} "
                        f"无对应 horizon 配置 (仅 short/long) → re-price 回退固定 SL%/TP 倍率")
                _atr_required = (_mom_policy is None
                                 and bool(_hp.get("vol_buckets") or pb_cfg.get("vol_buckets")))
                _dr = (None if _mom_policy is not None
                       else (getattr(scheduler, "data_router", None)
                             or getattr(scheduler, "market_data", None)))
                if _dr:
                    _ctx = _dr.get_full_market_context(inst["symbol"])
                    # ── [RFC-185 2026-11-06] 报价陈旧度门 ──
                    #   这里取到的 ctx 就是下面算 entry/SL/TP/size 的那一跳; 它过旧
                    #   ⇒ 止损距离按旧价定, 实际风险偏离桶值 (TCA 实测 >30s 档
                    #   |成交−执行| 中位 0.256% vs <10s 0.05%, max 0.49%)。
                    #   过龄不起价、下轮重试; 判据/阈值见 config 同名键注记 (RFC-185)。
                    #   判定本体 = data_health_guard.check_reprice_quote_age (可单测);
                    #   age 取不到 / 判定异常 ⇒ 放行 (fail-open, 与全文件口径一致)。
                    if not _reprice_quote_age_gate(
                            inst, _ctx, pb_cfg, db, logger, _emit_exec_gate_audit):
                        skipped_count += 1
                        continue
                    if _ctx and _positive_finite_atr(_ctx.get("price")) is not None:
                        _cur = float(_ctx["price"])
                        _bid = float(_ctx.get("lv3_bid") or _ctx.get("bid") or _cur)
                        _ask = float(_ctx.get("lv3_ask") or _ctx.get("ask") or _cur)
                        _dir = inst.get("direction", "")

                        # Phase 2/4 (2026-06-30): re-price 必须复用波动率分桶逻辑, 否则固定 SL/TP 会覆盖盘前生成的动态值
                        if _dir == "BUY":
                            _new_entry = _ask
                        else:
                            _new_entry = _bid

                        # [DEB-20260901 选项1② · ADR-035] archetype 路由 (默认关闭, 零行为变更):
                        #   PULLBACK_LIMIT/FADE_SHORT 指令 entry 锚定确认位, 下单走限价+TTL。
                        #   SL/TP/sizing 随之锚确认位 (与 DA thesis 语义一致)。
                        _arch_route = None
                        try:
                            from app.playbook.entry_routing import resolve_route
                            _arch_route = resolve_route(
                                inst.get("raw_instruction_json"), _dir, _new_entry, pb_cfg,
                                entry_price=inst.get("entry_price"))
                        except Exception as _ar_err:
                            logger.warning(
                                f"[playbook] {inst['symbol']} archetype 路由解析异常 (回退市价现状): {_ar_err}")
                            _arch_route = None
                        if _arch_route and _arch_route.get("action") == "skip":
                            db.update_instruction_tracking(inst["id"], {
                                "skip_reason": _arch_route.get("reason", "archetype_level_conflict")})
                            _emit_exec_gate_audit(inst, action="skipped_archetype_conflict",
                                                  entry=_new_entry, size=inst.get("position_size") or 0,
                                                  notional=0.0, extra={"route": _arch_route})
                            logger.warning(
                                f"[playbook] {inst['symbol']} archetype 价位矛盾: "
                                f"确认位 {_arch_route.get('limit_price')} vs 禁追线 "
                                f"{_arch_route.get('chase_cap_price')}, 终态跳过")
                            skipped_count += 1
                            continue
                        if _arch_route and _arch_route.get("action") == "limit":
                            logger.info(
                                f"[playbook] {inst['symbol']} archetype 路由: {_arch_route['archetype']} "
                                f"市价 ${_new_entry:.2f} → 确认位限价 ${_arch_route['limit_price']:.2f} "
                                f"TTL={_arch_route['ttl_seconds']:.0f}s")
                            _new_entry = float(_arch_route["limit_price"])

                        _atr = (_positive_finite_atr(_ctx.get("atr"))
                                or _positive_finite_atr(_ctx.get("atr_14")) or 0.0)
                        atr_pct = (_positive_finite_atr(_atr / _new_entry)
                                   if _positive_finite_atr(_new_entry) is not None else None)
                        if atr_pct is None:
                            _atr_percent = _positive_finite_atr(_ctx.get("atr_percent"))
                            atr_pct = (_positive_finite_atr(_atr_percent / 100.0)
                                       if _atr_percent is not None else None)
                        _atr_validated = atr_pct is not None
                        if _atr_required and not _atr_validated:
                            raise ValueError("valid ATR unavailable for configured volatility buckets")
                        atr_pct = atr_pct or 0.0

                        _vol_info = _compute_volatility_based_sl_tp(atr_pct, _dir, _hp, pb_cfg)
                        if _vol_info:
                            _sl_pct = _vol_info["sl_pct"]
                            _tp_mult = _vol_info["tp_multiplier"]
                        elif inst.get("stop_loss") and _new_entry > 0:
                            # 2026-07-16: ATR=0 → 从 instruction 原始 SL/TP 反算比例, 保持 vol_bucket 级别
                            _orig_sl = float(inst.get("stop_loss", 0))
                            _orig_tp = float(inst.get("take_profit", 0))
                            if _orig_sl > 0:
                                _sl_pct = abs(_new_entry - _orig_sl) / _new_entry * 100
                            else:
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5)) if _dir == "BUY" else float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            # TP: 用原始 TP 距离 / 新 entry 反算倍数
                            if _orig_tp > 0 and _orig_sl > 0:
                                _orig_sl_dist = abs(float(inst.get("entry_price", _new_entry)) - _orig_sl)
                                _orig_tp_dist = abs(_orig_tp - float(inst.get("entry_price", _new_entry)))
                                _tp_mult = _orig_tp_dist / _orig_sl_dist if _orig_sl_dist > 0 else float(_hp.get("rr_ratio", 1.5))
                            else:
                                _tp_mult = float(_hp.get("rr_ratio", 1.5))
                            logger.info(
                                f"[playbook] {inst['symbol']} re-price: ATR=0, 保留 instruction SL%={_sl_pct:.2f}% "
                                f"(从原始 SL=${_orig_sl:.2f} 反算)"
                            )
                        else:
                            # 未配置 vol_buckets 且无 instruction SL 时回退固定百分比
                            if _dir == "BUY":
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5))
                            else:
                                _sl_pct = float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            _tp_mult = float(_hp.get("rr_ratio", 1.5))

                        # ── 2026-08-07 (用户裁决: 再入场快收结构) ──
                        #   intraday_reentry- 前缀指令不走 vol_bucket: SL 放宽给跨夜空间,
                        #   ATR 相对按标的自适应 (quick_sl_pct).
                        #   2026-10-03 DEB-20261003: quick_tp 退役 (RR≈0.58 恒被 min_tp_rr=1.5
                        #   门禁吞, 物理零触发死参数) ⇒ re-price 同式复算 TP=0 (_tp_mult=0 →
                        #   _new_tp=0.0, 无固定TP腿) — 与被吞后的实际行为逐字一致, 零行为变更。
                        #   ⚠️ 不得回退 vol_bucket _tp_mult: 那会给 reentry 装上 RR≥1.5 的活 TP 腿。
                        #   用触发时刻 ATR 复算 (atr_pct 此处为分数口径 → ×100 转百分数);
                        #   异常/ATR缺失 → 不落本分支, 维持 vol_bucket 原逻辑 (保守).
                        try:
                            if str(inst.get("decision_id", "") or "").startswith("intraday_reentry-"):
                                from app.playbook.intraday_reentry import quick_sl_pct as _qsp
                                _q_sl = _qsp(atr_pct * 100.0 if atr_pct else None)
                                if _q_sl is not None:
                                    _sl_pct = _q_sl
                                    _tp_mult = 0.0   # 无固定 TP (下方 _tp_mult 假值分支 → _new_tp=0.0)
                                    logger.info(
                                        f"[playbook] {inst['symbol']} 再入场快收: "
                                        f"SL={_q_sl:.2f}% TP=无固定腿 (quick_tp 已退役, 骑 trailing/EOD)"
                                    )
                        except Exception:
                            pass

                        # ── 2026-07-17: 出价前 SL 钳制不变量 (ATR 无关, 越界必钳) ──
                        # 根因: DA/UE 直写指令绕过 vol_bucket; ATR 缺失时 fallback 用来源原始 SL
                        #   反算 sl_pct → 可能越过桶天花板 (MCK 6% / ABT 7.3% > ceil 5.0%)。
                        # 只堵越界: 已按桶钳过的合法值 (ceil ≤ max_ceil) 是 no-op; SHORT 含 short_widen。
                        _sl_pct_raw = _sl_pct
                        _sl_clamped = False
                        _bounds = _vol_bucket_sl_bounds(_hp, pb_cfg)
                        if _bounds:
                            _min_floor, _max_ceil, _max_widen = _bounds
                            _cap = _max_ceil + (_max_widen if _dir == "SELL_SHORT" else 0.0)
                            _sl_pct = max(_min_floor, min(_cap, _sl_pct))
                            if abs(_sl_pct - _sl_pct_raw) > 1e-6:
                                _sl_clamped = True
                                logger.info(
                                    f"[playbook] {inst['symbol']} SL 收口: "
                                    f"{_sl_pct_raw:.2f}%→{_sl_pct:.2f}% (桶上界 cap={_cap:.2f}%, 来源 SL 超界)"
                                )
                        _sl_clamp_meta = {
                            "sl_pct_raw": round(_sl_pct_raw, 3),
                            "sl_pct_final": round(_sl_pct, 3),
                            "sl_clamped": _sl_clamped,
                        }

                        # 计算新 stop_loss
                        if _dir == "BUY":
                            _new_sl = round(_new_entry * (1 - _sl_pct / 100), 2)
                        else:
                            _new_sl = round(_new_entry * (1 + _sl_pct / 100), 2)

                        # 处理 rr_ratio:999 哨兵 (禁用止盈); 否则基于波动率分桶的 tp_multiplier
                        if _tp_mult and _tp_mult < 100:
                            if _dir == "BUY":
                                _new_tp = round(_new_entry * (1 + _sl_pct * _tp_mult / 100), 2)
                            else:
                                _new_tp = round(_new_entry * (1 - _sl_pct * _tp_mult / 100), 2)
                        else:
                            _new_tp = 0.0

                        # 2026-07-09: position_size 兜底计算 — bridge直写DB时未计算仓位
                        # 公式: risk_amount / per_share_risk, 截断 max_position_pct
                        # 置信度加权 (设计原则): 期望值 = 胜率×盈亏比, 高置信信号 edge 更大 →
                        #   在正期望前提下按 Kelly 思想放大仓位, 低置信信号缩仓. 以 min_confidence
                        #   为 1.0 中性基准, 限幅 [0.6, 1.4] 防止单点误判造成极端仓位.
                        #   max_position_pct 仍是硬上限, confidence 只在其内部分配.
                        _inst_ps = inst.get("position_size") or 0
                        if _inst_ps <= 0 and _new_sl > 0:
                            _risk_base = _resolve_sizing_base(pb_cfg)  # R4 SSOT: 富途实时净值
                            _risk_pct = float(pb_cfg.get("risk_pct", 0.5))
                            _max_pos_pct = float(pb_cfg.get("max_position_pct", 0.06))
                            # F1 (2026-07-19): 补水位/regime 乘数, 与主路径 compute_position_size 对齐
                            #   (DEB-20260719-round2 Ω.3-4). 修复前兜底缺二者 → 回撤期仓位反向放大.
                            #   water_mult: capital_safety 水位减仓 (YELLOW/ORANGE), 单一真源同主路径.
                            #   regime_mult: MarketRegimeGate (live regime SSOT) + 主路径同款 regime 表.
                            _water_mult = 1.0
                            _regime_mult = 1.0
                            _regime_str_b1 = None  # B1 shadow 用: gate regime 标签 (assess 成功才填)
                            try:
                                from app.risk.capital_safety import get_capital_safety_system as _get_cs_f1
                                _cs_f1 = _get_cs_f1()
                                _water_mult = min(_cs_f1.get_position_multiplier(), _cs_f1.get_risk_multiplier())
                            except Exception as _wm_err:
                                # [RFC-168] fail-close: 取不到水位 ⇒ 仓位保护失效,
                                #   原实现停在初值 1.0 ⇒ 回撤期仓位**反向放大**
                                #   (DEB-20260719-round2 Ω.3-4 修的就是这个, 异常路径上复发)。
                                #   保守默认取 ORANGE 档 0.25 (既有档位, 非凭空值):
                                #   明显保守但不归零 —— 归零会造成"静默停止下单"的观感故障。
                                #   失败必告警, 不静默。
                                _water_mult = 0.25
                                logger.warning(
                                    f"[playbook] {inst.get('symbol')} 资本安全水位读取失败, "
                                    f"仓位乘数降为 ORANGE 保守档 0.25 (原 1.0): {_wm_err}",
                                    exc_info=True,
                                )
                            try:
                                from app.playbook.market_regime import MarketRegimeGate as _MRG_f1
                                _regime_str_f1 = _normalize_regime_str(_MRG_f1().assess())
                                _regime_str_b1 = _regime_str_f1
                                _regime_mult = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}.get(_regime_str_f1, 0.5)
                                # 2026-08-06 (用户指示, sim 采集期): cautious 不再降仓 — 数据采集优先.
                                #   原 0.5 与 LONG分歧0.5 相乘 → 名义仅 25%, 大量单被 min_notional=3000 物理拦截.
                                #   回滚: cautious_mult 改回 0.5 (或删本行恢复硬编码).
                                try:
                                    from app.core.config_container import get_config as _get_cfg_cm
                                    _cautious_mult = float(_get_cfg_cm().get_dotted("playbook.risk_parameters.cautious_mult", 1.0) or 1.0)
                                    if _regime_str_f1 == "CAUTIOUS" and _cautious_mult != 0.5:
                                        _regime_mult = {"RISK_ON": 1.0, "CAUTIOUS": _cautious_mult, "RISK_OFF": 0.3, "NO_TRADE": 0.0}.get(_regime_str_f1, 0.5)
                                        logger.info(f"[playbook] {inst['symbol']} CAUTIOUS 仓位乘数覆写: 0.5→{_cautious_mult} (sim采集期)")
                                except Exception:
                                    pass
                            except Exception as _rg_err:
                                # [RFC-168] fail-close: regime 评估失败 ⇒ 原停在 1.0
                                #   (RISK_ON 全额)。保守默认取 CAUTIOUS 档 0.5, 与上方
                                #   dict 的未知档默认值一致, 并告警。
                                _regime_mult = 0.5
                                logger.warning(
                                    f"[playbook] {inst.get('symbol')} regime 评估失败, "
                                    f"仓位乘数降为 CAUTIOUS 保守档 0.5 (原 1.0): {_rg_err}",
                                    exc_info=True,
                                )
                            _conf = float(inst.get("confidence", 0) or 0)
                            _conf_base = float(pb_cfg.get("min_confidence", 60) or 60)
                            # 2026-07-12: confidence 加权默认关闭 (拍平为 1.0). exit_scorecard 实测
                            #   当前 DA confidence 未校准且反向 (低信心桶反而更好, Playbook IC 为负) →
                            #   按 confidence 放大仓位 = 给更差的信号加钱. 校准为正前保持中性.
                            #   config: playbook.risk_parameters.confidence_sizing_enabled (默认 false).
                            _conf_factor = 1.0
                            if bool(pb_cfg.get("confidence_sizing_enabled", False)) and _conf > 0 and _conf_base > 0:
                                _conf_factor = max(0.6, min(1.4, _conf / _conf_base))
                            # 2026-07-16 P1 (2026-07-18 去黑盒): LONG "模型分歧" 降仓50%.
                            #   注意: 此处 conf<50 不是"低信心"语义 — DA confidence 未校准且反向
                            #   (低信心桶实测反而更好, 见上文 confidence_sizing 注释). conf<50 实际是
                            #   "2模型方向分歧" 的代理: DA 分歧写 conf=35, 一致写 ~70. 故变量命名为
                            #   _model_disagreement 而非"低信心", 避免误读为信心质量门.
                            #   数据依据: LONG 分歧单 38笔 39.5%WR -$2435 vs SHORT 23笔 43.5%WR +$1467 →
                            #   LONG 方向分歧系统性亏损. 修复: 降仓50% (不SKIP, 保留方向验证观测).
                            _model_disagreement = _conf < 50
                            if _dir == "BUY" and _model_disagreement:
                                # 2026-08-06 (用户指示, sim 采集期): LONG 分歧降仓 0.5 → 配置化, 默认 1.0 不降.
                                #   原 0.5 × CAUTIOUS 0.5 = 0.25 → 名义 < min_notional=3000 被物理拦截 → 交易量骤减.
                                #   分歧单仍保留日志/观测 (不 SKIP 语义不变); 回滚: 改回 0.5 + 重启.
                                try:
                                    from app.core.config_container import get_config as _get_cfg_ds
                                    _ds_factor = float(_get_cfg_ds().get_dotted(
                                        "playbook.risk_parameters.model_disagreement_downsize_factor", 1.0) or 1.0)
                                except Exception:
                                    _ds_factor = 1.0
                                _conf_factor *= _ds_factor
                                logger.info(
                                    f"[playbook] {inst['symbol']} LONG 模型分歧(conf={_conf}<50), 降仓{_ds_factor:.2f} "
                                    f"(conf_factor={_conf_factor:.2f})"
                                )
                            # F1: sizing SSOT 公共函数 (含 water_mult×regime_mult×conf_factor + 截断)
                            from app.playbook.instruction_generator import compute_position_size as _cps_f1
                            try:
                                from app.playbook.admission import get_admission_config as _get_adm_cfg_f1
                                _adm_cfg_fee = _get_adm_cfg_f1()
                            except Exception:
                                _adm_cfg_fee = {}
                            _inst_ps = _cps_f1(
                                _risk_base, _risk_pct, _new_entry, _new_sl, _max_pos_pct,
                                regime_mult=_regime_mult, water_mult=_water_mult, conf_factor=_conf_factor,
                                # [2026-11-06 RFC-186b] 费用可行下限: 与主路径同一开关,
                                #   否则兜底路径仍产出费用越线股数 (两路双标准)。
                                fee_floor=bool(_adm_cfg_fee.get("fee_floor_sizing", False)),
                                max_fee_bps=float(_adm_cfg_fee.get("max_fee_bps", 0) or 0),
                                min_notional_usd=float(_adm_cfg_fee.get("min_notional_usd", 0) or 0),
                                direction=_dir, trade_date=inst.get("date"),
                            )
                            # B1 shadow (DEB-feedback-loop, 2026-07-31): sizing-delta 对照 —
                            #   指令自带 regime 文本 (生成侧视角) vs MarketRegimeGate (sizing 实采)
                            #   + 各乘数与最终 size, 供 regime 统一裁决 (攒满10交易日) 供数. record-only.
                            try:
                                # 2026-07-31 复核补: 测试标的守卫 (07-30 遥测污染案军规 —
                                #   只读埋点必带, pytest 直调本段不得写生产 shadow)
                                if "TEST" in str(inst.get("symbol", "")).upper():
                                    raise StopIteration
                                from app.utils.timezone import now_et as _now_et_b1
                                _ri_b1 = inst.get("raw_instruction_json")
                                if isinstance(_ri_b1, str):
                                    try:
                                        _ri_b1 = json.loads(_ri_b1)
                                    except Exception:
                                        _ri_b1 = {}
                                _src_b1 = ((_ri_b1 or {}).get("source") if isinstance(_ri_b1, dict) else "") \
                                    or str(inst.get("decision_id") or "").split(":")[0]
                                _shadow_b1 = {
                                    "ts": _now_et_b1().isoformat(),
                                    "date": inst.get("date"),
                                    "symbol": inst.get("symbol"),
                                    "direction": _dir,
                                    "source": _src_b1,
                                    "inst_regime_text": inst.get("market_regime"),
                                    "gate_regime": _regime_str_b1,
                                    "gate_mult": _regime_mult,
                                    "water_mult": round(_water_mult, 4),
                                    "conf_factor": round(_conf_factor, 4),
                                    "size": _inst_ps,
                                }
                                _sp_b1 = Path(module_file).resolve().parents[2] / "data" / "shadow" / "sizing_delta.jsonl"
                                _sp_b1.parent.mkdir(parents=True, exist_ok=True)
                                with open(_sp_b1, "a") as _f_b1:
                                    _f_b1.write(json.dumps(_shadow_b1, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
                            if _inst_ps > 0:
                                logger.info(
                                    f"[playbook] {inst['symbol']} position_size兜底(F1 SSOT): "
                                    f"{_inst_ps}股 (water={_water_mult:.2f}×regime={_regime_mult:.2f}"
                                    f"×conf={_conf_factor:.2f}, 上限{int(_max_pos_pct*100)}%)"
                                )

                        _planned_ledger = build_price_ledger(inst)
                        _effective_risk_reward = calculate_risk_reward(
                            _dir, _new_entry, _new_sl, _new_tp)
                        _reprice_params = {
                            "entry_price": round(_new_entry, 2),
                            "stop_loss": _new_sl,
                            "take_profit": _new_tp,
                            # 2026-06-29: 必须传 position_size, 否则 execute_instruction
                            # 走 custom_params 分支但取不到 position_size → None → 降为1股
                            "position_size": _inst_ps or 1,
                            # 2026-07-13: risk_amount 派生落库 (显示/审计字段) = |entry-SL|×实际仓位.
                            #   此前 DA/bridge 路径 risk_amount 恒 NULL → 盘前推送 {risk:.0f} 曾崩
                            #   (已加 or 0 兜底); 此处补写真实值, 让推送/周报显示每笔风险敞口.
                            #   纯派生, 不影响 sizing 决策.
                            "risk_amount": round(abs(_new_entry - _new_sl) * (_inst_ps or 1), 2),
                            "risk_reward": _effective_risk_reward,
                            # 2026-06-29: 审计字段 — 记录 re-price 来源
                            "reprice_from": round(float(inst.get("entry_price", 0) or 0), 2),
                            "reprice_to": round(_new_entry, 2),
                            "reprice_original_sl": round(float(inst.get("stop_loss", 0) or 0), 2),
                        }
                        # [DEB-20260901 选项1②] 路由随下单参数透传 (update_instruction_tracking
                        #   白名单外键自动忽略, 仅 execute_instruction 消费)。
                        if _arch_route and _arch_route.get("action") == "limit":
                            _reprice_params["archetype_route"] = _arch_route
                        # 更新 DB 记录 re-priced 值 (审计可追溯)
                        db.update_instruction_tracking(inst["id"], _reprice_params)
                        # ── [P3.2 2026-09-13, DEB-20260910 §18.3] 让 re-price 审计真的落库 ──
                        #   上面 _reprice_params 里的 reprice_from/reprice_to/reprice_original_sl
                        #   三个键既不在 db._TRACKING_FIELDS (42 项) 白名单内、也不是
                        #   playbook_instructions 的 73 列之一 ⇒ 自 2026-06-29 起每次 re-price
                        #   都被 update_instruction_tracking 静默丢弃 (db.py:1532 注释甚至写着
                        #   "非 DB 列, 仍由 whitelist 正确忽略" —— 两侧注释意图相反, 净效果是
                        #   库里既无生成值也无"从哪重定价到哪")。
                        #   本块**不动 _reprice_params 一个键** (它随后还要原样传给
                        #   execute_instruction 的 custom_params, 见本函数 :4557), 只额外把
                        #   审计合并进 metadata_json.reprice_audit —— 零 schema 迁移。
                        #   补记 reprice_original_tp: 原三键只留了 SL 不留 TP, 而 §18.5 实测
                        #   已成交行里 12 行 take_profit<=0、其中 7 行是 SHORT (tp=0.0 在
                        #   SHORT 判据 tp<entry<sl 下恒"合法") ⇒ 不留原始 TP 就无法事后分辨
                        #   "生成时就没有 TP" 与 "重定价把 TP 算没了"。
                        #   失败不阻断下单: 审计降级优于丢单 (try/except 就地隔离)。
                        # 2026-11-06 (RFC-185): merge 是**顶层键覆盖**, 原实现每轮
                        #   整块替换 reprice_audit ⇒ 库里只留最后一跳, 累计漂移
                        #   (计划价→首次起价→末次起价) 无法从库重建, 只能翻日志。
                        #   这里把首跳与轮数随身带着走: first 只写一次, rounds 累加。
                        #   ⚠ 解析必须放在外层 try **之前**: 审计写入要直接位于 try 体内
                        #   (守卫 test_audit_failure_cannot_block_the_order 用"最近的 try
                        #   必须比最近的 except 更靠近 merge"判这件事; 中间夹一个解析用
                        #   try/except 会被它判成"不在 try 体内")。
                        _prev_rp = {}
                        try:
                            _md_rp = inst.get("metadata_json")
                            if isinstance(_md_rp, str):
                                _md_rp = json.loads(_md_rp or "{}")
                            if isinstance(_md_rp, dict):
                                _prev_rp = _md_rp.get("reprice_audit") or {}
                        except Exception:
                            _prev_rp = {}

                        try:
                            from app.utils.timezone import now_utc as _now_utc_rp
                            _rp_first = _prev_rp.get("first") or {
                                "from": _reprice_params["reprice_from"],
                                "to": _reprice_params["reprice_to"],
                                "stop_loss": _new_sl,
                                "take_profit": _new_tp,
                                "at": _now_utc_rp().isoformat(),
                            }
                            _rp_rounds = int(_prev_rp.get("rounds") or 0) + 1
                            _rp_written = db.merge_instruction_metadata(inst["id"], {
                                "reprice_audit": {
                                    # 首跳 / 轮数 (累计漂移可重建)
                                    "first": _rp_first,
                                    "rounds": _rp_rounds,
                                    "reprice_from": _reprice_params["reprice_from"],
                                    "reprice_to": _reprice_params["reprice_to"],
                                    "reprice_original_sl": _reprice_params["reprice_original_sl"],
                                    "reprice_original_tp": round(
                                        float(inst.get("take_profit", 0) or 0), 4),
                                    "planned_risk_reward": _planned_ledger["planned_risk_reward"],
                                    "effective_risk_reward": _effective_risk_reward,
                                    "reprice_stop_loss": _new_sl,
                                    "reprice_take_profit": _new_tp,
                                    "reprice_position_size": _inst_ps or 1,
                                    "reprice_risk_amount": _reprice_params["risk_amount"],
                                    "reprice_sl_pct": round(float(_sl_pct), 4),
                                    "reprice_tp_mult": round(float(_tp_mult), 4),
                                    "vol_bucket": (_vol_info or {}).get("bucket"),
                                    "atr_pct": round(float(atr_pct), 6),
                                    "audit_schema": "reprice_audit_v2",
                                    "at": _now_utc_rp().isoformat(),
                                },
                            })
                            if not _rp_written:
                                logger.debug(
                                    f"[playbook] {inst['symbol']} re-price 审计未写入 "
                                    f"(merge_instruction_metadata 返回 False, 已隔离)")
                        except Exception as _rp_audit_err:
                            # warning + exc_info, 不是 debug: W4_debug_swallow 把
                            #   except 内的 logger.debug 计为吞异常, W6_except_stack
                            #   要求带栈 ⇒ 这是同时满足两个棘轮的唯一形态。审计写不进
                            #   去本身就是要被看见的事 (静默死亡是本仓反复立案的病灶)。
                            logger.warning(
                                f"[playbook] {inst['symbol']} re-price 审计落库失败 "
                                f"(已隔离, 不影响下单): {_rp_audit_err}", exc_info=True)
                        # 2026-07-16: 盘中数据质量护栏 — 记录 re-price 时的数据完整性
                        _dq_flags = []
                        if _atr <= 0: _dq_flags.append("atr_missing")
                        if not _ctx.get("lv3_bid") or not _ctx.get("lv3_ask"):
                            _dq_flags.append("no_lv3_bidask")
                        if _dq_flags:
                            logger.warning(
                                f"[playbook] {inst['symbol']} re-price 数据质量: {', '.join(_dq_flags)} "
                                f"(使用 instruction 原始 SL 反算, 不影响执行)"
                            )
                        logger.info(
                            f"[playbook] re-price: {inst['symbol']} {_dir} "
                            f"盘前=${inst.get('entry_price', 0):.2f} → 实时=${_new_entry:.2f} "
                            f"SL={_new_sl:.2f} ({_sl_pct:.2f}%) TP={_new_tp:.2f}"
                        )
                        # 2026-07-12 L1: SL/TP 验证影子 (record-only, 不改任何交易动作)。
                        #   记录 LLM 原始 SL/TP vs 代码波动率桶 SL/TP 的分歧, 供日后与 pnl 联合校准。
                        #   自带 try/except, 绝不影响 reprice 与下单。
                        try:
                            from app.playbook.sltp_verify_shadow import record_sltp_shadow
                            record_sltp_shadow(
                                inst_id=inst.get("id"), symbol=inst.get("symbol", ""),
                                direction=_dir, entry=_new_entry,
                                llm_sl=inst.get("stop_loss", 0), llm_tp=inst.get("take_profit", 0),
                                code_sl=_new_sl, code_tp=_new_tp,
                                sl_pct=_sl_pct, bucket=(_vol_info or {}).get("bucket", ""),
                                atr_pct=atr_pct, regime=inst.get("market_regime", ""),
                            )
                        except Exception:
                            pass
            except Exception as _rp_err:
                logger.debug(f"[playbook] re-price 失败: {_rp_err}")

            if _mom_policy is not None:
                _mom_quote_why = ""
                try:
                    _dr_mom = (getattr(scheduler, "data_router", None)
                               or getattr(scheduler, "market_data", None))
                    _ctx = (_dr_mom.get_full_market_context(inst["symbol"])
                            if _dr_mom is not None else None)
                    if _dr_mom is None:
                        _mom_quote_why = "router_missing"
                except Exception as _mom_ctx_err:
                    logger.warning(f"[MOM-ETF-V1] {inst.get('symbol')} 报价获取失败: {_mom_ctx_err}",
                                   exc_info=True)
                    _ctx = None
                    _mom_quote_why = f"fetch_error:{_mom_ctx_err}"
                _mom_quote_px = _mom_quote_price(_ctx)
                if _mom_quote_px <= 0:
                    # 报价故障是暂时态，不能落成 MOM_FEE_SIZE_INFEASIBLE：
                    # guardrail_skip 会把 direction 改成 SKIP，永久错失当日调仓。
                    _mom_quote_why = (_mom_quote_why or "no_valid_ask_or_price")[:160]
                    _mom_skip_reason = f"MOM_QUOTE_UNAVAILABLE({_mom_quote_why})"
                    _record_mom_defer(
                        db, inst, today, _mom_skip_reason,
                        action="skipped_mom_quote_unavailable",
                        gate="mom_quote_available", why=_mom_quote_why,
                        logger=logger, emit_exec_gate_audit=_emit_exec_gate_audit)
                    skipped_count += 1
                    continue
                if not _reprice_quote_age_gate(
                        inst, _ctx, pb_cfg, db, logger, _emit_exec_gate_audit):
                    skipped_count += 1
                    continue
                # mom_reprice_params 的字段优先级与本提取器一致；用已验证价格
                # 规范化 lv3_ask，避免畸形高优先级字段在下游再次 float() 爆炸。
                _mom_ctx = dict(_ctx)
                _mom_ctx["lv3_ask"] = _mom_quote_px
                _reprice_params = mom_reprice_params(inst, _mom_ctx, today, _mom_policy)
                if _reprice_params is None:
                    db.mark_instruction_guardrail_skip(inst["id"], "MOM_FEE_SIZE_INFEASIBLE")
                    _emit_exec_gate_audit(
                        inst, action="skipped_mom_fee_size",
                        entry=float(inst.get("entry_price") or 0),
                        size=float(inst.get("position_size") or 0),
                        notional=0.0,
                        extra={"gate": "mom_fee_size"})
                    skipped_count += 1
                    continue
                try:
                    db.merge_instruction_metadata(inst["id"], {
                        "reprice_audit": {
                            "policy_id": _mom_policy.policy_id,
                            "reprice_from": _reprice_params["reprice_from"],
                            "reprice_to": _reprice_params["reprice_to"],
                            "reprice_original_sl": _reprice_params["reprice_original_sl"],
                            "reprice_stop_loss": _reprice_params["stop_loss"],
                            "reprice_take_profit": 0.0,
                            "reprice_position_size": _reprice_params["position_size"],
                            "audit_schema": "mom_reprice_audit_v1",
                        },
                    })
                except Exception as _mom_audit_err:
                    logger.warning(
                        f"[MOM-ETF-V1] {inst.get('symbol')} reprice 审计落库失败 "
                        f"(已隔离, 不影响下单): {_mom_audit_err}", exc_info=True)

            # 独立于重定价异常边界，落痕失败也不能放行缺 ATR 的新开仓。
            if _atr_required and not _atr_validated:
                logger.warning(f"[playbook] {inst['symbol']} ATR不可用，本轮暂缓开仓")
                try:
                    db.update_instruction_tracking(inst["id"], {
                        "skip_reason": "reprice_atr_unavailable"})
                    _emit_exec_gate_audit(
                        inst, action="deferred_atr_unavailable", entry=0.0,
                        size=inst.get("position_size") or 0, notional=0.0)
                except Exception as _atr_audit_err:
                    logger.warning(f"[playbook] ATR暂缓落痕失败 (仍跳过): {_atr_audit_err}")
                skipped_count += 1
                continue

            # ── 2026-10-23 统一 admission（用户裁「统一 admission」）──
            # 写入时 admission 只做估算预判；run_playbook_generate 只覆盖 watchlist。
            # DA 行必须在真实 re-price/尺寸确定后过一次同一门，否则小名义额/低价
            # 行可从旁路进入下单。阈值与 enforce 配置不变，只补齐执行时点。
            try:
                _exec_adm = execution_admission_evaluate(inst, _reprice_params)
            except Exception as _exec_adm_err:
                _exec_adm = None
                logger.warning(
                    f"[playbook] {inst.get('symbol')} execution admission 判定异常 "
                    f"(fail-open): {_exec_adm_err}", exc_info=True)
            if (_exec_adm
                    and _exec_adm.get("verdict") == "REJECT"
                    and _exec_adm.get("enforce")):
                _exec_adm_reason = "admission: " + "; ".join(
                    _exec_adm.get("reasons") or [])
                _exec_adm_metrics = _exec_adm.get("metrics") or {}
                try:
                    db.mark_instruction_guardrail_skip(
                        inst["id"], _exec_adm_reason)
                except Exception as _exec_adm_mark_err:
                    logger.warning(
                        f"[playbook] {inst.get('symbol')} admission skip 落库失败 "
                        f"(仍拦截): {_exec_adm_mark_err}", exc_info=True)
                try:
                    _emit_exec_gate_audit(
                        inst, action="skipped_admission_execution",
                        entry=float(_exec_adm_metrics.get("entry_price") or 0),
                        size=float(_exec_adm_metrics.get("shares") or 0),
                        notional=float(_exec_adm_metrics.get("notional") or 0),
                        extra={"gate": "admission_execution",
                               "reasons": _exec_adm.get("reasons") or [],
                               "metrics": _exec_adm_metrics})
                except Exception as _exec_adm_audit_err:
                    logger.warning(
                        f"[playbook] {inst.get('symbol')} admission 审计失败 "
                        f"(不影响拦截): {_exec_adm_audit_err}", exc_info=True)
                logger.info(
                    f"[playbook] {inst.get('symbol')} DA 指令执行前 admission "
                    f"REJECT: {_exec_adm_reason} "
                    f"(notional={_exec_adm_metrics.get('notional')}, "
                    f"fee_bps={_exec_adm_metrics.get('fee_bps')})")
                skipped_count += 1
                continue

            # ── [DEB-20260901 Phase B] chase_cap 违例遥测 (record-only) ──
            # DA 写明"禁止在$X以上追涨"但 re-price 把入场位顶过禁追线 →
            #   语义倒挂实锤采样。Phase B 仅记录不拦截; Phase C 才谈阻断。
            # [P0-A 2026-09-04, REVIEW-20260904 用户裁"按建议执行"]:
            #   chase_cap_enforce_enabled=true 时违例指令终态跳过 (见块尾)。
            # G-06 (2026-09-05 审计): 本闸门从 re-price try (上方 :3901→except _rp_err) 内
            #   **提出为独立门**。原缺陷 = 两条静默跳过路径, 使 P0-A 禁追高闸在异常下等于不存在:
            #   ① 块内任何异常 (含 entry_routing 导入失败) 被自身 except 吞成 warning → 照常下单;
            #   ② re-price 早段任何异常 → 外层 except 仅 debug, 本闸门根本不被求值,
            #      指令按 DB 原始价执行 (而"禁追线"语义恰应对真实下单价生效)。
            #   提出后判定对象 = "即将真实下单的价格": re-price 成功取 _reprice_params
            #   (与下方 custom_params=_reprice_params 消费口径同源), 失败取 DB 原值。
            #   纪律点保持: 遥测写盘先于 enforce、enforce 在去重门之外 (test_chase_cap_enforce
            #   源级契约), skip_reason 前缀 chase_cap_enforced(reprice= 与旧行兼容不改。
            #   回滚: chase_cap_enforce_enabled=false 退回纯遥测, 或 git revert 本块。
            # 开关先行独立读取 — 读不到取保守方向 (视为 ON): T2 "读取失败=保守方向"。
            #   跳过一笔违例单是安全的, 放行一笔追高单不是。
            _cc_enforce = True
            try:
                from app.playbook.entry_routing import (
                    chase_cap_enforce_enabled as _cc_enforce_f,
                )
                _cc_enforce = _cc_enforce_f()
            except Exception as _cc_sw_err:
                logger.warning(
                    f"[playbook] chase_cap enforce 开关读取失败 {inst.get('symbol')} "
                    f"→ 保守视为 ON: {_cc_sw_err}")
            _cc_gate_ok = False
            try:
                # 2026-09-01 修复: raw_instruction_json 经 db._row_to_dict
                #   已是 dict, json.loads(dict) 抛 TypeError (当日每轮刷屏)。
                #   parse_raw_semantics 天然兼容 str|dict。
                from app.playbook.entry_routing import (
                    parse_raw_semantics,
                    chase_cap_violated as _cc_violated,
                )
                _sem_cc = parse_raw_semantics(inst.get("raw_instruction_json"))
                _cc_f = _sem_cc["chase_cap_price"]
                # G-06: 真实下单价 SSOT — custom_params 非空即覆盖 DB entry_price。
                #   变量名沿用 _dir/_new_entry (源级契约 + 本循环下游不再消费, 无遮蔽风险)。
                _dir = inst.get("direction", "")
                _new_entry = float((_reprice_params or {}).get("entry_price")
                                   or inst.get("entry_price") or 0.0)
                # ── [RFC-120 2026-09-30] 倒挂 cap 归一化 (执行层防线, 与 DA skill 写入侧同源) ──
                # 病灶: LLM 把 SHORT 的 cap 填成 invalidation 上破失效位 (上界语义) →
                #   下单价天然低于它 → 出生即违例死锁 (9/4 后 born-violated 空头 18/18
                #   零执行 vs 合规组 24/33; 9/30 BE/KMX/TSLA 全灭, TSLA 空转 37+ 轮)。
                #   BUY 侧同病 18 条零执行 (cap<=entry 恒违例) — 归一化方向对称。
                # 处置: 倒挂值零信息增量 (上破失效位已由 stop_loss/invalidation 承担) →
                #   确定性退回入场带顺势沿 (=生成入场价×(1∓CHASE), 与 UE 约定同口径)。
                # 锚纪律: 必须用**生成时**入场价, 不可用 DB entry_price 列 (reprice 逐轮
                #   覆盖, UE P3.1 实证) — 用现价当锚会让 cap 追着价格跑 → 门恒放行 = 静默
                #   废闸; 也不可用 band 沿当锚直接喂归一 (entry_low 已含 ×0.95, 二次折价,
                #   且倒挂判定必须对生成入场价而非 band 沿, 否则合规 cap∈(entry_low,entry)
                #   被误判)。优先级: ① raw.planned_entry (生成值, DA直写/UE P3.1 后落库)
                #   ② band 沿反推生成价 (entry_low/(1-CHASE) | entry_high/(1+CHASE),
                #   "reprice 永不覆盖"列) ③ confirmation.price_level (首批限价≈生成入场)。
                #   全部缺失 → 不归一 (维持原 cap 原行为, 宁可继续拦也不静默放行)。
                # 异常 fail-open 到原值 (归一失败维持现状拦截, 不放行未判定单)。
                _cc_orig = _cc_f
                _cc_normed = False
                _gen_cc = None  # 2026-10-17: 预置 (下面 try 内赋值; 异常时保持 None, 供 skip_reason 的 gen= 段安全短路)
                if _cc_f is not None:
                    try:
                        from app.playbook.entry_band import (
                            normalize_chase_cap as _cc_norm,
                            CHASE_ZONE_PCT as _cc_chase_pct,
                        )
                        _rawj_cc = inst.get("raw_instruction_json")
                        if isinstance(_rawj_cc, str):
                            try:
                                _rawj_cc = json.loads(_rawj_cc or "{}")
                            except Exception:
                                _rawj_cc = {}
                        if not isinstance(_rawj_cc, dict):
                            _rawj_cc = {}
                        _is_short_cc = "SHORT" in str(_dir or "").upper()
                        _gen_cc = _rawj_cc.get("planned_entry")
                        if not _gen_cc:
                            _edge_cc = (inst.get("entry_low") if _is_short_cc
                                        else inst.get("entry_high"))
                            try:
                                _gen_cc = (float(_edge_cc) / (1 - _cc_chase_pct)
                                           if _is_short_cc
                                           else float(_edge_cc) / (1 + _cc_chase_pct)
                                           ) if _edge_cc else None
                            except (TypeError, ValueError):
                                _gen_cc = None
                        if not _gen_cc:
                            _conf_cc = _sem_cc.get("confirmation") or {}
                            if isinstance(_conf_cc, dict):
                                _gen_cc = _conf_cc.get("price_level")
                        if _gen_cc:
                            _cc_f, _cc_normed = _cc_norm(_dir, _gen_cc, _cc_f)
                        if _cc_normed and str(inst.get("id")) not in _CC_NORM_LOGGED:
                            if len(_CC_NORM_LOGGED) >= _CC_NORM_LOGGED_MAX:
                                _CC_NORM_LOGGED.clear()  # 有界防增长, 清空重报可接受
                            _CC_NORM_LOGGED.add(str(inst.get("id")))
                            logger.warning(
                                f"[playbook] chase_cap倒挂归一: {inst.get('symbol')} {_dir} "
                                f"原cap=${_cc_orig} (违反方向语义: "
                                f"{'SHORT cap必须<入场' if _is_short_cc else 'BUY cap必须>入场'}) "
                                f"→ 归一为入场带顺势沿 ${_cc_f} (RFC-120, 每指令只报一次)")
                    except Exception as _ccn_err:
                        logger.warning(
                            f"[playbook] chase_cap 归一化异常 {inst.get('symbol')} "
                            f"(维持原 cap 判定): {_ccn_err}")
                if _cc_violated(_dir, _new_entry, _cc_f):
                    from pathlib import Path as _Pcc
                    from datetime import datetime as _dtcc
                    _cc_path = _Pcc(module_file).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
                    # DEB-20260903 议题一 (方案A, 用户裁"按建议执行"): warning 移出去重门 —
                    #   每次违例 reprice 都打 (9/2 DELL 二次更糟违例 464.41→474.27 被
                    #   去重门静默吞没, 而 n_chase_cap_violated 是 Phase C 裁决证据列,
                    #   系统性少记=尺子偏向"违例罕见"); jsonl 行按 (inst_id, 值) 去重 —
                    #   同值重报不刷屏, 更差 reprice 记新行。行 type 保持
                    #   "chase_cap_violation" 不变 (replay 按存在性判, 零改动)。
                    logger.warning(
                        f"[playbook] chase_cap违例遥测: {inst['symbol']} 下单价=${_new_entry:.2f} "
                        f"越过禁追线 ${_cc_f:.2f}"
                        f"{f' (倒挂cap已归一, 原cap=${_cc_orig:.2f})' if _cc_normed else ''}"
                        f" (enforce={'ON' if _cc_enforce else 'OFF'}"
                        f"{'' if _reprice_params else ', re-price未生效判定用DB原值'})")
                    # G-06: 遥测写盘独立 try — 写盘失败 (磁盘满/权限) 不得冒泡到外层 except,
                    #   否则 _cc_gate_ok=False 会让下方 fail-close 误杀一笔"已判定未违例"或
                    #   "已判定违例应拦截"的指令 (前者误伤合法交易, 后者漏拦)。判定与留痕
                    #   分离: 判定失败→fail-close 不下单; 留痕失败→照判定结果执行。
                    #   次序纪律不变 (commit 仍先于 enforce, 源级契约测试锚点保持)。
                    try:
                        _cc_claim = _shadow_dedup_claim(_cc_path, inst.get("id"), f"chase_cap_violation:{round(_new_entry, 2)}")
                        if _cc_claim is not None:
                            _row_cc = {
                                "ts": _dtcc.now(_now_et()).isoformat(timespec="seconds"),
                                "date": _today_et_str(), "schema": "v1",
                                "type": "chase_cap_violation",
                                "inst_id": inst.get("id"),
                                "decision_id": inst.get("decision_id"),
                                "symbol": inst.get("symbol"), "direction": _dir,
                                "entry_archetype": _sem_cc["entry_archetype"],
                                "chase_cap_price": _cc_f,
                                # [RFC-120] 归一化留痕: cap_inverted=出生倒挂, orig_cap=归一前原值 —
                                #   replay/月报可切分"真追价违例"与"倒挂归一后仍违例"两人群。
                                "cap_inverted": bool(_cc_normed),
                                "orig_cap_price": (_cc_orig if _cc_normed else None),
                                "repriced_entry": round(_new_entry, 4),
                                # P0-A: 行级标记违例时 enforce 是否生效 —
                                #   replay/月报可区分"记录期违例"与"拦截期违例"。
                                "enforced": bool(_cc_enforce),
                                # G-06: 判定价来源 — re-price 失败路径判的是 DB 原值,
                                #   Phase C 复算须能区分 (否则违例率分母口径混淆)。
                                "reprice_ok": bool(_reprice_params),
                            }
                            _cc_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(_cc_path, "a", encoding="utf-8") as _f_cc:
                                _f_cc.write(json.dumps(_row_cc, ensure_ascii=False) + "\n")
                            # DEB-20260904 #175: 写盘成功后才提交键
                            _shadow_dedup_commit(_cc_claim)
                    except Exception as _cc_w_err:
                        logger.warning(
                            f"[playbook] chase_cap遥测写盘失败 {inst.get('symbol')} "
                            f"(不影响 enforce 判定/拦截): {_cc_w_err}")
                    # ── P0-A enforce: 本轮跳过 (非终态, 每~5min重评) ──
                    # 位置纪律: 在遥测写盘之后 (证据先落) 且在去重门之外
                    #   (enforce 不被同值去重吞掉; 每轮重评独立判定 — 价格回落帽下
                    #    下轮即放行, 9/8 SNDK 正是因此成交; SKHY 同指令可拦数十轮)。
                    # skip_reason 落库为拦截留痕; auto_execute 重评循环不读它 (REVIEW-20260909 F-2)。
                    if _cc_enforce:
                        # 2026-10-17 归因: 追加 gen=(生成锚)。使两类死因可**按标签**区分, 无需回解 raw:
                        #   ①真追高 (帽相对生成锚违规/或价格真涨穿自定容忍带) ②"计划锚被 reprice 抛弃"
                        #   (cap 对生成锚合规, 但 entry 被逐轮上调后落到锚的错误一侧 ⇒ 出生合规、漂移后死锁)。
                        #   实测 since 9/1 帽终态杀死 39 条中 28 条 (72%) 属 ② (如 REAL gen=9.30/
                        #   reprice=10.48/cap=9.50 整日 0 成交; SNDK gen=1535/reprice=1596.49/cap=1580)。
                        #   前缀 `chase_cap_enforced(reprice=` 逐字不变 (test_chase_cap_enforce / 
                        #   test_chase_cap_round_counter 按该前缀做源码契约断言); 取不到生成锚时省略 gen 段。
                        _cc_gen_txt = ""
                        try:
                            if _gen_cc:
                                _cc_gen_txt = f",gen={round(float(_gen_cc), 2)}"
                        except (TypeError, ValueError):
                            _cc_gen_txt = ""
                        db.update_instruction_tracking(inst["id"], {
                            "skip_reason": (
                                f"chase_cap_enforced(reprice={round(_new_entry, 2)},"
                                f"cap={_cc_f}{_cc_gen_txt})")})
                        _emit_exec_gate_audit(
                            inst, action="skipped_chase_cap",
                            entry=_new_entry, size=inst.get("position_size") or 0,
                            notional=0.0,
                            extra={"chase_cap_price": _cc_f,
                                   "cap_inverted": bool(_cc_normed),
                                   "repriced_entry": round(_new_entry, 4)})
                        # 轮次计数在 enforce 动作**之后**取 (纯观测, 不得影响上面的判定/落痕)
                        _cc_rounds = _chase_cap_round_bump(inst["id"])
                        # [RFC-120] 文案方向分流: 原「回落帽下即放行」对 SHORT 说反 —
                        #   SHORT cap=下限, 要**涨回帽上**才放行。误导文案让值班人把
                        #   结构性死锁误读成"等回落"。
                        _cc_release = ("涨回帽上即放行" if str(_dir).upper() != "BUY"
                                       else "回落帽下即放行")
                        logger.warning(
                            f"[playbook] chase_cap拦截: {inst['symbol']} {_dir} "
                            f"下单价=${_new_entry:.2f} 越过禁追线 ${_cc_f:.2f} "
                            f"→ 本轮跳过 (每~5min重评, {_cc_release}; "
                            f"同指令累计第{_cc_rounds}轮; P0-A enforce)")
                        skipped_count += 1
                        continue
                _cc_gate_ok = True
            except Exception as _cc_err:
                logger.warning(f"[playbook] chase_cap遥测异常 {inst.get('symbol')}: {_cc_err}")
            # G-06 fail-close: enforce=ON 而闸门自身未能完成判定 (解析/写盘/落库异常) →
            #   无法证明"未违例", 保守不下单。enforce=OFF 保持旧的纯遥测行为不拦截。
            #   落痕失败也不放行 — 跳过动作无条件执行。
            if not _cc_gate_ok and _cc_enforce:
                logger.critical(
                    f"🚨 [playbook] chase_cap闸门不可用 → fail-close 终态跳过 "
                    f"{inst.get('symbol')} (enforce=ON 时无法证明未违例, 保守不下单)")
                try:
                    db.update_instruction_tracking(inst["id"], {
                        "skip_reason": "chase_cap_gate_unavailable"})
                    _emit_exec_gate_audit(
                        inst, action="skipped_chase_cap",
                        entry=0.0, size=inst.get("position_size") or 0,
                        notional=0.0, extra={"fail_close": True})
                except Exception as _cc_fc_err:
                    logger.warning(
                        f"[playbook] chase_cap fail-close 落痕失败 (仍跳过): {_cc_fc_err}")
                skipped_count += 1
                continue

            # 2026-07-10: 删除 confidence<65 执行层gate (G6 conf<70已覆盖, E1从不拦截)
            # 原代码: _min_confidence=65, if conf<65 → SKIP
            # 删除原因: G6(line 1065, conf<70)已过滤, conf≥70的信号到执行层必>65, E1冗余

            # P0 (2026-06-25): buying_power 总量检查 — 防止批量触发耗尽购买力
            # 2026-07-10 fix: 优先用 re-price 后的 _reprice_params (含兜底计算的 position_size),
            # 否则 bridge 候选 position_size=None → _size=0 → _new_notional=0 → guardrail 全部空过
            if _reprice_params and _reprice_params.get("position_size"):
                _entry = _reprice_params.get("entry_price") or 0.0
                _size = _reprice_params.get("position_size") or 0
            else:
                _entry = inst.get("entry_price") or 0.0
                _size = inst.get("position_size") or 0
            _new_notional = _entry * _size if _entry > 0 and _size > 0 else 0.0
            if _bp_limit > 0 and (_max_deploy + _new_notional) > _bp_limit:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 购买力上限 "
                    f"(已用=${_max_deploy:,.0f} + 新=${_new_notional:,.0f} > 限=${_bp_limit:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_bp_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 2026-06-28: 单日 notional/数量 guardrail
            if _today_opened_notional + _new_notional > _daily_notional_cap:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日notional上限 "
                    f"(已用=${_today_opened_notional:,.0f} + 新=${_new_notional:,.0f} > 限=${_daily_notional_cap:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_notional_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            if _today_opened_count >= _max_daily_positions:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日开仓数上限 "
                    f"(已达到 {_max_daily_positions} 笔)"
                )
                _emit_exec_gate_audit(inst, action="skipped_max_positions", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            if _mom_policy is not None:
                _mom_reject = mom_budget_reject_reason(
                    _new_notional, _mom_open_count, _mom_today_notional,
                    _today_opened_notional, _today_opened_count,
                    _daily_notional_cap, _max_daily_positions,
                    _max_deploy, _bp_limit, _mom_policy,
                )
                if _mom_reject:
                    db.mark_instruction_guardrail_skip(inst["id"], _mom_reject)
                    _emit_exec_gate_audit(
                        inst, action="skipped_mom_budget",
                        entry=_entry, size=_size, notional=_new_notional,
                        extra={"gate": "mom_budget", "reason": _mom_reject})
                    skipped_count += 1
                    continue

            if _open_positions_count >= _max_open_positions:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 在仓标的数上限 "
                    f"(在仓 {_open_positions_count}/{_max_open_positions}, 平仓后下轮重算)"
                )
                _emit_exec_gate_audit(inst, action="skipped_open_position_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 2026-08-14 水坝效应修复: 单轮新开仓上限 (超出顺延下轮, triggered 状态不丢)
            if _cycle_new_count >= _max_new_per_cycle:
                logger.info(
                    f"[playbook] {inst['symbol']} 顺延 — 单轮新开仓上限 "
                    f"(本轮已开 {_cycle_new_count} 笔, 待下轮 auto_execute)"
                )
                _emit_exec_gate_audit(inst, action="deferred_cycle_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # ── [RFC-141] 入场延迟确认门 — arm / 放行 (execute 前最后一道) ──
            #   放在全部预算 guardrail 之后: 只在「本轮真会下单」时才 arm,
            #   否则被 BP/notional/单轮上限拦下的指令也会 arm ⇒ 白占 pending 名额
            #   且把「预算不足」误记成「延迟确认」样本 (污染判据人群)。
            #   锚取 reprice 后的执行锚 (_reprice_params.entry_price), 与影子裁决
            #   基准同源; 实测两锚分歧中位 2.10% ⇒ 用盘前计划锚 = 静默换尺子。
            if _mom_policy is None:
                try:
                    from app.playbook.entry_confirm_gate import (
                        gate_at_execute as _ec_gate, confirm_gate_config as _ec_cfg)
                    from app.utils.timezone import now_et as _ec_now_fn
                    # pb_cfg 注入: 与同函数其它门 (blackout/cooldown/chase_cap) 同一配置接缝
                    # v2 (RFC-141 §5.10): 传 scheduler ⇒ arm 瞬间快照 bid/ask 得 mid_arm
                    # (mid-mid 同尺判定的另一端); 取价失败自动回落 legacy 混尺 + ruler_used 留痕。
                    _ec_act = _ec_gate(inst, db, _reprice_params, _ec_now_fn(), _ec_cfg(pb_cfg),
                                       scheduler=scheduler)
                    if _ec_act == "defer":
                        _emit_exec_gate_audit(
                            inst, action="deferred_entry_confirm",
                            entry=float((_reprice_params or {}).get("entry_price")
                                        or inst.get("entry_price") or 0),
                            size=float((_reprice_params or {}).get("position_size")
                                       or inst.get("position_size") or 0),
                            notional=_new_notional,
                            extra={"gate": "entry_confirm_d2_forward", "rfc": "RFC-141"})
                        skipped_count += 1
                        continue
                except Exception as _ec_gate_err:
                    # fail-open: 门异常 ⇒ 照常执行 (维持基线, 不静默减少样本)
                    logger.warning(f"[EntryConfirm] arm 判定异常 (放行): {_ec_gate_err}",
                                   exc_info=True)

            # ── [RFC-152] 执行 TCA 台账 + 点差/报价年龄只读影子 ──
            #   只在通过全部既有闸门、本轮真会下单时快照到达报价; 执行后收口一行 jsonl。
            #   零行为变更: 所有异常只 warn, 不改变任何执行分支 (度量故障≠交易故障)。
            _tca_row = None
            _tca_result = None
            _tca_error = None
            _risk_row = None
            _risk_result = None
            _risk_error = None
            try:
                from app.playbook.execution_tca_shadow import (
                    build_attempt_row as _tca_build,
                    resolve_planned_entry as _tca_planned,
                    tca_config as _tca_cfg_fn,
                )
                _tca_cfg = _tca_cfg_fn(pb_cfg)
                if _tca_cfg.get("enabled"):
                    _tca_ctx = _ctx if isinstance(_ctx, dict) else None
                    _tca_quote_source = "reprice_ctx" if _tca_ctx is not None else "missing"
                    if _tca_ctx is None:
                        _dr_tca = (getattr(scheduler, "data_router", None)
                                   or getattr(scheduler, "market_data", None))
                        if _dr_tca is not None and hasattr(_dr_tca, "get_quote"):
                            try:
                                _tca_ctx = _dr_tca.get_quote(inst["symbol"])
                                _tca_quote_source = ("quote_fallback"
                                                     if isinstance(_tca_ctx, dict) else "missing")
                            except Exception as _tca_q_err:
                                logger.warning(
                                    f"[TCA] {inst.get('symbol')} 到达报价获取失败 "
                                    f"(不影响交易): {_tca_q_err}", exc_info=True)
                    _tca_plan, _tca_plan_src = _tca_planned(inst)
                    _tca_row = _tca_build(
                        inst=inst,
                        direction=inst.get("direction", ""),
                        planned_entry=_tca_plan,
                        planned_entry_source=_tca_plan_src,
                        execution_entry=((_reprice_params or {}).get("entry_price")
                                         or inst.get("entry_price")),
                        ctx=_tca_ctx,
                        quote_source=_tca_quote_source,
                        now_et=datetime_cls.now(_now_et()),
                        size=_size,
                        notional=_new_notional,
                        # 执行链 atr_pct 是分数口径 (0.025); TCA 契约是百分数 (2.5)
                        atr_pct=(atr_pct * 100.0 if isinstance(atr_pct, (int, float)) else None),
                        cfg=_tca_cfg,
                    )
            except Exception as _tca_build_err:
                logger.warning(f"[TCA] 行构建异常 (不影响交易): {_tca_build_err}",
                               exc_info=True)
                _tca_row = None

            # ── [RFC-155] 左尾风险尺寸前瞻记录影子 ──
            #   只记录 would_multiplier / counterfactual_size, 不改 _size 与实际下单。
            try:
                from app.playbook.risk_size_shadow import (
                    build_row as _rs_build,
                    risk_shadow_enabled as _rs_enabled,
                )
                if _rs_enabled(pb_cfg):
                    _risk_row = _rs_build(
                        inst=inst,
                        direction=inst.get("direction", ""),
                        # 执行链 atr_pct 是分数口径 (0.025); 影子契约是百分数 (2.5)
                        atr_pct=(atr_pct * 100.0 if isinstance(atr_pct, (int, float))
                                 else None),
                        size=_size,
                        notional=_new_notional,
                        entry_price=((_reprice_params or {}).get("entry_price")
                                     or inst.get("entry_price")),
                        now_et=datetime_cls.now(_now_et()),
                    )
            except Exception as _rs_build_err:
                logger.warning(f"[RiskSizeShadow] 行构建异常 (不影响交易): "
                               f"{_rs_build_err}", exc_info=True)
                _risk_row = None

            if _mom_policy is not None:
                if not entries_enabled():
                    continue
                if after_entry_cutoff(datetime_cls.now(_now_et()), _mom_policy):
                    db.mark_instruction_guardrail_skip(inst["id"], "MOM_SAME_DAY_EXPIRED")
                    skipped_count += 1
                    continue

            # 自动执行
            _audit_action = "error"
            try:
                result = service.execute_instruction(
                    instruction_id=inst["id"],
                    acknowledge_risk=True,  # Paper Trade 自动确认风险
                    custom_params=_reprice_params,  # 2026-06-29: 重新定价后的 entry/stop
                    requested_by="auto:playbook",
                )
                _tca_result = result
                _risk_result = result
                if result.get("success"):
                    executed_count += 1
                    _cycle_new_count += 1  # 2026-08-14 水坝效应修复: 单轮新开仓计数
                    _max_deploy += _new_notional  # 累加已用额度
                    _today_opened_notional += _new_notional
                    _today_opened_count += 1
                    _open_positions_count += 1
                    if _mom_policy is not None:
                        _mom_open_count += 1
                        _mom_today_notional += _new_notional
                    _audit_action = "executed"
                    logger.info(f"[playbook] ✅ 自动执行: {inst['symbol']} {inst['direction']}")

                    if live_context is not None:
                        live_instruction_id, live_gate = _mirror_instruction_to_live_if_allowed(
                            inst, live_context["db"]
                        )
                        if not live_gate.get("allowed"):
                            reason_messages = "; ".join(
                                reason.get("message", "unknown") for reason in live_gate.get("reasons", [])
                            )
                            live_skipped_count += 1
                            errors.append(
                                f"{inst['symbol']}: LIVE gate rejected mirror — {reason_messages}"
                            )
                            logger.info(
                                f"[playbook] LIVE mirror rejected before write: {inst['symbol']} "
                                f"reasons={[reason.get('code') for reason in live_gate.get('reasons', [])]}"
                            )
                            try:
                                from app.push import get_push_hub
                                from app.push.message import Level
                                get_push_hub().push_text(
                                    f"⚠️ [LIVE C-lite] {inst['symbol']} 未镜像: {reason_messages}",
                                    level=Level.HIGH,
                                )
                            except Exception as push_error:
                                logger.warning(
                                    f"[playbook] LIVE gate rejection push failed: {push_error}",
                                    exc_info=True,
                                )
                        else:
                            if live_instruction_id is None:
                                live_skipped_count += 1
                                errors.append(f"{inst['symbol']}: LIVE mirror failed")
                            else:
                                live_result = live_context["service"].execute_instruction(
                                    instruction_id=live_instruction_id,
                                    acknowledge_risk=True,
                                    custom_params=_reprice_params,
                                    target_env="real",
                                    requested_by="auto:playbook-live-mirror",
                                )
                                if live_result.get("success"):
                                    live_executed_count += 1
                                    logger.info(
                                        f"[playbook] ✅ LIVE C-lite 执行: {inst['symbol']} "
                                        f"decision_id={inst.get('decision_id')}"
                                    )
                                else:
                                    live_skipped_count += 1
                                    live_failure_marked = False
                                    if live_result.get("stop_protection_failed"):
                                        errors.append(
                                            f"{inst['symbol']}: LIVE C-lite 已成交但 STOP 保护失败 — "
                                            f"{live_result.get('message', 'unknown')}"
                                        )
                                    else:
                                        failure_reason = (
                                            f"LIVE_EXECUTION_REJECTED: "
                                            f"{live_result.get('message', 'unknown')}"
                                        )
                                        try:
                                            live_failure_marked = bool(
                                                live_context["db"].mark_instruction_guardrail_skip(
                                                    live_instruction_id,
                                                    failure_reason,
                                                )
                                            )
                                            live_context["db"].merge_instruction_metadata(
                                                live_instruction_id,
                                                {
                                                    "live_execution_failure": {
                                                        "reason": failure_reason,
                                                        "source_instruction_id": inst.get("id"),
                                                    }
                                                },
                                            )
                                        except Exception as mark_error:
                                            logger.error(
                                                f"[playbook] LIVE failure state mark failed "
                                                f"{inst['symbol']} id={live_instruction_id}: {mark_error}",
                                                exc_info=True,
                                            )
                                        errors.append(
                                            f"{inst['symbol']}: LIVE C-lite skipped — "
                                            f"{live_result.get('message', 'unknown')}"
                                        )
                                        if not live_failure_marked:
                                            errors.append(
                                                f"{inst['symbol']}: LIVE failure row not marked SKIP"
                                            )
                                    try:
                                        from app.push import get_push_hub
                                        from app.push.message import Level
                                        if live_result.get("stop_protection_failed"):
                                            get_push_hub().push_text(
                                                f"🔴 [LIVE C-lite] {inst['symbol']} 已成交但 STOP 保护失败: "
                                                f"{live_result.get('message', 'unknown')}",
                                                level=Level.HIGH,
                                            )
                                        else:
                                            get_push_hub().push_text(
                                                f"⚠️ [LIVE C-lite] {inst['symbol']} 未执行: "
                                                f"{live_result.get('message', 'unknown')}",
                                                level=Level.HIGH,
                                            )
                                    except Exception as push_error:
                                        logger.warning(
                                            f"[playbook] LIVE C-lite failure push failed: {push_error}",
                                            exc_info=True,
                                        )
                else:
                    if result.get("ttl_expired"):
                        # [DEB-20260901 选项1②] 路由限价 TTL 到期放弃 = 设计内终态, 非执行失败
                        skipped_count += 1
                        _audit_action = "skipped_ttl_expired"
                        logger.info(f"[playbook] ⏳ {inst['symbol']} 路由限价 TTL 到期放弃 (影子遥测已落)")
                    else:
                        # [RFC-169] 三态分账: 受理未确认 ≠ 跳过, 也 ≠ 成功。
                        #   实锤 2026-10-26 US.CAT: 21:41:02 受理 (fill=0) → 30s 确认窗走完仍
                        #   未确认 → 原实现记成"跳过"且原因只进返回 dict (日志无痕) ⇒ 读到
                        #   "0 成功" 而实际 21:41:23 已成交、21:41:55 由 order 回执 reconcile
                        #   补记 BOOKED。故先读 execution_state: 仍在途 ⇒ 单列待确认;
                        #   已回终态/无状态 ⇒ 才是真失败, 且必须落 WARNING 带上原因。
                        _exec_state = ""
                        try:
                            _inst_row = db.get_instruction_by_id(inst["id"]) or {}
                            _exec_state = str(_inst_row.get("execution_state") or "")
                        except Exception as _es_err:
                            logger.warning(f"[playbook] {inst['symbol']} execution_state 读取失败: {_es_err}", exc_info=True)
                        if _exec_state in ("SUBMITTED", "FILLED_UNBOOKED", "UNKNOWN"):
                            submitted_pending_count += 1
                            _audit_action = "submitted_unconfirmed"
                            logger.warning(
                                f"[playbook] ⏳ {inst['symbol']} 已受理待确认 "
                                f"(execution_state={_exec_state}) — {result.get('message', 'unknown')}"
                                f" · 不计成功也不计失败, 待 order 回执 reconcile 收口"
                            )
                        else:
                            skipped_count += 1
                            _audit_action = "skipped_fail"
                            errors.append(f"{inst['symbol']}: {result.get('message', 'unknown')}")
                            logger.warning(
                                f"[playbook] ⚠️ {inst['symbol']} 自动执行未成功 "
                                f"(execution_state={_exec_state or '-'}): "
                                f"{result.get('message', 'unknown')}"
                            )
            except Exception as e:
                skipped_count += 1
                _audit_action = "error"
                _tca_error = str(e)
                _risk_error = str(e)
                errors.append(f"{inst['symbol']}: {str(e)}")
                logger.warning(f"[playbook] 自动执行失败 {inst['symbol']}: {e}")
            # [RFC-152] TCA 收口: 成功/失败都落一行; 写盘失败只 warn, 不影响交易。
            if _tca_row is not None:
                try:
                    from app.playbook.execution_tca_shadow import (
                        finalize_and_record as _tca_flush,
                    )
                    _tca_flush(_tca_row, result=_tca_result, error=_tca_error)
                except Exception as _tca_flush_err:
                    logger.warning(f"[TCA] 收口异常 (不影响交易): {_tca_flush_err}",
                                   exc_info=True)
            # [RFC-155] 风险尺寸影子收口: 只写 jsonl, 不改交易。
            if _risk_row is not None:
                try:
                    from app.playbook.risk_size_shadow import (
                        finalize_and_record as _rs_flush,
                    )
                    _rs_flush(_risk_row, result=_risk_result, error=_risk_error)
                except Exception as _rs_flush_err:
                    logger.warning(f"[RiskSizeShadow] 收口异常 (不影响交易): "
                                   f"{_rs_flush_err}", exc_info=True)
            # 统一收口审计 (2026-07-17): 记录到达下单漏斗并被执行/失败的候选 (含 DA-bridge)
            _emit_exec_gate_audit(inst, action=_audit_action, entry=_entry,
                                  size=_size, notional=_new_notional,
                                  extra={
                                      "vol_bucket": inst.get("vol_bucket"),
                                      "atr_pct": inst.get("atr_pct"),
                                      **_sl_clamp_meta,
                                  })

        _pending_seg = f", {submitted_pending_count} 待确认" if submitted_pending_count else ""
        logger.info(
            f"[playbook] 自动执行完成: {executed_count} 成功{_pending_seg}, {skipped_count} 跳过"
        )
        # 2026-08-22 audit: 关闭 already_held 对账共享连接
        if _ah_conn is not None:
            try:
                _ah_conn.close()
            except Exception:
                pass
        # 盘中实时化: 广播轻量 nudge, 已连 dashboard 收到后自拉缓存接口刷新. 没人连时 broadcast 直接 return.
        try:
            from app.web_dashboard.routes.ws import publish_event_sync
            publish_event_sync("pipeline_update", {
                "date": today,
                "executed": executed_count,
                "submitted_pending": submitted_pending_count,
                "skipped": skipped_count,
                "errors": len(errors),
            })
        except Exception:
            pass  # WS 推送失败不影响交易主流程
        return {
            "date": today,
            "executed": executed_count,
            "submitted_pending": submitted_pending_count,
            "skipped": skipped_count,
            "live_executed": live_executed_count,
            "live_skipped": live_skipped_count,
            "errors": errors[:5],  # 最多返回 5 个错误
        }

    except Exception as e:
        logger.exception(f"[playbook] 自动执行异常 (已隔离): {e}")
        return {"error": str(e)}
