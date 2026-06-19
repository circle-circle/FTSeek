#!/usr/bin/env python3
"""
Position Protection Monitor — 极简止损+追踪止盈（人工确认模式）

逻辑:
  1. 查数据库获取持仓
  2. Futu API获取实时价格
  3. 检查止损 / 追踪止盈触发
  4. 飞书告警（不自动执行）
  5. 更新追踪止盈线（只升不降）

运行: cron 每5分钟 (盘中)
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── Load .env.us ──
_env_file = ROOT / ".env.us"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

ET = timezone(timedelta(hours=-4))
DB_PATH = ROOT / "data" / "ftseek_us_live.db"
STATE_FILE = ROOT / "data" / "protection_alerts_sent.json"
FEISHU_URL = os.getenv("FEISHU_WEBHOOK_URL_US", "")

# ── Config defaults ──
# ATR-based stop-loss (与 ExitMonitor atr_stop 一致: entry ± 4.0×ATR)
ATR_SL_MULTIPLIER = 4.0
# Fallback ATR estimate when cache miss (2% of price)
ATR_FALLBACK_PCT = 0.02
# ATR 缓存有效期 (秒) — 与 ExitMonitor 刷新周期对齐 (5min=300s)
ATR_CACHE_TTL = 300
# ATR-based trailing stop (与 ExitMonitor scale_out 一致: 1.0 ATR 激活, 1.0 ATR 追踪)
TRAILING_ACTIVATION_ATR = 1.0   # profit ≥ 1.0 ATR 时激活
TRAILING_TRAIL_ATR = 1.0        # 追踪距离 = 1.0×ATR
# ATR 缓存 DB 路径
ATR_CACHE_DB = ROOT / "data" / "atr_cache.db"


def is_market_hours() -> bool:
    """ET 4:00-20:00 (pre-market through after-hours), weekdays only."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


def load_sent_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            today = datetime.now(ET).strftime("%Y-%m-%d")
            return {k: v for k, v in data.items() if k == today}
        except Exception:
            pass
    return {}


def save_sent_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def get_positions() -> list:
    """Fetch open positions from DB."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, direction, size, avg_entry_price, "
        "stop_loss_price, trailing_stop_price, "
        "stop_loss_triggered, trailing_stop_triggered, "
        "entry_time, thesis "
        "FROM open_positions WHERE size > 0"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_db(symbol: str, **kwargs):
    """Update position fields."""
    conn = sqlite3.connect(str(DB_PATH))
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [symbol]
    conn.execute(f"UPDATE open_positions SET {sets} WHERE symbol = ?", vals)
    conn.commit()
    conn.close()


def get_prices(symbols: list) -> dict:
    """Get realtime prices via Futu get_market_snapshot."""
    try:
        from futu import OpenQuoteContext, RET_OK
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_market_snapshot(symbols)
            if ret == RET_OK and len(data) > 0:
                return {
                    row["code"]: float(row["last_price"])
                    for _, row in data.iterrows()
                    if float(row["last_price"]) > 0
                }
        finally:
            ctx.close()
    except Exception as e:
        print(f"[protection] Futu price fetch failed: {e}", file=sys.stderr)
    return {}


def send_feishu(text: str):
    """Send alert to Feishu."""
    if not FEISHU_URL:
        print("[protection] No FEISHU_WEBHOOK_URL_US set", file=sys.stderr)
        return
    import urllib.request
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": text}
    }).encode()
    req = urllib.request.Request(
        FEISHU_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[protection] Feishu send failed: {e}", file=sys.stderr)


def get_atr_from_cache(symbol: str) -> float:
    """从 ExitMonitor ATR 缓存 DB 读取最新 ATR 值.

    DB 由 ExitMonitor._persist_atr_to_db() 维护.
    缓存过期 (>ATR_CACHE_TTL) 或 DB 不存在时返回 0.0.
    """
    if not ATR_CACHE_DB.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(ATR_CACHE_DB), timeout=5)
        row = conn.execute(
            "SELECT atr FROM atr_cache WHERE symbol = ? AND ts > ?",
            (symbol, time.time() - ATR_CACHE_TTL),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        print(f"[protection] ATR cache read failed for {symbol}: {e}", file=sys.stderr)
        return 0.0


def get_atr_for_position(symbol: str, entry_price: float) -> float:
    """获取 ATR: 优先缓存, fallback 到 entry × 2%."""
    atr = get_atr_from_cache(symbol)
    if atr <= 0:
        atr = entry_price * ATR_FALLBACK_PCT
    return atr


def should_alert(sent_state: dict, symbol: str, alert_type: str) -> bool:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    sent_today = sent_state.get(today, {})
    sent_for_sym = set(sent_today.get(symbol, []))
    return alert_type not in sent_for_sym


def mark_alerted(sent_state: dict, symbol: str, alert_type: str):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if today not in sent_state:
        sent_state[today] = {}
    if symbol not in sent_state[today]:
        sent_state[today][symbol] = []
    sent_state[today][symbol].append(alert_type)


def calculate_stop_loss(entry_price: float, direction: str, symbol: str) -> float:
    """Calculate stop loss using ATR (与 ExitMonitor atr_stop 一致: entry ± 4.0×ATR)."""
    atr = get_atr_for_position(symbol, entry_price)
    if direction == "LONG":
        return round(entry_price - atr * ATR_SL_MULTIPLIER, 2)
    else:  # SHORT
        return round(entry_price + atr * ATR_SL_MULTIPLIER, 2)


def check_protection(positions: list, prices: dict, sent_state: dict) -> list:
    """Check all positions for stop loss / trailing stop triggers."""
    alerts = []

    for pos in positions:
        sym = pos["symbol"]
        if sym not in prices:
            continue

        price = prices[sym]
        entry = pos["avg_entry_price"]
        direction = pos["direction"]
        size = pos["size"]

        if entry <= 0 or size <= 0:
            continue

        # ── Calculate P&L ──
        if direction == "LONG":
            pnl_pct = (price - entry) / entry
        else:
            pnl_pct = (entry - price) / entry

        # ── 1. Ensure stop_loss_price is set ──
        sl = pos["stop_loss_price"]
        if sl is None or sl == 0:
            sl = calculate_stop_loss(entry, direction, sym)
            update_db(sym, stop_loss_price=sl)
            atr_used = get_atr_for_position(sym, entry)
            print(f"[protection] {sym} auto-set stop_loss=${sl} (ATR=${atr_used:.3f} × {ATR_SL_MULTIPLIER})")

        # ── 2. Check stop loss trigger ──
        sl_triggered = False
        if direction == "LONG" and price <= sl:
            sl_triggered = True
        elif direction == "SHORT" and price >= sl:
            sl_triggered = True

        if sl_triggered and not pos["stop_loss_triggered"]:
            update_db(sym, stop_loss_triggered=1)
            bare_sym = sym.replace("US.", "")
            if should_alert(sent_state, bare_sym, "stop_loss"):
                mark_alerted(sent_state, bare_sym, "stop_loss")
                msg = (
                    f"🔴 {bare_sym} 触发止损\n"
                    f"方向: {direction} | 入场: ${entry:.2f} | 现价: ${price:.2f}\n"
                    f"止损价: ${sl:.2f} | P&L: {pnl_pct*100:+.1f}%\n"
                    f"请在Dashboard确认操作"
                )
                send_feishu(msg)
                alerts.append(msg)

        # ── 3. Update trailing stop (ATR-based: 与 ExitMonitor scale_out 一致) ──
        ts = pos["trailing_stop_price"]
        atr = get_atr_for_position(sym, entry)
        # 激活条件: 浮盈 ≥ 1.0 ATR (替代原 +8% 固定阈值)
        activation_price = entry + atr * TRAILING_ACTIVATION_ATR if direction == "LONG" \
            else entry - atr * TRAILING_ACTIVATION_ATR
        trail_distance = atr * TRAILING_TRAIL_ATR

        activation_met = False
        if direction == "LONG" and price >= activation_price and pnl_pct > 0:
            activation_met = True
        elif direction == "SHORT" and price <= activation_price and pnl_pct > 0:
            activation_met = True

        if activation_met:
            # 追踪距离 = 1.0×ATR from current price (替代原 3% 固定回撤)
            if direction == "LONG":
                new_ts = round(price - trail_distance, 2)
            else:
                new_ts = round(price + trail_distance, 2)

            should_update = False
            if ts is None or ts == 0:
                should_update = True
            elif direction == "LONG" and new_ts > ts:
                should_update = True  # only raise
            elif direction == "SHORT" and new_ts < ts:
                should_update = True  # only lower

            if should_update:
                old_ts = ts
                update_db(sym, trailing_stop_price=new_ts, trailing_stop_triggered=0)
                ts = new_ts
                bare_sym = sym.replace("US.", "")
                if should_alert(sent_state, bare_sym, "trailing_activated"):
                    mark_alerted(sent_state, bare_sym, "trailing_activated")
                    action = "激活" if old_ts is None or old_ts == 0 else "上调" if direction == "LONG" else "下调"
                    msg = (
                        f"🚀 {bare_sym} 追踪止盈已{action}\n"
                        f"浮盈: {pnl_pct*100:+.1f}% | 现价: ${price:.2f}\n"
                        f"ATR: ${atr:.3f} | 止盈线: ${new_ts:.2f}（回撤{TRAILING_TRAIL_ATR:.0f}×ATR=${trail_distance:.2f}触发）"
                    )
                    send_feishu(msg)
                    alerts.append(msg)

        # ── 4. Check trailing stop trigger ──
        if ts and ts > 0 and not pos["trailing_stop_triggered"]:
            ts_triggered = False
            if direction == "LONG" and price <= ts:
                ts_triggered = True
            elif direction == "SHORT" and price >= ts:
                ts_triggered = True

            if ts_triggered:
                update_db(sym, trailing_stop_triggered=1)
                bare_sym = sym.replace("US.", "")
                if should_alert(sent_state, bare_sym, "trailing_stop"):
                    mark_alerted(sent_state, bare_sym, "trailing_stop")
                    msg = (
                        f"🟢 {bare_sym} 触发追踪止盈\n"
                        f"方向: {direction} | 入场: ${entry:.2f} | 现价: ${price:.2f}\n"
                        f"止盈线: ${ts:.2f} | P&L: {pnl_pct*100:+.1f}%\n"
                        f"请在Dashboard确认操作"
                    )
                    send_feishu(msg)
                    alerts.append(msg)

    return alerts


def main():
    if not is_market_hours():
        return  # silent outside market hours

    positions = get_positions()
    if not positions:
        return

    symbols = [p["symbol"] for p in positions]
    prices = get_prices(symbols)
    if not prices:
        print("[protection] No prices available, skipping", file=sys.stderr)
        return

    sent_state = load_sent_state()
    alerts = check_protection(positions, prices, sent_state)
    save_sent_state(sent_state)

    if alerts:
        print(f"[protection] {len(alerts)} alerts sent")
    # else: silent (cron no_agent mode)


if __name__ == "__main__":
    main()
