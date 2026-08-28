#!/usr/bin/env python
"""R-11 修复: crontab 盘后定点任务的 DST 年round 校准 (2026-08-23, DEB-20260823).

问题
----
crontab 跑在宿主本地时区 Asia/Shanghai (CST, **无 DST**)，而美股是 ET (**有 DST**)：

    EDT (夏, 3月中-11月初): CST - 12h = ET
    EST (冬, 11月初-3月中): CST - 13h = ET

于是所有"注释写 ET 时刻"的条目在 **2026-11-01 起偏 1 小时**。
落在 CST 04:xx 的**盘后**定点任务尤其危险：

    CST 04:xx → EDT: ET 16:xx (盘后 ✅)
    CST 04:xx → EST: ET 15:xx (**盘中** ❌)

即这些"盘后"任务在冬令时会在**盘中**运行：对账把在途单当不一致、
影子臂读未就绪的 replay_bars、盘中做"盘后"复盘。

修法
----
把它们整体后移到 CST 05:1x —— 两季都安全，且**一次性修完不需换季维护**：

    CST 05:1x → EDT: ET 17:1x (盘后 ✅)
    CST 05:1x → EST: ET 16:1x (盘后 ✅)

原相对执行顺序完整保留（reconcile → path_collector → e2_momentum → backfill 05:30）。

不动的条目
----------
* `*/30 0-4`、`*/5 0-4`、`*/15 0-5` 等**区间型盘中监控** —— CST 0-4 = ET 12:00-16:00
  正是盘中，本来就该在盘中跑，后移反而破坏语义。
* `0 4 * * *` disk_cleanup —— 基础设施任务，与市场无关。
* 已在 CST 05:xx 的条目 —— EDT→ET 17:xx / EST→ET 16:xx，两季均安全。

用法
----
    ./venv/bin/python scripts/fix_crontab_dst_r11.py           # 预演
    ./venv/bin/python scripts/fix_crontab_dst_r11.py --apply   # 实际写入

幂等：已改过则检测到后跳过，可重复执行。写入前自动备份。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups_r9_r13"

# (脚本特征, 原 "分 时", 新 "分 时", 说明)
MOVES = [
    ("live_reconcile_check.py",     "5 4",  "10 5", "券商↔账本对账"),
    ("position_path_collector.py",  "35 4", "15 5", "持仓路径湖采集"),
    ("e2_momentum_shadow_daily.py", "45 4", "20 5", "E2 动量影子臂"),
]


def et_of(cst_hour: int, cst_min: int, offset: int) -> str:
    h = (cst_hour - offset) % 24
    return f"{h:02d}:{cst_min:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入 (缺省仅预演)")
    args = ap.parse_args()

    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if cur.returncode != 0:
        print("❌ 读取 crontab 失败:", cur.stderr.strip())
        return 1
    lines = cur.stdout.splitlines()

    changes, already, missing = [], [], []

    for script, old_mh, new_mh, desc in MOVES:
        old_m, old_h = old_mh.split()
        new_m, new_h = new_mh.split()
        hit_old = hit_new = None
        for i, ln in enumerate(lines):
            if script not in ln or ln.lstrip().startswith("#"):
                continue
            m = re.match(r"^(\S+)\s+(\S+)\s+(\S+\s+\S+\s+\S+)\s+(.*)$", ln.strip())
            if not m:
                continue
            mi, h, rest, cmd = m.groups()
            if (mi, h) == (old_m, old_h):
                hit_old = (i, mi, h, rest, cmd)
            elif (mi, h) == (new_m, new_h):
                hit_new = (i, mi, h, rest, cmd)
        if hit_new and not hit_old:
            already.append((script, new_mh, desc))
        elif hit_old:
            changes.append((script, hit_old, new_m, new_h, desc))
        else:
            missing.append((script, old_mh, desc))

    print(f"{'脚本':<32} {'CST':<8} {'EDT→ET':<9} {'EST→ET':<9} 判定")
    print("-" * 82)
    for script, hit, new_m, new_h, desc in changes:
        _, mi, h, _, _ = hit
        print(f"{script:<32} {h+':'+mi.zfill(2):<8} "
              f"{et_of(int(h),int(mi),12):<9} {et_of(int(h),int(mi),13):<9} ❌ EST 落盘中")
        print(f"{'  → 改为':<32} {new_h+':'+new_m:<8} "
              f"{et_of(int(new_h),int(new_m),12):<9} {et_of(int(new_h),int(new_m),13):<9} ✅ 两季盘后")
    for script, mh, desc in already:
        print(f"{script:<32} {mh:<8} {'':<9} {'':<9} ✅ 已改过 (幂等)")
    for script, mh, desc in missing:
        print(f"{script:<32} {mh:<8} {'':<9} {'':<9} ⚠️ 未找到, 跳过")

    print("-" * 82)
    print(f"待改 {len(changes)} 条 / 已改 {len(already)} 条 / 未找到 {len(missing)} 条")

    if not changes:
        print("✅ 无需改动")
        return 0

    if not args.apply:
        print("\n预演模式 — 加 --apply 实际写入")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"crontab.pre_r11.{stamp}.txt"
    backup.write_text(cur.stdout, encoding="utf-8")
    print(f"\n已备份 → {backup}")

    for script, hit, new_m, new_h, desc in changes:
        i, mi, h, rest, cmd = hit
        lines[i] = f"{new_m} {new_h} {rest} {cmd}"
        # 顺带修正上一行注释里写死的 "CST 04:45 = ET 16:45" 之类断言
        if i > 0 and lines[i - 1].lstrip().startswith("#"):
            lines[i - 1] = re.sub(
                r"CST\s*\d{1,2}:\d{2}\s*=\s*ET\s*\d{1,2}:\d{2}",
                f"CST {new_h}:{new_m} (EDT→ET {et_of(int(new_h),int(new_m),12)} / "
                f"EST→ET {et_of(int(new_h),int(new_m),13)}, R-11 两季均盘后)",
                lines[i - 1],
            )

    new_cron = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".cron", delete=False,
                                     encoding="utf-8") as f:
        f.write(new_cron)
        tmp = f.name
    res = subprocess.run(["crontab", tmp], capture_output=True, text=True)
    if res.returncode != 0:
        print("❌ crontab 安装失败:", res.stderr.strip())
        print(f"   原内容仍在 {backup}")
        return 1

    print(f"✅ 已写入 {len(changes)} 条")
    verify = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    print("\n复核:")
    for script, _hit, new_m, new_h, _desc in changes:
        for ln in verify.splitlines():
            if script in ln and not ln.lstrip().startswith("#"):
                print(f"  {ln[:110]}")
    print(f"\n回滚: crontab {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
