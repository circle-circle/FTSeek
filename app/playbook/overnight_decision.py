"""EOD 隔夜豁免单一决策点 (2026-09-06, P1 三轨合一 — 行为等价合并).

合并前 (散落 pipeline.py, 两处 EOD 决策面各自链式调用):
  - 轨③ reentry 首夜: pipeline._reentry_overnight_exempt → 判定本体
    app.playbook.intraday_reentry.overnight_loser_exempt (开关/水下/首夜/decision_id 前缀);
  - W_WEEK SIM 多夜: pipeline._loser_overnight_exempt (SIM-only/水下/水深上限/5交易日上限);
  - 决策面: poll 侧 PRE_CLOSE (pipeline.py 规则4) 与 EOD 兜底 sweeper
    (_force_close_orphaned_sim_positions), 顺序恒定 = 轨③先判, 不中再判 W_WEEK,
    每层各自异常收口 (异常 → 不豁免该层 → 继续下一层, 保守方向).

合并后: 本模块是 EOD 隔夜豁免的唯一入口:
    overnight_eod_decision(...) -> (exempt: bool, track: str | None)
  track ∈ {"reentry_first_night", "w_week", None}, 与 metadata.overnight_arm.source
  台账值逐字一致 (RFC-067 契约, scripts/overnight_arm_report.py 消费).

边界 (明确不含):
  - 轨①总开关 overnight_hold_enabled → 上游 _skip_allowed 已门控, 不在此重复;
  - DEAD_SIGNAL/TIME_EXIT/MAX_HOLD 三处盘中豁免 — 轨③从不参与 (EOD-only),
    仍直连 w_week_loser_exempt (原 pipeline._loser_overnight_exempt 本体);
  - 赢家隔夜 (cohort B 盈利仓) 走 _skip_allowed 分支, 与本函数无关.

行为等价契约: tests/playbook/test_overnight_decision_contract.py 真值表钉锁
(对合并前旧链 ir.overnight_loser_exempt → pipeline._loser_overnight_exempt 全组合等价).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def horizon_max_hold_hit(entry_time_str: str, max_hold_days: int) -> bool:
    """判断 long horizon 仓位是否已到 max_hold_days 交易日.

    P1 (2026-09-06) 自 pipeline._horizon_max_hold_hit 逐字迁入; pipeline 侧保留同名转发。
    """
    if not entry_time_str or max_hold_days <= 0:
        return False
    try:
        from datetime import datetime
        from app.core.scheduler_triggers import ET as _ET
        from app.utils.holiday_calendar import nth_trading_day_after
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_ET).date()
        else:
            _entry_date = _entry.date()
        deadline = nth_trading_day_after("US", _entry_date, max_hold_days)
        if deadline is None:
            return False
        return datetime.now(_ET).date() >= deadline
    except Exception as e:
        logger.debug(f"[playbook·horizon] MAX_HOLD 日期计算异常 (不强平): {e}")
        return False


def w_week_loser_exempt(source_type: str, entry_time_str: str, profit_pct: float) -> bool:
    """SIM水下仓多夜 EOD/TIME_EXIT 豁免 (2026-08-15 用户三问Q3+追加裁决, DEB§33/§34).

    用户裁 (Q3): "盈利落袋为安, 未盈利的继续保持隔夜"。
    用户追加裁 (§34): "隔夜最长两日太少 — 要么等SL, 要么留足时间(约一周)"。

    多日回放裁决 (scripts/overnight_loser_multiday_replay.py, n=15, 6/1起):
      W_SL 纯等SL(5日上限) +$463 修复率53% < W_WEEK 落袋+等SL +$629 修复率73%
      → 采用 W_WEEK 语义: EOD转正即落袋 (盈利仓走 PRE_CLOSE 平仓, 天然路径),
        水下仓豁免持夜, 每日 SL 硬兜底, 到期强平 (无无限扛单路径)。

    条件 (任一不满足 → 照常退出, 保守):
      1. 配置开关 playbook.loser_overnight_enabled;
      2. 仅 PLAYBOOK_SIM (LIVE 隔夜是风险跃迁, 与 cohort B LIVE guard 同纪律, 须另裁);
      3. 水下 (profit_pct < 0) — 盈利仓仍当日落袋;
      4. 未达 loser_overnight_max_hold_days 交易日 (默认5≈一周, 交易日历计算);
         到期 → False → PRE_CLOSE 正常收。

    配套: 多夜水下仓同时豁免 TIME_EXIT (见 evaluate 规则3), 否则次日水下2h被砍,
    W_WEEK 修复空间不复存在; SL 仍是最高优先级硬兜底。OVERNIGHT_GAP_STOP 不覆盖
    本臂 (需 _skip_allowed), 跳空越 SL 由 SL 兜底市价收 (sim 期滑点可接受)。
    任何异常 → False (不豁免)。

    P1 (2026-09-06) 自 pipeline._loser_overnight_exempt 逐字迁入; pipeline 侧保留同名转发。
    """
    try:
        from app.playbook.config import get_playbook_config
        if not get_playbook_config().get("loser_overnight_enabled", False):
            return False
        if source_type != "PLAYBOOK_SIM":
            return False
        if float(profit_pct) >= 0:
            return False
        # 2026-08-20 (DEB §47 方案a, 用户裁决): 水深上限 — 水下超阈值的仓属"入场即错"
        #   非修复窗口, 撤销豁免转 EOD/DEAD_SIGNAL/TIME_EXIT 链. 本函数是 EOD/DEAD_SIGNAL/
        #   TIME_EXIT/MAX_HOLD 四处豁免唯一入口 → 单点生效全链 (SL 仍最高优先级硬兜底).
        #   证据: n=20 EV 曲线, 决策时刻水深>2% (MU/FIX -3.1%) 0修复2灾难 vs <2% 带
        #   修复臂占优; 1.5% 刀尖拟合弃用. 回滚: YAML 置 99 (=禁用).
        #   2026-08-21 (06432156): 次日即误杀 (AMZN 盘中水深-2.04%触发, 30分钟后回+1.1%),
        #   用户当日裁决回滚 99 — 教训: 水深闸放本函数=盘中豁免链也查, 盘中-2%是噪声.
        #   后续任何复活尝试必须改为 EOD 判定时刻查 (见 PROPOSAL-20260906B §0.5), 禁直改本闸.
        _max_depth = float(get_playbook_config().get("loser_overnight_max_depth_pct", 99) or 99)
        if float(profit_pct) < -_max_depth:
            return False
        _max_days = int(get_playbook_config().get("loser_overnight_max_hold_days", 5) or 5)
        # 已达最大持有交易日 → 不豁免 (到期强平, 走 PRE_CLOSE 正常收)
        if horizon_max_hold_hit(entry_time_str, _max_days):
            return False
        return True
    except Exception:
        return False


def reentry_first_night_exempt(symbol: str, entry_time_str: str, profit_pct: float,
                               is_reentry=None) -> bool:
    """轨③: 再入场水下仓 EOD 首夜豁免 (2026-08-06 用户裁决"再入场亏损可隔夜").

    判定本体在 app.playbook.intraday_reentry.overnight_loser_exempt
    (开关/水下/首夜/decision_id 前缀四条件)。此处仅做异常收口:
    任何异常 → False (不豁免, 照常当日平仓 — 保守方向)。

    P1 (2026-09-06) 自 pipeline._reentry_overnight_exempt 迁入, 新增 is_reentry
    透传 (默认 None = 查库, 与合并前逐字一致; 单测传桩)。
    """
    try:
        from app.playbook.intraday_reentry import overnight_loser_exempt
        return bool(overnight_loser_exempt(symbol, entry_time_str, profit_pct,
                                           is_reentry=is_reentry))
    except Exception:
        return False


def overnight_eod_decision(symbol: str, source_type: str, entry_time_str: str,
                           profit_pct: float, is_reentry=None):
    """EOD 隔夜豁免唯一决策点 → (exempt: bool, track: str | None).

    评估顺序与合并前两处决策面逐字一致: 轨③ reentry 首夜先判, 不中再判 W_WEEK。
    每层独立异常收口 (轨③异常 → 继续判 W_WEEK, 与合并前链式行为一致)。

    track ∈ {"reentry_first_night", "w_week", None} — 台账标签, 与 RFC-067
    overnight_arm.source 既有值逐字一致 (sweeper 豁免打点直接透传)。

    is_reentry: 可注入的 (symbol, date_str)->bool, 默认 None = 查库; 单测传桩。
    """
    try:
        if reentry_first_night_exempt(symbol, entry_time_str, profit_pct,
                                      is_reentry=is_reentry):
            return True, "reentry_first_night"
    except Exception as _e3:
        # 兜底层 (内层 reentry_first_night_exempt 已全量收口): 轨③异常 → 落 W_WEEK,
        # 与合并前链式行为逐字一致 (R5 留痕, 不吃 except:pass ratchet 额度)
        logger.debug(f"[overnight_decision] 轨③判定异常 {symbol} (继续判 W_WEEK): {_e3}")
    try:
        if w_week_loser_exempt(source_type, entry_time_str, profit_pct):
            return True, "w_week"
    except Exception as _ew:
        logger.debug(f"[overnight_decision] W_WEEK 判定异常 {symbol} (不豁免): {_ew}")
    return False, None


def overnight_eod_exempt(symbol: str, source_type: str, entry_time_str: str,
                         profit_pct: float, is_reentry=None) -> bool:
    """bool 投影 — poll 侧 PRE_CLOSE 只需豁免与否, 不需要台账标签 (单次求值)。"""
    return overnight_eod_decision(symbol, source_type, entry_time_str, profit_pct,
                                  is_reentry=is_reentry)[0]
