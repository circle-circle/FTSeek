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

# ── 2.7 判据基线 + 仓外 DA skill 源纳入异机快照 (2026-09-06, ADR-038 复核批, 用户裁"按建议执行") ──
#   两类文件此前全机器只有单份, 机器损毁即不可复原:
#   ① 判据基线 json — .gitignore:46 `data/*` 整体忽略 ⇒ config_freeze_baseline (W1/KR2)
#      与 field_cardinality_baseline (W2/P7) 从不入任何 commit。它们本身就是"判据",
#      丢了就无法证明守卫有没有被人偷偷放宽 (与 2026-08-28 hermes_jobs 空承诺同族病灶)。
#   ② DA 分析师 skill 源 (/root/.hermes/skills/deep-stock-analysis) — handoff 长期 caveat
#      明写"DA 是 repo 外的 Hermes skill, 修复点在 skill", 即交易决策的一半真身在本仓
#      版本托管之外; 该目录所属 git 仓 (/root) 无 remote, 2026-09-06 实测 llm_synth.py /
#      pipeline.py 自 9/5 起改动未提交、skill 最后一次提交停在 9/1 ⇒ 既无回滚点也无异地副本。
#   口径: 只拷源 (*.py / *.md, 排除 *.bak* / data/ / __pycache__), 实测 84 文件 1.3MB
#   (对比同快照内 DB 副本 46MB); 已核全文无硬编码密钥 (只有 os.environ/getenv 读法)。
#   不删旧文件 (只归档不删证, 同 scripts/rotate_data_backups.py 纪律) — 快照以覆盖为准,
#   force-push 只留最新一份, 不累积。回滚: 删本段。
SKILL_DIR=/root/.hermes/skills/deep-stock-analysis
mkdir -p data/ops/hermes_skill_dsa
if [ -d "$SKILL_DIR" ]; then
    if ( cd "$SKILL_DIR" && find . \( -path './data' -o -name '__pycache__' \) -prune -o \
           \( -name '*.py' -o -name '*.md' \) -not -name '*.bak*' -print0 ) \
       | tar --null -C "$SKILL_DIR" -cf - -T - \
       | tar -C data/ops/hermes_skill_dsa -xf - \
       && git add -f -- data/ops/hermes_skill_dsa/ 2>/dev/null; then
        echo "$LOG_PREFIX DA skill 源已纳入异机快照: $(find data/ops/hermes_skill_dsa -type f | wc -l) 文件 / $(du -sh data/ops/hermes_skill_dsa | cut -f1)"
    else
        echo "$LOG_PREFIX ⚠️ DA skill 源拷贝/add -f 失败, 本次快照不含仓外 DA 代码"
    fi
else
    echo "$LOG_PREFIX ⚠️ $SKILL_DIR 不存在 (路径迁移?), 本次快照不含仓外 DA 代码"
fi
if git add -f -- data/config_freeze_baseline.json data/field_cardinality_baseline.json 2>/dev/null; then
    echo "$LOG_PREFIX 判据基线已纳入异机快照: W1 config_freeze + W2 field_cardinality"
else
    echo "$LOG_PREFIX ⚠️ 判据基线 add -f 失败, 本次快照不含判据文件"
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
