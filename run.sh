#!/bin/bash
# FTSeek 启动脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR"
PYTHON=${PYTHON:-python3.8}

echo "=========================================="
echo "  FTSeek - 富途自动化交易系统"
echo "=========================================="

# 检查 Python
if ! command -v $PYTHON &> /dev/null; then
    echo "错误: $PYTHON 未找到"
    exit 1
fi

# 检查依赖
$PYTHON -c "import futu" 2>/dev/null || {
    echo "安装依赖..."
    $PYTHON -m pip install -r "$APP_DIR/requirements.txt"
}

# 创建必要目录
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

# 启动
echo "启动 FTSeek..."
cd "$APP_DIR"
exec $PYTHON -m app.main
