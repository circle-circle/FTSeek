#!/bin/bash
# 【已作废·勿再跑】2026-08-31 16:14 CST 用户已手工 systemctl restart ftseek-us 完成,
#   四项判据复核通过 (零 ERROR/持仓 5 只含券商 SL 单全 synced/进程龄>修复文件 mtime/find_tp 复验),
#   AVGO 8/27、8/28 回补亦已完成 (2/2, touch_tp 皆未触及)。保留作重启+机械自验判据模板。
# 一次性: 重启 ftseek-us 加载 scripts/ah_exit_shadow.py find_tp 加固
# (2026-08-31, TP关联秒级错位修复 — AVGO open_positions :34 vs trade_records :33
#  致 touch_tp 观测臂失明; 修复合契约测试 4 例, 生产库只读验证通过)。
# 判据照 ops/hermes/ftseek_us_restart_20260829_hotfix.sh 同形 (四项硬判+持仓快照)。
# 为何必须重启: scheduler _load_script_module 进程内缓存脚本模块, 改动不重启不生效;
#   首次生效点=重启后最近的 AFTER_HOURS/OVERNIGHT tick (今晚 CST 07:50=ET 19:50 实时预评)。
# 时间守卫: 04-06 CST (每日 ET 盘后窗)。窗外需 FORCE=1 (人工授权例外, 不入代码策略,
#   同 2026-08-29 盘中重启先例)。硬截止: 今晚 CST 07:50 前完成。
# 附带步骤: 重启后回补 AVGO 8/27、8/28 两夜 jsonl 的 tp 元数据 (从完整缓存重算,
#   record-only, 零下单), 带竞态重试 (调度器 2min tick 同写 jsonl)。
# 回滚: 本次仅加载既有工作区修复, 无新行为开关; 如需回退 revert find_tp 改动再重启。
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPO=/opt/ftseek/FTSeek-US
PY=$REPO/venv/bin/python
DB=$REPO/data/ftseek_us.db
FIXED=$REPO/scripts/ah_exit_shadow.py
LOG=$REPO/logs/restart_ah_shadow_tpfix.log
exec >>"$LOG" 2>&1

echo "===== restart start: $(date '+%F %T %Z') ====="
_H=$(date +%H)
if [ "${FORCE:-0}" != "1" ] && { [ "$_H" -lt 4 ] || [ "$_H" -ge 6 ]; }; then
    echo "[abort] 不在 04-06 CST 窗内, 拒绝重启 (窗外需 FORCE=1 人工授权)"; exit 2
fi

snap() {
    $PY - "$DB" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("SELECT symbol,size,current_sl,sl_broker_order_id,sync_status,"
                 "is_anomalous FROM open_positions ORDER BY symbol").fetchall()
for r in rows:
    print("|".join(str(v) for v in r))
print("COUNT=%d" % len(rows))
c.close()
PYEOF
}
snap > /tmp/pos_before_ah_tpfix.txt; echo "--- positions before ---"; cat /tmp/pos_before_ah_tpfix.txt

RESTART_STAMP=$(date '+%F %T')
systemctl restart ftseek-us
sleep 20

FAIL=""
systemctl is-active ftseek-us >/dev/null || FAIL="$FAIL ftseek-us:inactive"
PID=$(systemctl show ftseek-us -p MainPID --value)
echo "ftseek-us pid=$PID started=$(ps -o lstart= -p "$PID" 2>/dev/null)"
[ -n "$PID" ] && [ "$PID" != "0" ] || FAIL="$FAIL main_pid_zero"

snap > /tmp/pos_after_ah_tpfix.txt
if ! diff -q /tmp/pos_before_ah_tpfix.txt /tmp/pos_after_ah_tpfix.txt >/dev/null; then
    echo "[note] 持仓快照前后有差异 (状态流转可为正常), 逐行如下:"
    diff /tmp/pos_before_ah_tpfix.txt /tmp/pos_after_ah_tpfix.txt || true
fi

# ERROR 判据与 scripts/error_alert.sh 同形 (读应用日志非 journal)
ERRS=$(awk -v s="$RESTART_STAMP" '/ERROR|CRITICAL/{ ts=substr($0,1,19); if (ts >= s) n++ } END{print n+0}' \
        "$REPO/logs/ftseek_us.log" 2>/dev/null || echo 0)
echo "ERROR 行数 (重启后): $ERRS"
[ "$ERRS" = "0" ] || FAIL="$FAIL errors:$ERRS"

# 正面证据1: 进程启动晚于修复文件 mtime (新模块必被首次加载)
FIX_MTIME=$(stat -c %Y "$FIXED")
PROC_EPOCH=$(( $(date +%s) - $(ps -o etimes= -p "$PID" | tr -d ' ') ))
if [ "$PROC_EPOCH" -gt "$FIX_MTIME" ]; then
    echo "代码加载: OK (进程启动 $(date -d "@$PROC_EPOCH" '+%F %T') > 文件修改 $(date -d "@$FIX_MTIME" '+%F %T'))"
else
    FAIL="$FAIL process_older_than_fix"
    echo "代码加载: ❌ 进程启动早于修复文件修改时刻"
fi

# 正面证据2: 盘上代码只读复验 (在仓行语义关联 → AVGO tp=333.6)
$PY - "$REPO" <<'PYEOF' || FAIL="$FAIL probe_find_tp"
import importlib.util, sys
from pathlib import Path
repo = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("aes_probe", repo/"scripts/ah_exit_shadow.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
got = {p["symbol"]: m.find_tp(p["pos_key"]) for p in m.load_open_positions()}
print("find_tp 复验:", got)
assert got.get("US.AVGO") == 333.6, f"AVGO tp 未恢复: {got.get('US.AVGO')}"
print("find_tp 复验: OK")
PYEOF

# 回补 AVGO 8/27、8/28 两夜 tp 元数据 (完整缓存重算, 零下单; 竞态重试最多3轮)
for attempt in 1 2 3; do
    $PY - "$REPO" <<'PYEOF'
import importlib.util, json, sys
from pathlib import Path
repo = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("aes_backfill", repo/"scripts/ah_exit_shadow.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
rows = m._load_rows()
changed = 0
for r in rows:
    if r.get("symbol") == "US.AVGO" and r.get("night") in ("2026-08-27", "2026-08-28") \
            and r.get("eval_stage") == "final" and r.get("ah_close") is not None:
        tp = m.find_tp((r["symbol"], r["direction"], r["entry_time"][:19]))
        ck = m.CACHE / f"US_AVGO_{r['night']}_shadow.json"
        bars = json.loads(ck.read_text()).get("bars", []) if ck.exists() else []
        if tp is None or not bars:
            print(f"[backfill] skip {r['night']} tp={tp} bars={len(bars)}"); continue
        if r.get("tp") == tp:
            print(f"[backfill] {r['night']} 已就位"); continue
        r["tp"] = tp
        r["rules"] = m.eval_rules(r["direction"], r["entry_price"], tp, bars)
        changed += 1
        print(f"[backfill] {r['night']} tp={tp} touch_tp={r['rules']['touch_tp']}")
if changed:
    m._save_rows(rows)
    m._write_summary(rows)
print(f"[backfill] changed={changed}")
PYEOF
    sleep 130  # 跨过调度器一个 2min tick, 防同写竞态回卷
    OK_CNT=$($PY - "$REPO" <<'PYEOF'
import json, sys
from pathlib import Path
rows = [json.loads(l) for l in (Path(sys.argv[1])/"data/shadow/ah_exit_shadow.jsonl").read_text().splitlines() if l.strip()]
print(sum(1 for r in rows if r.get("symbol") == "US.AVGO"
          and r.get("night") in ("2026-08-27", "2026-08-28") and r.get("tp") == 333.6))
PYEOF
)
    echo "[backfill] 第${attempt}轮后 AVGO 就位数=$OK_CNT/2"
    [ "$OK_CNT" = "2" ] && break
done
[ "$OK_CNT" = "2" ] || FAIL="$FAIL backfill_incomplete:$OK_CNT"

echo "RESULT: ftseek-us pid=$PID ERROR=$ERRS backfill=$OK_CNT/2"
echo "===== restart done: $(date '+%F %T %Z') FAIL='${FAIL:-none}' ====="
[ -z "$FAIL" ]
