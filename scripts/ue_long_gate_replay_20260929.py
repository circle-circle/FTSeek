"""RFC-117 生产函数级反事实重放: 用已提交的 _long_breakdown_trend (非简化版)
对全部 UE LONG 成交逐笔, 在其真实入场日 asof 重放, 验证是否真拦得住历史大亏。
取数改为 request_history_kline(end=入场日前一交易日) 的 point-in-time 口径。
+ inject 取数延迟测量 (防撑爆 21:10 cron 的 inject timeout 600s)。
只读。
"""
import os, sys, time, sqlite3, statistics
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("FTSEEK_SHADOW_RUN", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.storage.db_pool import get_db_path
from scripts import llm_universe_scan as ue

from futu import OpenQuoteContext, KLType, AuType, RET_OK


def _fetch_bars_asof(ctx, symbol: str, entry_date: str):
    end_date = (date.fromisoformat(entry_date) - timedelta(days=1)).isoformat()
    start_date = (date.fromisoformat(end_date) - timedelta(days=180)).isoformat()
    ret, data, _ = ctx.request_history_kline(
        symbol, start=start_date, end=end_date, ktype=KLType.K_DAY,
        max_count=400, autype=AuType.QFQ,
    )
    if ret != RET_OK or data is None or len(data) == 0:
        return []
    bars = [{"time": str(row["time_key"])[:10], "close": float(row["close"])}
            for _, row in data.iterrows()]
    bars.sort(key=lambda b: b["time"])
    return bars


class _AsOfRouter:
    def __init__(self, bars):
        self._bars = bars

    def get_daily_bars(self, symbol, count=60):
        return self._bars

# 全部 UE LONG 成交 (不限窗, 用真实入场日)
con = sqlite3.connect(
    f"file:{get_db_path('ftseek_research')}?mode=ro",
    uri=True,
)
con.row_factory = sqlite3.Row
rows = con.execute("""
SELECT symbol, direction, COALESCE(pnl_net,realized_pnl) pnl, realized_pnl gross, fees_est,
       entry_time, exit_reason, mfe_pct, entry_price
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND entry_price>=5
  AND decision_id LIKE 'universe%' AND direction='LONG'
ORDER BY entry_time
""").fetchall()

print(f"═══ UE LONG 成交 {len(rows)} 笔 · 生产函数 _long_breakdown_trend 逐笔重放 ═══")
print(f"{'标的':9s}{'入场日':11s}{'净额':>9s}{'mfe%':>6s}  {'asof前trend':12s}  {'gate判定':8s}  MA5/10/20·close")
print("-" * 110)

blocked, passed, no_data = [], [], []
lat = []
ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                       port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
try:
    for r in rows:
        sym = r["symbol"]
        entry_date = str(r["entry_time"])[:10]      # asof = 入场日 (生产语义: 只看昨收前完成bar)
        t0 = time.time()
        bars = _fetch_bars_asof(ctx, sym, entry_date)
        trend, detail = ue._long_breakdown_trend(_AsOfRouter(bars), sym, entry_date)
        lat.append(time.time() - t0)
        would_block = (trend == "空头排列")          # 生产拦截条件
        pnl = r["pnl"] or 0
        ma = f"{detail['ma5']}/{detail['ma10']}/{detail['ma20']}·{detail['close']}" if detail else "(no_data)"
        verdict = "🔴SKIP" if would_block else "🟢放行"
        if trend == "no_data":
            no_data.append((sym, entry_date, pnl, trend, detail))
        else:
            (blocked if would_block else passed).append((sym, entry_date, pnl, trend, detail))
        flag = " ⬅大亏" if pnl < -50 else ""
        print(f"{sym:9s}{entry_date:11s}{pnl:>9.2f}{(r['mfe_pct'] or 0):>6.2f}  {trend:12s}  {verdict:8s}  {ma}{flag}")

    print("-" * 110)
    bp = [x[2] for x in blocked]
    pp = [x[2] for x in passed]
    print(f"\n【拦截组 n={len(blocked)}】净 ${sum(bp):.2f}  " + (f"笔均 ${statistics.mean(bp):.2f}" if bp else ""))
    print(f"【有数据放行组 n={len(passed)}】净 ${sum(pp):.2f}  " + (f"笔均 ${statistics.mean(pp):.2f}  WR {sum(1 for x in pp if x>0)/len(pp)*100:.0f}%" if pp else ""))
    if no_data:
        nd = [x[2] for x in no_data]
        print(f"【缺数 fail-open n={len(no_data)}】净 ${sum(nd):.2f}  笔均 ${statistics.mean(nd):.2f}  WR {sum(1 for x in nd if x>0)/len(nd)*100:.0f}%")
    print(f"\n★ 拦截组若全为亏损=闸有效; 若含盈利单=误杀。逐笔:")
    for sym, d, pnl, trend, detail in sorted(blocked, key=lambda x: x[2]):
        print(f"   {sym:9s}{d}  ${pnl:8.2f}  ({trend})")
    print(f"\n★ 放行组中的大亏 (< -$50) = 闸漏网 (结构边界):")
    miss = [(s,d,p) for s,d,p,t,det in passed if p < -50]
    for sym, d, pnl in sorted(miss, key=lambda x: x[2]):
        print(f"   {sym:9s}{d}  ${pnl:8.2f}")
    if not miss:
        print("   (无)")

    print(f"\n【历史取数延迟】n={len(lat)} 单次 mean={statistics.mean(lat):.2f}s max={max(lat):.2f}s "
          f"总={sum(lat):.1f}s (inject timeout 600s, LONG候选数≈此量级)")
    print(f"  首调(冷连接)={lat[0]:.2f}s 后续均={statistics.mean(lat[1:]):.2f}s" if len(lat)>1 else "")
finally:
    ctx.close()
