#!/bin/bash
# FTSeek 钉钉回调配置脚本

echo "🔧 FTSeek 钉钉回调配置"
echo "━━━━━━━━━━━━━━━━━━━━━━"

# 配置文件路径
CONFIG_FILE="/opt/ftseek/FTSeek/.env"

echo ""
echo "📋 请按提示输入钉钉应用配置信息"
echo ""

# 读取现有配置
if [ -f "$CONFIG_FILE" ]; then
    echo "✅ 检测到现有配置文件"
    
    # 检查是否已有钉钉配置
    if grep -q "DINGTALK_APP_SECRET" "$CONFIG_FILE"; then
        echo "⚠️ 已存在钉钉应用配置"
        read -p "是否覆盖？(y/n): " overwrite
        if [ "$overwrite" != "y" ]; then
            echo "❌ 取消配置"
            exit 0
        fi
    fi
else
    echo "⚠️ 配置文件不存在，将创建新文件"
fi

echo ""
echo "📱 钉钉应用配置"
echo "━━━━━━━━━━━━━━━━━━━━━━"

# 输入钉钉应用密钥
read -p "请输入钉钉应用 Secret: " app_secret

if [ -z "$app_secret" ]; then
    echo "❌ Secret 不能为空"
    exit 1
fi

# 输入 Webhook 端口
read -p "请输入 Webhook 端口 (默认 5000): " webhook_port
webhook_port=${webhook_port:-5000}

echo ""
echo "📝 正在写入配置..."

# 追加配置到 .env 文件
cat >> "$CONFIG_FILE" << EOF

# ============================================================
#  钉钉回调配置
# ============================================================
DINGTALK_APP_SECRET=$app_secret
DINGTALK_WEBHOOK_PORT=$webhook_port
DINGTALK_CALLBACK_URL=/dingtalk/callback
EOF

echo "✅ 配置已写入 $CONFIG_FILE"

echo ""
echo "🔧 正在创建 systemd 服务..."

# 创建 Webhook 服务文件
cat > /etc/systemd/system/ftseek-callback.service << EOF
[Unit]
Description=FTSeek DingTalk Callback Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek
Environment="FLASK_APP=app.v4.risk.dingtalk_callback"
Environment="FLASK_ENV=production"
ExecStart=/usr/bin/python3.8 -m flask run --host=0.0.0.0 --port=$webhook_port
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "✅ 服务文件已创建"

echo ""
echo "🚀 正在启动服务..."

# 重载 systemd
systemctl daemon-reload

# 启动服务
systemctl enable ftseek-callback
systemctl start ftseek-callback

# 检查状态
sleep 2
systemctl status ftseek-callback --no-pager

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 钉钉回调配置完成！"
echo ""
echo "📊 服务状态:"
echo "  • 端口：$webhook_port"
echo "  • 回调 URL: http://YOUR_IP:$webhook_port/dingtalk/callback"
echo ""
echo "🔧 管理命令:"
echo "  • 查看状态：systemctl status ftseek-callback"
echo "  • 重启服务：systemctl restart ftseek-callback"
echo "  • 查看日志：journalctl -u ftseek-callback -f"
echo ""
echo "⚠️ 下一步:"
echo "  1. 在钉钉开发者后台配置回调 URL"
echo "  2. 配置服务器白名单"
echo "  3. 测试回调功能"
echo "━━━━━━━━━━━━━━━━━━━━━━"
