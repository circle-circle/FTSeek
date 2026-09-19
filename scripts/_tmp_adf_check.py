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

2026-09-19 判据加严 (RFC-089; 用户裁"按建议执行"):
  ① ADF 的**检测边界**写进报告。判别力实测 (复用本脚本 adf(), 500 次替代世界):
     把真实序列整体下移 $1000/天 (注定巨亏) → t 一位不变 (-5.4599); 反号 / 打乱日期顺序
     → 仍 100% 判"平稳"; 每天恒定亏 $50 的序列 → 88% 判"平稳"; 而"亏损逐日加深"→ 0%。
     即 ADF 只抓水平漂移, 对均值大小/正负完全无感 ⇒ 通过 ≠ 有 edge, 降级为体检项。
  ② c2 由"号为正即可"加严为"号为正 ∧ t ≥ NET_T_MIN(=2)": 旧口径下 OOS t=+0.01
     (95% 置信区间 ±$121.55, 与 0 不可分辨) 也判达标 ⇒ 四项里唯一该咬人的门形同虚设。
  ③ 新增 c5 成本覆盖 (净 bps 扣 30bps 活体滑点 > 0), 并出 净留存/费用线 + MDE 两行,
     让"不可分辨"与"达标"在报告里可区分 (旧版只有 ✅/❌ 二元)。
  ④ 碎价股费用告警 (record-only, 零行为变更): 费率按**股数**计 (富途 $0.003/股 交收费,
     官网无上限), 低价/高股数票 往返费用/名义额可超 1% (实测 US.CTNT 9.15%, 正常票 0.07%)。
门槛数字不新造 — 与 RFC-077 毕业门禁草案同档 (见下方常量注释), 避免第三套门槛并行。
"""
import sqlite3
import numpy as np

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
# OOS (实盘化门槛监测) 样本起点 = DEB-20260808 落地首日
OOS_START = "2026-08-10"

# ── 判据门槛 (2026-09-19 RFC-089) ──────────────────────────────────────────
# 数字不新造: 与 RFC-077 毕业门禁草案 (docs/decisions/RFC-077-*) 同档, 避免"第三套门槛并行"。
NET_T_MIN = 2.0            # c2: 净日均显著性 — 同 RFC-077 G1 "净 bps 按日等权 t > 2"
SLIP_HAIRCUT_BPS = 30.0    # c5: 活体来回滑点扣减 — 同 RFC-077 G2 (活体保守值, 埋点实测仅 ~0.5bps)
FEE_ALERT_PCT = 1.0        # 往返费用/名义额 > 1% 告警线 (record-only)
# 房规: n_days 小 (df<5) 时用 df 校正临界值, 不看 2.0 (2.0 在小 df 下反保守)
_T_CRIT_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776}


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
    # 回归在退化序列上可奇异 (实测: 完美交替序列 [+a,-b,+a,-b,…] → X'X 奇异)。
    # 自投递脚本一处未护的异常 = 整条消息消失, 故此处返回 NaN 由调用方按"不可检 ⇒ 不满足"处理
    # (fail-close: 判据不该因为算不出来而变成通过)。
    try:
        beta, res, *_ = np.linalg.lstsq(X, d, rcond=None)
        resid = d - X @ beta
        dof = max(n - k, 1)
        sigma2 = float(resid @ resid) / dof
        cov = sigma2 * np.linalg.inv(X.T @ X)
        var_phi = float(cov[1, 1])
        # var_phi ≤ 0 = 数值退化 (近完美共线): 取 NaN 而非触发 invalid-sqrt 警告后静默成 nan
        t_phi = float(beta[1] / np.sqrt(var_phi)) if var_phi > 0 else float("nan")
        c0, rss = float(beta[0]), float(resid @ resid)
    except (np.linalg.LinAlgError, ZeroDivisionError, ValueError):
        t_phi, c0, rss = float("nan"), float("nan"), float("nan")
    # MacKinnon (1996) 响应面: C(p) = β_inf + β_1/n + β_2/n²  (含常数无趋势, n=回归样本量)
    # 2026-08-09 修正: 原写死大样本近似值, 小样本下应更负/更宽; 按 n 动态计算.
    mack = {
        0.01: (-3.43035, -6.5393, -16.786),
        0.05: (-2.86121, -2.8902, -4.0886),
        0.10: (-2.56677, -1.5384, -2.8094),
    }
    crit = {f"{int(p*100)}%": b_inf + b1 / n + b2 / (n * n)
            for p, (b_inf, b1, b2) in mack.items()}
    return t_phi, max_lag, n, crit, c0, rss


def mean_t(y):
    """日均与其 t 值. 零方差 (常数序列) 时返回 ±inf, 不返回 NaN (NaN 会打印成 +nan 混进报告)."""
    m = float(np.mean(y))
    s = float(np.std(y, ddof=1)) if len(y) > 1 else 0.0
    if s == 0:
        return m, (float("inf") if m > 0 else float("-inf") if m < 0 else 0.0)
    return m, m / (s / np.sqrt(len(y)))


def report(name, dates, y):
    if len(y) < 2:
        print(f"\n=== {name} (n={len(y)} 日) ===")
        print("  样本 <2 日, 无统计量可报 (窗口内无已了结交易?)")
        return
    print(f"\n=== {name} (n={len(y)} 日, {dates[0]}~{dates[-1]}) ===")
    m, tm = mean_t(y)
    print(f"日均 {m:+.2f} (t={tm:+.2f}) | 累计 {float(np.sum(y)):+.2f}")
    lines, summ = adf_lag_block(y, (0, 1, 2))
    for ln in lines:
        print(ln)
    if summ:
        print(summ)


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


def per_trade_rows(start="2026-07-01"):
    """逐笔 (出场日, 名义额, 净额, 费用, 标的) — 供 bps 口径 / 费用告警使用。

    名义额非正 (quantity 或 entry_price 缺失) 的笔整笔剔除: 分母缺失不得用 0 顶 (会稀释 bps)。
    表结构不符 (如测试夹具的极简 trade_records) 时降级返回 [] — 展示层 fail-open,
    自投递脚本一处未护的异常 = 整条消息消失。
    """
    try:
        c = sqlite3.connect(DB)
        rows = c.execute("""
            SELECT substr(exit_time,1,10) d, quantity * entry_price,
                   COALESCE(pnl_net, realized_pnl),
                   realized_pnl - COALESCE(pnl_net, realized_pnl), symbol
            FROM trade_records
            WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0
              AND substr(exit_time,1,10) >= ? AND quantity > 0 AND entry_price > 0
            ORDER BY exit_time
        """, (start,)).fetchall()
        c.close()
    except sqlite3.Error:
        return []
    return [(r[0], float(r[1]), float(r[2] or 0), float(r[3] or 0), r[4]) for r in rows]


def day_bps(rows):
    """按日等权净 bps: 每日 净额/名义额×1e4, 再对日取均值 (与 RFC-077 G1 '按日等权' 同式)."""
    agg = {}
    for d, notional, net, _fee, _sym in rows:
        a = agg.setdefault(d, [0.0, 0.0])
        a[0] += net
        a[1] += notional
    days = [d for d in sorted(agg) if agg[d][1] > 0]
    return days, np.array([agg[d][0] / agg[d][1] * 1e4 for d in days])


def mde(y):
    """80% 功效 / 双侧 5% 下能分辨的最小均值效应 (z_.975 + z_.80 = 2.8016)·sd/√n。

    用途: 区分"达标"与"与 0 不可分辨" — 效应量小于 MDE 时, 判据未过不是"运气差", 是功效不足。
    """
    y = np.asarray(y, dtype=float)
    if len(y) < 2:
        return float("nan")
    return 2.8016 * float(np.std(y, ddof=1)) / np.sqrt(len(y))


def _rank(verdict):
    """0=平稳 / 1=边缘 / 2=非平稳或不可检 → 保守侧 (不可检不得被当成通过)."""
    if verdict.startswith("平稳"):
        return 0
    if verdict.startswith("边缘"):
        return 1
    return 2


def adf_lag_block(y, lags=(0, 1, 2), indent="  "):
    """多阶 ADF → (逐行文本, 保守侧总结行)。

    2026-09-19 新增总结行: 旧版只打三行, 读者易只带走最先出现的那行 (lag=1 常是最有利的一档);
    lag 依赖真实存在 (OOS 窗实测 lag0/1 平稳 / lag2 边缘 / lag3+ 非平稳)。
    """
    lines, outs = [], []
    for lag in lags:
        if len(y) < lag + 12:
            continue
        t_phi, _, _nobs, crit, _, _ = adf(y, lag)
        if not np.isfinite(t_phi):
            v = "不可检 (X'X 奇异)"
        else:
            v = ("平稳 (拒绝单位根, 5%)" if t_phi < crit["5%"]
                 else "边缘 (10%)" if t_phi < crit["10%"] else "非平稳 (不能拒绝单位根)")
        outs.append((lag, v, t_phi))
        lines.append(f"{indent}ADF(lag={lag}): t={t_phi:+.2f} vs 5%临界 {crit['5%']:.4f} → {v}")
    if not outs:
        return lines, None
    worst = max(outs, key=lambda o: _rank(o[1]))
    ranks = sorted({_rank(v) for _l, v, _t in outs})
    if ranks == [0]:
        summ = (f"{indent}→ 各阶一致: 平稳。注: ADF 仅证伪「水平系统性漂移/随机游走」, "
                f"对均值大小与正负完全无感 (平移/反号/打乱均不变) — 通过 ≠ 有 edge。")
    elif ranks == [2]:
        summ = (f"{indent}→ 各阶一致: 非平稳 (不能拒绝单位根) → 该序列无稳定均值可言, "
                f"该判据按不满足计。")
    else:
        summ = (f"{indent}→ ⚠️ lag 依赖: lag={worst[0]} 判「{worst[1]}」为最保守一档 → "
                f"平稳性以该档为准, 勿只引最先出现的 lag。")
    return lines, summ


def graduation_verdict(dates, net, d_gross, gross, rows=None):
    """五判据自算 (无 agent 代读; 2026-09-19 RFC-089 由四判据加严):
      ① OOS ≥15 日 ② 日净均值 > 0 ∧ t ≥ NET_T_MIN ③ 毛均值 t > 1 (供给端)
      ④ ADF 平稳 (体检项: 只证伪水平漂移, 实测对均值大小/正负无感) ⑤ 成本覆盖 (净 bps 扣活体滑点 > 0)
    全满足 → 达到**形式门槛** (≠ 可实盘; 实盘化以 RFC-077 六道门禁为准, 仍需人工裁决)。
    判据源: DEB-20260808 四判据 + RFC-089 加严 (门槛数字同 RFC-077 G1/G2, 不新造)。"""
    def _oos(ds, ys):
        i = sum(1 for d in ds if d and d < OOS_START)
        return ds[i:], ys[i:]

    o_dates, o_net = _oos(dates, net)
    _, o_gross = _oos(d_gross, gross)
    n = len(o_net)
    print(f"\n=== 实盘化门槛判据 (OOS 窗口 {OOS_START} 起, n={n} 个已了结交易日) ===")
    print("  口径注: ADF 通过 ≠ 有 edge (它只证伪水平漂移); 实盘化讨论以 RFC-077 六道门禁为准,")
    print("          本表是周检早期预警 — 「全满足」只代表形式门槛, 「未达标」要看未达标项。")
    if n == 0:
        print("  OOS 窗口内无已了结交易 → 无法判定")
        return
    if n < 10:
        print("  ⚠️ OOS 样本 <10 日: 只报数, 不下结论 (功效不足)")

    mn, tmn = mean_t(o_net)
    c1 = n >= 15
    t_hurdle = _T_CRIT_95.get(n - 1, NET_T_MIN) if n < 6 else NET_T_MIN
    c2 = bool(mn > 0 and tmn >= t_hurdle)
    mg, tmg = mean_t(o_gross) if len(o_gross) > 1 else (float("nan"), float("nan"))
    c3 = bool(tmg == tmg and tmg > 1)        # nan-safe: NaN 比较恒 False
    if n >= 12:
        t_phi, _, _, crit, _, _ = adf(o_net, 1)
        c4 = bool(np.isfinite(t_phi) and t_phi < crit["5%"])     # NaN (奇异) → fail-close
        _other = []
        for _lag in (0, 2, 3):
            if n >= _lag + 12:
                _t, _, _, _c, _, _ = adf(o_net, _lag)
                _other.append(f"lag{_lag} " + ("不可检" if not np.isfinite(_t)
                                               else "平稳" if _t < _c["5%"]
                                               else "边缘" if _t < _c["10%"] else "非平稳"))
        adf_txt = ((f"不可检 (X'X 奇异, 判据按不满足计)" if not np.isfinite(t_phi)
                    else f"t={t_phi:+.2f} vs 5%临界 {crit['5%']:.2f}")
                   + " (体检项, 只证伪水平漂移)"
                   + (f"; 其余阶 {'/'.join(_other)}" if _other else ""))
    else:
        c4 = False
        adf_txt = f"样本 {n}<12 不可检 (判据按不满足计)"

    # ⑤ 成本覆盖 + 净留存/MDE/高费率告警 — 名义额或费用列不可得时降级 (只少报不误报)
    if rows is None:
        rows = per_trade_rows()
    o_rows = [r for r in rows if r[0] and r[0] >= OOS_START]
    _bps_days, bps = day_bps(o_rows)
    if len(bps):
        net_bps = float(np.mean(bps))
        c5 = bool(net_bps - SLIP_HAIRCUT_BPS > 0)
        c5_txt = (f"净 {net_bps:+.2f}bps − 活体滑点 {SLIP_HAIRCUT_BPS:.0f}bps = "
                  f"{net_bps - SLIP_HAIRCUT_BPS:+.2f}bps (按日等权 {len(bps)} 日)")
    else:
        c5 = False
        c5_txt = "名义额/费用列不可得 (降级: 按不满足计)"

    print(f"  [{'✅' if c1 else '❌'}] OOS ≥15 日              : {n}")
    print(f"  [{'✅' if c2 else '❌'}] 日净均值 > 0 且 t ≥ {t_hurdle:.2f} : {mn:+.2f} "
          f"(t={tmn:+.2f}, df={n - 1})")
    print(f"  [{'✅' if c3 else '❌'}] 毛均值 t > 1 (供给端)    : {mg:+.2f} (t={tmg:+.2f})")
    print(f"  [{'✅' if c4 else '❌'}] ADF 平稳 (5%, lag=1)    : {adf_txt}")
    print(f"  [{'✅' if c5 else '❌'}] 成本覆盖 (RFC-077 G2)   : {c5_txt}")

    if o_rows:
        _k = len(o_rows)
        _f = sum(r[3] for r in o_rows)
        _nn = sum(r[2] for r in o_rows)
        _g = _nn + _f
        print(f"  净留存: 毛 {_g / _k:+.2f}/笔 → 费 {_f / _k:+.2f}/笔 → 净 {_nn / _k:+.2f}/笔 "
              f"(费吃掉毛利 {(_f / _g * 100) if _g else float('nan'):.1f}%)")
    _m = mde(o_net)
    if _m == _m:
        print(f"  MDE (80% 功效): n={n} 日 / sd={float(np.std(o_net, ddof=1)):.0f} → 可分辨 ±{_m:.0f}/日; "
              f"当前日均 {mn:+.2f} " + ("落在噪声内 (与 0 不可分辨)" if abs(mn) < _m else "超出噪声"))
    _hi = sorted((r for r in o_rows if r[1] > 0 and r[3] / r[1] * 100 > FEE_ALERT_PCT),
                 key=lambda r: -r[3] / r[1])
    if _hi:
        print(f"  ⚠️ 高费率笔 (record-only, 零行为变更): {len(_hi)} 笔往返费用/名义 > {FEE_ALERT_PCT:.0f}% — "
              + ", ".join(f"{r[4]} {r[0]} {r[3] / r[1] * 100:.2f}%" for r in _hi[:4])
              + (f" 等 {len(_hi)} 笔" if len(_hi) > 4 else "")
              + f"; 费用合计 {sum(r[3] for r in _hi):.2f}, 净额合计 {sum(r[2] for r in _hi):+.2f}")

    _gaps = [nm for nm, _ok in (("OOS≥15日", c1), ("日净均值显著性", c2), ("毛均值 t>1", c3),
                                ("ADF 平稳", c4), ("成本覆盖", c5)) if not _ok]
    print("  → " + ("五项全满足 = 达到形式门槛 (≠ 可实盘: 以 RFC-077 六道门禁为准, 仍需人工裁决)"
                  if not _gaps else "未达标 — 不建议讨论实盘化 (未达标项: " + ", ".join(_gaps) + ")"))


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
    _lines, _summ = adf_lag_block(per, (1, 2, 4))
    for _ln in _lines:
        print(_ln)
    if _summ:
        print(_summ)
    if not _lines:
        print(f"  样本 n={len(per)} < 13, ADF 各阶均不可检 (功效不足, 只报上面的均值/t)")
    graduation_verdict(d_net, net, d_gross, gross)


if __name__ == "__main__":
    main()
