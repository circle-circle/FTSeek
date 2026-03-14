#!/bin/bash
# ============================================================
#  FTSeek 安全检查脚本
#  用法：./security_check.sh
# ============================================================

set -e

GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  FTSeek 安全检查${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

PASS=0
FAIL=0
WARN=0

check_pass() {
    echo -e "${GREEN}✅ PASS${NC}: $1"
    ((PASS++))
}

check_fail() {
    echo -e "${RED}❌ FAIL${NC}: $1"
    ((FAIL++))
}

check_warn() {
    echo -e "${YELLOW}⚠️  WARN${NC}: $1"
    ((WARN++))
}

# 1. 检查 API_TOKEN
echo "检查 API_TOKEN 配置..."
if grep -q 'API_TOKEN=请替换为你的安全 Token' /opt/ftseek/FTSeek/scripts/ftseek.service 2>/dev/null; then
    check_fail "API_TOKEN 未配置 (ftseek.service)"
elif ! grep -q 'API_TOKEN=' /opt/ftseek/FTSeek/scripts/ftseek.service 2>/dev/null; then
    check_fail "API_TOKEN 未在 systemd 服务中配置"
else
    check_pass "API_TOKEN 已配置"
fi

# 2. 检查环境变量
if [ -z "$API_TOKEN" ]; then
    check_warn "API_TOKEN 环境变量未设置 (如果通过 systemd 启动可忽略)"
else
    if [ ${#API_TOKEN} -lt 32 ]; then
        check_warn "API_TOKEN 长度过短 (建议至少 32 字符)"
    else
        check_pass "API_TOKEN 长度符合要求"
    fi
fi

# 3. 检查钉钉配置
if [ -z "$DINGTALK_WEBHOOK_URL" ]; then
    check_warn "DINGTALK_WEBHOOK_URL 环境变量未设置"
else
    if [[ "$DINGTALK_WEBHOOK_URL" == *"access_token="* ]]; then
        check_pass "钉钉 Webhook URL 格式正确"
    else
        check_fail "钉钉 Webhook URL 格式错误"
    fi
fi

if [ -z "$DINGTALK_SECRET" ]; then
    check_warn "DINGTALK_SECRET 环境变量未设置"
else
    if [[ "$DINGTALK_SECRET" == SEC* ]]; then
        check_pass "钉钉 Secret 格式正确"
    else
        check_warn "钉钉 Secret 格式可能不正确 (应以 SEC 开头)"
    fi
fi

# 4. 检查交易环境
if [ "$TRADE_ENV" == "0" ]; then
    check_warn "当前为 REAL 交易环境 - 请确保策略已充分测试"
elif [ "$TRADE_ENV" == "1" ]; then
    check_pass "当前为 SIMULATE 模拟环境 (安全)"
else
    check_warn "TRADE_ENV 未设置 (默认模拟环境)"
fi

# 5. 检查配置文件默认值
if grep -q 'ftseek_secret_token_change_me' /opt/ftseek/FTSeek/app/config.py 2>/dev/null; then
    check_fail "config.py 中存在默认 API Token (应删除)"
else
    check_pass "config.py 无默认 API Token"
fi

# 6. 检查日志文件权限
if [ -f /opt/ftseek/FTSeek/logs/ftseek.log ]; then
    LOG_PERMS=$(stat -c %a /opt/ftseek/FTSeek/logs/ftseek.log 2>/dev/null || echo "000")
    if [ "$LOG_PERMS" != "600" ] && [ "$LOG_PERMS" != "640" ]; then
        check_warn "日志文件权限过于开放：$LOG_PERMS (建议 600)"
    else
        check_pass "日志文件权限合理：$LOG_PERMS"
    fi
fi

# 7. 检查端口监听
if ss -tlnp | grep -q ':8899.*0.0.0.0'; then
    check_warn "API 端口 (8899) 监听在 0.0.0.0 (所有网卡)"
    echo -e "  ${YELLOW}建议:${NC} 如无需公网访问，改为监听 127.0.0.1"
elif ss -tlnp | grep -q ':8899.*127.0.0.1'; then
    check_pass "API 端口仅监听本地 (127.0.0.1)"
fi

# 8. 检查 .env 文件
if [ -f /opt/ftseek/FTSeek/.env ]; then
    ENV_PERMS=$(stat -c %a /opt/ftseek/FTSeek/.env 2>/dev/null || echo "000")
    if [ "$ENV_PERMS" != "600" ]; then
        check_warn ".env 文件权限过于开放：$ENV_PERMS (建议 600)"
    else
        check_pass ".env 文件权限合理：$ENV_PERMS"
    fi
else
    check_warn "未发现 .env 文件 (如果使用 systemd 可忽略)"
fi

# 9. 检查备份文件
if ls /opt/ftseek/FTSeek/app/*.bak 1>/dev/null 2>&1; then
    check_warn "发现 .bak 备份文件 (可能包含敏感信息)"
    ls -la /opt/ftseek/FTSeek/app/*.bak 2>/dev/null | head -5
fi

# 10. 检查 Python 版本
PYTHON_VERSION=$(python3.8 --version 2>&1 | awk '{print $2}')
echo -e "\n检查 Python 版本：$PYTHON_VERSION"
check_pass "Python 版本检查完成"

# ============================================================
#  总结
# ============================================================
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "检查结果汇总"
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}通过:${NC} $PASS"
echo -e "${RED}失败:${NC} $FAIL"
echo -e "${YELLOW}警告:${NC} $WARN"
echo ""

if [ $FAIL -gt 0 ]; then
    echo -e "${RED}⚠️  发现 $FAIL 个安全问题，请立即修复!${NC}"
    echo ""
    echo "修复建议:"
    echo "1. 生成 API Token: python3 -c 'import secrets; print(secrets.token_hex(32))'"
    echo "2. 编辑 /opt/ftseek/FTSeek/scripts/ftseek.service 配置 Token"
    echo "3. 重启服务：systemctl daemon-reload && systemctl restart ftseek"
    exit 1
elif [ $WARN -gt 0 ]; then
    echo -e "${YELLOW}⚠️  发现 $WARN 个警告，建议检查${NC}"
    exit 0
else
    echo -e "${GREEN}✅ 所有安全检查通过!${NC}"
    exit 0
fi
