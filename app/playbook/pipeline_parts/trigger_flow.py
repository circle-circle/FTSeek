"""盘中触发流 (阶段10迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional


def check_intraday_trigger(
    scheduler,
    inst: Dict[str, Any],
    *,
    _now_et: Callable,
    _today_et_str: Callable,
    module_file: str,
    logger: Any,
    INVALIDATED_SKIP_REASONS,
    _CUTOFF_LOGGED: set,
    _low_atr_logged: set,
    _scb_scan_logged: set,
    _LIQUIDITY_LOGGED: set,
    _BORROW_POOL_LOGGED: set,
    _LIMIT_SHADOW_EMITTED: set,
    _write_round_skip_reason: Callable,
    _calc_intraday_vol_ratio: Callable,
    _calc_vwap_slope: Callable,
    _capital_flow_divergence: Callable,
    _symbol_streak_banned: Callable,
    _emit_widen_shadow: Callable,
    _check_liquidity_gate: Callable,
    _check_borrow_pool_gate: Callable,
    _resolve_generated_anchor: Callable,
    _anchor_trigger_geometry: Callable,
    _fetch_l1_micro: Callable,
    _vwap_gate_ab_bypass: Callable,
    _shadow_dedup_claim: Callable,
    _shadow_dedup_commit: Callable,
    _shadow_portfolio_emit: Callable,
    _short_chase_shadow_row: Callable,
    _screening_age_hours: Callable,
) -> bool:
    """盘中实时检查指令是否触发 (突破当日高低点).

    2026-06-29: 从MA20突破改为当日高低点突破.
    根因: MA20突破在BULL regime下SHORT几乎不可能触发(需跌破MA20).
    修复: BUY触发=突破当日高点; SHORT触发=跌破当日低点. 不依赖MA20方向.
    成交量确认保留 (防止低流动性假突破).
    """
    entry_price = inst.get("entry_price")
    if not entry_price or entry_price <= 0:
        return False

    symbol = inst.get("symbol", "")
    if not symbol:
        return False

    direction = inst.get("direction", "")
    is_short = "SHORT" in direction.upper()

    # 2026-07-19 (DEB-round3 #2): 已作废指令直接拒 — 与 auto_execute 头检双保险.
    #   特异性匹配作废语义取值 (P1-2 起集合化), 不误伤其他 skip_reason 语义.
    if (inst.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
        return False

    # 2026-07-11: 只读埋点 — 记录未触发原因/实时价/偏离幅度.
    # 目的: 量化 never-trigger 里 "追高/接飞刀被正确拒绝" vs "盘前参考价过期但盘中价合理".
    # 零行为改动: 仅 append JSONL, best-effort, 不影响触发判定与返回值.
    def _emit_trigger_miss(reason: str, cur, extra: Optional[Dict[str, Any]] = None):
        try:
            # 2026-07-30 修尺子: pytest 直调本函数把 US.TEST* 假事件写进生产遥测
            #   (trigger_miss 5.6% / entry_shadow 13.4% 污染, 07-11 起) — G3 reanchor
            #   归零判据被测试跑弄脏。测试标的一律不落盘 (存量已清, bak 留档)。
            if "TEST" in symbol.upper():
                return
            import json as _json
            from datetime import datetime as _dt
            from pathlib import Path as _Path
            _e = float(entry_price or 0)
            _cur = float(cur or 0)
            _gap = (abs(_cur - _e) / _e * 100.0) if (_e > 0 and _cur > 0) else 0.0
            # ── R-8a (2026-08-23 DEB-20260823): ts/date 改 ET ──
            # 原用裸 _dt.now() = 宿主 Asia/Shanghai。ET 12:00-15:59 的事件在北京是次日
            # 00:00-03:59 → date 被记成 +1 天。实测 15,150 行中 9,049 行 (59.7%) 错位,
            # 文件内出现 `2026-08-22 Sat` 这类非交易日即铁证。
            # 下游全部按此 date 分组或与 playbook_instructions.date (ET) 关联:
            #   scripts/early_acceptance_check.py:168 / first_night_gate_report.py:101 /
            #   stale_anchor_shadow_tag.py:175 (inst["date"]==today_et 对 ET 下午行恒不成立) /
            #   gate_block_counterfactual.py:79,101 / gate_cost_report.py:48 /
            #   trigger_miss_analysis.py:57
            # → 门效果"尺子"被污染, 参数裁决基于错日样本。
            # 同文件 _emit_entry_shadow (:4319) 已用 ET, 本处是漏网单点。
            # 新增 ts_cst 保留原口径, 便于与 08-23 之前的历史行对齐排查。
            # 2026-08-26 修复: _now_et() 返回的是时区对象 (America/New_York) 而非
            #   datetime — 直接 .isoformat() 抛 AttributeError 被下方 except:pass 静默
            #   吞掉, 8/23 起本遥测 (及 _emit_exec_gate_audit) 全部静默死亡, 收口审计
            #   停更在 8/22。改为 _dt.now(_now_et()) 取真实 ET 当前时间。
            _now_et_row = _dt.now(_now_et())
            _row = {
                "ts": _now_et_row.isoformat(timespec="seconds"),
                "date": _now_et_row.strftime("%Y-%m-%d"),
                "ts_cst": _dt.now().isoformat(timespec="seconds"),
                "tz": "ET",
                "inst_id": inst.get("id"),
                # 2026-08-23 (用户裁 DA/UE+reentry 分流对比): 源标识入遥测 —
                #   无此字段则毕业判据"DA 对比组方向一致"不可计算 (分流可测性缺口)。
                #   DA 源的 falsification_vwap_slope 行 = would-block 对比样本 (记录不拦)。
                "decision_id": str(inst.get("decision_id") or ""),
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "entry_price": round(_e, 4),
                "current_price": round(_cur, 4),
                "gap_pct": round(_gap, 3),
                "reason": reason,
            }
            # [P1.2 2026-09-12, proud-slope-falcon] 可选附加维度 (仅新增键, 不改既有键
            #   与 reason 取值 — 既有消费方 early_acceptance_check.py:172 /
            #   first_night_gate_report.py:101 按 reason 计数, 不受影响)。
            #   当前唯一写者: 低ATR门的 atr_basis (判定依据是哪一路 ATR)。
            if isinstance(extra, dict):
                _row.update({k: v for k, v in extra.items() if v is not None})
            _tp = _Path(module_file).resolve().parents[2] / "data" / "trigger_miss_telemetry.jsonl"
            with open(_tp, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # 2026-07-15: 只读影子埋点 (入场时机门验证 v3) — 记录门的原始微结构特征
    #   + 司机(突破触发)的真实决策. 离线切片验证"过滤价值": 真实入场单中,
    #   同时过新门(VWAP/量能/大盘)的那批 PnL 是否更好. 全源通用.
    # 零行为改动: 仅 append JSONL, best-effort, 不影响触发判定与返回值 (镜像 _emit_trigger_miss).
    def _emit_entry_shadow(triggered_val, breakout_val=None, gate_enabled=False,
                           gate_block=None, gate_reason=None, vwap_dev=None,
                           vwap_populated=None, vol_gate_ratio=None,
                           vol_gate_block=None, entry_path=None, inst_confidence=None):
        try:
            if "TEST" in symbol.upper():  # 2026-07-30: 同款测试污染守卫 (见 _emit_trigger_miss)
                return
            import json as _json
            from datetime import datetime as _dt
            from pathlib import Path as _Path
            _c = ctx if isinstance(ctx, dict) else {}
            _keys = [
                "vwap", "ticker_vwap_deviation", "slope_5m", "rsi", "atr",
                "volume", "volume_ratio", "opening_high", "opening_low",
                "opening_range_ready", "order_flow_imbalance", "ob_spread_bps",
                "ob_depth_imbalance", "ticker_buy_sell_ratio",
                "spy_price", "spy_ma20", "spy_ma50", "spy_ma200",
                "change_percent", "trend",
            ]
            _feat = {k: _c.get(k) for k in _keys}
            _did = str(inst.get("decision_id") or "")
            _src = _did.split(":")[0] if ":" in _did else ""
            if not _src:
                try:
                    # 2026-09-01 修复: raw 可能已是 dict (_row_to_dict 自动解析),
                    #   旧 json.loads 抛 TypeError → source 字段静默丢失。
                    _raw = inst.get("raw_instruction_json")
                    if isinstance(_raw, str):
                        _raw = _json.loads(_raw or "{}")
                    _src = str((_raw or {}).get("source") or "")
                except Exception:
                    _src = ""
            try:
                _et = _dt.now(_now_et())
                _hm = _et.hour * 100 + _et.minute
                _sess = "open" if _hm < 945 else ("late" if _hm >= 1530 else "mid")
                _ts = _et.isoformat(timespec="seconds")
            except Exception:
                _sess = "unknown"
                _ts = _dt.now().isoformat(timespec="seconds")
            # 2026-08-05 (DEB-20260805(二) B1): 触发时点 L1 盘口快照入影子.
            #   2026-08-06 二次修复: 推送缓存路径被 ADR-013 架空 (ORDER_BOOK 不在推送订阅集,
            #   缓存恒空, 08-05 全天 233 行 micro=null) → 改 _fetch_l1_micro 拉取式 (S2 同款).
            _micro = _fetch_l1_micro(scheduler, symbol)
            _row = {
                "ts": _ts,
                "session": _sess,
                "inst_id": inst.get("id"),
                "decision_id": _did,
                "source": _src,
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "entry_price": round(float(entry_price or 0), 4),
                "current_price": round(float(current_price or 0), 4),
                "live_triggered": bool(triggered_val),
                "breakout_triggered": (bool(breakout_val) if breakout_val is not None else None),
                "gate_enabled": bool(gate_enabled),
                "gate_block": (bool(gate_block) if gate_block is not None else None),
                "gate_reason": gate_reason,
                "vwap_dev": vwap_dev,
                "vwap_populated": bool(vwap_populated),
                "vol_gate_ratio": vol_gate_ratio,
                "vol_gate_block": (bool(vol_gate_block) if vol_gate_block is not None else None),
                "entry_path": entry_path,
                "inst_confidence": inst_confidence,
                "micro": _micro,
                "features": _feat,
            }
            # ── [RFC-123 阶段0] 双锚影子块 (DEB-20261007 F1, 用户裁「按建议执行」) ──
            #   record-only: 同轮双记 生成锚(planned_entry/band沿/missing) vs 执行锚
            #   (DB entry_price, 被 reprice 逐轮覆盖) + 生成锚反事实触发几何。
            #   嵌套块不碰既有平键 ⇒ ETS 下游 (W7 哨兵/eval_vwap_gate/entry_gate 报表)
            #   全部 .get() 平键读取, 零影响。TEST 守卫已由本函数头部承担。
            #   回滚: entry_anchor_dual_shadow_enabled=false (playbook.* 热重载穿不透,
            #   须 systemctl restart) 或 revert 本块。
            try:
                if bool(pb_cfg.get("entry_anchor_dual_shadow_enabled", True)):
                    _da_gen, _da_src = _resolve_generated_anchor(inst, is_short)
                    _da_ct, _da_cp = _anchor_trigger_geometry(
                        _da_gen, current_price, is_short,
                        _knife_floor, _chase_zone, _vol_ok, _near_miss_tolerance)
                    _da_div = (None if not _da_gen else
                               round((float(entry_price or 0) - _da_gen) / _da_gen * 100.0, 4))
                    _da_tdiv = (None if (_da_gen is None or _da_ct is None)
                                else (bool(_da_ct) != bool(triggered_val)))
                    _row["anchor_recorded"] = True
                    _row["dual_anchor"] = {
                        "schema": "rfc123-s0-v1",
                        "date": _today_et_str(),
                        "generated": (round(_da_gen, 4) if _da_gen else None),
                        "gen_source": _da_src,
                        "effective": round(float(entry_price or 0), 4),
                        "divergence_pct": _da_div,
                        "gen_triggered": _da_ct,
                        "gen_path": _da_cp,
                        "live_triggered": bool(triggered_val),
                        "live_path": entry_path,
                        "trigger_divergent": _da_tdiv,
                    }
            except Exception as _da_err:
                # A6 fail-loud (2026-08-24 同款): 影子丢行必须可见 — 禁 except:pass
                # (静默丢行 = 攒样进度虚高 + 判据面缺样不可察)。warning 不抛, 不拦单。
                logger.warning(
                    f"[双锚影子] {symbol} dual_anchor 块构造失败: {_da_err} "
                    f"(该行缺块, RFC-123 判据面缺样)", exc_info=True)
            _tp = _Path(module_file).resolve().parents[2] / "data" / "entry_timing_shadow.jsonl"
            with open(_tp, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if not data_router:
        return False

    # 获取实时行情 (含 day_high/day_low)
    current_price = None
    day_high = None
    day_low = None
    vol_ratio = None
    ctx = None  # 2026-07-15: 预置, 供 _emit_entry_shadow 安全引用 (取数异常时保持 None)
    try:
        ctx = data_router.get_full_market_context(symbol)
        if ctx and isinstance(ctx, dict):
            current_price = float(ctx.get("price", 0) or 0)
            day_high = float(ctx.get("day_high", 0) or ctx.get("high_price", 0) or 0)
            day_low = float(ctx.get("day_low", 0) or ctx.get("low_price", 0) or 0)
            vol_ratio = float(ctx.get("volume_5d_avg_ratio", 0) or ctx.get("volume_ratio", 0) or 0)
    except Exception:
        pass

    # fallback: 用 get_quote 取价格
    if not current_price or current_price <= 0:
        try:
            quote = data_router.get_quote(symbol)
            if quote and isinstance(quote, dict):
                current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
        except Exception:
            pass

    if not current_price or current_price <= 0:
        return False

    # 如果没有日内高低点 (开盘第一根K线), 用 entry_price 作为基准
    if day_high is None or day_high <= 0:
        day_high = entry_price
    if day_low is None or day_low <= 0:
        day_low = entry_price

    # pb_cfg 提前加载 (下方 #2 三道门 + 触发带共用)
    from app.playbook.config import get_playbook_config
    pb_cfg = get_playbook_config()

    # ── 2026-07-19 (DEB-round3 #2): 触发时间窗收紧 — ET≥14:00 不再新触发 ──
    #   根因: bridge ~9:00 ET 写入方向, 盘中数小时后 thesis 衰减; TIME_EXIT 11笔0%WR
    #   全部为午盘后方向未兑现单. 14:00 后触发=高噪音入场, 直接拒.
    try:
        _cutoff_enabled = bool(pb_cfg.get("entry_time_cutoff_enabled", True))
        _cutoff_hhmm = int(pb_cfg.get("entry_time_cutoff_hhmm", 1400))
    except Exception:
        _cutoff_enabled, _cutoff_hhmm = True, 1400
    if _cutoff_enabled:
        try:
            from datetime import datetime as _dt
            _now_gate = _dt.now(_now_et())
            if _now_gate.hour * 100 + _now_gate.minute >= _cutoff_hhmm:
                # 2026-07-28 降噪: 每标的每天只 INFO 一次 (此前每轮扫描都打, 日志 35+ 条/日)
                _cut_key = f"{symbol}:{_now_gate.strftime('%Y-%m-%d')}"
                if _cut_key not in _CUTOFF_LOGGED:
                    _CUTOFF_LOGGED.add(_cut_key)
                    logger.info(
                        f"[playbook] #2 触发窗截止: {symbol} ET={_now_gate.strftime('%H:%M')} "
                        f">= {_cutoff_hhmm // 100:02d}:{_cutoff_hhmm % 100:02d}, 不再新触发 (今日仅提示一次)"
                    )
                _emit_trigger_miss("after_cutoff", None)
                return False
        except Exception:
            pass  # 时间获取异常 → fail-open (与既有异常处理一致, 不拦)

    # ── 2026-08-08 (DEB-20260807 复盘): 开盘禁触窗 — ET<10:00 不新触发 ──
    #   数据依据: 9点档(开盘首小时)入场 7月 120笔 净-$931 (93%亏损) / 8月 35笔 净-$250,
    #   10点档 7月 42笔 +$132 / 8月 7笔 +$42 — 开盘噪声期入场期望为负;
    #   MFE_STALL 12/12 全部 9:35-9:53 入场 (温床). 与既有 14:00 cutoff 对称.
    #   回滚: entry_open_blackout_enabled 改 false.
    try:
        _oob_enabled = bool(pb_cfg.get("entry_open_blackout_enabled", False))
        _oob_hhmm = int(pb_cfg.get("entry_open_blackout_hhmm", 1000))
    except Exception:
        _oob_enabled, _oob_hhmm = False, 1000
    if _oob_enabled:
        try:
            from datetime import datetime as _dt2
            _now_oob = _dt2.now(_now_et())
            if _now_oob.hour * 100 + _now_oob.minute < _oob_hhmm:
                _emit_trigger_miss("before_open_blackout", None)
                return False
        except Exception:
            pass  # 时间获取异常 → fail-open (不拦)

    # ── 2026-07-19 (DEB-round3 #2): 财报禁建门 — 触发时距财报<24h 不入场 ──
    #   bridge 盘前写入时距财报可能>1天, 盘中进入禁建窗后 thesis 面临事件风险重估.
    try:
        _eb_enabled = bool(pb_cfg.get("earnings_blackout_gate_enabled", True))
    except Exception:
        _eb_enabled = True
    if _eb_enabled:
        try:
            _erg = getattr(scheduler, "earnings_risk_guard", None)
            if _erg is not None:
                _is_eb, _eb_reason = _erg.is_earnings_blackout(symbol)
                if _is_eb:
                    logger.info(f"[playbook] #2 财报门拦截: {symbol} {_eb_reason}")
                    _emit_trigger_miss("earnings_blackout", current_price)
                    return False
        except Exception:
            pass  # 财报日历异常 → fail-open (与系统既有财报门行为一致)

    # ── 2026-07-24 (DEB-20260723/24): 触发侧双闸门 — 低ATR硬门 + 同标的低MFE连败封禁 ──
    #   低ATR门: 低波动票做日内动量结构性失败 (7月 atr_pct<2.5% 桶 13笔 15%WR -$244,
    #   MFE<0.5% 占 85% — 波动能力决定 MFE 天花板). atr_pct 缺失 → fail-open 不拦.
    #   回滚: low_atr_gate_enabled 改 false.
    try:
        _atr_gate_enabled = bool(pb_cfg.get("low_atr_gate_enabled", False))
        _atr_gate_min = float(pb_cfg.get("low_atr_gate_min_atr_pct", 0.025))
    except Exception:
        _atr_gate_enabled, _atr_gate_min = False, 0.025
    if _atr_gate_enabled:
        # [P1.2 2026-09-12, proud-slope-falcon] 筛选快照接到原执行低ATR门 (读同义字段)。
        #   主口径不变: inst["atr_pct"] = 盘前定桶写入的**止损口径** (get_atr_pct,
        #   可能含 VIX 缩放/三层底限/分钟兜底) —— 本批不重定义它的含义, 也不动
        #   vol_bucket / 重定价 / 退出参数。
        #   新增兜底: atr_pct 缺失或不可用时读 raw_instruction.candidate_screening
        #   .screening_atr_ratio (原始已完成日线 TR14 rolling-mean / 参考价)。修前该
        #   情形直接 fail-open = 门对无桶行恒开 (静默死门同族)。
        #   量纲声明: 两者**同为比例**, 0.03 = 3% (与 low_atr_gate_min_atr_pct 同单位);
        #   实测 DB 728 行 atr_pct ∈ [0.0064, 0.1538] 佐证比例口径。>1.0 一律判为
        #   量纲错误 (百分数误写) → 拒用该值并保持 fail-open, 绝不让门静默恒开/恒关。
        #   时效边界: screening 快照是**盘前**观测, 只在无行内更新值时兜底, 不当实时
        #   行情用; 命中时把依据与快照年龄写进遥测 (atr_basis / atr_age_hours),
        #   使"这单是哪一路 ATR 拦的"可事后归因。
        _atr_val, _atr_basis, _atr_age_h = None, "none", None
        try:
            _raw_atr = inst.get("atr_pct")
            if _raw_atr is not None:
                _cand_atr = float(_raw_atr)
                if 0 < _cand_atr:
                    _atr_val, _atr_basis = _cand_atr, "instruction_atr_pct"
                else:
                    _atr_basis = "instruction_atr_pct_nonpositive"
        except (TypeError, ValueError, OverflowError):
            _atr_basis = "instruction_atr_pct_unparsable"
        if _atr_val is None:
            try:
                from app.playbook.candidate_eligibility import get_instruction_screening
                _scr = get_instruction_screening(inst) or {}
                _ratio = _scr.get("screening_atr_ratio")
                if _ratio is not None:
                    _ratio_f = float(_ratio)
                    if 0 < _ratio_f <= 1.0:
                        _atr_val, _atr_basis = _ratio_f, "candidate_screening"
                        _atr_age_h = _screening_age_hours(_scr.get("observed_at"))
                    else:
                        _atr_basis = "candidate_screening_unit_error"
                        logger.warning(
                            f"[playbook] 低ATR门: {symbol} screening_atr_ratio={_ratio_f} "
                            f"超出比例口径 (0,1] → 拒用 (量纲错误, 保持 fail-open)")
                elif _scr:
                    _atr_basis = "candidate_screening_no_ratio"
            except Exception as _scr_e:
                _atr_basis = "candidate_screening_unreadable"
                logger.warning(f"[playbook] 低ATR门: {symbol} 筛选快照不可读 → fail-open: "
                               f"{_scr_e}", exc_info=True)
        if _atr_val is not None and _atr_val < _atr_gate_min:
            _k = (symbol, _today_et_str())
            if _k not in _low_atr_logged:
                _low_atr_logged.add(_k)
                logger.info(
                    f"[playbook] 低ATR门拦截: {symbol} atr={_atr_val:.4f} "
                    f"< {_atr_gate_min} (basis={_atr_basis}"
                    f"{f', 快照龄={_atr_age_h:.1f}h' if _atr_age_h is not None else ''}"
                    f"; 低波票不做日内动量, 当日首记后静默)"
                )
            _emit_trigger_miss("low_atr_block", current_price, extra={
                "atr_basis": _atr_basis, "atr_value": round(_atr_val, 6),
                "atr_threshold": _atr_gate_min, "atr_age_hours": _atr_age_h})
            return False
    #   连败封禁: 跨日亏损记忆 (CRM 三连 TIME_EXIT 磨死类模式), 见 _symbol_streak_banned 注释.
    if _symbol_streak_banned(symbol, pb_cfg):
        _emit_trigger_miss("streak_ban", current_price)
        return False

    # ── 触发条件: 方向穿越触发 (2026-07-02: 从±0.5%改为方向穿越) ──
    # 根因: ±0.5% touch zone导致0%触发率(17/17 miss).
    # 修复: 限价单逻辑 — 价格从entry_price的盈利方向穿越即触发.
    #   BUY:  当前价 ≤ entry_price × 1.01 (回落到入场价或更低 → 买)
    #   SHORT: 当前价 ≥ entry_price × 0.99 (反弹到入场价或更高 → 做空)
    # SL控制风险: 触发后仍检查是否已越过stop_loss.
    # (pb_cfg 已在上方 #2 门处加载, 不重复获取)

    # ── 触发条件: 顺势直接触发 + 逆势追入需量能确认 (质量门设计) ──
    # 设计原则 (非历史回填): 入场质量决定胜率, 分两类:
    #   (1) 有利方向 (回调买入 / 反弹做空): 价格来到计划入场价的有利一侧 → 最优 RR, 直接触发.
    #       仅设"接飞刀"下限, 避免价格已深度背离计划位仍入场.
    #   (2) 追入 (突破追涨 / 破位追空): 价格越过计划位, 属动量确认场景 →
    #       必须有成交量放大确认 (vol_ratio ≥ 阈值), 无量突破视为假突破, 不追.
    # 量能数据缺失时 fail-open, 避免因行情源无该字段而全部漏单.
    # P0 修复 (2026-07-10): vol_ratio 的"无数据"哨兵是 1.0 而非 0 —
    #   market_data_router.setdefault("volume_ratio", 1.0) 与 lv3_technical_calculator
    #   在 vol_ma5 缺失时均回落 1.0. Futu LV3 主链路盘中不算 volume_ratio → 恒为 1.0.
    #   原 fail-open 只覆盖 <=0, 1.0 会走 "1.0 < 1.5 → False" → 所有追入被永久阻断.
    #
    # 单一真源 app/playbook/entry_band.py — 执行端与 DA 直写共用同一 band 定义, 防百分比漂移/记录冲突.
    from app.playbook.entry_band import CHASE_ZONE_PCT as _chase_zone, KNIFE_FLOOR_PCT as _knife_floor
    # RFC-065 (DEB-20260819 §三十四, 条件版A): vol_widen 生产开关 — 默认 false 零行为变更.
    #   开启后 band 按 ATR 只放宽不收窄 (chase上限8%=extreme_gap线); ATR缺失自动回落扁平.
    #   影子记录 (widen_eligible) 与本开关无关, 常开攒样 (§32.3).
    try:
        _widen_prod_enabled = bool(pb_cfg.get("entry_band_vol_widen_enabled", False))
    except Exception:
        _widen_prod_enabled = False
    if _widen_prod_enabled:
        from app.playbook.entry_band import vol_widen_band_pct as _wb_prod
        _knife_floor, _chase_zone = _wb_prod(inst.get("atr_pct"))
    # 2026-07-20 重建: 盘中实时量能门 (替代原日级 volume_ratio).
    #   公式: today_cum_vol / (avg_daily_vol_5d × elapsed_fraction) ≥ threshold.
    #   数据源: Futu LV3 K_1M (300s缓存, 基本不额外耗配额) + 日K线 (5min缓存).
    #   fail-open: 数据不足 → None → 视为通过, 不因行情源异常阻断全部追入.
    #   可配置: pb_cfg["intraday_vol_gate_enabled"] (默认True),
    #           pb_cfg["chase_vol_ratio_threshold"] (默认1.5).
    _vol_gate_enabled = bool(pb_cfg.get("intraday_vol_gate_enabled", True))
    _vol_threshold = float(pb_cfg.get("chase_vol_ratio_threshold", 1.5))
    # 2026-08-23 (分流对比影子): 默认值前置 — 门关闭/豁免窗分支不定义这两个变量,
    #   后置影子发射块引用会 NameError。
    _vol_use_p50 = None
    _vol_p50_shadow_ratio = None
    # 2026-07-20: 开盘豁免窗 — 盘初 N 分钟量能数据最不可靠 (累计量小 + 推送/K线尚未铺满),
    #   此窗内不拦, 避免开盘系统性误拦. 默认 15min. 可配置 intraday_vol_gate_grace_min.
    _vol_grace_min = float(pb_cfg.get("intraday_vol_gate_grace_min", 15))
    _in_open_grace = False
    try:
        from datetime import datetime as _dt_vg
        _et_now = _dt_vg.now(_now_et())
        _mins_since_open = (_et_now.hour * 60 + _et_now.minute) - (9 * 60 + 30)
        _in_open_grace = 0 <= _mins_since_open < _vol_grace_min
    except Exception:
        _in_open_grace = False
    if _vol_gate_enabled and not _in_open_grace:
        # 分子由 _calc_intraday_vol_ratio 内部经 get_quote() 取 Futu LV3 快照当日累计量
        #   (真实盘中累计, <100ms). 不用 ctx["volume"] — 主链路 ctx 来自日K线技术指标,
        #   其 volume 是最近日线 bar(可能昨日), 非今日盘中累计, 混用会污染分子.
        # P1-3 源分流 (2026-08-23 用户裁): DA 走线性(对比组), reentry/UE 走 p50 曲线
        _did_vol = str(inst.get("decision_id") or "")
        _vol_da_enforce = bool(pb_cfg.get("volume_gate_vwap_relative_da_enforce", False))
        _vol_use_p50 = None if (not _did_vol.startswith("DA-") or _vol_da_enforce) else False
        _intraday_vr = _calc_intraday_vol_ratio(data_router, symbol, use_p50_curve=_vol_use_p50)
        # 2026-08-23 (用户裁分流对比可测性): DA 源走线性, 但同时记录 p50 影子比 —
        #   毕业判据"DA 对比组方向一致"需要知道每次 DA 评估"p50 会怎么判",
        #   否则对比组不可测。record-only 不影响触发; 二次调用成本 = get_quote
        #   (推送缓存 <100ms) + 曲线缓存, 每指令每轮一次, 可忽略。
        if _vol_use_p50 is False:
            try:
                _vol_p50_shadow_ratio = _calc_intraday_vol_ratio(
                    data_router, symbol, use_p50_curve=True)
            except Exception:
                _vol_p50_shadow_ratio = None
        else:
            _vol_p50_shadow_ratio = None
    else:
        _intraday_vr = None
    if _intraday_vr is None:
        _vol_ok = True  # fail-open: 数据不足/开盘豁免窗 不拦
    else:
        _vol_ok = _intraday_vr >= _vol_threshold
    if _intraday_vr is not None:
        logger.info(
            f"[playbook] 盘中量能门: {symbol} intraday_vol_ratio={_intraday_vr:.2f} "
            f"threshold={_vol_threshold} → {'PASS' if _vol_ok else 'BLOCK'}"
        )
    elif _vol_gate_enabled and _in_open_grace:
        logger.debug(f"[playbook] 盘中量能门: {symbol} 开盘豁免窗({_vol_grace_min:.0f}min)内, 跳过")
    elif _vol_gate_enabled:
        # DEB-20260721 R4: 数据缺失失明必须可观测可计数 (原完全静默放行). 豁免窗跳过是设计行为, 维持 debug.
        logger.warning(f"[playbook] 盘中量能门: {symbol} 量能数据缺失 (fail-open 放行)")

    # P1-C (2026-07-24, DEB-20260724): 入场路径标注 — pullback(回调/反弹有利侧) / chase(追入) /
    #   out_of_band(出带未触发). 量能门拦截时保留 chase (表示"想走 chase 被拦"). 供 _emit_entry_shadow 落库.
    # P0 (2026-09-17, 用户裁「接收建议」): 触发条件宽松化 — 98% missed 因"未触发",
    #   根因是 chase_zone=5% 过严. 加"接近确认位"宽松条件: 距 chase_zone 边界 ±1% 内也触发.
    _entry_path = None
    _near_miss_tolerance = 0.01  # ±1% 接近确认位容忍度
    if is_short:
        if current_price >= entry_price:
            # 反弹到入场价或更高 → 更优做空点, 直接触发 (设上限防已冲高背离)
            triggered = current_price <= entry_price * (1 + _knife_floor)
            _entry_path = "pullback"
        elif current_price >= entry_price * (1 - _chase_zone):
            # 跌破入场价 → 追空, 需量能确认 (防无量假跌)
            triggered = _vol_ok
            _entry_path = "chase"
        elif current_price >= entry_price * (1 - _chase_zone - _near_miss_tolerance):
            # P0: 接近 chase_zone 边界 (±1% 内) → 宽松触发
            triggered = _vol_ok
            _entry_path = "chase_near_miss"
        else:
            triggered = False
            _entry_path = "out_of_band"
    else:
        if current_price <= entry_price:
            # 回调到入场价或更低 → 更优买点, 直接触发 (设下限防接飞刀)
            triggered = current_price >= entry_price * (1 - _knife_floor)
            _entry_path = "pullback"
        elif current_price <= entry_price * (1 + _chase_zone):
            # 突破入场价 → 追涨, 需量能确认 (防无量假突破)
            triggered = _vol_ok
            _entry_path = "chase"
        elif current_price <= entry_price * (1 + _chase_zone + _near_miss_tolerance):
            # P0: 接近 chase_zone 边界 (±1% 内) → 宽松触发
            triggered = _vol_ok
            _entry_path = "chase_near_miss"
        else:
            triggered = False
            _entry_path = "out_of_band"

    # ── 2026-08-23 (用户裁分流对比): DA 源量能门线性 vs p50 影子对照 ──
    #   DA 走线性(不拦), 同轮记录"p50 会怎么判" — 毕业判据"DA 对比组方向一致"的
    #   唯一数据源。仅记 chase 路径 (量能门语义范围), record-only 不影响触发。
    #   回滚: volume_gate_vwap_relative_da_enforce 改 true 后本分支自动停写。
    if (_vol_use_p50 is False and _entry_path == "chase" and _intraday_vr is not None
            and "TEST" not in symbol.upper()):
        try:
            import json as _vps_json
            from pathlib import Path as _vps_path
            from datetime import datetime as _dt  # 显式绑定: 外层 _dt 是条件导入 (L4651
            #   cutoff_enabled 才绑定), 依赖它会重蹈 NameError 被静默吞的覆辙
            # 2026-08-26 修复: _now_et() 返回时区对象非 datetime (同 _emit_exec_gate_audit
            #   / _emit_trigger_miss 静默死亡根因) — 改 _dt.now(_now_et())。
            _vps_now = _dt.now(_now_et())
            _vps_row = {
                "ts": _vps_now.isoformat(timespec="seconds"),
                "date": _vps_now.strftime("%Y-%m-%d"),
                "inst_id": inst.get("id"),
                "decision_id": str(inst.get("decision_id") or ""),
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "linear_ratio": _intraday_vr,
                "p50_ratio": _vol_p50_shadow_ratio,
                "threshold": _vol_threshold,
                "linear_pass": bool(_intraday_vr >= _vol_threshold),
                "p50_would_pass": (None if _vol_p50_shadow_ratio is None
                                   else bool(_vol_p50_shadow_ratio >= _vol_threshold)),
            }
            _vps_fp = _vps_path(module_file).resolve().parents[2] / "data" / "vol_gate_da_p50_shadow.jsonl"
            with open(_vps_fp, "a", encoding="utf-8") as _f:
                _f.write(_vps_json.dumps(_vps_row, ensure_ascii=False) + "\n")
        except Exception:
            pass  # shadow 纯记录, 绝不影响触发

    # ── RFC-065 widen 影子 (DEB-20260819 §三十四, record-only 常开攒样) ──
    #   生产开关关闭时: 用 widen 带重算触发几何 — widen 能触发而 flat 不能 → 登记影子.
    #   widen 带是 flat 严格超集 (只放宽) + 量能门同款 → flat触发⟹widen同轮必触发,
    #   故只需记 "widen触发且flat未触发" 事件; 离线结算对照实际触发时点分 new/changed.
    #   回滚: playbook.entry_band_widen_shadow_enabled 置 false (影子独立于生产开关).
    if not _widen_prod_enabled and not triggered:
        try:
            if bool(pb_cfg.get("entry_band_widen_shadow_enabled", True)):
                from app.playbook.entry_band import vol_widen_band_pct as _wb_shadow
                _wk, _wc = _wb_shadow(inst.get("atr_pct"))
                if (_wk, _wc) != (_knife_floor, _chase_zone):
                    if is_short:
                        if current_price >= entry_price:
                            _w_hit, _w_path = current_price <= entry_price * (1 + _wk), "pullback"
                        elif current_price >= entry_price * (1 - _wc):
                            _w_hit, _w_path = _vol_ok, "chase"
                        else:
                            _w_hit, _w_path = False, "out_of_band"
                    else:
                        if current_price <= entry_price:
                            _w_hit, _w_path = current_price >= entry_price * (1 - _wk), "pullback"
                        elif current_price <= entry_price * (1 + _wc):
                            _w_hit, _w_path = _vol_ok, "chase"
                        else:
                            _w_hit, _w_path = False, "out_of_band"
                    if _w_hit:
                        _emit_widen_shadow(inst, symbol, is_short, _w_path,
                                           current_price, entry_price, _wk, _wc,
                                           flat_triggered=False,
                                           flat_path=_entry_path,
                                           vol_gate_block=not _vol_ok)
        except Exception:
            pass  # 影子异常绝不影响触发判定

    # ── 2026-08-08 (DEB-20260808 §8.3 裁定①): SHORT 禁追涨 shadow — 观察 10:00+ 纯逆势队列 ──
    #   背景: "SHORT 且当日涨>3% 禁入" 硬门在 post-blackout 基线上残存效应为负 (-$19.80,
    #   被禁触窗架空: 8月逆势亏单全在 9点档), 故不落硬门, 仅 shadow 记录: SHORT 且
    #   chg_pct>阈值 时落一行 (含是否触发/路径), 供攒样本后评估规则复活条件 (若禁触窗
    #   缩短或 10:00+ 出现逆势追空亏损). 回滚: short_chase_ban_shadow_enabled 改 false.
    try:
        _scb_enabled = bool(pb_cfg.get("short_chase_ban_shadow_enabled", False))
        if _scb_enabled and is_short:
            _scb_pct = float(pb_cfg.get("short_chase_ban_pct", 3.0))
            _chg_now = None
            if ctx and isinstance(ctx, dict):
                _chg_now = ctx.get("change_percent")
            if _chg_now is not None:
                import json as _scb_json
                from pathlib import Path as _scb_path
                from app.utils.timezone import now_et as _scb_now
                _scb_ts = _scb_now()
                _scb_date = _scb_ts.date().isoformat()
                _scb_inst_id = inst.get("id")
                _scb_over = float(_chg_now) > _scb_pct
                _scb_emit = _scb_over
                if not _scb_over:
                    # 对照臂扫描行: 每 (指令, ET日) 首轮 1 行 (2026-09-18 采样设计修复,
                    # 命中行逐轮写语义不变; 扫描行不入判据计数)。
                    _scb_key = (_scb_inst_id, _scb_date)
                    if _scb_key not in _scb_scan_logged:
                        _scb_scan_logged.add(_scb_key)
                        _scb_emit = True
                if _scb_emit:
                    _scb_row = _short_chase_shadow_row(
                        symbol=symbol, chg_pct=_chg_now, threshold=_scb_pct,
                        entry_price=entry_price, current_price=current_price,
                        triggered=triggered, entry_path=_entry_path,
                        inst_id=_scb_inst_id, decision_id=inst.get("decision_id"),
                        date_str=_scb_date, ts_str=_scb_ts.isoformat(timespec="seconds"),
                        scan=not _scb_over)
                    _scb_fp = _scb_path(module_file).resolve().parents[2] / "data" / "short_chase_ban_shadow.jsonl"
                    with open(_scb_fp, "a", encoding="utf-8") as _f:
                        _f.write(_scb_json.dumps(_scb_row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # shadow 纯记录, 绝不影响触发

    if not triggered and current_price > 0:
        # 区分量能门拦截 vs 其他原因 (供离线分析追入被拦频率)
        _miss_reason = "vol_gate_block" if (not _vol_ok and _intraday_vr is not None) else "price_band_miss"
        if _miss_reason == "vol_gate_block":
            _emit_trigger_miss("vol_gate_block", current_price)
        logger.debug(
            f"[playbook] 触发门未过: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} intraday_vr={_intraday_vr} "
            f"dir={'SHORT' if is_short else 'BUY'} reason={_miss_reason}"
        )

    # 极端gap过滤 (>8%): 暴涨暴跌中不入场
    _price_gap = abs(current_price - entry_price) / entry_price
    if _price_gap > 0.08:
        logger.warning(
            f"[playbook] ⚔️ {symbol} 极端gap, 跳过触发: "
            f"current=${current_price:.2f} entry=${entry_price:.2f} gap={_price_gap:.1%}"
        )
        _emit_trigger_miss("extreme_gap", current_price)
        # 2026-08-11 (LIF 盘前-20%实锤): 逆方向极端 gap → thesis 已死, 永久作废.
        #   原设计"gap>8% 只拦不作废(保留回旋)"留洞: gap 门提前 return 导致下方
        #   drift 作废门(5%)永不执行; 若盘中反弹回触发带 (knife floor 3%), BUY 走
        #   pullback 路径无量能确认直接触发 = thesis 已死仍接飞刀
        #   (LIF: entry 59 / 盘前 52 / 反弹带 57.23-59 恰在 5%作废线 56.05 之上).
        #   仅逆方向 (BUY 价跌 / SHORT 价涨) 作废; 顺方向 gap 本就不触发, 不作废
        #   (thesis 可能仍有效只是价格暂远). 复用 skip_reason='thesis_drift_invalidate'
        #   → 下游守卫 (auto_execute 循环头检 + 本函数头检) 已全部消费该值, 零下游改动.
        #   回滚: thesis_drift_invalidate_enabled 置 false (与 drift 作废门同开关).
        _adverse_gap = ((not is_short and current_price < entry_price) or
                        (is_short and current_price > entry_price))
        if _adverse_gap and inst.get("id") is not None:
            try:
                if bool(pb_cfg.get("thesis_drift_invalidate_enabled", True)):
                    from app.playbook.db import PlaybookDB as _PBDB
                    _PBDB().update_instruction_tracking(
                        inst["id"], {"skip_reason": "thesis_drift_invalidate"})
                    logger.warning(
                        f"[playbook] #2 thesis漂移作废(极端gap): {symbol} "
                        f"dir={'SHORT' if is_short else 'BUY'} entry=${entry_price:.2f} "
                        f"current=${current_price:.2f} gap={_price_gap:.1%} → 指令永久作废"
                    )
            except Exception as _gap_inv_e:
                logger.warning(f"[playbook] 极端gap作废落库失败 {symbol}: {_gap_inv_e}")
        return False

    # ── 流动性门 (2026-07-30, 用户裁"小盘票补充限制"): 仙股地板 + 名义额/ADV$ 上限 ──
    #   低 ATR 门挡的是低波动, 恰不挡高波动小票; sim 盘口快照成交使低流动盈利虚高。
    #   只减不加; ADV 日级缓存零配额增量; fail-open; 回滚=关 liquidity_gate_enabled。
    #   名义额取真实计划仓位 position_size (复核修正: 默认系数低估实际单 ~20%)。
    _planned_qty = float(inst.get("position_size") or 0)
    _lq_ok, _lq_detail = _check_liquidity_gate(data_router, symbol, current_price, pb_cfg,
                                               planned_qty=_planned_qty)
    if not _lq_ok:
        from app.utils.timezone import now_et as _lqt_now
        _lq_key = f"{symbol}:{_lqt_now().strftime('%Y-%m-%d')}"
        if _lq_key not in _LIQUIDITY_LOGGED:
            _LIQUIDITY_LOGGED.add(_lq_key)
            logger.warning(f"[playbook] 💧 {symbol} 流动性门拦截 (今日仅提示一次): {_lq_detail}")
        _emit_trigger_miss("liquidity_gate", current_price)
        return False

    # ── SHORT 借券池门 (2026-07-30, 用户裁"直接做"): 不可卖空/券池枯竭 → 拦 ──
    #   sim 无条件假成交, 实盘 is_short_permit=False 必拒/券池枯竭遭召回 — sim 贴实盘基准。
    if is_short:
        _bp_ok, _bp_detail = _check_borrow_pool_gate(symbol, _planned_qty, pb_cfg)
        if not _bp_ok:
            from app.utils.timezone import now_et as _bpt_now
            _bp_key = f"{symbol}:{_bpt_now().strftime('%Y-%m-%d')}"
            if _bp_key not in _BORROW_POOL_LOGGED:
                _BORROW_POOL_LOGGED.add(_bp_key)
                logger.warning(f"[playbook] 🩳 {symbol} 借券池门拦截 (今日仅提示一次): {_bp_detail}")
            _emit_trigger_miss("borrow_pool_gate", current_price)
            return False

    # ── 2026-07-19 (DEB-round3 #2): thesis 漂移作废门 (5%, 永久作废) ──
    #   价格相对 bridge 入场价【逆方向】漂移 >5% → 盘前 thesis 失效:
    #     BUY: current < entry×(1-5%);  SHORT: current > entry×(1+5%).
    #   写 skip_reason='thesis_drift_invalidate' 永久作废 (本指令不再触发/执行).
    #   与 knife_floor(3%) 间留 2% 缓冲防误杀正常回调; >8% 由 extreme_gap 拦截在上方,
    #   不作废 (极端波动日保留回旋). 特异性 skip_reason 不影响其他语义.
    try:
        _drift_enabled = bool(pb_cfg.get("thesis_drift_invalidate_enabled", True))
        _drift_pct = float(pb_cfg.get("thesis_drift_invalidate_pct", 5.0)) / 100.0
    except Exception:
        _drift_enabled, _drift_pct = True, 0.05
    if _drift_enabled:
        _adverse = ((not is_short and current_price < entry_price * (1 - _drift_pct)) or
                    (is_short and current_price > entry_price * (1 + _drift_pct)))
        if _adverse:
            try:
                from app.playbook.db import PlaybookDB as _PBDB
                _PBDB().update_instruction_tracking(
                    inst["id"], {"skip_reason": "thesis_drift_invalidate"})
            except Exception as _inv_e:
                logger.warning(f"[playbook] #2 漂移作废落库失败 {symbol}: {_inv_e}")
            logger.warning(
                f"[playbook] #2 thesis漂移作废: {symbol} dir={'SHORT' if is_short else 'BUY'} "
                f"entry=${entry_price:.2f} current=${current_price:.2f} "
                f"漂移={_price_gap:.1%} > {_drift_pct:.0%} → 指令永久作废"
            )
            _emit_trigger_miss("thesis_drift_invalidate", current_price)
            return False

    # 2026-07-11: 入场重锚 (Lever A) — 带内未触发但漂移在上限内, 用盘中价重锚触发.
    # 下游 auto_execute 的 re-price 块会以实时 bid/ask 重算 entry/SL/TP (风险% 不变),
    # 故这里无需改 DB/SL; 超上限→不追, 维持跳过. extreme_gap>8% 已在上方拦截.
    # 2026-07-20 修复: 量能门主动拦截时不允许 reanchor 绕过.
    #   根因: reanchor 原设计覆盖"价格出带"场景, 但 `if not triggered` 无差别覆盖
    #   了量能门拦截 (vol_gate_block) → 0-4% 追入被量能门拒后 reanchor 又放行,
    #   量能门形同虚设. 修复: 仅"价格出带"(非质量门拦截)时才允许 reanchor.
    _reanchored = False
    _vol_gate_actively_blocked = (not _vol_ok and _intraday_vr is not None)
    # 影子组合 (2026-08-09): 量能门拦下的 chase 单 / 跑出追入带的单 → 登记影子入场
    if not triggered and _vol_gate_actively_blocked and _entry_path == "chase":
        _shadow_portfolio_emit(inst, symbol, is_short, "vol_gate", _entry_path,
                               current_price, entry_price)
    elif not triggered and _entry_path == "out_of_band":
        _shadow_portfolio_emit(inst, symbol, is_short, "band", _entry_path,
                               current_price, entry_price)
    if not triggered and not _vol_gate_actively_blocked:
        try:
            _ra_enabled = bool(pb_cfg.get("entry_reanchor_enabled", False))
            _ra_max_drift = float(pb_cfg.get("entry_reanchor_max_drift_pct", 4.0)) / 100.0
        except Exception:
            _ra_enabled, _ra_max_drift = False, 0.04
        if _ra_enabled and _price_gap <= _ra_max_drift:
            _reanchored = True
            triggered = True
            _entry_path = "reanchored"
            _emit_trigger_miss("reanchored", current_price)
            logger.info(
                f"[playbook] 入场重锚触发: {symbol} price=${current_price:.2f} "
                f"entry=${entry_price:.2f} 漂移={_price_gap:.1%} (<= {_ra_max_drift:.0%}), 下游将 re-price"
            )

    # 入场前止损检查: 如果当前价格已越过 stop_loss, 不入场
    # 重锚单跳过此检查: 旧 SL 是盘前值, 下游 re-price 会基于新 entry 重算 SL
    if triggered and not _reanchored:
        stop_loss = float(inst.get("stop_loss", 0) or 0)
        if stop_loss > 0:
            if is_short and current_price >= stop_loss:
                logger.warning(f"[playbook] ⚔️ {symbol} current=${current_price:.2f} 已越过 SHORT SL=${stop_loss:.2f}")
                _emit_trigger_miss("sl_crossed", current_price)
                return False
            if not is_short and current_price <= stop_loss:
                logger.warning(f"[playbook] ⚔️ {symbol} current=${current_price:.2f} 已越过 LONG SL=${stop_loss:.2f}")
                _emit_trigger_miss("sl_crossed", current_price)
                return False

    # ── 2026-07-15: VWAP 方向一致性硬门 (Tier1, 免标定) ──
    #   规则: LONG 要价在当日 VWAP 上侧(dev>=tol), SHORT 要价在下侧(dev<=-tol), 逆侧拦.
    #   2026-07-16 修正: 改用当日累计 VWAP ctx["vwap"] (K线 cum(tp×vol)/cum(vol), K_1M 可靠填充);
    #     原 ticker_vwap_deviation 仅 ~250 笔滚动VWAP — 语义错(非当日锚, 突破瞬间近乎恒真),
    #     且依赖 futu push tickers>=10 常缺失. dev=(price-vwap)/vwap; VWAP缺失(盘初K线不足)→fail-open.
    #   config: entry_vwap_gate_enabled(开关) / entry_vwap_gate_tolerance(0.003=0.3%, 模块噪声底).
    breakout_triggered = triggered
    _vwap_dev = None
    _vwap_populated = False
    _vwap_val = None
    try:
        if isinstance(ctx, dict) and current_price and current_price > 0:
            # 2026-07-20 修复: 优先读 vwap_intraday (router 新增, 当日累计口径专用键).
            #   根因: ctx["vwap"] 主路径此前来自 60 根日K cumsum = "60日量加权均价",
            #   非当日 VWAP → 趋势股被方向性误拦 (上涨股 SHORT 恒拦 / 下跌股 LONG 恒拦).
            #   vwap_intraday=0 表示今日 bar 不足 (盘前/开盘头几分钟) → 视缺失 fail-open.
            #   无该键 (非 futu 主路径 ctx) → 退回 vwap (router 已同步覆盖为当日值).
            _vw = ctx.get("vwap_intraday")
            if _vw is None:
                _vw = ctx.get("vwap")
            if _vw is not None and float(_vw) > 0:
                _vwap_val = float(_vw)
                _vwap_dev = (float(current_price) - _vwap_val) / _vwap_val
                _vwap_populated = True
    except Exception:
        _vwap_dev, _vwap_populated, _vwap_val = None, False, None

    try:
        _gate_tol = float(pb_cfg.get("entry_vwap_gate_tolerance", 0.003) or 0.003)
    except Exception:
        _gate_tol = 0.0
    _gate_block = False
    if not _vwap_populated:
        _gate_reason = "vwap_missing"  # fail-open: 无 VWAP 数据不拦
    else:
        if is_short:
            _gate_block = _vwap_dev > _gate_tol   # 价在 VWAP 上侧 → 逆 SHORT
        else:
            _gate_block = _vwap_dev < -_gate_tol  # 价在 VWAP 下侧 → 逆 LONG
        _gate_reason = "wrong_side" if _gate_block else "aligned"

    try:
        _gate_enabled = bool(pb_cfg.get("entry_vwap_gate_enabled", True))
    except Exception:
        _gate_enabled = False

    if _gate_enabled and triggered and _gate_block:
        # 2026-08-07 (用户指示, sim A/B 双开): wrong_side 拦截 50/50 分流 —
        #   bypass 臂放行成交 (真实口径), gated 臂维持拦截 (影子反事实口径),
        #   双臂落 data/vwap_gate_ab.jsonl 台账. 非 sim / 开关关 / 异常 → 维持拦截.
        if _vwap_gate_ab_bypass(symbol, is_short, _vwap_dev, _vwap_val, current_price,
                                inst.get("id"), data_router=data_router, inst=inst):
            logger.info(
                f"[playbook] VWAP门A/B·放行臂: {symbol} price=${current_price:.2f} "
                f"vwap=${_vwap_val:.2f} dev={_vwap_dev:+.4f} "
                f"dir={'SHORT' if is_short else 'BUY'} (实验放行, 台账已记)"
            )
            # 2026-08-07 修复 (主动审计 P1-3): 放行臂是真实成交路径 (triggered 保持 True),
            #   不属于"未触发原因"漏斗 — 原 emit vwap_gate_ab_bypass 把成交单记入 miss 组,
            #   放行臂效果被系统性低估。台账行由 _vwap_gate_ab_bypass 内部保证。
        else:
            triggered = False
            logger.info(
                f"[playbook] VWAP门拦截: {symbol} price=${current_price:.2f} vwap=${_vwap_val:.2f} "
                f"dev={_vwap_dev:+.4f} dir={'SHORT' if is_short else 'BUY'} (当日VWAP逆侧)"
            )
            _emit_trigger_miss("vwap_gate_block", current_price)
            # [RFC-120 P1-4] skip_reason 回写 (非终态, 下轮复评) — 修复 LITE 9/30 型
            #   归因黑洞: 门拦 13 轮 DB 全程空白 → EOD 兜底盖 "未触发 (no_trigger)"
            #   误导标签。值不入 INVALIDATED_SKIP_REASONS, 头检/成交清库不变式不受影响。
            if inst.get("id") is not None:
                _write_round_skip_reason(
                    inst,
                    f"vwap_gate_blocked(dev={round(_vwap_dev * 100, 2)}%,"
                    f"{'逆SHORT' if is_short else '逆BUY'},下轮复评)")
            # 影子组合 (2026-08-09): VWAP 门拦下的本会触发单 → 影子入场 (A/B gated 臂全覆盖)
            _shadow_portfolio_emit(inst, symbol, is_short, "vwap_gate", _entry_path,
                                   current_price, entry_price, _vwap_dev)

    # ── P1-1 证伪门子集 (2026-08-23, DEB-20260823-phase1 知情破例) ──────────────
    #   把 reentry 已有的"证伪前置"思路下沉到 DA/UE 主链: 入场前先问"有什么在反驳这笔",
    #   而非只问"价格到了没"。四门中 ATR 地板已在 :4490 生产运行 (low_atr_gate_enabled),
    #   信息新鲜度门因 FTSeek 侧无新闻时间戳数据源而未实现 (见 DEB 设计文档 §P1-1 偏差)。
    #   全部 fail-open (数据缺失不拦), 逐门独立开关, 整体开关 falsification_gates_enabled。
    _fals_slope = None
    _fals_flow_diverged = None
    _fals_blocked = False
    if triggered and bool(pb_cfg.get("falsification_gates_enabled", True)):
        # ① VWAP 斜率门 (生产拦截): 近 5min volume-weighted 价在往反方向走 → 动量反驳该方向。
        #   与方向门正交 (方向门=价在均价哪侧, 本门=均价在往哪走)。证据方向支持:
        #   data/e2_momentum_replay_latest.json 入场动量确认反事实 blocked n=90 净-$1026
        #   WR 33% vs passed n=46 净+$184 WR 52%。回滚: falsification_gate_vwap_slope_enabled=false。
        if bool(pb_cfg.get("falsification_gate_vwap_slope_enabled", True)):
            try:
                _slope_tol = float(pb_cfg.get("falsification_gate_vwap_slope_tol", 0.001))
            except Exception:
                _slope_tol = 0.001
            _fals_slope = _calc_vwap_slope(data_router, symbol)
            if _fals_slope is not None:
                _slope_against = (_fals_slope > _slope_tol) if is_short else (_fals_slope < -_slope_tol)
                if _slope_against:
                    # 按源分流 (2026-08-23 用户裁): DA 大头先不动作对比组, reentry+UE 拦截
                    _did = str(inst.get("decision_id") or "")
                    _is_da = _did.startswith("DA-")
                    _da_enforce = bool(pb_cfg.get("falsification_gate_vwap_slope_da_enforce", False))
                    _do_block = not _is_da or _da_enforce
                    if _do_block:
                        triggered = False
                        _fals_blocked = True
                        # [RFC-120 P1-4] skip_reason 回写 (非终态) — 同 VWAP 门, 修 JBL 9/30 型归因黑洞
                        if inst.get("id") is not None:
                            _write_round_skip_reason(
                                inst,
                                f"falsification_slope_blocked(slope={_fals_slope:+.4f},"
                                f"下轮复评)")
                    logger.info(
                        f"[playbook] 证伪门·VWAP斜率: {symbol} slope={_fals_slope:+.4f} "
                        f"逆 {'SHORT' if is_short else 'BUY'} (tol={_slope_tol}) "
                        f"src={'DA' if _is_da else 'non-DA'} → "
                        f"{'拦截' if _do_block else '记录不拦(DA对比组)'}"
                    )
                    _emit_trigger_miss("falsification_vwap_slope", current_price)
                    if _do_block:
                        _shadow_portfolio_emit(inst, symbol, is_short, "falsification_vwap_slope",
                                               _entry_path, current_price, entry_price)
        # ② 资金流背离门 (record-only 影子, 用户 2026-08-23 裁决): 该信号回放四关门全不过
        #   (placebo p=0.85), 不得拦单 — 只落"若拦会怎样"供攒样后按同一判据重裁。
        if bool(pb_cfg.get("falsification_gate_capital_flow_shadow", True)):
            _fals_flow_diverged = _capital_flow_divergence(symbol, is_short)
            if _fals_flow_diverged is not None and "TEST" not in symbol.upper():
                try:
                    # 局部别名 import (本文件惯例, 见 L73-75): 迁移时本块漏带
                    #   Path/json/datetime 三名 ⇒ NameError 被下方 except 吞成 warning,
                    #   影子静默死亡 (2026-10-09 复查实证: 模块命名空间三名皆无)。
                    import json as _json
                    from datetime import datetime as _dt
                    from pathlib import Path as _Path
                    with open(_Path(module_file).resolve().parents[2] / "data"
                              / "falsification_gate_shadow.jsonl", "a", encoding="utf-8") as _ff:
                        _ff.write(_json.dumps({
                            "ts": _dt.now(_now_et()).isoformat(timespec="seconds"),
                            "date": _today_et_str(), "tz": "ET", "gate": "capital_flow",
                            "inst_id": inst.get("id"), "symbol": symbol,
                            "direction": "SHORT" if is_short else "BUY",
                            "price": round(float(current_price), 4),
                            "diverged": _fals_flow_diverged,
                            "would_block": _fals_flow_diverged,
                            "vwap_slope": _fals_slope,
                            "triggered": bool(triggered),
                        }, ensure_ascii=False) + "\n")
                except Exception as _shadow_err:
                    # A6 fail-loud (2026-08-24, 复查): 影子样本是十月重裁判据的
                    #   数据底座, 静默丢行 = 攒样进度虚高 + 故障不可见。降级为
                    #   warning 不抛 — 记录失败不得拦单 (fail-open 只属于交易路径)。
                    logger.warning(
                        f"[playbook] 证伪门·资金流影子落盘失败 {symbol}: "
                        f"{_shadow_err} (影子样本丢失, 十月攒样受影响)")

    if triggered:
        logger.info(
            f"[playbook] 方向穿越触发: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} dir={'SHORT' if is_short else 'BUY'}"
        )
        # 2026-08-06 (DEB-20260806 终裁"同意"): 锚限价影子 emit (record-only).
        #   每指令触发只落一行 → data/limit_entry_shadow.jsonl, 次日 crontab 重放判
        #   成交/延迟/改善 (配对δ完整定义: 追单可负/漏单=-净; 主门 sign-flip B=10000
        #   单侧α=0.025 + 双条件 + 符号共门). 回滚: config 置 false + 重启.
        try:
            from app.core.config_container import get_config as _gc_le
            if bool(_gc_le().get_dotted("playbook.limit_entry_shadow_observe", False)) \
                    and "TEST" not in symbol.upper():
                _lk = (inst.get("id"), _today_et_str())
                # [DEB-20260901 §六.4] 内存集 + 文件级双防重 (重启后内存集清零,
                #   文件级兜住; 历史旧行无 type 字段按 "row" 归一, 键不冲突)。
                # 2026-09-08 修潜伏 NameError: 原用 `_Path` 但本作用域无绑定 — 5079/5136 的
                #   `from pathlib import Path as _Path` 在嵌套子函数 (_emit_trigger_miss /
                #   _emit_entry_shadow) 内, 绑定不外泄到宿主 _check_intraday_trigger (AST 实证)。
                #   被 limit_entry_shadow_observe=false (8/29 用户裁 KILLED) 挡着从未求值,
                #   故影子 8/29 后停写的真因是那次裁决而非此 bug — 本行只治潜伏, 不改行为。
                from pathlib import Path as _Path
                _le_path = _Path(module_file).resolve().parents[2] / "data" / "limit_entry_shadow.jsonl"
                _le_claim = _shadow_dedup_claim(_le_path, inst.get("id"))
                if _lk not in _LIMIT_SHADOW_EMITTED and _le_claim is not None:
                    _LIMIT_SHADOW_EMITTED.add(_lk)
                    _micro = _fetch_l1_micro(scheduler, symbol)
                    import json as _json2
                    from datetime import datetime as _dt2
                    from pathlib import Path as _P2
                    _row = {
                        "ts": _dt2.now(_now_et()).isoformat(timespec="seconds"),
                        "date": _today_et_str(), "schema": "v1",
                        "inst_id": inst.get("id"), "decision_id": inst.get("decision_id"),
                        "symbol": symbol, "direction": "SHORT" if is_short else "LONG",
                        "anchor": round(float(entry_price), 4),
                        "entry_low": inst.get("entry_low"), "entry_high": inst.get("entry_high"),
                        "trigger_price": round(float(current_price), 4),
                        "entry_path": _entry_path,
                        "position_size": inst.get("position_size"),
                        "micro": _micro,
                    }
                    with open(_P2(module_file).resolve().parents[2] / "data" / "limit_entry_shadow.jsonl", "a", encoding="utf-8") as _f2:
                        _f2.write(_json2.dumps(_row, ensure_ascii=False) + "\n")
                    # DEB-20260904 #175: 写盘成功后才提交键
                    _shadow_dedup_commit(_le_claim)
                    logger.info(f"[锚限价影子] {symbol} {'SHORT' if is_short else 'LONG'} 触发行已落盘 (anchor=${entry_price:.2f})")
        except Exception as _anchor_err:
            # A6 fail-loud (2026-08-24): 锚限价影子行是次日重放判成交/延迟的输入,
            #   静默丢行会让配对δ样本悄悄缩水。warning 可见, 不抛不拦单。
            logger.warning(
                f"[playbook] 锚限价影子落盘失败 {symbol}: {_anchor_err} "
                f"(影子样本丢失, 重放判样本受影响)")
        # ── [DEB-20260901 Phase B] 结构化入场语义影子 (record-only, 零行为变更) ──
        # 仅对带 entry_archetype 的指令落触发上下文; 次日 cf_pullback_limit_replay.py
        # 判 would-fill / CF-PnL。无 archetype 的旧指令不进池。回滚: 配置开关置 false。
        try:
            from app.core.config_container import get_config as _gc_pb
            if bool(_gc_pb().get_dotted("playbook.pullback_limit_shadow_enabled", True)) \
                    and "TEST" not in symbol.upper():
                from pathlib import Path as _Ppb
                _pb_path = _Ppb(module_file).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
                _pb_claim = _shadow_dedup_claim(_pb_path, inst.get("id"), "trigger")
                if _pb_claim is not None:
                    # 2026-09-01 修复: raw_instruction_json 经 _row_to_dict 已是 dict,
                    #   旧 json.loads 抛 TypeError 被 except 静默吞 → archetype 恒 None
                    #   → 触发行零落盘 (首日全灭, 影子攒样实际未开始)。
                    from app.playbook.entry_routing import parse_raw_semantics
                    _sem_pb = parse_raw_semantics(inst.get("raw_instruction_json"))
                    _arch_pb = _sem_pb["entry_archetype"]
                    if _arch_pb:
                        import json as _json_pb
                        from datetime import datetime as _dt_pb
                        _row_pb = {
                            "ts": _dt_pb.now(_now_et()).isoformat(timespec="seconds"),
                            "date": _today_et_str(), "schema": "v1", "type": "trigger",
                            "inst_id": inst.get("id"), "decision_id": inst.get("decision_id"),
                            "symbol": symbol, "direction": "SHORT" if is_short else "LONG",
                            "entry_archetype": _arch_pb,
                            "confirmation": _sem_pb["confirmation"],
                            "chase_cap_price": _sem_pb["chase_cap_price"],
                            "entry_price": round(float(entry_price), 4),
                            "trigger_price": round(float(current_price), 4),
                            "entry_path": _entry_path,
                            "position_size": inst.get("position_size"),
                            "stop_loss": inst.get("stop_loss"),
                            "take_profit": inst.get("take_profit"),
                        }
                        _pb_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_pb_path, "a", encoding="utf-8") as _f_pb:
                            _f_pb.write(_json_pb.dumps(_row_pb, ensure_ascii=False) + "\n")
                        # DEB-20260904 #175: 写盘成功后才提交键
                        _shadow_dedup_commit(_pb_claim)
                        logger.info(
                            f"[archetype影子] {symbol} {_arch_pb} 触发行落盘 "
                            f"(确认位={(_sem_pb.get('confirmation') or {}).get('price_level')})")
        except Exception as _pb_err:
            logger.warning(
                f"[playbook] archetype影子落盘失败 {symbol}: {_pb_err} "
                f"(影子样本丢失, DEB-20260901 攒样受影响)")
    else:
        logger.debug(
            f"[playbook] 未触发: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} dir={'SHORT' if is_short else 'BUY'}"
        )
        # 2026-07-20 修复: 已有特异性 miss 原因 (vol_gate_block/vwap_gate_block) 时不重复发射,
        #   避免同一轮产生两条 trigger_miss 记录污染漏斗统计.
        #   P1-1 (2026-08-23): 证伪门拦截同属特异性原因, 一并纳入守卫.
        if not _vol_gate_actively_blocked and not (_gate_enabled and _gate_block) \
                and not _fals_blocked:
            # 2026-07-11: 带内未触发 — 分类追高/接飞刀 (BUY: >+5%追 / <-3%刀; SHORT 对称)
            if is_short:
                _miss_reason = "knife_above" if current_price > entry_price else "runaway_down"
            else:
                _miss_reason = "runaway_up" if current_price > entry_price else "knife_below"
            _emit_trigger_miss(_miss_reason, current_price)

    _emit_entry_shadow(
        triggered,
        breakout_val=breakout_triggered,
        gate_enabled=_gate_enabled,
        gate_block=_gate_block,
        gate_reason=_gate_reason,
        vwap_dev=_vwap_dev,
        vwap_populated=_vwap_populated,
        # 2026-07-20: 量能门状态 — ratio=None 表示数据不足 fail-open, block 表示本轮主动拦截.
        #   离线切片: 真实入场单中过量能门那批 PnL 是否更好 → 验证门的过滤价值.
        vol_gate_ratio=_intraday_vr,
        vol_gate_block=_vol_gate_actively_blocked,
        # P1-C (2026-07-24, DEB-20260724): 入场路径 + 指令conf — 分离 pullback/chase 两路径效果,
        #   并按 conf 切片验证"低conf回调路径零确认"是否为 MFE≈0 主力成因.
        entry_path=_entry_path,
        inst_confidence=inst.get("confidence"),
    )
    return triggered
