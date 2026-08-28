#!/usr/bin/env bash
# 每夜 Git 异地备份推送 (2026-07-26, 用户令"做好备份和自动化")
#
# 两层备份:
#   1. 已提交工作: push 全部本地分支到 GitHub (代码+文档异地容灾)
#   2. 未提交运行态: 用 git plumbing 把工作区快照做成孤立 commit
#      推到 backup/runtime-snapshot 分支 (强推覆盖, 只保留最新快照)
#      — 全程不触碰工作区/主索引/HEAD, 对运行中服务零影响
#
# 接线: crontab 30 3 * * * (db_backup 03:00 之后)
# 失败告警: 钉钉 (与 data_freshness_watchdog 兜底同款)
set -u
cd /opt/ftseek/FTSeek-US || exit 1
LOG_PREFIX="[git_backup $(date '+%F %T')]"

notify_fail() {
    WEBHOOK=$(grep DINGTALK_WEBHOOK_URL_US .env.us 2>/dev/null | cut -d= -f2-)
    [ -n "${WEBHOOK:-}" ] && curl -sf -H "Content-Type: application/json" \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"[FTSeek-US告警] git 备份推送失败: $1\"}}" \
        "$WEBHOOK" > /dev/null
}

# ── 0. hermes cron jobs.json 纳入版本跟踪 (2026-08-03 改进批 任务#5) ──
#   jobs.json 在仓库外 (/root/.hermes/cron/), 此前漂移/改动作业定义无版本可追溯.
#   每夜快照进 data/ops/, 随 runtime-snapshot 与常规提交留痕.
mkdir -p data/ops
cp -f /root/.hermes/cron/jobs.json data/ops/hermes_jobs.json 2>/dev/null || true

# ── 1. 推送全部本地分支 (已提交工作) ──
if ! git push origin --all --quiet 2>&1; then
    echo "$LOG_PREFIX 分支推送失败"
    notify_fail "分支推送失败, 见 logs/git_backup.log"
    exit 1
fi
echo "$LOG_PREFIX 分支推送完成: $(git branch --format='%(refname:short)' | tr '\n' ' ')"

# ── 2. 运行态快照 (未提交的 data/config 变更) ──
# 临时索引避免污染主索引; 快照 commit 以 HEAD 为父, 只存在于 backup 分支
TMP_INDEX=$(mktemp)
trap 'rm -f "$TMP_INDEX"' EXIT
export GIT_INDEX_FILE="$TMP_INDEX"
git read-tree HEAD
git add -A -- . ':!.backup_premigration' 2>/dev/null

# ── 2.5 DB 异机副本 (B4, 2026-08-24, 体检 O4) ──
#   data/*.db 与 data/backups 均被 gitignore → 此前 GitHub 侧无任何 DB 副本,
#   机器损毁即全丢. db_backup.sh 03:00 已产出当日一致性副本 (sqlite3 .backup,
#   WAL-safe), 本脚本 03:30 强加该目录进快照 = 每晚一份异机 DB 副本.
#   单文件 18MB < GitHub 100MB 限制; force-push 覆盖, 不累积.
TODAY_BAK="data/backups/$(date +%Y-%m-%d)"
if [ -d "$TODAY_BAK" ] && [ -s "$TODAY_BAK/ftseek_us.db" ]; then
    git add -f -- "$TODAY_BAK/" 2>/dev/null && \
        echo "$LOG_PREFIX 当日 DB 备份已纳入异机快照: $(du -sh "$TODAY_BAK" | cut -f1)"
else
    echo "$LOG_PREFIX ⚠️ 当日 DB 备份缺失 ($TODAY_BAK), 异机快照不含 DB — 检查 db_backup.sh"
fi

# ── 2.6 hermes cron 定义入库 (2026-08-28 修 08-03 静默空转) ──
#   上方 0 段已 cp jobs.json → data/ops/hermes_jobs.json, 但 .gitignore 有 data/*
#   且 git add -A 遵守忽略规则 → 该快照从未进过任何 commit (实测
#   origin/backup/runtime-snapshot 无此文件), "cron 定义可追溯" 一直是空承诺.
#   cron prompt 里内嵌可执行内容 (SQL/bash), 改动必须可回滚, 故与 DB 同款 -f 强加.
#   (已核 jobs.json 全文无密钥值, 只有 DASHSCOPE_API_KEY 等环境变量名)
if [ -s data/ops/hermes_jobs.json ]; then
    git add -f -- data/ops/hermes_jobs.json 2>/dev/null && \
        echo "$LOG_PREFIX hermes cron 定义已纳入异机快照" || \
        echo "$LOG_PREFIX ⚠️ hermes_jobs.json add -f 失败, 本次快照不含 cron 定义"
else
    echo "$LOG_PREFIX ⚠️ data/ops/hermes_jobs.json 缺失/为空 (cp 失败?), 本次快照不含 cron 定义"
fi

TREE=$(git write-tree)
unset GIT_INDEX_FILE

if [ "$TREE" = "$(git rev-parse HEAD^{tree})" ]; then
    echo "$LOG_PREFIX 运行态与 HEAD 无差异, 跳过快照"
    exit 0
fi

SNAP=$(git commit-tree "$TREE" -p HEAD -m "runtime snapshot $(date '+%F %T') (auto, 勿合并)")
if git push origin --force --quiet "$SNAP:refs/heads/backup/runtime-snapshot" 2>&1; then
    echo "$LOG_PREFIX 运行态快照已推送: $SNAP"
else
    echo "$LOG_PREFIX 快照推送失败"
    notify_fail "运行态快照推送失败"
    exit 1
fi
