"""
FTSeek-US 持仓恢复模块 (P2-1 拆分自 scheduler_tasks.py)

职责:
- 服务重启后从 DB 快照恢复未平仓持仓到内存
- 股票持仓恢复 + 期权持仓恢复
- 一致性校验 (DB vs Broker)
"""
import logging
from datetime import datetime
from typing import Dict, Any

from app.core.models import (
    Direction, MarketRegime, Position,
)
from app.execution.options_selector import OptionSelector

logger = logging.getLogger(__name__)


def _load_price_fail_closed_set() -> set:
    """加载 PRICE_FAIL 已关闭的期权代码集合，防止 broker_sync 重导入循环"""
    try:
        import json as _json
        import os as _os
        _pf_p = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
        if _os.path.exists(_pf_p):
            return set(_json.load(open(_pf_p)))
    except Exception:
        pass
    return set()


def restore_positions_from_db(scheduler):
    """服务重启后从 DB 快照恢复未平仓持仓到内存

    P0 修复 2026-04-25 v3: 幽灵检测以 Broker 确认为准
    
    旧逻辑缺陷:
    - 幽灵检测基于 trade_records 判断，但 broker_sync 源的持仓天然无交易记录
    - 导致 Broker 真实持仓被错误标记为"幽灵"并拒绝恢复
    - 例如: BILI/GOOGL/META/NTES 是 Broker 真实 SHORT 持仓，但被标记为幽灵
    
    新逻辑:
    1. 先查询 Broker 实际持仓集合 (broker_symbols)
    2. 幽灵检测: 仅在 broker_symbols 未知时使用 DB 启发式检测
    3. 如果 Broker 确认某持仓存在，绝不标记为幽灵
    4. 仅当 Broker 无此持仓 且 DB 有记录时，才可能是真正幽灵
    """
    # --- 0. 预先查询 Broker 实际持仓 ---
    broker_symbols = set()
    broker_positions_map = {}  # symbol → {qty, cost_price, ...}
    try:
        from futu import OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket, SecurityFirm
        import os as _os

        from app.core.config_container import get_config
        _trade_env = get_config().trade_env_int
        _acc_id_str = _os.getenv("SIMULATE_ACCOUNT_ID", "")
        _acc_id = int(_acc_id_str) if _acc_id_str else 0
        trade_env = TrdEnv.SIMULATE if _trade_env == 1 else TrdEnv.REAL
        trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=_os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(_os.getenv("FUTU_OPEND_PORT", "11111")),
            security_firm=SecurityFirm.FUTUINC,
        )
        ret, broker_data = trade_ctx.position_list_query(trd_env=trade_env, acc_id=_acc_id)
        if ret == RET_OK and not broker_data.empty:
            for _, row in broker_data.iterrows():
                qty = float(row.get('qty', 0))
                if abs(qty) > 0:
                    code = row.get('code', '')
                    broker_symbols.add(code)
                    broker_positions_map[code] = {
                        'qty': qty,
                        'cost_price': float(row.get('cost_price', 0)),
                    }
            logger.info(f"[恢复] Broker 确认 {len(broker_symbols)} 个持仓: {broker_symbols}")
        trade_ctx.close()
    except Exception as e:
        logger.warning(f"[恢复] Broker 预查询失败，将恢复所有 DB 持仓: {e}")
        broker_symbols = None  # None = skip broker check, restore all

    # --- 0.5 幽灵名单预计算 (必须在步骤 1 删除之前) ---
    # P0 修复 v3 (2026-04-25): 幽灵检测以 Broker 确认为最高优先级
    # 原则: 如果 Broker 确认某标的有持仓，绝不标记为幽灵
    known_ghosts = set()

    # 仅当 Broker 查询失败时，才使用 DB 启发式检测
    # 因为 broker_sync 源的持仓天然无 trade_records，会被误判为幽灵
    if broker_symbols is None:
        logger.warning("[恢复·防幽灵] Broker 查询失败，使用 DB 启发式检测 (可能误判)")
        
        # (a) 近期人工平仓
        try:
            _conn = scheduler.trade_store._get_conn()
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT DISTINCT symbol FROM trade_records "
                "WHERE exit_reason = 'EXTERNAL_CLOSE' "
                "AND date(exit_time) >= date('now', '-7 days')"
            )
            known_ghosts = set(row[0] for row in _cur.fetchall())
        except Exception:
            pass

        # (b) stoploss_events 标记的外部平仓
        try:
            _conn = scheduler.trade_store._get_conn()
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT DISTINCT symbol FROM stoploss_events "
                "WHERE event_type IN ('external_close', 'ghost_cleanup') "
                "AND date(event_time) >= date('now', '-30 days')"
            )
            known_ghosts |= set(row[0] for row in _cur.fetchall())
        except Exception:
            pass

        # (d) 持久化记忆中的幽灵名单
        try:
            import os as _os, json as _json
            _mem_path = _os.path.expanduser("~/.hermes/ftseek/memory/us.json")
            if _os.path.exists(_mem_path):
                with open(_mem_path) as _f:
                    _mem = _json.load(_f)
                _persisted_ghosts = set(_mem.get("known_issues", {}).get("ghost_positions", []))
                if _persisted_ghosts:
                    known_ghosts |= _persisted_ghosts
        except Exception:
            pass
    else:
        # P0 修复 v3: Broker 查询成功时，以 Broker 为准
        # 不再使用 trade_records 判断幽灵（broker_sync 源天然无交易记录）
        logger.info("[恢复·防幽灵] Broker 查询成功，以 Broker 确认为准")
        
        # 仅保留持久化记忆中与 Broker 冲突的幽灵（用于日志）
        try:
            import os as _os, json as _json
            _mem_path = _os.path.expanduser("~/.hermes/ftseek/memory/us.json")
            if _os.path.exists(_mem_path):
                with open(_mem_path) as _f:
                    _mem = _json.load(_f)
                _persisted_ghosts = set(_mem.get("known_issues", {}).get("ghost_positions", []))
                # 过滤掉 Broker 确认的持仓（它们不是幽灵）
                _persisted_ghosts = _persisted_ghosts - broker_symbols
                if _persisted_ghosts:
                    known_ghosts = _persisted_ghosts
        except Exception:
            pass
    
    if known_ghosts:
        logger.info(f"[恢复·防幽灵] 综合幽灵名单: {sorted(known_ghosts)}")
    else:
        logger.info("[恢复·防幽灵] 无已知幽灵持仓")

    # P1 修复 (2026-05-22): 加载 PRICE_FAIL 已关闭集合，防止 broker_sync 重导入循环
    _pf_closed = _load_price_fail_closed_set()
    if _pf_closed:
        logger.info(f"[恢复·PRICE_FAIL 防循环] 已关闭期权集合: {_pf_closed}")

    # --- 1. 恢复普通股票持仓 (仅恢复 Broker 确认的) ---
    snapshots = []
    restored_count = 0
    skipped_ghost_count = 0
    try:
        snapshots = scheduler.trade_store.load_open_positions()
        if not snapshots:
            logger.info("[恢复] 无未平仓股票持仓快照")
        else:
            for snap in snapshots:
                sym = snap.get("symbol", "")
                # 如果 Broker 预查询成功，仅恢复 Broker 确认的持仓
                if broker_symbols is not None and sym not in broker_symbols:
                    logger.warning(
                        f"[恢复·防幽灵] 跳过 DB 持仓 {sym} (Broker 无此持仓，"
                        f"可能是 reconciler 残留)"
                    )
                    skipped_ghost_count += 1
                    # 从 DB 清理孤儿记录
                    try:
                        scheduler.trade_store._get_conn().execute(
                            "DELETE FROM open_positions WHERE symbol = ?", (sym,)
                        ).connection.commit()
                    except Exception:
                        pass
                    continue

                # P0 修复 2026-04-25: pending_close 识别 — 跳过正在平仓中的持仓
                action_log = snap.get("action_log", [])
                if action_log and isinstance(action_log, list) and action_log[-1].get("action") == "pending_close":
                    logger.info(
                        f"[恢复·pending_close] 跳过 {sym} (平仓中，等待 Futu 确认)"
                    )
                    # 不清理 DB — 让 PositionSync 确认 Futu 已无此持仓后再删除
                    continue

                try:
                    direction = Direction.LONG if snap["direction"] == "LONG" else Direction.SHORT
                    regime_str = snap.get("market_regime", "RANGE")
                    try:
                        regime = MarketRegime(regime_str)
                    except ValueError:
                        regime = MarketRegime.RANGE
                    entry_time_str = snap.get("entry_time", "")
                    entry_time = datetime.fromisoformat(entry_time_str) if entry_time_str else datetime.now()

                    position = Position(
                        strategy_id=snap.get("strategy_id", "") or "main_pipeline",  # P0 (2026-06-05): 中性fallback
                        signal_id=snap.get("signal_id", ""),
                        symbol=snap["symbol"],
                        direction=direction,
                        size=snap["size"],
                        avg_entry_price=snap["avg_entry_price"],
                        initial_sl=snap["initial_sl"],
                        initial_tp=snap["initial_tp"],
                        current_sl=snap["current_sl"],
                        take_profit_levels=snap["take_profit_levels"],
                        time_stop_hours=snap.get("time_stop_hours", 168),
                        market_regime=regime,
                    )
                    # 恢复开仓时间和移动止损状态
                    position.entry_time = entry_time
                    position.trail_state = snap.get("trail_state", {})
                    position.owner_strategy = (
                        snap.get("owner_strategy")
                        or snap.get("strategy_id")
                        or getattr(position, 'strategy_id', '')
                        or "main"
                    )
                    if snap.get("position_id"):
                        position.position_id = snap["position_id"]

                    # P0 修复 2026-04-23: 恢复异常标记
                    # P0 修复 2026-05-13: 自动恢复 — 当 Broker 确认持仓存在且一致时自动清除
                    if snap.get("is_anomalous"):
                        broker_qty = None
                        if broker_positions_map and sym in broker_positions_map:
                            broker_qty = broker_positions_map[sym].get("qty")
                        if broker_qty is not None:
                            db_size = float(snap.get("size", 0))
                            if abs(broker_qty - db_size) < 1:
                                try:
                                    scheduler.trade_store.unmark_anomalous(
                                        sym, reason=f"broker_confirmed qty={broker_qty}"
                                    )
                                    logger.info(
                                        f"[恢复·自动修复] {sym} is_anomalous 已清除, "
                                        f"Broker 确认 {broker_qty} 股 = DB {db_size} 股"
                                    )
                                except Exception as e:
                                    logger.warning(f"[恢复·自动修复] {sym} unmark_anomalous 失败: {e}")
                                    position.metadata = position.metadata or {}
                                    position.metadata["_close_blocked"] = True
                                    position.metadata["_close_blocked_reason"] = f"DB is_anomalous=1, unmark failed: {e}"
                            else:
                                position.metadata = position.metadata or {}
                                position.metadata["_close_blocked"] = True
                                position.metadata["_close_blocked_reason"] = (
                                    f"DB is_anomalous=1, Broker qty={broker_qty} ≠ DB {db_size}"
                                )
                        else:
                            position.metadata = position.metadata or {}
                            position.metadata["_close_blocked"] = True
                            position.metadata["_close_blocked_reason"] = snap.get("action_log", [])[-1].get("action", "unknown") if snap.get("action_log") else "DB marked anomalous"
                            position.metadata["_close_blocked_at"] = snap.get("last_sync_at", "")

                    scheduler.position_store.add(snap["symbol"], position)
                    logger.info(
                        f"[恢复] 恢复股票持仓 {snap['symbol']} {snap['direction']} "
                        f"entry=${snap['avg_entry_price']:.2f} "
                        f"SL=${snap['current_sl']:.2f}"
                    )
                    restored_count += 1
                except Exception as e:
                    logger.error(f"[恢复] 恢复 {snap.get('symbol')} 失败：{e}", exc_info=True)
            logger.info(f"[恢复] 共恢复 {restored_count} 个股票持仓，跳过 {skipped_ghost_count} 个幽灵持仓")
    except Exception as e:
        logger.error(f"[恢复] 股票持仓恢复异常：{e}", exc_info=True)

    # --- 2. 同步 Broker 有但 DB 没有的新持仓 ---
    # P0 修复 2026-04-25: 跳过已知幽灵标的 (使用步骤 0.5 预计算的 known_ghosts)
    try:
        mem_positions = scheduler.position_store.all()
        mem_symbols = set(p.symbol for p in mem_positions)

        if broker_symbols is not None:
            missing_symbols = broker_symbols - mem_symbols
            if missing_symbols:
                # 过滤幽灵标的 + PRICE_FAIL 已关闭期权
                real_missing = missing_symbols - known_ghosts - _pf_closed
                ghost_rejected = missing_symbols & known_ghosts
                pf_rejected = missing_symbols & _pf_closed
                if pf_rejected:
                    logger.warning(
                        f"[恢复·一致性校验·PRICE_FAIL] 拒绝回写已关闭期权: {sorted(pf_rejected)}"
                    )
                    # 从 DB 永久清理这些已关闭期权
                    try:
                        for sym in pf_rejected:
                            scheduler.trade_store._get_conn().execute(
                                "DELETE FROM open_positions WHERE symbol = ?", (sym,)
                            ).connection.commit()
                    except Exception:
                        pass
                if ghost_rejected:
                    logger.warning(
                        f"[恢复·一致性校验·防幽灵] 拒绝回写已知幽灵持仓: {sorted(ghost_rejected)}"
                    )
                    # 从 DB 永久清理这些幽灵
                    try:
                        for sym in ghost_rejected:
                            scheduler.trade_store._get_conn().execute(
                                "DELETE FROM open_positions WHERE symbol = ?", (sym,)
                            ).connection.commit()
                    except Exception:
                        pass
                if real_missing:
                    logger.warning(
                        f"[恢复·一致性校验] Broker 有但本地无的持仓: {sorted(real_missing)}，"
                        f"正在回写到 DB..."
                    )
                    for sym in real_missing:
                        bp = broker_positions_map.get(sym, {})
                        qty = bp.get('qty', 0)
                        cost = bp.get('cost_price', 0)
                        if abs(qty) > 0 and cost > 0:
                            try:
                                is_short = qty < 0
                                direction = Direction.SHORT if is_short else Direction.LONG
                                size = abs(qty)
                                # SHORT 止损在成本上方，LONG 在下方
                                sl_mult = 1.08 if is_short else 0.92
                                tp_mult = 0.85 if is_short else 1.15
                                position = Position(
                                    strategy_id="broker_sync",
                                    symbol=sym,
                                    direction=direction,
                                    size=size,
                                    avg_entry_price=cost,
                                    initial_sl=cost * sl_mult,
                                    current_sl=cost * sl_mult,
                                    initial_tp=cost * tp_mult,
                                    take_profit_levels=[],
                                    time_stop_hours=72,
                                    market_regime=MarketRegime.RANGE,
                                )
                                position.owner_strategy = "broker_sync"
                                # Critical #2 (2026-06-02): 标记 broker 同步仓 → ExitMonitor 跳过
                                #   防止 broker 自动操作与系统下单冲突 (双重平仓风险)
                                position.metadata = {
                                    "source": "broker_sync",
                                    "_from_futu_balance": True,
                                    "sync_origin": "startup_reconcile",
                                }
                                scheduler.position_store.add(sym, position)
                                scheduler.trade_store.upsert_open_position(
                                    position, source="broker_sync", action="startup_reconcile"
                                )
                                logger.info(
                                    f"[恢复·一致性校验] 已同步 Broker 持仓 {sym}: "
                                    f"{qty}股 @${cost:.2f}"
                                )
                            except Exception as e:
                                logger.error(f"[恢复·一致性校验] 同步 {sym} 失败: {e}")
                else:
                    logger.info("[恢复·一致性校验] 无真实缺失持仓（所有差异均为已知幽灵，已跳过）")
    except Exception as e:
        logger.debug(f"[恢复·一致性校验] 检查异常: {e}")

    # --- 2. 恢复期权持仓 ---
    try:
        from app.core.config_container import get_config
        _oc = get_config().get_section("options", {})

        opt_snaps = scheduler.trade_store.load_option_positions()
        if not opt_snaps:
            logger.info("[恢复] 无未平仓期权持仓快照")
        else:
            for snap in opt_snaps:
                try:
                    symbol       = snap["symbol"]
                    option_code  = snap["option_code"]
                    entry_price  = snap["entry_price"]
                    qty          = int(snap["qty"])
                    cost_total   = snap.get("cost_total", entry_price * qty * 100)
                    expiry_date  = snap.get("expiry_date", "")
                    strike_price = snap.get("strike_price", 0.0)
                    entry_time_str = snap.get("entry_time", "")
                    entry_time = datetime.fromisoformat(entry_time_str) if entry_time_str else datetime.now()

                    # 到期检查
                    dte_remaining = OptionSelector.days_to_expiry(expiry_date)
                    force_dte = int(_oc.get("FORCE_CLOSE_DTE", 2))
                    if dte_remaining <= force_dte:
                        logger.warning(
                            f"[恢复] 期权 {option_code} 已临近到期 (DTE={dte_remaining})，跳过恢复（将触发强制平仓）"
                        )
                        pass

                    # P1 修复 (2026-05-22): PRICE_FAIL 已关闭期权跳过恢复
                    if option_code in _pf_closed:
                        logger.warning(
                            f"[恢复·PRICE_FAIL 防循环] 跳过已关闭期权 {option_code} (防止重导入循环)"
                        )
                        try:
                            scheduler.trade_store._get_conn().execute(
                                "DELETE FROM option_positions WHERE option_code = ?", (option_code,)
                            ).connection.commit()
                        except Exception:
                            pass
                        continue

                    # Broker 验证: 防止幽灵期权仓（DB 有但 Futu 无）
                    if broker_symbols is not None and option_code not in broker_symbols:
                        logger.warning(
                            f"[恢复·防幽灵] 跳过期权 {option_code} (Broker 无此持仓，清理 DB 记录)"
                        )
                        try:
                            scheduler.trade_store._get_conn().execute(
                                "DELETE FROM option_positions WHERE option_code = ?", (option_code,)
                            ).connection.commit()
                        except Exception:
                            pass
                        continue

                    position = Position(
                        strategy_id="voting_llm_option",
                        symbol=symbol,
                        direction=Direction.SHORT,
                        size=float(qty),
                        avg_entry_price=entry_price,
                        initial_sl=entry_price * (1 - float(_oc.get("STOP_LOSS_RATIO", 0.3))),
                        initial_tp=entry_price * float(_oc.get("TAKE_PROFIT_RATIO", 2.0)),
                        current_sl=entry_price * (1 - float(_oc.get("STOP_LOSS_RATIO", 0.3))),
                        take_profit_levels=[
                            {"ratio": 1.0, "price": entry_price * float(_oc.get("TAKE_PROFIT_RATIO", 2.0))},
                        ],
                        time_stop_hours=dte_remaining * 24,
                        market_regime=MarketRegime.RANGE,
                    )
                    position.entry_time = entry_time
                    position.metadata = {
                        "is_option":    True,
                        "option_code":  option_code,
                        "strike_price": strike_price,
                        "expiry_date":  expiry_date,
                        "dte":          dte_remaining,
                        "cost_total":   cost_total,
                        "is_simulated": snap.get("is_simulated", True),
                        "signal_strength":   snap.get("signal_strength", 0),
                        "signal_confidence": snap.get("signal_confidence", 0.0),
                    }

                    position.owner_strategy = "voting_llm_option"
                    scheduler.position_store.add(symbol, position)
                    logger.info(
                        f"[恢复] 恢复期权持仓 {symbol} {option_code} "
                        f"qty={qty} entry=${entry_price:.2f} DTE={dte_remaining}"
                    )
                except Exception as e:
                    logger.error(
                        f"[恢复] 恢复期权持仓 {snap.get('symbol')} 失败：{e}",
                        exc_info=True,
                    )
            logger.info(f"[恢复] 共恢复 {len(opt_snaps)} 个期权持仓")
    except Exception as e:
        logger.error(f"[恢复] 期权持仓恢复异常：{e}", exc_info=True)

    # --- 3. P0 修复 (2026-04-25): Broker→DB 全量对账回填 ---
    # 解决 stoploss_daemon 从 DB 删除但 Futu 实际未平仓的 "丢失持仓" 问题
    # P0 修复 v2: 使用步骤 0.5 预计算的 known_ghosts
    try:
        db_conn = scheduler.trade_store._get_conn()
        db_cursor = db_conn.cursor()
        db_cursor.execute("SELECT symbol FROM open_positions")
        db_symbols = set(row[0] for row in db_cursor.fetchall())

        # 计算差异: Broker 有但 DB 无，过滤已知幽灵 + PRICE_FAIL 已关闭期权
        missing_in_db = broker_symbols - db_symbols if broker_symbols else set()
        real_missing_p0 = missing_in_db - known_ghosts - _pf_closed
        ghost_rejected_p0 = missing_in_db & known_ghosts
        pf_rejected_p0 = missing_in_db & _pf_closed

        if pf_rejected_p0:
            logger.info(
                f"[恢复·P0 回填·PRICE_FAIL] 跳过已关闭期权: {sorted(pf_rejected_p0)}"
            )
            # 从 DB 清理已关闭期权（如果残留）
            try:
                for sym in pf_rejected_p0:
                    db_conn.execute(
                        "DELETE FROM open_positions WHERE symbol = ?", (sym,)
                    ).connection.commit()
            except Exception:
                pass

        if ghost_rejected_p0:
            logger.info(
                f"[恢复·P0 回填·防幽灵] 跳过已知幽灵标的: {sorted(ghost_rejected_p0)}"
            )

        if real_missing_p0:
            logger.warning(
                f"[恢复·P0 回填] Broker 有但 DB 无的真实持仓 (丢失持仓!): {sorted(real_missing_p0)}"
            )
            from futu import OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket, SecurityFirm
            import os as _os

            from app.core.config_container import get_config
            _trade_env = get_config().trade_env_int
            trade_env = TrdEnv.SIMULATE if _trade_env == 1 else TrdEnv.REAL
            trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=_os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                port=int(_os.getenv("FUTU_OPEND_PORT", "11111")),
                security_firm=SecurityFirm.FUTUINC,
            )
            ret2, pos_data = trade_ctx.position_list_query(trd_env=trade_env)
            trade_ctx.close()

            if ret2 == RET_OK and not pos_data.empty:
                filled_count = 0
                for _, row in pos_data.iterrows():
                    code = row.get('code', '')
                    if code not in real_missing_p0:
                        continue
                    qty = abs(float(row.get('qty', 0)))
                    cost = float(row.get('cost_price', 0))
                    side = str(row.get('position_side', 'LONG'))
                    is_short = (float(row.get('qty', 0)) < 0) or (side == 'SHORT')
                    direction = Direction.SHORT if is_short else Direction.LONG

                    # 做空止损 = entry + 8%, 做多止损 = entry - 8%
                    sl_mult = 0.08
                    if is_short:
                        initial_sl = cost * (1 + sl_mult)
                        initial_tp = cost * 0.85
                    else:
                        initial_sl = cost * (1 - sl_mult)
                        initial_tp = cost * 1.15

                    position = Position(
                        strategy_id="broker_reconcile_p0",
                        symbol=code,
                        direction=direction,
                        size=qty,
                        avg_entry_price=cost,
                        initial_sl=initial_sl,
                        current_sl=initial_sl,
                        initial_tp=initial_tp,
                        take_profit_levels=[],
                        time_stop_hours=72,
                        market_regime=MarketRegime.RANGE,
                    )
                    position.trail_state = {
                        "highest_price": cost if not is_short else cost,
                        "lowest_price": cost if is_short else cost,
                        "prev_scan_price": cost,
                    }

                    position.owner_strategy = "broker_reconcile_p0"
                    scheduler.position_store.add(code, position)
                    scheduler.trade_store.upsert_open_position(
                        position, source="broker_reconcile_p0", action="startup_fill"
                    )
                    logger.warning(
                        f"[恢复·P0 回填] ✅ 已回填丢失持仓: {code} {direction.value} "
                        f"{qty}股 @ ${cost:.2f} | SL=${initial_sl:.2f} | TP=${initial_tp:.2f}"
                    )
                    filled_count += 1
            else:
                filled_count = 0

            logger.info(
                f"[恢复·P0 回填] 完成: 回填 {filled_count} 只丢失持仓，"
                f"跳过 {len(ghost_rejected_p0)} 只幽灵标的 → "
                f"DB 现在共有 {len(db_symbols) + filled_count} 只"
            )
        else:
            logger.info("[恢复·P0 回填] 无需回填: Broker 与 DB 持仓一致（或差异均为幽灵）")
    except Exception as e:
        logger.error(f"[恢复·P0 回填] 失败: {e}", exc_info=True)
