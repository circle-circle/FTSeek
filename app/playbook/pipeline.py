"""
Playbook 端到端编排管线 — 把 Layer 0→1→2→3 串联通电。

本模块是整个 Trading Playbook System 唯一缺失的"接线层":
    - Phase 0/1/2 的 14 个模块都已实现且单测通过 (232 tests),
      但没有任何生产代码调用它们 — scheduler 不感知 playbook 的存在。
    - 本文件提供 3 个入口, 复用项目现有调度约定 (phase-polling + 每日/每周幂等标志):
        run_playbook_generate(scheduler)   盘前  regime→收集→筛选→VotingCoordinator→入库→飞书
        run_playbook_track(scheduler)      盘后  shadow追踪→因子更新→飞书
        run_playbook_weekly(scheduler)      周五  双层因子分析→权重闭环→周报→飞书

设计原则 (与现有 run_premarket_planning / check_daily_review 对齐):
    1. 每个入口自带节假日跳过 (is_trading_day) + 每日/每周幂等 guard
    2. 单只标的/单步异常不中断整体流程 (silent_except 风格)
    3. 不引入新的 cron daemon — 完全融入 scheduler._run_llm_loop 的 phase 分发
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 2026-08-05: bar 覆盖冻结告警的模块级计时器 (symbol -> stall_since_ts).
#   meta 每轮从 DB 重读且写回有条件门, 计时不可放 meta; 按 symbol 记 (同日重复开仓重置无害).
_BAR_STALL_SINCE: Dict[str, float] = {}

# 2026-08-06 (DEB-20260806 终裁): 锚限价影子幂等集 ((inst_id, date) — 每指令触发只落一行).
_LIMIT_SHADOW_EMITTED: set = set()

# 2026-08-06 (B1 二次修复): 盘口 micro 拉取的"已告警"集 ((symbol, date) 防刷屏).
#   首修 (08-05) 用 get_push_orderbook 推送缓存 — 但 ADR-013 早已把 ORDER_BOOK 移出推送
#   订阅集 (subscription_manager 默认 QUOTE+K_1M), 缓存恒空 → 08-05 全天 233 行 micro=null.
#   改用 S2 实测可行的拉取路径 (scripts/orderbook_s2_capture.py 同款):
#   无推送订阅 (subscribe_push=False) → get_order_book 拉取 → 即退订.
#   零推送 CPU (ADR-013 结论不动), 零订阅槽位累积; 频次 ≈影子行数 (~233/日), 远低于限额.
_OB_PULL_WARNED: set = set()

# 2026-08-09 (放量前性能预审): micro 拉取 60s/symbol 缓存 — 同一评估轮内
#   entry_timing/limit_entry/再入场 多路径可能各拉一次 (每次=一次完整 OpenD 连接,
#   实测 ~300ms), 缓存后同轮共享一次拉取, 且同轮数据一致. None 也短缓存 30s
#   (OpenD 拒答时防每轮重锤). 触发轮间隔 300s ≫ TTL, 不牺牲新鲜度.
_L1_MICRO_CACHE: Dict[str, tuple] = {}   # symbol -> (ts, value_or_None)
_L1_MICRO_TTL = 60.0
_L1_MICRO_NEG_TTL = 30.0


def _fetch_l1_micro(scheduler, symbol: str) -> Optional[Dict[str, Any]]:
    """带 60s 缓存的 L1 盘口快照入口 (所有调用方共用)."""
    import time as _t
    _hit = _L1_MICRO_CACHE.get(symbol)
    if _hit:
        _ts, _val = _hit
        _ttl = _L1_MICRO_TTL if _val is not None else _L1_MICRO_NEG_TTL
        if _t.time() - _ts < _ttl:
            return _val
    _val = _fetch_l1_micro_uncached(scheduler, symbol)
    _L1_MICRO_CACHE[symbol] = (_t.time(), _val)
    return _val


def _fetch_l1_micro_uncached(scheduler, symbol: str) -> Optional[Dict[str, Any]]:
    """触发时点 L1 盘口快照 (bid1/ask1/量/spread_bps)。失败/无数据返回 None。

    2026-08-07 终修 (用户接管): 弃用主进程共享 _quote_ctx 拉盘口 — 实盘观测其
    ORDER_BOOK 订阅被 OpenD 拒 ("未知股票", diag 显示连接健康/push 新鲜/44 订阅,
    多次重启+force_reconnect 均不恢复), 而独立 OpenQuoteContext 全天拉取成功
    (S2 脚本 30min 一次持续落盘 + 独立进程 7/7 实测)。08-06 注释宣称 "S2 同款"
    但实现实际用了共享 ctx, 与 S2 的独立连接不符 — 本次改为真正同款。

    走独立临时连接 (scripts/orderbook_s2_capture.py 同款): 端口探测→建连→
    get_global_state 验证→subscribe(ORDER_BOOK, pull)→get_order_book→退订→关闭。
    零订阅槽位累积 (瞬态连接), 不依赖主连接状态, 不新增推送订阅 (ADR-013 约束
    不动)。裸 ctx.get_order_book (dict: Bid/Ask=[(price,vol,n,{})...]) — 不用
    provider.get_order_book 包装: 其按 DataFrame 取值, 现 SDK 返回 dict → 恒 None
    (2026-05 起无消费者的隐性损坏死代码, 本函数不依赖它)。

    scheduler 参数保留兼容 (不再使用)。
    """
    try:
        import os as _os
        import socket as _socket
        try:
            import futu.common.ft_logger as _ftl
            _ftl.logger.file_level = logging.WARNING
            _ftl.logger.console_level = logging.WARNING
        except Exception:
            pass
        from futu import OpenQuoteContext as _OQC, SubType as _ST, SysConfig as _SC
        _SC.enable_proto_encrypt(False)
        _host = _os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        _port = int(_os.getenv("FUTU_OPEND_PORT", "11111"))
        # 2026-08-07 修复 (盘中实锤): 生产链路 symbol 恒带 US. 前缀 (trade_records/指令),
        #   直接拼 f"US.{symbol}" → "US.US.ORCL" 被 OpenD 拒 "未知股票" → micro 恒 None
        #   (昨日 233 行 + 今日全部路径的真根因; 此前独立进程测试用净码故通过).
        #   折叠前导 US. 前缀后再拼 (与 normalize_symbol 同语义).
        _sym = str(symbol)
        while _sym.upper().startswith("US."):
            _sym = _sym[3:]
        _code = f"US.{_sym}"

        # 端口探测 (S2 同款): OpenD 不可达快速失败, 避免 OpenQuoteContext 无限阻塞
        _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _sock.settimeout(2.0)
        _port_ok = _sock.connect_ex((_host, _port)) == 0
        _sock.close()
        if not _port_ok:
            return None

        _ctx = _OQC(host=_host, port=_port)
        try:
            _r0, _ = _ctx.get_global_state()
            if _r0 != 0:
                return None
            _ret, _msg = _ctx.subscribe([_code], [_ST.ORDER_BOOK], subscribe_push=False)
            if _ret != 0:
                raise RuntimeError(f"ORDER_BOOK 订阅被拒: {_msg}")
            try:
                _r2, _ob = _ctx.get_order_book(_code, num=1)
            finally:
                try:
                    _ctx.unsubscribe([_code], [_ST.ORDER_BOOK])
                except Exception:
                    pass
            if _r2 != 0 or not isinstance(_ob, dict):
                return None
            _bids, _asks = _ob.get("Bid") or [], _ob.get("Ask") or []
            if not _bids or not _asks:
                return None
            _b1, _bv1 = float(_bids[0][0] or 0), float(_bids[0][1] or 0)
            _a1, _av1 = float(_asks[0][0] or 0), float(_asks[0][1] or 0)
            if _b1 <= 0 or _a1 <= 0:
                return None
            # 2026-08-07: 交叉盘/陈旧报价守卫 — ask≤bid 的负 spread (实测 AVGO -123.84bps)
            #   不是流动性紧缩信号, 是快市瞬时交叉/数据毛刺; 喂给门会误判"极窄"放行 → 弃测.
            if _a1 <= _b1:
                return None
            return {
                "bid1": _b1, "ask1": _a1, "bid_vol1": _bv1, "ask_vol1": _av1,
                "spread_bps": round((_a1 - _b1) / ((_a1 + _b1) / 2.0) * 10000.0, 2),
                "quote_src": "futu_order_book_pull",
            }
        finally:
            try:
                _ctx.close()
            except Exception:
                pass
    except Exception as _e:
        _wk = (symbol, _today_et_str())
        if _wk not in _OB_PULL_WARNED:
            _OB_PULL_WARNED.add(_wk)
            logger.warning(f"[playbook] {symbol} 盘口micro拉取失败 (本日不再告警, micro=None): {_e}")
        return None

def _reentry_overnight_exempt(symbol: str, entry_time_str: str, profit_pct: float) -> bool:
    """再入场水下仓 EOD 豁免 (2026-08-06 用户裁决"再入场亏损可隔夜").

    判定本体在 app.playbook.intraday_reentry.overnight_loser_exempt
    (开关/水下/首夜/decision_id 前缀四条件)。此处仅做异常收口:
    任何异常 → False (不豁免, 照常当日平仓 — 保守方向)。
    """
    try:
        from app.playbook.intraday_reentry import overnight_loser_exempt
        return bool(overnight_loser_exempt(symbol, entry_time_str, profit_pct))
    except Exception:
        return False


# 2026-08-07 (用户指示, sim A/B 双开): VWAP 门 wrong_side 拦截实验分流.
#   证据 (DEB-20260806-source-control 修尺子后): 被拦 n=55 均值 +0.254% (SHORT +0.649%),
#   门倾向"扼杀"。影子反事实已可测 (gated 臂轨迹在 entry_timing_shadow 持续轮询),
#   但"真实成交口径 (费用/滑点/真实退出) 下是否仍成立"只能 A/B 双开实测 (SIM 无真金/无 PDT).
#   分臂纯函数日内稳定: 同标的当日恒定同臂, 不随 300s 轮询漂移, 进程重启不变.
_VWAP_AB_LOGGED: set = set()


def _vwap_ab_row_exists(date_str: str, symbol: str, direction: str) -> bool:
    """台账文件级去重 (2026-08-07 主动审计 P1-4 修复): _VWAP_AB_LOGGED 是进程内
    set, 进程重启(systemd 重启/cron 重跑)后清空 → 同 (symbol,date,direction) 重复
    append, 破坏"每 symbol-day-direction 一行"的台账唯一性承诺, A/B 统计重复计数。
    写前查文件历史行, 与进程 set 双保险。异常 → False (允许写, 审计降级优于丢行)."""
    from pathlib import Path as _Pab
    _f = _Pab(__file__).resolve().parents[2] / "data" / "vwap_gate_ab.jsonl"
    try:
        if not _f.exists():
            return False
        import json as _jab
        with open(_f, "r", encoding="utf-8") as _fh:
            for _line in _fh:
                try:
                    _j = _jab.loads(_line)
                except Exception:
                    continue
                if (_j.get("date") == date_str and _j.get("symbol") == symbol
                        and _j.get("direction") == direction):
                    return True
    except Exception:
        return False
    return False


def _vwap_ab_arm(symbol: str, date_str: str, direction: str) -> str:
    """确定性 50/50 分臂 (纯函数, 可单测): sha1 奇偶 → 'bypass' | 'gated'."""
    import hashlib
    h = hashlib.sha1(f"{date_str}|{symbol}|{direction}".encode()).hexdigest()
    return "bypass" if int(h, 16) % 2 == 0 else "gated"


def _vwap_gate_ab_bypass(symbol: str, is_short: bool, vwap_dev, vwap_val,
                         current_price, inst_id) -> bool:
    """VWAP 门 A/B 分流判定: 仅 sim + 开关开 + bypass 臂 → True (放行).

    双臂首次判定即落实验台账 data/vwap_gate_ab.jsonl (每 symbol-day-direction 一行);
    任何异常 → False (维持拦截, 即现状, 保守方向)。
    """
    try:
        # 2026-08-07 复核修复 (缺陷A): 开关读取改走 PlaybookConfig 扁平合并语义 —
        #   yaml 键位于 playbook.execution_parameters.vwap_gate_ab_enabled,
        #   get_dotted 读原始嵌套取不到 (恒 False → 实验从未生效, 实证).
        #   与既有 pb_cfg.get("entry_vwap_gate_enabled") 同语义; execute_mode 在
        #   playbook 顶层, 保留 get_dotted.
        from app.playbook.config import get_playbook_config as _gpc_ab
        if not bool(_gpc_ab().get("vwap_gate_ab_enabled", False)):
            return False
        from app.core.config_container import get_config as _gc_ab
        if _gc_ab().get_dotted("playbook.execute_mode", "futu_simulate") != "futu_simulate":
            return False
        _d = _today_et_str()
        _dir = "SHORT" if is_short else "BUY"
        arm = _vwap_ab_arm(symbol, _d, _dir)
        _lk = (symbol, _d, _dir)
        if _lk not in _VWAP_AB_LOGGED and not _vwap_ab_row_exists(_d, symbol, _dir):
            import json as _jab
            from datetime import datetime as _dtab
            from pathlib import Path as _Pab
            _row = {
                "ts": _dtab.now(_now_et()).isoformat(timespec="seconds"),
                "date": _d, "symbol": symbol, "direction": _dir, "arm": arm,
                "price": current_price, "vwap": vwap_val,
                "vwap_dev": round(vwap_dev, 6) if vwap_dev is not None else None,
                "inst_id": inst_id, "rule": "vwap_gate_ab_50_50",
            }
            with open(_Pab(__file__).resolve().parents[2] / "data" / "vwap_gate_ab.jsonl",
                      "a", encoding="utf-8") as _fab:
                _fab.write(_jab.dumps(_row, ensure_ascii=False) + "\n")
            # 2026-08-07 复核修复 (缺陷B): 写盘成功后才入进程 set — 原顺序 add 在前,
            #   NameError/写盘异常时 key 已标记 → 台账行永久丢失且 bypass 臂延迟一轮放行.
            _VWAP_AB_LOGGED.add(_lk)
        return arm == "bypass"
    except Exception:
        return False


# 2026-08-09 (用户指示 sim 放量①, 影子组合): 被门拦下的"本会触发"单落影子入场台账,
#   盘后由 scripts/settle_shadow_portfolio.py 按生产退出栈结算 → 每道门贡献可测
#   (真成交组 vs 被拦组的净结果对照), 零行为变更. 每指令每日只记首次.
_SP_EMITTED: set = set()


def _shadow_portfolio_emit(inst, symbol, is_short, blocked_by, entry_path,
                           current_price, entry_price, vwap_dev=None):
    """影子组合入场登记 (record-only). 任何异常静默."""
    try:
        from app.core.config_container import get_config as _gc_sp
        if not bool(_gc_sp().get_dotted("playbook.shadow_portfolio_enabled", True)):
            return
        if "TEST" in str(symbol).upper():
            return
        _d = _today_et_str()
        _lk = (inst.get("id"), _d)
        if _lk in _SP_EMITTED:
            return
        # 2026-08-09 性能/幂等预审: 进程内 set 重启即清空, 重启后同 (inst_id,date)
        #   可能重记一行 → 结算端虽按 inst_id 去重, 但文件冗余行会污染「行数≈样本量」的
        #   直观统计. 落盘前查文件兜底 (仅启动后首行触发, 每行一次 grep 成本可忽略),
        #   文件已有同键 → 只入 set 不重写.
        try:
            import json as _jsp_dup
            from pathlib import Path as _Pdup
            _sp_file = _Pdup(__file__).resolve().parents[2] / "data" / "shadow_portfolio_entries.jsonl"
            if _sp_file.exists():
                _dup = False
                with open(_sp_file, "r", encoding="utf-8") as _fdup:
                    for _line in _fdup:
                        try:
                            _r = _jsp_dup.loads(_line)
                            if _r.get("inst_id") == inst.get("id") and _r.get("date") == _d:
                                _dup = True
                                break
                        except Exception:
                            continue
                if _dup:
                    _SP_EMITTED.add(_lk)
                    return
        except Exception:
            pass  # 文件不可读时退化为仅进程内 set 去重
        _SP_EMITTED.add(_lk)
        import json as _jsp
        from datetime import datetime as _dtsp
        from pathlib import Path as _Psp
        _row = {
            "ts": _dtsp.now(_now_et()).isoformat(timespec="seconds"),
            "date": _d, "schema": "v1",
            "inst_id": inst.get("id"), "decision_id": inst.get("decision_id"),
            "symbol": symbol, "direction": "SHORT" if is_short else "LONG",
            "blocked_by": blocked_by, "entry_path": entry_path,
            "shadow_entry_price": round(float(current_price or 0), 4),
            "anchor": round(float(entry_price or 0), 4),
            "inst_stop_loss": inst.get("stop_loss"), "inst_take_profit": inst.get("take_profit"),
            "position_size": inst.get("position_size"),
            "confidence": inst.get("confidence"),
            "vwap_dev": round(vwap_dev, 6) if vwap_dev is not None else None,
        }
        with open(_Psp(__file__).resolve().parents[2] / "data" / "shadow_portfolio_entries.jsonl",
                  "a", encoding="utf-8") as _fsp:
            _fsp.write(_jsp.dumps(_row, ensure_ascii=False) + "\n")
        logger.info(f"[影子组合] {symbol} {'SHORT' if is_short else 'LONG'} "
                    f"被拦入场已登记 (blocked_by={blocked_by}, px=${current_price:.2f})")
    except Exception:
        pass

# ── ADR-008 (2026-06-28): Playbook 方向审计日志 ────────────────────────

def _log_playbook_direction_audit(symbol: str, llm_direction: str, technical_direction: Optional[str], vote_result: Dict[str, Any]):
    """记录 Playbook 方向决策审计日志 (JSONL)"""
    try:
        import os
        from datetime import datetime
        from app.core.config_container import get_config
        _cfg = get_config()
        log_path = str(_cfg.get_dotted("playbook.technical_arbitration.shadow_log_path", "data/shadow/playbook_direction_audit.jsonl"))
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except Exception:
            pass
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "llm_direction": llm_direction,
            "technical_direction": technical_direction or "",
            "llm_strength": int(vote_result.get("final_strength", 50) or 50),
            "llm_confidence": float(vote_result.get("final_confidence", 0) or 0),
            "llm_std": float(vote_result.get("std_dev", 0) or 0),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[playbook] 方向审计日志失败: {e}")


# ── 模块级单例: 避免每只候选股票都新建 DB 连接 ─────────────────────────
_horizon_selector: Optional[Any] = None


def _get_horizon_selector() -> Any:
    """获取 HorizonSelector 单例 (lazy init)"""
    global _horizon_selector
    if _horizon_selector is None:
        from app.playbook.horizon_selector import HorizonSelector
        _horizon_selector = HorizonSelector()
    return _horizon_selector


# ── 时间/节假日 (复用项目既有约定) ──────────────────────────────────────
ET = None  # lazy


def _now_et():
    global ET
    if ET is None:
        from app.core.scheduler_triggers import ET as _et
        ET = _et
    return ET


_low_atr_logged: set = set()


def _today_et_str() -> str:
    from datetime import datetime
    return datetime.now(_now_et()).date().isoformat()


def _is_trading_day_today() -> bool:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        from datetime import datetime
        cal = get_holiday_calendar()
        return cal.is_trading_day("US", datetime.now(_now_et()).date())
    except Exception as e:
        # 节假日日历不可用时按"是交易日"处理 (与 run_premarket_planning 一致, 宁可跑不可漏)
        logger.debug(f"[playbook] 节假日检查异常, 按交易日处理: {e}")
        return True


def _cohort_b_max_hold_hit(entry_time_str: str) -> bool:
    """RFC-061: cohort B 持仓是否已到 ≥5 交易日 (MAX_HOLD_T5 触发).

    用交易日历 (nth_trading_day_after) 算 entry_date + 5 交易日, 遇假期自动跳过
    (如 Juneteenth). 比日历日准. now 用 ET 日期. 异常时返回 False (不强平, 保守).
    """
    if not entry_time_str:
        return False
    try:
        from datetime import datetime
        from app.utils.holiday_calendar import nth_trading_day_after
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        # entry_time 可能 naive (CST 服务器存), 取 date; 若 tz-aware 转 ET
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        deadline = nth_trading_day_after("US", _entry_date, 5)
        if deadline is None:
            return False
        return datetime.now(_et).date() >= deadline
    except Exception as e:
        logger.debug(f"[playbook·RFC-061] MAX_HOLD_T5 日期计算异常 (不强平): {e}")
        return False


def _is_overnight_position(entry_time_str: str) -> bool:
    """RFC-061 #6: 仓位是否隔夜 (entry 在之前的交易日, 非今日入场).

    用于隔夜跳空止损: cohort B 持夜仓次日开盘若跳空 >3% 快平 (OVERNIGHT_GAP_STOP).
    用 ET 日期判断. 异常返回 False (不触发, 保守).
    """
    if not entry_time_str:
        return False
    try:
        from datetime import datetime
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        return _entry_date < datetime.now(_et).date()
    except Exception:
        return False


def _playbook_enabled() -> bool:
    """总闸门: 读取 config playbook.enabled (默认 False)。
    关闭时三个 pipeline 入口全部早退, 不调 Provider/LLM/DB/飞书。
    这是 shadow 上线的安全开关 — 确认就绪前保持 false。"""
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted("playbook.enabled", False))
    except Exception:
        # 配置不可用时默认关闭 (上线安全优先, 宁可漏跑不可误跑)
        return False


def _playbook_cfg(key: str, default=None):
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(f"playbook.{key}", default)
    except Exception:
        return default


# ── 依赖装配 (lazy, 单步异常隔离) ──────────────────────────────────────
def _build_data_collector():
    """构造 PlaybookDataCollector, 复用项目既有 Provider"""
    from app.playbook.data_collector import PlaybookDataCollector
    from app.data.providers.fundamental_provider import FundamentalProvider
    from app.data.finnhub_provider import FinnhubProvider
    from app.data.sec_form4_provider import SECForm4Provider
    from app.data.earnings_calendar import EarningsCalendar

    # FutuExtendedProvider (可选 — OpenD 10.7+ 新增 API)
    futu_ext = None
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        futu_ext = FutuExtendedProvider()
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"FutuExtendedProvider init skipped: {e}")

    return PlaybookDataCollector(
        fundamental_provider=FundamentalProvider(),
        finnhub_provider=FinnhubProvider(),
        insider_provider=SECForm4Provider(),
        earnings_calendar=EarningsCalendar(),
        futu_extended=futu_ext,
    )


def _get_watchlist(scheduler) -> List[str]:
    """从 scheduler.watchlist 取候选 + 合并 factor_engine 动态池.
    
    动态池来源 (DEB-20260629):
      - daily_active_pool.json (持仓 + 手动添加)
      - dragonhead_pool (行业龙头, 按 regime 切换)  
      - Finnhub 异动扫描 (盘前 >2% 涨跌幅)
      - factor_engine 质量门禁 (过滤低流动性)
    
    纯加法: 静态 watchlist 保留不变, 动态池只增不减.
    若 factor_engine 不可用, fallback 到纯静态 watchlist.
    """
    wl = list(getattr(scheduler, "watchlist", []) or [])
    
    # 2026-07-02: 修复 US.US.X 双重前缀 (factor_engine对已含US.前缀的symbol重复添加)
    wl = [s[3:] if s.startswith("US.US.") else s for s in wl]
    
    # 2026-06-30: 过滤 #SIM 后缀 (SIM账户持仓不进入pipeline)
    wl = [s for s in wl if '#' not in s]
    
    try:
        from app.plan.factor_engine import _get_stock_pool
        dynamic_pool, meta = _get_stock_pool()
        if dynamic_pool:
            merged = list(set(wl) | set(dynamic_pool))
            # 双保险: 合并后也过滤#SIM + 修复US.US.双前缀
            merged = [s[3:] if s.startswith("US.US.") else s for s in merged]
            merged = [s for s in merged if '#' not in s]
            logger.info(
                f"[playbook] watchlist 合并动态池: "
                f"static={len(wl)} + dynamic={len(dynamic_pool)} "
                f"(overlap={len(set(wl) & set(dynamic_pool))}) → merged={len(merged)} | "
                f"dynamic_meta={meta.get('final_pool_size', '?')}"
            )
            return sorted(merged)
    except Exception as e:
        logger.debug(f"[playbook] 动态池合并失败, 使用静态 watchlist: {e}")
    
    return wl


def _get_positions_as_dicts(scheduler) -> List[Dict[str, Any]]:
    """把 position_store 的 Position 对象转成 screener 期望的 dict(symbol, sector, direction)"""
    out: List[Dict[str, Any]] = []
    try:
        store = getattr(scheduler, "position_store", None)
        if not store:
            return out
        positions = store.all() if hasattr(store, "all") else []
        for p in positions:
            sym = getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else None)
            if not sym:
                continue
            sector = getattr(p, "sector", None) or getattr(p, "industry", None) or \
                (p.get("sector") or p.get("industry") if isinstance(p, dict) else None)
            direction = getattr(p, "direction", None) or \
                (p.get("direction") if isinstance(p, dict) else None)
            out.append({"symbol": sym, "sector": sector, "direction": str(direction) if direction else ""})
    except Exception as e:
        logger.debug(f"[playbook] 持仓读取失败 (行业集中度检查将降级): {e}")
    return out


def _get_total_equity(scheduler) -> float:
    """复用盘前计划的 equity 取数逻辑"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cs = get_capital_safety_system()
        if cs._state and cs._state.current_equity:
            return float(cs._state.current_equity)
    except Exception:
        pass
    try:
        from app.core.config_container import get_config
        return float(get_config().get_dotted("capital.fixed_amount", 82_000))
    except Exception:
        return 100_000.0


def _resolve_sizing_base(pb_cfg=None) -> float:
    """R4 统一风险基数 (SSOT): 单笔风险 + 日名义额度共用同一基数, 消除双尺子。

    面向未来切实盘: 优先取富途服务端真实净值 (capital_safety.current_equity,
    由 accinfo_query 每 30s 刷新, 跟随 AUTO_TRADE_ACCOUNT 自动切 sim/real)。
    净值随实盘变化 → 单笔/日额度同步缩放, 亏损后基数自动缩小 = 天然防爆仓。
    仅在净值取不到时兜底到 config risk_base_capital (默认 82000), 不再作为主口径。
    """
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        if _cs._state and _cs._state.current_equity and _cs._state.current_equity > 0:
            return float(_cs._state.current_equity)
    except Exception:
        pass
    try:
        if pb_cfg is None:
            from app.playbook.config import get_playbook_config
            pb_cfg = get_playbook_config()
        return float(pb_cfg.get("risk_base_capital", 82000.0))
    except Exception:
        return 82000.0


# ── VotingCoordinator → Instruction 转换器 ─────────────────────────────
# Playbook 不再跑独立 LLM, 而是消费 VotingCoordinator 的多模型投票结果.
# 方向由投票决定 (LONG/SHORT/NEUTRAL), 数值由 compute_instruction_values 确定性计算.

def _get_voting_coordinator(scheduler):
    """从 scheduler 获取 voting_coordinator (BridgeVotingAdapter / TechnicalFallbackAdapter / NullVotingAdapter).

    2026-07-09: scheduler_core 初始化时根据 bridge 状态赋值
    (有 bridge → BridgeVotingAdapter; 无 bridge → Technical/Null)。
    2026-07-11 (双脑清理): scheduler.voting_coordinator 恒被赋值, 不再 fallback 到
    di_container 的真·3模型VotingCoordinator (已退休)。万一为 None → NullVotingAdapter
    (全 SKIP), 不复活噪声投票。
    """
    vc = getattr(scheduler, "voting_coordinator", None)
    if vc is not None:
        return vc
    logger.warning("[playbook] scheduler.voting_coordinator 为 None → NullVotingAdapter (全 SKIP)")
    return NullVotingAdapter()


class NullVotingAdapter:
    """空投票适配器 — bridge文件不存在时使用, 全部候选SKIP.

    2026-07-09: 替代VotingCoordinator作为bridge不存在时的fallback。
    根因: 3模型投票方向准确率47%≈随机, 用噪声投票替代信号不如不交易。
    deep_analysis skill负责盘前生成bridge文件, bridge不存在=今天没有高质量信号。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """始终返回None → pipeline将SKIP该候选。"""
        logger.info(f"[NullVoting] {symbol} SKIP — 无bridge文件, 不用噪声投票")
        return None

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — NullVotingAdapter 不写 voting_predictions 表."""
        pass


class TechnicalFallbackAdapter:
    """DA bridge 缺席时的确定性 fallback — 用 technical 纯函数策略出方向.

    2026-07-11 (双脑清理): bridge 不存在时替代 NullVotingAdapter 的“全 SKIP”。
    设计担当: DA = 入场决策脑; DA 缺席时用确定性、非 LLM、可单测的 technical
    策略兜底，而非噪声投票。

    ⚠ 陷阱护栏: 纪律/风控无法把“随机方向入场”变成正期望。所以 fallback 必须有
    确定性依据(technical)、独立归因(strategy_id=technical_fallback)、可关停
    (playbook.fallback_when_no_bridge=none 即回退全 SKIP)。与 DA 互斥 → 任一时刻单链。

    接口兼容: vote(symbol, ctx) → vote_result dict (与 BridgeVotingAdapter 对齐)。
    entry/SL/TP 不在此产出——与 DA 同路径, 由 compute_instruction_values(data) 确定性计算。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """跑 technical 纯策略出方向; 无方向 → None (SKIP, 不硬凑成交)。"""
        try:
            from app.strategy.pure import evaluate as eval_strategy
            # 2026-07-30 (topup 零产出真凶, 第四道暗门): yaml `strategies.technical.
            #   enabled: false` 是主线时代遗留开关 — evaluate() 入口即 return None,
            #   F1 修的 band 从未被执行 (07-29 带 F1 跑 4 轮仍 +0, 手算三只 strength=65
            #   本应过)。本 Adapter 两个消费方 (topup / 无 bridge 兜底) 设计上就该出
            #   方向 — 局部覆写 enabled, 其余参数 (min_strength 等) 仍从 yaml 读,
            #   调参保持单一真相源。回滚 = 关 playbook.topup.technical_override_enabled。
            _t_cfg = None
            try:
                from app.core.config_container import get_config as _tf_gc
                if bool(_tf_gc().get_dotted("playbook.topup.technical_override_enabled", False)):
                    from app.strategy.pure import _load_yaml_config as _tf_lyc
                    _t_cfg = dict(_tf_lyc("technical") or {})
                    _t_cfg["enabled"] = True
            except Exception:
                _t_cfg = None  # 覆写失败退回 yaml 行为 (现状 = 全 None)
            cand = eval_strategy("technical", ctx, config=_t_cfg)
        except Exception as e:
            # 2026-07-27 (topup 零产出诊断): debug→warning — 全史零方向产出而日志无声,
            #   静默失败路径必须可见 (静默退化军规)
            logger.warning(f"[TechnicalFallback] {symbol} technical 评估异常: {type(e).__name__}: {e}")
            return None
        if cand is None:
            logger.debug(f"[TechnicalFallback] {symbol} 无候选 (momentum NEUTRAL 或回调不达标)")
            return None
        direction = str(getattr(cand, "direction", "") or "").upper()
        if direction not in ("LONG", "SHORT"):
            return None
        strength = int(getattr(cand, "strength", 50) or 50)
        confidence = float(getattr(cand, "confidence", 0.5) or 0.5)
        logger.info(f"[TechnicalFallback] {symbol} technical 方向={direction} str={strength} — DA 缺席兜底")
        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": strength,
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": f"TFB-{symbol}-{int(time.time())}",
            "strategy_id": "technical_fallback",
            "conditions": list(getattr(cand, "signal_tags", []) or []),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — TechnicalFallbackAdapter 不写 voting_predictions 表."""
        pass


class BridgeVotingAdapter:
    """桥接适配器 — 从 opportunity_scan_YYYYMMDD.json 读取 deep_analysis 规划结果.

    替代 VotingCoordinator.vote()，消除 LLM 在 FTSeek 热路径。
    接口兼容: vote(symbol, ctx) → vote_result dict (同 VotingCoordinator 格式)。

    Ponytail: 一个类一个方法，不是新模块。
    """

    def __init__(self, candidates_by_symbol: Dict[str, Dict], trade_date: str):
        self._candidates = candidates_by_symbol
        self._trade_date = trade_date

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """返回 bridge 中的候选, 格式与 VotingCoordinator.vote() 对齐。"""
        cand = self._candidates.get(symbol)
        if cand is None:
            return None

        direction = str(cand.get("direction", "NEUTRAL")).upper()
        if direction not in ("LONG", "SHORT"):
            return None

        score = float(cand.get("score", 5.0))
        # 2026-07-10: confidence 从辩论结果合成 (唯一有预测力的信号, r=+0.102)
        # 不用 LLM 定性 confidence (r=-0.211, 负相关)
        # 不用 score/10 (同义反复, 无区分度)
        confidence = 0.50  # 中性基准

        debate = cand.get("debate", {})
        if debate:
            agreed = debate.get("challenger_agreed", False)
            chall_dir = str(debate.get("challenger_direction", "")).upper()
            outcome = cand.get("debate_outcome", "")

            if chall_dir == direction:
                if agreed or "AGREE" in outcome.upper():
                    confidence += 0.20  # 2模型一致确认
            else:
                confidence -= 0.15  # 方向分歧

        confidence = max(0.10, min(0.95, confidence))

        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": int(score * 10),
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": cand.get("decision_id", ""),
            "strategy_id": "deep_analysis",
            "position_suggestion": cand.get("position_suggestion", {}),
            "thesis": cand.get("thesis", ""),
            "conditions": cand.get("conditions", []),
            "entry_price": cand.get("entry_price", 0),
            "stop_loss": cand.get("stop_loss", 0),
            "take_profit": cand.get("take_profit", 0),
            "entry_strategy": cand.get("entry_strategy", ""),
            # F1 (2026-07-31): 透传 DA 锚偏离度 — 桥接 json 有则带上, 无则 None (record-only)
            "anchor_dev_pct": cand.get("anchor_dev_pct"),
            "invalidation": cand.get("invalidation", ""),
            "risks": cand.get("risks", []),
            "strategy_type": cand.get("strategy_type", ""),
            "position_size_hint": cand.get("position_size_hint", ""),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — bridge 模式不写 voting_predictions 表。"""
        pass


# ── 2026-07-19 (DEB-round3 #1): bridge 三态健康告警 + schema fail-safe ──
# 缺失/空候选/schema非法 → 飞书 HIGH 告警 (当日去重, 不阻断).
# 盲区: bridge 文件仅 scheduler 启动时 _try_load_bridge 加载一次, 运行期不重载 →
#   启动后 Hermes cron 失败/文件损坏, 系统一整天 0 决策而运维不知情.
# 告警函数直读文件, 独立于 adapter 状态, 在 run_generate / auto_execute 入口调用.
_bridge_alert_sent = set()


def _validate_bridge_schema(candidates) -> bool:
    """轻量 schema 校验: list[dict] 且每项含 symbol/direction/decision_id.

    fail-safe 语义: 非法 → 视作缺失 (走技术面降级), 不消费半损坏文件.
    """
    if not isinstance(candidates, list) or not candidates:
        return False
    for c in candidates:
        if not isinstance(c, dict):
            return False
        if not c.get("symbol") or not c.get("direction") or not c.get("decision_id"):
            return False
    return True


def _bridge_file_state(trade_date: str) -> str:
    """直读当日 bridge 文件 → ok / missing / empty / invalid (record-only, 不抛异常)."""
    try:
        import json as _json
        import os as _os
        _path = f"/opt/ftseek/FTSeek-US/data/opportunity_scan_{trade_date}.json"
        if not _os.path.exists(_path):
            return "missing"
        with open(_path) as _f:
            _data = _json.load(_f)
        _cands = _data.get("candidates", [])
        if not _cands:
            return "empty"
        if not _validate_bridge_schema(_cands):
            return "invalid"
        return "ok"
    except Exception:
        return "invalid"


def _check_bridge_health_and_alert(context: str) -> None:
    """交易日 ET≥09:00 检查当日 bridge 三态, 异常 → HIGH 告警 (当日去重, 不阻断)."""
    try:
        if not _is_trading_day_today():
            return
        _today = _today_et_str()
        from datetime import datetime as _dt
        _now = _dt.now(_now_et())
        if _now.hour < 9:
            return
        _state = _bridge_file_state(_today)
        if _state == "ok":
            return
        _key = f"{_today}:{_state}"
        if _key in _bridge_alert_sent:
            return
        _bridge_alert_sent.add(_key)
        _msg = {
            "missing": "bridge 文件缺失 (Hermes DA 未产出?)",
            "empty": "bridge 文件无候选 (0 candidates)",
            "invalid": "bridge 文件 schema 非法 (缺 symbol/direction/decision_id)",
        }.get(_state, _state)
        logger.error(
            f"[playbook] #1 bridge健康告警 ({context}): {_today} {_msg} → 决策降级, 请检查 DA 产出链"
        )
        try:
            from app.push import get_push_hub
            from app.push.message import Level as _PushLevel
            get_push_hub().push_text(
                f"🚨 [Playbook bridge] {_today} {_msg}. 决策链降级 (Bridge→Technical/Null), "
                f"今日可能 0 决策. 请检查 Hermes DA 产出链 (入口={context}).",
                level=_PushLevel.HIGH,
            )
        except Exception as _push_e:
            logger.warning(f"[playbook] #1 bridge告警推送失败: {_push_e}")
    except Exception as _bh_e:
        logger.warning(f"[playbook] #1 bridge健康检查异常 (不阻断): {_bh_e}")


def _try_load_bridge() -> Optional[BridgeVotingAdapter]:
    """加载今日 bridge 文件。不存在 → None (调用方 fallback 到 VotingCoordinator)。"""
    import json as _json
    import os as _os
    from app.utils.timezone import get_trade_date

    _today_str = get_trade_date()

    bridge_path = f"/opt/ftseek/FTSeek-US/data/opportunity_scan_{_today_str}.json"
    if not _os.path.exists(bridge_path):
        return None

    try:
        with open(bridge_path) as f:
            data = _json.load(f)
    except Exception as e:
        logger.warning(f"[playbook] bridge文件读取失败: {e}")
        return None

    candidates = data.get("candidates", [])
    if not candidates:
        logger.info(f"[playbook] bridge文件无候选, fallback到VotingCoordinator")
        return None

    # 2026-07-19 (DEB-round3 #1): schema fail-safe — 非法文件视作缺失,
    #   走技术面降级而非消费半损坏数据 (错价决策比 0 决策更危险).
    if not _validate_bridge_schema(candidates):
        logger.error(
            f"[playbook] bridge文件schema非法 (缺symbol/direction/decision_id), "
            f"fail-safe视作缺失, fallback到VotingCoordinator"
        )
        return None

    # 按 symbol 索引
    by_symbol = {}
    for c in candidates:
        sym = c.get("symbol", "")
        if sym:
            by_symbol[sym] = c

    logger.info(
        f"[playbook] bridge加载: {len(by_symbol)} candidates "
        f"(date={data.get('trade_date', '?')}, market={data.get('market_condition', '?')[:40]})"
    )

    # ── 注入 bridge 候选到 daily_active_pool (2026-07-09) ──────────────
    # 根因: BridgeVotingAdapter.vote(symbol) 是被动查询, 只在 pipeline 遍历 pool
    # 已有 symbol 时才匹配. bridge 候选 (如 TTE/ANET/ASML) 不在 pool → 永不触发.
    # 修复: 加载 bridge 时, 把候选 symbol 追加写入 pool, source=SCAN.
    _pool_path = "/opt/ftseek/FTSeek-US/data/daily_active_pool.json"
    try:
        with open(_pool_path) as _pf:
            _pool = _json.load(_pf)
        _existing = {e.get("code", "") for e in _pool.get("entries", [])}
        _injected = []
        for _sym in by_symbol:
            if _sym not in _existing:
                _pool["entries"].append({
                    "code": _sym,
                    "source": "SCAN",
                    "added_at": datetime.now(timezone.utc).isoformat(),
                    # 2026-07-19 (DEB-round3 #3-B): 对齐 TTL_PLAN_HOURS=24h — 此前 None=永久,
                    #   SCAN 注入条目绕过池 TTL 管理永久驻留, 挤占容量且信号过期不淘汰.
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                    "metadata": {"bridge": True, "strategy_id": "deep_analysis"},
                })
                _injected.append(_sym)
        if _injected:
            _pool["timestamp"] = datetime.now(timezone.utc).isoformat()
            with open(_pool_path, "w") as _pf:
                _json.dump(_pool, _pf, indent=2)
            logger.info(f"[playbook] bridge候选注入pool: {_injected} ({len(_injected)} new)")
    except Exception as _e:
        logger.warning(f"[playbook] bridge候选注入pool失败(非致命): {_e}")

    return BridgeVotingAdapter(by_symbol, data.get("trade_date", _today_str))


def _normalize_regime_str(regime_context) -> str:
    """统一 regime 字符串为 RISK_ON/CAUTIOUS/RISK_OFF/NO_TRADE (大写枚举名).

    修 §2.7 (signal-source-unification-review-20260624): regime_context.regime.value 是
    小写 ("risk_on"), str(enum_member) 是 "MarketRegime.RISK_ON", 而 regime_mults 键是
    大写 "RISK_ON" — 三者不一致导致 regime_mults.get(regime_str, 0.5) 恒命中默认 0.5.
    统一用 .name 与 regime_mults 键空间对齐.
    """
    reg = getattr(regime_context, "regime", None)
    if reg is None:
        return "CAUTIOUS"
    if hasattr(reg, "name"):  # Enum → "RISK_ON" / "CAUTIOUS" / "RISK_OFF" / "NO_TRADE"
        return reg.name
    return str(reg)


def _build_vote_market_data(symbol: str, data, regime_context, data_router):
    """构造 VotingCoordinator.vote() 的输入.

    router raw dict → SymbolContext.from_raw → dataclass.
    与 Pipeline A (market_data_stage.process) 同路径同键名空间, 让 vote() 走 asdict 分支,
    LLM 从 50+ 字段做决策 (含 atr/bollinger/spread_bps/order_flow_imbalance/\
    slope_5m/day_high/opening_range 等), 并补 market_regime.

    Returns:
        SymbolContext dataclass / None (行情不可用 → 调用方 SKIP)
    """
    if data_router is None:
        return None

    try:
        raw_ctx = data_router.get_full_market_context(symbol)
    except Exception as e:
        logger.warning(f"[playbook] {symbol} get_full_market_context 异常: {e}")
        return None

    if not raw_ctx or float(raw_ctx.get("price", 0) or 0) <= 0:
        return None

    from app.core.pipeline_contracts import SymbolContext
    ctx = SymbolContext.from_raw(symbol, raw_ctx)
    ctx.vix = float(getattr(regime_context, "vix", 20.0) or 20.0)
    ctx.market_regime = _normalize_regime_str(regime_context)
    return ctx


# 2026-07-11 (双脑清理): _merge_playbook_data 已移除。
# 其唯一用途是向 vote() 的 market_data dict 注入 6 维度供已死的 3 模型
# LLM prompt 消费。现 vote() 直接收 SymbolContext, 此函数无调用方。


from app.playbook.instruction_generator import (
    Instruction,
    compute_instruction_values,
    compute_confidence,
    build_thesis,
    _compute_volatility_based_sl_tp,
    _vol_bucket_sl_bounds,
)


def _build_instructions_from_votes(
    screen_results,
    voting_coordinator,
    regime_context,
    total_equity: float,
    data_router=None,
    today: str = "",
):
    """消费 VotingCoordinator 投票结果, 构建 Instruction 列表.

    数据流:
      ScreenResult → market_data → VotingCoordinator.vote() → vote_result
      → 方向映射 (LONG→BUY, SHORT→SELL_SHORT, NEUTRAL→SKIP)
      → compute_instruction_values 确定性计算数值
      → Instruction
    """
    if voting_coordinator is None:
        logger.warning("[playbook] VotingCoordinator 不可用, 全部候选 SKIP")
        return [
            Instruction(symbol=r.symbol, direction="SKIP", skip_reason="VotingCoordinator unavailable")
            for r in screen_results
        ]

    instructions = []
    # §2.7 修复: 统一用 _normalize_regime_str, 与 regime_mults 键空间对齐 (原 str(enum) 恒命中默认 0.5)
    regime_str = _normalize_regime_str(regime_context)

    # Regime 仓位乘数
    regime_mults = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
    regime_mult = regime_mults.get(regime_str, 0.5)

    # ── 过滤审计计数器 (DEB-20260628) ──
    # 记录每层过滤干掉了几个candidates, 函数末尾统一输出
    _filter_audit = {
        "total_input": len(screen_results),
        "market_data_none": 0,
        "vote_failed": 0,
        "vote_neutral": 0,
        "buy_macd_ma50_disabled": 0,  # 已禁用, 计数用
        "buy_low_strength_skip": 0,
        "buy_high_std_skip": 0,
        "low_confidence_skip": 0,
        "direction_flip": 0,
        "direction_conflict_skip": 0,
        "neg_edge_factor_skip": 0,  # 2026-07-10: 纯负edge因子拦截
        "neg_pair_skip": 0,  # 2026-07-11: 配对级负edge拦截
        "price_gap_skip": 0,
        "no_entry_price": 0,
        "final_instructions": 0,
    }

    for sr in screen_results:
        symbol = sr.symbol
        data = sr.data
        factors = sr.factors

        # 持久化信息层上下文 (新闻/内部人/财报/基本面/主题雷达)
        # 在投票前保存, 即使投票失败也有数据
        _persist_signal_context(today, symbol, data)

        # 构造 market_data 并调 VotingCoordinator (新路径走 SymbolContext, legacy 走 10 字段 dict)
        _market_ctx = _build_vote_market_data(symbol, data, regime_context, data_router)
        if _market_ctx is None:
            _filter_audit["market_data_none"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="行情不可用 (router 无数据)"
            ))
            continue

        # 2026-07-11 (双脑清理): 直接传 SymbolContext (_market_ctx) 给 vote()。
        # 原 _merge_playbook_data 只为喂已死的 3 模型 LLM prompt, 现已移除。
        # Bridge/Null/Technical 适配器都接受 ctx: Bridge 按 symbol 查, Technical 跑 eval("technical", ctx)。
        try:
            vote_result = voting_coordinator.vote(symbol, _market_ctx)
        except Exception as e:
            _filter_audit["vote_failed"] += 1
            logger.warning(f"[playbook] {symbol} 投票失败: {e}, SKIP")
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason=f"投票异常: {e}"
            ))
            continue
        
        if vote_result is None:
            # 2026-07-27 (topup 零产出诊断): 此分支原无审计桶 — 18 skip 而 breakdown 全 0
            #   的隐形路径实锤。补桶使过滤审计闭合。
            _filter_audit["vote_none"] = _filter_audit.get("vote_none", 0) + 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票返回 None"
            ))
            continue
        
        # 方向映射: VotingCoordinator 输出 LONG/SHORT/NEUTRAL → Instruction BUY/SELL_SHORT/SKIP
        vote_direction = str(vote_result.get("direction", "NEUTRAL")).upper()
        is_neutral = vote_result.get("is_neutral", False) or vote_direction == "NEUTRAL"

        # 2026-06-30 Phase 1: 在过滤前记录信号快照（消除选择性偏差）
        # 所有信号（包括后续被SKIP的）都记录因子，这样IC能学习"被过滤的信号对不对"
        _snap_decision_id = vote_result.get("decision_id", "") or f"SNAP-{symbol}-{int(time.time()*1000)}"
        try:
            from app.analysis.signal_ic_tracker import record_signal_snapshot
            record_signal_snapshot(
                decision_id=_snap_decision_id,
                symbol=symbol,
                direction=vote_direction,
                strength=int(vote_result.get("final_strength", 50) or 50),
                confidence=float(vote_result.get("consensus_level", 0) or 0),
                consensus_level=float(vote_result.get("consensus_level", 0) or 0),
                strategy_id=vote_result.get("strategy_id", ""),
                factors=list(factors.keys()) if factors else [],
                market_context=factors if factors else None,  # P1: 持久化触发因子数值详情(非仅keys)
            )
        except Exception:
            pass  # 非关键路径

        if is_neutral:
            _filter_audit["vote_neutral"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票中性, 无方向信号"
            ))
            continue

        # ── ADR-008 (2026-06-28): 盘中技术方向仲裁 ──
        # 读取配置
        _direction_mode = "llm_vote"
        _llm_conflict_threshold = 0.70
        try:
            from app.core.config_container import get_config
            _cfg = get_config()
            _direction_mode = str(_cfg.get_dotted("playbook.direction_mode", "llm_vote")).lower()
            _llm_conflict_threshold = float(_cfg.get_dotted("playbook.technical_arbitration.llm_conflict_confidence_threshold", 0.70))
        except Exception:
            pass

        # 计算技术方向 (best-effort, 用于影子日志与技术仲裁)
        technical_candidate = None
        technical_direction = None
        try:
            from app.strategy.pure import evaluate as eval_strategy
            technical_candidate = eval_strategy("technical", _market_ctx)
            if technical_candidate is not None:
                technical_direction = str(technical_candidate.direction).upper()
        except Exception as e:
            logger.debug(f"[playbook] {symbol} 技术信号评估异常: {e}")

        # 记录方向审计日志 (无论模式)
        _log_playbook_direction_audit(symbol, vote_direction, technical_direction, vote_result)

        # 技术仲裁模式: 无技术方向则 SKIP; LLM 强反向冲突则 SKIP; 否则 technical 获胜
        if _direction_mode == "technical_arbitration":
            if not technical_direction:
                _filter_audit["technical_no_signal"] = _filter_audit.get("technical_no_signal", 0) + 1
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP", skip_reason="无技术方向信号"
                ))
                continue

            if vote_direction != technical_direction:
                _llm_conf = float(vote_result.get("final_confidence", 0) or 0)
                if _llm_conf >= _llm_conflict_threshold:
                    _filter_audit["direction_conflict_skip"] = _filter_audit.get("direction_conflict_skip", 0) + 1
                    logger.info(
                        f"[playbook] {symbol} 方向冲突 SKIP: "
                        f"LLM={vote_direction}(conf={_llm_conf:.2f}) vs technical={technical_direction}"
                    )
                    instructions.append(Instruction(
                        symbol=symbol, direction="SKIP",
                        skip_reason=f"方向冲突: LLM={vote_direction} vs technical={technical_direction}"
                    ))
                    continue
                # LLM 反向不强, technical 获胜
                vote_direction = technical_direction
                logger.info(f"[playbook] {symbol} 技术方向覆盖 LLM: {technical_direction}")
            # 同向: 使用 vote_direction (与 technical 一致)

        direction = "BUY" if vote_direction == "LONG" else "SELL_SHORT"
        decision_id = vote_result.get("decision_id", "")

        # 更新 signal_context: 追加 LLM reasoning (需要 decision_id 关联 decisions.db)
        if decision_id:
            _persist_signal_context(today, symbol, data, vote_result)

        # 2026-06-28 (DEB-20260628): 方向差异化信号强度门槛.
        # BUY 方向历史胜率低(38%), 需要比 SHORT 更强的投票强度.
        # SHORT 方向历史胜率高(91%), 保持默认阈值.
        _final_strength = int(vote_result.get("final_strength", 50) or 50)
        _vote_std = float(vote_result.get("std_dev", 0) or 0)
        _vote_consensus = float(vote_result.get("consensus_level", 0) or 0)
        if direction == "BUY":
            # 2026-07-10: 删除 BUY_min_strength gate (strength=confidence×100同义反复, r=-0.063无预测力, 拦截0笔)
            # 保留 BUY_max_std gate (模型分歧 r=+0.102, 唯一弱正相关信号)
            _vote_sid = vote_result.get("strategy_id", "") or ""
            try:
                from app.core.strategy_registry import get_strategy_config
                _strat_cfg = get_strategy_config(_vote_sid)
                _buy_max_std = float(_strat_cfg["direction_filter"].get("BUY_max_std", 15))
            except Exception:
                _buy_max_std = 20
            # 模型一致性: BUY 要求分歧更小
            if _vote_std > _buy_max_std:
                _filter_audit["buy_high_std_skip"] = _filter_audit.get("buy_high_std_skip", 0) + 1
                logger.info(
                    f"[playbook] {symbol} BUY 模型分歧 std={_vote_std:.1f} > {_buy_max_std} → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP", skip_reason="BUY 模型分歧过大"
                ))
                continue

        # ── 2026-07-11 (双脑清理): deep_analysis 方向门禁已删除 ──────────────
        # 原门禁(L786-868)用于用 deep_analysis 交叉验证 voting_llm 方向。
        # 3 模型 voting 退休后可证空转: bridge 候选在上方刚写入自己的 snapshot,
        # 门禁又查到自己刚写的行 → 永远自我背书; 且 conditions 注入对 list 调
        # .get() 必抛异常 → 恒 no-op。technical_fallback 日无 DA snapshot → 恒放行。
        # 删除零行为变化, 且省掉每标的 2 次 DB 连接。

        # ── 过滤审计日志 (DEB-20260628) ──────────────────────────────
        # 目标: 记录每层过滤干掉了几个candidates, 为调优提供数据
        # 审计计数器在函数末尾统一输出
        # ──────────────────────────────────────────────────────────────

        # §2026-06-26: BUY 入场质量过滤 — MACD 柱确认 + MA50 方向过滤.
        # ⚠️ DEB-20260628: 暂时禁用 (6/26新增, 导致6/25从19→1的断崖式下降)
        # 根因: 弱势市场中MACD柱≤0 + 价格≤MA50 会批量SKIP所有BUY方向候选
        # 恢复条件: IC数据达标(≥30笔)后, 用数据验证是否重新启用
        # if direction == "BUY":
        #     macd_hist = market_data.macd - market_data.macd_signal
        #     if macd_hist <= 0:
        #         ...SKIP
        #     if market_data.ma50 > 0 and market_data.price > 0 and market_data.price <= market_data.ma50:
        #         ...SKIP
        #
        # 审计: 记录本会被这两层过滤干掉的 BUY 候选数(禁用期间只计数, 不 SKIP)
        if direction == "BUY":
            _macd = float(getattr(_market_ctx, "macd", 0) or 0)
            _macd_sig = float(getattr(_market_ctx, "macd_signal", 0) or 0)
            _ma50 = float(getattr(_market_ctx, "ma50", 0) or 0)
            _price = float(getattr(_market_ctx, "price", 0) or 0)
            _macd_hist = _macd - _macd_sig
            _would_skip_macd = _macd_hist <= 0
            _would_skip_ma50 = (
                _ma50 > 0
                and _price > 0
                and _price <= _ma50
            )
            if _would_skip_macd or _would_skip_ma50:
                _filter_audit["buy_macd_ma50_disabled"] += 1
                logger.debug(
                    f"[playbook] {symbol} BUY 原 MACD/MA50 过滤命中 "
                    f"(macd_hist={_macd_hist:.4f}, price={_price:.2f}, ma50={_ma50:.2f})"
                )

        # 2026-06-30: 方向冲突检查 — 从硬编码翻转改为IC加权+记录不拦截
        # 原逻辑: 看空条件数>看多 → 翻转方向; 相等 → SKIP
        # 问题1: BEARISH/BULLISH列表方向和真实IC相反(above_ma20被当bullish但IC=-0.553)
        # 问题2: 因子冲突SKIP杀掉8只大市值(GOOG/META/ORCL) — 信号不可见
        # 新逻辑: 用因子IC加权判断净方向; 冲突时记录但放行(让IC gate+退出策略管理风险)
        from app.playbook.condition_taxonomy import BEARISH_CONDITIONS, BULLISH_CONDITIONS
        _conditions = list(factors.keys()) if factors else []
        _bearish_hits = [c for c in _conditions if c in BEARISH_CONDITIONS]
        _bullish_hits = [c for c in _conditions if c in BULLISH_CONDITIONS]
        _bear_count = len(_bearish_hits)
        _bull_count = len(_bullish_hits)

        if _bear_count > 0 or _bull_count > 0:
            # 用因子IC加权判断净方向（如果有IC数据）
            try:
                import json as _json
                from pathlib import Path as _Path
                _ic_path = _Path("data/factor_ic_latest.json")
                _factor_ic = {}
                if _ic_path.exists():
                    with open(_ic_path) as _f:
                        _factor_ic = _json.load(_f)
            except Exception:
                _factor_ic = {}

            # 计算因子IC加权的净方向分
            _net_score = 0.0
            for c in _bullish_hits:
                _ic_val = float(_factor_ic.get(c, {}).get("ic", 0))
                _n_val = int(_factor_ic.get(c, {}).get("n", 0))
                if _n_val >= 30:  # 只有统计显著的因子才参与
                    _net_score += _ic_val
            for c in _bearish_hits:
                _ic_val = float(_factor_ic.get(c, {}).get("ic", 0))
                _n_val = int(_factor_ic.get(c, {}).get("n", 0))
                if _n_val >= 30:
                    _net_score -= _ic_val

            # 2026-07-04: IC加权方向与LLM方向冲突时→SKIP (原"记录但放行"导致insider_net_sell出现在BUY中)
            # Karpathy做减法: 已有IC加权逻辑算出了正确冲突信号, 只需把"放行"改"拦截"
            if _net_score > 0.05 and direction == "SELL_SHORT":
                _filter_audit["direction_flip"] += 1
                logger.info(
                    f"[playbook] {symbol} IC加权方向(LONG)与LLM方向(SELL_SHORT)冲突 "
                    f"(net_ic={_net_score:+.2f}) → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"IC方向冲突: 因子净IC={_net_score:+.2f}指向LONG但LLM=SHORT"
                ))
                continue
            elif _net_score < -0.05 and direction == "BUY":
                _filter_audit["direction_flip"] += 1
                logger.info(
                    f"[playbook] {symbol} IC加权方向(SHORT)与LLM方向(BUY)冲突 "
                    f"(net_ic={_net_score:+.2f}) → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"IC方向冲突: 因子净IC={_net_score:+.2f}指向SHORT但LLM=BUY"
                ))
                continue
        
        # ── 2026-07-10: 负 edge 因子硬拦截 (数据驱动, OKR factor 表) ──
        # 2026-07-11 停用 (audit 结论, 见 _FACTOR_BLACKLIST_ENABLED):
        #   该黑名单证据 99% 来自旧 config v1; v2 下 news_positive 反而净正(+$70/35笔),
        #   pe/rsi v2 样本≈零 → 基于旧策略给新策略定黑名单=打影子, 暂停用。
        # 规则: 当【全部】触发因子都在黑名单时 SKIP (纯负 edge 信号).
        _NEG_EDGE_FACTORS = {"pe_below_industry", "news_positive", "rsi_oversold"}
        if _FACTOR_BLACKLIST_ENABLED and _conditions:
            _non_neg = [c for c in _conditions if c not in _NEG_EDGE_FACTORS]
            if not _non_neg:
                _filter_audit["neg_edge_factor_skip"] += 1
                logger.info(
                    f"[playbook] {symbol} {direction} 纯负edge因子 {_conditions} "
                    f"(WR16-25%) \u2192 SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"负edge因子拦截: 唯一触发={_conditions} (胜率16-25%)"
                ))
                continue

        # ── 2026-07-11: 配对级负 edge 闸门 (数据驱动, playbook_factor_stats) ──
        # 2026-07-11 停用 (audit 结论): 同单因子黑名单, 99.5% 亏损证据来自旧 v1,
        #   v2 下这些配对几乎不交易/打平 → 暂停用, 待 v2 足量样本后再决定. 见 _FACTOR_BLACKLIST_ENABLED.
        # 规则: 触发因子的任一配对在 playbook_factor_stats 中 verdict=negative 且 n>=8 且
        #   total_pnl<0 → SKIP. 用配对级 verdict 避免误伤 (macd+pe 56%WR +$97 / news+volume +$286 放行).
        if _FACTOR_BLACKLIST_ENABLED and _conditions and len(_conditions) >= 2:
            _neg_pairs = _get_negative_factor_pairs()
            if _neg_pairs:
                _hit_pair = None
                for _pi in range(len(_conditions)):
                    for _pj in range(_pi + 1, len(_conditions)):
                        if frozenset((_conditions[_pi], _conditions[_pj])) in _neg_pairs:
                            _hit_pair = (_conditions[_pi], _conditions[_pj])
                            break
                    if _hit_pair:
                        break
                if _hit_pair:
                    _filter_audit["neg_pair_skip"] = _filter_audit.get("neg_pair_skip", 0) + 1
                    logger.info(
                        f"[playbook] {symbol} {direction} 负edge配对 {_hit_pair} "
                        f"(实证 verdict=negative/亏损) → SKIP"
                    )
                    instructions.append(Instruction(
                        symbol=symbol, direction="SKIP",
                        skip_reason=f"负edge配对拦截: {_hit_pair[0]}+{_hit_pair[1]} (实证亏损组合)"
                    ))
                    continue

        # 2026-06-26: Horizon 选择 — 基于个股阶段特征选择 short/long
        try:
            _horizon_decision = _get_horizon_selector().select(sr, regime_context)
            _horizon = _horizon_decision.horizon
            _horizon_confidence = _horizon_decision.confidence
            _horizon_reasoning = _horizon_decision.reasoning
        except Exception as e:
            logger.warning(f"[playbook] {symbol} Horizon 选择失败: {e}, 默认 short")
            _horizon = "short"
            _horizon_confidence = 0.0
            _horizon_reasoning = {"source": "error_fallback", "error": str(e)}

        # 构造 LLM decision dict (compute_instruction_values 期望的格式)
        decision = {
            "symbol": symbol,
            "direction": direction,
            "selected_conditions": list(factors.keys()) if factors else [],
            "thesis_template": _select_thesis_template(factors, direction),
        }

        # 数值计算 (确定性, LLM 不参与)
        # 2026-07-09: risk_pct从配置读取(原硬编码1.0)
        from app.playbook.config import get_playbook_config as _gpc
        _risk_pct = float(_gpc().get("risk_pct", 1.0))
        values = compute_instruction_values(
            decision=decision,
            data=data,
            total_equity=total_equity,
            risk_pct=_risk_pct,
            regime_multiplier=regime_mult,
            horizon=_horizon,
            regime_context=regime_context,
        )

        if not values or not values.get("entry_price"):
            _filter_audit["no_entry_price"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="价格缺失, 无法计算数值"
            ))
            continue

        # 2026-06-25 P0 修复: 盘前 last_price 与 bid/ask 偏离 guardrail.
        # 若 last_traded_price 与真实可成交价偏离 >2%, 说明信号生成后价格已大幅异动,
        # 原条件 (如 rsi_oversold) 可能失效, 应跳过.
        _last_price = float(data.technical.price or 0)
        _entry = float(values.get("entry_price", 0))
        if _last_price > 0 and _entry > 0:
            _price_gap = abs(_entry - _last_price) / _last_price
            if _price_gap > 0.02:
                _filter_audit["price_gap_skip"] += 1
                logger.warning(
                    f"[playbook] {symbol} 价格偏离过大: last_price={_last_price:.2f} "
                    f"entry={_entry:.2f} 偏离={_price_gap:.2%} → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason=f"价格偏离过大 ({_price_gap:.1%}), 信号失效",
                ))
                continue
        
        # 2026-07-10: bridge候选用LLM分析confidence, 非bridge用数据质量confidence
        # 根因: compute_confidence()是纯数据质量分(数据维度ok数×12+条件验证×28,封顶90)
        #   → 数据完整时永远=90, 与分析确信度无关
        #   → confidence=90但胜率31%, 因为"90"是数据完整度不是确信度
        # 修复: bridge候选(deep_analysis)用vote_result.final_confidence(已含LLM定性+辩论调整)
        #       非bridge保持compute_confidence不变
        _is_bridge = (vote_result.get("strategy_id", "") == "deep_analysis" 
                      or str(vote_result.get("decision_id", "")).startswith("DA"))
        if _is_bridge:
            _llm_conf = float(vote_result.get("final_confidence", 0) or 0)
            confidence = int(_llm_conf * 100)  # 0.0-1.0 → 0-100
        else:
            confidence = compute_confidence(data, decision["selected_conditions"], factors)

        # 2026-07-02: 跨日方向翻转检查 — 同标的24h内方向翻转需更高confidence
        # 根因: LLM方向翻转率34%, ORCL 6/23 SHORT赚→6/24 LONG亏(conf=90但方向错)
        # 方案: 查前一天同标的方向, 翻转时confidence门槛+15
        try:
            from datetime import timedelta as _td, datetime as _dt
            _today_date = _today_et_str()
            _prev_date = (_dt.now(_now_et()) - _td(days=1)).date().isoformat()
            # 跳过周末
            while _prev_date < _today_date:
                _prev_dt = _dt.strptime(_prev_date, "%Y-%m-%d")
                if _prev_dt.weekday() < 5:
                    break
                _prev_date = (_prev_dt - _td(days=1)).date().isoformat()
            from app.playbook.db import PlaybookDB as _PDB
            _pdb = _PDB()
            _prev_inst = _pdb.get_instruction(_prev_date, symbol)
            if _prev_inst and _prev_inst.get("direction") in ("BUY", "SELL_SHORT"):
                _prev_dir = _prev_inst["direction"]
                if _prev_dir != direction:
                    # 方向翻转 — 标记, 在confidence门槛检查中提高门槛
                    _filter_audit["direction_flip"] += 1
                    logger.info(
                        f"[playbook] {symbol} 跨日方向翻转: {_prev_dir}→{direction} "
                        f"(prev={_prev_date}), confidence门槛+15"
                    )
                    _direction_flipped = True
                else:
                    _direction_flipped = False
            else:
                _direction_flipped = False
        except Exception as _flip_err:
            logger.debug(f"[playbook] {symbol} 方向翻转检查失败(忽略): {_flip_err}")
            _direction_flipped = False

        # 2026-06-30: confidence门槛从strategy_registry读取（原从playbook config读）
        # 数据修复后: LONG IC=+0.238(强), SHORT IC=-0.051(弱) → 不再需要方向不对称门槛
        # 默认 BUY=70, SHORT=70 (对称), 可在registry的direction_filter中覆盖
        # 2026-08-09 (用户指示 sim 放量): 兜底 70→55 — 60日实测 conf IC≈-0.02 (无预测力,
        #   DEB-20260806-source-control), 用死指标卡供给=纯浪费; VOTE 路径是当前最优
        #   供给单元格 (60日 +$186/21笔), 放宽给它增量. config 覆盖键:
        #   playbook.supply_conf_gate_buy / supply_conf_gate_short. 回滚: 恢复 70.
        try:
            from app.core.config_container import get_config as _gc_sc
            _fb_buy = int(_gc_sc().get_dotted("playbook.supply_conf_gate_buy", 55))
            _fb_short = int(_gc_sc().get_dotted("playbook.supply_conf_gate_short", 55))
        except Exception:
            _fb_buy, _fb_short = 55, 55
        try:
            from app.core.strategy_registry import get_strategy_config
            _sr_cfg = get_strategy_config(vote_result.get("strategy_id", ""))
            _buy_min_conf = int(_sr_cfg["direction_filter"].get("BUY_min_confidence", _fb_buy))
            _short_min_conf = int(_sr_cfg["direction_filter"].get("SHORT_min_confidence", _fb_short))
        except Exception:
            _buy_min_conf = _fb_buy
            _short_min_conf = _fb_short

        _dir_conf_map = {"BUY": _buy_min_conf, "SELL_SHORT": _short_min_conf}
        _min_conf_for_dir = _dir_conf_map.get(direction, 65)

        # P0 (2026-07-11): edge 影子闸门 — 记录"数据驱动 edge 判定 vs 实盘置信度闸门"对照
        # 纯影子: 不改实盘指令; 待影子数据验证 edge 闸门更优后, 再经人工/灰度审批切换
        try:
            from app.decision.edge_shadow_gate import get_edge_shadow_gate
            get_edge_shadow_gate().evaluate(
                symbol, direction, float(confidence),
                live_pass=(confidence >= _min_conf_for_dir),
            )
        except Exception:
            pass  # 非关键路径

        if confidence < _min_conf_for_dir:
            _filter_audit.setdefault("low_confidence_skip", 0)
            _filter_audit["low_confidence_skip"] += 1
            logger.info(
                f"[playbook] {symbol} {direction} confidence={confidence:.0f} "
                f"< 门槛={_min_conf_for_dir} → SKIP"
            )
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason=f"{direction} confidence {confidence:.0f} < {_min_conf_for_dir}"
            ))
            continue
        
        data_quality = {
            "fundamental": data.fundamental.quality,
            "technical": data.technical.quality,
            "insider": data.insider.quality,
            "news": data.news.quality,
            "earnings": data.earnings.quality,
            "sentiment": data.sentiment.quality,
        }
        
        thesis = build_thesis(decision["thesis_template"], decision["selected_conditions"])
        
        risks = []
        ok_count = sum(1 for q in data_quality.values() if q == "ok")
        if ok_count < 4:
            risks.append(f"数据不完整 ({ok_count}/6 维度 ok)")
        if data.earnings.days_until is not None and data.earnings.days_until < 14:
            risks.append(f"财报 {data.earnings.days_until} 天后")
        if values.get("atr_source", "").startswith("FALLBACK"):
            risks.append("ATR 缺失, 使用 fallback 止损")
        
        inst = Instruction(
            symbol=symbol,
            direction=direction,
            entry_price=values.get("entry_price", 0),
            stop_loss=values.get("stop_loss", 0),
            take_profit=values.get("take_profit", 0),
            position_size=values.get("position_size", 0),
            risk_amount=values.get("risk_amount", 0),
            risk_reward=values.get("risk_reward", 0),
            confidence=confidence,
            thesis=thesis,
            conditions=decision["selected_conditions"],
            data_quality=data_quality,
            risks=risks,
            sector=data.fundamental.industry or "",
            entry_price_source=values.get("entry_price_source", ""),
            atr_source=values.get("atr_source", ""),
            decision_id=decision_id,
            horizon=_horizon,
            horizon_confidence=_horizon_confidence,
            horizon_reasoning=_horizon_reasoning,
            # 2026-07-11 Stage 1: 保留 DA 失效条件 + 真实论点 (bridge 缺失时为空串)
            invalidation=vote_result.get("invalidation", "") or "",
            da_thesis=vote_result.get("thesis", "") or "",
            # F1 (2026-07-31): DA 锚偏离度透传 (非 bridge 候选为 None, record-only)
            anchor_dev_pct=vote_result.get("anchor_dev_pct"),
        )
        # 方向翻转时降仓50% — 同guardrail方式, 不SKIP信号只降风险敞口
        if _direction_flipped:
            _orig_size = inst.position_size or 0
            if _orig_size > 0:
                inst.position_size = max(1, int(_orig_size * 0.5))
                inst.skip_reason = f"跨日方向翻转 降仓50% (原{_orig_size}→{inst.position_size})"
        instructions.append(inst)
        
        logger.info(
            f"[playbook] {symbol} vote→instruction: {vote_direction}→{direction} "
            f"strength={vote_result.get('final_strength', '?')} "
            f"consensus={vote_result.get('consensus_level', '?')} "
            f"decision_id={decision_id}"
        )

    # ── 过滤审计日志输出 (DEB-20260628) ──
    _filter_audit["final_instructions"] = sum(1 for i in instructions if i.direction != "SKIP")
    _skip_count = _filter_audit["total_input"] - _filter_audit["final_instructions"]
    logger.info(
        f"[playbook] 过滤审计: input={_filter_audit['total_input']} → "
        f"instructions={_filter_audit['final_instructions']} skips={_skip_count} | "
        f"breakdown: mkt_data_none={_filter_audit['market_data_none']} "
        f"vote_fail={_filter_audit['vote_failed']} "
        # 2026-07-27 (topup 零产出诊断): 补两个硬编码清单缺失桶 — skips=18 而 breakdown
        #   全 0 的矛盾根因 (vote 返 None / technical 无信号都不在日志行里)
        f"vote_none={_filter_audit.get('vote_none', 0)} "
        f"tech_no_sig={_filter_audit.get('technical_no_signal', 0)} "
        f"neutral={_filter_audit['vote_neutral']} "
        f"buy_str_skip={_filter_audit['buy_low_strength_skip']} "
        f"buy_std_skip={_filter_audit['buy_high_std_skip']} "
        f"low_conf_skip={_filter_audit['low_confidence_skip']} "
        f"buy_filter_disabled={_filter_audit['buy_macd_ma50_disabled']} "
        f"dir_flip={_filter_audit['direction_flip']} "
        f"dir_conflict={_filter_audit['direction_conflict_skip']} "
        f"price_gap={_filter_audit['price_gap_skip']} "
        f"no_entry={_filter_audit['no_entry_price']}"
    )

    # 组合级 guardrail: 方向偏斜 / sector 集中度 / 最低 R:R
    instructions = _apply_portfolio_guardrails(instructions)

    return instructions


def _select_thesis_template(factors: Dict[str, Any], direction: str) -> str:
    """根据触发因子选择 thesis 模板"""
    if not factors:
        return "momentum_continuation" if direction == "BUY" else "mean_reversion"

    factor_keys = set(factors.keys())

    if "insider_cluster_buy" in factor_keys or "insider_net_buy" in factor_keys:
        return "insider_confidence"
    if "macd_golden_cross" in factor_keys:
        return "breakout" if direction == "BUY" else "mean_reversion"
    if any("rsi" in k for k in factor_keys):
        return "trend_pullback" if direction == "BUY" else "value_reversal"

    return "momentum_continuation" if direction == "BUY" else "mean_reversion"


def _apply_portfolio_guardrails(
    instructions: List[Any],
    *,
    max_sector_pct: float = 0.40,
    max_direction_skew: float = 0.80,  # 2026-06-30: 0.65→0.80, 允许cautious regime偏SHORT
    min_rr: float = 1.0,  # 2026-07-16: 0.8→1.0, R:R≥1 保证正期望 (还需胜率>50%)
) -> List[Any]:
    """组合级风险 guardrail (2026-06-25).

    2026-06-30: 放宽 max_direction_skew 和 min_rr.
    根因: cautious regime 下 3 模型一致看空 → 86% SHORT 被截杀;
          rr_ratio=1.0 < min_rr=1.5 → 全部指令被杀.
    原则: guardrail 是安全网, 不是信号杀手. 真正的方向风险由仓位管理控制.
    """
    from collections import Counter

    # 先过滤掉 SKIP / 无方向 / R:R 不达标的
    active = []
    for inst in instructions:
        if getattr(inst, "direction", None) in ("SKIP", None, ""):
            active.append(inst)
            continue
        rr = getattr(inst, "risk_reward", 0) or 0
        if rr < min_rr:
            logger.warning(
                f"[playbook guardrail] {inst.symbol} R:R {rr:.2f} < {min_rr}, 转为 SKIP"
            )
            inst.direction = "SKIP"
            inst.skip_reason = f"R:R {rr:.2f} < 最低 {min_rr}"
        active.append(inst)

    # 只统计非 SKIP 的有效指令
    valid = [i for i in active if i.direction in ("BUY", "SELL_SHORT")]
    if len(valid) <= 1:
        return active

    total = len(valid)

    # 方向偏斜
    buy_count = sum(1 for i in valid if i.direction == "BUY")
    short_count = total - buy_count
    majority_pct = max(buy_count, short_count) / total if total > 0 else 0

    # 如果方向偏斜超过阈值, 砍掉低 conv 的多数方信号直到平衡
    if majority_pct > max_direction_skew:
        majority_dir = "BUY" if buy_count > short_count else "SELL_SHORT"
        logger.warning(
            f"[playbook guardrail] 方向偏斜 {majority_pct:.0%} 超限, "
            f"{_trim_majority_direction(valid, majority_dir, max_direction_skew)} 条信号被降级为 SKIP"
        )

    # Sector 集中度 (仅当有 sector 信息时)
    sectors = Counter(getattr(i, "sector", "") or "Unknown" for i in valid)
    for sector, count in sectors.items():
        if count / total > max_sector_pct:
            logger.warning(
                f"[playbook guardrail] sector {sector} 占比 {count/total:.0%} 超限, "
                f"{_trim_majority_sector(valid, sector, max_sector_pct)} 条信号被降级为 SKIP"
            )

    return active


def _trim_majority_direction(valid: List[Any], majority_dir: str, target_skew: float) -> int:
    """多数方信号降仓, 控制方向风险.
    
    2026-06-30 根因修复: SKIP → position_size *= 0.5
    原则: guardrail 是安全网, 不是信号杀手. 方向风险由仓位管理控制.
    多数方信号全部降仓50%, 保留信号产出率.
    """
    majority = [i for i in valid if i.direction == majority_dir]
    if not majority:
        return 0
    trimmed = 0
    for inst in majority:
        _orig = getattr(inst, "position_size", 0) or 0
        if _orig > 0:
            inst.position_size = max(1, int(_orig * 0.5))
            inst.skip_reason = f"方向偏斜 guardrail 降仓50% (原{_orig}→{inst.position_size})"
            trimmed += 1
    return trimmed


def _trim_majority_sector(valid: List[Any], sector: str, target_pct: float) -> int:
    """将指定 sector confidence 最低的信号降级, 直到占比 ≤ target_pct."""
    trimmed = 0
    from collections import Counter
    while True:
        active_valid = [i for i in valid if i.direction in ("BUY", "SELL_SHORT")]
        total = len(active_valid)
        sector_count = Counter(
            getattr(i, "sector", "") or "Unknown" for i in active_valid
        ).get(sector, 0)
        if total == 0 or sector_count / total <= target_pct:
            break
        candidates = [
            i for i in active_valid
            if (getattr(i, "sector", "") or "Unknown") == sector
        ]
        candidates.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        candidates[0].direction = "SKIP"
        candidates[0].skip_reason = f"sector 集中度 guardrail 降级 ({sector})"
        trimmed += 1
    return trimmed


# ── 入口 1: 盘前生成 (regime → 收集 → 筛选 → VotingCoordinator → 入库 → 飞书) ──
def run_playbook_generate(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘前生成今日交易指令 (Layer 0 + Layer 1)。

    幂等: scheduler._playbook_generated_date == today 即跳过 (force=True 可重跑)。
    节假日跳过。任何异常都 catch 并 log, 不向上抛 (保证不影响 scheduler 主循环)。
    """
    try:
        today = _today_et_str()

        if not _playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘前生成 (上线闸门关闭)")
            return None

        if not force:
            if not _is_trading_day_today():
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
            # 2026-07-09: 如果voting_coordinator是NullVotingAdapter, 跳过生成
            # 根因: NullVotingAdapter.vote()返回None → 所有候选SKIP → save_instruction
            # 会UPSERT覆盖bridge已写的BUY/SELL_SHORT instruction为SKIP, 破坏bridge候选
            _vc = getattr(scheduler, "voting_coordinator", None)
            if _vc is not None and type(_vc).__name__ == "NullVotingAdapter":
                logger.info(
                    f"[playbook] {today} voting_coordinator=NullVotingAdapter, "
                    f"跳过生成 (bridge候选已由deep_analysis skill直写DB, 不需要VotingCoordinator)"
                )
                return None
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB，不再依赖内存属性
            # 内存属性在进程重启后丢失 → 重启后重新执行 → 覆盖当天数据
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                existing = db.get_instructions_by_date(today)
                if existing and len(existing) > 0:
                    logger.debug(f"[playbook] {today} DB 已有 {len(existing)} 条指令，跳过生成 (幂等)")
                    scheduler._playbook_generated_date = today  # 同步内存标志
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")
                if getattr(scheduler, "_playbook_generated_date", None) == today:
                    logger.debug(f"[playbook] {today} 已生成 (内存标志), 跳过")
                    return None

        logger.info(f"[playbook] 🚀 盘前生成开始 (date={today})")

        # 2026-07-19 (DEB-round3 #1): bridge 三态告警 (缺失/空/非法 → 飞书, 不阻断生成)
        _check_bridge_health_and_alert("run_generate")

        from app.playbook.db import PlaybookDB
        from app.playbook.market_regime import MarketRegimeGate
        from app.playbook.screener import PlaybookScreener

        db = PlaybookDB()
        db.ensure_tables()

        # Layer 0: 市场环境门禁
        regime_ctx = MarketRegimeGate().assess()
        db.save_regime_log({
            "date": today,
            "regime": regime_ctx.regime.value,
            "vix": regime_ctx.vix,
            "spy_price": regime_ctx.spy_price,
            "spy_ma20": regime_ctx.spy_ma20,
            "spy_ma50": regime_ctx.spy_ma50,
            "score": regime_ctx.score,
            "notes": f"top_n={regime_ctx.top_n}, pos_mult={regime_ctx.position_multiplier}",
        })
        logger.info(f"[playbook] Layer0 regime={regime_ctx.regime.value} VIX={regime_ctx.vix}")

        # Layer 1: 数据收集 + 规则筛选 (不截断, 全量通过门槛的候选)
        watchlist = _get_watchlist(scheduler)
        if not watchlist:
            logger.warning("[playbook] watchlist 为空, 跳过生成")
            scheduler._playbook_generated_date = today  # 标记已尝试, 避免空跑循环
            return {"date": today, "instructions": 0, "reason": "empty_watchlist"}

        collector = _build_data_collector()
        screener = PlaybookScreener(collector)
        positions = _get_positions_as_dicts(scheduler)
        screen_results = screener.screen(
            watchlist=watchlist,
            regime=regime_ctx.regime,
            positions=positions,
        )
        logger.info(f"[playbook] Layer1 筛选出 {len(screen_results)} 只候选 (无截断)")

        if not screen_results:
            logger.info("[playbook] 无候选通过质量门槛, 跳过生成")
            scheduler._playbook_generated_date = today
            return {"date": today, "instructions": 0, "reason": "no_candidates_passed_filter"}

        # Layer 2: VotingCoordinator 统一方向决策
        # 信号源唯一化: Playbook 消费 VotingCoordinator 的多模型投票结果,
        # 不再跑独立 LLM — 消除平行管线, 方向决策统一归因.
        voting_coordinator = _get_voting_coordinator(scheduler)
        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        instructions = _build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=voting_coordinator,
            regime_context=regime_ctx,
            total_equity=_get_total_equity(scheduler),
            data_router=data_router,
            today=today,
        )

        # 入库
        saved = 0
        for inst in instructions:
            try:
                inst_dict = _instruction_to_db_dict(inst, regime_ctx, today)
                db.save_instruction(inst_dict)
                saved += 1
            except Exception as e:
                logger.warning(f"[playbook] 指令入库失败 {getattr(inst, 'symbol', '?')}: {e}")

        logger.info(f"[playbook] ✅ 生成完成: {saved}/{len(instructions)} 条指令入库")

        # 2026-07-16: 盘前推送已移除内联触发, 统一归 run_playbook_morning_push 独占。
        # 根因: 内联走 sync=False, 而独立任务走 sync=True (绕过 PushHub dedup) →
        #       两条路径都发 → 用户收到两次。独立任务有持久化幂等+重试, 足以覆盖送达。

        scheduler._playbook_generated_date = today
        return {"date": today, "instructions": saved, "regime": regime_ctx.regime.value}

    except Exception as e:
        logger.exception(f"[playbook] 盘前生成异常 (已隔离, 不影响 scheduler): {e}")
        return {"error": str(e)}


_MORNING_PUSH_STATE = "/opt/ftseek/FTSeek-US/data/playbook_push_state.json"


def run_playbook_morning_push(scheduler=None) -> Optional[bool]:
    """独立的盘前指令推送任务 — 不依赖生成内联触发, 每日幂等(持久化), 失败下轮重试.

    背景 (2026-07-11): 原推送仅在 run_playbook_generate 末尾内联触发, 但该函数
    每日幂等早退 → 生成后再调到不了推送行; 且走异步 PushHub, 重启 churn 中易丢失,
    导致 GOOG/NU 等指令从未送达。本任务在 PRE_MARKET/TRADING 每轮调用:
      - 已成功推送(持久化标记) → 跳过, 不重复;
      - PlaybookDB 无当日指令 → 跳过, 下轮重试;
      - 同步发送(真实送达确认) → 成功才落幂等标记, 失败下轮自动重试。
    """
    import json as _json
    from datetime import datetime as _dt
    try:
        from app.utils.timezone import now_et
        today = now_et().date().isoformat()
    except Exception:
        today = _dt.now().date().isoformat()

    try:
        with open(_MORNING_PUSH_STATE) as f:
            state = _json.load(f)
    except Exception:
        state = {}
    morning = state.setdefault("morning", {})
    if morning.get(today):
        return None  # 今日已推送, 幂等跳过

    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.feishu_push import PlaybookPusher
        db = PlaybookDB()
        instructions = db.get_instructions_by_date(today)
        if not instructions:
            return None  # 尚无指令, 下轮重试

        ok = PlaybookPusher(db).push_morning_instructions(today, sync=True)
        if ok:
            morning[today] = True
            # 仅保留最近 30 天, 防止文件膨胀
            if len(morning) > 30:
                for _k in sorted(morning)[:-30]:
                    morning.pop(_k, None)
            try:
                with open(_MORNING_PUSH_STATE, "w") as f:
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


# D2 (2026-07-24 夜): topup 每窗口尝试计数 {"YYYY-MM-DD:timing": [count, last_ts]} — +0 退避用,
#   内存级 (重启重置无害), 隶属 run_playbook_topup_supplement。
_TOPUP_ATTEMPTS: Dict[str, list] = {}


def _topup_window(hhmm: int, cfg: Dict[str, Any]) -> Optional[str]:
    """topup 时间窗判定 → "premarket" / "intraday" / None (窗外)。

    2026-07-24 (用户裁决, 知情破例): 盘中窗 — 盘前批用昨收价评回调窗系统性偏陈旧,
    盘中窗用实时价重评同一正式链, 捕捉当日跌进窗的标的。两窗共享当日上限, 分窗幂等。
    """
    _pm_start = int(cfg.get("not_before_et_hhmm", 845))
    _pm_end = int(cfg.get("premarket_end_et_hhmm", 930))
    _id_start = int(cfg.get("intraday_start_et_hhmm", 1030))
    _id_end = int(cfg.get("intraday_end_et_hhmm", 1130))
    if _pm_start <= hhmm < _pm_end:
        return "premarket"
    if _id_start <= hhmm < _id_end:
        return "intraday"
    return None


def run_playbook_topup_supplement(scheduler=None) -> Optional[Dict[str, Any]]:
    """扩容补量 (2026-07-24, 用户裁决: 加快样本收集, 但不降标准、走正式链).

    当日可执行指令数 < 目标下限时, 从配置流动性池按**与 DA 完全相同的正式生成链**补足:
      PlaybookScreener 质量门 → TechnicalFallbackAdapter 确定性方向 (无方向不硬凑)
      → _build_instructions_from_votes (负 edge 拦截/价隔检查/确定性 SL-TP) → save_instruction。
    下游全部质量闸照常适用: 低ATR门/连败封禁/VWAP门/量能门/触发窗/财报门/风控 sizing。

    不降标准的保证:
      - 方向来自确定性 technical 纯函数 (已有 fallback 设计, 非 LLM 噪声投票);
      - 独立归因: strategy_id=technical_fallback + raw.source=topup_pool, 报表可切分;
      - 时间门: 双窗口 — 盘前 (默认 08:45-09:30, DA/UE 供给定型后) +
        盘中 (默认 10:30-11:30, 实时价评估, 避开开盘噪声且留足 14:00 触发窗);
      - 幂等: 每交易日每窗一次 (raw.source+raw.timing 标记); 已有指令的标的不重复注入;
      - 两窗共享 max_add_per_day 当日上限; raw.timing=premarket/intraday 供效果切分;
      - 回滚: playbook.topup.enabled=false。
    """
    try:
        from app.core.config_container import get_config as _gc_tu
        _tu_cfg = _gc_tu().get_dotted("playbook.topup", {}) or {}
    except Exception:
        _tu_cfg = {}
    if not bool(_tu_cfg.get("enabled", False)):
        return None

    try:
        _now = datetime.now(_now_et())
        _hhmm = _now.hour * 100 + _now.minute
        _timing = _topup_window(_hhmm, _tu_cfg)
        if _timing is None:
            return None

        # D2 (2026-07-24 夜, DEB §十-b 用户批): +0 退避 — 每窗口最多 N 次尝试、间隔≥M 分钟。
        #   背景: +0 时不落幂等标记 → 每 ~2min 全池重扫 (解锁风暴放大器 + 无谓配额消耗)。
        #   内存级计数 (重启重置可接受: 重启后多一次尝试无害)。回滚: max_attempts_per_window 设 999。
        _att_key = f"{_today_et_str()}:{_timing}"
        _max_att = int(_tu_cfg.get("max_attempts_per_window", 2))
        _retry_min = float(_tu_cfg.get("retry_interval_min", 15))
        _att = _TOPUP_ATTEMPTS.get(_att_key, [0, 0.0])
        if _att[0] >= _max_att:
            return None
        if _att[0] > 0 and (time.time() - _att[1]) < _retry_min * 60:
            return None

        from app.playbook.db import PlaybookDB
        from app.playbook.market_regime import MarketRegimeGate
        from app.playbook.screener import PlaybookScreener

        today = _today_et_str()
        db = PlaybookDB()
        db.ensure_tables()
        existing = db.get_instructions_by_date(today)
        # 分窗幂等: 本窗已补过则不再补; 无 timing 标记的历史行视为 premarket。
        # 统计当日已补总数, 供两窗共享 max_add_per_day。
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
        # P-1 (2026-08-01 名单合一, DEB-20260724 §7): pool 真源 = watchlist roles.topup_eligible;
        #   静态 playbook.topup.pool 双名单已退役 (漂移源). 回滚 = git revert.
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
        # D2: 记录本次尝试 (昂贵段开始前)
        _TOPUP_ATTEMPTS[_att_key] = [_att[0] + 1, time.time()]

        # 与 run_playbook_generate 同链: Layer0 regime → Layer1 质量筛 → 方向 → 指令构建
        regime_ctx = MarketRegimeGate().assess()
        collector = _build_data_collector()
        screener = PlaybookScreener(collector)
        positions = _get_positions_as_dicts(scheduler)
        screen_results = screener.screen(watchlist=_cands, regime=regime_ctx.regime, positions=positions)
        if not screen_results:
            logger.info("[playbook·topup] 池内无标的通过质量门槛, 不硬凑")
            return {"added": 0, "reason": "no_screen_pass"}

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        instructions = _build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=TechnicalFallbackAdapter(),
            regime_context=regime_ctx,
            total_equity=_get_total_equity(scheduler),
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
                inst_dict = _instruction_to_db_dict(inst, regime_ctx, today)
                inst_dict["raw_instruction"]["source"] = "topup_pool"  # 归因切分 + 幂等标记
                inst_dict["raw_instruction"]["timing"] = _timing  # 分窗幂等 + 盘前/盘中效果切分
                # D1 (2026-07-24 夜, DEB §十-b): 评估快照 — 生成时刻的实时价+时戳落库,
                #   使"半新鲜(实时价×日线指标)是否伤害质量"可审计 (指标可由 eval_asof+日线确定性重算)。
                try:
                    _eq = data_router.get_quote(inst.symbol) if data_router else None
                    inst_dict["raw_instruction"]["eval_price"] = round(float(
                        (_eq or {}).get("last", 0) or (_eq or {}).get("price", 0) or 0), 4) or None
                except Exception:
                    inst_dict["raw_instruction"]["eval_price"] = None
                inst_dict["raw_instruction"]["eval_asof"] = _now.isoformat()
                # 2026-07-30 Phase1 (用户裁"统一"): 盘中方向证据并排记录 (record-only) —
                #   VWAP侧/板块脉搏/订单流 × 一致度, 供 S1 按证据一致度切片定预测力。
                _ev = _gather_intraday_evidence(data_router, inst.symbol, inst.direction)
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


# 2026-07-30 Phase1: rotation 细分行业名 → sector_pulse 12 键 (SPDR 口径) 关键词映射。
#   两侧名库不同源 (实测 Semiconductors≠Semis / Health Care≠Healthcare 直匹配全 null),
#   映射不上如实 null 不猜。顺序匹配, 特异词在前 (semi 先于 tech)。
_SECTOR_TO_PULSE = (
    (("semiconductor",), "Semis"),
    (("software", "technology", "it services", "electronic"), "Technology"),
    (("pharma", "biotech", "health", "medical", "life sciences"), "Healthcare"),
    (("bank", "financial", "insurance", "capital markets", "consumer finance"), "Financials"),
    (("real estate", "reit"), "RealEstate"),
    (("telecom", "media", "entertainment", "interactive", "communication"), "Communication"),
    (("energy", "oil", "gas"), "Energy"),
    (("chemical", "metals", "mining", "materials", "paper", "container"), "Materials"),
    (("staple", "food", "beverage", "household", "tobacco", "consumer products"), "ConsStaples"),
    (("hotel", "restaurant", "leisure", "apparel", "luxury", "retail", "automobile", "textiles"), "ConsDiscretionary"),
    (("aerospace", "defense", "machinery", "industrial", "transport", "airline", "construction", "building", "electrical"), "Industrials"),
    (("utilit",), "Utilities"),
)


def _gather_intraday_evidence(data_router, symbol: str, direction: str) -> Optional[Dict[str, Any]]:
    """盘中方向证据叠加 (2026-07-30 Phase1, 用户裁"统一" — record-only 零行为变更)。

    背景: technical 三票/VWAP 门/sector_pulse/订单流四层盘中方向证据各自存在但互不
    通气且无预测力检验。本函数在 topup 指令落库时把证据并排写进 raw.evidence —
    S1 记分牌按"证据一致度"切片, 胜出者才升确认/否决门 (Phase2, 另裁)。
    失败返 None 不阻断落库; 板块名与 pulse 键不匹配时 pulse_chg=null 如实缺省。
    """
    try:
        raw = data_router.get_full_market_context(symbol) if data_router else None
        if not raw:
            return None
        px = float(raw.get("price", 0) or 0)
        vwap = float(raw.get("vwap", 0) or 0)
        ofi = raw.get("order_flow_imbalance")
        is_short = "SHORT" in str(direction).upper()
        ev: Dict[str, Any] = {
            "vwap_side": ("above" if px >= vwap else "below") if (px > 0 and vwap > 0) else None,
            "vwap_dev_pct": round((px - vwap) / vwap * 100, 3) if (px > 0 and vwap > 0) else None,
            "order_flow_imbalance": round(float(ofi), 4) if ofi is not None else None,
            "ticker_buy_sell_ratio": raw.get("ticker_buy_sell_ratio"),
            "volume_ratio": raw.get("volume_ratio"),
            "sector": None, "sector_pulse_chg": None, "pulse_asof_et": None,
        }
        # 板块脉搏: symbol→sector (deep_sector_rotation) × 实时涨跌 (sector_pulse)
        try:
            import json as _json
            _root = Path(__file__).resolve().parents[2] / "data"
            _rot = _json.loads((_root / "deep_sector_rotation.json").read_text())
            _sec = (_rot.get("symbol_sectors") or {}).get(symbol)
            ev["sector"] = _sec
            _pl = _json.loads((_root / "sector_pulse.json").read_text())
            _chg = _pl.get("changes") or {}
            if _sec:
                _hit = _chg.get(_sec)
                if _hit is None:  # 关键词映射 (两侧板块名库不同源, 映射不上如实 null)
                    _sl = _sec.lower()
                    _pk = next((pk for kws, pk in _SECTOR_TO_PULSE
                                if any(kw in _sl for kw in kws)), None)
                    _hit = _chg.get(_pk) if _pk else None
                ev["sector_pulse_chg"] = _hit
                ev["pulse_asof_et"] = _pl.get("asof_et")
        except Exception:
            pass
        # 一致度计数: 可用证据里与方向同向的份数 (方向无关统一式: sign × 证据正负)
        _sign = -1 if is_short else 1
        _bits = []
        if ev["vwap_dev_pct"] is not None:
            _bits.append(_sign * ev["vwap_dev_pct"] > 0)
        if ev["order_flow_imbalance"] is not None:
            _bits.append(_sign * ev["order_flow_imbalance"] > 0)
        if ev["sector_pulse_chg"] is not None:
            _bits.append(_sign * float(ev["sector_pulse_chg"]) > 0)
        ev["agree"] = sum(_bits)
        ev["avail"] = len(_bits)
        return ev
    except Exception:
        return None  # record-only, 任何异常不得影响指令落库


def run_playbook_normalize_vol_buckets(scheduler=None) -> Optional[Dict[str, Any]]:
    """盘前波动率定桶落库 (2026-07-17): 为当日指令补 vol_bucket + atr_pct 到主表。

    背景: DA/UE bridge 直写 DB 的指令绕过 _generate_instructions → 从不经过 vol_bucket 分桶体系,
    写库那刻也拿不到 ATR (DA 在外部 skill / UE 只取现价)。此前桶分类只在盘中 re-price 时临时算,
    且仅落 shadow jsonl, 主表无从查询"这单当初被分到哪个波动桶"。

    本任务在盘前跑一次: 用日线 ATR (ATRProvider, 盘前稳定, 不依赖盘中 OpenD) 定桶, 落主表
    vol_bucket / atr_pct 列, 使桶分类可解释、可事后查询。
    - 只写分类字段, 不覆盖 entry/SL/TP (精确价留给触发时 re-price)。
    - 幂等: 每轮盘前重算覆盖同分类, 无副作用。
    - best-effort: 每行独立 try, ATR 取不到则跳过 (vol_bucket 留空), 绝不影响盘前流程。
    """
    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.config import get_playbook_config
        from app.core.di_container import get_di_container

        today = _today_et_str()
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
            # 幂等: 已落桶的行跳过 (盘前首跑标注; TRADING catch-up 只补盘中重启/新增漏掉的)
            if inst.get("vol_bucket"):
                continue
            symbol = inst.get("symbol", "")
            try:
                atr_pct = atr_provider.get_atr_pct(symbol)
                if not atr_pct or atr_pct <= 0:
                    _missing += 1
                    continue
                _hp = pb_cfg.get_horizon_params(inst.get("horizon", "short"))
                _vol_info = _compute_volatility_based_sl_tp(atr_pct, inst["direction"], _hp, pb_cfg)
                db.update_instruction_tracking(inst["id"], {
                    "vol_bucket": (_vol_info or {}).get("bucket"),
                    "atr_pct": round(float(atr_pct), 6),
                })
                _tagged += 1
            except Exception as _e:
                logger.debug(f"[playbook] {symbol} 定桶跳过 (已隔离): {_e}")
                continue

        # 2026-07-25 (隐患扫描小项②): 零工作轮 (全部已幂等跳过) 降为 debug —
        #   TRADING catch-up 每分钟重跑, 原 INFO 刷屏淩没真信号
        _bucket_log = logger.info if (_tagged or _missing) else logger.debug
        _bucket_log(
            f"[playbook] 盘前定桶落库 ({today}): 已标注 {_tagged} 条, ATR 缺失 {_missing} 条"
        )

        # 2026-07-24 (DEB-20260723 方向4): regime/VIX/SPY 落库补全 — DA/UE 直写指令绕过
        #   _instruction_to_db_dict, 三字段恒 NULL (07-20~23 全部缺失), 归因地基断裂.
        #   本块盘前统一补写: 每轮 assess 一次, 只填 NULL 行 (幂等, 不覆盖已有值).
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


def _compute_key_levels(data: Any) -> Dict[str, Any]:
    """从 PlaybookData 计算支撑/阻力位. 纯Python, 零LLM.

    支撑: 52周低点 + MA20 + MA50(如果有)
    阻力: 52周高点 + 前高(如果有)
    """
    try:
        fund = getattr(data, "fundamental", None)
        high_52w = getattr(fund, "high_52w", None) or 0 if fund else 0
        low_52w = getattr(fund, "low_52w", None) or 0 if fund else 0

        # 从 technical 维度获取 MA
        tech = getattr(data, "technical", None)
        ma20 = getattr(tech, "ma20", None) or 0 if tech else 0
        ma50 = getattr(tech, "ma50", None) or 0 if tech else 0

        supports = []
        resistances = []
        if low_52w > 0:
            supports.append(round(low_52w, 2))
        if ma50 > 0:
            supports.append(round(ma50, 2))
        if ma20 > 0:
            supports.append(round(ma20, 2))
        if high_52w > 0:
            resistances.append(round(high_52w, 2))

        # 去重+排序
        supports = sorted(set(supports))
        resistances = sorted(set(resistances))

        return {"support": supports, "resistance": resistances}
    except Exception:
        return {"support": [], "resistance": []}


def _persist_signal_context(
    date: str, symbol: str, data: Any, vote_result: Optional[Dict] = None,
) -> None:
    """持久化信息层上下文 (PlaybookData 6 维度 + Deepulse + LLM reasoning).

    在 pipeline 生成时调用, 把此前纯内存的 PlaybookData 落盘到 playbook_signal_context 表.
    单步异常隔离, 不影响主流程.
    """
    try:
        from app.playbook.db import PlaybookDB
        from dataclasses import asdict, is_dataclass
        import json as _json

        db = PlaybookDB()

        # PlaybookData → dict (安全提取, dataclass 字段可能 None)
        ctx: Dict[str, Any] = {"date": date, "symbol": symbol}

        # 新闻
        news = getattr(data, "news", None)
        if news:
            ctx["news_headlines"] = list(getattr(news, "headlines", []) or [])[:5]
            ctx["news_sentiment"] = getattr(news, "sentiment_score", 0) or 0

        # 内部人交易
        ins = getattr(data, "insider", None)
        if ins:
            ctx["insider_signal"] = getattr(ins, "signal", "neutral")
            ctx["insider_cluster_score"] = getattr(ins, "cluster_score", 0) or 0
            ctx["insider_net_shares"] = getattr(ins, "net_shares_30d", 0) or 0
            ctx["insider_cluster_buyers"] = list(getattr(ins, "cluster_buyers", []) or [])

        # 财报
        ern = getattr(data, "earnings", None)
        if ern:
            ctx["earnings_next_date"] = getattr(ern, "next_date", None)
            ctx["earnings_days_until"] = getattr(ern, "days_until", None)
            ctx["earnings_beat_rate"] = getattr(ern, "beat_rate", 0) or 0

        # 基本面
        fund = getattr(data, "fundamental", None)
        if fund:
            ctx["pe"] = getattr(fund, "pe", None)
            ctx["market_cap"] = getattr(fund, "market_cap", None)
            ctx["industry"] = getattr(fund, "industry", None)
            ctx["revenue_growth_yoy"] = getattr(fund, "revenue_growth_yoy", None)
            ctx["roe"] = getattr(fund, "roe", None)
            ctx["debt_to_equity"] = getattr(fund, "debt_to_equity", None)
            ctx["high_52w"] = getattr(fund, "high_52w", None)
            ctx["low_52w"] = getattr(fund, "low_52w", None)

        # Deepulse 主题雷达 (从 theme_context.json 读取 per-symbol)
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parent.parent.parent
            tc_path = root / "data" / "theme_context.json"
            if tc_path.exists():
                with open(tc_path, encoding="utf-8") as f:
                    tc = _json.load(f)
                # 2026-07-03 fix: theme_context.json 的 symbols 在顶层, 不在 domains[*].per_symbol
                bare_sym = symbol.replace("US.", "")
                tc_symbols = tc.get("symbols") or {}
                if bare_sym in tc_symbols:
                    sym_val = tc_symbols[bare_sym]
                    ctx["theme_momentum"] = sym_val.get("net_momentum")
                    ctx["theme_wind"] = sym_val.get("net_wind")
                    ctx["theme_narratives"] = list(sym_val.get("narratives", []) or [])
                # 也从 domains 补充全局主题方向 (per-domain momentum)
                if "theme_momentum" not in ctx:
                    # 用该 symbol 所属 domain 的全局 momentum 作为 fallback
                    for sym_dom in (tc_symbols.get(bare_sym, {}).get("domains") or []):
                        dom_name = sym_dom.get("domain", "")
                        dom_data = (tc.get("domains") or {}).get(dom_name, {})
                        if dom_data:
                            ctx["theme_momentum"] = dom_data.get("theme_momentum")
                            ctx["theme_wind"] = dom_data.get("sentiment_direction")
                            ctx["theme_narratives"] = list(dom_data.get("key_narratives", []) or [])[:3]
                            break
        except Exception:
            pass

        # LLM reasoning (从 decisions.db 关联)
        if vote_result:
            decision_id = vote_result.get("decision_id", "")
            if decision_id:
                try:
                    import sqlite3 as _sqlite3
                    root = Path(__file__).resolve().parent.parent.parent
                    dec_db = root / "data" / "decisions.db"
                    if dec_db.exists():
                        conn = _sqlite3.connect(str(dec_db))
                        conn.row_factory = _sqlite3.Row
                        rows = conn.execute(
                            "SELECT model_id, direction, strength, confidence, reasoning "
                            "FROM model_votes WHERE decision_id = ?",
                            (decision_id,),
                        ).fetchall()
                        conn.close()
                        ctx["llm_reasoning"] = [
                            {"model_id": r["model_id"], "direction": r["direction"],
                             "strength": r["strength"], "confidence": r["confidence"],
                             "reasoning": r["reasoning"] or ""}
                            for r in rows
                        ]
                except Exception:
                    pass

        # 原始数据快照 (完整 PlaybookData → JSON, 诊断用)
        # 2026-07-04: brief字段也编码到raw_data_json, 不加DB新列 (ponytail)
        try:
            if is_dataclass(data):
                _raw = asdict(data)
            else:
                _raw = {}
        except Exception:
            _raw = {}

        # 2026-07-04: Python计算关键位 (纯数值, 不依赖LLM)
        _key_levels = _compute_key_levels(data)
        _raw["key_levels"] = _key_levels

        # 2026-07-04: 从LLM reasoning中提取brief字段 (已由_parse_response编码到reasoning)
        # reasoning格式: "brief text | {json}" — 解析JSON部分
        try:
            _reasoning_str = ""
            if vote_result:
                _reasoning_str = str(vote_result.get("reasoning", "") or "")
            elif ctx.get("llm_reasoning"):
                _first = ctx["llm_reasoning"][0] if isinstance(ctx["llm_reasoning"], list) else {}
                _reasoning_str = str(_first.get("reasoning", "") or "") if isinstance(_first, dict) else ""
            if "|" in _reasoning_str:
                _json_part = _reasoning_str.rsplit("|", 1)[-1].strip()
                _brief_data = _json.loads(_json_part) if _json_part.startswith("{") else {}
                for _bf in ("brief_conclusion", "brief_reasons", "brief_risks",
                            "entry_strategy", "key_support", "key_resistance", "stop_trigger"):
                    if _bf in _brief_data:
                        _raw[_bf] = _brief_data[_bf]
        except Exception:
            pass

        ctx["raw_data"] = _raw

        db.save_signal_context(ctx)
    except Exception as e:
        logger.warning(f"[playbook] signal_context 持久化失败 {symbol}: {e}")


def _instruction_to_db_dict(inst, regime_ctx, today: str) -> Dict[str, Any]:
    """Instruction dataclass → save_instruction 期望的 dict (字段映射, spec 6.1)"""
    conditions = list(getattr(inst, "conditions", []) or [])
    risks = list(getattr(inst, "risks", []) or [])
    dq = getattr(inst, "data_quality", {}) or {}
    ok_dims = sum(1 for v in dq.values() if v == "ok")
    return {
        "date": today,
        "symbol": inst.symbol,
        "direction": inst.direction,
        "skip_reason": getattr(inst, "skip_reason", "") or "",
        "entry_price": inst.entry_price,
        "stop_loss": inst.stop_loss,
        "take_profit": inst.take_profit,
        "position_size": inst.position_size,
        "risk_amount": getattr(inst, "risk_amount", 0.0),
        "risk_reward": getattr(inst, "risk_reward", 0.0),
        "confidence": inst.confidence,
        "conditions": conditions,
        "thesis": getattr(inst, "thesis", "") or "",
        "sector": getattr(inst, "sector", "") or "",
        "risks": risks,
        "market_regime": regime_ctx.regime.value,
        "vix_at_generation": regime_ctx.vix,
        "spy_trend_at_generation": "above_ma20" if regime_ctx.spy_ma20 >= regime_ctx.spy_ma50 else "below_ma20",
        "data_quality": dq,
        "ok_dimensions": ok_dims,
        "matched_patterns": [],
        "pattern_verdict": None,
        "raw_instruction": {
            "entry_price_source": getattr(inst, "entry_price_source", ""),
            "atr_source": getattr(inst, "atr_source", ""),
            # 2026-07-11 Stage 1: DA 真实论点存入 raw json (不占独立列, 不覆盖模板 thesis)
            "da_thesis": getattr(inst, "da_thesis", "") or "",
        },
        "decision_id": getattr(inst, "decision_id", "") or "",
        "invalidation": getattr(inst, "invalidation", "") or "",
        # F1 (2026-07-31): 锚偏离度落库键 (save_instruction :202 读取, record-only)
        "anchor_dev_pct": getattr(inst, "anchor_dev_pct", None),
        # 2026-08-05 (DEB-20260805 项C 终裁): model_used 归因补全 — 本映射走 TechnicalFallbackAdapter
        #   确定性链 (无 LLM), 原恒 NULL (NVDA 08-04 topup_pool 缺口实例). UE 路径由
        #   llm_universe_scan 记录级写真实模型名, DA skill 写 config.LLM_MODEL, 互不覆盖.
        "model_used": getattr(inst, "model_used", None) or "deterministic",
        "horizon": getattr(inst, "horizon", "short"),
        "horizon_confidence": getattr(inst, "horizon_confidence", 0.0),
        "horizon_reasoning": getattr(inst, "horizon_reasoning", {}),
    }


def _run_ic_backfill_only(scheduler, today: str) -> None:
    """ponytail: 幂等跳过daily_tracking时仍跑IC backfill — Bug A修复.

    2026-07-23: 加日期级幂等 — 主循环每2分钟进一次幂等guard分支, 曾导致
    30天窗口249条指令被全天反复重算 (119次/日, 纯无用功: 日线收盘价每天
    只变一次, backfill 每天一次即完备). 重启后重跑一次无害.
    """
    if getattr(scheduler, "_ic_backfilled_date", None) == today:
        return
    try:
        from app.playbook.ic_tracker import backfill_forward_returns
        from app.playbook.db import PlaybookDB
        db = PlaybookDB()
        market_data = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if market_data:
            backfilled = backfill_forward_returns(db, market_data, today)
            scheduler._ic_backfilled_date = today  # 日期级幂等置位 (成功才置, 失败下轮重试)
            logger.info(f"[playbook] IC backfill (幂等路径): {backfilled} 条 (今日首次, 后续轮跳过)")
    except Exception as e:
        logger.debug(f"[playbook] IC backfill (幂等路径) 失败: {e}")


# ── 入口 2: 盘后追踪 (shadow PnL + MFE/MAE + 因子更新 + 飞书) ──────────
def run_playbook_track(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘后追踪 (Layer 3 每日盘后)。

    幂等: scheduler._playbook_tracked_date == today 即跳过 (force=True 可重跑)。
    """
    try:
        today = _today_et_str()

        if not _playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘后追踪 (上线闸门关闭)")
            return None

        if not force:
            if not _is_trading_day_today():
                logger.debug(f"[playbook] {today} 非交易日, 跳过盘后追踪")
                return None
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB
            # 2026-06-23 修复: any→all. 原逻辑 any(...) = 批次内任意一条到终态即跳过整批,
            #   导致 (a) 盘后追踪后才生成的指令永久不追踪 (b) 'holding' 仓位被同批的终态指令
            #   连带跳过, 无法每日重追踪直到产生最终 outcome. 改为 all: 仅当全部指令到终态
            #   (win/loss/skipped/missed) 才跳过, 否则重跑追踪未终态的.
            # 2026-06-23 修复(OKR): 去掉 "shadow_pnl is not None" 条件 — 旧逻辑把
            #   shadow_pnl=0.0 但 outcome=NULL 的指令 (如未走 SKIP 分支的 SKIP 指令)
            #   误判为已追踪, 导致其 outcome 永远停留在 NULL, OKR 样本泄漏.
            #   终态必须由 outcome 字段本身判定, shadow_pnl 只是辅助浮盈不计终态.
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                instructions = db.get_instructions_by_date(today)

                # 内存幂等优先: 已标记今日追踪完成 → 跳过 (空列表/正常列表都覆盖)
                if getattr(scheduler, "_playbook_tracked_date", None) == today:
                    _run_ic_backfill_only(scheduler, today)
                    return None

                # 检查是否已追踪：所有指令均有终态 outcome (win/loss/skipped/missed)
                # outcome="holding"/NULL 均不算终态 — 持仓中或未追踪的指令需每日重追踪直到产生最终 outcome
                # 空列表 (PRE_MARKET错过/重启) 也算已追踪 — 没有指令就没有需要追踪的
                tracked = not instructions or all(
                    inst.get("outcome") in ("win", "loss", "skipped", "missed")
                    for inst in instructions
                )
                if tracked:
                    scheduler._playbook_tracked_date = today
                    # ponytail: backfill 不能跳过 — Bug A: 幂等guard跳过backfill导致IC永远0
                    # 即使今日指令已全部终态, 前几天的指令仍需要 T+3/T+5 backfill
                    _run_ic_backfill_only(scheduler, today)
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")
                if getattr(scheduler, "_playbook_tracked_date", None) == today:
                    logger.debug(f"[playbook] {today} 已追踪 (内存标志), 跳过")
                    return None

        logger.info(f"[playbook] 📊 盘后追踪开始 (date={today})")

        from app.playbook.db import PlaybookDB
        from app.playbook.shadow_tracker import ShadowTracker
        from app.playbook.feishu_push import PlaybookPusher

        db = PlaybookDB()

        tracker = ShadowTracker(
            playbook_db=db,
            market_data=getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None),
            trade_store=getattr(scheduler, "trade_store", None),
        )
        # OKR (2026-06-23): EOD 强制平仓兜底 — monitor_playbook_sim_exits 的 PRE_CLOSE_EXIT
        # 只在 15:52-16:00 ET 的 8 分钟窗口内、且实时报价可用时才触发; 一旦该窗口错过 (报价
        # 失败/时序错位), 仓位永久孤儿化 (exit_price=0), 永不产生 win/loss → OKR 样本永不增长.
        # 这里在盘后追踪前, 对所有仍未平仓的 PLAYBOOK_SIM 仓位用当前价 (≈收盘价) 强制平仓,
        # 保证每个执行仓位当天必出最终 outcome. force=True 历史回补时也适用 (按当前价实现已开仓位).
        # 2026-06-26: PLAYBOOK_SHADOW 已废弃, 只兜底 PLAYBOOK_SIM 真实模拟仓位.
        try:
            sim_closed = _force_close_orphaned_sim_positions(scheduler)
            if sim_closed:
                logger.info(f"[playbook] EOD sim 强制平仓: {sim_closed} 个 sim 仓位已 broker 平仓")
        except Exception as _e:
            logger.warning(f"[playbook] EOD sim 强制平仓异常 (已隔离): {_e}")

        tracker.daily_tracking(today)

        # 双层因子统计每日增量更新 (spec 6.3) — 冷启动期 trades 不足时自动返回 0
        # 注意: Screener._load_factor_ic_adjustments 已在运行时动态调整因子权重
        # (IC>0.05→+1, IC<-0.05→-1, 读factor_ic_latest.json), 无需额外调用WeightAdjuster
        # 2026-07-04: 原计划接通WeightAdjuster但发现与Screener重复, 撤销避免双重惩罚
        try:
            from app.playbook.factor_analyzer import FactorAnalyzer
            updated = FactorAnalyzer(db).update_all_stats()
            logger.info(f"[playbook] 因子统计更新: {updated}")
        except Exception as e:
            logger.debug(f"[playbook] 因子更新失败 (不影响追踪): {e}")

        # 2026-06-26: Horizon 收益模型每日增量更新（样本不足时跳过，避免无效 CPU）
        try:
            from app.playbook.horizon_return_model import HorizonReturnModel
            total_with_outcome = db._conn().execute(
                "SELECT COUNT(*) FROM playbook_instructions WHERE outcome IN ('win','loss')"
            ).fetchone()[0]
            if total_with_outcome >= 50:
                hrm_updated = HorizonReturnModel(db).update(days_back=90)
                logger.info(f"[playbook] Horizon 收益模型更新: {hrm_updated}")
            else:
                logger.debug(
                    f"[playbook] Horizon 收益模型跳过 (样本不足: {total_with_outcome} < 50)"
                )
        except Exception as e:
            logger.debug(f"[playbook] Horizon 收益模型更新失败 (不影响追踪): {e}")

        # IC 追踪 (Phase 1: 2026-06-19) — 回填前瞻收益 + 计算 IC
        try:
            from app.playbook.ic_tracker import backfill_forward_returns, compute_playbook_ic, format_ic_report
            market_data = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
            if market_data:
                backfilled = backfill_forward_returns(db, market_data, today)
                logger.info(f"[playbook] IC 前瞻收益回填: {backfilled} 条")
                
                # 每周五推送 IC 报告
                from datetime import datetime
                now_et = datetime.now(_now_et())
                if now_et.weekday() == 4:  # 周五
                    ic_report = compute_playbook_ic(db, days=30)
                    if ic_report.get("sample_t3", 0) > 0:
                        report_text = format_ic_report(ic_report)
                        try:
                            PlaybookPusher(db)._send(report_text)
                        except Exception as e:
                            logger.debug(f"[playbook] IC 周报推送失败: {e}")
        except Exception as e:
            logger.debug(f"[playbook] IC 追踪失败 (不影响主流程): {e}")

        # P2 (2026-06-29): Signal IC 统一回填 — 与 Playbook IC 共享 router 缓存
        try:
            from app.core.feature_flags import FeatureFlags
            if FeatureFlags().is_enabled("unified_backfill"):
                from app.analysis.signal_ic_tracker import run_signal_ic_backfill
                signal_stats = run_signal_ic_backfill(dry_run=False)
                total_signal = sum(signal_stats.values())
                logger.info(f"[playbook] Signal IC 统一回填: {total_signal} 条, {len(signal_stats)} 只股票")
        except Exception as e:
            logger.debug(f"[playbook] Signal IC 统一回填失败 (不影响主流程): {e}")

        # 2026-07-03: 同步 signal_lifecycle 表 (trade_records ↔ signal_snapshots 关联)
        # 之前此脚本只能手动运行, 导致 fwd_ret 字段长期不更新
        try:
            import subprocess, sys
            subprocess.run(
                [sys.executable, "scripts/update_signal_lifecycle.py"],
                cwd=str(Path(__file__).resolve().parent.parent.parent),
                timeout=60, capture_output=True,
            )
            logger.info(f"[playbook] signal_lifecycle 同步完成")
        except Exception as e:
            logger.debug(f"[playbook] signal_lifecycle 同步失败 (不影响主流程): {e}")

        try:
            PlaybookPusher(db).push_evening_tracking(today)
        except Exception as e:
            logger.debug(f"[playbook] 盘后推送失败 (不影响追踪): {e}")

        scheduler._playbook_tracked_date = today
        return {"date": today, "tracked": True}

    except Exception as e:
        logger.exception(f"[playbook] 盘后追踪异常 (已隔离): {e}")
        return {"error": str(e)}


# ── 入口 3: 周报 + 权重闭环 (周五) ─────────────────────────────────────
def run_playbook_weekly(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    每周五: 双层因子分析 + 权重自动调整 + LLM 周报 + 飞书推送 (Layer 3 每周)。

    幂等: scheduler._playbook_weekly_done == 本周结束日 即跳过 (force=True 可重跑)。
    """
    try:
        from datetime import datetime, timedelta
        now_et = datetime.now(_now_et())

        # 本周结束日 (周五, 若今天是周五则是今天)
        days_to_friday = (4 - now_et.weekday()) % 7
        week_end = (now_et.date() + timedelta(days=days_to_friday)).isoformat()

        if not _playbook_enabled() or not _playbook_cfg("enable_weekly", True):
            logger.debug("[playbook] playbook 周报闸门关闭, 跳过周度复盘")
            return None

        if not force:
            # 幂等检查: 先查内存标志 (快路径), 再查 DB (重启兜底)
            if getattr(scheduler, "_playbook_weekly_done", None) == week_end:
                logger.debug(f"[playbook] 本周 ({week_end}) 已出周报 (内存标志), 跳过")
                return None
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                regime_log = db.get_regime_log(days=7, include_markers=True)
                has_weekly = any(
                    r.get("regime") == "weekly_report" or "weekly" in str(r.get("regime", ""))
                    for r in regime_log
                )
                if has_weekly:
                    logger.info(f"[playbook] 本周 ({week_end}) DB 已有周报，跳过 (幂等)")
                    scheduler._playbook_weekly_done = week_end
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")

        logger.info(f"[playbook] 📈 周度复盘开始 (week_end={week_end})")

        from app.playbook.db import PlaybookDB
        from app.playbook.weekly_report import WeeklyReportGenerator

        db = PlaybookDB()
        generator = WeeklyReportGenerator(playbook_db=db)
        report = generator.generate(_today_et_str())

        # 权重自动闭环 (正收益因子 +1, 负收益 -1) + 周报飞书推送
        generator.format_report(report)
        try:
            generator.push_report(report)
        except Exception as e:
            logger.debug(f"[playbook] 周报推送失败 (不影响分析): {e}")

        # P0 配套 (2026-07-11): edge 影子闸门 & 信号质量 周度汇总推送 (飞书)
        try:
            from app.decision.edge_weekly_report import push_edge_weekly_report
            push_edge_weekly_report(days=7)
        except Exception as e:
            logger.debug(f"[playbook] edge 周报推送失败 (不影响分析): {e}")

        # 周五风控熔断状态检查 (spec 5.4) — 仅记录告警, 不阻断
        try:
            from app.playbook.risk_circuit_breaker import RiskCircuitBreaker
            status = RiskCircuitBreaker(db).check_status(_get_total_equity(scheduler))
            if status.get("action") in ("reduce", "stop"):
                logger.warning(f"[playbook] 风控熔断触发: {status}")
        except Exception as e:
            logger.debug(f"[playbook] 风控检查失败 (不影响周报): {e}")

        # Phase 1: 反馈学习 (从用户跳过/修改中学习)
        try:
            from app.playbook.feedback_learner import FeedbackLearner
            learner = FeedbackLearner(db)
            feedback_report = learner.learn_from_feedback(days=7)
            
            # 如果有足够的反馈数据，将学习报告附加到周报
            if feedback_report.get("feedback_count", 0) > 0:
                report["feedback_learning"] = feedback_report
                logger.info(f"[playbook] 反馈学习完成: {feedback_report['feedback_count']} 条反馈")
                
                # 如果有 Screener 调整建议，记录到日志（不自动应用，需要人工确认）
                screener_adj = feedback_report.get("screener_adjustments", {})
                if screener_adj:
                    logger.info(f"[playbook] Screener 调整建议: {screener_adj}")
                    report["screener_adjustments_suggested"] = screener_adj
        except Exception as e:
            logger.debug(f"[playbook] 反馈学习失败 (不影响周报): {e}")

        logger.info(f"[playbook] ✅ 周度复盘完成: {report.get('week', week_end)}")
        scheduler._playbook_weekly_done = week_end
        # DB 幂等标记: 重启后仍可跳过
        try:
            from app.utils.timezone import now_utc
            db.save_regime_log({
                "date": week_end,
                "regime": "weekly_report",
                "vix": 0,
                "spy_price": 0,
                "spy_ma20": 0,
                "spy_ma50": 0,
                "score": 0,
                "notes": f"weekly_report generated at {now_utc().isoformat()}",
            })
        except Exception as e:
            logger.debug(f"[playbook] DB 幂等标记写入失败: {e}")
        return report

    except Exception as e:
        logger.exception(f"[playbook] 周度复盘异常 (已隔离): {e}")
        return {"error": str(e)}


# ── 入口 4: 盘中自动执行 Paper Trade (Phase 1: 2026-06-19) ──────────


def _has_consecutive_losses(n: int = 5) -> bool:
    """检查最近是否有连续 n 笔亏损 (按时间倒序, 仅看终态 win/loss).

    fail-close: 异常返回 True (阻止实盘).
    """
    try:
        from app.playbook.db import PlaybookDB
        db = PlaybookDB()
        instructions = db.get_recent_instructions(limit=200)
        consecutive = 0
        for inst in instructions:
            outcome = inst.get("outcome")
            if outcome == "loss":
                consecutive += 1
                if consecutive >= n:
                    return True
            elif outcome == "win":
                consecutive = 0
            # 其他 outcome (holding/skipped/missed/null) 不重置也不累加
        return False
    except Exception as e:
        logger.warning(f"[playbook] 连续亏损检查失败 (fail-close): {e}")
        return True


def _check_graduation_gate() -> Dict[str, Any]:
    """毕业门禁 (Phase 2, 2026-06-24) — 实盘自动交易的前置条件.

    检查 (在模拟数据上):
      1. IC(T+3) > 0.05  (ic_tracker._check_graduation)
      2. 样本量 ≥ 30    (ic_tracker._check_graduation)
      3. 无连续 5 笔亏损 (_has_consecutive_losses)

    fail-close: 任何异常都返回 graduated=False (阻止实盘).
    用于: playbook execute_mode=live 路径 + dashboard 实盘开关.

    Returns: {"graduated": bool, "checks": {...}, "reason": str}
    """
    try:
        from app.playbook.ic_tracker import compute_playbook_ic, _check_graduation
        from app.playbook.db import PlaybookDB

        db = PlaybookDB()
        ic_report = compute_playbook_ic(db, days=30)
        grad = _check_graduation(ic_report)

        checks = dict(grad.get("checks", {}))
        # 补 "无连续5亏" 检查
        checks["no_consecutive_5_losses"] = not _has_consecutive_losses(5)
        graduated = all(checks.values()) and grad.get("graduated", False)

        failed = [k for k, v in checks.items() if not v]
        return {
            "graduated": graduated,
            "checks": checks,
            "ic_t3": grad.get("ic_t3"),
            "sample_count": grad.get("sample_count"),
            "ic_t1": ic_report.get("overall_ic_t1"),
            "ic_t5": ic_report.get("overall_ic_t5"),
            "sample_t1": ic_report.get("sample_t1", 0),
            "sample_t3": ic_report.get("sample_t3", 0),
            "sample_t5": ic_report.get("sample_t5", 0),
            "reason": "" if graduated else f"未毕业: {failed}",
        }
    except Exception as e:
        logger.error(f"[playbook] graduation gate 异常 (fail-close): {e}")
        return {"graduated": False, "checks": {}, "reason": f"门禁异常: {e}"}


def _emit_exec_gate_audit(inst: Dict[str, Any], *, action: str, entry: float,
                          size: float, notional: float, extra: Optional[Dict[str, Any]] = None) -> None:
    """统一收口审计 (2026-07-17): 记录每个到达执行漏斗的候选单 → data/shadow/exec_gate_audit.jsonl.

    背景 (2026-07-18 显式化): DA-bridge / UE 直写 DB 的指令绕过 _generate_instructions 的
    G6 confidence 门与 EdgeShadowGate; 实盘走量几乎全是 DA-bridge/UE → 主路径无 confidence 质量门.
    这是当前刻意设计, 非疏漏: IC/confidence 属未校准的历史/legacy 数据, 不足以作为新统一管线的
    下单门 (校准前用它拦单 = 拿噪声当信号). 风控由"安全带"承担 — 触发带 + VWAP 方向门 +
    敞口三闸 (单仓4% / 日名义80% / 日30笔) + vol_bucket SL 钳制, 而非 confidence 门.
    本函数装在 run_playbook_auto_execute 下单前唯一汇合点, 无论来源一律记录, 让全链路可见;
    G6 若"假设生效"会拦掉哪些、这些后来盈亏如何, 由本审计 + edge_gate 影子持续观测,
    待实盘样本充足、confidence 校准为正后再评估是否对走量激活质量门.

    同时对所有执行候选 (含 DA-bridge) 跑一次 EdgeShadowGate — edge 判定首次覆盖 DA-bridge.
    纯影子: 只记录, 绝不拦单、不改任何交易动作. best-effort, 异常一律吞掉.
    """
    try:
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        _did = str(inst.get("decision_id", "") or "")
        if _did.startswith("DA-"):
            _source = "da_bridge"
        elif _did.startswith("universe_expansion"):
            _source = "universe_expansion"
        elif _did:
            _source = "internal"
        else:
            _source = "unknown"

        _direction = inst.get("direction", "")
        _conf = float(inst.get("confidence", 0) or 0)

        # EdgeShadowGate — 数据驱动 edge 判定首次覆盖 DA-bridge (纯影子, live_pass=None 语义: 无实盘门)
        _edge = {}
        try:
            from app.decision.edge_shadow_gate import get_edge_shadow_gate
            _edge = get_edge_shadow_gate().evaluate(
                inst.get("symbol", ""), _direction, _conf, live_pass=True, log=False,
            ) or {}
        except Exception:
            _edge = {}

        _row = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "date": _dt.now().strftime("%Y-%m-%d"),
            "inst_id": inst.get("id"),
            "symbol": inst.get("symbol", ""),
            "direction": _direction,
            "confidence": round(_conf, 2),
            "source": _source,
            "decision_id": _did,
            "action": action,  # executed / skipped_* / error
            "entry": round(float(entry or 0), 4),
            "size": int(size or 0),
            "notional": round(float(notional or 0), 2),
            "edge_bucket": _edge.get("bucket"),
            "edge_expectancy": _edge.get("expectancy"),
            "edge_verdict": _edge.get("edge_verdict"),
            "edge_reason": _edge.get("reason"),
        }
        if extra:
            _row.update(extra)

        _p = _Path(__file__).resolve().parents[2] / "data" / "shadow" / "exec_gate_audit.jsonl"
        _p.parent.mkdir(parents=True, exist_ok=True)
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 纯审计, 绝不影响交易主流程


def run_playbook_auto_execute(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘中自动执行 triggered 的指令 (Paper Trade 模式)。

    流程:
    1. 检查 playbook.auto_execute 配置开关 (默认 false)
    2. 获取今日指令
    3. 对每条 triggered=1 且 executed=0 且 direction != SKIP 的指令，自动执行
    4. 调用 playbook_service.execute_instruction()

    幂等: 同一指令不会重复执行 (executed=1 后跳过)。
    """
    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.playbook_service import PlaybookService

        # 1. 检查配置开关
        if not _playbook_cfg("auto_execute", False):
            logger.info("[playbook] auto_execute=false, 跳过自动执行")
            return None

        today = _today_et_str()
        if not force and not _is_trading_day_today():
            logger.info(f"[playbook] {today} 非交易日, 跳过自动执行")
            return None

        # 2026-06-24: 开盘后5分钟内不执行 — 避开开盘最大噪声 (gap fill + 做市商调仓)
        # 数据依据: 6/23 shadow 22笔中21笔在 9:31:14 批量执行, 2笔瞬间止损 (78ms/473ms)
        # 延迟5min → 入场价更稳定, SL不被秒杀
        from datetime import datetime as _dt
        _et_tz = _now_et()
        _now_et_dt = _dt.now(_et_tz)
        _market_open_min = 9 * 60 + 30  # 9:30 ET
        _now_min = _now_et_dt.hour * 60 + _now_et_dt.minute
        if not force and _now_min < _market_open_min + 5:
            logger.info(f"[playbook] 开盘5分钟内不执行 (now={_now_et_dt.strftime('%H:%M')} ET)")
            return None

        # 2026-07-19 (DEB-round3 #1): bridge 三态告警 — auto_execute 每30min入口,
        #   覆盖"启动后文件仍缺失"盲区 (bridge 仅启动时加载一次). 当日去重, 不阻断.
        _check_bridge_health_and_alert("auto_execute")

        # 2026-07-31 全链清理批2② (用户裁): kill switch 直挂 playbook 下单入口.
        #   原链路 flag→scan_loop(60s)→RiskStateMachine→此门 ≈90s 延迟且 scan_loop
        #   线程一死即断路; 现直读标记文件, 与 order_executor/engine 同函数同口径.
        #   fail-close 与 order_executor.py:3430 一致 (检查异常即不下单).
        try:
            from app.us_pipeline.emergency_interceptor import check_kill_switch
            if check_kill_switch():
                logger.warning("🚨 [playbook] Kill Switch 已激活, 跳过 auto_execute")
                return {"date": today, "executed": 0, "skipped": 0, "reason": "kill_switch"}
        except Exception as _ks_err:
            logger.warning(f"🚨 [playbook] Kill Switch 检查异常, fail-close 跳过本轮: {_ks_err}")
            return {"date": today, "executed": 0, "skipped": 0, "reason": "kill_switch_check_error"}

        # 2026-06-30: 全局风险/市场环境检查 — 极端 regime 或风险状态禁止新开仓
        try:
            from app.playbook.market_regime import MarketRegime, MarketRegimeGate
            from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
            regime_ctx = MarketRegimeGate().assess()
            if regime_ctx.regime == MarketRegime.NO_TRADE:
                logger.warning(
                    f"[playbook] 当前 regime=NO_TRADE (VIX={regime_ctx.vix:.2f}), 跳过 auto_execute"
                )
                return {"date": today, "executed": 0, "skipped": 0, "reason": "regime_no_trade"}
            risk_state = RiskStateMachine().get_state()
            if risk_state in (RiskState.RISK_OFF, RiskState.HALT):
                logger.warning(
                    f"[playbook] 当前 risk_state={risk_state}, 跳过 auto_execute"
                )
                return {"date": today, "executed": 0, "skipped": 0, "reason": f"risk_state_{risk_state}"}
        except Exception as e:
            # R1a (2026-07-19, DEB-round2 Ω.3-3): 预检 fail 模式由 risk.preflight_fail_mode 驱动.
            #   closed → 跳过本轮 auto_execute (regime/risk_state 未知时不开仓). SIM 默认 open.
            try:
                from app.core.config_container import get_config as _get_cfg_r1a
                _r1a_mode = _get_cfg_r1a().get_dotted("risk.preflight_fail_mode", "open")
            except Exception:
                _r1a_mode = "open"
            if _r1a_mode == "closed":
                logger.error(f"[playbook] 风险状态检查异常 (fail-close 跳过本轮): {e}")
                return {"date": today, "executed": 0, "skipped": 0, "reason": "preflight_fail_close"}
            logger.warning(f"[playbook] 风险状态检查异常 (放行): {e}")

        db = PlaybookDB()
        service = PlaybookService(db)
        instructions = db.get_instructions_by_date(today)

        logger.info(f"[playbook] auto_execute 开始: {len(instructions)} 条指令 (now={_now_et_dt.strftime('%H:%M')} ET)")

        if not instructions:
            logger.info(f"[playbook] {today} 无指令, 跳过自动执行")
            return {"date": today, "executed": 0, "skipped": 0}

        executed_count = 0
        skipped_count = 0
        errors = []

        # P0 fix (2026-06-25): 开仓前 buying_power 总量检查
        # 根因: 批量同时触发 (6笔/2秒), 每笔独立算仓位 → $388K on $82K 账户
        # 修复: 累计已开 PLAYBOOK_SIM notional, 超过预算上限则跳过后续
        _max_deploy = 0.0
        try:
            from app.core.db_utils import safe_sqlite_connect
            from app.core.paths import get_trade_records_db as _get_trdb
            _bp_conn = safe_sqlite_connect(_get_trdb(), timeout=5)
            _row = _bp_conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trade_records "
                "WHERE source_type='PLAYBOOK_SIM' AND (exit_time IS NULL OR exit_time = '') "
                "AND entry_time >= date('now','start of day','-1 day')"
            ).fetchone()
            _bp_conn.close()
            _max_deploy = _row[0] if _row else 0.0
        except Exception as _r1b_err:
            # R1b (2026-07-19, DEB-round2 Ω.3-1): 预算门无条件 fail-close (不受配置开关控制).
            #   本地 SQLite 只读查询失败 = DB 级严重事件 (非网络抖动), 不可静默放行.
            #   _max_deploy=+inf 视为预算已用满 → 拦后续所有开仓 (宁可误拦一轮, 不可无上限开仓).
            #   历史根因: 2026-06-25 静默放行 → $388K on $82K 账户 (见上方注释).
            _max_deploy = float("inf")
            logger.error(f"[playbook] R1b 预算查询失败 → fail-close 拦后续开仓: {_r1b_err}")

        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()

        # 2026-06-29: 统一从 capital_safety.get_available_budget_for_auto() 取预算
        # 替代旧的 buying_power × 0.8 — 该方法已扣除 manual+auto 已用敞口,
        # 且受 conservative/normal/aggressive 模式约束, Playbook 与 ORB 共享同一预算源
        from app.risk.capital_safety import get_capital_safety_system
        _bp_limit = 0.0
        _bp_source = "unavailable"
        try:
            _cs = get_capital_safety_system()
            _bp_limit = _cs.get_available_budget_for_auto()
            _bp_source = f"capital_safety_auto_budget"
        except Exception as e:
            logger.warning(f"[playbook] capital_safety auto_budget 读取失败: {e}")
        # 降级: 用 sizing_base × 80% 兜底
        if _bp_limit <= 0:
            _risk_base = _resolve_sizing_base(pb_cfg)  # R4 SSOT: 富途实时净值
            _bp_limit = _risk_base * 0.8
            _bp_source = f"fallback_{_risk_base:.0f}×80%"
        logger.info(f"[playbook] 预算上限: ${_bp_limit:,.0f} (来源={_bp_source}), 已部署=${_max_deploy:,.0f}")

        # 2026-06-28: 单日交易 guardrail — 防止恢复BUY过滤后市场过冷/过热时爆量
        # 2026-07-18 (R4 SSOT): 日名义额度与单笔风险共用 _resolve_sizing_base (富途实时净值),
        #   消除双尺子; 净值取不到时兜底 config risk_base_capital。
        _risk_base_capital = _resolve_sizing_base(pb_cfg)
        _daily_notional_cap_pct = float(pb_cfg.get("daily_notional_cap_pct", 0.20))
        _daily_notional_cap = _risk_base_capital * _daily_notional_cap_pct
        _max_daily_positions = int(pb_cfg.get("max_daily_positions", 5))

        # 2026-07-09: 从DB查当日已开仓notional/count，替代每次调用从0开始 (P0修复)
        _today_opened_notional = 0.0
        _today_opened_count = 0
        try:
            import sqlite3 as _sql3
            _db_path = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
            _db_conn = _sql3.connect(_db_path)
            # S3 (2026-07-19, DEB-round2 Ω.3-6): 日名义额度仅统计 Playbook 自身交易.
            #   修复前无 source_type 过滤 → 当日手动/ORB/主线交易 notional 挤占 Playbook 的
            #   daily_notional_cap, 导致 Playbook 被误拦 (保守方向, 不漏放但漏信号).
            #   与同函数 R1b 查询 (2315 source_type='PLAYBOOK_SIM') 口径对齐.
            _row = _db_conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0), COUNT(*) "
                "FROM trade_records WHERE substr(entry_time,1,10) = ? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')",
                (today,)
            ).fetchone()
            _today_opened_notional = float(_row[0] or 0)
            _today_opened_count = int(_row[1] or 0)
            _db_conn.close()
        except Exception as _e:
            logger.warning(f"[playbook] 查询当日已开仓失败, 从0开始: {_e}")

        logger.info(
            f"[playbook] 单日 guardrail: notional_cap=${_daily_notional_cap:,.0f} "
            f"({_daily_notional_cap_pct*100:.0f}% of ${_risk_base_capital:,.0f}), "
            f"max_daily_positions={_max_daily_positions}, "
            f"已开仓: ${_today_opened_notional:,.0f}/{_today_opened_count}笔"
        )

        # 按来源优先级 + confidence 降序 — 购买力优先分配给高优先来源的最强信号.
        # 漏斗最后一层: 多层过滤后剩N只, BP只够开2-3笔 → 先执行优先级最高的.
        # [ADR-020] 2026-07-17: UE(宇宙扩张) 执行优先级高于 DA — 新机会/新资讯动态标的
        #   优先占预算, DA 静态盘次之. 以 source_priority 为主键、confidence 为次键,
        #   与 confidence 语义解耦 (UE conf 上限50 仍保留在 llm_universe_scan.py, 不为排序而篡改).
        #   (取代 2026-07-09 "bridge候选(DA-)优先" 旧策略.)
        def _src_priority(inst):
            did = (inst.get("decision_id", "") or "")
            if did.startswith("universe_expansion"):
                return 2   # UE 最优先 (用户指定)
            if did.startswith("DA-"):
                return 1   # DA 次之
            return 0       # VotingCoordinator / 其他
        instructions.sort(key=lambda x: (
            _src_priority(x),
            x.get("confidence", 0) or 0
        ), reverse=True)

        # 2026-08-09 性能预审: broker 持仓是轮级全局状态, 循环外查一次即可.
        #   原实现每条未执行指令调 get_positions (OpenD 交易连接, 放量后 40+指令×78轮/日
        #   = 数千次连接调用), 同轮内重复查询结果不变 → 提升到循环外, 一轮一次.
        #   失败时降级为空列表 (与原先每条指令独立失败返回 [] 语义一致, 防误拦).
        try:
            from app.execution.dual_broker import get_dual_broker
            _dual = get_dual_broker()
            _broker_positions = _dual.get_positions(target_env="sim") or []
        except Exception as _bp_err:
            logger.warning(f"[playbook] 轮级 broker 持仓查询失败 (降级空表): {_bp_err}")
            _broker_positions = []

        for inst in instructions:
            # 跳过已执行、SKIP 方向的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue
            # 2026-07-19 (DEB-round3 #2): 漂移作废指令不再参与触发 (特异性匹配, 防误杀)
            if (inst.get("skip_reason") or "") == "thesis_drift_invalidate":
                continue
            # 2026-07-19 (DEB-round3 #3-B 消费端): expires_at 过期指令跳过 (SCAN 24h 失效)
            if _is_instruction_expired(inst):
                logger.info(f"[playbook] {inst.get('symbol')} 指令已过期 (expires_at={inst.get('expires_at')}), 跳过")
                skipped_count += 1
                continue

            # 2026-06-29 P0: 执行前查broker持仓 — 有仓则跳过，防重复下单+孤儿仓循环
            # 根因: fill=0时playbook拒绝记账但broker已成交, 下次auto_execute又下单 → 仓位叠加
            # 2026-08-09 性能预审: 持仓已提到循环外查一次 (_broker_positions), 此处仅复用.
            _symbol = inst.get("symbol", "")
            try:
                # F1 (2026-07-21): 方向感知 — 持有 SHORT 不应拦截新 BUY (反之亦然),
                #   否则合法反向再入被误标 already_held. qty>0=LONG, qty<0=SHORT.
                _want_long = inst.get("direction", "").upper() == "BUY"
                _already_held = any(
                    p.get("code") == _symbol
                    and abs(float(p.get("qty", 0) or 0)) > 0
                    and ((float(p.get("qty", 0) or 0) > 0) == _want_long)
                    for p in _broker_positions
                )
                if _already_held:
                    # P0 (2026-07-21): already_held 不再一律标 $0 —
                    #   broker 已有仓往往是 playbook 自己当日的成交 (fill=0 拒记账后 broker 已成交,
                    #   或先前批次已开). 旧逻辑把自成交标 execution_price=0/pnl=0/outcome=already_held,
                    #   导致日报盈亏低报、归因失真. 先对账 playbook 自己的当日 trade_record 回填真实价/盈亏,
                    #   仅当确无 playbook 记录 (外部/手动/ORB 持仓) 才退回旧 already_held 标记.
                    _pb_rec = None
                    try:
                        import sqlite3 as _sql3_ah
                        _dir_norm = "LONG" if inst.get("direction", "").upper() == "BUY" else "SHORT"
                        _conn_ah = _sql3_ah.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db", timeout=10.0)
                        _conn_ah.row_factory = _sql3_ah.Row
                        _pb_rec = _conn_ah.execute(
                            "SELECT entry_price, exit_price, exit_time, exit_reason, realized_pnl, pnl_pct, quantity "
                            "FROM trade_records WHERE symbol = ? AND substr(entry_time,1,10) = ? "
                            "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                            "AND UPPER(direction) = ? ORDER BY id DESC LIMIT 1",
                            (_symbol, today, _dir_norm)
                        ).fetchone()
                        _conn_ah.close()
                    except Exception as _ah_q_err:
                        logger.debug(f"[playbook] {_symbol} already_held 对账查询失败: {_ah_q_err}")

                    _now_et_iso = datetime.now(_now_et()).isoformat()
                    if _pb_rec and float(_pb_rec["entry_price"] or 0) > 0:
                        _entry = float(_pb_rec["entry_price"] or 0)
                        _exit = float(_pb_rec["exit_price"] or 0)
                        _upd = {
                            "executed": 1,
                            "execution_price": _entry,
                            "execution_time": _now_et_iso,
                        }
                        if _exit > 0:
                            _pnl = float(_pb_rec["realized_pnl"] or 0)
                            _upd.update({
                                "exit_price": _exit,
                                "exit_time": _pb_rec["exit_time"] or "",
                                "exit_reason": _pb_rec["exit_reason"] or "unknown",
                                "pnl": round(_pnl, 2),
                                "pnl_pct": round(float(_pb_rec["pnl_pct"] or 0), 2),
                                "outcome": "win" if _pnl > 0 else "loss",
                            })
                        else:
                            _upd["outcome"] = "holding"
                        db.update_instruction_tracking(inst["id"], _upd)
                        logger.info(
                            f"[playbook] {_symbol} broker已有持仓 (playbook自成交), "
                            f"回填 entry=${_entry:.2f} outcome={_upd['outcome']}, 跳过重复下单"
                        )
                    else:
                        logger.info(f"[playbook] {_symbol} broker已有持仓 (外部/手动/ORB), 跳过 (防重复下单)")
                        db.update_instruction_tracking(inst["id"], {"executed": 1, "execution_price": 0, "execution_time": _now_et_iso, "outcome": "already_held"})
                    skipped_count += 1
                    continue
            except Exception as _bh_err:
                # R1c (2026-07-19, DEB-round2 Ω.3-3): 预检 fail 模式由 risk.preflight_fail_mode 驱动.
                #   closed → 跳过该仓 (broker 持仓未知时不重复下单). SIM 默认 open.
                try:
                    from app.core.config_container import get_config as _get_cfg_r1c
                    _r1c_mode = _get_cfg_r1c().get_dotted("risk.preflight_fail_mode", "open")
                except Exception:
                    _r1c_mode = "open"
                if _r1c_mode == "closed":
                    logger.error(f"[playbook] broker持仓检查失败 (fail-close 跳过该仓): {_bh_err}")
                    skipped_count += 1
                    continue
                logger.debug(f"[playbook] broker持仓检查失败 (放行): {_bh_err}")

            # 盘中实时触发检查: 价格是否进入入场区间 ±5%
            # shadow_tracker 只在盘后跑, 盘中 triggered=0 是正常的
            # auto_execute 每30分钟跑一次, 实时检查价格是否触发
            if not inst.get("triggered"):
                triggered = _check_intraday_trigger(scheduler, inst)
                if not triggered:
                    continue
                # 触发了 → 先更新 DB
                db.update_instruction_tracking(inst["id"], {"triggered": 1})
                logger.info(f"[playbook] 盘中触发: {inst['symbol']} {inst['direction']}")

            # ── 2026-06-29: 触发后重新定价 (re-price at trigger) ──
            # 根因: 盘前生成的 entry_price 用的是盘前 bid/ask, 开盘后 gap 导致 entry/stop 失准
            # 修复: 触发瞬间取实时 bid/ask, 重算 entry_price + stop_loss, 传 custom_params 给 executor
            # DB 原值保留不动 (审计可追溯盘前价), custom_params 覆盖执行价
            _reprice_params = None
            _sl_clamp_meta = {}
            try:
                _dr = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
                if _dr:
                    _ctx = _dr.get_full_market_context(inst["symbol"])
                    if _ctx and _ctx.get("price", 0) > 0:
                        _cur = float(_ctx["price"])
                        _bid = float(_ctx.get("lv3_bid") or _ctx.get("bid") or _cur)
                        _ask = float(_ctx.get("lv3_ask") or _ctx.get("ask") or _cur)
                        _dir = inst.get("direction", "")
                        _hp = pb_cfg.get_horizon_params(inst.get("horizon", "short"))

                        # Phase 2/4 (2026-06-30): re-price 必须复用波动率分桶逻辑, 否则固定 SL/TP 会覆盖盘前生成的动态值
                        if _dir == "BUY":
                            _new_entry = _ask
                        else:
                            _new_entry = _bid

                        # 优先用真实 ATR 计算 atr_pct, 未取到则回退到 ctx 中的 atr_percent
                        # 2026-07-16 P1 修复: ATR=0 时保留 instruction 原始 SL/TP, 不用固定 fallback 覆盖.
                        #   根因: 盘前生成 instruction 时 ATR 可用 (vol_bucket 正确匹配),
                        #   但盘中 re-price 时 OpenD 可能不可用 → _atr=0 → atr_pct=0 →
                        #   vol_bucket 返回 None → 固定 fallback (2.0-2.5%) 覆盖原 SL.
                        #   修复: ATR=0 时从 instruction 原始 SL 反算 sl_pct, 保持 vol_bucket 比例不变.
                        _atr = float(_ctx.get("atr") or _ctx.get("atr_14") or 0)
                        atr_pct = 0.0
                        if _new_entry > 0 and _atr > 0:
                            atr_pct = _atr / _new_entry
                        if atr_pct <= 0:
                            atr_pct = (float(_ctx.get("atr_percent") or 0)) / 100.0

                        _vol_info = _compute_volatility_based_sl_tp(atr_pct, _dir, _hp, pb_cfg)
                        if _vol_info:
                            _sl_pct = _vol_info["sl_pct"]
                            _tp_mult = _vol_info["tp_multiplier"]
                        elif inst.get("stop_loss") and _new_entry > 0:
                            # 2026-07-16: ATR=0 → 从 instruction 原始 SL/TP 反算比例, 保持 vol_bucket 级别
                            _orig_sl = float(inst.get("stop_loss", 0))
                            _orig_tp = float(inst.get("take_profit", 0))
                            if _orig_sl > 0:
                                _sl_pct = abs(_new_entry - _orig_sl) / _new_entry * 100
                            else:
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5)) if _dir == "BUY" else float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            # TP: 用原始 TP 距离 / 新 entry 反算倍数
                            if _orig_tp > 0 and _orig_sl > 0:
                                _orig_sl_dist = abs(float(inst.get("entry_price", _new_entry)) - _orig_sl)
                                _orig_tp_dist = abs(_orig_tp - float(inst.get("entry_price", _new_entry)))
                                _tp_mult = _orig_tp_dist / _orig_sl_dist if _orig_sl_dist > 0 else float(_hp.get("rr_ratio", 1.5))
                            else:
                                _tp_mult = float(_hp.get("rr_ratio", 1.5))
                            logger.info(
                                f"[playbook] {inst['symbol']} re-price: ATR=0, 保留 instruction SL%={_sl_pct:.2f}% "
                                f"(从原始 SL=${_orig_sl:.2f} 反算)"
                            )
                        else:
                            # 未配置 vol_buckets 且无 instruction SL 时回退固定百分比
                            if _dir == "BUY":
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5))
                            else:
                                _sl_pct = float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            _tp_mult = float(_hp.get("rr_ratio", 1.5))

                        # ── 2026-08-07 (用户裁决: 再入场快收结构) ──
                        #   intraday_reentry- 前缀指令不走 vol_bucket: SL 放宽给跨夜空间 /
                        #   TP 降档快速落袋, ATR 相对按标的自适应 (quick_sl_tp_pct).
                        #   用触发时刻 ATR 复算 (atr_pct 此处为分数口径 → ×100 转百分数);
                        #   异常/ATR缺失 → 不落本分支, 维持 vol_bucket 原逻辑 (保守).
                        try:
                            if str(inst.get("decision_id", "") or "").startswith("intraday_reentry-"):
                                from app.playbook.intraday_reentry import quick_sl_tp_pct as _qstp
                                _q_sl, _q_tp = _qstp(atr_pct * 100.0 if atr_pct else None)
                                if _q_sl is not None and _q_tp is not None:
                                    _sl_pct = _q_sl
                                    _tp_mult = _q_tp / _q_sl
                                    logger.info(
                                        f"[playbook] {inst['symbol']} 再入场快收: "
                                        f"SL={_q_sl:.2f}% TP={_q_tp:.2f}% (ATR相对, 替代vol_bucket)"
                                    )
                        except Exception:
                            pass

                        # ── 2026-07-17: 出价前 SL 钳制不变量 (ATR 无关, 越界必钳) ──
                        # 根因: DA/UE 直写指令绕过 vol_bucket; ATR 缺失时 fallback 用来源原始 SL
                        #   反算 sl_pct → 可能越过桶天花板 (MCK 6% / ABT 7.3% > ceil 5.0%)。
                        # 只堵越界: 已按桶钳过的合法值 (ceil ≤ max_ceil) 是 no-op; SHORT 含 short_widen。
                        _sl_pct_raw = _sl_pct
                        _sl_clamped = False
                        _bounds = _vol_bucket_sl_bounds(_hp, pb_cfg)
                        if _bounds:
                            _min_floor, _max_ceil, _max_widen = _bounds
                            _cap = _max_ceil + (_max_widen if _dir == "SELL_SHORT" else 0.0)
                            _sl_pct = max(_min_floor, min(_cap, _sl_pct))
                            if abs(_sl_pct - _sl_pct_raw) > 1e-6:
                                _sl_clamped = True
                                logger.info(
                                    f"[playbook] {inst['symbol']} SL 收口: "
                                    f"{_sl_pct_raw:.2f}%→{_sl_pct:.2f}% (桶上界 cap={_cap:.2f}%, 来源 SL 超界)"
                                )
                        _sl_clamp_meta = {
                            "sl_pct_raw": round(_sl_pct_raw, 3),
                            "sl_pct_final": round(_sl_pct, 3),
                            "sl_clamped": _sl_clamped,
                        }

                        # 计算新 stop_loss
                        if _dir == "BUY":
                            _new_sl = round(_new_entry * (1 - _sl_pct / 100), 2)
                        else:
                            _new_sl = round(_new_entry * (1 + _sl_pct / 100), 2)

                        # 处理 rr_ratio:999 哨兵 (禁用止盈); 否则基于波动率分桶的 tp_multiplier
                        if _tp_mult and _tp_mult < 100:
                            if _dir == "BUY":
                                _new_tp = round(_new_entry * (1 + _sl_pct * _tp_mult / 100), 2)
                            else:
                                _new_tp = round(_new_entry * (1 - _sl_pct * _tp_mult / 100), 2)
                        else:
                            _new_tp = 0.0

                        # 2026-07-09: position_size 兜底计算 — bridge直写DB时未计算仓位
                        # 公式: risk_amount / per_share_risk, 截断 max_position_pct
                        # 置信度加权 (设计原则): 期望值 = 胜率×盈亏比, 高置信信号 edge 更大 →
                        #   在正期望前提下按 Kelly 思想放大仓位, 低置信信号缩仓. 以 min_confidence
                        #   为 1.0 中性基准, 限幅 [0.6, 1.4] 防止单点误判造成极端仓位.
                        #   max_position_pct 仍是硬上限, confidence 只在其内部分配.
                        _inst_ps = inst.get("position_size") or 0
                        if _inst_ps <= 0 and _new_sl > 0:
                            _risk_base = _resolve_sizing_base(pb_cfg)  # R4 SSOT: 富途实时净值
                            _risk_pct = float(pb_cfg.get("risk_pct", 0.5))
                            _max_pos_pct = float(pb_cfg.get("max_position_pct", 0.04))
                            # F1 (2026-07-19): 补水位/regime 乘数, 与主路径 compute_position_size 对齐
                            #   (DEB-20260719-round2 Ω.3-4). 修复前兜底缺二者 → 回撤期仓位反向放大.
                            #   water_mult: capital_safety 水位减仓 (YELLOW/ORANGE), 单一真源同主路径.
                            #   regime_mult: MarketRegimeGate (live regime SSOT) + 主路径同款 regime 表.
                            _water_mult = 1.0
                            _regime_mult = 1.0
                            _regime_str_b1 = None  # B1 shadow 用: gate regime 标签 (assess 成功才填)
                            try:
                                from app.risk.capital_safety import get_capital_safety_system as _get_cs_f1
                                _cs_f1 = _get_cs_f1()
                                _water_mult = min(_cs_f1.get_position_multiplier(), _cs_f1.get_risk_multiplier())
                            except Exception:
                                pass
                            try:
                                from app.playbook.market_regime import MarketRegimeGate as _MRG_f1
                                _regime_str_f1 = _normalize_regime_str(_MRG_f1().assess())
                                _regime_str_b1 = _regime_str_f1
                                _regime_mult = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}.get(_regime_str_f1, 0.5)
                                # 2026-08-06 (用户指示, sim 采集期): cautious 不再降仓 — 数据采集优先.
                                #   原 0.5 与 LONG分歧0.5 相乘 → 名义仅 25%, 大量单被 min_notional=3000 物理拦截.
                                #   回滚: cautious_mult 改回 0.5 (或删本行恢复硬编码).
                                try:
                                    from app.core.config_container import get_config as _get_cfg_cm
                                    _cautious_mult = float(_get_cfg_cm().get_dotted("playbook.risk_parameters.cautious_mult", 1.0) or 1.0)
                                    if _regime_str_f1 == "CAUTIOUS" and _cautious_mult != 0.5:
                                        _regime_mult = {"RISK_ON": 1.0, "CAUTIOUS": _cautious_mult, "RISK_OFF": 0.3, "NO_TRADE": 0.0}.get(_regime_str_f1, 0.5)
                                        logger.info(f"[playbook] {inst['symbol']} CAUTIOUS 仓位乘数覆写: 0.5→{_cautious_mult} (sim采集期)")
                                except Exception:
                                    pass
                            except Exception:
                                pass
                            _conf = float(inst.get("confidence", 0) or 0)
                            _conf_base = float(pb_cfg.get("min_confidence", 60) or 60)
                            # 2026-07-12: confidence 加权默认关闭 (拍平为 1.0). exit_scorecard 实测
                            #   当前 DA confidence 未校准且反向 (低信心桶反而更好, Playbook IC 为负) →
                            #   按 confidence 放大仓位 = 给更差的信号加钱. 校准为正前保持中性.
                            #   config: playbook.risk_parameters.confidence_sizing_enabled (默认 false).
                            _conf_factor = 1.0
                            if bool(pb_cfg.get("confidence_sizing_enabled", False)) and _conf > 0 and _conf_base > 0:
                                _conf_factor = max(0.6, min(1.4, _conf / _conf_base))
                            # 2026-07-16 P1 (2026-07-18 去黑盒): LONG "模型分歧" 降仓50%.
                            #   注意: 此处 conf<50 不是"低信心"语义 — DA confidence 未校准且反向
                            #   (低信心桶实测反而更好, 见上文 confidence_sizing 注释). conf<50 实际是
                            #   "2模型方向分歧" 的代理: DA 分歧写 conf=35, 一致写 ~70. 故变量命名为
                            #   _model_disagreement 而非"低信心", 避免误读为信心质量门.
                            #   数据依据: LONG 分歧单 38笔 39.5%WR -$2435 vs SHORT 23笔 43.5%WR +$1467 →
                            #   LONG 方向分歧系统性亏损. 修复: 降仓50% (不SKIP, 保留方向验证观测).
                            _model_disagreement = _conf < 50
                            if _dir == "BUY" and _model_disagreement:
                                # 2026-08-06 (用户指示, sim 采集期): LONG 分歧降仓 0.5 → 配置化, 默认 1.0 不降.
                                #   原 0.5 × CAUTIOUS 0.5 = 0.25 → 名义 < min_notional=3000 被物理拦截 → 交易量骤减.
                                #   分歧单仍保留日志/观测 (不 SKIP 语义不变); 回滚: 改回 0.5 + 重启.
                                try:
                                    from app.core.config_container import get_config as _get_cfg_ds
                                    _ds_factor = float(_get_cfg_ds().get_dotted(
                                        "playbook.risk_parameters.model_disagreement_downsize_factor", 1.0) or 1.0)
                                except Exception:
                                    _ds_factor = 1.0
                                _conf_factor *= _ds_factor
                                logger.info(
                                    f"[playbook] {inst['symbol']} LONG 模型分歧(conf={_conf}<50), 降仓{_ds_factor:.2f} "
                                    f"(conf_factor={_conf_factor:.2f})"
                                )
                            # F1: sizing SSOT 公共函数 (含 water_mult×regime_mult×conf_factor + 截断)
                            from app.playbook.instruction_generator import compute_position_size as _cps_f1
                            _inst_ps = _cps_f1(
                                _risk_base, _risk_pct, _new_entry, _new_sl, _max_pos_pct,
                                regime_mult=_regime_mult, water_mult=_water_mult, conf_factor=_conf_factor,
                            )
                            # B1 shadow (DEB-feedback-loop, 2026-07-31): sizing-delta 对照 —
                            #   指令自带 regime 文本 (生成侧视角) vs MarketRegimeGate (sizing 实采)
                            #   + 各乘数与最终 size, 供 regime 统一裁决 (攒满10交易日) 供数. record-only.
                            try:
                                # 2026-07-31 复核补: 测试标的守卫 (07-30 遥测污染案军规 —
                                #   只读埋点必带, pytest 直调本段不得写生产 shadow)
                                if "TEST" in str(inst.get("symbol", "")).upper():
                                    raise StopIteration
                                from app.utils.timezone import now_et as _now_et_b1
                                _ri_b1 = inst.get("raw_instruction_json")
                                if isinstance(_ri_b1, str):
                                    try:
                                        _ri_b1 = json.loads(_ri_b1)
                                    except Exception:
                                        _ri_b1 = {}
                                _src_b1 = ((_ri_b1 or {}).get("source") if isinstance(_ri_b1, dict) else "") \
                                    or str(inst.get("decision_id") or "").split(":")[0]
                                _shadow_b1 = {
                                    "ts": _now_et_b1().isoformat(),
                                    "date": inst.get("date"),
                                    "symbol": inst.get("symbol"),
                                    "direction": _dir,
                                    "source": _src_b1,
                                    "inst_regime_text": inst.get("market_regime"),
                                    "gate_regime": _regime_str_b1,
                                    "gate_mult": _regime_mult,
                                    "water_mult": round(_water_mult, 4),
                                    "conf_factor": round(_conf_factor, 4),
                                    "size": _inst_ps,
                                }
                                _sp_b1 = Path(__file__).resolve().parents[2] / "data" / "shadow" / "sizing_delta.jsonl"
                                _sp_b1.parent.mkdir(parents=True, exist_ok=True)
                                with open(_sp_b1, "a") as _f_b1:
                                    _f_b1.write(json.dumps(_shadow_b1, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
                            if _inst_ps > 0:
                                logger.info(
                                    f"[playbook] {inst['symbol']} position_size兜底(F1 SSOT): "
                                    f"{_inst_ps}股 (water={_water_mult:.2f}×regime={_regime_mult:.2f}"
                                    f"×conf={_conf_factor:.2f}, 上限{int(_max_pos_pct*100)}%)"
                                )

                        _reprice_params = {
                            "entry_price": round(_new_entry, 2),
                            "stop_loss": _new_sl,
                            "take_profit": _new_tp,
                            # 2026-06-29: 必须传 position_size, 否则 execute_instruction
                            # 走 custom_params 分支但取不到 position_size → None → 降为1股
                            "position_size": _inst_ps or 1,
                            # 2026-07-13: risk_amount 派生落库 (显示/审计字段) = |entry-SL|×实际仓位.
                            #   此前 DA/bridge 路径 risk_amount 恒 NULL → 盘前推送 {risk:.0f} 曾崩
                            #   (已加 or 0 兜底); 此处补写真实值, 让推送/周报显示每笔风险敞口.
                            #   纯派生, 不影响 sizing 决策.
                            "risk_amount": round(abs(_new_entry - _new_sl) * (_inst_ps or 1), 2),
                            # 2026-06-29: 审计字段 — 记录 re-price 来源
                            "reprice_from": round(float(inst.get("entry_price", 0) or 0), 2),
                            "reprice_to": round(_new_entry, 2),
                            "reprice_original_sl": round(float(inst.get("stop_loss", 0) or 0), 2),
                        }
                        # 更新 DB 记录 re-priced 值 (审计可追溯)
                        db.update_instruction_tracking(inst["id"], _reprice_params)
                        # 2026-07-16: 盘中数据质量护栏 — 记录 re-price 时的数据完整性
                        _dq_flags = []
                        if _atr <= 0: _dq_flags.append("atr_missing")
                        if not _ctx.get("lv3_bid") or not _ctx.get("lv3_ask"):
                            _dq_flags.append("no_lv3_bidask")
                        if _dq_flags:
                            logger.warning(
                                f"[playbook] {inst['symbol']} re-price 数据质量: {', '.join(_dq_flags)} "
                                f"(使用 instruction 原始 SL 反算, 不影响执行)"
                            )
                        logger.info(
                            f"[playbook] re-price: {inst['symbol']} {_dir} "
                            f"盘前=${inst.get('entry_price', 0):.2f} → 实时=${_new_entry:.2f} "
                            f"SL={_new_sl:.2f} ({_sl_pct:.2f}%) TP={_new_tp:.2f}"
                        )

                        # 2026-07-12 L1: SL/TP 验证影子 (record-only, 不改任何交易动作)。
                        #   记录 LLM 原始 SL/TP vs 代码波动率桶 SL/TP 的分歧, 供日后与 pnl 联合校准。
                        #   自带 try/except, 绝不影响 reprice 与下单。
                        try:
                            from app.playbook.sltp_verify_shadow import record_sltp_shadow
                            record_sltp_shadow(
                                inst_id=inst.get("id"), symbol=inst.get("symbol", ""),
                                direction=_dir, entry=_new_entry,
                                llm_sl=inst.get("stop_loss", 0), llm_tp=inst.get("take_profit", 0),
                                code_sl=_new_sl, code_tp=_new_tp,
                                sl_pct=_sl_pct, bucket=(_vol_info or {}).get("bucket", ""),
                                atr_pct=atr_pct, regime=inst.get("market_regime", ""),
                            )
                        except Exception:
                            pass
            except Exception as _rp_err:
                logger.debug(f"[playbook] re-price 失败, 用DB原值: {_rp_err}")

            # 2026-07-10: 删除 confidence<65 执行层gate (G6 conf<70已覆盖, E1从不拦截)
            # 原代码: _min_confidence=65, if conf<65 → SKIP
            # 删除原因: G6(line 1065, conf<70)已过滤, conf≥70的信号到执行层必>65, E1冗余

            # P0 (2026-06-25): buying_power 总量检查 — 防止批量触发耗尽购买力
            # 2026-07-10 fix: 优先用 re-price 后的 _reprice_params (含兜底计算的 position_size),
            # 否则 bridge 候选 position_size=None → _size=0 → _new_notional=0 → guardrail 全部空过
            if _reprice_params and _reprice_params.get("position_size"):
                _entry = _reprice_params.get("entry_price") or 0.0
                _size = _reprice_params.get("position_size") or 0
            else:
                _entry = inst.get("entry_price") or 0.0
                _size = inst.get("position_size") or 0
            _new_notional = _entry * _size if _entry > 0 and _size > 0 else 0.0
            if _bp_limit > 0 and (_max_deploy + _new_notional) > _bp_limit:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 购买力上限 "
                    f"(已用=${_max_deploy:,.0f} + 新=${_new_notional:,.0f} > 限=${_bp_limit:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_bp_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 2026-06-28: 单日 notional/数量 guardrail
            if _today_opened_notional + _new_notional > _daily_notional_cap:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日notional上限 "
                    f"(已用=${_today_opened_notional:,.0f} + 新=${_new_notional:,.0f} > 限=${_daily_notional_cap:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_notional_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            if _today_opened_count >= _max_daily_positions:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日开仓数上限 "
                    f"(已达到 {_max_daily_positions} 笔)"
                )
                _emit_exec_gate_audit(inst, action="skipped_max_positions", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 自动执行
            _audit_action = "error"
            try:
                result = service.execute_instruction(
                    instruction_id=inst["id"],
                    acknowledge_risk=True,  # Paper Trade 自动确认风险
                    custom_params=_reprice_params,  # 2026-06-29: 重新定价后的 entry/stop
                )
                if result.get("success"):
                    executed_count += 1
                    _max_deploy += _new_notional  # 累加已用额度
                    _today_opened_notional += _new_notional
                    _today_opened_count += 1
                    _audit_action = "executed"
                    logger.info(f"[playbook] ✅ 自动执行: {inst['symbol']} {inst['direction']}")
                else:
                    skipped_count += 1
                    _audit_action = "skipped_fail"
                    errors.append(f"{inst['symbol']}: {result.get('message', 'unknown')}")
            except Exception as e:
                skipped_count += 1
                _audit_action = "error"
                errors.append(f"{inst['symbol']}: {str(e)}")
                logger.warning(f"[playbook] 自动执行失败 {inst['symbol']}: {e}")
            # 统一收口审计 (2026-07-17): 记录到达下单漏斗并被执行/失败的候选 (含 DA-bridge)
            _emit_exec_gate_audit(inst, action=_audit_action, entry=_entry,
                                  size=_size, notional=_new_notional,
                                  extra={
                                      "vol_bucket": inst.get("vol_bucket"),
                                      "atr_pct": inst.get("atr_pct"),
                                      **_sl_clamp_meta,
                                  })

        logger.info(f"[playbook] 自动执行完成: {executed_count} 成功, {skipped_count} 跳过")
        # 盘中实时化: 广播轻量 nudge, 已连 dashboard 收到后自拉缓存接口刷新. 没人连时 broadcast 直接 return.
        try:
            from app.web_dashboard.routes.ws import publish_event_sync
            publish_event_sync("pipeline_update", {
                "date": today,
                "executed": executed_count,
                "skipped": skipped_count,
                "errors": len(errors),
            })
        except Exception:
            pass  # WS 推送失败不影响交易主流程
        return {
            "date": today,
            "executed": executed_count,
            "skipped": skipped_count,
            "errors": errors[:5],  # 最多返回 5 个错误
        }

    except Exception as e:
        logger.exception(f"[playbook] 自动执行异常 (已隔离): {e}")
        return {"error": str(e)}


def _calc_intraday_vol_ratio(data_router, symbol: str,
                             realtime_cum_vol: Optional[float] = None) -> Optional[float]:
    """计算盘中实时量能比 (intraday volume ratio).

    公式: today_cumulative_vol / (avg_daily_vol_5d × elapsed_fraction)
      - today_cumulative_vol: 今日开盘至当前的累计成交量
      - avg_daily_vol_5d: 最近5个已完成交易日日均成交量
      - elapsed_fraction: 已过交易时间占全天比例 (9:30-16:00 ET = 390min)

    Returns:
        float > 0: 盘中量能比 (1.0=与历史同时段持平, >1.5=放量)
        None: 数据不足, 调用方应 fail-open.

    2026-07-20 新增: 替代原日级 volume_ratio (对盘中突破零预测力), 用真实盘中
    累计量/同时段期望量做动量确认.
    2026-07-20 修复 (量能门误拦根因): 分子改用 Futu LV3 实时快照的当日累计成交量
      (get_quote()["volume"], <100ms 推送维护) — 原来聚合 K_1M(300s缓存) 系统性
      滞后低估, 令 AAPL/MSFT 盘中40min仍仅~0.7 → 近乎全拦. 实时快照不可用时才回退
      K_1M 聚合. 数据源: Futu LV3 快照(主) / K_1M(兜底) + 日K线.
    """
    try:
        from datetime import datetime as _dt
        _now_et_dt = _dt.now(_now_et())
        _today_str = _now_et_dt.strftime("%Y-%m-%d")

        # ── 1. 今日累计成交量 ──
        # 优先: 实时快照 volume (当日累计, 由 QUOTE push <100ms 维护, 无缓存滞后).
        today_cum_vol = 0.0
        if realtime_cum_vol is None:
            try:
                _q = data_router.get_quote(symbol) if hasattr(data_router, "get_quote") else None
                if _q:
                    realtime_cum_vol = float(_q.get("volume", 0) or 0)
            except Exception:
                realtime_cum_vol = None
        if realtime_cum_vol and realtime_cum_vol > 0:
            today_cum_vol = float(realtime_cum_vol)
        else:
            # 兜底: 聚合 K_1M (300s缓存, 滞后但盘初实时源缺失时聊胜于无)
            _futu = getattr(data_router, "futu", None)
            if _futu is None or not hasattr(_futu, "get_kline"):
                return None
            # count=800 — HTTP 冷路径 start=T-1 时 OpenD 返回"最旧N根",
            #   count 小于范围总数(~780)会拿到纯 T-1 bar → 今日过滤后空 → 门恒 fail-open.
            bars_1m = _futu.get_kline(symbol, ktype="K_1M", count=800) or []
            if not bars_1m:
                return None
            today_vols = []
            for b in bars_1m:
                _t = str(b.get("time") or b.get("time_key") or "")
                if _t[:10] == _today_str:
                    _v = float(b.get("volume", 0) or 0)
                    if _v > 0:
                        today_vols.append(_v)
            if len(today_vols) < 5:  # 至少5根bar才有意义
                return None
            today_cum_vol = sum(today_vols)

        # ── 2. 最近5日日均成交量 (日K线) ──
        daily_bars = data_router.get_daily_bars(symbol, count=10) or []
        # 排除今日 (可能不完整), 取最近5个已完成交易日
        hist_vols = []
        for db in daily_bars:
            _dt_str = str(db.get("time", ""))[:10]
            if _dt_str >= _today_str:
                continue  # 跳过今日及未来
            _dv = float(db.get("volume", 0) or 0)
            if _dv > 0:
                hist_vols.append(_dv)
        hist_vols = hist_vols[-5:]  # 最近5日
        if len(hist_vols) < 3:  # 至少3日数据
            return None
        avg_daily_vol = sum(hist_vols) / len(hist_vols)
        if avg_daily_vol <= 0:
            return None

        # ── 3. 已过交易时间占比 ──
        # 美股常规交易时段: 9:30 - 16:00 ET = 390 min (半日市 13:00 收 = 210 min)
        _market_open_min = 9 * 60 + 30  # 570
        # 2026-07-25 (隐患扫描修复): 半日市支持 — 否则提前收盘日 elapsed_fraction 分母偏大, 量能门误判
        try:
            from app.utils.holiday_calendar import get_us_close_minutes_et
            _market_close_min = get_us_close_minutes_et(_now_et_dt.date())
        except Exception:
            _market_close_min = 16 * 60      # 960
        _now_min = _now_et_dt.hour * 60 + _now_et_dt.minute
        _elapsed = max(_now_min - _market_open_min, 1)
        _total = _market_close_min - _market_open_min  # 390
        elapsed_fraction = min(max(_elapsed / _total, 0.02), 1.0)

        # ── 4. 计算比值 ──
        expected_vol = avg_daily_vol * elapsed_fraction
        if expected_vol <= 0:
            return None
        ratio = today_cum_vol / expected_vol
        return round(ratio, 3)
    except Exception:
        return None


# 2026-07-24 (DEB-20260723 缺陷D 修正版): 同标的低MFE连败封禁 — 触发侧查询缓存 (5min TTL,
#   防盘中轮询每 tick 打 DB). 封禁自然过期: 亏损单滑出 lookback 窗口即解封, 无需持久化状态.
_STREAK_BAN_CACHE: Dict[str, Any] = {}


def _symbol_streak_banned(symbol: str, pb_cfg) -> bool:
    """近 lookback 日内, 该标的最近连续≥min_streak 笔 [亏损且 MFE<阈值] → 拒新触发.

    数据依据 (7月 PLAYBOOK): CRM 5笔-$150 / UBER 3笔-$102 / DIS 4笔-$76 同形态跨日重复出血;
    盈利重复标的 (MRK/EOG/LLY) 逐笔 MFE 全≥0.5% 不会命中 (DEB 第4轮: 纯连亏封禁已否决,
    必须叠加 MFE 过滤). 查询异常 → fail-open (不拦). 回滚: symbol_streak_ban_enabled 改 false.
    """
    try:
        if not bool(pb_cfg.get("symbol_streak_ban_enabled", False)):
            return False
        _now = time.time()
        _hit = _STREAK_BAN_CACHE.get(symbol)
        if _hit and (_now - _hit[0]) < 300:
            return _hit[1]
        _lookback = int(pb_cfg.get("symbol_streak_ban_lookback_days", 7))
        _min_streak = int(pb_cfg.get("symbol_streak_ban_min_streak", 2))
        _mfe_th = float(pb_cfg.get("symbol_streak_ban_mfe_pct", 0.5))
        import sqlite3 as _sq3
        from app.core.paths import get_trade_records_db as _gtrd
        _conn = _sq3.connect(str(_gtrd()))
        _rows = _conn.execute(
            "SELECT realized_pnl, mfe_pct FROM trade_records "
            "WHERE symbol=? AND source_type LIKE 'PLAYBOOK%' "
            "AND exit_time IS NOT NULL AND exit_time!='' "
            "AND trade_date >= date('now', ?) "
            "ORDER BY trade_date DESC, exit_time DESC LIMIT 10",
            (symbol, f"-{_lookback} day"),
        ).fetchall()
        _conn.close()
        # 从最近一笔往前数连续命中数; 任一笔盈利/高MFE/缺数据即断链 (不误伤既亏过又修复的标的)
        _streak = 0
        for _pnl, _mfe in _rows:
            if _pnl is not None and _pnl < 0 and _mfe is not None and _mfe < _mfe_th:
                _streak += 1
            else:
                break
        _banned = _streak >= _min_streak
        if _banned:
            logger.info(
                f"[playbook] 连败封禁: {symbol} 近{_lookback}日连续{_streak}笔低MFE亏损 "
                f"(阈值{_min_streak}笔/MFE<{_mfe_th}%) → 拒新触发"
            )
        _STREAK_BAN_CACHE[symbol] = (_now, _banned)
        return _banned
    except Exception as _e:
        logger.debug(f"[playbook] 连败封禁查询异常 (fail-open): {_e}")
        return False


_CUTOFF_LOGGED: set = set()  # 2026-07-28 降噪: 触发窗截止日志 每标的每日一次 (重启清零无害)

# 2026-07-30 流动性门: (symbol, date) → (adv_dollar, avg_vol) 日级缓存 — ADV 盘中不变,
#   每标的每日只算一次, 不给触发循环增加 K 线配额压力 (bar-ruler 冻结教训)
_LIQUIDITY_CACHE: dict = {}
_LIQUIDITY_LOGGED: set = set()  # 拦截日志每标的每日一次


def _check_liquidity_gate(data_router, symbol: str, current_price: float,
                          pb_cfg, planned_qty: float = 0.0) -> tuple:
    """小盘/低流动性门 (2026-07-30, 用户裁"小盘票补充限制", 实盘基准审计 T1-#5)。

    背景: 现有三面零覆盖 (screener 无流动性过滤/触发侧仅 gap 门/UE 仅 LLM 软自报);
    低 ATR 门恰好不拦小票 (小票 ATR 高)。sim 按盘口快照成交, 低流动标的盈利含虚高
    成分 (实盘冲击 30-100bps) — 拦掉它们让 sim 更贴实盘基准。

    判据 (只减不加, 两条任一不过即拦):
      ① current_price < min_price (仙股/枯竭票地板)
      ② 仓位名义额 > max_adv_pct% × ADV$ (5日均成交额) — 名义额优先取真实计划仓位
        (inst position_size×price, 2026-07-30 复核修正: 默认系数低估实际单 ~20%),
        缺省回退 资金×max_position_pct 保守上界
    fail-open: 日K/均量数据不足 → 放行 (与量能门同口径, 可用性优先)。
    Returns: (ok: bool, detail: str)
    """
    try:
        if not pb_cfg.get("liquidity_gate_enabled", False):
            return True, "disabled"
        _min_px = float(pb_cfg.get("liquidity_gate_min_price", 3.0))
        if current_price and 0 < current_price < _min_px:
            return False, f"price ${current_price:.2f} < 地板 ${_min_px}"
    except Exception:
        return True, "cfg_error_failopen"
    try:
        from app.utils.timezone import now_et as _lq_now
        _key = (symbol, _lq_now().strftime("%Y-%m-%d"))
        if _key not in _LIQUIDITY_CACHE:
            daily_bars = data_router.get_daily_bars(symbol, count=10) or []
            _today = _key[1]
            vols = [float(b.get("volume", 0) or 0) for b in daily_bars
                    if str(b.get("time", ""))[:10] < _today and float(b.get("volume", 0) or 0) > 0]
            vols = vols[-5:]
            if len(vols) < 3:
                _LIQUIDITY_CACHE[_key] = None  # 数据不足, 当日不再重查
            else:
                _LIQUIDITY_CACHE[_key] = sum(vols) / len(vols)  # 股数均量
        _avg_vol = _LIQUIDITY_CACHE.get(_key)
        if not _avg_vol:
            return True, "no_adv_data_failopen"
        _adv_dollar = _avg_vol * float(current_price or 0)
        if _adv_dollar <= 0:
            return True, "no_adv_data_failopen"
        # 名义额: 优先真实计划仓位 (inst position_size×price); 缺省回退 资金×单仓上限 保守上界
        _planned_notional = float(planned_qty or 0) * float(current_price or 0)
        if _planned_notional > 0:
            _notional = _planned_notional
        else:
            try:
                from app.core.config_container import get_config as _lq_gc
                _cap = float(_lq_gc().get_dotted("capital.fixed_amount", 82000) or 82000)
            except Exception:
                _cap = 82000.0
            _notional = _cap * float(pb_cfg.get("max_position_pct", 0.04) or 0.04)
        _max_pct = float(pb_cfg.get("liquidity_gate_max_adv_pct", 0.5))
        _ratio_pct = _notional / _adv_dollar * 100.0
        if _ratio_pct > _max_pct:
            return False, (f"名义额 ${_notional:.0f} = ADV$ {_adv_dollar / 1e6:.2f}M 的 "
                           f"{_ratio_pct:.2f}% > {_max_pct}%")
        return True, f"ok ({_ratio_pct:.3f}% of ADV)"
    except Exception as _lq_err:
        return True, f"failopen: {type(_lq_err).__name__}"  # 门自身异常不得拦交易


# 2026-07-30 SHORT 借券池门: (symbol, date) → {pool, fee_rate, shortable} 日级缓存
_BORROW_POOL_CACHE: dict = {}
_BORROW_POOL_LOGGED: set = set()  # 拦截日志每标的每日一次


def _check_borrow_pool_gate(symbol: str, planned_qty: float, pb_cfg) -> tuple:
    """SHORT 借券池门 (2026-07-30, 用户裁"直接做" — Qoder 复核补充项②)。

    背景: 做空低流动/小盘票的头号风险不是冲击成本而是借不到券/被召回 — sim 无条件
    假成交, 实盘 is_short_permit=False 直接拒单, 券池枯竭则开不了仓或遭召回。
    数据源 dual_broker.get_short_pool (get_margin_ratio, SIM/REAL 均可查, 实测)。

    判据 (仅 SHORT 指令, 任一不过即拦):
      ① is_short_permit=False → 不可卖空
      ② 券池余量 < 计划股数 × min_cover (默认 10x — 券池盘中会被消耗, 留量级余量)
    fail-open (查不到数据不拦) / 日级缓存零配额增量 / 回滚=关 borrow_pool_gate_enabled。
    Returns: (ok: bool, detail: str)
    """
    try:
        if not pb_cfg.get("borrow_pool_gate_enabled", False):
            return True, "disabled"
        from app.utils.timezone import now_et as _bp_now
        _key = (symbol, _bp_now().strftime("%Y-%m-%d"))
        if _key not in _BORROW_POOL_CACHE:
            from app.execution.dual_broker import get_dual_broker
            _info = get_dual_broker().get_short_pool([symbol])
            _BORROW_POOL_CACHE[_key] = (_info or {}).get(symbol)
        _bp = _BORROW_POOL_CACHE.get(_key)
        if not _bp:
            return True, "no_pool_data_failopen"
        if _bp.get("shortable") is False:
            return False, "is_short_permit=False (券商不可卖空, sim 假成交陷阱)"
        _pool = _bp.get("pool")
        _cover = float(pb_cfg.get("borrow_pool_gate_min_cover", 10.0))
        if _pool is not None and planned_qty and planned_qty > 0:
            if _pool < planned_qty * _cover:
                return False, (f"券池 {_pool:,.0f}股 < 计划 {planned_qty:.0f}股 × "
                               f"{_cover:.0f} 安全倍率")
            return True, f"ok (券池覆盖 {_pool / planned_qty:.0f}x)"
        return True, "ok"
    except Exception as _bp_err:
        return True, f"failopen: {type(_bp_err).__name__}"  # 门自身异常不得拦交易


def _check_intraday_trigger(scheduler, inst: Dict[str, Any]) -> bool:
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
    #   特异性匹配 'thesis_drift_invalidate', 不误伤其他 skip_reason 语义.
    if (inst.get("skip_reason") or "") == "thesis_drift_invalidate":
        return False
    # 2026-07-19 (DEB-round3 #3-B 消费端): 过期指令不再触发 — 与 auto_execute 双保险.
    if _is_instruction_expired(inst):
        return False

    # 2026-07-11: 只读埋点 — 记录未触发原因/实时价/偏离幅度.
    # 目的: 量化 never-trigger 里 "追高/接飞刀被正确拒绝" vs "盘前参考价过期但盘中价合理".
    # 零行为改动: 仅 append JSONL, best-effort, 不影响触发判定与返回值.
    def _emit_trigger_miss(reason: str, cur):
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
            _row = {
                "ts": _dt.now().isoformat(timespec="seconds"),
                "date": _dt.now().strftime("%Y-%m-%d"),
                "inst_id": inst.get("id"),
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "entry_price": round(_e, 4),
                "current_price": round(_cur, 4),
                "gap_pct": round(_gap, 3),
                "reason": reason,
            }
            _tp = _Path(__file__).resolve().parents[2] / "data" / "trigger_miss_telemetry.jsonl"
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
                    _raw = _json.loads(inst.get("raw_instruction_json") or "{}")
                    _src = str(_raw.get("source") or "")
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
            _tp = _Path(__file__).resolve().parents[2] / "data" / "entry_timing_shadow.jsonl"
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
        try:
            _inst_atr = inst.get("atr_pct")
            if _inst_atr is not None and 0 < float(_inst_atr) < _atr_gate_min:
                _k = (symbol, _today_et_str())
                if _k not in _low_atr_logged:
                    _low_atr_logged.add(_k)
                    logger.info(
                        f"[playbook] 低ATR门拦截: {symbol} atr_pct={float(_inst_atr):.4f} "
                        f"< {_atr_gate_min} (低波票不做日内动量, 当日首记后静默)"
                    )
                _emit_trigger_miss("low_atr_block", current_price)
                return False
        except Exception:
            pass  # atr 解析异常 → fail-open
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
    # 2026-07-20 重建: 盘中实时量能门 (替代原日级 volume_ratio).
    #   公式: today_cum_vol / (avg_daily_vol_5d × elapsed_fraction) ≥ threshold.
    #   数据源: Futu LV3 K_1M (300s缓存, 基本不额外耗配额) + 日K线 (5min缓存).
    #   fail-open: 数据不足 → None → 视为通过, 不因行情源异常阻断全部追入.
    #   可配置: pb_cfg["intraday_vol_gate_enabled"] (默认True),
    #           pb_cfg["chase_vol_ratio_threshold"] (默认1.5).
    _vol_gate_enabled = bool(pb_cfg.get("intraday_vol_gate_enabled", True))
    _vol_threshold = float(pb_cfg.get("chase_vol_ratio_threshold", 1.5))
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
        _intraday_vr = _calc_intraday_vol_ratio(data_router, symbol)
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
    _entry_path = None
    if is_short:
        if current_price >= entry_price:
            # 反弹到入场价或更高 → 更优做空点, 直接触发 (设上限防已冲高背离)
            triggered = current_price <= entry_price * (1 + _knife_floor)
            _entry_path = "pullback"
        elif current_price >= entry_price * (1 - _chase_zone):
            # 跌破入场价 → 追空, 需量能确认 (防无量假跌)
            triggered = _vol_ok
            _entry_path = "chase"
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
        else:
            triggered = False
            _entry_path = "out_of_band"

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
            if _chg_now is not None and float(_chg_now) > _scb_pct:
                import json as _scb_json
                from pathlib import Path as _scb_path
                from app.utils.timezone import now_et as _scb_now
                _scb_row = {
                    "ts": _scb_now().isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "direction": "SHORT",
                    "chg_pct": round(float(_chg_now), 3),
                    "threshold": _scb_pct,
                    "entry_price": entry_price,
                    "current_price": round(float(current_price or 0), 4),
                    "triggered": bool(triggered),
                    "entry_path": _entry_path,
                    "inst_id": inst.get("id"),
                }
                _scb_fp = _scb_path(__file__).resolve().parents[2] / "data" / "short_chase_ban_shadow.jsonl"
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
        _gate_tol = float(pb_cfg.get("entry_vwap_gate_tolerance", 0.0) or 0.0)
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
        _gate_enabled = bool(pb_cfg.get("entry_vwap_gate_enabled", False))
    except Exception:
        _gate_enabled = False

    if _gate_enabled and triggered and _gate_block:
        # 2026-08-07 (用户指示, sim A/B 双开): wrong_side 拦截 50/50 分流 —
        #   bypass 臂放行成交 (真实口径), gated 臂维持拦截 (影子反事实口径),
        #   双臂落 data/vwap_gate_ab.jsonl 台账. 非 sim / 开关关 / 异常 → 维持拦截.
        if _vwap_gate_ab_bypass(symbol, is_short, _vwap_dev, _vwap_val, current_price,
                                inst.get("id")):
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
            # 影子组合 (2026-08-09): VWAP 门拦下的本会触发单 → 影子入场 (A/B gated 臂全覆盖)
            _shadow_portfolio_emit(inst, symbol, is_short, "vwap_gate", _entry_path,
                                   current_price, entry_price, _vwap_dev)

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
                if _lk not in _LIMIT_SHADOW_EMITTED:
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
                    with open(_P2(__file__).resolve().parents[2] / "data" / "limit_entry_shadow.jsonl", "a", encoding="utf-8") as _f2:
                        _f2.write(_json2.dumps(_row, ensure_ascii=False) + "\n")
                    logger.info(f"[锚限价影子] {symbol} {'SHORT' if is_short else 'LONG'} 触发行已落盘 (anchor=${entry_price:.2f})")
        except Exception:
            pass
    else:
        logger.debug(
            f"[playbook] 未触发: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} dir={'SHORT' if is_short else 'BUY'}"
        )
        # 2026-07-20 修复: 已有特异性 miss 原因 (vol_gate_block/vwap_gate_block) 时不重复发射,
        #   避免同一轮产生两条 trigger_miss 记录污染漏斗统计.
        if not _vol_gate_actively_blocked and not (_gate_enabled and _gate_block):
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


def _is_instruction_expired(inst) -> bool:
    """#3-B (2026-07-19, DEB-round3): expires_at 过期判定 (SCAN 注入 24h 失效).

    消费端: auto_execute 跳过 + _check_intraday_trigger 拒触发 (双保险).
    无 expires_at 字段 → 不过期 (旧指令/非 SCAN 源兼容). 解析异常 → 不过期 (不误杀).
    """
    try:
        _exp = inst.get("expires_at")
        if not _exp:
            return False
        _exp_dt = datetime.fromisoformat(str(_exp).replace("Z", "+00:00"))
        if _exp_dt.tzinfo is None:
            _exp_dt = _exp_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= _exp_dt
    except Exception:
        return False


def _classify_ghost_cause(conn, symbol: str, self_trade_id: int) -> str:
    """GHOST 平仓归因 (2026-07-19, DEB-round3 GHOST 根因诊断).

    RC1 (entry_unfilled): 入场单未成交却记账 — quote_fallback 无成交校验 → 虚假仓位,
        退出时 broker_no_position → BROKER_GHOST_CLOSE (7/6 六连实证, position_sync
        证实 broker 无持仓且无今日成交记录).
    RC2 (double_tracked): 一仓两账 — 同一 broker 净仓位被 playbook (ET-aware) 与主链路
        (naive-CST) 各自记账, 先平者抽空 broker, 后者 ghost (RIVN #1060/#1069 同刻双记录).
    判定: 当日同 symbol 存在其他非 ghost 已平仓记录 → double_tracked(linked=#id:reason);
          否则 entry_unfilled_or_external.
    record-only: 供 2 周 shadow 复审区分 RC1/RC2 处置, 不改任何平仓行为.
    """
    try:
        _today_et = datetime.now(_now_et()).strftime("%Y-%m-%d")
        _row = conn.execute(
            "SELECT id, exit_reason FROM trade_records "
            "WHERE symbol=? AND id!=? AND exit_time IS NOT NULL AND exit_time != '' "
            "AND substr(exit_time,1,10)=? "
            "AND exit_reason NOT LIKE '%GHOST%' "
            "AND exit_reason NOT LIKE '%ORPHAN%' "
            "AND exit_reason NOT LIKE '%PHANTOM%' "
            "ORDER BY id DESC LIMIT 1",
            (symbol, self_trade_id, _today_et),
        ).fetchone()
        if _row:
            return f"double_tracked(linked=#{_row[0]}:{_row[1]})"
        return "entry_unfilled_or_external"
    except Exception:
        return "unknown"


def _max_drawdown_from_mfe_mae(mfe_pct: float, mae_pct: float) -> float:
    """peak-to-trough 回撤反算 (2026-08-07 B9 + 复核修复, 纯函数可单测).

    mfe_pct/mae_pct 均为方向归一化正数百分比口径 (LONG/SHORT 通用):
      peak = entry*(1+mfe/100), trough = entry*(1-mae/100)
      回撤(%) = (peak-trough)/peak*100 = 100*(mfe+mae)/(100+mfe)
    数据缺失 (mfe≤0 或 mae≤0, 含从未水下/采样缺失) → 0.0, 与历史一致不引入新值。
    """
    try:
        if mfe_pct > 0 and mae_pct > 0:
            return round(100.0 * (mfe_pct + mae_pct) / (100.0 + mfe_pct), 4)
    except Exception:
        pass
    return 0.0


def _close_trade_record(conn, trade_id: int, exit_price: float, exit_reason: str,
                        pnl: float, pnl_pct: float, holding_hours: float,
                        inst_id = None, symbol: str = None,
                        close_futu_order_id: str = None) -> None:
    """统一平仓写入 — 原子更新 trade_records + playbook_instructions + open_positions.

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
    try:
        now_iso = datetime.now(_now_et()).isoformat()
    except Exception:
        now_iso = datetime.now().isoformat()

    # 2026-07-20 #6: 读写收敛 — 本函数原对同一 trade_id 反复 SELECT (metadata/entry_time/
    #   direction/quantity 等至少 8 次), 且 metadata_json 多次独立读改写.
    #   收敛为: 函数开头读一次行快照 _tr + 解析一份 _meta, 各块顺序加工同一 dict,
    #   telemetry/ghost_cause 合并为一次写回. 行为零改变: 取值口径/写入内容/commit 时点不变.
    _tr = None
    _meta = {}
    _meta_parse_ok = False
    _meta_dirty = False
    try:
        _tr = conn.execute(
            "SELECT metadata_json, direction, entry_price, entry_time, quantity, source_type "
            "FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if _tr is not None and _tr[0]:
            try:
                _meta = json.loads(_tr[0])
                _meta_parse_ok = True
            except Exception:
                _meta = {}
    except Exception:
        _tr = None

    # 2026-07-09: 从 metadata 提取 MFE/MAE
    # 2026-07-10 fix: metadata["mfe_pct"] 键几乎从不写 — monitor_playbook_sim_exits 只在
    #   `if not exit_reason` 分支累积 mfe_pct, 触发退出那一轮直接平仓跳过累积块,
    #   导致 393/397 笔 mfe_pct=0 (含 121/122 盈利单, 逻辑违规).
    #   根因: metadata["peak_price"] 是可靠持久化的 (入场写入 + 每轮更新), 但从未回算成 mfe_pct.
    #   修复: mfe_pct 缺失/为0时, 用真峰值 peak_price + entry_price/direction 反算真实 MFE%.
    #   历史无 intraday 缓存无法回填, 此修复对新平仓的单子向前生效 (纯 surgical, 不碰热路径).
    _mfe_pct = 0.0
    _mae_pct = 0.0
    try:
        # #6: 复用顶部行快照 _tr/_meta (原此处独立 SELECT metadata_json/direction/entry_price)
        if _meta_parse_ok:
            _mfe_pct = float(_meta.get("mfe_pct", 0) or 0)
            _mae_pct = float(_meta.get("mae_pct", 0) or 0)
            # mfe_pct 缺失 → 从 peak_price 反算 (peak_price 是持久化的真峰值)
            if _mfe_pct <= 0:
                _peak = float(_meta.get("peak_price", 0) or 0)
                _entry = float(_tr[2] or 0)
                _dir = (_tr[1] or "LONG").upper()
                if _peak > 0 and _entry > 0:
                    if _dir == "LONG":
                        _calc_mfe = (_peak - _entry) / _entry * 100
                    else:
                        _calc_mfe = (_entry - _peak) / _entry * 100
                    if _calc_mfe > 0:
                        _mfe_pct = round(_calc_mfe, 4)
            # 2026-07-16: MAE 对称反算 — 与 MFE 同理, mae_pct 缺失时从 trough_price 反算.
            #   根因: trough_price 入场写入 + 每轮更新 (与 peak 对称), 但 close 时从未回算成 mae_pct,
            #   导致 MAE 常年为 0, 无法评估止损距离/浮亏承受度. 纯 surgical, 对新平仓向前生效.
            if _mae_pct <= 0:
                _trough = float(_meta.get("trough_price", 0) or 0)
                _entry = float(_tr[2] or 0)
                _dir = (_tr[1] or "LONG").upper()
                if _trough > 0 and _entry > 0:
                    if _dir == "LONG":
                        _calc_mae = (_entry - _trough) / _entry * 100
                    else:
                        _calc_mae = (_trough - _entry) / _entry * 100
                    if _calc_mae > 0:
                        _mae_pct = round(_calc_mae, 4)
            # 定义兜底: MFE 必须 ≥ 已落袋的有利变动 (pnl_pct 为分数, 转百分比).
            #   处理采样粒度问题: 峰值轮询未抓到最高点 (如 META peak<exit) 时不至低估 MFE.
            _realized_fav = round(pnl_pct * 100, 4)
            if _realized_fav > _mfe_pct:
                _mfe_pct = _realized_fav
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

    # ── 根因修复 (2026-07-07): 从 DB 查 entry_time, 用 get_market_hours_us 重算 holding_hours ──
    # 消灭所有调用方传 wall-clock 差值的变体 (naive TypeError, CST时间差, 纯秒数差).
    try:
        # #6: 复用 _tr (原此处独立 SELECT entry_time)
        if _tr is not None and _tr[3]:
            _entry_dt = datetime.fromisoformat(_tr[3])
            _et_tz = _now_et()
            _now_dt = datetime.now(_et_tz)
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
                "take_profit": float(_tmeta.get("take_profit", 0) or 0),
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

    # #6: telemetry/ghost_cause 对 metadata 的读改写合并为一次写回 (仍在主 UPDATE 之前,
    #   内容与旧逻辑两次独立写的最终状态完全一致)
    if _meta_dirty:
        try:
            conn.execute(
                "UPDATE trade_records SET metadata_json=? WHERE id=?",
                (json.dumps(_meta, ensure_ascii=False, default=str), trade_id),
            )
        except Exception as _mw_err:
            logger.debug(f"[_close_trade_record] metadata 合并写回失败 trade_id={trade_id}: {_mw_err}")

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
    if close_futu_order_id:
        conn.execute("""
            UPDATE trade_records
            SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, close_futu_order_id=?, mfe_pct=?, mae_pct=?, max_drawdown=?
            WHERE id=?
        """, (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), close_futu_order_id, round(_mfe_pct, 4), round(_mae_pct, 4), _max_dd_val, trade_id))
    else:
        conn.execute("""
            UPDATE trade_records
            SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, mfe_pct=?, mae_pct=?, max_drawdown=?
            WHERE id=?
        """, (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), round(_mfe_pct, 4), round(_mae_pct, 4), _max_dd_val, trade_id))
    # 2026-07-30 (DEB-fee-net-pnl): 净额列跟随主 UPDATE 落库 — pnl_net = 毛 - 估算费;
    #   三态只升不降 (broker/reconciled 不被 est 覆盖); 旧库/测试库无列 → 静默跳过。
    if _fees_total is not None:
        try:
            conn.execute(
                "UPDATE trade_records SET fees_est=?, pnl_net=?, "
                "fees_source=CASE WHEN fees_source IN ('broker','reconciled') "
                "THEN fees_source ELSE 'est' END WHERE id=?",
                (_fees_total, round(round(pnl, 2) - _fees_total, 2), trade_id))
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
        _outcome = "void" if _is_ghost else ("win" if pnl > 0 else "loss")
        # 2026-07-16 P0 修复: 幂等条件加入 'void', 防止已标 void 的幽灵仓被重复处理.
        #   旧逻辑 WHERE outcome NOT IN ('win','loss') — void 不在排除列表 →
        #   幽灵仓可被重复平仓写入. 修复: 排除 'void' 终态.
        conn.execute(
            "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
            "holding_period_hours=?, max_favorable_excursion=?, max_adverse_excursion=? "
            "WHERE id=? AND outcome NOT IN ('win','loss','void')",
            (_outcome, exit_price, exit_reason, round(pnl, 2),
             round(pnl_pct, 4), round(holding_hours, 1),
             _mfe_usd, _mae_usd, inst_id),
        )

    # ── 切实盘前风控接线: 平仓后喂给 record 端 (复用现有模块, fail-open) ──
    #   此前 Playbook 路径从不调用这两个 record, 导致 ACB 连亏/日亏计数恒为0、
    #   PDT 日内交易数恒为0 → 绝对熔断与 PDT 限制在实盘形同虚设.
    #   幽灵/孤儿平仓不计入风控统计 (非真实交易结果).
    _rc_is_ghost = any(_k in (exit_reason or "").upper() for _k in ("ORPHAN", "PHANTOM", "GHOST"))
    if symbol and not _rc_is_ghost:
        try:
            from app.risk.absolute_circuit_breaker import get_absolute_circuit_breaker
            get_absolute_circuit_breaker().record_trade_result("playbook", float(pnl), symbol)
        except Exception as _acb_e:
            logger.debug(f"[风控接线] ACB record 跳过 {symbol}: {_acb_e}")
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

    # 根因修复: 同时关闭 open_positions, 消灭幽灵仓源头
    # 2026-07-14: 软删除→直接DELETE, 软删除行无消费者(所有查询都过滤sync_status),
    #             且auto_position_reconcile只在09:30 ET运行, 非交易日/非开盘时残留→EOD报告误读
    if symbol:
        # 2026-07-16 P1 修复: sync_status NULL 行也会被删除 (旧 NOT IN 不匹配 NULL).
        #   正常流程 sync_status 会被设置, 但重启恢复或异常写入可能 NULL.
        conn.execute(
            "DELETE FROM open_positions WHERE symbol=? "
            "AND (sync_status IS NULL OR sync_status NOT IN "
            "('closed_by_daemon','closed_by_sync','closed_emergency','stale_not_in_broker'))",
            (symbol,)
        )
    conn.commit()

    # 2026-07-21 修复: 平仓落库后同步注销 ManualHoldRegistry.
    #   根因: 开仓时注册 (playbook_service "防 auto pipeline 干扰") 但平仓路径从不注销,
    #   进程内残留 → position_monitor 对账把已平仓标的一直报"跳过手动持仓",
    #   且 registry 被陈旧条目污染 (仅服务重启才清零). fail-open: 注销异常不影响平仓.
    #   回滚: 删除本块即可 (仅影响对账提示与手动仓判定, 不影响平仓写入).
    if symbol:
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry as _mhr_close
            if _mhr_close.get_hold_info(symbol) is not None:
                _mhr_close.unregister(symbol)
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
            logger.warning(f"[归因] {symbol} 自动归因失败 (不影响平仓): {_attr_e}", exc_info=True)

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
            try:
                _entry_px = float(_tr[2] or 0) if _tr is not None else 0
                _mfe = float(_meta.get("mfe_pct", 0) or 0)
                _px_extra = {
                    "entry_price": _entry_px or None,
                    "exit_price": float(exit_price or 0) or None,
                    "held_h": round(float(holding_hours or 0), 1),
                    "mfe_pct": _mfe if _meta.get("mfe_pct") is not None else None,
                    "mae_pct": float(_meta.get("mae_pct", 0) or 0),
                    "time_to_mfe": _meta.get("time_to_mfe"),
                    "cohort": _meta.get("cohort"),
                    "horizon": _meta.get("horizon"),
                    "inv_price": _meta.get("alite_inv_price"),
                }
                if _mfe > 0:
                    _px_extra["capture_pct"] = round(
                        max(0.0, min(1.0, float(pnl_pct or 0) * 100.0 / _mfe)) * 100, 0)
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


# 2026-07-19 (DEB-round3 #6-A): factor_exit 缺规则 WARN 去重 (进程级, 防刷屏)
_factor_exit_warned = set()


def _get_factor_exit_config(conditions: list, factor_rules: dict, direction: str = None) -> dict:
    """根据触发因子选择对应的退出规则配置 (Phase 3 2026-06-30).

    7/7 fix: 方向感知匹配。BUY 优先匹配 BULLISH_CONDITIONS, SHORT 优先匹配 BEARISH_CONDITIONS。
    避免 SHORT 指令因第一个 condition 是趋势类而配上 trailing=true 的退出策略。

    Args:
        conditions: 触发条件列表 (e.g. ['insider_cluster_buy', 'above_ma20'])
        factor_rules: playbook_horizon.yaml 顶层 factor_exit_rules 字典
        direction: "LONG"/"SHORT" — 方向感知匹配 (None 时退回原始逻辑)

    Returns:
        匹配到的因子退出规则, 未命中返回 default
    """
    default = factor_rules.get("default", {})
    if not conditions:
        return default

    # 7/7 fix: 方向感知 — 优先匹配与方向一致的 condition
    if direction:
        from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS
        _preferred = BULLISH_CONDITIONS if direction == "LONG" else BEARISH_CONDITIONS
        # 第一轮: 方向一致 + 有 exit_rule 的 condition
        for cond in conditions:
            if cond in factor_rules and cond in _preferred:
                return factor_rules[cond]

    # 第二轮: 方向一致的没匹配到, 退回第一个有 exit_rule 的 condition
    # P0-2 fix (2026-07-20): 跳过方向相反的 condition — 原逻辑第二轮无方向过滤,
    #   SHORT 指令的 bullish conditions (如 macd_golden_cross) 会配上 trailing=true 规则,
    #   与 7/7 fix 注释目标矛盾. 修复: 第二轮仍排除方向相反因子, 无命中则落 default.
    for cond in conditions:
        if cond in factor_rules:
            if direction:
                from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS
                _opposite = BEARISH_CONDITIONS if direction == "LONG" else BULLISH_CONDITIONS
                if cond in _opposite:
                    continue
            return factor_rules[cond]

    # 2026-07-19 (DEB-round3 #6-A): conditions 非空但零命中 → WARN (去重防刷屏).
    #   根因: 新因子进了 conditions 但未配 factor_exit_rules → 静默落 default,
    #   退出行为与设计意图漂移而无人察觉. 只告警不改行为 (default 兜底不变).
    _unregistered = [c for c in conditions if c not in factor_rules]
    if _unregistered:
        _wkey = tuple(sorted(str(c) for c in _unregistered))
        if _wkey not in _factor_exit_warned:
            _factor_exit_warned.add(_wkey)
            logger.warning(
                f"[playbook] #6-A factor_exit 缺规则: conditions={_unregistered} 未注册 "
                f"factor_exit_rules, 落 default 退出 (direction={direction}) — "
                f"请在 playbook_horizon.yaml 补配或确认有意为之"
            )

    return default


def _compute_dynamic_effective_sl(
    direction: str,
    entry_price: float,
    initial_sl: float,
    peak_profit_pct: float,
    exit_cfg: dict,
    fee_cushion_pct: float = 0.0,
) -> float:
    """根据因子退出规则计算动态 effective stop loss.

    逻辑:
    - breakeven_at_pct: 浮盈达到该阈值, SL 提到 entry (保本)
    - drawdown_from_peak_pct: 从峰值回撤超过该百分比时, SL锁定到 peak×(1-drawdown)
      2026-07-09: 替代原步进trailing. 原trailing_step=2%但MFE中位数仅1.5%, 永远触发不了.
      新逻辑: 浮盈2%→峰值, 回撤0.6%→SL锁在peak-0.6%=1.4%利润. 高波动美股也能落袋.
    - 兼容: 若无drawdown_from_peak_pct配置, 回退原步进逻辑(最多3步).
    """
    breakeven_pct = float(exit_cfg.get("breakeven_at_pct", 0.5))
    # 2026-08-08 (DEB-20260808 §17.3-2 + §22 审计修正): SL 收紧系数 sl_tighten_beta
    #   β=0.20 被审计否决: 收紧后 SL 0.6-1.2% 全落 7/9 证伪的 <2% 噪声扫损区 (WR47% 亏$1352).
    #   修正 β=0.50 + 下限 1.5%: 收紧后 SL 1.5-2.5%, 贴近 7/9 最优区 (2-3% WR59%) 下沿,
    #   保留 ~1/3 模拟改善 (双臂对照 β0.5 +$596 vs β0.2 +$1555), 风险大幅下降.
    #   回滚: sl_tighten_beta 改 1.0.
    try:
        from app.playbook.config import get_playbook_config as _get_pb_cfg_tb
        _sl_tb = float(_get_pb_cfg_tb().get("sl_tighten_beta", 1.0))
        _sl_tb_min = float(_get_pb_cfg_tb().get("sl_tighten_min_pct", 1.5))
    except Exception:
        _sl_tb = 1.0
        _sl_tb_min = 1.5
    if _sl_tb < 1.0 and initial_sl > 0:
        if direction == "LONG":
            # 收紧 = SL 上移向 entry; 但不得高于 -(min_pct) 线 (防过紧误扫正常回撤)
            initial_sl = min(entry_price - (entry_price - initial_sl) * _sl_tb,
                             entry_price * (1 - _sl_tb_min / 100))
        else:
            # SHORT 对称: 收紧 = SL 下移向 entry; 不得低于 +(min_pct) 线
            initial_sl = max(entry_price + (initial_sl - entry_price) * _sl_tb,
                             entry_price * (1 + _sl_tb_min / 100))
    effective_sl = initial_sl

    if peak_profit_pct < breakeven_pct:
        return effective_sl

    # 浮盈超过 breakeven: 先保本
    # 2026-07-17: 净保本 — 保本线含手续费缓冲 (成本价 ± fee_cushion), 确保被保本SL打掉时不亏双边费用.
    if direction == "LONG":
        _be_line = entry_price * (1 + fee_cushion_pct / 100.0)
        effective_sl = max(effective_sl, _be_line)
    else:
        _be_line = entry_price * (1 - fee_cushion_pct / 100.0)
        effective_sl = min(effective_sl if effective_sl > 0 else _be_line, _be_line)

    if not exit_cfg.get("trailing_enabled", False):
        # 2026-07-18 profit_override (三轮辩论裁决): 浮盈超过阈值时强制启用锁利.
        #   根因: trailing_enabled=false 的因子(17个"快速证伪"类)只有保本没有锁利,
        #   浮盈 3.89% 的 COIN SHORT 最终保本出场, 白送 3.9% 利润.
        #   修复: 浮盈 >= 1.5% (3×breakeven) 时, 不管因子配置, 强制启用回撤锁利.
        #   安全: 0-1.5% 证伪期行为不变, 设计意图兼容.
        #   回滚: 设 profit_override_threshold_pct=999 即禁用.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_override
            _po_cfg = _get_pb_cfg_override()
            _po_threshold = float(_po_cfg.get("profit_override_threshold_pct", 1.5))
            _po_dd = float(_po_cfg.get("profit_override_drawdown_pct", 0.5))
        except Exception:
            _po_threshold = 1.5
            _po_dd = 0.5
        if peak_profit_pct < _po_threshold:
            return effective_sl
        # 浮盈已大, 强制锁利: SL = peak × (1 - dd%), 但不低于保本线
        if direction == "LONG":
            _po_peak = entry_price * (1 + peak_profit_pct / 100)
            _po_sl = _po_peak * (1 - _po_dd / 100)
            effective_sl = max(effective_sl, _po_sl)
        else:
            _po_peak = entry_price * (1 - peak_profit_pct / 100)
            _po_sl = _po_peak * (1 + _po_dd / 100)
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, _po_sl)
        return effective_sl

    # ── 回撤止损 (新逻辑, 2026-07-09) ──
    drawdown_pct = float(exit_cfg.get("drawdown_from_peak_pct", 0))
    if drawdown_pct > 0 and peak_profit_pct > breakeven_pct:
        # SL = peak_price × (1 - drawdown_pct/100), 但不低于保本
        if direction == "LONG":
            peak_price = entry_price * (1 + peak_profit_pct / 100)
            dd_sl = peak_price * (1 - drawdown_pct / 100)
            effective_sl = max(effective_sl, dd_sl)
        else:
            peak_price = entry_price * (1 - peak_profit_pct / 100)
            dd_sl = peak_price * (1 + drawdown_pct / 100)
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, dd_sl)
        return effective_sl

    # ── 回退: 原步进 trailing (兼容旧配置) ──
    trailing_step = float(exit_cfg.get("trailing_step_pct", 2.0))
    if trailing_step > 0:
        for step in range(1, 4):  # 最多 3 步
            threshold = breakeven_pct + trailing_step * step
            if peak_profit_pct < threshold:
                break
            locked_pct = breakeven_pct + trailing_step * (step - 1) + trailing_step * 0.5
            if direction == "LONG":
                effective_sl = max(effective_sl, entry_price * (1 + locked_pct / 100))
            else:
                effective_sl = min(effective_sl if effective_sl > 0 else entry_price,
                                 entry_price * (1 - locked_pct / 100))

    return effective_sl


def is_playbook_open_position(symbol: str) -> bool:
    """统一判定: 该 symbol 是否有未平仓的 Playbook 仓位 (由 monitor_playbook_sim_exits 自管).

    所有旧管线入口 (position_sync, position_monitor, scheduler_core 等) 必须调用此函数
    跳过 Playbook 管理的仓位, 防止双重管理/重复平仓.

    判定标准: trade_records 中存在 source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') 且 exit_time
    为空的记录. 与 monitor_playbook_sim_exits 的查询对齐 (含 LIVE, 切 live 后不漏兜底护栏).
    不使用时间窗口 (旧代码用 yesterday/2-days-ago 导致跨日/跨窗口漏判).

    异常处理 (分层 fail-closed):
      - sqlite3.OperationalError (锁超时/DB busy): return True (fail-closed) — 宁可让主线跳过,
        由 sim_exits 自管, 避免 DB 异常时护栏失效导致主线双重平仓.
      - 其他未知异常: return False + warning — 不无条件 fail-closed, 否则结构性错误 (schema/路径)
        会让主线对所有 symbol 永久失管、漏平真实风险仓.
    """
    import sqlite3 as _pb_sql
    from app.core.paths import get_trade_records_db
    _c = None
    try:
        _c = _pb_sql.connect(str(get_trade_records_db()), timeout=5)
        _row = _c.execute(
            "SELECT 1 FROM trade_records WHERE symbol=? "
            "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL) LIMIT 1",
            (symbol,)
        ).fetchone()
        return _row is not None
    except _pb_sql.OperationalError as e:
        logger.warning(
            f"[playbook护栏] is_playbook_open_position({symbol}) DB 锁超时/忙: {e} "
            f"→ fail-closed (return True, 主线跳过交由 sim_exits 自管)"
        )
        return True
    except Exception as e:
        logger.warning(
            f"[playbook护栏] is_playbook_open_position({symbol}) 未知异常: {e} "
            f"→ return False (主线接管). 请排查 DB schema/路径."
        )
        return False
    finally:
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass


# 规则0.5 审计日志去重集 (key: "symbol:date") — 见 _check_playbook_exit_rules 规则0.5 段.
# 进程级, 每 symbol每日 只记一次紧SL生效事件, 防止 monitor 轮询刷屏.
_EARLY_TIGHT_SL_LOGGED: set = set()


def _extract_invalidation_price(text: str, direction: str, entry_price: float):
    """A-lite shadow (DEB-20260723): 从 DA invalidation 自然语言提取首个方向合理的证伪价.

    取 $123.45 / 跌破578 样式的价格, 方向合理性夹逼防误取无关数字:
      LONG : 证伪价 ∈ [entry*0.5, entry)   (跌破证伪)
      SHORT: 证伪价 ∈ (entry, entry*1.5]   (升破证伪)
    无命中返回 None (落 meta 占位, 避免监控轮重复解析). record-only, 不参与退出决策.
    """
    if not text or not entry_price or entry_price <= 0:
        return None
    try:
        import re as _re
        prices = [float(m) for m in _re.findall(r"\$\s*(\d+(?:\.\d+)?)", text)]
        if not prices:  # 兼容 "跌破578美元" / "跌破 578" 样式
            prices = [float(m) for m in _re.findall(
                r"(?:跌破|升破|突破|跌至|回落至)\s*(\d+(?:\.\d+)?)", text)]
        for p in prices:
            if direction == "LONG" and entry_price * 0.5 <= p < entry_price:
                return round(p, 4)
            if direction == "SHORT" and entry_price < p <= entry_price * 1.5:
                return round(p, 4)
    except Exception:
        pass
    return None


def _track_bar_peak_trough(meta, data_router, symbol, entry_time_str, direction="LONG") -> bool:
    """#3 (2026-07-20, record-only): 1min bar 高低点并行采集 peak_price_bar/trough_price_bar.

    背景: peak/trough 现用每轮 last quote 采样, 轮询间隙的真实高/低点全部丢失 →
    MFE 系统性低估 (实证 SL_TRAIL_LOCK 批次 MFE 1.48% 仅捕获一半), trailing 判分失真,
    exit_shadow 反事实也吃同一个失真 peak → 影子判分被污染.

    本函数用 1min K线 high/low 做并行测量 (仅落 metadata, **不参与任何退出决策**).
    周度对比 bar 版 vs quote 版差距, 再决定是否切换决策输入
    (切换=显式行为变更, 需开新 config_version 窗口, 另行批准).
    K线来源: futu LV3 get_kline (300s 持久缓存 + push 缓存, 基本不额外耗配额).
    已知噪声: 入场分钟的 bar 含入场前 tick, 峰谷可能微量高估 (可接受, 判分时知晓).

    方向语义 (与 quote 版 peak_price/trough_price 对齐):
      LONG:  peak_price_bar = max(highs)  最有利; trough_price_bar = min(lows)  最不利
      SHORT: peak_price_bar = min(lows)   最有利; trough_price_bar = max(highs) 最不利

    Args:
        direction: "LONG"/"SHORT" (与 monitor 循环的 direction 变量一致)

    Returns: metadata 是否有更新.
    """
    try:
        _futu = getattr(data_router, "futu", None)
        if _futu is None or not hasattr(_futu, "get_kline"):
            return False
        try:
            _edt = datetime.fromisoformat(entry_time_str)
            # 2026-07-28 (DEB-bar-ruler F1① 复核确证): futu K_1M time_key 是分钟末标签
            #   (bar "09:36" 覆盖 09:35:00-59; 开盘量签名实证: 首根 09:31 带开盘巨量、无
            #   09:30 bar)。旧版按首标签过滤把入场前一分钟整分钟混入 (SOLS trough_bar
            #   60.777 实为入场前低点)。末标签口径: 入场分钟的 bar 标签 = 入场分钟+1min。
            from datetime import timedelta as _btd
            _entry_key = (_edt + _btd(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
        except Exception:
            return False
        # 2026-07-20: count=800 — HTTP 冷路径 start=T-1 时 OpenD 返回"最旧N根",
        #   count 小于范围总数(~780)会拿到纯 T-1 bar → 入场后过滤恒空 → 采集失效.
        # 2026-07-28 (F2): autype=NONE — 本尺子与原始成交价对比, 不得用前复权 bar
        #   (除息日附近会错位 ~0.3-0.5%)。技术指标侧仍用缺省 QFQ, 互不影响。
        try:
            bars = _futu.get_kline(symbol, ktype="K_1M", count=800, autype="NONE") or []
        except TypeError:  # 旧签名兼容 (provider 未重载时不阻断采集)
            bars = _futu.get_kline(symbol, ktype="K_1M", count=800) or []
        if not bars:
            return False
        highs, lows = [], []
        _max_key = ""
        for b in bars:
            _t = str(b.get("time") or b.get("time_key") or "")
            if _t > _max_key:
                _max_key = _t
            if not _t or _t < _entry_key:
                continue
            _h = float(b.get("high", 0) or 0)
            _l = float(b.get("low", 0) or 0)
            if _h > 0:
                highs.append(_h)
            if _l > 0:
                lows.append(_l)
        changed = False
        # 2026-07-28 (F1②③): 覆盖可观测 + 冻结告警 — 07-27 实锤 bar 峰值锁死第一根
        #   (真峰 61.695 全程未采), 降级链无声。bar_covered_until 使判分端可识别冻结;
        #   连续 5 轮不前进 WARNING 一次 (静默退化军规)。冻结真因 (TTL 短路/配额/订阅)
        #   由本可观测性上线后用真数据裁决 — 起案"配额打爆"假设未获日志证实, 存疑。
        if _max_key:
            _prev_covered = str(meta.get("bar_covered_until") or "")
            if _max_key > _prev_covered:
                meta["bar_covered_until"] = _max_key
                _BAR_STALL_SINCE.pop(symbol, None)
            else:
                # 2026-08-05 修复 (A1 轮询 10s 后误报): 原"5 轮"阈值与轮询间隔耦合 —
                #   K_1M 持久缓存 60s TTL 下 bar 最快每 6 轮才前进一次, 5 轮阈值每 60s 必误报.
                #   改时间制 (模块级计时, 不依赖 meta 写回): 覆盖 ≥180s 未前进才 WARNING.
                import time as _btime
                _since = _BAR_STALL_SINCE.get(symbol)
                if not _since:
                    _BAR_STALL_SINCE[symbol] = _btime.time()
                elif _btime.time() - _since >= 180:
                    logger.warning(
                        f"[bar遥测·冻结] {symbol} bar 覆盖停在 {_max_key} 已 "
                        f"{int(_btime.time() - _since)}s 未前进 "
                        f"(缓存/配额/订阅链降级?) — bar 峰谷可能失真"
                    )
                    _BAR_STALL_SINCE[symbol] = _btime.time()  # 告警后重计时, 防每轮刷屏
        _is_short = direction.upper() in ("SHORT", "SELL_SHORT")
        if not _is_short:
            # LONG: peak = max(highs) 最有利, trough = min(lows) 最不利
            if highs:
                _old_pk = float(meta.get("peak_price_bar", 0) or 0)
                _new_pk = max(highs)
                if _new_pk > _old_pk:
                    meta["peak_price_bar"] = round(_new_pk, 4)
                    changed = True
            if lows:
                _old_tr = float(meta.get("trough_price_bar", 0) or 0)
                _new_tr = min(lows)
                if _new_tr > 0 and (_old_tr <= 0 or _new_tr < _old_tr):
                    meta["trough_price_bar"] = round(_new_tr, 4)
                    changed = True
        else:
            # SHORT: peak = min(lows) 最有利, trough = max(highs) 最不利
            if lows:
                _old_pk = float(meta.get("peak_price_bar", 0) or 0)
                _new_pk = min(lows)
                if _new_pk > 0 and (_old_pk <= 0 or _new_pk < _old_pk):
                    meta["peak_price_bar"] = round(_new_pk, 4)
                    changed = True
            if highs:
                _old_tr = float(meta.get("trough_price_bar", 0) or 0)
                _new_tr = max(highs)
                if _new_tr > _old_tr:
                    meta["trough_price_bar"] = round(_new_tr, 4)
                    changed = True
        return changed
    except Exception:
        return False


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str):
    """共用退出规则判断 — 被 monitor_playbook_sim_exits 调用.

    计算盈利百分比 → peak tracking → breakeven ratchet / trailing stop (factor-specific) → 6 条退出规则.
    退出规则 (按优先级):
      [0] 开盘冷却期: 前15min SL放宽到4% (防开盘噪声秒杀)
      [0.5] 早期紧SL: 入场后30min内未浮盈达标 → SL收紧到1% (防方向全错慢性失血)
      [1] SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK: effective_sl 触及 (按初始止损/保本/锁利区分)
      [1.5] TAKE_PROFIT: 触及止盈价 (RR≥1.5 才生效)
      [2] OVERNIGHT_GAP_STOP: cohort B 隔夜仓跳空 >3%
      [3] PRE_CLOSE_EXIT: 收盘前 8min (cohort B 盈利可跳过)
      [3.4] MFE_STALL_EXIT: 持仓≥150min 且 峰值浮盈<0.5% 且 当前水下 → 提前离场 (2026-07-24)
      [4] TIME_EXIT: 持仓 > factor_time_exit_hours 且 盈利<0 (factor-specific)
      [5] MAX_HOLD_T5: cohort B 持仓≥5 交易日

    Args:
        direction: "LONG"/"SHORT"
        entry_price: 入场价
        current_price: 当前报价
        initial_sl: 初始 stop_loss (meta["stop_loss"])
        peak_price: 历史峰值价 (从 meta 恢复, 函数内更新后返回)
        meta: metadata dict (读取 cohort/_skip_allowed/_log_tag/_symbol/conditions)
        held_minutes: 持仓分钟数
        minutes_to_close: 距收盘剩余分钟 (ET)
        minutes_from_open: 开盘后分钟数 (ET)
        entry_time_str: entry_time ISO 字符串

    Returns: (exit_reason, exit_price, peak_price, breakeven_triggered)
        exit_reason 为 "" 表示不退出; 调用方负责 peak_price metadata 写回.
    """
    _log_tag = meta.get("_log_tag", "playbook")
    _symbol = meta.get("_symbol", "")

    # ── 盈利百分比计算 ──
    if direction == "LONG":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    # ── peak tracking (从参数恢复, 更新后返回) ──
    if direction == "LONG":
        if current_price > peak_price:
            peak_price = current_price
    else:
        if peak_price == 0 or current_price < peak_price:
            peak_price = current_price

    if peak_price > 0:
        if direction == "LONG":
            peak_profit_pct = (peak_price - entry_price) / entry_price * 100
        else:
            peak_profit_pct = (entry_price - peak_price) / entry_price * 100
    else:
        peak_profit_pct = 0

    # ── 读取 horizon 配置 + 因子退出规则 (Phase 3 2026-06-30) ──
    _cohort = meta.get("cohort", "A")
    _horizon = meta.get("horizon", "short")
    # _skip_allowed: 盈利仓跳过 EOD 的条件
    _skip_allowed = meta.get("_skip_allowed", _cohort == "B" or _horizon == "long")
    # 极简版B (2026-07-17): 隔夜总开关关闭时强制不跳过 EOD/PRE_CLOSE → 全部当日平仓.
    #   一处收口: 失效 OVERNIGHT_GAP_STOP / PRE_CLOSE_EXIT 盈利仓 pass / MAX_HOLD 三个隔夜分支.
    from app.playbook.config import get_playbook_config as _get_pb_cfg
    if not _get_pb_cfg().get("overnight_hold_enabled", False):
        _skip_allowed = False
    _horizon_cfg = _load_horizon_config(_horizon)
    _overnight_gap_stop_pct = _horizon_cfg.get("overnight_gap_stop_pct", 3.0)

    from app.playbook.config import get_playbook_config
    _pb_cfg = get_playbook_config()
    _factor_exit_rules = _pb_cfg.get_horizon_top("factor_exit_rules", {})
    _default_exit_cfg = _factor_exit_rules.get("default", {})

    # 改动9 [已退役 2026-07-11]: exit_param_override.json 合并 — 测量优先重构下移除.
    #   根因: 消费端只匹配 TIME_EXIT, 但 Hermes feedback_loop 生成端按 direction+exit_reason
    #   分组【混合 v1+v2】全样本, 可能产出 LONG+TIME_EXIT 从而【静默篡改】factor_exit_rules —
    #   典型"打影子"(用旧策略数据硬调新策略退出参数). 此处移除消费端后影响链已切断,
    #   生成的 json 现无人读取(惰性). 生成端在 Hermes skill (工作区外, feedback_loop.py:348/439),
    #   需手动注掉那两行调用才能彻底停写. 参数重设留到 Section 5 启用门槛满足后、在 v2 数据上另起执行阶段.
    #   (原合并逻辑见 git 历史; 此处保持 no-op, factor_exit_rules 仅由 yaml 决定.)

    # 根据触发因子选择退出规则 (7/7: 传 direction 做方向感知匹配)
    _conditions = meta.get("conditions", [])
    _exit_cfg = _get_factor_exit_config(_conditions, _factor_exit_rules, direction=direction)

    # 最大持有天数: 优先因子级, 其次 horizon 级
    _max_hold_days = int(
        _exit_cfg.get(
            "max_hold_days",
            _horizon_cfg.get("max_hold_days", 5 if _horizon == "long" else 1),
        )
    )

    # ── effective SL (factor-specific breakeven ratchet + trailing stop) ──
    # Phase 3 (2026-06-30): 按因子类型动态调整利润锁定/跟踪止损
    _fee_cushion_pct = float(_pb_cfg.get("breakeven_fee_cushion_pct", 0.0))
    # 2026-07-30 (DEB-fee-net-pnl D4, 用户裁"直接实现"): 费用感知保本线 — 拍的 0.15%
    #   换真实往返费用/名义额 (真成本由 $/股固定佣金主导: 小仓 cushion 低估→假保本净亏,
    #   大仓高估→锁利线虚高)。同一 _fee_cushion_pct 变量下游三处 (ratchet/退出归类/
    #   be_shadow B 锚) 自动同口径。失败/无股数 → 回退旧 cushion; 回滚=关 yaml 开关。
    if _pb_cfg.get("breakeven_fee_aware_enabled", False):
        try:
            _d4_qty = float(meta.get("_qty", 0) or 0)
            if _d4_qty > 0 and entry_price > 0:
                from app.execution.fee_model import estimate_round_trip as _d4_rt
                _d4_fee = _d4_rt(_d4_qty, entry_price, entry_price, direction)["total"]
                _fee_cushion_pct = _d4_fee / (_d4_qty * entry_price) * 100.0
                meta["_be_cushion_fee_aware"] = round(_fee_cushion_pct, 4)  # 验收可观测
        except Exception:
            pass  # 回退旧 cushion (静默容错: 保本链路不得因记账增强而阻断)
    effective_sl = _compute_dynamic_effective_sl(
        direction, entry_price, initial_sl, peak_profit_pct, _exit_cfg, _fee_cushion_pct
    )
    breakeven_triggered = peak_profit_pct >= float(_exit_cfg.get("breakeven_at_pct", 0.5))

    # ── 退出判断 (按优先级, if/elif/elif 链) ──
    # 优先级: SL > TAKE_PROFIT > OVERNIGHT_GAP > PRE_CLOSE > TIME_EXIT > MAX_HOLD
    exit_reason = ""
    exit_price = current_price

    # 规则0: 开盘冷却期 — 前15分钟SL放宽到4% (不改SL参数本身, 只加时间窗口保护)
    # 原因: 3/5笔在0-6min秒杀, MFE=0%, 价格从未浮盈. 开盘噪声大, 2%SL被瞬间打掉.
    # 诊断: 修后MFE>0%=时机问题, MFE仍=0%=信号问题
    _cooldown_pct = float(_pb_cfg.get("cooldown_sl_pct", 4.0))  # 2026-07-16: 从 config 读取, 默认 4%
    _cooldown_sl = entry_price * (1 - _cooldown_pct / 100.0) if direction == "LONG" else entry_price * (1 + _cooldown_pct / 100.0)
    _in_cooldown = minutes_from_open is not None and minutes_from_open < 15
    if _in_cooldown:
        _active_sl = min(effective_sl, _cooldown_sl) if direction == "LONG" else max(effective_sl, _cooldown_sl)
    else:
        _active_sl = effective_sl

    # 规则0.5 (2026-07-19): 早期紧SL — 入场后 N 分钟内未浮盈达标 → SL收紧到 early_tight_sl_pct.
    # 数据依据: TIME_EXIT 11笔 0%WR, MFE 全部<0.5%, 方向从未兑现但等 4-6h 才触发 TIME_EXIT.
    # 逻辑: 如果方向对, 价格很快向有利方向移动, breakeven 激活后本规则自动失效;
    #        如果方向错, 1% 紧SL 在 ~15-30min 内触发 SL_INITIAL → 快速脱身, 只亏1%而非等4h.
    # 与 cooldown 互补: cooldown 防开盘瞬间秒杀(放宽到4%); 本规则防方向全错慢性失血(收紧到1%).
    #        cooldown 优先(开盘噪声>方向信号); cooldown 过期后本规则接管.
    # 退出原因: 仍为 SL_INITIAL (breakeven 未激活), 无需新标签, 复用现有基建.
    # 回滚: early_tight_sl_enabled 改 false. 判分: TIME_EXIT 笔数下降 + SL_INITIAL 早期笔数/损失.
    _early_tight_enabled = bool(_pb_cfg.get("early_tight_sl_enabled", False))
    if _early_tight_enabled and not _in_cooldown and not breakeven_triggered:
        _et_minutes = float(_pb_cfg.get("early_tight_sl_minutes", 30))
        _et_sl_pct = float(_pb_cfg.get("early_tight_sl_pct", 1.0))
        if held_minutes <= _et_minutes:
            _et_sl = entry_price * (1 - _et_sl_pct / 100.0) if direction == "LONG" \
                else entry_price * (1 + _et_sl_pct / 100.0)
            # 紧SL = 更靠近入场价 = LONG取max, SHORT取min
            _sl_before_tight = _active_sl
            _active_sl = max(_active_sl, _et_sl) if direction == "LONG" else min(_active_sl, _et_sl)
            # 审计日志 (2026-07-19 辩论收敛#2): 紧SL实际生效(改变了SL)时每 symbol每日 只记一次,
            #   供 exit_scorecard 判分用: "SL_INITIAL 早期出场" ← 本日志记录, 可区分紧SL触发 vs 正常2%SL触发.
            #   防刷屏: monitor 轮询期间同一仓位每轮都命中本分支, 模块级 set 去重 (进程重启后重记一次, 可接受).
            if _active_sl != _sl_before_tight:
                _et_log_key = f"{_symbol}:{date.today().isoformat()}"
                if _et_log_key not in _EARLY_TIGHT_SL_LOGGED:
                    _EARLY_TIGHT_SL_LOGGED.add(_et_log_key)
                    logger.info(
                        f"[规则0.5-早期紧SL] {_symbol} {direction} held={held_minutes:.1f}min "
                        f"SL {_sl_before_tight:.4f}→{_active_sl:.4f} "
                        f"(entry={entry_price:.4f}, tight={_et_sl_pct}%)"
                    )

    # 规则1: SL止损 (最高优先级 — 亏损仓位必须先检查止损)
    # 2026-07-14: 拆分原 SL_FIXED_PCT 复合标签. 同一标签曾混合三种机制(近30天9笔仅1笔
    #   真止损, 其余为保本+锁利), 污染退出侧诊断. 按 effective_sl 相对 entry 的位置区分,
    #   便于 exit_shadow / 报表按真实语义归因. 保留 SL_ 前缀以兼容 LIKE 'SL%' 过滤.
    if _active_sl > 0 and ((direction == "LONG" and current_price <= _active_sl) or
                           (direction == "SHORT" and current_price >= _active_sl)):
        # 2026-07-16 P0 修复: exit_price 用 current_price 而非 _active_sl.
        #   _active_sl 是止损线价格, 实际市价单成交价可能远低于 SL (滑点/gap).
        #   旧逻辑 exit_price = _active_sl 系统性高估退出价 → PnL 偏高.
        #   修复: 用 current_price (本轮报价) 作为退出价, 与 broker 实际成交更接近.
        #   最终退出价在调用方 (monitor_playbook_sim_exits) 用 broker filled_price 覆盖.
        exit_price = current_price
        # 2026-07-17: 分类阈值用"净保本线"(成本±fee_cushion), 否则纯净保本会被误标 SL_TRAIL_LOCK.
        _be_line = entry_price * (1 + _fee_cushion_pct / 100.0) if direction == "LONG" \
            else entry_price * (1 - _fee_cushion_pct / 100.0)
        _be_tol = entry_price * 1e-4  # 1bp 容差, 吸收浮点/取整
        if not breakeven_triggered:
            exit_reason = "SL_INITIAL"        # 浮盈未达 breakeven → 初始固定止损被打掉
        elif (direction == "LONG" and effective_sl > _be_line + _be_tol) or \
             (direction == "SHORT" and 0 < effective_sl < _be_line - _be_tol):
            exit_reason = "SL_TRAIL_LOCK"     # 回撤锁利 → SL 已锁在净保本线之上(空:之下)
        else:
            exit_reason = "SL_BREAKEVEN"      # 净保本 ratchet → SL 提到成本价+手续费缓冲附近
            # ── 死亡谷 shadow 方案A (2026-07-28, record-only): breakeven 触发线 0.5%→0.8%
            #   反事实 — 近两周 26 笔均浮盈 0.96% 只捕获 11% (保本锚上被回踩杀)。
            #   触发时 MFE<0.8% 的仓在 A 方案下尚无保本锚→不会在此出场; 记快照,
            #   最终反事实出场由离线回放补 (出场后走势超出仓位生命周期)。回滚: 删本块。
            try:
                if peak_profit_pct < 0.8 and not meta.get("_be_shadow_a"):
                    meta["_be_shadow_a"] = True
                    import json as _bj
                    from app.utils.timezone import now_et as _be_now
                    with open("data/breakeven_shadow.jsonl", "a") as _bf:
                        _bf.write(_bj.dumps({
                            "variant": "A_no_be_yet", "ts": _be_now().isoformat(),
                            "symbol": _symbol, "direction": direction,
                            "entry_price": entry_price, "real_exit_price": current_price,
                            "mfe_at_trigger": round(peak_profit_pct, 4),
                            "initial_sl": initial_sl, "held_minutes": round(held_minutes, 1),
                        }, ensure_ascii=False) + "\n")
                    logger.info(f"[影子·保本谷A] {_symbol} MFE={peak_profit_pct:.2f}%<0.8% "
                                f"真实保本出@{current_price} (A方案不会在此出场, record-only)")
            except Exception:
                pass

    # ── 死亡谷 shadow 方案B (2026-07-28, record-only): 保本锚 +0.15% 缓冲的反事实 —
    #   B 锚更高→更早触发: 真实仓未出场但 B 锚已被碰到时记录反事实出场点。
    #   真实仓继续持有, 后续真实结局与 B 出场点可直接对比 (无需离线回放)。
    #   仅当 B 锚严于当前生效 SL (真实不会同点出) 才是真差异点。回滚: 删本块。
    if (not exit_reason and breakeven_triggered and not meta.get("_be_shadow_b")
            and _active_sl > 0):
        try:
            _b_buffer = 0.15
            # 块内自算净保本线 (规则1 内的 _be_line/_be_tol 在本路径未定义 — NameError
            # 会被 try 吞成永久静默, shadow now_et 教训同款, 自查揭出)
            _b_be_line = entry_price * (1 + _fee_cushion_pct / 100.0) if direction == "LONG" \
                else entry_price * (1 - _fee_cushion_pct / 100.0)
            _b_be_tol = entry_price * 1e-4
            _b_anchor = _b_be_line * (1 + _b_buffer / 100.0) if direction == "LONG" \
                else _b_be_line * (1 - _b_buffer / 100.0)
            _b_hit = (direction == "LONG" and current_price <= _b_anchor) or \
                     (direction == "SHORT" and current_price >= _b_anchor)
            _b_differs = (direction == "LONG" and _b_anchor > _active_sl + _b_be_tol) or \
                         (direction == "SHORT" and _b_anchor < _active_sl - _b_be_tol)
            if _b_hit and _b_differs:
                meta["_be_shadow_b"] = True
                import json as _bj
                from app.utils.timezone import now_et as _be_now
                with open("data/breakeven_shadow.jsonl", "a") as _bf:
                    _bf.write(_bj.dumps({
                        "variant": "B_buffer_exit", "ts": _be_now().isoformat(),
                        "symbol": _symbol, "direction": direction,
                        "entry_price": entry_price, "shadow_exit_price": current_price,
                        "b_anchor": round(_b_anchor, 4),
                        "mfe_pct": round(peak_profit_pct, 4),
                        "held_minutes": round(held_minutes, 1),
                    }, ensure_ascii=False) + "\n")
                logger.info(f"[影子·保本谷B] {_symbol} B锚({_b_anchor:.4f})被碰到 "
                            f"反事实出@{current_price}, 真实仓继续 (record-only)")
        except Exception:
            pass

    # 规则1.5 (2026-06-28): 止盈退出 — 价格触及 take_profit 时主动锁定利润.
    # 智能跳过: rr_ratio=999 / take_profit=0 的指令不触发 (用户决策).
    # 2026-07-10: 增加 RR 底线 (MIN_TP_RR=1.5). 根因: short_horizon rr_ratio=999
    #   (设计上不设TP, 让 trailing/EOD 捕获利润) 但指令级 analyst TP 仍会触发,
    #   出现 NVDA SL-4.5%/TP+1.7%(RR=0.38) 的畸形几何 — 冒大风险博小利润.
    #   修法: TP 相对【初始SL】的 RR < 1.5 时忽略该 TP, 交给 trailing/EOD;
    #   RR>=1.5 的好几何(如 NU RR=2.26)仍正常止盈. 无初始SL时不做门禁(退回原行为).
    _take_profit = meta.get("take_profit", 0)
    _MIN_TP_RR = float(_pb_cfg.get("min_tp_rr", 1.5))  # 2026-07-17: config 化 (SSOT), 原硬编码 1.5
    if not exit_reason and _take_profit and float(_take_profit) > 0:
        _tp = float(_take_profit)
        _tp_risk = abs(entry_price - initial_sl) if (initial_sl and initial_sl > 0) else 0
        _tp_reward = abs(_tp - entry_price)
        _tp_rr = (_tp_reward / _tp_risk) if _tp_risk > 0 else 999  # 无SL→不门禁
        if _tp_rr < _MIN_TP_RR:
            logger.debug(f"[TP-RR门禁] {_symbol} {direction} TP RR={_tp_rr:.2f}<{_MIN_TP_RR}, "
                         f"忽略指令TP(交给trailing/EOD锁利)")
        elif direction == "LONG" and current_price >= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price
        elif direction == "SHORT" and current_price <= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price

    # 规则2 (RFC-061 #6): 隔夜跳空止损
    if not exit_reason and _skip_allowed and _is_overnight_position(entry_time_str) and profit_pct <= -_overnight_gap_stop_pct:
        exit_reason = "OVERNIGHT_GAP_STOP"
        exit_price = current_price

    # 规则4: 盘末退出 — 7/7 fix: 恢复此规则
    # 原因: 7/3 fix 让 ExitMonitor 不再管 PLAYBOOK_SIM/LIVE 仓位 (reconciler 拦截→PROTECTED),
    #        6/30 DEB 把 pre_close 交给 ExitMonitor 的决策前提已不成立。
    #        PLAYBOOK 仓位的 pre_close 必须由 Pipeline 自管, 否则收盘前8min 盈利落袋无人执行。
    if not exit_reason and 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
        if _skip_allowed and profit_pct > 0:
            pass  # cohort B 盈利仓跳过, 持过夜
        elif _reentry_overnight_exempt(_symbol, entry_time_str, profit_pct):
            pass  # 2026-08-06 用户裁决: 再入场水下仓首夜豁免 EOD, 次日正常收
        else:
            exit_reason = "PRE_CLOSE_EXIT"
            exit_price = current_price

    # ── 高 ATR 退出档 shadow 探针 (2026-07-28 首夜复盘 #1, record-only 零行为影响) ──
    #   处方源: L2 回测 hi4.0 翻案 (高 ATR 不拦入场→转退出档) + 07-27 实证 (TSM/GS ATR 5%
    #   享 -5% 宽 SL + 150min 慢停损, 两笔 -$176)。shadow 假设: ATR≥4% 且 60min 内 MFE<0.3%
    #   且水下 → 反事实提前离场。只记录触发点价格, 不动真实退出; 与真实 exit 对比出
    #   反事实收益差, n≥5 后 08-07 裁决点定是否转 live。回滚: 删本块。
    try:
        _atr_val = float(meta.get("atr", 0) or 0)
        _atr_pct_live = (_atr_val / entry_price * 100.0) if (entry_price and _atr_val) else 0.0
        # 2026-08-05 (DEB-20260805 门B 终裁): 单一 60min 记录点扩为 45/60/75/90min 多时点 —
        #   原硬编码 held≥60 只记首次越线点, 时点选择零敏感性无法检验; 多时点同臂内对比.
        #   寄生特征 slope_5m/vwap_dev (复用 K_1M 300s 缓存, 零新采集器); record-only,
        #   见数前不得升级为判据 (预注册规范§3). 回滚: 删多时点/字段即恢复原行为.
        if (_atr_pct_live >= 4.0 and peak_profit_pct < 0.3 and profit_pct < 0):
            # 2026-08-05 修复: set 不可序列化且 meta 每轮 DB 重读 → 去重失效每 10s 重复发射.
            #   改 list 落 meta (JSON-safe, 随 metadata_json 写回跨轮持久) — 水下单 mae>0 必触发写回.
            _shadow_pts = meta.setdefault("_high_atr_stall_shadow_pts", [])
            if not isinstance(_shadow_pts, list):
                _shadow_pts = meta["_high_atr_stall_shadow_pts"] = []
            if meta.get("_high_atr_stall_shadow") and 60 not in _shadow_pts:
                _shadow_pts.append(60)  # 旧标志已在 60min 记录过, 防重复
            _hit = None
            for _pt in (45, 60, 75, 90):
                if held_minutes >= _pt and _pt not in _shadow_pts:
                    _hit = _pt
                    break
            if _hit is not None:
                _shadow_pts.append(_hit)
                if _hit == 60:
                    meta["_high_atr_stall_shadow"] = True  # 保留旧标志语义 (会话内 60 点已记)
                import json as _sj
                from pathlib import Path as _SP
                from app.utils.timezone import now_et as _shadow_now_et  # pipeline 无模块级 now_et
                _slope_5m = None
                _vwap_dev = None
                try:
                    from app.data.market_data_router import get_market_data_router as _get_rtr
                    _futu = getattr(_get_rtr(), "futu", None)
                    if _futu is not None and hasattr(_futu, "get_kline"):
                        try:
                            _sbars = _futu.get_kline(_symbol, ktype="K_1M", count=400, autype="NONE") or []
                        except TypeError:
                            _sbars = _futu.get_kline(_symbol, ktype="K_1M", count=400) or []
                        _today = _shadow_now_et().strftime("%Y-%m-%d")
                        _rows = []
                        for _b in _sbars:
                            _t = str(_b.get("time") or _b.get("time_key") or "")
                            if not _t.startswith(_today):
                                continue
                            _c = float(_b.get("close", 0) or 0)
                            _v = float(_b.get("volume", 0) or 0)
                            _tv = float(_b.get("turnover", 0) or 0)
                            if _c > 0:
                                _rows.append((_c, _v, _tv))
                        if _rows:
                            _tv_sum = sum(r[2] for r in _rows)
                            _v_sum = sum(r[1] for r in _rows)
                            if _v_sum > 0 and _tv_sum > 0:
                                _vwap_v = _tv_sum / _v_sum
                                if _vwap_v > 0:
                                    _vwap_dev = round((current_price - _vwap_v) / _vwap_v, 6)
                            if len(_rows) >= 6:
                                _c5 = _rows[-1][0]
                                _c0 = _rows[-6][0]
                                if _c0 > 0:
                                    _slope_5m = round((_c5 - _c0) / _c0 * 100.0, 4)
                except Exception:
                    _slope_5m, _vwap_dev = None, None  # 寄生特征缺失不阻断影子行
                _srow = {
                    "ts": _shadow_now_et().isoformat(), "symbol": _symbol, "direction": direction,
                    "entry_price": entry_price, "shadow_exit_price": current_price,
                    "held_minutes": round(held_minutes, 1), "mfe_pct": round(peak_profit_pct, 4),
                    "cur_pct": round(profit_pct, 4), "atr_pct": round(_atr_pct_live, 2),
                    "rule": "high_atr_stall_60m_mfe0.3",
                    "checkpoint": _hit, "slope_5m": _slope_5m, "vwap_dev": _vwap_dev,
                }
                with open(_SP("data/high_atr_stall_shadow.jsonl"), "a") as _sf:
                    _sf.write(_sj.dumps(_srow, ensure_ascii=False) + "\n")
                logger.info(
                    f"[影子·高ATR停滞] {_symbol} {direction} atr={_atr_pct_live:.1f}% "
                    f"checkpoint={_hit}min held={held_minutes:.0f}min mfe={peak_profit_pct:.2f}% "
                    f"cur={profit_pct:.2f}% 反事实退出点=${current_price} (record-only)"
                )
    except Exception:
        pass

    # 影子·死单早期识别 (2026-08-11, DEB-20260811-dead-order-early-detection, record-only):
    #   回放实锤: 入场后 30min MFE<0.2% 双窗口死单率 69%/64% (W1 n=35 / W2 n=14,
    #   命中组实际净 PnL 全负 -1218/-366, 误杀代价近零)。与已毕业 DEAD_SIGNAL_EXIT
    #   (60min, MFE<0.2%) 同源不同参数 — 本影子验证"提前到 30min"的独立价值:
    #   逐笔配对 30min 命中 vs 60min 命中的差集结局。15min 为辅助观测点 (回放
    #   MFE<0.15 死单率 61-62%)。零行为变更; 回滚: early_dead_signal_shadow_enabled=false。
    #   裁决预注册 (攒样≥30 命中): ①双窗口死单率均≥55% ②命中组实际净PnL<0
    #   ③差集(30min命中但60min未命中)结局为负。scripts/early_dead_signal_report.py 结算。
    try:
        if bool(_pb_cfg.get("early_dead_signal_shadow_enabled", True)):
            _ed_pts = meta.setdefault("_early_dead_shadow_pts", [])
            if not isinstance(_ed_pts, list):
                _ed_pts = []
            for _ed_pt in (15, 30):
                # 新鲜度守卫 (2026-08-11): 只在检查点窗口 [pt, pt+5min] 内穿越才记录。
                #   MFE 是持仓期累计最大值 — 服务重启后对已远超检查点的存量仓
                #   (如隔夜仓 held=796min) 若仍触发, 会把"整个持仓期的 MFE"误标为
                #   30min 检查点, 系统性高估 MFE/漏计死单, 污染裁决数据。
                #   快循环 10s 节奏下正常穿越落在 pt+~0.02min; +5min 容差覆盖轮询间隙,
                #   同时拒绝重启后的穿越 (held≫pt+5)。错过窗口即不记录该检查点 (不伪造)。
                if held_minutes >= _ed_pt and held_minutes <= _ed_pt + 5 and _ed_pt not in _ed_pts:
                    _ed_pts.append(_ed_pt)
                    import json as _edj
                    from pathlib import Path as _EDP
                    from app.utils.timezone import now_et as _ed_now_et
                    _ed_row = {
                        "ts": _ed_now_et().isoformat(),
                        "symbol": _symbol, "direction": direction,
                        "entry_time": entry_time_str, "entry_price": entry_price,
                        "checkpoint": _ed_pt,
                        "held_minutes": round(held_minutes, 1),
                        "mfe_pct": round(peak_profit_pct, 4),
                        "cur_pct": round(profit_pct, 4),
                        "rule": "early_dead_signal_shadow",
                        "settled": False,
                    }
                    with open(_EDP("data/early_dead_signal_shadow.jsonl"), "a") as _edf:
                        _edf.write(_edj.dumps(_ed_row, ensure_ascii=False) + "\n")
                    logger.info(
                        f"[影子·死单早期] {_symbol} {direction} checkpoint={_ed_pt}min "
                        f"mfe={peak_profit_pct:.2f}% cur={profit_pct:.2f}% (record-only)"
                    )
    except Exception:
        pass  # 影子埋点不阻断退出判断

    # 规则3.3 (2026-08-08, exit_shadow dead_signal_60m_mfe0.2 毕业): 死信号退出 —
    #   shadow 判分 PASS (全历史 171 笔: 触发组 mean -1.12% vs 未触发组 +0.37%,
    #   分离度 1.49pp) → 60min 时峰值浮盈从未达 0.2% = 方向从未兑现 → 立即离场.
    #   与规则3.4 区别: 不要求当前水下 (0%~-0.2% 也走), 更早承认信号失效.
    #   MFE_STALL(150min) 保留兜底. 回滚: dead_signal_exit_enabled 改 false.
    if not exit_reason and bool(_pb_cfg.get("dead_signal_exit_enabled", False)):
        _ds_minutes = float(_pb_cfg.get("dead_signal_exit_minutes", 60))
        _ds_mfe_pct = float(_pb_cfg.get("dead_signal_exit_mfe_pct", 0.2))
        if held_minutes >= _ds_minutes and peak_profit_pct < _ds_mfe_pct:
            exit_reason = "DEAD_SIGNAL_EXIT"
            exit_price = current_price
            meta["dead_signal_exit"] = {
                "check_minutes": round(held_minutes, 1),
                "mfe_at_trigger": round(peak_profit_pct, 4),
                "cur_at_trigger": round(profit_pct, 4),
                "threshold": _ds_mfe_pct,
            }
            logger.info(
                f"[规则3.3-死信号] {_symbol} {direction} held={held_minutes:.0f}min "
                f"peak={peak_profit_pct:.2f}%<{_ds_mfe_pct}% → 离场"
            )

    # 规则3.4 (2026-07-24, DEB-20260723 crash-day + DEB-20260724 rootcause): MFE停滞退出 —
    #   持仓≥stall_minutes 且 峰值浮盈从未达标 且 当前水下 → 立即离场, 不等 TIME_EXIT 磨满时间窗.
    #   数据依据 (PLAYBOOK 全历史): MFE<0.5% 且持有≥2.5h 共47笔仅3胜(+$18.7),
    #   TIME_EXIT 子集 0/33 -$1666; 赢家 MFE≥0.5% 平均 2.1h 内达成 → 2.5h 检查点几乎碰不到
    #   未来赢家 (历史误杀上限 $6.7: ORCL+1.0/COF+5.7).
    #   三重条件防误杀: 时间 × 峰值(从未≥0.5%) × 现状(水下); 盈利/曾浮盈达标的仓不受影响
    #   (零质量损失约束: EOD/TRAIL_LOCK 盈利路径完整保留). 回滚: mfe_stall_exit_enabled 改 false.
    if not exit_reason and bool(_pb_cfg.get("mfe_stall_exit_enabled", False)):
        _stall_minutes = float(_pb_cfg.get("mfe_stall_exit_minutes", 150))
        _stall_mfe_pct = float(_pb_cfg.get("mfe_stall_exit_mfe_pct", 0.5))
        if held_minutes >= _stall_minutes and peak_profit_pct < _stall_mfe_pct and profit_pct < 0:
            # 2026-08-04 (21笔反事实, 用户裁"同意 先调整再观察"): 检查点恒落午餐低谷,
            #   浅亏单市价卖系统性劣于持收 (+$158/21笔) — 浅亏转持有, 深亏维持立即退。
            _shallow_hold = bool(_pb_cfg.get("mfe_stall_exit_shallow_hold_enabled", False))
            _shallow_pct = float(_pb_cfg.get("mfe_stall_exit_shallow_pct", 1.0))
            if _shallow_hold and profit_pct > -_shallow_pct:
                if not meta.get("_stall_shallow_held"):
                    meta["_stall_shallow_held"] = True
                    logger.info(
                        f"[规则3.4-浅亏持有] {_symbol} {direction} held={held_minutes:.0f}min "
                        f"peak={peak_profit_pct:.2f}% cur={profit_pct:.2f}%>-{_shallow_pct}% "
                        f"— 不市价卖, 转 SL/PRE_CLOSE 收尾"
                    )
            else:
                exit_reason = "MFE_STALL_EXIT"
                exit_price = current_price
                # 2026-07-25 (首夜验证遗留): 触发现场字段入 meta — 零附近边界单 (如 MRK
                #   cur=-0.03% 触发、成交后落库 +$0.12) 事后归因不再翻日志。
                #   调用方退出落盘处从内存 meta 搬运 (主路径 _meta_out 用的是开环旧行).
                meta["stall_exit"] = {
                    "stall_minutes": round(held_minutes, 1),
                    "mfe_at_trigger": round(peak_profit_pct, 4),
                    "cur_at_trigger": round(profit_pct, 4),
                    "threshold": _stall_mfe_pct,
                }
                logger.info(
                    f"[规则3.4-MFE停滞] {_symbol} {direction} held={held_minutes:.0f}min "
                    f"peak={peak_profit_pct:.2f}%<{_stall_mfe_pct}% cur={profit_pct:.2f}% → 提前离场"
                )

    # 规则3: 时间退出 — 7/7 fix: 恢复此规则, 由 factor_exit_rules.time_exit_hours 驱动
    # 原因: 6/30 DEB 禁用时把 time_exit 交给 exit_policy.yaml, 但 7/3 fix 后 ExitMonitor 不再管
    #        PLAYBOOK 仓位。PLAYBOOK 仓位的 time_exit 必须由 Pipeline 自管。
    # 安全保障: time_exit_only_if_loss=true → 只砍亏损仓, 盈利仓不受影响
    #           factor 差异化: volume_expansion 0.5h / default 2h / macd_golden_cross 6h
    _time_exit_enabled = True
    _time_exit_hours = float(_exit_cfg.get("time_exit_hours", 2.0))
    _time_exit_only_if_loss = bool(_exit_cfg.get("time_exit_only_if_loss", True))
    if _time_exit_enabled and not exit_reason and held_minutes > (_time_exit_hours * 60):
        if not _time_exit_only_if_loss or profit_pct < 0:
            exit_reason = "TIME_EXIT"
            exit_price = current_price

    # 规则5: MAX_HOLD — long horizon 最大持有期检查 (short horizon 默认不持夜, _skip_allowed 控制)
    if not exit_reason and _skip_allowed and held_minutes > 0:
        if _horizon_max_hold_hit(entry_time_str, _max_hold_days):
            exit_reason = "MAX_HOLD"
            exit_price = current_price

    return exit_reason, exit_price, peak_price, breakeven_triggered


def _load_horizon_config(horizon: str) -> Dict[str, Any]:
    """读取指定 horizon 的配置参数"""
    try:
        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()
        return pb_cfg.get_horizon_params(horizon)
    except Exception as e:
        logger.debug(f"[playbook] 读取 horizon 配置失败: {e}, 使用默认")
        return {}


# 2026-07-11 (audit 结论): 因子黑名单(单因子 + 配对级) 暂停用.
#   审计证明其 99.5% 证据来自旧 config v1(v2 下这些组合几乎不交易/打平,
#   news_positive 单因子 v2 反而 +$70/35笔). 基于旧策略数据给新策略(v2)硬编码黑名单 = 打影子.
#   待 v2 积累足量真实样本后重新分层统计, 再决定是否启用. 重新启用: 置 True.
_FACTOR_BLACKLIST_ENABLED = False


# 2026-07-11: 配对级负 edge 闸门缓存 (从 playbook_factor_stats 读实证毒性配对)
_NEG_PAIR_CACHE: Dict[str, Any] = {"ts": 0.0, "pairs": None}


def _get_negative_factor_pairs(min_trades: int = 8, ttl_sec: int = 1800) -> set:
    """从 playbook_factor_stats 加载实证负 edge 的因子配对.

    条件: analysis_level='pair' 且 verdict='negative' 且 total_trades>=min_trades 且 total_pnl<0.
    返回: set[frozenset({factor_a, factor_b})].
    TTL 缓存 30min — 统计表由 deep_analysis 定期刷新, 无需每信号查库.
    命名与 pipeline 内 _conditions 同一套 condition_taxonomy, 直接匹配.
    """
    _now = time.time()
    if _NEG_PAIR_CACHE["pairs"] is not None and (_now - _NEG_PAIR_CACHE["ts"]) < ttl_sec:
        return _NEG_PAIR_CACHE["pairs"]
    pairs = set()
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute(
            "SELECT factor_key FROM playbook_factor_stats "
            "WHERE analysis_level='pair' AND verdict='negative' "
            "AND total_trades>=? AND total_pnl<0",
            (min_trades,),
        ).fetchall()
        conn.close()
        for (fk,) in rows:
            if fk and "|" in fk:
                parts = [p for p in fk.split("|") if p]
                if len(parts) == 2:
                    pairs.add(frozenset(parts))
    except Exception as e:
        logger.debug(f"[playbook] 负配对闸门加载失败 (忽略): {e}")
    _NEG_PAIR_CACHE["pairs"] = pairs
    _NEG_PAIR_CACHE["ts"] = _now
    return pairs


def _horizon_max_hold_hit(entry_time_str: str, max_hold_days: int) -> bool:
    """判断 long horizon 仓位是否已到 max_hold_days 交易日."""
    if not entry_time_str or max_hold_days <= 0:
        return False
    try:
        from datetime import datetime
        from app.utils.holiday_calendar import nth_trading_day_after
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        deadline = nth_trading_day_after("US", _entry_date, max_hold_days)
        if deadline is None:
            return False
        return datetime.now(_et).date() >= deadline
    except Exception as e:
        logger.debug(f"[playbook·horizon] MAX_HOLD 日期计算异常 (不强平): {e}")
        return False


# ── Phase 1 (2026-06-24): futu_simulate 真实仓位退出监控 ──────────────

# R2 (2026-07-19, DEB-round2 Ω.3-4): 取价失败连续轮次追踪 (进程级跨轮记忆).
#   monitor 无状态设计, 但"连续 N 轮取价失败告警"需跨轮记忆 → 模块级 dict.
#   symbol→连续失败轮数; _alerted 避免每轮重复推送 (取价恢复后重置).
_quote_fail_streak = {}
_quote_fail_alerted = set()

# 2026-07-19 (DEB-round3): 批量止损熔断 shadow 告警去重 (进程级, 当日去重).
#   与 _quote_fail_alerted 同范式: monitor 无状态, 跨轮记信用模块级 set.
_sl_burst_alerted = set()

# 2026-07-20 (DEB-20260720 修复3b): 平仓失败/仓位卡住告警去重 (进程级, symbol+day+reason).
#   补上"为什么平仓失败"这一半黑暗 — 平仓失败=仓位卡住无止损保护, 切实盘后最致命.
_close_fail_alerted = set()


def _alert_close_failure(symbol: str, target_env: str, reason: str, detail: str) -> None:
    """平仓失败结构化告警 (WARN 日志 + 飞书 HIGH). record-only, 不改平仓行为, 永不抛异常.

    2026-07-20 修复3b: 平仓失败意味着仓位卡住、无止损保护, 必须可见. monitor 每轮巡检
    命中同一失败会重复调用, 故按 symbol+当日+reason 去重, 避免刷屏 (进程重启后重记一次).
    """
    try:
        logger.error(
            f"[playbook平仓失败] {symbol} ({target_env}) reason={reason} detail={detail} — "
            f"仓位可能卡住/无止损保护, 需人工核查"
        )
        _key = f"{symbol}:{date.today().isoformat()}:{reason}"
        if _key in _close_fail_alerted:
            return
        _close_fail_alerted.add(_key)
        try:
            from app.push import get_push_hub
            from app.push.message import Level as _CFLevel
            get_push_hub().push_text(
                f"🚨 [Playbook平仓失败] {symbol} ({target_env}) {reason}: {detail}. "
                f"仓位可能卡住/无止损保护, 请人工核查 broker 持仓与 DB 一致性.",
                level=_CFLevel.HIGH,
            )
        except Exception as _cf_push_e:
            logger.warning(f"[playbook平仓失败] {symbol} 告警推送失败: {_cf_push_e}")
    except Exception:
        pass


def monitor_playbook_sim_exits(scheduler) -> int:
    """盘中监控 PLAYBOOK_SIM 真实模拟仓位的退出.

    与 monitor_playbook_shadow_exits 的区别: PLAYBOOK_SIM 是富途模拟账户的真实持仓,
    退出时必须调 DualBrokerAdapter.close_position(target_env="sim") 真实平仓,
    而非仅更新 trade_records 字段.

    退出规则 (与 shadow 对齐, 复用 exit_policy 阈值):
      [1] SL_INITIAL: 当前价触及原始 stop_loss (兜底)
      [2] TIME_EXIT: 持仓>2h 且 盈利<-0.5% → 退出 (2026-06-24放宽)
      [3] PRE_CLOSE_EXIT: 收盘前 8min → 全平

    Returns: 本轮平仓数量
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime
        from app.core.scheduler_triggers import ET as _et

        # 2026-07-22: 交易时段门 — 仅盘中执行退出监控.
        #   根因: 旧宿主调用点在 llm_strategy_loop 的 `phase == TRADING` 分支内 (仅盘中);
        #   独立快循环仅用 sleep_if_market_closed (只在 WEEKEND 休眠), 工作日
        #   PRE_MARKET/AFTER_HOURS/OVERNIGHT 仍会跑 → 用延迟/盘后失真价比 SL 兜底,
        #   产生非常规时段误动作+告警噪声. 此门收口, 非 TRADING 直接返回 (无副作用).
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        if get_market_phase() != MarketPhase.TRADING:
            return 0

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type,
                   decision_id
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            monitor_playbook_sim_exits._last_n = 0
            conn.close()
            return 0

        # 2026-07-31 降噪: 20s 循环每轮 INFO → 持仓数变化才 INFO, 不变 DEBUG (~180条/盘中h → ~0)
        if len(rows) != getattr(monitor_playbook_sim_exits, "_last_n", None):
            logger.info(f"[playbook dual] 退出监控: {len(rows)} 个未平仓 sim/live 仓位待检查")
            monitor_playbook_sim_exits._last_n = len(rows)
        else:
            logger.debug(f"[playbook dual] 退出监控: {len(rows)} 个未平仓 (不变)")

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook sim] 无 data_router, 跳过退出检查")
            conn.close()
            return 0

        # 2026-07-21: 批量取价 (独立快循环配套) — get_batch_quotes 一次调用:
        #   Futu Push缓存优先(近零成本)→快照→Finnhub 逐个回落. 替代逐仓串行 get_quote
        #   (原单轮 ~48s, 20s 快循环节奏下会打爆 Finnhub 限额). 缺失标的循环内回落单原取价.
        #   回滚: 删除本块, 并把循环内 quote 来源改回 data_router.get_quote(symbol) 即可.
        _batch_quotes: dict = {}
        try:
            _bq = data_router.get_batch_quotes([r["symbol"] for r in rows])
            if isinstance(_bq, dict):
                _batch_quotes = _bq
                logger.debug(f"[playbook dual] 批量取价: {len(_batch_quotes)}/{len(rows)} 命中")
        except Exception as _bq_e:
            logger.debug(f"[playbook dual] 批量取价失败, 本轮回落逐仓取价: {_bq_e}")

        # 市场时段 (ET) — 复用 scheduler_triggers.ET (pytz, 处理夏令时)
        now_et = datetime.now(_et)
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
            _cooldown_sl_pct = float(_get_pb_cfg_m1().get("cooldown_sl_pct", 4.0))
        except Exception:
            _cooldown_sl_pct = 4.0
        _in_open_cooldown = minutes_from_open is not None and minutes_from_open < 15

        exited = 0
        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            if entry_price <= 0:
                continue

            meta = {}
            try:
                meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                pass
            stop_loss = float(meta.get("stop_loss", 0))
            inst_id = meta.get("playbook_instruction_id")
            qty = r["quantity"] or 0

            # 当前价 (2026-07-21: 批量快照优先, 缺失/无价的标的回落单原取价)
            current_price = None
            try:
                quote = _batch_quotes.get(symbol)
                if not (isinstance(quote, dict) and (quote.get("last") or quote.get("price"))):
                    quote = data_router.get_quote(symbol)
                if quote and isinstance(quote, dict):
                    current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
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
            # 2026-07-03 fix: entry_time 带 tz 后缀 (如 -04:00) 是 aware, datetime.now() 是 naive,
            # 两者相减抛 TypeError → held_minutes=0 → holding_hours=0.0. 改用 tz-aware now.
            held_minutes = 0
            try:
                _entry_dt = datetime.fromisoformat(r["entry_time"])
                _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
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
                    # 真实平仓 (broker)
                    _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
                    try:
                        from app.execution.dual_broker import get_dual_broker
                        dual = get_dual_broker()
                        close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
                    except Exception as _e:
                        logger.error(f"[playbook dual] 止损兜底平仓异常 {symbol} ({_row_target}): {_e}")
                        _alert_close_failure(symbol, _row_target, "SL_BROKER_EXCEPTION", str(_e))
                        continue
                    if not close_res.get("success"):
                        _sl_err = close_res.get("error", "")
                        if "broker_no_position" in _sl_err or "持仓不足" in _sl_err or "insufficient" in _sl_err.lower():
                            # 2026-06-30 fix: broker 无此持仓 → DB-only 平仓
                            logger.warning(f"[playbook dual] {symbol} 止损兜底: broker持仓不足, DB-only平仓")
                            _db_exit = current_price
                            _db_pnl = (_db_exit - entry_price) * qty if direction == "LONG" else (entry_price - _db_exit) * qty
                            _db_pct = (_db_exit - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _db_exit) / entry_price * 100
                            _close_trade_record(conn, r["id"], _db_exit, "BROKER_GHOST_CLOSE",
                                                _db_pnl, _db_pct / 100, round(held_minutes / 60, 1), inst_id=inst_id, symbol=symbol)
                            exited += 1
                            logger.info(f"[playbook sim] 🔧 {symbol} SL DB-only平仓: exit=${_db_exit:.2f}")
                            continue
                        logger.warning(f"[playbook dual] 止损兜底平仓失败 {symbol}: {_sl_err}")
                        _alert_close_failure(symbol, _row_target, "SL_CLOSE_FAILED", _sl_err or "unknown")
                        continue
                    _exit_price = close_res.get("filled_price", 0) or current_price
                    _sl_src = close_res.get("price_source", "unknown")
                    _sl_broker_after = close_res.get("broker_qty_after", -1)
                    _sl_order_filled = close_res.get("order_filled", False)
                    _sl_close_futu_id = close_res.get("futu_order_id", "")
                    # ── 根因修复 (2026-07-07): 用 order_filled 判定, 不用 broker_qty_after ──
                    # SL 兜底: 订单未成交 → 不写 trade_records
                    if not _sl_order_filled:
                        logger.warning(
                            f"[playbook dual] {symbol} SL 兜底平仓未成交: "
                            f"order_filled={_sl_order_filled} (src={_sl_src}), 不写 trade_records"
                        )
                        _alert_close_failure(symbol, _row_target, "SL_ORDER_NOT_FILLED", f"src={_sl_src}")
                        continue
                    # fill=0 但 broker 已无仓 → 用当前报价记账 (标记来源)
                    if _exit_price <= 0:
                        logger.info(f"[playbook dual] {symbol} SL fill=0 但 broker 已无仓 (src={_sl_src}), 用报价记账")
                        _exit_price = current_price
                        _sl_src = "quote_fallback"
                    if _exit_price <= 0:
                        logger.warning(f"[playbook dual] {symbol} SL 无法获取退出报价, 跳过记账")
                        continue
                    _pnl = (_exit_price - entry_price) * qty if direction == "LONG" else (entry_price - _exit_price) * qty
                    _pnl_pct = (_exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _exit_price) / entry_price * 100
                    _hh = round(held_minutes / 60, 1)
                    # Fix 3: 写入 exit_price_source
                    try:
                        _sl_meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                    except Exception:
                        _sl_meta = {}
                    _sl_meta["exit_price_source"] = _sl_src
                    _sl_meta["broker_qty_before"] = close_res.get("broker_qty_before")
                    _sl_meta["broker_qty_after"] = _sl_broker_after
                    # C1 补测 (2026-07-24, DEB-20260724 §八): 出场滑点 — 决策参考价(current_price) vs 成交价.
                    #   入场侧已有 execution.slippage_bps (中位 4.6bps), 出场侧此前 0 记录 → 往返成本只测了一半.
                    #   正值=不利 (LONG 卖在参考价下方 / SHORT 回补在参考价上方). record-only.
                    if current_price and current_price > 0 and _exit_price > 0:
                        _sl_xraw = (current_price - _exit_price) / current_price * 10000.0
                        _sl_meta["exit_slippage_bps"] = round(_sl_xraw if direction == "LONG" else -_sl_xraw, 2)
                    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                                 (_json.dumps(_sl_meta), r["id"]))
                    conn.commit()
                    # 2026-07-14: 兜底用原始 initial stop_loss(L3361 判定) → 归为 SL_INITIAL.
                    _close_trade_record(conn, r["id"], _exit_price, "SL_INITIAL",
                                        _pnl, _pnl_pct / 100, _hh, inst_id=inst_id, symbol=symbol,
                                        close_futu_order_id=_sl_close_futu_id)
                    exited += 1
                    logger.info(f"[playbook sim] ✅ {symbol} SL_INITIAL 兜底: exit=${_exit_price:.2f} src={_sl_src} pnl=${_pnl:.2f}")
                    continue

            if abs(current_price - entry_price) / entry_price > 0.15:
                continue  # 价格异常跳过 (2026-07-02: 50%→15%, 50%让巨亏/巨盈仓位失保护)

            # cohort B skip 判定 (sim 特有: LIVE 需显式开 overnight)
            _cohort = meta.get("cohort", "A")
            _src = r["source_type"]
            from app.storage.trade_store import cohort_b_skip_allowed as _skip_ok
            _skip_allowed = (_cohort == "B" and _skip_ok(_src))  # LIVE 需显式开

            # peak_price 从 metadata 恢复 (sim 此前未做 peak tracking, 现与 shadow 对齐)
            peak_price = float(meta.get("peak_price", 0))
            # 注入共用函数所需字段
            meta["_skip_allowed"] = _skip_allowed
            meta["_log_tag"] = "playbook sim"
            meta["_symbol"] = symbol
            meta["_qty"] = qty  # 2026-07-30 D4: 保本线费用感知需股数换算每股往返成本

            # ── 共用退出规则判断 (与 shadow 完全对齐) ──
            exit_reason, _exit_price_hint, peak_price, _be = _check_playbook_exit_rules(
                direction, entry_price, current_price, stop_loss,
                peak_price, meta, held_minutes, minutes_to_close,
                minutes_from_open, r["entry_time"],
            )

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
            if peak_price != float(meta.get("peak_price", 0)) or _cur_mfe > 0 or _cur_mae > 0 or _trough != _trough_old or _inval_shadow_new or _exit_shadow_new or _bar_track_new:
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

            # 真实平仓 (broker) — target_env 按 source_type (PLAYBOOK_LIVE→real, 否则 sim)
            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            try:
                from app.execution.dual_broker import get_dual_broker
                dual = get_dual_broker()
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.error(f"[playbook dual] 平仓异常 {symbol} ({_row_target}): {_e}")
                _alert_close_failure(symbol, _row_target, "BROKER_EXCEPTION", str(_e))
                continue

            if not close_res.get("success"):
                _err = close_res.get("error", "")
                # 2026-06-30 fix: "持仓不足"/"broker_no_position" = broker 无此持仓 (永久错误, 不重试)
                # 查询 broker 实际持仓, 若为 0 则 DB-only 平仓 (用当前报价)
                # 2026-07-06 fix: 补 "broker_no_position" 匹配 (SL/EOD 路径已有, 正常退出路径遗漏)
                if "broker_no_position" in _err or "持仓不足" in _err or "insufficient" in _err.lower():
                    logger.warning(
                        f"[playbook dual] {symbol} ({_row_target}) broker 持仓不足, "
                        f"检查实际持仓并 DB-only 平仓"
                    )
                    _broker_qty = 0.0
                    try:
                        broker_positions = dual.get_positions(target_env=_row_target)
                        for _bp in broker_positions:
                            _bp_code = _bp.get("code", _bp.get("symbol", ""))
                            if _bp_code == symbol:
                                _broker_qty = float(_bp.get("qty", 0) or 0)
                                break
                    except Exception as _be:
                        logger.warning(f"[playbook dual] 查询 broker 持仓异常 {symbol}: {_be}")
                    if _broker_qty <= 0:
                        # Broker 无此持仓 → DB-only 平仓 (ghost position)
                        # E2 (2026-07-19): 无报价 ghost 用入场价兜底 → PnL=0, 标 void 不计统计 (告警保证可见).
                        _db_has_quote = bool(current_price and current_price > 0)
                        _db_exit_price = current_price if _db_has_quote else entry_price
                        if not _db_has_quote:
                            logger.warning(
                                f"[playbook sim] E2 {symbol} ghost 无报价, 用入场价${entry_price:.2f}兜底 "
                                f"(PnL=0, 标 void 不计统计)"
                            )
                        _db_pnl = (_db_exit_price - entry_price) * qty if direction == "LONG" else (entry_price - _db_exit_price) * qty
                        _db_pnl_pct = (_db_exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _db_exit_price) / entry_price * 100
                        _db_hh = round(held_minutes / 60, 1)
                        _close_trade_record(conn, r["id"], _db_exit_price, "BROKER_GHOST_CLOSE",
                                            _db_pnl, _db_pnl_pct / 100, _db_hh, inst_id=inst_id, symbol=symbol)
                        exited += 1
                        logger.info(
                            f"[playbook sim] 🔧 {symbol} DB-only 平仓 (broker无持仓): "
                            f"exit=${_db_exit_price:.2f} pnl=${_db_pnl:.2f} reason={exit_reason}"
                        )
                        continue
                    elif _broker_qty < qty:
                        # Broker 有部分持仓 → 用实际数量平仓
                        logger.info(f"[playbook dual] {symbol} broker实际qty={_broker_qty} < DB qty={qty}, 用broker数量平仓")
                        try:
                            close_res2 = dual.close_position(symbol, _broker_qty, direction, target_env=_row_target)
                            if close_res2.get("success"):
                                _fill = close_res2.get("filled_price", 0) or current_price
                                _pnl2 = (_fill - entry_price) * _broker_qty if direction == "LONG" else (entry_price - _fill) * _broker_qty
                                _pnl_pct2 = (_fill - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _fill) / entry_price * 100
                                _hh2 = round(held_minutes / 60, 1)
                                _close_trade_record(conn, r["id"], _fill, exit_reason,
                                                    _pnl2, _pnl_pct2 / 100, _hh2, inst_id=inst_id, symbol=symbol)
                                exited += 1
                                logger.info(f"[playbook sim] ✅ {symbol} 部分平仓: qty={_broker_qty} exit=${_fill:.2f}")
                                continue
                        except Exception as _e2:
                            logger.error(f"[playbook dual] 部分平仓异常 {symbol}: {_e2}")
                logger.warning(f"[playbook dual] 平仓失败 {symbol} ({_row_target}): {_err}")
                _alert_close_failure(symbol, _row_target, "CLOSE_FAILED", _err or "unknown")
                continue

            exit_price = close_res.get("filled_price", 0) or current_price
            _price_source = close_res.get("price_source", "unknown")
            _broker_qty_after = close_res.get("broker_qty_after", -1)

            # ── 根因修复 (2026-07-07): 用 order_filled 判定成交, 不用 broker_qty_after ──
            # 旧逻辑: if _broker_qty_after > 0.5 → 平仓未成交 (依赖持仓列表, SIM 有延迟)
            # 新逻辑: if not _order_filled → 平仓未成交 (依赖订单状态, 无延迟问题)
            _order_filled = close_res.get("order_filled", False)
            _close_futu_id = close_res.get("futu_order_id", "")

            # 订单未成交 → 不写 trade_records (与 SL 路径/EOD 路径对齐)
            if not _order_filled:
                logger.warning(
                    f"[playbook dual] {symbol} {direction} {exit_reason} 平仓未成交: "
                    f"order_filled={_order_filled} (src={_price_source}), 不写 trade_records"
                )
                _alert_close_failure(symbol, _row_target, "ORDER_NOT_FILLED",
                                     f"{exit_reason} src={_price_source}")
                continue

            # 订单已成交 → 用 filled_price 记账 (优先), fill=0 时用当前报价兜底
            if exit_price <= 0:
                logger.info(f"[playbook dual] {symbol} order_filled 但 fill=0 (src={_price_source}), 用报价记账")
                try:
                    q = data_router.get_quote(symbol)
                    exit_price = float(q.get("last", 0) or q.get("price", 0) or 0) if q else 0
                except Exception:
                    pass
                _price_source = "quote_fallback"
            if exit_price <= 0:
                logger.warning(f"[playbook dual] {symbol} 无法获取退出报价, 跳过记账")
                continue

            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = round(held_minutes / 60, 1)

            # ── 隔夜持仓 shadow (2026-08-10, record-only, 用户批准) ──
            #   benign 退出 (SL_BREAKEVEN/SL_TRAIL_LOCK/PRE_CLOSE_EXIT) 落决策点,
            #   次日 settle_overnight_shadow.py 结算"持仓过夜 vs 已退出"反事实.
            #   历史反事实 (7/15-8/7, 73笔): 隔夜跳空胜率60% +0.36%, 但 COIN 单笔占78%,
            #   剔除后≈打平, regime 未剥离 → 跨 regime 攒样本再裁. 回滚: 配置键置 false.
            try:
                from app.playbook.overnight_hold_shadow import emit as _overnight_emit
                # 2026-08-11 修复: r 是 sqlite3.Row — 无 .get() 方法, 原 r.get(...)
                #   抛 AttributeError 被下方 except 静默吞掉, emit 从未执行过 (8/10 首笔
                #   benign 退出 TSM 决策点丢失). 改用索引访问 (SELECT 已补 decision_id 列).
                #   异常改为 logger.warning 可见, 影子埋点故障不应无声.
                try:
                    _ov_decision_id = r["decision_id"]
                except Exception:
                    _ov_decision_id = None
                _overnight_emit(
                    symbol, direction, exit_reason, exit_price, entry_price,
                    pnl, notional=round(exit_price * qty, 2),
                    decision_id=_ov_decision_id,
                )
            except Exception as _ov_e:
                logger.warning(f"[隔夜shadow] emit 异常 (不阻断退出记账) {symbol}: {_ov_e}")

            # 写入 exit_price_source 到 metadata
            try:
                _meta_out = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                _meta_out = {}
            _meta_out["exit_price_source"] = _price_source
            _meta_out["broker_qty_before"] = close_res.get("broker_qty_before")
            _meta_out["broker_qty_after"] = _broker_qty_after
            # 2026-07-25: stall 触发现场从内存 meta 搬运 (_meta_out 基于开环旧行重建,
            #   规则函数写进 meta 的字段不搬会丢)
            if meta.get("stall_exit"):
                _meta_out["stall_exit"] = meta["stall_exit"]
            # C1 补测 (2026-07-24, DEB-20260724 §八): 出场滑点 (主退出路径: TIME_EXIT/SL/TRAIL/
            #   PRE_CLOSE/MFE_STALL 全覆盖). 口径同 SL 路径: 决策参考价 vs 成交价, 正=不利.
            if current_price and current_price > 0 and exit_price > 0:
                _x_raw = (current_price - exit_price) / current_price * 10000.0
                _meta_out["exit_slippage_bps"] = round(_x_raw if direction == "LONG" else -_x_raw, 2)
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                         (_json.dumps(_meta_out), r["id"]))
            conn.commit()

            _close_trade_record(conn, r["id"], exit_price, exit_reason,
                                pnl, pnl_pct / 100, holding_hours, inst_id=inst_id, symbol=symbol,
                                close_futu_order_id=_close_futu_id)
            exited += 1
            logger.info(
                f"[playbook sim] 平仓: {symbol} {direction} reason={exit_reason} "
                f"entry={entry_price} exit={exit_price} pnl=${pnl:.2f}"
            )

        conn.close()
        return exited

    except Exception as e:
        # 2026-07-16: debug→exception — 退出监控循环顶层崩溃(如 pb_cfg NameError)
        # 在 debug 级别下生产 INFO 日志完全隐身, 曾导致 P0 潜伏. 必须带 traceback 可见.
        logger.exception(f"[playbook sim] exit monitor 异常 (已隔离): {e}")
        return 0


def _force_close_orphaned_sim_positions(scheduler) -> int:
    """EOD 兜底: 强制平仓所有未平仓的 PLAYBOOK_SIM/ORB_ENGINE 真实仓位 (via broker).

    与 _force_close_orphaned_shadow_positions 的区别: PLAYBOOK_SIM 是真实模拟持仓,
    必须调 broker.close_position 真实平仓, 不能只更新 DB.

    2026-06-24: 也覆盖 ORB_ENGINE 仓位 — ORB daemon 崩溃时, 其自管 EOD (_force_close_all)
    不执行, 仓位会孤儿化. 此函数作为 scheduler EOD 兜底, 用 DualBroker.close_position 平仓.
    ORB 在 sim 账户交易, target_env="sim".
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'ORB_ENGINE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            conn.close()
            return 0

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook dual] EOD 兜底跳过: 无 data_router")
            conn.close()
            return 0

        from app.execution.dual_broker import get_dual_broker
        dual = get_dual_broker()
        closed = 0
        now_iso = datetime.now().isoformat()

        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            qty = r["quantity"] or 0
            if entry_price <= 0 or qty <= 0:
                continue

            # RFC-061 v2: cohort B 盈利仓跳过 EOD broker 兜底 (持过夜); 5 交易日仍平 (MAX_HOLD_T5).
            # 仅 PLAYBOOK_SIM/LIVE 且 cohort_b_skip_allowed (LIVE 需显式开); ORB 强制平 (日内语义).
            try:
                _meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                _meta = {}
            _cohort = _meta.get("cohort", "A")
            from app.storage.trade_store import cohort_b_skip_allowed as _skip_ok
            _skip_allowed = (_cohort == "B" and _skip_ok(r["source_type"]))
            # 极简版B (2026-07-17): 隔夜总开关关闭 → EOD 全平, cohort B 盈利仓不跳过.
            from app.playbook.config import get_playbook_config as _get_pb_cfg
            if not _get_pb_cfg().get("overnight_hold_enabled", False):
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

            # 2026-08-06 (用户裁决"再入场亏损可隔夜"): 再入场水下仓首夜豁免 EOD 兜底.
            #   报价缺失 → 不豁免 (保守平仓); 次日 entry_date≠当日 → 自动失效正常收.
            try:
                _rq = data_router.get_quote(symbol)
                _rp = float((_rq or {}).get("last", 0) or (_rq or {}).get("price", 0) or 0) if _rq else 0.0
                if _rp > 0:
                    _rpnl = ((_rp - entry_price) / entry_price * 100) if direction == "LONG" \
                        else ((entry_price - _rp) / entry_price * 100)
                    if _reentry_overnight_exempt(symbol, r["entry_time"], _rpnl):
                        logger.info(
                            f"[playbook sim] EOD 兜底豁免 {symbol} 再入场水下仓 {_rpnl:.2f}% 持夜 (用户裁决)"
                        )
                        continue
            except Exception as _rex:
                logger.debug(f"[playbook sim] 再入场豁免判定异常 {symbol} (不豁免): {_rex}")

            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            # P1 fix (2026-06-25): 连续失败 3 次后放弃, 停止死循环日志刷屏
            _fail_count = _meta.get("eod_close_fail_count", 0)
            if _fail_count >= 3:
                logger.debug(f"[playbook dual] EOD 兜底放弃 {symbol} ({_row_target}) — 连续失败 {_fail_count} 次")
                continue
            try:
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.warning(f"[playbook dual] EOD 兜底平仓异常 {symbol} ({_row_target}): {_e}")
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                continue
            if not close_res.get("success"):
                _eod_err = close_res.get("error", "")
                # 2026-07-03: close_position 内置持仓检查, broker 无仓时返回 "broker_no_position"
                # 2026-06-30 fix: "持仓不足" = 永久错误, 查 broker 实际持仓决定 DB-only 平仓
                if "broker_no_position" in _eod_err or "持仓不足" in _eod_err or "insufficient" in _eod_err.lower():
                    _broker_qty = 0.0
                    try:
                        _bpos = dual.get_positions(target_env=_row_target)
                        for _bp in _bpos:
                            if _bp.get("code", _bp.get("symbol", "")) == symbol:
                                _broker_qty = float(_bp.get("qty", 0) or 0)
                                break
                    except Exception:
                        pass
                    if _broker_qty <= 0:
                        # Broker 无此持仓 → DB-only 平仓 (ghost)
                        _eod_price = 0.0
                        try:
                            _q = data_router.get_quote(symbol)
                            if _q and isinstance(_q, dict):
                                _eod_price = float(_q.get("last", 0) or _q.get("price", 0) or 0)
                        except Exception:
                            pass
                        if _eod_price <= 0:
                            # E2 (2026-07-19): 无报价 ghost 用入场价兜底 → PnL=0, 标 void 不计统计 (告警保证可见).
                            _eod_price = entry_price
                            logger.warning(
                                f"[playbook sim] E2 {symbol} EOD ghost 无报价, 用入场价${entry_price:.2f}兜底 "
                                f"(PnL=0, 标 void 不计统计)"
                            )
                        _eod_pnl = (_eod_price - entry_price) * qty if direction == "LONG" else (entry_price - _eod_price) * qty
                        _eod_pct = (_eod_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _eod_price) / entry_price * 100
                        _eod_hh = 0.0
                        try:
                            _entry_dt = datetime.fromisoformat(r["entry_time"])
                            _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
                            _eod_hh = (_now_ref - _entry_dt).total_seconds() / 3600
                        except Exception:
                            pass
                        _close_trade_record(conn, r["id"], _eod_price, "BROKER_GHOST_CLOSE",
                                            _eod_pnl, _eod_pct / 100, _eod_hh,
                                            inst_id=_meta.get("playbook_instruction_id"), symbol=symbol)
                        closed += 1
                        logger.info(f"[playbook sim] 🔧 EOD {symbol} DB-only平仓 (broker无持仓): exit=${_eod_price:.2f} pnl=${_eod_pnl:.2f}")
                        continue
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.warning(f"[playbook dual] EOD 兜底 {symbol} ({_row_target}) 无报价/平仓失败 (第{_fail_count}次): {_eod_err}")
                if _fail_count >= 3:
                    _alert_close_failure(symbol, _row_target, "EOD_CLOSE_FAILED",
                                         f"连续{_fail_count}次失败 {_eod_err or 'unknown'}")
                continue

            # 2026-07-03: close_position 已内置平仓前/后持仓验证 + price_source
            exit_price = close_res.get("filled_price", 0) or 0
            _price_source = close_res.get("price_source", "unknown")
            _broker_qty_after = close_res.get("broker_qty_after", -1)
            _eod_order_filled = close_res.get("order_filled", False)
            _eod_close_futu_id = close_res.get("futu_order_id", "")

            # ── 根因修复 (2026-07-07): 用 order_filled 判定, 不用 broker_qty_after ──
            # EOD: 订单未成交 → 不写 trade_records, 记录失败次数
            if not _eod_order_filled:
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                _meta["exit_price_source"] = _price_source
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.warning(
                    f"[playbook dual] EOD {symbol} 订单未成交 "
                    f"order_filled={_eod_order_filled} (第{_fail_count}次) src={_price_source}, 不写 trade_records"
                )
                if _fail_count >= 3:
                    _alert_close_failure(symbol, _row_target, "EOD_ORDER_NOT_FILLED",
                                         f"连续{_fail_count}次未成交 src={_price_source}")
                continue

            # 订单已成交 → 用 filled_price 记账 (优先), fill=0 时用当前报价兜底
            if exit_price <= 0:
                logger.info(f"[playbook dual] EOD {symbol} order_filled 但 fill=0 (src={_price_source}), 用报价记账")
                try:
                    q = data_router.get_quote(symbol)
                    exit_price = float(q.get("last", 0) or q.get("price", 0) or 0) if q else 0
                except Exception:
                    pass
                _price_source = "quote_fallback"
            if exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = 0.0
            try:
                _entry_dt = datetime.fromisoformat(r["entry_time"])
                _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
                holding_hours = (_now_ref - _entry_dt).total_seconds() / 3600
            except Exception:
                pass

            # Fix 3: 写入 exit_price_source 到 metadata
            _meta["exit_price_source"] = _price_source
            _meta["broker_qty_before"] = close_res.get("broker_qty_before")
            _meta["broker_qty_after"] = _broker_qty_after
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                         (_json.dumps(_meta), r["id"]))
            conn.commit()

            _close_trade_record(conn, r["id"], exit_price, _eod_reason,
                                pnl, pnl_pct / 100, holding_hours,
                                inst_id=_meta.get("playbook_instruction_id"), symbol=symbol,
                                close_futu_order_id=_eod_close_futu_id)
            closed += 1
            logger.info(f"[playbook sim] EOD 兜底平仓: {symbol} reason={_eod_reason} exit={exit_price} src={_price_source} pnl=${pnl:.2f}")

        conn.close()
        return closed

    except Exception as e:
        logger.warning(f"[playbook sim] EOD force_close 异常 (已隔离): {e}")
        return 0

