#!/bin/bash
# 一次性: 收盘后重启加载 2026-08-28 hotfix (f06464c2, 7 处 app/* + 台账元环) + 自验证.
# 【已作废·勿再跑】实际生效于 2026-08-29 03:50 CST (=15:50 ET, 收盘前 10min): 用户改裁
#   "趁还在盘中, 直接重启 观察", 由本人按本脚本同判据手工执行 (at job 04:35 随之撤销)。
#   时间守卫 (<4 点拒绝) 保持原样未放宽 —— 盘中重启是人工授权的一次例外, 不写进代码策略。
#   本文件保留作"重启+机械自验"判据模板 (其 ERROR 模式曾恒 0, 见下方注释)。
# 与 ops/hermes/ftseek_us_restart_macro_calendar.sh 同法, 但把"人肉看日志"换成机械判据:
#   active/MainPID/重启后零 ERROR/进程年龄晚于 commit 四项硬判 (任一失败 exit≠0),
#   持仓 SSOT (含 sl_broker_order_id) 前后各拍一次快照逐行留档 —— 盘后平仓可致合理差异,
#   故只落日志供人工核对, 不作硬判 (硬判会把正常平仓变成假故障)。
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPO=/opt/ftseek/FTSeek-US
PY=$REPO/venv/bin/python
LOG=$REPO/logs/restart_20260829_hotfix.log
DB=$REPO/data/ftseek_us.db
exec >>"$LOG" 2>&1

echo "===== restart start: $(date '+%F %T %Z') ====="
_H=$(date +%H)
if [ "$_H" -lt 4 ] || [ "$_H" -gt 6 ]; then
    echo "[abort] 不在盘后窗口 (04-06 CST), 拒绝重启"; exit 2
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
snap > /tmp/pos_before_20260829.txt; echo "--- positions before ---"; cat /tmp/pos_before_20260829.txt

RESTART_STAMP=$(date '+%F %T')
systemctl restart ftseek-us
sleep 20
systemctl restart ftseek-dashboard
sleep 40

FAIL=""
for s in ftseek-us ftseek-dashboard; do
    systemctl is-active "$s" >/dev/null || FAIL="$FAIL $s:inactive"
done
PID=$(systemctl show ftseek-us -p MainPID --value)
echo "ftseek-us pid=$PID started=$(ps -o lstart= -p "$PID" 2>/dev/null)"
[ -n "$PID" ] && [ "$PID" != "0" ] || FAIL="$FAIL main_pid_zero"

snap > /tmp/pos_after_20260829.txt
if ! diff -q /tmp/pos_before_20260829.txt /tmp/pos_after_20260829.txt >/dev/null; then
    # 盘后平仓/状态流转可致差异 → 只报不改判, 差异逐行落日志供人工核对
    echo "[note] 持仓快照前后有差异 (盘后状态流转可为正常), 逐行如下:"
    diff /tmp/pos_before_20260829.txt /tmp/pos_after_20260829.txt || true
fi

# ERROR 判据必须读 logs/ftseek_us.log (应用自身 logging 落此, scripts/error_alert.sh 扫的也是它);
# 用 journalctl 会恒 0 —— 本服务 stdout 几乎不进 journal (8/21 至今仅 283 行), 那是
# "只能报健康的守卫", 与本日修掉的 D1/D2/D4 静默失明同型。
# 模式同样踩过"只能报健康": 首版写 '/ ERROR /', 而本日志形态是 "] ERROR:" (后随冒号非空格),
# 该模式永不匹配 → 恒 0 (2026-08-29 盘中重启实跑才被证伪, 当日真 0 侥幸未掩盖故障)。
# 现与生产告警链 scripts/error_alert.sh 同形 (ERROR|CRITICAL) —— 判据必须与告警判据一致。
ERRS=$(awk -v s="$RESTART_STAMP" '/ERROR|CRITICAL/{ ts=substr($0,1,19); if (ts >= s) n++ } END{print n+0}' \
        "$REPO/logs/ftseek_us.log" 2>/dev/null || echo 0)
echo "ERROR 行数 (重启后, logs/ftseek_us.log): $ERRS"
[ "$ERRS" = "0" ] || FAIL="$FAIL errors:$ERRS"

# 正面证据: 进程启动时刻必须晚于 f06464c2 提交时刻 (文件 grep 只证明在盘, 不证明已加载)。
# 注: 不可用 stat -c %Y /proc/PID —— 那是 /proc 目录的实时 mtime (= 此刻), 判据会恒真,
#     正是本日反复踩到的"只能报健康的守卫"。etimes 才是真实进程年龄。
COMMIT_EPOCH=$(git -C $REPO show -s --format=%ct f06464c2)
PROC_EPOCH=$(( $(date +%s) - $(ps -o etimes= -p "$PID" | tr -d ' ') ))
if [ "$PROC_EPOCH" -gt "$COMMIT_EPOCH" ]; then
    echo "代码加载: OK (进程启动 $(date -d "@$PROC_EPOCH" '+%F %T') > commit $(date -d "@$COMMIT_EPOCH" '+%F %T'))"
else
    FAIL="$FAIL process_older_than_commit"
    echo "代码加载: ❌ 进程启动 $(date -d "@$PROC_EPOCH" '+%F %T') 早于 commit $(date -d "@$COMMIT_EPOCH" '+%F %T')"
fi
grep -q 'get_dotted("orb"' $REPO/app/core/scheduler_core.py && echo "ORB 命名空间修复在盘: OK"
grep -q 'get_dotted('"'"'orb'"'"')' $REPO/app/pipeline/stages/risk_stage.py && echo "risk_stage ORB 读点在盘: OK"

echo "RESULT: ftseek-us pid=$PID 持仓前后差异=$(diff /tmp/pos_before_20260829.txt /tmp/pos_after_20260829.txt | grep -c '^[<>]' || true) 行 ERROR=$ERRS"
echo "===== restart done: $(date '+%F %T %Z') FAIL='${FAIL:-none}' ====="
[ -z "$FAIL" ]
