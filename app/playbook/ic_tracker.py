"""
Playbook IC Tracker — Playbook 指令预测力追踪 (Phase 1)

核心指标:
  Playbook IC = Spearman rank correlation(confidence, forward_return)
  - IC > 0.05: 指令有预测力 (可考虑毕业)
  - IC ≈ 0:    指令是噪音
  - IC < 0:    指令反向 (需要调整)

数据流:
  盘后 shadow_tracker → 更新 instruction 追踪字段
  盘后 backfill_forward_returns() → 回填 T+1/T+3/T+5 收益
  盘后 compute_playbook_ic() → 计算 IC + 按因子分解

设计原则:
  1. 纯只读计算, 不修改交易路径
  2. 复用 PlaybookDB, 不引入新表 (扩展 _TRACKING_FIELDS)
  3. 失败静默, 不影响主流程
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Any, Tuple
from datetime import date, datetime, timedelta
from collections import defaultdict
from app.core.scheduler_triggers import ET as _ET

logger = logging.getLogger(__name__)


def _rank(values: List[float]) -> List[float]:
    """Pure-Python rank (average rank for ties, 1-based)."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman_rank_correlation(
    x: List[float], y: List[float]
) -> Tuple[Optional[float], Optional[float]]:
    """
    Spearman 秩相关系数 (使用 scipy, 自动处理 tie / NaN / Inf)

    Args:
        x: 信号强度 (confidence)
        y: 前瞻收益 (forward_return)

    Returns:
        (rho, pvalue), 样本不足时返回 (None, None)
    """
    if not x or not y or len(x) != len(y):
        return None, None

    # 过滤 NaN / Inf
    pairs = [(float(a), float(b)) for a, b in zip(x, y)]
    pairs = [
        (a, b)
        for a, b in pairs
        if not (math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b))
    ]
    n = len(pairs)
    if n < 5:
        return None, None

    try:
        from scipy.stats import spearmanr

        _x = [p[0] for p in pairs]
        _y = [p[1] for p in pairs]
        rho, pvalue = spearmanr(_x, _y)
        if math.isnan(rho) or math.isnan(pvalue):
            return None, None
        return float(rho), float(pvalue)
    except Exception as e:
        logger.debug(f"Spearman computation failed, using fallback: {e}")

    # Fallback: pure-Python Spearman without scipy
    try:
        _x = [p[0] for p in pairs]
        _y = [p[1] for p in pairs]
        _xr = _rank(_x)
        _yr = _rank(_y)
        _n = len(_xr)
        _mx = sum(_xr) / _n
        _my = sum(_yr) / _n
        _cov = sum((a - _mx) * (b - _my) for a, b in zip(_xr, _yr))
        _vx = sum((a - _mx) ** 2 for a in _xr)
        _vy = sum((b - _my) ** 2 for b in _yr)
        if _vx == 0 or _vy == 0:
            return None, None
        _rho = _cov / math.sqrt(_vx * _vy)
        return float(_rho), None
    except Exception:
        return None, None


from app.utils.holiday_calendar import nth_trading_day_after


def _day_clustered_ic(valid_rows: List[Tuple]) -> Dict[str, Any]:
    """按日聚类 IC 检验 (2026-08-24, OKR-2026-09 KR1 修尺子 / 宪章第 9 律).

    背景 (DEB-20260823): pooled Spearman 对同日聚集的信号 (峰值 67 信号/日)
    把伪重复当独立样本, 显著性系统性夸大约 8 倍. 按日聚类: 每日算一个日内
    Spearman IC (≥5 样本的日才计), 再对日序列做单样本 t 检验 (H0: 日均 IC=0).

    判据 (宪章第 9 律, 预注册):
        可分辨 = |日均IC| > MDE 且 |t| >= 2
        MDE = 2.8 × sd_daymean / sqrt(n_days)   (单样本版; 配对版为 ×sqrt(2/n))

    Args:
        valid_rows: [(conf, fwd_return, ..., date), ...] date 为元组末位

    Returns:
        {"n_days", "day_ic_mean", "t", "mde", "distinguishable"} 或样本不足空态
    """
    by_day = defaultdict(list)
    for row in valid_rows:
        d = row[-1]
        if d:
            by_day[d].append((row[0], row[1]))

    day_ics = []
    for d in sorted(by_day):
        pairs = by_day[d]
        if len(pairs) < 5:  # 日内样本太少, 秩相关不稳定, 不计入
            continue
        ic, _p = _spearman_rank_correlation([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            day_ics.append(ic)

    n_days = len(day_ics)
    if n_days < 3:
        return {"n_days": n_days, "day_ic_mean": None, "t": None,
                "mde": None, "distinguishable": False}

    mean_ic = sum(day_ics) / n_days
    var = sum((x - mean_ic) ** 2 for x in day_ics) / (n_days - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return {"n_days": n_days, "day_ic_mean": round(mean_ic, 4), "t": None,
                "mde": 0.0, "distinguishable": False}
    t = mean_ic / (sd / math.sqrt(n_days))
    mde = 2.8 * sd / math.sqrt(n_days)
    return {
        "n_days": n_days,
        "day_ic_mean": round(mean_ic, 4),
        "t": round(t, 3),
        "mde": round(mde, 4),
        "distinguishable": bool(abs(mean_ic) > mde and abs(t) >= 2),
    }


def _sim_mechanism_adjusted_return(
    direction: str,
    entry_price: float,
    bars: list,
    exit_cfg: dict,
) -> float:
    """模拟"若按当前锁利/净保本/SL机制逐日盯市"的 T+N 实际收益 (日K OHLC 回放).

    统一坐标系: 所有量都是"相对 entry 的浮盈%"(正=赚, 负=亏), LONG/SHORT 完全对称.
      profit(px) = sign * (px - entry)/entry*100,  sign = +1 (LONG) / -1 (SHORT)
      - 峰值 peak = max over days of profit(day high 对 LONG / day low 对 SHORT)
      - 当日最不利点 adverse = profit(day low 对 LONG / day high 对 SHORT)

    机制 (参数读 config 实值, 与 _compute_dynamic_effective_sl 同源):
      1. 浮盈 >= breakeven_at_pct(0.4) → 保本武装, SL 抬到 fee_pad(0.4+0.2)
      2. trailing: SL = peak*(1-dd/100)  (dd=0.8), 不低于保本线
      3. 无 trailing 且浮盈 >= profit_override_threshold(1.5) → SL = peak*(1-po_dd/100)
      4. 初始 SL = -initial_sl_pct (默认 -5)
      5. 每日 adverse <= effective_sl → 按 effective_sl 成交 (保守无滑点)
      6. 无触发 → T+N 收盘价兜底

    返回: (exit_price - entry)/entry 的小数收益 (与 fwd_return_t{N} 同单位).
    """
    if not bars or entry_price <= 0:
        return 0.0
    is_long = direction == "BUY"
    sign = 1.0 if is_long else -1.0

    def profit(px):
        return sign * (px - entry_price) / entry_price * 100.0

    from app.playbook.config import BE_FALLBACK_FEE_CUSHION_PCT as _BE_FEE_FB
    be_pct = float(exit_cfg.get("breakeven_at_pct", 0.4))
    dd_pct = float(exit_cfg.get("drawdown_from_peak_pct", 0.8))
    fee_pad = float(exit_cfg.get("breakeven_fee_cushion_pct", _BE_FEE_FB)) + \
              float(exit_cfg.get("breakeven_slip_cushion_pct", 0.2))
    po_thresh = float(exit_cfg.get("profit_override_threshold_pct", 1.5))
    po_dd = float(exit_cfg.get("profit_override_drawdown_pct", 0.5))
    trailing_en = bool(exit_cfg.get("trailing_enabled", True))
    init_sl_pct = float(exit_cfg.get("initial_sl_pct", 5.0))

    peak_pct = 0.0
    effective_sl_pct = -init_sl_pct

    for bar in bars:
        h = float(bar.get("high", 0) or 0)
        l = float(bar.get("low", 0) or 0)
        cl = float(bar.get("close", 0) or 0)
        if h <= 0 or l <= 0 or cl <= 0:
            continue

        # 当日最有利点(更新峰值)与最不利点(判 SL)
        favorable = profit(h) if is_long else profit(l)
        adverse = profit(l) if is_long else profit(h)
        if favorable > peak_pct:
            peak_pct = favorable

        # 保本武装后更新 effective SL
        if peak_pct >= be_pct:
            cur_sl = fee_pad
            if trailing_en and dd_pct > 0:
                lock = peak_pct * (1.0 - dd_pct / 100.0)
                cur_sl = max(cur_sl, lock)
            elif not trailing_en and peak_pct >= po_thresh:
                lock = peak_pct * (1.0 - po_dd / 100.0)
                cur_sl = max(cur_sl, lock)
            effective_sl_pct = max(effective_sl_pct, cur_sl)

        # 最不利点击穿 SL → 按 SL 成交
        if adverse <= effective_sl_pct:
            return round(effective_sl_pct / 100.0, 6)

    last_close = float(bars[-1].get("close", 0) or 0)
    if last_close > 0:
        return round(profit(last_close) / 100.0, 6)
    return 0.0


def backfill_forward_returns(db, market_data, trade_date: str = None) -> int:
    """
    回填前瞻收益: 对历史指令计算 T+1/T+3/T+5 收益率

    Args:
        db: PlaybookDB 实例
        market_data: 市场数据提供者 (get_daily_bars)
        trade_date: 当前日期 (用于判断哪些历史指令需要回填)

    Returns:
        更新了多少条指令
    """
    if trade_date is None:
        trade_date = datetime.now(_ET).date().isoformat()

    today = date.fromisoformat(trade_date)
    updated = 0

    # 2026-08-27 ret=-1 根因修: 旧循环逐"指令"拉日线 (427 指令 ≈ 50 唯一 symbol, 8倍重复敲门),
    # 24h 缓存过期日 miss 叠打撞 OpenD 60次/30s 频控, 且失败结果不缓存 → 同 symbol 每条指令重试一次.
    # 现在每个 symbol 每次运行只拉一遍 (失败/异常也记忆, 同跑内不重试; bars 下游只读, 共享安全).
    _bars_memo = {}

    # 查最近 30 天的指令 (T+5 最多需要 5 个交易日 ≈ 7 个自然日，
    # 但历史数据可能因 10 天窗口被永久截断，扩到 30 天以回填积压数据)
    lookback = today - timedelta(days=30)
    try:
        # ponytail: 同 compute_playbook_ic, 接受 raw connection
        if not hasattr(db, 'get_recent_instructions'):
            rows = db.execute("""
                SELECT * FROM playbook_instructions
                WHERE date >= date('now', '-45 days') AND direction != 'SKIP'
                ORDER BY date DESC LIMIT 1000
            """).fetchall()
            instructions = [dict(r) if hasattr(r, 'keys') else r for r in rows]
        else:
            instructions = db.get_recent_instructions(limit=1000)
    except Exception as e:
        logger.error(f"Failed to get instructions: {e}")
        return 0

    for inst in instructions:
        inst_date_str = inst.get("date")
        if not inst_date_str:
            continue
        try:
            inst_date = date.fromisoformat(inst_date_str)
        except (ValueError, TypeError):
            continue

        # 只处理 lookback 范围内的
        # 2026-08-02 (R3-3): >=today 改 >today — 允许当日指令当日回填.
        #   旧守卫把周五指令推到下周二 (Sat 05:30 CST = 周五 17:30 ET 运行时 today=周五,
        #   inst_date>=today 跳过) → 周日周报系统性缺周五样本 (每周丢 20%).
        #   安全性: 每个写字段都有 bar 存在闸 (intraday 需当日日线 bar=收盘后才出现;
        #   t+N 需未来交易日 bar), 盘中跑也写不出半成品; update_instruction_tracking
        #   是全值覆盖, 偶发半成品下一跑自愈.
        if inst_date < lookback or inst_date > today:
            continue

        direction = inst.get("direction", "")
        # Phase 4 (2026-06-24): 优先用真实成交价 execution_price (futu_simulate/live 真实成交),
        # fallback entry_price (虚拟 shadow / 未执行). OKR 量真实成交更准.
        entry_price = inst.get("execution_price") or inst.get("entry_price", 0) or 0
        symbol = inst.get("symbol", "")

        if direction == "SKIP" or not symbol:
            continue

        # 获取日线数据 (按 symbol 去重, 见上方 _bars_memo 注释)
        if symbol in _bars_memo:
            if not _bars_memo[symbol]:
                continue
            bars = _bars_memo[symbol]
        else:
            try:
                bars = market_data.get_daily_bars(symbol, count=30)
            except Exception as e:
                logger.info(f"[PlaybookIC] Failed to get bars for {symbol}: {e}")
                bars = None
            _bars_memo[symbol] = bars
            if not bars:
                logger.info(f"[PlaybookIC] No bars returned for {symbol} (get_daily_bars failed/empty)")
                continue

        # 按日期排序
        bars_by_date = {}
        for bar in bars:
            bar_date = bar.get("time", "")
            if bar_date:
                bars_by_date[bar_date] = bar

        # 2026-07-19 (DEB-round3 #11): 未触发指令 entry_price 缺失 → 当日 open 代理回填.
        #   背景: 154 条未触发指令缺 entry_price (2026-06-14~07-08, 145 条在 30 天窗口),
        #   旧逻辑直接 skip → IC 漏掉"未触发但方向可验证"样本, 覆盖率虚低.
        #   口径: 仅 triggered=0 且 entry_price≤0 才代理 — 已触发缺价属数据异常, 不伪造.
        #   fwd_proxy=1 落库标记 (db 迁移列), IC 统计可区分/剔除代理样本.
        _proxy = False
        _open0 = float((bars_by_date.get(inst_date_str) or {}).get("open", 0) or 0)
        if (not entry_price or entry_price <= 0) and not inst.get("triggered", 0):
            if _open0 > 0:
                entry_price = _open0
                _proxy = True

        if not entry_price or entry_price <= 0:
            continue

        updates = {}
        if _proxy:
            updates["fwd_proxy"] = 1
        sign = 1 if direction == "BUY" else -1

        for horizon, field_name in [(1, "fwd_return_t1"), (3, "fwd_return_t3"), (5, "fwd_return_t5")]:
            # 找 T+horizon 的收盘价 (使用交易日历, 跨周末/假日自动跳过)
            target_date = nth_trading_day_after("US", inst_date, horizon)
            if target_date is None:
                continue
            check_str = target_date.isoformat()
            if check_str in bars_by_date:
                close = bars_by_date[check_str].get("close", 0)
                if close > 0:
                    fwd_return = sign * (close - entry_price) / entry_price
                    updates[field_name] = round(fwd_return, 6)
                    # 2026-08-01: open 基准孪生列 (双价格基准口径) — 同一 close, 起点换当日 open.
                    #   未执行指令 limit 价不可成交, entry 基准会制造"纸面 edge"幻觉 (n=100 实测
                    #   +1.65%→open 基准 +0.04%). t5 暂无消费端, 不落 open 孪生.
                    if _open0 > 0 and field_name in ("fwd_return_t1", "fwd_return_t3"):
                        updates[field_name + "_open"] = round(sign * (close - _open0) / _open0, 6)

        # 2026-08-22 (用户裁"落 T+N 影子字段"): 机制调整前瞻收益影子口径 —
        #   与上方 T+N 纯收盘价口径并行, 模拟"若按当前锁利/净保本/SL机制逐日盯市"
        #   在 T+N 收盘前实际能拿到的收益. 影子(record-only): 不动 fwd_return_t{N} 主口径,
        #   方向 IC 仍用主口径; 收益类报告/锁利参数优化裁决用 _adj 口径.
        #   参数读 config 实值 (与 _compute_dynamic_effective_sl 同源), 日K OHLC 回放.
        try:
            from app.playbook.config import get_playbook_config as _gpc_adj
            from app.playbook.config import BE_FALLBACK_FEE_CUSHION_PCT as _BE_FEE_FB
            _pb_adj = _gpc_adj()
            _exit_cfg = {
                "breakeven_at_pct": float(_pb_adj.get("breakeven_at_pct", 0.4)),
                "drawdown_from_peak_pct": float(_pb_adj.get("drawdown_from_peak_pct", 0.8)),
                "trailing_enabled": bool(_pb_adj.get("trailing_enabled", True)),
                "trailing_step_pct": float(_pb_adj.get("trailing_step_pct", 2.0)),
                "breakeven_fee_cushion_pct": float(_pb_adj.get("breakeven_fee_cushion_pct", _BE_FEE_FB)),
                "breakeven_slip_cushion_pct": float(_pb_adj.get("breakeven_slip_cushion_pct", 0.2)),
                "profit_override_threshold_pct": float(_pb_adj.get("profit_override_threshold_pct", 1.5)),
                "profit_override_drawdown_pct": float(_pb_adj.get("profit_override_drawdown_pct", 0.5)),
                "initial_sl_pct": float(_pb_adj.get("initial_sl_pct", 5.0)),
            }
            for _hz, _fld in [(1, "fwd_return_t1_adj"), (3, "fwd_return_t3_adj"), (5, "fwd_return_t5_adj")]:
                _td = nth_trading_day_after("US", inst_date, _hz)
                if _td is None:
                    continue
                _cs = _td.isoformat()
                if _cs not in bars_by_date:
                    continue
                # 收集 entry 当日到 T+_hz 的日K序列 (含 entry 日)
                _seq = []
                for _d in sorted(bars_by_date.keys()):
                    if inst_date_str <= _d <= _cs:
                        _seq.append(bars_by_date[_d])
                if not _seq:
                    continue
                _adj = _sim_mechanism_adjusted_return(direction, entry_price, _seq, _exit_cfg)
                updates[_fld] = _adj
        except Exception as _e_adj:
            logger.debug(f"[PlaybookIC] 机制调整影子回填失败 (不影响主口径): {_e_adj}")

        # 2026-07-12: 日内 IC — entry → 当日收盘 (纯日内系统真正收割的 horizon).
        #   与 t+N 同口径 (用收盘价), 但 horizon=当日. DA horizon 几乎恒 short (当天平),
        #   t+3/t+5 是系统收割不到的多日 edge → 用日内收益评信号方向预测力才不 horizon 错配.
        #   保留 t+1/t+3/t+5 供对比: 若某标的多日 IC 显著 > 日内 IC, 是"该不该转非日内持有"的证据.
        if inst_date_str in bars_by_date:
            _close0 = bars_by_date[inst_date_str].get("close", 0)
            if _close0 and _close0 > 0:
                updates["fwd_return_intraday"] = round(sign * (_close0 - entry_price) / entry_price, 6)
                if _open0 > 0:
                    updates["fwd_return_intraday_open"] = round(sign * (_close0 - _open0) / _open0, 6)

        if updates:
            try:
                db.update_instruction_tracking(inst["id"], updates)
                updated += 1
            except Exception as e:
                logger.debug(f"Failed to update IC for {symbol}: {e}")

    logger.info(f"[PlaybookIC] Backfilled forward returns for {updated} instructions")
    return updated


def compute_playbook_ic(db, days: int = 30) -> Dict[str, Any]:
    """
    计算 Playbook 整体 IC 和按因子分解的 IC

    Args:
        db: PlaybookDB 实例
        days: 回溯天数

    Returns:
        {
            "overall_ic": float,
            "overall_ic_t3": float,
            "overall_ic_t5": float,
            "sample_count": int,
            "by_factor": {factor_name: {"ic": float, "count": int}},
            "by_regime": {regime: {"ic": float, "count": int}},
        }
    """
    try:
        # ponytail: 用 raw SQL 替代 db.get_recent_instructions(), 接受 sqlite3.Connection 或 PlaybookDB
        if hasattr(db, 'execute'):
            import sqlite3 as _sqlite
            # 如果是 raw connection (没有 get_recent_instructions 方法)
            if not hasattr(db, 'get_recent_instructions'):
                rows = db.execute("""
                    SELECT * FROM playbook_instructions
                    WHERE date >= date('now', ?) AND direction != 'SKIP'
                    ORDER BY date DESC LIMIT ?
                """, (f'-{days} days', days * 10)).fetchall()
                instructions = [dict(r) if hasattr(r, 'keys') else r for r in rows]
            else:
                instructions = db.get_recent_instructions(limit=days * 10)
        else:
            instructions = db.get_recent_instructions(limit=days * 10)
    except Exception as e:
        logger.error(f"Failed to get instructions for IC: {e}")
        return {"overall_ic": None, "sample_count": 0}

    # 过滤有前瞻收益的指令
    # 元组末位 = date (指令日期, ET) —— 2026-08-24 KR1 修尺子: 按日聚类需要
    valid_intraday = []  # (confidence, fwd_return, conditions, regime, horizon, date) — 日内 horizon (主 edge 尺子)
    valid_t1 = []  # (confidence, fwd_return, conditions, regime, horizon, date)
    valid_t3 = []
    valid_t5 = []

    for inst in instructions:
        conf = inst.get("confidence", 0)
        if not conf or conf <= 0:
            continue

        fwd_intraday = inst.get("fwd_return_intraday")
        fwd_t1 = inst.get("fwd_return_t1")
        fwd_t3 = inst.get("fwd_return_t3")
        fwd_t5 = inst.get("fwd_return_t5")
        _date = str(inst.get("date") or "")[:10]

        conditions = []
        try:
            cond_json = inst.get("conditions_json")
            if cond_json:
                import json
                conditions = json.loads(cond_json) if isinstance(cond_json, str) else cond_json
        except Exception as _ce:
            # R5 同款纪律 (2026-08-31 DEB-20260831 P1③): 测量链异常不得静默 —
            # 解析失败=该行退出 conditions 切片, 吞错会让 IC 尺子无声丢样本。
            logger.warning(f"[ic_tracker] conditions_json 解析失败, 该行退出条件切片: "
                           f"id={inst.get('id', '?')} err={_ce}")

        regime = inst.get("market_regime", "UNKNOWN")
        horizon = inst.get("horizon", "short") or "short"

        if fwd_intraday is not None:
            valid_intraday.append((conf, fwd_intraday, conditions, regime, horizon, _date))
        if fwd_t1 is not None:
            valid_t1.append((conf, fwd_t1, conditions, regime, horizon, _date))
        if fwd_t3 is not None:
            valid_t3.append((conf, fwd_t3, conditions, regime, horizon, _date))
        if fwd_t5 is not None:
            valid_t5.append((conf, fwd_t5, conditions, regime, horizon, _date))

    # 计算整体 IC
    overall_intraday, overall_intraday_p = _spearman_rank_correlation(
        [v[0] for v in valid_intraday], [v[1] for v in valid_intraday]
    ) if len(valid_intraday) >= 5 else (None, None)

    overall_t1, overall_t1_p = _spearman_rank_correlation(
        [v[0] for v in valid_t1], [v[1] for v in valid_t1]
    ) if len(valid_t1) >= 5 else (None, None)

    overall_t3, overall_t3_p = _spearman_rank_correlation(
        [v[0] for v in valid_t3], [v[1] for v in valid_t3]
    ) if len(valid_t3) >= 5 else (None, None)

    overall_t5, overall_t5_p = _spearman_rank_correlation(
        [v[0] for v in valid_t5], [v[1] for v in valid_t5]
    ) if len(valid_t5) >= 5 else (None, None)

    # 按因子分解 IC (T+3)
    by_factor = {}
    if valid_t3:
        factor_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, conditions, _, _, _ in valid_t3:
            for cond in conditions:
                factor_data[cond]["confs"].append(conf)
                factor_data[cond]["returns"].append(ret)

        for factor_name, data in factor_data.items():
            if len(data["confs"]) >= 5:
                ic, pvalue = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_factor[factor_name] = {
                        "ic": round(ic, 4),
                        "pvalue": round(pvalue, 4) if pvalue is not None else None,
                        "count": len(data["confs"]),
                    }

    # 按 regime 分解 IC (T+3)
    by_regime = {}
    if valid_t3:
        regime_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, _, regime, _, _ in valid_t3:
            regime_data[regime]["confs"].append(conf)
            regime_data[regime]["returns"].append(ret)

        for regime_name, data in regime_data.items():
            if len(data["confs"]) >= 5:
                ic, pvalue = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_regime[regime_name] = {
                        "ic": round(ic, 4),
                        "pvalue": round(pvalue, 4) if pvalue is not None else None,
                        "count": len(data["confs"]),
                    }

    # 按 source_type 分解 IC (T+3) — Phase 4 (2026-06-24): 区分 SHADOW/SIM/LIVE 数据可信度
    by_source_type = {}
    if valid_t3:
        src_data = defaultdict(lambda: {"confs": [], "returns": []})
        for inst in instructions:
            fwd_t3 = inst.get("fwd_return_t3")
            conf = inst.get("confidence", 0)
            if fwd_t3 is None or not conf or conf <= 0:
                continue
            # 推断 source_type: executed + execution_price>0 → SIM/LIVE (有真实成交), 否则 SHADOW
            if inst.get("executed") and inst.get("execution_price"):
                # 区分 sim/live 需读 trade_records; 此处简化为 SIM/LIVE 合并标 "EXECUTED"
                src = "EXECUTED"
            else:
                src = "SHADOW"
            src_data[src]["confs"].append(conf)
            src_data[src]["returns"].append(fwd_t3)
        for src, data in src_data.items():
            if len(data["confs"]) >= 5:
                ic, pvalue = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_source_type[src] = {
                        "ic": round(ic, 4),
                        "pvalue": round(pvalue, 4) if pvalue is not None else None,
                        "count": len(data["confs"]),
                    }

    # 按方向分解 IC (T+3) —  2026-06-28
    # 根因: BUY PF=0.12 vs SHORT PF=4.16 差距极端, 必须分方向看 IC.
    by_direction = {}
    if valid_t3:
        direction_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, _, _, _, _ in valid_t3:
            # direction 字段从 instruction 中读取 (BUY/SELL_SHORT)
            # 这里 conf 是元组第0个, 没有 direction; 需要从 instructions 中关联
            pass
        # 重新遍历 instructions 以按方向分组
        for inst in instructions:
            _dir = inst.get("direction", "")
            _conf = inst.get("confidence", 0)
            _fwd = inst.get("fwd_return_t3")
            if _dir in ("BUY", "SELL_SHORT") and _conf and _fwd is not None:
                direction_data[_dir]["confs"].append(_conf)
                direction_data[_dir]["returns"].append(_fwd)
        for dir_name, data in direction_data.items():
            if len(data["confs"]) >= 5:
                ic, pvalue = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_direction[dir_name] = {
                        "ic": round(ic, 4),
                        "pvalue": round(pvalue, 4) if pvalue is not None else None,
                        "count": len(data["confs"]),
                    }
    by_horizon = {}
    if valid_t3:
        horizon_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, _, _, horizon, _ in valid_t3:
            horizon_data[horizon]["confs"].append(conf)
            horizon_data[horizon]["returns"].append(ret)

        for horizon_name, data in horizon_data.items():
            if len(data["confs"]) >= 5:
                ic, pvalue = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_horizon[horizon_name] = {
                        "ic": round(ic, 4),
                        "pvalue": round(pvalue, 4) if pvalue is not None else None,
                        "count": len(data["confs"]),
                    }

    result = {
        "overall_ic_intraday": round(overall_intraday, 4) if overall_intraday is not None else None,
        "overall_ic_intraday_p": round(overall_intraday_p, 4) if overall_intraday_p is not None else None,
        "overall_ic_t1": round(overall_t1, 4) if overall_t1 is not None else None,
        "overall_ic_t1_p": round(overall_t1_p, 4) if overall_t1_p is not None else None,
        "overall_ic_t3": round(overall_t3, 4) if overall_t3 is not None else None,
        "overall_ic_t3_p": round(overall_t3_p, 4) if overall_t3_p is not None else None,
        "overall_ic_t5": round(overall_t5, 4) if overall_t5 is not None else None,
        "overall_ic_t5_p": round(overall_t5_p, 4) if overall_t5_p is not None else None,
        "sample_intraday": len(valid_intraday),
        "sample_t1": len(valid_t1),
        "sample_t3": len(valid_t3),
        "sample_t5": len(valid_t5),
        # 按日聚类成对输出 (2026-08-24, OKR-2026-09 KR1): pooled 值与日聚类值必须成对呈现,
        # 无日聚类+MDE 的 IC 结论不予采信 (宪章第 9 律). 采信判据见 _day_clustered_ic().
        "day_clustered": {
            "intraday": _day_clustered_ic(valid_intraday),
            "t1": _day_clustered_ic(valid_t1),
            "t3": _day_clustered_ic(valid_t3),
            "t5": _day_clustered_ic(valid_t5),
        },
        "by_factor": by_factor,
        "by_regime": by_regime,
        "by_source_type": by_source_type,
        "by_horizon": by_horizon,
        "by_direction": by_direction,
    }

    # 毕业门禁判定
    result["graduation_check"] = _check_graduation(result)

    return result


def _check_graduation(ic_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    毕业门禁检查

    ⚠️ DEPRECATED (2026-08-24, OKR-2026-09 KR1/KR4 裁决): 本判据
    (confidence-based IC > 0.05) 已停用为毕业依据 —— confidence 自 08-14 起
    恒=70 (辩论离散标签), 对常量做 Spearman 无意义, 现值纯属并列值处理
    artifact (DEB-20260823 §KR1-d). 门禁本体保持现状不动 (fail-close=安全),
    新毕业判据按日聚类尺子预注册 + 用户批准后才替换. 在此之前任何
    "graduated" 输出不得作为切实盘依据.

    条件 (全部满足):
    - Playbook IC (T+3) > 0.05
    - 样本量 ≥ 30
    - 最大回撤 < 5% (由 RiskCircuitBreaker 提供)

    2026-06-26: 优先使用 long-horizon 的 IC(T+3), 因为目标是吃到周级 alpha.
    """
    # 优先 long-horizon IC
    by_horizon = ic_report.get("by_horizon", {})
    long_ic = by_horizon.get("long", {}).get("ic") if by_horizon else None
    long_sample = by_horizon.get("long", {}).get("count", 0) if by_horizon else 0

    if long_ic is not None and long_sample >= 30:
        ic_t3 = long_ic
        sample = long_sample
        horizon_used = "long"
    else:
        ic_t3 = ic_report.get("overall_ic_t3")
        sample = ic_report.get("sample_t3", 0)
        horizon_used = "overall"

    checks = {
        "ic_positive": ic_t3 is not None and ic_t3 > 0.05,
        "sample_sufficient": sample >= 30,
    }

    all_pass = all(checks.values())

    return {
        "graduated": all_pass,
        "checks": checks,
        "ic_t3": ic_t3,
        "sample_count": sample,
        "horizon_used": horizon_used,
        "verdict": "READY" if all_pass else "ACCUMULATING",
    }


def format_ic_report(report: Dict[str, Any]) -> str:
    """格式化 IC 报告为飞书消息"""
    lines = ["📊 **Playbook IC 追踪报告**", ""]

    # 整体 IC
    ic_t1 = report.get("overall_ic_t1")
    ic_t3 = report.get("overall_ic_t3")
    ic_t5 = report.get("overall_ic_t5")

    def _ic_emoji(ic):
        if ic is None:
            return "⏳ 数据不足"
        if ic > 0.05:
            return f"✅ {ic:+.4f}"
        if ic > -0.05:
            return f"🟡 {ic:+.4f}"
        return f"🔴 {ic:+.4f}"

    # 按日聚类成对呈现 (2026-08-24, KR1 修尺子): 只报 pooled 值 = 无 MDE 不予采信
    _dc = report.get("day_clustered", {})

    def _dc_str(key):
        d = _dc.get(key) or {}
        if d.get("day_ic_mean") is None:
            return f"日聚类: ⏳ 不足({d.get('n_days', 0)}日)"
        flag = "✅可分辨" if d.get("distinguishable") else "❌不可分辨"
        return (f"日聚类: {d['day_ic_mean']:+.4f} (t={d['t']}, MDE={d['mde']}, "
                f"n={d['n_days']}日) {flag}")

    lines.append(f"T+1 IC: {_ic_emoji(ic_t1)} (n={report.get('sample_t1', 0)})")
    lines.append(f"  └ {_dc_str('t1')}")
    lines.append(f"T+3 IC: {_ic_emoji(ic_t3)} (n={report.get('sample_t3', 0)})")
    lines.append(f"  └ {_dc_str('t3')}")
    lines.append(f"T+5 IC: {_ic_emoji(ic_t5)} (n={report.get('sample_t5', 0)})")
    lines.append(f"  └ {_dc_str('t5')}")
    lines.append("")
    lines.append("📏 口径: pooled 与日聚类成对呈现; 无日聚类+MDE 的结论不予采信 (宪章第 9 律)")
    lines.append("")

    # 按因子分解
    by_factor = report.get("by_factor", {})
    if by_factor:
        lines.append("**按因子 IC (T+3):**")
        sorted_factors = sorted(by_factor.items(), key=lambda x: x[1]["ic"], reverse=True)
        for name, data in sorted_factors[:10]:
            ic = data["ic"]
            emoji = "✅" if ic > 0.05 else "🟡" if ic > -0.05 else "🔴"
            lines.append(f"  {emoji} {name}: IC={ic:+.4f} (n={data['count']})")
        lines.append("")

    # 按 horizon 分解
    by_horizon = report.get("by_horizon", {})
    if by_horizon:
        lines.append("**按 Horizon IC (T+3):**")
        sorted_horizons = sorted(by_horizon.items(), key=lambda x: x[1]["ic"], reverse=True)
        for name, data in sorted_horizons:
            ic = data["ic"]
            emoji = "✅" if ic > 0.05 else "🟡" if ic > -0.05 else "🔴"
            lines.append(f"  {emoji} {name}: IC={ic:+.4f} (n={data['count']})")
        lines.append("")

    # 毕业门禁
    grad = report.get("graduation_check", {})
    verdict = grad.get("verdict", "UNKNOWN")
    lines.append("⚠️ 毕业门禁判据已停用 (DEPRECATED, 2026-08-24): confidence-based IC 测常量无意义,")
    lines.append("   新判据待按日聚类尺子预注册+用户批准; 在此之前不产生切实盘效力 (fail-close)")
    if verdict == "READY":
        lines.append(f"🎓 毕业门禁(旧判据): 通过 — ⚠️ 该结果不再作为切实盘依据")
    else:
        lines.append(f"⏳ 毕业门禁(旧判据): 积累中 (verdict={verdict})")
        checks = grad.get("checks", {})
        for check_name, passed in checks.items():
            status = "✅" if passed else "❌"
            lines.append(f"  {status} {check_name}")

    return "\n".join(lines)
