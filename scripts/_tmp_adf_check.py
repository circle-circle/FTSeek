"""ADF 单位根检验 (Qoder, 2026-08-09) — 当前项目策略日盈亏序列的平稳性.

对象: trade_records 日聚合净盈亏 (pnl_net) 与毛盈亏 (realized_pnl).
  - 平稳 (拒绝单位根) → PnL 围绕稳定均值波动 → 存在可统计的"均值" (配合均值符号判 edge)
  - 非平稳 (不能拒绝) → 随机游走特征 → 历史绩效无稳定期望可言
方法: Δy_t = c + φ·y_{t-1} + Σβ_i·Δy_{t-i} + ε, 报告 φ 的 t 统计量,
  对照 MacKinnon 近似临界值 (含常数无趋势, 小样本). 纯 numpy OLS 实现, 无新依赖.
样本: ~27 个交易日 — 功效极低, 结论只作方向性参考 (p 值仅供参考, 强调效应量).
"""
import sqlite3
import numpy as np

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def daily_pnl(col):
    c = sqlite3.connect(DB)
    rows = c.execute(f"""
        SELECT trade_date, SUM({col}) FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0 AND trade_date >= '2026-07-01'
        GROUP BY trade_date ORDER BY trade_date
    """).fetchall()
    c.close()
    return [r[0] for r in rows], np.array([float(r[1] or 0) for r in rows])


def adf(y, max_lag=1):
    """ADF (含常数项, 无趋势). 返回 (t_stat, lag, n_obs, 临界值dict)."""
    y = np.asarray(y, dtype=float)
    dy = np.diff(y)
    rows, target = [], []
    for t in range(max_lag, len(dy)):
        row = [1.0, y[t]]  # 常数 + y_{t-1}  (y[t] 是差分序列索引 t 对应的原序列滞后)
        for i in range(1, max_lag + 1):
            row.append(dy[t - i])
        rows.append(row)
        target.append(dy[t])
    X = np.array(rows)
    d = np.array(target)
    n, k = X.shape
    beta, res, *_ = np.linalg.lstsq(X, d, rcond=None)
    resid = d - X @ beta
    dof = max(n - k, 1)
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.inv(X.T @ X)
    t_phi = float(beta[1] / np.sqrt(cov[1, 1]))
    # MacKinnon 近似临界值 (含常数无趋势; n≈25-30 小样本):
    crit = {"1%": -3.75, "5%": -3.00, "10%": -2.63}
    return t_phi, max_lag, n, crit, beta[0], float(resid @ resid)


def mean_t(y):
    m = float(np.mean(y))
    t = m / (float(np.std(y, ddof=1)) / np.sqrt(len(y)))
    return m, t


def report(name, dates, y):
    print(f"\n=== {name} (n={len(y)} 日, {dates[0]}~{dates[-1]}) ===")
    m, tm = mean_t(y)
    print(f"日均 {m:+.2f} (t={tm:+.2f}) | 累计 {float(np.sum(y)):+.2f}")
    for lag in (0, 1, 2):
        if len(y) < lag + 12:
            continue
        t_phi, _, nobs, crit, c, _ = adf(y, lag)
        verdict = ("平稳 (拒绝单位根, 5%)" if t_phi < crit["5%"]
                   else "边缘 (10%)" if t_phi < crit["10%"] else "非平稳 (不能拒绝单位根)")
        print(f"  ADF(lag={lag}): t={t_phi:+.2f} vs 5%临界 {crit['5%']} → {verdict}")


def main():
    d_net, net = daily_pnl("pnl_net")
    d_gross, gross = daily_pnl("realized_pnl")
    report("日净盈亏 (pnl_net)", d_net, net)
    report("日毛盈亏 (realized_pnl)", d_gross, gross)
    # 稳健性: 逐笔净盈亏序列 (n≈200, 时间序)
    c = sqlite3.connect(DB)
    per = np.array([float(r[0]) for r in c.execute("""
        SELECT pnl_net FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0 AND trade_date >= '2026-07-01'
        ORDER BY entry_time
    """)])
    c.close()
    print(f"\n=== 逐笔净盈亏序列 (n={len(per)}) — 平稳性稳健参考 ===")
    m, tm = mean_t(per)
    print(f"笔均 {m:+.2f} (t={tm:+.2f})")
    for lag in (1, 2, 4):
        t_phi, _, _, crit, _, _ = adf(per, lag)
        verdict = ("平稳 (5%)" if t_phi < crit["5%"] else "边缘 (10%)" if t_phi < crit["10%"] else "非平稳")
        print(f"  ADF(lag={lag}): t={t_phi:+.2f} → {verdict}")


if __name__ == "__main__":
    main()
