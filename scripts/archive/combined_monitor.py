#!/usr/bin/env python3
"""
合并监控脚本 — lifecycle_monitor + silent_failure_check (2026-07-10)

根因: 两个脚本各自启动独立 Python 进程, 每个 ~60MB RSS.
在 3.5GB/2核机器上, 交易时段每 10 分钟同时触发 = 峰值 +120MB.
合并为单进程顺序执行, 峰值 -60MB.

用法:
  python scripts/combined_monitor.py

cron (替代原来的两条):
  */10 21-23 * * 1-5 cd /opt/ftseek/FTSeek-US && /opt/ftseek/FTSeek-US/venv/bin/python3 scripts/combined_monitor.py >> /opt/ftseek/FTSeek-US/data/combined_monitor.log 2>&1
  */10 0-4 * * 2-6 cd /opt/ftseek/FTSeek-US && /opt/ftseek/FTSeek-US/venv/bin/python3 scripts/combined_monitor.py >> /opt/ftseek/FTSeek-US/data/combined_monitor.log 2>&1
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    # 1. silent_failure_check (日志扫描, 无外部依赖, 极快)
    try:
        from scripts.silent_failure_check import main as _silent_check
        _silent_check()
    except SystemExit as _se:
        # silent_failure_check 用 sys.exit(0/1) 表示状态, 不影响后续
        if _se.code != 0:
            # 有告警时已经打印, 继续执行 lifecycle
            pass
    except Exception as _e:
        print(f"[combined] silent_failure_check 异常: {_e}", file=sys.stderr)

    # 2. lifecycle_monitor (需要 DB 查询 + 飞书推送, 稍重)
    try:
        from scripts.lifecycle_monitor import main as _lifecycle
        _lifecycle()
    except SystemExit:
        pass
    except Exception as _e:
        print(f"[combined] lifecycle_monitor 异常: {_e}", file=sys.stderr)


if __name__ == "__main__":
    main()
