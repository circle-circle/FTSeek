"""盘中指标/纯影子域 (阶段 5 迁自 app/playbook/pipeline.py)。

首批只迁 VWAP 斜率与双锚影子纯函数; 判定顺序与返回值不变。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_CAPITAL_FLOW_CACHE: Dict[str, Any] = {}
_STREAK_BAN_CACHE: Dict[str, Any] = {}
_VOL_CURVE_CACHE: Dict[str, Any] = {}
_LIQUIDITY_CACHE: Dict[str, Any] = {}
_BORROW_POOL_CACHE: Dict[str, Any] = {}

# 尾部窗口读取 (2026-11-15): data/replay_bars_1m/{sym}.json 单文件 5-7 MB / 8.5 万根,
# 原先每标的每日首读全量 json.load 只为取最近 ~20 日 ⇒ 日首 8-15 标的 = 40-105 MB
# 解析突发。从文件尾按 _BAR_READ_CHUNK 回读, 攒够天数后只解析尾部片段。
_BAR_READ_CHUNK = 1 << 20
_BAR_START_RE = re.compile(rb'\["(\d{4}-\d{2}-\d{2})')


def _resolve_tz_provider(now_et_provider):
    """统一 now_et_provider 契约 = **返回 tzinfo** (配 `datetime.now(tz)` 使用)。

    2026-11-15 根因修复: 本模块 6 处默认分支原写 `from app.utils.timezone import now_et`,
    而 `now_et()` 返回的是 **datetime**, 与函数体 `datetime.now(now_et_provider())` 的
    用法自相矛盾 ⇒ 任何"不注入 provider"的直接调用必抛
    `TypeError: tzinfo argument must be None or of a tzinfo subclass`。
    生产 wrapper (app/playbook/pipeline.py) 注入的是 `_now_et` (返回 ET tzinfo),
    所以线上从未暴露 —— 属**默认值契约错误**, 只咬直接调用/脚本/新用例。
    """
    if now_et_provider is not None:
        return now_et_provider
    from app.utils.timezone import ET as _ET
    return lambda: _ET


def _resolve_repo_root(module_file=None) -> Path:
    """定位仓库根 (含 data/replay_bars_1m 的那一级), 不依赖调用方文件深度。

    2026-11-15 根因修复: 原写法是 `Path(module_file or __file__).parents[2]` ——
    这个"上跳两级"只对 `app/playbook/pipeline.py` 成立; 用本模块自己的 `__file__`
    作默认值时会算成 `app/` ⇒ 找 `app/data/replay_bars_1m/…` 必然不存在 ⇒
    直接调用静默返回 None (不报错, 看起来像"数据不足")。现改为按标记目录向上找,
    与调用方文件所在层级解耦。
    """
    start = Path(module_file) if module_file else Path(__file__)
    for cand in start.resolve().parents:
        if (cand / "data" / "replay_bars_1m").is_dir():
            return cand
    return Path(__file__).resolve().parents[3]


def _read_bars_window(path: Path, days_needed: int):
    """只解析文件尾部覆盖最近 days_needed 个交易日的 bar (与全量解析同值)。

    格式契约: 生产端 (scripts/fetch_minute_bars.py) 写的是单行 JSON 数组,
    每根 bar 形如 ``["YYYY-MM-DD HH:MM:SS", o, h, l, c, v]``。这里按 ``["`` + 日期
    定位元素起点, 用 ``json.loads(b"[" + 片段)`` **整体解析** (不逐条正则拼数),
    因此结果与 ``json.load`` 逐值相同; 片段起点之后的字节一直延伸到 EOF, 不会截断元素。

    任何边界/格式异常都 raise (由调用方回落全量 json.load) —— 宁可慢不能错:
    该曲线是盘中量能门的分母 (交易判定输入)。
    """
    with path.open("rb") as source:
        source.seek(0, 2)
        pos = source.tell()
        buf = b""
        while pos > 0:
            step = min(_BAR_READ_CHUNK, pos)
            pos -= step
            source.seek(pos)
            buf = source.read(step) + buf
            if len({m.group(1) for m in _BAR_START_RE.finditer(buf)}) >= days_needed:
                break
    if pos == 0:
        return json.loads(buf)  # 已把整文件读进内存 (小文件/数据不足) → 全量语义
    dates = sorted({m.group(1) for m in _BAR_START_RE.finditer(buf)})
    if len(dates) < days_needed:
        raise ValueError("tail window accumulated %d days < %d" % (len(dates), days_needed))
    cutoff = dates[-days_needed]
    for match in _BAR_START_RE.finditer(buf):
        if match.group(1) == cutoff:
            bars = json.loads(b"[" + buf[match.start():])
            if not isinstance(bars, list) or not bars:
                raise ValueError("tail window parse produced empty list")
            return bars
    raise ValueError("cutoff bar start not found in tail window")

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


def _gather_intraday_evidence(
    data_router,
    symbol: str,
    direction: str,
    data_root: Optional[Path] = None,
    *,
    today_provider: Callable[[], str],
    now_et_provider: Callable,
    datetime_cls,
    module_file: str,
) -> Optional[Dict[str, Any]]:
    """盘中方向证据叠加; record-only, 失败返回 None。"""
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
            "pulse_date": None, "pulse_age_min": None, "pulse_stale_reason": None,
        }
        # 板块脉搏: symbol→sector (deep_sector_rotation) × 实时涨跌 (sector_pulse)
        # data_root 仅供测试注入 (默认走生产 data/): 脉搏新鲜度守卫需要构造
        #   "陈旧/当日"两种文件, 而测试禁止写生产 data/ (tests/conftest.py)。
        try:
            root = data_root if data_root is not None else (
                Path(module_file).resolve().parents[2] / "data")
            rotation = json.loads(
                (root / "deep_sector_rotation.json").read_text()
            )
            sector = (rotation.get("symbol_sectors") or {}).get(symbol)
            ev["sector"] = sector
            pulse = json.loads((root / "sector_pulse.json").read_text())
            changes = pulse.get("changes") or {}
            today = today_provider()
            # ── [P1-2 2026-09-14, 用户裁「全部处理」盘前数据有效性审计] ────────────
            # 病根: 原实现读 sector_pulse.json 只记 `asof_et` (如 "15:30") **不记日期**,
            #   且**无任何新鲜度检查**。该文件由 scripts/sector_pulse.py 在盘中每 30min
            #   覆盖写 (cron: CST 22:00-23:30 + 00:00-03:30 = ET 10:00-15:30) ⇒
            #   ① topup 盘前窗 (ET 08:45-09:30) 读到的**必然是上一交易日**的收盘读数
            #      (周一更跨周末 = 3 天前), 却记成"盘中脉搏 asof 15:30";
            #   ② 若脉搏 cron 挂掉, 可以是任意久的旧文件。
            #   后果: 该 evidence 会被写进 raw_instruction.evidence 供 S1 记分牌按
            #   "证据一致度"切片 (胜出者才升确认/否决门, Phase2) —— 陈旧读数被当成
            #   当日盘中读数参与切片, 污染候选证据。本条 record-only, 不动任何交易
            #   行为, 只把"这份读数到底新不新"如实记下来。
            # 修法: ① 只接受 date == 当日 ET 的脉搏 (盘前窗必然为旧 ⇒ 如实 null 并
            #   写明原因, 不拿昨收读数冒充日内); ② 补记 pulse_date 与 pulse_age_min,
            #   让 scoreboard 能按陈旧度切片而不是被动继承一个无日期的 "15:30"。
            pulse_date = str(pulse.get("date", "") or "")
            if pulse_date != today:
                ev["pulse_stale_reason"] = (
                    f"脉搏日期 {pulse_date or '缺失'} != 当日 ET {today} (非当日盘中读数)")
            else:
                ev["pulse_date"] = pulse_date
                ev["pulse_asof_et"] = pulse.get("asof_et")
                try:
                    asof = str(pulse.get("asof_et", "") or "")
                    hour, minute = (int(value) for value in asof.split(":")[:2])
                    now = datetime_cls.now(now_et_provider())
                    # 同 ET 日内的"分钟差"即可: 跨日情形已被上面的 date 守卫拒掉,
                    #   故不必做完整日期运算。负值 = 时钟偏差, 如实 null 不取绝对值。
                    age = ((now.hour * 60 + now.minute) - (hour * 60 + minute))
                    ev["pulse_age_min"] = age if age >= 0 else None
                except Exception:
                    ev["pulse_age_min"] = None
                if sector:
                    hit = changes.get(sector)
                    if hit is None:
                        sector_lower = sector.lower()
                        pulse_key = next(
                            (
                                key
                                for keywords, key in _SECTOR_TO_PULSE
                                if any(keyword in sector_lower for keyword in keywords)
                            ),
                            None,
                        )
                        hit = changes.get(pulse_key) if pulse_key else None
                    ev["sector_pulse_chg"] = hit
        except Exception:
            pass
        sign = -1 if is_short else 1
        bits = []
        if ev["vwap_dev_pct"] is not None:
            bits.append(sign * ev["vwap_dev_pct"] > 0)
        if ev["order_flow_imbalance"] is not None:
            bits.append(sign * ev["order_flow_imbalance"] > 0)
        if ev["sector_pulse_chg"] is not None:
            bits.append(sign * float(ev["sector_pulse_chg"]) > 0)
        ev["agree"] = sum(bits)
        ev["avail"] = len(bits)
        return ev
    except Exception:
        return None


def _calc_vwap_slope(
    data_router,
    symbol: str,
    window: int = 5,
    *,
    now_et_provider: Optional[Callable] = None,
) -> Optional[float]:
    """P1-1 证伪门: 滚动窗 VWAP 斜率 (最近 window 根 1min bar 相对前 window 根的变化率)。

    与既有 VWAP 方向门 (当日累计 VWAP 的哪一侧) 正交 — 后者问"价在均价哪侧",
    本函数问"volume-weighted 价近 5min 在往哪走"。用当日累计 VWAP 算斜率是错的:
    d(累计VWAP)/dt 的符号恒等于 sign(近期价 - 累计VWAP), 与方向门同信息量。

    VWAP 优先用 turnover/volume (Futu 原生成交额, 真 VWAP); 缺 turnover 回落
    典型价 (h+l+c)/3 × volume。

    Returns: float 变化率 (BUY 期望 >0) / None 数据不足 → 调用方 fail-open。
    (2026-10-09 迁移自 pipeline.py; now_et_provider 由转发器注入, 缺省回落
    app.utils.timezone.now_et。)
    """
    try:
        futu = getattr(data_router, "futu", None)
        if futu is None or not hasattr(futu, "get_kline"):
            return None
        bars = futu.get_kline(symbol, ktype="K_1M", count=800) or []
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        today = datetime.now(now_et_provider()).strftime("%Y-%m-%d")
        today_bars = [
            bar for bar in bars
            if str(bar.get("time") or bar.get("time_key") or "")[:10] == today
        ]
        if len(today_bars) < window * 2:
            return None

        def vwap(segment):
            numerator = denominator = 0.0
            for bar in segment:
                volume = float(bar.get("volume", 0) or 0)
                if volume <= 0:
                    continue
                turnover = float(bar.get("turnover", 0) or 0)
                numerator += turnover if turnover > 0 else (
                    float(bar.get("high", 0) or 0)
                    + float(bar.get("low", 0) or 0)
                    + float(bar.get("close", 0) or 0)
                ) / 3.0 * volume
                denominator += volume
            return (numerator / denominator) if denominator > 0 else None

        recent = vwap(today_bars[-window:])
        prior = vwap(today_bars[-window * 2:-window])
        if not recent or not prior or prior <= 0:
            return None
        return (recent - prior) / prior
    except Exception:
        return None


def _capital_flow_divergence(
    symbol: str,
    is_short: bool,
    *,
    now_et_provider: Optional[Callable] = None,
    module_file: Optional[str] = None,
) -> Optional[bool]:
    """资金流背离判定 (record-only, 不参与拦截)。"""
    try:
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        day = datetime.now(now_et_provider()).strftime("%Y-%m-%d")
        if _CAPITAL_FLOW_CACHE.get("day") != day:
            by_symbol: Dict[str, Any] = {}
            path = (
                Path(module_file or __file__).resolve().parents[2]
                / "data" / "capital_flow_daily.jsonl"
            )
            if path.exists():
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        record_symbol = record.get("symbol")
                        record_date = str(record.get("date", ""))
                        if not record_symbol:
                            continue
                        previous = by_symbol.get(record_symbol)
                        if previous is None or record_date >= previous[0]:
                            by_symbol[record_symbol] = (
                                record_date,
                                float(record.get("super_in_flow", 0) or 0)
                                + float(record.get("big_in_flow", 0) or 0),
                            )
            _CAPITAL_FLOW_CACHE.clear()
            _CAPITAL_FLOW_CACHE.update({
                "day": day,
                "by_symbol": by_symbol,
            })
        hit = (_CAPITAL_FLOW_CACHE.get("by_symbol") or {}).get(symbol)
        if hit is None or hit[1] == 0:
            return None
        return hit[1] > 0 if is_short else hit[1] < 0
    except Exception:
        return None


def _symbol_streak_banned(symbol: str, pb_cfg) -> bool:
    """低 MFE 连败封禁; 查询异常 fail-open。"""
    try:
        if not bool(pb_cfg.get("symbol_streak_ban_enabled", False)):
            return False
        now = time.time()
        hit = _STREAK_BAN_CACHE.get(symbol)
        if hit and (now - hit[0]) < 300:
            return hit[1]
        lookback = int(pb_cfg.get("symbol_streak_ban_lookback_days", 7))
        min_streak = int(pb_cfg.get("symbol_streak_ban_min_streak", 2))
        mfe_threshold = float(pb_cfg.get("symbol_streak_ban_mfe_pct", 0.5))
        import sqlite3
        from app.core.paths import get_trade_records_db
        connection = sqlite3.connect(str(get_trade_records_db()))
        rows = connection.execute(
            "SELECT realized_pnl, mfe_pct FROM trade_records "
            "WHERE symbol=? AND source_type LIKE 'PLAYBOOK%' "
            "AND exit_time IS NOT NULL AND exit_time!='' "
            "AND trade_date >= date('now', ?) "
            "ORDER BY trade_date DESC, exit_time DESC LIMIT 10",
            (symbol, f"-{lookback} day"),
        ).fetchall()
        connection.close()
        streak = 0
        for realized_pnl, mfe in rows:
            if (
                realized_pnl is not None
                and realized_pnl < 0
                and mfe is not None
                and mfe < mfe_threshold
            ):
                streak += 1
            else:
                break
        banned = streak >= min_streak
        if banned:
            logger.info(
                f"[playbook] 连败封禁: {symbol} 近{lookback}日连续{streak}笔"
                f"低MFE亏损 (阈值{min_streak}笔/MFE<{mfe_threshold}%) → 拒新触发"
            )
        _STREAK_BAN_CACHE[symbol] = (now, banned)
        return banned
    except Exception as error:
        logger.debug(f"[playbook] 连败封禁查询异常 (fail-open): {error}")
        return False


def _intraday_vol_curve_p50(
    symbol: str,
    days: int = 20,
    *,
    now_et_provider: Optional[Callable] = None,
    module_file: Optional[str] = None,
    cache: Optional[Dict[str, Any]] = None,
):
    """近 days 个交易日的同分钟累积成交量中位数曲线。"""
    cache = cache if cache is not None else _VOL_CURVE_CACHE
    try:
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        day = datetime.now(now_et_provider()).strftime("%Y-%m-%d")
        hit = cache.get(symbol)
        if hit is not None and hit[0] == day:
            return hit[1]
        path = (
            _resolve_repo_root(module_file)
            / "data" / "replay_bars_1m" / f"{symbol.replace('.', '_')}.json"
        )
        if not path.exists():
            cache[symbol] = (day, None)
            return None
        with path.open(encoding="utf-8") as source:
            try:
                # 尾部窗口 (days + 3 留缓冲: 文件若已含当日 bar, 仍需 ≥ days 个历史日)
                bars = _read_bars_window(path, days + 3)
            except Exception as _tail_err:
                # W4 棘轮处置 (2026-11-16): 原 logger.debug 把"快读断裂→回落全量解析"藏在
                # LOG_LEVEL=INFO 之下。该曲线是盘中量能门的分母 (交易判定输入), 回落虽不错但
                # 会变慢, 必须可见且带栈。cache 按 (symbol, day) 命中 ⇒ 每标的每日至多一条。
                logger.warning(
                    "[playbook] 量能曲线尾部读取回落全量 %s: %s", symbol, _tail_err,
                    exc_info=True,
                )
                source.seek(0)
                bars = json.load(source)
        by_day: Dict[str, list] = {}
        for bar in bars:
            try:
                by_day.setdefault(str(bar[0])[:10], []).append(float(bar[5]))
            except (IndexError, TypeError, ValueError):
                continue
        dates = sorted(key for key in by_day if key < day)[-days:]
        if len(dates) < 10:
            cache[symbol] = (day, None)
            return None
        cumulative_curves = []
        for date in dates:
            running = 0.0
            cumulative = []
            for volume in by_day[date]:
                running += volume
                cumulative.append(running)
            cumulative_curves.append(cumulative)
        curve = []
        for index in range(max(len(values) for values in cumulative_curves)):
            values = sorted(
                cumulative[index]
                for cumulative in cumulative_curves
                if index < len(cumulative)
            )
            if not values:
                break
            count = len(values)
            curve.append(
                values[count // 2] if count % 2
                else (values[count // 2 - 1] + values[count // 2]) / 2.0
            )
        cache[symbol] = (day, curve or None)
        return curve or None
    except Exception:
        cache[symbol] = (
            datetime.now(_default_now_et(now_et_provider)).strftime("%Y-%m-%d"),
            None,
        )
        return None


def _default_now_et(now_et_provider):
    """返回 ET 的 tzinfo (供 `datetime.now(...)` 用) —— 与 _resolve_tz_provider 同契约。"""
    return _resolve_tz_provider(now_et_provider)()


def _calc_intraday_vol_ratio(
    data_router,
    symbol: str,
    realtime_cum_vol: Optional[float] = None,
    use_p50_curve: Optional[bool] = None,
    *,
    now_et_provider: Optional[Callable] = None,
    curve_provider: Optional[Callable] = None,
) -> Optional[float]:
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
    (2026-10-09 迁移自 pipeline.py; now_et_provider/curve_provider 由转发器注入。)
    """
    try:
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        now = datetime.now(now_et_provider())
        today = now.strftime("%Y-%m-%d")

        today_cumulative_volume = 0.0
        if realtime_cum_vol is None:
            try:
                quote = (
                    data_router.get_quote(symbol)
                    if hasattr(data_router, "get_quote") else None
                )
                if quote:
                    realtime_cum_vol = float(quote.get("volume", 0) or 0)
            except Exception:
                realtime_cum_vol = None
        if realtime_cum_vol and realtime_cum_vol > 0:
            today_cumulative_volume = float(realtime_cum_vol)
        else:
            # 兜底: 聚合 K_1M (300s缓存, 滞后但盘初实时源缺失时聊胜于无)
            futu = getattr(data_router, "futu", None)
            if futu is None or not hasattr(futu, "get_kline"):
                return None
            # count=800 — HTTP 冷路径 start=T-1 时 OpenD 返回"最旧N根",
            #   count 小于范围总数(~780)会拿到纯 T-1 bar → 今日过滤后空 → 门恒 fail-open.
            bars = futu.get_kline(symbol, ktype="K_1M", count=800) or []
            if not bars:
                return None
            today_volumes = []
            for bar in bars:
                bar_time = str(bar.get("time") or bar.get("time_key") or "")
                if bar_time[:10] == today:
                    volume = float(bar.get("volume", 0) or 0)
                    if volume > 0:
                        today_volumes.append(volume)
            if len(today_volumes) < 5:
                return None
            today_cumulative_volume = sum(today_volumes)

        # ── 分母: 已过交易时间 ──
        # 美股常规交易时段: 9:30 - 16:00 ET = 390 min (半日市 13:00 收 = 210 min)
        market_open_minutes = 9 * 60 + 30  # 570
        # 2026-07-25 (隐患扫描修复): 半日市支持 — 否则提前收盘日 elapsed_fraction 分母偏大, 量能门误判
        try:
            from app.utils.holiday_calendar import get_us_close_minutes_et
            market_close_minutes = get_us_close_minutes_et(now.date())
        except Exception:
            market_close_minutes = 16 * 60  # 960
        elapsed = max(
            now.hour * 60 + now.minute - market_open_minutes, 1
        )
        total = market_close_minutes - market_open_minutes

        # ── P1-3 (2026-08-23): VWAP-relative — 分母改用历史同分钟累积量 p50 ──
        #   原线性折算 (日均量 × elapsed/390) 忽略 U 型分布, 开盘时段分母系统性偏小 →
        #   ratio 虚高 → 门在最该确认的时段恒开 (见 _intraday_vol_curve_p50 docstring)。
        #   ⚠️ 本分支会实质改变量能门行为 (阈值 0.8 从"几乎恒过"变为真实过滤), 这是修门的
        #   目的而非副作用。覆盖外标的 (~45 票之外) 自动回落线性, 故行为变化仅限有曲线的票。
        #   回滚: volume_gate_vwap_relative 改 false。
        if use_p50_curve is not None:
            use_curve = use_p50_curve
        else:
            try:
                from app.core.config_container import get_config
                use_curve = bool(get_config().get_dotted(
                    "playbook.execution_parameters.volume_gate_vwap_relative",
                    True,
                ))
            except Exception:
                use_curve = False
        if use_curve:
            curve_provider = curve_provider or _intraday_vol_curve_p50
            curve = curve_provider(symbol)
            # bar i (09:31) 覆盖开盘后第 1 分钟 → 开盘后 elapsed 分钟对应 index elapsed-1
            index = min(
                max(elapsed - 1, 0),
                (len(curve) - 1) if curve else 0,
            )
            if curve and curve[index] > 0:
                return round(today_cumulative_volume / curve[index], 3)

        # ── 回落: 最近5日日均成交量 × 时间占比 (线性模型) ──
        daily_bars = data_router.get_daily_bars(symbol, count=10) or []
        # 排除今日 (可能不完整), 取最近5个已完成交易日
        historical_volumes = []
        for daily_bar in daily_bars:
            date = str(daily_bar.get("time", ""))[:10]
            if date >= today:
                continue
            volume = float(daily_bar.get("volume", 0) or 0)
            if volume > 0:
                historical_volumes.append(volume)
        historical_volumes = historical_volumes[-5:]
        if len(historical_volumes) < 3:
            return None
        average_daily_volume = sum(historical_volumes) / len(historical_volumes)
        if average_daily_volume <= 0:
            return None

        elapsed_fraction = min(max(elapsed / total, 0.02), 1.0)
        expected_volume = average_daily_volume * elapsed_fraction
        if expected_volume <= 0:
            return None
        return round(today_cumulative_volume / expected_volume, 3)
    except Exception:
        return None


def _check_liquidity_gate(
    data_router,
    symbol: str,
    current_price: float,
    pb_cfg,
    planned_qty: float = 0.0,
    *,
    now_et_provider: Optional[Callable] = None,
) -> tuple:
    """小盘/低流动性门; 数据不足 fail-open。"""
    try:
        if not pb_cfg.get("liquidity_gate_enabled", True):
            return True, "disabled"
        minimum_price = float(pb_cfg.get("liquidity_gate_min_price", 3.0))
        if current_price and 0 < current_price < minimum_price:
            return False, (
                f"price ${current_price:.2f} < 地板 ${minimum_price}"
            )
    except Exception:
        return True, "cfg_error_failopen"

    try:
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        key = (symbol, datetime.now(now_et_provider()).strftime("%Y-%m-%d"))
        if key not in _LIQUIDITY_CACHE:
            daily_bars = data_router.get_daily_bars(symbol, count=10) or []
            today = key[1]
            volumes = [
                float(bar.get("volume", 0) or 0)
                for bar in daily_bars
                if str(bar.get("time", ""))[:10] < today
                and float(bar.get("volume", 0) or 0) > 0
            ]
            volumes = volumes[-5:]
            if len(volumes) < 3:
                _LIQUIDITY_CACHE[key] = None
            else:
                _LIQUIDITY_CACHE[key] = sum(volumes) / len(volumes)
        average_volume = _LIQUIDITY_CACHE.get(key)
        if not average_volume:
            return True, "no_adv_data_failopen"
        adv_dollar = average_volume * float(current_price or 0)
        if adv_dollar <= 0:
            return True, "no_adv_data_failopen"

        planned_notional = float(planned_qty or 0) * float(current_price or 0)
        if planned_notional > 0:
            notional = planned_notional
        else:
            try:
                from app.core.config_container import get_config
                capital = float(get_config().get_dotted(
                    "capital.fixed_amount", 82000
                ) or 82000)
            except Exception:
                capital = 82000.0
            notional = capital * float(
                pb_cfg.get("max_position_pct", 0.06) or 0.06
            )
        maximum_pct = float(pb_cfg.get("liquidity_gate_max_adv_pct", 0.5))
        ratio_pct = notional / adv_dollar * 100.0
        if ratio_pct > maximum_pct:
            return False, (
                f"名义额 ${notional:.0f} = ADV$ {adv_dollar / 1e6:.2f}M 的 "
                f"{ratio_pct:.2f}% > {maximum_pct}%"
            )
        return True, f"ok ({ratio_pct:.3f}% of ADV)"
    except Exception as error:
        return True, f"failopen: {type(error).__name__}"


def _check_borrow_pool_gate(
    symbol: str,
    planned_qty: float,
    pb_cfg,
    *,
    now_et_provider: Optional[Callable] = None,
) -> tuple:
    """SHORT 借券池门; 查询异常 fail-open。"""
    try:
        if not pb_cfg.get("borrow_pool_gate_enabled", False):
            return True, "disabled"
        if now_et_provider is None:
            now_et_provider = _resolve_tz_provider(None)
        key = (
            symbol,
            datetime.now(now_et_provider()).strftime("%Y-%m-%d"),
        )
        if key not in _BORROW_POOL_CACHE:
            from app.execution.dual_broker import get_dual_broker
            information = get_dual_broker().get_short_pool([symbol])
            _BORROW_POOL_CACHE[key] = (information or {}).get(symbol)
        pool_info = _BORROW_POOL_CACHE.get(key)
        if not pool_info:
            return True, "no_pool_data_failopen"
        if pool_info.get("shortable") is False:
            return False, (
                "is_short_permit=False (券商不可卖空, sim 假成交陷阱)"
            )
        pool = pool_info.get("pool")
        cover = float(pb_cfg.get("borrow_pool_gate_min_cover", 10.0))
        if pool is not None and planned_qty and planned_qty > 0:
            if pool < planned_qty * cover:
                return False, (
                    f"券池 {pool:,.0f}股 < 计划 {planned_qty:.0f}股 × "
                    f"{cover:.0f} 安全倍率"
                )
            return True, f"ok (券池覆盖 {pool / planned_qty:.0f}x)"
        return True, "ok"
    except Exception as error:
        return True, f"failopen: {type(error).__name__}"


def _resolve_generated_anchor(
    instruction: Dict[str, Any], is_short: bool
):
    """生成锚解析 (RFC-123 阶段0, 纯函数)。"""
    try:
        raw = instruction.get("raw_instruction_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except Exception:
                raw = {}
        if isinstance(raw, dict):
            planned_entry = raw.get("planned_entry")
            if isinstance(planned_entry, (int, float)) and float(planned_entry) > 0:
                return float(planned_entry), "planned_entry"
    except Exception:
        pass
    try:
        from app.playbook.entry_band import CHASE_ZONE_PCT
        edge = (
            instruction.get("entry_low") if is_short
            else instruction.get("entry_high")
        )
        if edge:
            anchor = (
                float(edge) / (1 - CHASE_ZONE_PCT) if is_short
                else float(edge) / (1 + CHASE_ZONE_PCT)
            )
            if anchor > 0:
                return anchor, "band_edge"
    except (TypeError, ValueError) as error:
        logger.warning(
            f"[双锚影子] band 沿反推解析失败 (回退 missing): {error}",
            exc_info=True,
        )
    except Exception:
        pass
    return None, "missing"


def _anchor_trigger_geometry(
    anchor,
    current_price,
    is_short: bool,
    knife: float,
    chase: float,
    vol_ok: bool,
    near_miss_tol: float,
):
    """反事实触发几何 (RFC-123 阶段0, 纯函数)。

    2026-11-12: 判定体统一到 `entry_band.classify_trigger` —— 生产 `check_intraday_trigger`、
    本影子、开盘禁入窗观测三处原先各有一份同义分支, 改一处不同步另外两处就会让
    "反事实几何 / 观测值"与生产判定脱钩 (本文件的测试 `test_geometry_stays_in_sync_*`
    正是为此存在)。现在只有一份实现, 该测试改为钉"三处都委托同一函数"。
    保留本函数的 None 契约: 无效价 → (None, None), 下游按 None 判"几何不可算"。
    """
    try:
        anchor_value = float(anchor or 0)
        price = float(current_price or 0)
    except (TypeError, ValueError):
        return None, None
    if anchor_value <= 0 or price <= 0:
        return None, None
    from app.playbook.entry_band import classify_trigger as _classify
    return _classify(bool(is_short), anchor_value, price,
                     knife, chase, vol_ok, near_miss_tol)
