#!/usr/bin/env python3.11
"""富途未利用通道影子采集 (DEB-20260812-futu-unused-channels 主张A/B, record-only).

背景 (2026-08-12): OpenD/SDK 升级 10.9 后解锁的通道中, 两条有研究支撑且零行为变更:
  A. get_derivative_unusual — 期权大单异动 (Cremers-Weinbaum 2010 等: 知情资金
     先进期权后进正股). DA skill 侧已注入 prompt (llm_synth.py:231), 但无结构化
     落库 → 无法与成交结局 join 验证"大单流领先正股方向"假说. 本脚本补落库.
  B. get_capital_flow — 分钟级大小单净流入 (每次返回全天序列, 391行/日).
     DA skill 侧仅生成时单次快照, 无盘中连续采样 → 无法做增量 IC. 本脚本补采样.

设计:
  - 采集对象 = scheduler._get_subscription_codes() 活跃集 (基准+当日指令+未平仓)
  - TRADING: 节流 300s/次 (capital_flow 12标的×1次+derivative_unusual 12标的×1次,
    低于 30次/30s 限流); AFTER_HOURS: 每日 1 次全量收尾拉取 (盘后序列才完整).
  - capital_flow 按 (symbol, capital_flow_item_time) 合并去重 — API 每次返回全天
    序列, 只追加比已存最大时间戳新的行 (防轮询放大, 2026-08-10 教训).
  - derivative_unusual 按事件稳定键去重 (symbol|ts|action|opt|vol|strike|expiry,
    剔除 OI/V-OI/amount 渲染抖动字段; API 返回滚动 7 天窗, 重复拉取会重放).
  - 时间口径: capital_flow_item_time 为美东市场时间 (原样存储); derivative 事件
    时间戳为 OpenD 服务器本地时区 (CST, 原样存储 raw_ts_str, 解析仅做结构提取).

输出:
  data/futu_capital_flow.jsonl     — {symbol, item_time, in_flow, super/big/mid/sml}
  data/futu_deriv_unusual.jsonl    — {symbol, event_ts_raw, side, opt_type, volume,
                                      open_interest, v_oi, amount_usd, strike, expiry, raw}
  data/.futu_channel_shadow_state.json — 节流/幂等状态

挂载: scheduler_core._llm_strategy_loop TRADING 分支 (盘末休眠块前) + AFTER_HOURS.
回滚: futu_channels.shadow_collect_enabled=false + 重启.
"""
import hashlib
import json
import logging
import os
import re
import socket
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.futu_channel_shadow")

_BASE = Path(__file__).resolve().parents[1]
_FLOW_OUT = _BASE / "data" / "futu_capital_flow.jsonl"
_DERIV_OUT = _BASE / "data" / "futu_deriv_unusual.jsonl"
_STATE = _BASE / "data" / ".futu_channel_shadow_state.json"

# 期权大单异动文本行解析 (富途中文文案, 改文案即断流 → 解析失败记 _parse_fail 不抛)
# 动作三态: 买入/卖出/中性 (中性=方向不明的大单, 2026-08-12 实测 SPY 有此变体)
_DERIV_LINE_RE = re.compile(
    r"(?P<ts>\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2})，出现一笔"
    r"(?P<action>买入|卖出|中性)(?P<opt>看涨|看跌)期权交易，"
    r"成交量为(?P<vol>[\d,]+)张，未平仓数为(?P<oi>[\d,]+)张，"
    r"V/OI值为(?P<voi>[\d.]+)，交易金额为(?P<amt>[\d,.]+)USD，"
    r"合约行权价是(?P<strike>[\d.]+)，到期日为(?P<expiry>[\d/]+)。"
)


def _deriv_event_key(symbol: str, ts: str, action: str, opt_type: str,
                     volume: int, strike: float, expiry: str) -> str:
    """事件稳定去重键 (2026-08-12 修复): 剔除 OI/V-OI/amount 易变字段.

    原实现对整行原文取 MD5 — 富途多次渲染同一事件时 V/OI 存在舍入抖动
    (实测 SPY 8.10 23:58 同行 v_oi 0.3/0.2 并存), 旧键会把同一事件判为
    两条 → 逻辑重复 5/354. 稳定键只用事件中不变的身份字段.
    代价: 同分钟同价同量的两笔真实独立交易会被合并 (概率极低, 接受).
    """
    raw = f"{symbol}|{ts}|{action}|{opt_type}|{volume}|{strike}|{expiry}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _enabled() -> bool:
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(
            "futu_channels.shadow_collect_enabled", True))
    except Exception:
        return True


def _interval_sec() -> int:
    try:
        from app.core.config_container import get_config
        return max(120, int(get_config().get_dotted(
            "futu_channels.shadow_collect_interval_sec", 300)))
    except Exception:
        return 300


def _load_state() -> Dict:
    try:
        if _STATE.exists():
            return json.loads(_STATE.read_text())
    except Exception:
        pass
    return {}


def _save_state(st: Dict) -> None:
    try:
        _STATE.write_text(json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def _max_item_time_per_symbol() -> Dict[str, str]:
    """已存 capital_flow 每标的最大 item_time (增量合并基准)."""
    out: Dict[str, str] = {}
    if not _FLOW_OUT.exists():
        return out
    try:
        for line in _FLOW_OUT.read_text().splitlines()[-6000:]:
            try:
                r = json.loads(line)
                sym, t = r.get("symbol"), r.get("item_time")
                if sym and t and t > out.get(sym, ""):
                    out[sym] = t
            except Exception:
                continue
    except Exception:
        pass
    return out


def _seen_deriv_hashes() -> set:
    """已存事件的稳定键集合.

    存量行是旧键 (整行哈希) 写入的, 但结构化字段齐全 → 直接重算稳定键,
    键制切换不会让 7 天滚动窗内的旧事件重放进库.
    """
    seen = set()
    if not _DERIV_OUT.exists():
        return seen
    try:
        for line in _DERIV_OUT.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("event_ts_raw") and r.get("action"):
                    seen.add(_deriv_event_key(
                        r["symbol"], r["event_ts_raw"], r["action"],
                        r.get("opt_type", ""), int(r.get("volume", 0)),
                        float(r.get("strike", 0) or 0), r.get("expiry", "")))
                elif r.get("event_hash"):
                    seen.add(r["event_hash"])  # 结构字段缺失的旧行兜底
            except Exception:
                continue
    except Exception:
        pass
    return seen


def _active_symbols(scheduler) -> List[str]:
    codes = list(scheduler._get_subscription_codes()) if scheduler else []
    try:
        from app.core.config_container import get_config
        cap = int(get_config().get_dotted(
            "futu_channels.shadow_symbols_max", 20))
    except Exception:
        cap = 20
    return [c for c in codes if c.startswith("US.")][:cap]


def _connect_ctx():
    """复用 LV3 provider 的连接纪律: 端口探测 + SDK 日志静默."""
    from app.data.futu_lv3_provider import silence_futu_sdk_logs
    silence_futu_sdk_logs()
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    ok = sock.connect_ex((host, port)) == 0
    sock.close()
    if not ok:
        return None
    from futu import OpenQuoteContext
    return OpenQuoteContext(host=host, port=port)


def _collect_capital_flow(ctx, symbols: List[str]) -> int:
    from futu import RET_OK
    max_t = _max_item_time_per_symbol()
    new_rows = []
    for sym in symbols:
        try:
            ret, data = ctx.get_capital_flow(sym)
            if ret != RET_OK or data is None or not len(data):
                continue
            cutoff = max_t.get(sym, "")
            for _, row in data.iterrows():
                t = str(row.get("capital_flow_item_time", ""))
                if not t or t <= cutoff:
                    continue
                new_rows.append({
                    "symbol": sym,
                    "item_time": t,
                    "in_flow": float(row.get("in_flow", 0) or 0),
                    "super_in_flow": float(row.get("super_in_flow", 0) or 0),
                    "big_in_flow": float(row.get("big_in_flow", 0) or 0),
                    "mid_in_flow": float(row.get("mid_in_flow", 0) or 0),
                    "sml_in_flow": float(row.get("sml_in_flow", 0) or 0),
                    "main_in_flow": str(row.get("main_in_flow", "N/A")),
                })
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"[futu通道影子] capital_flow {sym} 异常: {e}")
    if new_rows:
        _FLOW_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _FLOW_OUT.open("a", encoding="utf-8") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new_rows)


_OFI_OUT = _BASE / "data" / "futu_ofi_baseline.jsonl"


def _collect_ofi_baseline(ctx, symbols: List[str]) -> int:
    """通道 C: OFI 基线快照 (DEB-20260818 P2 重排, 2026-08-21 接入).

    Cont-Kukanov-Stoikov (2014) 增量定义需要连续时点的 bid1/ask1 量,
    本采集按轮存 level-1 快照 {bid1, bid1_qty, ask1, ask1_qty, last},
    增量 ΔOFI 在评估侧 (futu_channel_shadow_eval) 对相邻快照计算.
    数据源: get_market_snapshot 批量 HTTP 免订阅 (实测闭市也有盘口) —
    get_order_book 需 ORDER_BOOK 订阅 (SDK 10.3 时代 8/5 即被拒, 非新变化);
    snapshot 批量方案复用 spread_depth_capture.py 已验证设计 (Qoder 第5轮勘误).
    用途: capital_flow 增量 IC 的 OFI 基线口径
    (预注册纪律'禁裸边际'的解法). 独立开关可单独回滚.
    """
    from futu import RET_OK
    try:
        from app.core.config_container import get_config
        if not bool(get_config().get_dotted(
                "futu_channels.ofi_collect_enabled", True)):
            return 0
    except Exception:
        pass
    rows = []
    from datetime import timezone
    from app.utils.timezone import ET
    now_et = datetime.now(timezone.utc).astimezone(ET)
    ts_et = now_et.strftime("%Y-%m-%d %H:%M:%S")
    try:
        ret, data = ctx.get_market_snapshot(symbols[:400])
        if ret != RET_OK or data is None or not len(data):
            return 0
        for _, row in data.iterrows():
            try:
                bid1 = float(row.get("bid_price", 0) or 0)
                ask1 = float(row.get("ask_price", 0) or 0)
                bid1_qty = float(row.get("bid_vol", 0) or 0)
                ask1_qty = float(row.get("ask_vol", 0) or 0)
                if bid1 <= 0 or ask1 <= 0:
                    continue
                rows.append({
                    "symbol": str(row.get("code", "")), "ts_et": ts_et,
                    "bid1": bid1, "bid1_qty": bid1_qty,
                    "ask1": ask1, "ask1_qty": ask1_qty,
                    "last": float(row.get("last_price", 0) or 0),
                })
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[futu通道影子] ofi_baseline snapshot 异常: {e}")
    if rows:
        _OFI_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _OFI_OUT.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def _collect_deriv_unusual(ctx, symbols: List[str]) -> int:
    from futu import RET_OK
    seen = _seen_deriv_hashes()
    new_rows = []
    parse_fail = 0
    for sym in symbols:
        try:
            ret, data = ctx.get_derivative_unusual(sym)
            if ret != RET_OK or not isinstance(data, dict):
                continue
            content = data.get("content") or ""
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("期权大单异动"):
                    continue  # 空行/标题行
                m = _DERIV_LINE_RE.search(line)
                if m:
                    # 先解析后去重: 稳定键剔除 OI/V-OI/amount (跨次渲染会抖动)
                    _opt_type = "CALL" if m.group("opt") == "看涨" else "PUT"
                    h = _deriv_event_key(
                        sym, m.group("ts"), m.group("action"), _opt_type,
                        int(m.group("vol").replace(",", "")),
                        float(m.group("strike")), m.group("expiry"))
                    if h in seen:
                        continue
                    new_rows.append({
                        "symbol": sym,
                        "event_hash": h,
                        "event_ts_raw": m.group("ts"),
                        "action": m.group("action"),
                        "opt_type": _opt_type,
                        "volume": int(m.group("vol").replace(",", "")),
                        "open_interest": int(m.group("oi").replace(",", "")),
                        "v_oi": float(m.group("voi")),
                        "amount_usd": float(m.group("amt").replace(",", "")),
                        "strike": float(m.group("strike")),
                        "expiry": m.group("expiry"),
                        "raw": line,
                        "collected_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    seen.add(h)
                elif "出现一笔" in line:
                    parse_fail += 1
                # 其余行 = 非事件内容 ([timestamp:] 标记 / IV 评述 / 支撑阻力
                # 评述文本), 静默跳过 — 2026-08-12 实测 content 混合多段内容
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"[futu通道影子] derivative_unusual {sym} 异常: {e}")
    if new_rows:
        _DERIV_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _DERIV_OUT.open("a", encoding="utf-8") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if parse_fail:
        logger.warning(f"[futu通道影子] derivative 文本解析失败 {parse_fail} 行 (富途文案可能变更)")
    return len(new_rows)


def run_collect(scheduler=None, phase: str = "TRADING") -> None:
    """入口 (挂载点显式传 phase). TRADING 节流 300s; AFTER_HOURS 每日 1 次
    (盘后序列才完整)."""
    if not _enabled():
        return
    now = time.time()
    st = _load_state()
    is_after_hours = "AFTER" in phase.upper()

    if is_after_hours:
        if st.get("last_afterhours_date") == str(date.today()):
            return
    else:
        if now - float(st.get("last_collect_ts", 0)) < _interval_sec():
            return

    symbols = _active_symbols(scheduler)
    if not symbols:
        return
    ctx = _connect_ctx()
    if ctx is None:
        logger.warning("[futu通道影子] OpenD 不可达, 跳过本轮采集")
        return
    try:
        n_flow = _collect_capital_flow(ctx, symbols)
        n_deriv = _collect_deriv_unusual(ctx, symbols)
        # 2026-08-21: 通道 C OFI 基线 (DEB-20260818 P2 重排, capital_flow 增量
        # IC 的 OFI 基线口径解法). 独立开关 futu_channels.ofi_collect_enabled.
        n_ofi = _collect_ofi_baseline(ctx, symbols)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    st["last_collect_ts"] = now
    if is_after_hours:
        st["last_afterhours_date"] = str(date.today())
    _save_state(st)
    if n_flow or n_deriv or n_ofi:
        logger.info(
            f"[futu通道影子] 采集完成 ({phase}): capital_flow +{n_flow} 行, "
            f"deriv_unusual +{n_deriv} 事件, ofi_baseline +{n_ofi} 快照, "
            f"标的 {len(symbols)} 只")
