#!/usr/bin/env python3
"""SPY 暴跌观察器 — 预注册观察项 (DEB-20260826 §二, 用户 8/26 批准)

盘中市场压力门已按回放否决 (|t|=1.26<2, 效应132%系于两天, 历史无≤-2%日)。
替代动作: 当未来出现 SPY 日跌 ≤ -2.0% 的真暴跌日, 采集当日入场笔结局攒样,
n≥30 后按宪章第9律(日聚类)+双窗口一致裁决是否上门。

行为 (no_agent watchdog 模式):
  - 非暴跌日: 空 stdout = 静默
  - 暴跌日: 落盘台账 data/spy_crash_watch.jsonl (幂等) + 打印告警

测试钩子: SPY_CRASH_THRESH / SPY_CRASH_LEDGER 环境变量覆盖 (生产勿用)。
"""
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
SPY = ROOT / "data" / "replay_bars_daily" / "US_SPY.json"
LEDGER = Path(os.environ.get("SPY_CRASH_LEDGER", ROOT / "data" / "spy_crash_watch.jsonl"))
THRESH = float(os.environ.get("SPY_CRASH_THRESH", "-0.02"))


def main():
    if not SPY.exists():
        print(f"[spy_crash_watch] 缺数据源 {SPY}, 本轮跳过")
        return
    bars = json.loads(SPY.read_text())
    if len(bars) < 2:
        return
    day = bars[-1][0]
    prev_close = bars[-2][4]
    ret = bars[-1][4] / prev_close - 1
    if ret > THRESH:
        return  # 静默

    # 幂等: 同一暴跌日只记一次
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                if json.loads(line).get("date") == day:
                    return
            except json.JSONDecodeError:
                continue

    c = sqlite3.connect(ROOT / "data" / "ftseek_us.db")
    settled = c.execute(
        """SELECT symbol, direction, COALESCE(pnl_net, realized_pnl) AS net, exit_reason
           FROM trade_records
           WHERE trade_date = ? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
             AND COALESCE(pnl_net, realized_pnl) IS NOT NULL""",
        (day,),
    ).fetchall()
    open_n = c.execute(
        """SELECT COUNT(*) FROM trade_records
           WHERE trade_date = ? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
             AND (exit_price IS NULL OR exit_price = 0 OR exit_time IS NULL OR exit_time = '')""",
        (day,),
    ).fetchone()[0]
    c.close()

    nets = [r[2] for r in settled]
    rec = {
        "date": day,
        "spy_ret": round(ret, 4),
        "n_settled": len(settled),
        "net": round(sum(nets), 2),
        "wr": round(sum(1 for x in nets if x > 0) / len(nets), 3) if nets else None,
        "n_open": open_n,
        "trades": [
            {"symbol": r[0], "direction": r[1], "net": round(r[2], 2), "exit": r[3]}
            for r in settled
        ],
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    cum_net = sum(r["net"] for r in rows)
    cum_trades = sum(r["n_settled"] for r in rows)
    print(
        f"🚨 SPY 暴跌日 {day}: 全天 {ret*100:.2f}% (预注册观察项触发)\n"
        f"当日入场已结算 n={len(settled)} 净=${sum(nets):.0f}"
        + (f" WR={rec['wr']:.0%}" if rec["wr"] is not None else "")
        + f" 未平 {open_n} 仓\n"
        f"累计 {len(rows)} 个暴跌日 / {cum_trades} 笔 / 净=${cum_net:.0f}"
        f" — 判据: n≥30 笔按宪章第9律(日聚类)+双窗口一致裁决压力门 (DEB-20260826)"
    )


if __name__ == "__main__":
    main()
