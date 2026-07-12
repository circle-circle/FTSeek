#!/usr/bin/env python3
"""
Lifecycle Monitor — 飞书关键推送 (Sprint 2)

4类推送:
  🔴 止损接近 — 持仓距止损 < 2%
  🔴 Thesis失效 — 价格已穿越失效价位
  🟡 浮盈保护 — 浮盈 > 10% 但无追踪止盈
  ⚫ 紧急 — 单日亏损 > 5% / 系统异常

运行方式:
  - 由 cron 每10分钟执行 (盘中)
  - 盘前/盘后/休市: 自动跳过 (ET 4:00-20:00 以外不推送)
  - 去重: 同一天同一symbol同一alert类型只推一次
  - 静默: 无新告警时不输出 (cron no_agent 模式)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Project root
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env.us for FEISHU_WEBHOOK_URL_US etc.
_ENV_LOADED = False
_env_file = ROOT / ".env.us"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    _ENV_LOADED = True

STATE_FILE = ROOT / "data" / "lifecycle_alerts_sent.json"
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://127.0.0.1:8901")
EXTERNAL_URL = os.getenv("EXTERNAL_DASHBOARD_URL", "https://ft.aitz.cc:8888")

ET = timezone(timedelta(hours=-4))  # US Eastern (EDT, summer)


def is_market_window() -> bool:
    """ET 4:00-20:00 covers pre-market through after-hours."""
    now_et = datetime.now(ET)
    # Weekend check
    if now_et.weekday() >= 5:
        return False
    hour = now_et.hour
    return 4 <= hour < 20


def load_sent_state() -> dict:
    """Load dedup state: {date_str: {symbol: [alert_types]}}"""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Prune old dates (keep today only)
            today = datetime.now(ET).strftime("%Y-%m-%d")
            return {k: v for k, v in data.items() if k == today}
        except Exception:
            pass
    return {}


def save_sent_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def get_session_token() -> str:
    """Create a valid session token for API access."""
    from app.web_dashboard.auth import create_session_token
    return create_session_token()


def fetch_snapshot() -> dict:
    """Fetch dashboard snapshot via local API."""
    import requests
    token = get_session_token()
    s = requests.Session()
    s.cookies.set("ftseek_session", token, domain="127.0.0.1")
    resp = s.get(f"{DASHBOARD_URL}/api/dashboard/snapshot", timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_alerts(lifecycle: list, sent_state: dict) -> list:
    """Check lifecycle items for new alerts. Returns list of alert messages."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    sent_today = sent_state.get(today, {})
    new_alerts = []

    for item in lifecycle:
        sym = item.get("symbol", "").replace("US.", "")
        alert_level = item.get("alert_level", "normal")
        thesis_valid = item.get("thesis_valid")
        pnl_pct = item.get("pnl_pct", 0)
        sl_dist = item.get("sl_distance_pct", 99)
        recs = item.get("recommendations", [])

        sent_for_sym = set(sent_today.get(sym, []))

        # ── Alert 1: Stop loss danger ──
        if alert_level == "danger":
            alert_type = "sl_danger"
            if alert_type not in sent_for_sym:
                if sl_dist < 0:
                    msg = (
                        f"🔴 {sym} — 已跌破止损位\n"
                        f"跌破估算止损 {abs(sl_dist):.1f}%\n"
                        f"P&L: {pnl_pct:+.1f}%\n"
                    )
                else:
                    msg = (
                        f"🔴 {sym} — 止损接近\n"
                        f"距离止损仅 {sl_dist:.1f}%\n"
                        f"P&L: {pnl_pct:+.1f}%\n"
                    )
                if recs:
                    msg += f"建议: {recs[0]}\n"
                msg += f"→ {EXTERNAL_URL}"
                new_alerts.append(("🔴", sym, alert_type, msg))
                sent_for_sym.add(alert_type)

        # ── Alert 2: Thesis invalidated ──
        if thesis_valid is False:
            alert_type = "thesis_invalid"
            if alert_type not in sent_for_sym:
                inv_price = item.get("invalidation_price", 0)
                cur_price = item.get("cur_price", 0)
                msg = (
                    f"🔴 {sym} — Thesis失效\n"
                    f"当前: ${cur_price:.2f} | 失效价: ${inv_price:.2f}\n"
                    f"进场理由不再成立，建议评估退出\n"
                    f"→ {EXTERNAL_URL}"
                )
                new_alerts.append(("🔴", sym, alert_type, msg))
                sent_for_sym.add(alert_type)

        # ── Alert 3: Large unrealized gain without protection ──
        if pnl_pct > 10:
            alert_type = "profit_unprotected"
            if alert_type not in sent_for_sym:
                msg = (
                    f"🟡 {sym} — 浮盈 {pnl_pct:+.1f}% 无保护\n"
                    f"建议设置追踪止盈锁定利润\n"
                    f"→ {EXTERNAL_URL}"
                )
                new_alerts.append(("🟡", sym, alert_type, msg))
                sent_for_sym.add(alert_type)

        sent_today[sym] = list(sent_for_sym)

    sent_state[today] = sent_today
    return new_alerts


def check_emergency(snapshot: dict, sent_state: dict) -> list:
    """Check for system-level emergencies."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    sent_today = sent_state.get(today, {})
    sent_for_sys = set(sent_today.get("__system__", []))
    alerts = []

    # ── Emergency 1: Daily loss > 5% ──
    pnl = snapshot.get("pnl", {})
    today_pnl = pnl.get("today", 0)
    total_assets = snapshot.get("account", {}).get("total_assets", 1)
    if total_assets > 0:
        daily_loss_pct = today_pnl / total_assets * 100
        if daily_loss_pct < -5 and "daily_loss" not in sent_for_sys:
            msg = (
                f"⚫ 紧急 — 日亏损 {daily_loss_pct:.1f}%\n"
                f"今日P&L: ${today_pnl:,.0f}\n"
                f"建议暂停所有交易，复盘后再操作\n"
                f"→ {EXTERNAL_URL}"
            )
            alerts.append(("⚫", "__system__", "daily_loss", msg))
            sent_for_sys.add("daily_loss")

    # ── Emergency 2: Margin critical ──
    margin = snapshot.get("margin", {})
    usage = margin.get("usage_pct", 0)
    if usage > 80 and "margin_critical" not in sent_for_sys:
        msg = (
            f"⚫ 紧急 — 保证金使用率 {usage:.0f}%\n"
            f"接近强平风险，请立即减仓\n"
            f"→ {EXTERNAL_URL}"
        )
        alerts.append(("⚫", "__system__", "margin_critical", msg))
        sent_for_sys.add("margin_critical")

    sent_today["__system__"] = list(sent_for_sys)
    sent_state[today] = sent_today
    return alerts


def send_feishu(text: str) -> bool:
    """Send via Feishu webhook (keyword: [FTSeek-US]).

    限流保护: 逐条发送时间隔 300ms, 避免命中飞书 5/s 频率限制 (code=11232).
    """
    import requests
    import time as _time

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US", "")
    if not webhook_url:
        print("[warn] FEISHU_WEBHOOK_URL_US not set", file=sys.stderr)
        return False

    content = f"[FTSeek-US] {text}" if not text.startswith("[FTSeek-US]") else text
    payload = {"msg_type": "text", "content": {"text": content}}
    try:
        _time.sleep(0.3)  # 防止 burst 触发飞书 11232 限频
        resp = requests.post(webhook_url, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            return True
        # 飞书限频 → 指数退避重试 1 次
        if result.get("code") == 11232:
            _time.sleep(2)
            resp = requests.post(webhook_url, json=payload, timeout=10)
            result = resp.json()
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                return True
        print(f"[feishu] send failed: {result}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[feishu] send error: {e}", file=sys.stderr)
        return False


def main():
    # Skip outside market window
    if not is_market_window():
        return  # silent

    # Load dedup state
    sent_state = load_sent_state()

    # Fetch snapshot
    try:
        snapshot = fetch_snapshot()
    except Exception as e:
        print(f"[monitor] snapshot fetch failed: {e}", file=sys.stderr)
        return

    if "error" in snapshot:
        print(f"[monitor] snapshot error: {snapshot['error']}", file=sys.stderr)
        return

    lifecycle = snapshot.get("lifecycle", [])
    if not lifecycle:
        return  # no positions, nothing to monitor

    # Check alerts
    all_alerts = []
    all_alerts.extend(check_alerts(lifecycle, sent_state))
    all_alerts.extend(check_emergency(snapshot, sent_state))

    if not all_alerts:
        return  # nothing new, silent

    # Save state BEFORE sending (prevent double-send on retry)
    save_sent_state(sent_state)

    # Send each alert
    for level, sym, alert_type, msg in all_alerts:
        ok = send_feishu(msg)
        if ok:
            print(f"[sent] {level} {sym}/{alert_type}")
        else:
            print(f"[fail] {level} {sym}/{alert_type}", file=sys.stderr)


if __name__ == "__main__":
    main()
