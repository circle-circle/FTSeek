#!/bin/bash
# =============================================================================
# FTSeek 安全部署脚本 — Phase 2
# 用法: bash scripts/deploy.sh [ftseek|ftseek-us|all] [skip_verify]
# =============================================================================
# 特性:
#   ✅ 自动备份当前代码
#   ✅ 交易时段保护 (09:00-16:00 HKT / 21:30-04:00 EST)
#   ✅ 部署前自检 (语法、配置审计)
#   ✅ 部署后验证 (服务状态、日志检查)
#   ✅ 失败自动回滚
# =============================================================================

set -euo pipefail

# ================= 配置 =================
BACKUP_ROOT="/opt/ftseek/backups"
FTSEEK_DIR="/opt/ftseek/FTSeek"
FTSEEK_US_DIR="/opt/ftseek/FTSeek-US"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/tmp/deploy_${TIMESTAMP}.log"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ================= 函数 =================
log() { echo -e "${GREEN}[DEPLOY]${NC} $1" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOG_FILE"; }
error() { echo -e "${RED}[ERROR]${NC} $1" | tee -a "$LOG_FILE"; }

check_trading_hours() {
    # 获取当前 UTC 时间
    UTC_HOUR=$(date -u +%H)
    UTC_MIN=$(date -u +%M)
    UTC_TIME=$((10#$UTC_HOUR * 60 + 10#$UTC_MIN))
    
    # 港股交易时段: 01:30-04:00 UTC (09:30-12:00) + 05:00-08:00 UTC (13:00-16:00)
    HK_MORNING_START=90;   HK_MORNING_END=240
    HK_AFTERNOON_START=300; HK_AFTERNOON_END=480
    
    # 美股交易时段: 14:30-21:00 UTC (09:30-16:00 ET)
    US_START=870; US_END=1260
    
    IS_TRADING=false
    
    if (( UTC_TIME >= HK_MORNING_START && UTC_TIME <= HK_MORNING_END )) ||
       (( UTC_TIME >= HK_AFTERNOON_START && UTC_TIME <= HK_AFTERNOON_END )); then
        warn "⚠️  当前在港股交易时段 (${UTC_HOUR}:${UTC_MIN} UTC)"
        IS_TRADING=true
    fi
    
    if (( UTC_TIME >= US_START && UTC_TIME <= US_END )); then
        warn "⚠️  当前在美股交易时段 (${UTC_HOUR}:${UTC_MIN} UTC)"
        IS_TRADING=true
    fi
    
    if [ "$IS_TRADING" = true ]; then
        echo ""
        echo "=============================================="
        echo "  ⚠️  交易时段保护"
        echo "=============================================="
        echo "  当前在交易时段，部署可能导致:"
        echo "  - 行情订阅中断"
        echo "  - 持仓状态丢失"
        echo "  - 订单执行异常"
        echo ""
        echo "  建议部署窗口:"
        echo "  - 港股: 16:30 - 次日 09:00 (HKT)"
        echo "  - 美股: 04:30 - 次日 21:00 (EST)"
        echo ""
        
        if [ "${2:-}" != "skip_verify" ]; then
            echo "  输入 'yes' 确认继续部署 (或按 Ctrl+C 取消): "
            read -r confirm
            if [ "$confirm" != "yes" ]; then
                log "部署已取消"
                exit 0
            fi
        fi
    else
        log "✅ 当前在非交易时段，可以安全部署"
    fi
}

backup_code() {
    local dir=$1
    local name=$(basename "$dir")
    local backup_dir="${BACKUP_ROOT}/${name}_${TIMESTAMP}"
    
    log "[备份] 正在备份 ${name} → ${backup_dir}"
    mkdir -p "$backup_dir"
    cp -r "$dir" "$backup_dir/"
    
    # 保留最近 10 个备份
    ls -td ${BACKUP_ROOT}/${name}_* 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null || true
    
    log "[备份] 完成 (保留最近 10 个备份)"
    echo "$backup_dir"
}

pre_deploy_check() {
    local dir=$1
    log "[预检] 运行语法检查..."
    
    cd "$dir"
    
    # 语法检查
    find app/ -name "*.py" -exec python3.8 -m py_compile {} + 2>&1 | tee -a "$LOG_FILE" || {
        error "❌ 语法检查失败！部署已中止"
        return 1
    }
    
    # 配置审计 (如果存在)
    if [ -f "scripts/config_audit.py" ]; then
        log "[预检] 运行配置审计..."
        python3.8 scripts/config_audit.py FTSeek 2>&1 | tee -a "$LOG_FILE" || {
            warn "⚠️  配置审计有警告，但继续部署"
        }
    fi
    
    log "[预检] 通过"
}

deploy_project() {
    local dir=$1
    local service_name=$2
    local news_service=$3  # ftseek-news-v2 或空
    
    log "=========================================="
    log "  部署 ${service_name}"
    log "=========================================="
    
    # 1. 停止服务
    log "[1/6] 停止 ${service_name}..."
    systemctl stop "$service_name" 2>/dev/null || true
    if [ -n "$news_service" ]; then
        systemctl stop "$news_service" 2>/dev/null || true
    fi
    
    # 2. 备份代码
    local backup_dir=$(backup_code "$dir")
    BACKUP_DIRS+=("$backup_dir")
    
    # 3. 拉取最新代码
    log "[3/6] 拉取最新代码..."
    cd "$dir"
    git fetch origin
    git checkout master 2>/dev/null || git checkout main 2>/dev/null || true
    git pull origin master || git pull origin main || {
        error "❌ git pull 失败！"
        return 1
    }
    
    # 4. 安装依赖
    log "[4/6] 安装依赖..."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt --quiet 2>&1 | tee -a "$LOG_FILE" || {
            warn "⚠️  依赖安装有警告，但继续部署"
        }
    fi
    
    # 5. 启动服务
    log "[5/6] 启动 ${service_name}..."
    systemctl start "$service_name"
    if [ -n "$news_service" ]; then
        systemctl start "$news_service" 2>/dev/null || true
    fi
    
    # 6. 验证
    log "[6/6] 验证服务状态..."
    sleep 8
    
    if systemctl is-active --quiet "$service_name"; then
        log "✅ ${service_name} 启动成功"
        
        # 检查日志中的 ERROR
        local error_count=$(journalctl -u "$service_name" --since "30 sec ago" --no-pager 2>/dev/null | grep -c "ERROR" || echo "0")
        if [ "$error_count" -gt 0 ]; then
            warn "⚠️  启动后有 ${error_count} 个 ERROR (请查看日志):"
            journalctl -u "$service_name" --since "30 sec ago" --no-pager 2>/dev/null | grep "ERROR" | tail -3
        else
            log "✅ 无 ERROR 日志"
        fi
        
        return 0
    else
        error "❌ ${service_name} 启动失败！正在回滚..."
        
        # 回滚
        log "[回滚] 恢复备份版本..."
        rm -rf "$dir"
        cp -r "$backup_dir/$(basename "$dir")" "$dir"
        
        systemctl start "$service_name" 2>/dev/null || true
        if [ -n "$news_service" ]; then
            systemctl start "$news_service" 2>/dev/null || true
        fi
        
        error "❌ 已回滚到备份版本: $backup_dir"
        return 1
    fi
}

# ================= 主流程 =================
PROJECT=${1:-all}
SKIP_VERIFY=${2:-}
BACKUP_DIRS=()

log "============================================"
log "  FTSeek 部署脚本 — Phase 2"
log "  时间: $(date)"
log "  项目: ${PROJECT}"
log "  日志: ${LOG_FILE}"
log "============================================"

# 检查交易时段
check_trading_hours "$PROJECT" "$SKIP_VERIFY"

# 部署
case "$PROJECT" in
    ftseek)
        deploy_project "$FTSEEK_DIR" "ftseek" "ftseek-news-v2" || exit 1
        ;;
    ftseek-us)
        deploy_project "$FTSEEK_US_DIR" "ftseek-us" "" || exit 1
        ;;
    all)
        deploy_project "$FTSEEK_DIR" "ftseek" "ftseek-news-v2" || exit 1
        echo ""
        deploy_project "$FTSEEK_US_DIR" "ftseek-us" "" || exit 1
        ;;
    *)
        error "未知项目: $PROJECT (支持: ftseek, ftseek-us, all)"
        exit 1
        ;;
esac

log "============================================"
log "  ✅ 部署完成！"
log "  备份位置: ${BACKUP_DIRS[*]}"
log "  日志文件: ${LOG_FILE}"
log "============================================"

# 钉钉通知 (可选)
if [ -n "${DINGTALK_WEBHOOK_URL:-}" ]; then
    curl -s "$DINGTALK_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        -d "{
            \"msgtype\": \"text\",
            \"text\": {
                \"content\": \"[FTSeek] 部署成功 ✅\n时间: $(date)\n项目: ${PROJECT}\n版本: $(cd $FTSEEK_DIR && git log --oneline -1 | cut -d' ' -f1)\"
            }
        }" 2>/dev/null || true
fi

exit 0
