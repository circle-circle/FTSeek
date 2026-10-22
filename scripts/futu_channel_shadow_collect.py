#!/usr/bin/env python3.11
"""富途未利用通道影子采集 (DEB-20260812-futu-unused-channels 主张A/B, record-only).

背景 (2026-08-12): OpenD/SDK 升级 10.9 后解锁的通道中, capital_flow 有研究支撑
且零行为变更。通道 A (get_derivative_unusual) 已于 2026-08-29 两轮评估后 KILLED,
采集实现退役; 历史 data/futu_deriv_unusual.jsonl 保留。

设计:
  - 采集对象 = scheduler._get_subscription_codes() 活跃集 (基准+当日指令+未平仓)
  - TRADING: 节流 300s/次; AFTER_HOURS: 每日 1 次全量收尾拉取 (盘后序列才完整).
  - capital_flow 按 (symbol, capital_flow_item_time) 合并去重 — API 每次返回全天
    序列, 只追加比已存最大时间戳新的行 (防轮询放大, 2026-08-10 教训).
  - 时间口径: capital_flow_item_time 为美东市场时间 (原样存储).

输出:
  data/futu_capital_flow.jsonl     — {symbol, item_time, in_flow, super/big/mid/sml}
  data/.futu_channel_shadow_state.json — 节流/幂等状态

挂载: scheduler_core._llm_strategy_loop TRADING 分支 (盘末休眠块前) + AFTER_HOURS.
回滚: futu_channels.shadow_collect_enabled=false + 重启.
"""
import json
import logging
import math
import os
import socket
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.futu_channel_shadow")

_BASE = Path(__file__).resolve().parents[1]
_FLOW_OUT = _BASE / "data" / "futu_capital_flow.jsonl"
_STATE = _BASE / "data" / ".futu_channel_shadow_state.json"


def _enabled() -> bool:
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(
            "futu_channels.shadow_collect_enabled", True))
    except Exception:
        return True


# ── 采集前置闸 (2026-09-21) ────────────────────────────────────────────────────
# 病根: 通道"要不要继续采"过去没有任何显式判断点 —— 只有总闸 (shadow_collect_enabled)
#   和几条零散子开关, 于是出现两种坏态: (a) 判据已 FAIL 的通道继续花 IO (iv_hv/异动榜);
#   (b) "判据 FAIL"被误当成"数据没用" ⇒ 差点停掉**有生产消费者**的料 (capital_flow 实锤:
#   原料喂 app/playbook/intraday_reentry.py 盘中资金背离门, 停采会让门 fail-open 退化)。
# 治法 (两道, 都必须**显式**才不会误停):
#   ① 登记里显式写 collection_retired=True (+ 理由) ⇒ 该通道不再采集;
#   ② 该通道独立开关 futu_channels.<channel>_collect_enabled=false ⇒ 停采。
# 刻意**不**从"判据状态"反推是否停采 (KILLED/FAIL ≠ 无消费者): 判据退役与采集退役是两件事,
#   同一条料可以既在喂生产门、又是某条已被证伪的研究判据的输入 (capital_flow 就是)。
# 停采时数据一律保留不删; 重开 = 改登记/开关 + 说明新假设 (留痕)。
def _registry_entry(channel_id: str):
    try:
        from app.playbook.shadow_registry import REGISTRY
        for it in REGISTRY:
            if it.id == channel_id:
                return it
        logger.warning("[futu通道影子] 注册表无此通道 id (%s) ⇒ 采集闸放行", channel_id)
    except Exception as e:                                            # noqa: BLE001
        logger.warning("[futu通道影子] 注册表读取失败 (%s): %s ⇒ 采集闸放行", channel_id, e, exc_info=True)
    return None


def _collector_gate(channel_id: str, switch_key: str, default: bool = True) -> bool:
    """返回 True = 允许采集。停采时记一行 INFO (可见, 不静默)。"""
    it = _registry_entry(channel_id)
    if it is not None and bool(getattr(it, "collection_retired", False)):
        logger.info("[futu通道影子] 停采 %s — 登记 collection_retired=True (数据保留)", channel_id)
        return False
    try:
        from app.core.config_container import get_config
        on = bool(get_config().get_dotted(switch_key, default))
    except Exception as e:                                            # noqa: BLE001
        logger.warning("[futu通道影子] 开关读取失败 %s: %s ⇒ 按默认 %s", switch_key, e, default)
        on = default
    if not on:
        logger.info("[futu通道影子] 停采 %s — 开关 %s=false", channel_id, switch_key)
    return on


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
        logger.warning("_load_state: state file read failed", exc_info=True)
    return {}


def _save_state(st: Dict) -> None:
    try:
        _STATE.write_text(json.dumps(st, ensure_ascii=False))
    except Exception:
        logger.warning("_save_state: state file write failed", exc_info=True)


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
                logger.warning("_max_item_time_per_symbol: jsonl parse", exc_info=True)
                continue
    except Exception:
        logger.warning("_max_item_time_per_symbol: file read failed", exc_info=True)
    return out


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
_IV_OUT = _BASE / "data" / "futu_iv_hv_shadow.jsonl"
_MOVERS_OUT = _BASE / "data" / "futu_top_movers_shadow.jsonl"


def _ofi_timing(row, ingested_at: datetime, previous: Dict,
                phase: str, interval_sec: int) -> Dict:
    from app.utils.timezone import ET, _localize

    update_time = row.get("update_time")
    raw_time = str(update_time) if update_time is not None else None
    age_sec = None
    time_status = "MISSING"
    if raw_time:
        try:
            source_at = _localize(datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S"), ET)
            age_sec = (ingested_at - source_at).total_seconds()
            if age_sec < -2:
                time_status = "FUTURE"
            elif age_sec > 2 * interval_sec:
                time_status = "STALE"
            else:
                time_status = "RECENT"
        except (TypeError, ValueError, OverflowError):
            time_status = "INVALID"

    gap_sec = None
    gap_status = "FIRST_SAMPLE"
    if phase != "TRADING":
        gap_status = "OFF_SESSION"
    elif previous:
        try:
            previous_at = datetime.fromisoformat(previous["ingested_at_utc"])
            if previous_at.tzinfo is None:
                raise ValueError("ingestion timestamp must have a UTC offset")
            if (previous_at.astimezone(ET).date() != ingested_at.astimezone(ET).date()
                    or previous["phase"] != "TRADING"):
                gap_status = "SESSION_RESET"
            else:
                gap_sec = (ingested_at - previous_at).total_seconds()
                gap_status = ("CLOCK_ROLLBACK" if gap_sec < 0 else
                              "GAP" if gap_sec > 2 * interval_sec else "OK")
        except (KeyError, TypeError, ValueError, OverflowError):
            gap_status = "INVALID_PREVIOUS"

    # Snapshot update_time does not establish the freshness of either book side.
    return {
        "ingested_at_utc": ingested_at.isoformat(timespec="microseconds"),
        "snapshot_update_time_et": raw_time,
        "snapshot_age_sec": age_sec,
        "snapshot_time_status": time_status,
        "sample_gap_sec": gap_sec,
        "sample_gap_status": gap_status,
        "timing_threshold_sec": 2 * interval_sec,
    }


def _collect_ofi_baseline(ctx, symbols: List[str], state: Dict, phase: str) -> int:
    """通道 C: OFI 基线快照 (DEB-20260818 P2 重排, 2026-08-21 接入).

    Cont-Kukanov-Stoikov (2014) 增量定义需要连续时点的 bid1/ask1 量,
    本采集按轮存 level-1 快照, 增量 ΔOFI 在评估侧对相邻快照计算.
    数据源: get_market_snapshot 批量 HTTP 免订阅 (试测闭市也有盘口) —
    get_order_book 需 ORDER_BOOK 订阅 (SDK 10.3 时代即被拒, 非新变化).
    用途: capital_flow 增量 IC 的 OFI 基线口径 (预注册纪律'禁裸边际'的解法).
    @ts_et / ingested_at_utc 是采集接收时刻, 不是交易所盘口事件时间;
    update_time 只代表整份快照更新时间, 无 bid/ask 各自源时间 (DEB-20260812 §6.5).
    独立开关可单独回滚.
    """
    from futu import RET_OK
    from app.utils.timezone import ET

    try:
        from app.core.config_container import get_config
        if not bool(get_config().get_dotted(
                "futu_channels.ofi_collect_enabled", True)):
            return 0
    except Exception:
        logger.warning("_collect_ofi_baseline: config read failed", exc_info=True)
    rows = []
    previous_by_symbol = state.get("ofi_last_ingested", {})
    if not isinstance(previous_by_symbol, dict):
        logger.warning("_collect_ofi_baseline: invalid ingestion state")
        previous_by_symbol = {}
    interval_sec = _interval_sec()
    phase = phase.upper()
    try:
        started_ns = time.monotonic_ns()
        ret, data = ctx.get_market_snapshot(symbols[:400])
        ingested_at = datetime.now(timezone.utc)
        duration_ms = (time.monotonic_ns() - started_ns) / 1_000_000
        if ret != RET_OK or data is None or not len(data):
            return 0
        ts_et = ingested_at.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")
        for _, row in data.iterrows():
            try:
                bid1 = float(row.get("bid_price", 0) or 0)
                ask1 = float(row.get("ask_price", 0) or 0)
                bid1_qty = float(row.get("bid_vol", 0) or 0)
                ask1_qty = float(row.get("ask_vol", 0) or 0)
                last = float(row.get("last_price", 0) or 0)
                if not all(math.isfinite(v) for v in (bid1, ask1, bid1_qty, ask1_qty, last)):
                    logger.warning("_collect_ofi_baseline: non-finite snapshot for %s", row.get("code"))
                    continue
                if bid1 <= 0 or ask1 <= 0:
                    continue
                symbol = str(row.get("code", ""))
                if symbol not in symbols:
                    continue
                rows.append({
                    "symbol": symbol, "ts_et": ts_et,
                    "bid1": bid1, "bid1_qty": bid1_qty,
                    "ask1": ask1, "ask1_qty": ask1_qty, "last": last,
                    "request_duration_ms": duration_ms,
                    "collection_phase": phase,
                    **_ofi_timing(row, ingested_at, previous_by_symbol.get(symbol, {}),
                                  phase, interval_sec),
                })
            except Exception:
                logger.warning("_collect_ofi_baseline: row parse failed", exc_info=True)
                continue
    except Exception:
        logger.warning("[futu通道影子] ofi_baseline snapshot 异常", exc_info=True)
    if rows:
        _OFI_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _OFI_OUT.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n")
        current = {s: p for s, p in previous_by_symbol.items()
                   if isinstance(p, dict) and p.get("date_et") == ts_et[:10]}
        current.update({r["symbol"]: {"ingested_at_utc": r["ingested_at_utc"],
                                       "date_et": ts_et[:10], "phase": phase}
                        for r in rows})
        state["ofi_last_ingested"] = current
    return len(rows)


def _collect_iv_hv(ctx, symbols: List[str]) -> int:
    """通道 D: 期权 IV/HV 日序列影子 (2026-08-29 用户裁"4也做", record-only).

    用途: 现 SL/TP vol_buckets 只按 atr_pct 分桶 (playbook_horizon.yaml,
    后视已实现波动率); 富途 get_option_underlying_his_volatility 提供
    股票级 IV (前瞻) + HV 250 日序列 (实测 2026-08-29: US.AAPL ret=0,
    250 行, 无需期权合约代码). 影子攒样验证 "IV/HV 在 atr_pct 分桶上
    有无桶内残存效应" (基线纪律: 在已落地分桶上算残存, 不另起炉灶).

    设计: 日级数据, 仅 AFTER_HOURS 每日 1 次 (盘中轮询纯浪费);
    按 (symbol, time) 去重防轮询放大 (2026-08-10 教训); 接口限流未知,
    标的间 0.3s 缓冲 (活跃集 ≤20 只, 每日 ≤20 次调用).
    独立开关 futu_channels.iv_collect_enabled, 回滚: 置 false + 重启.
    """
    from futu import RET_OK
    try:
        from app.core.config_container import get_config
        if not bool(get_config().get_dotted(
                "futu_channels.iv_collect_enabled", True)):
            return 0
    except Exception:
        logger.warning("_collect_iv_hv: config read failed", exc_info=True)

    # 已存 (symbol, time) 集合 — 增量合并基准 (API 返回全量 250 日序列)
    seen = set()
    if _IV_OUT.exists():
        try:
            for line in _IV_OUT.read_text().splitlines()[-8000:]:
                try:
                    r = json.loads(line)
                    seen.add((r.get("symbol"), r.get("time")))
                except Exception:
                    logger.warning("_collect_iv_hv: jsonl parse", exc_info=True)
                    continue
        except Exception:
            logger.warning("_collect_iv_hv: iv file read failed", exc_info=True)

    rows_buf = []
    for sym in symbols:
        try:
            ret, data, _pk = ctx.get_option_underlying_his_volatility(sym)
            if ret != RET_OK or data is None or not len(data):
                continue
            for _, row in data.iterrows():
                t = str(row.get("time", ""))
                if not t or (sym, t) in seen:
                    continue
                seen.add((sym, t))
                try:
                    rows_buf.append({
                        "symbol": sym,
                        "time": t,
                        "iv": float(row.get("iv", 0) or 0),
                        "hv": float(row.get("hv", 0) or 0),
                        "underlying_price": float(
                            row.get("underlying_price", 0) or 0),
                        "collected_at": datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    logger.warning("_collect_iv_hv: row parse failed", exc_info=True)
                    continue
        except Exception as e:
            logger.warning(f"[futu通道影子] iv_hv 采集异常 {sym}: {e}")
            continue
        time.sleep(0.3)

    if rows_buf:
        _IV_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _IV_OUT.open("a", encoding="utf-8") as f:
            for r in rows_buf:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows_buf)


def _collect_top_movers(ctx) -> int:
    """通道 E: 全市场盘中异动榜快照 (2026-08-29 能力利用率审计, record-only).

    get_top_movers_rank(Market.US) 一次调用返回全市场实时异动榜 (当日涨幅/换手),
    此前系统盘中无任何异动发现通道 (盘中新冒出的标的进不了候选池)。
    影子先行: 每轮存一行快照, 攒样评估 "盘中异动 → 后续窗口收益" 是否有信号,
    不进任何门禁。独立开关: futu_channels.movers_shadow_enabled.
    """
    from futu import RET_OK
    try:
        from app.core.config_container import get_config
        if not bool(get_config().get_dotted(
                "futu_channels.movers_shadow_enabled", True)):
            return 0
    except Exception:
        logger.warning("_collect_top_movers: config read failed", exc_info=True)
    try:
        from futu import Market
        ret, out = ctx.get_top_movers_rank(Market.US, count=100)
        if ret != RET_OK or not isinstance(out, tuple) or len(out) < 2:
            return 0
        _, df = out
        if df is None or not len(df):
            return 0
        movers = []
        for _, r in df.iterrows():
            try:
                movers.append({
                    "symbol": str(r.get("security", "")),
                    "change_pct": round(float(r.get("change_ratio", 0) or 0), 2),
                    "turnover_ratio": round(float(r.get("turnover_ratio", 0) or 0), 2),
                    "volume_ratio": round(float(r.get("volume_ratio", 0) or 0), 2),
                })
            except Exception:
                logger.warning("_collect_top_movers: row parse failed", exc_info=True)
                continue
        if not movers:
            return 0
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "universe": int(out[0] or 0),
            "movers": movers,
        }
        _MOVERS_OUT.parent.mkdir(parents=True, exist_ok=True)
        with _MOVERS_OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return 1
    except Exception as e:
        logger.warning(f"[futu通道影子] top_movers 异常: {e}")
        return 0


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
        # 2026-09-21: 四路采集全部过闸 (登记终态 ⇒ 自动停采; 每路独立开关)。
        n_flow = (_collect_capital_flow(ctx, symbols)
                  if _collector_gate("futu_channel_capital_flow",
                                     "futu_channels.capital_flow_collect_enabled") else 0)
        # 2026-08-21: 通道 C OFI 基线 (DEB-20260818 P2 重排, capital_flow 增量
        # IC 的 OFI 基线口径解法). 独立开关 futu_channels.ofi_collect_enabled.
        n_ofi = (_collect_ofi_baseline(ctx, symbols, st, phase)
                 if _collector_gate("futu_channel_ofi_baseline",
                                    "futu_channels.ofi_collect_enabled") else 0)
        # 2026-08-29: 通道 D IV/HV 日序列 (用户裁"4也做"). 日级数据仅盘后
        # 每日 1 次; 独立开关 futu_channels.iv_collect_enabled.
        n_iv = (_collect_iv_hv(ctx, symbols)
                if is_after_hours and _collector_gate("futu_channel_iv_hv",
                                                      "futu_channels.iv_collect_enabled") else 0)
        # 2026-08-29: 通道 E 全市场盘中异动榜 (能力利用率审计, 用户裁'全做').
        # 市场级快照不依赖 watchlist; 盘中每轮 1 行. 独立开关
        # futu_channels.movers_shadow_enabled.
        n_movers = (_collect_top_movers(ctx)
                    if not is_after_hours and _collector_gate("futu_channel_top_movers",
                                                              "futu_channels.movers_shadow_enabled") else 0)
    finally:
        try:
            ctx.close()
        except Exception:
            logger.warning("run_collect: ctx.close() failed", exc_info=True)
    st["last_collect_ts"] = now
    if is_after_hours:
        st["last_afterhours_date"] = str(date.today())
    _save_state(st)
    if n_flow or n_ofi or n_iv or n_movers:
        logger.info(
            f"[futu通道影子] 采集完成 ({phase}): capital_flow +{n_flow} 行, "
            f"ofi_baseline +{n_ofi} 快照, "
            f"iv_hv +{n_iv} 行, movers +{n_movers} 快照, 标的 {len(symbols)} 只")
