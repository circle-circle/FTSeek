"""ADF 单位根检验 (Qoder, 2026-08-09) — 当前项目策略日盈亏序列的平稳性.

对象: trade_records 日聚合净盈亏 (pnl_net) 与毛盈亏 (realized_pnl).
  - 平稳 (拒绝单位根) → PnL 围绕稳定均值波动 → 存在可统计的"均值" (配合均值符号判 edge)
  - 非平稳 (不能拒绝) → 随机游走特征 → 历史绩效无稳定期望可言
方法: Δy_t = c + φ·y_{t-1} + Σβ_i·Δy_{t-i} + ε, 报告 φ 的 t 统计量,
  对照 MacKinnon 近似临界值 (含常数无趋势, 小样本). 纯 numpy OLS 实现, 无新依赖.
样本: ~27 个交易日 — 功效极低, 结论只作方向性参考 (p 值仅供参考, 强调效应量).

2026-08-28 三处纠偏 (本脚本自今日起是**自投递**周检器, 无人代读, 数错即直接误导裁决):
  ① 日聚合改按**出场日** (substr(exit_time,1,10)) 归集。原按 trade_date (入场日) 分桶 —
     隔夜仓跳空亏损记进了开仓那天, 与日报/熔断/IC 周报 (本周已全仓统一为出场日) 两套口径,
     即"同名日净盈亏两个权威"; 被测平稳性的序列不再是其它防线发布的那一条。
  ② 净额改 COALESCE(pnl_net, realized_pnl)。原 `float(r[1] or 0)` 把 pnl_net 为 NULL 的
     旧行 (pnl_net 列晚于交易行落地) 静默当 0 计入当日桶 → 均值被稀释、t 值被压低。
  ③ 逐笔序列改按 exit_time 排序。原按 entry_time — 被检对象是"已实现盈亏的时间序列",
     实现时点是出场; 持仓期长短不一时出场次序 ≠ 入场次序, 按入场排会错置序列自相关结构。

2026-08-28 调度迁移: 前身是 agent-session 定时任务 (每周六 09:05, permanent), 该机制绑定
创建会话进程、进程死则任务照列"已排定"而永不触发 (创建至今零实跑)。现挂 Hermes no_agent
包装 ops/hermes/adf_weekly_check.sh — stdout 即投递内容, 故"转正四判据"结论必须由本脚本
自己算 (原由 agent 读判据文字代判, 无人代读后只剩裸统计量 = 又一处"看着有输出其实没结论")。
"""
import sqlite3
import numpy as np

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
# OOS (实盘化门槛监测) 样本起点 = DEB-20260808 落地首日
OOS_START = "2026-08-10"


# 序列表达式白名单 (键名进 SQL, 取值不外露 → 无注入面):
#   net = 净额主口径 (pnl_net 缺失的旧行回退毛额, 与日报/熔断同尺);
#   gross = 毛额 (realized_pnl 本身即毛值, 不做回退)。
_SERIES = {
    "net": "COALESCE(pnl_net, realized_pnl)",
    "gross": "realized_pnl",
}


def daily_pnl(name="net", start="2026-07-01"):
    """按出场日聚合的日序列. 2026-08-28: 原按 trade_date(入场日) 分桶 → 隔夜跳空盈亏记错日,
    且与其余日盈亏权威口径分叉, 现统一 substr(exit_time,1,10)。"""
    expr = _SERIES[name]
    c = sqlite3.connect(DB)
    rows = c.execute(f"""
        SELECT substr(exit_time,1,10) AS d, SUM({expr}) FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0
          AND substr(exit_time,1,10) >= ?
        GROUP BY d ORDER BY d
    """, (start,)).fetchall()
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
    # MacKinnon (1996) 响应面: C(p) = β_inf + β_1/n + β_2/n²  (含常数无趋势, n=回归样本量)
    # 2026-08-09 修正: 原写死大样本近似值, 小样本下应更负/更宽; 按 n 动态计算.
    mack = {
        0.01: (-3.43035, -6.5393, -16.786),
        0.05: (-2.86121, -2.8902, -4.0886),
        0.10: (-2.56677, -1.5384, -2.8094),
    }
    crit = {f"{int(p*100)}%": b_inf + b1 / n + b2 / (n * n)
            for p, (b_inf, b1, b2) in mack.items()}
    return t_phi, max_lag, n, crit, beta[0], float(resid @ resid)


def mean_t(y):
    m = float(np.mean(y))
    t = m / (float(np.std(y, ddof=1)) / np.sqrt(len(y)))
    return m, t


def report(name, dates, y):
    if len(y) < 2:
        print(f"\n=== {name} (n={len(y)} 日) ===")
        print("  样本 <2 日, 无统计量可报 (窗口内无已了结交易?)")
        return
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


def per_trade_net(start="2026-07-01"):
    """逐笔已实现净盈亏序列 (按实现时点 exit_time 排序). 原按 entry_time → 出场次序≠入场次序时
    会错置自相关结构 (2026-08-28 纠偏③)."""
    c = sqlite3.connect(DB)
    rows = c.execute("""
        SELECT COALESCE(pnl_net, realized_pnl) FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0
          AND substr(exit_time,1,10) >= ?
        ORDER BY exit_time
    """, (start,)).fetchall()
    c.close()
    return np.array([float(r[0] or 0) for r in rows])


def graduation_verdict(dates, net, d_gross, gross):
    """四判据自算 (无 agent 代读): OOS≥15日 + 日净均值>0 + 毛均值t>1 + ADF平稳.
    全满足 → 可提交"是否实盘化"讨论; 任一不满足 → 明确"未达标" (判据源 DEB-20260808)."""
    def _oos(ds, ys):
        i = sum(1 for d in ds if d and d < OOS_START)
        return ds[i:], ys[i:]

    o_dates, o_net = _oos(dates, net)
    _, o_gross = _oos(d_gross, gross)
    n = len(o_net)
    print(f"\n=== 实盘化门槛四判据 (OOS 窗口 {OOS_START} 起, n={n} 个已了结交易日) ===")
    if n == 0:
        print("  OOS 窗口内无已了结交易 → 无法判定")
        return
    if n < 10:
        print("  ⚠️ OOS 样本 <10 日: 只报数, 不下结论 (功效不足)")

    mn, tmn = mean_t(o_net)
    c1 = n >= 15
    c2 = mn > 0
    mg, tmg = mean_t(o_gross) if len(o_gross) > 1 else (float("nan"), float("nan"))
    c3 = bool(tmg == tmg and tmg > 1)        # nan-safe: NaN 比较恒 False
    if n >= 12:
        t_phi, _, _, crit, _, _ = adf(o_net, 1)
        c4 = t_phi < crit["5%"]
        adf_txt = f"t={t_phi:+.2f} vs 5%临界 {crit['5%']:.2f}"
    else:
        c4 = False
        adf_txt = f"样本 {n}<12 不可检 (判据按不满足计)"

    print(f"  [{'✅' if c1 else '❌'}] OOS ≥15 日          : {n}")
    print(f"  [{'✅' if c2 else '❌'}] 日净均值 > 0        : {mn:+.2f} (t={tmn:+.2f})")
    print(f"  [{'✅' if c3 else '❌'}] 毛均值 t > 1 (供给端): {mg:+.2f} (t={tmg:+.2f})")
    print(f"  [{'✅' if c4 else '❌'}] ADF 平稳 (5%)       : {adf_txt}")
    print("  → " + ("四项全满足, 可提交实盘化讨论 (仍需人工裁决)" if (c1 and c2 and c3 and c4)
                  else "未达标 — 不建议讨论实盘化"))


def main():
    d_net, net = daily_pnl("net")
    d_gross, gross = daily_pnl("gross")
    report("日净盈亏 (净额主口径, 出场日归集)", d_net, net)
    report("日毛盈亏 (realized_pnl, 出场日归集)", d_gross, gross)
    # 稳健性: 逐笔净盈亏序列 (n≈200, 时间序)
    per = per_trade_net()
    print(f"\n=== 逐笔净盈亏序列 (n={len(per)}) — 平稳性稳健参考 ===")
    if len(per) < 2:
        print("  样本 <2 笔, 无统计量可报")
        graduation_verdict(d_net, net, d_gross, gross)
        return
    m, tm = mean_t(per)
    print(f"笔均 {m:+.2f} (t={tm:+.2f})")
    # 与 report() 同规矩: ADF 需 n ≥ lag+12, 小样本下 X'X 奇异 (LinAlgError),
    # 而本脚本是**自投递**周检 — 一处未护的异常会让整条消息消失 (等于又一种静默失败)。
    _tested = 0
    for lag in (1, 2, 4):
        if len(per) < lag + 12:
            continue
        _tested += 1
        t_phi, _, _, crit, _, _ = adf(per, lag)
        verdict = ("平稳 (5%)" if t_phi < crit["5%"] else "边缘 (10%)" if t_phi < crit["10%"] else "非平稳")
        print(f"  ADF(lag={lag}): t={t_phi:+.2f} → {verdict}")
    if not _tested:
        print(f"  样本 n={len(per)} < 13, ADF 各阶均不可检 (功效不足, 只报上面的均值/t)")
    graduation_verdict(d_net, net, d_gross, gross)


if __name__ == "__main__":
    main()
