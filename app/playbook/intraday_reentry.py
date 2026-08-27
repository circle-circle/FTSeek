"""盘中再入场机会生成器 (2026-08-06, 用户指示: sim 环境直上线, 快速验真).

立项背景: 指令供给全部盘前生成 (DA bridge / UE scan / generate), 盘中唯一供给源
topup 已于 2026-08-06 终裁停供 → 盘中事实上零新增入场机会。但盘中指标链
(VWAP/slope_5m/量比/L1 spread) 全部已在实时计算, 且当日平仓 (非硬止损) 的标的
thesis 未被证伪 — 回踩 VWAP 企稳是合法的二次入场点。

机制: TRADING 相位每 300s 扫一次"当日已平仓且前笔非硬亏"的 sim 标的, 指标确认后
**重武装 (re-arm) 该标的当日指令行** (save_instruction UPSERT 刷新锚点/SL/TP +
显式 executed 复位) → 走完整既有触发门链 (13 门 + admission 入口/执行双复核 +
熔断/PDT/报价健康), 不绕过任何风控, 不新增订阅 (ADR-013 约束不变)。

安全边界:
  - 仅 execute_mode=futu_simulate 武装真指令; 其他模式自动降级 record-only;
  - 每标的每日最多 1 次再入场 (由"当日已平仓笔数==1"自然约束, 零额外状态);
  - 每日武装上限 max_arms_per_day; 10:00-13:55 ET 窗 (避开盘初噪声 + 14:00 触发cutoff);
  - 全程落 data/intraday_reentry_shadow.jsonl (含未武装候选+拒因), 供 T+1 对照评估.

回滚: playbook.intraday_reentry.enabled=false + 重启。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/intraday_reentry_shadow.jsonl")
_last_run_ts = 0.0

# ── 2026-08-22b 复查修复: 证伪门数据源进程级单例 ──
# 原实现在 run_scan 内 (或候选循环内) 新建实例: FutuExtendedProvider 每 scan 一条
# OpenD 连接只建不关 (慢泄漏); EarningsCalendar/FinnhubProvider 每候选新建使
# 实例级缓存 (日历 1h / 情绪 1h) 形同虚设 → 每候选一次全量拉取。
# 惰性单例 + False 哨兵: 初始化失败不逐 scan 重试。
_futu_ext_singleton = None
_earnings_cal_singleton = None
_finnhub_singleton = None


def _futu_ext():
    global _futu_ext_singleton
    if _futu_ext_singleton is None:
        try:
            from app.data.providers.futu_extended_provider import FutuExtendedProvider
            _futu_ext_singleton = FutuExtendedProvider()
        except Exception:
            _futu_ext_singleton = False
    return _futu_ext_singleton or None


def _earnings_cal():
    global _earnings_cal_singleton
    if _earnings_cal_singleton is None:
        try:
            from app.data.earnings_calendar import EarningsCalendar
            _earnings_cal_singleton = EarningsCalendar()
        except Exception:
            _earnings_cal_singleton = False
    return _earnings_cal_singleton or None


def _finnhub():
    global _finnhub_singleton
    if _finnhub_singleton is None:
        try:
            from app.data.finnhub_provider import FinnhubProvider
            _finnhub_singleton = FinnhubProvider()
        except Exception:
            _finnhub_singleton = False
    return _finnhub_singleton or None

# 前笔退出判据 (2026-08-07 精化, 替代纯 pnl 代理):
#   硬止损/0-WR 类 = thesis 被证伪 → 不再入 (防报复性交易);
#   良性类 (保本/锁利/止盈/移动止损/日终) = thesis 未证伪 → 可再入;
#   未知 reason → 回退 pnl 代理 (max_prior_loss_pct 阈值)。
#   分类数据依据 (2026-08-07 全量 trade_records, pnl_net 口径):
#     MFE_STALL_EXIT 28笔 0%WR -$1169 / TIME_EXIT 33笔 0%WR -$1151 → 0胜率纯亏损,
#     停滞=方向从未兑现, 再入=给失败 thesis 第二次机会 → 禁入;
#     SL_TRAIL_LOCK 46笔 91.3%WR +$1015 / SL_BREAKEVEN 43笔 41.9%WR -$65 (中性) →
#     保本/锁盈=thesis 未证伪 → 可入;
#     PRE_CLOSE_EXIT 36笔 61.1%WR +$568 / EOD_FORCE_CLOSE 7笔 71.4%WR +$332 →
#     日终全平偏健康 → 可入 (半日市防御; 常规日这些平仓发生在窗口后, 不构成候选);
#     操作类 (GHOST/EXIT_MONITOR/OKR_AUDIT) = 非 thesis 信号 → 禁入 (fail-closed)。
_HARD_STOP_REASONS = frozenset({
    "SL_INITIAL", "SL_ATR_STOP", "SL_FIXED_PCT", "OVERNIGHT_GAP_STOP",
    "DEAD_SIGNAL_EXIT", "MFE_STALL_EXIT", "TIME_EXIT",   # 0% WR 纯亏损 (n=61, -$2320)
    # 2026-08-13 (DEB-20260813): 耐心出场三腿 = DEAD_SIGNAL_EXIT 的延迟执行变体,
    #   thesis 同样未兑现 → 禁入 (fill 保本退出若漏归类会回退 pnl 代理误放行).
    "PATIENT_EXIT_FILL", "PATIENT_EXIT_STOP", "PATIENT_EXIT_EOD",
    "BROKER_GHOST_CLOSE", "EXIT_MONITOR_CLOSE", "FORCED_CLOSE_OKR_AUDIT",  # 操作类
})
_BENIGN_REASONS = frozenset({
    "SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT", "TRAILING_STOP",
    "TRAILING_STOP_BY_MAIN", "PRE_CLOSE_EXIT", "EOD_FORCE_CLOSE",
})


def prior_exit_reenterable(exit_reason: Optional[str], pnl_pct: float,
                           max_prior_loss_pct: float) -> bool:
    """纯函数: 前笔退出是否允许再入场 (可单测, 零 IO).

    pnl_pct 为小数口径 (0.0033=0.33%); max_prior_loss_pct 为百分数 (1.0=1%).
    """
    r = str(exit_reason or "").upper()
    if r in _HARD_STOP_REASONS:
        return False
    if r in _BENIGN_REASONS:
        return True
    try:
        return float(pnl_pct) > -float(max_prior_loss_pct) / 100.0
    except Exception:
        return False


def quick_sl_tp_pct(atr_pct: Optional[float], cfg: Optional[Dict[str, Any]] = None):
    """纯函数: ATR 相对快收 SL/TP 百分数 (2026-08-07 用户裁决: SL放宽/TP降档/按标的).

    SL = clamp(quick_sl_atr_mult×atr_pct, floor, ceil) — 放宽给跨夜空间;
    TP = clamp(quick_tp_atr_mult×atr_pct, floor, ceil) — 降档快速落袋.
    atr_pct 为百分数口径 (5.0=5%); 缺失/<=0 → (None, None), 调用方回退 sl_pct/tp_rr.
    """
    cfg = cfg or _cfg()
    try:
        atr = float(atr_pct)
    except Exception:
        return None, None
    if atr <= 0:
        return None, None
    sl = min(max(float(cfg["quick_sl_atr_mult"]) * atr,
                 float(cfg["quick_sl_floor_pct"])), float(cfg["quick_sl_ceil_pct"]))
    tp = min(max(float(cfg["quick_tp_atr_mult"]) * atr,
                 float(cfg["quick_tp_floor_pct"])), float(cfg["quick_tp_ceil_pct"]))
    return round(sl, 2), round(tp, 2)


def cumulative_exposure_ok(prior_notional: float, max_pos_pct: float,
                           equity: float, mult: float) -> bool:
    """纯函数: 同标的当日累计暴露护栏 (可单测, 零 IO).

    再入场估算名义按单笔上限满配 (max_pos_pct×equity, 保守 — 实际 sizing 受
    regime/水位乘数只会更小, 估算不超则实际必不超; 估算超则宁拦勿放)。
    条件: prior_notional + max_pos_pct×equity ≤ mult×max_pos_pct×equity
        ⇔ prior_notional ≤ (mult−1)×max_pos_pct×equity。
    equity/max_pos_pct 异常 → True (fail-open: 护栏是增量保护, 不阻断再入场)。
    """
    if equity <= 0 or max_pos_pct <= 0:
        return True
    return prior_notional <= (float(mult) - 1.0) * max_pos_pct * equity


def _cumulative_exposure_ok(prior: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """包装: 从 capital_safety 取实时净值 + playbook config 取单笔上限, 调纯函数."""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        _equity = float(getattr(_cs._state, "current_equity", 0) or 0)
        if _equity <= 0:
            return True
        from app.playbook.config import get_playbook_config
        _max_pos_pct = float(get_playbook_config().get("max_position_pct", 0.06) or 0.06)
        _prior_notional = float(prior.get("entry_price") or 0) * float(prior.get("quantity") or 0)
        return cumulative_exposure_ok(
            _prior_notional, _max_pos_pct, _equity,
            float(cfg.get("max_cumulative_pos_mult", 1.5) or 1.5),
        )
    except Exception:
        return True

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "mode": "live",               # live=重武装走全门链; shadow=仅记录 (非 sim 环境强制 shadow)
    "window_start_hhmm": 1045,    # 2026-08-08: 禁触窗后首笔平仓≥10:30, 早于 10:45 候选池恒空
    "window_end_hhmm": 1355,      # 13:55 ET 止 (14:00 触发 cutoff 前)
    "scan_interval_sec": 300,
    "max_arms_per_day": 3,
    "max_prior_loss_pct": 1.0,    # 前笔亏损 >1% 视为硬止损/论破位 → 不再入场 (防报复性交易)
    "direction_adaptive_enabled": True,   # 2026-08-12 用户裁决直接落地: 方向由 slope_5m 顺势裁决, 缺失/0 回退继承
    "max_cumulative_pos_mult": 1.5,  # 同标的当日累计名义 ≤ 1.5×单笔上限 (防再入场=加仓的标的集中暴露)
    "vwap_band_low_pct": 0.3,     # LONG: 允许跌破 VWAP 至多 0.3% (回踩区下沿)
    "vwap_band_high_pct": 1.0,    # LONG: price ≤ vwap×(1+1.0%) (不追高位); SHORT 对称
    "min_slope_5m_pct": 0.0,      # LONG 要求 >0 / SHORT 要求 <0 (方向转正)
    "min_vol_ratio": 0.8,         # 盘中实时量能比 (与 chase 门同档)
    "max_spread_bps": 25.0,       # L1 盘口 spread 上限
    "min_atr_pct": 2.0,           # 日 ATR% 下限 (低波不做动量, 比 low_atr_gate 3% 宽一档)
    "sl_pct": 1.5,                # 再入场止损回退值 (ATR 缺失时; 主口径见 quick_* 快收组)
    "tp_rr": 1.6,                 # TP = SL×1.6 (回退口径, 过 min_tp_rr=1.5 门)
    # 2026-08-07 (用户裁决: 再入场可跨夜 → SL 放宽给空间 / TP 降档快落袋, 退出栈不变):
    #   ATR 相对化 = 按标的自适应 (高波 AVGO 与低波 AAPL 不同档); 触发 re-price 同式复算.
    #   SL = clamp(0.6×ATR%, 2.0, 3.5)  TP = clamp(0.35×ATR%, 1.0, 2.0)
    #   示例: AVGO(ATR5%)→SL3.0/TP1.75  AAPL(ATR1.8%)→SL2.0/TP1.0  TSM(ATR3%)→SL2.0/TP1.05
    #   代价明示: RR≈0.5-0.6 倒置, 需净胜率 >~62% 才正期望 — 由 eval_intraday_reentry 验证.
    "quick_sl_atr_mult": 0.6,
    "quick_sl_floor_pct": 2.0,    # 对齐 vol_bucket 低波噪声地板, 不入 <2% 扫损区
    "quick_sl_ceil_pct": 3.5,
    "quick_tp_atr_mult": 0.35,
    "quick_tp_floor_pct": 1.0,
    "quick_tp_ceil_pct": 2.0,
    # 水下仓持夜豁免 (2026-08-06 用户裁决"再入场亏损可隔夜"):
    #   本开关管"首夜豁免" (entry date == 当日 ET), 仅水下仓 (盈利仍当日落袋,
    #   与 DEB-20260806-asymmetric ① 一致), 仅本生成器武装的仓 (decision_id 前缀判定).
    #   ⚠️ 2026-08-27 勘误 (RFC-067): "次日强平"语义已不存在 — 首夜豁免后次日起
    #   由 W_WEEK (_loser_overnight_exempt, 不查 decision_id) 接管, 再入场仓与
    #   DA 仓同权享受最多 5 交易日修复窗口 (时间mock实弹验证).
    #   注意: overnight_hold_enabled 总开关下 OVERNIGHT_GAP_STOP 不生效, 持夜风险由
    #   原 SL 1.5% 兜底 (开盘跳空越 SL 有次日市价滑点风险, sim 期可接受, 影子记录对照).
    "overnight_hold_loser_enabled": False,
    # 2026-08-22 (用户裁决): 资金流背离检查 — DA方向与实时大单资金流背离时拒绝重武装.
    #   数据源: futu_capital_flow.jsonl 盘中300s节流采集, 读最近一条该标的大单净流入方向.
    #   证伪机制非预测机制: 资金流不告诉你该做什么, 但告诉你不该做什么(方向背离时别重入场).
    "capital_flow_divergence_check_enabled": True,
    # 2026-08-22 (用户裁决): 卖空压力检查 — SHORT 方向时 short_percent 飙升(>30%)=拥挤交易, squeeze 风险高.
    #   证伪机制: 卖空量激增不预测方向, 但告诉你 SHORT 可能被挤压.
    "short_squeeze_check_enabled": True,
    # 2026-08-22 (用户裁决): 内部人背离检查 — DA方向与内部人净买卖背离时拒绝.
    #   2026-08-22c 收窄: 只留 buy 侧 (异常买入拦 SHORT); sell 侧例行减持是噪音不拦 (见 evaluate_candidate).
    "insider_divergence_check_enabled": True,
    # 2026-08-22 (用户裁决): 分析师背离检查 — DA方向与分析师评级趋势背离时拒绝.
    #   证伪机制: 分析师评级下调不预测方向, 但告诉你基本面支撑减弱.
    "analyst_divergence_check_enabled": True,
    # 2026-08-22 (用户裁决): 财报临近检查 — 持仓标的财报临近(<3天)时收紧, 波动加剧.
    #   证伪机制: 财报临近不预测方向, 但告诉你波动风险上升.
    "earnings_proximity_check_enabled": True,
    # 2026-08-22 (用户裁决): 情绪极端检查 — DA方向与社交情绪极端背离时拒绝.
    #   证伪机制: 情绪极端不预测方向, 但告诉你可能是拥挤交易.
    "sentiment_extreme_check_enabled": True,
    # 2026-08-22 (用户裁决): 重大事件背离检查 — 8-K/13D-G 披露重大负面时拒绝.
    #   证伪机制: 重大事件不预测方向, 但告诉你基本面可能已变.
    "sec_event_divergence_check_enabled": True,
    # 2026-08-22 (用户裁决): 多源情绪验证 — social_sentiment 与 finnhub 情绪分数背离时拒绝.
    #   证伪机制: 多源情绪背离不预测方向, 但告诉你情绪数据可能不可靠.
    #   2026-08-22 接线修复 (ADR-032): 默认改 False — 实测 social_sentiment.collect_sentiment
    #   返回结构 {symbol, reddit[], hackernews[], polymarket[], summary{计数}, fetched_at}
    #   全程无极性分数 (只有点赞数与标题文本), 本门需要的带符号 score 无从取得;
    #   与其留着恒不触发的死门伪装保护, 不如诚实关断, 待接入有符号分数的源再启用.
    "social_sentiment_divergence_check_enabled": False,
    # 2026-08-22 (用户裁决): 盘中逐笔情绪背离检查 — DA方向与逐笔成交情绪背离时拒绝.
    #   证伪机制: 逐笔情绪不预测方向, 但告诉你盘中实时情绪已变.
    "ticker_sentiment_divergence_check_enabled": True,
    # 2026-08-22 (用户裁决): 阈值配置 — 证伪门阈值统一放 config, 不硬编码.
    "short_squeeze_threshold_pct": 30.0,  # SHORT 拥挤阈值
    "sentiment_extreme_threshold": 0.8,  # 情绪极端阈值
    "social_sentiment_divergence_threshold": 0.3,  # 多源情绪背离阈值
    "earnings_proximity_days": 3,  # 财报临近阈值
    # 2026-08-27 (8/26 LLY 复盘, 用户批准落地): 当日反手硬闸 — 再入场方向与当日
    #   第一轮 DA/UE 指令相反 → 拒绝武装 (同向多轮如 AMD/RIVN 不受影响)。
    #   回滚: unified_config.yaml playbook.intraday_reentry.same_day_reversal_block_enabled=false
    "same_day_reversal_block_enabled": True,
}


def _cfg() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    try:
        from app.core.config_container import get_config
        override = get_config().get_dotted("playbook.intraday_reentry", None)
        if isinstance(override, dict):
            for k, v in override.items():
                if k in cfg and v is not None:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def _today_et_str() -> str:
    from app.utils.timezone import now_et
    return now_et().strftime("%Y-%m-%d")


def _now_iso_et() -> str:
    from app.utils.timezone import now_et
    return now_et().isoformat(timespec="seconds")


def _now_hhmm_et() -> int:
    from app.utils.timezone import now_et
    n = now_et()
    return n.hour * 100 + n.minute


# ── 2026-08-22 证伪门接线修复 (ADR-032, 用户裁"请修复") ──────────────────────
# 修复前四个门按想象 schema 读 provider (buy_pct/sell_pct、dict键财报、social score、
# buy_strength/sell_strength 均不存在) → 恒 None → 永不触发。以下 helper 按真实
# 签名适配, 纯函数可单测; run_scan 接线只调 helper 不再自读键名。

def _derive_analyst_trend(rt: Optional[dict]) -> Optional[str]:
    """从 get_recommendation_trend 真实输出 {periods: [{strong_buy,buy,hold,sell,
    strong_sell}, ...]} (periods[0]=当月, [1]=上月) 推导修订动量方向。

    语义 = 净多头占比 (bull-bear)/total 的环比变化: ≥+5pp → upgrade, ≤−5pp →
    downgrade, 其余 (含样本<2期/无覆盖) → None。±5pp 死区防月度噪声误触。
    """
    periods = (rt or {}).get("periods") or []
    if len(periods) < 2:
        return None

    def _net(p: dict) -> Optional[float]:
        bull = (p.get("strong_buy") or 0) + (p.get("buy") or 0)
        bear = (p.get("sell") or 0) + (p.get("strong_sell") or 0)
        total = bull + bear + (p.get("hold") or 0)
        return (bull - bear) / total if total > 0 else None

    now, prev = _net(periods[0]), _net(periods[1])
    if now is None or prev is None:
        return None
    delta = now - prev
    if delta >= 0.05:
        return "upgrade"
    if delta <= -0.05:
        return "downgrade"
    return None


def _find_days_to_earnings(earnings: Optional[list], symbol: str) -> Optional[int]:
    """从 get_watchlist_earnings 真实输出 List[Dict] (行含 Futu 格式 symbol /
    days_until) 中找该标的的距财报天数。未覆盖/字段缺失 → None。"""
    for row in earnings or []:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            du = row.get("days_until")
            if du is None:
                return None
            try:
                return int(du)
            except (TypeError, ValueError):
                return None
    return None


def _derive_ticker_direction(ts: Optional[dict]) -> Optional[str]:
    """从 get_ticker_sentiment 真实输出 {sentiment: [-1,1], buy_ratio,
    volume_weighted, confidence} 推导盘中逐笔情绪方向。

    confidence<=0 (无成交数据时的 default 返回) → None; sentiment ±0.15 死区外
    才给方向, 防近零流量噪声。
    """
    if not ts or float(ts.get("confidence") or 0) <= 0:
        return None
    s = ts.get("sentiment")
    if s is None:
        return None
    s = float(s)
    if s >= 0.15:
        return "bullish"
    if s <= -0.15:
        return "bearish"
    return None


def _day_first_direction(date_str: str, symbol: str) -> Optional[str]:
    """当日第一轮 DA/UE 指令方向 (最小 id 行); None = 无反手判定依据, 不拦。

    2026-08-27 (8/26 LLY 反手复盘): 盘前 DA BUY #1714 止盈后, reentry 于 #1776
    反手 SELL_SHORT — 仅被资金护栏拦在下单前, shadow_pnl=-73.06 证明反手方向本身
    错误。当日第一轮论据未失效时, 反手再入场 = 对当日论据的自我否定, 拒之。
    仅认 DA/UE 轮 (当日首行本就是再入场的历史多轮场景不作依据); SKIP 行不构成
    方向依据 (查询已限定 BUY/SELL_SHORT)。fail-open: 查询失败返回 None。
    """
    try:
        from app.playbook.db import PlaybookDB
        row = PlaybookDB()._conn().execute(
            "SELECT direction, model_used FROM playbook_instructions "
            "WHERE date=? AND symbol=? AND direction IN ('BUY','SELL_SHORT') "
            "ORDER BY id ASC LIMIT 1",
            (date_str, symbol),
        ).fetchone()
        if row and (row[1] or "") != "intraday_reentry_v1":
            return row[0]
    except Exception:
        pass
    return None


def evaluate_candidate(direction: str, price: float, vwap: float,
                       slope_5m: Optional[float], atr_pct: Optional[float],
                       vol_ratio: Optional[float], spread_bps: Optional[float],
                       cfg: Dict[str, Any], capital_flow_direction: Optional[str] = None,
                       short_percent: Optional[float] = None,
                       insider_signal: Optional[str] = None,
                       analyst_trend: Optional[str] = None,
                       days_to_earnings: Optional[int] = None,
                       sentiment_score: Optional[float] = None,
                       sec_event_negative: Optional[bool] = None,
                       sec_event_13dg: Optional[bool] = None,
                       social_sentiment_score: Optional[float] = None,
                       ticker_sentiment: Optional[str] = None) -> Tuple[bool, List[str]]:
    """纯函数: 盘中指标是否确认 thesis 延续 (可单测, 零 IO).

    LONG: 回踩区 [vwap×(1−low), vwap×(1+high)] 内 + slope>min + 量比/ATR/spread 达标.
    SHORT 对称 (price 在 [vwap×(1−high), vwap×(1+low)], slope<−min).

    两类门语义:
      - 技术指标门 (vwap/slope/atr/vol/spread): 缺失→拒 (宁缺毋滥, 指标是入场必要条件)
      - 证伪门 (capital_flow/short/insider/analyst/earnings/sentiment/sec/social/ticker):
        缺失→不拦 (fail-open, 证伪是额外保险非必要条件; 数据读取失败时不应阻止交易)
    """
    reasons: List[str] = []
    is_long = direction.upper() in ("BUY", "LONG")
    if price <= 0 or vwap <= 0:
        return False, ["no_price_or_vwap"]
    dev_pct = (price - vwap) / vwap * 100.0
    lo, hi = float(cfg["vwap_band_low_pct"]), float(cfg["vwap_band_high_pct"])
    if is_long:
        if not (-lo <= dev_pct <= hi):
            reasons.append(f"vwap_dev({dev_pct:+.2f}%∉[{-lo},{hi}])")
        if slope_5m is None or slope_5m <= float(cfg["min_slope_5m_pct"]):
            reasons.append(f"slope_5m({slope_5m}≤{cfg['min_slope_5m_pct']})")
    else:
        if not (-hi <= dev_pct <= lo):
            reasons.append(f"vwap_dev({dev_pct:+.2f}%∉[{-hi},{lo}])")
        if slope_5m is None or slope_5m >= -float(cfg["min_slope_5m_pct"]):
            reasons.append(f"slope_5m({slope_5m}≥{-float(cfg['min_slope_5m_pct'])})")
    if atr_pct is None or atr_pct < float(cfg["min_atr_pct"]):
        reasons.append(f"atr_pct({atr_pct}<{cfg['min_atr_pct']})")
    if vol_ratio is None or vol_ratio < float(cfg["min_vol_ratio"]):
        reasons.append(f"vol_ratio({vol_ratio}<{cfg['min_vol_ratio']})")
    if spread_bps is None or spread_bps > float(cfg["max_spread_bps"]):
        reasons.append(f"spread_bps({spread_bps}>{cfg['max_spread_bps']})")
    # 2026-08-22 (用户裁决): 资金流背离检查 — DA方向与实时大单资金流背离时拒绝.
    #   证伪机制非预测机制: 资金流不告诉你该做什么, 但告诉你不该做什么(方向背离时别重入场).
    if cfg.get("capital_flow_divergence_check_enabled", True):
        if capital_flow_direction:
            if is_long and capital_flow_direction == "OUTFLOW":
                reasons.append(f"capital_flow_divergence(LONG vs OUTFLOW)")
            elif not is_long and capital_flow_direction == "INFLOW":
                reasons.append(f"capital_flow_divergence(SHORT vs INFLOW)")
    # 2026-08-22 (用户裁决): 卖空压力检查 — SHORT 方向时 short_percent 飙升(>threshold)=拥挤交易,
    #   squeeze 风险与做空方向背离 → 拒.
    #   2026-08-22b (ADR-032, 用户裁"请修复"): 删除 392f018e 加的 LONG 对称分支
    #   (LONG+short_percent>40% 拒) — 本门族语义是"方向与证据背离才拒", 而高空头仓位
    #   不构成 LONG 的反证(常规读法反而是 squeeze 燃料=利多); 原注释自述"底部确认"
    #   (多头证据)却用于拒绝做多、尾句"拒绝追空"又与 is_long 分支自相矛盾。
    #   若本意是"多空激战名波动过大不宜再入场", 那是波动率风控门非证伪门, 应按验证期
    #   纪律另立机制带数据裁决, 不以矛盾形式混入本门族。
    if cfg.get("short_squeeze_check_enabled", True):
        if not is_long and short_percent is not None and short_percent > float(cfg.get("short_squeeze_threshold_pct", 30.0)):
            reasons.append(f"short_squeeze_risk(short_percent={short_percent:.1f}%>{cfg['short_squeeze_threshold_pct']}%)")
    # 2026-08-22 (用户裁决): 内部人背离检查 — DA方向与内部人净买卖背离时拒绝.
    #   2026-08-22c 收窄 (用户裁"按建议执行"): 只留 buy 侧 — 内部人异常买入
    #   (非例行行为) 有真实信息量, 作 SHORT 反证; 删 sell 侧拦 LONG: 大公司
    #   10b5-1 例行减持常年存在, sell_count>buy_count 是常态而非背离信号,
    #   拿它拦 LONG = 用噪音系统性误杀多头再入场 (共识: 内部人买入有预测力,
    #   例行卖出几乎没有)。sell 信号仍采集落 shadow 供数据侧后续裁决。
    if cfg.get("insider_divergence_check_enabled", True):
        if insider_signal:
            if not is_long and insider_signal == "buy":
                reasons.append(f"insider_divergence(SHORT vs insider_buy)")
    # 2026-08-22 (用户裁决): 分析师背离检查 — DA方向与分析师评级趋势背离时拒绝.
    if cfg.get("analyst_divergence_check_enabled", True):
        if analyst_trend:
            if is_long and analyst_trend == "downgrade":
                reasons.append(f"analyst_divergence(LONG vs downgrade_trend)")
            elif not is_long and analyst_trend == "upgrade":
                reasons.append(f"analyst_divergence(SHORT vs upgrade_trend)")
    # 2026-08-22 (用户裁决): 财报临近检查 — 持仓标的财报临近(<3天)时收紧.
    if cfg.get("earnings_proximity_check_enabled", True):
        _days = int(cfg.get("earnings_proximity_days", 3))
        if days_to_earnings is not None and days_to_earnings < _days:
            reasons.append(f"earnings_proximity({days_to_earnings}d<{_days}d)")
    # 2026-08-22 (用户裁决): 情绪极端检查 — DA方向与社交情绪极端背离时拒绝.
    if cfg.get("sentiment_extreme_check_enabled", True):
        if sentiment_score is not None:
            _threshold = float(cfg.get("sentiment_extreme_threshold", 0.8))
            if is_long and sentiment_score > _threshold:
                reasons.append(f"sentiment_extreme(LONG vs extreme_bullish={sentiment_score:.2f}>{_threshold})")
            elif not is_long and sentiment_score < -_threshold:
                reasons.append(f"sentiment_extreme(SHORT vs extreme_bearish={sentiment_score:.2f}<-{_threshold})")
    # 2026-08-22 (用户裁决): 重大事件背离检查 — 8-K/13D-G 披露重大负面时拒绝.
    #   2026-08-22b 复查修复: 对齐本门族"方向与证据背离才拒"语义 —
    #   利空 8-K item (协议终止/破产/减值/财报重述/高管变动/重组成本) 只构成 LONG
    #   反证; SC 13D 主动举牌 (增持=利多) 只构成 SHORT 反证。原实现无方向区分,
    #   且把 13D/G 举牌一律当"负面"属语义颠倒 (举牌是做多证据)。
    if cfg.get("sec_event_divergence_check_enabled", True):
        if is_long and sec_event_negative:
            reasons.append("sec_event_divergence(LONG vs bearish_8k_items)")
        elif not is_long and sec_event_13dg:
            reasons.append("sec_event_divergence(SHORT vs SC13D_accumulation)")
    # 2026-08-22 (用户裁决): 多源情绪验证 — social_sentiment 与 finnhub 情绪分数背离时拒绝.
    if cfg.get("social_sentiment_divergence_check_enabled", True):
        if sentiment_score is not None and social_sentiment_score is not None:
            _threshold = float(cfg.get("social_sentiment_divergence_threshold", 0.3))
            # 多源背离: finnhub 看多(>threshold) 但 social 看空(<-threshold), 或反之
            if ((sentiment_score > _threshold and social_sentiment_score < -_threshold)
                    or (sentiment_score < -_threshold and social_sentiment_score > _threshold)):
                reasons.append(f"social_sentiment_divergence(finnhub={sentiment_score:.2f} vs social={social_sentiment_score:.2f}, threshold={_threshold})")
    # 2026-08-22 (用户裁决): 盘中逐笔情绪背离检查 — DA方向与逐笔成交情绪背离时拒绝.
    if cfg.get("ticker_sentiment_divergence_check_enabled", True):
        if ticker_sentiment:
            if is_long and ticker_sentiment == "bearish":
                reasons.append(f"ticker_sentiment_divergence(LONG vs bearish_tick)")
            elif not is_long and ticker_sentiment == "bullish":
                reasons.append(f"ticker_sentiment_divergence(SHORT vs bullish_tick)")
    return (not reasons), reasons


def choose_direction(prior_direction: str, slope_5m: Optional[float],
                     adaptive_enabled: bool) -> Tuple[str, str]:
    """纯函数: 再入场方向裁决 (可单测, 零 IO).

    2026-08-12 用户裁决直接落地 (非影子对照): 方向不再硬继承前笔,
    由 slope_5m 符号顺势裁决 — 顺势选择, 不持续偏多/偏空:
      slope > 0  → BUY        (动量向上, 顺势做多)
      slope < 0  → SELL_SHORT (动量向下, 顺势做空)
      slope 缺失/==0 → 继承前笔方向 (与改动前行为完全一致, fail-safe)
    返回 (direction, mode_tag): mode_tag ∈ {adaptive, inherited} 供影子审计。

    历史证据披露 (skill ftseek-intraday-reentry 坑#2): 8/7 审计实测
    "盲翻转/盲继承"对后续 1h 方向无区分力 (55.4% vs 44.6%, ≈随机)。
    本改动与其不同: 不是盲翻, 是指标确认门 (evaluate_candidate 的
    vwap_band+slope 双向对称分支) 按裁决方向二次过滤 — 方向错则门拒。
    """
    inherited = "BUY" if str(prior_direction).upper() in ("LONG", "BUY") else "SELL_SHORT"
    if not adaptive_enabled or slope_5m is None or float(slope_5m) == 0.0:
        return inherited, "inherited"
    chosen = "BUY" if float(slope_5m) > 0 else "SELL_SHORT"
    return chosen, ("inherited" if chosen == inherited else "adaptive")


def _load_closed_today() -> Dict[str, Dict[str, Any]]:
    """当日已平仓 sim 交易, 按 symbol 聚合。只取恰好平仓 1 笔的标的 (自然约束=每标的日 1 次)."""
    from app.core.paths import get_trade_records_db
    today = _today_et_str()
    conn = sqlite3.connect(str(get_trade_records_db()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, exit_price, pnl_pct, exit_reason, quantity
            FROM trade_records
            WHERE trade_date = ?
              AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND exit_price > 0 AND exit_time != '' AND exit_time IS NOT NULL
            ORDER BY id
        """, (today,)).fetchall()
    finally:
        conn.close()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r["symbol"]
        e = out.setdefault(sym, {"symbol": sym, "closed_count": 0, "trades": []})
        e["closed_count"] += 1
        e["trades"].append({
            "trade_id": r["id"], "direction": r["direction"],
            "entry_price": float(r["entry_price"] or 0),
            "exit_price": float(r["exit_price"] or 0),
            "pnl_pct": float(r["pnl_pct"] or 0),  # 小数口径 (0.0033=0.33%)
            "exit_reason": r["exit_reason"],
            "quantity": float(r["quantity"] or 0),
        })
    return out


def _has_open_trade(symbol: str) -> bool:
    from app.core.paths import get_trade_records_db
    conn = sqlite3.connect(str(get_trade_records_db()))
    try:
        n = conn.execute("""
            SELECT COUNT(*) FROM trade_records
            WHERE symbol = ? AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
        """, (symbol,)).fetchone()[0]
        return n > 0
    finally:
        conn.close()


def _arms_today() -> int:
    if not _OUTPUT_PATH.exists():
        return 0
    today = _today_et_str()
    n = 0
    try:
        with _OUTPUT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("date") == today and r.get("action") == "armed":
                    n += 1
    except Exception:
        return 0
    return n


def _armed_symbols_today() -> set:
    """当日已武装锁定的标的集合 (armed 或 arm_failed — 锚点已刷新即锁定)。

    2026-08-07 (用户接管审查#2): 一次性武装 — 武装后即使未触发也不再重刷锚点,
    防止\"武装→拦→重武装\"循环让 entry 追着市价跑 (跟随重武装语义), 锚点锁定
    在武装时刻的确认价, 符合\"回踩企稳再入\" thesis (price ≤ entry 才触发)。
    """
    if not _OUTPUT_PATH.exists():
        return set()
    today = _today_et_str()
    out = set()
    try:
        with _OUTPUT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("date") == today and r.get("action") in ("armed", "arm_failed"):
                    out.add(r.get("symbol"))
    except Exception:
        return set()
    return out


def _shadow_write(row: Dict[str, Any]) -> None:
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _arm_instruction(symbol: str, direction: str, price: float,
                     cfg: Dict[str, Any], trade_id: Any,
                     atr_pct: Optional[float] = None) -> Optional[int]:
    """重武装当日指令: 为新一轮入场武装一条**独立指令行** (过 admission 入口审计)。

    返回指令 id; 失败 None。

    RFC-066 (2026-08-25, 根因修复): 多行台账下, 再入场走 save_instruction 的
    `intraday_reentry-` 分支 → **INSERT 新轮次行**, 与第一轮已结算行共存,
    不再覆盖/复位第一轮 (旧版 UPSERT 整行覆盖 + 复位 executed/outcome/pnl
    曾把第一轮结算物理抹除, 8/7-8/24 25 行实锤)。
    original_instruction 快照仍归档到 metadata_json (首笔归因审计), 但
    不再是"防覆盖"的唯一手段 — 第一轮行本身不再被触碰。

    2026-08-07 修复 (审查盲区1/2) 历史背景:
    - 盲区1: 旧 save_instruction 按 (date,symbol) UPSERT 会原地覆盖当日原指令行
      (decision_id/thesis/confidence/direction 全被改写) → 首笔成交的指令层
      归因断链。当时修复 = 覆盖前归档快照; RFC-066 起根因已消除 (不覆盖)。
    - 盲区2: 旧复位 UPDATE 带 WHERE executed=1 → 触发未成交时不复位。RFC-066
      起复位逻辑整体删除 (新轮次行天然 executed=0/triggered=0)。

    2026-08-07 (用户裁决快收结构): SL/TP 主口径改 quick_sl_tp_pct (ATR 相对,
    SL放宽/TP降档); ATR 缺失回退 sl_pct/tp_rr 固定值。
    """
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    today = _today_et_str()
    is_long = direction.upper() == "BUY"
    _q_sl, _q_tp = quick_sl_tp_pct(atr_pct, cfg)
    if _q_sl is not None and _q_tp is not None:
        sl_pct, tp_pct = _q_sl / 100.0, _q_tp / 100.0
    else:
        sl_pct = float(cfg["sl_pct"]) / 100.0
        tp_pct = sl_pct * float(cfg["tp_rr"])
    stop_loss = round(price * (1 - sl_pct), 2) if is_long else round(price * (1 + sl_pct), 2)
    take_profit = (round(price * (1 + tp_pct), 2) if is_long
                   else round(price * (1 - tp_pct), 2))
    hhmm = _now_hhmm_et()

    # ── RFC-066: 快照取当日第一轮指令 (多行台账下 = 最小 id 行; 旧时代是唯一行) ──
    orig_fields = None
    try:
        orig = db._conn().execute(
            "SELECT id, decision_id, thesis, confidence, direction, entry_price, "
            "       stop_loss, take_profit, raw_instruction_json, metadata_json "
            "FROM playbook_instructions WHERE date = ? AND symbol = ? "
            "ORDER BY id ASC LIMIT 1",
            (today, symbol),
        ).fetchone()
        if orig is not None:
            orig_fields = {
                "instruction_id": orig[0], "decision_id": orig[1], "thesis": orig[2],
                "confidence": orig[3], "direction": orig[4], "entry_price": orig[5],
                "stop_loss": orig[6], "take_profit": orig[7],
                "raw_instruction_json": orig[8], "metadata_json": orig[9],
            }
    except Exception:
        orig_fields = None                    # 快照失败不阻断武装 (归档是增量审计)

    inst = {
        "date": today, "symbol": symbol, "direction": direction,
        "entry_price": round(price, 2),
        "stop_loss": stop_loss, "take_profit": take_profit,
        "position_size": None,          # sizing SSOT 在执行期确定 (admission 执行复核兜底)
        "confidence": 55,               # >50 避开 LONG 分歧降仓分支; confidence 无 IC 证据, 仅占位
        "decision_id": f"intraday_reentry-{trade_id}-{hhmm}",
        "thesis": (f"盘中再入场: 前笔(#{trade_id})非硬亏平仓后"
                   + ("反弹VWAP下方受阻, " if not is_long else "回踩VWAP企稳, ")
                   + "指标二次确认(方向由slope_5m顺势裁决)"),
        "horizon": "short",
        "model_used": "intraday_reentry_v1",
        "raw_instruction": {"source": "intraday_reentry", "reentry_of_trade": trade_id},
        "metadata_json": {
            "source": "intraday_reentry",
            "reentry_of_trade": trade_id,
            "original_instruction": orig_fields,   # 首笔归因快照 (盲区1)
        },
    }
    new_id = db.save_instruction(inst)   # RFC-066: 新轮次 INSERT 独立行, 第一轮结算行不动
    if not new_id:
        # save_instruction 仲裁拒绝 (理论不可达: reentry 前缀恒走 INSERT) — 防御
        logger.warning(f"[再入场] {symbol} 指令武装被仲裁拒绝, 记 arm_failed")
        return None
    logger.info(
        f"[再入场] {symbol} {direction} 重武装指令#{new_id} 完成 "
        f"(原指令#{orig_fields['instruction_id'] if orig_fields else '?'} 已归档 metadata_json)"
    )
    return new_id


def run_scan(scheduler=None) -> int:
    """调度器钩子: TRADING 相位每 300s 扫一轮, 返回武装指令数。全程异常隔离由调用方兜底。"""
    global _last_run_ts
    cfg = _cfg()
    if not cfg["enabled"]:
        return 0

    from app.core.scheduler_triggers import MarketPhase, get_market_phase
    if get_market_phase() != MarketPhase.TRADING:
        return 0
    now = time.time()
    if now - _last_run_ts < int(cfg["scan_interval_sec"]):
        return 0
    hm = _now_hhmm_et()
    if not (int(cfg["window_start_hhmm"]) <= hm <= int(cfg["window_end_hhmm"])):
        return 0
    _last_run_ts = now

    # sim-only 硬守卫: 非模拟环境强制 record-only (双保险, 即使 mode=live)
    mode = str(cfg["mode"]).lower()
    try:
        from app.core.config_container import get_config as _gc
        if _gc().get_dotted("playbook.execute_mode", "futu_simulate") != "futu_simulate":
            mode = "shadow"
    except Exception:
        mode = "shadow"

    router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if router is None:
        return 0

    arms_so_far = _arms_today()
    armed_syms = _armed_symbols_today()
    armed = 0
    today = _today_et_str()

    # ── 2026-08-22 (性能修复): 证伪门数据循环外批量预读 ──
    # 修复: ①capital_flow 原循环内全文件读取(58812行×候选数) → 循环外一次读取建索引
    #       ②FutuExtendedProvider 原循环内每候选新建连接(资源泄漏) → 进程级单例复用
    # 2026-08-22b 复查修复: ③新鲜度 — 原索引取"该标的最后一条"无日期过滤, 实测 62
    #   标的最后记录全部来自历史交易日 (采集有订阅上限, 标的掉出订阅后旧记录永驻),
    #   拿数日前的收盘资金流方向拒当日单 = 伪证伪 → 只认当日记录;
    #   ④大单净流==0 原归 OUTFLOW 拦 LONG, 属无信息误伤 → 不给方向。
    _cf_last_flow: Dict[str, str] = {}  # symbol → INFLOW/OUTFLOW (当日最后一条)
    if cfg.get("capital_flow_divergence_check_enabled", True):
        try:
            import json as _json
            from pathlib import Path as _Path
            _cf_path = _Path("data/futu_capital_flow.jsonl")
            _today_prefix = _today_et_str()
            if _cf_path.exists():
                for _line in _cf_path.read_text().strip().split("\n"):
                    try:
                        _d = _json.loads(_line)
                    except Exception:
                        continue
                    _s = _d.get("symbol")
                    if not _s:
                        continue
                    if not str(_d.get("item_time") or "").startswith(_today_prefix):
                        continue  # 非当日记录 → 陈旧方向不可作证
                    _large = float(_d.get("super_in_flow", 0) or 0) + float(_d.get("big_in_flow", 0) or 0)
                    if _large > 0:
                        _cf_last_flow[_s] = "INFLOW"   # 后写覆盖=最新一条
                    elif _large < 0:
                        _cf_last_flow[_s] = "OUTFLOW"
        except Exception:
            pass  # 读取失败 → 空索引 → 不拦 (fail-open)

    _futu_ext_shared = None  # 进程级单例 (见 _futu_ext), 证伪门共用的 OpenD 连接
    if cfg.get("short_squeeze_check_enabled", True) or cfg.get("insider_divergence_check_enabled", True):
        _futu_ext_shared = _futu_ext()

    for sym, info in _load_closed_today().items():
        if sym in armed_syms:
            continue                      # 一次性武装锁定: 锚点不再跟随市价重刷 (防追价循环)
        if arms_so_far + armed >= int(cfg["max_arms_per_day"]):
            break
        if info["closed_count"] != 1:
            continue                      # 0 笔=未平, ≥2 笔=已再入场过 → 每标的日 1 次
        prior = info["trades"][0]
        direction = "BUY" if str(prior["direction"]).upper() in ("LONG", "BUY") else "SELL_SHORT"
        if not prior_exit_reenterable(prior.get("exit_reason"), prior["pnl_pct"],
                                      float(cfg["max_prior_loss_pct"])):
            base = {
                "ts_et": _now_iso_et(), "date": today, "symbol": sym,
                "direction": direction,
                "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
                "prior_exit_reason": prior.get("exit_reason"), "mode": mode,
            }
            base["action"] = "rejected"
            base["reasons"] = [f"exit_reason({prior.get('exit_reason')})"]
            _shadow_write(base)           # 可审计: 白名单拦截的候选落 shadow, 不静默丢弃
            continue                      # 硬止损/0-WR/操作类 → 不报复性再入
        if not _cumulative_exposure_ok(prior, cfg):
            base = {
                "ts_et": _now_iso_et(), "date": today, "symbol": sym,
                "direction": direction,
                "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
                "prior_exit_reason": prior.get("exit_reason"), "mode": mode,
            }
            _pnot = float(prior.get("entry_price") or 0) * float(prior.get("quantity") or 0)
            base["action"] = "rejected"
            base["reasons"] = [f"cumulative_exposure(prior_notional=${_pnot:.0f})"]
            _shadow_write(base)           # 可审计: 累计暴露拦截落 shadow
            continue                      # 首笔名义已高 → 再入=标的集中暴露, 拦
        if _has_open_trade(sym):
            continue
        try:
            from app.playbook.pipeline import _symbol_streak_banned
            from app.playbook.config import get_playbook_config
            if _symbol_streak_banned(sym, get_playbook_config()):
                continue
        except Exception:
            pass                          # streak 查询异常 → 不拦 (与门本体 fail-open 语义一致)

        base: Dict[str, Any] = {
            "ts_et": _now_iso_et(),
            "date": today, "symbol": sym, "direction": direction,
            "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
            "prior_exit_reason": prior.get("exit_reason"),
            "mode": mode,
        }
        try:
            ctx = router.get_full_market_context(sym) or {}
        except Exception:
            ctx = {}
        price = float(ctx.get("price", 0) or 0)
        vwap = float(ctx.get("vwap_intraday") or ctx.get("vwap") or 0)
        slope_5m = ctx.get("slope_5m")
        atr_pct = ctx.get("atr_percent")
        try:
            from app.playbook.pipeline import _calc_intraday_vol_ratio
            vol_ratio = _calc_intraday_vol_ratio(router, sym)
        except Exception:
            vol_ratio = None
        spread_bps = None
        try:
            from app.playbook.pipeline import _fetch_l1_micro
            micro = _fetch_l1_micro(scheduler, sym)
            if micro:
                spread_bps = micro.get("spread_bps")
        except Exception:
            micro = None

        # 2026-08-22 (用户裁决): 资金流背离检查 — 消费循环外预读的最后一条方向索引.
        capital_flow_direction = _cf_last_flow.get(sym)

        # 2026-08-22 (用户裁决): 卖空压力检查 — 读该标的每日卖空成交量 (共享连接).
        short_percent = None
        if cfg.get("short_squeeze_check_enabled", True) and _futu_ext_shared is not None:
            try:
                _dsv = _futu_ext_shared.get_daily_short_volume(sym)
                if _dsv.get("status") == "ok" and _dsv.get("data"):
                    short_percent = _dsv["data"].get("short_percent")
            except Exception:
                pass  # 卖空数据读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 内部人背离检查 — 读该标的内部人净买卖方向 (共享连接).
        #   实测字段: data.signal ∈ {strong_buy, buy, sell, neutral}
        insider_signal = None
        if cfg.get("insider_divergence_check_enabled", True) and _futu_ext_shared is not None:
            try:
                _it = _futu_ext_shared.get_insider_trades(sym)
                if _it.get("status") == "ok" and _it.get("data"):
                    _raw_signal = _it["data"].get("signal")
                    # strong_buy/buy → buy; sell → sell; neutral → None
                    if _raw_signal in ("buy", "strong_buy"):
                        insider_signal = "buy"
                    elif _raw_signal == "sell":
                        insider_signal = "sell"
            except Exception:
                pass  # 内部人数据读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 分析师背离检查 — 读该标的分析师评级趋势.
        # 2026-08-22 接线修复 (ADR-032): 原读不存在的 buy_pct/sell_pct 键恒死;
        # 改 _derive_analyst_trend 按真实 {periods:[...]} schema 推修订动量.
        analyst_trend = None
        if cfg.get("analyst_divergence_check_enabled", True):
            try:
                from app.data.analyst_revision_provider import get_analyst_provider
                _ap = get_analyst_provider()
                _rt = _ap.get_recommendation_trend(sym)
                analyst_trend = _derive_analyst_trend(_rt if isinstance(_rt, dict) else None)
            except Exception:
                pass  # 分析师数据读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 财报临近检查 — 读该标的财报日期.
        # 2026-08-22 接线修复 (ADR-032): 原对 List[Dict] 做 `sym in _earn` 恒 False;
        # 改 _find_days_to_earnings 遍历行匹配 Futu 格式 symbol.
        days_to_earnings = None
        if cfg.get("earnings_proximity_check_enabled", True):
            try:
                # ADR-032: 原导入名 get_earnings_calendar 不存在(ImportError 被 fail-open
                # 吞掉, 系该门第三重死因); 日历实例进程级复用 (实例缓存 1h, 原每候选
                # 新建使缓存失效=每候选一次全量拉取)。
                _ec = _earnings_cal()
                if _ec is not None:
                    _earn = _ec.get_watchlist_earnings(watchlist_symbols=[sym])
                    days_to_earnings = _find_days_to_earnings(_earn, sym)
            except Exception:
                pass  # 财报数据读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 情绪极端检查 — 读该标的社交情绪分数.
        sentiment_score = None
        if cfg.get("sentiment_extreme_check_enabled", True):
            try:
                _fp = _finnhub()
                if _fp is not None:
                    sentiment_score = _fp.get_sentiment_score(sym)
            except Exception:
                pass  # 情绪数据读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 重大事件背离检查 — 读该标的 8-K/13D-G 重大事件.
        # 2026-08-22b 复查修复 (三重死因): ①原 universe 传 Futu 格式 "US.X", 而
        #   provider 按 CIK 映射的裸 ticker 过滤 → 恒空列表 (死门); ②英文关键词
        #   (resignation/sell...) 对 str(dict) 匹配, 而条目文本是中文 item_labels
        #   → 即便修①也近零命中; ③13D/G=举牌(利多)被当负面。改: 裸 ticker 过滤 +
        #   ITEM_SEMANTICS 利空 item 代码集 + 13D 只作 SHORT 反证 (见 evaluate_candidate)。
        sec_event_negative = None
        sec_event_13dg = None
        if cfg.get("sec_event_divergence_check_enabled", True):
            try:
                from app.data.sec_8k_provider import fetch_recent_8k, fetch_recent_13dg
                _tk = sym[3:] if sym.startswith("US.") else sym
                _bearish_items = {"1.02", "1.03", "2.05", "2.06", "4.02", "5.02"}
                _8k = fetch_recent_8k({_tk})
                _13dg = fetch_recent_13dg({_tk})
                sec_event_negative = any(
                    _bearish_items.intersection(e.get("items") or []) for e in _8k)
                sec_event_13dg = any(
                    str(e.get("form") or "").startswith("SC 13D") for e in _13dg)
            except Exception:
                pass  # 重大事件读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 多源情绪验证 — 读 social_sentiment.
        # 2026-08-22 接线修复 (ADR-032): 该源返回结构无极性分数 (仅计数/点赞/标题),
        # 本门默认关断 (_DEFAULTS False); 下方 .get("score") 在现源下恒 None, 属已知
        # 不可实现, 留作接入带符号分数源后的接线占位.
        social_sentiment_score = None
        if cfg.get("social_sentiment_divergence_check_enabled", True):
            try:
                from app.data.social_sentiment import collect_sentiment
                _ss = collect_sentiment(sym)
                if _ss and isinstance(_ss, dict):
                    social_sentiment_score = _ss.get("score")
            except Exception:
                pass  # 社交情绪读取失败 → None → 不拦 (fail-open)

        # 2026-08-22 (用户裁决): 盘中逐笔情绪背离检查 — 读逐笔成交情绪.
        # 2026-08-22 接线修复 (ADR-032): 原读不存在的 buy_strength/sell_strength 键恒死;
        # 改 _derive_ticker_direction 按真实 {sentiment, confidence} schema 推方向.
        ticker_sentiment = None
        if cfg.get("ticker_sentiment_divergence_check_enabled", True):
            try:
                from app.data.futu_lv3_provider import get_futu_lv3_provider
                _lv3 = get_futu_lv3_provider()
                _ts = _lv3.get_ticker_sentiment(sym, window=30)
                ticker_sentiment = _derive_ticker_direction(_ts if isinstance(_ts, dict) else None)
            except Exception:
                pass  # 逐笔情绪读取失败 → None → 不拦 (fail-open)

        # ── 2026-08-12 方向顺势裁决 (用户裁决直接落地): slope_5m 符号定方向, 缺失/0 → 继承前笔 ──
        direction, dir_mode = choose_direction(
            prior["direction"],
            float(slope_5m) if slope_5m is not None else None,
            bool(cfg["direction_adaptive_enabled"]),
        )
        base["direction"] = direction                    # 裁决后方向覆盖审计行 (原为继承值)
        base["direction_mode"] = dir_mode                # adaptive=顺势改向 / inherited=继承前笔
        base["prior_direction"] = str(prior["direction"]).upper()

        # ── 2026-08-27 (8/26 LLY 反手, 用户批准): 当日反手硬闸 — 与当日第一轮
        #    DA/UE 方向相反 → 拒绝 (不进入证伪门评估, shadow 记 rejected 可审计)。
        base["day_first_direction"] = None
        if cfg.get("same_day_reversal_block_enabled", True):
            _fd = _day_first_direction(today, sym)
            base["day_first_direction"] = _fd
            if _fd and _fd != direction:
                base["action"] = "rejected"
                base["reasons"] = [f"same_day_reversal: 当日第一轮指令{_fd}, 禁止反手{direction}"]
                _shadow_write(base)
                logger.info(
                    f"[再入场] {sym} 反手拦截: 当日第一轮{_fd} → 拒绝{direction} "
                    f"(前笔#{prior['trade_id']}, 8/26 LLY 教训闸)"
                )
                continue

        ok, reasons = evaluate_candidate(
            direction, price, vwap,
            float(slope_5m) if slope_5m is not None else None,
            float(atr_pct) if atr_pct is not None else None,
            vol_ratio, spread_bps, cfg,
            capital_flow_direction=capital_flow_direction,
            short_percent=short_percent,
            insider_signal=insider_signal,
            analyst_trend=analyst_trend,
            days_to_earnings=days_to_earnings,
            sentiment_score=sentiment_score,
            sec_event_negative=sec_event_negative,
            sec_event_13dg=sec_event_13dg,
            social_sentiment_score=social_sentiment_score,
            ticker_sentiment=ticker_sentiment,
        )
        base.update({"price": price, "vwap": vwap, "slope_5m": slope_5m,
                     "atr_pct": atr_pct, "vol_ratio": vol_ratio,
                     "spread_bps": spread_bps, "micro": micro})
        if not ok:
            base["action"] = "rejected"
            base["reasons"] = reasons
            _shadow_write(base)
            continue

        if mode != "live":
            base["action"] = "would_arm"
            _shadow_write(base)
            logger.info(f"[再入场·影子] {sym} {direction} 指标确认, shadow 模式不武装")
            continue

        inst_id = _arm_instruction(sym, direction, price, cfg, prior["trade_id"],
                                   atr_pct=float(atr_pct) if atr_pct is not None else None)
        base["action"] = "armed" if inst_id else "arm_failed"
        base["instruction_id"] = inst_id
        _f_sl, _f_tp = quick_sl_tp_pct(float(atr_pct) if atr_pct is not None else None, cfg)
        base["sl_pct"] = _f_sl if _f_sl is not None else cfg["sl_pct"]
        base["tp_pct"] = _f_tp if _f_tp is not None else round(cfg["sl_pct"] * cfg["tp_rr"], 2)
        _shadow_write(base)
        if inst_id:
            armed += 1
            logger.info(
                f"[再入场] {sym} {direction} 已重武装指令#{inst_id} "
                f"(前笔#{prior['trade_id']} pnl={prior['pnl_pct']*100:+.2f}%, "
                f"price=${price:.2f} vwap=${vwap:.2f} slope={slope_5m} vr={vol_ratio})"
            )
    return armed


def _is_reentry_instruction(symbol: str, date_str: str) -> bool:
    """该标的当日最新指令是否本生成器武装 (decision_id 前缀判定). 异常 → False (不豁免, 保守).

    RFC-066 多行台账: 同一 (date, symbol) 可有多轮, 取最大 id 行 = 最新一轮。
    """
    try:
        from app.playbook.db import PlaybookDB
        conn = PlaybookDB()._conn()
        row = conn.execute(
            "SELECT decision_id FROM playbook_instructions "
            "WHERE date = ? AND symbol = ? ORDER BY id DESC LIMIT 1",
            (date_str, symbol),
        ).fetchone()
        did = str(row[0] or "") if row else ""
        return did.startswith("intraday_reentry-")
    except Exception:
        return False


def overnight_loser_exempt(symbol: str, entry_time_str: str, profit_pct: float,
                           is_reentry=None) -> bool:
    """EOD 强平豁免判定 (2026-08-06 用户裁决"再入场亏损可隔夜").

    全部满足才豁免 (任一不满足 → 正常当日平仓):
      1. 配置开关 overnight_hold_loser_enabled;
      2. 水下仓 (profit_pct < 0) — 盈利仓仍当日落袋;
      3. 首夜 (entry 日期 == 今日 ET) — 次日起不再豁免, PRE_CLOSE/EOD 正常收;
      4. 本生成器武装的仓 (decision_id 前缀) — 其他来源仓位不适用。

    is_reentry: 可注入的 (symbol, date_str)->bool, 默认查库; 单测传桩。
    """
    cfg = _cfg()
    if not cfg.get("overnight_hold_loser_enabled"):
        return False
    try:
        if float(profit_pct) >= 0:
            return False
    except Exception:
        return False
    try:
        from datetime import datetime as _dt
        from app.utils.timezone import now_et as _now_et_fn
        _et_now = _now_et_fn()
        _entry = _dt.fromisoformat(str(entry_time_str))
        _entry_date = (_entry.astimezone(_et_now.tzinfo).date()
                       if _entry.tzinfo is not None else _entry.date())
        if _entry_date != _et_now.date():
            return False                     # 非首夜 → 不豁免
    except Exception:
        return False                         # 日期解析异常 → 不豁免 (保守)
    checker = is_reentry or _is_reentry_instruction
    try:
        return bool(checker(symbol, _entry_date.isoformat()))
    except Exception:
        return False
