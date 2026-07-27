#!/usr/bin/env python3
"""启动冒烟检查 (2026-07-26, 用户批"回归验证接线") — 重启后机器断言, 替代人肉 grep。

背景: 07-26 17:37 事故 — cleanup-r3 删 yaml 段漏改校验清单, ConfigGov 整块中断
(漂移检测/热重载/审计未启动), 但 980 测试全绿 + 人工重启验证 grep "服务已启动"
都没拦住, 靠用户读日志才发现。教训: 启动判据必须固化成机器断言, 不能靠临场记忆。

七条断言 (对最近一次启动段, 以最后一个 [PreFlight] 为界):
  1. ConfigValidator 校验结果 ✅ 全部通过
  2. [ConfigGov] ✅ 漂移检测已启动
  3. [ConfigGov] ✅ 热重载已启动
  4. [ConfigAudit] ✅ SSOT 配置一致性审计通过 (0 BLOCKING)
  5. 模拟盘安全确认 (TRADE_ENV=1)
  6. [PlaybookExit] 快循环已启动
  7. 启动段零 Traceback / ImportError / ModuleNotFoundError / CRITICAL

用法 (重启 SOP, 见 AGENT.md 军规):
  systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
  --wait N : 最多轮询 N 秒 (每 5s 重读日志), 适配启动耗时; 缺省单次判定
  --push   : 失败时飞书告警 (app.notifier.feishu)
退出码: 0=全过 / 1=断言失败 / 2=日志缺失或无近期启动段
"""
import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "ftseek_us.log"

# (名称, 判定函数) — 判定输入为启动段文本
CHECKS = [
    ("ConfigValidator 全部通过", lambda s: "[ConfigValidator] 校验结果: ✅ 全部通过" in s),
    ("漂移检测已启动",           lambda s: "[ConfigGov] ✅ 漂移检测已启动" in s),
    ("热重载已启动",             lambda s: "[ConfigGov] ✅ 热重载已启动" in s),
    ("SSOT 审计 0 BLOCKING",     lambda s: "[ConfigAudit] ✅ SSOT 配置一致性审计通过" in s),
    ("模拟盘安全确认",           lambda s: "模拟盘 (TRADE_ENV=1" in s),
    ("退出快循环已启动",         lambda s: "[PlaybookExit] 快循环已启动" in s),
    ("零 Traceback/CRITICAL",    lambda s: not re.search(
        r"Traceback|ImportError|ModuleNotFoundError| CRITICAL", s)),
]

BOOT_MARKER = "[PreFlight]"          # 每次启动最早的稳定标记
BOOT_MARKER_FALLBACK = "[安全确认]"
MAX_BOOT_AGE_HOURS = 6               # 启动段太旧 = 本次重启没产生新段, 判定无效


def _extract_boot_segment(text: str):
    """取最后一次启动段; 返回 (segment, boot_time|None), 找不到返回 (None, None)"""
    idx = text.rfind(BOOT_MARKER)
    if idx < 0:
        idx = text.rfind(BOOT_MARKER_FALLBACK)
    if idx < 0:
        return None, None
    seg = text[idx:]
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text[text.rfind("\n", 0, idx) + 1:idx + 30].strip())
    boot_time = None
    if m:
        try:
            boot_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return seg, boot_time


def run_once():
    """单次判定. 返回 (exit_code, 失败清单, boot_time)"""
    if not LOG.exists():
        return 2, [f"日志文件缺失: {LOG}"], None
    text = LOG.read_text(encoding="utf-8", errors="replace")
    seg, boot_time = _extract_boot_segment(text)
    if seg is None:
        return 2, ["日志中未找到启动标记 [PreFlight]/[安全确认]"], None
    if boot_time and datetime.now() - boot_time > timedelta(hours=MAX_BOOT_AGE_HOURS):
        return 2, [f"最近启动段过旧 ({boot_time}), 本次重启可能未产生日志"], boot_time
    failed = [name for name, fn in CHECKS if not fn(seg)]
    return (1 if failed else 0), failed, boot_time


def main():
    ap = argparse.ArgumentParser(description="FTSeek-US 启动冒烟检查")
    ap.add_argument("--wait", type=int, default=0, help="最多轮询 N 秒 (每 5s), 适配启动耗时")
    ap.add_argument("--push", action="store_true", help="失败时飞书告警")
    args = ap.parse_args()

    # 2026-07-27 修复 (实战暴露): restart 后立即跑 --wait 时, 新启动段尚未写出,
    #   首轮匹配到"上一次启动段"且全绿 → 假阳性放行。--wait 模式下要求启动段
    #   不早于脚本启动前 120s, 旧段视为"新段未出"继续轮询。
    script_start = datetime.now()
    deadline = time.time() + max(args.wait, 0)
    while True:
        code, failed, boot_time = run_once()
        stale_boot = (args.wait > 0 and boot_time is not None
                      and boot_time < script_start - timedelta(seconds=120))
        if stale_boot:
            code, failed = 2, [f"启动段 ({boot_time}) 早于本次重启, 等待新段写出"]
        if (code == 0 and not stale_boot) or time.time() >= deadline:
            break
        time.sleep(5)

    print("=" * 60)
    print(f"启动冒烟检查 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (启动段: {boot_time or '未知'})")
    for name, _ in CHECKS:
        mark = "✗" if name in failed else "✓"
        print(f"  [{mark}] {name}")
    if code == 2:
        print(f"  [!] {failed[0]}")
    print(("✅ 全部通过" if code == 0 else f"❌ 失败 {len(failed)} 项") + f" (exit={code})")
    print("=" * 60)

    if code != 0 and args.push:
        try:
            from app.notifier.feishu import send_text
            send_text(f"🚨 [startup_smoke] 启动冒烟失败 (exit={code}): {', '.join(failed)}")
        except Exception as e:
            print(f"(告警推送失败: {e})")
    sys.exit(code)


if __name__ == "__main__":
    main()
