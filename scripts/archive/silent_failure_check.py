#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 静默失败检测 (Silent Failure Detector)
================================================
扫描 logs/ftseek_us.log 最近 30 分钟内的告警级日志，识别"系统性静默失败"。

背景 Bug: Futu K线返回过期数据 → 代码静默 return None → 回退 TwelveData → 429 限流
→ 再次 return None。整条数据链断裂但无 ERROR，仅 WARNING，无人感知。

检测策略 (阈值化，避免单次抖动误报):
  1. '所有数据源均失败'          → 出现即告警 (终态失败，无兜底)
  2. '富途历史K线过期'           → >3 次/30min 视为系统性
  3. '富途历史K线过期'           → >3 个不同 symbol/30min 视为 Futu 整体故障
  4. '429 限流'                  → >5 次/30min 视为 TwelveData 系统性限流
  5. get_daily_kline.*None /
     get_daily_bars.*None        → 数据管线静默返回 None (出现即记录)

输出: 多行告警信息，最后一行固定为 '🚨 SILENT_FAILURE_DETECTED' (便于 cron/脚本捕获)。
退出码: 1 = 需要告警; 0 = 正常 (cron 据此决定是否推送)。

# Crontab: */10 21-23 * * 1-5 and */10 0-4 * * 2-6
#   (美股盘前21点至次日4点，每10分钟运行一次；周一至周五晚 + 周二至周六凌晨)
# 安装: crontab -e
#   */10 21-23 * * 1-5 /opt/ftseek/FTSeek-US/scripts/silent_failure_check.py
#   */10 0-4  * * 2-6 /opt/ftseek/FTSeek-US/scripts/silent_failure_check.py
"""

import os
import re
import sys
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
LOG_FILE = "/opt/ftseek/FTSeek-US/logs/ftseek_us.log"
WINDOW_MINUTES = 30

# 阈值 (30 分钟窗口)
THRESH_FUTU_STALE_COUNT = 3      # 富途历史K线过期 >3 次即系统性
THRESH_FUTU_STALE_SYMBOLS = 3    # 富途历史K线过期 影响 >3 个不同 symbol 即整体故障
THRESH_TWELVEDATA_429 = 5        # 429 限流 >5 次即系统性

# 日志行时间戳正则: '2026-06-30 03:17:03,637 [logger] LEVEL: ...'
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Futu 过期行中提取 symbol: '[FutuLV3·DailyK] US.TSLA 富途历史K线过期'
FUTU_SYMBOL_RE = re.compile(r"\[FutuLV3·DailyK\]\s+(US\.[A-Za-z0-9.#\-]+)\s+富途历史K线过期")

# 所有数据源均失败行中提取 symbol，兼容三种日志格式:
#   '[Router] get_daily_bars US.US.BA: 所有数据源均失败'
#   '[FutuLV3] ❌ US.UNH#SIM 所有数据源均失败'
#   '[Router] US.UNH#SIM 所有数据源均失败:'
# symbol 可能带 #SIM 模拟后缀，用宽松字符集
ALL_FAIL_SYMBOL_RE = re.compile(
    r"(?:get_daily_bars\s+|❌\s+|\[Router\]\s+)(US\.[A-Za-z0-9.#\-]+)"
)


def parse_ts(line):
    """从日志行提取时间戳，失败返回 None。"""
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def collect_recent(log_path, cutoff):
    """
    读取日志文件，返回 [(line, ts)] 列表，仅保留 ts >= cutoff 的行。
    日志可能很大，从末尾反向扫描提高效率；但为保持简单用正向读取 + 过滤。
    """
    if not os.path.exists(log_path):
        return []

    recent = []
    # 用 latin-1 避免偶发非 UTF-8 字节导致解码失败中断扫描
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "富途历史K线过期" not in line \
                        and "所有数据源均失败" not in line \
                        and "429 限流" not in line \
                        and "get_daily_kline" not in line \
                        and "get_daily_bars" not in line:
                    continue  # 快速跳过无关行，避免逐行解析时间戳
                ts = parse_ts(line)
                if ts is None:
                    continue
                if ts >= cutoff:
                    recent.append((line.rstrip("\n"), ts))
    except OSError:
        return []
    return recent


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = datetime.now() - timedelta(minutes=WINDOW_MINUTES)

    recent = collect_recent(LOG_FILE, cutoff)

    # ---- 分类统计 ----
    all_fail_lines = []          # '所有数据源均失败'
    futu_stale_lines = []        # '富途历史K线过期'
    futu_stale_symbols = set()
    twelvedata_429_lines = []    # '429 限流'
    none_return_lines = []       # get_daily_kline.*None / get_daily_bars.*None

    for line, _ts in recent:
        if "所有数据源均失败" in line:
            all_fail_lines.append(line)
        if "富途历史K线过期" in line:
            futu_stale_lines.append(line)
            sm = FUTU_SYMBOL_RE.search(line)
            if sm:
                futu_stale_symbols.add(sm.group(1))
        if "429 限流" in line:
            twelvedata_429_lines.append(line)
        # 静默 None 返回: 优先匹配显式日志，兜底匹配代码上下文残留
        if re.search(r"get_daily_kline.*None", line) or re.search(r"get_daily_bars.*None", line):
            none_return_lines.append(line)
        # Futu 过期行的 '→ 返回None触发TwelveData fallback' 同属静默 None
        if "返回None" in line or "返回 None" in line:
            if line not in none_return_lines:
                none_return_lines.append(line)

    # ---- 触发判定 ----
    alerts = []

    # 1) 所有数据源均失败 (终态失败，出现即告警)
    if all_fail_lines:
        fail_symbols = set()
        for ln in all_fail_lines:
            sm = ALL_FAIL_SYMBOL_RE.search(ln)
            if sm:
                fail_symbols.add(sm.group(1))
        sym_detail = ", ".join(sorted(fail_symbols)) if fail_symbols else "(symbol 未提取)"
        alerts.append(
            f"[1/终态失败] '所有数据源均失败' ×{len(all_fail_lines)} "
            f"({len(fail_symbols)} symbols: {sym_detail})。"
            f"这是数据链完全断裂的终态：Futu→TwelveData 均失败，return None 无兜底。"
        )

    # 2) 富途历史K线过期 - 次数系统性 (>3 次)
    if len(futu_stale_lines) > THRESH_FUTU_STALE_COUNT:
        alerts.append(
            f"[2/Futu过期频次] '富途历史K线过期' ×{len(futu_stale_lines)} "
            f">(阈值 {THRESH_FUTU_STALE_COUNT})/30min。"
            f"非单次抖动，Futu 历史 K 线通道系统性失效。"
        )

    # 3) 富途历史K线过期 - 多 symbol (>3 个不同 symbol)
    if len(futu_stale_symbols) > THRESH_FUTU_STALE_SYMBOLS:
        alerts.append(
            f"[3/Futu多symbol] '富途历史K线过期' 影响 {len(futu_stale_symbols)} 个不同标的 "
            f"({', '.join(sorted(futu_stale_symbols))}) >{THRESH_FUTU_STALE_SYMBOLS}/30min。"
            f"非单股问题，Futu 整体历史数据源故障。"
        )

    # 4) TwelveData 429 限流系统性 (>5 次)
    if len(twelvedata_429_lines) > THRESH_TWELVEDATA_429:
        alerts.append(
            f"[4/TwelveData限流] '429 限流' ×{len(twelvedata_429_lines)} "
            f">(阈值 {THRESH_TWELVEDATA_429})/30min。"
            f"Futu 失效后大量回退 TwelveData 触发持续限流，fallback 链失效。"
        )

    # 5) 静默 return None
    if none_return_lines:
        alerts.append(
            f"[5/静默None] 数据管线 return None ×{len(none_return_lines)}/30min "
            f"(get_daily_kline/get_daily_bars/返回None)。"
            f"代码静默吞错，无 ERROR 抛出 → 外层无感知 → 盲持风险。"
        )

    # ---- 输出 ----
    if not alerts:
        # 正常退出，打印简明状态便于 cron 日志留痕
        print(f"[{now}] OK: no systematic silent failure in last {WINDOW_MINUTES}min "
              f"(futu_stale={len(futu_stale_lines)}, all_fail={len(all_fail_lines)}, "
              f"td429={len(twelvedata_429_lines)}, none_ret={len(none_return_lines)})")
        sys.exit(0)

    # 告警输出
    print(f"=" * 70)
    print(f"🚨 FTSeek-US SILENT FAILURE ALERT")
    print(f"  检测时间: {now}")
    print(f"  日志窗口: 最近 {WINDOW_MINUTES} 分钟")
    print(f"  日志文件: {LOG_FILE}")
    print(f"  告警条数: {len(alerts)}")
    print(f"-" * 70)
    for i, a in enumerate(alerts, 1):
        print(f"  [{i}] {a}")
    print(f"-" * 70)
    print("  建议动作:")
    print("    1) 检查 Futu OpenD 连接状态与历史 K 线权限")
    print("    2) 确认 TwelveData API 额度/配额是否耗尽")
    print("    3) 核实受影响 symbol 的最新成交价是否可用 (避免盲持)")
    print("    4) 必要时人工暂停交易决策直至数据恢复")
    print(f"=" * 70)
    # 最后一行固定标记，便于上游 cron/脚本 grep 捕获
    print("🚨 SILENT_FAILURE_DETECTED")
    sys.exit(1)


if __name__ == "__main__":
    main()
