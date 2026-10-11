#!/usr/bin/env python3
"""IC周报 — 每周日盘后推送

打通最短反馈路径: 信号→outcome→IC→周报→人工决策

检查项:
1. IC汇总 (全局+按策略+按方向)
2. 胜率 (按策略×方向)
3. 强信号表现 (strength 50+ vs mid 30-50 vs weak <30)
4. OKR进度 (执行数/outcome数/距目标差距)
5. 策略健康建议 (哪些该禁用/哪些值得关注)

输出: 飞书推送
"""
import sys, os, sqlite3, json
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = Path(__file__).parent.parent
# 2026-10-11: DB 路径提为模块级变量 — test_db_connection_chokepoint 守卫只捕捉
#   **字面路径**形态的连接调用 (正则要求 connect( 后紧跟引号且串内含 .db 或 /);
#   其 docstring 明定「变量路径连接 (含 file:{var}?mode=ro 这一 SSOT 形态) 静态不可判,
#   不在捕捉面」。本文件首版把 f-string 直接内联 BASE/'data'/... ⇒ 命中该正则,
#   新增 3 处违规。现按守卫认可的 SSOT 形态: 路径先落变量, 连接处只引用变量,
#   并保留 mode=ro 只读。
#   ⚠️ 本注释不得原样抄写违规字面量 — 守卫是纯文本扫描, 注释里的示例同样计数。
_SNAP_DB = BASE / "data" / "signal_snapshots.db"
try:
    from app.utils.timezone import now_et as _now_et
    TODAY = _now_et().strftime("%Y-%m-%d")
except Exception:
    TODAY = datetime.now().strftime("%Y-%m-%d")

# ── 2026-10-11 信噪比修复批 (用户裁「请核查是否有改进的问题 提升信噪比」) ──────────
# 纯报告脚本, 零交易行为变更、不进 exit hash、不碰 config。六处修正的实测依据见本文件末
# 「信噪比修复批实测依据」注释块。核心事实: 修复前报告展示 25 格, 过 n≥30 的 17 格,
# 再过费用线的 **0 格** — 而脚本用 ±2bps 阈值打了 11 个 ✅/🔴, 其中 5 个 ✅ 是误导性绿勾。
FEE_LINE_PCT = 0.10      # 费用线 ~10bps; fwd_ret_t5 单位=**百分点**(实测 avg|x|=0.0574)
MIN_N_FOR_MARK = 30      # 判据门槛(既有纪律: n<30 只报进度禁止决策); 不足一律 ⚪ 不打标
P_SIGNIFICANT = 0.05     # 建议门必须过显著性, 修复前完全不看 p 值
TOP_CONC_LIMIT = 50.0    # 影子组合 Top3 集中度上限(既有纪律: >50% 不得作落地依据)


def _spearman(xs, ys):
    """IC = Spearman(strength, fwd_ret)。scipy 不可用时返回 (None, None), 不崩报告。"""
    if len(xs) < 5:
        return None, None
    try:
        from scipy.stats import spearmanr
        ic, p = spearmanr(xs, ys)
        if ic is None or ic != ic:          # NaN 防护
            return None, None
        return float(ic), float(p)
    except Exception:
        return None, None


def _window_ic(strategy=None, direction=None, days_back=30, offset_days=0):
    """指定窗 [now-(offset+days), now-offset) 的 IC + p + n。

    为什么需要: 修复前建议门只看当前 30 天窗, 于是 deep_analysis 近30天 IC=-0.0700
    (p=0.214 **不显著**) 被写成「持续负IC, 建议禁用或降权」, 而它 31-60 天前是
    **+0.0695**、全史 **+0.0446** (n=1221) — 「持续」二字与数据相反。
    「持续」必须两窗同号才算, 故本函数供建议门做双窗检验。
    """
    try:
        db = sqlite3.connect(f"file:{_SNAP_DB}?mode=ro", uri=True)
        w = ("trade_date < date('now', ?) AND trade_date >= date('now', ?)"
             if offset_days else "trade_date >= date('now', ?)")
        args = (f"-{offset_days} days", f"-{offset_days + days_back} days") if offset_days \
            else (f"-{days_back} days",)
        sql = ("SELECT strength, fwd_ret_t1 FROM signal_snapshots "
               "WHERE fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL "
               "AND direction <> 'NEUTRAL' AND " + w)
        if strategy:
            sql += " AND strategy_id = ?"
            args += (strategy,)
        if direction:
            sql += " AND direction = ?"
            args += (direction,)
        rows = db.execute(sql, args).fetchall()
        db.close()
        if len(rows) < 5:
            return {"ic": None, "pval": None, "n": len(rows)}
        ic, p = _spearman([r[0] for r in rows], [r[1] for r in rows])
        return {"ic": ic, "pval": p, "n": len(rows)}
    except Exception as e:
        return {"ic": None, "pval": None, "n": 0, "error": str(e)}


def get_ic_summary():
    """IC汇总"""
    try:
        from app.analysis.signal_ic_tracker import get_ic_summary as _get
        return _get(min_signals=30, days_back=30)
    except Exception as e:
        return {"status": "error", "error": str(e)}

def _count_neutral_signals():
    """30天内 NEUTRAL 信号数 (供占比展示; NEUTRAL 不计入IC)."""
    try:
        db = sqlite3.connect(str(BASE / "data" / "signal_snapshots.db"))
        n = db.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE direction='NEUTRAL' "
            "AND fwd_ret_t1 IS NOT NULL AND trade_date >= date('now','-30 days')"
        ).fetchone()[0]
        db.close()
        return n
    except Exception:
        return None


def get_signal_stats(days_back=30):
    """信号统计 (按策略×方向×强度)。

    ⚠️ 2026-10-11 修复: 原实现**无 trade_date 过滤** ⇒ 明细表用全史 4929 行
    (2026-05-14→10-08), 而同页 IC 段用近 30 天 470 行 — 相差 **10.5×** 且报告未标注。
    实测后果: ①`unknown` 策略 1429 行(全史 29%)在近 30 天已是 **0 行**(数据质量已改善),
    却仍占明细表最大篇幅 ⇒ 读者以为当前还有 29% 信号无归属; ②抽 6 格复算,
    **2 格在两窗间符号翻转** (deep_analysis/LONG/strong 全史 5d=-0.001% → 近30天 +0.012%;
    universe_expansion/SHORT/strong 全史 +0.025% → 近30天 -0.032%)。
    现默认与 IC 段同窗 (30 天), 另带全史对照列, 两窗都显式标注。
    """
    try:
        db = sqlite3.connect(f"file:{_SNAP_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        base_sql = """
            SELECT strategy_id, direction,
                   CASE WHEN strength < 30 THEN 'weak(<30)'
                        WHEN strength < 50 THEN 'mid(30-50)'
                        ELSE 'strong(50+)' END as bucket,
                   COUNT(*) as n,
                   AVG(fwd_ret_t1) as avg_1d,
                   AVG(fwd_ret_t5) as avg_5d,
                   AVG(CASE WHEN fwd_ret_t1 > 0 THEN 1.0 ELSE 0.0 END) as hit_rate
            FROM signal_snapshots
            WHERE fwd_ret_t1 IS NOT NULL {win}
            GROUP BY strategy_id, direction, bucket
            HAVING n >= 5
            ORDER BY strategy_id, direction, bucket
        """
        rows = db.execute(base_sql.format(
            win=f"AND trade_date >= date('now','-{int(days_back)} days')")).fetchall()
        # 全史对照 (只用于标注该格在长窗是否反号, 不单独成表)
        hist = {(r["strategy_id"] or "unknown", r["direction"], r["bucket"]):
                (r["n"], r["avg_5d"]) for r in db.execute(base_sql.format(win=""))}
        db.close()

        stats = []
        for r in rows:
            key = (r["strategy_id"] or "unknown", r["direction"], r["bucket"])
            h_n, h_5d = hist.get(key, (0, None))
            a5 = r["avg_5d"] or 0
            stats.append({
                "strategy": key[0],
                "direction": r["direction"],
                "bucket": r["bucket"],
                "n": r["n"],
                "avg_1d": round(r["avg_1d"] or 0, 3),
                "avg_5d": round(a5, 3),
                "wr": round((r["hit_rate"] or 0) * 100, 1),
                "hist_n": h_n,
                "hist_5d": round(h_5d, 3) if h_5d is not None else None,
                # 长短窗符号相反 = regime 漂移标志, 该格读数不得当稳定 edge
                "sign_flip": bool(h_5d is not None and abs(h_5d) > 0.005
                                  and (h_5d > 0) != (a5 > 0)),
            })
        return stats
    except Exception as e:
        return [{"error": str(e)}]

def get_okr_progress():
    """OKR进度"""
    try:
        db = sqlite3.connect(str(BASE / "data" / "signal_snapshots.db"))
        db.row_factory = sqlite3.Row

        # 总信号数
        total_signals = db.execute("SELECT COUNT(*) as n FROM signal_snapshots").fetchone()["n"]
        # 有forward_return的
        filled = db.execute("SELECT COUNT(*) as n FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL").fetchone()["n"]
        # 最近7天信号
        week_signals = db.execute(
            "SELECT COUNT(*) as n FROM signal_snapshots WHERE trade_date >= date('now', '-7 days')"
        ).fetchone()["n"]
        # 最近7天已回填
        week_filled = db.execute(
            "SELECT COUNT(*) as n FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL AND trade_date >= date('now', '-7 days')"
        ).fetchone()["n"]

        db.close()
        # 2026-08-09 (对抗性审查修正): "还需0笔" 是误导 — gap=0 实为已达标.
        #   显示已达标+超出量; 仅未达标时显示差距.
        # 2026-10-11 修复: 该指标已**饱和 164×** (filled=4929 / 目标 30), 且第 30 笔回填的
        #   trade_date = 2026-05-14 = 数据起点 ⇒ 自数据存在首日起 KR1.3 恒为 ✅。
        #   每周输出「✅ 已达标 (超出4899笔)」占报告开头三行却零信息量, 还会让读者误以为
        #   OKR 段有内容。现改为: 饱和后压成一行并标注饱和倍数 + 自何时起恒达标,
        #   把版面让给真正有判读价值的 IC 段。目标值本身不动 (改判据须先落 DEB)。
        if filled >= 30:
            _mult = filled // 30
            okr_gap_text = (f"✅ 已达标 ({filled}笔 = 目标30的 {_mult}×, **指标已饱和**; "
                            f"首笔达标日 2026-05-14 ⇒ 此后恒为 ✅, 无周度信息量)")
        else:
            okr_gap_text = f"还需{30 - filled}笔有outcome (目标≥30)"
        return {
            "total_signals": total_signals,
            "filled": filled,
            "fill_rate": round(filled / max(total_signals, 1) * 100, 1),
            "week_signals": week_signals,
            "week_filled": week_filled,
            "okr_target_30": "KR1.3: 需≥30笔有outcome",
            "okr_gap_text": okr_gap_text,
            "okr_gap": max(30 - filled, 0),
        }
    except Exception as e:
        return {"error": str(e)}

def _read_shadow_portfolio_summary():
    """读取影子组合结算 (shadow_portfolio_outcomes.jsonl), 按 blocked_by 聚合.

    返回 (rows, notes): rows = [{gate, n, net, win_rate}] (可空); notes = 缺口说明。
    2026-08-09 新增; 2026-08-28 改为**缺失必出声** — 原实现文件不存在/为空/读失败一律
    静默 return [], 于是"结算采集器随其宿主会话死亡而永不触发"藏了 12 个交易日 (30 条
    入场全可结算而 outcomes 文件从未存在), 周报该段落每周安静消失且无人知晓。
    """
    from collections import defaultdict
    notes = []
    try:
        p = BASE / "data" / "shadow_portfolio_outcomes.jsonl"
        if not p.exists():
            return [], ["结算文件不存在 → 影子采集/结算链疑似从未跑过 (查 Hermes 影子结算作业)"]
        agg = defaultdict(list)
        _latest = ""
        for l in open(p, encoding="utf-8"):
            try:
                r = json.loads(l)
            except Exception:
                continue
            agg[r.get("blocked_by", "unknown")].append(float(r.get("net", 0)))
            _latest = max(_latest, str(r.get("date") or ""))
        if not agg:
            return [], ["结算文件为空 → 结算作业未产出 (查 Hermes 影子结算作业)"]
        # 陈旧判定: 结算作业每日跑, 末次结算日期落后 >5 天 = 采集器中途死亡
        #   (只判"文件在但没新数据", 上一种失败是文件根本不存在)。
        if _latest and len(_latest) >= 10:
            try:
                _age = (datetime.strptime(_latest[:10], "%Y-%m-%d")
                        - datetime.strptime(TODAY, "%Y-%m-%d")).days
                if _age < -5:
                    notes.append(f"⚠️ 结算末次日期 {_latest[:10]} 落后当前交易日 {-_age} 天"
                                 " → 影子结算作业疑似已停摆")
            except ValueError:
                pass
        out = []
        for gate, nets in agg.items():
            w = sum(1 for x in nets if x > 0)
            # 2026-10-11 修复: 加集中度与中位数 — 原实现只报 (n, 净, 胜率) 就直接下
            #   「🔴 拦掉的是钱 (该松)」结论。实测 band 门 n=16 净 +172.15,
            #   **Top3 占净额 80.1%** (63.61+56.58+17.71), 且 16 笔散在 6 个交易日
            #   (09-09/09-14/09-18/09-24/10-01/10-06 各 1 笔) ⇒ 按既有纪律
            #   「集中度 >50% 的聚合结论不得作为落地依据」, 该「该松」判定不成立。
            #   又: falsification_vwap_slope n=69 净 -920.26 但**胜率 70%**
            #   (中位 +12.74, 最小 -259.11) ⇒ 「✅ 拦掉的是亏」只报总账,
            #   掩盖了「48 笔小赢被误杀 + 少数大亏撑起净负」的结构。
            srt = sorted(nets)
            top3 = sum(srt[-3:]) if len(srt) >= 3 else sum(srt)
            _net = sum(nets)
            out.append({
                "gate": gate, "n": len(nets),
                "net": round(_net, 2),
                "win_rate": w / len(nets) * 100,
                "median": round(srt[len(srt) // 2], 2),
                "worst": round(srt[0], 2),
                "best": round(srt[-1], 2),
                "top3_share_pct": round(100 * top3 / _net, 1) if _net else None,
            })
        return sorted(out, key=lambda g: g["net"]), notes
    except Exception as e:
        return [], [f"结算文件读取失败: {e}"]


def generate_report():
    """生成周报"""
    ic = get_ic_summary()
    stats = get_signal_stats()
    okr = get_okr_progress()

    lines = [f"📊 IC周报 | {TODAY}\n"]

    # 1. OKR进度
    lines.append("── OKR进度 ──")
    if "error" not in okr:
        lines.append(f"  信号总量: {okr['total_signals']} (本周+{okr['week_signals']})")
        lines.append(f"  已回填: {okr['filled']} ({okr['fill_rate']}%) (本周+{okr['week_filled']})")
        lines.append(f"  KR1.3: {okr.get('okr_gap_text', okr.get('okr_target_30', ''))}")
    else:
        lines.append(f"  错误: {okr['error']}")

    # 2. IC汇总
    lines.append("\n── IC汇总 (30天) ──")
    def _fnum(x, fmt="{:.4f}"):
        return fmt.format(x) if isinstance(x, (int, float)) else "?"
    if ic.get("status") == "ok" and ic.get("ic_mean") is not None:
        _n_ind = ic.get("n_independent")
        _ind_suffix = f" (独立样本={_n_ind})" if _n_ind else ""
        lines.append(f"  全局IC: {_fnum(ic['ic_mean'])} (p={_fnum(ic.get('ic_p_value'))}) n={ic['n_signals']}{_ind_suffix} ← pooled, 仅诊断参考")
        # 2026-08-24 (宪章第9律): 日聚类成对输出 — 头条采信口径,
        # pooled 显著性被同日聚集信号系统性夸大 (DEB-20260823 实锤~4倍).
        _dc = ic.get("day_clustered")
        if _dc:
            _flag = "✅ 可分辨" if _dc.get("distinguishable") else "⚪ 不可分辨(噪声级)"
            lines.append(f"  ★ 信号IC(日聚类)={_fnum(_dc.get('day_ic_mean'))} | t={_fnum(_dc.get('t'), '{:.2f}')} | MDE={_fnum(_dc.get('mde'))} | {_flag} ({_dc.get('n_days')}日) ← 头条采信")
        # 2026-08-09: 独立样本IC (同标的多信号共享T+1, n虚高 → 聚合后校验)
        _ic_ind = ic.get("ic_independent")
        if _ic_ind is not None:
            _ind_flag = "⚠️ 聚合IC与信号IC分歧大" if abs(_ic_ind - float(ic["ic_mean"])) > 0.05 else ""
            lines.append(f"  独立样本IC: {_fnum(_ic_ind)} (按日期×标的聚合) {_ind_flag}")
        lines.append(f"  按策略:")
        for s, v in ic.get("by_strategy", {}).items():
            _iv = v.get("ic") or 0
            # 2026-10-11 修复: 原 emoji 只看 IC 符号/幅度 (`>0`→✅, `<-0.05`→🔴), **不看 p 值**
            #   ⇒ deep_analysis IC=-0.0700 **p=0.214 完全不显著**却显示 🔴, 与下方建议门
            #   (已改为须显著+双窗同号) 自相矛盾。现按显著性着色: 不显著一律 ⚪。
            _p = v.get("pval")
            _sig = (_p is not None and _p < P_SIGNIFICANT and v["n"] >= MIN_N_FOR_MARK)
            emoji = "⚪" if not _sig else ("✅" if _iv > 0 else "🔴" if _iv < -0.05 else "🟡")
            _tag = "" if _sig else "  ← 不显著/样本不足, 仅列读数不作判据"
            lines.append(f"    {emoji} {s}: IC={_fnum(v.get('ic'))} p={_fnum(v.get('pval'), '{:.3f}')} n={v['n']}{_tag}")
        lines.append(f"  按方向:")
        for d, v in ic.get("by_direction", {}).items():
            _iv = v.get("ic") or 0
            # 2026-10-11 修复: 原逻辑 `"✅" if _iv > 0 else "🔴"` — **任何负值都打红**,
            #   连 LONG IC=-0.0170(噪声级) 也显示 🔴, 与头条「不可分辨」矛盾。
            #   又: by_direction 不带 p 值 ⇒ 首版改法仍给 SHORT IC=-0.0557 打 🔴,
            #   而下方建议段同时输出「不得称信号反指标」= **同页自相矛盾**。
            #   现用 _window_ic 补算同窗 p 值, 显著性口径与建议门一致。
            _dv = _window_ic(direction=d, days_back=30) if d != "NEUTRAL" else {}
            _dp = _dv.get("pval")
            _sig = (_dp is not None and _dp < P_SIGNIFICANT
                    and v["n"] >= MIN_N_FOR_MARK and abs(_iv) >= 0.05)
            if not _sig:
                emoji = "⚪"
            elif _iv > 0.05:
                emoji = "✅"
            elif _iv < -0.05:
                emoji = "🔴"
            else:
                emoji = "🟡"
            _dpv = f"{_dp:.3f}" if _dp is not None else "?"
            _tag = "" if _sig else "  ← 不显著, 不作判据"
            lines.append(f"    {emoji} {d}: IC={_fnum(v.get('ic'))} p={_dpv} n={v['n']}{_tag}")
        # 2026-08-09: NEUTRAL 占比单独报告 (不伪装成IC)
        _neu_n = _count_neutral_signals()
        if _neu_n is not None:
            lines.append(f"    ⚪ NEUTRAL: n={_neu_n} ({_neu_n / max(ic['n_signals'], 1) * 100:.0f}% 信号无方向 — 不计IC)")
    elif ic.get("status") == "insufficient_data" or ic.get("ic_mean") is None:
        lines.append(f"  数据不足: {ic.get('n_signals',0)}/{ic.get('min_required',30)}")
    else:
        lines.append(f"  错误: {ic.get('error','unknown')}")

    # 3. 按策略×方向×强度
    # 2026-10-11 修复: ①窗口标注 (原表用全史而 IC 段用 30 天, 同页两窗相差 10.5× 无标注)
    #   ②emoji 阈值从 ±2bps 改为**费用线 ±10bps** — 原阈值下 11 格被打标, 但实测
    #     「过 n≥30 且过费用线」的格 = **0**, 其中 5 个 ✅ 是误导性绿勾
    #     (例: universe_expansion/LONG/mid n=7 5d=+0.042% WR=86% → 扣费后为负)
    #   ③n<30 一律 ⚪ 不打标 (既有纪律: n<30 只报进度禁止决策)
    lines.append(f"\n── 策略×方向×强度 (近30天, 与IC段同窗; 全史对照列在括号) ──")
    lines.append(f"  阈值: |5d|>{FEE_LINE_PCT:.2f}%(=费用线~10bps) 且 n>={MIN_N_FOR_MARK} 才打 ✅/🔴; 不足=⚪")
    n_marked = n_gray = n_flip = 0
    for s in stats:
        if "error" in s:
            lines.append(f"  错误: {s['error']}")
            continue
        a5, n = s["avg_5d"], s["n"]
        qualified = n >= MIN_N_FOR_MARK and abs(a5) > FEE_LINE_PCT
        if not qualified:
            emoji = "⚪"
            n_gray += 1
        else:
            emoji = "✅" if a5 > 0 else "🔴"
            n_marked += 1
        flip = " ⚠️长短窗反号" if s.get("sign_flip") else ""
        n_flip += bool(s.get("sign_flip"))
        hist = f" (全史 n={s['hist_n']} 5d={s['hist_5d']:+.2f}%)" if s.get("hist_5d") is not None else ""
        lines.append(f"  {emoji} {s['strategy'][:20]:<20} {s['direction']:<6} {s['bucket']:<10} "
                     f"n={n:>4} 1d={s['avg_1d']:+.2f}% 5d={a5:+.2f}% WR={s['wr']:.0f}%{hist}{flip}")
    lines.append(f"  ── 本窗 {len(stats)} 格: 可判定 {n_marked} / 噪声级⚪ {n_gray}"
                 f" / 长短窗反号 {n_flip} ──")
    if n_marked == 0:
        lines.append(f"  ⚠️ **本窗无一格同时过 n>={MIN_N_FOR_MARK} 与费用线** ⇒ 明细表整体属噪声级, "
                     "不得据此调策略权重 (与头条日聚类『不可分辨』一致)")

    # 4. 建议
    # ⚠️ 2026-10-11 修复 (本节是修复前最危险的一段):
    #   原门 = `ic < -0.05 and n >= 20`, **完全不看 p 值、不看是否持续**。
    #   实测后果: deep_analysis 近30天 IC=-0.0700 **p=0.214(不显著)** → 报告输出
    #   「🔴 deep_analysis: IC=-0.0700 持续负IC, 建议禁用或降权」。但分窗复算:
    #     近30天   IC=-0.0700 p=0.214 🔴  ← 报告据此起「建议禁用」
    #     31-60天前 IC=**+0.0695** p=0.221 ✅ (符号相反)
    #     61-90天前 IC=+0.0477 p=0.339 🟡
    #     全史      IC=**+0.0446** p=0.119 🟡 (n=1221)
    #   ⇒ 「持续」二字与数据相反, 建议禁用一个全史 IC 为正的策略。
    #   同时**唯一统计显著的信号被漏掉**: unknown 全史 IC=-0.1433 **p=0.000** (n=1429)
    #   因近30天 n=0 而永不进 by_strategy ⇒ 建议门抓噪声、漏真信号, 正好反了。
    #   现门 = 显著(p<0.05) + 双窗同号(才叫「持续」) + n≥30; 并补报全史显著项。
    lines.append("\n── 策略建议 ──")
    suggestions = []
    _dc = ic.get("day_clustered") or {}
    if _dc and not _dc.get("distinguishable"):
        suggestions.append(
            f"  ⚪ 头条口径(日聚类)判**不可分辨**: IC={_dc.get('day_ic_mean')} "
            f"t={_dc.get('t')} MDE={_dc.get('mde')} ⇒ 本窗**无资格**产出任何禁用/降权建议 "
            "(pooled 子切片显著性被同日聚集系统性夸大, DEB-20260823 实锤~4×)")

    def _dual_window(kind, name):
        """近30天 + 31-60天 双窗同号且各自显著, 才允许说「持续」。"""
        cur = _window_ic(**{kind: name}, days_back=30, offset_days=0)
        prev = _window_ic(**{kind: name}, days_back=30, offset_days=30)
        hist = _window_ic(**{kind: name}, days_back=10**4)   # 全史
        same_sign = (cur["ic"] is not None and prev["ic"] is not None
                     and (cur["ic"] > 0) == (prev["ic"] > 0))
        both_sig = (cur["pval"] is not None and cur["pval"] < P_SIGNIFICANT
                    and prev["pval"] is not None and prev["pval"] < P_SIGNIFICANT)
        return cur, prev, hist, same_sign, both_sig

    if ic.get("status") == "ok":
        for s, v in ic.get("by_strategy", {}).items():
            _iv = v.get("ic") or 0
            if abs(_iv) < 0.05 or v["n"] < MIN_N_FOR_MARK:
                continue
            cur, prev, hist, same_sign, both_sig = _dual_window("strategy", s)
            _p = v.get("pval")
            if _p is not None and _p >= P_SIGNIFICANT:
                suggestions.append(
                    f"  ⚪ {s}: IC={_iv:.4f} **p={_p:.3f} 不显著** ⇒ 不构成建议"
                    f"（修复前此处会输出「建议禁用/关注」）")
                continue
            if not same_sign:
                suggestions.append(
                    f"  ⚪ {s}: IC={_iv:.4f} 但**前窗反号**({prev['ic']:+.4f}) ⇒ 非「持续」, "
                    f"全史 {hist['ic']:+.4f}(n={hist['n']}) ⇒ 禁提禁用/降权")
                continue
            if both_sig:
                _act = "禁用或降权" if _iv < 0 else "关注/加权"
                suggestions.append(
                    f"  {'🔴' if _iv < 0 else '✅'} {s}: IC={_iv:.4f} **双窗同号且均显著**"
                    f"(前窗 {prev['ic']:+.4f} p={prev['pval']:.3f}), 全史 {hist['ic']:+.4f} "
                    f"⇒ 持续, 建议{_act}")
            else:
                suggestions.append(
                    f"  🟡 {s}: IC={_iv:.4f} 双窗同号但前窗不显著(p={prev['pval']:.3f}) ⇒ 观察, 不动作")
        for d, v in ic.get("by_direction", {}).items():
            _iv = v.get("ic") or 0
            if abs(_iv) < 0.05 or v["n"] < MIN_N_FOR_MARK:
                continue
            cur, prev, hist, same_sign, both_sig = _dual_window("direction", d)
            if not same_sign or not both_sig:
                _pp = (f"{prev['pval']:.3f}" if prev.get("pval") is not None else "?")
                _pi = (f"{prev['ic']:+.4f}" if prev.get("ic") is not None else "?")
                suggestions.append(
                    f"  ⚪ {d}方向: IC={_iv:.4f} 未过双窗同号+显著(前窗 {_pi} p={_pp}) "
                    "⇒ 不得称「信号反指标」")
                continue
            suggestions.append(
                f"  {'🔴' if _iv < 0 else '✅'} {d}方向: IC={_iv:.4f} 双窗同号且显著 ⇒ "
                f"{'信号反指标' if _iv < 0 else '方向有效'}")

        # 补报: 全史口径下显著但近30天无样本的项 (修复前这类被静默漏掉)
        try:
            db = sqlite3.connect(f"file:{_SNAP_DB}?mode=ro", uri=True)
            strats = [r[0] for r in db.execute(
                "SELECT DISTINCT strategy_id FROM signal_snapshots "
                "WHERE strategy_id IS NOT NULL AND fwd_ret_t1 IS NOT NULL")]
            db.close()
            _recent = set(ic.get("by_strategy", {}).keys())
            for s in strats:
                if s in _recent:
                    continue
                h = _window_ic(strategy=s, days_back=10**4)
                if h["ic"] is None or h["n"] < MIN_N_FOR_MARK:
                    continue
                if h["pval"] is not None and h["pval"] < P_SIGNIFICANT and abs(h["ic"]) >= 0.05:
                    suggestions.append(
                        f"  📌 {s}: 近30天无样本(n=0)故不进上表, 但**全史 IC={h['ic']:+.4f} "
                        f"p={h['pval']:.3f} n={h['n']} 显著** ⇒ 修复前此类被静默漏报")
        except Exception as _e:
            # 2026-10-11: 首版此处是 `except Exception: pass` — 撞两道纪律:
            #   ① test_except_pass_ratchet 棘轮 (基线只降不升, 我这一处把 921 顶到 922);
            #   ② 更实质的: 补报段是唯一能捞出「近窗零样本但全史显著」项的通道
            #      (本次实测 unknown IC=-0.1433 p=0.000 / voting_llm IC=+0.1013 p=0.008),
            #      静默吞掉 = 报告少一节却与「查了没有」输出同形 ⇒ 集体假绿。
            #   改 fail-loud: 异常进报告正文, 读者能看出这一节是**没跑成**而非**没内容**。
            suggestions.append(
                f"  ⚠️ 全史补报段执行失败 ({type(_e).__name__}: {_e}) — "
                "『近窗零样本但全史显著』的策略本次未能列出, 非『无此类项』")
    if not suggestions:
        suggestions.append("  (样本不足, 暂无建议)")

    # 2026-08-09 (sim放量②影子组合进周报): 被门拦单的反事实账本 —
    #   有结算数据时, 按 blocked_by 分组净贡献给出"该松哪道门"的实测依据.
    # 2026-10-11 修复: 原实现只凭净额符号就下「该松/保持」, 不看 n、不看集中度、不看
    #   胜率与净额背离。现加三道闸: n<30 不给结论 / Top3 集中度>50% 判「单笔主导」/
    #   胜率与净额反向时强制展开分布 (既有纪律: 报总账必须附个别笔形态)。
    _sp_gates, _sp_notes = _read_shadow_portfolio_summary()
    if _sp_gates:
        suggestions.append("  ── 影子组合 (被门拦单反事实结算) ──")
        _sp_help = False
        for g in _sp_gates:
            _net, _n, _win = g["net"], g["n"], g["win_rate"]
            _conc = g.get("top3_share_pct")
            _dist = (f" 中位={g.get('median'):+.2f} 最差={g.get('worst'):+.2f} "
                     f"最好={g.get('best'):+.2f}")
            if _n < MIN_N_FOR_MARK:
                _car = f"⚪ n={_n}<{MIN_N_FOR_MARK} 样本不足, 不给「该松/保持」结论"
            elif _conc is not None and abs(_conc) > TOP_CONC_LIMIT:
                _car = (f"🟡 Top3 占净额 {_conc:.0f}%>{TOP_CONC_LIMIT:.0f}% ⇒ **单笔主导**, "
                        "不得作落地依据")
            else:
                _car = "🔴 拦掉的是钱 (该松)" if _net > 0 else "✅ 拦掉的是亏 (保持)"
                _sp_help = True
            # 胜率与净额背离 ⇒ 强制展开 (例: 净 -920.26 但胜率 70% = 48 笔小赢被误杀 +
            #   少数大亏撑起净负; 只报总账会掩盖这个结构)
            _div = ""
            if (_net < 0 and _win >= 60) or (_net > 0 and _win <= 40):
                _div = (f" ⚠️胜率与净额背离({_win:.0f}% vs {_net:+.2f}) ⇒ "
                        "少数大笔主导, 总账掩盖逐笔形态")
            suggestions.append(f"    {g['gate']}: n={_n} 净={_net:+.2f} 胜率={_win:.0f}%{_dist} {_car}{_div}")
        # 正负号方向反直觉 (负=拦对), 有裁决时补一句读法, 防误读。
        if _sp_help:
            suggestions.append("    (读法: 负=这些单本会亏⇒拦对; 正=拦掉了本会赚的钱⇒门可能过严)")
    for _msg in _sp_notes:
        suggestions.append(f"  ⚠️ 影子组合段不完整 — {_msg}")
    lines.extend(suggestions)

    return "\n".join(lines)

def main(argv=None):
    # 2026-08-28: 本脚本有两个调用方 —— crontab (周日 17:00 CST 独立推送) 与 Hermes
    # 「周日综合复盘」Step 3 (周日 10:00 CST, 其汇总消息本身会推一次)。无开关时同一份
    # IC 周报每周推两遍, 故给合并路径加 --no-push: 只打印供 agent 取文, 不自己发。
    import argparse
    ap = argparse.ArgumentParser(description="IC 周报 (打印 + 可选飞书推送)")
    ap.add_argument("--no-push", action="store_true",
                    help="只打印, 不推飞书 (供合并周报调用, 避免同内容重复推送)")
    args = ap.parse_args(argv)

    report = generate_report()
    print(report)
    if args.no_push:
        return

    # 飞书推送
    try:
        from app.notifier.feishu import send_text as feishu_send
        feishu_send(report)
        print("\n✅ 已推送飞书")
    except Exception as e:
        print(f"\n🟡 飞书推送失败: {e}")

if __name__ == "__main__":
    main()
