#!/bin/bash
# FTSeek 港股第一期清理 - 2026-05-06
# 功能: 删除死代码目录 + 合并重复模块 + 验证服务健康
# 回滚: 从 backups/phase1_20260506/ 还原

set -e

PROJECT="/opt/ftseek/FTSeek"
BACKUP="$PROJECT/backups/phase1_20260506"
LOG="$PROJECT/logs/cleanup_20260506.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

# ═══════════════════════════════════════════════════════
# 0. 前置检查
# ═══════════════════════════════════════════════════════
log "=== 第一期清理开始 ==="

if systemctl is-active ftseek > /dev/null 2>&1; then
    log "✅ ftseek 服务正在运行"
else
    log "❌ ftseek 服务未运行，中止清理"
    exit 1
fi

# 创建备份
log "📦 创建备份到 $BACKUP"
mkdir -p "$BACKUP"
cp -r "$PROJECT/app" "$BACKUP/app_backup"
cp "$PROJECT/config/unified_config.yaml" "$BACKUP/" 2>/dev/null || true
log "✅ 备份完成"

# 记录清理前状态
log "📊 清理前状态:"
log "  app 目录数: $(ls -d $PROJECT/app/*/ 2>/dev/null | wc -l)"
log "  app Python 文件数: $(find $PROJECT/app -name '*.py' -not -path '*__pycache__*' | wc -l)"
log "  app 总行数: $(find $PROJECT/app -name '*.py' -not -path '*__pycache__*' -exec cat {} + 2>/dev/null | wc -l)"

# ═══════════════════════════════════════════════════════
# 1. 删除死代码目录 (无任何外部引用的目录)
# ═══════════════════════════════════════════════════════
DEAD_DIRS="
app/deep_review
app/autoresearch
app/profit
app/executor
app/news_v2
app/ftlogging
app/compliance
app/infrastructure
app/limiter
app/performance
app/reporters
app/reports
app/signal_engine
app/intraday
app/config
app/config_mgmt
app/dashboard
app/docs
app/static
"

log "🧹 开始删除死代码目录..."
for d in $DEAD_DIRS; do
    if [ -d "$PROJECT/$d" ]; then
        lines=$(find "$PROJECT/$d" -name '*.py' -exec cat {} + 2>/dev/null | wc -l)
        files=$(find "$PROJECT/$d" -name '*.py' | wc -l)
        log "  🗑️  删除 $d/ ($files 文件, $lines 行)"
        rm -rf "$PROJECT/$d"
    else
        log "  ⏭️  $d/ 不存在 (已删除)"
    fi
done

# ═══════════════════════════════════════════════════════
# 2. 合并 consensus/ 到 strategy/
# ═══════════════════════════════════════════════════════
if [ -d "$PROJECT/app/consensus" ]; then
    log "📦 合并 consensus/ 到 strategy/..."
    # 仅保留 weight_calculator.py 中有用的部分（如果 consensus_engine.py 还没有）
    if [ -f "$PROJECT/app/consensus/weight_calculator.py" ]; then
        # 检查 strategy/consensus_engine.py 是否已有 weight engine
        if ! grep -q "weight_engine" "$PROJECT/app/strategy/consensus_engine.py" 2>/dev/null; then
            cp "$PROJECT/app/consensus/weight_calculator.py" "$PROJECT/app/strategy/consensus_weight_calculator.py"
            log "  📄 复制 weight_calculator.py → strategy/consensus_weight_calculator.py"
        fi
    fi
    rm -rf "$PROJECT/app/consensus"
    log "  🗑️  删除 consensus/"
fi

# ═══════════════════════════════════════════════════════
# 3. 清理 __pycache__
# ═══════════════════════════════════════════════════════
log "🧹 清理 __pycache__..."
find "$PROJECT/app" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
log "✅ __pycache__ 已清理"

# ═══════════════════════════════════════════════════════
# 4. 语法检查
# ═══════════════════════════════════════════════════════
log "🔍 执行 Python 语法检查..."
ERRORS=0
for f in $(find "$PROJECT/app" -name '*.py' -not -path '*__pycache__*'); do
    if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
        log "  ❌ 语法错误: $f"
        ERRORS=$((ERRORS + 1))
    fi
done

if [ $ERRORS -gt 0 ]; then
    log "❌ 发现 $ERRORS 个语法错误，执行回滚..."
    rm -rf "$PROJECT/app"
    mv "$BACKUP/app_backup" "$PROJECT/app"
    log "✅ 回滚完成"
    exit 1
fi
log "✅ 语法检查通过 (0 错误)"

# ═══════════════════════════════════════════════════════
# 5. 重启服务 + 健康检查
# ═══════════════════════════════════════════════════════
log "🔄 重启 ftseek 服务..."
systemctl restart ftseek
sleep 15

if systemctl is-active ftseek > /dev/null 2>&1; then
    log "✅ ftseek 服务已启动"
else
    log "❌ ftseek 服务启动失败，执行回滚..."
    rm -rf "$PROJECT/app"
    mv "$BACKUP/app_backup" "$PROJECT/app"
    systemctl restart ftseek
    sleep 10
    log "✅ 回滚完成"
    exit 1
fi

# 健康检查
HEALTH=$(curl -s http://localhost:8899/health 2>/dev/null || echo "FAIL")
if echo "$HEALTH" | grep -q "ok\|healthy\|status" 2>/dev/null; then
    log "✅ 健康检查通过: $HEALTH"
else
    log "⚠️  健康检查响应: $HEALTH"
fi

# 检查日志有无 ERROR
sleep 5
ERROR_COUNT=$(grep -c "ERROR" "$PROJECT/logs/ftseek.log" 2>/dev/null || echo "0")
# 只检查重启后的 ERROR
RECENT_ERRORS=$(tail -50 "$PROJECT/logs/ftseek.log" 2>/dev/null | grep -c "ERROR" || echo "0")
log "📊 重启后最近 50 行日志中 ERROR 数: $RECENT_ERRORS"

# ═══════════════════════════════════════════════════════
# 6. 记录清理后状态
# ═══════════════════════════════════════════════════════
log "📊 清理后状态:"
log "  app 目录数: $(ls -d $PROJECT/app/*/ 2>/dev/null | wc -l)"
log "  app Python 文件数: $(find $PROJECT/app -name '*.py' -not -path '*__pycache__*' | wc -l)"
log "  app 总行数: $(find $PROJECT/app -name '*.py' -not -path '*__pycache__*' -exec cat {} + 2>/dev/null | wc -l)"

# ═══════════════════════════════════════════════════════
# 7. 钉钉通知
# ═══════════════════════════════════════════════════════
BEFORE_LINES=$(find "$BACKUP/app_backup" -name '*.py' -not -path '*__pycache__*' -exec cat {} + 2>/dev/null | wc -l)
AFTER_LINES=$(find "$PROJECT/app" -name '*.py' -not -path '*__pycache__*' -exec cat {} + 2>/dev/null | wc -l)
REMOVED=$((BEFORE_LINES - AFTER_LINES))

log ""
log "=== 清理完成 ==="
log "删除行数: $REMOVED"
log "删除目录: $(echo $DEAD_DIRS | wc -w)"
log "备份位置: $BACKUP"
log "回滚命令: rm -rf $PROJECT/app && cp -r $BACKUP/app $PROJECT/app/ && systemctl restart ftseek"

echo "SUCCESS"
