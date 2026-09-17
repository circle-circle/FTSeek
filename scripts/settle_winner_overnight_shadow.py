#!/usr/bin/env python3
"""赢家隔夜影子结算器 (RFC-074, 2026-09-02, record-only).

对 data/shadow/winner_overnight_shadow.jsonl 中未结算的 arm 行, 用次日 1min
replay_bars 结算"带 SL 持一夜"反事实臂:
  - 次日开盘跳空越过 SL → 开盘价出场 (gap-through)
  - 盘中 SL 触碰 → SL 价出场 (悲观序, 不假设更优出场)
  - 未触 → 持到次日收盘 (EOD 落袋, 与既有赢家臂语义一致)
结算 = 反事实出场价相对 arm_price 的增量 × 数量 × 方向符号 (净口径基线:
费用结构同实际平仓 — 多持一夜不增加往返费, delta 为纯价差)。

B16 判据样本口径 (2026-09-02 核查补): arm 点挂 PRE_CLOSE_EXIT 触发处,
非豁免亏损臂 (LIVE 水下/到期 SIM 仓) 也会落行 (近30日实测 11赢/1输≈8%污染) —
DEB 问题定义是 "cohort A 盈利仓持夜", 汇总的 n/均值/胜率/by_outcome 只按
盈利臂 (arm_profit_pct>0, 毛口径, 同 PRE_CLOSE skip 判定) 取样; 亏损臂照
结算仅留对照 (loser_n), 不入判据。min_n=20 = 盈利臂结算行数。

SL 口径注记: stop_loss_at_arm = 初始 SL (meta.stop_loss, 退出链不随
breakeven/trail 收紧更新) — 真实赢家仓 arm 时刻生效 SL 更紧, 反事实"带SL
持夜"的 SL_touch 读数因此偏少 (初始 SL 口径), 判据侧知悉。

数据源: data/replay_bars/{SYM}_{date}.json (position_path_collector 盘后落).
次日交易日 = holiday_calendar.nth_trading_day_after; bar 缺失 → 保留
unsettled 待补算 (静默死亡族纪律: 每次运行打印未结算积压数)。

用法: venv/bin/python scripts/settle_winner_overnight_shadow.py [--days N]

积压告警 (2026-09-16, B16 测量链路修复): 未结算 arm 超
--pending-alert-days (默认 3 天) → app/push hub HIGH 告警 + exit 1。
缘起: 9/4 META arm 因 US_META_2026-09-08.json 1min bar 缺失卡死 12 天,
cron 每天只打印"未结算积压 1"而无主动通知 = 静默死亡族 (B16 判据
n≥20 在积压清空前不可信)。回滚: 告警分支独立于结算, 无需回滚键。
"""
import argparse
import logging
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BAR_DIR = ROOT / "data" / "replay_bars"
SHADOW = ROOT / "data" / "shadow" / "winner_overnight_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "winner_overnight_shadow_latest.json"

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, text: str):
    """temp+rename 原子写 — 06:25 cron 与 15:52 arm 追加写无并发窗口, 防半写."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def next_trading_day(d: str):
    from app.utils.holiday_calendar import nth_trading_day_after
    from datetime import date
    y, m, dd = d.split("-")
    nd = nth_trading_day_after("US", date(int(y), int(m), int(dd)), 1)
    return nd.isoformat() if nd else None


def load_1m(symbol: str, day: str):
    """1min bars: list of [ts, o, h, l, c]; None 表示缺失."""
    p = BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        bars = data.get("bars", []) if isinstance(data, dict) else data
    except Exception:
        return None
    return bars if len(bars) >= 30 else None


def settle_row(row):
    """返回 (ok, fields). ok=False 表示 bar 缺失."""
    sym = row["symbol"]
    nxt = next_trading_day(row["arm_date"])
    if not nxt:
        return False, {}
    bars = load_1m(sym, nxt)
    if not bars:
        return False, {}
    entry = float(row["entry_price"])
    sl = float(row.get("stop_loss_at_arm") or 0)
    q = float(row.get("quantity") or 0)
    sign = 1.0 if row["direction"] == "LONG" else -1.0
    ap = float(row["arm_price"])
    fact_price = None
    tag = ""
    if sl > 0:
        o0 = bars[0][1]
        if (row["direction"] == "LONG" and o0 <= sl) or \
                (row["direction"] == "SHORT" and o0 >= sl):
            fact_price, tag = o0, "SL_gap"
        else:
            for b in bars:
                if row["direction"] == "LONG" and b[3] <= sl:
                    fact_price, tag = sl, "SL_touch"
                    break
                if row["direction"] == "SHORT" and b[2] >= sl:
                    fact_price, tag = sl, "SL_touch"
                    break
    if fact_price is None:
        fact_price, tag = bars[-1][4], "EOD_next_day"
    delta = (fact_price - ap) * q * sign
    return True, {
        "settle_date": nxt,
        "settle_outcome": tag,
        "settle_price": round(fact_price, 4),
        "overnight_delta": round(delta, 2),
        "settled": True,
    }


def _pending_arms(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("settled") and r.get("type") == "arm"]


def _stale_pending(rows: list[dict], alert_days: int, today=None) -> list[dict]:
    """未结算 arm 且 arm_date 距今超 alert_days 天 (arm_date 损坏视为超龄)."""
    from datetime import date
    if today is None:
        today = date.today()
    stale = []
    for r in _pending_arms(rows):
        try:
            age = (today - date.fromisoformat(str(r.get("arm_date", "")))).days
        except (TypeError, ValueError):
            stale.append(r)
            continue
        if age > int(alert_days):
            stale.append(r)
    return stale


def _push_stale_alert(stale: list[dict], pending_n: int, alert_days: int) -> bool:
    """积压超龄告警 — app/push hub, sync=True (cron 短命进程, 2026-09-16).

    daily_invariant_check W1 直推同款: 推送成败以 hub.push 返回为准;
    stop() 只关线程池, 关闭异常不影响返回值。
    """
    lines = []
    for r in stale:
        sym = str(r.get("symbol", "?"))
        arm_day = str(r.get("arm_date", "?"))
        try:
            nxt = next_trading_day(arm_day)
        except Exception:
            nxt = None
        bar_hint = (f"data/replay_bars/{sym.replace('.', '_')}_{nxt}.json"
                    if nxt else "arm_date 损坏, 无法定位次日 bar")
        lines.append(f"· {sym} arm_date={arm_day} — 需 {bar_hint}")
    hub = None
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        hub = get_push_hub()
        ok = hub.push(PushMessage(
            title=f"⚠️ 赢家隔夜影子积压 {len(stale)} 条 (>{alert_days} 天未结算)",
            body="\n".join(lines) + (
                f"\n未结算总数 {pending_n}; 结算器每日 06:25 运行, bar 缺失会无限重试。"
                "修复: 补拉对应次日 1min bar (scripts/exit_replay_backtest.fetch_bars) "
                "或落档 DEB 注销损坏行。B16 判据 (n≥20) 在积压清空前不可信。"),
            level=Level.HIGH,
            category=Category.HEALTH,
            dedup_key="winner_overnight_pending_stale",
        ), sync=True)
        return bool(ok)
    except Exception as e:
        logger.error("winner_overnight 积压告警推送异常: %s", e, exc_info=True)
        return False
    finally:
        try:
            if hub is not None:
                hub.stop()
        except Exception as e:
            logger.warning("积压告警后 push hub 关闭异常 (推送结果不受影响): %s", e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只结算近 N 天 arm 行 (0=全部)")
    ap.add_argument("--pending-alert-days", type=int, default=3,
                    help="未结算 arm 超过 N 天未结算 → HIGH 告警 + exit 1 (积压棘轮)")
    args = ap.parse_args(argv)
    if not SHADOW.exists():
        print("无影子文件, 退出 (尚未产生 arm 行)")
        return 0
    rows = [json.loads(l) for l in SHADOW.open() if l.strip()]
    pending = [r for r in rows if not r.get("settled") and r.get("type") == "arm"]
    if args.days > 0:
        from datetime import date, timedelta
        cut = (date.today() - timedelta(days=args.days)).isoformat()
        pending = [r for r in pending if r.get("arm_date", "") >= cut]
    n_set = 0
    for r in rows:
        if r.get("settled") or r.get("type") != "arm":
            continue
        if args.days > 0 and r.get("arm_date", "") < cut:
            continue
        ok, fields = settle_row(r)
        if ok:
            r.update(fields)
            n_set += 1
    _atomic_write(SHADOW, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    settled = [r for r in rows if r.get("settled")]
    # B16 判据样本 = 盈利臂 (docstring 口径注记); 亏损臂仅对照
    winners = [r for r in settled if float(r.get("arm_profit_pct", 0) or 0) > 0]
    losers = [r for r in settled if float(r.get("arm_profit_pct", 0) or 0) <= 0]
    tot = sum(r.get("overnight_delta", 0) for r in winners)
    wins = sum(1 for r in winners if r.get("overnight_delta", 0) > 0)
    summary = {
        "settled_n": len(settled),
        "winner_n": len(winners),
        "loser_n": len(losers),
        "pending_n": len([r for r in rows if not r.get("settled")]),
        "sum_overnight_delta": round(tot, 2),
        "win_rate": round(wins / len(winners), 3) if winners else None,
        "by_outcome": {},
    }
    from collections import Counter
    summary["by_outcome"] = dict(Counter(r.get("settle_outcome") for r in winners))
    _atomic_write(OUT, json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"本次结算 {n_set} 笔 | 盈利臂 {len(winners)} (B16样本) / 亏损臂 {len(losers)} (对照) "
          f"| 未结算积压 {summary['pending_n']} | 盈利臂合计delta {tot:+.2f} | 胜率 {summary['win_rate']}")
    print(f"by_outcome(盈利臂): {summary['by_outcome']}")
    stale = _stale_pending(rows, args.pending_alert_days)
    if stale:
        push_ok = _push_stale_alert(stale, summary["pending_n"], args.pending_alert_days)
        print(f"[winner_overnight] ⚠️ 积压超龄 {len(stale)} 条 "
              f"(>{args.pending_alert_days} 天, 推送{'成功' if push_ok else '失败'}) — "
              f"详见上方逐条 bar 缺口")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
