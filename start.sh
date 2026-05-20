#!/bin/bash
# FTSeek-US 启动脚本

set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

echo "========================================"
echo "  FTSeek-US 美股交易平台"
echo "========================================"

cd "$PROJECT_DIR"

# 检查虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境..."
    python3.8 -m venv "$VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 安装依赖
if [ -f "requirements.txt" ]; then
    echo "检查依赖..."
    $PIP install -q -r requirements.txt
fi

# 检查环境配置
if [ ! -f ".env.us" ]; then
    echo "⚠️  未找到 .env.us 配置文件"
    echo "   请复制 .env.us.example 并配置:"
    echo "   cp .env.us.example .env.us"
    echo "   vim .env.us"
    exit 1
fi

# 检查 API Token
if ! grep -q "FTSEEK_US_API_TOKEN=" .env.us; then
    echo "⚠️  未设置 FTSEEK_US_API_TOKEN"
    exit 1
fi

# 创建必要目录
mkdir -p logs data

# 启动服务
echo "启动 FTSeek-US 服务..."
echo "API: http://localhost:8901"
echo "日志：logs/ftseek_us.log"
echo ""

$PYTHON -m app.main
