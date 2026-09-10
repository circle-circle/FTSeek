#!/usr/bin/env python3
"""隔夜持仓 shadow 结算器 (2026-08-10, record-only).

对 overnight_hold_shadow.jsonl 中未结算的决策点, 用次日 replay_bars 结算
"持仓过夜 vs 已退出"反事实:
  - gap: 隔夜跳空 (次日开盘 vs D日收盘) × 方向
  - fullday: 持到次日收盘 × 方向
  - best/worst: 次日路径上下界
结算结果写回 jsonl (settled=True + 反事实字段), 汇总输出 stdout +
data/overnight_hold_shadow_latest.json。

数据源: data/replay_bars/{SYM}_{date}.json (position_path_collector 盘后落)。
若次日 bar 缺失 (未采集), 保留 unsettled 待下次补算。

用法: python3 scripts/settle_overnight_shadow.py [--days N]  (默认结算全部未结算行)
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
BAR_DIR = ROOT / "data" / "replay_bars"
SHADOW = ROOT / "data" / "overnight_hold_shadow.jsonl"
OUT = ROOT / "data" / "overnight_hold_shadow_latest.json"

# 2026-09-10 (可观测性批): print → logger, 命名沿用 in-process 脚本既有约定 ftseek.<name>。
#   本脚本经 scheduler._load_script_module 在**服务进程内**执行, print 落 systemd stdout 后
#   无时间戳/无级别/无来源, 且整段报告会被其他线程输出穿插 (无锁)。合成单条 record 后由
#   handler 锁保证原子, 且首行可 grep 可判级。
logger = logging.getLogger("ftseek.settle_overnight_shadow")


def load_day(symbol: str, date_str: str):
    """返回 (open, close, high, low) 或 None"""
    p = BAR_DIR / f"{symbol.replace('.', '_')}_{date_str}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        bars = data.get("bars", []) if isinstance(data, dict) else data
    except Exception:
        return None
    if len(bars) < 30:
        return None
    o, c = bars[0][1], bars[-1][4]
    h = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    return o, c, h, lo


def _next_trading_day(d0: str, symbol: str):
    dt = datetime.strptime(d0, "%Y-%m-%d")
    for off in (1, 2, 3):
        cand = (dt + timedelta(days=off)).strftime("%Y-%m-%d")
        if load_day(symbol, cand):
            return cand
    return None


def run_check(scheduler=None) -> int:
    """调度器钩子 (AFTER_HOURS): 结算隔夜 shadow. 返回新结算笔数.

    2026-09-10 (DEB-20260910 §2): 补配置门 playbook.overnight_hold_settler_enabled (默认 false)。
    此前本钩子是 AFTER_HOURS 段 12 个 in-process 钩子里**唯一无任何门**的一个 ⇒ 写入方
    (overnight_hold_shadow_enabled=false) 已停, 结算器却仍每 ~7m47s 重算重印 74 笔全表, 一晚 32 轮。

    配置读取失败时**fail-closed (跳过)**, 与另 10 个钩子的 `except: pass`(fail-open 继续跑) 相反,
    理由: ①本门默认 false = "本就不该跑", fail-open 会让一次 ConfigContainer 抖动把已停的作业复活;
    ②跳过零代价 —— 结算是**全量幂等重算** (从 jsonl 读全部 settled=False 行), 漏一夜下夜自动补齐,
    不像"当日一次"型作业漏了就永久丢数据。fail-closed 必须吼一声 (WARNING), 不得静默。
    """
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.overnight_hold_settler_enabled", False)):
            return 0
    except Exception as e:
        logger.warning("[隔夜shadow] 配置门读取失败 → fail-closed 跳过本轮 (结算全量幂等, 下轮自动补): %s",
                       e, exc_info=True)
        return 0
    try:
        n = _settle_all()
        if n:
            logger.info("[隔夜shadow] 结算 %d 笔", n)
        return n
    except Exception as e:
        logger.warning("[隔夜shadow] 结算异常 (已隔离): %s", e, exc_info=True)
        return 0


def main():
    # 独立 CLI 运行需自配 handler (同 ah_exit_shadow.py:452), 否则 print→logger 后输出静默变空。
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    _settle_all(dry_run=args.dry_run)


def _settle_all(dry_run: bool = False) -> int:
    if not SHADOW.exists():
        return 0

    rows = [json.loads(l) for l in SHADOW.read_text().splitlines() if l.strip()]
    pending = [r for r in rows if not r.get("settled")]

    settled_ct = 0
    for r in pending:
        d0 = r.get("trade_date")
        sym = r.get("symbol")
        if not d0 or not sym:
            continue
        d1 = _next_trading_day(d0, sym)
        if not d1:
            continue  # 次日 bar 未采集, 留待下次
        day0 = load_day(sym, d0)
        day1 = load_day(sym, d1)
        if not day0 or not day1:
            continue
        close0 = day0[1]
        open1, close1, high1, low1 = day1
        sign = 1 if r.get("direction") == "LONG" else -1
        notional = r.get("notional") or 5000.0
        r["next_trade_date"] = d1
        r["gap_pct"] = round(sign * (open1 / close0 - 1) * 100, 4)
        r["fullday_pct"] = round(sign * (close1 / close0 - 1) * 100, 4)
        r["best_pct"] = round(sign * ((high1 if sign == 1 else low1) / close0 - 1) * 100, 4)
        r["worst_pct"] = round(sign * ((low1 if sign == 1 else high1) / close0 - 1) * 100, 4)
        r["cf_gap_pnl"] = round(r["gap_pct"] / 100 * notional, 2)
        r["cf_fullday_pnl"] = round(r["fullday_pct"] / 100 * notional, 2)
        # 2026-08-10 (Qoder 复核建议): from-exit 口径 — 对盘中 benign 退出 (如 11:00 的
        #   SL_BREAKEVEN), close0 基准丢弃了退出点→收盘的盘中路径. 补 exit_price 基准,
        #   两口径并存: close0 回答"EOD平 vs 持夜", exit 回答"退出点不平 vs 持夜".
        _exit_px = float(r.get("exit_price") or 0)
        if _exit_px > 0:
            r["gap_from_exit_pct"] = round(sign * (open1 / _exit_px - 1) * 100, 4)
            r["fullday_from_exit_pct"] = round(sign * (close1 / _exit_px - 1) * 100, 4)
            r["cf_gap_from_exit_pnl"] = round(r["gap_from_exit_pct"] / 100 * notional, 2)
            r["cf_fullday_from_exit_pnl"] = round(r["fullday_from_exit_pct"] / 100 * notional, 2)
        r["settled"] = True
        settled_ct += 1

    # 写回 (dry-run 不落盘)
    if not dry_run and settled_ct:
        with open(SHADOW, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 汇总
    done = [r for r in rows if r.get("settled")]
    n = len(done)
    if n == 0:
        return settled_ct
    gap_win = sum(1 for r in done if r["gap_pct"] > 0)
    full_win = sum(1 for r in done if r["fullday_pct"] > 0)
    gap_pnl = sum(r["cf_gap_pnl"] for r in done)
    full_pnl = sum(r["cf_fullday_pnl"] for r in done)
    summary = {
        "updated_at": datetime.now().isoformat(),
        "n_settled": n,
        "gap": {"win_rate": round(gap_win / n * 100, 1), "avg_pct": round(sum(r["gap_pct"] for r in done) / n, 3), "total_pnl": round(gap_pnl, 2)},
        "fullday": {"win_rate": round(full_win / n * 100, 1), "avg_pct": round(sum(r["fullday_pct"] for r in done) / n, 3), "total_pnl": round(full_pnl, 2)},
    }
    # 2026-08-10 (Qoder): from-exit 口径汇总 (仅含已算该口径的行)
    _fx = [r for r in done if "cf_fullday_from_exit_pnl" in r]
    if _fx:
        summary["fullday_from_exit"] = {
            "n": len(_fx),
            "win_rate": round(sum(1 for r in _fx if r["fullday_from_exit_pct"] > 0) / len(_fx) * 100, 1),
            "total_pnl": round(sum(r["cf_fullday_from_exit_pnl"] for r in _fx), 2),
        }
    # 按标的 — 主口径 from-exit (隔夜裁决的正确基准: "退出点不平 vs 持夜");
    #   无 from-exit 数据的行 (exit_price 缺失) 退回 close0 口径.
    def _sym_pnl(x):
        return x.get("cf_fullday_from_exit_pnl", x["cf_fullday_pnl"])

    def _sym_win(x):
        return (x["fullday_from_exit_pct"] if "fullday_from_exit_pct" in x else x["fullday_pct"]) > 0

    by_sym = defaultdict(list)
    for r in done:
        by_sym[r["symbol"]].append(r)
    summary["by_symbol"] = {
        s: {"n": len(v), "win_rate": round(sum(1 for x in v if _sym_win(x)) / len(v) * 100, 1),
            "total_pnl": round(sum(_sym_pnl(x) for x in v), 2),
            "caliber": "from_exit" if all("cf_fullday_from_exit_pnl" in x for x in v) else "mixed"}
        for s, v in sorted(by_sym.items(), key=lambda kv: -sum(_sym_pnl(x) for x in kv[1]))
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    _rpt = [
        f"=== 隔夜持仓反事实 (已结算 {n} 笔) ===",
        f"隔夜跳空: 胜率 {summary['gap']['win_rate']}% 均 {summary['gap']['avg_pct']:+.2f}% 金额 {gap_pnl:+.0f}",
        f"持到次日收盘: 胜率 {summary['fullday']['win_rate']}% 均 {summary['fullday']['avg_pct']:+.2f}% 金额 {full_pnl:+.0f}",
        "按标的 (n≥2):",
    ]
    _rpt += [f"  {s:8} n={v['n']:2} 胜率{v['win_rate']:5.0f}% 金额{v['total_pnl']:+8.0f}"
             for s, v in summary["by_symbol"].items() if v["n"] >= 2]
    _rpt.append(f"本次新结算 {settled_ct} 笔, 汇总已写 {OUT.name}")
    logger.info("[隔夜shadow] %s", "\n".join(_rpt))
    return settled_ct


if __name__ == "__main__":
    main()
