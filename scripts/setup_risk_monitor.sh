#!/bin/bash
# FTSeek 风险监控安装配置脚本
# 功能：配置定时任务、验证安装、测试推送

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="/usr/bin/python3"

echo "============================================================"
echo "FTSeek 风险监控安装配置"
echo "============================================================"
echo "项目目录：$PROJECT_DIR"
echo "脚本目录：$SCRIPT_DIR"
echo ""

# 1. 检查环境变量
echo "1. 检查环境变量..."
if [ -z "$API_TOKEN" ]; then
    echo "⚠️  警告：API_TOKEN 未设置"
    echo "   请执行：export API_TOKEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
else
    echo "✅ API_TOKEN 已设置"
fi

if [ -z "$DINGTALK_WEBHOOK_URL" ]; then
    echo "⚠️  警告：DINGTALK_WEBHOOK_URL 未设置"
    echo "   请在钉钉群机器人中获取 Webhook URL"
else
    echo "✅ DINGTALK_WEBHOOK_URL 已设置"
fi

if [ -z "$DINGTALK_SECRET" ]; then
    echo "⚠️  警告：DINGTALK_SECRET 未设置"
    echo "   请在钉钉群机器人中获取加签密钥"
else
    echo "✅ DINGTALK_SECRET 已设置"
fi
echo ""

# 2. 测试脚本运行
echo "2. 测试脚本运行..."
cd "$PROJECT_DIR"
if $PYTHON scripts/risk_monitor.py --test 2>&1 | grep -q "账户总值"; then
    echo "✅ 脚本运行测试通过"
else
    echo "❌ 脚本运行测试失败"
    exit 1
fi
echo ""

# 3. 运行单元测试
echo "3. 运行单元测试..."
if $PYTHON scripts/test_risk_monitor.py 2>&1 | grep -q "所有测试通过"; then
    echo "✅ 单元测试通过"
else
    echo "❌ 单元测试失败"
    exit 1
fi
echo ""

# 4. 配置 crontab
echo "4. 配置 crontab 定时任务..."

CRON_ENTRY="0 10,12,14,16 * * 1-5 cd $PROJECT_DIR && $PYTHON $SCRIPT_DIR/risk_monitor.py >> $PROJECT_DIR/logs/risk_monitor.log 2>&1"

# 检查 crontab 是否已存在
if crontab -l 2>/dev/null | grep -q "risk_monitor.py"; then
    echo "✅ crontab 已配置"
    crontab -l | grep "risk_monitor.py"
else
    echo "正在添加 crontab 条目..."
    (crontab -l 2>/dev/null | grep -v "^#" ; echo "$CRON_ENTRY") | crontab -
    echo "✅ crontab 已添加:"
    echo "   $CRON_ENTRY"
fi
echo ""

# 5. 创建日志目录
echo "5. 创建日志目录..."
mkdir -p "$PROJECT_DIR/logs"
touch "$PROJECT_DIR/logs/risk_monitor.log"
echo "✅ 日志目录已创建：$PROJECT_DIR/logs/risk_monitor.log"
echo ""

# 6. 验证配置
echo "6. 验证配置..."
echo "查看当前 crontab 配置:"
crontab -l | grep "risk_monitor" || echo "无配置"
echo ""

echo "============================================================"
echo "安装配置完成!"
echo "============================================================"
echo ""
echo "推送时间：周一至周五 10:00, 12:00, 14:00, 16:00"
echo "日志文件：$PROJECT_DIR/logs/risk_monitor.log"
echo ""
echo "手动测试命令:"
echo "  cd $PROJECT_DIR && $PYTHON $SCRIPT_DIR/risk_monitor.py --test"
echo "  cd $PROJECT_DIR && $PYTHON $SCRIPT_DIR/risk_monitor.py --force"
echo ""
echo "查看日志:"
echo "  tail -f $PROJECT_DIR/logs/risk_monitor.log"
echo ""
echo "修改配置:"
echo "  编辑 $PROJECT_DIR/config/risk_config.yaml"
echo ""
echo "============================================================"
