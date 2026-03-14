#!/bin/bash
# ============================================================
#  FTSeek 安全配置一键脚本
#  用法：./setup_security.sh
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  FTSeek 安全配置向导${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

FTSEEK_DIR="/opt/ftseek/FTSeek"
SERVICE_FILE="$FTSEEK_DIR/scripts/ftseek.service"

# 1. 生成 API Token
echo -e "${YELLOW}步骤 1: 生成 API Token${NC}"
API_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "已生成安全 Token: ${API_TOKEN:0:16}..."
echo ""

# 2. 获取钉钉配置
echo -e "${YELLOW}步骤 2: 配置钉钉推送 (可选)${NC}"
read -p "是否配置钉钉推送？(y/n): " CONFIG_DINGTALK
if [ "$CONFIG_DINGTALK" == "y" ] || [ "$CONFIG_DINGTALK" == "Y" ]; then
    read -p "请输入钉钉 Webhook URL: " DINGTALK_URL
    read -p "请输入钉钉 Secret (以 SEC 开头): " DINGTALK_SECRET
else
    DINGTALK_URL=""
    DINGTALK_SECRET=""
fi
echo ""

# 3. 配置交易环境
echo -e "${YELLOW}步骤 3: 配置交易环境${NC}"
echo "  1) 模拟环境 (推荐，安全)"
echo "  2) 真实环境 (仅在策略充分测试后使用)"
read -p "请选择 (1/2): " TRADE_ENV_CHOICE
if [ "$TRADE_ENV_CHOICE" == "2" ]; then
    TRADE_ENV=0
    echo -e "${RED}⚠️  警告：已选择真实交易环境，请确保策略已充分测试!${NC}"
else
    TRADE_ENV=1
    echo -e "${GREEN}已选择模拟环境 (安全)${NC}"
fi
echo ""

# 4. 更新 systemd 服务文件
echo -e "${YELLOW}步骤 4: 更新 systemd 服务配置${NC}"

# 备份原文件
if [ -f "$SERVICE_FILE" ]; then
    cp "$SERVICE_FILE" "${SERVICE_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    echo "已备份原配置文件"
fi

# 替换配置
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=FTSeek 富途自动化交易系统
After=network.target
Wants=opend.service

[Service]
Type=simple
User=root
WorkingDirectory=$FTSEEK_DIR

# 安全配置
Environment="API_TOKEN=$API_TOKEN"
Environment="TRADE_ENV=$TRADE_ENV"
Environment="CALLBACK_EXTERNAL_URL=http://aitz.cc:8899"
Environment="FTSEEK_DEV_MODE=false"

EOF

# 添加钉钉配置 (如果用户选择配置)
if [ -n "$DINGTALK_URL" ]; then
    cat >> "$SERVICE_FILE" << EOF
# 钉钉推送
Environment="DINGTALK_WEBHOOK_URL=$DINGTALK_URL"
Environment="DINGTALK_SECRET=$DINGTALK_SECRET"

EOF
fi

# 添加其他配置
cat >> "$SERVICE_FILE" << EOF
# 可选配置
Environment="OPEND_HOST=127.0.0.1"
Environment="OPEND_PORT=11111"
Environment="CALLBACK_HOST=0.0.0.0"
Environment="CALLBACK_PORT=8899"
Environment="LOG_LEVEL=INFO"

# 监控标的
Environment="WATCH_LIST_HK=[\"HK.00700\",\"HK.09988\",\"HK.09992\"]"
Environment="WATCH_LIST_US=[\"US.AAPL\",\"US.TSLA\",\"US.NVDA\",\"US.MSFT\",\"US.AMZN\"]"

# 执行命令
ExecStart=/usr/bin/python3.8 -m app.main
Restart=on-failure
RestartSec=10

# 日志
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ftseek

# 安全增强
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$FTSEEK_DIR/logs $FTSEEK_DIR/data

LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}✅ 服务配置已更新${NC}"
echo ""

# 5. 创建 .env 文件 (备份用)
echo -e "${YELLOW}步骤 5: 创建 .env 备份文件${NC}"
cat > "$FTSEEK_DIR/.env" << EOF
# FTSeek 环境变量配置 (备份用)
# 生成时间：$(date)
# ⚠️ 请妥善保管此文件，不要泄露给他人

API_TOKEN=$API_TOKEN
TRADE_ENV=$TRADE_ENV
CALLBACK_EXTERNAL_URL=http://aitz.cc:8899
FTSEEK_DEV_MODE=false
EOF

if [ -n "$DINGTALK_URL" ]; then
    cat >> "$FTSEEK_DIR/.env" << EOF
DINGTALK_WEBHOOK_URL=$DINGTALK_URL
DINGTALK_SECRET=$DINGTALK_SECRET
EOF
fi

chmod 600 "$FTSEEK_DIR/.env"
echo -e "${GREEN}✅ .env 文件已创建 (权限 600)${NC}"
echo ""

# 6. 安装 systemd 服务
echo -e "${YELLOW}步骤 6: 安装 systemd 服务${NC}"
cp "$SERVICE_FILE" /etc/systemd/system/ftseek.service
systemctl daemon-reload
echo -e "${GREEN}✅ systemd 服务已安装${NC}"
echo ""

# 7. 完成提示
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  配置完成!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "下一步操作:"
echo ""
echo -e "  ${YELLOW}1. 启动服务:${NC}"
echo "     systemctl start ftseek"
echo ""
echo -e "  ${YELLOW}2. 查看状态:${NC}"
echo "     systemctl status ftseek"
echo ""
echo -e "  ${YELLOW}3. 查看日志:${NC}"
echo "     journalctl -u ftseek -f"
echo ""
echo -e "  ${YELLOW}4. 运行安全检查:${NC}"
echo "     $FTSEEK_DIR/scripts/security_check.sh"
echo ""
echo -e "${RED}⚠️  重要提示:${NC}"
echo "  - 已生成 .env 文件包含敏感信息，请妥善保管"
echo "  - 建议将 .env 文件备份到安全位置后删除"
echo "  - 定期检查日志是否有异常访问"
echo ""
echo -e "${GREEN}祝交易顺利! 📈${NC}"
