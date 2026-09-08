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


# ── [DEB-20260901 §六.4] 影子行文件级防重 (修复重启重复) ─────────────────
# 根因: 纯内存幂等集被服务重启清零 → 同一指令重复 emit (8/17 实证 3 指令各 2 行,
#   reconciliation_ok 恒 false)。改为文件级: 进程首次接触该文件时读入已有键,
#   之后内存为准 (文件只追加)。键 = (inst_id, type), type 缺省 "row"。
_SHADOW_SEEN: Dict[str, set] = {}


def _shadow_dedup_claim(path, inst_id, kind: str = "row"):
    """DEB-20260904 #175: 两阶段防重 — claim 只查不占.

    返回 None = 已存在 (调用方应跳过); 否则返回 (seen_set, key) 供写盘成功后
    _shadow_dedup_commit 提交. 旧实现 _shadow_dedup_ok 在返回前就 seen.add(_k)
    预占, 写盘失败时键残留 → 该信号永久被 dedup 拦下.
    异常时放行 (影子宁多勿漏) — 返回 (None, key) 哨兵, commit 时安全跳过.
    """
    try:
        key = str(path)
        seen = _SHADOW_SEEN.get(key)
        if seen is None:
            seen = set()
            _bad = 0
            try:
                if path.exists():
                    with open(path, encoding="utf-8") as _fsd:
                        for _ln in _fsd:
                            try:
                                _r = json.loads(_ln)
                                _t = _r.get("type", "row")
                                seen.add((_r.get("inst_id"), _t))
                                # DEB-20260903 议题一: chase_cap 违例按值去重 (kind 内嵌 reprice 值,
                                #   形如 "chase_cap_violation:464.41") — 文件侧补种值键, 否则重启后
                                #   文件里已有的同值违例被误判首次 (重复入行)。行 type 保持裸值,
                                #   值键仅存在于本防重层, 不落盘。
                                _rv = _r.get("repriced_entry")
                                if isinstance(_rv, (int, float)):
                                    seen.add((_r.get("inst_id"), f"{_t}:{round(float(_rv), 2)}"))
                            except Exception:
                                _bad += 1
                    if _bad:
                        logger.warning(
                            f"[shadow dedup] {path.name} 有 {_bad} 行解析失败已跳过 "
                            f"(防重可能不完整, DEB-20260901)")
            except Exception as _e:
                logger.warning(f"[shadow dedup] 读取 {path} 失败, 退化为纯内存防重: {_e}")
            _SHADOW_SEEN[key] = seen
        _k = (inst_id, kind)
        if _k in seen:
            return None
        return (seen, _k)
    except Exception:
        return (None, (inst_id, kind))


def _shadow_dedup_commit(claim) -> None:
    """DEB-20260904 #175: 写盘成功后提交 claim 的键 (与 _shadow_dedup_claim 配对)."""
    try:
        if not claim:
            return
        seen, _k = claim
        if seen is not None:
            seen.add(_k)
    except Exception:
        pass


def _shadow_dedup_ok(path, inst_id, kind: str = "row") -> bool:
    """[向后兼容] 返回 True = 首次, 允许写入; 已存在返回 False。异常时放行.

    DEB-20260904 #175: 新代码应改用 _shadow_dedup_claim/_shadow_dedup_commit
    两阶段 (写盘失败不留残留键). 本函数保留给旧调用点, 仍为"查即占"语义.
    """
    claim = _shadow_dedup_claim(path, inst_id, kind)
    if claim is None:
        return False
    _shadow_dedup_commit(claim)
    return True

# 2026-08-06 (B1 二次修复): 盘口 micro 拉取的"已告警"集 ((symbol, date) 防刷屏).
#   首修 (08-05) 用 get_push_orderbook 推送缓存 — 但 ADR-013 早已把 ORDER_BOOK 移出推送
#   订阅集 (subscription_manager 默认 QUOTE+K_1M), 缓存恒空 → 08-05 全天 233 行 micro=null.
#   改用 S2 实测可行的拉取路径 (scripts/archive/orderbook_s2_capture.py 同款;
#   S2 实验 08-18 已收官归档, 但其拉取路径是本函数的参考实现):
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

    走独立临时连接 (同 S2 采集脚本 orderbook_s2_capture.py, 已归档): 端口探测→建连→
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
        # Futu SDK 日志静默 — SSOT 统一入口 (2026-08-12 收敛 3 处 inline 拷贝)
        from app.data.futu_lv3_provider import silence_futu_sdk_logs as _silence
        try:
            _silence()
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

def _overnight_eod_exempt(symbol: str, source_type: str, entry_time_str: str,
                          profit_pct: float, is_reentry=None) -> bool:
    """EOD 隔夜豁免单点 bool 投影 (P1 三轨合一, 2026-09-06, 行为等价合并).

    本体在 app.playbook.overnight_decision (轨③ reentry 首夜 → W_WEEK 多夜,
    顺序/净口径与合并前逐字一致)。is_reentry 默认 None = 查库 (生产路径不变)。
    任何异常 → False (不豁免 — 保守方向)。
    """
    try:
        from app.playbook.overnight_decision import overnight_eod_exempt
        return bool(overnight_eod_exempt(symbol, source_type, entry_time_str, profit_pct,
                                         is_reentry=is_reentry))
    except Exception:
        return False


def _loser_overnight_exempt(source_type: str, entry_time_str: str, profit_pct: float) -> bool:
    """SIM水下仓多夜 EOD/TIME_EXIT 豁免 (2026-08-15 用户三问Q3+追加裁决, DEB§33/§34).

    P1 (2026-09-06) 起本体在 app.playbook.overnight_decision.w_week_loser_exempt
    (判定语义逐字未动, 含 8/20 水深上限与 8/21 回滚教训注释)。本转发器保留供
    DEAD_SIGNAL/TIME_EXIT/MAX_HOLD 三处盘中豁免点与契约测试引用; EOD 决策面
    已收单点 _overnight_eod_exempt / overnight_decision.overnight_eod_decision。
    任何异常 → False (不豁免)。
    """
    try:
        from app.playbook.overnight_decision import w_week_loser_exempt
        return bool(w_week_loser_exempt(source_type, entry_time_str, profit_pct))
    except Exception:
        return False


# 2026-08-07 (用户指示, sim A/B 双开): VWAP 门 wrong_side 拦截实验分流.
#   证据 (DEB-20260806-source-control 修尺子后): 被拦 n=55 均值 +0.254% (SHORT +0.649%),
#   门倾向"扼杀"。影子反事实已可测 (gated 臂轨迹在 entry_timing_shadow 持续轮询),
#   但"真实成交口径 (费用/滑点/真实退出) 下是否仍成立"只能 A/B 双开实测 (SIM 无真金/无 PDT).
#   分臂纯函数日内稳定: 同标的当日恒定同臂, 不随 300s 轮询漂移, 进程重启不变.
# ── 生产数据写入缝 (2026-09-03 复盘批, REVIEW-20260903 §四.8) ────────────────
#   取证 (全量套件挂 open/write_text 探针): 33 次写入落在生产 data/ 下 — 死单早期影子
#   被灌 2307 行 symbol=TEST (占该文件 90%, shadow_board 段10 分母虚增 44×)、审计留痕
#   363 行 100% 虚构、当日推荐 JSON 连续 8 天是合成 AAPL 180-185。根因: 埋点路径在
#   函数体内就地拼字符串, 测试无从隔离 (对照: penetration_shadow._PEN_PATH 有缝,
#   故其测试全量 monkeypatch 零污染)。
#   提为模块常量 = **值与解析语义逐字不变** (systemd WorkingDirectory=/opt/ftseek/FTSeek-US,
#   故相对/绝对两种既有写法各自保持原样), 仅把"写哪"变成可重定向的一个名字:
#   tests/conftest.py 自动把它们指向 tmp 沙箱, 并对未登记的 data/ 写入硬闸报错。
_VWAP_AB_LEDGER_PATH = Path(__file__).resolve().parents[2] / "data" / "vwap_gate_ab.jsonl"
# _EARLY_DEAD_SHADOW_PATH 已随 _check_playbook_exit_rules 迁入 app.playbook.exit_engine
#   (P2a, 2026-09-06; P7 缝登记同步迁至 tests/conftest.py → exit_engine 模块属性)。

_VWAP_AB_LOGGED: set = set()


def _vwap_ab_row_exists(date_str: str, symbol: str, direction: str) -> bool:
    """台账文件级去重 (2026-08-07 主动审计 P1-4 修复): _VWAP_AB_LOGGED 是进程内
    set, 进程重启(systemd 重启/cron 重跑)后清空 → 同 (symbol,date,direction) 重复
    append, 破坏"每 symbol-day-direction 一行"的台账唯一性承诺, A/B 统计重复计数。
    写前查文件历史行, 与进程 set 双保险。异常 → False (允许写, 审计降级优于丢行)."""
    _f = _VWAP_AB_LEDGER_PATH
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
                         current_price, inst_id, data_router=None) -> bool:
    """VWAP 门 A/B 分流判定: 仅 sim + 开关开 + bypass 臂 → True (放行).

    双臂首次判定即落实验台账 data/vwap_gate_ab.jsonl (每 symbol-day-direction 一行);
    任何异常 → False (维持拦截, 即现状, 保守方向)。

    2026-08-27 (A/B 污染修复): 台账行补记决策时刻 vwap_slope — 下游证伪门·
    VWAP斜率 (生产拦截) 与方向门 wrong_side 同现率高 (下跌晨 dev<0 ∧ slope<0),
    bypass 臂放行后常被斜率门二次拦截 → 双臂非同权, VWAP 门边际效应测不出
    (8/27 BWXT 实录: 放行 3ms 后被斜率门拦). 记录决策时刻斜率供分析层
    分层恢复同权对比; data_router 缺失/计算失败 → None (不阻断). 零行为改动.
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
            _slope = None
            try:
                if data_router is not None:
                    _slope = _calc_vwap_slope(data_router, symbol)
            except Exception:
                _slope = None
            _row = {
                "ts": _dtab.now(_now_et()).isoformat(timespec="seconds"),
                "date": _d, "symbol": symbol, "direction": _dir, "arm": arm,
                "price": current_price, "vwap": vwap_val,
                "vwap_dev": round(vwap_dev, 6) if vwap_dev is not None else None,
                "vwap_slope": round(_slope, 6) if isinstance(_slope, (int, float)) else None,
                "inst_id": inst_id, "rule": "vwap_gate_ab_50_50",
            }
            with open(_VWAP_AB_LEDGER_PATH, "a", encoding="utf-8") as _fab:
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
    """影子组合入场登记 (record-only). 任何异常静默.

    ⚠️ 去重语义 (消费方必读): 键是 (inst_id, date), **跨全部 blocked_by 原因**,
    每指令每日只落一行, 先到先占 —— 同一指令当日先因 band 落行, 晚轮被 vwap_gate
    或证伪门拦截时不再落行。这对本台账的用途是正确的: 它回答"若把被拦的单都做了,
    影子组合赚多少钱", 同一指令入场两次会重复计钱 (settle_shadow_portfolio.py
    亦按 (inst_id,date) 去重结算)。
    但**不能用作单门归因源** —— 按 blocked_by 分组统计会系统性漏计后发原因
    (同构实测: 既有 vwap_gate 捕获率 87%)。需要"某门到底拦了哪些单"请用
    data/trigger_miss_telemetry.jsonl (逐轮 append, 按 reason 区分, 无跨原因去重)。
    """
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
        # DEB-20260903 #84: 写盘成功后才入进程 set — 原写盘前置位, 写盘异常时
        #   (inst_id,date) 键已被标记 → 该影子入场行永久丢失, 与 _VWAP_AB_LOGGED 一致.
        _SP_EMITTED.add(_lk)
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
            # DEB-20260903 #84: 时间戳用 ET 口径 (原 datetime.now() 服务器本地时区,
            #   读侧 playbook_service 按 ET trade_date 子串过滤, 跨时区会漏配).
            "timestamp": datetime.now(_now_et()).isoformat(),
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

    P2a (2026-09-06) 起本体在 app.playbook.exit_engine (逐字迁入)。本转发器保留
    供 pipeline 内非引擎调用点 (_sl_fallback_exit_reason) 与既有测试。
    """
    from app.playbook.exit_engine import _is_overnight_position as _impl
    return _impl(entry_time_str)


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
            #   调参保持单一真相源。
            # 2026-08-22 (topup孤儿开关修复): 键名从 playbook.topup.technical_override_enabled
            #   改为 playbook.technical_fallback_override_enabled — topup 已于 8/6 停供,
            #   该开关实际控制的是 no-bridge 兜底链(TFA)是否覆写 strategies.technical.enabled=false,
            #   与 topup 无关. 语义修正, 调参者不再误以为控制已停供的 topup.
            _t_cfg = None
            try:
                from app.core.config_container import get_config as _tf_gc
                if bool(_tf_gc().get_dotted("playbook.technical_fallback_override_enabled", False)):
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
        # DEB-20260904 #177: `or 50`/`or 0.5` 会把合法的 0 值静默改写成默认值
        # (strength=0 应被下游 SignalQuality 地板拦截, 改写成 50 反而放行)
        _cand_str = getattr(cand, "strength", None)
        _cand_conf = getattr(cand, "confidence", None)
        strength = int(_cand_str) if _cand_str is not None else 50
        confidence = float(_cand_conf) if _cand_conf is not None else 0.5
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

        # DEB-20260903 #85: score 类型守卫 — cand.get("score", 5.0) 当 score=None 时
        # 返回 None, float(None) 抛 TypeError; schema 校验不查 score 字段, 需消费端守卫
        _raw_score = cand.get("score")
        score = float(_raw_score) if (isinstance(_raw_score, (int, float)) and not isinstance(_raw_score, bool)) else 5.0
        # 2026-08-30 (DEB-20260830 E11 修复): 删除消费端重复合成, 直读生成端权威值.
        # 根因: 原处用 `chall_dir == direction` 字面比较, 但挑战者方向是 BUY/SELL_SHORT
        # 标签体系而 direction 是 LONG/SHORT → 恒不等 → 全候选被误判"方向分歧"恒扣 0.35,
        # 与生成端权威合成 (DA skill adapter._compute_confidence: 同标签体系比较+弃权
        # 中性处理+score差值衰减+fallback独立性检测) 及落库真值 (8/28 全候选 70) 矛盾。
        # 双合成漂移实测泄漏: 8/18-8/25 六条 "BUY confidence 35 < 55" SKIP 行 +
        # :1347 方向冲突门 (阈值0.70) 恒死。生成端是权威: 39 个历史 bridge 文件
        # 全候选带 confidence 字段 (含 WARN 降档后的值), 消费端重合成多余且错 — 删除。
        # 回滚: revert 本提交 (回到双合成漂移态, 不比现在更坏)。
        # 2026-08-30 第6轮F修复: `or 0.5` 会把合法 0.0 静默改写成中性放行, 与教训③(SSOT)矛盾.
        # 生成端契约值域 [0.10, 0.95], 但消费端不再二次解释语义 — None/非数 → 中性兜底,
        # 数值 → 原样透传 (含 0.0, 虽然当前生成端不可能产出).
        # 第8轮F边界: isinstance(True, (int, float)) 为真, JSON true 会被当真数 1.0 →
        # 钳到 0.95 满信心. bool 显式排除, 走中性兜底.
        _raw_conf = cand.get("confidence")
        if isinstance(_raw_conf, bool) or not isinstance(_raw_conf, (int, float)):
            confidence = 0.5
        else:
            confidence = float(_raw_conf)
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
            # [DEB-20260901] 结构化入场语义透传 (bridge 有则带, 无则 None; record-only)
            "entry_archetype": cand.get("entry_archetype"),
            "confirmation": cand.get("confirmation"),
            "chase_cap_price": cand.get("chase_cap_price"),
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


# [v0.70 P0-2 2026-08-26] 运行期 bridge 重载 — 治 L845 所述盲区的"接不上"半边:
#   服务启动早于 DA 产出 (hermes cron 19:00CST=07:00ET) 时, __init__ 里
#   _try_load_bridge 找不到当日文件 → 内存恒为 TechnicalFallbackAdapter,
#   当晚 bridge 落盘后也永远接不上, 全天技术面降级且四路监控均不可见
#   (健康检查读的是文件态, 不是内存态). 修复: run_generate 入口探测重载.
_bridge_reloaded_date = None


# [DEB-20260902-stale-bridge P1-① 2026-09-02] 陈旧 bridge 交易日滚动重载 —
#   P0-2 守卫①"已是 BridgeVotingAdapter 就不重载"只治 TechnicalFallback→Bridge 半边,
#   留下另半边: 服务在 ET 日界前启动 → 内存载 T-1 bridge, 当日 bridge 落盘后永不刷新,
#   直到下一次重启 (而重启是事故驱动的, 不是设计驱动的)。
#   9/1 实锤 (logs/ftseek_us.log-20260902): 11:42CST 启动载 8/31 bridge (7候选) →
#   9/1 bridge 19:27:54 落盘 (13候选, DA skill 同时直写 13 条当日指令) → 21:00 服务侧
#   生成批仍用陈旧 adapter, 21:04 五条 vote→instruction 全带 DA-202608311*
#   (BABA/LLY/BWXT/META/EIX) → 4/14 成交溯源 T-1 决策 (净 +157.17 = 当日净利 72%),
#   其中 BABA/BWXT 当日 DA 门已显式 BLOCK (破位硬买拦截), EIX 当日 DA 为 SKIP/BLOCK
#   (8/31 SHORT strength=80 全天重试 67 次, 仅被单轮上限→单日 notional 上限挡住;
#   EIX 54.00→收盘 58.94, 若成交会在 5.5% SL 被扫 ≈ -$240)。22:01 冻结 heal 重启才换血。
#   回滚: playbook.bridge_stale_reload_enabled=false (逐字节回到 P0-2 行为)。


def _stale_bridge_reload_enabled() -> bool:
    """陈旧 bridge 滚动重载开关 (默认开)。配置读失败 → 保持开启 (完整性保护优先, R5 warn 留痕)。"""
    try:
        from app.core.config_container import get_config as _gc_sb
        return bool(_gc_sb().get_dotted("playbook.bridge_stale_reload_enabled", True))
    except Exception as _sb_e:
        logger.warning(f"[playbook] P1 陈旧bridge重载开关读取失败 (保持开启): {_sb_e}")
        return True


def _reload_bridge_if_landed(scheduler) -> None:
    """运行期 bridge 重载: 非 Bridge adapter (或陈旧 Bridge) + 当日文件已产出(ok) → 重挂.

    条件全满足才动作 (幂等, 一天最多一次成功重载):
      ① 当前 scheduler.voting_coordinator 非 BridgeVotingAdapter, 或虽是 Bridge 但其
         _trade_date ≠ 今日 (P1 陈旧 bridge 滚动重载, 开关
         playbook.bridge_stale_reload_enabled; _trade_date 缺失 → 不判陈旧, fail-soft);
      ② 今日未重载过 (_bridge_reloaded_date 守卫 — 文件一日一产, 失败也不反复试);
      ③ 当日 bridge 文件 state==ok 且内部 trade_date==今日 (复用 _bridge_file_state).
    成功 → 替换 scheduler.voting_coordinator (该属性仅 __init__ 写入, 运行期无其他写者);
    失败/未就绪 → 静默返回 (缺失告警由 _check_bridge_health_and_alert #1 兜底).
    """
    global _bridge_reloaded_date
    try:
        _today = _today_et_str()
        if _bridge_reloaded_date == _today:
            return
        vc = getattr(scheduler, "voting_coordinator", None)
        _is_bridge = vc is not None and type(vc).__name__ == "BridgeVotingAdapter"
        _stale = False
        if _is_bridge:
            _vc_date = str(getattr(vc, "_trade_date", "") or "")
            _stale = bool(_vc_date) and _vc_date != _today
            if not _stale:
                return
            if not _stale_bridge_reload_enabled():
                logger.warning(
                    f"[playbook] P1 陈旧bridge在内存 (trade_date={_vc_date} ≠ 今日{_today}, "
                    f"{len(getattr(vc, '_candidates', {}) or {})} candidates) 但重载开关关闭 → "
                    f"本批生成将消费 T-1 候选 (playbook.bridge_stale_reload_enabled=false)"
                )
                return
            _from = f"BridgeVotingAdapter(trade_date={_vc_date})"
        else:
            _from = type(vc).__name__ if vc is not None else "None"
        if _bridge_file_state(_today) != "ok":
            return
        bridge = _try_load_bridge()
        if bridge is None or type(bridge).__name__ != "BridgeVotingAdapter":
            return
        scheduler.voting_coordinator = bridge
        _bridge_reloaded_date = _today
        logger.info(
            f"[playbook] {'P1 陈旧bridge滚动重载' if _stale else 'P0-2 运行期bridge重载'}成功: "
            f"{_today} {_from} → BridgeVotingAdapter ({len(bridge._candidates)} candidates)"
        )
    except Exception as _rl_e:
        logger.warning(f"[playbook] P0-2 bridge重载探测异常(不阻断): {_rl_e}")


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

    # [v0.70 P0-2 2026-08-26] 文件内 trade_date 与今日不一致 → 过期视作缺失.
    #   场景: 服务在 DA 产出前重启, 文件名日期由 get_trade_date() 决定本应自洽,
    #   但跨日边界/生产端写错日期时, 旧候选会被当成今日信号消费 (错价决策源).
    #   实测口径: 文件名与内部 trade_date 均为 YYYY-MM-DD 纯文本, 直接比对安全.
    _inner_date = str(data.get("trade_date", "") or "")
    if _inner_date and _inner_date != _today_str:
        logger.error(
            f"[playbook] bridge文件trade_date={_inner_date} ≠ 今日{_today_str}, "
            f"过期数据fail-safe视作缺失, fallback到VotingCoordinator"
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


def _direction_conditioned_ic_net(direction: str, bearish_hits, bullish_hits,
                                  factor_ic: dict):
    """方向条件化 IC 净分 (DEB §五十四, 2026-08-20)。

    旧法用 pooled IC (样本 ~91% LONG) 对 SHORT 候选投反对票 = 样本污染:
    AVGO 8/19 被 pooled net_ic=+0.09 拦空, 当日 -5.2% 方向正确 (空单未丢,
    但再评估被污染分否决; §五十三修正定性)。
    新法: 候选方向取同侧 IC (fwd_ret_t1 已方向调整, ic_short>0 = 该因子在
    SHORT 信号上做空赚钱)。n_<side>≥30 才计分, 不足者不参与 (不用 pooled
    顶替 — 宁缺毋滥)。bearish/bullish 分类不再参与计分: 方向信息已在
    ic_<side> 内, 双重方向化会翻转符号 (§五十四语义推演实锤)。

    Returns: (net_score, scored_factors) — net<-0.05 = 因子整体反对此方向。
    """
    side = "short" if direction == "SELL_SHORT" else "long"
    ic_key = "ic_" + side
    n_key = "n_" + side
    net_score = 0.0
    scored_factors = []
    for c in list(bearish_hits) + list(bullish_hits):
        fdata = factor_ic.get(c) or {}
        ic_val = fdata.get(ic_key)
        n_val = int(fdata.get(n_key) or 0)
        if ic_val is None or n_val < 30:
            continue  # ponytail: 该方向样本不足 → 不计分
        net_score += float(ic_val)
        scored_factors.append(c)
    return net_score, scored_factors


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

    # 2026-08-22 (P2): factor_ic_latest.json 循环外一次读取 — 原循环内逐候选 open+json.load,
    #   20-50 候选重复读同一文件. 提到循环外, 函数级一次读取.
    _factor_ic_global = {}
    try:
        import json as _json
        from pathlib import Path as _Path
        _ic_path = _Path("data/factor_ic_latest.json")
        if _ic_path.exists():
            with open(_ic_path) as _f:
                _factor_ic_global = _json.load(_f)
    except Exception:
        _factor_ic_global = {}

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
        # "price_gap_skip" 已删除 (2026-09-08): 价格偏离门结构性恒不触发 (同对象同快照,
        #   只能测买卖价差), 盘前必假阳, 对 DA 标的又被 prio 仲裁丢弃 — 详见 1677 行注释。
        "no_entry_price": 0,
        "final_instructions": 0,
    }

    # [v0.70 P1] ADR-008 仲裁配置 — 循环外一次读取 (原每候选 get_config+2次get_dotted)
    _direction_mode = "llm_vote"
    _llm_conflict_threshold = 0.70
    try:
        from app.core.config_container import get_config as _get_config_hoisted
        _cfg_hoisted = _get_config_hoisted()
        _direction_mode = str(_cfg_hoisted.get_dotted("playbook.direction_mode", "llm_vote")).lower()
        _llm_conflict_threshold = float(_cfg_hoisted.get_dotted(
            "playbook.technical_arbitration.llm_conflict_confidence_threshold", 0.70))
    except Exception:
        pass

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
            # 2026-08-18 (标签勘误): 旧文案"投票返回 None"误导漏斗诊断 — 8/18 复盘据此
            #   误判"DA 产出被静默丢弃 64%"。真相: BridgeVotingAdapter 对非 DA 候选查无
            #   此人返回 None (设计如此, DA 14 候选当日 100% 被消费); Technical/Null 适配器
            #   返回 None 分别是"technical 无方向"/"无 bridge"。改为语义准确文案。
            #   零行为变更, 下游无字符串消费方 (8/18 grep 验证)。
            _filter_audit["vote_none"] = _filter_audit.get("vote_none", 0) + 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="非当前通道候选(bridge未提名/无方向)"
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
        except Exception as _snap_e:
            # [v0.70 P1 2026-08-26] 静默 pass → 告警日志。快照断更是本项目"影子墓地"
            #   的根因模式 (LLM审计库空转两个月无人知同类): IC 管线会静默饿死。
            #   保持非阻断语义 (不 raise), 但必须可观测。
            logger.warning(
                f"[playbook] {symbol} 信号快照记录失败(非阻断, IC管线将缺此样本): "
                f"decision_id={_snap_decision_id}, err={_snap_e}"
            )

        if is_neutral:
            _filter_audit["vote_neutral"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票中性, 无方向信号"
            ))
            continue

        # ── ADR-008 (2026-06-28): 盘中技术方向仲裁 ──
        # [v0.70 P1] 配置读取已提到循环外 (_direction_cfg_init) — 原每候选重复
        #   get_config()+两次 get_dotted, 50候选=150次冗余调用。
        technical_candidate = None

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
        # [E12 登记·2026-08-30 第6轮] 休眠门语义漂移陷阱: 本块当前 100% 不可达
        # (生产 direction_mode=llm_vote), 但修复 04150669 后若切到 technical_arbitration:
        # 生成端主路径输出精确 0.70, `>= _llm_conflict_threshold(0.70)` 在浮点刀刃上恒真
        # → 门在 100% 方向冲突上触发 SKIP, 技术覆盖分支永不执行 (修复前是技术永远赢)。
        # 切 direction_mode 前必须先复核本门语义。
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
                _factor_ic = _factor_ic_global  # P2: 循环外已读, 复用
            except Exception:
                _factor_ic = _factor_ic_global

            # ── 2026-08-20 (DEB §五十四): 方向条件化 IC 净分, 取代 pooled 加权 ──
            if direction in ("BUY", "SELL_SHORT"):
                _net_score, _scored_factors = _direction_conditioned_ic_net(
                    direction, _bearish_hits, _bullish_hits, _factor_ic)

                # 2026-07-04 门语义保留: 净IC为负 = 命中因子整体反对此方向 → SKIP
                if _net_score < -0.05:
                    _side = "short" if direction == "SELL_SHORT" else "long"
                    _filter_audit["direction_flip"] += 1
                    logger.info(
                        f"[playbook] {symbol} 方向条件化IC冲突 ({_side}侧净IC={_net_score:+.2f}, "
                        f"计分因子={_scored_factors}) 与LLM方向({direction})相悖 → SKIP"
                    )
                    instructions.append(Instruction(
                        symbol=symbol, direction="SKIP",
                        skip_reason=f"IC方向冲突: {direction}候选{_side}侧净IC={_net_score:+.2f}(方向条件化)"
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
        _risk_pct = float(_gpc().get("risk_pct", 0.5))
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
            # [DEB-fullchain 低优 2026-08-16] 透传生成器带因返回 (ATR缺失≠价格缺失)
            _skip_why = (values or {}).get("skip_reason") or "价格缺失, 无法计算数值"
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason=_skip_why
            ))
            continue

        # 2026-06-25 P0 修复的"价格偏离 guardrail"于 2026-09-08 删除 (根治, 非调参):
        #   原实现: `_last_price = data.technical.price` vs `_entry = values.entry_price`,
        #   偏离 >2% 则 SKIP。三条证据表明它从未产生价值, 只产假阳 WARNING:
        #   ① **同对象同快照** → 结构上测不到它声称要防的东西。entry 由
        #      compute_instruction_values(decision, data=**同一个 data**) 取 tech.bid/ask,
        #      而 _last_price 取 tech.price —— 两者来自同一次 router.get_full_market_context
        #      的同一个 ctx (market_data_router:431)。所以差值只含**买卖价差**, 不含
        #      "信号生成后价格已异动"。实测 TRADING 相位 10 标的最大偏离 0.142%,
        #      全部 << 2% → 盘中恒不触发 (死门)。
        #   ② **盘前必然假阳**。盘前 technical.price 是上一交易日收盘价 (无成交),
        #      而 entry 已由 6/25 同批修复改用实时 bid/ask → 偏离纯粹是数据不同步。
        #      近 5 日 9 次触发全在 ET 09:04-09:07, 偏离 2.1%-26%
        #      (BE 8.16% / ROIV 19.18% / GTLB 25.97%) 全是假阳。
        #   ③ **对 DA 直写标的恒失效**。本门产出空 decision_id 的 SKIP 票 (prio 0),
        #      save_instruction 的 8/17 覆盖仲裁阶梯拒绝其覆盖 DA 行 (prio 3) →
        #      上述 9 次 SKIP **全部被仲裁丢弃**, 对应 DA 指令 7/9 照常触发成交。
        #      即: 对 DA 标的是死门, 对投票通道标的是假阳误杀 — 两头都不产生价值。
        #   6/25 的原始事故 (MU last=$1048 vs bid/ask=$1242 → 按旧价做空止损瞬间击穿)
        #   的真修法是"entry 改用 bid/ask", **那一半已经修了** (instruction_generator:377);
        #   本门是同批留下的残留。盘中保护由 re-price + chase_cap enforce +
        #   极端gap门(>8%) 三道在触发时刻用实时价承担, 不受影响。
        #   同步删除 _filter_audit["price_gap_skip"] 与其报告字段 (无测试/下游依赖, 已 grep 确认)。

        # 2026-07-10: bridge候选用辩论合成confidence, 非bridge用数据质量confidence
        # 根因: compute_confidence()是纯数据质量分(数据维度ok数×12+条件验证×28,封顶90)
        #   → 数据完整时永远=90, 与分析确信度无关
        #   → confidence=90但胜率31%, 因为"90"是数据完整度不是确信度
        # 口径勘误 (2026-08-30, DEB-20260830 E11): BridgeVotingAdapter.vote() 的
        #   final_confidence 现直读生成端权威值 (DA skill adapter._compute_confidence,
        #   同标签体系比较+弃权中性+score差值衰减), 不再是消费端字面重合成
        #   (旧重合成用 LONG==BUY 恒不等全候选误判分歧恒=0.35, 与落库真值矛盾)。
        #   非bridge保持compute_confidence不变
        _is_bridge = (vote_result.get("strategy_id", "") == "deep_analysis" 
                      or str(vote_result.get("decision_id", "")).startswith("DA"))
        if _is_bridge:
            _llm_conf = float(vote_result.get("final_confidence", 0) or 0)
            confidence = int(_llm_conf * 100)  # 0.0-1.0 → 0-100
        else:
            confidence = compute_confidence(data, decision["selected_conditions"], factors)

        # 2026-07-02: 跨日方向翻转检查 — 同标的24h内方向翻转降仓50% (原+15门槛方案已废弃)
        # 根因: LLM方向翻转率34%, ORCL 6/23 SHORT赚→6/24 LONG亏(conf=90但方向错)
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
                    # 方向翻转 — 标记, 后续按"降仓50%"处理 (2026-07-11 起 conf门槛+15方案已废弃)
                    _filter_audit["direction_flip"] += 1
                    logger.info(
                        f"[playbook] {symbol} 跨日方向翻转: {_prev_dir}→{direction} "
                        f"(prev={_prev_date}), 降仓50%"
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
                # 2026-08-30 F4: 透传 decision_id, 结算可按指令唯一键 join
                decision_id=str(vote_result.get("decision_id", "") or ""),
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
            # [DEB-20260901] 结构化入场语义透传 (record-only, 零行为变更)
            entry_archetype=vote_result.get("entry_archetype"),
            confirmation=vote_result.get("confirmation"),
            chase_cap_price=vote_result.get("chase_cap_price"),
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


# ── Vault industry 缓存 (2026-08-21 sector guardrail 复活, DEB §44 定案修法) ──
# data.fundamental.industry 填充率仅 2% (8月 298/300 指令 sector 为空) →
# _apply_portfolio_guardrails 的 sector 集中度检查实质致盲。
# 修法: 空 sector 从 obsidian_vault/symbols/*.md frontmatter industry 回填
# (覆盖 197/213=92%), 不新建数据源。进程级缓存 1h, 读失败 fail-open (空 dict)。
_VAULT_INDUSTRY_CACHE: Optional[Dict[str, str]] = None
_VAULT_INDUSTRY_CACHE_TS: float = 0.0


def _get_vault_industry_map(max_age_s: float = 3600.0) -> Dict[str, str]:
    """obsidian_vault/symbols/*.md frontmatter industry → {US.MU: Semiconductors}"""
    global _VAULT_INDUSTRY_CACHE, _VAULT_INDUSTRY_CACHE_TS
    _now = time.time()
    if _VAULT_INDUSTRY_CACHE is not None and (_now - _VAULT_INDUSTRY_CACHE_TS) < max_age_s:
        return _VAULT_INDUSTRY_CACHE
    import re
    _m: Dict[str, str] = {}
    try:
        _vault_dir = Path(__file__).resolve().parent.parent.parent / "obsidian_vault" / "symbols"
        _pat = re.compile(r'^industry:\s*"?([^"\n]+)"?', re.M)
        if _vault_dir.is_dir():
            for _f in _vault_dir.glob("*.md"):
                try:
                    _head = _f.read_text(encoding="utf-8", errors="ignore")[:2000]
                except Exception:
                    continue
                _mm = _pat.search(_head)
                if _mm and _mm.group(1).strip():
                    _m[_f.stem.replace("_", ".")] = _mm.group(1).strip()  # US_MU → US.MU
    except Exception as _e:
        logger.warning(f"[playbook] vault industry 读取失败 (fail-open, sector 维持原值): {_e}")
    _VAULT_INDUSTRY_CACHE = _m
    _VAULT_INDUSTRY_CACHE_TS = _now
    return _m


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

    # 2026-08-21 sector 复活 (DEB §44 定案修法): 空 sector 回填 vault industry。
    #   data.fundamental.industry 填充率仅 2% → sector 集中度检查实质致盲。
    #   原地回填同一对象: guardrail 判定与后续 save_instruction 落库同值。
    if valid:
        _ind_map = _get_vault_industry_map()
        for _i in valid:
            if not (getattr(_i, "sector", "") or ""):
                _vs = _ind_map.get(getattr(_i, "symbol", ""), "")
                if _vs:
                    _i.sector = _vs

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
    # 2026-08-21: Unknown = 缺信息 ≠ 行业相关, 不参与集中度判定 (防 vault 未覆盖票被误降级)
    sectors = Counter(getattr(i, "sector", "") or "" for i in valid)
    for sector, count in sectors.items():
        if not sector or sector == "Unknown":
            continue
        if count / total > max_sector_pct:
            logger.warning(
                f"[playbook guardrail] sector {sector} 占比 {count/total:.0%} 超限, "
                f"{_trim_majority_sector(valid, sector, max_sector_pct)} 条信号被降级为 SKIP"
            )

    return active


def _trim_majority_direction(valid: List[Any], majority_dir: str, target_skew: float) -> int:
    """多数方信号降级为 SKIP 直到偏斜 ≤ 阈值 (控制方向风险).

    2026-09-01 RFC-071 根因修复 (砍半 → SKIP):
      旧实现砍 position_size *= 0.5, 但 6% 单名帽下满仓≈$5,900 砍半≈$2,950 结构性
      穿破 admission min_notional 地板 → 砍半产物在执行前必死于地板, 且死因被覆写为
      NOTIONAL_TOO_SMALL (8/31 实证: 4 只高置信单 100% BUY 偏斜被砍半, 全灭, 真实
      死因被掩盖; 与日志文案"被降级为SKIP"亦不符)。
      直接 SKIP: 死因如实留痕, position_size 保留原值供审计。循环"SKIP 最低
      confidence 的多数方直到达标"与 docstring 原意("砍掉低 conv 的多数方信号直到
      平衡")及 sector 集中度孪生实现 (_trim_majority_sector) 对齐。
    """
    trimmed = 0
    while True:
        active_valid = [i for i in valid if i.direction in ("BUY", "SELL_SHORT")]
        total = len(active_valid)
        if total <= 1:
            break
        majority = [i for i in active_valid if i.direction == majority_dir]
        if not majority or len(majority) / total <= target_skew:
            break
        majority.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        victim = majority[0]
        victim.direction = "SKIP"
        victim.skip_reason = (
            f"方向偏斜超限 guardrail 降级 (多数方 {majority_dir}, "
            f"原size={getattr(victim, 'position_size', 0) or 0})"
        )
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
def _mark_generate_done(today: str) -> None:
    """[#22] 写生成完成标记 (best-effort, 失败不阻断返回)."""
    try:
        from app.playbook.db import PlaybookDB
        PlaybookDB().mark_generate_completed(today)
    except Exception as e:
        logger.warning(f"[playbook] 生成完成标记写入失败(不影响本次结果): {e}")


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
            # [v0.70 P0-2 2026-08-26] 运行期bridge重载探测 — 服务启动早于19:00CST产出时,
            #   内存恒为 TechnicalFallback; 此处(ET≥09:00 已过闸)文件应已就绪, 补挂 Bridge.
            #   一天一次幂等, 失败静默 (缺失告警由 _check_bridge_health_and_alert 兜底).
            _reload_bridge_if_landed(scheduler)
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
            _mark_generate_done(today)  # [#22] 终态: 空表也是完成态, 不留锁死窗口
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
            _mark_generate_done(today)  # [#22] 终态
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
        _mark_generate_done(today)  # [#22] 成功收尾才写完成标记 (异常路径不写, 允许重跑)
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
                # [2026-08-17 根源修复] 覆盖仲裁已下沉 save_instruction SQL 层,
                # topup 无需调用方守卫 (同 generate)。
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


# [v0.70 P1 2026-08-26] theme_context.json mtime 缓存 — 原实现每候选重新 open+parse
#   同一文件 (50候选=50次冗余IO+解析)。按 mtime 失效, 文件更新自动重载。
_tc_cache = {"mtime": None, "data": None}


def _load_theme_context_cached(tc_path):
    """读 theme_context.json, 进程内按 mtime 缓存。失败返回 None (与原 try/except 等价)。"""
    import json as _json_tc
    from pathlib import Path as _Path_tc
    _p = _Path_tc(tc_path)
    try:
        mt = _p.stat().st_mtime
    except Exception:
        return None
    if _tc_cache["mtime"] == mt and _tc_cache["data"] is not None:
        return _tc_cache["data"]
    try:
        with open(_p, encoding="utf-8") as f:
            _tc_cache["data"] = _json_tc.load(f)
        _tc_cache["mtime"] = mt
        return _tc_cache["data"]
    except Exception:
        return None


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
            # [v0.70 P1] mtime 缓存: 同一 generate 轮内 50 候选只 parse 一次
            tc = _load_theme_context_cached(tc_path)
            if tc is not None:
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
        # [DEB-fullchain #27 2026-08-16] decision_conf (0-1) 落库 — 列 2026-08-14 已加
        #   (DEB-20260814 P6) 但 pipeline 路径零写入, 全库 18 个非空值全来自 DA skill
        #   (口径=confidence/100)。本行补齐 pipeline 写入, 与 DA 同口径。
        "decision_conf": round(float(inst.confidence) / 100.0, 4) if inst.confidence else None,
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
            # [DEB-20260901] 结构化入场语义落库 (消费方: Phase B 影子; 缺失=旧指令, 按现状行为)
            "entry_archetype": getattr(inst, "entry_archetype", None),
            "confirmation": getattr(inst, "confirmation", None),
            "chase_cap_price": getattr(inst, "chase_cap_price", None),
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

                # 检查是否已追踪：所有指令均有终态 outcome (win/loss/skipped/missed/void)
                # outcome="holding"/NULL 均不算终态 — 持仓中或未追踪的指令需每日重追踪直到产生最终 outcome
                # 空列表 (PRE_MARKET错过/重启) 也算已追踪 — 没有指令就没有需要追踪的
                # DEB-20260904 #S6: 补 'void' (幽灵仓终态, db.py:438) — 漏掉则含 void 的
                # 批次永远凑不齐"全部终态", 盘后追踪每日重跑
                tracked = not instructions or all(
                    inst.get("outcome") in ("win", "loss", "skipped", "missed", "void")
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
                # DEB-20260904 #S7: 精确标记查询, 替代 get_regime_log(days=7) 行数窗误判
                has_weekly = db.has_weekly_report_marker(week_end)
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
        # TZ-1 附带修复 (2026-08-30): 原条件读 status["action"] (check_status 从不返回
        # 该键) → 告警日志从未触发过。改读真实键 trading_allowed / position_multiplier。
        try:
            from app.playbook.risk_circuit_breaker import RiskCircuitBreaker
            status = RiskCircuitBreaker(db).check_status(_get_total_equity(scheduler))
            if not status.get("trading_allowed") or float(status.get("position_multiplier", 1.0)) < 1.0:
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

# 日亏熔断基线快照 (2026-08-27 组合风控P1): 每交易日首评估时落盘, 当日后续轮次比较
_DAILY_LOSS_BASELINE = Path(__file__).resolve().parents[2] / "data" / "daily_loss_baseline.json"


def _check_daily_loss_circuit(today: str) -> Optional[Dict[str, Any]]:
    """日亏熔断 (2026-08-27, 死键接线): 当日账户净值跌幅超
    risk.circuit_breaker.strategy.daily_loss_threshold (yaml 2026-05-21 起零消费)
    → auto_execute 停新开 (平仓路径不受影响, monitor 独立).

    口径: 富途实时净值 (capital_safety.current_equity, 30s 刷新, 与
    _resolve_sizing_base 同尺) vs 当日首评基线快照. 已实现+未实现+全部交易来源
    天然包含 (账户级亏损无漏项); 基线与现值强制同源, 取不到净值时本轮跳过
    (fail-open + 日志) — 混用 config 兜底尺子会制造 ±15% 级假触发.
    解除语义 (2026-09-05 加滞回带, 用户裁"按照建议执行"): 触发线 -3% / 放行线 -2%,
    两线之间**维持已触发态**。此前放行线==触发线 (无状态机, 每轮独立判), 净值在阈值
    附近来回就会 on/off 逐轮抖动 — 同一交易日内入场口径不稳定, 采到的样本分不清
    "被熔断拦下"还是"恰好那一轮放行"。latch 与基线同文件持久化 (data/daily_loss_baseline.json),
    跨重启不丢; 新交易日首评自动清零。健全性守卫: 若配置把放行线设得比触发线更负
    (永不解除的方向), 自动退回无滞回 (_rel = _thr), 宁可抖动也不把闸门焊死。
    """
    # ts/flip_at 改用 ET 写 (2026-09-05 M-6/B1 第一阶): 两键是**纯信息位** — 全仓唯一读点
    #   是下方 "ts": base.get("ts") 的原值透传, 无解析无比较, 故改写尺度零风险; 改后文件
    #   自证尺度 (带 -04:00 偏移), 免人工读档时把 CST 墙钟当 ET (G-16 同源坑)。
    from app.utils.timezone import now_et as _now_et_ts
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        _eq = float(_cs._state.current_equity or 0) if _cs._state else 0.0
    except Exception as _e:
        logger.warning(f"[playbook] 日亏熔断: 净值读取失败, 本轮跳过 (fail-open): {_e}")
        return None
    if _eq <= 0:
        return None
    try:
        base = json.loads(_DAILY_LOSS_BASELINE.read_text()) if _DAILY_LOSS_BASELINE.exists() else {}
        if not isinstance(base, dict):
            base = {}
    except Exception:
        base = {}
    if base.get("date") != today:
        # 新交易日首评: 落基线, 下一轮起可比 (隔夜跳空计入当日 = 账户口径)
        try:
            _DAILY_LOSS_BASELINE.parent.mkdir(parents=True, exist_ok=True)
            _DAILY_LOSS_BASELINE.write_text(json.dumps(
                {"date": today, "equity": _eq, "ts": _now_et_ts().isoformat(),
                 "latched": False}))
        except Exception as _e:
            logger.warning(f"[playbook] 日亏熔断: 基线落盘失败: {_e}")
        return None
    _base_eq = float(base.get("equity") or 0)
    if _base_eq <= 0:
        return None
    try:
        from app.core.config_container import get_config as _gc
        _thr = float(_gc().get_dotted(
            "risk.circuit_breaker.strategy.daily_loss_threshold", -0.03))
        _rel = float(_gc().get_dotted(
            "risk.circuit_breaker.strategy.daily_loss_release_threshold", -0.02))
    except Exception:
        _thr = -0.03
        _rel = -0.02
    if _rel <= _thr:
        # 放行线必须比触发线更接近 0; 否则触发后永不解除 → 退回无滞回 (fail-safe)
        _rel = _thr
    _pct = _eq / _base_eq - 1
    # 阈值比较加 1e-9 容差: _pct 出自浮点除法, "恰卡线"会被表示误差推到线外。
    #   实证 (旧测 test_recovers_when_equity_rebounds 撞红): 98000/100000-1 =
    #   -0.020000000000000018 < -0.02 → 恰卡放行线却被判"仍在带内", 解除永久失效。
    #   容差方向刻意不对称: 触发线取"稍早触发"(保守), 放行线取"恰好卡线即解除"
    #   (与文档口径"回升到 ≥-2% 才解除"及本闸改动前的既有契约一致)。
    _EPS = 1e-9
    _breached = _pct <= _thr + _EPS
    _was_latched = bool(base.get("latched"))
    if _breached:
        _latched = True
    elif _was_latched and _pct < _rel - _EPS:
        _latched = True      # 滞回带内: 维持触发态
    else:
        _latched = False
    if _latched != _was_latched:
        # 仅状态翻转时落盘+留痕 (本函数每轮 auto_execute 调一次, 不每轮写盘)
        if _latched:
            logger.critical(
                f"🚨 [playbook] 日亏熔断触发: 净值 {_pct * 100:.2f}% ≤ {_thr * 100:.2f}% "
                f"(基线 ${_base_eq:,.0f} → 现值 ${_eq:,.0f}) | 停新开, 平仓照常; "
                f"解除需回升过 {_rel * 100:.2f}% (滞回带)")
        else:
            logger.warning(
                f"[playbook] 日亏熔断解除: 净值回升至 {_pct * 100:.2f}% ≥ "
                f"{_rel * 100:.2f}% 放行线 → 恢复新开仓")
        try:
            _DAILY_LOSS_BASELINE.write_text(json.dumps({
                "date": base.get("date"), "equity": _base_eq,
                "ts": base.get("ts"), "latched": _latched,
                "flip_equity": round(_eq, 2),
                "flip_pct": round(_pct * 100, 2),
                "flip_at": _now_et_ts().isoformat()}))
        except Exception as _e:
            # 落盘失败 → latch 只存内存本轮 (下次重启回到无 latch), 属放松方向, 必须留痕
            logger.warning(f"[playbook] 日亏熔断: latch 落盘失败 (跨重启会丢失触发态): {_e}")
    return {
        "triggered": _latched,
        "raw_breach": _breached,
        "equity": round(_eq, 2),
        "baseline": round(_base_eq, 2),
        "loss_pct": round(_pct * 100, 2),
        "threshold_pct": round(_thr * 100, 2),
        "release_threshold_pct": round(_rel * 100, 2),
    }


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
            # 注 (第10轮口径修正): 本调用点 log=False 永不写影子行, 上面的
            # decision_id 为备用透传 (该路径审计另有落库); 能产出带键影子行的
            # 只有 :1676 一处。
            _edge = get_edge_shadow_gate().evaluate(
                inst.get("symbol", ""), _direction, _conf, live_pass=True, log=False,
                decision_id=str(inst.get("decision_id", "") or ""),
            ) or {}
        except Exception:
            _edge = {}

        # ── R-8a (2026-08-23 DEB-20260823): ts/date 改 ET ──
        # 与 _emit_trigger_miss 同款错位。本 emitter 是"每个到达执行漏斗的候选单"的统一
        # 审计真源 (data/shadow/exec_gate_audit.jsonl, 消费方 scripts/shadow_board.py /
        # scripts/data_freshness_watchdog.py), 比 trigger_miss 更核心 —— 执行漏斗归因、
        # 拦截率分层全靠它。原用裸 _dt.now() = 宿主 Asia/Shanghai, ET 下午的
        # executed/skipped 事件被记到 +1 天, 与 playbook_instructions.date (ET) 关联即错位。
        _now_et_audit = _dt.now(_now_et())
        _row = {
            "ts": _now_et_audit.isoformat(timespec="seconds"),
            "date": _now_et_audit.strftime("%Y-%m-%d"),
            "ts_cst": _dt.now().isoformat(timespec="seconds"),
            "tz": "ET",
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

        # 2026-08-27 (组合风控P1): 日亏熔断 — risk.circuit_breaker.strategy.
        #   daily_loss_threshold 死键接线 (该键 2026-05-21 起全仓零消费, 组合级
        #   日亏熔断从未存在, ADR-032 死门同款). 净值基线口径见
        #   _check_daily_loss_circuit docstring; 触发停新开, 平仓照常 (monitor
        #   路径不经过此门). 回滚: 阈值改 -1.0 + 重启 (等效失效, 无需改码).
        try:
            _dl = _check_daily_loss_circuit(today)
        except Exception as _dl_e:
            logger.warning(f"[playbook] 日亏熔断检查异常 (fail-open): {_dl_e}")
            _dl = None
        if _dl and _dl.get("triggered"):
            logger.warning(f"[playbook] 日亏熔断触发, 本轮停新开: {_dl}")
            return {"date": today, "executed": 0, "skipped": 0,
                    "reason": "daily_loss_circuit", "detail": _dl}

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
            # 2026-08-16 (DEB§37, Qoder §36.4 结构性关注实锤修复): 删除 entry_time
            #   时间窗 (-1 day). 根因: W_WEEK 多夜持仓 (§34, 最长5交易日) 第3-5天仓位
            #   漏出统计 → 预算门低估已用敞口; 且 date('now') 为 UTC, entry_time 为 ET,
            #   时区边界毛刺. exit_time IS NULL 已保证只算未平仓, 时间窗冗余且有害.
            #   方向: 多算不少算 (保守, 同 R1b fail-close); 若存在僵尸未平仓行, 宁拦新开.
            _row = _bp_conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trade_records "
                "WHERE source_type='PLAYBOOK_SIM' AND (exit_time IS NULL OR exit_time = '')"
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
        _daily_notional_cap_pct = float(pb_cfg.get("daily_notional_cap_pct", 0.80))
        _daily_notional_cap = _risk_base_capital * _daily_notional_cap_pct
        # C4 (2026-08-24, 体检 E3): fallback 5→16 对齐 yaml SSOT (unified_config.yaml
        # playbook.execution_parameters.max_daily_positions=16). 原 fallback=5 与 config.py
        # 默认 10 均落后于 8/20 升帽裁决 — 键误删时日开仓帽会静默缩到 5, 是最危险的漂移.
        _max_daily_positions = int(pb_cfg.get("max_daily_positions", 16))
        # ── 2026-08-14 (水坝效应修复): 单轮新开仓上限 ──
        # 根因: 10:15 禁触窗解禁后同一 auto_execute 轮把全部入带指令一次开完 —
        #   实锤 8/14: 9笔LONG挤在87秒(10:16:03-10:17:30), UCTT/SNDK 15分钟内相继SL;
        #   8/8以来水坝窗(10:15-10:20)入场 n=7 净-$330 均笔-$47 vs 其余时刻 n=17 均笔-$8.6 (5.5x)。
        # 修复: 每轮最多新开 N 笔, 剩余 triggered 指令顺延下轮 (auto_execute 间隔300s
        #   → 梯度入场每5分钟一档), 同源回撤不再同时止损。排序已按 source_priority+
        #   confidence 降序, cap 天然保留最强信号。回滚: 配置改 999 即等同解除。
        _max_new_per_cycle = int(pb_cfg.get("max_new_positions_per_cycle", 3))
        _cycle_new_count = 0

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

        # 2026-08-22 audit: already_held 对账查询的共享连接 — 原每条命中指令新开
        #   sqlite3.connect (N+1), 现循环级懒建一次、循环尾关闭; 查询失败自动回退逐条建连.
        _ah_conn = None
        for inst in instructions:
            # 跳过已执行、SKIP 方向的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue
            # 2026-07-19 (DEB-round3 #2): 漂移作废指令不再参与触发 (特异性匹配, 防误杀)
            #   P1-2 (2026-08-23): 取值集合化 — 含 tripwire 核销 (见 INVALIDATED_SKIP_REASONS)
            if (inst.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
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
                        if _ah_conn is None:
                            _ah_conn = _sql3_ah.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db", timeout=10.0)
                            _ah_conn.row_factory = _sql3_ah.Row
                        _pb_rec = _ah_conn.execute(
                            "SELECT entry_price, exit_price, exit_time, exit_reason, realized_pnl, pnl_pct, quantity, "
                            "pnl_net "
                            "FROM trade_records WHERE symbol = ? AND substr(entry_time,1,10) = ? "
                            "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                            "AND UPPER(direction) = ? ORDER BY id DESC LIMIT 1",
                            (_symbol, today, _dir_norm)
                        ).fetchone()
                    except Exception as _ah_q_err:
                        logger.debug(f"[playbook] {_symbol} already_held 对账查询失败: {_ah_q_err}")
                        # 连接级故障 (锁忙/schema) → 弃用共享连接, 下次重试重建
                        _ah_conn = None

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
                            # 2026-08-20 净口径切换 (用户裁"切"): 对账回填同样用净PnL/净判定,
                            #   与 _close_trade_record 的 outcome 契约一致. pnl_net 缺→回退毛.
                            _pnl_net_ah = _pb_rec["pnl_net"]
                            _inst_pnl_ah = round(float(_pnl_net_ah), 2) if _pnl_net_ah is not None else round(_pnl, 2)
                            _upd.update({
                                "exit_price": _exit,
                                "exit_time": _pb_rec["exit_time"] or "",
                                "exit_reason": _pb_rec["exit_reason"] or "unknown",
                                "pnl": _inst_pnl_ah,
                                "pnl_pct": round(float(_pb_rec["pnl_pct"] or 0), 2),
                                "outcome": "win" if _inst_pnl_ah > 0 else "loss",
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

            # ── RFC-068 (2026-08-29, 用户批准): B门硬门 — 接飞刀拒入场 ──
            # 证据 (8/10-8/28 影子×成交对账, RFC-068文档+8/29勘误节为准):
            #   飞刀区成交 4 笔净 -$439:
            #   UCTT pm-6.0%→SL -$145, BABA pm-5.6%→SL -$138, LRCX pm-5.5%→+$3,
            #   RBRK pm-3.4%→SL(26分钟) -$159.
            #   本门执行时刻口径(实时价/真昨收)反事实独立重算(139笔全窗口, 8/29):
            #   拦 4 笔 = BABA -6.20%/LRCX -5.58%/RBRK -5.41%/SNDK -5.48%,
            #   避损 $305.48, 误杀 LRCX +$2.51 → 净收益 +$302.97.
            #   注: UCTT 执行时刻已回升至 -2.85% 放行(其后盘中跌穿止损, 非缺口风险);
            #   RFC 初版表用盘前快照口径记"拦3省283", 与执行时刻口径不同, 见勘误节.
            # 语义: 执行时刻实时价 vs 昨收 < -5% → 拒入场; 若价格回升出飞刀区, 下轮自然复评.
            #   规则判定复用 premarket_invalidation.evaluate_rules (SSOT, 阈值唯一),
            #   影子版 record-only 继续采集对照, 本门只取 B_long_knife 命中转正为硬拦.
            # 数据缺失 (价/昨收≤0, 取数异常) → 放行 (质量门非安全门, 下游仍有 SL 兜底).
            # 回滚: playbook.premarket_invalidation_hard_gate 置 false + restart ftseek-us.
            if str(inst.get("direction", "")).upper() in ("BUY", "LONG") and _playbook_cfg(
                    "premarket_invalidation_hard_gate", True):
                _kg_block = False
                _chg_kg = None
                try:
                    _dr_kg = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
                    _ctx_kg = _dr_kg.get_full_market_context(inst["symbol"]) if _dr_kg is not None else None
                    if _ctx_kg:
                        _px_kg = float(_ctx_kg.get("price") or 0)
                        _pc_kg = float(_ctx_kg.get("prev_close") or 0)
                        if _px_kg > 0 and _pc_kg > 0:
                            _chg_kg = (_px_kg - _pc_kg) / _pc_kg * 100.0
                            from app.playbook.premarket_invalidation import evaluate_rules as _kg_eval
                            if "B_long_knife" in _kg_eval(inst.get("direction"), _chg_kg):
                                _kg_block = True
                                logger.info(
                                    f"[playbook] 🚫 {inst['symbol']} B门硬门拦截: 现价{_px_kg:.2f} vs "
                                    f"昨收{_pc_kg:.2f} ({_chg_kg:+.1f}% < -5%) 接飞刀拒入场 (下轮复评)"
                                )
                except Exception as _kg_err:
                    logger.warning(f"[playbook] {inst['symbol']} B门硬门评估异常 (放行): {_kg_err}")
                if _kg_block:
                    _emit_exec_gate_audit(inst, action="skipped_knife_gate",
                                          entry=float(inst.get("entry_price") or 0),
                                          size=float(inst.get("position_size") or 0),
                                          notional=0.0,
                                          extra={"gate": "B_long_knife",
                                                 "chg_pct": (round(_chg_kg, 2) if _chg_kg is not None else None)})
                    skipped_count += 1
                    continue

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

                        # [DEB-20260901 选项1② · ADR-035] archetype 路由 (默认关闭, 零行为变更):
                        #   PULLBACK_LIMIT/FADE_SHORT 指令 entry 锚定确认位, 下单走限价+TTL。
                        #   SL/TP/sizing 随之锚确认位 (与 DA thesis 语义一致)。
                        _arch_route = None
                        try:
                            from app.playbook.entry_routing import resolve_route
                            _arch_route = resolve_route(
                                inst.get("raw_instruction_json"), _dir, _new_entry, pb_cfg)
                        except Exception as _ar_err:
                            logger.warning(
                                f"[playbook] {inst['symbol']} archetype 路由解析异常 (回退市价现状): {_ar_err}")
                            _arch_route = None
                        if _arch_route and _arch_route.get("action") == "skip":
                            db.update_instruction_tracking(inst["id"], {
                                "skip_reason": _arch_route.get("reason", "archetype_level_conflict")})
                            _emit_exec_gate_audit(inst, action="skipped_archetype_conflict",
                                                  entry=_new_entry, size=inst.get("position_size") or 0,
                                                  notional=0.0, extra={"route": _arch_route})
                            logger.warning(
                                f"[playbook] {inst['symbol']} archetype 价位矛盾: "
                                f"确认位 {_arch_route.get('limit_price')} vs 禁追线 "
                                f"{_arch_route.get('chase_cap_price')}, 终态跳过")
                            skipped_count += 1
                            continue
                        if _arch_route and _arch_route.get("action") == "limit":
                            logger.info(
                                f"[playbook] {inst['symbol']} archetype 路由: {_arch_route['archetype']} "
                                f"市价 ${_new_entry:.2f} → 确认位限价 ${_arch_route['limit_price']:.2f} "
                                f"TTL={_arch_route['ttl_seconds']:.0f}s")
                            _new_entry = float(_arch_route["limit_price"])

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
                            _max_pos_pct = float(pb_cfg.get("max_position_pct", 0.06))
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
                        # [DEB-20260901 选项1②] 路由随下单参数透传 (update_instruction_tracking
                        #   白名单外键自动忽略, 仅 execute_instruction 消费)。
                        if _arch_route and _arch_route.get("action") == "limit":
                            _reprice_params["archetype_route"] = _arch_route
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

            # ── [DEB-20260901 Phase B] chase_cap 违例遥测 (record-only) ──
            # DA 写明"禁止在$X以上追涨"但 re-price 把入场位顶过禁追线 →
            #   语义倒挂实锤采样。Phase B 仅记录不拦截; Phase C 才谈阻断。
            # [P0-A 2026-09-04, REVIEW-20260904 用户裁"按建议执行"]:
            #   chase_cap_enforce_enabled=true 时违例指令终态跳过 (见块尾)。
            # G-06 (2026-09-05 审计): 本闸门从 re-price try (上方 :3901→except _rp_err) 内
            #   **提出为独立门**。原缺陷 = 两条静默跳过路径, 使 P0-A 禁追高闸在异常下等于不存在:
            #   ① 块内任何异常 (含 entry_routing 导入失败) 被自身 except 吞成 warning → 照常下单;
            #   ② re-price 早段任何异常 → 外层 except 仅 debug, 本闸门根本不被求值,
            #      指令按 DB 原始价执行 (而"禁追线"语义恰应对真实下单价生效)。
            #   提出后判定对象 = "即将真实下单的价格": re-price 成功取 _reprice_params
            #   (与下方 custom_params=_reprice_params 消费口径同源), 失败取 DB 原值。
            #   纪律点保持: 遥测写盘先于 enforce、enforce 在去重门之外 (test_chase_cap_enforce
            #   源级契约), skip_reason 前缀 chase_cap_enforced(reprice= 与旧行兼容不改。
            #   回滚: chase_cap_enforce_enabled=false 退回纯遥测, 或 git revert 本块。
            # 开关先行独立读取 — 读不到取保守方向 (视为 ON): T2 "读取失败=保守方向"。
            #   跳过一笔违例单是安全的, 放行一笔追高单不是。
            _cc_enforce = True
            try:
                from app.playbook.entry_routing import (
                    chase_cap_enforce_enabled as _cc_enforce_f,
                )
                _cc_enforce = _cc_enforce_f()
            except Exception as _cc_sw_err:
                logger.warning(
                    f"[playbook] chase_cap enforce 开关读取失败 {inst.get('symbol')} "
                    f"→ 保守视为 ON: {_cc_sw_err}")
            _cc_gate_ok = False
            try:
                # 2026-09-01 修复: raw_instruction_json 经 db._row_to_dict
                #   已是 dict, json.loads(dict) 抛 TypeError (当日每轮刷屏)。
                #   parse_raw_semantics 天然兼容 str|dict。
                from app.playbook.entry_routing import (
                    parse_raw_semantics,
                    chase_cap_violated as _cc_violated,
                )
                _sem_cc = parse_raw_semantics(inst.get("raw_instruction_json"))
                _cc_f = _sem_cc["chase_cap_price"]
                # G-06: 真实下单价 SSOT — custom_params 非空即覆盖 DB entry_price。
                #   变量名沿用 _dir/_new_entry (源级契约 + 本循环下游不再消费, 无遮蔽风险)。
                _dir = inst.get("direction", "")
                _new_entry = float((_reprice_params or {}).get("entry_price")
                                   or inst.get("entry_price") or 0.0)
                if _cc_violated(_dir, _new_entry, _cc_f):
                    from pathlib import Path as _Pcc
                    from datetime import datetime as _dtcc
                    _cc_path = _Pcc(__file__).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
                    # DEB-20260903 议题一 (方案A, 用户裁"按建议执行"): warning 移出去重门 —
                    #   每次违例 reprice 都打 (9/2 DELL 二次更糟违例 464.41→474.27 被
                    #   去重门静默吞没, 而 n_chase_cap_violated 是 Phase C 裁决证据列,
                    #   系统性少记=尺子偏向"违例罕见"); jsonl 行按 (inst_id, 值) 去重 —
                    #   同值重报不刷屏, 更差 reprice 记新行。行 type 保持
                    #   "chase_cap_violation" 不变 (replay 按存在性判, 零改动)。
                    logger.warning(
                        f"[playbook] chase_cap违例遥测: {inst['symbol']} 下单价=${_new_entry:.2f} "
                        f"越过禁追线 ${_cc_f:.2f} (enforce={'ON' if _cc_enforce else 'OFF'}"
                        f"{'' if _reprice_params else ', re-price未生效判定用DB原值'})")
                    # G-06: 遥测写盘独立 try — 写盘失败 (磁盘满/权限) 不得冒泡到外层 except,
                    #   否则 _cc_gate_ok=False 会让下方 fail-close 误杀一笔"已判定未违例"或
                    #   "已判定违例应拦截"的指令 (前者误伤合法交易, 后者漏拦)。判定与留痕
                    #   分离: 判定失败→fail-close 不下单; 留痕失败→照判定结果执行。
                    #   次序纪律不变 (commit 仍先于 enforce, 源级契约测试锚点保持)。
                    try:
                        _cc_claim = _shadow_dedup_claim(_cc_path, inst.get("id"), f"chase_cap_violation:{round(_new_entry, 2)}")
                        if _cc_claim is not None:
                            _row_cc = {
                                "ts": _dtcc.now(_now_et()).isoformat(timespec="seconds"),
                                "date": _today_et_str(), "schema": "v1",
                                "type": "chase_cap_violation",
                                "inst_id": inst.get("id"),
                                "decision_id": inst.get("decision_id"),
                                "symbol": inst.get("symbol"), "direction": _dir,
                                "entry_archetype": _sem_cc["entry_archetype"],
                                "chase_cap_price": _cc_f,
                                "repriced_entry": round(_new_entry, 4),
                                # P0-A: 行级标记违例时 enforce 是否生效 —
                                #   replay/月报可区分"记录期违例"与"拦截期违例"。
                                "enforced": bool(_cc_enforce),
                                # G-06: 判定价来源 — re-price 失败路径判的是 DB 原值,
                                #   Phase C 复算须能区分 (否则违例率分母口径混淆)。
                                "reprice_ok": bool(_reprice_params),
                            }
                            _cc_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(_cc_path, "a", encoding="utf-8") as _f_cc:
                                _f_cc.write(json.dumps(_row_cc, ensure_ascii=False) + "\n")
                            # DEB-20260904 #175: 写盘成功后才提交键
                            _shadow_dedup_commit(_cc_claim)
                    except Exception as _cc_w_err:
                        logger.warning(
                            f"[playbook] chase_cap遥测写盘失败 {inst.get('symbol')} "
                            f"(不影响 enforce 判定/拦截): {_cc_w_err}")
                    # ── P0-A enforce: 终态跳过 ──
                    # 位置纪律: 在遥测写盘之后 (证据先落) 且在去重门之外
                    #   (enforce 不被同值去重吞掉; 首违即终态, 本就无第二次)。
                    # skip_reason 非空 → 触发头检/auto_execute 双保险不再捞起。
                    if _cc_enforce:
                        db.update_instruction_tracking(inst["id"], {
                            "skip_reason": (
                                f"chase_cap_enforced(reprice={round(_new_entry, 2)},"
                                f"cap={_cc_f})")})
                        _emit_exec_gate_audit(
                            inst, action="skipped_chase_cap",
                            entry=_new_entry, size=inst.get("position_size") or 0,
                            notional=0.0,
                            extra={"chase_cap_price": _cc_f,
                                   "repriced_entry": round(_new_entry, 4)})
                        logger.warning(
                            f"[playbook] chase_cap拦截: {inst['symbol']} {_dir} "
                            f"下单价=${_new_entry:.2f} 越过禁追线 ${_cc_f:.2f} "
                            f"→ 终态跳过 (P0-A enforce)")
                        skipped_count += 1
                        continue
                _cc_gate_ok = True
            except Exception as _cc_err:
                logger.warning(f"[playbook] chase_cap遥测异常 {inst.get('symbol')}: {_cc_err}")
            # G-06 fail-close: enforce=ON 而闸门自身未能完成判定 (解析/写盘/落库异常) →
            #   无法证明"未违例", 保守不下单。enforce=OFF 保持旧的纯遥测行为不拦截。
            #   落痕失败也不放行 — 跳过动作无条件执行。
            if not _cc_gate_ok and _cc_enforce:
                logger.critical(
                    f"🚨 [playbook] chase_cap闸门不可用 → fail-close 终态跳过 "
                    f"{inst.get('symbol')} (enforce=ON 时无法证明未违例, 保守不下单)")
                try:
                    db.update_instruction_tracking(inst["id"], {
                        "skip_reason": "chase_cap_gate_unavailable"})
                    _emit_exec_gate_audit(
                        inst, action="skipped_chase_cap",
                        entry=0.0, size=inst.get("position_size") or 0,
                        notional=0.0, extra={"fail_close": True})
                except Exception as _cc_fc_err:
                    logger.warning(
                        f"[playbook] chase_cap fail-close 落痕失败 (仍跳过): {_cc_fc_err}")
                skipped_count += 1
                continue

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

            # 2026-08-14 水坝效应修复: 单轮新开仓上限 (超出顺延下轮, triggered 状态不丢)
            if _cycle_new_count >= _max_new_per_cycle:
                logger.info(
                    f"[playbook] {inst['symbol']} 顺延 — 单轮新开仓上限 "
                    f"(本轮已开 {_cycle_new_count} 笔, 待下轮 auto_execute)"
                )
                _emit_exec_gate_audit(inst, action="deferred_cycle_cap", entry=_entry,
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
                    _cycle_new_count += 1  # 2026-08-14 水坝效应修复: 单轮新开仓计数
                    _max_deploy += _new_notional  # 累加已用额度
                    _today_opened_notional += _new_notional
                    _today_opened_count += 1
                    _audit_action = "executed"
                    logger.info(f"[playbook] ✅ 自动执行: {inst['symbol']} {inst['direction']}")
                else:
                    skipped_count += 1
                    if result.get("ttl_expired"):
                        # [DEB-20260901 选项1②] 路由限价 TTL 到期放弃 = 设计内终态, 非执行失败
                        _audit_action = "skipped_ttl_expired"
                        logger.info(f"[playbook] ⏳ {inst['symbol']} 路由限价 TTL 到期放弃 (影子遥测已落)")
                    else:
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
        # 2026-08-22 audit: 关闭 already_held 对账共享连接
        if _ah_conn is not None:
            try:
                _ah_conn.close()
            except Exception:
                pass
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


# P1-3 (2026-08-23): 逐分钟 p50 累积量曲线缓存 {symbol: (day, [cum_p50...])} —
#   源文件 5-6MB/票, 只在当日首次访问时解析一次。
_VOL_CURVE_CACHE: Dict[str, Any] = {}


def _intraday_vol_curve_p50(symbol: str, days: int = 20) -> Optional[List[float]]:
    """P1-3: 该标的近 days 个交易日"开盘后第 i+1 分钟累积成交量"的中位数曲线。

    替代线性折算 (elapsed/390) 的依据: 真实日内成交量呈 U 型 (开盘/收盘高、午间低),
    线性折算在开盘时段系统性【低估】期望量 (分母 = 日均量 × elapsed/390, 开盘 20min
    只给 5% 配额, 而真实成交常已达 15-20%) → ratio 被系统性高估, 量能门在成交最集中、
    最该做确认的开盘时段恒开, 等于没门。

    数据源 data/replay_bars_1m/US_<SYM>.json ([time,o,h,l,c,volume], 由
    scripts/pangan_daily_rebuild.py 每日 cron 增量维护, 覆盖 ~45 票)。
    覆盖外标的返回 None → 调用方回落线性模型。
    """
    try:
        _day = datetime.now(_now_et()).strftime("%Y-%m-%d")
        _hit = _VOL_CURVE_CACHE.get(symbol)
        if _hit is not None and _hit[0] == _day:
            return _hit[1]
        _p = (Path(__file__).resolve().parents[2] / "data" / "replay_bars_1m"
              / f"{symbol.replace('.', '_')}.json")
        if not _p.exists():
            _VOL_CURVE_CACHE[symbol] = (_day, None)
            return None
        with open(_p, encoding="utf-8") as _f:
            _bars = json.load(_f)
        _by_day: Dict[str, List[float]] = {}
        for _b in _bars:
            try:
                _by_day.setdefault(str(_b[0])[:10], []).append(float(_b[5]))
            except (IndexError, TypeError, ValueError):
                continue
        # 只取历史已完成交易日 (排除今日, 当日曲线不完整会污染中位数)
        _dates = sorted(d for d in _by_day if d < _day)[-days:]
        if len(_dates) < 10:  # 少于 10 日样本的中位数不稳, 回落线性
            _VOL_CURVE_CACHE[symbol] = (_day, None)
            return None
        _cums = []
        for _d in _dates:
            _run, _acc = 0.0, []
            for _v in _by_day[_d]:
                _run += _v
                _acc.append(_run)
            _cums.append(_acc)
        _curve = []
        for _i in range(max(len(c) for c in _cums)):
            # 半日市等短日在该 index 无值 → 只用有值的日 (不补零, 否则中位数被拉低)
            _vals = sorted(c[_i] for c in _cums if _i < len(c))
            if not _vals:
                break
            _n = len(_vals)
            _curve.append(_vals[_n // 2] if _n % 2 else
                          (_vals[_n // 2 - 1] + _vals[_n // 2]) / 2.0)
        _VOL_CURVE_CACHE[symbol] = (_day, _curve or None)
        return _curve or None
    except Exception:
        _VOL_CURVE_CACHE[symbol] = (datetime.now(_now_et()).strftime("%Y-%m-%d"), None)
        return None


def _calc_intraday_vol_ratio(data_router, symbol: str,
                             realtime_cum_vol: Optional[float] = None,
                             use_p50_curve: Optional[bool] = None) -> Optional[float]:
    """计算盘中实时量能比 (intraday volume ratio).

    分母两条路径 (P1-3, 2026-08-23):
      主: today_cumulative_vol / 历史同分钟累积量 p50 (_intraday_vol_curve_p50, ~45票)
      回落: today_cumulative_vol / (avg_daily_vol_5d × elapsed_fraction)  ← 原线性模型
      - today_cumulative_vol: 今日开盘至当前的累计成交量
      - elapsed_fraction: 已过交易时间占全天比例 (9:30-16:00 ET = 390min)

    Args:
        use_p50_curve: True=强制走 p50, False=强制走线性, None=从 config 读开关。

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

        # ── 2. 分母: 已过交易时间 ──
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

        # ── 2a. P1-3 (2026-08-23): VWAP-relative — 分母改用历史同分钟累积量 p50 ──
        #   原线性折算 (日均量 × elapsed/390) 忽略 U 型分布, 开盘时段分母系统性偏小 →
        #   ratio 虚高 → 门在最该确认的时段恒开 (见 _intraday_vol_curve_p50 docstring)。
        #   ⚠️ 本分支会实质改变量能门行为 (阈值 0.8 从"几乎恒过"变为真实过滤), 这是修门的
        #   目的而非副作用。覆盖外标的 (~45 票之外) 自动回落线性, 故行为变化仅限有曲线的票。
        #   回滚: volume_gate_vwap_relative 改 false。
        if use_p50_curve is not None:
            _use_curve = use_p50_curve
        else:
            try:
                from app.core.config_container import get_config as _gc_vc
                _use_curve = bool(_gc_vc().get_dotted(
                    "playbook.execution_parameters.volume_gate_vwap_relative", True))
            except Exception:
                _use_curve = False
        if _use_curve:
            _curve = _intraday_vol_curve_p50(symbol)
            # bar i (09:31) 覆盖开盘后第 1 分钟 → 开盘后 _elapsed 分钟对应 index _elapsed-1
            _idx = min(max(_elapsed - 1, 0), (len(_curve) - 1) if _curve else 0)
            if _curve and _curve[_idx] > 0:
                return round(today_cum_vol / _curve[_idx], 3)

        # ── 2b. 回落: 最近5日日均成交量 × 时间占比 (线性模型) ──
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

        elapsed_fraction = min(max(_elapsed / _total, 0.02), 1.0)

        # ── 3. 计算比值 ──
        expected_vol = avg_daily_vol * elapsed_fraction
        if expected_vol <= 0:
            return None
        ratio = today_cum_vol / expected_vol
        return round(ratio, 3)
    except Exception:
        return None


# P1-2 (2026-08-23, DEB-20260823-phase1): 永久作废指令的 skip_reason 取值集合 —
#   原先只有 'thesis_drift_invalidate' 一个值, 硬编码在 auto_execute 头检与
#   _check_intraday_trigger 头检两处; 新增 tripwire 核销 (scripts/tripwire_checker.py)
#   后需要第二个可归因的值, 集中定义避免两处守卫漏更新。
# [DEB-20260901 选项1②] archetype 路由三种终态跳过并入集合 —
#   TTL 过期放弃的指令不得下轮重触发重挂 (thesis 首腿窗口已过)。
INVALIDATED_SKIP_REASONS = frozenset({
    "thesis_drift_invalidate", "TRIPWIRE_INVALIDATED",
    "archetype_ttl_expired", "archetype_level_conflict", "archetype_order_dead",
})


def _calc_vwap_slope(data_router, symbol: str, window: int = 5) -> Optional[float]:
    """P1-1 证伪门: 滚动窗 VWAP 斜率 (最近 window 根 1min bar 相对前 window 根的变化率)。

    与既有 VWAP 方向门 (:4837 当日累计 VWAP 的哪一侧) 正交 — 后者问"价在均价哪侧",
    本函数问"volume-weighted 价近 5min 在往哪走"。用当日累计 VWAP 算斜率是错的:
    d(累计VWAP)/dt 的符号恒等于 sign(近期价 - 累计VWAP), 与方向门同信息量。

    VWAP 优先用 turnover/volume (Futu 原生成交额, 真 VWAP); 缺 turnover 回落
    典型价 (h+l+c)/3 × volume。

    Returns: float 变化率 (BUY 期望 >0) / None 数据不足 → 调用方 fail-open。
    """
    try:
        _futu = getattr(data_router, "futu", None)
        if _futu is None or not hasattr(_futu, "get_kline"):
            return None
        # count=800 与 _calc_intraday_vol_ratio 同源理由: HTTP 冷路径 start=T-1 返回
        #   "最旧N根", count 小于范围总数会拿到纯 T-1 bar。
        bars = _futu.get_kline(symbol, ktype="K_1M", count=800) or []
        _today = datetime.now(_now_et()).strftime("%Y-%m-%d")
        today_bars = [b for b in bars
                      if str(b.get("time") or b.get("time_key") or "")[:10] == _today]
        if len(today_bars) < window * 2:
            return None

        def _vwap(seg):
            _num = _den = 0.0
            for b in seg:
                _v = float(b.get("volume", 0) or 0)
                if _v <= 0:
                    continue
                _to = float(b.get("turnover", 0) or 0)
                _num += _to if _to > 0 else ((float(b.get("high", 0) or 0)
                                              + float(b.get("low", 0) or 0)
                                              + float(b.get("close", 0) or 0)) / 3.0) * _v
                _den += _v
            return (_num / _den) if _den > 0 else None

        _recent = _vwap(today_bars[-window:])
        _prior = _vwap(today_bars[-window * 2:-window])
        if not _recent or not _prior or _prior <= 0:
            return None
        return (_recent - _prior) / _prior
    except Exception:
        return None


def _capital_flow_divergence(symbol: str, is_short: bool) -> Optional[bool]:
    """P1-1 资金流背离判定 (record-only 影子专用, 不参与拦截)。

    用户裁决 (2026-08-23): 本信号已有回放判定且四关门全不过
    (data/capital_flow_replay_verdict.json: inflow t=-0.48 / outflow t=-0.70 /
    placebo p=0.85 / IS-OOS 符号翻转) → 不得作为硬拦门, 先 record-only 攒样,
    够样本后按同一四关门判据重裁是否转生产。

    背离定义: BUY 但大单净流为负 / SHORT 但大单净流为正 (大单 = super + big)。
    Returns: True 背离 / False 同向 / None 无数据。
    """
    try:
        _day = datetime.now(_now_et()).strftime("%Y-%m-%d")
        if _CAPITAL_FLOW_CACHE.get("day") != _day:
            import json as _cfj
            _by_symbol: Dict[str, Any] = {}
            _p = Path(__file__).resolve().parents[2] / "data" / "capital_flow_daily.jsonl"
            if _p.exists():
                with open(_p, encoding="utf-8") as _f:
                    for _line in _f:
                        try:
                            _r = _cfj.loads(_line)
                        except Exception:
                            continue
                        _sym, _d = _r.get("symbol"), str(_r.get("date", ""))
                        if not _sym:
                            continue
                        _prev = _by_symbol.get(_sym)
                        if _prev is None or _d >= _prev[0]:
                            _by_symbol[_sym] = (_d, float(_r.get("super_in_flow", 0) or 0)
                                                + float(_r.get("big_in_flow", 0) or 0))
            _CAPITAL_FLOW_CACHE.clear()
            _CAPITAL_FLOW_CACHE.update({"day": _day, "by_symbol": _by_symbol})
        _hit = (_CAPITAL_FLOW_CACHE.get("by_symbol") or {}).get(symbol)
        if _hit is None or _hit[1] == 0:
            return None
        return (_hit[1] > 0) if is_short else (_hit[1] < 0)
    except Exception:
        return None


# 资金流影子: capital_flow_daily.jsonl 的日级解析缓存 {day, by_symbol:{sym:(date,net)}} —
#   盘中每标的每轮调用, 不缓存则每轮重复全文件扫描.
_CAPITAL_FLOW_CACHE: Dict[str, Any] = {}


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

# RFC-065 widen 影子 (DEB-20260819 §三十四): (inst_id, date) 去重 — 只记最早一次 widen_eligible.
#   重启清零 + 文件兜底查重 (同 _shadow_portfolio_emit 模式), 冗余行由结算端按 inst_id 去重.
_WIDEN_SHADOW_EMITTED: set = set()


def _emit_widen_shadow(inst, symbol: str, is_short: bool, widen_path: str,
                       current_price: float, entry_price: float,
                       knife_pct: float, chase_pct: float,
                       flat_triggered: bool, flat_path: str, vol_gate_block: bool):
    """RFC-065 widen 影子登记 (record-only, 零执行影响). §32.3 必记字段全含.

    结算口径 (离线): 对照指令实际触发时点 —
      flat 从未触发            → 事件类型 = new (widen 新增)
      flat 触发但晚于本记录时点 → 事件类型 = changed (变更触发, F1 通道)
    任何异常静默 — 影子绝不影响触发判定.
    """
    try:
        if "TEST" in str(symbol).upper():
            return
        _d = _today_et_str()
        _lk = (inst.get("id"), _d)
        if _lk in _WIDEN_SHADOW_EMITTED:
            return
        import json as _jws
        from datetime import datetime as _dtws
        from pathlib import Path as _Pws
        _fp = _Pws(__file__).resolve().parents[2] / "data" / "entry_band_widen_shadow.jsonl"
        # 文件兜底查重 (重启后 set 清空, 启动后首次落盘前查一次)
        if _fp.exists():
            try:
                with open(_fp, "r", encoding="utf-8") as _fchk:
                    for _ln in _fchk:
                        try:
                            _r = _jws.loads(_ln)
                            if _r.get("inst_id") == inst.get("id") and _r.get("date") == _d:
                                _WIDEN_SHADOW_EMITTED.add(_lk)
                                return
                        except Exception:
                            continue
            except Exception:
                pass
        _WIDEN_SHADOW_EMITTED.add(_lk)
        _now = _dtws.now(_now_et())
        _e = float(entry_price or 0)
        _c = float(current_price or 0)
        _row = {
            "ts": _now.isoformat(timespec="seconds"),
            "date": _d,
            "trigger_hhmm": _now.strftime("%H:%M"),   # §32.3: 触发时点 (第一协变量)
            "inst_id": inst.get("id"),
            "symbol": symbol,
            "direction": "SHORT" if is_short else "BUY",
            "atr_pct": inst.get("atr_pct"),            # §32.3: ATR
            "entry_price": round(_e, 4),
            "current_price": round(_c, 4),
            "gap_pct": round(abs(_c - _e) / _e * 100.0, 3) if _e > 0 else None,  # 触发gap
            "widen_path": widen_path,                  # pullback/chase
            "widen_knife_pct": round(knife_pct * 100, 2),
            "widen_chase_pct": round(chase_pct * 100, 2),
            "flat_triggered": bool(flat_triggered),    # 结算分类依据 (new vs changed)
            "flat_path": flat_path,
            "vol_gate_block": bool(vol_gate_block),
        }
        with open(_fp, "a", encoding="utf-8") as _f:
            _f.write(_jws.dumps(_row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 影子纯记录, 绝不影响触发

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
        if not pb_cfg.get("liquidity_gate_enabled", True):
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
            _notional = _cap * float(pb_cfg.get("max_position_pct", 0.06) or 0.06)
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
    #   特异性匹配作废语义取值 (P1-2 起集合化), 不误伤其他 skip_reason 语义.
    if (inst.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
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
            _vps_fp = _vps_path(__file__).resolve().parents[2] / "data" / "vol_gate_da_p50_shadow.jsonl"
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
                                inst.get("id"), data_router=data_router):
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
                    with open(Path(__file__).resolve().parents[2] / "data"
                              / "falsification_gate_shadow.jsonl", "a", encoding="utf-8") as _ff:
                        _ff.write(json.dumps({
                            "ts": datetime.now(_now_et()).isoformat(timespec="seconds"),
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
                _le_path = _Path(__file__).resolve().parents[2] / "data" / "limit_entry_shadow.jsonl"
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
                    with open(_P2(__file__).resolve().parents[2] / "data" / "limit_entry_shadow.jsonl", "a", encoding="utf-8") as _f2:
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
                _pb_path = _Ppb(__file__).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
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


def _net_pnl_pair(pnl: float, pnl_pct: float, fees_total, tr_row):
    """毛PnL→净 (pnl, pnl_pct) 对; 费用缺失回退毛值 (fail-open).

    tr_row: trade_records 行快照, [2]=entry_price [4]=quantity (与 _close_trade_record 同序).
    pnl_pct 为 ratio 口径; 净 ratio = 毛 ratio - fees/notional. 2026-08-20 净口径切换 (用户裁"切").
    """
    _net = round(float(pnl), 2)
    _net_pct = round(float(pnl_pct), 4)
    try:
        _f = float(fees_total) if fees_total is not None else 0.0
        if _f > 0:
            _net = round(float(pnl) - _f, 2)
            if tr_row is not None:
                _notional = float(tr_row[2] or 0) * float(tr_row[4] or 0)
                if _notional > 0:
                    _net_pct = round(float(pnl_pct) - _f / _notional, 4)
    except Exception:
        pass
    return _net, _net_pct


def classify_be_exit(be_net: Optional[float], be_drift: Optional[float]) -> str:
    """BE 出场分类纯函数 (2026-08-21, Qoder §56.3 判序修复后口径).

    判序: OK 先 (净额正即名实相符) → GAP (坠穿≥5bp) → SLIP (其余).
    旧序 GAP 先判致 BWXT drift=-15.44bp 但净+8.82 被标瀑布自相矛盾.
    telemetry 缺失 (None) → 按净额回退 OK/SLIP (fail-open). 契约测试见
    tests/test_be_exit_class.py.
    """
    if be_net is not None and be_net > 0:
        return "OK"
    if be_drift is not None and float(be_drift) <= -5.0:
        return "GAP"
    return "SLIP"


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
            get_absolute_circuit_breaker().record_trade_result("playbook", float(_acb_pnl), symbol)
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
    if symbol:
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
                    "time_to_mfe_trading_min": _meta.get("time_to_mfe_trading_min"),
                    "cohort": _meta.get("cohort"),
                    "horizon": _meta.get("horizon"),
                    "inv_price": _meta.get("alite_inv_price"),
                    # 2026-08-21 (用户裁"落"): BE 出场分类 — 通知直接区分"真保本/贴线残差/瀑布坠穿",
                    #   消除"保本名义亏损"歧义. drift 仅 GAP 路径有意义 (bps, 负=坠穿).
                    "be_exit_class": _meta.get("be_exit_class"),
                    "be_drift_bps": (_meta.get("_be_close_telemetry") or {}).get("drift_from_be_line"),
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


# factor_exit 缺规则 WARN 去重集 _factor_exit_warned 已随本体迁入 exit_engine (P2a)。
def _get_factor_exit_config(conditions: list, factor_rules: dict, direction: str = None) -> dict:
    """根据触发因子选择对应的退出规则配置 (Phase 3 2026-06-30).

    P2a (2026-09-06) 起本体在 app.playbook.exit_engine (逐字迁入)。本转发器保留
    供影子调用点 (:8695) 与既有测试 (test_factor_exit_direction_fallback)。
    """
    from app.playbook.exit_engine import _get_factor_exit_config as _impl
    return _impl(conditions, factor_rules, direction=direction)


# _compute_dynamic_effective_sl 本体已迁入 app.playbook.exit_engine (P2a, 2026-09-06, 逐字)。
# 唯一生产调用点随 _check_playbook_exit_rules 同迁; exit_shadow.py 内为有意冻结的
# "忠实复刻"副本 (影子铁律: 只评更紧变体, 不 import 生产实现), 不随动。


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
      - 其他未知异常 (2026-08-23 R-7a 改为同样 fail-closed): 原实现 return False = 主线接管,
        但本函数返回 False 的语义就是"主线可以平这个仓", 故一个 schema/路径级结构性错误
        即可让主线对全部标的接管 → 双线管理 (2026-06-24 三笔 SL_ATR_STOP -$2,416 即护栏
        缺位期实证). 原注释顾虑"fail-close 会让主线对所有 symbol 永久失管、漏平真实风险仓",
        该顾虑今已弱化: Playbook 有独立退出宿主 monitor_playbook_sim_exits, 闸门失效时
        "主线接管" 严格劣于 "主线跳过 + 大声告警". 遗留/手动仓的代价由 CRITICAL 告警承接.
        回滚: risk.playbook_guard_fail_mode=open.
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
        try:
            from app.core.config_container import get_config as _get_cfg_pbg
            _pbg_mode = str(_get_cfg_pbg().get_dotted("risk.playbook_guard_fail_mode", "closed")).lower()
        except Exception:
            _pbg_mode = "closed"
        if _pbg_mode == "open":
            logger.error(
                f"[playbook护栏] is_playbook_open_position({symbol}) 未知异常: {e} "
                f"→ fail-OPEN (return False, 主线接管) — 由 risk.playbook_guard_fail_mode=open 指定"
            )
            return False
        logger.critical(
            f"🚨 [playbook护栏·fail-close] is_playbook_open_position({symbol}) 未知异常: {e} "
            f"→ return True (主线跳过). 排他性闸门已失效, 遗留/手动仓本轮不受主线管理, "
            f"请立即排查 DB schema/路径。"
        )
        return True
    finally:
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass


def load_playbook_open_symbols():
    """一次查询返回全部未平 Playbook 标的集合 (与 is_playbook_open_position 同口径).

    2026-08-22 audit: 循环内逐 symbol 调 is_playbook_open_position 是 N+1 连接
    (ghost清理/新仓同步/对账/主线退出扫描 每30-300s一轮 × 每仓一连接).
    调用方应在循环外预载本函数, 循环内用 playbook_open_check() 判定.

    Returns:
        set[str] 未平 Playbook 标的集合; None = 查询失败 —
        调用方必须回退逐 symbol 的 is_playbook_open_position 以保留其
        分层 fail-closed/fail-open 语义 (bulk 层不代行语义决策).
    """
    import sqlite3 as _pb_sql_bulk
    from app.core.paths import get_trade_records_db
    _c = None
    try:
        _c = _pb_sql_bulk.connect(str(get_trade_records_db()), timeout=5)
        _rows = _c.execute(
            "SELECT DISTINCT symbol FROM trade_records "
            "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)"
        ).fetchall()
        return {r[0] for r in _rows if r[0]}
    except Exception as e:
        logger.warning(f"[playbook护栏] load_playbook_open_symbols 查询失败, 调用方回退逐symbol判定: {e}")
        return None
    finally:
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass


def playbook_open_check(symbol: str, preloaded) -> bool:
    """配合 load_playbook_open_symbols 使用的循环内判定.

    preloaded 为 None (预载失败) 时回退逐 symbol 判定, 保留原 fail-closed 语义;
    否则纯内存集合命中测试.
    """
    if preloaded is None:
        return is_playbook_open_position(symbol)
    return symbol in preloaded

# 规则0.5/规则3.3 日志去重集 (_EARLY_TIGHT_SL_LOGGED/_LOSER_DS_EXEMPT_LOGGED)
#   已随 _check_playbook_exit_rules 迁入 app.playbook.exit_engine (P2a, 2026-09-06)。


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


# ── 耐心出场 (2026-08-13, DEB-20260813 §十六/§18.3, 用户裁"直接实现 sim 观察") ──
#   DEAD_SIGNAL_EXIT 触发且信号时刻浅水下 (0~max_underwater_pct) 时, 不立即市价卖,
#   转为轮询等待: 价格站回入场价 → 市价卖 (fill, 保本退出); 水下加深至 stop_pct →
#   立即市价卖 (stop); 到 deadline_hhmm (1550, 先于 PRE_CLOSE 15:52) → 强制卖 (eod).
#   证据: 51笔死单轮询级回放 +$107~112 ≈ +$6/日, δ扫描严格保本(δ=0)最优 (让价单调劣化),
#   止损 1.0/1.5/2.0 三档稳健 (§14/§19 仲裁后口径). 状态持久化于 meta["patient_exit"],
#   重启安全. 整条路径由 patient_exit_enabled 单开关门控, 回滚: false + 重启.
def patient_exit_arm_eligible(exit_reason, profit_pct, meta, pb_cfg) -> bool:
    """纯函数: DEAD_SIGNAL_EXIT 是否应转入耐心等待而非立即市价卖.

    作用域 (§18.3 六条规格之 1): 信号时刻水下 ∈ [0, max_underwater_pct] —
      profit_pct ∈ [-max, 0]; profit_pct > 0 (微盈) 无"等回本"必要, 照常立即退出.
    已 armed 的仓位不得重复 arm (守卫).
    """
    if not bool(pb_cfg.get("patient_exit_enabled", False)):
        return False
    if exit_reason != "DEAD_SIGNAL_EXIT":
        return False
    if meta.get("patient_exit"):
        return False
    # expire 墓碑: 跨日作废的等待态不得重新 arm (防无限跨日等待循环)
    if meta.get("_patient_exit_expired"):
        return False
    try:
        _max_uw = float(pb_cfg.get("patient_exit_max_underwater_pct", 1.0))
        return -_max_uw <= float(profit_pct) <= 0.0
    except Exception:
        return False


def patient_exit_eval_wait(meta, direction, entry_price, current_price, now_et, pb_cfg):
    """纯函数: 评估耐心等待中仓位的三条退出腿.

    Returns: (mode, detail)
      mode="":      继续等待
      mode="fill":  价格站回净保本线 (profit_pct ≥ 往返费率, 8/17 净口径) → 净保本退出
      mode="stop":  水下加深至 patient_exit_stop_pct → 保护止损
      mode="eod":   到达 patient_exit_deadline_hhmm → 强制兜底
      mode="expire": 等待状态跨交易日 → 作废 (隔夜仓次日水下是另一回事, 回正常规则链)
    """
    _pe = meta.get("patient_exit") or {}
    if not _pe:
        return "", ""
    # 跨日守卫: 等待状态仅当日有效
    try:
        _armed_day = str(_pe.get("armed_day", ""))
        _today = now_et.strftime("%Y-%m-%d")
        if _armed_day and _armed_day != _today:
            return "expire", "跨交易日, 等待状态作废"
    except Exception:
        pass
    try:
        _stop_pct = float(pb_cfg.get("patient_exit_stop_pct", 1.5))
        _deadline = int(pb_cfg.get("patient_exit_deadline_hhmm", 1550))
    except Exception:
        _stop_pct, _deadline = 1.5, 1550
    if entry_price and entry_price > 0 and current_price and current_price > 0:
        if direction == "LONG":
            _profit_pct = (current_price - entry_price) / entry_price * 100.0
        else:
            _profit_pct = (entry_price - current_price) / entry_price * 100.0
        if _profit_pct <= -_stop_pct:
            return "stop", f"水下 {_profit_pct:.2f}% ≤ -{_stop_pct:.1f}% 保护止损"
        # 8/17 净口径: fill 腿"站回保本"必须是净保本线 (entry×(1±费率)), 不是入场价 —
        #   正好在入场价成交 = 毛利0, 白交一趟往返费 (8月已有 PATIENT_EXIT_FILL 实弹).
        #   cushion 取自 meta._be_cushion_fee_aware (arm 轮规则链算出并写回); 算不出→0=原行为.
        try:
            _fill_cushion = float((meta or {}).get("_be_cushion_fee_aware", 0) or 0)
        except Exception:
            _fill_cushion = 0.0
        if _profit_pct >= _fill_cushion:
            return "fill", f"价格站回净保本线 ({_profit_pct:.2f}% ≥ {_fill_cushion:.2f}%), 净保本退出"
    try:
        _hhmm = now_et.hour * 100 + now_et.minute
        if _hhmm >= _deadline:
            return "eod", f"{_hhmm:04d} ≥ {_deadline:04d} 强制兜底"
    except Exception:
        pass
    return "", ""


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str):
    """共用退出规则判断 — P2a (2026-09-06) 起本体在 app.playbook.exit_engine
    (711 行逐字迁入, 零行为变更)。本转发器保留: 10+ 既有测试文件直引本路径,
    生产调用点 monitor_playbook_sim_exits 经本转发器到达, 判定语义不变。
    """
    from app.playbook.exit_engine import _check_playbook_exit_rules as _impl
    return _impl(direction, entry_price, current_price, initial_sl, peak_price, meta,
                 held_minutes, minutes_to_close, minutes_from_open, entry_time_str)


def _load_horizon_config(horizon: str) -> Dict[str, Any]:
    """读取指定 horizon 的配置参数.

    P2a (2026-09-06) 起本体在 app.playbook.exit_engine (逐字迁入)。本转发器保留供
    _sl_fallback_exit_reason 与既有测试 (monkeypatch 落点在本模块全局, 语义不变)。
    """
    from app.playbook.exit_engine import _load_horizon_config as _impl
    return _impl(horizon)


def _sl_fallback_exit_reason(pnl_pct: float, entry_time_str: str, horizon: str) -> str:
    """止损兜底平仓的 exit_reason 归因 (2026-08-28 全链路复查).

    SL 兜底跑在 _check_playbook_exit_rules 规则2 之前, 跳空打穿 SL 的隔夜仓因此全部
    被记成 SL_INITIAL, OVERNIGHT_GAP_STOP 桶全史 0 笔 → 持夜风险被归因给"止损纪律",
    并沿 pattern-mining 变成标的档案里的 "LONG+SL_INITIAL 亏钱模式" 教训。
    这里用与规则2 相同的尺子重贴标签: 隔夜仓 且 亏损幅度已达该 horizon 的
    overnight_gap_stop_pct → OVERNIGHT_GAP_STOP; 其余 (含任何异常) 保持 SL_INITIAL。

    只影响记账归因, 不影响平仓动作; 两值同在 intraday_reentry._HARD_STOP_REASONS,
    再入场门行为中性。
    """
    try:
        _gap_stop_pct = float(_load_horizon_config(horizon).get("overnight_gap_stop_pct", 3.0))
        if float(pnl_pct) <= -_gap_stop_pct and _is_overnight_position(entry_time_str):
            return "OVERNIGHT_GAP_STOP"
    except Exception as e:
        logger.debug(f"[playbook sim] 跳空归因判定失败 (按 SL_INITIAL 记账): {e}")
    return "SL_INITIAL"


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
    """判断 long horizon 仓位是否已到 max_hold_days 交易日.

    P1 (2026-09-06) 起本体在 app.playbook.overnight_decision.horizon_max_hold_hit
    (判定语义逐字未动)。本转发器保留供 MAX_HOLD 规则引用。
    """
    try:
        from app.playbook.overnight_decision import horizon_max_hold_hit
        return bool(horizon_max_hold_hit(entry_time_str, max_hold_days))
    except Exception:
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


def _portfolio_backstop_evaluate(rows, batch_quotes: dict, cfg: dict):
    """G-01/A9 (2026-09-05 审计): playbook 组合级敞口聚合 — 纯函数 (可单测)。

    输入 trade_records 未平行快照 + 批量报价 + 配置; 输出聚合敞口与阈值 dict,
    不可评估 (无任何有价标的) 时返回 None。抽成纯函数的理由: 风险阈值若不可单测,
    就会像 G-03 (小时熔断每日仅一次) 那样长期带病存活。

    阈值口径 (刻意宽于噪声, 吸取 2026-08-21 AMZN 误杀教训):
      threshold = max(min_abs_loss_usd, Σ逐仓SL距离 × sl_loss_multiple)
    Σ SL 距离 = "所有逐仓 SL 同时按设计触发"的合计损失 → 乘倍数后本兜底只在逐仓 SL
    **集体失效**时才可能开火, 结构上不可能被盘中噪声触发 (AMZN 案: 水深上限被开盘噪声
    打穿即清仓, 30min 后价格回 +1.1%, -5% 的 SL 从未参与 → 用户裁撤销该上限)。

    保守口径: 批量报价未命中/无价的标的**既不计入浮亏也不计入成本与 ΣSL** — 宁少算敞口,
    也不用陈旧价触发全平 (缺价标的已由逐仓路径 R2 连续取价失败告警覆盖)。
    浮亏逐行按 direction 定号后求和 (SHORT 反向), 不用毛市值差 — 否则空头盈亏符号翻转
    (盈利当亏损→误开火, 亏损当盈利→永不开火), 与 H-06 同族。
    单行异常只跳过该行, 不污染整体聚合。
    """
    cost = 0.0
    mkt = 0.0
    sl_sum = 0.0
    unrealized = 0.0
    n_priced = 0
    n_unusable = 0
    last_err = ""
    for r in rows:
        try:
            entry_price = float(r["entry_price"] or 0)
            qty = float(r["quantity"] or 0)
            if entry_price <= 0 or qty <= 0:
                continue
            quote = (batch_quotes or {}).get(r["symbol"])
            if not (isinstance(quote, dict) and (quote.get("last") or quote.get("price"))):
                continue
            price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
            if price <= 0:
                continue
            # 方向感知 (H-06 同族: "非 LONG 一律按 SHORT" 曾致盈亏反转)。账本实测 direction
            #   仅 LONG/SHORT 两值 (388/85), 未知值按 LONG 兜底 = position_exit_monitor 同款默认。
            _is_short = str(r["direction"] or "").strip().upper() in ("SHORT", "SELL")
            cost += entry_price * qty
            mkt += price * qty
            n_priced += 1
            # 浮亏必须逐行按方向定号再求和: mkt-cost 只对 LONG 成立, SHORT 符号相反
            #   (空头 price<entry 是盈利)。用毛市值差聚合会把空头盈利算成亏损 → 兜底误开火,
            #   把空头亏损算成盈利 → 兜底永不开火。两个方向都是钱路径上的错。
            unrealized += ((entry_price - price) if _is_short else (price - entry_price)) * qty
            try:
                meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                meta = {}
            sl = float(meta.get("stop_loss", 0) or 0)
            if sl > 0:
                if not _is_short:
                    sl_sum += max(0.0, (entry_price - sl) * qty)
                else:
                    sl_sum += max(0.0, (sl - entry_price) * qty)
        except Exception as _row_err:
            # 单行异常只跳过该行, 但**必须留痕** (except:pass 棘轮纪律 / R5 静默死亡病灶族):
            #   计数 + 末条错误随聚合结果上交, 由调用方 warn — 否则"敞口被少算"与
            #   "本来就没有敞口"不可区分 (守卫自己坏了却报零违例)。
            n_unusable += 1
            last_err = f"{type(_row_err).__name__}: {_row_err}"
            continue
    if n_priced <= 0 or cost <= 0:
        return None
    threshold = max(float(cfg.get("min_abs_loss_usd", 500.0) or 0.0),
                    sl_sum * float(cfg.get("sl_loss_multiple", 1.5) or 0.0))
    return {
        "n_priced": n_priced,
        "cost_usd": cost,
        "market_usd": mkt,
        "unrealized_usd": unrealized,
        "unrealized_pct": (unrealized / cost * 100.0) if cost else 0.0,
        "sl_loss_sum_usd": sl_sum,
        "threshold_usd": threshold,
        "breached": (-unrealized) >= threshold,
        "n_unusable_rows": n_unusable,
        "unusable_last_err": last_err,
    }


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
        from app.core.scheduler_triggers import ET as _et
        # 注: datetime 用模块级 import (顶部 L23) — 局部 import 会遮蔽模块名,
        #     使测试无法经 pipeline_mod.datetime 冻结时钟 (2026-09-02 钉修复配套).

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
            logger.warning(f"[playbook组合兜底] 配置读取失败 → 用内置保守默认: {_bs_cfg_e}")
            _bs_raw = {}
        _bs_cfg = {"enabled": True, "sl_loss_multiple": 1.5, "min_abs_loss_usd": 500.0,
                   "cooldown_sec": 300.0}
        if isinstance(_bs_raw, dict):
            for _bs_k in list(_bs_cfg):
                if _bs_k in _bs_raw:
                    _bs_cfg[_bs_k] = _bs_raw[_bs_k]

        if _bs_cfg.get("enabled") and _batch_quotes:
            # 聚合与阈值判定走纯函数 _portfolio_backstop_evaluate (可单测);
            # 本处只保留告警动作 (需 logger/push, 不入纯函数)。
            _bs_agg = _portfolio_backstop_evaluate(rows, _batch_quotes, _bs_cfg)
            if _bs_agg is not None:
                _bs_n = _bs_agg["n_priced"]
                # 脏行留痕 (棘轮纪律): 仅在计数变化时 warn, 避免 20s 快环刷屏
                _bs_bad = _bs_agg.get("n_unusable_rows") or 0
                if _bs_bad and _bs_bad != getattr(monitor_playbook_sim_exits, "_bs_last_bad_rows", 0):
                    monitor_playbook_sim_exits._bs_last_bad_rows = _bs_bad
                    logger.warning(
                        f"[playbook组合兜底] {_bs_bad} 行无法计入聚合 (敞口按保守少算) | "
                        f"末条错误: {_bs_agg.get('unusable_last_err')}")
                _bs_cost = _bs_agg["cost_usd"]
                _bs_mkt = _bs_agg["market_usd"]
                _bs_sl_sum = _bs_agg["sl_loss_sum_usd"]
                _bs_unreal = _bs_agg["unrealized_usd"]
                _bs_thresh = _bs_agg["threshold_usd"]
                # 判定复用纯函数的 breached 键 (判定只此一处, 防双写分裂);
                # cooldown_sec 加 or 兜底 — yaml 误配 null 时 float(None) 会炸当轮监控循环。
                if _bs_agg["breached"]:
                    _bs_now_ts = time.time()
                    _bs_last_ts = getattr(monitor_playbook_sim_exits, "_bs_last_alert_ts", 0.0)
                    if _bs_now_ts - _bs_last_ts >= float(_bs_cfg.get("cooldown_sec") or 300.0):
                        monitor_playbook_sim_exits._bs_last_alert_ts = _bs_now_ts
                        _bs_pct = (_bs_unreal / _bs_cost * 100.0) if _bs_cost else 0.0
                        logger.critical(
                            f"🚨 [playbook组合兜底] {_bs_n} 仓合并浮亏 ${-_bs_unreal:,.2f} "
                            f"({_bs_pct:.2f}%) ≥ 阈值 ${_bs_thresh:,.2f} "
                            f"(Σ逐仓SL距离 ${_bs_sl_sum:,.2f} × {_bs_cfg['sl_loss_multiple']}, "
                            f"绝对下限 ${_bs_cfg['min_abs_loss_usd']:,.0f}) — 逐仓 SL 已集体失效, "
                            f"成本 ${_bs_cost:,.0f} 市值 ${_bs_mkt:,.0f}"
                        )
                        try:
                            from app.push import get_push_hub
                            from app.push.message import Level as _BsLevel
                            get_push_hub().push_text(
                                f"🚨 [Playbook组合兜底] {_bs_n} 仓合并浮亏 ${-_bs_unreal:,.2f} "
                                f"({_bs_pct:.2f}%) 越过组合阈值 ${_bs_thresh:,.2f}. "
                                f"逐仓 SL 未能兜住, 请人工核查是否全平. "
                                f"(G-01/A9, 阈值=ΣSL距离×{_bs_cfg['sl_loss_multiple']})",
                                level=_BsLevel.HIGH,
                            )
                        except Exception as _bs_push_e:
                            logger.warning(f"[playbook组合兜底] 告警推送失败: {_bs_push_e}")

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
            _quote_src = ""  # 2026-08-18 报价源遥测 (WDC/AMD 案: 触发价与1分K线矛盾待对账)
            try:
                quote = _batch_quotes.get(symbol)
                if not (isinstance(quote, dict) and (quote.get("last") or quote.get("price"))):
                    quote = data_router.get_quote(symbol)
                    _quote_src = "single_fallback"
                if quote and isinstance(quote, dict):
                    current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
                    if not _quote_src:
                        _quote_src = str(quote.get("data_source", "batch"))
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
                    # 2026-08-13 (耐心出场): armed 仓被灾难 SL 兜底关闭 — 标注 exit_mode=sl_override
                    #   (优先级正确: 灾难保护先于耐心等待), patient_exit dict 本身已随开环读保留.
                    if isinstance(_sl_meta.get("patient_exit"), dict) and not _sl_meta["patient_exit"].get("exit_mode"):
                        _sl_meta["patient_exit"]["exit_mode"] = "sl_override"
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
                    # 2026-08-28: 隔夜跳空打穿 SL 改归 OVERNIGHT_GAP_STOP (根因见
                    #   _sl_fallback_exit_reason docstring) — 只改归因, 平仓动作不变.
                    _sl_reason = _sl_fallback_exit_reason(_pnl_pct, r["entry_time"], meta.get("horizon", "short"))
                    _close_trade_record(conn, r["id"], _exit_price, _sl_reason,
                                        _pnl, _pnl_pct / 100, _hh, inst_id=inst_id, symbol=symbol,
                                        close_futu_order_id=_sl_close_futu_id)
                    exited += 1
                    logger.info(f"[playbook sim] ✅ {symbol} {_sl_reason} 兜底: exit=${_exit_price:.2f} src={_sl_src} pnl=${_pnl:.2f}")
                    continue

            if abs(current_price - entry_price) / entry_price > 0.15:
                continue  # 价格异常跳过 (2026-07-02: 50%→15%, 50%让巨亏/巨盈仓位失保护)

            # cohort B skip 判定 (sim 特有: LIVE 需显式开 overnight)
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
                        _armed_dt = datetime.fromisoformat(_armed_iso)
                        _now_ref_pe = datetime.now(_armed_dt.tzinfo) if _armed_dt.tzinfo else now_et
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
                )
                # ── 耐心出场 arm (§18.3-1): DEAD_SIGNAL_EXIT + 浅水下 → 转等待不立即平 ──
                if exit_reason == "DEAD_SIGNAL_EXIT":
                    if direction == "LONG":
                        _pe_arm_profit = (current_price - entry_price) / entry_price * 100
                    else:
                        _pe_arm_profit = (entry_price - current_price) / entry_price * 100
                    if patient_exit_arm_eligible(exit_reason, _pe_arm_profit, meta, _pb_cfg_m):
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
                    _t2m_e = datetime.fromisoformat(r["entry_time"])
                    _t2m_n = datetime.now(_t2m_e.tzinfo) if _t2m_e.tzinfo else datetime.now()
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
                    }
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
                # 2026-08-20 (方案A): 限价超时 8s→3s 配置化. 8/20 实弹: MU 限价悬空等满 8s,
                #   期间价格续跌 0.43% → 降级市价砸在 940.16 (净-$24.42). 等待本身就是出血.
                #   配合滑点垫 (触发线抬高→限价翻主动侧), 3s 足够吃价; 超时仍降级市价无卡单.
                #   回滚: breakeven_limit_timeout_s 改回 8.
                try:
                    from app.playbook.config import get_playbook_config as _get_pb_cfg_be
                    _be_timeout = float(_get_pb_cfg_be().get("breakeven_limit_timeout_s", 8.0))
                except Exception:
                    _be_timeout = 8.0
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target,
                                                limit_price=_close_limit_price,
                                                limit_timeout_s=_be_timeout)
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
            # 2026-08-26 审计加固: 基底从轮初旧行 r["metadata_json"] 改为内存 meta 快照。
            #   根因: L7763 每轮(含退出轮)已把完整 meta 写回 DB, 但此处用轮初旧行重建
            #   _meta_out 再写回 → 退出轮的 peak/mfe/e_shadow 等末轮增量被旧行覆盖丢失
            #   (30笔实测核心字段在, 丢的是退出轮最后一跳 + 仅退出轮新产生的字段)。
            #   dict(meta) == 当前 DB 行内容 (L7763 写回前已 pop 同 3 个临时键), 严格等价
            #   + 本轮增量; 下方手工搬运块保留为安全网 (覆写同值, 幂等)。
            _meta_out = dict(meta)
            for _k in ("_log_tag", "_symbol", "_skip_allowed"):
                _meta_out.pop(_k, None)
            _meta_out["exit_price_source"] = _price_source
            _meta_out["broker_qty_before"] = close_res.get("broker_qty_before")
            _meta_out["broker_qty_after"] = _broker_qty_after
            # 2026-07-25: stall 触发现场从内存 meta 搬运 (_meta_out 基于开环旧行重建,
            #   规则函数写进 meta 的字段不搬会丢)
            if meta.get("stall_exit"):
                _meta_out["stall_exit"] = meta["stall_exit"]
            # 2026-08-13 (耐心出场 §18.3-5 双口径记账): patient_exit 从内存 meta 搬运 +
            #   双口径 delta — immediate_exit_price = arm 时刻若立即市价平的反事实参考价,
            #   delta = 实际退出价相对立即平的改善 (LONG: exit-imm; SHORT: imm-exit, $/股×qty).
            #   expire 路径反向清理: 内存 meta 已 pop 但开环旧行仍带 armed 状态 → 抹除,
            #   否则作废状态复活 (墓碑同步搬运防重新 arm).
            if meta.get("patient_exit"):
                _pe_final = meta["patient_exit"]
                try:
                    _pe_imm = float(_pe_final.get("immediate_exit_price", 0) or 0)
                    if _pe_imm > 0 and exit_price > 0 and qty:
                        if direction == "LONG":
                            _pe_delta = (exit_price - _pe_imm) * qty
                        else:
                            _pe_delta = (_pe_imm - exit_price) * qty
                        _pe_final["patient_vs_immediate_delta"] = round(_pe_delta, 2)
                except Exception:
                    pass
                _meta_out["patient_exit"] = _pe_final
            else:
                _meta_out.pop("patient_exit", None)  # expire/未 arm: 开环旧行的状态不复活
            if meta.get("_patient_exit_expired"):
                _meta_out["_patient_exit_expired"] = True
            # 2026-08-18 遥测搬运 (WDC/AMD 案): 限价保护退出的侧/触发价/报价源 +
            #   成交结局 (limit_filled=限价成交; limit_fallback=30s→8s 未成交降级市价)
            #   + 降级漂移 (fallback_fill 相对保本线的越位). 毕业判分按 limit_side 分组.
            if meta.get("_be_close_telemetry"):
                _be_tel = dict(meta["_be_close_telemetry"])
                if _price_source == "broker_limit_deal":
                    _be_tel["outcome"] = "limit_filled"
                else:
                    _be_tel["outcome"] = "limit_fallback_market"
                    try:
                        _be_tel["fallback_fill"] = exit_price
                        _be_tel["drift_from_be_line"] = round(
                            (exit_price - _be_tel["limit_px"]) / _be_tel["limit_px"] * 10000, 2)
                    except Exception:
                        pass
                _meta_out["_be_close_telemetry"] = _be_tel
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


def _mark_overnight_arm(conn, row_id: int, meta: dict, source: str,
                        pnl_net_pct: float) -> None:
    """RFC-067 方案A (2026-08-27, 用户裁): 隔夜臂台账标记 — 精准统计多夜水下仓.

    在 EOD 兜底的豁免点 (再入场首夜 / W_WEEK) 调用: 把"这一夜为何持夜、第几夜、
    决策时刻净水深"写进 metadata_json.overnight_arm。零行为变化 (纯记录),
    供 scripts/overnight_arm_report.py 按臂分组复盘 (来源 × 夜数 × 最终结局)。

    night_n 单调递增 (每次豁免 +1); source 覆盖为最近一次豁免来源
    (再入场仓第2夜起由 W_WEEK 接管 → source 自然从 reentry 变 w_week, 如实反映)。
    异常不阻断豁免 (统计降级优于丢失豁免)。
    """
    try:
        _ov = meta.setdefault("overnight_arm", {})
        if not isinstance(_ov, dict):
            _ov = meta["overnight_arm"] = {}
        from app.utils.timezone import now_et as _oa_now_et
        _oa_now = _oa_now_et()
        # 同 ET 日重跑不重复计夜: run_playbook_track 的幂等对持夜仓无效 (outcome=
        # holding 非终态 → 每日重跑), 且盘后窗口内重启会整段重跑 EOD 兜底 —
        # 若无守卫, 同一夜被多次标记 → night_n 虚增 (8/27 复核推演实锤).
        _same_night = str(_ov.get("marked_at_et", ""))[:10] == _oa_now.strftime("%Y-%m-%d")
        if _same_night:
            # 2026-08-27 收盘审计⑥: 旧实现同日只防夜数虚增, 但仍覆盖 source/水深快照 —
            #   部署重启补跑 EOD 兜底时, 16:01 收盘首标被 17:24 盘后实时价覆盖 (口径漂移).
            #   改为首标即定稿: 同 ET 日直接保留当日快照, 跨日才追加夜数与更新快照.
            return
        _ov["night_n"] = int(_ov.get("night_n") or 0) + 1
        _ov["source"] = source
        _ov["underwater_pct_net"] = round(float(pnl_net_pct), 4)
        _ov["marked_at_et"] = _oa_now.isoformat(timespec="seconds")
        import json as _oa_json
        conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                     (_oa_json.dumps(meta), row_id))
        conn.commit()
    except Exception as _oa_ex:
        # 2026-08-27 审计⑥: commit 失败 (DB lock/IO) 时回滚, 防未提交 UPDATE 残留
        #   叠进同 conn 后续写 (EOD fail_count/ghost close) 的事务里连带提交.
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug(f"[playbook sim] overnight_arm 标记异常 (不阻断豁免): {_oa_ex}")


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
            # P2b 单点化 (2026-09-06): 两公式收进 exit_engine (与 poll monitor 同源).
            from app.playbook.exit_engine import (
                cohort_b_skip_eligible as _cse,
                overnight_master_switch_on as _master_on,
            )
            _skip_allowed = _cse(_cohort, r["source_type"])
            # 极简版B (2026-07-17): 隔夜总开关关闭 → EOD 全平, cohort B 盈利仓不跳过.
            if not _master_on():
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

            # P1 三轨合一 (2026-09-06): 轨③再入场首夜 (8/6 裁决) + W_WEEK SIM水下多夜
            #   (8/15 三问Q3) 收为单一决策点 overnight_eod_decision, 评估顺序与合并前
            #   逐字一致 (轨③先判); 台账 source 由决策返回, 值域同 RFC-067 契约。
            #   报价缺失 → 不豁免 (保守平仓); 次日 entry_date≠当日 → 轨③自动失效,
            #   W_WEEK 仍在 ≤5 交易日窗口内接管。
            # 2026-08-17 净口径: 豁免水下判定用净盈亏 (毛 0<毛<费用 的微盈仓=净水下→豁免,
            #   与盘中规则链同口径; 算不出费 → 毛口径原行为).
            # 2026-08-22 audit fix: _rp/_rpnl 每轮重置 — 此前仅 _rpnl_net 重置,
            #   若上一标的报价成功而当前标的 get_quote 抛异常, 下方第二 try 块会拿
            #   上一标的的价格/盈亏判定本标的的隔夜豁免 (跨持仓污染).
            #   重置后报价失败 → None → 不豁免 (保守平仓), 与注释纪律一致.
            _rp = _rpnl = _rpnl_net = None
            try:
                _rq = data_router.get_quote(symbol)
                _rp = float((_rq or {}).get("last", 0) or (_rq or {}).get("price", 0) or 0) if _rq else 0.0
                if _rp > 0:
                    _rpnl = ((_rp - entry_price) / entry_price * 100) if direction == "LONG" \
                        else ((entry_price - _rp) / entry_price * 100)
                    # 净口径换算 (qty/entry 可得时): 往返费/名义额 → 毛-费率
                    try:
                        from app.execution.fee_model import estimate_round_trip as _eod_rt
                        _eod_fee = _eod_rt(qty, entry_price, _rp, direction)["total"]
                        _rpnl_net = _rpnl - _eod_fee / (qty * entry_price) * 100.0
                    except Exception:
                        _rpnl_net = None
                    from app.playbook.overnight_decision import overnight_eod_decision as _eod_dec
                    _eod_exempt, _eod_track = _eod_dec(
                        symbol, r["source_type"], r["entry_time"],
                        _rpnl_net if _rpnl_net is not None else _rpnl)
                    if _eod_exempt:
                        _mark_overnight_arm(conn, r["id"], _meta, _eod_track,
                                            _rpnl_net if _rpnl_net is not None else _rpnl)
                        if _eod_track == "reentry_first_night":
                            logger.info(
                                f"[playbook sim] EOD 兜底豁免 {symbol} 再入场水下仓 {_rpnl:.2f}%"
                                f"(净{_rpnl_net if _rpnl_net is not None else _rpnl:.2f}%) 持夜 (用户裁决)"
                            )
                        else:
                            logger.info(
                                f"[playbook sim] EOD 兜底豁免 {symbol} 水下仓 {_rpnl:.2f}%"
                                f"(净{_rpnl_net if _rpnl_net is not None else _rpnl:.2f}%) 持夜 (用户三问Q3)"
                            )
                        continue
            except Exception as _oex:
                logger.debug(f"[playbook sim] 隔夜豁免判定异常 {symbol} (不豁免): {_oex}")

            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            # P1 fix (2026-06-25): 连续失败 3 次后当日放弃, 停止死循环日志刷屏
            # DEB-20260904 #178: 计数按日重置 — 旧逻辑跨日累计 ≥3 即永久 skip → 孤儿仓;
            # 且 skip 升级为 warning 明示需人工对账
            _fail_count = _meta.get("eod_close_fail_count", 0)
            _fail_date = _meta.get("eod_close_fail_date", "")
            if _fail_date != _today_et_str():
                _fail_count = 0  # 跨日重置
            if _fail_count >= 3:
                logger.warning(
                    f"[playbook dual] EOD 兜底当日放弃 {symbol} ({_row_target}) — "
                    f"当日已连续失败 {_fail_count} 次, 需人工对账确认是否孤儿仓"
                )
                continue
            try:
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.warning(f"[playbook dual] EOD 兜底平仓异常 {symbol} ({_row_target}): {_e}")
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                _meta["eod_close_fail_date"] = _today_et_str()  # DEB-20260904 #178
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
                _meta["eod_close_fail_date"] = _today_et_str()  # DEB-20260904 #178
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
                _meta["eod_close_fail_date"] = _today_et_str()  # DEB-20260904 #178
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
                # DEB-20260903 #87: 已成交但成交价/报价均缺失不得静默 continue —
                # 计失败次数+告警, 防止 trade_records 永久 open (孤儿仓, 监控看不到)
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                _meta["eod_close_fail_date"] = _today_et_str()  # DEB-20260904 #178
                _meta["exit_price_source"] = _price_source
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.error(
                    f"[playbook dual] EOD {symbol} 订单已成交但价格缺失 "
                    f"(第{_fail_count}次) src={_price_source} — trade_records 保持未平仓, 需人工核查"
                )
                if _fail_count >= 3:
                    _alert_close_failure(symbol, _row_target, "EOD_FILLED_NO_PRICE",
                                         f"已成交但价格缺失 连续{_fail_count}次")
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
