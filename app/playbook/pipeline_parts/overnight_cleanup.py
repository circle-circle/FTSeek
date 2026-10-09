"""隔夜孤儿仓清理 (阶段14迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

from typing import Any, Callable


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
                                          earnings_guard=getattr(scheduler, "earnings_risk_guard", None))  # RFC-095 身份锚
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
                    raise
                if not close_res.get("success"):
                    _alert_close_failure(symbol, _row_target, "EOD_EXIT_PENDING",
                                         close_res.get("error", "exit evidence pending"),
                                         # 2026-09-14: 第四处同病调用点 (EOD 平仓走同一
                                         # close_res.error 语义) — 瞬态 *_pending 走宽限分流.
                                         error=close_res.get("error", ""))
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
