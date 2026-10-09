"""盘中指标/纯影子域 (阶段 5 迁自 app/playbook/pipeline.py)。

首批只迁 VWAP 斜率与双锚影子纯函数; 判定顺序与返回值不变。
"""
from __future__ import annotations

import json
import logging
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
    """滚动窗 VWAP 斜率; 数据不足返回 None。"""
    try:
        futu = getattr(data_router, "futu", None)
        if futu is None or not hasattr(futu, "get_kline"):
            return None
        bars = futu.get_kline(symbol, ktype="K_1M", count=800) or []
        if now_et_provider is None:
            from app.utils.timezone import now_et
            now_et_provider = now_et
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
            from app.utils.timezone import now_et
            now_et_provider = now_et
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
            from app.utils.timezone import now_et
            now_et_provider = now_et
        day = datetime.now(now_et_provider()).strftime("%Y-%m-%d")
        hit = cache.get(symbol)
        if hit is not None and hit[0] == day:
            return hit[1]
        path = (
            Path(module_file or __file__).resolve().parents[2]
            / "data" / "replay_bars_1m" / f"{symbol.replace('.', '_')}.json"
        )
        if not path.exists():
            cache[symbol] = (day, None)
            return None
        with path.open(encoding="utf-8") as source:
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
    if now_et_provider is not None:
        return now_et_provider()
    from app.utils.timezone import now_et
    return now_et()


def _calc_intraday_vol_ratio(
    data_router,
    symbol: str,
    realtime_cum_vol: Optional[float] = None,
    use_p50_curve: Optional[bool] = None,
    *,
    now_et_provider: Optional[Callable] = None,
    curve_provider: Optional[Callable] = None,
) -> Optional[float]:
    """盘中实时量能比; 数据不足返回 None。"""
    try:
        if now_et_provider is None:
            from app.utils.timezone import now_et
            now_et_provider = now_et
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
            futu = getattr(data_router, "futu", None)
            if futu is None or not hasattr(futu, "get_kline"):
                return None
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

        market_open_minutes = 9 * 60 + 30
        try:
            from app.utils.holiday_calendar import get_us_close_minutes_et
            market_close_minutes = get_us_close_minutes_et(now.date())
        except Exception:
            market_close_minutes = 16 * 60
        elapsed = max(
            now.hour * 60 + now.minute - market_open_minutes, 1
        )
        total = market_close_minutes - market_open_minutes

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
            index = min(
                max(elapsed - 1, 0),
                (len(curve) - 1) if curve else 0,
            )
            if curve and curve[index] > 0:
                return round(today_cumulative_volume / curve[index], 3)

        daily_bars = data_router.get_daily_bars(symbol, count=10) or []
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
            from app.utils.timezone import now_et
            now_et_provider = now_et
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
            from app.utils.timezone import now_et
            now_et_provider = now_et
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
    """反事实触发几何 (RFC-123 阶段0, 纯函数)。"""
    try:
        anchor_value = float(anchor or 0)
        price = float(current_price or 0)
    except (TypeError, ValueError):
        return None, None
    if anchor_value <= 0 or price <= 0:
        return None, None
    if is_short:
        if price >= anchor_value:
            return price <= anchor_value * (1 + knife), "pullback"
        if price >= anchor_value * (1 - chase):
            return bool(vol_ok), "chase"
        if price >= anchor_value * (1 - chase - near_miss_tol):
            return bool(vol_ok), "chase_near_miss"
        return False, "out_of_band"
    if price <= anchor_value:
        return price >= anchor_value * (1 - knife), "pullback"
    if price <= anchor_value * (1 + chase):
        return bool(vol_ok), "chase"
    if price <= anchor_value * (1 + chase + near_miss_tol):
        return bool(vol_ok), "chase_near_miss"
    return False, "out_of_band"
