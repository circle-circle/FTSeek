#!/usr/bin/env python3
"""RTH fade 影子**毕业裁定器** (只读, 零下单) —— 影子攒到门槛后, 一键回答"能不能上真钱".

## 预注册判据 (与 scripts/rth_fade_shadow.py / RFC-098 逐字一致; 读数出来前不得改)
1. 样本: **≥15 个 RTH 日 且 ≥30 笔**
2. 净/笔 **> 0** (费已扣, 0.53%)
3. 胜率 **≥55%**
4. 留一口 (**剔最强 RTH 日**) 净/笔 **≥0**

四项**同时**成立 ⇒ 本器输出「达标, 提请 DEB 转正」; 任一项不成立 ⇒ 输出缺哪项、还差多少。
本器**只给结论, 不动任何生产路径** (转正须用户裁决 + 独立 RFC)。

## 附带: 通道就绪度 (转正前必须先确认"钱能落地")
- **禁追线语义** (`app/playbook/entry_routing.py:chase_cap_violated`): SHORT 的 cap 是价格**下限**,
  违例判据 `price < cap` = "**低卖**"。⇒ shorting a +3% gap-up (高价卖出) **不违例**, fade 规则
  与禁追线**不碰撞** (实读代码, 非推测)。注 (2026-09-23 复查批更正): `chase_cap_enforce_enabled`
  在 `config/unified_config.yaml:1530` 实际为 **true (enforce 已激活)**, 代码缺省值才是 false ——
  早前本文写"默认 false = record-only"描述的是代码缺省, 与生产配置不符, 已勘误。
- **落地方案**: 走 RTH 空头通道 (= 已存在、已在成交的那条), **不需要**延长时段开空成交能力。
  实测该通道 (trade_records, 剔 P3_DUP_CLEANUP/EXTERNAL_CLOSE 大小写不敏感, 8/25 起,
  现役时代口径 A10) 有真实成交与净额。
- 另一条路 (ETH 开空) 仍是**未证实项** ⇒ 本规则的执行不依赖它。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LED = ROOT / "data" / "rth_fade_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"

NEED_DAYS, NEED_N = 15, 30
MIN_WIN, MIN_LEAVE = 55.0, 0.0


def load():
    if not LED.exists():
        return []
    rows = [json.loads(l) for l in LED.read_text().splitlines() if l.strip()]
    # 读取侧幂等防线 (2026-09-23 复查批): 生产侧 rth_fade_shadow.py 已按
    # (symbol, rth_day) 去重, 此处再兜一层 —— 历史重复行不膨胀毕业门计数。
    seen, out = set(), []
    for r in rows:
        k = (r.get("symbol"), r.get("rth_day"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _net_usd(r: dict) -> float:
    """每笔净收益 — 优先含借券费口径 (pnl_net_after_borrow), 向后兼容回退 net_usd.

    2026-09-24 RFC-060: 毕业判据切换到含借券费净额口径. 老数据 (无新字段) 退回
    net_usd (只扣佣金), 并在 main() 打印口径提示. 新数据含借券费 ⇒ 净额更保守.
    """
    if "pnl_net_after_borrow" in r and r["pnl_net_after_borrow"] is not None:
        return float(r["pnl_net_after_borrow"])
    return float(r.get("net_usd") or 0)


def channel_readiness():
    """RTH 空头通道实况 (只读 sqlite; 剔非策略平仓)。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.core.db_utils import era_clean_where
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        # A10 现役时代口径 (2026-10-14 用户裁): 当前 entry_time>=8/25 窗口天然在现役
        # 时代内, 拼谓词是防窗口放宽后 4-6 月 1M 旧时代大单倒灌 (SSOT: db_utils)。
        era = era_clean_where(c)
        row = c.execute("""
            SELECT COUNT(*),
                   ROUND(AVG(pnl_pct)*100, 2),
                   ROUND(SUM(COALESCE(pnl_net, realized_pnl)), 0),
                   SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END)
            FROM trade_records
            WHERE direction LIKE '%SHORT%'
              -- 2026-10-14 大小写修复: DB 混存 'EXTERNAL_CLOSE'/'external_close' 两态,
              -- SQLite IN 大小写敏感, 旧写法只剔小写 (ORCL 4/24 人工单即大写态,
              -- 此前靠 entry_time>=8/25 窗口挡住, 窗口一放宽即漏)。UPPER(NULL)=NULL
              -- ⇒ 未平仓行排除语义与旧写法一致。
              AND UPPER(exit_reason) NOT IN ('P3_DUP_CLEANUP', 'EXTERNAL_CLOSE')
              AND entry_time >= '2026-08-25'
        """ + era).fetchone()
        c.close()
        n, avg, net, win = row
        if not n:
            return "RTH 空头通道: 近期无干净成交样本"
        return (f"RTH 空头通道实况(8/25起, 剔非策略平仓·大小写不敏感, 现役时代口径A10): "
                f"{n} 笔, 均 {avg:+.2f}%/笔, "
                f"净 ${net:+.0f}, 胜 {win}/{n} ({100*win/n:.0f}%)")
    except Exception as e:
        return f"RTH 空头通道实况: 读取失败 ({e})"


def main():
    recs = load()
    print("=" * 62)
    print("RTH fade 影子 · 毕业裁定 (预注册判据, 只读)")
    print("=" * 62)
    if not recs:
        print("台账无样本 ⇒ 未开始攒样 (检查 cron 的 scripts/rth_fade_shadow.py)")
        return 1

    days = sorted({r.get("rth_day") for r in recs if r.get("rth_day")})
    nets = [_net_usd(r) for r in recs]
    rets = [r.get("ret_pct") or 0 for r in recs]
    # 口径提示 (2026-09-24 RFC-060): 多少笔含借券费字段 vs 老口径回退
    has_bf = sum(1 for r in recs if "pnl_net_after_borrow" in r)
    bf_rate = next((r.get("borrow_fee_rate_used") for r in recs if r.get("borrow_fee_rate_used")), None)
    bf_total = sum(float(r.get("borrow_fee_est") or 0) for r in recs)
    n = len(recs)
    per = sum(nets) / n
    win = 100 * sum(1 for x in nets if x > 0) / n
    day_tot = {}
    for i, r in enumerate(recs):
        day_tot[r.get("rth_day")] = day_tot.get(r.get("rth_day"), 0) + nets[i]
    best = max(day_tot, key=lambda d: day_tot[d]) if day_tot else None
    rest = [nets[i] for i, r in enumerate(recs) if r.get("rth_day") != best]
    leave_net_per = (sum(rest) / len(rest)) if rest else 0.0
    # 留一口用 net_usd 口径 (含借券费后), 不再单独减 fee_pct (nets 已含费)
    # 原逻辑 leave_pct = avg(ret_pct) - fee_pct; 新逻辑 leave_net = avg(pnl_net_after_borrow)
    # 两者语义等价 (pnl_net_after_borrow ≈ SIZE*(ret_pct-fee_pct)/100 - borrow_fee)

    print(f"台账: {n} 笔 / {len(days)} 个 RTH 日 | 净/笔 ${per:+.2f} 胜率 {win:.0f}% "
          f"合计 ${sum(nets):+.0f} | 留一口均净 ${leave_net_per:+.2f}")
    print(f"  口径: 含借券费 (RFC-060 净额) | {has_bf}/{n} 笔含借券费字段 | "
          f"借券费率 {bf_rate}% (缺省 1.0) | 借券费合计 ${bf_total:.2f}")
    print(f"  注: RTH fade 严格日内, 默认口径下借券费=$0 (int(6.5h/24)=0); "
          f"设 BORROW_FEE_MIN_DAYS=1 可模拟最低 1 日收费")
    print(f"最近入账: {max((r.get('date') or '') for r in recs)} (影子逐日累积)\n")

    checks = [
        ("① 样本 ≥15 日 且 ≥30 笔", len(days) >= NEED_DAYS and n >= NEED_N,
         f"现有 {len(days)} 日 / {n} 笔 ⇒ 还差 {max(0, NEED_DAYS-len(days))} 日 / {max(0, NEED_N-n)} 笔"),
        ("② 净/笔 > 0 (含借券费)", per > 0, f"${per:+.2f}/笔"),
        ("③ 胜率 ≥55%", win >= MIN_WIN, f"{win:.0f}%"),
        ("④ 留一口 (剔最强日) 净/笔 ≥0", leave_net_per >= MIN_LEAVE,
         f"${leave_net_per:+.2f}/笔 (最强日 {best} 剔除后 n={len(rest)})"),
    ]
    for name, ok, detail in checks:
        print(f"  [{'✅' if ok else '❌'}] {name} — {detail}")
    passed = all(ok for _, ok, _ in checks)

    print()
    print(channel_readiness())
    print("禁追线审查: SHORT cap = 价格下限, 违例=低卖 ⇒ 高价做空 fade **不碰撞** "
          "(app/playbook/entry_routing.py:chase_cap_violated; enforce 默认 false)")
    print()
    if passed:
        print("===> 四项全过: **达标**, 提请 DEB 转正 (须用户裁决 + 独立 RFC; 本器不下任何单)")
        return 0
    print("===> 未达门槛: 继续攒样。禁止在读数未达标时挑臂/改判据 (RFC-098 预注册纪律)")
    return 0


if __name__ == "__main__":
    sys.exit(main())