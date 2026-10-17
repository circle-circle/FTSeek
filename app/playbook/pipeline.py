"""
Playbook 端到端编排管线 — 把 Layer 0→1→2→3 串联通电。

本模块是整个 Trading Playbook System 唯一缺失的"接线层":
    - Phase 0/1/2 的 14 个模块都已实现且单测通过 (232 tests),
      但没有任何生产代码调用它们 — scheduler 不感知 playbook 的存在。
    - 本文件提供 3 个入口, 复用项目现有调度约定 (phase-polling + 每日/每周幂等标志):
        run_playbook_generate(scheduler)   盘前  regime→收集→筛选→决策适配器→入库→飞书
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
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# [阶段 2a 2026-10-09] voting adapter 本体已迁 pipeline_parts.bridge_adapters;
# 此处 re-export 保 scheduler 懒 import 与既有 pipeline 公开名不破。
from app.playbook.pipeline_parts.bridge_adapters import (
    BridgeVotingAdapter,
    NullVotingAdapter,
    TechnicalFallbackAdapter,
    _validate_bridge_schema,
)
from app.playbook.pipeline_parts.runtime_context import RuntimeContext

# 2026-08-05: bar 覆盖冻结告警的模块级计时器 (symbol -> stall_since_ts).
#   meta 每轮从 DB 重读且写回有条件门, 计时不可放 meta; 按 symbol 记 (同日重复开仓重置无害).
_BAR_STALL_SINCE: Dict[str, float] = {}

# 2026-09-11 (DEB-20260910-logging §11.8-3): chase_cap **同指令累计拦截轮次** (inst_id -> 轮次)。
#   纯观测面 — **不参与任何判定**: enforce 仍每轮独立重评 (9/8 SNDK 正是靠重评成交, 行为零改动)。
#   动机: 9/10 夜 US.AMD SELL_SHORT 被连拦 **69 轮** (21:45→03:58, 每~5min 一轮), 每轮文案除价格外
#   完全相同 ⇒ 值班人无法从单行分清"首次拦截"与"整夜拦了 69 次", 只能 grep 数行 (那 138 行 WARNING
#   占当晚 237 条的 58%)。补上轮次 ⇒ 一眼可判"是否该人工放弃", 不必再数行。
#   进程内计数 (重启归零, 与 _BAR_STALL_SINCE 同款取舍): 一夜美股会话 = 一个进程生命周期, 够用;
#   跨重启累计要落盘, 不值得为一个观测面计数器引入状态文件。
#   ⚠️ 本助手**刻意不写 try/except**: 调用点在 G-06 fail-close 的 try 内, 任何异常都会让
#   `_cc_gate_ok` 停在 False ⇒ 把"本轮跳过"升级成"终态跳过"(= 真行为变更)。故只用不可能抛的
#   内存字典操作 (str + dict.get/len/pop/赋值), **不碰 DB / 文件 / 网络**。
#   容量上界 _CHASE_CAP_ROUNDS_MAX, 超限淘汰最旧键 (一夜指令量级 ~10², 512 用不满, 仅防长驻进程无界增长)。
_CHASE_CAP_ROUNDS: Dict[str, int] = {}
_CHASE_CAP_ROUNDS_MAX = 512

# [RFC-120 2026-09-30] 倒挂 cap 归一化日志防重集 (每指令进程内只报一次 warning,
#   否则逐轮刷屏 — 归一化本身逐轮幂等, 只有首次值得报)。有界同 _CHASE_CAP_ROUNDS 思路,
#   指令量级 ~10²/日, 进程重启清零可接受 (重启后重报一次)。
_CC_NORM_LOGGED: set = set()
_CC_NORM_LOGGED_MAX = 1024


def _chase_cap_round_bump(inst_id) -> int:
    """该指令**进程内累计**被 chase_cap 拦截的轮次 (从 1 起)。见上方"刻意不写 try/except"。"""
    key = str(inst_id)
    rounds = _CHASE_CAP_ROUNDS.get(key, 0) + 1
    if key not in _CHASE_CAP_ROUNDS and len(_CHASE_CAP_ROUNDS) >= _CHASE_CAP_ROUNDS_MAX:
        _CHASE_CAP_ROUNDS.pop(next(iter(_CHASE_CAP_ROUNDS)), None)
    _CHASE_CAP_ROUNDS[key] = rounds
    return rounds


def _write_round_skip_reason(inst: Dict[str, Any], reason: str) -> None:
    """[RFC-120 P1-4 2026-09-30] 逐轮拦截门的 skip_reason 回写 (非终态, 下轮复评)。

    根因: VWAP方向门/证伪斜率门拦截只发遥测不落库 → EOD shadow_tracker 兜底盖
      "未触发 (no_trigger)" 误导标签, 诊断报告分不清"门拦截"与"价格未到"
      (9/30 LITE 被 VWAP 门拦 13 轮 skip_reason 全程空白, 审计靠翻日志才归因)。
    纪律: ① 值不入 INVALIDATED_SKIP_REASONS (非终态, 头检精确匹配不受影响);
      ② 既有值同前缀则跳过 (每轮重评不重复写库); ③ 执行成交时 db 层不变式
      (executed=1 → skip_reason 清空) 照常兜底; ④ 异常只 warn 不影响拦截。
    """
    try:
        _cur = str(inst.get("skip_reason") or "")
        _prefix = reason.split("(")[0]
        if _cur.startswith(_prefix):
            return
        from app.playbook.db import PlaybookDB as _PBDB
        _PBDB().update_instruction_tracking(inst["id"], {"skip_reason": reason})
        inst["skip_reason"] = reason
    except Exception as _sr_err:
        logger.warning(
            f"[playbook] 门拦截 skip_reason 回写失败 {inst.get('symbol')} "
            f"(不影响拦截): {_sr_err}", exc_info=True)

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
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.shadow_telemetry; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import shadow_telemetry as _st
    return _st._vwap_ab_row_exists(date_str, symbol, direction)


def _vwap_ab_arm(symbol: str, date_str: str, direction: str) -> str:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.shadow_telemetry; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import shadow_telemetry as _st
    return _st._vwap_ab_arm(symbol, date_str, direction)


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
        logger.warning(f"[playbook] 方向审计日志失败: {e}", exc_info=True)


def _get_horizon_selector() -> Any:
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _get_horizon_selector as _impl
    return _impl()


# ── 时间/节假日 (复用项目既有约定) ──────────────────────────────────────
ET = None  # lazy


def _now_et():
    global ET
    if ET is None:
        from app.core.scheduler_triggers import ET as _et
        ET = _et
    return ET


_low_atr_logged: set = set()

# short_chase_ban_shadow 对照臂去重 (2026-09-18 采样设计修复): 扫描行每 (指令,ET日) 只写 1 行,
# 否则逐轮写会灌水 (命中行仍逐轮写, 语义不变)。仅内存态, 重启清空 = 当日最多多写 1 行。
_scb_scan_logged: set = set()


def _short_chase_shadow_row(symbol, chg_pct, threshold, entry_price, current_price,
                            triggered, entry_path, inst_id, decision_id, date_str, ts_str,
                            scan=False):
    """构造 short_chase_ban_shadow 行 (纯函数, 便于单测)。

    2026-09-18 采样设计修复 (用户令"按建议"): 原实现**只在 chg_pct>阈值 时落行** ⇒ 8/24-9/18
    全窗命中仅 9 条指令 (配到成交 6 笔), 预注册死线 10/31 前到不了 n≥20 ⇒ 判据永不可裁;
    且**无对照臂** ⇒ 「反事实方向一致率」无法与未命中组对照, 无法算 IC。
    现改为两类行, 同文件同 schema:
      - 命中行 (over_threshold=True): 字段与写时机逐字不变 (逐轮写), 仅新增 date/decision_id/
        over_threshold/scan 四个字段 ⇒ 既有消费者与判据口径不受影响;
      - 扫描行 (over_threshold=False, scan=True): 每 (指令,ET日) 首轮写 1 行 = 对照组,
        不参与判据计数 (注册表 count_mode=where:over_threshold=True)。
    date/decision_id 顺带修 join 键脏数据 (注册表 notes 记载: 行内原无日期、inst_id 有跨方向脏值)。
    """
    return {
        "ts": ts_str,
        "date": date_str,
        "symbol": symbol,
        "direction": "SHORT",
        "chg_pct": round(float(chg_pct), 3),
        "threshold": threshold,
        "entry_price": entry_price,
        "current_price": round(float(current_price or 0), 4),
        "triggered": bool(triggered),
        "entry_path": entry_path,
        "inst_id": inst_id,
        "decision_id": decision_id,
        "over_threshold": bool(not scan),
        "scan": bool(scan),
    }


def _today_et_str() -> str:
    from datetime import datetime
    return datetime.now(_now_et()).date().isoformat()


def _screening_age_hours(observed_at: Any) -> Optional[float]:
    """候选筛选快照的年龄 (小时) — 只用于审计留痕, 不参与任何判定。

    [P1.2 2026-09-12] 低ATR门兜底读 `candidate_screening.screening_atr_ratio` 时,
    "这个比例是几小时前的盘前观测"必须可见: 快照不当实时行情用 (计划边界),
    值班人需要一眼看出依据有多旧。

    口径: `observed_at` 是 ISO8601 (筛选时刻, 带时区); 年龄 = now(ET) − observed_at。
    单位/量纲: **小时** (float, 保留 1 位由调用方格式化); 负值 (未来时间戳) 原样返回,
    不夹到 0 — 夹零会把时钟/时区错误伪装成"刚观测"。
    任何解析失败 → None (调用方按"龄未知"记录, 不猜)。
    """
    if not observed_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None          # naive 不猜时区 (to_et 会把 naive 当北京时间 → 移半天)
    return (datetime.now(_now_et()) - stamp).total_seconds() / 3600.0


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
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _playbook_enabled as _impl
    return _impl()


def _playbook_cfg(key: str, default=None):
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _playbook_cfg as _impl
    return _impl(key, default)


# ── 依赖装配 (lazy, 单步异常隔离) ──────────────────────────────────────
def _build_data_collector():
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _build_data_collector as _impl
    return _impl()


def _get_watchlist(scheduler) -> List[str]:
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _get_watchlist as _impl
    return _impl(scheduler)


def _get_positions_as_dicts(scheduler) -> List[Dict[str, Any]]:
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _get_positions_as_dicts as _impl
    return _impl(scheduler)


def _get_total_equity(scheduler) -> float:
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _get_total_equity as _impl
    return _impl(scheduler)


def _resolve_sizing_base(pb_cfg=None) -> float:
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _resolve_sizing_base as _impl
    return _impl(pb_cfg)


# ── 决策适配器 → Instruction 转换器 ─────────────────────────────────
# Playbook 不跑独立 LLM; 方向由 DA bridge/technical fallback/Null 适配器给出.
# 数值由 compute_instruction_values 确定性计算.

def _get_voting_coordinator(scheduler):
    """[阶段 2b] 本体迁 pipeline_parts.runtime_context; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts.runtime_context import _get_voting_coordinator as _impl
    return _impl(scheduler)


def _build_runtime_context(scheduler) -> RuntimeContext:
    """装配显式运行上下文; provider 注入 pipeline facade, 既有 monkeypatch 缝不换。"""
    from app.playbook.pipeline_parts.runtime_context import build_runtime_context as _impl
    return _impl(
        scheduler,
        watchlist_provider=_get_watchlist,
        collector_factory=_build_data_collector,
        positions_provider=_get_positions_as_dicts,
        voting_coordinator_provider=_get_voting_coordinator,
        total_equity_provider=_get_total_equity,
    )


# ── 2026-07-19 (DEB-round3 #1): bridge 三态健康告警 + schema fail-safe ──
# 缺失/空候选/schema非法 → 飞书 HIGH 告警 (当日去重, 不阻断).
# 盲区: bridge 文件仅 scheduler 启动时 _try_load_bridge 加载一次, 运行期不重载 →
#   启动后 Hermes cron 失败/文件损坏, 系统一整天 0 决策而运维不知情.
# 告警函数直读文件, 独立于 adapter 状态, 在 run_generate / auto_execute 入口调用.
_bridge_alert_sent = set()


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
    """加载今日 bridge 文件。不存在 → None (调用方 fallback 到决策适配器)。"""
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
        logger.info(f"[playbook] bridge文件无候选, fallback到决策适配器")
        return None

    # 2026-07-19 (DEB-round3 #1): schema fail-safe — 非法文件视作缺失,
    #   走技术面降级而非消费半损坏数据 (错价决策比 0 决策更危险).
    if not _validate_bridge_schema(candidates):
        logger.error(
            f"[playbook] bridge文件schema非法 (缺symbol/direction/decision_id), "
            f"fail-safe视作缺失, fallback到决策适配器"
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
            f"过期数据fail-safe视作缺失, fallback到决策适配器"
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
    """[阶段 3a] 本体迁 pipeline_parts.instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._direction_conditioned_ic_net(direction, bearish_hits, bullish_hits, factor_ic)


def _normalize_regime_str(regime_context) -> str:
    """[阶段 3a] 本体迁 pipeline_parts.instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._normalize_regime_str(regime_context)


def _build_vote_market_data(symbol: str, data, regime_context, data_router):
    """[阶段 3a] 本体迁 pipeline_parts.instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._build_vote_market_data(symbol, data, regime_context, data_router)


def _load_factor_ic_global() -> dict:
    """[阶段 3a] 构建前置态本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._load_factor_ic_global()


def _regime_multiplier(regime_str: str) -> float:
    """[阶段 3a] 构建前置态本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._regime_multiplier(regime_str)


def _new_filter_audit(total_input: int) -> Dict[str, int]:
    """[阶段 3a] 构建前置态本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._new_filter_audit(total_input)


def _load_direction_config() -> tuple:
    """[阶段 3a] 构建前置态本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._load_direction_config()


def _record_vote_snapshot(symbol: str, vote_result, factors) -> None:
    """[阶段 3b] 投票快照本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._record_vote_snapshot(symbol, vote_result, factors)


def _evaluate_technical_direction(market_ctx, symbol: str = "") -> Optional[str]:
    """[阶段 3b] technical 方向本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._evaluate_technical_direction(market_ctx, symbol)


def _apply_technical_arbitration(symbol: str, vote_direction: str,
                                 technical_direction: Optional[str],
                                 vote_result: Dict[str, Any],
                                 llm_conflict_threshold: float) -> tuple:
    """[阶段 3b] technical 仲裁本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._apply_technical_arbitration(
        symbol, vote_direction, technical_direction, vote_result, llm_conflict_threshold
    )


def _apply_buy_std_gate(symbol: str, vote_result: Dict[str, Any], direction: str) -> bool:
    """[阶段 3b] BUY 分歧门本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._apply_buy_std_gate(symbol, vote_result, direction)


def _factor_direction_conflict(direction: str, factors, factor_ic: dict) -> tuple:
    """[阶段 3b] IC 方向门本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._factor_direction_conflict(direction, factors, factor_ic)


def _select_horizon(sr, regime_context, selector_provider=None) -> tuple:
    """[阶段 3b] horizon 阶段本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._select_horizon(sr, regime_context, selector_provider)


def _build_instruction_decision(symbol: str, direction: str, factors,
                                vote_result: Dict[str, Any]) -> Dict[str, Any]:
    """[阶段 3b] decision 构造本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._build_instruction_decision(symbol, direction, factors, vote_result)


def _compute_instruction_values(decision: Dict[str, Any], data,
                                total_equity: float, regime_multiplier: float,
                                horizon: str, regime_context) -> Dict[str, Any]:
    """[阶段 3b] 数值计算阶段迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._compute_instruction_values(
        decision, data, total_equity, regime_multiplier, horizon, regime_context
    )


def _decision_confidence(vote_result: Dict[str, Any], data,
                         decision: Dict[str, Any], factors) -> int:
    """[阶段 3b] confidence 阶段迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._decision_confidence(vote_result, data, decision, factors)


def _build_instruction(symbol: str, direction: str, data, factors,
                       vote_result: Dict[str, Any], decision: Dict[str, Any],
                       values: Dict[str, Any], decision_id: str, confidence: int,
                       horizon: str, horizon_confidence: float,
                       horizon_reasoning):
    """[阶段 3b] Instruction 组装本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._build_instruction(
        symbol=symbol,
        direction=direction,
        data=data,
        factors=factors,
        vote_result=vote_result,
        decision=decision,
        values=values,
        decision_id=decision_id,
        confidence=confidence,
        horizon=horizon,
        horizon_confidence=horizon_confidence,
        horizon_reasoning=horizon_reasoning,
    )


def _log_filter_audit(instructions: List[Any], filter_audit: Dict[str, int]) -> None:
    """[阶段 3b] 过滤审计本体迁 instruction_builder; 转发器保 pipeline 缝。"""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._log_filter_audit(instructions, filter_audit)


# 2026-07-11 (双脑清理): _merge_playbook_data 已移除。
# 其唯一用途是向 vote() 的 market_data dict 注入 6 维度供已死的 3 模型
# LLM prompt 消费。现 vote() 直接收 SymbolContext, 此函数无调用方。


from app.playbook.instruction_generator import (
    Instruction,
    compute_instruction_values,
    compute_confidence,
    build_thesis,
    _compute_volatility_based_sl_tp,
    _positive_finite_atr,
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
    """消费决策适配器结果, 构建 Instruction 列表.

    数据流:
      ScreenResult → market_data → decision_adapter.vote() → vote_result
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
    _factor_ic_global = _load_factor_ic_global()
    regime_mult = _regime_multiplier(regime_str)

    # ── 过滤审计计数器 (DEB-20260628) ──
    # 记录每层过滤干掉了几个candidates, 函数末尾统一输出
    _filter_audit = _new_filter_audit(len(screen_results))

    # [v0.70 P1] ADR-008 仲裁配置 — 循环外一次读取 (原每候选 get_config+2次get_dotted)
    _direction_mode, _llm_conflict_threshold = _load_direction_config()

    for sr in screen_results:
        symbol = sr.symbol
        data = sr.data
        factors = sr.factors

        # 持久化信息层上下文 (新闻/内部人/财报/基本面/主题雷达)
        # 在投票前保存, 即使投票失败也有数据
        _persist_signal_context(today, symbol, data)

        # 构造 market_data 并调决策适配器 (新路径走 SymbolContext, legacy 走 10 字段 dict)
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
        _record_vote_snapshot(symbol, vote_result, factors)

        if is_neutral:
            _filter_audit["vote_neutral"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票中性, 无方向信号"
            ))
            continue

        # ── ADR-008 (2026-06-28): 盘中技术方向仲裁 ──
        # [v0.70 P1] 配置读取已提到循环外 (_direction_cfg_init) — 原每候选重复
        #   get_config()+两次 get_dotted, 50候选=150次冗余调用。
        technical_direction = _evaluate_technical_direction(_market_ctx, symbol)

        # 记录方向审计日志 (无论模式)
        _log_playbook_direction_audit(symbol, vote_direction, technical_direction, vote_result)

        # 技术仲裁模式: 无技术方向则 SKIP; LLM 强反向冲突则 SKIP; 否则 technical 获胜
        # [E12 登记·2026-08-30 第6轮] 休眠门语义漂移陷阱: 本块当前 100% 不可达
        # (生产 direction_mode=llm_vote), 但修复 04150669 后若切到 technical_arbitration:
        # 生成端主路径输出精确 0.70, `>= _llm_conflict_threshold(0.70)` 在浮点刀刃上恒真
        # → 门在 100% 方向冲突上触发 SKIP, 技术覆盖分支永不执行 (修复前是技术永远赢)。
        # 切 direction_mode 前必须先复核本门语义。
        if _direction_mode == "technical_arbitration":
            vote_direction, _arbitration_skip_reason, _arbitration_audit_key = (
                _apply_technical_arbitration(
                    symbol,
                    vote_direction,
                    technical_direction,
                    vote_result,
                    _llm_conflict_threshold,
                )
            )
            if _arbitration_audit_key:
                _filter_audit[_arbitration_audit_key] = (
                    _filter_audit.get(_arbitration_audit_key, 0) + 1
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=_arbitration_skip_reason
                ))
                continue

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
        if _apply_buy_std_gate(symbol, vote_result, direction):
            _filter_audit["buy_high_std_skip"] = _filter_audit.get("buy_high_std_skip", 0) + 1
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
        _conditions = list(factors.keys()) if factors else []
        _ic_conflict, _ic_side, _net_score, _scored_factors = (
            _factor_direction_conflict(direction, factors, _factor_ic_global)
        )
        if _ic_conflict:
            _filter_audit["direction_flip"] += 1
            logger.info(
                f"[playbook] {symbol} 方向条件化IC冲突 ({_ic_side}侧净IC={_net_score:+.2f}, "
                f"计分因子={_scored_factors}) 与LLM方向({direction})相悖 → SKIP"
            )
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP",
                skip_reason=f"IC方向冲突: {direction}候选{_ic_side}侧净IC={_net_score:+.2f}(方向条件化)"
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
        _horizon, _horizon_confidence, _horizon_reasoning = _select_horizon(
            sr, regime_context, selector_provider=_get_horizon_selector
        )

        decision = _build_instruction_decision(symbol, direction, factors, vote_result)
        values = _compute_instruction_values(
            decision,
            data,
            total_equity,
            regime_mult,
            _horizon,
            regime_context,
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
        confidence = _decision_confidence(vote_result, data, decision, factors)

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
        
        inst = _build_instruction(
            symbol=symbol,
            direction=direction,
            data=data,
            factors=factors,
            vote_result=vote_result,
            decision=decision,
            values=values,
            decision_id=decision_id,
            confidence=confidence,
            horizon=_horizon,
            horizon_confidence=_horizon_confidence,
            horizon_reasoning=_horizon_reasoning,
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

    _log_filter_audit(instructions, _filter_audit)

    # 组合级 guardrail: 方向偏斜 / sector 集中度 / 最低 R:R
    # [RFC-122 阶段0] 传 today 供偏斜门双口径影子读当日 DB (record-only)
    instructions = _apply_portfolio_guardrails(instructions, today=today)

    return instructions


def _select_thesis_template(factors: Dict[str, Any], direction: str,
                            entry_archetype: Optional[str] = None) -> str:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.instruction_builder; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._select_thesis_template(factors, direction, entry_archetype)


# ── 组合闸域 (阶段 3b): 本体迁 pipeline_parts.portfolio_guardrails ──
def _get_vault_industry_map(max_age_s: float = 3600.0) -> Dict[str, str]:
    """[阶段 3b] 本体迁 portfolio_guardrails; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import portfolio_guardrails as _pg
    return _pg._get_vault_industry_map(max_age_s)


def portfolio_guardrail_enabled() -> bool:
    """[阶段 3b] 本体迁 portfolio_guardrails; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import portfolio_guardrails as _pg
    return _pg.portfolio_guardrail_enabled()


def _apply_portfolio_guardrails(
    instructions: List[Any],
    *,
    max_sector_pct: float = 0.40,
    max_direction_skew: float = 0.80,
    min_rr: float = 1.0,
    today: str = "",
) -> List[Any]:
    """[阶段 3b] 本体迁 portfolio_guardrails; 显式注入旧公开回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import portfolio_guardrails as _pg
    return _pg._apply_portfolio_guardrails(
        instructions,
        max_sector_pct=max_sector_pct,
        max_direction_skew=max_direction_skew,
        min_rr=min_rr,
        today=today,
        emit_shadow=_emit_skew_gate_vision_shadow,
        guardrail_enabled=portfolio_guardrail_enabled,
        industry_map_provider=_get_vault_industry_map,
        trim_direction=_trim_majority_direction,
        trim_sector=_trim_majority_sector,
    )


def _emit_skew_gate_vision_shadow(valid: List[Any], today: str,
                                  max_direction_skew: float,
                                  max_sector_pct: float = 0.40) -> None:
    """[阶段 3b] 本体迁 portfolio_guardrails; module_file 保 __file__ 测试缝。"""
    from app.playbook.pipeline_parts import portfolio_guardrails as _pg
    return _pg._emit_skew_gate_vision_shadow(
        valid,
        today,
        max_direction_skew,
        max_sector_pct,
        module_file=__file__,
    )


def _trim_majority_direction(valid: List[Any], majority_dir: str, target_skew: float) -> int:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.instruction_builder; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._trim_majority_direction(valid, majority_dir, target_skew)


def _trim_majority_sector(valid: List[Any], sector: str, target_pct: float) -> int:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.instruction_builder; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._trim_majority_sector(valid, sector, target_pct)


# ── 入口 1: 盘前生成 (regime → 收集 → 筛选 → 决策适配器 → 入库 → 飞书) ──
def _mark_generate_done(today: str) -> None:
    """[#22] 写生成完成标记 (best-effort, 失败不阻断返回)."""
    try:
        from app.playbook.db import PlaybookDB
        PlaybookDB().mark_generate_completed(today)
    except Exception as e:
        logger.warning(f"[playbook] 生成完成标记写入失败(不影响本次结果): {e}")


def run_playbook_generate(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """[阶段 6] 本体迁 generation_flow; 显式注入 pipeline 回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import generation_flow as _gf
    return _gf.run_playbook_generate(
        scheduler,
        force=force,
        today_provider=_today_et_str,
        playbook_enabled=_playbook_enabled,
        trading_day_today=_is_trading_day_today,
        reload_bridge_if_landed=_reload_bridge_if_landed,
        check_bridge_health_and_alert=_check_bridge_health_and_alert,
        build_runtime_context=_build_runtime_context,
        build_instructions_from_votes=_build_instructions_from_votes,
        instruction_to_db_dict=_instruction_to_db_dict,
        mark_generate_done=_mark_generate_done,
        logger=logger,
    )


_MORNING_PUSH_STATE = "/opt/ftseek/FTSeek-US/data/playbook_push_state.json"


def run_playbook_morning_push(scheduler=None) -> Optional[bool]:
    """[阶段 6] 本体迁 generation_flow; state_path/logger 注入保路径与日志语义。"""
    from app.playbook.pipeline_parts import generation_flow as _gf
    return _gf.run_playbook_morning_push(
        scheduler,
        state_path=_MORNING_PUSH_STATE,
        logger=logger,
    )


# D2 (2026-07-24 夜): topup 每窗口尝试计数 {"YYYY-MM-DD:timing": [count, last_ts]} — +0 退避用,
#   内存级 (重启重置无害), 隶属 run_playbook_topup_supplement。
_TOPUP_ATTEMPTS: Dict[str, list] = {}


def _topup_window(hhmm: int, cfg: Dict[str, Any]) -> Optional[str]:
    """[阶段 6] 本体迁 generation_flow; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import generation_flow as _gf
    return _gf._topup_window(hhmm, cfg)


def run_playbook_topup_supplement(scheduler=None) -> Optional[Dict[str, Any]]:
    """[阶段 6] 本体迁 generation_flow; 注入时间/退避缓存/回调保测试缝。"""
    from app.playbook.pipeline_parts import generation_flow as _gf
    return _gf.run_playbook_topup_supplement(
        scheduler,
        now_et_provider=_now_et,
        today_provider=_today_et_str,
        topup_window=_topup_window,
        attempts_cache=_TOPUP_ATTEMPTS,
        build_runtime_context=_build_runtime_context,
        build_instructions_from_votes=_build_instructions_from_votes,
        instruction_to_db_dict=_instruction_to_db_dict,
        gather_intraday_evidence=_gather_intraday_evidence,
        logger=logger,
    )


def _gather_intraday_evidence(data_router, symbol: str, direction: str,
                              data_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """[阶段 5] 本体迁 intraday_indicators; 注入时间/文件/datetime 保测试缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._gather_intraday_evidence(
        data_router,
        symbol,
        direction,
        data_root,
        today_provider=_today_et_str,
        now_et_provider=_now_et,
        datetime_cls=datetime,
        module_file=__file__,
    )


def run_playbook_normalize_vol_buckets(scheduler=None) -> Optional[Dict[str, Any]]:
    """[阶段 6] 本体迁 generation_flow; 注入 ATR/SLTP 回调保测试缝。"""
    from app.playbook.pipeline_parts import generation_flow as _gf
    return _gf.run_playbook_normalize_vol_buckets(
        scheduler,
        today_provider=_today_et_str,
        positive_finite_atr=_positive_finite_atr,
        compute_volatility_based_sl_tp=_compute_volatility_based_sl_tp,
        logger=logger,
    )


def _compute_key_levels(data: Any) -> Dict[str, Any]:
    """[阶段 8] 本体迁 signal_context_store; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import signal_context_store as _sc
    return _sc._compute_key_levels(data)


# [v0.70 P1 2026-08-26] theme_context.json mtime 缓存 — 原实现每候选重新 open+parse
#   同一文件 (50候选=50次冗余IO+解析)。按 mtime 失效, 文件更新自动重载。
_tc_cache = {"mtime": None, "data": None}


def _load_theme_context_cached(tc_path):
    """[阶段 8] 本体迁 signal_context_store; cache 注入保进程内语义。"""
    from app.playbook.pipeline_parts import signal_context_store as _sc
    return _sc._load_theme_context_cached(tc_path, cache=_tc_cache)


def _persist_signal_context(
    date: str, symbol: str, data: Any, vote_result: Optional[Dict] = None,
) -> None:
    """[阶段 8] 本体迁 signal_context_store; 注入 module_file/回调保测试缝。"""
    from app.playbook.pipeline_parts import signal_context_store as _sc
    return _sc._persist_signal_context(
        date,
        symbol,
        data,
        vote_result,
        module_file=__file__,
        compute_key_levels=_compute_key_levels,
        load_theme_context=_load_theme_context_cached,
        logger=logger,
    )


def _instruction_to_db_dict(inst, regime_ctx, today: str) -> Dict[str, Any]:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.instruction_builder; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import instruction_builder as _ib
    return _ib._instruction_to_db_dict(inst, regime_ctx, today)


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
    """[阶段 6] 本体迁 tracking_flow; 注入时间/回调/module_file 保测试缝。"""
    from app.playbook.pipeline_parts import tracking_flow as _tf
    return _tf.run_playbook_track(
        scheduler,
        force=force,
        today_provider=_today_et_str,
        playbook_enabled=_playbook_enabled,
        trading_day_today=_is_trading_day_today,
        run_ic_backfill_only=_run_ic_backfill_only,
        force_close_orphaned_sim_positions=_force_close_orphaned_sim_positions,
        now_et_provider=_now_et,
        module_file=__file__,
        logger=logger,
    )


# ── 入口 3: 周报 + 权重闭环 (周五) ─────────────────────────────────────
def run_playbook_weekly(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """[阶段 6] 本体迁 tracking_flow; 注入时间/配置/回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import tracking_flow as _tf
    return _tf.run_playbook_weekly(
        scheduler,
        force=force,
        now_et_provider=_now_et,
        playbook_enabled=_playbook_enabled,
        playbook_cfg=_playbook_cfg,
        today_provider=_today_et_str,
        get_total_equity=_get_total_equity,
        logger=logger,
    )


# ── 入口 4: 盘中自动执行 Paper Trade (Phase 1: 2026-06-19) ──────────

# 日亏熔断基线快照 (阶段 4: 本体迁 pipeline_parts.risk_gates)
_DAILY_LOSS_BASELINE = Path(__file__).resolve().parents[2] / "data" / "daily_loss_baseline.json"


def _check_daily_loss_circuit(today: str) -> Optional[Dict[str, Any]]:
    """[阶段 4] 本体迁 risk_gates; baseline_path 保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._check_daily_loss_circuit(today, baseline_path=_DAILY_LOSS_BASELINE)


def _has_consecutive_losses(n: int = 5) -> bool:
    """[阶段 4] 本体迁 risk_gates; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._has_consecutive_losses(n)


def _check_graduation_gate() -> Dict[str, Any]:
    """[阶段 4] 本体迁 risk_gates; 显式注入连亏回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._check_graduation_gate(consecutive_losses=_has_consecutive_losses)


def _check_same_day_hard_stop_cooldown(symbol: str, direction: str,
                                       today: str) -> Optional[Dict[str, Any]]:
    """[阶段 4] 本体迁 risk_gates; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._check_same_day_hard_stop_cooldown(symbol, direction, today)


def _emit_exec_gate_audit(inst: Dict[str, Any], *, action: str, entry: float,
                          size: float, notional: float, extra: Optional[Dict[str, Any]] = None) -> None:
    """[阶段 4] 本体迁 risk_gates; 注入 now_et/module_file 保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._emit_exec_gate_audit(
        inst,
        action=action,
        entry=entry,
        size=size,
        notional=notional,
        extra=extra,
        now_et_provider=_now_et,
        module_file=__file__,
    )


def _live_engineering_probe_context() -> Optional[Dict[str, Any]]:
    """[阶段 4] 本体迁 risk_gates; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._live_engineering_probe_context()


def _mirror_instruction_to_live(inst: Dict[str, Any], live_db) -> Optional[int]:
    """[阶段 4] 本体迁 risk_gates; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._mirror_instruction_to_live(inst, live_db)


def _live_mirror_gate_decision(live_db, inst: Dict[str, Any]) -> Dict[str, Any]:
    """[阶段 4] 本体迁 risk_gates; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._live_mirror_gate_decision(live_db, inst)


def _mirror_instruction_to_live_if_allowed(inst: Dict[str, Any], live_db):
    """[阶段 4] 本体迁 risk_gates; 显式注入镜像回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._mirror_instruction_to_live_if_allowed(
        inst,
        live_db,
        mirror=_mirror_instruction_to_live,
        gate_decision=_live_mirror_gate_decision,
    )


def _auto_execute_preflight(today: str, *, logger=None):
    """[阶段 7] 本体迁 risk_gates; 注入日亏回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import risk_gates as _rg
    return _rg._auto_execute_preflight(
        today,
        check_daily_loss_circuit=_check_daily_loss_circuit,
        logger=logger or globals()["logger"],
    )


def _load_execution_budget(today: str):
    """[阶段 9] 本体迁 execution_flow; 注入 sizing 回调保测试缝。"""
    from app.playbook.pipeline_parts import execution_flow as _ef
    return _ef.load_budget_and_guardrails(
        today,
        resolve_sizing_base=_resolve_sizing_base,
        logger=logger,
    )


def run_playbook_auto_execute(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """[阶段11] 本体迁 auto_execute_flow; 显式注入全部 pipeline 回调/状态保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import auto_execute_flow as _aef
    return _aef.run_playbook_auto_execute(
        scheduler,
        force=force,
        datetime_cls=datetime,
        _now_et=_now_et,
        _today_et_str=_today_et_str,
        module_file=__file__,
        logger=logger,
        INVALIDATED_SKIP_REASONS=INVALIDATED_SKIP_REASONS,
        _CC_NORM_LOGGED=_CC_NORM_LOGGED,
        _CC_NORM_LOGGED_MAX=_CC_NORM_LOGGED_MAX,
        _auto_execute_preflight=_auto_execute_preflight,
        _chase_cap_round_bump=_chase_cap_round_bump,
        _check_bridge_health_and_alert=_check_bridge_health_and_alert,
        _check_intraday_trigger=_check_intraday_trigger,
        _check_same_day_hard_stop_cooldown=_check_same_day_hard_stop_cooldown,
        _compute_volatility_based_sl_tp=_compute_volatility_based_sl_tp,
        _emit_exec_gate_audit=_emit_exec_gate_audit,
        _is_trading_day_today=_is_trading_day_today,
        _live_engineering_probe_context=_live_engineering_probe_context,
        _load_execution_budget=_load_execution_budget,
        _mirror_instruction_to_live_if_allowed=_mirror_instruction_to_live_if_allowed,
        _normalize_regime_str=_normalize_regime_str,
        _playbook_cfg=_playbook_cfg,
        _positive_finite_atr=_positive_finite_atr,
        _resolve_sizing_base=_resolve_sizing_base,
        _shadow_dedup_claim=_shadow_dedup_claim,
        _shadow_dedup_commit=_shadow_dedup_commit,
        _vol_bucket_sl_bounds=_vol_bucket_sl_bounds,
    )


# P1-3 (2026-08-23): 逐分钟 p50 累积量曲线缓存 {symbol: (day, [cum_p50...])} —
#   源文件 5-6MB/票, 只在当日首次访问时解析一次。
_VOL_CURVE_CACHE: Dict[str, Any] = {}


def _intraday_vol_curve_p50(symbol: str, days: int = 20) -> Optional[List[float]]:
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et/file/cache 保行为与测试缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._intraday_vol_curve_p50(
        symbol,
        days,
        now_et_provider=_now_et,
        module_file=__file__,
        cache=_VOL_CURVE_CACHE,
    )


def _calc_intraday_vol_ratio(data_router, symbol: str,
                             realtime_cum_vol: Optional[float] = None,
                             use_p50_curve: Optional[bool] = None) -> Optional[float]:
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et/curve 保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._calc_intraday_vol_ratio(
        data_router,
        symbol,
        realtime_cum_vol,
        use_p50_curve,
        now_et_provider=_now_et,
        curve_provider=_intraday_vol_curve_p50,
    )


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
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et 保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._calc_vwap_slope(
        data_router, symbol, window, now_et_provider=_now_et
    )


def _capital_flow_divergence(symbol: str, is_short: bool) -> Optional[bool]:
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et/module_file 保测试缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._capital_flow_divergence(
        symbol,
        is_short,
        now_et_provider=_now_et,
        module_file=__file__,
    )


def _symbol_streak_banned(symbol: str, pb_cfg) -> bool:
    """[阶段 5] 本体迁 intraday_indicators; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._symbol_streak_banned(symbol, pb_cfg)


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
#   2026-10-09: 缓存本体随函数迁入 pipeline_parts/intraday_indicators.py (此处原声明
#   成死副本, 已删防直引分裂; _LIQUIDITY_LOGGED 仍注入 trigger_flow 故保留)。
_LIQUIDITY_LOGGED: set = set()  # 拦截日志每标的每日一次


def _check_liquidity_gate(data_router, symbol: str, current_price: float,
                          pb_cfg, planned_qty: float = 0.0) -> tuple:
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et 保行为不变。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._check_liquidity_gate(
        data_router,
        symbol,
        current_price,
        pb_cfg,
        planned_qty,
        now_et_provider=_now_et,
    )


# 2026-07-30 SHORT 借券池门: (symbol, date) → {pool, fee_rate, shortable} 日级缓存
#   2026-10-09: 缓存本体随函数迁入 pipeline_parts/intraday_indicators.py (死副本已删)。
_BORROW_POOL_LOGGED: set = set()  # 拦截日志每标的每日一次


def _check_borrow_pool_gate(symbol: str, planned_qty: float, pb_cfg) -> tuple:
    """[阶段 5] 本体迁 intraday_indicators; 注入 now_et 保行为不变。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._check_borrow_pool_gate(
        symbol,
        planned_qty,
        pb_cfg,
        now_et_provider=_now_et,
    )


# ── [RFC-123 阶段0] 双锚影子纯函数 (阶段 5: 本体迁 intraday_indicators) ──
def _resolve_generated_anchor(inst: Dict[str, Any], is_short: bool):
    """[阶段 5] 本体迁 intraday_indicators; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._resolve_generated_anchor(inst, is_short)


def _anchor_trigger_geometry(anchor, current_price, is_short: bool,
                             knife: float, chase: float, vol_ok: bool,
                             near_miss_tol: float):
    """[阶段 5] 本体迁 intraday_indicators; 转发器保 pipeline monkeypatch 缝。"""
    from app.playbook.pipeline_parts import intraday_indicators as _ii
    return _ii._anchor_trigger_geometry(
        anchor, current_price, is_short, knife, chase, vol_ok, near_miss_tol
    )


def _check_intraday_trigger(scheduler, inst: Dict[str, Any]) -> bool:
    """[阶段10] 本体迁 trigger_flow; 注入回调/状态/module_file 保 monkeypatch 与路径缝。"""
    from app.playbook.pipeline_parts import trigger_flow as _tf
    return _tf.check_intraday_trigger(
        scheduler,
        inst,
        _now_et=_now_et,
        _today_et_str=_today_et_str,
        module_file=__file__,
        logger=logger,
        INVALIDATED_SKIP_REASONS=INVALIDATED_SKIP_REASONS,
        _CUTOFF_LOGGED=_CUTOFF_LOGGED,
        _low_atr_logged=_low_atr_logged,
        _scb_scan_logged=_scb_scan_logged,
        _LIQUIDITY_LOGGED=_LIQUIDITY_LOGGED,
        _BORROW_POOL_LOGGED=_BORROW_POOL_LOGGED,
        _LIMIT_SHADOW_EMITTED=_LIMIT_SHADOW_EMITTED,
        _write_round_skip_reason=_write_round_skip_reason,
        _calc_intraday_vol_ratio=_calc_intraday_vol_ratio,
        _calc_vwap_slope=_calc_vwap_slope,
        _capital_flow_divergence=_capital_flow_divergence,
        _symbol_streak_banned=_symbol_streak_banned,
        _emit_widen_shadow=_emit_widen_shadow,
        _check_liquidity_gate=_check_liquidity_gate,
        _check_borrow_pool_gate=_check_borrow_pool_gate,
        _resolve_generated_anchor=_resolve_generated_anchor,
        _anchor_trigger_geometry=_anchor_trigger_geometry,
        _fetch_l1_micro=_fetch_l1_micro,
        _vwap_gate_ab_bypass=_vwap_gate_ab_bypass,
        _shadow_dedup_claim=_shadow_dedup_claim,
        _shadow_dedup_commit=_shadow_dedup_commit,
        _shadow_portfolio_emit=_shadow_portfolio_emit,
        _short_chase_shadow_row=_short_chase_shadow_row,
        _screening_age_hours=_screening_age_hours,
    )


def _classify_ghost_cause(conn, symbol: str, self_trade_id: int) -> str:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec._classify_ghost_cause(conn, symbol, self_trade_id)


def _max_drawdown_from_mfe_mae(mfe_pct: float, mae_pct: float) -> float:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec._max_drawdown_from_mfe_mae(mfe_pct, mae_pct)


def _net_pnl_pair(pnl: float, pnl_pct: float, fees_total, tr_row):
    """[P1b 2026-10-09] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec._net_pnl_pair(pnl, pnl_pct, fees_total, tr_row)


def classify_be_exit(be_net: Optional[float], be_drift: Optional[float]) -> str:
    """[P1a 2026-10-08] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec.classify_be_exit(be_net, be_drift)


def _include_observed_prices_in_extremes(metadata, direction, entry_price, exit_price) -> bool:
    """(2026-09-18) 实现下沉到 app/core/trade_extremes.include_observed_prices。

    与 scripts/repair_trade_exit_invariants.py 共用同一份 —— 此前那份实现对本函数
    处理方式不同的输入类 ("极值字段存在但不可解析") 采取相反动作 (此处覆盖, 那边跳过)。
    留这个薄壳只为调用点 (本文件 1 处) 与既有测试不必改签名; 口径一律以
    app/core/trade_extremes 为准, 换口径只改那一处。
    """
    from app.core.trade_extremes import include_observed_prices

    return include_observed_prices(metadata, direction, entry_price, exit_price)


def _close_trade_record(conn, trade_id: int, exit_price: float, exit_reason: str,
                        pnl: float, pnl_pct: float, holding_hours: float,
                        inst_id = None, symbol: str = None,
                        close_futu_order_id: str = None, *, exit_intent_id=None,
                        exit_owner=None, exit_metadata=None, exit_time=None,
                        fee_broker=None) -> bool:
    """[阶段12] 本体迁 close_flow; 注入时间/分类/状态回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import close_flow as _cf
    return _cf._close_trade_record(
        conn, trade_id, exit_price, exit_reason, pnl, pnl_pct, holding_hours,
        inst_id=inst_id, symbol=symbol, close_futu_order_id=close_futu_order_id,
        exit_intent_id=exit_intent_id, exit_owner=exit_owner,
        exit_metadata=exit_metadata, exit_time=exit_time, fee_broker=fee_broker,
        datetime_cls=datetime,
        _now_et=_now_et,
        logger=logger,
        _sl_burst_alerted=_sl_burst_alerted,
        classify_be_exit=classify_be_exit,
        _classify_ghost_cause=_classify_ghost_cause,
        _include_observed_prices_in_extremes=_include_observed_prices_in_extremes,
        _max_drawdown_from_mfe_mae=_max_drawdown_from_mfe_mae,
        _net_pnl_pair=_net_pnl_pair,
    )


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
            "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)"
            + _PB_CLOSED_TWIN_NOT_EXISTS
            + " LIMIT 1",
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


# 2026-09-17 (用户裁「按建议执行」; 9/16 MRCY/SNDK 案): "同一 broker 单已有已平孪生行" 排除谓词.
#   建仓簿记 CAS 失败 → P3 补录出孪生 open 行 (无 decision_id), playbook 重试写入带身份行并正常平仓;
#   孪生行悬空 (exit_time NULL) ⇒ 被当"未平仓"反复平仓, exit_lifecycle 报 exit_owner_conflict,
#   快环每 20s 重试且按终态失败升级 ERROR+"仓位可能卡住/无止损保护" (9/16 实测 192 条/32min, 全是误报:
#   仓位 09:52 已真平, broker_qty=0). 同一 broker 单既有已平行 ⇒ 该行的身份已被结算, 不得再入未平集.
#   2026-09-17 GIPR 案补充: POSITION_SYNC 的 external_close 行没有共享 futu_order_id,
#   但同日/同环境/同方向/同数量/同入场价的已平孪生同样证明该 Playbook 行已外部结算,
#   不得再进入 ghost 豁免或退出行源 (21:30 误报 exit_position_ownership_conflict).
#   SSOT: ghost 豁免 (load_playbook_open_symbols) 与退出快环行源共用本片段, 防两处漂移.
#   安全性: order-id 精确匹配优先; external_close 兜底匹配必须同日+环境+方向+数量+入场价.
_PB_CLOSED_TWIN_NOT_EXISTS = (
    " AND NOT EXISTS ("
    "  SELECT 1 FROM trade_records t2"
    "  WHERE t2.id <> trade_records.id"
    "  AND t2.exit_price > 0 AND t2.exit_time IS NOT NULL AND t2.exit_time <> ''"
    "  AND ("
    "   (t2.futu_order_id = trade_records.futu_order_id"
    "    AND trade_records.futu_order_id <> '')"
    "   OR (t2.source_type = 'POSITION_SYNC'"
    "    AND t2.exit_reason = 'external_close'"
    "    AND t2.symbol = trade_records.symbol"
    "    AND t2.direction = trade_records.direction"
    "    AND t2.quantity = trade_records.quantity"
    "    AND t2.entry_price = trade_records.entry_price"
    "    AND t2.trade_date = trade_records.trade_date"
    "    AND t2.trade_mode = trade_records.trade_mode)"
    "  )"
    ")"
)


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
            "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL) "
            "AND decision_id IS NOT NULL AND decision_id <> '' "
            + _PB_CLOSED_TWIN_NOT_EXISTS
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


def patient_exit_trigger_telemetry(exit_reason, profit_pct, pb_cfg, armed):
    """Record why a DEAD_SIGNAL trigger did or did not enter the patient-exit arm."""
    if exit_reason != "DEAD_SIGNAL_EXIT":
        return {}
    try:
        max_underwater = float(pb_cfg.get("patient_exit_max_underwater_pct", 1.0))
        profit = float(profit_pct)
        in_window = -max_underwater <= profit <= 0.0
    except (TypeError, ValueError):
        max_underwater = None
        profit = None
        in_window = None
    return {
        "profit_pct_at_trigger": round(profit, 4) if profit is not None else None,
        "in_arm_window": in_window,
        "armed": bool(armed),
        "max_underwater_pct": max_underwater,
    }


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
    except Exception as _pe_day_e:
        # 2026-10-08 棘轮清剿: 跨日守卫异常须留痕 (原为 except: pass)
        logger.warning(f"[playbook] patient_exit 跨日守卫异常 (守卫本轮失效): {_pe_day_e}",
                       exc_info=True)
    try:
        _stop_pct = float(pb_cfg.get("patient_exit_stop_pct", 1.5))
        _deadline = int(pb_cfg.get("patient_exit_deadline_hhmm", 1550))
    except Exception:
        _stop_pct, _deadline = 1.5, 1550
    # 2026-10-17 审计: 固定 1550 在 13:00 半日市永不触发, armed 仓会绕过
    #   PRE_CLOSE_EXIT 直接过夜。有效截止取 min(配置, 当日收盘前 10 分钟)。
    _PATIENT_EXIT_CLOSE_BUFFER_MIN = 10
    try:
        _deadline_minutes = (_deadline // 100) * 60 + (_deadline % 100)
        from app.utils.holiday_calendar import get_us_close_minutes_et
        _close_minutes = get_us_close_minutes_et(now_et.date())
        _effective_deadline_minutes = min(
            _deadline_minutes, _close_minutes - _PATIENT_EXIT_CLOSE_BUFFER_MIN
        )
    except Exception as _pe_close_e:
        logger.warning(
            f"[playbook] patient_exit 收盘时刻解析异常 (回退配置截止 {_deadline:04d}): "
            f"{_pe_close_e}",
            exc_info=True,
        )
        _effective_deadline_minutes = _deadline_minutes
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
        #   BE_FALLBACK_EXEMPT (DEB-20260909-exit-params 行动项C): 本腿回退有意保持 0,
        #   不收敛 BE_FALLBACK_FEE_CUSHION_PCT — 在仓旧仓 meta 无该标记,
        #   改回退=变更在仓实时行为, 超出"零交易行为变更"裁决范围.
        try:
            _fill_cushion = float((meta or {}).get("_be_cushion_fee_aware", 0) or 0)
        except Exception:
            _fill_cushion = 0.0
        if _profit_pct >= _fill_cushion:
            return "fill", f"价格站回净保本线 ({_profit_pct:.2f}% ≥ {_fill_cushion:.2f}%), 净保本退出"
    try:
        _now_minutes = now_et.hour * 60 + now_et.minute
        if _now_minutes >= _effective_deadline_minutes:
            _effective_hhmm = (
                _effective_deadline_minutes // 60 * 100
                + _effective_deadline_minutes % 60
            )
            return "eod", (
                f"{_now_minutes // 60 * 100 + _now_minutes % 60:04d} ≥ "
                f"{_effective_hhmm:04d} 强制兜底 (收盘前缓冲)"
            )
    except Exception as _pe_eod_e:
        # 2026-10-08 棘轮清剿: EOD 兜底判定异常须留痕 (原为 except: pass)
        logger.warning(f"[playbook] patient_exit EOD 兜底判定异常 (本轮回退规则链): {_pe_eod_e}",
                       exc_info=True)
    return "", ""


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str, earnings_guard=None):
    """共用退出规则判断 — P2a (2026-09-06) 起本体在 app.playbook.exit_engine
    (711 行逐字迁入, 零行为变更)。本转发器保留: 10+ 既有测试文件直引本路径,
    生产调用点 monitor_playbook_sim_exits 经本转发器到达, 判定语义不变。
    """
    from app.playbook.exit_engine import _check_playbook_exit_rules as _impl
    return _impl(direction, entry_price, current_price, initial_sl, peak_price, meta,
                 held_minutes, minutes_to_close, minutes_from_open, entry_time_str,
                 earnings_guard=earnings_guard)


def _load_horizon_config(horizon: str) -> Dict[str, Any]:
    """读取指定 horizon 的配置参数.

    P2a (2026-09-06) 起本体在 app.playbook.exit_engine (逐字迁入)。本转发器保留供
    _sl_fallback_exit_reason 与既有测试 (monkeypatch 落点在本模块全局, 语义不变)。
    """
    from app.playbook.exit_engine import _load_horizon_config as _impl
    return _impl(horizon)


def _sl_fallback_exit_reason(pnl_pct: float, entry_time_str: str, horizon: str,
                             *, attribution=None) -> str:
    """止损兜底平仓的 exit_reason 归因 (2026-08-28 全链路复查).

    SL 兜底跑在 _check_playbook_exit_rules 规则2 之前, 跳空打穿 SL 的隔夜仓因此全部
    被记成 SL_INITIAL, OVERNIGHT_GAP_STOP 桶全史 0 笔 → 持夜风险被归因给"止损纪律",
    并沿 pattern-mining 变成标的档案里的 "LONG+SL_INITIAL 亏钱模式" 教训。
    这里用与规则2 相同的尺子重贴标签: 隔夜仓 且 亏损幅度已达该 horizon 的
    overnight_gap_stop_pct → OVERNIGHT_GAP_STOP; 其余 (含任何异常) 保持 SL_INITIAL。

    ⚠️ 2026-09-24 勘误: 本判据**没有 gap 校验** (只是"持仓过夜 + 亏损≥3%"), 所以该桶混着
    真开盘跳空与纯盘中触发两类 (实测 9/23 桶内 5 笔: GAP/UBER 在开盘 09:30 出场, CRM 09:49 /
    RIVN 10:23 是盘中触发)。归因只到"隔夜仓尾部"这一层, 不得当成 gap 事件读, metadata 里
    也没有 gap% 字段可查。

    只影响记账归因, 不影响平仓动作; 两值同在 intraday_reentry._HARD_STOP_REASONS,
    再入场门行为中性。
    """
    try:
        if attribution is None:
            _gap_stop_pct = float(_load_horizon_config(horizon).get("overnight_gap_stop_pct", 3.0))
            overnight = _is_overnight_position(entry_time_str)
        else:
            _gap_stop_pct = float(attribution["gap_stop_pct"])
            overnight = attribution["overnight"]
        if float(pnl_pct) <= -_gap_stop_pct and overnight:
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

# 2026-09-14 (复核方修复, 用户裁「按照建议执行」建议①): 瞬态 pending 与终态失败分流.
#   病灶: exit_lifecycle 的 8 个 `*_pending` error 语义是"成交证据尚未回流"(腿刚提交、
#   券商回报未到), **不是失败**; 但三处调用点 (EXIT_HELD 7732 / EXIT_PENDING 8432 /
#   SL_EXIT_PENDING 8062) 一律按 `not result["success"]` 升级为 `[playbook平仓失败]`
#   ERROR + 飞书 HIGH("仓位可能卡住/无止损保护").
#   生产实证 (2026-09-14 21:33 US.FCX SHORT 64, intent 959d5f30c267):
#     21:33:09 leg2 MARKET 提交 → 21:33:12/22/32 快环三轮查到 pending → 3 条 ERROR + 1 条飞书
#     → 21:33:43 leg2 FILLED_ALL 64股@67.835 → intent BOOKED, trade_records#1483 净 +$157.77,
#       broker_qty_after=0.0 与 DB 一致 ⇒ 平仓全程成功, 告警窗口 34s 纯误报.
#   同类误报日次: 9/11 20 条 / 9/12 12 条 / 9/14 6 条 (9/11 那 781 条 CLOSE_FAILED 是真事故,
#   机制不同 — 本次分流对它零影响, 因其 error 不带 _pending 后缀).
#   为何不按 intent age 判宽限: 三处调用点只有 _resume_playbook_exits 有 intent 上下文,
#   另两处拿不到 created_at ⇒ 在本函数内自记"首次见到该 key 的时刻", 一处注入覆盖三处.
#   终态零延迟 (宁可多告不可漏告): error 不以 `_pending` 结尾 (25 个, 含 9/11 事故根因
#   exit_identity_required、exit_order_identity_conflict、exit_terminal_shortfall、
#   exit_account_mismatch) 或 error 为空 ⇒ 立即 ERROR + 推送, 与修复前逐字同语义.
_close_fail_first_seen = {}
_TRANSIENT_PENDING_GRACE_S = 120.0
#   宽限取值依据: sim 实测腿提交→回报落定 34s (LIMIT 3s 超时 + MARKET 30s deadline + 4s 回报
#   滞后), 120s ≈ 3.5x 余量; 真卡单持续数分钟至数小时, 仍会升级告警, 只是晚 120s
#   (该仓位的 broker SL 单仍挂着, 保护不因告警延后而消失).


def _alert_close_failure(symbol: str, target_env: str, reason: str, detail: str,
                         *, error: str = "") -> None:
    """平仓失败结构化告警 (ERROR 日志 + 飞书 HIGH). record-only, 不改平仓行为, 永不抛异常.

    2026-07-20 修复3b: 平仓失败意味着仓位卡住、无止损保护, 必须可见. monitor 每轮巡检
    命中同一失败会重复调用, 故按 symbol+当日+reason 去重, 避免刷屏 (进程重启后重记一次).

    2026-09-14: 新增 keyword-only `error` (exit_lifecycle 的 error 码原文). 以 `_pending`
    结尾 = 成交证据未回流的瞬态 ⇒ 首次只记 INFO, 超过 _TRANSIENT_PENDING_GRACE_S 才升级;
    其余 (含 error 为空, 兼容未传该参数的调用点) = 终态失败, 立即升级. 见上方注释实证.
    """
    try:
        _key = f"{symbol}:{date.today().isoformat()}:{reason}"
        if _key in _close_fail_alerted:
            return
        # ── 瞬态 pending 宽限分流 (见 _close_fail_first_seen 上方注释) ──
        if str(error).endswith("_pending"):
            _now = time.time()
            _first = _close_fail_first_seen.setdefault(_key, _now)
            if len(_close_fail_first_seen) > 512:
                # 防长跑无界增长: 键含日期, 跨日自然换键, 此处清掉非今日的残留
                _today = date.today().isoformat()
                for _k in [k for k in _close_fail_first_seen if _today not in k]:
                    _close_fail_first_seen.pop(_k, None)
            _age = _now - _first
            if _age < _TRANSIENT_PENDING_GRACE_S:
                logger.info(
                    f"[playbook平仓待回报] {symbol} ({target_env}) reason={reason} error={error} "
                    f"age={_age:.0f}s < 宽限 {_TRANSIENT_PENDING_GRACE_S:.0f}s — "
                    f"成交证据未回流, 暂不升级为失败告警 (超窗仍会告)"
                )
                return
            detail = (f"{detail} (瞬态 pending 已持续 {_age:.0f}s > "
                      f"宽限 {_TRANSIENT_PENDING_GRACE_S:.0f}s, 判定为真卡单)")
        logger.error(
            f"[playbook平仓失败] {symbol} ({target_env}) reason={reason} detail={detail} — "
            f"仓位可能卡住/无止损保护, 需人工核查"
        )
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


def _playbook_exit_owner(db_path, row, meta):
    """[P1b 2026-10-09] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec._playbook_exit_owner(db_path, row, meta)


def _playbook_exit_decision(reason, reference_price, meta, holding_hours=0.0):
    """[P1b 2026-10-09] 本体逐字迁 pipeline_parts.exit_classification; 转发器保 monkeypatch 缝."""
    from app.playbook.pipeline_parts import exit_classification as _ec
    return _ec._playbook_exit_decision(reason, reference_price, meta, holding_hours=holding_hours)


def _reconcile_broker_stop_exit(dual, conn, row, meta) -> bool:
    """Book a live position already closed by its broker-side STOP order."""
    if row.get("source_type") != "PLAYBOOK_LIVE":
        return False
    stop_order_id = str(meta.get("sl_broker_order_id") or "").strip()
    if not stop_order_id:
        return False

    try:
        filled_price, filled_qty = dual.query_order_fill(
            stop_order_id, target_env="real"
        )
    except Exception as error:
        logger.warning(
            "[playbook live] broker STOP fill reconciliation pending "
            f"{row.get('symbol')} stop_id={stop_order_id}: {error}",
            exc_info=True,
        )
        return False

    quantity = float(row.get("quantity") or 0)
    if not filled_price or filled_qty != quantity:
        return False

    entry_price = float(row["entry_price"] or 0)
    direction = str(row["direction"] or "").upper()
    pnl = (
        (filled_price - entry_price) * quantity
        if direction == "LONG"
        else (entry_price - filled_price) * quantity
    )
    booked = _close_trade_record(
        conn,
        int(row["id"]),
        float(filled_price),
        "SL_BROKER_STOP",
        pnl,
        pnl / (entry_price * quantity),
        0.0,
        symbol=str(row["symbol"]),
        close_futu_order_id=stop_order_id,
        exit_metadata={
            "exit_price_source": "broker_stop_reconciled",
            "broker_stop_reconciled": True,
            "broker_stop_order_id": stop_order_id,
            "broker_stop_dealt_qty": filled_qty,
        },
        fee_broker=dual,
    )
    if booked:
        logger.info(
            f"[playbook live] broker STOP fill reconciled {row['symbol']} "
            f"qty={quantity} price={filled_price} stop_id={stop_order_id}"
        )
    return bool(booked)


def _release_live_broker_stop_for_rule_exit(dual, conn, row, meta) -> Dict[str, Any]:
    """Release only this arm's broker STOP before a LIVE rule exit.

    The normal broker STOP remains the offline protection. When an online rule
    exit needs to close the position, this gate first books a raced STOP fill,
    otherwise cancels the STOP and waits for a terminal no-fill cancellation.
    It never releases an unmatched/manual order.
    """
    if row.get("source_type") != "PLAYBOOK_LIVE":
        return {"action": "proceed"}
    stop_order_id = str(meta.get("sl_broker_order_id") or "").strip()
    if not stop_order_id:
        return {"action": "proceed"}

    symbol = str(row.get("symbol") or "")
    direction = str(row.get("direction") or "").upper()
    quantity = float(row.get("quantity") or 0)
    expected_side = "SELL" if direction == "LONG" else "BUY"
    expected_remark = f"pb_stop_{symbol}_real"
    terminal_no_fill = {
        "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED",
    }

    def _status():
        raw = dual.query_order_status(stop_order_id, target_env="real")
        if not isinstance(raw, dict) or not raw.get("success"):
            error = str(raw.get("error", "broker STOP status unavailable") if isinstance(raw, dict) else raw)
            return None, f"broker_stop_status_pending: {error}"
        return raw, ""

    def _identity_matches(raw):
        try:
            raw_qty = float(raw.get("qty", 0) or 0)
        except (TypeError, ValueError):
            raw_qty = -1.0
        values = {
            "order_id": str(raw.get("order_id", "") or "").strip(),
            "code": str(raw.get("code", "") or "").strip(),
            "trd_side": str(raw.get("trd_side", "") or "").rsplit(".", 1)[-1].upper(),
            "order_type": str(raw.get("order_type", "") or "").rsplit(".", 1)[-1].upper(),
            "remark": str(raw.get("remark", "") or "").strip(),
        }
        return (
            values["order_id"] == stop_order_id
            and values["code"] == symbol
            and values["trd_side"] == expected_side
            and values["order_type"] == "STOP"
            and values["remark"] == expected_remark
            and raw_qty == quantity
        )

    def _dealt_qty(raw):
        try:
            return float(raw.get("dealt_qty", 0) or 0)
        except (TypeError, ValueError):
            return -1.0

    raw, error = _status()
    if raw is None:
        return {"action": "hold", "error": error}
    if not _identity_matches(raw):
        logger.error(
            "[playbook live] broker STOP identity mismatch "
            f"{symbol} stop_id={stop_order_id}"
        )
        return {"action": "hold", "error": "broker_stop_identity_mismatch"}

    dealt_qty = _dealt_qty(raw)
    if dealt_qty > 0:
        if _reconcile_broker_stop_exit(dual, conn, row, meta):
            return {"action": "reconciled"}
        return {"action": "hold", "error": "broker_stop_fill_reconcile_pending"}
    if str(raw.get("order_status", "") or "").upper() in terminal_no_fill:
        return {"action": "proceed"}

    cancel_result = dual.cancel_order(stop_order_id, target_env="real")
    if not cancel_result.get("success"):
        raw, error = _status()
        if (
            raw is not None
            and _identity_matches(raw)
            and _dealt_qty(raw) == 0
            and str(raw.get("order_status", "") or "").upper() in terminal_no_fill
        ):
            return {"action": "proceed"}
        return {"action": "hold", "error": "broker_stop_cancel_failed"}

    for _ in range(20):
        time.sleep(0.25)
        raw, error = _status()
        if raw is None:
            return {"action": "hold", "error": error}
        if not _identity_matches(raw):
            return {"action": "hold", "error": "broker_stop_identity_mismatch"}
        dealt_qty = _dealt_qty(raw)
        if dealt_qty > 0:
            if _reconcile_broker_stop_exit(dual, conn, row, meta):
                return {"action": "reconciled"}
            return {"action": "hold", "error": "broker_stop_fill_reconcile_pending"}
        if str(raw.get("order_status", "") or "").upper() in terminal_no_fill:
            logger.info(
                "[playbook live] broker STOP released before rule exit "
                f"{symbol} stop_id={stop_order_id}"
            )
            return {"action": "proceed"}

    return {"action": "hold", "error": "broker_stop_cancel_pending"}


def _annotate_breakeven_close_telemetry(
    meta: Dict[str, Any],
    price: float,
    direction: str,
    quantity: float,
    price_source: str,
) -> None:
    """Annotate BE close telemetry against the actual net-line floor.

    The legacy ``drift_from_be_line`` value compared the fill with ``limit_px``
    without reversing SHORT exits.  That made a favorable SHORT fill look like a
    breach and, on the passive marketable path, compared against a price that
    was already outside the net floor.  The value is now direction-aware and
    based on ``floor_px`` (falling back to ``_be_net_line_price``), with an
    explicit breach amount and execution-guarantee flag.
    """
    from app.execution.exit_lifecycle import number

    if not isinstance(meta.get("_be_close_telemetry"), dict):
        return
    telemetry = dict(meta["_be_close_telemetry"])
    limit = number(telemetry.get("limit_px"), positive=True)
    floor = number(telemetry.get("floor_px"), positive=True)
    if floor is None:
        floor = number(meta.get("_be_net_line_price"), positive=True)

    limit_filled = price_source == "broker_limit_deal"
    telemetry["outcome"] = "limit_filled" if limit_filled else "limit_fallback_market"
    telemetry["floor_enforced"] = bool(limit_filled and floor is not None)
    if not limit_filled:
        telemetry["fallback_fill"] = price

    if limit is not None:
        limit_delta = (price - limit) if direction == "LONG" else (limit - price)
        telemetry["fill_vs_limit_bps"] = round(limit_delta / limit * 10000.0, 2)

    if floor is None:
        telemetry["drift_from_be_line"] = None
        telemetry["fill_vs_net_line_bps"] = None
        telemetry["net_line_delta_usd"] = None
        telemetry["net_breach_usd"] = None
        telemetry["drift_basis"] = "missing"
    else:
        floor_delta = (price - floor) if direction == "LONG" else (floor - price)
        drift_bps = round(floor_delta / floor * 10000.0, 2)
        line_delta_usd = round(floor_delta * quantity, 2)
        telemetry["drift_from_be_line"] = drift_bps
        telemetry["fill_vs_net_line_bps"] = drift_bps
        telemetry["net_line_delta_usd"] = line_delta_usd
        telemetry["net_breach_usd"] = round(max(0.0, -line_delta_usd), 2)
        telemetry["drift_basis"] = "floor_px"

    meta["_be_close_telemetry"] = telemetry


def _book_playbook_exit_result(dual, result, conn=None):
    from app.execution.exit_lifecycle import number
    if not (result.get("success") and result.get("order_filled")
            and result.get("exit_state") in ("FILLED_UNBOOKED", "BOOKED")):
        return 0
    owner = dict(result["owner"])
    decision = result["decision"]
    intent_id = result["exit_intent_id"]
    price = number(result.get("filled_price"), positive=True)
    qty = number(result.get("dealt_qty"), positive=True)
    if not intent_id or price is None or qty != number(owner["quantity"], positive=True):
        raise ValueError("exit fill evidence incomplete")
    owned_connection = conn is None
    if owned_connection:
        import sqlite3
        if not Path(owner["db_path"]).is_file():
            raise ValueError("exit source ledger missing")
        conn = sqlite3.connect(owner["db_path"])
        conn.row_factory = sqlite3.Row
    try:
        receipt = {"kind": owner["kind"], "db_path": owner["db_path"],
                   "exit_intent_id": intent_id}
        if owner["kind"] == "compensation":
            entry_id = owner.get("entry_order_id") or ""
            candidates = conn.execute(
                "SELECT * FROM trade_records WHERE (?<>'' AND futu_order_id=?) "
                "OR (symbol=? AND entry_time=?)",
                (entry_id, entry_id, owner["symbol"], owner["entry_time"]),
            ).fetchall()
            matching = []
            for candidate in candidates:
                candidate = dict(candidate)
                candidate_meta = json.loads(candidate.get("metadata_json") or "{}")
                identity_fields = ("symbol", "direction", "source_type", "entry_time", "entry_price", "quantity")
                if (not isinstance(candidate_meta, dict)
                        or any(candidate[key] != owner[key] for key in identity_fields)
                        or candidate_meta.get("playbook_instruction_id") != owner["instruction_id"]
                        or str(candidate.get("futu_order_id") or "") != entry_id):
                    raise ValueError("compensation entry identity conflict")
                matching.append(candidate)
            if len(matching) > 1:
                raise ValueError("compensation entry identity ambiguous")
            if not matching:
                receipt.update(instruction_id=owner["instruction_id"],
                               execution_attempt=owner["execution_attempt"],
                               entry_order_id=owner.get("entry_order_id"))
                if not dual.acknowledge_close(intent_id, receipt):
                    logger.error("[playbook] compensation receipt acknowledgement pending: %s", intent_id)
                return 0
            owner["trade_id"] = matching[0]["id"]
        receipt["trade_id"] = owner["trade_id"]
        meta = dict(decision["metadata"])
        for key in ("_log_tag", "_symbol", "_skip_allowed"):
            meta.pop(key, None)
        meta["exit_price_source"] = result["price_source"]
        meta["broker_qty_before"] = result.get("broker_qty_before")
        meta["broker_qty_after"] = result.get("broker_qty_after")
        reference = number(decision.get("reference_price"), positive=True)
        direction = owner["direction"]
        if reference is not None:
            slippage = (reference - price) / reference * 10000.0
            meta["exit_slippage_bps"] = round(slippage if direction == "LONG" else -slippage, 2)
        if isinstance(meta.get("patient_exit"), dict):
            patient = dict(meta["patient_exit"])
            immediate = number(patient.get("immediate_exit_price"), positive=True)
            if immediate is not None:
                delta = (price - immediate) * qty
                patient["patient_vs_immediate_delta"] = round(delta if direction == "LONG" else -delta, 2)
            meta["patient_exit"] = patient
        _annotate_breakeven_close_telemetry(
            meta, price, owner["direction"], qty, result["price_source"])
        entry = float(owner["entry_price"])
        pnl = (price - entry) * qty if direction == "LONG" else (entry - price) * qty
        exit_reason = decision["reason"]
        if "sl_fallback_attribution" in decision:
            exit_reason = _sl_fallback_exit_reason(
                pnl / (entry * qty) * 100, owner["entry_time"], meta.get("horizon", "short"),
                attribution=decision["sl_fallback_attribution"])
        confirmed_at = number(result.get("confirmed_at"), positive=True)
        if confirmed_at is None:
            raise ValueError("exit confirmation time missing")
        closed_at = datetime.fromtimestamp(confirmed_at, _now_et()).isoformat()
        booked = _close_trade_record(
            conn, owner["trade_id"], price, exit_reason, pnl, pnl / (entry * qty),
            decision.get("holding_hours", 0.0), inst_id=owner.get("instruction_id"),
            symbol=owner["symbol"], close_futu_order_id=result.get("futu_order_id"),
            exit_intent_id=intent_id, exit_owner=owner, exit_metadata=meta, exit_time=closed_at,
            fee_broker=dual)
        if booked and decision.get("emit_overnight_shadow"):
            try:
                from app.playbook.overnight_hold_shadow import emit as overnight_emit
                overnight_emit(owner["symbol"], direction, decision["reason"], price, entry,
                               pnl, notional=round(price * qty, 2),
                               decision_id=decision.get("decision_id"))
            except Exception:
                logger.exception("[playbook] overnight shadow failed after close: %s", intent_id)
        if not dual.acknowledge_close(intent_id, receipt):
            logger.error("[playbook] trade receipt acknowledgement pending: %s", intent_id)
        return int(bool(booked))
    finally:
        if owned_connection:
            conn.close()


# ── P0-1 滞留出场意图: 年龄升级告警 (2026-10-17 审计) ──────────────────────
# blocked_exits 是符号级冻结: 一个未结算 intent 会让该标的 SL/TP/时间/因子规则
# 全部停摆。审计建议先影子计数再改行为 (受控重评/重提交须预注册), 本批只补
# 「滞留年龄升级告警」— 不释放 intent、不改评估/下单行为, 每 (intent, tier) 一次:
#   30min HIGH / 4h CRITICAL; 日常重复失败仍由 _alert_close_failure 按日去重兜底。
_BLOCKED_EXIT_ALERT_TIERS = ((30 * 60.0, "HIGH"), (4 * 3600.0, "CRITICAL"))
_blocked_exit_age_alerted: set = set()


def _alert_blocked_exit_age(intent, result, *, now_ts=None) -> None:
    """滞留意图年龄升级 (record/alert-only, 永不抛异常)."""
    try:
        created_at = float(intent.get("created_at") or 0.0)
        if created_at <= 0:
            return
        now_ts = time.time() if now_ts is None else float(now_ts)
        age = max(0.0, now_ts - created_at)
        tier = level_name = None
        for threshold, name in _BLOCKED_EXIT_ALERT_TIERS:
            if age >= threshold:
                tier, level_name = threshold, name
        if tier is None:
            return
        intent_id = str(intent.get("intent_id") or "")
        key = f"{intent_id}:{int(tier)}"
        if key in _blocked_exit_age_alerted:
            return
        _blocked_exit_age_alerted.add(key)
        if len(_blocked_exit_age_alerted) > 512:
            _blocked_exit_age_alerted.clear()  # 集合只防刷屏, 清空最坏=下次多推一轮
        symbol = intent.get("symbol") or "?"
        target_env = intent.get("target_env") or "?"
        state = result.get("exit_state") or intent.get("state") or "?"
        error = result.get("error") or intent.get("last_error") or ""
        from app.push import get_push_hub
        from app.push.message import Category, Level
        get_push_hub().push_text(
            f"🚨 [出场意图滞留 {age / 60:.0f}min] {symbol} ({target_env}) "
            f"state={state} error={error or '-'}\n"
            f"该标的 SL/TP/时间/因子规则被 blocked_exits **整体冻结**, 仓位无规则保护。\n"
            f"intent={intent_id}\n"
            f"处置: 核对券商侧订单状态; 确认可释放后执行 "
            f"scripts/release_stuck_exit_intent.py --intent-id {intent_id} --apply\n"
            f"(审计 P0-1; 自动受控重评须先由影子定阈值, 本告警只升级可见性)",
            level=getattr(Level, level_name),
            category=Category.RISK_ALERT,
        )
    except Exception as error:
        logger.warning("[playbook] 滞留出场意图年龄告警失败: %s", error, exc_info=True)


def _resume_playbook_exits(*, allow_submit):
    from collections import Counter
    from app.execution.dual_broker import get_dual_broker
    closed = 0
    blocked = {}
    try:
        dual = get_dual_broker()
        pending = dual.pending_closes()
    except Exception:
        logger.exception("[playbook] exit ledger enumeration failed; new exits paused")
        return closed, blocked, False
    if pending:
        counts = dict(Counter(item["state"] for item in pending))
        oldest = max(0.0, time.time() - min(item["created_at"] for item in pending))
        logger.info("[playbook] unsettled exits states=%s oldest_seconds=%.0f", counts, oldest)
    for intent in pending:
        _bkey = (intent["target_env"], intent["symbol"])
        _detail = {
            "target_env": intent["target_env"],
            "symbol": intent["symbol"],
            "intent_id": intent["intent_id"],
            "state": intent.get("state") or "",
            "error": intent.get("last_error") or "",
            "created_at": intent.get("created_at"),
        }
        _prev = blocked.get(_bkey)
        if _prev is None or float(_detail.get("created_at") or 0.0) < float(
                _prev.get("created_at") or 0.0):
            blocked[_bkey] = _detail
        try:
            result = dual.resume_close(intent["intent_id"], allow_submit=allow_submit)
            closed += _book_playbook_exit_result(dual, result)
            _cur = blocked.get(_bkey)
            if _cur is not None:
                _cur["state"] = result.get("exit_state") or _cur.get("state") or ""
                _cur["error"] = result.get("error") or _cur.get("error") or ""
            if _release_eth_held_exit_for_rth_takeover(dual, intent, result):
                blocked.pop(_bkey, None)
                continue
            # [RFC-127 阶段0] ETH出场HELD且RTH已开盘 → 只记录"本可会话升级补腿",
            # 不改恢复/告警行为 (10/1 ACN 首例: SL评估被blocked_exits整体跳过)。
            try:
                from app.playbook.eth_held_exit_shadow import emit_eth_held_exit_shadow
                emit_eth_held_exit_shadow(intent, result)
            except Exception as _eth_held_shadow_error:
                logger.warning(
                    "[playbook] ETH HELD出场影子调用失败 (不影响恢复/告警): %s",
                    _eth_held_shadow_error, exc_info=True)
            if not result.get("success"):
                _alert_blocked_exit_age(intent, result)
                _alert_close_failure(intent["symbol"], intent["target_env"],
                                     "EXIT_" + result.get("exit_state", "HELD"),
                                     f"intent={intent['intent_id']} {result.get('error', '')}",
                                     # 2026-09-14: 传 error 原文 → 瞬态 *_pending 走宽限分流,
                                     # 终态 (identity_conflict 等) 仍零延迟告警. 见函数头注释实证.
                                     error=result.get("error", ""))
        except Exception as error:
            logger.exception("[playbook] exit recovery failed intent=%s", intent["intent_id"])
            try:
                _alert_close_failure(intent["symbol"], intent["target_env"],
                                     "EXIT_RECOVERY_ERROR", f"intent={intent['intent_id']}: {error}")
            except Exception:
                logger.exception("[playbook] exit recovery alert failed")
    return closed, blocked, True


def _release_eth_held_exit_for_rth_takeover(dual, intent, result):
    """Release a terminal ETH-held intent so regular-hours exits can resume."""
    try:
        from app.playbook.eth_held_exit_shadow import build_eth_held_rth_event
        try:
            dealt_qty = float(result.get("dealt_qty"))
        except (TypeError, ValueError):
            return False
        if dealt_qty > 0.0:
            return False
        event = build_eth_held_rth_event(intent, result)
        if event is None:
            return False
        evidence = {
            "remaining_qty": event.get("remaining_qty"),
            "futu_order_id": event.get("futu_order_id"),
            "position_open_guard": event.get("position_open_guard"),
            "rth_open": True,
        }
        archive_path, refuse = dual.release_exit_intent(
            intent["intent_id"], reason="rth_intraday_takeover",
            evidence=evidence, kind="rth_intraday_takeover")
        if archive_path is not None:
            logger.warning(
                "[playbook] ETH HELD出场已移交盘中策略 symbol=%s env=%s "
                "intent=%s archive=%s remaining_qty=%s — SL/BE/trailing/EOD评估恢复",
                intent["symbol"], intent["target_env"], intent["intent_id"],
                archive_path, event.get("remaining_qty"))
            return True
        logger.warning(
            "[playbook] ETH HELD出场移交盘中失败 symbol=%s intent=%s refuse=%s",
            intent["symbol"], intent["intent_id"], refuse)
        return False
    except Exception as error:
        logger.warning(
            "[playbook] ETH HELD出场移交盘中异常 symbol=%s intent=%s: %s",
            intent.get("symbol"), intent.get("intent_id"), error, exc_info=True)
        return False


# ── 退出链取价有界化 (2026-09-22 开盘 OpenD 行情瞬断实证) ────────────────────
# 实证: ET 09:31:09 OpenD 行情连接瞬断 (全历史首次) → 退出快循环 09:30:53~09:32:54
#   在取价链上**无上界阻塞 121s** (watchdog 120s 阈值告警 `playbook_exit_loop`),
#   期间该仓 SL/TP 一轮未评。取价链 = Futu(push 缓存已被重连清空)→批量快照→
#   Finnhub→TwelveData 逐个回落，每层都可能长时间阻塞在坏连接/HTTP 上；且批量
#   miss 后逐仓还会再走一次单原取价 → 阻塞可叠加。
# 修法: 取价一律在**有界线程**内执行 (超时即放弃本轮), 并用最近一次成功价兜底 →
#   快循环心跳恒 ≤ interval (现役 10s), 连接故障期也不停摆; 取价成功即刷新兜底价。
# 兜底价只"迟到"不"捏造": 旧价只在**已跌破线**时才可能触发退出, 不会凭空造出触发。
# 回滚: 删除本块 + 把调用点还原为直接 data_router.get_batch_quotes/get_quote。
_PB_EXIT_PRICE_CACHE: Dict[str, Any] = {}        # symbol -> 最近一次成功报价
_PB_EXIT_PRICE_CACHE_TS: Dict[str, float] = {}   # symbol -> 该报价取到时刻
_PB_EXIT_PRICE_CACHE_MAX_AGE = 120.0             # 兜底价可用上限 (秒); 超龄按无价
_PB_EXIT_FETCH_TIMEOUT = 6.0                     # 批量取价上界 (秒)
_PB_EXIT_FETCH_SINGLE_TIMEOUT = 2.0              # 逐仓回落上界 (秒)
_PB_EXIT_FETCH_INFLIGHT: Dict[str, Any] = {}     # label -> 在途取价线程 (单飞, 防堆积)
_PB_EXIT_FETCH_BOX: Dict[str, Dict[str, Any]] = {}  # label -> 在途线程的结果盒 (超时后收编)
_PB_EXIT_STALE_ALERT_SEC = 30.0                  # 兜底价"可感陈旧"门槛 (仅告警用)


def _pb_exit_price_cache_update(quotes: Dict[str, Any]) -> None:
    """把本轮成功取到的报价并入兜底缓存 (仅接受有正价的 dict)。"""
    now = time.time()
    for sym, q in (quotes or {}).items():
        if isinstance(q, dict) and (q.get("last") or q.get("price")):
            _PB_EXIT_PRICE_CACHE[sym] = q
            _PB_EXIT_PRICE_CACHE_TS[sym] = now


def _pb_exit_price_cache_view(symbols: List[str]) -> Dict[str, Any]:
    """取兜底缓存视图 (仅未超龄的标的; 超龄视为无价, 与"无价跳过"旧行为一致)。"""
    now = time.time()
    out: Dict[str, Any] = {}
    for sym in symbols:
        q = _PB_EXIT_PRICE_CACHE.get(sym)
        if q and (now - _PB_EXIT_PRICE_CACHE_TS.get(sym, 0.0)) <= _PB_EXIT_PRICE_CACHE_MAX_AGE:
            out[sym] = q
    return out


def _pb_exit_bounded_fetch(label: str, fn, timeout_s: float):
    """在有界时间内执行取价调用。

    返回 (value, timed_out, error):
      value     = fn() 的返回值; 超时/异常时为 None
      timed_out = 超时仍在途 (线程被放弃, 由缓存兜底; 线程自行结束, 不再被等待)
      error     = fn() 抛出的异常 (不静默: 由调用方决定日志级别)

    单飞: 同名 label 的上一轮取价仍在途 → 本轮直接返回 (None, True, None),
      不重复发起 — 防连接故障期每轮堆积一个阻塞线程; 若上一轮超时线程已结束,
      先收编其结果入兜底缓存 (不白丢一次成功取价), 再发起本轮。
    """
    prev = _PB_EXIT_FETCH_INFLIGHT.get(label)
    if prev is not None:
        if prev.is_alive():
            return None, True, None
        _prev_r = (_PB_EXIT_FETCH_BOX.pop(label, None) or {}).get("r")
        if isinstance(_prev_r, dict):
            _pb_exit_price_cache_update(_prev_r)
        _PB_EXIT_FETCH_INFLIGHT.pop(label, None)

    box: Dict[str, Any] = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception as _e:  # 取价失败 = 本轮无价, 交给缓存/告警路径
            box["e"] = _e

    th = threading.Thread(target=_run, name="PBExitFetch", daemon=True)
    _PB_EXIT_FETCH_INFLIGHT[label] = th
    _PB_EXIT_FETCH_BOX[label] = box
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        return None, True, None   # 超时: 线程留作下一轮收编, 本轮不阻塞
    _PB_EXIT_FETCH_INFLIGHT.pop(label, None)
    _PB_EXIT_FETCH_BOX.pop(label, None)
    return box.get("r"), False, box.get("e")


def monitor_playbook_exits(scheduler) -> int:
    """[阶段13] 本体迁 exit_monitor_flow; 注入状态/回调/函数属性保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import exit_monitor_flow as _emf
    return _emf.monitor_playbook_exits(
        scheduler,
        monitor_playbook_exits=monitor_playbook_exits,
        datetime_cls=datetime,
        time_module=time,
        logger=logger,
        _PB_EXIT_PRICE_CACHE_TS=_PB_EXIT_PRICE_CACHE_TS,
        _PB_EXIT_FETCH_TIMEOUT=_PB_EXIT_FETCH_TIMEOUT,
        _PB_EXIT_FETCH_SINGLE_TIMEOUT=_PB_EXIT_FETCH_SINGLE_TIMEOUT,
        _PB_EXIT_STALE_ALERT_SEC=_PB_EXIT_STALE_ALERT_SEC,
        _PB_CLOSED_TWIN_NOT_EXISTS=_PB_CLOSED_TWIN_NOT_EXISTS,
        _quote_fail_streak=_quote_fail_streak,
        _quote_fail_alerted=_quote_fail_alerted,
        _alert_close_failure=_alert_close_failure,
        _book_playbook_exit_result=_book_playbook_exit_result,
        _check_playbook_exit_rules=_check_playbook_exit_rules,
        _extract_invalidation_price=_extract_invalidation_price,
        _get_factor_exit_config=_get_factor_exit_config,
        _is_overnight_position=_is_overnight_position,
        _load_horizon_config=_load_horizon_config,
        _pb_exit_bounded_fetch=_pb_exit_bounded_fetch,
        _pb_exit_price_cache_update=_pb_exit_price_cache_update,
        _pb_exit_price_cache_view=_pb_exit_price_cache_view,
        _playbook_exit_decision=_playbook_exit_decision,
        _playbook_exit_owner=_playbook_exit_owner,
        _portfolio_backstop_evaluate=_portfolio_backstop_evaluate,
        _reconcile_broker_stop_exit=_reconcile_broker_stop_exit,
        _release_live_broker_stop_for_rule_exit=_release_live_broker_stop_for_rule_exit,
        _resume_playbook_exits=_resume_playbook_exits,
        _sl_fallback_exit_reason=_sl_fallback_exit_reason,
        _track_bar_peak_trough=_track_bar_peak_trough,
        patient_exit_arm_eligible=patient_exit_arm_eligible,
        patient_exit_eval_wait=patient_exit_eval_wait,
        patient_exit_trigger_telemetry=patient_exit_trigger_telemetry,
    )


# Compatibility alias for legacy callers/tests; the active name reflects both arms.
monitor_playbook_sim_exits = monitor_playbook_exits


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
    """[阶段14] 本体迁 overnight_cleanup; 注入出场回调保 monkeypatch 缝。"""
    from app.playbook.pipeline_parts import overnight_cleanup as _oc
    return _oc._force_close_orphaned_sim_positions(
        scheduler,
        logger=logger,
        _alert_close_failure=_alert_close_failure,
        _book_playbook_exit_result=_book_playbook_exit_result,
        _cohort_b_max_hold_hit=_cohort_b_max_hold_hit,
        _mark_overnight_arm=_mark_overnight_arm,
        _playbook_exit_decision=_playbook_exit_decision,
        _playbook_exit_owner=_playbook_exit_owner,
        _release_live_broker_stop_for_rule_exit=_release_live_broker_stop_for_rule_exit,
        _resume_playbook_exits=_resume_playbook_exits,
        _today_et_str=_today_et_str,
    )
