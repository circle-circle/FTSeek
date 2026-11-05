"""出场监控流 (阶段13迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict


def monitor_playbook_exits(
    scheduler,
    *,
    monitor_playbook_exits: Callable,
    datetime_cls: Callable,
    time_module: Any,
    logger: Any,
    _PB_EXIT_PRICE_CACHE_TS: Dict[str, float],
    _PB_EXIT_FETCH_TIMEOUT: float,
    _PB_EXIT_FETCH_SINGLE_TIMEOUT: float,
    _PB_EXIT_STALE_ALERT_SEC: float,
    _PB_CLOSED_TWIN_NOT_EXISTS: str,
    _quote_fail_streak: dict,
    _quote_fail_alerted: set,
    _alert_close_failure: Callable,
    _book_playbook_exit_result: Callable,
    _check_playbook_exit_rules: Callable,
    _extract_invalidation_price: Callable,
    _get_factor_exit_config: Callable,
    _is_overnight_position: Callable,
    _load_horizon_config: Callable,
    _pb_exit_bounded_fetch: Callable,
    _pb_exit_price_cache_update: Callable,
    _pb_exit_price_cache_view: Callable,
    _playbook_exit_decision: Callable,
    _playbook_exit_owner: Callable,
    _portfolio_backstop_evaluate: Callable,
    _reconcile_broker_stop_exit: Callable,
    _release_live_broker_stop_for_rule_exit: Callable,
    _resume_playbook_exits: Callable,
    _sl_fallback_exit_reason: Callable,
    _track_bar_peak_trough: Callable,
    patient_exit_arm_eligible: Callable,
    patient_exit_eval_wait: Callable,
    patient_exit_trigger_telemetry: Callable,
    _handle_live_rule_exit_failure: Callable | None = None,
) -> int:

    """盘中监控 PLAYBOOK_SIM / PLAYBOOK_LIVE 真实仓位的退出.

    与 monitor_playbook_shadow_exits 的区别: PLAYBOOK_SIM/PLAYBOOK_LIVE 是券商账户
    的真实持仓, 退出时必须调 DualBrokerAdapter.close_position 按臂真实平仓,
    而非仅更新 trade_records 字段.

    退出规则 (与 shadow 对齐, 复用 exit_policy 阈值):
      [1] SL_INITIAL: 当前价触及原始 stop_loss (兜底)
      [2] TIME_EXIT: 持仓>2h 且 盈利<-0.5% → 退出 (2026-06-24放宽)
      [3] PRE_CLOSE_EXIT: 收盘前 8min → 全平

    Returns: 本轮平仓数量
    """
    conn = None
    _arm_connections = {}
    exited = 0
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db_for_arm
        from app.core.scheduler_triggers import ET as _et
        # 注: datetime 用模块级 import (顶部 L23) — 局部 import 会遮蔽模块名,
        #     使测试无法经 pipeline_mod.datetime 冻结时钟 (2026-09-02 钉修复配套).

        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        phase = get_market_phase()
        exited, blocked_exits, recovery_ok = _resume_playbook_exits(
            allow_submit=phase == MarketPhase.TRADING)
        if phase != MarketPhase.TRADING or not recovery_ok:
            return exited

        rows = []
        rows_by_arm = {}
        for _arm_target in ("sim", "real"):
            db_path = str(get_trade_records_db_for_arm(_arm_target))
            if not Path(db_path).exists():
                continue
            arm_conn = sqlite3.connect(db_path)
            arm_conn.row_factory = sqlite3.Row
            _arm_connections[db_path] = arm_conn
            source_type = "PLAYBOOK_SIM" if _arm_target == "sim" else "PLAYBOOK_LIVE"
            arm_rows = arm_conn.execute("""
                SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type,
                       decision_id, futu_order_id
                FROM trade_records
                WHERE source_type = ?
                  AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            """ + _PB_CLOSED_TWIN_NOT_EXISTS + """
                ORDER BY id DESC
            """, (source_type,)).fetchall()
            arm_rows = [dict(row) | {"arm_db_path": db_path} for row in arm_rows]
            rows.extend(arm_rows)
            rows_by_arm[db_path] = arm_rows

        if not rows:
            monitor_playbook_exits._last_n = 0
            return exited

        # 2026-07-31 降噪: 20s 循环每轮 INFO → 持仓数变化才 INFO, 不变 DEBUG (~180条/盘中h → ~0)
        if len(rows) != getattr(monitor_playbook_exits, "_last_n", None):
            logger.info(f"[playbook dual] 退出监控: {len(rows)} 个未平仓 sim/live 仓位待检查")
            monitor_playbook_exits._last_n = len(rows)
        else:
            logger.debug(f"[playbook dual] 退出监控: {len(rows)} 个未平仓 (不变)")

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook sim] 无 data_router, 跳过退出检查")
            return exited

        # 2026-07-21: 批量取价 (独立快循环配套) — get_batch_quotes 一次调用:
        #   Futu Push缓存优先(近零成本)→快照→Finnhub 逐个回落. 替代逐仓串行 get_quote
        #   (原单轮 ~48s, 20s 快循环节奏下会打爆 Finnhub 限额). 缺失标的循环内回落单原取价.
        #   回滚: 删除本块, 并把循环内 quote 来源改回 data_router.get_quote(symbol) 即可.
        _batch_quotes: dict = {}
        _pb_syms = [r["symbol"] for r in rows]
        try:
            # 2026-09-22: 批量取价加**上界** (原直调 get_batch_quotes 在 OpenD 瞬断时
            #   无上界阻塞 121s, 见模块上方 _pb_exit_* 说明)。超时/失败 → 兜底缓存。
            _bq, _bq_to, _bq_err = _pb_exit_bounded_fetch(
                "batch", lambda: data_router.get_batch_quotes(_pb_syms), _PB_EXIT_FETCH_TIMEOUT)
            if isinstance(_bq, dict):
                _pb_exit_price_cache_update(_bq)
                logger.debug(f"[playbook dual] 批量取价: {len(_bq)}/{len(rows)} 命中")
            if _bq_err is not None:
                logger.debug(f"[playbook dual] 批量取价失败, 本轮回落逐仓取价: {_bq_err}")
            elif _bq_to:
                logger.warning(
                    f"[playbook dual] 批量取价超时 (>{_PB_EXIT_FETCH_TIMEOUT:.0f}s, 报价源/连接故障?) "
                    f"→ 本轮用兜底价, 快循环不停摆"
                )
        except Exception as _bq_e:
            logger.warning(f"[playbook dual] 批量取价异常, 本轮回落逐仓取价: {_bq_e}", exc_info=True)
        # 兜底: 并入最近一次成功价 (仅未超龄; 超龄按无价 = 旧行为)
        _batch_quotes = _pb_exit_price_cache_view(_pb_syms)
        _pb_oldest_age = 0.0
        for _pb_s in _batch_quotes:
            _pb_oldest_age = max(_pb_oldest_age, time_module.time() - _PB_EXIT_PRICE_CACHE_TS.get(_pb_s, 0.0))
        if _pb_oldest_age > _PB_EXIT_STALE_ALERT_SEC:
            if not getattr(monitor_playbook_exits, "_pb_stale_alerted", False):
                monitor_playbook_exits._pb_stale_alerted = True
                logger.warning(
                    f"[playbook dual] 取价降级: 本轮用兜底价评估退出 (最旧 {_pb_oldest_age:.0f}s) "
                    f"— 快循环不停摆, 报价恢复即自动切回实时"
                )
        else:
                monitor_playbook_exits._pb_stale_alerted = False

        # 市场时段 (ET) — 复用 scheduler_triggers.ET (pytz, 处理夏令时)
        now_et = datetime_cls.now(_et)
        now_minutes = now_et.hour * 60 + now_et.minute
        # 2026-07-25 (隐患扫描修复): 半日市 (13:00 ET 提前收盘) 支持 — 原硬编码 16*60
        #   导致提前收盘日 PRE_CLOSE_EXIT/EOD 不触发, 日内仓被迫隔夜。兜底 960。
        try:
            from app.utils.holiday_calendar import get_us_close_minutes_et
            _close_min = get_us_close_minutes_et(now_et.date())
        except Exception:
            _close_min = 16 * 60
        minutes_to_close = _close_min - now_minutes
        market_open_minutes = 9 * 60 + 30
        minutes_from_open = now_minutes - market_open_minutes

        # M1 修复 (2026-07-16): 开盘冷却 SL 放宽参数 — 与 _check_playbook_exit_rules 规则0 对齐.
        #   原始 SL 硬兜底 (下方) 此前直接比原始 stop_loss, 绕过了冷却放宽,
        #   导致开盘 15min 内价格触及原始(较紧)SL 就被兜底先斩, 架空"防开盘秒杀"意图.
        #   此处预取冷却参数 (循环不变量), 兜底处按 entry_price 逐仓放宽 SL floor.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_m1
            _pb_cfg_m = _get_pb_cfg_m1()
            _cooldown_sl_pct = float(_pb_cfg_m.get("cooldown_sl_pct", 4.0))
        except Exception:
            _pb_cfg_m = {}
            _cooldown_sl_pct = 4.0
        _in_open_cooldown = minutes_from_open is not None and minutes_from_open < 15

        # ── G-01/A9 (2026-09-05 审计): playbook 组合级敞口聚合兜底 ──
        # 缺陷: playbook 是唯一活体执行链, 但组合级保护为零 — ExitMonitor 的
        #   FLAT_ALL/REDUCE_50 在 R-7c (app/risk/position_exit_monitor.py:781) 结构性排除
        #   playbook 仓; 本函数只有逐仓规则无组合聚合; _check_daily_loss_circuit 明确
        #   "触发停新开, 平仓照常"= entry-only; BLACKOUT 仅乘数/入场闸; sim 无 broker 侧
        #   STOP (app/execution/dual_broker.py:675 对 target_env!="real" 直接 skipped)。
        # 阈值设计 (吸取 2026-08-21 AMZN 误杀教训: 水深上限被开盘噪声打穿即清仓, 30min 后
        #   价格回到 +1.1%, -5% 的 SL 从未参与, 用户裁撤销 loser_overnight_max_depth_pct):
        #   阈值 = max(绝对下限, Σ逐仓SL距离 × 倍数)。Σ SL 距离是"所有逐仓 SL 同时按设计
        #   触发"的合计损失, 乘倍数后本兜底**只在逐仓 SL 集体失效时**才可能开火 →
        #   结构上不可能是盘中噪声触发, 不会重演 AMZN。
        # 保守口径: ① 仅 TRADING 相位 (上方相位门) + 批量报价命中的标的计入, 不用盘后失真价;
        #   ② 缺价标的**不计入分子也不计入分母** (宁少算敞口, 不用陈旧价触发全平)。
        # 动作边界 (刻意偏离 DEB-20260905 A9 的"全仓逐笔市价平"): 本函数自带 TRADING 相位门,
        #   进程死亡/休市/盘前盘后时它与兜底一起不跑 —— 那才是 G-01 的真实空洞, 在函数内
        #   加强平堵不到它; 而在函数内另建平仓回路会与逐仓路径的 close+簿记+tombstone 重复,
        #   引入双平/账实不符风险 (G-04/G-09 同族)。故 v1 = 检测 + critical 告警 + 推送,
        #   真实存活保护交外部监督层 (systemd Restart + freeze_sentinel 非 TRADING 存活检查)。
        #   升级路径: 攒到告警样本后, 若要接强平, 应复用逐仓 SL 分支的 close+簿记路径而非新建。
        #   回滚: playbook.portfolio_backstop.enabled=false (热载, 无需重启)。
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_bs
            _bs_raw = _get_pb_cfg_bs().get("portfolio_backstop", {}) or {}
        except Exception as _bs_cfg_e:
            logger.warning(f"[playbook组合兜底] 配置读取失败 → 用内置保守默认: {_bs_cfg_e}", exc_info=True)
            _bs_raw = {}
        _bs_cfg = {"enabled": True, "sl_loss_multiple": 1.5, "min_abs_loss_usd": 500.0,
                   "cooldown_sec": 300.0}
        if isinstance(_bs_raw, dict):
            for _bs_k in list(_bs_cfg):
                if _bs_k in _bs_raw:
                    _bs_cfg[_bs_k] = _bs_raw[_bs_k]

        if _bs_cfg.get("enabled") and _batch_quotes:
            # RFC-090 §17: portfolio backstop is evaluated per arm. SIM drawdown
            # must not trigger from LIVE exposure and vice versa.
            for _bs_arm_path, _bs_arm_rows in rows_by_arm.items():
                _bs_agg = _portfolio_backstop_evaluate(_bs_arm_rows, _batch_quotes, _bs_cfg)
                if _bs_agg is None:
                    continue
                _bs_n = _bs_agg["n_priced"]
                _bs_bad = _bs_agg.get("n_unusable_rows") or 0
                if _bs_bad and _bs_bad != getattr(monitor_playbook_exits, "_bs_last_bad_rows", 0):
                    monitor_playbook_exits._bs_last_bad_rows = _bs_bad
                    logger.warning(
                        f"[playbook组合兜底] {_bs_bad} 行无法计入聚合 (敞口按保守少算) | "
                        f"末条错误: {_bs_agg.get('unusable_last_err')}")
                _bs_cost = _bs_agg["cost_usd"]
                _bs_mkt = _bs_agg["market_usd"]
                _bs_sl_sum = _bs_agg["sl_loss_sum_usd"]
                _bs_unreal = _bs_agg["unrealized_usd"]
                _bs_thresh = _bs_agg["threshold_usd"]
                if _bs_agg["breached"]:
                    _bs_now_ts = time_module.time()
                    _bs_last_ts = getattr(monitor_playbook_exits, "_bs_last_alert_ts", 0.0)
                    if _bs_now_ts - _bs_last_ts >= float(_bs_cfg.get("cooldown_sec") or 300.0):
                        monitor_playbook_exits._bs_last_alert_ts = _bs_now_ts
                        _bs_pct = (_bs_unreal / _bs_cost * 100.0) if _bs_cost else 0.0
                        logger.critical(
                            f"🚨 [playbook组合兜底] arm={Path(_bs_arm_path).name} "
                            f"{_bs_n} 仓合并浮亏 ${-_bs_unreal:,.2f} "
                            f"({_bs_pct:.2f}%) ≥ 阈值 ${_bs_thresh:,.2f} "
                            f"(Σ逐仓SL距离 ${_bs_sl_sum:,.2f} × {_bs_cfg['sl_loss_multiple']}, "
                            f"绝对下限 ${_bs_cfg['min_abs_loss_usd']:,.0f}) — 逐仓 SL 已集体失效, "
                            f"成本 ${_bs_cost:,.0f} 市值 ${_bs_mkt:,.0f}"
                        )
                        try:
                            from app.push import get_push_hub
                            from app.push.message import Level as _BsLevel
                            get_push_hub().push_text(
                                f"🚨 [Playbook组合兜底] arm={Path(_bs_arm_path).name} "
                                f"{_bs_n} 仓合并浮亏 ${-_bs_unreal:,.2f} "
                                f"({_bs_pct:.2f}%) 越过组合阈值 ${_bs_thresh:,.2f}. "
                                f"逐仓 SL 未能兜住, 请人工核查是否全平. "
                                f"(G-01/A9, 阈值=ΣSL距离×{_bs_cfg['sl_loss_multiple']})",
                                level=_BsLevel.HIGH,
                            )
                        except Exception as _bs_push_e:
                            logger.warning(f"[playbook组合兜底] 告警推送失败: {_bs_push_e}")

        for r in rows:
            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            db_path = r["arm_db_path"]
            conn = _arm_connections[db_path]
            try:
                if (_row_target, r["symbol"]) in blocked_exits:
                    # 2026-10-17 审计 P0-1: 该标的退出规则被未结算 intent 整体冻结。
                    # record-only 采样 (节流 60s) 供 shadow 定 N/M; 行为不变。
                    try:
                        from app.playbook.blocked_exit_opportunity import observe as _obs_blocked
                        _obs_blocked(
                            r, blocked_exits[(_row_target, r["symbol"])],
                            _batch_quotes.get(r["symbol"]), source="exit_monitor")
                    except Exception as _blocked_shadow_err:
                        logger.warning(
                            "[playbook dual] blocked-exit 影子写入失败 (不影响监控): %s",
                            _blocked_shadow_err, exc_info=True)
                    continue
                symbol = r["symbol"]
                direction = r["direction"]
                entry_price = float(r["entry_price"] or 0)
                if entry_price <= 0:
                    continue

                meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                if not isinstance(meta, dict):
                    raise ValueError("trade metadata must be an object")
                stop_loss = float(meta.get("stop_loss", 0))
                inst_id = meta.get("playbook_instruction_id")
                qty = r["quantity"] or 0

                if _row_target == "real":
                    from app.execution.dual_broker import get_dual_broker
                    if _reconcile_broker_stop_exit(get_dual_broker(), conn, r, meta):
                        exited += 1
                        continue

                # 当前价 (2026-07-21: 批量快照优先, 缺失/无价的标的回落单原取价)
                current_price = None
                _quote_src = ""  # 2026-08-18 报价源遥测 (WDC/AMD 案: 触发价与1分K线矛盾待对账)
                _be_q_bid = 0.0  # 2026-09-25 P3: 初始化, 防 quote 缺失路径下 telemetry 构建 NameError
                _be_q_ask = 0.0
                _be_book_src = "quote_fallback"  # [hotfix 2026-10-02] 同款初始化 (见下方 L1 尺子修)
                try:
                    quote = _batch_quotes.get(symbol)
                    if not (isinstance(quote, dict) and (quote.get("last") or quote.get("price"))):
                        # 2026-09-22: 逐仓回落同样加界 (原 data_router.get_quote 直调无上界,
                        #   与批量取价叠加 = 本次开盘 OpenD 瞬断时 121s 停摆的另一半)。
                        #   超时 → 用兜底价; 两者都没有 → 走下方 R2 连续取价失败告警。
                        _q1, _q1_to, _q1_err = _pb_exit_bounded_fetch(
                            f"single:{symbol}", lambda _s=symbol: data_router.get_quote(_s),
                            _PB_EXIT_FETCH_SINGLE_TIMEOUT)
                        if isinstance(_q1, dict) and _q1:
                            _pb_exit_price_cache_update({symbol: _q1})
                            quote = _q1
                            _quote_src = "single_fallback"
                        else:
                            quote = _pb_exit_price_cache_view([symbol]).get(symbol)
                            _quote_src = "cached_fallback" if quote else ""
                        if _q1_err is not None:
                            logger.debug(f"[playbook sim] {symbol} 逐仓取价异常: {_q1_err}")
                    if quote and isinstance(quote, dict):
                        current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
                        if not _quote_src:
                            _quote_src = str(quote.get("data_source", "batch"))
                        # 2026-09-25 P3 遥测 (TDOC 9/25 案; 用户裁「要」): 侧判定审计 —
                        #   _be_side 用 current_price(=quote.last) 比地板, 但 SHORT 回补真要吃
                        #   **ask**、LONG 卖出真要吃 **bid**。实测判定 active 的 166 笔里 88%
                        #   仍 3s 超时降级市价 ⇒ 历史无法区分「误判侧」与「薄盘口 3s 吃不到」。
                        #   DEB-20260903 议题二毕业判分按侧分组 ⇒ 标尺必须可审计。纯观测, 不改下单。
                        _be_q_bid = float(quote.get("bid", 0) or 0)
                        _be_q_ask = float(quote.get("ask", 0) or 0)
                        # [hotfix 2026-10-02] L1 尺子修 (INTC 10/1 案, 用户裁 P0-2):
                        #   StockQuote push 协议不含盘口, PushQuoteHandler 用 last_price 合成
                        #   bid/ask (book_is_proxy=True) → 9/25 埋点以来 23/23 笔 bid==ask、
                        #   spread=0, 侧判定审计尺子全假。真 L1 一直在 _push_orderbook_cache
                        #   (ORDER_BOOK 自 8/10 已在动态活跃集订阅, push handler 实时维护),
                        #   本遥测从未读过。修法: 优先读真盘口, 读不到如实标注来源 —
                        #   纯测量修复, 侧判定/限价计算 (current_price 基准) 行为零变更。
                        if quote.get("book_is_proxy") or (
                                _be_q_bid and _be_q_bid == _be_q_ask):
                            _be_book_src = "proxy_quote"
                        else:
                            _be_book_src = "quote_book"
                        try:
                            _futu_p = getattr(data_router, "futu", None)
                            _ob = (_futu_p.get_push_orderbook(symbol)
                                   if _futu_p is not None else None)
                            if _ob:
                                _ob_bid = float(_ob.get("BidPrice", 0) or 0)
                                _ob_ask = float(_ob.get("AskPrice", 0) or 0)
                                if _ob_bid > 0 and _ob_ask > 0:
                                    _be_q_bid, _be_q_ask = _ob_bid, _ob_ask
                                    _be_book_src = "l1_push"
                        except Exception as _ob_e:
                            logger.debug(f"[playbook sim] {symbol} L1盘口遥测读取失败(降级quote): {_ob_e}")
                except Exception:
                    pass
                if not current_price or current_price <= 0:
                    # R2 (2026-07-19): 连续取价失败追踪 + 3 轮告警 (不改跳过行为, 仅升级可观测性).
                    #   单仓价格源持续失败 (如 Finnhub 限流) → 该仓多轮无止损保护, 必须可见.
                    _quote_fail_streak[symbol] = _quote_fail_streak.get(symbol, 0) + 1
                    if _quote_fail_streak[symbol] >= 3 and symbol not in _quote_fail_alerted:
                        _quote_fail_alerted.add(symbol)
                        logger.error(
                            f"[playbook] R2 {symbol} 连续 {_quote_fail_streak[symbol]} 轮取价失败, 本轮无止损保护"
                        )
                        try:
                            from app.push import get_push_hub
                            from app.push.message import Level as _PushLevel
                            get_push_hub().push_text(
                                f"⚠️ [Playbook退出监控] {symbol} 连续 {_quote_fail_streak[symbol]} 轮取价失败, "
                                f"该仓本轮无止损保护 (下轮重试). 请检查报价源.",
                                level=_PushLevel.HIGH,
                            )
                        except Exception as _r2_push_err:
                            logger.warning(f"[playbook] R2 取价失败告警推送失败: {_r2_push_err}")
                    continue
                # 取价成功 → 重置连续失败计数 + 告警标记 (下次再失败重新计 3 轮)
                if symbol in _quote_fail_streak:
                    _quote_fail_streak.pop(symbol, None)
                    _quote_fail_alerted.discard(symbol)

                # 持仓时长 (在止损兜底之前计算, 兜底需要用到)
                # 2026-07-03 fix: entry_time 带 tz 后缀 (如 -04:00) 是 aware, datetime_cls.now() 是 naive,
                # 两者相减抛 TypeError → held_minutes=0 → holding_hours=0.0. 改用 tz-aware now.
                held_minutes = 0
                try:
                    _entry_dt = datetime_cls.fromisoformat(r["entry_time"])
                    _now_ref = datetime_cls.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime_cls.now()
                    held_minutes = (_now_ref - _entry_dt).total_seconds() / 60
                except Exception:
                    pass

                # OKR 优化 (2026-06-26): 入场后 30 分钟冷却期.
                # 根因: PLAYBOOK_SIM 最近 7 笔闭环交易中, 3 笔在 1-5 分钟内被 SL_FIXED_PCT 扫出,
                #       属开盘噪声/假突破, 不是信号本身失效. 冷却期内跳过 SL/TIME_EXIT,
                #       仅保留 PRE_CLOSE_EXIT 兜底 (收盘前 8min 必须平).
                # 2026-07-02: grace期从30min降到5min — 30min让SL形同虚设
                # 原因: 开盘噪声在5min内消退, 30min内若暴跌SL不触发=无保护
                _GRACE_MINUTES = 5
                if held_minutes < _GRACE_MINUTES and minutes_to_close > 8:
                    # 2026-07-20 #4: grace 期不再完全豁免 — 保留冷却级 4% 灾难兜底.
                    #   此前直接 continue → 入场后 5min 内闪崩 >4% 零保护, 与规则0
                    #   "冷却期放宽而非豁免"语义冲突. 仅未击穿 4% 兜底线才跳过退出检查;
                    #   击穿则落入下方 SL 兜底 (冷却窗内本就按 4% 放宽, 语义一致).
                    _grace_floor = entry_price * (1 - _cooldown_sl_pct / 100.0) if direction == "LONG" \
                        else entry_price * (1 + _cooldown_sl_pct / 100.0)
                    _grace_breached = (direction == "LONG" and current_price <= _grace_floor) or \
                                      (direction == "SHORT" and current_price >= _grace_floor)
                    if not _grace_breached:
                        logger.debug(
                            f"[playbook sim] {symbol} 处于冷却期 "
                            f"(held={held_minutes:.1f}min < {_GRACE_MINUTES}min), 跳过退出检查"
                        )
                        continue
                    logger.warning(
                        f"[playbook sim] {symbol} {direction} grace期击穿{_cooldown_sl_pct}%灾难兜底 "
                        f"(price=${current_price:.2f}, entry=${entry_price:.2f}), 不豁免退出检查"
                    )

                # ponytail: 止损兜底 — 同 monitor_playbook_shadow_exits, 防止隔夜跳空越过 SL 被 TIME_EXIT 抢占
                if stop_loss > 0:
                    # M1 修复 (2026-07-16): 开盘冷却期放宽兜底 SL, 与规则0一致.
                    #   冷却窗内取原始 SL 与冷却 SL 中"更松"的一侧 (LONG 取更低价, SHORT 取更高价),
                    #   避免开盘噪声触及较紧的原始 SL 被兜底秒杀.
                    _sl_floor = stop_loss
                    if _in_open_cooldown:
                        _cd_sl = entry_price * (1 - _cooldown_sl_pct / 100.0) if direction == "LONG" \
                            else entry_price * (1 + _cooldown_sl_pct / 100.0)
                        _sl_floor = min(stop_loss, _cd_sl) if direction == "LONG" else max(stop_loss, _cd_sl)
                    _sl_hit = (direction == "LONG" and current_price <= _sl_floor) or \
                              (direction == "SHORT" and current_price >= _sl_floor)
                    if _sl_hit:
                        logger.warning(
                            f"[playbook sim] {symbol} {direction} 止损兜底: "
                            f"price=${current_price:.2f} 触及 SL=${_sl_floor:.2f} "
                            f"(原始SL=${stop_loss:.2f}{' 冷却放宽' if _in_open_cooldown else ''}, entry=${entry_price:.2f})"
                        )
                        from app.execution.dual_broker import get_dual_broker
                        dual = get_dual_broker()
                        if _row_target == "real":
                            release = _release_live_broker_stop_for_rule_exit(
                                dual, conn, r, meta
                            )
                            if release["action"] == "reconciled":
                                exited += 1
                                continue
                            if release["action"] != "proceed":
                                _alert_close_failure(
                                    symbol, _row_target, "BROKER_STOP_RELEASE",
                                    release["error"],
                                    error=release["error"].split(":", 1)[0]
                                )
                                continue
                        if isinstance(meta.get("patient_exit"), dict) and not meta["patient_exit"].get("exit_mode"):
                            meta["patient_exit"]["exit_mode"] = "sl_override"
                        decision_pnl_pct = ((current_price - entry_price) if direction == "LONG"
                                            else (entry_price - current_price)) / entry_price * 100
                        attribution = {"overnight": False, "gap_stop_pct": 3.0}
                        try:
                            attribution = {
                                "overnight": _is_overnight_position(r["entry_time"]),
                                "gap_stop_pct": float(_load_horizon_config(meta.get("horizon", "short")).get(
                                    "overnight_gap_stop_pct", 3.0)),
                            }
                        except Exception:
                            logger.debug("[playbook] SL attribution snapshot unavailable", exc_info=True)
                        reason = _sl_fallback_exit_reason(
                            decision_pnl_pct, r["entry_time"], meta.get("horizon", "short"),
                            attribution=attribution)
                        # RFC-106 §5.2: 出场日 gap 证据 (record-only) — 该路径正是
                        # OVERNIGHT_GAP_STOP 归因的主要来源, QCOM 10/8 案此前只能靠日志猜 gap。
                        from app.playbook.exit_engine import note_exit_gap as _note_exit_gap
                        _note_exit_gap(meta, quote, reason, current_price)
                        decision = _playbook_exit_decision(
                            reason, current_price, meta, round(held_minutes / 60, 1))
                        decision["sl_fallback_attribution"] = attribution
                        try:
                            close_res = dual.close_position(
                                symbol, qty, direction, target_env=_row_target,
                                exit_owner=_playbook_exit_owner(db_path, r, meta),
                                exit_decision=decision)
                        except Exception as _sl_close_error:
                            logger.error(
                                f"[playbook dual] SL 平仓异常 {symbol} ({_row_target}): "
                                f"{_sl_close_error}"
                            )
                            _alert_close_failure(
                                symbol, _row_target, "BROKER_EXCEPTION", str(_sl_close_error)
                            )
                            if _handle_live_rule_exit_failure is not None:
                                _handle_live_rule_exit_failure(
                                    dual, conn, r, meta,
                                    {"success": False, "error": "broker_exception", "dealt_qty": None},
                                )
                            continue
                        if not close_res.get("success"):
                            _alert_close_failure(symbol, _row_target, "SL_EXIT_PENDING",
                                                 close_res.get("error", "exit evidence pending"),
                                                 # 2026-09-14: 同上 — 瞬态分流, 终态零延迟
                                                 error=close_res.get("error", ""))
                            if _handle_live_rule_exit_failure is not None:
                                _handle_live_rule_exit_failure(dual, conn, r, meta, close_res)
                            continue
                        exited += _book_playbook_exit_result(dual, close_res, conn)
                        continue

                if abs(current_price - entry_price) / entry_price > 0.15:
                    continue  # 价格异常跳过 (2026-07-02: 50%→15%, 50%让巨亏/巨盈仓位失保护)

                # cohort B skip 判定 (RFC-130 2026-10-03: SIM/LIVE 同语义, 原 LIVE env 门已退役)
                _cohort = meta.get("cohort", "A")
                _src = r["source_type"]
                # P2b 单点化 (2026-09-06): 公式收进 exit_engine.cohort_b_skip_eligible.
                # 本点刻意不叠隔夜总开关 — 总开关由退出引擎在 meta["_skip_allowed"] 读取处施加
                # (exit_engine "极简版B" 段), 与此处原本就无总开关的旧行为逐字一致.
                from app.playbook.exit_engine import cohort_b_skip_eligible as _cse
                _skip_allowed = _cse(_cohort, _src)  # LIVE 需显式开

                # peak_price 从 metadata 恢复 (sim 此前未做 peak tracking, 现与 shadow 对齐)
                peak_price = float(meta.get("peak_price", 0))
                # 注入共用函数所需字段
                meta["_skip_allowed"] = _skip_allowed
                meta["_source_type"] = _src  # 2026-08-15 Q3: 水下仓隔夜豁免需SIM/LIVE判别
                meta["_log_tag"] = "playbook sim"
                meta["_symbol"] = symbol
                meta["_qty"] = qty  # 2026-07-30 D4: 保本线费用感知需股数换算每股往返成本
                meta["_trade_id"] = r["id"]  # RFC-095 (2026-09-22): 影子打点身份锚 (trade_records.id)

                # ── 耐心出场 (2026-08-13, DEB-20260813 §18.3): armed 等待态评估 ──
                #   armed 仓位跳过正常规则链 (防等待期被 TIME_EXIT/MFE_STALL/PRE_CLOSE
                #   市价抢卖), 三条腿 fill/stop/eod 独立触发; expire (跨日) → 清状态回
                #   正常规则链. SL 硬兜底 (上方) 优先级不变 (灾难保护先于耐心等待).
                _pe_mode = ""
                _pe_detail = ""
                _pe_armed_now = False
                _pe_state = meta.get("patient_exit")
                if _pe_state:
                    _pe_mode, _pe_detail = patient_exit_eval_wait(
                        meta, direction, entry_price, current_price, now_et, _pb_cfg_m)
                    if _pe_mode == "expire":
                        meta.pop("patient_exit", None)
                        meta["_patient_exit_expired"] = True  # 墓碑: 防重新 arm
                        _pe_state = None
                        _pe_mode = ""
                        logger.info(f"[耐心出场·expire] {symbol} 跨交易日, 清等待态回正常规则链")

                if _pe_state and _pe_mode == "":
                    # 等待中: 本轮不评估退出规则 (下方 MFE/MAE 采集与 metadata 写回照常)
                    exit_reason = ""
                    _exit_price_hint = current_price
                    _be = bool(meta.get("breakeven_triggered"))
                elif _pe_state and _pe_mode:
                    # 等待腿触发 → 跳过正常规则, 直接退出
                    exit_reason = f"PATIENT_EXIT_{_pe_mode.upper()}"
                    _exit_price_hint = current_price
                    _be = bool(meta.get("breakeven_triggered"))
                    try:
                        _pe_rec = meta["patient_exit"]
                        _pe_rec["exit_mode"] = _pe_mode
                        _pe_rec["exit_detail"] = _pe_detail
                        _pe_rec["exit_at"] = now_et.isoformat()
                        _armed_iso = str(_pe_rec.get("armed_at", ""))
                        if _armed_iso:
                            _armed_dt = datetime_cls.fromisoformat(_armed_iso)
                            _now_ref_pe = datetime_cls.now(_armed_dt.tzinfo) if _armed_dt.tzinfo else now_et
                            _pe_rec["waited_minutes"] = round(
                                (_now_ref_pe - _armed_dt).total_seconds() / 60.0, 1)
                    except Exception:
                        pass
                    logger.info(
                        f"[耐心出场·{_pe_mode}] {symbol} {direction} {_pe_detail} "
                        f"(等待态退出, 跳过正常规则链)"
                    )
                else:
                    # ── 共用退出规则判断 (与 shadow 完全对齐) ──
                    exit_reason, _exit_price_hint, peak_price, _be = _check_playbook_exit_rules(
                        direction, entry_price, current_price, stop_loss,
                        peak_price, meta, held_minutes, minutes_to_close,
                        minutes_from_open, r["entry_time"],
                        earnings_guard=getattr(scheduler, "earnings_risk_guard", None),
                    )
                    # P1 DEB-20261025: read-only drift observation; throttled by
                    # stop id inside the module.  Never calls modify_order.
                    if not exit_reason and _src == "PLAYBOOK_LIVE":
                        try:
                            from app.playbook.broker_stop_drift_shadow import (
                                record_stop_drift_shadow,
                            )
                            record_stop_drift_shadow(
                                dual, row=r, meta=meta,
                                effective_sl=float(meta.get("_effective_sl_observed") or 0),
                                now_ts=time_module.time(),
                            )
                        except Exception:
                            logger.debug(
                                "[playbook live] broker STOP drift shadow failed", exc_info=True
                            )
                    # ── 耐心出场 arm (§18.3-1): DEAD_SIGNAL_EXIT + 浅水下 → 转等待不立即平 ──
                    if exit_reason == "DEAD_SIGNAL_EXIT":
                        if direction == "LONG":
                            _pe_arm_profit = (current_price - entry_price) / entry_price * 100
                        else:
                            _pe_arm_profit = (entry_price - current_price) / entry_price * 100
                        _pe_armed = patient_exit_arm_eligible(
                            exit_reason, _pe_arm_profit, meta, _pb_cfg_m)
                        meta["patient_exit_trigger_telemetry"] = {
                            **patient_exit_trigger_telemetry(
                                exit_reason, _pe_arm_profit, _pb_cfg_m, _pe_armed),
                            "ts": now_et.isoformat(),
                            "exit_reason": exit_reason,
                        }
                        if _pe_armed:
                            meta["patient_exit"] = {
                                "armed_at": now_et.isoformat(),
                                "armed_day": now_et.strftime("%Y-%m-%d"),
                                "armed_held_min": round(held_minutes, 1),
                                "armed_profit_pct": round(_pe_arm_profit, 4),
                                "mfe_at_arm": round(float(meta.get("mfe_pct", 0) or 0), 4),
                                "stop_pct": float(_pb_cfg_m.get("patient_exit_stop_pct", 1.5)),
                                "deadline_hhmm": int(_pb_cfg_m.get("patient_exit_deadline_hhmm", 1550)),
                                # 双口径记账 (§18.3-5): 立即平反事实参考价, 平仓时算 delta
                                "immediate_exit_price": round(float(current_price), 4),
                            }
                            _pe_armed_now = True
                            exit_reason = ""  # 本轮不平仓, 转等待
                            logger.info(
                                f"[耐心出场·arm] {symbol} {direction} DEAD_SIGNAL_EXIT→等待: "
                                f"水下={_pe_arm_profit:.2f}% stop="
                                f"{float(_pb_cfg_m.get('patient_exit_stop_pct', 1.5)):.1f}% "
                                f"deadline={int(_pb_cfg_m.get('patient_exit_deadline_hhmm', 1550)):04d}"
                            )

                # ── DEB-20260928① 金字塔影子采集 (record-only, 2026-09-28 用户裁决落地) ──
                #   武装事件+逐轮价格轨迹 → data/shadow/trail_lock_pyramid.jsonl;
                #   零行为变更, 任何异常静默 (结算=scripts/trail_lock_pyramid_settle.py).
                try:
                    from app.playbook import pyramid_shadow as _pyr
                    _pyr.observe(symbol=symbol, direction=direction,
                                 entry_price=entry_price, current_price=current_price,
                                 qty=r["quantity"], entry_time_str=r["entry_time"],
                                 be_armed=bool(_be), peak_price=peak_price, meta=meta,
                                 now_et=now_et, exiting=bool(exit_reason))
                except Exception as _pyr_e:
                    # R5 模式 (except:pass 棘轮): warn 留痕但进程级只报一次, 防 20s 循环刷屏
                    if not getattr(monitor_playbook_exits, "_pyr_warned", False):
                        monitor_playbook_exits._pyr_warned = True
                        logger.warning(f"[pyramid影子] 采集钩子异常 (本进程只报一次): {_pyr_e}",
                                       exc_info=True)

                # 2026-07-16 P1 修复: MFE/MAE/peak/trough 在每一轮都采集 (含退出轮).
                #   旧逻辑: 这段代码在 `if not exit_reason:` 内, 退出轮跳过 →
                #   peak_price/trough_price 从未在退出轮写回 → _close_trade_record 读到旧值 →
                #   mfe_pct=0/mae_pct=0 (393/397 笔为0, 含121/122盈利单).
                #   修复: 移到 exit_reason 判断之前, 退出轮也更新 peak/trough/mfe/mae.
                if direction == "LONG":
                    _cur_mfe = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                    _cur_mae = (entry_price - current_price) / entry_price * 100 if entry_price > 0 else 0
                else:
                    _cur_mfe = (entry_price - current_price) / entry_price * 100 if entry_price > 0 else 0
                    _cur_mae = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                _cur_mfe = max(_cur_mfe, 0)
                _cur_mae = max(_cur_mae, 0)
                _MFE_MAE_SANE_MAX = 50.0
                if 0 < _cur_mfe <= _MFE_MAE_SANE_MAX and _cur_mfe > float(meta.get("mfe_pct", 0) or 0):
                    meta["mfe_pct"] = round(_cur_mfe, 4)
                    # 2026-07-22: 记录浮盈峰值到达时点 (分钟) — 解锁路径依赖退出验证.
                    #   record-only, 与 mfe_pct 同步刷新: 每次创新高即覆盖为当时 held.
                    meta["time_to_mfe"] = round(held_minutes, 1)
                    # 2026-08-19 (§三十七 P2-b): 交易分钟口径并行字段 — 墙钟 time_to_mfe
                    #   (隔夜仓 1421min) 与交易时 holding_hours (6.4h) 同一推送自相矛盾。
                    #   原字段语义不动 (口径漂移教训), 推送端优先读本字段。
                    try:
                        from app.risk.position_exit_monitor import get_market_hours_us as _gmh
                        _t2m_e = datetime_cls.fromisoformat(r["entry_time"])
                        _t2m_n = datetime_cls.now(_t2m_e.tzinfo) if _t2m_e.tzinfo else datetime_cls.now()
                        meta["time_to_mfe_trading_min"] = round(_gmh(_t2m_e, _t2m_n) * 60, 1)
                    except Exception:
                        pass
                if 0 < _cur_mae <= _MFE_MAE_SANE_MAX and _cur_mae > float(meta.get("mae_pct", 0) or 0):
                    meta["mae_pct"] = round(_cur_mae, 4)
                    # 2026-07-22: 记录最大回撤到达时点 (分钟), 同上.
                    meta["time_to_mae"] = round(held_minutes, 1)

                # ── E shadow (DEB-20260723, record-only): 早砍判定多阈值矩阵 ──
                #   首次跨过 45/60/90min 时点时快照累计 MFE/MAE → 事后可判任意
                #   (时点 × 0.2/0.3/0.5% 阈值) 组合的错杀/节省, 与反事实臂对齐口径.
                #   背景: 7月终值 MFE<0.3% 组 39笔 -$1364 vs >=0.3% 组 70笔 +$1392.
                #   不平仓不拦截; 裁决门槛见 DEB 决策栏 (错杀成本<节省50%).
                try:
                    for _ckpt in (45, 60, 90):
                        _ck = f"e_shadow_{_ckpt}m"
                        if held_minutes >= _ckpt and _ck not in meta:
                            meta[_ck] = {
                                "mfe_pct": float(meta.get("mfe_pct", 0) or 0),
                                "mae_pct": float(meta.get("mae_pct", 0) or 0),
                                "price": round(float(current_price), 4),
                                "held_min": round(held_minutes, 1),
                            }
                except Exception:
                    pass  # 埋点异常不阻断退出监控

                # ── A-lite shadow (DEB-20260723, record-only): DA 证伪价跌破记录 ──
                #   从 invalidation 自然语言提取首个方向合理的 $价格 (首轮算一次缓存),
                #   盘中首次穿越时记录时点/价格 → 事后判 "若按证伪价退出" 反事实.
                try:
                    if "alite_inv_price" not in meta:
                        meta["alite_inv_price"] = _extract_invalidation_price(
                            str(meta.get("invalidation") or ""), direction, entry_price)
                    _inv_px = meta.get("alite_inv_price")
                    if _inv_px and "alite_breach" not in meta:
                        _breached = (current_price <= _inv_px if direction == "LONG"
                                     else current_price >= _inv_px)
                        if _breached:
                            meta["alite_breach"] = {
                                "held_min": round(held_minutes, 1),
                                "price": round(float(current_price), 4),
                                "inv_price": _inv_px,
                            }
                except Exception:
                    pass  # 埋点异常不阻断退出监控

                _trough_old = float(meta.get("trough_price", 0) or 0)
                _trough = _trough_old if _trough_old > 0 else entry_price
                if direction == "LONG":
                    _trough = min(_trough, current_price)
                else:
                    _trough = max(_trough, current_price)
                _trough = round(_trough, 4)

                # #3 (2026-07-20, record-only): 1min bar 版峰谷并行采集 — 不参与退出决策,
                #   仅落 metadata 供周度对比 quote 采样的 MFE 低估幅度 (见 helper docstring).
                # 2026-07-20 修复: 传入 direction, SHORT 时 peak=min(lows)/trough=max(highs),
                #   与 quote 版 peak_price/trough_price 方向语义对齐, 否则 SHORT 对比数据失真.
                _bar_track_new = _track_bar_peak_trough(meta, data_router, symbol, r["entry_time"] or "", direction)

                # 退出参数影子 (record-only) — 仅在非退出轮评估 (退出轮无意义).
                _exit_shadow_new = False
                if not exit_reason:
                    try:
                        from app.playbook.exit_shadow import record_exit_shadow_poll
                        _peak_prof = 0.0
                        if entry_price > 0 and peak_price > 0:
                            _peak_prof = (((peak_price - entry_price) if direction == "LONG"
                                           else (entry_price - peak_price)) / entry_price * 100)
                        # 2026-07-18: 传入 time_exit_hours 供提案 B 作用域判断
                        from app.playbook.config import get_playbook_config as _get_pb_cfg_shadow
                        _shadow_conditions = meta.get("conditions", [])
                        _shadow_exit_cfg = _get_factor_exit_config(
                            _shadow_conditions,
                            _get_pb_cfg_shadow().get_horizon_top("factor_exit_rules", {}),
                            direction=direction,
                        )
                        _shadow_te_hours = float(_shadow_exit_cfg.get("time_exit_hours", 0.0))
                        _exit_shadow_new = record_exit_shadow_poll(
                            meta, direction=direction, entry_price=entry_price,
                            initial_sl=stop_loss, current_price=current_price,
                            peak_profit_pct=_peak_prof, held_minutes=held_minutes,
                            time_exit_hours=_shadow_te_hours,
                        )
                    except Exception:
                        pass

                # invalidation 影子 — 仅在非退出轮评估.
                _inval_shadow_new = False
                if not exit_reason and not meta.get("invalidation_shadow") and (meta.get("invalidation") or ""):
                    try:
                        from app.playbook.invalidation_shadow import evaluate_invalidation_shadow
                        _sh = evaluate_invalidation_shadow(
                            invalidation=meta.get("invalidation"), direction=direction,
                            entry_price=entry_price, current_price=current_price,
                        )
                        if _sh.get("would_trigger"):
                            meta["invalidation_shadow"] = {
                                "reason": _sh["reason"],
                                "held_minutes_at_hit": held_minutes,
                                "price_at_hit": current_price,
                            }
                            _inval_shadow_new = True
                    except Exception:
                        pass

                # peak/trough/mfe/mae metadata 写回 (每一轮, 含退出轮 — 这是修复的核心)
                # 2026-08-13: + _pe_armed_now/等待轮 — patient_exit 状态必须持久化 (重启安全,
                #   否则重启后等待态丢失, 仓位回正常规则链被 DEAD_SIGNAL_EXIT 重复 arm).
                _pe_state_changed = _pe_armed_now or bool(_pe_state)
                if peak_price != float(meta.get("peak_price", 0)) or _cur_mfe > 0 or _cur_mae > 0 or _trough != _trough_old or _inval_shadow_new or _exit_shadow_new or _bar_track_new or _pe_state_changed:
                    # 2026-08-05: _high_atr_stall_shadow_pts 已改 JSON-safe list (门B跨轮去重需持久), 不再 pop
                    for _k in ("_log_tag", "_symbol", "_skip_allowed"):
                        meta.pop(_k, None)
                    meta["peak_price"] = peak_price
                    meta["trough_price"] = _trough
                    meta["breakeven_triggered"] = _be
                    conn.execute(
                        "UPDATE trade_records SET metadata_json = ? WHERE id = ?",
                        (_json.dumps(meta), r["id"]),
                    )
                    conn.commit()

                if not exit_reason:
                    continue

                # ── [2026-11-04 · LMT 案] 退出侧时段窗影子 (record-only) ──
                #   地面真值: 11-04 LMT OVERNIGHT_GAP_STOP 在 09:30:20 (开盘后 20s) 成交
                #   603.33, 而 09:35 = 616.85 / 09:37 = 619.32 (用户观察成立)。但 35 笔
                #   同类历史回放: 延后到 09:35 Σ −$510.65 (11 正 24 负), 六档全负, 其中
                #   OVERNIGHT_GAP_STOP 族 −$324 (2 正 8 负) ⇒ 先留痕攒样, 不改行为。
                #   记完本函数照常往下走真实平仓 (下方 _row_target/限价逻辑不受影响)。
                #   报表: scripts/exit_session_window_shadow_report.py
                try:
                    from app.playbook.exit_session_window_shadow import (
                        record_exit_session_window_shadow as _rec_sess_win)
                    _rec_sess_win(
                        now_et=now_et,
                        symbol=symbol,
                        direction=direction,
                        qty=r["quantity"],
                        exit_reason=exit_reason,
                        price=current_price,
                        entry_time=r["entry_time"],
                        source_type=r["source_type"],
                    )
                except Exception as _sw_e:
                    logger.debug(f"[ExitSessionWindowShadow] 记录异常 (已隔离): {_sw_e}")

                # 真实平仓 (broker) — target_env 按 source_type (PLAYBOOK_LIVE→real, 否则 sim)
                _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
                # 净保本限价平仓 (2026-08-17, 用户裁"保本单真正净保本, 包含费用, 避免白跑"):
                #   SL_BREAKEVEN 市价平仓 fill 系统性落回保本线下方 (D4后31笔24笔净亏, 越位≈费用
                #   — 20s轮询间隙漂移吃掉 cushion)。挂限价单@净保本线 (meta._be_net_line_price,
                #   _check_playbook_exit_rules 按真实往返费算出): 成交价物理上≥(空:≤)保本线。
                #   30s 未成交 → 撤单降级市价 (最坏=现状, 无卡单)。
                # 2026-08-18 (SNDK 实弹案, 用户裁"需要"): SL_TRAIL_LOCK 纳入限价保护。
                #   SNDK: peak +1.05% → 锁利线 1693.07 已高于入场价, 市价单成交 1687.38
                #   击穿入场价 (09:45-09:47 三分钟 1700→1663 急跌), 保本机制触发了但
                #   执行层把锁住的利润全部吐回 (净 -$8.99)。原注释"TRAIL_LOCK 退出价
                #   不是保本线, 限价扭曲语义"在急跌场景不成立: 限价是"地板"不是"目标价"
                #   —— 限价=净保本线 < 检测时市价 → 立即按市价成交 (锁利利润保留),
                #   仅急跌时成交价地板抬到净保本线; 30s 未成交降级市价=现状无卡单。
                #   SL_INITIAL 仍市价 (亏损退出, 限价无意义); EOD/死信号要确定性不变。
                _close_limit_price = 0.0
                if exit_reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "PATIENT_EXIT_FILL"):
                    _close_limit_price = float(meta.get("_be_net_line_price", 0) or 0)
                    if _close_limit_price > 0:
                        # 2026-08-18 遥测 (WDC/AMD 案): 限价单相对触发价 = 主动侧(可立即吃)/
                        #   被动侧(等价格回摆). SL_BREAKEVEN 触发几何=价格已穿越保本线 → 结构性
                        #   被动侧; TRAIL_LOCK 地板<锁利线<触发价 → 主动侧. 毕业判分按侧分组.
                        #   ⚠️ DEB-20260903 勘误: "TRAIL_LOCK→结构性主动侧"假设已被 9/2 DELL 击穿
                        #   证伪 — 20s 轮询间隙内价格可击穿保本地板 (检测价 473.12 < 地板 474.65),
                        #   TRAIL_LOCK 同样会落被动侧。
                        if direction == "LONG":
                            _be_side = "passive" if _close_limit_price > current_price else "active"
                        else:
                            _be_side = "passive" if _close_limit_price < current_price else "active"
                        _be_path = "breakeven_floor"
                        _be_floor_px = _close_limit_price
                        _mkt_pad_bps = 0.0
                        # DEB-20260903 议题二 (方案A, 用户裁"按建议执行"): 被动侧=保本线已被击穿
                        #   (检测价在限价外侧), 挂保本限价结构性不可成交 — 家族5案0回摆成交
                        #   (SNDK 8/18/MU 8/20/WPM 8/20/AU 8/20/DELL 9/2 全负, 均值≈-$19.7/案),
                        #   唯一结局=3s死等→撤单→市价 ~12s 往返 (DELL 该分量 $30.90/63%).
                        #   改挂 marketable limit=检测价∓垫: 立即成交且带地板, 砍掉死等往返。
                        #   主动侧 (8/20 方案A 贴线几何) 行为完全不变。
                        #   回滚: breakeven_passive_marketable_pad_bps 改 0 (=回旧保本限价行为).
                        if _be_side == "passive" and current_price and current_price > 0:
                            try:
                                from app.playbook.config import get_playbook_config as _get_pb_cfg_mk
                                _mkt_pad_bps = float(_get_pb_cfg_mk().get("breakeven_passive_marketable_pad_bps", 15.0))
                            except Exception:
                                _mkt_pad_bps = 15.0
                            if _mkt_pad_bps > 0:
                                if direction == "LONG":
                                    _close_limit_price = round(current_price * (1 - _mkt_pad_bps / 10000.0), 4)
                                else:
                                    _close_limit_price = round(current_price * (1 + _mkt_pad_bps / 10000.0), 4)
                                _be_path = "marketable_limit"
                        meta["_be_close_telemetry"] = {
                            "trigger_price": current_price,
                            "quote_src": _quote_src,
                            "limit_px": _close_limit_price,
                            "limit_side": _be_side,
                            "path": _be_path,
                            "floor_px": _be_floor_px,
                            "pad_bps": _mkt_pad_bps,
                            # P3 侧判定审计三件 (设计缘由见上方 _be_q_bid 取价处注释)
                            # [hotfix 2026-10-02] bid/ask 优先取 ORDER_BOOK push 真 L1,
                            #   book_src 如实标注来源 (l1_push=真盘口 / proxy_quote=合成 /
                            #   quote_book=quote自带盘口). side_basis 保持 last_price —
                            #   侧判定确实比的是 current_price(=quote.last), 语义未变。
                            "bid": _be_q_bid,
                            "ask": _be_q_ask,
                            "spread_bps": (round((_be_q_ask - _be_q_bid) / current_price * 1e4, 2)
                                           if (_be_q_bid and _be_q_ask and current_price > 0
                                               and _be_q_ask >= _be_q_bid) else None),
                            "side_basis": "last_price",
                            "book_src": _be_book_src,
                        }  # _be_close_telemetry (+bid/ask/spread 侧判定审计)
                        # 2026-09-03 (DEB-20260903 议题二后续, 用户裁"A"): 穿透影子 record-only
                        #   埋点 — 触发瞬间采集"快跌 vs V回"判别特征, 夜间回填离场后结局。
                        #   零执行变更: marketable limit 保持默认; 任何异常静默。
                        try:
                            from app.playbook.penetration_shadow import record_penetration as _rec_pen
                            _rec_pen(symbol=symbol, direction=direction, exit_reason=exit_reason,
                                     side=_be_side, path=_be_path, trigger_price=current_price,
                                     floor_px=_be_floor_px, entry_price=entry_price,
                                     peak_price=peak_price, trough_price=_trough,
                                     held_minutes=held_minutes, quote_src=_quote_src,
                                     inst_id=inst_id, telemetry=meta.get("_be_close_telemetry"))
                        except Exception as _pen_e:
                            # 棘轮纪律 (DEB-20260831 P1③): 吞错必须留痕; 仍为 record-only 不影响退出
                            logger.warning(f"[playbook sim] {symbol} 穿透影子落盘异常(已忽略): {_pen_e}")
                        if _be_path == "marketable_limit":
                            logger.info(
                                f"[playbook sim] {symbol} {direction} {exit_reason} 击穿marketable限价平仓 "
                                f"@{_close_limit_price:.4f} (保本地板 {_be_floor_px:.4f} 已被击穿, "
                                f"检测价∓{_mkt_pad_bps:.0f}bps垫, 触发价={current_price:.4f} src={_quote_src})"
                            )
                        else:
                            logger.info(
                                f"[playbook sim] {symbol} {direction} {exit_reason} 净保本限价平仓 "
                                f"@{_close_limit_price:.4f} (entry={entry_price}, 费感知cushion, 地板保护, "
                                f"侧={_be_side} 触发价={current_price:.4f} src={_quote_src})"
                            )
                try:
                    from app.execution.dual_broker import get_dual_broker
                    dual = get_dual_broker()
                    if _row_target == "real":
                        release = _release_live_broker_stop_for_rule_exit(
                            dual, conn, r, meta
                        )
                        if release["action"] == "reconciled":
                            exited += 1
                            continue
                        if release["action"] != "proceed":
                            _alert_close_failure(
                                symbol, _row_target, "BROKER_STOP_RELEASE",
                                release["error"],
                                error=release["error"].split(":", 1)[0]
                            )
                            continue
                    # 2026-08-20 (方案A): 限价超时 8s→3s 配置化. 8/20 实弹: MU 限价悬空等满 8s,
                    #   期间价格续跌 0.43% → 降级市价砸在 940.16 (净-$24.42). 等待本身就是出血.
                    #   配合滑点垫 (触发线抬高→限价翻主动侧), 3s 足够吃价; 超时仍降级市价无卡单.
                    #   回滚: breakeven_limit_timeout_s 改回 8.
                    try:
                        from app.playbook.config import get_playbook_config as _get_pb_cfg_be
                        _be_timeout = float(_get_pb_cfg_be().get("breakeven_limit_timeout_s", 8.0))
                    except Exception:
                        _be_timeout = 8.0
                    # RFC-106 §5.2: 出场日 gap 证据 (record-only) — 规则链出场同源落盘,
                    # 供 OVERNIGHT_GAP_STOP / SL_* 桶按真 gap vs 开盘后下杀分层复盘。
                    from app.playbook.exit_engine import note_exit_gap as _note_exit_gap
                    _note_exit_gap(meta, quote, exit_reason, current_price)
                    decision = _playbook_exit_decision(
                        exit_reason, current_price, meta, round(held_minutes / 60, 1))
                    decision["emit_overnight_shadow"] = True
                    decision["decision_id"] = r["decision_id"]
                    close_res = dual.close_position(
                        symbol, qty, direction, target_env=_row_target,
                        limit_price=_close_limit_price, limit_timeout_s=_be_timeout,
                        exit_owner=_playbook_exit_owner(db_path, r, meta), exit_decision=decision)
                except Exception as _e:
                    logger.error(f"[playbook dual] 平仓异常 {symbol} ({_row_target}): {_e}")
                    _alert_close_failure(symbol, _row_target, "BROKER_EXCEPTION", str(_e))
                    if _handle_live_rule_exit_failure is not None:
                        _handle_live_rule_exit_failure(
                            dual, conn, r, meta,
                            {"success": False, "error": "broker_exception", "dealt_qty": None},
                        )
                    continue

                if not close_res.get("success"):
                    _alert_close_failure(symbol, _row_target, "EXIT_PENDING",
                                         close_res.get("error", "exit evidence pending"),
                                         # 2026-09-14: 传 error 原文 → 瞬态 *_pending 走宽限分流
                                         error=close_res.get("error", ""))
                    if _handle_live_rule_exit_failure is not None:
                        _handle_live_rule_exit_failure(dual, conn, r, meta, close_res)
                    continue
                exited += _book_playbook_exit_result(dual, close_res, conn)
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

        return exited

    except Exception as e:
        # 2026-07-16: debug→exception — 退出监控循环顶层崩溃(如 pb_cfg NameError)
        # 在 debug 级别下生产 INFO 日志完全隐身, 曾导致 P0 潜伏. 必须带 traceback 可见.
        logger.exception(f"[playbook sim] exit monitor 异常 (已隔离): {e}")
        return exited

    finally:
        for _arm_conn in _arm_connections.values():
            try:
                _arm_conn.close()
            except Exception as _arm_close_e:
                # 2026-10-08 棘轮清剿: 连接关闭失败须留痕 (原为 except: pass)
                logger.warning(f"[playbook sim] arm 连接关闭失败 (不影响退出监控): {_arm_close_e}",
                               exc_info=True)
