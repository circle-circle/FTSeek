"""ACF/PACF 诊断 (Qoder, 2026-08-09) — 策略盈亏的序列相关结构.

回答: 今天的盈亏和昨天/前天… 像不像?
  ACF(k)  = y_t 与 y_{t-k} 的相关 (含中间环节传导的总效应)
  PACF(k) = 剔除 y_{t-1}..y_{t-k+1} 影响后, y_t 与 y_{t-k} 的直接相关
判读: 显著正 lag1 = 连赢连亏扎堆 (regime 依赖); 显著负 = 隔日交替;
  全部不显著 = 白噪声 (每日独立, 亏损无记忆). 显著带 ±1.96/sqrt(n).
实现: 纯 numpy。PACF 用逐阶 OLS (y_t ~ y_{t-1..t-k}, 取末位系数)。
"""
import sqlite3
import numpy as np

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def acf(y, k):
    y = np.asarray(y, float)
    y = y - y.mean()
    return float(np.sum(y[k:] * y[:-k]) / np.sum(y * y))


def pacf(y, k):
    y = np.asarray(y, float)
    if k == 1:
        return acf(y, 1)
    rows, tgt = [], []
    for t in range(k, len(y)):
        rows.append([1.0] + [y[t - i] for i in range(1, k + 1)])
        tgt.append(y[t])
    X = np.array(rows)
    beta, *_ = np.linalg.lstsq(X, np.array(tgt), rcond=None)
    return float(beta[-1])


def report(name, y, max_k):
    y = np.asarray(y, float)
    band = 1.96 / np.sqrt(len(y))
    print(f"\n=== {name} (n={len(y)}) 显著带 ±{band:.2f} ===")
    print(f"{'lag':>4} {'ACF':>7} {'PACF':>7}  判读")
    for k in range(1, max_k + 1):
        a, p = acf(y, k), pacf(y, k)
        flag = ""
        if abs(a) > band:
            flag += "ACF显著" + ("(正/扎堆)" if a > 0 else "(负/交替)")
        if abs(p) > band:
            flag += (" " if flag else "") + "PACF显著" + ("(直接动量)" if p > 0 else "(直接反转)")
        print(f"{k:>4} {a:+7.3f} {p:+7.3f}  {flag}")


def main():
    c = sqlite3.connect(DB)
    daily = [float(r[0]) for r in c.execute("""
        SELECT SUM(pnl_net) FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0 AND trade_date >= '2026-07-01'
        GROUP BY trade_date ORDER BY trade_date""")]
    per_trade = [float(r[0]) for r in c.execute("""
        SELECT pnl_net FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0 AND trade_date >= '2026-07-01'
        ORDER BY entry_time""")]
    gross_daily = [float(r[0]) for r in c.execute("""
        SELECT SUM(realized_pnl) FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0 AND trade_date >= '2026-07-01'
        GROUP BY trade_date ORDER BY trade_date""")]
    c.close()
    report("日净盈亏", daily, 6)
    report("日毛盈亏", gross_daily, 6)
    report("逐笔净盈亏", per_trade, 10)


if __name__ == "__main__":
    main()
