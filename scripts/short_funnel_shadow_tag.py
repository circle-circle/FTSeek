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
  3. 三问gate痕迹: thesis/skip_reason 含"三问/逼空/催化剂时效" → gate_block标记
  落 data/short_funnel_shadow.jsonl, 按 (date) 幂等。

注意 rotation 日期陷阱: 文件名 .log-YYYYMMDD 装的是前一日那轮 (DA 19:00 CST 盘前跑)。
挂载: AFTER_HOURS (scheduler_core), 配置键 short_funnel_shadow_enabled。
回滚: 配置键 false + 重启。
"""
import json, os, re, sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "short_funnel_shadow.jsonl"
LOG_DIR = ROOT / "logs"


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
    """解析DA stderr日志 → {symbol: (rating, score)}"""
    ratings = {}
    cur_sym = None
    for ln in open(path, errors="ignore"):
        m = re.search(r"深度分析 \d+/\d+: (US\.\w+)", ln)
        if m:
            cur_sym = m.group(1)
            continue
        m2 = re.search(r"评级: (\S+) \(([\d.]+)/10\)", ln)
        if m2 and cur_sym:
            ratings[cur_sym] = (m2.group(1), float(m2.group(2)))
            cur_sym = None
    return ratings


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

        ratings = _parse_da_log(lf)
        if len(ratings) < 3:
            continue  # 日志不完整/非DA轮

        # 评级分布
        from collections import Counter
        rc = Counter(r for r, s in ratings.values())
        bearish_syms = [sym for sym, (r, s) in ratings.items() if r in ("看空", "强烈看空")]

        # 当日指令对照
        insts = conn.execute(
            "SELECT symbol, direction, executed, skip_reason FROM playbook_instructions "
            "WHERE substr(created_at,1,10) = ?", (trade_date,)).fetchall()
        inst_by_sym = {}
        for i in insts:
            inst_by_sym.setdefault(i["symbol"], []).append(dict(i))

        short_insts = [i for i in insts if i["direction"] == "SELL_SHORT"]
        # 三问gate痕迹
        gate_blocks = 0
        for i in insts:
            sr = i["skip_reason"] or ""
            if re.search(r"三问|逼空|催化剂时效", sr):
                gate_blocks += 1

        rec = {
            "date": trade_date,
            "log_file": lf.name,
            "analyzed_n": len(ratings),
            "rating_dist": dict(rc),
            "rated_bearish_n": len(bearish_syms),
            "rated_bearish_syms": bearish_syms[:10],
            "short_instructions": len(short_insts),
            "short_executed": sum(1 for i in short_insts if i["executed"]),
            "gate_block_hints": gate_blocks,
            "total_instructions": len(insts),
        }
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        existing_dates.add(trade_date)
        added += 1

    print(f"[short_funnel_shadow] 新增 {added} 日 → {OUT}")
    conn.close()


if __name__ == "__main__":
    main()
