#!/usr/bin/env python3
"""死亡不可见普查 (严格判据): 哪些 crontab 环停了我没人会察觉?

判据链 (逐环):
  ① 载体形态: crontab 行是否只 `>> logs/*.log` (无 --push/无 app/push 调用/无 stdout 读者);
  ② 读者面: **可执行体** (仓 scripts/ app/ ops/, ~/.hermes/scripts/) 是否读它的日志文件名或产物;
  ③ 注册表: 是否在 app/playbook/shadow_registry.py 登记 (有哨兵 = 有读者);
  ④ DB 载体: 脚本是否写 DB 表 (D1 载体活性可兜底);
  ⑤ error_alert 覆盖: 只在 4 个时间窗内, 且要求脚本自己 logger.error。

输出四类: 执行环(必留 crontab) / 有哨兵 / 有读者 / **孤儿**(仅日志, 无人读, 无哨兵).
只读扫描。
"""
import logging
import pathlib
import re
import subprocess

REPO = pathlib.Path("/opt/ftseek/FTSeek-US")
HERMES = pathlib.Path("/root/.hermes")

_log = logging.getLogger(__name__)

cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
lines = [l for l in cron.splitlines() if l.strip() and not l.strip().startswith("#")]

# 可执行体语料
exec_blobs = {}
for d in (REPO / "scripts", REPO / "app", REPO / "ops", HERMES / "scripts", REPO / "tests"):
    for f in d.rglob("*"):
        if f.suffix in (".py", ".sh") and "__pycache__" not in str(f):
            try:
                exec_blobs[f] = f.read_text(errors="replace")
            except Exception:
                _log.warning("file read failed", exc_info=True)
registry = (REPO / "app" / "playbook" / "shadow_registry.py").read_text(errors="replace") \
    if (REPO / "app" / "playbook" / "shadow_registry.py").exists() else ""

rows = []
for l in lines:
    m = re.search(r"(?:scripts|ops/hermes)/([\w\.\-]+\.(?:py|sh))", l)
    if not m:
        continue
    name = m.group(1)
    sched = " ".join(l.split()[:5])
    push = ("--push" in l) or ("--sync" in l)
    redir = re.search(r">>\s*(\S+)", l)
    log = redir.group(1) if redir else ""
    src = REPO / "scripts" / name
    text = src.read_text(errors="replace") if src.exists() else ""

    writes_db = bool(re.search(r"sqlite3\.connect|INSERT INTO|UPSERT|execute\(", text)) and "db" in text.lower()
    self_push = ("app.push" in text) or ("get_push_hub" in text) or ("send_feishu" in text) or ("dingtalk" in text.lower())
    stem = name.rsplit(".", 1)[0]
    in_registry = (stem in registry) or (stem.replace("_shadow", "") in registry)

    readers = []
    for f, blob in exec_blobs.items():
        if f.name == name:
            continue
        if (log and pathlib.Path(log).name in blob and len(pathlib.Path(log).name) > 4) or (
                f"/{name}" in blob) or (f"'{name}'" in blob) or (f'"{name}"' in blob) or (stem in blob and f.name != name and stem not in ("__init__",)):
            if name in blob or stem in blob:
                readers.append(f.name)
    # 排除仅自引用
    readers = [r for r in readers if r != name]

    rows.append(dict(name=name, sched=sched, push=push, log=log, db=writes_db,
                     self_push=self_push, registry=in_registry, readers=sorted(set(readers))[:3],
                     nread=len(set(readers))))

print(f"{'脚本':44s} {'调度':18s} push db 自推 注册 读者")
for r in sorted(rows, key=lambda x: x["name"]):
    print(f"{r['name']:44s} {r['sched']:18s} {int(r['push'])}    {int(r['db'])}  "
          f"{int(r['self_push'])}   {int(r['registry'])}    {r['nread']}")

orphan = [r for r in rows if not r["push"] and not r["self_push"] and not r["registry"]
          and r["nread"] <= 1 and ("* * * * *" != r["sched"]) and not r["sched"].startswith("*")]
print("\n=== 孤儿候选 (无推送/无自推/不在注册表/几乎无读者, 非每分钟环) ===")
for r in orphan:
    print(f"  {r['name']:44s} {r['sched']:18s} log={pathlib.Path(r['log']).name:34s} 读者={r['readers']}")