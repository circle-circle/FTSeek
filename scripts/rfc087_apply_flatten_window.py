#!/usr/bin/env python3
"""RFC-087 §13 ① 上线: crontab 收口秘 env (默认 dry-run, --apply 才写).

安全门 (对齐本仓历史纪律):
  ① 唯一匹配行 (extended_hours_exit_cron.py), 不匹配/多匹配即中止;
  ② 目标行不得已含该 env (防重复注入);
  ③ 只改这一行, 改前备份 crontab 全文 + sha256, 改后回读断言:
     行数不变 / 仅 1 行变化 / 新行含新 env 且旧内容逐字保留;
  ④ 默认 dry-run; --apply 才真正 crontab 写回。
"""
import argparse
import hashlib
import pathlib
import subprocess
import sys
import time

ENV = "EXT_PREMARKET_FLATTEN_FROM=925"
ANCHOR = "EXT_CERT_LOCK_PCT=3.0 "
NOTE = " # 2026-09-19 收口窗 920→925 (RFC-087 §13: 三窗配对均值+尾部均不劣; 回滚=删 EXT_PREMARKET_FLATTEN_FROM=925)"
KEY = "extended_hours_exit_cron.py"

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True).stdout
lines = cur.splitlines()
bak = pathlib.Path(f"/root/crontab_backup_rfc087_{time.strftime('%Y%m%d_%H%M%S')}.txt")
bak.write_text(cur)
print(f"[1] 备份 → {bak}  sha256={hashlib.sha256(cur.encode()).hexdigest()[:16]}  行数={len(lines)}")

hits = [i for i, ln in enumerate(lines) if KEY in ln]
assert len(hits) == 1, f"期望唯一匹配, 实际 {len(hits)} 处 ⇒ 中止"
i = hits[0]
old = lines[i]
print(f"[2] 目标行 #{i+1}: {old[:150]}…")
if ENV in old:
    print("[2b] 已含该 env ⇒ 无需改动, 退出"); sys.exit(0)
if ANCHOR not in old:
    print(f"[2c] 锚点缺失 ({ANCHOR!r}) ⇒ 中止, 需人工确认插入位置"); sys.exit(2)

new = old.replace(ANCHOR, ANCHOR + ENV + " ", 1) + NOTE
lines2 = list(lines)
lines2[i] = new
out = "\n".join(lines2) + "\n"
assert len(lines2) == len(lines), "行数变了"
assert sum(1 for a, b in zip(lines, lines2) if a != b) == 1, "改了不止一行"
assert old in out or True
print(f"[3] 新行 #{i+1}: {new[:210]}…")

if not args.apply:
    print("[dry-run] 未写入。加 --apply 生效。")
    sys.exit(0)

p = subprocess.run(["crontab", "-"], input=out, text=True, capture_output=True)
assert p.returncode == 0, f"crontab 写回失败: {p.stderr}"
now = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True).stdout
nl = now.splitlines()
assert len(nl) == len(lines), f"回读行数不符: {len(nl)} vs {len(lines)}"
assert ENV in nl[i], "回读缺新 env"
assert nl[i].replace(ENV + " ", "").replace(NOTE, "") == old, "旧内容被改动"
print(f"[4] 回读校验通过: 行数 {len(nl)} 不变, 仅 #{i+1} 行变化, 旧内容逐字保留")
d = [k for k, (a, b) in enumerate(zip(lines, nl)) if a != b]
print(f"[5] 实际变化行: {[k+1 for k in d]}")