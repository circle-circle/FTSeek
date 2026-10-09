"""盘前生成流 (阶段 6 迁自 app/playbook/pipeline.py)。

本模块只承接编排本体; DB、决策、运行上下文与 pipeline monkeypatch 缝
仍由 pipeline facade 在调用时显式注入。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional


def _topup_window(hhmm: int, cfg: Dict[str, Any]) -> Optional[str]:
    """topup 时间窗判定 → "premarket" / "intraday" / None (窗外)。"""
    _pm_start = int(cfg.get("not_before_et_hhmm", 845))
    _pm_end = int(cfg.get("premarket_end_et_hhmm", 930))
    _id_start = int(cfg.get("intraday_start_et_hhmm", 1030))
    _id_end = int(cfg.get("intraday_end_et_hhmm", 1130))
    if _pm_start <= hhmm < _pm_end:
        return "premarket"
    if _id_start <= hhmm < _id_end:
        return "intraday"
    return None


def run_playbook_generate(
    scheduler,
    *,
    force: bool = False,
    today_provider: Callable[[], str],
    playbook_enabled: Callable[[], bool],
    trading_day_today: Callable[[], bool],
    reload_bridge_if_landed: Callable,
    check_bridge_health_and_alert: Callable,
    build_runtime_context: Callable,
    build_instructions_from_votes: Callable,
    instruction_to_db_dict: Callable,
    mark_generate_done: Callable[[str], None],
    logger,
) -> Optional[Dict[str, Any]]:
    """
    盘前生成今日交易指令 (Layer 0 + Layer 1)。

    幂等: scheduler._playbook_generated_date == today 即跳过 (force=True 可重跑)。
    节假日跳过。任何异常都 catch 并 log, 不向上抛 (保证不影响 scheduler 主循环)。
    """
    try:
        today = today_provider()

        if not playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘前生成 (上线闸门关闭)")
            return None

        if not force:
            if not trading_day_today():
                logger.debug(f"[playbook] {today} 非交易日, 跳过盘前生成")
                return None
            # 2026-07-09: 生成时间窗口限制 — ET 09:00后才生成
            # 根因: 原无时间窗口, PRE_MARKET(04:00-09:30)第一次循环就触发生成
            # → ET 04:xx 生成 → 用昨日数据, 5小时gap信号衰减
            # 改为 ET 09:00 生成 → pre-market价格已反映隔夜消息, 更接近开盘reality
            from datetime import datetime as _dt
            import pytz as _pz
            _now_et = _dt.now(_pz.timezone('US/Eastern'))
            _generate_hour = _now_et.hour
            if _generate_hour < 9:
                logger.debug(
                    f"[playbook] {today} ET {_now_et.strftime('%H:%M')} < 09:00, "
                    f"等待盘前数据成熟后生成 (目标: ET 09:00+)"
                )
                return None
            # [v0.70 P0-2 2026-08-26] 运行期bridge重载探测 — 服务启动早于19:00CST产出时,
            #   内存恒为 TechnicalFallback; 此处(ET≥09:00 已过闸)文件应已就绪, 补挂 Bridge.
            #   一天一次幂等, 失败静默 (缺失告警由 _check_bridge_health_and_alert 兜底).
            reload_bridge_if_landed(scheduler)
            # 2026-07-09: 如果voting_coordinator是NullVotingAdapter, 跳过生成
            # 根因: NullVotingAdapter.vote()返回None → 所有候选SKIP → save_instruction
            # 会UPSERT覆盖bridge已写的BUY/SELL_SHORT instruction为SKIP, 破坏bridge候选
            _vc = getattr(scheduler, "voting_coordinator", None)
            if _vc is not None and type(_vc).__name__ == "NullVotingAdapter":
                logger.info(
                    f"[playbook] {today} voting_coordinator=NullVotingAdapter, "
                    f"跳过生成 (bridge候选已由deep_analysis skill直写DB, 不需要决策适配器)"
                )
                return None
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB，不再依赖内存属性
            # 内存属性在进程重启后丢失 → 重启后重新执行 → 覆盖当天数据
            # [DEB-fullchain #22 2026-08-16] 幂等依据从"任意行存在"改为"生成完成标记":
            #   原逻辑下 UE 盘前 cron 先写入任意一行 → pipeline 生成全天锁死 (供给归零);
            #   半成品 (生成中途异常) 也永久锁死。完成标记在生成成功收尾时写入;
            #   有行无标记 (UE 先写/上次中断) → 继续生成, UPSERT 幂等覆写。
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                if db.is_generate_completed(today):
                    logger.debug(f"[playbook] {today} 生成完成标记存在，跳过生成 (幂等)")
                    scheduler._playbook_generated_date = today  # 同步内存标志
                    return None
                _existing_cnt = 0
                try:
                    _existing_cnt = len(db.get_instructions_by_date(today))
                except Exception:
                    pass
                if _existing_cnt:
                    logger.info(
                        f"[playbook] {today} 已有 {_existing_cnt} 行但无完成标记 "
                        f"(UE 先写/上次中断), 继续生成"
                    )
            except Exception as e:
                logger.debug(f"[playbook] 完成标记检查失败，降级为内存检查: {e}")
                if getattr(scheduler, "_playbook_generated_date", None) == today:
                    logger.debug(f"[playbook] {today} 已生成 (内存标志), 跳过")
                    return None

        logger.info(f"[playbook] 🚀 盘前生成开始 (date={today})")

        # 2026-07-19 (DEB-round3 #1): bridge 三态告警 (缺失/空/非法 → 飞书, 不阻断生成)
        check_bridge_health_and_alert("run_generate")

        from app.playbook.db import PlaybookDB
        from app.playbook.market_regime import MarketRegimeGate
        from app.playbook.screener import PlaybookScreener

        db = PlaybookDB()
        db.ensure_tables()

        # Layer 0: 市场环境门禁
        regime_ctx = MarketRegimeGate().assess()
        # [DEB-20260924 F5-B'影子] notes 附带盘前双算遥测 (schema 零改动, 消费方按文本读):
        #   pre=<盘前价> 影子score=<n> 影子regime=<v> —— 攒 5-10 交易日后按此裁决是否 enforce。
        _f5_shadow = ""
        if getattr(regime_ctx, "score_shadow", None) is not None:
            _f5_shadow = (f", F5影子: pre={regime_ctx.spy_pre_price:.2f} "
                          f"score'={regime_ctx.score_shadow} regime'={regime_ctx.regime_shadow}")
        db.save_regime_log({
            "date": today,
            "regime": regime_ctx.regime.value,
            "vix": regime_ctx.vix,
            "spy_price": regime_ctx.spy_price,
            "spy_ma20": regime_ctx.spy_ma20,
            "spy_ma50": regime_ctx.spy_ma50,
            "score": regime_ctx.score,
            "notes": f"top_n={regime_ctx.top_n}, pos_mult={regime_ctx.position_multiplier}{_f5_shadow}",
        })
        logger.info(f"[playbook] Layer0 regime={regime_ctx.regime.value} VIX={regime_ctx.vix}")

        # Layer 1: 数据收集 + 规则筛选 (不截断, 全量通过门槛的候选)
        runtime = build_runtime_context(scheduler)
        watchlist = runtime.watchlist
        if not watchlist:
            logger.warning("[playbook] watchlist 为空, 跳过生成")
            scheduler._playbook_generated_date = today  # 标记已尝试, 避免空跑循环
            mark_generate_done(today)  # [#22] 终态: 空表也是完成态, 不留锁死窗口
            return {"date": today, "instructions": 0, "reason": "empty_watchlist"}

        collector = runtime.collector
        screener = PlaybookScreener(collector)
        positions = runtime.positions
        screen_results = screener.screen(
            watchlist=watchlist,
            regime=regime_ctx.regime,
            positions=positions,
        )
        logger.info(f"[playbook] Layer1 筛选出 {len(screen_results)} 只候选 (无截断)")

        if not screen_results:
            logger.info("[playbook] 无候选通过质量门槛, 跳过生成")
            scheduler._playbook_generated_date = today
            mark_generate_done(today)  # [#22] 终态
            return {"date": today, "instructions": 0, "reason": "no_candidates_passed_filter"}

        # Layer 2: 决策适配器统一方向决策
        # 信号源唯一化: DA bridge → technical fallback → Null 全 SKIP.
        voting_coordinator = runtime.voting_coordinator
        data_router = runtime.data_router
        from app.playbook.pipeline_parts.bridge_adapters import TechnicalFallbackAdapter
        instructions = build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=voting_coordinator,
            regime_context=regime_ctx,
            total_equity=runtime.total_equity,
            data_router=data_router,
            today=today,
        )

        # 入库
        saved = 0
        for inst in instructions:
            try:
                inst_dict = instruction_to_db_dict(inst, regime_ctx, today)
                # [2026-08-17 根源修复] 覆盖仲裁已下沉 save_instruction SQL 层
                # (db.py ON CONFLICT WHERE 优先级阶梯): DA-/UE 深度行原子地拒绝
                # SKIP/TFB 票覆盖, 无需调用方守卫 (先读后写有 TOCTOU 窗口, 已删)。
                db.save_instruction(inst_dict)
                saved += 1
            except Exception as e:
                logger.warning(f"[playbook] 指令入库失败 {getattr(inst, 'symbol', '?')}: {e}")

        logger.info(f"[playbook] ✅ 生成完成: {saved}/{len(instructions)} 条指令入库")

        # 2026-07-16: 盘前推送已移除内联触发, 统一归 run_playbook_morning_push 独占。
        # 根因: 内联走 sync=False, 而独立任务走 sync=True (绕过 PushHub dedup) →
        #       两条路径都发 → 用户收到两次。独立任务有持久化幂等+重试, 足以覆盖送达。

        scheduler._playbook_generated_date = today
        mark_generate_done(today)  # [#22] 成功收尾才写完成标记 (异常路径不写, 允许重跑)
        return {"date": today, "instructions": saved, "regime": regime_ctx.regime.value}

    except Exception as e:
        logger.exception(f"[playbook] 盘前生成异常 (已隔离, 不影响 scheduler): {e}")
        return {"error": str(e)}


def run_playbook_topup_supplement(
    scheduler=None,
    *,
    now_et_provider: Callable,
    today_provider: Callable[[], str],
    topup_window: Callable[[int, Dict[str, Any]], Optional[str]],
    attempts_cache: Dict[str, list],
    build_runtime_context: Callable,
    build_instructions_from_votes: Callable,
    instruction_to_db_dict: Callable,
    gather_intraday_evidence: Callable,
    logger,
) -> Optional[Dict[str, Any]]:
    """扩容补量 (2026-07-24, 用户裁决: 加快样本收集, 但不降标准、走正式链)."""
    try:
        from app.core.config_container import get_config as _gc_tu
        _tu_cfg = _gc_tu().get_dotted("playbook.topup", {}) or {}
    except Exception:
        _tu_cfg = {}
    if not bool(_tu_cfg.get("enabled", False)):
        return None

    try:
        _now = datetime.now(now_et_provider())
        _hhmm = _now.hour * 100 + _now.minute
        _timing = topup_window(_hhmm, _tu_cfg)
        if _timing is None:
            return None

        _att_key = f"{today_provider()}:{_timing}"
        _max_att = int(_tu_cfg.get("max_attempts_per_window", 2))
        _retry_min = float(_tu_cfg.get("retry_interval_min", 15))
        _att = attempts_cache.get(_att_key, [0, 0.0])
        if _att[0] >= _max_att:
            return None
        if _att[0] > 0 and (time.time() - _att[1]) < _retry_min * 60:
            return None

        from app.playbook.db import PlaybookDB
        from app.playbook.market_regime import MarketRegimeGate
        from app.playbook.screener import PlaybookScreener

        today = today_provider()
        db = PlaybookDB()
        db.ensure_tables()
        existing = db.get_instructions_by_date(today)
        _topup_today = 0
        for _e in existing:
            _raw = _e.get("raw_instruction_json") or {}
            if isinstance(_raw, dict) and _raw.get("source") == "topup_pool":
                _topup_today += 1
                if (_raw.get("timing") or "premarket") == _timing:
                    return None

        _target = int(_tu_cfg.get("target_min_instructions", 25))
        _actionable = [e for e in existing if e.get("direction") in ("BUY", "SELL_SHORT")]
        if len(_actionable) >= _target:
            logger.info(f"[playbook·topup] 当日可执行指令 {len(_actionable)} ≥ 目标 {_target}, 无需补量")
            return {"added": 0, "reason": "target_met"}

        _need = min(
            _target - len(_actionable),
            int(_tu_cfg.get("max_add_per_day", 12)) - _topup_today,
        )
        try:
            from app.utils.watchlist_manager import get_symbols_by_role as _tu_pool
            _pool = [str(s) for s in _tu_pool("topup_eligible")]
        except Exception as _pe:
            logger.error(f"[playbook·topup] 名单真源解析失败 (roles.topup_eligible): {_pe}")
            _pool = []
        logger.info(f"[playbook·topup] 补量池 {len(_pool)} 只 (源=roles.topup_eligible)")
        _held = {e.get("symbol") for e in existing}
        _cands = [s for s in _pool if s not in _held]
        if not _cands or _need <= 0:
            return {"added": 0, "reason": "no_pool_candidates"}

        logger.info(
            f"[playbook·topup] 补量启动 ({_timing}): 当日 {len(_actionable)} 条 < 目标 {_target}, "
            f"从池 {len(_cands)} 只补最多 {_need} 条 (正式链: 质量门+technical方向, 无方向不硬凑; "
            f"尝试 {_att[0] + 1}/{_max_att})"
        )
        attempts_cache[_att_key] = [_att[0] + 1, time.time()]

        regime_ctx = MarketRegimeGate().assess()
        runtime = build_runtime_context(scheduler)
        collector = runtime.collector
        screener = PlaybookScreener(collector)
        positions = runtime.positions
        screen_results = screener.screen(watchlist=_cands, regime=regime_ctx.regime, positions=positions)
        if not screen_results:
            logger.info("[playbook·topup] 池内无标的通过质量门槛, 不硬凑")
            return {"added": 0, "reason": "no_screen_pass"}

        data_router = runtime.data_router
        # 局部 import (本文件惯例): 迁移时 run_playbook_generate 已带而本函数漏带,
        # 屏幕通过分支必 NameError (2026-10-09 提交治理钩实证; 旧 pipeline 类在模块级
        # 故旧码无此坑)。局部 import 保持与 generate 同缝, 便于测试 monkeypatch。
        from app.playbook.pipeline_parts.bridge_adapters import TechnicalFallbackAdapter
        instructions = build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=TechnicalFallbackAdapter(),
            regime_context=regime_ctx,
            total_equity=runtime.total_equity,
            data_router=data_router,
            today=today,
        )

        saved = 0
        for inst in instructions:
            if saved >= _need:
                break
            if getattr(inst, "direction", "SKIP") not in ("BUY", "SELL_SHORT"):
                continue
            try:
                inst_dict = instruction_to_db_dict(inst, regime_ctx, today)
                inst_dict["raw_instruction"]["source"] = "topup_pool"
                inst_dict["raw_instruction"]["timing"] = _timing
                try:
                    _eq = data_router.get_quote(inst.symbol) if data_router else None
                    inst_dict["raw_instruction"]["eval_price"] = round(float(
                        (_eq or {}).get("last", 0) or (_eq or {}).get("price", 0) or 0), 4) or None
                except Exception:
                    inst_dict["raw_instruction"]["eval_price"] = None
                inst_dict["raw_instruction"]["eval_asof"] = _now.isoformat()
                _ev = gather_intraday_evidence(data_router, inst.symbol, inst.direction)
                if _ev is not None:
                    inst_dict["raw_instruction"]["evidence"] = _ev
                db.save_instruction(inst_dict)
                saved += 1
            except Exception as e:
                logger.warning(f"[playbook·topup] 指令入库失败 {getattr(inst, 'symbol', '?')}: {e}")
        logger.info(f"[playbook·topup] ✅ 补量完成 ({_timing}): +{saved} 条 (池评估 {len(screen_results)} 只, 无方向/被门拦下的不计)")
        return {"added": saved, "pool_screened": len(screen_results), "timing": _timing}
    except Exception as e:
        logger.warning(f"[playbook·topup] 补量任务异常 (已隔离): {e}")
        return None


def run_playbook_morning_push(
    scheduler=None,
    *,
    state_path: str,
    logger,
) -> Optional[bool]:
    """独立的盘前指令推送任务 — 不依赖生成内联触发, 每日幂等(持久化), 失败下轮重试."""
    import json as _json
    from datetime import datetime as _dt
    try:
        from app.utils.timezone import now_et
        today = now_et().date().isoformat()
    except Exception:
        today = _dt.now().date().isoformat()

    try:
        with open(state_path) as f:
            state = _json.load(f)
    except Exception:
        state = {}
    morning = state.setdefault("morning", {})
    if morning.get(today):
        return None

    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.feishu_push import PlaybookPusher
        db = PlaybookDB()
        instructions = db.get_instructions_by_date(today)
        if not instructions:
            return None

        ok = PlaybookPusher(db).push_morning_instructions(today, sync=True)
        if ok:
            morning[today] = True
            if len(morning) > 30:
                for _k in sorted(morning)[:-30]:
                    morning.pop(_k, None)
            try:
                with open(state_path, "w") as f:
                    _json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception as _we:
                logger.warning(f"[playbook] 推送幂等状态写入失败: {_we}")
            logger.info(f"[playbook] ✅ 盘前指令推送成功 ({today}, {len(instructions)}条), 已标记幂等")
        else:
            logger.warning(f"[playbook] ⚠️ 盘前指令推送未成功 ({today}), 下轮重试")
        return ok
    except Exception as e:
        logger.warning(f"[playbook] 盘前指令推送任务异常 (已隔离): {e}")
        return None


def run_playbook_normalize_vol_buckets(
    scheduler=None,
    *,
    today_provider: Callable[[], str],
    positive_finite_atr: Callable,
    compute_volatility_based_sl_tp: Callable,
    logger,
) -> Optional[Dict[str, Any]]:
    """盘前波动率定桶落库; best-effort, 每行独立隔离。"""
    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.config import get_playbook_config
        from app.core.di_container import get_di_container

        today = today_provider()
        db = PlaybookDB()
        db.ensure_tables()
        instructions = db.get_instructions_by_date(today)
        if not instructions:
            return None

        pb_cfg = get_playbook_config()
        atr_provider = get_di_container().atr_provider()

        _tagged = 0
        _missing = 0
        for inst in instructions:
            if inst.get("direction") not in ("BUY", "SELL_SHORT"):
                continue
            if inst.get("vol_bucket"):
                continue
            symbol = inst.get("symbol", "")
            try:
                atr_pct = positive_finite_atr(atr_provider.get_atr_pct(symbol))
                if atr_pct is None:
                    _missing += 1
                    continue
                _hp = pb_cfg.get_horizon_params(inst.get("horizon", "short"))
                _vol_info = compute_volatility_based_sl_tp(
                    atr_pct, inst["direction"], _hp, pb_cfg
                )
                db.update_instruction_tracking(inst["id"], {
                    "vol_bucket": (_vol_info or {}).get("bucket"),
                    "atr_pct": round(float(atr_pct), 6),
                })
                _tagged += 1
            except Exception as _e:
                logger.debug(f"[playbook] {symbol} 定桶跳过 (已隔离): {_e}")
                continue

        _bucket_log = logger.info if (_tagged or _missing) else logger.debug
        _bucket_log(
            f"[playbook] 盘前定桶落库 ({today}): 已标注 {_tagged} 条, ATR 缺失 {_missing} 条"
        )

        try:
            _regime_missing = [i for i in instructions if not i.get("market_regime")]
            if _regime_missing:
                from app.playbook.market_regime import MarketRegimeGate as _MRG_bf
                _rc = _MRG_bf().assess()
                _spy_trend = "above_ma20" if _rc.spy_ma20 >= _rc.spy_ma50 else "below_ma20"
                _rg_tagged = 0
                for _ri in _regime_missing:
                    try:
                        db.update_instruction_tracking(_ri["id"], {
                            "market_regime": _rc.regime.value,
                            "vix_at_generation": _rc.vix,
                            "spy_trend_at_generation": _spy_trend,
                        })
                        _rg_tagged += 1
                    except Exception:
                        continue
                logger.info(
                    f"[playbook] regime落库补全 ({today}): {_rg_tagged}/{len(_regime_missing)} 条 "
                    f"regime={_rc.regime.value} vix={_rc.vix}"
                )
        except Exception as _rg_e:
            logger.warning(f"[playbook] regime落库补全失败 (已隔离): {_rg_e}")

        return {"tagged": _tagged, "missing": _missing}
    except Exception as e:
        logger.warning(f"[playbook] 盘前定桶任务异常 (已隔离): {e}")
        return None
