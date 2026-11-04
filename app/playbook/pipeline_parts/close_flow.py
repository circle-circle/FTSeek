"""出场落账流 (阶段12迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Optional

from app.risk.exit_reasons import normalize_exit_reason


def _close_trade_record(
    conn,
    trade_id: int,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    pnl_pct: float,
    holding_hours: float,
    inst_id=None,
    symbol: str = None,
    close_futu_order_id: str = None,
    *,
    exit_intent_id=None,
    exit_owner=None,
    exit_metadata=None,
    exit_time=None,
    fee_broker=None,
    datetime_cls: Callable,
    _now_et: Callable,
    logger: Any,
    _sl_burst_alerted: set,
    classify_be_exit: Callable,
    _classify_ghost_cause: Callable,
    _include_observed_prices_in_extremes: Callable,
    _max_drawdown_from_mfe_mae: Callable,
    _net_pnl_pair: Callable,
) -> bool:

    """统一平仓写入 — 原子更新 trade_records + playbook_instructions + open_positions.

    Owns a clean transaction; returns True for a new close, False for receipt replay.

    根因修复 (2026-07-01): 之前只更新 trade_records + instructions,
    不碰 open_positions → position_sync 从 DB 重建幽灵仓 → 开盘后异常平仓.
    现在直接 DELETE open_positions, 从源头消灭状态分裂.
    2026-07-14: 软删除→物理删除 (trade_records已有完整审计记录, 软删除行无消费者).
    根因修复 (2026-07-07): 
      1. 增加 close_futu_order_id 参数, 确保平仓订单可追溯.
      2. 从 trade_records 查 entry_time, 用 get_market_hours_us 重算 holding_hours.
         旧逻辑: 调用方传 wall-clock 差值 (naive-aware TypeError→0, 或 CST时间差).
         新逻辑: 统一用交易时段计算, 与 models.py holding_hours property 对齐.
    2026-07-09: 从 metadata_json 提取 mfe_pct/mae_pct 写入独立列 (MFE/MAE采集).
    """
    # 2026-07-03 fix: 用 ET 时区写入 exit_time, 与 entry_time 格式对齐 (均 tz-aware)
    exit_reason = normalize_exit_reason(exit_reason)
    try:
        now_iso = datetime_cls.now(_now_et()).isoformat()
    except Exception:
        now_iso = datetime_cls.now().isoformat()

    if conn.in_transaction:
        raise ValueError("close bookkeeping requires a clean connection (pending transaction)")
    if exit_time is not None:
        # First full-fill confirmation time, not the later booking retry time.
        datetime_cls.fromisoformat(exit_time)
        now_iso = exit_time
    if exit_intent_id is not None and (not isinstance(exit_intent_id, str) or not exit_intent_id):
        raise ValueError("exit intent receipt must be a non-empty string")
    if exit_metadata is not None and not isinstance(exit_metadata, dict):
        raise ValueError("exit metadata must be a dictionary")

    try:
        conn.execute("BEGIN IMMEDIATE")
        _tr = conn.execute(
            "SELECT metadata_json, direction, entry_price, entry_time, quantity, source_type, "
            "symbol, exit_time, exit_price FROM trade_records WHERE id=?", (trade_id,),
        ).fetchone()
        if _tr is None:
            raise ValueError(f"exit owner trade {trade_id} does not exist")
        _meta = {}
        _meta_parse_ok = False
        if _tr[0]:
            try:
                _meta = json.loads(_tr[0])
                if not isinstance(_meta, dict):
                    raise ValueError("trade metadata must be a dictionary")
                _meta_parse_ok = True
            except (ValueError, TypeError):
                if exit_intent_id is not None:
                    raise ValueError("invalid metadata for exit intent receipt")
                _meta = {}
        if symbol and symbol != _tr[6]:
            raise ValueError("exit owner symbol identity mismatch")
        if exit_intent_id is not None:
            from pathlib import Path
            if not isinstance(exit_owner, dict):
                raise ValueError("exit owner identity is required")
            _expected = {
                "trade_id": trade_id, "entry_time": _tr[3], "entry_price": _tr[2],
                "quantity": _tr[4], "source_type": _tr[5],
            }
            if any(key not in exit_owner or exit_owner[key] != value for key, value in _expected.items()):
                raise ValueError("exit owner immutable entry identity mismatch")
            if not symbol or exit_owner.get("symbol", symbol) != _tr[6]:
                raise ValueError("exit owner symbol identity mismatch")
            if str(exit_owner.get("direction", "")).upper() != str(_tr[1]).upper():
                raise ValueError("exit owner direction identity mismatch")
            _db_path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
            if not exit_owner.get("db_path") or (
                _db_path and Path(exit_owner["db_path"]).resolve() != Path(_db_path).resolve()
            ) or (not _db_path and exit_owner["db_path"] != ":memory:"):
                raise ValueError("exit owner source database identity mismatch")
            _entry_order = conn.execute("SELECT futu_order_id FROM trade_records WHERE id=?", (trade_id,)).fetchone()[0]
            if str(_entry_order or "") != str(exit_owner.get("entry_order_id") or ""):
                raise ValueError("exit owner entry order identity mismatch")
            if _meta.get("exit_intent_id") == exit_intent_id and _tr[7] and _tr[8] is not None:
                conn.rollback()
                return False
        if _meta.get("exit_intent_id"):
            raise ValueError("exit intent receipt conflict")
        if _tr[7] or _tr[8] not in (None, 0, 0.0):
            raise ValueError("trade is already closed without this exit receipt")
        if exit_metadata is not None:
            _meta.update(exit_metadata)
            _meta_parse_ok = True
        _extremes_dirty = _include_observed_prices_in_extremes(
            _meta, _tr[1], _tr[2], exit_price
        )
        _meta_dirty = exit_metadata is not None or _extremes_dirty
        if exit_intent_id is not None:
            _meta["exit_intent_id"] = exit_intent_id
            _meta_parse_ok = _meta_dirty = True

        # 2026-07-09: 从 metadata 提取 MFE/MAE
        # 2026-07-10 fix: metadata["mfe_pct"] 键几乎从不写 — monitor_playbook_sim_exits 只在
        #   `if not exit_reason` 分支累积 mfe_pct, 触发退出那一轮直接平仓跳过累积块,
        #   导致 393/397 笔 mfe_pct=0 (含 121/122 盈利单, 逻辑违规).
        #   根因: metadata["peak_price"] 是可靠持久化的 (入场写入 + 每轮更新), 但从未回算成 mfe_pct.
        #   修复: mfe_pct 缺失/为0时, 用真峰值 peak_price + entry_price/direction 反算真实 MFE%.
        #   历史无 intraday 缓存无法回填, 此修复对新平仓的单子向前生效 (纯 surgical, 不碰热路径).
        _mfe_pct = 0.0
        _mfe_tick_pct = 0.0
        _mfe_accum = 0.0
        _mae_pct = 0.0
        try:
            # #6: 复用顶部行快照 _tr/_meta (原此处独立 SELECT metadata_json/direction/entry_price)
            if _meta_parse_ok:
                _mfe_pct = float(_meta.get("mfe_pct", 0) or 0)
                _mae_pct = float(_meta.get("mae_pct", 0) or 0)
                # mfe_pct 缺失 → 从 peak_price 反算 (peak_price 是持久化的真峰值)
                # 2026-09-25 P2口径修 (MU 9/25 盘前BE案, 用户裁「按建议执行」): 原"仅缺失(<=0)才补"
                #   会保留 bar/轮询累积值 — 该累积冻结在 bar_covered_until, ETH tick 不进 bar,
                #   推送 "MFE 0.36%/capture 100%" 低于真峰值 (tick 0.4219%/bar 0.4375%)。改为恒取 max:
                #   peak_price (tick级, 含 ETH 轮询) 与 peak_price_bar (1min bar) 都参与, 与下方
                #   _realized_fav 兜底同型。影响面: mfe_pct 列交易侧唯一消费者 = symbol_streak_ban
                #   (mfe<0.5 才计低MFE连败), mfe 更真只会缩小误封禁; 其余(推送/看板/周报)皆观测。
                _mfe_accum = _mfe_pct  # 累积口径基线, 供 time_to_mfe 陈旧判定
                _entry = float(_tr[2] or 0)
                _dir = (_tr[1] or "LONG").upper()
                for _pk_key in ("peak_price", "peak_price_bar"):
                    _peak = float(_meta.get(_pk_key, 0) or 0)
                    if _peak > 0 and _entry > 0:
                        if _dir == "LONG":
                            _calc_mfe = (_peak - _entry) / _entry * 100
                        else:
                            _calc_mfe = (_entry - _peak) / _entry * 100
                        if _calc_mfe > _mfe_pct:
                            _mfe_pct = round(_calc_mfe, 4)
                        # DEB-20261104 D2: tick 通道单独一路 (不加已落袋兜底) ——
                        #   镜像在线闸门 (exit_engine:383 peak_profit_pct 同源) 的读数口径.
                        if _pk_key == "peak_price" and _calc_mfe > _mfe_tick_pct:
                            _mfe_tick_pct = round(_calc_mfe, 4)
                # 2026-07-16: MAE 对称反算 — 与 MFE 同理, mae_pct 缺失时从 trough_price 反算.
                #   根因: trough_price 入场写入 + 每轮更新 (与 peak 对称), 但 close 时从未回算成 mae_pct,
                #   导致 MAE 常年为 0, 无法评估止损距离/浮亏承受度. 纯 surgical, 对新平仓向前生效.
                # 2026-09-25 P2口径修: MAE 侧对称 — 恒取 max(累积, trough_price, trough_price_bar)。
                for _tr_key in ("trough_price", "trough_price_bar"):
                    _trough = float(_meta.get(_tr_key, 0) or 0)
                    if _trough > 0 and _entry > 0:
                        if _dir == "LONG":
                            _calc_mae = (_entry - _trough) / _entry * 100
                        else:
                            _calc_mae = (_trough - _entry) / _entry * 100
                        if _calc_mae > _mae_pct:
                            _mae_pct = round(_calc_mae, 4)
                # 定义兜底: MFE 必须 ≥ 已落袋的有利变动 (pnl_pct 为分数, 转百分比).
                #   处理采样粒度问题: 峰值轮询未抓到最高点 (如 META peak<exit) 时不至低估 MFE.
                _realized_fav = round(pnl_pct * 100, 4)
                if _realized_fav > _mfe_pct:
                    _mfe_pct = _realized_fav
                # mfe_tick_pct / mfe_gate_pct **不加**已落袋兜底 (刻意) ——
                #   前者镜像在线闸门读数, 后者是闸门评估时刻的实际读数;
                #   加兜底会把"闸门当时看到的"污染成"出场时的结果".
                # 对称兜底: 亏损单的 MAE 必须 ≥ 已实现的不利变动 (谷值轮询未抓到最低点时不低估 MAE).
                _realized_adv = round(-pnl_pct * 100, 4)
                if _realized_adv > _mae_pct:
                    _mae_pct = _realized_adv
        except Exception:
            pass
        # 2026-07-11: 量纲夹取 — 日内单笔 MFE/MAE >50% 基本是脏价位 (entry_price 异常等),
        #   判脏归 0 (0 落库视同缺失, 由回填/后续采集修正)。与 monitor 循环采集处阈值一致。
        _MFE_MAE_SANE_MAX = 50.0
        if _mfe_pct > _MFE_MAE_SANE_MAX:
            _mfe_pct = 0.0
        if _mae_pct > _MFE_MAE_SANE_MAX:
            _mae_pct = 0.0
        if _meta_parse_ok:
            _mfe_pct = round(_mfe_pct, 4)
            _mae_pct = round(_mae_pct, 4)
            _meta["mfe_pct"] = _mfe_pct
            _meta["mae_pct"] = _mae_pct

        # ── 根因修复 (2026-07-07): 从 DB 查 entry_time, 用 get_market_hours_us 重算 holding_hours ──
        # 消灭所有调用方传 wall-clock 差值的变体 (naive TypeError, CST时间差, 纯秒数差).
        try:
            # #6: 复用 _tr (原此处独立 SELECT entry_time)
            if _tr is not None and _tr[3]:
                _entry_dt = datetime_cls.fromisoformat(_tr[3])
                _et_tz = _now_et()
                _now_dt = datetime_cls.fromisoformat(now_iso)
                if _now_dt.tzinfo is None:
                    _now_dt = _et_tz.localize(_now_dt) if hasattr(_et_tz, "localize") else _now_dt.replace(tzinfo=_et_tz)
                # 2026-07-16 P1 修复: entry_time 可能是 naive (CST 无 tz), now_dt 是 tz-aware (ET).
                #   两者相减抛 TypeError → holding_hours 回退为调用方传入值 (0.0).
                #   修复: naive entry_dt 加上 ET tz 再相减, 或用 wall-clock fallback.
                if _entry_dt.tzinfo is None:
                    # 2026-07-25 (时区SSOT批漏网补修): pytz 时区禁 replace(tzinfo=) — 产生 -04:56 LMT 偏移
                    _entry_dt = _et_tz.localize(_entry_dt) if hasattr(_et_tz, "localize") else _entry_dt.replace(tzinfo=_et_tz)
                from app.risk.position_exit_monitor import get_market_hours_us
                _market_hh = get_market_hours_us(_entry_dt, _now_dt)
                if _market_hh > 0:
                    holding_hours = round(_market_hh, 1)
                else:
                    # fallback: wall-clock 差值 (比 0 好); abs 兜底防 naive 北京时间被当 ET 导致负值 (如 EOG -6.7h)
                    holding_hours = round(abs((_now_dt - _entry_dt).total_seconds()) / 3600, 1)
        except Exception as _hh_err:
            logger.debug(f"[_close_trade_record] holding_hours 重算失败 trade_id={trade_id}: {_hh_err}")
            # 降级: 用调用方传入的 holding_hours (比0好)

        # 2026-07-16 P1 修复: pnl_pct 兜底反算.
        #   旧逻辑: pnl_pct 由调用方传入, 但某些路径 (旧管线写入/早期代码) 传 0 →
        #   下游所有按 pnl_pct 的分析 (影子退出/归因/回测) 全部出错.
        #   修复: pnl_pct=0 且 pnl≠0 时, 用 exit_price/entry_price 反算.
        if abs(pnl_pct) < 1e-10 and abs(pnl) > 1e-6 and exit_price:
            try:
                # #6: 复用 _tr (原此处独立 SELECT direction/entry_price)
                if _tr is not None:
                    _dir = str(_tr[1] or "LONG").upper()
                    _ent = float(_tr[2] or 0)
                    if _ent > 0:
                        if _dir == "SHORT":
                            pnl_pct = (_ent - exit_price) / _ent
                        else:
                            pnl_pct = (exit_price - _ent) / _ent
            except Exception:
                pass

        # 2026-07-11: exit_telemetry — 平仓时集中组装退出画像写回 metadata_json,
        #   供 config_version×regime×session 分层复盘 (命中规则/峰谷价位/MFE-MAE/保本触发).
        #   只聚合已持久化字段, 不虚构 effective_sl (未持久化 → 存原料 initial_sl+breakeven_triggered).
        try:
            # #6: 复用 _tr/_meta (原此处独立 SELECT metadata_json/entry_price + 单独 UPDATE)
            if _meta_parse_ok:
                _tmeta = _meta
                # 2026-07-20 修复M: 退出参数版本打标 — 毕业统计 (cap_reduction/OKR)
                #   只在同一 config_version 窗口内取样, 消除"边积累边调退出参数"对 PnL/胜率的污染.
                try:
                    from app.playbook.config import get_playbook_config as _gpc_ver
                    _tmeta["config_version"] = _gpc_ver().get_exit_config_version()
                except Exception:
                    pass
                _tmeta["exit_telemetry"] = {
                    "exit_reason": exit_reason,
                    "entry_price": float(_tr[2] or 0),
                    "exit_price": round(float(exit_price or 0), 4),
                    "peak_price": float(_tmeta.get("peak_price", 0) or 0),
                    "trough_price": float(_tmeta.get("trough_price", 0) or 0),
                    # #3 (2026-07-20): bar 版峰谷 (record-only 并行测量) — 周度对比采样低估幅度
                    "peak_price_bar": float(_tmeta.get("peak_price_bar", 0) or 0),
                    "trough_price_bar": float(_tmeta.get("trough_price_bar", 0) or 0),
                    "mfe_pct": round(_mfe_pct, 4),
                    "mae_pct": round(_mae_pct, 4),
                    # 2026-07-22: MFE/MAE 到达时点 (分钟) — 解锁路径依赖退出验证.
                    "time_to_mfe": _tmeta.get("time_to_mfe"),
                    "time_to_mae": _tmeta.get("time_to_mae"),
                    "pnl_pct": round(pnl_pct * 100, 4),
                    "holding_hours": round(holding_hours, 2),
                    # 2026-07-21 修复: breakeven 终态丢失兜底 — 触发平仓那轮不走监控循环的
                    #   metadata 写回 (仅未退出轮执行), _be=True 无人持久化 (7/21 NTES 实证:
                    #   SL_BREAKEVEN 单 metadata 却记 False). 用 exit_reason 蕴含关系兜底:
                    #   这两个标签只在 breakeven 触发后产出 (退出分类逻辑保证). 零行为变更.
                    "breakeven_triggered": bool(_tmeta.get("breakeven_triggered", False)) \
                        or exit_reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK"),
                    "initial_sl": float(_tmeta.get("stop_loss", 0) or 0),
                    # [2026-09-16 用户裁"同意按建议执行"第1条] 生效SL观测性 — ARM 9/15 案:
                    #   SL_INITIAL 标签不区分初始线 vs 规则0.5紧SL线, 对账要翻日志。
                    #   触发瞬间由 exit_engine 规则1 写 _effective_sl_at_exit/_tight_sl_killed
                    #   (record-only); 0/False = 历史仓或未走规则1 (兜底/耐心出场路径)。
                    "effective_sl": float(_tmeta.get("_effective_sl_at_exit", 0) or 0),
                    "tight_sl_triggered": bool(_tmeta.get("_tight_sl_killed", False)),
                    "take_profit": float(_tmeta.get("take_profit", 0) or 0),
                    # RFC-106 §5.2: gap 证据镜像 (record-only, 结构与 entry_gap 对称;
                    # quote 缺 prev_close/open 时为 None — 历史仓/缺价不虚构)
                    "exit_gap": _tmeta.get("exit_gap"),
                    "config_version": _tmeta.get("config_version", ""),
                    "recorded_at": now_iso,
                }
                _meta_dirty = True
        except Exception as _tel_err:
            logger.debug(f"[_close_trade_record] exit_telemetry 组装失败 trade_id={trade_id}: {_tel_err}")

        # 2026-07-19 (DEB-round3 GHOST-RC2): ghost 平仓归因标记 — 写 metadata.ghost_cause.
        #   record-only, 供 shadow 复审区分 RC1 (入场未成交) / RC2 (一仓两账) 处置,
        #   不改平仓行为. 需在主 UPDATE (下方) 之前读改写 metadata, 避免被覆盖.
        try:
            if symbol and "GHOST" in (exit_reason or "").upper():
                # #6: 复用 _meta (原此处独立 SELECT+解析+单独 UPDATE; 解析失败时 _meta 已是 {},
                #   与旧逻辑 _gc_meta={} 兜底一致)
                _meta["ghost_cause"] = _classify_ghost_cause(conn, symbol, trade_id)
                _meta_dirty = True
        except Exception as _gc_err:
            logger.debug(f"[_close_trade_record] ghost_cause 写入失败 trade_id={trade_id}: {_gc_err}")

        # 2026-07-30 (DEB-fee-net-pnl D2 go-forward): 平仓时顺手估算往返费用 — 纯记账,
        #   失败只 debug 绝不阻断平仓链路。fee_breakdown 入 metadata (审计/未来 broker 升级逐项 diff),
        #   fees_est/pnl_net 列在主 UPDATE 后写 (列由 backfill_fee_net.py ALTER, 旧库无列时静默跳过)。
        _fees_total = None
        try:
            if _tr is not None and _meta_parse_ok and exit_price and float(exit_price) > 0:
                from app.execution.fee_model import estimate_round_trip as _est_rt
                _f_qty = float(_tr[4] or 0)
                _f_entry = float(_tr[2] or 0)
                _f_edate = str(_tr[3] or "")[:10] or now_iso[:10]
                if _f_qty > 0 and _f_entry > 0:
                    _f_rt = _est_rt(_f_qty, _f_entry, float(exit_price), str(_tr[1] or ""),
                                    trade_date=_f_edate, exit_date=now_iso[:10])
                    _fees_total = round(_f_rt["total"], 2)
                    _meta["fee_breakdown"] = {**_f_rt, "source": "est", "recorded_at": now_iso}
                    _meta_dirty = True
        except Exception as _fee_err:
            logger.debug(f"[_close_trade_record] 费用估算失败 trade_id={trade_id}: {_fee_err}")

        # 2026-08-21 (用户裁"落"): BE 出场分类标签 be_exit_class — 解决"保本名义亏损"歧义.
        #   8/1-8/21 47 笔 SL_BREAKEVEN 实证三分: 净正 17 / 瀑布坠穿 6 (-$85) / 贴线滑点残差 24.
        #   GAP = 触发时价格已坠穿保本线 ≥5bp (两次轮询缝里的瀑布, 砍掉的是坠落本身, 机制无误);
        #   OK  = 净正, 名实相符; SLIP = 其余 (门口成交只付几 bp 滑点/费用残差, 小额).
        #   drift 来自 monitor 路径先落库的 _be_close_telemetry.drift_from_be_line (fallback 才有);
        #   telemetry 缺失 → 按净额回退 OK/SLIP (fail-open). 消费方: 平仓推送文案 + 周报分组.
        try:
            if (exit_reason or "") == "SL_BREAKEVEN" and _meta_parse_ok:
                _be_drift = (_meta.get("_be_close_telemetry") or {}).get("drift_from_be_line")
                _be_net = (round(round(pnl, 2) - _fees_total, 2)
                           if _fees_total is not None else None)
                # 2026-08-21 (Qoder §56.3): 判序逻辑收口到 classify_be_exit 纯函数
                # (OK 先判, 契约测试 tests/test_be_exit_class.py).
                _meta["be_exit_class"] = classify_be_exit(_be_net, _be_drift)
                _meta_dirty = True
        except Exception as _bec_err:
            logger.debug(f"[_close_trade_record] be_exit_class 分类失败 trade_id={trade_id}: {_bec_err}")

        # Metadata (including the intent receipt) is mandatory in the close transaction.
        if _meta_dirty:
            conn.execute(
                "UPDATE trade_records SET metadata_json=? WHERE id=?",
                (json.dumps(_meta, ensure_ascii=False, default=str), trade_id),
            )

        # ── 量纲约定 (2026-07-18 显式文档化) ──
        #   pnl_pct: ratio (0.0311 = 3.11%)  — 与 realized_pnl 同向, 下游消费者按 ratio 处理
        #   mfe_pct / mae_pct: percentage (3.72 = 3.72%) — 与 peak_price/trough_price 反算一致
        #   两者差 100x, 混合比较时必须 pnl_pct*100 或 mfe_pct/100 对齐.
        #   历史原因: mfe/mae 从 metadata["mfe_pct"] 继承百分比语义, pnl_pct 从 order_executor 继承 ratio 语义.
        #
        # 2026-07-20 量纲防护栏: 写入前自动检测 pnl_pct 是否误用百分比量纲.
        #   日内单笔 |pnl_pct| > 1.0 (即 >100%) 几乎不可能 → 大概率是 3.11 而非 0.0311.
        #   自动修正 + 告警, 防止静默污染下游 analytics (capture_ratio/IC/edge 全部依赖 ratio 语义).
        if abs(pnl_pct) > 1.0:
            logger.warning(
                f"[_close_trade_record] ⚠️ 量纲防护栏触发: trade_id={trade_id} "
                f"pnl_pct={pnl_pct:.4f} (|>1.0|), 疑似百分比量纲误写, 自动 /100 修正"
            )
            pnl_pct = pnl_pct / 100.0
        # 2026-08-07 修复 (主动审计 B9): max_drawdown 原直接拷贝 mae_pct (入场→谷值),
        #   语义错 — 传统 max_drawdown = 峰值→谷值, 盈利单 MAE=0 时回撤被记 0, 系统性低估。
        #   改用 mfe/mae (均已含 peak/trough 反算+现实兜底) 反算 peak-to-trough 回撤:
        #     peak=entry*(1+mfe/100), trough=entry*(1-mae/100)
        #     回撤(%) = (peak-trough)/peak*100 = 100*(mfe + mae) / (100 + mfe)
        #   数据缺失 (mfe≤0 或 mae≤0) → 0, 与历史一致不引入新值。
        # 2026-08-07 复核修复 (缺陷C, 两处): ①_mae_pct 是正数口径 (calc>0 才赋值,
        #   亏损兜底 -pnl*100 亦为正; DB 574 笔 mae<0 共 0 笔), 原条件 `_mae_pct < 0`
        #   恒 False → max_drawdown 恒 0, 比修复前更退化; ②原公式 (mfe-mae)/(100+mfe)
        #   输出比例量纲, 与 mfe/mae 的百分比量纲差 100 倍 (注释自述"同量纲"不成立)。
        #   mfe/mae 均已方向归一化 (SHORT 对称), 本公式 LONG/SHORT 通用。
        _max_dd_val = _max_drawdown_from_mfe_mae(_mfe_pct, _mae_pct)
        # DEB-20261104 D2: 时间检查点 peak 落库 (缺失留 NULL, 绝不猜 — 军规: 不得用
        #   默认值填充会制造"看起来有留痕"的假象). 单位 = 百分比, 与 mfe_pct 同.
        #   _mfe_t15_pct = 入场后 15min 检查点 (速死族前置读数)
        #   _mfe_gate_pct = 入场后 60min 检查点 (豁免/死信号判定口径)
        _mfe_t15_pct_db = None
        _mfe_gate_pct_db = None
        try:
            _raw15 = _meta.get("_mfe_t15_pct")
            if _raw15 is not None:
                _mfe_t15_pct_db = round(float(_raw15), 4)
        except (TypeError, ValueError):
            _mfe_t15_pct_db = None
        try:
            _raw60 = _meta.get("_mfe_gate_pct")
            if _raw60 is not None:
                _mfe_gate_pct_db = round(float(_raw60), 4)
        except (TypeError, ValueError):
            _mfe_gate_pct_db = None
        if close_futu_order_id:
            _sql_new = """
                UPDATE trade_records
                SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, close_futu_order_id=?, mfe_pct=?, mae_pct=?, max_drawdown=?, mfe_tick_pct=?, mfe_t15_pct=?, mfe_gate_pct=?
                WHERE id=? AND (exit_time IS NULL OR exit_time='') AND (exit_price IS NULL OR exit_price=0)
            """
            _sql_old = """
                UPDATE trade_records
                SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, close_futu_order_id=?, mfe_pct=?, mae_pct=?, max_drawdown=?
                WHERE id=? AND (exit_time IS NULL OR exit_time='') AND (exit_price IS NULL OR exit_price=0)
            """
            _params_old = (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), close_futu_order_id, round(_mfe_pct, 4), round(_mae_pct, 4), _max_dd_val, trade_id)
            _params_new = _params_old[:-1] + (round(_mfe_tick_pct, 4), _mfe_t15_pct_db, _mfe_gate_pct_db, trade_id)
        else:
            _sql_new = """
                UPDATE trade_records
                SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, mfe_pct=?, mae_pct=?, max_drawdown=?, mfe_tick_pct=?, mfe_t15_pct=?, mfe_gate_pct=?
                WHERE id=? AND (exit_time IS NULL OR exit_time='') AND (exit_price IS NULL OR exit_price=0)
            """
            _sql_old = """
                UPDATE trade_records
                SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, mfe_pct=?, mae_pct=?, max_drawdown=?
                WHERE id=? AND (exit_time IS NULL OR exit_time='') AND (exit_price IS NULL OR exit_price=0)
            """
            _params_old = (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), round(_mfe_pct, 4), round(_mae_pct, 4), _max_dd_val, trade_id)
            _params_new = _params_old[:-1] + (round(_mfe_tick_pct, 4), _mfe_t15_pct_db, _mfe_gate_pct_db, trade_id)
        # 缺列 fail-open (同族取舍, 见 AGENT.md 军规"新增落库字段"与 intraday_indicators 同款):
        #   旧库/最小夹具 DB 没有新列时, UPDATE 在 PREPARE 阶段就失败 (无任何行被写) ⇒
        #   可安全回退到不含新列的原语句。**只吞 "no such column"**, 其它异常照旧抛出
        #   (不得把真实写失败静默掉)。
        try:
            _closed_row = conn.execute(_sql_new, _params_new)
        except sqlite3.OperationalError as _new_col_err:
            if "no such column" not in str(_new_col_err).lower():
                raise
            logger.debug(
                "[_close_trade_record] mfe 口径新列缺失 (旧库/夹具), 回退旧语句: %s", _new_col_err)
            _closed_row = conn.execute(_sql_old, _params_old)
        if _closed_row.rowcount != 1:
            raise ValueError("exit owner row is no longer open")
        # 2026-07-30 (DEB-fee-net-pnl): 净额列跟随主 UPDATE 落库 — pnl_net = 毛 - 估算费;
        #   三态只升不降 (broker/reconciled 不被 est 覆盖); 旧库/测试库无列 → 静默跳过。
        if _fees_total is not None:
            try:
                _net_pnl = round(round(pnl, 2) - _fees_total, 2)
                _fee_notional = float(_tr[2] or 0) * float(_tr[4] or 0)
                _net_pnl_pct = (
                    round(_net_pnl / _fee_notional, 4)
                    if _fee_notional > 0 else None
                )
                conn.execute(
                    "UPDATE trade_records SET fees_est=?, pnl_net=?, net_pnl_pct=?, "
                    "fees_source=CASE WHEN fees_source IN ('broker','reconciled') "
                    "THEN fees_source ELSE 'est' END "
                    "WHERE id=?",
                    (_fees_total, _net_pnl, _net_pnl_pct, trade_id))
            except Exception as _fee_col_err:
                logger.debug(f"[_close_trade_record] 费用列写入跳过 trade_id={trade_id}: {_fee_col_err}")
        # instruction 表的 MFE/MAE 列历史语义是【美元金额】(shadow_tracker 写 (high-price)*size,
        #   feishu 显示 ${mfe:.0f}), 而 _mfe_pct/_mae_pct 是百分比 → 按 notional 换算成美元后落库,
        #   与所有既有消费者单位一致. notional = entry_price * quantity.
        _mfe_usd = 0.0
        _mae_usd = 0.0
        try:
            # #6: 复用 _tr (原此处独立 SELECT entry_price/quantity)
            if _tr is not None:
                _notional = float(_tr[2] or 0) * float(_tr[4] or 0)
                _mfe_usd = round(_mfe_pct / 100.0 * _notional, 2)
                _mae_usd = round(_mae_pct / 100.0 * _notional, 2)
        except Exception:
            pass

        if inst_id:
            # E2 契约 (2026-07-19, DEB-round2 Ω.3-5): exit_reason 含 ORPHAN/PHANTOM/GHOST → outcome=void.
            #   ghost 平仓 (含无报价兜底 PnL=0) 经此标 void, 不计 win/loss 统计 — get_summary_stats 仅计
            #   outcome IN (win,loss), void 被排除, 故无报价 ghost 不污染胜率/净PnL. trade_records 无 outcome
            #   列, 基于它的统计须过滤 exit_reason NOT LIKE '%GHOST%'.
            _is_ghost = "ORPHAN" in exit_reason.upper() or "PHANTOM" in exit_reason.upper() or "GHOST" in exit_reason.upper()
            # 2026-08-20 (用户裁"切"): outcome/pnl/pnl_pct 全净口径 — 毛>0但≤费用的单=假盈利,
            #   8/1-8/20 实测 125 笔中 23 笔假盈利, 毛胜率虚高 18pp. pnl_net 已在本函数算出
            #   (_fees_total); 费用估算失败→回退毛判定 (fail-open, 不阻断). instruction.pnl 语义
            #   从此=净, 下游周报/推送/因子分析/Obsidian 自动继承, 无需逐处改.
            _inst_pnl, _inst_pnl_pct = _net_pnl_pair(pnl, pnl_pct, _fees_total, _tr)
            _outcome = "void" if _is_ghost else ("win" if _inst_pnl > 0 else "loss")
            # 2026-07-16 P0 修复: 幂等条件加入 'void', 防止已标 void 的幽灵仓被重复处理.
            #   旧逻辑 WHERE outcome NOT IN ('win','loss') — void 不在排除列表 →
            #   幽灵仓可被重复平仓写入. 修复: 排除 'void' 终态.
            # 2026-08-25 RFC-066: NULL 陷阱修复 — `outcome NOT IN (...)` 对 outcome=NULL
            #   行恒为 NULL (falsy) → 结算 UPDATE 静默不触发。新轮次行经 save_instruction
            #   INSERT 时 outcome 初始为 NULL, 若不显式放行会永远无法结算 (8/17-8/21 15 行
            #   out=NULL 实锤)。改为 (outcome IS NULL OR outcome NOT IN (...))。
            conn.execute(
                "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_time=?, exit_reason=?, "
                "pnl=?, pnl_pct=?, "
                "holding_period_hours=?, max_favorable_excursion=?, max_adverse_excursion=? "
                "WHERE id=? AND (outcome IS NULL OR outcome NOT IN ('win','loss','void'))",
                (_outcome, exit_price, now_iso, exit_reason, _inst_pnl,
                 _inst_pnl_pct, round(holding_hours, 1),
                 _mfe_usd, _mae_usd, inst_id),
            )

        # A symbol is not an entry identity. Never remove a replacement position.
        _cleanup_original = False
        if symbol:
            _other_open = conn.execute(
                "SELECT 1 FROM trade_records WHERE symbol=? AND id<>? "
                "AND (COALESCE(exit_time,'')='' OR COALESCE(exit_price,0)=0) LIMIT 1",
                (symbol, trade_id),
            ).fetchone()
            _position_cursor = conn.execute("SELECT * FROM open_positions WHERE symbol=?", (symbol,))
            _position_row = _position_cursor.fetchone()
            if _other_open is None:
                if _position_row is None:
                    _cleanup_original = True
                else:
                    _position = dict(zip((item[0] for item in _position_cursor.description), _position_row))
                    _position_identity = {
                        "direction": _tr[1], "size": _tr[4],
                        "avg_entry_price": _tr[2], "entry_time": _tr[3],
                    }
                    # Playbook stores the entry order in entry_broker_order_id.
                    # A changed entry ID disproves ownership even when the other
                    # fields happen to match; sl_broker_order_id is a STOP order.
                    if _position.get("entry_broker_order_id"):
                        _original_order = conn.execute(
                            "SELECT futu_order_id FROM trade_records WHERE id=?", (trade_id,),
                        ).fetchone()[0]
                        _position_identity["entry_broker_order_id"] = str(_original_order or "")
                    if all(value is not None and _position.get(key) == value
                           for key, value in _position_identity.items()):
                        _deleted = conn.execute(
                            "DELETE FROM open_positions WHERE symbol=? AND direction=? AND size=? "
                            "AND avg_entry_price=? AND entry_time=? "
                            "AND (sync_status IS NULL OR sync_status NOT IN "
                            "('closed_by_daemon','closed_by_sync','closed_emergency','stale_not_in_broker'))",
                            (symbol, _tr[1], _tr[4], _tr[2], _tr[3]),
                        )
                        _cleanup_original = _deleted.rowcount == 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    def _can_cleanup_symbol():
        # Recheck after commit too: another writer may have opened the next entry.
        try:
            return _cleanup_original and not conn.execute(
                "SELECT 1 FROM open_positions WHERE symbol=?", (symbol,),
            ).fetchone() and not conn.execute(
                "SELECT 1 FROM trade_records WHERE symbol=? AND id<>? "
                "AND (COALESCE(exit_time,'')='' OR COALESCE(exit_price,0)=0) LIMIT 1",
                (symbol, trade_id),
            ).fetchone()
        except Exception:
            return False

    # 2026-07-13: 退出参数影子 — close 时对比"更紧 breakeven/drawdown 变体"反事实 capture。
    #   record-only, 读 metadata._exit_shadow(各变体首触发)+peak/trough, 落 JSONL。自带 try/except。
    try:
        from app.playbook.exit_shadow import finalize_exit_shadow
        # #6: 复用 _tr/_meta (finalize_exit_shadow 只读 meta 不回写, 共享安全)
        if _meta_parse_ok:
            finalize_exit_shadow(
                symbol=symbol or "", direction=(_tr[1] or "LONG"),
                entry_price=float(_tr[2] or 0), live_exit_price=float(exit_price or 0),
                peak_price=float(_meta.get("peak_price", 0) or 0),
                trough_price=float(_meta.get("trough_price", 0) or 0),
                live_exit_reason=exit_reason, held_minutes=float(holding_hours or 0) * 60,
                meta=_meta, inst_id=inst_id,
            )
    except Exception:
        pass

    # ── 切实盘前风控接线: 平仓后喂给 record 端 (复用现有模块, fail-open) ──
    #   此前 Playbook 路径从不调用这两个 record, 导致 ACB 连亏/日亏计数恒为0、
    #   PDT 日内交易数恒为0 → 绝对熔断与 PDT 限制在实盘形同虚设.
    #   幽灵/孤儿平仓不计入风控统计 (非真实交易结果).
    _rc_is_ghost = any(_k in (exit_reason or "").upper() for _k in ("ORPHAN", "PHANTOM", "GHOST"))
    if symbol and not _rc_is_ghost:
        try:
            from app.risk.absolute_circuit_breaker import get_absolute_circuit_breaker
            # 2026-08-28 全链路复查: 毛→净, 与 ACB 启动重建 (_restore_from_db 读
            # COALESCE(pnl_net, realized_pnl)) 同尺。此前喂毛值 → 8/20 用户裁"切"点名的
            # "假盈利"行 (毛>0 净<0, 8/1-8/20 实测 23/125) 在熔断侧被当赢单清零连亏计数,
            # 且日亏累计少计费用。费用估算缺失时 _net_pnl_pair 自回退毛值 (fail-open)。
            _acb_pnl = _net_pnl_pair(pnl, pnl_pct, _fees_total, _tr)[0]
            _acb_target = "real" if _tr and _tr[5] == "PLAYBOOK_LIVE" else "sim"
            get_absolute_circuit_breaker(_acb_target).record_trade_result(
                "playbook", float(_acb_pnl), symbol
            )
        except Exception as _acb_e:
            # 2026-10-04 (RFC-132 复查): debug → warning.
            #   原 logger.debug 在生产 LOG_LEVEL=INFO 下**根本不落盘**, 故 ACB 记账失败
            #   (连亏计数/日亏累计不更新) 在真实环境完全不可见 — 埋点禁 except:pass 同型。
            #   触发面: RFC-132 改 per-arm get_absolute_circuit_breaker(_acb_target) 后,
            #   任何取实例/记账异常都会走此分支; 静默 ⇒ 熔断在实盘形同虚设。
            #   不 re-raise: 平仓链路不得因风控记账失败而中断 (fail-open 语义保持)。
            logger.warning(
                "[风控接线] ACB record 跳过 %s (target=%s pnl=%s) — 熔断计数未更新, 需人工核对",
                symbol, _acb_target, _acb_pnl,
                exc_info=True,   # W6_except_stack: 有告警无栈等于让值班人猜根因
            )
        # #6: 复用 _tr (原此处独立 SELECT entry_time)
        _entry_time_val = _tr[3] if _tr is not None else None
        # 2026-07-21 修复: PDT record_close 调用移至本函数 conn.commit() 之后 (原在此处).
        #   根因: 原调用点在主DB事务中间 (UPDATE 后 commit 前), PDT 用独立连接写同库
        #   → WAL 单写者原则下自己等自己 → 死等 30s 超时, PDT 必失败 + 平仓链路卡 30s,
        #   同窗口 capital_safety._save_state (timeout=5s) 也撞锁失败. 7/21 每次平仓 100% 复现.
        #   回滚: 把 commit 后的 PDT 块移回此处即可.

        # 2026-07-19 (DEB-round3): 批量止损熔断 — shadow 告警版 (命中即告警, 不阻断).
        #   背景: 7/6 六连分钟级止损 (-$312) 疑似系统性失效; 实单熔断需 2 周 shadow 数据定阈值.
        #   规则: 当日 PLAYBOOK_SIM/LIVE 平仓中 holding≤0.1h 且亏损 ≥2 笔 → HIGH 告警 (当日去重).
        #   口径说明: holding_hours 落库已 round(.,1), <5min 的原始时长只能存成 0.0/0.1,
        #   故用 <=0.1 覆盖"分钟级"语义 (0.1h=6min 上限, 贴近 5min 设计意图).
        try:
            from app.playbook.config import get_playbook_config as _get_pbcfg_sl
            if bool(_get_pbcfg_sl().get("sl_burst_alert_enabled", True)):
                _sl_today = now_iso[:10]
                _burst_n = conn.execute(
                    "SELECT COUNT(*) FROM trade_records "
                    "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                    "AND substr(exit_time,1,10)=? "
                    "AND holding_hours <= 0.1 AND realized_pnl < 0 "
                    "AND exit_reason NOT LIKE '%GHOST%' "
                    "AND exit_reason NOT LIKE '%ORPHAN%' "
                    "AND exit_reason NOT LIKE '%PHANTOM%'",
                    (_sl_today,),
                ).fetchone()[0]
                if _burst_n >= 2 and _sl_today not in _sl_burst_alerted:
                    _sl_burst_alerted.add(_sl_today)
                    logger.error(
                        f"[playbook] 批量止损熔断 shadow: {_sl_today} 已 {_burst_n} 笔分钟级亏损平仓 "
                        f"(最新 {symbol} {exit_reason}) — 疑似系统性失效, 请人工复核"
                    )
                    try:
                        from app.push import get_push_hub
                        from app.push.message import Level as _SLLevel
                        get_push_hub().push_text(
                            f"🚨 [Playbook熔断shadow] {_sl_today} {_burst_n} 笔分钟级亏损平仓 "
                            f"(最新 {symbol} {exit_reason}). 疑似系统性失效, 请人工复核 (不阻断).",
                            level=_SLLevel.HIGH,
                        )
                    except Exception:
                        pass
        except Exception as _slb_e:
            logger.debug(f"[playbook] 批量止损 shadow 检查异常 (不影响平仓): {_slb_e}")


    # G-09/A5 (2026-09-05 审计): playbook 平仓必须标 tombstone。
    #   根因: 全仓唯一标记点在 app/pipeline/order_executor.py:5091 (主链平仓路径),
    #   playbook 平仓回路从不标 → 本函数上方已 DELETE open_positions, 而 broker 结算
    #   存在延迟窗口, position_sync 在窗口内看到 "broker 有 / DB 无" → 复活为主链幽灵仓
    #   (7/15 RIVN/NVDA/XOM 双平事故家族; 账本实证 BROKER_GHOST_CLOSE 现行制度 10 笔)。
    #   挂钩点选本函数而非各 close_position 调用点: 本函数是 7 条平仓路径的唯一簿记收口,
    #   逐点补写必漏 (T2 "修而不彻" 正是上批修复的最大回归模式)。
    #   语义照抄主链样板: mark → confirm (broker 已确认成交) → 即时 sync。
    #   fail-open: 标记失败不影响已完成的平仓写入 (与本函数其余 post-commit 块一致)。
    #   回滚: 删本块即恢复旧行为。
    if symbol and _can_cleanup_symbol():
        _tomb_sym = symbol
        try:
            from app.core.position_sync import get_us_position_sync
            _sync_tomb = get_us_position_sync()
            _sync_tomb.mark_closed_tombstone(_tomb_sym)
            _sync_tomb.confirm_tombstone(_tomb_sym)
            _sync_tomb.request_immediate_sync()
        except Exception as _tomb_err:
            logger.warning(
                f"[playbook平仓] tombstone 标记失败 {_tomb_sym} "
                f"(不影响平仓写入, 但结算延迟窗内可能被 sync 复活): {_tomb_err}"
            )

    # ── 再入场快TP门禁吞掉影子·结算 (2026-08-31, record-only, 见 app/playbook/reentry_tp_shadow.py) ──
    #   放 commit 之后: 只读 SELECT + append-only jsonl, 零平仓事务影响; 异常仅 warn 不阻断。
    try:
        from app.playbook.reentry_tp_shadow import settle_on_close as _rqtp_settle
        _rqtp_settle(conn, trade_id, exit_price, exit_reason)
    except Exception as _rqtp_se:
        logger.warning(f"[shadow·reentry_tp] 结算挂点跳过 trade_id={trade_id}: {_rqtp_se}")

    # 2026-07-21 修复: 平仓落库后同步注销 ManualHoldRegistry.
    #   根因: 开仓时注册 (playbook_service "防 auto pipeline 干扰") 但平仓路径从不注销,
    #   进程内残留 → position_monitor 对账把已平仓标的一直报"跳过手动持仓",
    #   且 registry 被陈旧条目污染 (仅服务重启才清零). fail-open: 注销异常不影响平仓.
    #   回滚: 删除本块即可 (仅影响对账提示与手动仓判定, 不影响平仓写入).
    if symbol and _can_cleanup_symbol():
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry as _mhr_close
            if _mhr_close.get_hold_info(symbol) is not None:
                _mhr_close.unregister(
                    symbol,
                    account="real" if _tr and _tr[5] == "PLAYBOOK_LIVE" else "sim",
                )
        except Exception as _unreg_e:
            logger.debug(f"[playbook] ManualHoldRegistry 注销跳过 {symbol}: {_unreg_e}")
    
    # 2026-07-21: PDT 日内交易记录 (从事务内移至 commit 后, 消除 WAL 自锁; 详见函数中部注释).
    #   _entry_time_val/_rc_is_ghost 在上方已定义, 此处仅复用; fail-open 不影响平仓结果.
    if symbol and not _rc_is_ghost and _entry_time_val:
        try:
            from app.risk.pdt_guard import get_pdt_guard
            get_pdt_guard().record_close(symbol, _entry_time_val)
        except Exception as _pdt_e:
            logger.debug(f"[风控接线] PDT record 跳过 {symbol}: {_pdt_e}")

    # 2026-07-03: PLAYBOOK_SIM 平仓后自动归因 (之前只在 manual_order_service 调用, 212笔0归因)
    if symbol and exit_price > 0:
        try:
            from app.analysis.trade_attribution import compute_attribution
            # #6: 复用 _tr/_meta (原此处重读一次 metadata 以拿到 telemetry/ghost 写后的最新状态;
            #   共享 _meta 已含同一内容, 无需回读 DB)
            if _tr is not None:
                # 7/7 增强: 传入 peak_price 用于真实 MFE 计算
                # 2026-07-13: 同时传 trough_price (07-11 起记录), 让入场时机/止损效率维度用真实谷值
                _peak_price = None
                _trough_price = None
                try:
                    _peak_price = float(_meta.get("peak_price", 0)) or None
                    _trough_price = float(_meta.get("trough_price", 0)) or None
                except Exception:
                    pass
                _attr = compute_attribution(
                    symbol=symbol,
                    direction=_tr[1] or "LONG",
                    entry_price=float(_tr[2] or 0),
                    exit_price=exit_price,
                    quantity=int(_tr[4] or 0),
                    entry_time=_tr[3] or now_iso,
                    exit_time=now_iso,
                    peak_price=_peak_price,
                    trough_price=_trough_price,
                )
                logger.info(f"[归因] {symbol}: {_attr.summary} | {_attr.improvement}")
                # 写入 metadata_json (与 manual_order_service 一致)
                _meta["attribution"] = _attr.to_dict()
                conn.execute(
                    "UPDATE trade_records SET metadata_json=? WHERE id=?",
                    (json.dumps(_meta, ensure_ascii=False, default=str), trade_id)
                )
                conn.commit()
        except Exception as _attr_e:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except Exception:
                pass
            logger.warning(f"[归因] {symbol} 自动归因失败 (不影响平仓): {_attr_e}", exc_info=True)

    # 2026-10-08: LIVE-only fee truth reconciliation. Futu can return actual
    # order fees after fills, while SIM cannot. This upgrades the LIVE ledger
    # and push message to broker-authoritative fees without changing strategy
    # decisions or touching SIM rows. Failure is observable but never blocks exit.
    if _tr is not None and _tr[5] == "PLAYBOOK_LIVE":
        try:
            from app.execution.live_fee_reconciliation import (
                fee_discrepancy_is_material,
                reconcile_live_order_fees,
            )
            # 2026-10-08 复查修复: 优先复用调用链已持有的 broker (测试注入 FakeDualBroker
            # 时不再触发 get_dual_broker() 单例 → 真实 Futu socket 污染 I/O guard)。
            if fee_broker is None:
                from app.execution.dual_broker import get_dual_broker
                fee_broker = get_dual_broker()
            _fee_reconcile = reconcile_live_order_fees(fee_broker, conn, trade_id)
            if _fee_reconcile.get("status") == "ok":
                _fees_total = float(_fee_reconcile["fees_total"])
                if fee_discrepancy_is_material(
                    _fee_reconcile.get("estimated_total"), _fees_total
                ):
                    try:
                        from app.push import get_push_hub
                        from app.push.message import Level
                        get_push_hub().push_text(
                            f"⚠️ [LIVE费用对账] {symbol} 券商实收费=${_fees_total:.2f}, "
                            f"估算=${_fee_reconcile.get('estimated_total'):.2f}, "
                            f"差异=${_fee_reconcile.get('difference'):.2f}",
                            level=Level.HIGH,
                        )
                    except Exception as _fee_push_error:
                        logger.warning(
                            f"[LIVE费用对账] {symbol} 差异推送失败: {_fee_push_error}",
                            exc_info=True,
                        )
            elif _fee_reconcile.get("status") == "failed":
                logger.warning(
                    f"[LIVE费用对账] {symbol} trade_id={trade_id} 失败: "
                    f"{_fee_reconcile.get('reason')}"
                )
                try:
                    from app.push import get_push_hub
                    from app.push.message import Level
                    get_push_hub().push_text(
                        f"🔴 [LIVE费用对账] {symbol} 券商实收费获取失败, 净额暂用估算: "
                        f"{_fee_reconcile.get('reason')}",
                        level=Level.HIGH,
                    )
                except Exception as _fee_push_error:
                    logger.warning(
                        f"[LIVE费用对账] {symbol} 失败推送失败: {_fee_push_error}",
                        exc_info=True,
                    )
        except Exception as _fee_reconcile_error:
            logger.warning(
                f"[LIVE费用对账] {symbol} trade_id={trade_id} 异常 (不影响平仓): "
                f"{_fee_reconcile_error}",
                exc_info=True,
            )

    # 2026-07-16: playbook 平仓逐笔飞书推送 (复用 PushHub trade_exit)。
    #   来源精准标注 — 按 trade_records.source_type 映射 strategy (PLAYBOOK_SIM→playbook_sim,
    #   PLAYBOOK_LIVE→playbook_live); shadow 仓不推 (纯影子追踪, 非真实成交)。
    #   自带 try/except, 绝不影响平仓。dedup 用 trade_id 保证每笔平仓唯一推送。
    try:
        # #6: 复用 _tr (原此处独立 SELECT direction/source_type)
        _pb_src_type = ((_tr[5] if _tr is not None else "") or "").upper()
        _pb_strategy_map = {"PLAYBOOK_SIM": "playbook_sim", "PLAYBOOK_LIVE": "playbook_live"}
        _pb_strategy = _pb_strategy_map.get(_pb_src_type)
        if symbol and _pb_strategy:
            from app.push import get_push_hub, trade_exit as _pb_exit
            # 2026-07-23: 平仓通知画像显性化 — 路径(MFE/MAE/表态时点/capture)、
            #   持有时长、60min表态判定(E shadow 同源)、cohort/horizon 分桶。
            #   全部取自 _meta 已有字段, 组装失败降级为原精简版, 不影响平仓。
            _px_extra = {}
            # [观察点1 2026-09-29, 修 v2] 跨夜数 (ET 日历日差): held_h 是 RTH-only 口径,
            #   隔夜单通知需显式标注防误读为日内单 (MU 9/29 案: 6.1h 实跨 19h)。
            #   ⚠️ v1 曾读 _tr[7] (exit_time 列) — 但 _tr 在本函数 UPDATE 之前 SELECT
            #   (L6571), 此刻 exit_time 列仍为 NULL → 恒 0 → 标注永不出现 (COIN 9/29
            #   08:21ET 隔夜单实证补丁是死的)。正确退出时刻 = 局部变量 now_iso
            #   (L6884 UPDATE exit_time 用的同值), 入场 = _tr[3] (UPDATE 不碰它)。
            #   解析失败/同日 → 0 (渲染端不加标注, 外观与旧版一致)。
            _overnights_et = 0
            try:
                from app.utils.timezone import parse_iso_safe as _pt, to_et as _to_et
                _et_entry = _to_et(_pt(_tr[3])) if _tr is not None and _tr[3] else None
                _et_exit = _to_et(_pt(now_iso))
                if _et_entry and _et_exit:
                    _overnights_et = max(0, (_et_exit.date() - _et_entry.date()).days)
            except Exception:
                _overnights_et = 0
            try:
                _entry_px = float(_tr[2] or 0) if _tr is not None else 0
                _mfe = float(_meta.get("mfe_pct", 0) or 0)
                # 2026-09-25 P2口径修 (MU 9/25 案): 最终 mfe 若来自峰谷重算/落袋兜底 (大于累积值),
                #   meta 里的 time_to_mfe 对应的仍是旧累积峰 (如 9/23 早段小峰), 时点已失真
                #   → 推送不再显示 "(Xmin到峰)", 防把到达时点与新口径峰值混读。
                _t2m_push = _meta.get("time_to_mfe")
                _t2m_tr_push = _meta.get("time_to_mfe_trading_min")
                if _mfe > _mfe_accum + 1e-9:
                    _t2m_push = _t2m_tr_push = None
                _px_extra = {
                    "entry_price": _entry_px or None,
                    "exit_price": float(exit_price or 0) or None,
                    "held_h": round(float(holding_hours or 0), 1),
                    # [RFC-117 P2 复核观察点1 2026-09-29, 用户裁"继续"落地] 跨夜标注:
                    #   held_h 是 RTH-only 口径 (holding_hours 列), 隔夜单墙钟差远大于它
                    #   (MU 9/29 案: 显示 6.1h 实际跨 19h), 用户易误读为当日日内单。
                    #   按 entry/exit 的 ET 日历日差算跨夜数; 0=同日单不加标注 (外观不变)。
                    "overnights": _overnights_et,
                    "mfe_pct": _mfe if _meta.get("mfe_pct") is not None else None,
                    "mae_pct": float(_meta.get("mae_pct", 0) or 0),
                    "time_to_mfe": _t2m_push,
                    "time_to_mfe_trading_min": _t2m_tr_push,
                    "cohort": _meta.get("cohort"),
                    "horizon": _meta.get("horizon"),
                    "inv_price": _meta.get("alite_inv_price"),
                    # [2026-09-16 用户裁"同意按建议执行"第1条] 生效SL观测性 — ARM 9/15 案:
                    #   SL_INITIAL 推送只见初始SL, 真杀仓的紧SL线要翻日志。exit_engine 规则1
                    #   触发瞬间写 _effective_sl_at_exit/_tight_sl_killed, 渲染端区分展示。
                    "effective_sl": float(_meta.get("_effective_sl_at_exit", 0) or 0) or None,
                    "tight_sl_triggered": bool(_meta.get("_tight_sl_killed", False)) or None,
                    # 2026-08-21 (用户裁"落"): BE 出场分类 — 通知直接区分"真保本/贴线残差/瀑布坠穿",
                    #   消除"保本名义亏损"歧义. drift 仅 GAP 路径有意义 (bps, 负=坠穿).
                    "be_exit_class": _meta.get("be_exit_class"),
                    "be_drift_bps": (_meta.get("_be_close_telemetry") or {}).get("drift_from_be_line"),
                    # 2026-09-25 P3 (TDOC 9/25 案; 用户裁「要」): BE 几何退化标记上推送。
                    #   fee+slip ≥ breakeven_at_pct ⇒ 武装即触发, 净利上限=MFE−fee 常只剩几 bps。
                    #   推送不提示时, 读到「保本线出场 净-$1.31」无法知道它从武装那刻就注定触发。
                    "be_degenerate": bool(_meta.get("be_geometry_degenerate")) or None,
                    "be_headroom_pct": _meta.get("_be_headroom_pct"),
                }
                if _mfe > 0:
                    # [hotfix 2026-10-02] capture 钳位放开 (INTC 10/1 案): 旧 max(0.0,·)
                    #   把负捕获 (实际盈亏吐穿 MFE, 真值 -8.7%) 钳成 0 → 推送恒显
                    #   "capture 0%", 读者以为没吐回。去掉下限, 负值如实落 payload;
                    #   上限 min(1.0,·) 保留 (MFE 为峰值, 捕获>100% 只可能来自费/舍入)。
                    #   渲染端 (message.py) 同步 :.0f→:.1f 显示一位小数。
                    _px_extra["capture_pct"] = round(
                        min(1.0, float(pnl_pct or 0) * 100.0 / _mfe) * 100, 1)
                _e60 = _meta.get("e_shadow_60m")
                if isinstance(_e60, dict):
                    _px_extra["e60_ok"] = float(_e60.get("mfe_pct", 0) or 0) >= 0.3
            except Exception:
                _px_extra = {}
            get_push_hub().push(_pb_exit(
                symbol=symbol,
                direction=(_tr[1] if _tr is not None else "LONG") or "LONG",
                pnl=round(float(pnl or 0), 2),
                exit_reason=exit_reason,
                strategy=_pb_strategy,
                trace_id=str(trade_id),
                source_type=_pb_strategy,
                extra=_px_extra,
                # 2026-07-30 (DEB-fee-net-pnl G5): 净额主口径 — _fees_total 由本函数
                #   前段费用估算提供, None (估算失败/旧库) 时消息自动退回毛口径。
                fees=_fees_total,
                pnl_net=(round(round(float(pnl or 0), 2) - _fees_total, 2)
                         if _fees_total is not None else None),
            ))
    except Exception as _pb_push_e:
        logger.warning(f"[playbook推送] {symbol} 平仓推送失败 (不影响平仓): {_pb_push_e}")
    return True
