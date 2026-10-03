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
from datetime import date

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
      2. PLAYBOOK_SIM / PLAYBOOK_LIVE 同语义 (2026-10-03 用户裁「sim和实盘策略一致」:
         删原 SIM-only 门 — LIVE 臂继承同一策略语义; 执行安全层 (broker STOP/PDT/
         购买力门) 不属策略语义, 保持 LIVE 特有不动。SHADOW/其他源仍不豁免);
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
        if source_type not in ("PLAYBOOK_SIM", "PLAYBOOK_LIVE"):
            # 2026-10-03 用户裁「sim和实盘策略一致」: 原 `!= "PLAYBOOK_SIM"` 单臂门
            # 改为双臂白名单 — LIVE 臂继承同一 W_WEEK 语义 (水下仓多夜修复窗口)。
            # 零当前行为变更前提 (已实测): live 库 auto 臂 0 行 (trade_records 仅
            # MANUAL 12 / POSITION_SYNC 2, open_positions size>0 = 0), LIVE 臂从未
            # 运行 ⇒ 本改动只影响未来 armed 的 LIVE 臂, SIM 侧逐字不变。
            # SHADOW/MAIN_PIPELINE/ORB 仍不豁免 (非 playbook 双臂, 语义不同)。
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


# ── RFC-079 (2026-09-09): EOD 深水闸影子打点 (record-only, 零行为变更) ──────────
# 缘起: 8/1-9/8 隔夜仓回放 (24 笔) — EOD 收盘水深 ≤-2.5% 的豁免仓 3/3 开盘死亡
#   (8/27 UBER -$307 / 8/31 GAP -$387 / 9/8 UBER -$273), 反事实 EOD 平省 +$410,
#   误杀修复赢家 0; 浅水豁免仓修复率 69% (机制保留)。按 PROPOSAL-20260906B P3
#   修正设计 (§0.5): 新建独立键 eod_depth_gate_pct (不复用 8/21 已裁决回滚的
#   loser_overnight_max_depth_pct=99 键), 仅 EOD 判定时刻查水深, 盘中豁免链不查。
# 本函数 = 影子第一步: 豁免成立后记录决策时刻水深 + would_revoke 标志,
#   次日结算 scripts/settle_eod_depth_gate_shadow.py 算 Δ (反事实 EOD 平 vs 实际结局)。
# 预注册判据 (判据字典 B19): 触发结算 n≥8 ∧ ΣΔ>0 ∧ 误杀赢家≤1 → 提请转硬闸 (另裁)。
# 回滚: eod_depth_gate_shadow_enabled=false (热重载可读, 无需重启)。
import json as _edg_json
import os as _edg_os
from pathlib import Path as _EdgPath

_EDG_SHADOW_PATH = _EdgPath(__file__).resolve().parents[2] / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"


def _anchor_int(v):
    """身份锚 trade_ref 安全转换 (RFC-095): 无法转 int → None, 绝不让打点整行丢失。

    度量连续性优先于锚纯度: 锚坏了退化为 None (结算侧再退化到 entry_time/价量锚),
    而不是因为一个类型错误把该笔豁免记录整行吞掉。
    """
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_earnings_phase(time_of_day):
    """日历相位 → 'BMO' / 'AMC' / 'UNKNOWN'。

    与 scripts/extended_hours_entry_executor._parse_earnings_phase **同口径**
    (该函数是全仓财报相位单一真源, tests/scripts/test_eth_event_plan.py:535
    契约钉住)。此处内联而非 import: app/ 不得反向依赖 scripts/。

    为何必需: 富途源 (earnings_calendar.py:539 entry.get("timeOfTheDay")) 的
    生产真实值域是 'pre-market' / 'post-market' / ''，**不是** 'BMO'/'AMC' 字面量。
    旧实现直接 .strip().upper() 比较 → 两分支永不命中 → is_earnings_night 恒
    False (实测 19/19 只真实财报票全 False = 结构性死字段, DEB-20260929 §14.1)。
    空/双相/不可识别 → 'UNKNOWN'，调用方 fail-closed 不当财报夜。
    """
    phase = str(time_of_day or "").lower()
    before = "before" in phase or "pre" in phase or "bmo" in phase
    after = "after" in phase or "post" in phase or "amc" in phase
    if before and not after:
        return "BMO"
    if after and not before:
        return "AMC"
    return "UNKNOWN"


def _earnings_night_info(symbol, earnings_guard, arm_date):
    """Return (is_earnings_night, report_date, time_of_day), or Nones when unavailable.

    Risk night is the overnight session that ends after the event:
    BMO on the next trading day, or AMC on the decision day. The field is
    record-only; calendar unavailability degrades to null instead of blocking.
    """
    if earnings_guard is None:
        return None, None, None
    info = getattr(earnings_guard, "_earnings_symbols", {}).get(symbol)
    if not info:
        return False, None, None
    report_date = str(info.get("report_date") or "")
    # §14.1: 归一到单一真源口径, 不再假设上游给 'BMO'/'AMC' 字面量
    time_of_day = _normalize_earnings_phase(info.get("time_of_day"))
    if len(report_date) != 10 or len(str(arm_date)) != 10:
        return None, None, time_of_day
    try:
        from app.utils.holiday_calendar import nth_trading_day_after
        next_trading_date = nth_trading_day_after(
            "US", date.fromisoformat(str(arm_date)), 1)
        next_trading_date = next_trading_date.isoformat() if next_trading_date else None
    except ValueError:
        return None, None, time_of_day
    if time_of_day == "BMO":
        return report_date == next_trading_date, report_date, time_of_day
    if time_of_day == "AMC":
        return report_date == str(arm_date), report_date, time_of_day
    return False, report_date, time_of_day


def _edg_pytest_guard() -> bool:
    """pytest 哨兵 (先例 RFC-074 winner shadow): 单测触发豁免路径不得污染生产账本。
    独立函数以便单测 monkeypatch (pytest 会在 call 阶段重设 PYTEST_CURRENT_TEST,
    fixture 里 delenv 拦不住)。生产判定不读测试注入。"""
    return bool(_edg_os.environ.get("PYTEST_CURRENT_TEST"))


def emit_eod_depth_gate_shadow(decision_point: str, symbol: str, direction: str,
                               source_type: str, entry_price: float, qty,
                               entry_time_str: str, decision_price: float,
                               profit_pct: float, track=None, trade_id=None,
                               earnings_guard=None) -> None:
    """EOD 豁免成立后的深水闸影子打点。任何异常 → debug 日志，绝不影响豁免主链。

    Args:
        decision_point: "poll_pre_close" (规则[3] 15:52) / "sweeper" (EOD 兜底)
        profit_pct: 决策时刻净盈亏% (与豁免判定同口径 _profit_net_pct)
        track: 豁免臂标签 (sweeper 传 overnight_arm.source; poll 侧无标签传 None)
        trade_id: **身份锚** (RFC-095, 2026-09-22) = 该仓在 trade_records.id。
            None (旧调用方未传) → 行内 trade_ref 为空, 结算侧退化到 entry_time/价量锚。
    去重键 = (arm_date_et, symbol): 同日 poll+sweeper 对同一仓双评估只落首行。
    """
    try:
        if _edg_pytest_guard():
            return  # pytest 隔离 (先例 RFC-074 winner shadow): 防单测污染生产账本
        from app.playbook.config import get_playbook_config
        _cfg = get_playbook_config()
        if not bool(_cfg.get("eod_depth_gate_shadow_enabled", False)):
            return
        _gate_pct = float(_cfg.get("eod_depth_gate_pct", 2.5))
        from app.utils.timezone import now_et as _edg_now
        _now = _edg_now()
        _arm_date = _now.strftime("%Y-%m-%d")
        # 持久化去重 (跨进程重启): 读既有行键 — 文件量级 <10行/周, 全读可接受
        _seen = set()
        if _EDG_SHADOW_PATH.exists():
            with open(_EDG_SHADOW_PATH) as _f:
                for _ln in _f:
                    try:
                        _r = _edg_json.loads(_ln)
                        _seen.add((_r.get("arm_date"), _r.get("symbol")))
                    except Exception as _parse_err:
                        # R5 (2026-09-09 棘轮清剿): 坏行跳过但必须留痕 — 静默 = 去重失效不可见
                        logger.warning(f"[EOD深水闸] 影子档坏行跳过: {_parse_err} | {_ln[:120]!r}")
        if (_arm_date, symbol) in _seen:
            return
        _entry_date = ""
        try:
            from datetime import datetime as _edg_dt
            from app.core.scheduler_triggers import ET as _EDG_ET
            _et = _edg_dt.fromisoformat(str(entry_time_str))
            if _et.tzinfo is not None:
                _et = _et.astimezone(_EDG_ET)
            _entry_date = _et.strftime("%Y-%m-%d")
        except Exception:
            _entry_date = str(entry_time_str or "")[:10]
        _earnings_night, _earnings_report_date, _earnings_time = _earnings_night_info(
            symbol, earnings_guard, _arm_date)
        _row = {
            "shadow": "eod_depth_gate",
            "ts": _now.isoformat(),
            "arm_date": _arm_date,
            "symbol": symbol,
            "direction": direction,
            "source_type": source_type,
            "track": track,
            "decision_point": decision_point,
            # ── RFC-095 (2026-09-22) 身份锚 ──────────────────────────────────
            #   结算侧必须能认出"这个 arm 行对应哪一笔仓"。旧结算按
            #   symbol+trade_date 取"最新已平"会配到同日另一笔 (实测 US.MU
            #   1503 被配成 1501) 并永久冻结。两个锚: trade_ref = trade_records.id
            #   (单位=整数行号, 最强); entry_time = 该行 entry_time 原样透传
            #   (单位=ISO 时间串, 三格式混存不在此解析, 仅做逐字比对)。
            "trade_ref": _anchor_int(trade_id),
            "entry_time": str(entry_time_str or ""),
            "entry_price": float(entry_price or 0),
            "entry_date_et": _entry_date,
            "qty": float(qty or 0),
            "decision_price": float(decision_price or 0),
            "depth_net_pct": round(float(profit_pct), 4) if profit_pct is not None else None,
            "gate_pct": _gate_pct,
            "would_revoke": bool(profit_pct is not None and float(profit_pct) <= -_gate_pct),
            "is_earnings_night": _earnings_night,
            "earnings_report_date": _earnings_report_date,
            "earnings_time_of_day": _earnings_time,
            # ⚠️ §14.4 注记: 当前是 is_earnings_night 的**纯别名**(不含深水条件),
            #   与上方 would_revoke(=profit_pct≤-gate_pct)不是同一判据。名字易被误读为
            #   "财报夜 AND 该撤销"。结算侧 settle_eod_depth_gate_shadow.py **不消费**此字段
            #   (grep 全仓仅测试断言它), 故保留别名以免撞既有契约; 若将来结算侧要做
            #   "财报夜∩深水"分轴, 届时再补 AND would_revoke 逻辑并同步测试。
            "earnings_night_would_revoke": _earnings_night,
        }
        _EDG_SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_EDG_SHADOW_PATH, "a") as _f:
            _f.write(_edg_json.dumps(_row, ensure_ascii=False) + "\n")
        if _row["would_revoke"]:
            logger.info(
                f"[shadow·eod_depth_gate] {symbol} {decision_point} 水深{_row['depth_net_pct']:.2f}% "
                f"≤-{_gate_pct}% → would_revoke (反事实EOD平, record-only 真仓照常豁免)")
    except Exception as _edg_e:
        logger.debug(f"[shadow·eod_depth_gate] 打点跳过 {symbol}: {_edg_e}")
