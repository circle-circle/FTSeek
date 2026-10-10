#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Exit Scorecard — 退出纪律周度判分器 (只读)

用途 (2026-07-12 建立):
  DA-FTSeek 链路的迭代仪表。回答一个问题:
    "信号本身(持到收盘)是赚的, 但退出机制把它做亏了多少?"

  核心发现 (建立时): 同批成交单里, 收盘方向为正的占比 ≈ 51%,
  但实际成交胜率只有 41% —— 退出机制把本该赢的单子做成了亏损。
  漏血在执行/退出纪律, 不在预测。本脚本把这个缺口固化成每周可对比的数字。

每周五盘后跑一次, 只改一个退出参数, 下周同一张表对比: 变好→保留, 变差→回滚。

口径 (与 scripts/okr_status.py 对齐):
  - $ 层面 (R:R / expectancy): trade_records COALESCE(pnl_net, realized_pnl) 为 SSOT
    (净口径, DEB-20260730 D1; playbook_instructions.pnl 会被 ORPHAN_CLEANUP 幽灵单污染, 不用)。
  - 混淆矩阵 (收盘方向 vs 实际结局): playbook_instructions
    (outcome=win/loss 是分类, 不涉 $, 不受幽灵单影响) join fwd_return_intraday。
  - 尺寸归一 (2026-09-06 新增, 与 $ 口径**并列不替代**): bps of notional。美元口径跨
    2026-08-06 sizing 变更 (名义额中位 $3,307→$5,737) 不可比, 见 size_normalized()。
  - G4 回撤腿 (2026-09-06 修尺子): 从 trade_records 出场日记账重建权益曲线, 锚定
    capital_state.current_equity。原读 capital_state 逐行算峰谷 → 该表是单行状态表
    (UNIQUE(trade_env)), 窗口内恒 1 行 → max_dd 恒 0.0% = 回撤腿永久免检 (假绿)。
    见 reconstruct_max_drawdown()。

用法:
  python scripts/exit_scorecard.py                 # 默认近30天
  python scripts/exit_scorecard.py --since 2026-07-11   # 只看断点后干净窗口
  python scripts/exit_scorecard.py --days 14       # 近14天
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

# 与 okr_status.py 同口径: OKR 目标是 Playbook 系统
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')


def _et_cutoff(days: int) -> str:
    """ET 口径的 N 天前日期 (date('now') 是 UTC, 会差一天)。"""
    try:
        from app.utils.timezone import now_et
        return (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs):
    """样本标准差 (n-1)。n<2 或零方差 → 0.0。"""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _day_clustered_t(daily_values):
    """按日聚类后的单样本 t + 80% 功效 MDE (宪章第9律: pooled 显著性夸大~8x)。

    与 IC 侧 `day_ic_mean` 同约定: 先按日取均值 (等权日), 再对日均值做 t 检验 —
    否则成交笔数多的日子会主导统计量。
    单位声明: t 无量纲; day_mean / mde_80pct 与入参同量纲 (本脚本用于 bps)。
    日聚类数 <3 或零方差 → None (不可判, 不得当成 0 或 PASS)。
    """
    n_days = len(daily_values)
    if n_days < 3:
        return None
    sd = _stdev(daily_values)
    if sd == 0:
        return None
    day_mean = _mean(daily_values)
    t = day_mean / (sd / math.sqrt(n_days))
    return {
        "t": round(t, 2),
        "n_days": n_days,
        "day_mean": round(day_mean, 2),
        "mde_80pct": round(2.802 * sd / math.sqrt(n_days), 2),
        "distinguishable": bool(abs(t) >= 2.0),
    }


def size_normalized(rows, slip_haircut_bps):
    """尺寸归一口径 (bps of notional) — 美元口径的并列头条。

    为什么必须有这把尺子 (2026-09-06 修尺子):
      2026-08-06 三个 sizing hotfix (`8b81f4dc` 单笔上限 4%→6% / `8e281cd3` BUY 仓位因子
      0.5→1.0 / `e889b6b5` 删重复 max_position_pct 键致此前上调空转) 使名义额中位
      $3,307→$5,737 (8/7 一步跳), 而每笔佣金几乎不变 ($4.14→$4.21) → 费率 14.5→9.0bps。
      于是"净 $/笔变好"里混着"注下大了 2.5 倍", 跨该断点的美元口径不可比。
      bps 口径把分母归一, 才是同一把尺子; 与美元口径并排输出, 防止自己骗自己。

    rows: [{"date","gross","net","fee","notional"}, ...]  ($)
    单位声明: gross_bps/fee_bps/net_bps/day_mean/mde_80pct = 占名义额万分比 (1bp = 0.01%);
              slip_haircut_roundtrip_bps 同量纲, 由 2×live_readiness.slippage_haircut_bps 得。
    取样面 (与 okr_status.py 的 _BPS_GUARD 严格同面, 两边必须同值 — 见
    tests/test_ruler_bps_and_g4_reconstruction.py): notional>0 **且** net 非空。未结算净额的行
    不得计入 n, 否则 n 与三个均值的实际样本数不一致 (同 okr_status cnt_bps 曾犯的错)。
    """
    rs = [r for r in rows if r.get("notional") and r["notional"] > 0 and r.get("net") is not None]
    if not rs:
        return {"unit": "bps of notional (万分比)", "n": 0,
                "note": ("窗口内无可算样本 (需 notional>0 且净额非空) → bps 口径不可算; "
                         "不可测不得当成 0")}

    def _bps(key):
        return [r[key] / r["notional"] * 1e4 for r in rs if r.get(key) is not None]

    gross_bps, fee_bps, net_bps = _bps("gross"), _bps("fee"), _bps("net")
    daily = {}
    for r in rs:
        if r.get("net") is None or not r.get("date"):
            continue
        daily.setdefault(r["date"], []).append(r["net"] / r["notional"] * 1e4)
    net_mean = _mean(net_bps)
    roundtrip = 2 * float(slip_haircut_bps)
    return {
        "unit": "bps of notional (万分比; 1bp = 0.01%)",
        "n": len(rs),
        "gross_bps": round(_mean(gross_bps), 2),
        "fee_bps": round(_mean(fee_bps), 2),
        "net_bps": round(net_mean, 2),
        "net_bps_day_clustered": _day_clustered_t([_mean(v) for v in daily.values()]),
        "slip_haircut_roundtrip_bps": round(roundtrip, 2),
        "net_bps_after_slip_haircut": round(net_mean - roundtrip, 2),
        "edge_covers_roundtrip_cost": bool(net_mean - roundtrip > 0),
        "note": ("美元口径受 2026-08-06 sizing 变更污染 (名义额中位 $3,307→$5,737), "
                 "跨 8/7 断点只可比 bps 口径; 本块与 payoff 美元口径并列, 不替代它"),
    }


def reconstruct_max_drawdown(events, anchor_equity):
    """从出场日记账重建窗口内权益曲线, 算峰谷最大回撤 (净口径)。

    为什么不读 capital_state 逐行算峰谷 (2026-09-06 修尺子, 治 G4 回撤腿假绿):
      `capital_state` 是**单行状态表** — `UNIQUE INDEX idx_capital_state_trade_env`
      (app/risk/capital_safety.py:741) + 建表时 DELETE 非最大 id 行 (:733) → 每个 trade_env
      只保留最新一行。用它算峰谷恒得 0.0% → G4 回撤腿永久免检 (实测 max_drawdown_pct=0.0
      而 ok=True, 全表仅 1 行)。旧注释说该表 drawdown 列"语义不可信", 实际是**量纲**问题
      (入库为带符号分数, 见下 state 交叉校验), 但"只有 1 行"是更根本的失明。

    口径与 app/risk/absolute_circuit_breaker.py::_restore_from_db 同源 (不造平行层):
      trade_records 出场日记账 + 净额 COALESCE(pnl_net, realized_pnl)。
    锚点: capital_state.current_equity (live 权威值), 由窗口末倒推窗口初权益 →
      曲线终点与权威权益严格对齐, 不依赖 initial_capital 的全史对账 (存在出入金/非
      playbook 盈亏, 实测 initial+全史净 ≠ current_equity, 差 ~$7.5k)。

    events: [(exit_time, net_$), ...] 按 exit_time 升序; anchor_equity: 窗口末权益 ($)
    单位声明: 返回 max_drawdown_pct 为**正百分点** (5.0 = 5%), 与 G4 阈值 `< 5.0` 同量纲。
    已知代价 (fail-close 方向): 不含未平仓浮盈与出入金 → 是"已实现权益"近似, 真实回撤
      只会更大不会更小。不可测 (无锚点/无事件) → None, 调用方不得当成 0。
    """
    if not events or anchor_equity is None or anchor_equity <= 0:
        return None
    total_net = sum((net or 0) for _, net in events)
    eq = anchor_equity - total_net          # 窗口初权益
    peak = eq
    mdd = 0.0
    n_points = 1                            # 含窗口初那一点
    for _, net in events:
        eq += (net or 0)
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak * 100.0)
        n_points += 1
    if n_points < 2:
        return None
    return {"max_drawdown_pct": round(mdd, 2),
            "equity_start": round(anchor_equity - total_net, 2),
            "equity_end": round(anchor_equity, 2),
            "n_points": n_points, "n_events": len(events)}


def compute(since: str) -> dict:
    from app.storage.db_pool import get_db
    conn = get_db("ftseek_us").connection()  # row_factory=Row 由 pool 设置

    # ── 1. 混淆矩阵: 收盘方向(持到收盘) vs 实际成交结局 ──
    #    fwd_return_intraday = sign*(收盘-entry)/entry, >0 表示"若持到收盘方向是对的"
    ci_rows = conn.execute(
        """
        SELECT outcome, fwd_return_intraday AS fwd, direction
        FROM playbook_instructions
        WHERE outcome IN ('win','loss')
          AND fwd_return_intraday IS NOT NULL
          AND direction != 'SKIP'
          AND date >= ?
        """,
        (since,),
    ).fetchall()

    def confusion(rows):
        n = len(rows)
        a = sum(1 for r in rows if r["fwd"] > 0 and r["outcome"] == "win")   # 收盘正→赢 (忠实)
        b = sum(1 for r in rows if r["fwd"] > 0 and r["outcome"] == "loss")  # 收盘正→亏 (杀赢家) ★
        d = sum(1 for r in rows if r["fwd"] <= 0 and r["outcome"] == "win")  # 收盘负→赢 (救回)
        e = sum(1 for r in rows if r["fwd"] <= 0 and r["outcome"] == "loss")
        killed = [r["fwd"] for r in rows if r["fwd"] > 0 and r["outcome"] == "loss"]
        held_wr = (a + b) / n if n else 0.0
        exec_wr = (a + d) / n if n else 0.0
        return {
            "n": n,
            "held_to_close_wr": round(held_wr * 100, 1),   # 信号命中(持到收盘)
            "executed_wr": round(exec_wr * 100, 1),          # 实际成交胜率
            "edge_leak_pts": round((held_wr - exec_wr) * 100, 1),  # 退出漏掉的百分点
            "killed_winners": b,                              # ★ 被退出杀死的赢家
            "saved_losers": d,                                # 退出救回的输家
            "net_flipped": b - d,                             # 净被翻成亏的笔数
            "killed_avg_ret_pct": round(_mean(killed) * 100, 2) if killed else 0.0,
            "cm": {"pos_win": a, "pos_loss": b, "neg_win": d, "neg_loss": e},
        }

    overall_cm = confusion(ci_rows)
    buy_cm = confusion([r for r in ci_rows if r["direction"] == "BUY"])
    short_cm = confusion([r for r in ci_rows if ("SHORT" in (r["direction"] or "") or r["direction"] == "SELL")])

    # ── 2. R:R & 期望值: trade_records pnl_net (SSOT, DEB-20260730 D1) ──
    #   旧库无 pnl_net 列 → COALESCE 回退 realized_pnl (毛口径, 仅兼容)
    #   2026-09-06: 本查询是 G4 三腿 (payoff / 连亏 / 回撤重建) 的**唯一取样面**,
    #   原先回撤与连亏各写一遍同条件 SQL → 存在两份口径漂移风险, 现合一并按 exit_time 排序。
    src_filter = ", ".join(f"'{s}'" for s in PLAYBOOK_SOURCE_TYPES)
    tr_rows = conn.execute(
        f"""
        SELECT trade_date,
               exit_time,
               realized_pnl AS gross,
               COALESCE(fees_est, 0) AS fee,
               COALESCE(pnl_net, realized_pnl) AS pnl,
               entry_price * quantity AS notional
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({src_filter})
          AND trade_mode != 'live'
          AND exit_reason IS NOT NULL AND exit_reason != ''
        ORDER BY exit_time
        """,
        (since,),
    ).fetchall()
    pnls = [r["pnl"] or 0 for r in tr_rows]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]  # 存正数, 便于算 R:R
    nwl = len(wins) + len(losses)
    wr = len(wins) / nwl if nwl else 0.0
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    rr = (avg_win / avg_loss) if avg_loss else float("inf")
    breakeven_rr = ((1 - wr) / wr) if wr else float("inf")   # 当前胜率的盈亏平衡 R:R
    expectancy = wr * avg_win - (1 - wr) * avg_loss           # 每笔期望值 ($)
    gross_win = sum(wins)
    gross_loss = sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # RFC-062 G1-1 (2026-08-02): 滑点折算后期望 — round-trip = 2×haircut, 按 avg 名义本金折算
    _notionals = [r["notional"] for r in tr_rows if r["notional"] and r["notional"] > 0]
    avg_notional = _mean(_notionals)
    try:
        from app.core.config_container import get_config as _gc
        _hc_bps = float(_gc().get_dotted("live_readiness.slippage_haircut_bps", 15))
    except Exception:
        _hc_bps = 15.0
    expectancy_adj = expectancy - avg_notional * (2 * _hc_bps / 10000.0)

    payoff = {
        "n": nwl,
        "win_rate": round(wr * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "rr": round(rr, 2) if rr != float("inf") else None,
        "breakeven_rr": round(breakeven_rr, 2) if breakeven_rr != float("inf") else None,
        "rr_gap": round(rr - breakeven_rr, 2) if (rr != float("inf") and breakeven_rr != float("inf")) else None,
        "expectancy_per_trade": round(expectancy, 2),
        "avg_notional": round(avg_notional, 2),
        "slip_haircut_bps": _hc_bps,
        "expectancy_slip_adj": round(expectancy_adj, 2),
        "total_pnl": round(sum(pnls), 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
    }

    # ── 2b. 尺寸归一口径 (bps) — 与美元 payoff 并列的第二把尺子 ──
    size_norm = size_normalized(
        [{"date": r["trade_date"], "gross": r["gross"], "net": r["pnl"],
          "fee": r["fee"], "notional": r["notional"]} for r in tr_rows],
        _hc_bps,
    )

    # ── 3. G4 存活判据 (2026-08-01 补落盘: 原终裁 G4 无任何判分产物) ──
    #    阈值按冻结框架: 最大回撤 <5% 且 无连续 5 笔亏损 (窗口 = since, 净口径)
    #    回撤自算: capital_state 是单行状态表 (UNIQUE(trade_env)), 逐行峰谷恒 0.0% = 免检假绿
    #    → 2026-09-06 改从 tr_rows 出场日记账重建 (与 absolute_circuit_breaker 同源口径)。
    _cap = conn.execute(
        "SELECT current_equity, max_drawdown_pct FROM capital_state ORDER BY id DESC LIMIT 1"
    ).fetchone()
    _anchor_equity = float(_cap["current_equity"]) if (_cap and _cap["current_equity"]) else None
    _dd = reconstruct_max_drawdown(
        [(r["exit_time"], r["pnl"]) for r in tr_rows], _anchor_equity)
    max_dd = _dd["max_drawdown_pct"] if _dd else None

    # 交叉校验 (量纲声明): capital_state.max_drawdown_pct **入库为带符号分数**
    #   (app/risk/capital_safety.py:226 `(current-peak)/peak`, 负值=回撤), 对外 API 才 ×100
    #   (同文件 :499)。此处 ×100 取绝对值转成正百分点。注意窗口不同: 该列是**开户至今**,
    #   max_drawdown_pct 是**本窗口** — 并列输出只为互相测谎, 不可混用。
    _state_dd_pct = (round(abs(float(_cap["max_drawdown_pct"])) * 100.0, 2)
                     if (_cap and _cap["max_drawdown_pct"] is not None) else None)

    # 连亏腿沿用 tr_rows (与回撤腿同一取样面, 消除两份 SQL 漂移)
    seq_rows = tr_rows
    max_consec_loss = 0
    _cur = 0
    for r in seq_rows:
        if (r["pnl"] or 0) < 0:
            _cur += 1
            max_consec_loss = max(max_consec_loss, _cur)
        else:
            _cur = 0
    g4 = {
        "unit": "max_drawdown_pct / state_max_dd_pct_inception = 正百分点 (5.0 = 5%)",
        "max_drawdown_pct": round(max_dd, 2) if max_dd is not None else None,
        "max_drawdown_ok": (max_dd < 5.0) if max_dd is not None else None,
        "dd_source": "trade_journal_reconstruction" if _dd else None,
        "dd_window": ({"equity_start": _dd["equity_start"], "equity_end": _dd["equity_end"],
                       "n_points": _dd["n_points"], "n_events": _dd["n_events"]} if _dd else None),
        "dd_anchor_missing": bool(_anchor_equity is None),
        "state_max_dd_pct_inception": _state_dd_pct,
        "state_dd_unit_note": ("capital_state.max_drawdown_pct 入库=带符号分数 "
                               "(capital_safety.py:226), 此处已 ×100 转正百分点; 窗口=开户至今"),
        "max_consecutive_losses": max_consec_loss,
        "consecutive_losses_ok": max_consec_loss < 5,
        "verdict": None,
    }
    # fail-close: 任一条腿不可测 (仪器失明/无样本) → 不给 PASS, 判 UNKNOWN_FAIL_CLOSE。
    #   原逻辑在 max_dd 为 None 时 verdict=None, 下游 `verdict == 'PASS'` 之外一律当不过,
    #   但 None 会被读成"未判"而非"不过" → 显式化, 免得回撤腿失明时静默变绿。
    if g4["max_drawdown_ok"] is None or g4["consecutive_losses_ok"] is None:
        g4["verdict"] = "UNKNOWN_FAIL_CLOSE"
    else:
        g4["verdict"] = "PASS" if (g4["max_drawdown_ok"] and g4["consecutive_losses_ok"]) else "FAIL"

    return {"since": since, "pnl_basis": "net: COALESCE(pnl_net, realized_pnl) (DEB-20260730 D1)",
            "confusion": overall_cm, "by_buy": buy_cm, "by_short": short_cm, "payoff": payoff,
            "size_normalized": size_norm, "g4_survival": g4}


def render(rep: dict) -> None:
    cm = rep["confusion"]
    pf = rep["payoff"]
    print("=" * 66)
    print("  EXIT SCORECARD — " + datetime.now().strftime("%Y-%m-%d %H:%M") + f"   (since {rep['since']})")
    print("=" * 66)

    print("\n🩸 EDGE LEAK — 信号 vs 执行 (源: playbook_instructions)")
    if cm["n"] == 0:
        print("  ⚠️ 窗口内无成交样本 (需 date≥since 的 win/loss 且 fwd_return_intraday 已回填)")
    else:
        print(f"  样本 n={cm['n']}")
        print(f"  信号命中(持到收盘): {cm['held_to_close_wr']}%")
        print(f"  实际成交胜率:       {cm['executed_wr']}%")
        print(f"  ── 退出漏掉: {cm['edge_leak_pts']} 个百分点 ──")
        print(f"  收盘正→成交亏 (杀赢家 ★): {cm['killed_winners']}   本可 +{cm['killed_avg_ret_pct']}%/笔")
        print(f"  收盘负→成交赢 (救回):     {cm['saved_losers']}")
        print(f"  净被翻成亏损: {cm['net_flipped']} 笔")

    print("\n📐 PAYOFF — R:R & 期望值 (源: trade_records SSOT)")
    if pf["n"] == 0:
        print("  ⚠️ 窗口内无已平仓成交")
    else:
        print(f"  样本 n={pf['n']}  胜率 {pf['win_rate']}%")
        print(f"  avg赢 +${pf['avg_win']}   avg亏 ${pf['avg_loss']}")
        _rr = pf["rr"] if pf["rr"] is not None else "∞"
        _be = pf["breakeven_rr"] if pf["breakeven_rr"] is not None else "∞"
        print(f"  R:R = {_rr}   盈亏平衡需 ≥ {_be}   缺口 {pf['rr_gap']}")
        print(f"  每笔期望值: ${pf['expectancy_per_trade']}   总盈亏: ${pf['total_pnl']}")
        print(f"  每笔期望值(折滑点{pf['slip_haircut_bps']:g}bps×2): ${pf['expectancy_slip_adj']}  "
              f"(avg名义 ${pf['avg_notional']:.0f}, RFC-062 门1口径)")
        _pfv = pf["profit_factor"] if pf["profit_factor"] is not None else "∞"
        print(f"  Profit Factor: {_pfv}   (G2 裁决线 ≥1.3, 终裁窗整体)")
        if pf["n"] < 30:
            print(f"  ⚠️ n={pf['n']} <30 — 本窗统计功效不足, 期望值/PF 仅供方向参考")

    sn = rep.get("size_normalized") or {}
    print("\n📏 SIZE-NORMALIZED — bps 口径 (剔除仓位大小影响; 与上面美元口径并列)")
    if not sn.get("n"):
        print(f"  ⚠️ {sn.get('note', '窗口内无可用名义额')}")
    else:
        print(f"  样本 n={sn['n']}   毛 {sn['gross_bps']:+.2f}bps − 费 {sn['fee_bps']:.2f}bps "
              f"= 净 {sn['net_bps']:+.2f}bps/笔")
        _t = sn.get("net_bps_day_clustered")
        if _t:
            print(f"  净 bps 按日聚类: t={_t['t']:+.2f} (日={_t['n_days']}, 日均 {_t['day_mean']:+.2f}bps, "
                  f"MDE±{_t['mde_80pct']:.2f}bps) → "
                  f"{'✅ 与零可区分' if _t['distinguishable'] else '⚠️ 与零不可区分 (样本不足以下结论)'}")
        else:
            print("  净 bps 按日聚类: 日聚类数 <3 → 不可判 (不得当成 0)")
        print(f"  扣活体滑点 {sn['slip_haircut_roundtrip_bps']:g}bps(来回) 后: "
              f"{sn['net_bps_after_slip_haircut']:+.2f}bps → "
              f"{'✅ edge 盖得住成本' if sn['edge_covers_roundtrip_cost'] else '🔴 edge 盖不住来回成本'}")
        print(f"  ℹ️ {sn['note']}")

    g4 = rep.get("g4_survival") or {}
    if g4:
        print("\n🛡️ G4 SURVIVAL — 存活判据 (裁决线: 回撤<5% 且 连亏<5笔)")
        _dd = g4.get("max_drawdown_pct")
        print(f"  最大回撤(本窗口): {('%s%%' % _dd) if _dd is not None else 'N/A (不可测 → fail-close)'}   "
              f"连亏峰值: {g4.get('max_consecutive_losses')} 笔   裁决: {g4.get('verdict') or 'N/A'}")
        _dw = g4.get("dd_window")
        if _dw:
            print(f"  回撤源: {g4.get('dd_source')} — 权益 ${_dw['equity_start']:,.0f} → "
                  f"${_dw['equity_end']:,.0f} ({_dw['n_points']} 点/{_dw['n_events']} 笔出场)")
        elif g4.get("dd_anchor_missing"):
            print("  ⚠️ 回撤不可测: capital_state.current_equity 缺失 (锚点无从对齐)")
        _sdd = g4.get("state_max_dd_pct_inception")
        if _sdd is not None:
            print(f"  交叉校验 (开户至今, 源 capital_state): {_sdd}%   ← 窗口不同, 仅互相测谎")

    print("\n🧭 BY DIRECTION — 命中率 (持到收盘 / 实际成交)")
    for label, key in (("BUY  ", "by_buy"), ("SHORT", "by_short")):
        c = rep[key]
        if c["n"] == 0:
            print(f"  {label}: 无样本")
        else:
            print(f"  {label}: n={c['n']:3d}  持到收盘 {c['held_to_close_wr']}%  →  成交 {c['executed_wr']}%  (杀赢家 {c['killed_winners']})")

    # ── 一句话判词 ──
    print("\n📝 判词")
    if cm["n"] and pf["n"]:
        leak = cm["edge_leak_pts"]
        gap = pf["rr_gap"]
        exp = pf["expectancy_per_trade"]
        verdict = "✅ 正期望" if exp > 0 else "🔴 负期望"
        print(f"  {verdict} (${exp}/笔). 退出漏 {leak}pts, 杀赢家净 {cm['net_flipped']} 笔; "
              f"R:R 距盈亏平衡 {'+' if (gap or 0) >= 0 else ''}{gap}。")
        if sn.get("n"):
            _t = sn.get("net_bps_day_clustered")
            _sig = bool(_t and _t["distinguishable"])
            print(f"  尺寸归一: 净 {sn['net_bps']:+.2f}bps/笔, 扣来回滑点 "
                  f"{sn['slip_haircut_roundtrip_bps']:g}bps 后 {sn['net_bps_after_slip_haircut']:+.2f}bps; "
                  f"统计上{'可' if _sig else '不可'}与零区分。")
            if not sn["edge_covers_roundtrip_cost"]:
                print("  → bps edge 盖不住来回成本时, 美元正期望只是仓位放大的账面效果 — 不构成上线理由。")
        if exp <= 0:
            print("  → 本周只改一个退出参数(优先: 让赢家别被砍 / 拉高 R:R), 下周对比此表。")
            print("  → ⚠️ 期望值转正前, 不要靠加仓/放大 R:R 去追收益 —— 那是加速亏损。")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="退出纪律周度判分器 (只读)")
    ap.add_argument("--since", type=str, default=None, help="起始日期 YYYY-MM-DD (优先于 --days)")
    ap.add_argument("--days", type=int, default=30, help="近 N 天 (默认30)")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    since = args.since or _et_cutoff(args.days)

    # 基线警告: 07-24 三门上线 = 退出参数 config_version 断点, 跨窗口混合无意义
    BASELINE = "2026-07-24"
    if since < BASELINE:
        print(f"⚠️  警告: since={since} 早于三门基线 {BASELINE}, 窗口混入旧退出参数 regime。"
              f" 周度对比/裁决判据请用 --since {BASELINE} 或更晚。", file=sys.stderr)

    rep = compute(since)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        render(rep)

    # 落盘供每周复盘对比
    data_dir = Path(PROJECT_ROOT) / "data"
    data_dir.mkdir(exist_ok=True)
    rep_out = dict(rep, timestamp=datetime.now().isoformat())
    with open(data_dir / "exit_scorecard_latest.json", "w") as f:
        json.dump(rep_out, f, ensure_ascii=False, indent=2)
    with open(data_dir / "exit_scorecard_history.jsonl", "a") as f:
        f.write(json.dumps(rep_out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
