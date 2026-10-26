"""隔夜孤儿仓清理 (阶段14迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

from typing import Any, Callable

# ── EOD 兜底「可成交窗口」(2026-10-22 P0 TSLA 实锤, 见 eod_close_verdict docstring) ──
# 盘后窗口里下 RTH 市价腿必被券商撤 ⇒ 意图卡死 ⇒ 该标的进 blocked_exits ⇒ 规则链整行
# 跳过 (零保护)。实证: trade_records 里 EOD_FORCE_CLOSE 的券商成交 7/7 落在 16:0x。
_EOD_CLOSE_FILL_WINDOW_MIN = 10

# 延后日志去重 (symbol/env/date/action) — 本函数在 AFTER_HOURS 每 ~2min 被调用
_EOD_DEFER_LOGGED = set()


def _entry_et(entry_time) -> Any:
    """entry_time → ET datetime; 解析失败 → None。

    entry_time 在库中三种格式混存 (aware ET / naive CST / 空)。naive 按 app 约定
    视为 CST (app.utils.timezone.to_et), 不做裸日期比较 — 否则 CST 凌晨录入的
    盘后仓 (ET 前一日 16:xx) 会被算成"今日收盘前"入场。
    """
    if not entry_time:
        return None
    try:
        from datetime import datetime as _dt
        from app.utils.timezone import to_et as _to_et
        return _to_et(_dt.fromisoformat(str(entry_time)))
    except Exception:
        return None


def eod_close_verdict(now_et_dt, entry_time, *, phase=None, close_min=None):
    """EOD 兜底是否可**提交**平仓腿 → (action, detail)。

    action ∈ {"submit", "defer_fresh_ext", "defer_window"}；只决定"是否交腿",
    不改平仓决策、不改持仓/账本。

    背景 (2026-10-21 TSLA, P0): 16:45:23 EXT 建仓 → 16:45:53 本函数下 RTH 市价腿
    → 券商 CANCELLED_ALL dealt=0 → intent 卡 PENDING → 该标的进 blocked_exits ⇒
    SL/TP/时间/因子规则**整行跳过**(零保护静默期)直到人工释放。盘中规则链已由
    exit_monitor_flow 的 `phase != TRADING → return` 挡住, 本函数是唯一缺口。

    两条判据:
      ① 收盘后建仓的 EXT 仓不是"收盘孤儿" — 9/17 用户裁 EXT 仓可隔夜 (cohort B),
         交回 EXT 退出链与次日规则链; 否则会出现"建仓 30 秒后被 EOD 强平"。
      ② 只在 TRADING, 或收盘后 _EOD_CLOSE_FILL_WINDOW_MIN 分钟内提交腿。
    延后不降保护: 不建意图 ⇒ 不进 blocked_exits ⇒ 下一 TRADING 相位规则链照常
    评估 SL/TP/MAX_HOLD (卡死的意图反而会让规则链整行跳过)。
    """
    from app.core.scheduler_triggers import get_market_phase, MarketPhase

    if phase is None:
        phase = get_market_phase()
    if close_min is None:
        try:
            from app.utils.holiday_calendar import get_us_close_minutes_et
            close_min = int(get_us_close_minutes_et(now_et_dt.date()))
        except Exception:
            close_min = 960  # 兜底 16:00 ET, 与 get_us_close_minutes_et 约定一致
    _now_min = now_et_dt.hour * 60 + now_et_dt.minute

    _entry = _entry_et(entry_time)
    if (_entry is not None and _entry.date() == now_et_dt.date()
            and (_entry.hour * 60 + _entry.minute) >= close_min):
        return "defer_fresh_ext", (
            "今日收盘后建仓 (%s ET ≥ 收盘 %02d:%02d), 非收盘孤儿; "
            "交回 EXT/次日退出链 (9/17 裁决 EXT 仓可隔夜)"
            % (_entry.strftime("%H:%M"), close_min // 60, close_min % 60))

    if phase == MarketPhase.TRADING:
        return "submit", "TRADING 相位, 可成交"
    if phase == MarketPhase.AFTER_HOURS and close_min <= _now_min < close_min + _EOD_CLOSE_FILL_WINDOW_MIN:
        return "submit", "收盘后 %d 分钟窗口内, 可成交" % (_now_min - close_min)

    return "defer_window", (
        "%s %02d:%02d ET 不在可成交窗口 (TRADING 或 收盘+%dmin); 不提交必被撤的腿, "
        "仓位保留 SL/规则链, 下一 TRADING 相位接管"
        % (getattr(phase, "value", phase), _now_min // 60, _now_min % 60,
           _EOD_CLOSE_FILL_WINDOW_MIN))


def _force_close_orphaned_sim_positions(
    scheduler,
    *,
    logger: Any,
    _alert_close_failure: Callable,
    _book_playbook_exit_result: Callable,
    _cohort_b_max_hold_hit: Callable,
    _mark_overnight_arm: Callable,
    _playbook_exit_decision: Callable,
    _playbook_exit_owner: Callable,
    _release_live_broker_stop_for_rule_exit: Callable,
    _resume_playbook_exits: Callable,
    _today_et_str: Callable,
    _handle_live_rule_exit_failure: Callable | None = None,
) -> int:

    """EOD 兜底: 强制平仓所有未平仓的 PLAYBOOK_SIM/ORB_ENGINE 真实仓位 (via broker).

    与 _force_close_orphaned_shadow_positions 的区别: PLAYBOOK_SIM 是真实模拟持仓,
    必须调 broker.close_position 真实平仓, 不能只更新 DB.

    2026-06-24: 也覆盖 ORB_ENGINE 仓位 — ORB daemon 崩溃时, 其自管 EOD (_force_close_all)
    不执行, 仓位会孤儿化. 此函数作为 scheduler EOD 兜底, 用 DualBroker.close_position 平仓.
    ORB 在 sim 账户交易, target_env="sim".
    """
    conn = None
    closed = 0
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime

        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        closed, blocked_exits, recovery_ok = _resume_playbook_exits(
            allow_submit=get_market_phase() == MarketPhase.TRADING)
        if not recovery_ok:
            return closed

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type, futu_order_id
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'ORB_ENGINE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            return closed

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook dual] EOD 兜底跳过: 无 data_router")
            return closed

        from app.execution.dual_broker import get_dual_broker
        dual = get_dual_broker()

        for r in rows:
            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            try:
                if (_row_target, r["symbol"]) in blocked_exits:
                    continue
                symbol = r["symbol"]
                direction = r["direction"]
                entry_price = float(r["entry_price"] or 0)
                qty = r["quantity"] or 0
                if entry_price <= 0 or qty <= 0:
                    continue

                # RFC-061 v2: cohort B 盈利仓跳过 EOD broker 兜底 (持过夜); 5 交易日仍平 (MAX_HOLD_T5).
                # 仅 cohort B (RFC-130: SIM/LIVE 同语义, 原 cohort_b_skip_allowed LIVE 门已退役); ORB 强制平 (日内语义).
                _meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                if not isinstance(_meta, dict):
                    raise ValueError("trade metadata must be an object")
                _cohort = _meta.get("cohort", "A")
                # P2b 单点化 (2026-09-06): 两公式收进 exit_engine (与 poll monitor 同源).
                from app.playbook.exit_engine import (
                    cohort_b_skip_eligible as _cse,
                    overnight_master_switch_on as _master_on,
                )
                _skip_allowed = _cse(_cohort, r["source_type"])
                # 极简版B (2026-07-17): 隔夜总开关关闭 → EOD 全平, cohort B 盈利仓不跳过.
                if not _master_on():
                    _skip_allowed = False
                _eod_reason = "EOD_FORCE_CLOSE"
                if _skip_allowed and _cohort_b_max_hold_hit(r["entry_time"]):
                    _eod_reason = "MAX_HOLD_T5"  # 5 交易日硬上限, 仍须 broker 平仓
                elif _skip_allowed:
                    # 估算盈利决定是否跳过 (取当前报价)
                    _est_price = 0.0
                    try:
                        _q = data_router.get_quote(symbol)
                        if _q and isinstance(_q, dict):
                            _est_price = float(_q.get("last", 0) or _q.get("price", 0) or 0)
                    except Exception:
                        pass
                    if _est_price > 0:
                        _est_pnl_pct = ((_est_price - entry_price) / entry_price * 100) if direction == "LONG" \
                            else ((entry_price - _est_price) / entry_price * 100)
                        if _est_pnl_pct > 0:
                            logger.info(
                                f"[playbook sim] EOD 兜底跳过 {symbol} cohort=B profit~{_est_pnl_pct:.2f}% 持过夜 (RFC-061)"
                            )
                            continue
                    else:
                        # 2026-07-04: 报价缺失时不跳过 — 原逻辑已对, 但加显式日志便于诊断
                        logger.warning(
                            f"[playbook sim] EOD 兜底 {symbol} cohort=B 报价缺失 → 强制平仓 (保守)"
                        )
                    # 报价缺失或亏损 → 不跳过, 走兜底平仓 (保守)

                # P1 三轨合一 (2026-09-06): 轨③再入场首夜 (8/6 裁决) + W_WEEK SIM水下多夜
                #   (8/15 三问Q3) 收为单一决策点 overnight_eod_decision, 评估顺序与合并前
                #   逐字一致 (轨③先判); 台账 source 由决策返回, 值域同 RFC-067 契约。
                #   报价缺失 → 不豁免 (保守平仓); 次日 entry_date≠当日 → 轨③自动失效,
                #   W_WEEK 仍在 ≤5 交易日窗口内接管。
                # 2026-08-17 净口径: 豁免水下判定用净盈亏 (毛 0<毛<费用 的微盈仓=净水下→豁免,
                #   与盘中规则链同口径; 算不出费 → 毛口径原行为).
                # 2026-08-22 audit fix: _rp/_rpnl 每轮重置 — 此前仅 _rpnl_net 重置,
                #   若上一标的报价成功而当前标的 get_quote 抛异常, 下方第二 try 块会拿
                #   上一标的的价格/盈亏判定本标的的隔夜豁免 (跨持仓污染).
                #   重置后报价失败 → None → 不豁免 (保守平仓), 与注释纪律一致.
                _rp = _rpnl = _rpnl_net = None
                try:
                    _rq = data_router.get_quote(symbol)
                    _rp = float((_rq or {}).get("last", 0) or (_rq or {}).get("price", 0) or 0) if _rq else 0.0
                    if _rp > 0:
                        _rpnl = ((_rp - entry_price) / entry_price * 100) if direction == "LONG" \
                            else ((entry_price - _rp) / entry_price * 100)
                        # 净口径换算 (qty/entry 可得时): 往返费/名义额 → 毛-费率
                        try:
                            from app.execution.fee_model import estimate_round_trip as _eod_rt
                            _eod_fee = _eod_rt(qty, entry_price, _rp, direction)["total"]
                            _rpnl_net = _rpnl - _eod_fee / (qty * entry_price) * 100.0
                        except Exception:
                            _rpnl_net = None
                        from app.playbook.overnight_decision import overnight_eod_decision as _eod_dec
                        _eod_exempt, _eod_track = _eod_dec(
                            symbol, r["source_type"], r["entry_time"],
                            _rpnl_net if _rpnl_net is not None else _rpnl)
                        if _eod_exempt:
                            _mark_overnight_arm(conn, r["id"], _meta, _eod_track,
                                                _rpnl_net if _rpnl_net is not None else _rpnl)
                            # ── RFC-079 (2026-09-09): EOD 深水闸影子打点 (record-only, 豁免行为零变更) ──
                            try:
                                from app.playbook.overnight_decision import emit_eod_depth_gate_shadow as _edg_emit
                                _edg_emit("sweeper", symbol, direction, r["source_type"],
                                          entry_price, qty, r["entry_time"], _rp,
                                          _rpnl_net if _rpnl_net is not None else _rpnl,
                                          track=_eod_track,
                                          trade_id=r["id"],
                                          earnings_guard=getattr(scheduler, "earnings_risk_guard", None),  # RFC-095 身份锚
                                          peak_price=_meta.get("peak_price"))  # RFC-160 MFE 轨
                            except Exception as _edg_err:
                                # R5: 打点内层已全量收口, 本层只兜 import 级故障 — 必须留痕,
                                #   否则「影子零样本」与「打点链路断了」不可区分 (2026-09-09 棘轮清剿)
                                logger.warning(f"[playbook] EOD深水闸影子打点兜底异常 {symbol}: {_edg_err}")
                            if _eod_track == "reentry_first_night":
                                logger.info(
                                    f"[playbook sim] EOD 兜底豁免 {symbol} 再入场水下仓 {_rpnl:.2f}%"
                                    f"(净{_rpnl_net if _rpnl_net is not None else _rpnl:.2f}%) 持夜 (用户裁决)"
                                )
                            else:
                                logger.info(
                                    f"[playbook sim] EOD 兜底豁免 {symbol} 水下仓 {_rpnl:.2f}%"
                                    f"(净{_rpnl_net if _rpnl_net is not None else _rpnl:.2f}%) 持夜 (用户三问Q3)"
                                )
                            continue
                except Exception as _oex:
                    logger.debug(f"[playbook sim] 隔夜豁免判定异常 {symbol} (不豁免): {_oex}")

                # ── 2026-10-22 P0 (TSLA 10-21 实锤): 只在可成交窗口提交腿, 且不扫收盘后新建仓 ──
                # 位置刻意放在隔夜豁免判定之后: 豁免/深水闸影子打点(RFC-079)照常记录,
                # 本守卫只管"是否把腿交给券商", 不回滚任何豁免/持仓/账本状态。
                try:
                    from app.utils.timezone import now_et as _now_et_fn
                    _act, _why = eod_close_verdict(_now_et_fn(), r["entry_time"])
                except Exception as _gw_err:
                    # 判据异常 → 保守延后 (宁可不平, 不制造必被撤的卡单意图)
                    _act, _why = "defer_window", "窗口判据异常 (保守延后): %s" % _gw_err
                if _act != "submit":
                    _dday = _today_et_str()
                    _dk = (r["symbol"], _row_target, _dday, _act)
                    if _dk not in _EOD_DEFER_LOGGED:
                        _EOD_DEFER_LOGGED.add(_dk)
                        if len(_EOD_DEFER_LOGGED) > 200:  # 键含日期, 跨日自然换键
                            for _k in [k for k in _EOD_DEFER_LOGGED if k[2] != _dday]:
                                _EOD_DEFER_LOGGED.discard(_k)
                        logger.info(f"[playbook dual] EOD 兜底延后 {r['symbol']} "
                                    f"({_row_target}) action={_act}: {_why}")
                    continue

                _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
                # P1 fix (2026-06-25): 连续失败 3 次后当日放弃, 停止死循环日志刷屏
                # DEB-20260904 #178: 计数按日重置 — 旧逻辑跨日累计 ≥3 即永久 skip → 孤儿仓;
                # 且 skip 升级为 warning 明示需人工对账
                _fail_count = _meta.get("eod_close_fail_count", 0)
                _fail_date = _meta.get("eod_close_fail_date", "")
                if _fail_date != _today_et_str():
                    _fail_count = 0  # 跨日重置
                if _fail_count >= 3:
                    logger.warning(
                        f"[playbook dual] EOD 兜底当日放弃 {symbol} ({_row_target}) — "
                        f"当日已连续失败 {_fail_count} 次, 需人工对账确认是否孤儿仓"
                    )
                    continue
                if _row_target == "real":
                    release = _release_live_broker_stop_for_rule_exit(
                        dual, conn, r, _meta
                    )
                    if release["action"] == "reconciled":
                        closed += 1
                        continue
                    if release["action"] != "proceed":
                        _alert_close_failure(
                            symbol, _row_target, "BROKER_STOP_RELEASE",
                            release["error"],
                            error=release["error"].split(":", 1)[0]
                        )
                        continue
                try:
                    close_res = dual.close_position(
                        symbol, qty, direction, target_env=_row_target,
                        exit_owner=_playbook_exit_owner(db_path, r, _meta),
                        exit_decision=_playbook_exit_decision(_eod_reason, _rp, _meta))
                except Exception:
                    _meta["eod_close_fail_count"] = _fail_count + 1
                    _meta["eod_close_fail_date"] = _today_et_str()
                    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                                 (_json.dumps(_meta), r["id"]))
                    conn.commit()
                    if _handle_live_rule_exit_failure is not None:
                        _handle_live_rule_exit_failure(
                            dual, conn, r, _meta,
                            {"success": False, "error": "broker_exception", "dealt_qty": None},
                        )
                    raise
                if not close_res.get("success"):
                    _alert_close_failure(symbol, _row_target, "EOD_EXIT_PENDING",
                                         close_res.get("error", "exit evidence pending"),
                                         # 2026-09-14: 第四处同病调用点 (EOD 平仓走同一
                                         # close_res.error 语义) — 瞬态 *_pending 走宽限分流.
                                         error=close_res.get("error", ""))
                    if _handle_live_rule_exit_failure is not None:
                        _handle_live_rule_exit_failure(dual, conn, r, _meta, close_res)
                    continue
                closed += _book_playbook_exit_result(dual, close_res, conn)
            except Exception as row_error:
                conn.rollback()
                logger.exception(
                    f"[playbook] exit row failed trade_id={r['id']} "
                    f"symbol={r['symbol']} env={_row_target}: {row_error}")
                try:
                    _alert_close_failure(r["symbol"], _row_target, "EXIT_ROW_ERROR",
                                         f"trade_id={r['id']}: {row_error}")
                except Exception:
                    logger.exception("[playbook] exit row alert failed")
                continue

        return closed

    except Exception as e:
        logger.warning(f"[playbook sim] EOD force_close 异常 (已隔离): {e}")
        return closed

    finally:
        if conn is not None:
            conn.close()
