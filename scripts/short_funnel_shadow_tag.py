#!/usr/bin/env python3
"""空头转化漏斗影子打标 (2026-08-16, record-only, 零执行路径改动).

背景 (8/16 策略评价, 用户裁"真正有空头产出是最核心问题"):
  SELL_SHORT指令 8/7后断崖 (gate前62笔/3.5周 → 8/7后仅2笔)。逐层排查纠正两个误诊后
  定位真实断点 = **LLM分析层转化率低**: 8/15轮35只深度分析 → 看多18/观望16/看空仅1。
  (扫描层top_bearish已按conviction合并进池 pipeline.py L243; conf gate 8/9已放宽70→55。)

本脚本盘后解析当日DA stderr日志 (logs/da_pipeline_stderr.log[-YYYYMMDD]):
  1. "深度分析 N/M: US.XXX" + "评级: X (Y/10)" 配对 → 每标的评级分布
  2. 与 playbook_instructions 当日方向对照 → 空头转化漏斗:
     analyzed_n → rated_bearish → short_instructions → executed
  3. 同轴转化链 (2026-09-26 RFC-109②-A 修轴): 按 (trade_date, symbol) JOIN ⇒
     rated_bearish → bear_with_short → bear_short_exec, 并分列「非当日看空评级」的旁路空头。
     原 3.「三问gate痕迹」已删: 正则 `三问|逼空|催化剂时效` 在同期 1853 条 skip_reason 命中 0
     ⇒ 门恒不触发 (生产文案里没这三个词, 代码中的「三问」全指用户三问 Q1/Q2/Q3 决策留痕)。
     死测量替换为真实拦截原因分布 bearish_no_short_reasons。
  落 data/short_funnel_shadow.jsonl, 按 (date) 幂等。

注意 rotation 日期陷阱: 文件名 .log-YYYYMMDD 装的是前一日那轮 (DA 19:00 CST 盘前跑)。
挂载: AFTER_HOURS (scheduler_core), 配置键 short_funnel_shadow_enabled。
回滚: 配置键 false + 重启。
"""
import json, os, re, sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "short_funnel_shadow.jsonl"
LOG_DIR = ROOT / "logs"

# 2026-09-10 (可观测性批): print → logger。本脚本经 scheduler._load_script_module 在服务
#   进程内执行, print 无时间戳/级别/来源。命名沿用 in-process 脚本约定 ftseek.<name>;
#   消息正文保持逐字不变, 以免打断既有 grep 口径。
logger = logging.getLogger("ftseek.short_funnel_shadow")


def run_check(scheduler=None):
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.short_funnel_shadow_enabled", True)):
            return
    except Exception:
        pass
    main()


def _parse_da_log(path: Path):
    """解析DA stderr日志 → ({symbol: (rating, score)}, consistency计数)"""
    ratings = {}
    cur_sym = None
    consistency_flagged = []  # [ScoreConsistency] 行 (DEB-20260816 §9.4, R7补缺口)
    for ln in open(path, errors="ignore"):
        m = re.search(r"深度分析 \d+/\d+: (US\.\w+)", ln)
        if m:
            cur_sym = m.group(1)
            continue
        m2 = re.search(r"评级: (\S+) \(([\d.]+)/10\)", ln)
        if m2 and cur_sym:
            ratings[cur_sym] = (m2.group(1), float(m2.group(2)))
            cur_sym = None
            continue
        m3 = re.search(r"\[ScoreConsistency\] ⚠️ (US\.\w+): (.+)", ln)
        if m3:
            consistency_flagged.append({"symbol": m3.group(1), "issues": m3.group(2).strip()})
    return ratings, consistency_flagged


def main():
    conn = sqlite3.connect(str(DB), timeout=15)
    conn.row_factory = sqlite3.Row

    existing_dates = set()
    if OUT.exists():
        for ln in OUT.read_text().splitlines():
            if ln.strip():
                try:
                    existing_dates.add(json.loads(ln).get("date"))
                except Exception:
                    pass

    added = 0
    insufficient = []  # 2026-10-16: 输入链异常日 (有日志但 <3 评级) 计数透出
    # 找最近7个日志文件 (含rotation)
    log_files = sorted(LOG_DIR.glob("da_pipeline_stderr.log*"))
    for lf in log_files:
        if lf.suffix == ".gz":
            continue
        # 推断交易日: rotation文件的日期部分 = 次日 (前一日那轮)
        m = re.search(r"log-(\d{8})$", lf.name)
        if m:
            d_file = datetime.strptime(m.group(1), "%Y%m%d")
            trade_date = (d_file - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        if trade_date in existing_dates:
            continue

        ratings, consistency_flagged = _parse_da_log(lf)
        if len(ratings) < 3:
            # 2026-10-16 (推送面巡检 P0-2): 原静默 continue ⇒ carrier 接管后 stderr 不落盘
            # (10-08 起) 整整 8 天零采集无任何告警, 看板还把停摆归因错指写方。
            # 有日志文件但解析 <3 条 = 输入链异常, 必须留痕可见。
            insufficient.append((trade_date, lf.name, len(ratings)))
            logger.warning("[short_funnel_shadow] %s 日志 %s 仅解析出 %d 条评级 (<3) "
                           "⇒ 当日跳过 — 查 DA stderr 采集链 (da_main_carrier 落盘)",
                           trade_date, lf.name, len(ratings))
            continue

        # 评级分布
        from collections import Counter
        rc = Counter(r for r, s in ratings.values())
        bearish_syms = [sym for sym, (r, s) in ratings.items() if r in ("看空", "强烈看空")]

        # 当日指令对照
        insts = conn.execute(
            "SELECT symbol, direction, executed, skip_reason FROM playbook_instructions "
            "WHERE substr(created_at,1,10) = ?", (trade_date,)).fetchall()
        # 2026-09-26 (RFC-109②-A 修轴): inst_by_sym 原本建了却从未被使用 ⇒ 「看空评级→空头指令」
        # 这条链**答不出来**, 因为 rated_bearish_n(评级看空的股票数) 与 short_instructions(当日全部
        # SELL_SHORT 指令数) 不同分母, 相除无意义 (实测 108/67=161% 的假「转化率」就是这么来的)。
        # 补上按 symbol 的同轴 JOIN — 这才是判据「n≥10 交易日再裁分层 gate」要问的东西。
        inst_by_sym = {}
        for i in insts:
            inst_by_sym.setdefault(i["symbol"], []).append(dict(i))

        short_insts = [i for i in insts if i["direction"] == "SELL_SHORT"]
        short_syms = {i["symbol"] for i in short_insts}
        bear_set = set(bearish_syms)
        # 同轴转化链: 看空评级 ∩ 当日出了空头指令 ∩ 真成交
        bear_with_short = sorted(bear_set & short_syms)
        bear_short_exec = sorted(s for s in bear_with_short
                                 if any(i["executed"] for i in inst_by_sym[s]
                                        if i["direction"] == "SELL_SHORT"))
        # 旁路: 空头指令但当日 DA 未评看空 (扫描层 top_bearish 按 conviction 合并进池, pipeline L243)
        short_from_non_bearish = sorted(short_syms - bear_set)
        # 看空评级却没出空头指令 → 真实拦截原因 (取代下面的死列)
        bearish_no_short_reasons = Counter()
        for s in sorted(bear_set - short_syms):
            others = inst_by_sym.get(s) or []
            if not others:
                bearish_no_short_reasons["(当日无任何指令)"] += 1
            for i in others:
                bearish_no_short_reasons[
                    f"{i['direction']}: {(i['skip_reason'] or '(空)')[:60]}"] += 1

        # 2026-09-26 (RFC-109②-A 删死列): 原 `gate_block_hints` 用正则 `三问|逼空|催化剂时效`
        # 去数「被三问门拦掉的空头」, 实测 **30 天恒为 0** — 在同期 1853 条非空 skip_reason 里
        # 搜这三个词命中 0 条。根因: 生产 skip_reason 文案里根本没有这三个词 (代码中的「三问」全指
        # 用户三问 Q1/Q2/Q3 的决策留痕, 不是一个门) ⇒ 门恒不触发 = 死测量, 占看板一格却答不了问题。
        # 按「死门三路径全中→删门非加守卫」删除, 并连带清理下游 (看板段②、词典 conclusion、
        # 注册表 description); 替换为真实拦截原因分布 bearish_no_short_reasons。

        rec = {
            "date": trade_date,
            "log_file": lf.name,
            "analyzed_n": len(ratings),
            "rating_dist": dict(rc),
            "rated_bearish_n": len(bearish_syms),
            "rated_bearish_syms": bearish_syms[:10],
            "short_instructions": len(short_insts),
            "short_executed": sum(1 for i in short_insts if i["executed"]),
            "total_instructions": len(insts),
            # ── 同轴转化链 (2026-09-26 RFC-109②-A 新增; 历史行无这些键 ⇒ 消费方一律 .get()) ──
            "bear_with_short_n": len(bear_with_short),
            "bear_with_short_syms": bear_with_short[:10],
            "bear_short_exec_n": len(bear_short_exec),
            "short_from_non_bearish_n": len(short_from_non_bearish),
            "bearish_no_short_reasons": dict(bearish_no_short_reasons.most_common(8)),
            # R7 (DEB-20260816 §10.3): [ScoreConsistency] 矛盾率判据的聚合数据
            "consistency_flagged_n": len(consistency_flagged),
            "consistency_flagged_syms": [c["symbol"] for c in consistency_flagged][:10],
            "consistency_total_n": len(ratings),
        }
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        existing_dates.add(trade_date)
        added += 1

    logger.info("[short_funnel_shadow] 新增 %d 日 → %s (输入链异常 %d 日: %s)",
                added, OUT, len(insufficient),
                ",".join(d for d, _, _ in insufficient[-5:]) or "无")
    conn.close()


if __name__ == "__main__":
    # 独立 CLI 运行需自配 handler (同 ah_exit_shadow.py:452), 否则 print→logger 后输出静默变空。
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    main()
