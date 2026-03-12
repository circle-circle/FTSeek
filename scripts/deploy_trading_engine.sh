#!/bin/bash
#
# 统一决策引擎部署脚本
# 功能：检查依赖、执行数据库迁移、启动定时任务调度器和 API 服务器
#
# 用法:
#   bash scripts/deploy_trading_engine.sh [OPTIONS]
#
# 选项:
#   --dry-run     仅检查，不执行实际操作
#   --skip-check  跳过依赖检查
#   --help        显示帮助信息
#

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目路径
PROJECT_ROOT="/opt/ftseek/FTSeek"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="${PROJECT_ROOT}/venv"
PYTHON_CMD="python3"

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 显示帮助信息
show_help() {
    cat << EOF
统一决策引擎部署脚本

用法: $0 [OPTIONS]

选项:
  --dry-run     仅检查依赖和配置，不执行实际操作
  --skip-check  跳过 Python 依赖检查
  --help        显示此帮助信息

部署步骤:
  1. 检查 Python 依赖 (APScheduler, Flask, pytz)
  2. 执行数据库迁移
  3. 启动定时任务调度器
  4. 启动 API 服务器
  5. 健康检查

示例:
  $0                    # 正常部署
  $0 --dry-run          # 仅检查
  $0 --skip-check       # 跳过依赖检查

EOF
}

# 检查 Python 版本
check_python() {
    log_info "检查 Python 版本..."
    
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
        log_success "Python 版本：$PYTHON_VERSION"
    else
        log_error "未找到 Python3，请先安装 Python 3.8+"
        exit 1
    fi
}

# 检查 Python 依赖
check_dependencies() {
    log_info "检查 Python 依赖..."
    
    local missing_deps=()
    
    # 检查关键依赖
    for dep in "apscheduler" "flask" "pytz"; do
        if ! python3 -c "import ${dep}" 2>/dev/null; then
            missing_deps+=("$dep")
        fi
    done
    
    if [ ${#missing_deps[@]} -eq 0 ]; then
        log_success "所有依赖已安装"
    else
        log_warning "缺少依赖：${missing_deps[*]}"
        
        if [ "$DRY_RUN" != "true" ]; then
            log_info "正在安装缺失的依赖..."
            pip3 install -q "${missing_deps[@]}"
            log_success "依赖安装完成"
        else
            log_warning "[Dry-run] 将安装依赖：${missing_deps[*]}"
        fi
    fi
}

# 检查虚拟环境
check_venv() {
    log_info "检查虚拟环境..."
    
    if [ -d "$VENV_PATH" ]; then
        PYTHON_CMD="${VENV_PATH}/bin/python3"
        log_success "使用虚拟环境：$VENV_PATH"
    else
        log_warning "未找到虚拟环境，使用系统 Python"
    fi
}

# 检查配置文件
check_config() {
    log_info "检查配置文件..."
    
    local config_file="${PROJECT_ROOT}/config/trading_decision.yaml"
    local env_file="${PROJECT_ROOT}/.env"
    
    if [ -f "$env_file" ]; then
        log_success "环境变量文件存在：$env_file"
    else
        log_warning "环境变量文件不存在：$env_file"
        log_info "请确保已配置必要的环境变量："
        echo "  - API_TOKEN / API_KEY"
        echo "  - DINGTALK_WEBHOOK (可选)"
        echo "  - FUTU_PASSWORD"
    fi
    
    if [ -f "$config_file" ]; then
        log_success "配置文件存在：$config_file"
    else
        log_warning "配置文件不存在：$config_file"
        log_info "将使用默认配置"
    fi
}

# 执行数据库迁移
run_migrations() {
    log_info "执行数据库迁移..."
    
    if [ "$DRY_RUN" == "true" ]; then
        log_info "[Dry-run] 将执行数据库迁移"
        return
    fi
    
    cd "$PROJECT_ROOT"
    
    # 检查迁移脚本
    local migrate_script="${PROJECT_ROOT}/app/v4/storage/db_migrate.py"
    if [ -f "$migrate_script" ]; then
        if $PYTHON_CMD "$migrate_script" 2>&1; then
            log_success "数据库迁移完成"
        else
            log_error "数据库迁移失败"
            exit 1
        fi
    else
        log_warning "未找到迁移脚本，跳过迁移"
    fi
}

# 启动定时任务调度器
start_scheduler() {
    log_info "启动定时任务调度器..."
    
    if [ "$DRY_RUN" == "true" ]; then
        log_info "[Dry-run] 将启动定时任务调度器"
        log_info "[Dry-run] 定时任务包括："
        echo "  - 港股日报：周一至周五 16:30 HKT"
        echo "  - 美股日报：周一至周五 04:30 EST"
        echo "  - 周报：周日 20:00 Asia/Shanghai"
        echo "  - 绩效检查：每小时一次"
        return
    fi
    
    cd "$PROJECT_ROOT"
    
    # 创建启动脚本
    cat > /tmp/start_scheduler.py << 'PYEOF'
import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

try:
    from app.v4.report.report_generator import ReportGenerator
    from app.v4.core.decision_engine import DecisionEngine
    from app.v4.scheduler.trading_scheduler import init_trading_scheduler
    
    # 初始化组件
    db_path = "/opt/ftseek/FTSeek/data/trading_decision.db"
    
    report_gen = ReportGenerator(db_path=db_path)
    decision_eng = DecisionEngine(db_path=db_path)
    
    scheduler = init_trading_scheduler(
        report_generator=report_gen,
        decision_engine=decision_eng,
        db_path=db_path
    )
    
    # 调度任务
    scheduler.schedule_daily_report("0700.HK", "HK")
    scheduler.schedule_performance_check()
    
    # 启动
    scheduler.start()
    
    logging.info("定时任务调度器已启动")
    logging.info("按 Ctrl+C 停止")
    
    # 保持运行
    while True:
        import time
        time.sleep(60)
        
except Exception as e:
    logging.error(f"启动失败：{e}")
    sys.exit(1)
PYEOF
    
    log_info "定时任务调度器启动中..."
    # 后台运行
    nohup $PYTHON_CMD /tmp/start_scheduler.py > "${PROJECT_ROOT}/logs/scheduler.log" 2>&1 &
    SCHEDULER_PID=$!
    echo $SCHEDULER_PID > "${PROJECT_ROOT}/logs/scheduler.pid"
    
    sleep 2
    
    if kill -0 $SCHEDULER_PID 2>/dev/null; then
        log_success "定时任务调度器已启动 (PID: $SCHEDULER_PID)"
    else
        log_error "定时任务调度器启动失败，请检查日志：${PROJECT_ROOT}/logs/scheduler.log"
        exit 1
    fi
}

# 启动 API 服务器
start_api_server() {
    log_info "启动 API 服务器..."
    
    if [ "$DRY_RUN" == "true" ]; then
        log_info "[Dry-run] 将启动 API 服务器"
        log_info "[Dry-run] API 端点："
        echo "  - GET  /api/v2/positions     - 查询持仓"
        echo "  - GET  /api/v2/performance   - 查询绩效"
        echo "  - POST /api/v2/mode/{code}   - 切换模式"
        echo "  - POST /api/v2/manual_exit/{code} - 手动平仓"
        echo "  - GET  /api/v2/health        - 健康检查"
        log_info "[Dry-run] 监听端口：8900"
        return
    fi
    
    cd "$PROJECT_ROOT"
    
    # 设置环境变量
    export API_PORT=8900
    export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH}"
    
    # 启动 API 服务器
    nohup $PYTHON_CMD -m app.v4.api.decision_api > "${PROJECT_ROOT}/logs/api_server.log" 2>&1 &
    API_PID=$!
    echo $API_PID > "${PROJECT_ROOT}/logs/api_server.pid"
    
    sleep 2
    
    if kill -0 $API_PID 2>/dev/null; then
        log_success "API 服务器已启动 (PID: $API_PID, 端口：8900)"
    else
        log_error "API 服务器启动失败，请检查日志：${PROJECT_ROOT}/logs/api_server.log"
        exit 1
    fi
}

# 健康检查
health_check() {
    log_info "执行健康检查..."
    
    if [ "$DRY_RUN" == "true" ]; then
        log_info "[Dry-run] 将执行健康检查"
        return
    fi
    
    local api_url="http://localhost:8900/api/v2/health"
    
    # 检查 API 服务器
    if command -v curl &> /dev/null; then
        if curl -s -o /dev/null -w "%{http_code}" "$api_url" | grep -q "200"; then
            log_success "API 服务器健康检查通过"
        else
            log_error "API 服务器健康检查失败"
            exit 1
        fi
    else
        log_warning "curl 未安装，跳过 API 健康检查"
    fi
    
    # 检查调度器进程
    if [ -f "${PROJECT_ROOT}/logs/scheduler.pid" ]; then
        SCHEDULER_PID=$(cat "${PROJECT_ROOT}/logs/scheduler.pid")
        if kill -0 $SCHEDULER_PID 2>/dev/null; then
            log_success "定时任务调度器运行正常 (PID: $SCHEDULER_PID)"
        else
            log_error "定时任务调度器进程异常"
            exit 1
        fi
    fi
    
    # 检查 API 服务器进程
    if [ -f "${PROJECT_ROOT}/logs/api_server.pid" ]; then
        API_PID=$(cat "${PROJECT_ROOT}/logs/api_server.pid")
        if kill -0 $API_PID 2>/dev/null; then
            log_success "API 服务器运行正常 (PID: $API_PID)"
        else
            log_error "API 服务器进程异常"
            exit 1
        fi
    fi
    
    log_success "所有健康检查通过!"
}

# 显示部署信息
show_deployment_info() {
    echo ""
    echo "========================================"
    echo "  统一决策引擎部署完成!"
    echo "========================================"
    echo ""
    echo "服务状态:"
    echo "  - 定时任务调度器：运行中"
    echo "  - API 服务器：运行中 (端口 8900)"
    echo ""
    echo "日志文件:"
    echo "  - 调度器日志：${PROJECT_ROOT}/logs/scheduler.log"
    echo "  - API 日志：${PROJECT_ROOT}/logs/api_server.log"
    echo ""
    echo "API 端点:"
    echo "  - 健康检查：GET http://localhost:8900/api/v2/health"
    echo "  - 查询持仓：GET http://localhost:8900/api/v2/positions"
    echo "  - 查询绩效：GET http://localhost:8900/api/v2/performance?code=0700.HK"
    echo "  - 切换模式：POST http://localhost:8900/api/v2/mode/{code}"
    echo "  - 手动平仓：POST http://localhost:8900/api/v2/manual_exit/{code}"
    echo ""
    echo "停止服务:"
    echo "  - 停止调度器：kill \$(cat ${PROJECT_ROOT}/logs/scheduler.pid)"
    echo "  - 停止 API:    kill \$(cat ${PROJECT_ROOT}/logs/api_server.pid)"
    echo ""
    echo "========================================"
}

# 主函数
main() {
    echo "========================================"
    echo "  统一决策引擎部署脚本"
    echo "  版本：1.0.0"
    echo "========================================"
    echo ""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run)
                DRY_RUN=true
                log_info "Dry-run 模式：仅检查，不执行实际操作"
                shift
                ;;
            --skip-check)
                SKIP_CHECK=true
                log_info "跳过依赖检查"
                shift
                ;;
            --help)
                show_help
                exit 0
                ;;
            *)
                log_error "未知参数：$1"
                show_help
                exit 1
                ;;
        esac
    done
    
    # 进入项目目录
    cd "$PROJECT_ROOT"
    
    # 执行部署步骤
    check_python
    check_venv
    
    if [ "$SKIP_CHECK" != "true" ]; then
        check_dependencies
    fi
    
    check_config
    run_migrations
    start_scheduler
    start_api_server
    health_check
    
    if [ "$DRY_RUN" != "true" ]; then
        show_deployment_info
    else
        echo ""
        log_success "Dry-run 完成！所有检查通过。"
    fi
}

# 执行主函数
main "$@"
