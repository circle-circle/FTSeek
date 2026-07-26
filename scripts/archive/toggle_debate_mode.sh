#!/bin/bash
# 双Agent辩论模式 - 快速启动/停止脚本
# 
# 用法:
#   ./scripts/toggle_debate_mode.sh on   # 开启辩论模式
#   ./scripts/toggle_debate_mode.sh off  # 关闭辩论模式
#   ./scripts/toggle_debate_mode.sh status  # 查看状态

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 配置
FTSEEK_DIR="/opt/ftseek/FTSeek-US"
CONFIG_FILE="$FTSEEK_DIR/config/unified_config.yaml"
SERVICE="ftseek-us"

# 函数：检查状态
check_status() {
    echo -e "${YELLOW}=== 辩论模式状态 ===${NC}"
    
    # 检查环境变量
    if [ "$FTSEEK_DEBATE_MODE" = "true" ]; then
        echo -e "环境变量: ${GREEN}已开启${NC}"
    else
        echo -e "环境变量: ${RED}已关闭${NC}"
    fi
    
    # 检查配置文件
    if grep -q "enabled: true" "$CONFIG_FILE" 2>/dev/null; then
        echo -e "配置文件: ${GREEN}enabled: true${NC}"
    else
        echo -e "配置文件: ${RED}enabled: false${NC}"
    fi
    
    # 检查服务状态
    if systemctl is-active --quiet $SERVICE 2>/dev/null; then
        echo -e "服务状态: ${GREEN}运行中${NC}"
    else
        echo -e "服务状态: ${RED}已停止${NC}"
    fi
    
    # 检查辩论相关日志
    echo ""
    echo -e "${YELLOW}=== 最近辩论日志 ===${NC}"
    grep -i "辩论\|debate\|dual_agent" /opt/ftseek/FTSeek-US/logs/*.log 2>/dev/null | tail -5 || echo "无辩论日志"
}

# 函数：开启辩论模式
enable_debate() {
    echo -e "${GREEN}=== 开启辩论模式 ===${NC}"
    
    # 1. 设置环境变量
    export FTSEEK_DEBATE_MODE=true
    export FTSEEK_DEBATE_TIMEOUT=300
    export FTSEEK_DEBATE_FALLBACK_MODEL=qwen-max
    
    echo "✓ 环境变量已设置"
    
    # 2. 更新配置文件
    if [ -f "$CONFIG_FILE" ]; then
        # 使用sed替换enabled值
        sed -i 's/enabled: false/enabled: true/' "$CONFIG_FILE"
        echo "✓ 配置文件已更新"
    else
        echo -e "${RED}✗ 配置文件不存在: $CONFIG_FILE${NC}"
        exit 1
    fi
    
    # 3. 重启服务 (如果运行中)
    if systemctl is-active --quiet $SERVICE 2>/dev/null; then
        echo "⟳ 重启服务中..."
        systemctl restart $SERVICE 2>/dev/null || echo "⚠ 服务重启失败 (可能需要手动重启)"
    else
        echo "ℹ 服务未运行，无需重启"
    fi
    
    echo ""
    echo -e "${GREEN}✓ 辩论模式已开启${NC}"
    echo ""
    echo "下一步:"
    echo "1. 等待下一个盘前时间 (美东 08:00)"
    echo "2. 检查日志: tail -f /opt/ftseek/FTSeek-US/logs/*.log | grep -i debate"
    echo "3. 验证计划: cat /opt/ftseek/FTSeek-US/data/daily_battle_plan_us.json | jq '.debate_stats'"
}

# 函数：关闭辩论模式
disable_debate() {
    echo -e "${RED}=== 关闭辩论模式 ===${NC}"
    
    # 1. 清除环境变量
    unset FTSEEK_DEBATE_MODE
    unset FTSEEK_DEBATE_TIMEOUT
    unset FTSEEK_DEBATE_FALLBACK_MODEL
    
    echo "✓ 环境变量已清除"
    
    # 2. 更新配置文件
    if [ -f "$CONFIG_FILE" ]; then
        sed -i 's/enabled: true/enabled: false/' "$CONFIG_FILE"
        echo "✓ 配置文件已更新"
    fi
    
    # 3. 重启服务 (如果运行中)
    if systemctl is-active --quiet $SERVICE 2>/dev/null; then
        echo "⟳ 重启服务中..."
        systemctl restart $SERVICE 2>/dev/null || echo "⚠ 服务重启失败"
    fi
    
    echo ""
    echo -e "${GREEN}✓ 辩论模式已关闭${NC}"
    echo "系统已恢复为单Agent模式"
}

# 主逻辑
case "${1}" in
    on|enable|开启)
        enable_debate
        ;;
    off|disable|关闭)
        disable_debate
        ;;
    status|状态)
        check_status
        ;;
    *)
        echo "用法: $0 {on|off|status}"
        echo ""
        echo "命令:"
        echo "  on      - 开启辩论模式"
        echo "  off     - 关闭辩论模式"
        echo "  status  - 查看当前状态"
        exit 1
        ;;
esac
