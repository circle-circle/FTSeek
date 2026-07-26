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
