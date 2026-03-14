#!/bin/bash
# FTSeek 钉钉企业应用配置脚本

echo "🔧 FTSeek 钉钉企业应用配置"
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 配置文件路径
CONFIG_FILE="/opt/ftseek/FTSeek/.env"

echo "📋 请输入钉钉企业应用配置信息"
echo ""
echo "💡 获取方式:"
echo "1. 登录钉钉开发者后台：https://open-dev.dingtalk.com/"
echo "2. 创建企业内部应用"
echo "3. 在'凭证与基础信息'页面获取 AppKey 和 AppSecret"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 输入钉钉应用凭证
read -p "请输入 AppKey (ding 开头): " app_key
if [ -z "$app_key" ]; then
    echo "❌ AppKey 不能为空"
    exit 1
fi

read -p "请输入 AppSecret: " app_secret
if [ -z "$app_secret" ]; then
    echo "❌ AppSecret 不能为空"
    exit 1
fi

read -p "请输入 Agent ID (数字): " agent_id
if [ -z "$agent_id" ]; then
    echo "❌ Agent ID 不能为空"
    exit 1
fi

# 输入回调配置
echo ""
echo "📡 回调配置"
echo "━━━━━━━━━━━━━━━━━━━━━━"
read -p "请输入加密密钥 (AES Key, 可选): " aes_key
read -p "请输入 Token (可选): " token

# 更新配置文件
echo ""
echo "📝 正在更新配置..."

# 备份原配置
cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"

# 更新钉钉配置
sed -i "/^DINGTALK_APP_SECRET=/c\\DINGTALK_APP_SECRET=$app_secret" "$CONFIG_FILE"
sed -i "/^DINGTALK_WEBHOOK_PORT=/c\\DINGTALK_WEBHOOK_PORT=8900" "$CONFIG_FILE"
sed -i "/^DINGTALK_CALLBACK_URL=/c\\DINGTALK_CALLBACK_URL=/dingtalk/callback" "$CONFIG_FILE"

# 添加或更新企业应用配置
if grep -q "^DINGTALK_APP_KEY=" "$CONFIG_FILE"; then
    sed -i "/^DINGTALK_APP_KEY=/c\\DINGTALK_APP_KEY=$app_key" "$CONFIG_FILE"
else
    echo "DINGTALK_APP_KEY=$app_key" >> "$CONFIG_FILE"
fi

if grep -q "^DINGTALK_AGENT_ID=" "$CONFIG_FILE"; then
    sed -i "/^DINGTALK_AGENT_ID=/c\\DINGTALK_AGENT_ID=$agent_id" "$CONFIG_FILE"
else
    echo "DINGTALK_AGENT_ID=$agent_id" >> "$CONFIG_FILE"
fi

if [ -n "$aes_key" ]; then
    if grep -q "^DINGTALK_AES_KEY=" "$CONFIG_FILE"; then
        sed -i "/^DINGTALK_AES_KEY=/c\\DINGTALK_AES_KEY=$aes_key" "$CONFIG_FILE"
    else
        echo "DINGTALK_AES_KEY=$aes_key" >> "$CONFIG_FILE"
    fi
fi

if [ -n "$token" ]; then
    if grep -q "^DINGTALK_TOKEN=" "$CONFIG_FILE"; then
        sed -i "/^DINGTALK_TOKEN=/c\\DINGTALK_TOKEN=$token" "$CONFIG_FILE"
    else
        echo "DINGTALK_TOKEN=$token" >> "$CONFIG_FILE"
    fi
fi

# 添加推送模式配置
if grep -q "^DINGTALK_PUSH_MODE=" "$CONFIG_FILE"; then
    sed -i "/^DINGTALK_PUSH_MODE=/c\\DINGTALK_PUSH_MODE=enterprise" "$CONFIG_FILE"
else
    echo "DINGTALK_PUSH_MODE=enterprise" >> "$CONFIG_FILE"
fi

echo "✅ 配置已更新"

# 显示当前配置
echo ""
echo "📊 当前钉钉配置:"
echo "━━━━━━━━━━━━━━━━━━━━━━"
grep "^DINGTALK_" "$CONFIG_FILE" | grep -v "URL\|SECRET\|TOKEN\|KEY" || true
echo "DINGTALK_APP_KEY: ${app_key:0:12}..."
echo "DINGTALK_AGENT_ID: $agent_id"
echo "DINGTALK_PUSH_MODE: enterprise"
echo "━━━━━━━━━━━━━━━━━━━━━━"

# 重启服务
echo ""
echo "🚀 正在重启服务..."

pkill -f "trade_callback_server.py" || true
sleep 2

cd /opt/ftseek/FTSeek
nohup python3.8 trade_callback_server.py --port 8901 > /tmp/trade_callback_8901.log 2>&1 &

sleep 3

# 检查服务状态
if curl -s http://localhost:8901/health > /dev/null; then
    echo "✅ 回调服务已重启 (端口 8901)"
else
    echo "⚠️ 服务启动中，请稍候检查..."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 钉钉企业应用配置完成！"
echo ""
echo "📝 下一步:"
echo "1. 在钉钉开发者后台配置回调 URL:"
echo "   http://39.97.58.4:8900/dingtalk/callback"
echo ""
echo "2. 订阅事件:"
echo "   - btn_callback (按钮回调)"
echo "   - im_chat (消息接收)"
echo ""
echo "3. 配置服务器 IP 白名单:"
echo "   - 39.97.58.4"
echo "   - 127.0.0.1"
echo ""
echo "4. 测试推送和按钮回调功能"
echo ""
echo "🔧 管理命令:"
echo "  • 查看状态：curl http://localhost:8901/health"
echo "  • 查看日志：tail -f /tmp/trade_callback_8901.log"
echo "  • 重启服务：pkill -f trade_callback_server.py && cd /opt/ftseek/FTSeek && nohup python3.8 trade_callback_server.py --port 8901 > /tmp/trade_callback_8901.log 2>&1 &"
echo "━━━━━━━━━━━━━━━━━━━━━━"
